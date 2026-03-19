# Service Level Objectives

Formal reliability targets for the RAG Production Pipeline. Measured on a rolling 30-day window via CloudWatch Metrics.

Error budget = the amount of unreliability we accept before stopping deployments and prioritising stability.

---

## SLO 1 - Retrieval Latency

| | Value |
|---|---|
| Objective | p99 of `POST /query` responses < 2 seconds |
| Measurement | CloudWatch metric `RetrievalLatencyP99` |
| Window | Rolling 30 days |
| Error budget | 0.1% = **43 minutes/month** above threshold |

**Why 2s:** Bedrock RetrieveAndGenerate includes embedding generation + HNSW search + LLM inference. Median is ~350ms. p99 allows for Aurora cold starts and Bedrock API variance.

**Alert:** CloudWatch Alarm triggers when p99 > 2s for 5 consecutive minutes.

---

## SLO 2 - Availability

| | Value |
|---|---|
| Objective | 99.5% of requests return HTTP 2xx |
| Measurement | `(successful_requests / total_requests) * 100` |
| Window | Rolling 30 days |
| Error budget | 0.5% = **3.6 hours/month** of errors |

Errors counted: Lambda 5xx, API Gateway 5xx, Bedrock API failures.
Not counted: 4xx client errors (bad input, auth failures, rate limits).

**Alert:** CloudWatch Alarm triggers when error rate > 1% for 5 consecutive minutes.

---

## SLO 3 - Ingestion Pipeline Health

| | Value |
|---|---|
| Objective | DLQ message count = 0 |
| Measurement | CloudWatch metric `ApproximateNumberOfMessagesVisible` on DLQ |
| Window | Instantaneous |
| Error budget | None - any DLQ message is an immediate incident |

A message in the DLQ means a document failed ingestion after 3 retries. The user uploaded a document they cannot query. This is always an incident.

**Alert:** CloudWatch Alarm triggers immediately when DLQ depth > 0. SNS → Slack + email.

---

## SLO 4 - Ingestion Latency

| | Value |
|---|---|
| Objective | 95% of documents reach COMPLETE status within 5 minutes of upload |
| Measurement | `completed_at - uploaded_at` from DynamoDB jobs table |
| Window | Rolling 7 days |
| Error budget | 5% of documents may take longer |

Typical ingestion time: 1-3 minutes for a 10-page PDF. Large documents (50MB) may take longer.

**Alert:** CloudWatch Alarm triggers when `EmbeddingPipelineLag` (SQS queue depth) > 10 messages for 10 consecutive minutes.

---

## Error Budget Policy

| Budget remaining | Action |
|---|---|
| > 50% | Normal deployment cadence |
| 25-50% | Review recent changes, increase monitoring |
| < 25% | Freeze non-critical deployments, focus on reliability |
| 0% | Full deployment freeze until budget resets |

Budget resets at the start of each 30-day window.

---

## CloudWatch Dashboard

All four SLOs are visualised on a single CloudWatch dashboard defined in `infra/modules/observability/dashboard.json`.

Widgets:
- p50 / p95 / p99 retrieval latency (time series)
- Error rate % (time series)
- DLQ depth (single value - red if > 0)
- Ingestion pipeline lag / SQS queue depth (time series)
- Error budget burn rate (gauge)
