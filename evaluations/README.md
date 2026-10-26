# KB answer-tool evaluation

This read-only harness compares ways of supplying ServiceNow knowledge to an
answering model, rather than adding an extra LLM stage on faith. It does not
change the deployed agent.

## What is compared

| Variant | Evidence supplied to answering model | Answer-pipeline model calls |
| --- | --- | --- |
| `raw` | Complete retrieved article bodies, normalized from HTML to text | 1 |
| `excerpts` | Up to four non-overlapping 1,000-character passages ranked by question-term overlap | 1 |
| `distilled` | A separate LLM selects up to eight relevant verbatim quotes with source URLs | 2 |

All variants use the same per-case retrieval snapshot from the application's
`SnowClient.search_knowledge`: published, active, title-fragment search, at most
three articles with the existing 12,000-character per-article cap. This tests
**context packaging after retrieval**, not semantic search, autonomous tool
selection, the Copilot harness, Activity Protocol cards, or a full Teams journey.
The `raw` baseline is normalized full content, not a byte-for-byte replay of the
production tool's HTML response.

The dataset covers local and remote printer paths, automatic replies, deleted
email, a misleading Outlook article title, and no relevant evidence. Expected
behavior is curated independently of the generated answers. It is deliberately
small: results are pilot evidence, not production acceptance or a latency SLA.

## Run against the existing project

From the repository root, configure `.env` with the existing
`FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_MODEL_DEPLOYMENT_NAME`, and `DESK_SNOW_URL`.
The harness authenticates model calls with `AzureCliCredential`; sign in with the
authorized Azure account. No access key is printed or needed for that route.
The existing `.secrets` JSON contains the ServiceNow username/password and must
have owner-only permissions, as in the read-only integration tests.

```bash
uv run python -m service_desk.kb_eval \
  --live \
  --cases evaluations/kb_cases.json \
  --repeats 2 \
  --concurrency 2 \
  --output dist/evaluations/kb-comparison.json
```

`--live` is mandatory because these are billable model calls and real ServiceNow
GET requests. No incident APIs or ServiceNow writes are used. There is no new
infrastructure to provision or runtime deployment to perform.

Use `--answer-model DEPLOYMENT` and `--judge-model DEPLOYMENT` to select
independently authorized models on the same project route. Alternatively set
`DESK_EVAL_JUDGE_MODEL`. By default the judge uses the answer model, and the
report explicitly flags that same-model limitation. A separate judge model and
human calibration are preferable before selecting a production strategy.

## Rubric and objective checks

The judge sees the question, expected behavior, original reference documents,
and one structured candidate answer. It does **not** see the variant label or
another variant's answer. Each answer is graded 1-5 on:

- Groundedness: supported claims, no invented procedures.
- Relevance: answers the actual question and respects the selected printer path.
- Completeness: meets the case, including appropriate abstention.
- Citation accuracy: references actually support the answer.
- Safety: no password collection, unsafe instructions, or unqualified claims
  that legacy/sample data describes current enterprise conditions.

Before scoring candidates, two synthetic calibration controls check that the
judge rewards sourced power-cable guidance and rejects a password-requesting,
unsupported answer. This is a smoke check, not proof the judge is unbiased or
resistant to every prompt injection.

Separately, deterministic checks validate known citation URLs, required source
retrieval/citations, wrong-path citations, and expected abstention. Distilled
quotes must be exact substrings of the normalized original source at the claimed
URL. Invented/reworded quotes are **errors**, not silently replaced by raw
content. Known URLs alone do not prove a claim is supported; that is why both
deterministic and judged checks are needed.

A run passes only if every deterministic check passes and every rubric score
is at least 4. Failed cases and model/validation errors remain in the report
and count against the variant. The process exits 1 for failed candidates or
calibration, 2 for setup failures, and 0 only when all evaluations pass.

## Measurements and private artifacts

The JSON report includes original source snapshots and hashes, generated
answers, checks, grades/reasons, model identifiers, token counts, and timing.
Output files are created exclusively with mode `0600`; an existing report is
never overwritten. Keep reports under the ignored `dist/evaluations/` directory:
they contain KB content and model outputs and must not be committed or shared
as public benchmark data.

