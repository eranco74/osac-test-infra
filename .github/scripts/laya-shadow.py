#!/usr/bin/env python3
"""Bounded, redacted Laya shadow route for a failed E2E job.

The result is diagnostic metadata only. This module never decides whether to
call Vertex or whether to publish a PR comment/status.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "analysis/laya/schema.json").read_text())
DEFAULT_ENDPOINT = "https://laya-server-laya.apps.cnv2.engineering.redhat.com/predict"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_FILES = 300
FIELD_LIMITS = {"failed_step": 80, "job_error": 350, "pod_error": 500, "traceback": 250}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*")
SENSITIVE = (
    (re.compile(r"(?i)Bearer\s+[^\s\"']+"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b"), "[REDACTED_TOKEN]"),
    (
        re.compile(
            r"(?i)\b(?:password|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[^\s,\"']+"
        ),
        "[REDACTED_SECRET]",
    ),
    (
        re.compile(
            r"(?i)[\"']?(?:password|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token)[\"']?\s*:\s*[\"'][^\"']+[\"']"
        ),
        "[REDACTED_SECRET]",
    ),
    (re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@[^\s\"']+"), "[REDACTED_URL]"),
    (re.compile(r"https?://[^\s\"']+"), "[REDACTED_URL]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED_IP]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", re.I), "[REDACTED_UUID]"),
    (re.compile(r"ssh-ed25519\s+[A-Za-z0-9+/=]+"), "ssh-ed25519 [REDACTED_KEY]"),
)
BUILD_SIGNAL = re.compile(
    r"go: module |module declares its path|but was required as|undefined: |error TS\d+|"
    r"ERROR: Cannot install|ResolutionImpossible|not exported under the conditions|"
    r"context must be a directory|fatal: couldn.t find remote ref|could not be found on container",
    re.I,
)
TEST_SIGNAL = re.compile(
    r"\bE\s+(?:AssertionError|TimeoutError|ValueError|ExceptionGroup)|"
    r"stderr\s*=.*(?:ERROR:|Error:|failed to|FailedPrecondition)",
    re.I,
)
INSTALL_TIMEOUT = re.compile(
    r"Error: context deadline exceeded|Error: failed post-install|timed out waiting for the condition", re.I
)


def redact(value: str) -> str:
    """Remove credential-shaped content before it can enter state or JSON."""
    if "PRIVATE KEY-----" in value:
        return "[REDACTED_PRIVATE_KEY]"
    text = TIMESTAMP.sub("", ANSI.sub("", value.strip()))
    for pattern, replacement in SENSITIVE:
        text = pattern.sub(replacement, text)
    return text


def _safe_file(path: Path, root: Path | None = None) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return False
        if root is not None:
            path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _read_lines(path: Path, root: Path | None = None) -> list[str]:
    if not _safe_file(path, root):
        return []
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _add(candidates: list[tuple[int, str, str]], priority: int, source: str, line_number: int, message: str) -> None:
    clean = redact(message)
    if clean:
        candidates.append((priority, f"{source}:{line_number}", clean[:300]))


def _format(candidates: list[tuple[int, str, str]], limit: int, count: int = 2) -> tuple[str, list[str], bool]:
    selected: list[str] = []
    refs: list[str] = []
    keys: set[str] = set()
    direct = False
    for priority, ref, message in sorted(candidates, key=lambda row: -row[0]):
        key = re.sub(r"\d+", "#", message)
        if key in keys:
            continue
        keys.add(key)
        line = f"{ref}: {message}"
        if selected and len("\n".join(selected)) + len(line) + 1 > limit:
            continue
        selected.append(line[:limit])
        refs.append(ref)
        direct |= priority >= 70
        if len(selected) >= count:
            break
    return "\n".join(selected)[:limit], refs, direct


def _job_candidates(path: Path, stage: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    for number, line in enumerate(_read_lines(path), 1):
        line = TIMESTAMP.sub("", ANSI.sub("", line))
        if stage == "build" and BUILD_SIGNAL.search(line):
            priority = 90 if not re.search(r"ResolutionImpossible", line) else 50
            _add(candidates, priority, "job.log", number, line)
        elif stage == "ci" and line.startswith("##[error]") and ("Fork PR blocked:" in line or "/ok-to-test" in line):
            _add(candidates, 95, "job.log", number, line)
        elif stage == "e2e" and TEST_SIGNAL.search(line):
            _add(candidates, 90, "job.log", number, line)
        elif stage == "install" and INSTALL_TIMEOUT.search(line):
            _add(candidates, 20, "job.log", number, line)
    return candidates


def _junit_candidates(path: Path, root: Path) -> list[tuple[int, str, str]]:
    if not _safe_file(path, root):
        return []
    try:
        document = ET.parse(path)
    except (OSError, ET.ParseError):
        return []
    candidates: list[tuple[int, str, str]] = []
    for testcase in document.getroot().iter("testcase"):
        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        if failure is None:
            continue
        name = re.sub(r"[^A-Za-z0-9_.\[\]-]", "_", testcase.get("name", "unknown"))[:90]
        text = failure.text or ""
        errors = [
            line.strip() for line in text.splitlines() if re.search(r"^\s*(?:E\s+|\|\s+).*(?:Error:|Exception:)", line)
        ]
        message = errors[-1] if errors else (failure.get("message") or "")
        if "subprocess.CalledProcessError: Command" in message:
            message = "subprocess.CalledProcessError in " + name
        _add(candidates, 80, f"junit.xml/{name}", 1, message)
        if len(candidates) >= 5:
            break
    return candidates


def _pod_message(line: str) -> str:
    """Keep the message, not a JSON log's stack, request, or event payload."""
    if "FAILED! =>" in line:
        line = line.split("FAILED! =>", 1)[1].strip()
    if line.lstrip().startswith("{"):
        try:
            obj = json.loads(line)
            error = obj.get("error") or {}
            detail = error.get("message", "") if isinstance(error, dict) else ""
            return f"{obj.get('msg', '')}: {detail}".strip(": ")
        except (ValueError, TypeError, AttributeError):
            pass
    return line


