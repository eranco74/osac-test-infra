# OSAC-5641: Laya routing evaluation

Observed 2026-09-28 against the pinned `english` checkpoint of the internal
Laya server. This is a retrospective replay, not live shadow capture. The
exact questions, selected evidence states, root groups, and labels are in
[`evaluate.py`](evaluate.py) and [`cases.json`](cases.json).

## What was evaluated

The corpus contains 59 distinct failed E2E workflow runs on 59 PRs. The
original 25 formed the starting development set; 34 runs from other PRs added
build, install, authorization, and test failures. After grouping the repeated
AAP, fork approval, missing metering, and TypeScript failures, 37 runs remained
in development and 22 were held out. None of the 29 observed root groups
crosses the split. The holdout has no AAP policy or CI authorization root.
Selection was purposeful rather than random, so these fractions do not predict
production prevalence.

The same four fields (`failed_step`, `job_error`, `pod_error`, `traceback`) and
the same source precedence were used for both splits. The excerpts were
reviewed against the failed step, job log, and available artifacts. In
particular, a later successful AAP retry was not labeled as a cause, and
artifact-gather errors after a failed fork gate were not labeled as the cause.
The extraction was prepared for this replay and is not yet the production
extractor from OSAC-5639.

## Development comparison and held-out result

| Route | Development | Holdout | Laya calls on holdout |
|---|---:|---:|---:|
| Global nine-choice question, failed step and job error only | 7/37 | not run | — |
| Global nine-choice question, four source fields | 7/37 | 7/22 | 22 |
| Global nine-choice question, compact source fields | 6/37 | not run | — |
| Failed-step route, four-choice Laya question within build/install | **32/37** | **17/22** | **11** |

The short and compact inputs did not improve the global question. The
stage-constrained route improved the development AAP result to 20/20, but its
top choice probabilities were only 0.319–0.351. All 20 share the same AAP
instance-group policy rejection, so they are not 20 independent causes.

The held-out 17/22 includes **11/11 E2E cases routed directly from the failed
step**. On the 11 held-out build/install cases that called Laya, it was
**6/11**. This is useful for coarse triage, not for a diagnostic bypass.

## Held-out confusion matrix

Rows are reviewed routes and columns are predictions from the selected
stage-constrained route. `unknown` is an abstention.

| Actual \ Predicted | AAP | Dependency | Compile | Source | Deploy | CI | App startup | E2E | Unknown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| AAP policy | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Build dependency | 0 | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Build compile | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Source checkout | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 2 |
| Deployment config | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| CI environment | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| App startup | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| E2E / app behavior | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 0 |
| Unknown | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |

| Class | True positives | False positives | False negatives | Abstentions on that class |
|---|---:|---:|---:|---:|
| AAP policy | 0 | 0 | 0 | 0 |
| Build dependency | 2 | 0 | 2 | 0 |
| Build compile | 1 | 2 | 0 | 0 |
| Source checkout | 1 | 0 | 2 | 2 |
| Deployment config | 0 | 0 | 1 | 1 |
| CI environment | 0 | 0 | 0 | 0 |
| App startup | 1 | 0 | 0 | 0 |
| E2E / app behavior | 11 | 0 | 0 | 0 |
| Unknown | 1 | 3 | 0 | 1 |

The wrong-class predictions are:

