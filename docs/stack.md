# Stack

## At a Glance

| Component | Technology | Why |
|---|---|---|
| Document storage | Amazon S3 | Infinitely scalable, cheap, triggers pipeline via events - no polling |
| CDC trigger | S3 Events + SQS FIFO | Event-driven - only new/changed documents trigger re-processing. FIFO deduplication by S3 ETag |
| Message queue | Amazon SQS FIFO | Decouples trigger from processing, built-in retry, DLQ, exactly-once deduplication |
| Ingestion pipeline | Bedrock Knowledge Bases | Fully managed: chunking, embedding, indexing in one API call. Replaces 3 custom Lambdas |
| Embedding model | Bedrock Titan Embeddings v2 | 1536-dim vectors. AWS-native, IAM auth only, no external API keys |
| LLM inference | Bedrock Claude 3 Haiku | Called by Bedrock KB after retrieval. AWS-native, no OpenAI dependency |
| Vector store | pgvector on Aurora PostgreSQL | HNSW index, 10-50ms at 1M+ vectors. Near-$0 when idle. Standard SQL for metadata filters |
| Database engine | Aurora Serverless v2 | Min 0.5 ACU (~$43/month). Multi-AZ, automatic failover |
| State & metadata | Amazon DynamoDB | Job status, session history, document registry. Serverless, ~$0 at low volume |
| Compute | AWS Lambda | Three thin functions: presigned URL (~15 lines), ingestion trigger (~20 lines), retrieval (~20 lines) |
| API layer | Amazon API Gateway | SSL termination, rate limiting, Cognito JWT authorization |
| Authentication | Amazon Cognito (invite-only) | Admin creates users, temporary password via email, OTP MFA |
| Secrets | AWS Secrets Manager | Aurora credentials fetched at Lambda cold start via IAM - no hardcoded values |
| Encryption | AWS KMS | S3, Aurora, SQS encrypted with customer-managed KMS keys |
| Network isolation | VPC + Private Subnets | Aurora and Lambda not exposed to internet. NAT Gateway for outbound Bedrock traffic |
| Frontend | React on S3 + CloudFront | Static hosting, global CDN, HTTPS, WAF integration |
| WAF | AWS WAF + Managed Rules | CommonRuleSet + KnownBadInputs blocks SQLi, XSS, rate-limits abuse |
| Structured logging | CloudWatch Logs (JSON) | Query logs by doc_id, user_id, measure p99 latency with CloudWatch Insights |
| Custom metrics | CloudWatch Metrics | IndexStalenessRate, EmbeddingPipelineLag, RetrievalLatencyP99 |
| Distributed tracing | AWS X-Ray | Full trace: Lambda → Bedrock KB → Aurora |
| Alerting | CloudWatch Alarms + SNS | Error spikes and DLQ messages fan out to Slack + email |
| RAG evaluation | RAGAS | Quality gate in CI/CD: Faithfulness, Relevancy, Context Precision, Context Recall |
| Infrastructure as code | Terraform | Modular, remote state on S3 + DynamoDB lock |
| CI/CD | GitHub Actions | Auto-deploy on push: tests → Terraform plan → staging → RAGAS gate → prod |
| Local development | Docker Compose + LocalStack | Postgres + pgvector + S3/SQS/DynamoDB locally, Bedrock via real AWS credentials |

---

## Storage & Ingestion

**Amazon S3**
Stores raw documents (PDF, DOCX, TXT). Presigned URL policy enforces `content-length-range` (max 50MB) and `content-type: application/pdf` - the Lambda never handles file bytes directly.

**S3 Event Notifications → SQS FIFO**
CDC mechanism: S3 pushes a message to SQS the moment a file lands. FIFO queue with `MessageDeduplicationId = S3 ETag` means re-uploading the same file never triggers duplicate ingestion.

**Amazon SQS FIFO**
Buffer between S3 and the ingestion Lambda. If Lambda fails, messages wait and retry automatically. DLQ captures failures after max retries.

---

## Processing - Bedrock Knowledge Bases

**Amazon Bedrock Knowledge Bases**

One API call (`start_ingestion_job`) does everything:
1. Reads the document from S3
2. Chunks it (hierarchical strategy by default)
3. Embeds each chunk via Titan Embeddings v2 (1536 dims)
4. Upserts vectors into Aurora pgvector: `INSERT ... ON CONFLICT DO UPDATE`

Replaces what would otherwise be three custom Lambdas (chunking, embedding, indexing).

For retrieval, `retrieve_and_generate`:
1. Embeds the user query via Titan
2. Runs HNSW similarity search on Aurora pgvector
3. Passes top K chunks + conversation history + question to Claude 3 Haiku
4. Returns a grounded natural language answer

**Titan Embeddings v2** - 1536-dim vectors, AWS-native, IAM auth only.

**Claude 3 Haiku** - cheapest Claude model. Routes to Sonnet for complex queries in Phase 3.

---

## Vector Store

**pgvector on Aurora PostgreSQL Serverless v2**

Chosen over OpenSearch Serverless (see [ADR 001](adr/001-aurora-vs-opensearch.md)).

HNSW index:
```sql
CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

Enables 10-50ms approximate nearest-neighbor search at 1M+ vectors without full table scans.

---

## State & Metadata - DynamoDB

Three logical tables. DynamoDB is only accessed by Lambda functions - never by Bedrock KB or Aurora.

- `documents` - registry of uploaded files (doc_id, user_id, filename, status, chunk_count)
- `jobs` - ingestion job lifecycle (job_id, doc_id, status, started_at, completed_at)
- `sessions` - conversation history, one item per turn (session_id PK, turn_id SK, role, content, TTL 24h)

See [data-model.md](data-model.md) for full schemas.

---

## Security

**Cognito (invite-only)**
`allow_admin_create_user_only = true`. Admin creates users via console or CLI, Cognito sends temporary password by email. MFA via OTP on every login.

**IAM Least Privilege**
Each Lambda has its own role with only the permissions it needs:
- presigned_fn: `s3:PutObject` on one bucket prefix + `dynamodb:PutItem`
- ingestion_fn: `bedrock:StartIngestionJob` + `dynamodb:UpdateItem`
- retrieval_fn: `bedrock:RetrieveAndGenerate` + `dynamodb:Query`

**KMS** - S3, Aurora, SQS encrypted with customer-managed keys. Separate key per environment.

**WAF Managed Rules** - `AWSManagedRulesCommonRuleSet` + `AWSManagedRulesKnownBadInputsRuleSet` on both CloudFront and API Gateway.

---

## Observability

**Structured JSON logs** across all Lambdas:
```json
{
  "event": "retrieval_complete",
  "request_id": "uuid",
  "user_id": "giovanni",
  "session_id": "sess-abc",
  "latency_ms": 340,
  "chunks_retrieved": 5,
  "environment": "prod"
}
```

**CloudWatch Alarms:**
- Lambda error rate > 1%
- Retrieval p99 latency > 5s
- DLQ message count > 0
- Aurora CPU > 80%

**X-Ray** - distributed tracing across Lambda → Bedrock KB → Aurora.