def _artifact_candidates(root: Path) -> list[tuple[int, str, str]]:
    if not root.is_dir() or root.is_symlink():
        return []
    try:
        paths = sorted(root.iterdir())
    except OSError:
        return []
    candidates: list[tuple[int, str, str]] = []
    consumed = 0
    relevant = [
        path
        for path in paths
        if path.name in ("events.txt", "pods-describe.txt")
        or (path.name.startswith("pod-osac-aap-bootstrap-") and path.name.endswith(".log"))
        or (path.name.startswith("pod-fulfillment-grpc-server-") and path.name.endswith(".log"))
        or (path.name.startswith("pod-fulfillment-controller-") and path.name.endswith(".log"))
    ]
    for path in relevant[:MAX_ARTIFACT_FILES]:
        name = path.name
        if not _safe_file(path, root) or consumed + path.stat().st_size > MAX_ARTIFACT_BYTES:
            continue
        consumed += path.stat().st_size
        for number, line in enumerate(_read_lines(path, root), 1):
            if (
                "Unable to create instance_group" in line and "pod_spec_override" in line
            ) or "duplicate migration file" in line:
                _add(candidates, 100, name, number, _pod_message(line))
            elif "Failed to send token form" in line and "unsupported protocol scheme" in line:
                _add(candidates, 95, name, number, _pod_message(line))
            elif name == "events.txt" and (
                ("FailedCreate" in line and "job/osac-copy-fulfillment-kafka" in line)
                or ("FailedMount" in line and "references non-existent secret key" in line)
            ):
                _add(candidates, 95, name, number, line)
    aap_dir = root / "aap-jobs"
    if aap_dir.is_dir() and not aap_dir.is_symlink():
        try:
            aap_paths = sorted(aap_dir.iterdir())
        except OSError:
            aap_paths = []
        aap_paths = [path for path in aap_paths if re.fullmatch(r"project-update-\d+-failed\.txt", path.name)]
        for path in aap_paths[:MAX_ARTIFACT_FILES]:
            if not _safe_file(path, root) or consumed + path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            consumed += path.stat().st_size
            for number, line in enumerate(_read_lines(path, root), 1):
                if "Failed to checkout" not in line and "unable to read tree" not in line:
                    continue
                if "FAILED! =>" in line:
                    try:
                        item = json.loads(line.split("FAILED! =>", 1)[1])
                        line = f"{item.get('msg', '')}; {item.get('stderr', '')}"
                    except (ValueError, TypeError, AttributeError):
                        pass
                _add(candidates, 95, f"aap-jobs/{path.name}", number, line)
    return candidates