- [PR #1269](https://github.com/osac-project/osac/actions/runs/36361314336): Go dependency declares a different module path; Laya chose `build_compile` at 0.409.
- [PR #1126](https://github.com/osac-project/osac/actions/runs/36305767053): the installed `@novnc/novnc` package does not export the requested path; Laya chose `build_compile` at 0.417.
- [PR #1093](https://github.com/osac-project/osac/actions/runs/35757502813) and [PR #836](https://github.com/osac-project/osac/actions/runs/35602675588): the `osac-ui` build context directory is absent; Laya abstained at 0.376 and 0.370.
- [PR #1240](https://github.com/osac-project/osac/actions/runs/36208878598): the controller cannot obtain a token because its endpoint is empty; Laya abstained at 0.361.

The correct abstention is [PR #1233](https://github.com/osac-project/osac/actions/runs/36177191564): the operator install timed out, but the available excerpt did not show a supported upstream cause. The duplicate migration in [PR #1166](https://github.com/osac-project/osac/actions/runs/36188200390) was correctly routed to app startup at 0.633; its UI crashes and Helm timeout were downstream.

## Evidence, latency, and bypass potential

- All 59 runs had a failed-job log. Five had no diagnostic artifact: four failed at fork authorization and one at Git-ref fetch, before OSAC installation. The failed step supplied direct evidence in those five. The upstream cause for #1233 remains unverified; several E2E timeout labels identify the failing operation but not the deeper product cause.
- On the 11 held-out requests that used Laya, single-request `/predict` latency was median **0.572 s**, nearest-rank p95 **0.845 s**, maximum **0.845 s**. Three five-or-fewer-item `/predict/batch` requests had median **1.67 s**, maximum **3.00 s**. These are small, sequential samples, not a production latency SLO.
- Thirty of 59 runs were repeats after the first occurrence of one of seven observed root groups. That is an upper bound of **30/59 (51%)** for root-level duplicate suppression if every grouping were confirmed and current-run evidence matched. It is not an estimate of safe Vertex-call savings. The largest group is 20 AAP rejections; the holdout has no independent AAP root to validate a bypass.
- An earlier global prompt gave a wrong Kubernetes route at 0.864 for a duplicate-migration case. The selected route's two wrong compile predictions were 0.409–0.417. Score alone has no demonstrated safe cutoff. No held-out Laya subtype prediction reached 0.90. A threshold of 0.90 plus direct signature agreement fires on **zero** held-out cases, including zero wrong-class cases. With bypass disabled, projected Vertex reduction is **0%**.

## Bypass decision and remaining acceptance work

Keep Laya in shadow for build/install subtype experiments. Use the failed CI
step directly for coarse E2E/authorization routing. Keep Vertex for diagnosis
and for every ambiguous or conflicting case. A templated bypass can be proposed
only for a human-confirmed exact signature, with direct evidence from the
current run in a named log or artifact and no later successful retry. Laya must
agree with that route at a calibrated threshold; the current conservative
candidate is 0.90. Generic timeouts, missing evidence, conflicting sources,
unknown predictions, and below-threshold scores abstain. Any wrong-class
bypass in a root-separated audit, or loss of required evidence in live shadow
capture, turns bypass off immediately. Review the template and signature with
the owning team before enabling it.

The exact candidates for that review are:

| Candidate template | Required current-run source and exact signal | Evaluation status |
|---|---|---|
| Fork approval needed | Failed `Authorize fork PR` step, with executed `job.log` lines `##[error]Fork PR blocked:` and `/ok-to-test`; ignore later artifact-gather errors | 4 development runs, no independent holdout root; no human approval |
| AAP instance-group policy rejection | Failed `Install OSAC` step and `pod-osac-aap-bootstrap-*.log` line containing `Unable to create instance_group`, `pod_spec_override`, and `Mounting Kubernetes secrets ... not allowed`; exclude resolved retries | 20 development runs of one root; Laya scores 0.319–0.351; no holdout root or human approval |
| Duplicate migration blocks gRPC startup | `pod-fulfillment-grpc-server*.log` line containing `failed to init driver` and `duplicate migration file:` with the filename; verify gRPC pod failed to start | Two different migration files across splits; held-out Laya score 0.633; no human approval |

These are **review candidates**, not enabled rules. Build dependency errors and
E2E timeouts do not yet have a sufficiently specific, approved response.

This work satisfies the retrospective confusion/error review and documents a
candidate gate. **OSAC-5641 is not complete**: OSAC-5638 still needs a
human-approved corpus, OSAC-5639 needs the shared production extractor, and
OSAC-5640 needs live shadow observations through that same extractor/schema.
No human-confirmed signature or live bypass was approved here. Repeat this
evaluation on those observations and on another root-separated holdout before
changing the Vertex path.
