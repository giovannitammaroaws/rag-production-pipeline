# RAG Production Pipeline

Production-grade Retrieval-Augmented Generation pipeline on AWS. CDC-based document ingestion via Bedrock Knowledge Bases, event-driven with S3 + SQS, vector storage on Aurora pgvector with HNSW indexing, full observability, deployed with Terraform and GitHub Actions CI/CD.

---

## Architecture

Two independent flows share the same Aurora pgvector database via Bedrock Knowledge Bases.

![Architecture](images/architecture_v7.png)

```
ONE API GATEWAY — two routes:
  POST /upload-url  → Lambda (presigned URL, 15 lines)
  POST /query       → Lambda (retrieval, calls Bedrock RetrieveAndGenerate)


FLOW 1 - INGESTION (user uploads a document)
=============================================

  React Frontend → Cognito (login, get JWT)
       |
       | POST /upload-url + JWT
       v
  API Gateway (Cognito Authorizer validates JWT)
       |
       v
  Lambda (presigned URL)
       |
       | returns signed S3 URL to client
       v
  Client uploads PDF directly to S3
       |
       | S3 Event Notification
       v
  SQS Queue (+ DLQ)
       |
       v
  Lambda (ingestion trigger)
       |
       | bedrock.start_ingestion_job()
       v
  Bedrock Knowledge Base
       |── reads document from S3
       |── chunks document (hierarchical strategy)
       |── embeds each chunk → Titan Embeddings v2 (1536 dims)
       |── upserts vectors → Aurora pgvector (HNSW index)
       v
  Aurora PostgreSQL Serverless v2


FLOW 2 - RETRIEVAL (user asks a question)
==========================================

  React Frontend
       |
       | POST /query + JWT
       v
  API Gateway (Cognito Authorizer validates JWT)
       |
       v
  Lambda (retrieval)
       |
       | bedrock.retrieve_and_generate()
       v
  Bedrock Knowledge Base
       |── embeds query → Titan Embeddings v2
       |── HNSW similarity search → Aurora pgvector
       |── top K chunks + question → Claude 3 Haiku
       v
  natural language answer → React Frontend


WHERE THINGS LIVE
==================

  Outside VPC (AWS Managed):   API Gateway, S3, SQS, Bedrock Knowledge Base,
                                Titan Embeddings, Claude 3 Haiku,
                                Cognito, CloudFront, WAF,
                                Secrets Manager, KMS, CloudWatch, X-Ray

  VPC - Public Subnet:          NAT Gateway

  VPC - Private Subnet:         Lambda (presigned URL)
                                Lambda (ingestion trigger)
                                Lambda (retrieval)
                                Aurora PostgreSQL + pgvector


SHARED INFRASTRUCTURE
======================

  Aurora PostgreSQL Serverless v2 + pgvector
  (written by Bedrock KB during ingestion, read by Bedrock KB during retrieval)

  VPC + Private Subnets + NAT Gateway
  CloudWatch Logs + Metrics + Alarms + X-Ray
  Terraform (IaC) + GitHub Actions (CI/CD)
```

---

## Stack at a Glance - What We Use and Why

