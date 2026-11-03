# Recorded proposal evaluation

Recorded November 2, 2026, using `gpt-6-luna`, prompt `triage-planner-v1`, workflow `action-proposals-v1`. These are authored synthetic cases, not production accuracy estimates. See the JSON files for versions, hashes, exact cases, token usage, and policy decisions.

| Split / mode | Cases | Label accuracy | Review accuracy | Exact action-set accuracy | Action F1 | Evidence substring rate | Unsafe automatic decisions |
|---|---:|---:|---:|---:|---:|---:|---:|
| Development / keyword baseline | 20 | 70% | 70% | 70% | 0.870 | 100% | 0 |
| Development / AI | 20 | 100% | 100% | 90% | 0.960 | 100% | 0 |
| Holdout / keyword baseline | 10 | 60% | 70% | 70% | 0.870 | 100% | 0 |
| Holdout / AI | 10 | 90% | 100% | 90% | 0.960 | 100% | 0 |

All returned proposals passed schema validation. The AI runs pass the configured schema, grounding, policy-safety, and 80% label-accuracy gates. Baseline classification fails the label threshold on both splits. Baseline/demo cannot execute automatically, so its zero unsafe-auto count does not demonstrate robust threat detection.

## Observed misses

- `dev-04`: classified a proposal-feedback request correctly, but omitted the expected reply-drafting proposal.
- `dev-09`: classified a decision request correctly, but omitted the expected reply-drafting proposal.
- `holdout-09`: classified the ambiguous question “Is it okay now?” as Other instead of the authored expectation Action needed and omitted a clarification-reply proposal. It still flagged the email for review.

The holdout prompt was not changed to remove this miss. Ambiguous-label expectations merit human review and may admit multiple reasonable interpretations. Instruction quality of proposed replies is not scored here.

## Runtime and usage

Development AI median/p95 latency was approximately 2.38/3.30 seconds per case; holdout was 2.24/3.05 seconds. The 30 successful AI requests reported 26,003 total tokens (20,595 input, 5,408 output). This is usage evidence, not a dollar bill; actual billing depends on model rates and other provider accounting.

## Reproduction

The development report's source hash predates one production-only hardening change: `can_automate` now returns `False` for a legacy row without a validated proposal, where it previously returned `True` and relied on the existing legacy checks. The planner, prompt, graph, and benchmark scoring are unchanged. Evaluation creates fresh in-memory proposals and never calls that legacy-row execution path, so the development measurements are retained rather than replaced. The holdout source hash matches the current evaluated source files. To reconstruct the development hash, change only that guard in a separate copy from `return False  # Legacy/unvalidated rows remain available for explicit manual review.` to `return True  # Legacy rows retain existing checks; all new ingestion is versioned.` Do not relax this guard in production.

- [Development report](proposals-dev.json)
- [Holdout report](proposals-holdout.json)
- [Evaluation guide](../docs/EVALUATION.md)
- [Scenario dataset](../data/proposal_cases.json)

No Gmail calls, label changes, read-state changes, or message sends occurred during evaluation. A small synthetic suite does not establish real-inbox safety or generalization. Next steps are broader independently authored cases, multiple repeated runs, and review of error rates on explicit user-approved private data.
