# RAG Production Pipeline

Production-grade Retrieval-Augmented Generation pipeline on AWS. Handles document ingestion via CDC, chunk-level embedding with model versioning, batched vector indexing, metadata-aware retrieval, and full observability - deployed with Terraform and automated via GitHub Actions CI/CD.

---

## Architecture

Two independent flows share the same Aurora pgvector database.

![Architecture](images/architecture.png)

```
FLOW 1 - INGESTION (user uploads a document)
=============================================

  React Frontend
       |
       | login (username + password)
       v
  Cognito User Pool  ──── returns JWT token
       |
       | GET /upload-url  (JWT in header)
       v
  API Gateway  ──── Cognito Authorizer (validates JWT)
       |
       v
  Lambda  ──── generates S3 presigned URL
       |
       | upload document directly to S3 (presigned URL)
       v
  Amazon S3  ──── S3 Event Notification ────▶  SQS Queue
                                                    |
                                                    v
                                          Step Functions (trigger + state)
                                                    |
                                                    v
                                          Prefect Flow (Python orchestration)
                                                    |
                                     ┌──────────────┼──────────────┐
                                     v              v              v
                                  @task           @task          @task
                                Chunking        Embedding       Indexing
                                                   |                |
                                             AWS Bedrock       Aurora pgvector
                                          Titan Embed v2      (upsert + HNSW)


FLOW 2 - RETRIEVAL (user asks a question)
==========================================

  React Frontend
       |
       | HTTP request + JWT token
       v
  API Gateway  ──── Cognito Authorizer (validates JWT)
       |
       v
  FastAPI (Lambda + Mangum)
       |
       |── 1. convert query to vector (Bedrock Titan Embed v2)
       |── 2. HNSW similarity search + metadata filters (Aurora pgvector)
       |── 3. pass top K chunks + question to LLM (Bedrock Claude 3 Haiku)
       |
       v
  natural language answer  ──────────────────────▶  React Frontend


SHARED INFRASTRUCTURE
======================

  Aurora PostgreSQL Serverless v2 + pgvector
  (written by Flow 1 ingestion, read by Flow 2 retrieval)

  VPC + Private Subnets + NAT Gateway
  CloudWatch Logs + Metrics + Alarms + X-Ray
  Terraform (IaC) + GitHub Actions (CI/CD)
```

---

## Stack at a Glance - What We Use and Why