| Component | Technology | Why |
|---|---|---|
| Document storage | Amazon S3 | Infinitely scalable, cheap, triggers pipeline via events - no polling |
| CDC trigger | S3 Events + SQS | Event-driven - only new/changed documents trigger re-processing |
| Message queue | Amazon SQS | Decouples trigger from processing, built-in retry, dead-letter queue |
| Ingestion pipeline | Bedrock Knowledge Bases | Fully managed: chunking, embedding, indexing in one API call. No custom Lambda for each step. Replaces custom chunking/embedding/indexing Lambdas |
| Embedding model | Bedrock Titan Embeddings v2 | 1536-dim vectors. Used internally by Bedrock KB for both ingestion and retrieval. AWS-native, IAM auth only, no external API keys |
| LLM inference | Bedrock Claude 3 Haiku | Called internally by Bedrock KB RetrieveAndGenerate to produce the final answer. AWS-native, no OpenAI dependency |
| Vector store | pgvector on Aurora PostgreSQL | Standard PostgreSQL + vector extension. Bedrock KB writes directly to Aurora. HNSW index handles 1M+ vectors at 10-50ms. SQL filters for metadata |
| Database engine | Aurora Serverless v2 (auto-pause) | Scales to 0 when idle (~$0 when not in use). Multi-AZ, automatic failover in ~30 seconds |
| Compute | AWS Lambda | Three thin functions: presigned URL (15 lines), ingestion trigger (5 lines), retrieval (10 lines). Scales from 0 automatically |
| API layer | Amazon API Gateway | SSL, rate limiting, Cognito JWT authorization out of the box |
| Authentication | Amazon Cognito | Managed user auth (signup, login, JWT). API Gateway validates JWT on every request |
| Secrets | AWS Secrets Manager | Aurora credentials fetched at runtime via IAM - no hardcoded credentials |
| Encryption at rest | AWS KMS | S3, Aurora, SQS all encrypted with KMS keys |
| Network isolation | VPC + Private Subnets | Aurora and Lambda not exposed to internet. NAT Gateway for outbound to Bedrock KB |
| Frontend | React on S3 + CloudFront | Static hosting, global CDN, HTTPS, WAF integration |
| DDoS / WAF | AWS WAF | Blocks SQL injection, XSS, rate-limits abuse on API Gateway and CloudFront |
| Structured logging | CloudWatch Logs (JSON) | Queryable logs - find errors by doc_id, measure p99 latency |
| Custom metrics | CloudWatch Metrics | IndexStalenessRate, EmbeddingPipelineLag, pipeline error rate |
| Distributed tracing | AWS X-Ray | Traces full request path Lambda → Bedrock KB → Aurora |
| Alerting | CloudWatch Alarms + SNS | DLQ messages or error spikes fan out to Slack + email |
| RAG evaluation | RAGAS | Quality gate in CI/CD: Faithfulness, Relevancy, Context Precision, Context Recall |
| Infrastructure as code | Terraform | Modular, cloud-agnostic, infra diffs in pull requests |
| CI/CD | GitHub Actions | Auto-deploy on push, runs tests + Terraform plan + RAGAS evaluation |
| Local development | Docker Compose + LocalStack | Full stack locally without AWS costs |
| Architecture diagram | diagram.py (Python diagrams library) | Official AWS icons as code, versioned in git |

---

## Full AWS Stack - What We Use and Why

### Storage & Ingestion

**Amazon S3**
Stores raw documents (PDF, DOCX, TXT) uploaded by users. Infinitely scalable, cheap ($0.023/GB), and natively integrates with event notifications - when a file lands in S3, the pipeline starts automatically without polling.

**S3 Event Notifications → SQS**
This is our CDC (Change Data Capture) mechanism. Instead of polling S3, we use event notifications: S3 pushes a message to SQS the moment a file is uploaded or modified. Only changed documents trigger re-processing. Bedrock Knowledge Bases tracks ETags and checksums - when `start_ingestion_job` is called, it processes only new or modified documents, skipping already-indexed ones.

**Amazon SQS**
Buffer between S3 trigger and the ingestion Lambda. If Lambda fails, messages wait and retry automatically. The dead-letter queue (DLQ) captures messages that fail after max retries - nothing is silently lost.

---

### Processing - Bedrock Knowledge Bases

**Amazon Bedrock Knowledge Bases**
The core of the ingestion pipeline. One API call (`start_ingestion_job`) does everything:

1. Reads the document from S3
2. Chunks it using the configured strategy (hierarchical by default)
3. Embeds each chunk using Titan Embeddings v2
4. Upserts vectors into Aurora pgvector using `INSERT ... ON CONFLICT DO UPDATE`

