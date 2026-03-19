# RAG Production Pipeline

Production-grade Retrieval-Augmented Generation pipeline on AWS. CDC-based document ingestion via Bedrock Knowledge Bases, event-driven with S3 + SQS FIFO, vector storage on Aurora pgvector with HNSW indexing, session state on DynamoDB, full observability, deployed with Terraform and GitHub Actions CI/CD.

---

## Architecture

Two independent flows share the same Aurora pgvector database via Bedrock Knowledge Bases.

![Architecture](images/architecture_v9.png)

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
       |── writes document entry to DynamoDB { doc_id, user_id, status: PENDING }
       | returns signed S3 URL to client
       v
  Client uploads PDF directly to S3 (max 50MB, application/pdf only)
       |
       | S3 Event Notification
       v
  SQS FIFO Queue (+ DLQ, MessageDeduplicationId = S3 ETag)
       |
       v
  Lambda (ingestion trigger)
       |── bedrock.start_ingestion_job()
       |── writes to DynamoDB { doc_id, job_id, status: RUNNING }
       v
  Bedrock Knowledge Base
       |── reads document from S3
       |── chunks document (hierarchical strategy)
       |── embeds each chunk → Titan Embeddings v2 (1536 dims)
       |── upserts vectors → Aurora pgvector (HNSW index)
       v
  Lambda (ingestion trigger) polls job status
       |── writes to DynamoDB { doc_id, status: COMPLETE, chunk_count }
       v
  Aurora PostgreSQL Serverless v2


FLOW 2 - RETRIEVAL (user asks a question)
==========================================

  React Frontend
       |
       | POST /query + JWT + session_id
       v
  API Gateway (Cognito Authorizer validates JWT)
       |
       v
  Lambda (retrieval)
       |── reads last N turns from DynamoDB { session_id }
       | bedrock.retrieve_and_generate() with conversation history
       v
  Bedrock Knowledge Base
       |── embeds query → Titan Embeddings v2
       |── HNSW similarity search → Aurora pgvector
       |── top K chunks + question + history → Claude 3 Haiku
       v
  Lambda (retrieval)
       |── writes turn to DynamoDB { session_id, role, content, ttl }
       v
  natural language answer → React Frontend


WHERE THINGS LIVE
==================

  Outside VPC (AWS Managed):   API Gateway, S3, SQS, Bedrock Knowledge Base,
                                Titan Embeddings, Claude 3 Haiku,
                                Cognito, CloudFront, WAF, DynamoDB,
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

  DynamoDB (job status + session history + document registry)
  VPC + Private Subnets + NAT Gateway
  CloudWatch Logs + Metrics + Alarms + X-Ray
  Terraform (IaC) + GitHub Actions (CI/CD)
