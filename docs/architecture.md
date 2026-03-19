# Architecture

Two independent flows share the same Aurora pgvector database via Bedrock Knowledge Bases.

![Architecture](../images/architecture_v9.png)

---

## API Routes

```
POST /upload-url        → Lambda (presigned URL)
GET  /status/{doc_id}  → Lambda (ingestion trigger, reads DynamoDB)
POST /query             → Lambda (retrieval, calls Bedrock RetrieveAndGenerate)
```

All routes require a Cognito JWT in the `Authorization` header. API Gateway validates the token before reaching any Lambda.

---

## Flow 1 - Ingestion

User uploads a document.

```
React Frontend → Cognito (login, get JWT)
     |
     | POST /upload-url + JWT
     v
API Gateway (Cognito Authorizer validates JWT)
     |
     v
Lambda (presigned URL)
     |-- writes to DynamoDB: { doc_id, user_id, status: PENDING }
     |   returns { presigned_url, doc_id } to client
     v
Client uploads PDF directly to S3 (max 50MB, application/pdf only)
     |
     | S3 Event Notification
     v
SQS FIFO Queue (+ DLQ, MessageDeduplicationId = S3 ETag)
     |
     v
Lambda (ingestion trigger)
     |-- bedrock.start_ingestion_job()
     |-- writes to DynamoDB: { doc_id, job_id, status: RUNNING }
     v
Bedrock Knowledge Base
     |-- reads document from S3
     |-- chunks document (hierarchical strategy)
     |-- embeds each chunk via Titan Embeddings v2 (1536 dims)
     |-- upserts vectors into Aurora pgvector (HNSW index)
     v
Lambda (ingestion trigger) polls job status
     |-- writes to DynamoDB: { doc_id, status: COMPLETE, chunk_count }
     v
Aurora PostgreSQL Serverless v2
```

---

## Flow 1b - Status Check

Client polls until the document is ready to query.

```
React Frontend
     |
     | GET /status/{doc_id} + JWT   (polls every 5s)
     v
API Gateway (Cognito Authorizer validates JWT)
     |
     v
Lambda (ingestion trigger)
     |-- reads DynamoDB: { doc_id }
     v
{ status: "PENDING" | "RUNNING" | "COMPLETE" | "FAILED", chunk_count? }
```

---

## Flow 2 - Retrieval

User asks a question.

```
React Frontend
     |
     | POST /query + JWT + session_id
     v
API Gateway (Cognito Authorizer validates JWT)
     |
     v
Lambda (retrieval)
     |-- reads DynamoDB: Query(session_id, ScanIndexForward=False, Limit=10)
     |   bedrock.retrieve_and_generate() with conversation history
     v
Bedrock Knowledge Base
     |-- embeds query via Titan Embeddings v2
     |-- HNSW similarity search on Aurora pgvector
     |-- top K chunks + question + history → Claude 3 Haiku
     v
Lambda (retrieval)
     |-- writes to DynamoDB: { session_id, turn_id: timestamp, role, content, ttl }
     v
natural language answer → React Frontend
```

---

## Who Owns What

Aurora and DynamoDB never interact directly. Each service has a single owner.

| Store | Written by | Read by | Contains |
|---|---|---|---|
| Aurora pgvector | Bedrock KB (ingestion) | Bedrock KB (retrieval) | Chunks + 1536-dim vectors |
| DynamoDB | Lambda functions | Lambda functions | Job status, session turns, document registry |
| S3 (documents) | Client (via presigned URL) | Bedrock KB | Raw PDF/DOCX files |
| Secrets Manager | Terraform | Lambda (at cold start) | Aurora connection string |

---

## Where Things Live

```
Outside VPC (AWS Managed):
  API Gateway, Cognito, WAF, CloudFront
  S3 (documents + frontend), SQS FIFO
  Bedrock Knowledge Base, Titan Embeddings v2, Claude 3 Haiku
  DynamoDB, Secrets Manager, KMS
  CloudWatch, X-Ray

VPC - Public Subnet:
  NAT Gateway  (Phase 2: replaced by VPC Endpoints)

VPC - Private Subnet:
  Lambda (presigned URL)
  Lambda (ingestion trigger)
  Lambda (retrieval)
  Aurora PostgreSQL Serverless v2 + pgvector
```

---

## Job States

```
PENDING → RUNNING → COMPLETE
                 \→ FAILED → DLQ
                              |
                         CloudWatch Alarm
                              |
                         SNS → Slack + Email
```

- **Retry policy**: SQS retries 3 times with exponential backoff before sending to DLQ
- **Deduplication**: FIFO queue with `MessageDeduplicationId = S3 ETag` - same file re-uploaded never triggers two jobs
- **Idempotency**: Bedrock KB tracks ETags internally - re-running an ingestion job on an unchanged file is a no-op