| Component | Technology | Why |
|---|---|---|
| Document storage | Amazon S3 | Infinitely scalable, cheap, triggers pipeline automatically via events - no polling |
| Ingestion trigger | S3 Events + SQS | CDC without polling - only new/changed documents trigger re-processing |
| Message queue | Amazon SQS | Decouples trigger from processing, built-in retry, dead-letter queue for failed messages |
| Pipeline orchestration | AWS Step Functions | Triggers the Prefect flow and tracks the overall job state (PENDING/RUNNING/COMPLETED/FAILED). Provides 90 days execution history and visual state graph |
| Data pipeline | Prefect | Orchestrates the ingestion steps (chunking, embedding, indexing) as Python tasks with @task decorator. Chosen over Airflow (too heavy) and raw Step Functions (JSON, hard to debug). Free cloud tier with UI showing every flow run, retry history, and logs |
| RAG evaluation | RAGAS | Runs in two places: (1) CI/CD quality gate after staging deploy - blocks prod if Faithfulness/Relevancy/Precision/Recall drop below threshold; (2) nightly Prefect scheduled flow that publishes scores to CloudWatch for continuous monitoring |
| Compute | AWS Lambda | Scales from 0 to 1000 concurrent executions automatically, costs nothing when idle, each function has its own least-privilege IAM role |
| Embedding model | AWS Bedrock - Titan Embeddings v2 | Converts text to 1536-dimensional vectors. Used twice: at ingestion (chunks) and at retrieval (user query). AWS-native - no external API keys, IAM auth only |
| LLM inference | AWS Bedrock - Claude 3 Haiku | Called only after pgvector retrieval to generate the final answer. AWS-native, no OpenAI dependency, IAM auth |
| Vector store | pgvector on Aurora PostgreSQL | Standard PostgreSQL with vector extension - no new infra to learn. HNSW index handles 1M+ vectors at 10-50ms. SQL filters for metadata. Upsert prevents duplicates |
| Database engine | Aurora Serverless v2 (auto-pause) | Scales to 0 when idle - costs ~$0 when not in use. Auto-scales up when traffic arrives |
| API layer | Amazon API Gateway | SSL, rate limiting, Cognito JWT authorization out of the box |
| Backend | FastAPI + Lambda (Mangum) | Python-native, fast to develop, Mangum adapter makes it run inside Lambda |
| Authentication | Amazon Cognito | Managed user auth (signup, login, JWT). API Gateway validates JWT on every request - no custom auth code |
| Secrets | AWS Secrets Manager | DB password and API keys fetched at runtime via IAM - no hardcoded credentials anywhere |
| Encryption at rest | AWS KMS | S3, Aurora, SQS all encrypted with KMS keys - required for compliance |
| Network isolation | VPC + Private Subnets | Aurora and Lambda not exposed to internet. NAT Gateway for outbound traffic to AWS services |
| NAT Gateway | AWS NAT Gateway | Allows Lambda in private subnet to reach Bedrock, SQS, Secrets Manager. Activated only when needed via `terraform apply -target=module.networking` |
| Frontend | React on S3 + CloudFront | Static hosting, global CDN, HTTPS by default, near-zero cost |
| DDoS / WAF | AWS WAF | Blocks SQL injection, XSS, rate-limits abuse on API Gateway and CloudFront |
| Structured logging | CloudWatch Logs (JSON) | Queryable logs with CloudWatch Log Insights - find errors by doc_id, measure p99 latency |
| Custom metrics | CloudWatch Metrics | Three pipeline metrics not covered by AWS defaults: IndexStalenessRate, EmbeddingPipelineLag, ChunkingStrategyCoverage |
| Alerting | CloudWatch Alarms + SNS | Triggers on error thresholds or DLQ messages - fans out to Slack and email immediately |
| Distributed tracing | AWS X-Ray | Traces full request path Lambda → Bedrock → Aurora - pinpoints where latency comes from |
| Config per environment | AWS SSM Parameter Store | Stores non-secret configuration (Bedrock model IDs, chunk size, environment URLs) separately per environment (dev/staging/prod). Unlike Secrets Manager it is not for passwords - it is for settings that change between environments but are not sensitive. Nearly free. |
| Infrastructure as code | Terraform | Cloud-agnostic, widely adopted, infrastructure diffs visible in pull requests. Modular structure allows turning NAT GW on/off independently |
| CI/CD | GitHub Actions | Auto-deploy on push, runs tests + Terraform plan + RAGAS evaluation before prod |
| RAG quality evaluation | RAGAS | Measures Faithfulness, Relevancy, Context Precision, Context Recall after every staging deploy - quality gate before prod |
| Local development | Docker Compose + LocalStack | Run the full stack locally without AWS costs - Postgres+pgvector + AWS service emulation |
| Architecture diagram | diagram.py (Python diagrams library) | Generates images/architecture.png with official AWS icons as code. Run `python diagram.py` to regenerate after architecture changes. Versioned in git alongside the code |

---

## Full AWS Stack - What We Use and Why

### Storage & Ingestion

**Amazon S3**
Stores raw documents (PDF, DOCX, TXT) uploaded by users. We use S3 because it's infinitely scalable, cheap ($0.023/GB), and natively integrates with event notifications - when a file lands in S3, the pipeline starts automatically without any polling logic.

**S3 Event Notifications → SQS**
Instead of polling S3 for new documents (wasteful and slow), we use event notifications: S3 pushes a message to SQS the moment a file is uploaded or modified. This is our CDC (Change Data Capture) mechanism - only changed documents trigger re-processing.