def extract_state(artifact_dir: Path, junit_path: Path, job_log_path: Path, failed_step: str) -> dict[str, Any]:
    """Select direct evidence using one bounded schema for replay and CI."""
    step = redact(failed_step)[: FIELD_LIMITS["failed_step"]]
    if "Run E2E" in step:
        stage = "e2e"
    elif "Authorize" in step:
        stage = "ci"
    elif "Build" in step:
        stage = "build"
    else:
        stage = "install"
    job, job_refs, job_direct = _format(_job_candidates(job_log_path, stage), FIELD_LIMITS["job_error"])
    pod_candidates = _artifact_candidates(artifact_dir) if stage == "install" else []
    pod, pod_refs, pod_direct = _format(pod_candidates, FIELD_LIMITS["pod_error"])
    trace_candidates = _junit_candidates(junit_path, artifact_dir) if stage == "e2e" else []
    traceback, trace_refs, trace_direct = _format(trace_candidates, FIELD_LIMITS["traceback"])
    state = {"failed_step": step, "job_error": job, "pod_error": pod, "traceback": traceback}
    assert sum(len(value) for value in state.values()) <= sum(FIELD_LIMITS.values())
    return {
        "state": state,
        "stage": stage,
        "evidence_available": job_direct or pod_direct or trace_direct,
        "evidence_refs": list(dict.fromkeys(job_refs + pod_refs + trace_refs))[:6],
    }


def _parse_answer(response: dict[str, Any], stage: str) -> tuple[str, float, float, str, int]:
    answer = response["answers"]["failure_domain"]
    criteria = SCHEMA["questions"][stage]["failure_domain"]["criteria"]
    choice = answer["choice"]
    probabilities = answer["probabilities"]
    if answer.get("type") != "choice" or choice not in criteria or set(probabilities) != set(criteria):
        raise ValueError("invalid choice response")
    values = [float(probabilities[label]) for label in criteria]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise ValueError("invalid probabilities")
    if abs(sum(values) - 1.0) > 0.02:
        raise ValueError("probabilities do not sum to one")
    score = float(answer["answer_confidence"])
    if not math.isfinite(score) or abs(score - float(probabilities[choice])) > 0.01:
        raise ValueError("choice score mismatch")
    runner_up = max(float(value) for label, value in probabilities.items() if label != choice)
    model = response["routing"]["model"]
    tokens = response["usage"]["input_tokens"]
    if not isinstance(model, str) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("invalid metadata")
    return choice, score, runner_up, model, tokens


def shadow_classify(evidence: dict[str, Any], endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> dict[str, Any]:
    """Return metadata only; every service failure becomes an abstention."""
    stage = evidence["stage"]
    result = {
        "schema_version": SCHEMA["version"],
        "mode": "shadow",
        "method": "failed_step" if stage in SCHEMA["direct_step_routes"] else "laya",
        "route": "unknown",
        "model": None,
        "score": None,
        "runner_up_score": None,
        "latency_ms": None,
        "input_tokens": None,
        "evidence_available": evidence["evidence_available"],
        "evidence_refs": evidence["evidence_refs"],
        "state": evidence["state"],
    }
    if stage in SCHEMA["direct_step_routes"]:
        result["route"] = SCHEMA["direct_step_routes"][stage]
        return result
    payload = {"state": evidence["state"], "questions": SCHEMA["questions"][stage], "model": SCHEMA["model"]}
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        route, score, runner_up, model, tokens = _parse_answer(body, stage)
        result.update(route=route, score=score, runner_up_score=runner_up, model=model, input_tokens=tokens)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        result.update(method="error", error=type(exc).__name__)
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main() -> None:
    artifact_dir = Path(os.environ.get("ARTIFACT_DIR") or "/nonexistent")
    evidence = extract_state(
        artifact_dir,
        Path(os.environ.get("JUNIT_PATH") or "/nonexistent"),
        Path(os.environ.get("JOB_LOG_PATH") or "/nonexistent"),
        os.environ.get("FAILED_STEP_NAME", ""),
    )
    enabled = os.environ.get("LAYA_SHADOW_ENABLED", "true").lower() in {"true", "1", "yes"}
    if enabled:
        result = shadow_classify(evidence, os.environ.get("LAYA_ENDPOINT") or DEFAULT_ENDPOINT)
    else:
        result = {
            "schema_version": SCHEMA["version"],
            "mode": "disabled",
            "method": "disabled",
            "route": "unknown",
            "evidence_available": evidence["evidence_available"],
            "evidence_refs": evidence["evidence_refs"],
            "state": evidence["state"],
        }
    result["run"] = {
        "id": os.environ.get("FAILED_RUN_ID", ""),
        "url": os.environ.get("FAILED_RUN_URL", ""),
        "pr": os.environ.get("PR_NUMBER", ""),
        "workflow": os.environ.get("WORKFLOW_NAME", ""),
        "head_sha": os.environ.get("HEAD_SHA", ""),
    }
    output = os.environ.get("LAYA_SHADOW_FILE", "")
    if output:
        temp = f"{output}.tmp-{os.getpid()}"
        try:
            with open(temp, "w") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
            os.replace(temp, output)
        except OSError:
            with suppress(OSError):
                os.remove(temp)
            raise
    print(f"Laya shadow: {result['method']} route={result['route']} evidence={result['evidence_available']}")


if __name__ == "__main__":
    main()
