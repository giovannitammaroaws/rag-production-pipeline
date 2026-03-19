# Data Model

---

## S3 - Raw Documents

```
s3://rag-pipeline-docs/{user_id}/{doc_id}/{filename.pdf}
```

Presigned URL policy enforces:
- `content-length-range`: 1 byte to 52,428,800 bytes (50MB)
- `content-type`: `application/pdf`

---

## SQS FIFO - Trigger Message

```json
{
  "doc_id": "uuid-1234",
  "s3_key": "giovanni/uuid-1234/report.pdf",
  "user_id": "giovanni",
  "uploaded_at": "2026-03-19T10:00:00Z",
  "content_type": "application/pdf"
}
```

`MessageDeduplicationId = S3 ETag` - same file re-uploaded within the 5-minute deduplication window is ignored.

---

## DynamoDB - Documents Table

Registry of all uploaded files.

```
Partition key: doc_id (String)
```

```json
{
  "doc_id": "uuid-1234",
  "user_id": "giovanni",
  "filename": "annual_report.pdf",
  "s3_key": "giovanni/uuid-1234/annual_report.pdf",
  "status": "INDEXED",
  "chunk_count": 47,
  "uploaded_at": "2026-03-19T10:00:00Z"
}
```

Status values: `PENDING` → `RUNNING` → `COMPLETE` | `FAILED` | `QUARANTINED` (Phase 2)

---

## DynamoDB - Jobs Table

Ingestion job lifecycle.

```
Partition key: job_id (String)
GSI:           doc_id-index (to look up jobs by document)
```

```json
{
  "job_id": "bedrock-job-xyz",
  "doc_id": "uuid-1234",
  "status": "COMPLETE",
  "started_at": "2026-03-19T10:01:00Z",
  "completed_at": "2026-03-19T10:03:00Z",
  "error_message": null
}
```

---

## DynamoDB - Sessions Table

Conversation history. One item per turn to avoid the 400KB DynamoDB item size limit on long conversations.

```
Partition key: session_id (String)
Sort key:      turn_id    (String, ISO timestamp - enables time-ordered queries)
```

```json
{
  "session_id": "sess-abc",
  "turn_id": "2026-03-19T10:05:00.000Z",
  "user_id": "giovanni",
  "role": "user",
  "content": "What is the revenue for 2025?",
  "ttl": 1742486700
}
```

To retrieve last N turns for context:
```python
response = table.query(
    KeyConditionExpression=Key("session_id").eq(session_id),
    ScanIndexForward=False,  # newest first
    Limit=10
)
turns = list(reversed(response["Items"]))  # restore chronological order
```

TTL = 24 hours from creation. DynamoDB deletes expired items automatically.

---

## Aurora pgvector - Managed by Bedrock KB

Bedrock Knowledge Bases creates and manages the vector table schema automatically. The table contains:

- `id` - chunk UUID
- `content` - raw chunk text
- `embedding` - vector(1536) - cosine similarity via HNSW index
- `metadata` - JSONB with source doc info (doc_id, s3_key, page_number, chunk_index)
- `bedrock_knowledge_base_id` - KB identifier

HNSW index:
```sql
CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

Phase 2 adds `user_id` to metadata for per-user retrieval filtering.

---

## Secrets Manager

```
/rag-pipeline/prod/aurora-connection-string
  → "postgresql://user:pass@cluster.rds.amazonaws.com:5432/ragdb"
```

One secret per environment (dev / staging / prod). Lambda fetches at cold start via IAM role - no hardcoded credentials anywhere in the codebase.

---

## Terraform Remote Backend

```
s3://rag-pipeline-tf-state/prod/terraform.tfstate   - state file (encrypted, versioned)
DynamoDB table: terraform-state-lock                 - prevents concurrent applies
```

Not part of the application runtime. Provisioned once before any other Terraform apply.

---

## CloudWatch Logs - Structured JSON

Every Lambda emits JSON logs queryable with CloudWatch Insights.

```json
{
  "event": "ingestion_job_started",
  "request_id": "uuid",
  "doc_id": "uuid-1234",
  "job_id": "bedrock-job-xyz",
  "user_id": "giovanni",
  "environment": "prod",
  "timestamp": "2026-03-19T10:01:00Z"
}
```

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

Example CloudWatch Insights query:
```
fields @timestamp, user_id, latency_ms, chunks_retrieved
| filter event = "retrieval_complete"
| stats avg(latency_ms), pct(latency_ms, 99) by bin(5m)
```