```

---

## Stack at a Glance - What We Use and Why

| Component | Technology | Why |
|---|---|---|
| Document storage | Amazon S3 | Infinitely scalable, cheap, triggers pipeline via events - no polling |
| CDC trigger | S3 Events + SQS FIFO | Event-driven - only new/changed documents trigger re-processing. FIFO deduplication by S3 ETag prevents double-indexing |
| Message queue | Amazon SQS FIFO | Decouples trigger from processing, built-in retry, dead-letter queue, exactly-once deduplication |
| Ingestion pipeline | Bedrock Knowledge Bases | Fully managed: chunking, embedding, indexing in one API call. Replaces 3 custom Lambdas |
| Embedding model | Bedrock Titan Embeddings v2 | 1536-dim vectors. AWS-native, IAM auth only, no external API keys |
| LLM inference | Bedrock Claude 3 Haiku | Called internally by Bedrock KB. AWS-native, no OpenAI dependency |
| Vector store | pgvector on Aurora PostgreSQL | HNSW index handles 1M+ vectors at 10-50ms. Scales to near-$0 when idle. SQL metadata filters |
| Database engine | Aurora Serverless v2 | Scales down to 0.5 ACU minimum (~$43/month). Multi-AZ, automatic failover |
| State & metadata | Amazon DynamoDB | Job status tracking, session history, document registry. Serverless, $0 at low volume |
| Compute | AWS Lambda | Three thin functions: presigned URL (15 lines), ingestion trigger (5 lines), retrieval (10 lines) |
| API layer | Amazon API Gateway | SSL, rate limiting, Cognito JWT authorization out of the box |
| Authentication | Amazon Cognito (invite-only) | Admin creates users, temporary password via email, OTP MFA on login |
| Secrets | AWS Secrets Manager | Aurora credentials fetched at runtime via IAM - no hardcoded credentials |
| Encryption at rest | AWS KMS | S3, Aurora, SQS all encrypted with KMS keys |
| Network isolation | VPC + Private Subnets | Aurora and Lambda not exposed to internet. NAT Gateway for outbound to Bedrock KB |
| Frontend | React on S3 + CloudFront | Static hosting, global CDN, HTTPS, WAF integration |
| DDoS / WAF | AWS WAF + Managed Rules | CommonRuleSet + KnownBadInputs blocks SQLi, XSS, rate-limits abuse |
| Structured logging | CloudWatch Logs (JSON) | Queryable logs - find errors by doc_id, measure p99 latency |
| Custom metrics | CloudWatch Metrics | IndexStalenessRate, EmbeddingPipelineLag, RetrievalLatencyP99 |
| Distributed tracing | AWS X-Ray | Traces full request path Lambda → Bedrock KB → Aurora |
| Alerting | CloudWatch Alarms + SNS | DLQ messages or error spikes fan out to Slack + email |
| RAG evaluation | RAGAS | Quality gate in CI/CD: Faithfulness, Relevancy, Context Precision, Context Recall |
| Infrastructure as code | Terraform | Modular, remote state on S3 + DynamoDB lock |
| CI/CD | GitHub Actions | Auto-deploy on push, runs tests + Terraform plan + RAGAS evaluation |
| Local development | Docker Compose + LocalStack | Full stack locally without AWS costs |

---

## Full AWS Stack - What We Use and Why

### Storage & Ingestion

**Amazon S3**
Stores raw documents (PDF, DOCX, TXT) uploaded by users. Presigned URL policy enforces `content-length-range` (max 50MB) and `content-type: application/pdf` — the Lambda never touches the file bytes, and users cannot bypass the size or type constraint.

**S3 Event Notifications → SQS FIFO**
This is our CDC (Change Data Capture) mechanism. S3 pushes a message to SQS the moment a file is uploaded. We use a FIFO queue with `MessageDeduplicationId = S3 ETag` so re-uploading the same file never triggers two ingestion jobs for the same content.

**Amazon SQS FIFO**
Buffer between S3 trigger and the ingestion Lambda. If Lambda fails, messages wait and retry automatically. The dead-letter queue captures messages that fail after max retries - nothing is silently lost.

---

### Processing - Bedrock Knowledge Bases

**Amazon Bedrock Knowledge Bases**
The core of the ingestion pipeline. One API call (`start_ingestion_job`) does everything:

1. Reads the document from S3
2. Chunks it using the configured strategy (hierarchical by default)
3. Embeds each chunk using Titan Embeddings v2
4. Upserts vectors into Aurora pgvector using `INSERT ... ON CONFLICT DO UPDATE`

This replaces what would otherwise be three custom Lambdas (chunking, embedding, indexing).

For retrieval, `retrieve_and_generate` does:
1. Embeds the user query using Titan
2. Runs HNSW similarity search on Aurora pgvector
3. Passes top K chunks + conversation history + user question to Claude 3 Haiku
4. Returns a natural language answer grounded in the retrieved context

**AWS Bedrock - Titan Embeddings v2**
Converts text to 1536-dimensional vectors. AWS-native - no external API keys, IAM auth only.

**AWS Bedrock - Claude 3 Haiku**
Called by Bedrock KB after retrieval to generate the final answer. AWS-native, no OpenAI dependency.

---

### Vector Store

**pgvector on Aurora PostgreSQL Serverless v2**
We chose Aurora pgvector over the default Bedrock KB option (OpenSearch Serverless) because:
- Aurora Serverless v2 min 0.5 ACU → ~$43/month (OpenSearch Serverless ~$345/month minimum)
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

### State & Metadata - DynamoDB

Three logical tables in a single DynamoDB deployment:

**`documents` table** — registry of all uploaded files
```json
{
  "doc_id": "uuid-1234",
  "user_id": "giovanni",
  "filename": "annual_report.pdf",
  "s3_key": "uploads/uuid-1234.pdf",
  "status": "INDEXED",
  "chunk_count": 47,
  "uploaded_at": "2026-03-19T10:00:00Z"
}
```

**`jobs` table** — ingestion job lifecycle tracking
```json
{
  "job_id": "bedrock-job-xyz",
  "doc_id": "uuid-1234",
  "status": "COMPLETE",
  "started_at": "2026-03-19T10:01:00Z",
  "completed_at": "2026-03-19T10:03:00Z"
}
```

**`sessions` table** — conversation history per user (TTL = 24h)
```json
{
  "session_id": "sess-abc",
  "user_id": "giovanni",
  "turns": [
    {"role": "user", "content": "What is the revenue for 2025?"},
    {"role": "assistant", "content": "The 2025 revenue was..."}
  ],
  "ttl": 1742400000
}
```

---

### API & Frontend

**Amazon API Gateway**
Single gateway with two routes: `POST /upload-url` and `POST /query`. Handles SSL termination, rate limiting, and Cognito JWT authorization.

**Lambda (presigned URL) — 15 lines**
Generates a signed S3 URL so the client uploads directly to S3. Writes the document entry to DynamoDB with `status: PENDING` before returning the URL.

**Lambda (ingestion trigger) — ~20 lines**
Triggered by SQS FIFO. Calls `bedrock.start_ingestion_job()`, writes `status: RUNNING` to DynamoDB, polls until complete, updates to `COMPLETE` or `FAILED`.

**Lambda (retrieval) — ~20 lines**
Reads session history from DynamoDB, calls `bedrock.retrieve_and_generate()` with context, writes the new turn back to DynamoDB.

**Amazon CloudFront + S3 (Frontend)**
Serves the React frontend as a static site. CloudFront acts as CDN - HTTPS by default, WAF integration for DDoS protection.

---

### Security

**Amazon Cognito (invite-only)**
Admin creates users via `admin_create_user` — no self-signup. Cognito sends a temporary password by email. On first login, user sets a permanent password and enrolls MFA (OTP via email). API Gateway validates the Cognito JWT on every request — unauthenticated calls are rejected before reaching Lambda.

```hcl
resource "aws_cognito_user_pool" "main" {
  name = "rag-pipeline-users"
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  mfa_configuration = "ON"
}
```

**AWS Secrets Manager**
Stores Aurora DB credentials. Lambda functions fetch at runtime via IAM role - no hardcoded credentials. Automatic rotation enabled.

**AWS KMS**
Encrypts data at rest: S3, Aurora, SQS all use KMS-managed keys.

**VPC + Private Subnets**
Aurora and Lambda run in a private subnet - no direct internet exposure. Lambda reaches Bedrock KB via NAT Gateway.

**IAM Least Privilege**
Each Lambda has its own role with only the permissions it needs. The ingestion trigger Lambda can call Bedrock KB but cannot write to Aurora directly.

**AWS WAF + Managed Rules**
`AWSManagedRulesCommonRuleSet` blocks SQLi and XSS. `AWSManagedRulesKnownBadInputsRuleSet` blocks known malicious input patterns. Attached to both CloudFront and API Gateway.

---

### Observability

**CloudWatch Logs (structured JSON)**
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

**CloudWatch Metrics (custom namespace: `RAG/Pipeline`)**
- `IndexStalenessRate` - % of S3 documents not yet in Aurora
- `EmbeddingPipelineLag` - SQS queue depth over time
- `RetrievalLatencyP99` - p99 latency of RetrieveAndGenerate calls

**CloudWatch Alarms → SNS → Slack + Email**
- Lambda error rate > 1%
- Retrieval p99 latency > 5s
- DLQ message count > 0
- Aurora CPU > 80%

**AWS X-Ray**
Distributed tracing: Lambda → Bedrock KB → Aurora. Pinpoints where latency comes from.

---

### Infrastructure & CI/CD

**Terraform — Remote Backend**
State stored in S3 with DynamoDB locking — no local state file, safe for team collaboration:

```hcl
terraform {
  backend "s3" {
    bucket         = "rag-pipeline-tf-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}
```

**GitHub Actions**
1. Run pytest
2. Terraform plan
3. Deploy to staging
4. Run RAGAS evaluation (quality gate)
5. Manual approval
6. Deploy to prod

---

## Roadmap

The project is structured in three phases: ship a working, secure pipeline first — then harden it for real traffic — then improve retrieval quality with ML techniques.

### Phase 1 — Core (this repo)

Foundational: the pipeline works end-to-end, is secure, and is observable.

| # | Item | Status |
|---|---|---|
| 1 | Bedrock KB ingestion (chunking + Titan embeddings + Aurora pgvector) | ✅ Architecture defined |
| 2 | Three thin Lambdas (presigned URL, ingestion trigger, retrieval) | ✅ Architecture defined |
| 3 | Cognito invite-only + OTP MFA | ✅ Architecture defined |
| 4 | DynamoDB: job status + session history + document registry | 🔧 In progress |
| 5 | SQS FIFO + MessageDeduplicationId = S3 ETag | 🔧 In progress |
| 6 | Presigned URL: content-length-range (50MB) + content-type enforcement | 🔧 In progress |
| 7 | Terraform remote backend (S3 state + DynamoDB lock) | 🔧 In progress |
| 8 | Structured JSON logging across all Lambdas | 🔧 In progress |
| 9 | CloudWatch Alarms (error rate, p99 latency, DLQ depth, Aurora CPU) | 🔧 In progress |
| 10 | WAF Managed Rules (CommonRuleSet + KnownBadInputs) | 🔧 In progress |

---

### Phase 2 — Production Hardening

Security and reliability improvements before handling real multi-tenant traffic.

**1. Re-ranking (Cohere Rerank API)**
Bedrock KB returns top-K chunks sorted by cosine similarity. The problem: a highly relevant chunk can sit at rank 6 and never make it into the LLM context window. Re-ranking re-scores the top-K candidates using a cross-encoder model (Cohere Rerank) which understands query-chunk semantic relationships better than raw vector similarity. Result: better recall without increasing chunk count.

**2. Document ACL + metadata filtering**
Today every authenticated user can retrieve content from every document in the knowledge base. A proper multi-tenant system must isolate documents per user or per tenant. Bedrock KB supports metadata filters on retrieval — we add `user_id` as chunk metadata at ingestion time and filter by it on every retrieve call. No cross-tenant data leakage.

**3. VPC Endpoints for Bedrock (replace NAT Gateway)**
The NAT Gateway is both a single point of failure and a $32/month fixed cost. If it goes down, both ingestion and retrieval stop working. VPC Interface Endpoints (PrivateLink) for Bedrock and Secrets Manager eliminate the public internet path entirely — traffic stays inside the AWS network, there is no single gateway to fail, and at higher query volumes the per-GB cost is lower than NAT data processing fees. Also required for HIPAA/SOC2 compliance.

**4. Rate limiting per user (API Gateway Usage Plans)**
A single malicious or buggy client can spam `/query` and generate unbounded Bedrock API costs. API Gateway Usage Plans let us assign a quota per API key (e.g. 1,000 queries/day per user) and throttle at 100 requests/minute per key. Protects both cost and availability.

**5. Document versioning (delete old chunks before re-index)**
When a user re-uploads a modified version of a document, Bedrock KB creates new chunks but does not delete the old ones. Aurora ends up with two versions of the same document — the LLM receives contradictory context and produces inconsistent answers. Fix: the ingestion trigger Lambda deletes all existing chunks for the `doc_id` before calling `start_ingestion_job`.

**6. Virus scanning (ClamAV Lambda Layer)**
Users upload files directly to S3 via presigned URL — the content is never inspected by our code. A malicious PDF could contain embedded scripts or exploit payloads that later get processed by Bedrock. A ClamAV Lambda Layer triggered on S3 `ObjectCreated` events scans the file before it reaches the SQS ingestion queue. Infected files are quarantined to a separate S3 prefix and the document entry in DynamoDB is marked `QUARANTINED`.

---

### Phase 3 — Advanced ML

Retrieval quality improvements. Each change is gated behind RAGAS benchmarks — we only ship what measurably improves Faithfulness, Context Recall, or Answer Relevancy.

**1. HyDE — Hypothetical Document Embeddings**
Short or ambiguous queries (e.g. "revenue?") produce poor embeddings because the query vector is far from the document chunk vectors in the embedding space. HyDE fixes this: ask Claude to generate a hypothetical answer to the question first, then embed that hypothetical answer instead of the raw query. The hypothetical answer lives in the same semantic space as real document chunks, improving retrieval recall significantly on short queries.

**2. Semantic caching (ElastiCache Redis)**
Every `/query` call hits Bedrock — embedding generation + HNSW search + LLM inference — even for identical or near-identical questions. Semantic caching stores query embeddings and answers in Redis. On each new query, we compute the embedding and search Redis for a cached entry with cosine similarity > 0.95. If found, return the cached answer instantly at zero Bedrock cost. At moderate query volumes (1,000+ queries/day) this can cut Bedrock costs by 40-60%.

**3. Model routing (Haiku → Sonnet)**
Claude 3 Haiku handles simple factual questions well but struggles with complex multi-step reasoning or synthesis across many chunks. A lightweight classifier (based on query length, question word count, and keyword heuristics) routes simple questions to Haiku ($0.00025/1K input) and complex ones to Claude 3 Sonnet ($0.003/1K input). Measured impact before/after via RAGAS Answer Relevancy.

**4. RAGAS automated evaluation pipeline**
All Phase 3 changes must be validated against a fixed golden dataset before merging. The CI/CD pipeline runs RAGAS evaluation on staging after every deploy — Faithfulness, Answer Relevancy, Context Precision, Context Recall — and blocks promotion to prod if any metric drops more than 2% from baseline.

---

## Architectural Decision: NAT Gateway vs VPC Endpoints

Lambda in the private subnet needs outbound access to reach Bedrock Knowledge Bases (outside VPC).

### Option A - NAT Gateway (Phase 1)

| Service | Cost/month |
|---|---|
| NAT Gateway (fixed) | ~$32 |
| NAT Gateway (traffic, low volume) | ~$1 |
| S3 Gateway Endpoint | Free |
| **Total** | **~$33/month** |

Simple to set up. Single point of failure. Traffic exits to the internet.

### Option B - VPC Interface Endpoints (Phase 2)

Required for HIPAA, PCI-DSS, SOC2, FedRAMP where data must never leave the AWS private network.

| Endpoint | Cost (2 AZ) |
|---|---|
| Bedrock | ~$14.60/month |
| Secrets Manager | ~$14.60/month |
| CloudWatch Logs | ~$14.60/month |
| X-Ray | ~$14.60/month |
| S3 (Gateway) | Free |
| **Total** | **~$58/month** |

No SPOF. No internet path. Higher fixed cost but lower at volume.

**Phase 1 uses NAT Gateway. Phase 2 migrates to VPC Endpoints.**

---

## Cost Breakdown (Portfolio / Low Traffic)

| Service | Cost/month | Note |
|---|---|---|
| Aurora Serverless v2 | ~$43 | Min 0.5 ACU always active. Snapshot + restore when not in use to cut cost |
| NAT Gateway | ~$33 | Can be destroyed between demos: `terraform destroy -target=module.networking` |
| Lambda (3 functions) | ~$0 | Pay per request, near zero at low traffic |
| Bedrock KB | ~$0 | Pay per ingestion job + per query |
| DynamoDB | ~$0 | On-demand, near zero at low volume |
| S3 + CloudFront | ~$1 | Minimal storage + CDN |
| API Gateway | ~$1 | Pay per request |
| SQS FIFO + DLQ | ~$0 | Near zero at low volume |
| CloudWatch + X-Ray | ~$2 | Log storage + traces |
| Cognito | ~$0 | Free up to 50,000 MAU |
| Secrets Manager | ~$1 | $0.40 per secret |
| KMS | ~$1 | $1 per key/month |
| **Total** | **~$80/month** | Destroy NAT GW + snapshot Aurora → drops to ~$5/month |

**Cost for a one-day ingestion test with 200 PDF documents:**
- Titan Embeddings: ~$0.03
- Aurora (24h active): ~$1.44
- NAT Gateway (24h): ~$1.08
- Test queries (100): ~$0.11
- **Total: ~$3**

---

## Job States (Ingestion)

```
PENDING → RUNNING → COMPLETE
                 ↘ FAILED → DLQ (dead-letter queue)
                              ↓
                         CloudWatch Alarm
                              ↓
                         SNS → Slack + Email
```

- **Retry policy**: SQS retries 3 times with backoff before sending to DLQ
- **Deduplication**: FIFO queue with `MessageDeduplicationId = S3 ETag` - re-uploading same content never triggers two jobs
- **Status tracking**: DynamoDB `jobs` table - poll `GET /status/{doc_id}` to know when a document is ready to query
- **Monitoring**: CloudWatch metric `IndexStalenessRate` shows % of S3 documents not yet indexed

---

## RAG Quality Evaluation (RAGAS)

Evaluation module that measures retrieval and generation quality - not just whether the system runs, but whether it produces good answers:

| Metric | What it measures |
|---|---|
| Faithfulness | Is the answer grounded in retrieved context? (no hallucinations) |
| Answer Relevancy | Is the answer relevant to the question asked? |
| Context Precision | Are the retrieved chunks actually useful for this question? |
| Context Recall | Were all relevant chunks retrieved? (nothing important missed) |

Runs automatically in CI/CD after every staging deploy. Blocks prod deployment if scores drop below threshold. Used in Phase 3 to validate every ML improvement before shipping.

---

## Data Model

### S3 - raw documents
```
s3://rag-pipeline-docs/{user_id}/{doc_id}/{filename.pdf}
```

### SQS FIFO - trigger message
```json
{
  "doc_id": "uuid-1234",
  "s3_key": "giovanni/uuid-1234/report.pdf",
  "user_id": "giovanni",
  "uploaded_at": "2026-03-19T10:00:00Z",
  "content_type": "application/pdf"
}
```

### DynamoDB - documents table
```json
{
  "doc_id": "uuid-1234",
  "user_id": "giovanni",
  "filename": "annual_report.pdf",
  "status": "INDEXED",
  "chunk_count": 47,
  "uploaded_at": "2026-03-19T10:00:00Z"
}
```

### DynamoDB - sessions table (TTL = 24h)
```json
{
  "session_id": "sess-abc",
  "user_id": "giovanni",
  "turns": [
    {"role": "user", "content": "What is the revenue for 2025?"},
    {"role": "assistant", "content": "The 2025 revenue was $4.2B..."}
  ],
  "ttl": 1742400000
}
```

### Aurora pgvector - managed by Bedrock KB

Bedrock Knowledge Bases creates and manages the vector table automatically. Schema: chunk content, embedding vector (1536 dims), source metadata, document ID, user ID (Phase 2: metadata filter).

### Secrets Manager
```
/rag-pipeline/aurora-connection-string   → "postgresql://user:pass@host/db"
```

### Terraform Backend
```
s3://rag-pipeline-tf-state/prod/terraform.tfstate   → state file
DynamoDB table: terraform-state-lock                 → prevents concurrent applies
```

---

## Project Structure

```
rag-production-pipeline/
├── ingestion/          # Lambda: presigned URL + ingestion trigger
├── retrieval/          # Lambda: calls Bedrock RetrieveAndGenerate + session mgmt
├── evaluation/         # RAGAS evaluation pipeline
├── observability/      # CloudWatch metrics publisher, structured logger
├── infra/              # Terraform modules and environments
│   ├── modules/
│   │   ├── networking/      # VPC, subnets, NAT Gateway (toggle on/off)
│   │   ├── ingestion/       # S3, SQS FIFO, S3 event notifications
│   │   ├── knowledge_base/  # Bedrock KB + Aurora pgvector config
│   │   ├── metadata/        # DynamoDB tables (jobs, sessions, documents)
│   │   ├── retrieval/       # API Gateway, Lambda
│   │   ├── security/        # Cognito, Secrets Manager, KMS, WAF, IAM
│   │   ├── frontend/        # S3 static hosting, CloudFront
│   │   └── observability/   # CloudWatch, SNS, X-Ray
│   ├── backend.tf           # Remote state: S3 + DynamoDB lock
│   └── envs/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── frontend/           # React app (upload UI + chat interface)
├── tests/              # Unit + integration tests
├── images/             # Architecture diagrams
├── diagram.py          # Generates images/architecture_v9.png
├── .github/workflows/  # GitHub Actions CI/CD
└── docker-compose.yml  # Local dev (Postgres + pgvector + LocalStack)
```

---

## Local Development

```bash
# Start local stack (Postgres + pgvector + LocalStack for S3/SQS/DynamoDB)
docker-compose up

# Install dependencies
pip install -r requirements.txt

# Test embedding directly against Bedrock (requires AWS credentials)
python ingestion/test_embedding.py

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

> Phase 1 architecture defined. Implementation in progress.

---

## References

- [Amazon Bedrock Knowledge Bases](https://aws.amazon.com/bedrock/knowledge-bases/)
- [pgvector HNSW indexing](https://github.com/pgvector/pgvector)
- [RAGAS - RAG Evaluation Framework](https://docs.ragas.io/)
- [HyDE - Precise Zero-Shot Dense Retrieval](https://arxiv.org/abs/2212.10496)
- [Cohere Rerank API](https://docs.cohere.com/docs/rerank-2)
- [Data Pipelines for Production RAG](https://www.linkedin.com/pulse/data-pipelines-production-rag-ashish-kumar-clu3c/)