This replaces what would otherwise be three custom Lambdas (chunking, embedding, indexing) with their associated infrastructure, error handling, and maintenance overhead.

For retrieval, `retrieve_and_generate` does:
1. Embeds the user query using Titan
2. Runs HNSW similarity search on Aurora pgvector
3. Passes top K chunks + user question to Claude 3 Haiku
4. Returns a natural language answer grounded in the retrieved context

**AWS Bedrock - Titan Embeddings v2**
Converts text to 1536-dimensional vectors. Used by Bedrock KB during both ingestion (chunks) and retrieval (user query). AWS-native - no external API keys, IAM auth only.

**AWS Bedrock - Claude 3 Haiku**
Called by Bedrock KB after retrieval to generate the final answer. Does not search - it receives the top K chunks plus the original question and generates a grounded response. AWS-native, no OpenAI dependency.

---

### Vector Store

**pgvector on Aurora PostgreSQL Serverless v2**
Bedrock Knowledge Bases writes vectors directly to Aurora. We chose Aurora pgvector over the default Bedrock KB option (OpenSearch Serverless) because:
- Aurora Serverless v2 auto-pauses → costs ~$0 when idle (OpenSearch Serverless costs ~$345/month minimum)
- Standard PostgreSQL - no new query language to learn
- HNSW index for fast approximate nearest-neighbor search at 1M+ vectors
- Metadata filters via standard SQL WHERE clauses

**HNSW Index**
```sql
CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```
Hierarchical Navigable Small World - enables 10-50ms similarity search at 1M+ vectors without comparing every row.

---

### API & Frontend

**Amazon API Gateway**
Single gateway with two routes: `POST /upload-url` and `POST /query`. Handles SSL termination, rate limiting, and Cognito JWT authorization out of the box.

**Lambda (presigned URL)**
Generates a signed S3 URL so the client uploads directly to S3 without routing through Lambda. 15 lines of Python, no framework needed.

**Lambda (ingestion trigger)**
Triggered by SQS. Calls `bedrock.start_ingestion_job()`. 5 lines of Python.

**Lambda (retrieval)**
Calls `bedrock.retrieve_and_generate()` and returns the answer to the client. 10 lines of Python. No FastAPI, no Mangum adapter needed.

**Amazon CloudFront + S3 (Frontend)**
Serves the React frontend as a static site. CloudFront acts as CDN - low latency globally, HTTPS by default, WAF integration for DDoS protection.

---

### Security

**Amazon Cognito**
Manages user authentication. API Gateway validates the Cognito JWT on every request - unauthenticated calls are rejected before reaching Lambda.

**AWS Secrets Manager**
Stores Aurora DB credentials. Lambda functions fetch at runtime via IAM role - no hardcoded credentials. Automatic rotation enabled.

**AWS KMS**
Encrypts data at rest: S3, Aurora, SQS all use KMS-managed keys.

**VPC + Private Subnets**
Aurora and Lambda run in a private subnet - no direct internet exposure. Lambda reaches Bedrock KB via NAT Gateway.

**IAM Least Privilege**
Each Lambda has its own role with only the permissions it needs. The ingestion trigger Lambda can call Bedrock KB but cannot write to Aurora directly. Blast radius is minimised.

**AWS WAF**
Attached to CloudFront and API Gateway. Blocks SQL injection, XSS, rate-limits abuse.

---

### Observability

**CloudWatch Logs (structured JSON)**
```json
{
  "event": "ingestion_job_started",
  "doc_id": "uuid-1234",
  "kb_id": "rag-kb-prod",
  "tenant_id": "tenant-abc",
  "environment": "prod"
}
```

**CloudWatch Metrics (custom namespace: `RAG/Pipeline`)**
- `IndexStalenessRate` - % of S3 documents not yet in Aurora
- `EmbeddingPipelineLag` - SQS queue depth over time
- `RetrievalLatencyP99` - p99 latency of RetrieveAndGenerate calls