**Amazon SQS**
Acts as the buffer between the S3 trigger and the processing pipeline. If the pipeline is busy or a Lambda fails, messages wait in the queue and are retried automatically. SQS also provides the dead-letter queue (DLQ) where failed messages land after max retries - so nothing is silently lost.

---

### Processing

**AWS Step Functions**
Orchestrates the multi-step pipeline (chunking → embedding → indexing) as a state machine with explicit states: PENDING, RUNNING, COMPLETED, FAILED. We use Step Functions because it gives us a visual execution graph, built-in retry with exponential backoff, and 90 days of execution history - all without writing orchestration code from scratch.

**AWS Lambda**
Runs each pipeline step (chunking, embedding, indexing, retrieval) as isolated, stateless functions. We use Lambda because it scales automatically from 0 to 1000 concurrent executions, costs nothing when idle, and each function has its own IAM role with least-privilege permissions.

**AWS Bedrock - Titan Embeddings v2**
Used in two distinct moments of the pipeline:

1. **During ingestion** - every text chunk is converted into a vector of 1536 numbers that represents its semantic meaning. This is called embedding. Two chunks with similar meaning will produce numerically close vectors. These vectors are then stored (indexed) in Aurora pgvector with an HNSW index so they can be searched quickly.

2. **During retrieval** - when a user asks a question, pgvector does not understand words, only numbers. So before searching, the user query is converted into a vector using the same Titan model. pgvector then compares that query vector against all stored vectors and returns the most similar chunks.

```
"how does the refund work?" -> Titan -> [0.45, -0.12, 0.88, ...] -> pgvector HNSW search -> top 5 chunks
```

We use Titan Embeddings v2 and not alternatives like OpenAI text-embedding-3 because we do not want external dependencies outside AWS. Authentication is handled via IAM role - no API keys to manage or rotate. Requests are batched (up to 500 items per call) to reduce cost and latency by ~100x vs per-document calls.

**AWS Bedrock - Claude 3 Haiku**
Called only at the end of the retrieval phase, after pgvector has already found the relevant chunks. Claude does not search anything - it receives the top K chunks plus the original user question, reads them, and generates a natural language answer based exclusively on that context.

```
user question
    +
top 5 chunks from pgvector
    |
    v
Claude 3 Haiku on Bedrock
    |
    v
natural language answer grounded in your documents
```

We use Claude 3 on Bedrock (not OpenAI GPT) for the same reason as Titan - zero external dependencies, IAM-based auth, everything stays inside AWS.

---

### Vector Store

**pgvector on Aurora PostgreSQL Serverless v2**
Stores document chunks alongside their vector embeddings and metadata. We chose pgvector over dedicated vector databases (Qdrant, Pinecone, Weaviate) because:
- It runs on standard PostgreSQL - no new infrastructure to learn or operate
- Aurora Serverless v2 auto-pauses when idle → **costs ~$0 when not in use** (critical for a portfolio project)
- Supports HNSW indexing for fast approximate nearest-neighbor search at 1M+ vectors
- Metadata filters use standard SQL WHERE clauses - no proprietary query language
- Upsert via `INSERT ... ON CONFLICT DO UPDATE` prevents duplicates and HNSW graph fragmentation

**HNSW Index**
Hierarchical Navigable Small World - a graph-based index that enables fast similarity search without comparing all 1M vectors. Think of it as a multi-level map: start with wide jumps at the top level, zoom in at lower levels. Result: query latency of 10–50ms instead of seconds.

