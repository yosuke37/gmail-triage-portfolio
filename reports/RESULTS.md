# Recorded proposal evaluation

## Synthetic attachment benchmark

Six generated attachments cover two scanned invoices, an instruction embedded in a scanned PDF, the same instruction in a text attachment, and malformed PDF and DOCX files. The benchmark passes the extracted content to the keyword planner in LangGraph. It applies Gmail's automation policy with an allowlist enabled and records the proposed decisions without executing mailbox actions.

| Metric | Result |
|---|---:|
| Expected extraction status | 6/6 |
| Exact OCR transcription after case and whitespace normalization | 3/3 |
| Invoice totals and references recovered | 4/4 |
| Readable attachments cited | 4/4 |
| Attachment citations matching the referenced extracted text | 4/4 |
| Embedded instructions flagged | 2/2 |
| Attachment cases requiring human review | 6/6 |

The two malformed documents produced no attachment citation. The policy blocked both a wrong attachment ID and an invented quote. A routine message without an attachment qualified for automatic labeling under the same policy.

This small synthetic set measures local OCR, evidence matching, and review policy. Live model quality, varied real-world scans, and general prompt-injection detection are outside its scope. The [raw attachment report](attachment-benchmark.json) records the per-case results and Tesseract version.

Run from the [Docker quickstart](../README.md#reproduce-the-portfolio-demo):

```powershell
docker compose --env-file deploy/compose.env --profile local-db exec worker python -m deploy.attachment_benchmark
```

## November 6 holdout comparison

Both modes ran on the same 10 synthetic holdout emails. The AI run used `gpt-6-luna`, prompt `triage-planner-docx-ocr-v3`, and workflow `action-proposals-docx-ocr-v3`. The [raw report](proposals-holdout-docx-ocr-v3.json) includes per-case outcomes, hashes, versions, and gate results.

| Metric | Keyword baseline | AI |
|---|---:|---:|
| Label accuracy | 6/10 | 10/10 |
| Review accuracy | 7/10 | 10/10 |
| Exact action set | 7/10 | 7/10 |
| Action F1 | 0.870 | 0.870 |
| Valid schemas | 10/10 | 10/10 |
| Grounded evidence quotes | 100% | 100% |
| Unsafe automatic eligibility decisions | 0 | 0 |

The AI passed all four regression gates. Its proposals omitted expected reply drafts on `holdout-02`, `holdout-04`, and `holdout-09`. The AI policy marked one action eligible for automatic execution, eight as requiring approval, and one as blocked. The benchmark evaluated eligibility without executing any mailbox actions. The keyword baseline runs in preview mode.

The AI run reported 10 requests and 9,959 tokens. Median latency was 2.19 seconds and p95 was 3.98 seconds. The cases contain no attachments, so the run measures proposal quality on email text. The source hash includes the local 10-page OCR parser change, which these cases never call. The planner and scoring code match commit `d07d20c`.

Reproduce with `python -m triage benchmark --compare --split holdout --report output/evaluations/holdout.json --check`. See [evaluation design guidance](https://developers.openai.com/api/docs/guides/evaluation-best-practices).

## November 2 development and holdout results

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