**CloudWatch Alarms → SNS → Slack + Email**
Triggers on DLQ messages or error rate spikes.

**AWS X-Ray**
Distributed tracing: Lambda → Bedrock KB → Aurora. Pinpoints where latency comes from.

---

### Infrastructure & CI/CD

**Terraform**
All AWS resources defined as code. Modular structure - NAT Gateway can be toggled independently:
```bash
terraform apply -target=module.networking   # activate NAT GW
terraform destroy -target=module.networking # destroy NAT GW when not needed
```

**GitHub Actions**
1. Run pytest
2. Terraform plan
3. Deploy to staging
4. Run RAGAS evaluation (quality gate)
5. Manual approval
6. Deploy to prod

---

## Architectural Decision: NAT Gateway vs VPC Endpoints

Lambda in the private subnet needs outbound access to reach Bedrock Knowledge Bases (outside VPC).

### Option A - NAT Gateway (current setup)

| Service | Cost/month |
|---|---|
| NAT Gateway (fixed) | ~$32 |
| NAT Gateway (traffic, low volume) | ~$1 |
| S3 Gateway Endpoint | Free |
| **Total** | **~$33/month** |

### Option B - VPC Interface Endpoints (regulated environments)

Required for HIPAA, PCI-DSS, SOC2, FedRAMP where data must never leave the AWS private network.

| Endpoint | Cost (2 AZ) |
|---|---|
| Bedrock | ~$14.60/month |
| Secrets Manager | ~$14.60/month |
| CloudWatch Logs | ~$14.60/month |
| X-Ray | ~$14.60/month |
| S3 (Gateway) | Free |
| **Total** | **~$58/month** |

**This project uses NAT Gateway.** For healthcare, financial, or government deployments, switch to full VPC Interface Endpoints to satisfy compliance requirements.

---

## Cost Breakdown (Portfolio / Low Traffic)

| Service | Cost/month | Note |
|---|---|---|
| Aurora Serverless v2 (auto-pause) | ~$5-10 | Scales to 0 when idle |
| NAT Gateway | ~$33 | Only needed when Lambda calls Bedrock KB |
| Lambda (3 functions) | ~$0 | Pay per request, near zero at low traffic |
| Bedrock KB | ~$0 | Pay per ingestion job + per query |
| S3 + CloudFront | ~$1 | Minimal storage + CDN |
| API Gateway | ~$1 | Pay per request |
| SQS + DLQ | ~$0 | Near zero at low volume |
| CloudWatch + X-Ray | ~$2 | Log storage + traces |
| Cognito | ~$0 | Free up to 50,000 MAU |
| Secrets Manager | ~$1 | $0.40 per secret |
| KMS | ~$1 | $1 per key/month |
| **Total** | **~$44-50/month** | |

---

## Job States (Ingestion)

Every document ingestion is a tracked job via SQS + Bedrock KB:

```
PENDING → RUNNING → COMPLETED
                 ↘ FAILED → DLQ (dead-letter queue)
                              ↓
                         CloudWatch Alarm
                              ↓
                         SNS → Slack + Email
```

- **Retry policy**: SQS retries 3 times with backoff before sending to DLQ
- **Idempotency**: Bedrock KB tracks ETags - re-uploading the same document does not create duplicates
- **Monitoring**: CloudWatch metric `IndexStalenessRate` shows how many S3 documents are not yet in Aurora

---

## RAG Quality Evaluation (RAGAS)

Evaluation module that measures retrieval and generation quality - not just whether the system runs, but whether it produces good answers:

| Metric | What it measures |
|---|---|
| Faithfulness | Is the answer grounded in retrieved context? |
| Answer Relevancy | Is the answer relevant to the question? |
| Context Precision | Are the retrieved chunks actually useful? |
| Context Recall | Were all relevant chunks retrieved? |