```sql
CREATE INDEX ON documents
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

---

### API & Frontend

**Amazon API Gateway**
Exposes the FastAPI retrieval endpoint to the internet. We use API Gateway because it handles SSL termination, rate limiting, and Cognito JWT authorization out of the box - without any additional infrastructure.

**FastAPI on Lambda (Mangum)**
Handles retrieval requests: takes a user query, converts it to an embedding, runs HNSW search on pgvector with metadata filters, and passes results to Claude 3 for response generation. Mangum is a thin adapter that makes FastAPI run inside Lambda.

**Amazon CloudFront + S3 (Frontend)**
Serves the React frontend (document upload UI + chat interface) as a static site. CloudFront acts as a CDN - low latency globally, HTTPS by default, and WAF integration for DDoS protection.

---

### Security

**Amazon Cognito**
Manages user authentication for the frontend (sign up, login, JWT tokens). API Gateway validates the Cognito JWT on every request - unauthenticated calls are rejected before they reach Lambda. We use Cognito because it's fully managed, supports MFA, and integrates natively with API Gateway.

**AWS Secrets Manager**
Stores sensitive credentials: Aurora DB password, any third-party API keys. Lambda functions fetch secrets at runtime via IAM role - no hardcoded credentials anywhere in the codebase. Secrets are automatically rotated.

**AWS KMS**
Encrypts data at rest: S3 buckets, Aurora DB, SQS queues all use KMS-managed keys. This ensures that even if someone gains physical access to the underlying storage, data is unreadable without the KMS key.

**VPC + Private Subnets**
Aurora and Lambda run inside a private VPC subnet - no direct internet exposure. Lambda reaches AWS services via NAT Gateway or VPC Endpoints depending on the environment. See the architectural decision below.

**IAM Least Privilege**
Each Lambda function has its own IAM role with only the permissions it needs. The chunking Lambda can read S3 but cannot call Bedrock. The embedding Lambda can call Bedrock but cannot write to S3. This limits blast radius if a function is compromised.

**AWS WAF**
Attached to CloudFront and API Gateway. Blocks common web attacks (SQL injection, XSS) and rate-limits requests to prevent abuse.

---

### Observability

**CloudWatch Logs**
All Lambda functions emit structured JSON logs automatically. We use structured logging (JSON instead of plain text) so logs are queryable with CloudWatch Log Insights - find all errors for a specific document, measure p99 latency, etc.

```json
{
  "event": "embedding_batch_complete",
  "doc_id": "abc123",
  "chunks": 12,
  "model": "amazon.titan-embed-text-v2",
  "model_version": "v2",
  "latency_ms": 1240,
  "environment": "prod"
}
```

**CloudWatch Metrics (custom namespace: `RAG/Pipeline`)**
We publish three pipeline-specific metrics that standard AWS metrics don't cover:
- `IndexStalenessRate` - % of S3 documents not yet reflected in pgvector
- `EmbeddingPipelineLag` - SQS queue depth over time (how far behind is the pipeline?)
- `ChunkingStrategyCoverage` - % of documents processed per chunking strategy

**CloudWatch Alarms → SNS → Slack + Email**
Alarms trigger when error rate exceeds threshold or DLQ receives messages. SNS fans out to both email and a Slack webhook - the team is notified immediately without checking dashboards.

**AWS X-Ray**
Distributed tracing across Lambda → Bedrock → Aurora. Pinpoints exactly where latency comes from in the end-to-end request path. Especially useful when debugging slow embedding calls or cold starts.

**Step Functions Execution History**
Every document ingestion is a tracked execution with visual state graph. You can see exactly which step failed, what the input/output was, and replay failed executions - without digging through logs.

---

### Infrastructure & CI/CD

**Terraform**
All AWS resources are defined as code in Terraform modules. We use Terraform (not AWS CDK) because it's cloud-agnostic, more widely adopted in the industry, and its declarative syntax makes infrastructure diffs easy to review in pull requests.

**GitHub Actions**
Automated pipeline that runs on every push to main:
1. Run pytest (unit + integration tests)
2. Terraform plan (show what will change)
3. Deploy to staging
4. Run RAGAS evaluation (measure RAG quality)
5. Manual approval gate
6. Deploy to prod

---

## Architectural Decision: NAT Gateway vs VPC Endpoints

This is a cost vs. security trade-off that depends on the environment.

### Option A - NAT Gateway (current setup)

Traffic from Lambda to AWS services (Bedrock, SQS, Secrets Manager) routes through a NAT Gateway and exits/re-enters the AWS network.

| Service | Cost/month |
|---|---|
| NAT Gateway (fixed) | ~$32 |
| NAT Gateway (traffic, low volume) | ~$1 |
| S3 Gateway Endpoint | Free |
| **Total networking** | **~$33/month** |

### Option B - VPC Interface Endpoints (highly regulated environments)

Traffic never leaves the AWS private network. Required for compliance frameworks like **HIPAA, PCI-DSS, SOC2, FedRAMP** where data exfiltration risk must be minimised at the network layer.

| Endpoint | Type | Cost (2 AZ) |
|---|---|---|
| S3 | Gateway | Free |
| Bedrock | Interface | ~$14.60/month |
| SQS | Interface | ~$14.60/month |
| Secrets Manager | Interface | ~$14.60/month |
| Step Functions | Interface | ~$14.60/month |
| CloudWatch Logs | Interface | ~$14.60/month |
| X-Ray | Interface | ~$14.60/month |
| **Total networking** | | **~$87/month** |

### Decision

| | NAT Gateway | VPC Endpoints |
|---|---|---|
| Traffic path | Exits AWS network | Stays inside AWS network |
| Security posture | Standard | Required for regulated industries |
| Cost (low traffic) | **~$33/month** | ~$87/month |
| Cost (>700GB/month) | Expensive ($0.045/GB) | **More economical** |
| Compliance (HIPAA/PCI) | Not sufficient | **Required** |

**This project uses NAT Gateway** - appropriate for a standard SaaS workload. For a healthcare, financial, or government deployment, the architecture would switch to full VPC Interface Endpoints to satisfy compliance requirements, accepting the higher fixed cost in exchange for zero data exfiltration risk.

---

## Cost Breakdown (Portfolio / Low Traffic)

| Service | Cost/month | Note |
|---|---|---|
| Aurora Serverless v2 (auto-pause) | ~$5–10 | Scales to 0 when idle |
| NAT Gateway | ~$33 | See networking decision above |
| Lambda + SQS + Step Functions | ~$1 | Pay per use, near zero at low traffic |
| S3 + CloudFront | ~$1 | Minimal storage + CDN |
| API Gateway | ~$1 | Pay per request |
| CloudWatch + X-Ray | ~$2 | Log storage + traces |
| Cognito | ~$0 | Free up to 50,000 MAU |
| Secrets Manager | ~$1 | $0.40 per secret |
| KMS | ~$1 | $1 per key/month |
| **Total** | **~$45–50/month** | |

---

## Job States (Step Functions)

Every document ingestion is a tracked job:

```
PENDING → RUNNING → COMPLETED
                 ↘ FAILED → DLQ (dead-letter queue)
                              ↓
                         CloudWatch Alarm
                              ↓
                         SNS → Slack + Email