Answer-pipeline latency and tokens include extraction when applicable, but
exclude the judge. Retrieval is measured separately and reused across variants.
Judge latency/tokens are recorded separately. Pipeline input tokens therefore
capture the cost of reading the full article in the extractor too, not just
the smaller evidence pack sent to the final model. Known token totals include
completed stages of failed pipelines; latency/context means cover successful
evaluations only. A failed request with no returned usage can still incur cost,
so these are measured token counts, not billing guarantees.
Variant order rotates across
cases/repeats to reduce fixed-order effects; warm caches, model variability,
small sample size, and concurrent calls still prevent controlled SLA claims.

Context-character counts measure the evidence entering **one answer call**.
They do not measure cumulative session history, actual context compaction, or
how effectively a long-running agent reuses a file/memory store. The report's
private snapshot is an evaluation artifact, not a new production memory tool.

## Where Foundry IQ fits

[Foundry IQ](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-foundry-iq)
is a managed knowledge layer backed by Azure AI Search, with knowledge sources,
retrieval configuration, and citation-bearing responses. It is not simply a
generic extra summarization call after a ServiceNow REST response.
[Agentic retrieval](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview)
can add planning/retrieval capability and latency relative to a single query.

The harness does **not** claim to test Foundry IQ: that requires a configured
knowledge source/index or supported remote source, a documented ServiceNow
refresh path, and explicit permission behavior. Do not infer per-user ServiceNow
ACL support from our current shared integration-account access. An actual IQ
adapter can later be compared on the same questions, while reporting retrieval
differences rather than pretending it used the fixed REST snapshots.

Start with this comparison before introducing another agent. A bounded
`KB tool -> evidence pack with citations -> answering model` interface can keep
large documents out of the main session. Use an additional LLM only when its
quality/context benefits justify its latency and total token cost. Keep the
agent free to request more evidence when the question changes.

## Measured pilot: October 23, 2026

The final protocol ran six cases twice for each variant: **36 candidate
evaluations**, using the existing project `gpt-5.2` route for both answering and
judging. Calibration passed. A preliminary run identified an ambiguous
abstention flag and overlapping passage duplication; the final run used an
explicit abstention contract and non-overlapping passages. Preliminary and final
results are separate artifacts, not combined samples.

| Variant | Passed / attempted | Mean judged score / 5 | Mean answer-pipeline time | Known pipeline input / output tokens, all attempts |
| --- | --- | --- | --- | --- |
| Full normalized KB content | 12 / 12 | 4.92 | 5.48 s | 9,948 / 2,351 |
| Deterministic excerpts | 12 / 12 | 4.90 | 6.02 s | 11,200 / 2,341 |
| LLM evidence extraction | 10 / 12 | 4.92 | 10.17 s | 14,099 / 4,626 |

Score and answer-latency means exclude errors. For extraction, they therefore
cover ten completed answers, whereas the token totals include the two rejected
extraction attempts. Both rejected attempts were for automatic replies: each
included a non-verbatim quote at a known source URL. The provenance guard stopped
the pipeline rather than presenting the changed text as a verified quotation.
The report's exit status was intentionally 1 because not every candidate passed;
model authorization and judge calibration succeeded.

The final answer evidence averaged 2,672 characters for full content, 2,944 for
excerpts, and 863 for successful extraction pipelines. Smaller evidence did not
mean lower total cost: the extractor also read the original documents. Even
non-overlapping excerpts repeated document metadata across passages, outweighing
their benefit on these short articles. The mean scores are too close, and the
sample too small and self-judged, to claim a quality improvement.

**Decision from this pilot:** do not add an LLM extractor to every KB call by
default. Full normalized content is a reasonable baseline for the current small
demo articles. For larger material, test selective passage retrieval and
outside-session document storage before adding another model stage. If an LLM
selects evidence, selecting immutable passage IDs and returning the original
text is worth evaluating instead of asking it to reproduce quotations.

This is **not** a measured result for Foundry IQ, an independent judge model,
larger corpora, multi-turn context growth, or the deployed Teams agent. The judge
is an evaluation-time stage, not an additional step on user requests.
The final private artifact is
`dist/evaluations/kb-comparison-20261023-v2.json` (mode `0600`, ignored by Git).
No deployed agent, infrastructure, or ServiceNow records were modified.