Runs automatically in CI/CD after every staging deploy. Blocks prod deployment if scores drop below threshold.

---

## Data Model

### S3 - raw documents
```
s3://bucket/{tenant_id}/{doc_id}/{filename.pdf}
```

### SQS - trigger message
```json
{
  "doc_id": "uuid-1234",
  "s3_key": "tenant-abc/uuid-1234/report.pdf",
  "tenant_id": "tenant-abc",
  "uploaded_at": "2026-03-19T10:00:00Z",
  "content_type": "application/pdf"
}
```

### Aurora pgvector - managed by Bedrock KB

Bedrock Knowledge Bases creates and manages the vector table in Aurora automatically. The schema follows the Bedrock KB format with chunk content, embedding vector (1536 dims), source metadata, and document ID.

### Secrets Manager
```
/rag-pipeline/aurora-connection-string   → "postgresql://user:pass@host/db"
```

### SSM Parameter Store (per environment)
```
/rag-pipeline/dev/bedrock_kb_id          → "rag-kb-dev-xxxx"
/rag-pipeline/dev/bedrock_embed_model    → "amazon.titan-embed-text-v2"
/rag-pipeline/dev/bedrock_llm_model      → "anthropic.claude-3-haiku-20240307-v1:0"
/rag-pipeline/prod/bedrock_kb_id         → "rag-kb-prod-xxxx"
```

### CloudWatch Logs - structured JSON
```json
{
  "event": "retrieval_complete",
  "query_id": "uuid-5678",
  "kb_id": "rag-kb-prod",
  "latency_ms": 340,
  "chunks_retrieved": 5,
  "environment": "prod"
}
```

---

## Project Structure

```
rag-production-pipeline/
├── ingestion/          # Lambda: presigned URL + ingestion trigger
├── retrieval/          # Lambda: calls Bedrock RetrieveAndGenerate
├── evaluation/         # RAGAS evaluation pipeline
├── observability/      # CloudWatch metrics publisher, structured logger
├── infra/              # Terraform modules and environments
│   ├── modules/
│   │   ├── networking/      # VPC, subnets, NAT Gateway (toggle on/off)
│   │   ├── ingestion/       # S3, SQS, S3 event notifications
│   │   ├── knowledge_base/  # Bedrock KB + Aurora pgvector config
│   │   ├── retrieval/       # API Gateway, Lambda
│   │   ├── security/        # Cognito, Secrets Manager, KMS, WAF, IAM
│   │   ├── frontend/        # S3 static hosting, CloudFront
│   │   └── observability/   # CloudWatch, SNS, X-Ray
│   └── envs/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── frontend/           # React app (upload UI + chat interface)
├── tests/              # Unit + integration tests
├── images/             # Architecture diagrams
├── diagram.py          # Generates images/architecture.png (pip install diagrams)
├── .github/workflows/  # GitHub Actions CI/CD
└── docker-compose.yml  # Local dev (Postgres + pgvector + LocalStack)
```

---

## Local Development

```bash
# Start local stack (Postgres + pgvector + LocalStack for AWS services)
docker-compose up

# Install dependencies
pip install -r requirements.txt

# Test ingestion trigger locally
python ingestion/trigger.py

# Test retrieval locally
python retrieval/handler.py

# Run tests
pytest tests/

# Run RAGAS evaluation
python evaluation/run.py

# Regenerate architecture diagram
python diagram.py
```

---

## Status

> Architecture defined. Implementation in progress.

---

## References

- [Amazon Bedrock Knowledge Bases](https://aws.amazon.com/bedrock/knowledge-bases/)
- [pgvector HNSW indexing](https://github.com/pgvector/pgvector)
- [RAGAS - RAG Evaluation Framework](https://docs.ragas.io/)
- [Data Pipelines for Production RAG](https://www.linkedin.com/pulse/data-pipelines-production-rag-ashish-kumar-clu3c/)
