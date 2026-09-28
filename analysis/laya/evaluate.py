#!/usr/bin/env python3
"""Replay source-attributed E2E failure evidence through the pinned Laya schema."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "https://laya-server-laya.apps.cnv2.engineering.redhat.com/predict/batch"

CRITERIA = {
    "aap_policy": (
        "AAP controller rejects instance_group creation or pod_spec_override policy, even if its error mentions "
        "Kubernetes secrets."
    ),
    "build_dependency": (
        "Container or app build fails due to dependency resolution, module path mismatch, or package export mismatch."
    ),
    "build_compile": "Go or TypeScript compiler reports a source error such as undefined symbol or type error.",
    "source_checkout": (
        "Git fetch/checkout fails, a requested ref is absent, or a required source/build context directory is missing."
    ),
    "deployment_config": (
        "Kubernetes resource, operator, or controller configuration prevents installation or readiness; includes "
        "missing secret, bad auth endpoint, or operator timeout."
    ),
    "ci_environment": (
        "CI authorization or runner setup blocks execution before build/install/test, such as fork approval."
    ),
    "app_startup": (
        "Deployed application process fails to start due to an application error, such as duplicate database migration."
    ),
    "app_or_e2e": (
        "An E2E assertion, API call, provisioned resource, metering event, lifecycle, or teardown fails "
        "after installation."
    ),
    "unknown": "Only an uninformative symptom is shown and no direct failing component can be determined.",
}

GLOBAL_QUESTION = {
    "failure_domain": {
        "type": "choice",
        "instructions": (
            "Choose the first directly failing component shown by the evidence. Failed AAP bootstrap is aap_policy, "
            "not Kubernetes config. A failed AAP job that later succeeds is not the cause. "
            "Use unknown if only a generic install timeout is shown."
        ),
        "criteria": CRITERIA,
    }
}


def stage_question(stage: str) -> dict[str, Any]:
    if stage == "build":
        choices = ("build_dependency", "build_compile", "source_checkout", "unknown")
        instructions = "Classify the direct error in the build or source stage."
    else:
        choices = ("aap_policy", "deployment_config", "app_startup", "unknown")
        instructions = (
            "Classify the direct install failure. AAP instance_group API rejection is aap_policy even if "
            "Kubernetes secrets appear in the message. Use unknown for a generic timeout without direct evidence."
        )
    return {
        "failure_domain": {
            "type": "choice",
            "instructions": instructions,
            "criteria": {k: CRITERIA[k] for k in choices},
        }
    }


def compact_state(state: dict[str, str]) -> dict[str, str]:
    out = {"failed_step": state["failed_step"]}
    for key in ("job_error", "pod_error", "traceback"):
        lines = [re.sub(r"^[^:]+:\d+:\s*", "", line) for line in state[key].splitlines()]
        out[key] = " | ".join(lines)[: 340 if key == "pod_error" else 280]
    return out


def state_for_arm(state: dict[str, str], arm: str) -> dict[str, str]:
    if arm == "short":
        return {"failed_step": state["failed_step"], "job_error": state["job_error"][:360]}
    if arm == "compact":
        return compact_state(state)
    return state


def classify_stage(step: str) -> str:
    if "Run E2E" in step:
        return "e2e"
    if "Authorize" in step:
        return "ci"
    if "Build" in step:
        return "build"
    return "install"


def predict_batch(
    items: list[tuple[dict[str, Any], dict[str, str]]], question: dict[str, Any], endpoint: str
) -> tuple[float, list[dict[str, Any]]]:
    payload = {"states": [state for _, state in items], "questions": question, "model": "english"}
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    elapsed = time.monotonic() - started
    results: list[dict[str, Any]] = body["results"]
    if len(results) != len(items):
        raise ValueError(f"Expected {len(items)} Laya results, received {len(results)}")
    return elapsed, results


def evaluate(cases: list[dict[str, Any]], arm: str, endpoint: str) -> dict[str, Any]:
    groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for case in cases:
        stage = classify_stage(case["state"]["failed_step"]) if arm == "stage" else "global"
        groups.setdefault(stage, []).append((case, state_for_arm(case["state"], arm)))

    predictions: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for stage, items in groups.items():
        if arm == "stage" and stage in ("e2e", "ci"):
            choice = "app_or_e2e" if stage == "e2e" else "ci_environment"
            predictions.extend(
                {"pr": case["pr"], "choice": choice, "score": 1.0, "tokens": 0, "method": "deterministic_step"}
                for case, _ in items
            )
            continue
        question = stage_question(stage) if arm == "stage" else GLOBAL_QUESTION
        for offset in range(0, len(items), 5):
            batch = items[offset : offset + 5]
            elapsed, results = predict_batch(batch, question, endpoint)
            batches.append({"stage": stage, "prs": [case["pr"] for case, _ in batch], "seconds": round(elapsed, 3)})
            for (case, _), result in zip(batch, results, strict=True):
                answer = result["answers"]["failure_domain"]
                predictions.append(
                    {
                        "pr": case["pr"],
                        "choice": answer["choice"],
                        "score": answer["answer_confidence"],
                        "probabilities": answer["probabilities"],
                        "tokens": result["usage"]["input_tokens"],
                        "method": "laya",
                    }
                )
            print(f"{stage}: {offset + len(batch)}/{len(items)} in {elapsed:.2f}s", flush=True)
    return {"arm": arm, "predictions": predictions, "batches": batches}


def summarize(cases: list[dict[str, Any]], result: dict[str, Any]) -> str:
    by_pr = {prediction["pr"]: prediction for prediction in result["predictions"]}
    if len(by_pr) != len(cases):
        raise ValueError("Predictions do not match the number of cases")
    labels = list(CRITERIA)
    matrix = Counter((case["label"], by_pr[case["pr"]]["choice"]) for case in cases)
    correct = sum(matrix[label, label] for label in labels)
    lines = [f"# {result['arm']} evaluation", "", f"Correct: {correct}/{len(cases)}", ""]
    lines.extend(("| Actual \\ Predicted | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)))
    for actual in labels:
        lines.append("| " + actual + " | " + " | ".join(str(matrix[actual, predicted]) for predicted in labels) + " |")
    lines.extend(("", "| Actual | TP | FP | FN | Abstentions |", "|---|---:|---:|---:|---:|"))
    for label in labels:
        tp = matrix[label, label]
        fp = sum(matrix[actual, label] for actual in labels if actual != label)
        fn = sum(matrix[label, predicted] for predicted in labels if predicted != label)
        lines.append(f"| {label} | {tp} | {fp} | {fn} | {matrix[label, 'unknown']} |")
    wrong = [
        (case["pr"], case["label"], by_pr[case["pr"]]["choice"], by_pr[case["pr"]]["score"])
        for case in cases
        if case["label"] != by_pr[case["pr"]]["choice"]
    ]
    lines.extend(("", "Wrong routes:", ""))
    lines.extend(f"- PR #{pr}: {actual} → {predicted} ({score:.3f})" for pr, actual, predicted, score in wrong)
    latencies = [batch["seconds"] for batch in result["batches"]]
    if latencies:
        lines.extend(("", f"Batch latency: median {statistics.median(latencies):.2f}s, max {max(latencies):.2f}s."))
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--split", choices=("development", "holdout"), required=True)
    parser.add_argument("--arm", choices=("short", "direct", "compact", "stage"), required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [case for case in json.loads(args.cases.read_text()) if case["split"] == args.split]
    result = evaluate(cases, args.arm, args.endpoint)
    result["split"] = args.split
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(summarize(cases, result))


if __name__ == "__main__":
    main()