```

- **Retry policy**: 3 attempts with exponential backoff (1s, 2s, 4s)
- **Dead-letter queue**: messages go to SQS DLQ after max retries
- **Execution history**: Step Functions console, last 90 days
- **Config per environment**: SSM Parameter Store (`/rag-pipeline/dev/`, `/rag-pipeline/prod/`)

---

## RAG Quality Evaluation (RAGAS)

The pipeline includes an evaluation module that measures retrieval and generation quality - not just whether the system runs, but whether it produces good answers:

| Metric | What it measures |
|---|---|
| Faithfulness | Is the answer grounded in retrieved context? |
| Answer Relevancy | Is the answer relevant to the question? |
| Context Precision | Are the retrieved chunks actually useful? |
| Context Recall | Were all relevant chunks retrieved? |

RAGAS evaluation runs automatically in CI/CD after every staging deploy.

---

## Data Model - What Goes Where

### S3 - raw documents
```
s3://bucket/{tenant_id}/{doc_id}/{filename.pdf}
```

### SQS - trigger message (on every S3 upload)
```json
{
  "doc_id": "uuid-1234",
  "s3_key": "tenant-abc/uuid-1234/report.pdf",
  "tenant_id": "tenant-abc",
  "uploaded_at": "2026-03-19T10:00:00Z",
  "content_type": "application/pdf"
}
```

### Aurora pgvector - two tables

```sql
-- documents: one row per uploaded file
CREATE TABLE documents (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  s3_key        TEXT,
  title         TEXT,
  content_type  TEXT,
  status        TEXT,        -- pending | processing | completed | failed
  created_at    TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ
);

-- chunks: one row per chunk, with vector embedding
CREATE TABLE chunks (
  id                      TEXT PRIMARY KEY,
  doc_id                  TEXT REFERENCES documents(id),
  tenant_id               TEXT,
  chunk_index             INTEGER,         -- position inside the document
  content                 TEXT,            -- raw text of the chunk
  embedding               vector(1536),    -- Titan Embeddings v2 output
  embedding_model         TEXT,            -- amazon.titan-embed-text-v2
  embedding_model_version TEXT,            -- for versioning / re-indexing
  metadata                JSONB,           -- access_level, tags, author, ...
  created_at              TIMESTAMPTZ
);

CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### Secrets Manager
```
/rag-pipeline/aurora-connection-string   → "postgresql://user:pass@host/db"
```

### SSM Parameter Store (per environment)
```
/rag-pipeline/dev/bedrock_embed_model    → "amazon.titan-embed-text-v2"
/rag-pipeline/dev/bedrock_llm_model      → "anthropic.claude-3-haiku-20240307-v1:0"
/rag-pipeline/dev/chunk_size             → "512"
/rag-pipeline/prod/bedrock_embed_model   → "amazon.titan-embed-text-v2"
```

### CloudWatch Logs - structured JSON per Lambda
```json
{
  "event": "chunk_embedded",
  "doc_id": "uuid-1234",
  "chunk_index": 3,
  "model": "amazon.titan-embed-text-v2",
  "latency_ms": 240,
  "environment": "prod"
}
```

---

## Project Structure

```
rag-production-pipeline/
├── ingestion/          # S3 event handling, CDC logic
├── chunking/           # Fixed-size, sentence-aware, hierarchical strategies
├── embedding/          # Batch embedding pipeline, model versioning
├── indexing/           # pgvector upsert, HNSW index management
├── retrieval/          # FastAPI app, metadata filters, reranking
├── flows/              # Prefect flows and tasks (ingestion pipeline)
├── orchestration/      # Step Functions state machine definitions
├── evaluation/         # RAGAS evaluation pipeline
├── observability/      # CloudWatch metrics publisher, structured logger
├── infra/              # Terraform modules and environments
│   ├── modules/
│   │   ├── networking/    # VPC, subnets, NAT Gateway (toggle on/off)
│   │   ├── ingestion/     # S3, SQS, S3 event notifications
│   │   ├── processing/    # Lambda functions, Step Functions
│   │   ├── storage/       # Aurora PostgreSQL, pgvector setup
│   │   ├── retrieval/     # API Gateway, Lambda
│   │   ├── security/      # Cognito, Secrets Manager, KMS, WAF, IAM
│   │   ├── frontend/      # S3 static hosting, CloudFront
│   │   └── observability/ # CloudWatch, SNS, X-Ray
│   └── envs/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── frontend/           # React app (upload UI + chat interface)
├── tests/              # Unit + integration tests
├── images/             # Architecture diagrams and AWS deployment screenshots
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

# Start Prefect server locally (pipeline UI on http://localhost:4200)
prefect server start

# Run the ingestion flow locally
python flows/ingestion_flow.py

# Run the retrieval API locally
uvicorn retrieval.main:app --reload

# Run tests
pytest tests/

# Regenerate architecture diagram
python diagram.py   # outputs to images/architecture.png
```

---

## Status

> Architecture defined. Implementation in progress.

---

## References

- [pgvector HNSW indexing](https://github.com/pgvector/pgvector)
- [AWS Bedrock Titan Embeddings v2](https://aws.amazon.com/bedrock/)
- [RAGAS - RAG Evaluation Framework](https://docs.ragas.io/)
- [Step Functions - Amazon States Language](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-amazon-states-language.html)
