# Roadmap

Three phases: ship a working secure pipeline first, harden it for real traffic, then improve retrieval quality with ML techniques. Phase 3 changes are gated behind RAGAS benchmarks - nothing ships without measured improvement.

---

## Phase 1 - Core (this repo)

Foundational: pipeline works end-to-end, is secure, and observable.

| # | Item | Status |
|---|---|---|
| 1 | Bedrock KB ingestion (chunking + Titan embeddings + Aurora pgvector) | Architecture defined |
| 2 | Three thin Lambdas (presigned URL, ingestion trigger, retrieval) | Architecture defined |
| 3 | Cognito invite-only + OTP MFA | Architecture defined |
| 4 | DynamoDB: job status + session history (one item per turn) + document registry | Architecture defined |
| 5 | SQS FIFO + MessageDeduplicationId = S3 ETag | Architecture defined |
| 6 | Presigned URL: content-length-range (50MB) + content-type enforcement | In progress |
| 7 | Terraform remote backend (S3 state + DynamoDB lock) | In progress |
| 8 | Structured JSON logging across all Lambdas | In progress |
| 9 | CloudWatch Alarms (error rate, p99 latency, DLQ depth, Aurora CPU) | In progress |
| 10 | WAF Managed Rules (CommonRuleSet + KnownBadInputs) | In progress |
| 11 | Locust load tests (`tests/load/`) - p95 latency and error rate under concurrent traffic | In progress |
| 12 | Right to be forgotten - `DELETE /documents/{doc_id}` cascades across S3, Aurora chunks, DynamoDB jobs and sessions (GDPR art. 17) | Planned |
| 13 | Audit logging - DynamoDB `audit_events` table tracking who uploaded what and who queried what, when | Planned |
| 14 | RAGAS golden dataset - `evaluation/golden_dataset.json` with 20-30 question/ground-truth pairs required to run quality evaluation | Planned |

---

## Phase 2 - Production Hardening

Security and reliability improvements before handling real multi-tenant traffic.

**1. Re-ranking (Cohere Rerank API)**

Bedrock KB returns top-K chunks sorted by cosine similarity. The problem: a highly relevant chunk can sit at rank 6 and never make it into the LLM context window, causing the model to answer from incomplete context. Re-ranking re-scores the top-K candidates with a cross-encoder model (Cohere Rerank) which understands query-chunk semantic relationships better than raw vector similarity. Result: better recall without increasing chunk count or cost significantly.

**2. Document ACL + metadata filtering**

Every authenticated user can currently retrieve content from every document in the knowledge base. A proper multi-tenant system must isolate documents per user or per tenant. Bedrock KB supports metadata filters on retrieval - we add `user_id` as chunk metadata at ingestion time and filter by it on every retrieve call. No cross-tenant data leakage, no code changes to Aurora.

**3. VPC Endpoints for Bedrock (replace NAT Gateway)**

The NAT Gateway is a single point of failure and a $32/month fixed cost regardless of traffic. If it goes down, both ingestion and retrieval stop working. VPC Interface Endpoints (PrivateLink) for Bedrock and Secrets Manager eliminate the public internet path entirely - traffic stays inside the AWS backbone, there is no single gateway to fail, and at higher query volumes per-GB cost is lower than NAT data processing fees. Also a prerequisite for HIPAA/SOC2 compliance. See [ADR 002](adr/002-nat-gateway-vs-vpc-endpoints.md).

**4. Rate limiting per user (API Gateway Usage Plans)**

A single malicious or buggy client can spam `/query` and generate unbounded Bedrock API costs - every query costs money regardless of who sends it. API Gateway Usage Plans assign a quota per API key (e.g. 1,000 queries/day) and throttle at 100 requests/minute per key. Protects both cost and availability without touching Lambda code.

**5. Document versioning (delete old chunks before re-index)**

When a user re-uploads a modified document, Bedrock KB creates new chunks but does not delete the old ones. Aurora ends up with two versions of the same document - the LLM receives contradictory context and produces inconsistent answers. Fix: the ingestion trigger Lambda deletes all chunks for `doc_id` from Aurora before calling `start_ingestion_job`.

**6. AWS Fault Injection Simulator (chaos engineering)**

The system claims to be resilient but resilience without evidence is just a hypothesis. AWS FIS lets us run controlled experiments against the real infrastructure: terminate the NAT Gateway while queries are in flight, inject latency on Aurora connections, throttle Bedrock API calls. Each experiment has a defined steady state (p99 < 2s, error rate < 1%), a hypothesis ("the system degrades gracefully"), and a rollback condition. Results are documented as evidence that the architecture handles real failure modes - not just theoretical ones. Pairs directly with the VPC Endpoints migration (item 3): run the same NAT Gateway termination experiment before and after to prove the improvement.

**7. Virus scanning (ClamAV Lambda Layer)**

Files go from browser directly to S3 via presigned URL - our code never inspects the content. A malicious PDF can contain embedded scripts or exploit payloads that get processed by Bedrock. A ClamAV Lambda Layer triggered on `s3:ObjectCreated` scans each file before it reaches SQS. Infected files are moved to a quarantine prefix and DynamoDB is updated to `status: QUARANTINED`.

---

## Phase 3 - Advanced ML

Retrieval quality improvements. Each change is validated against a fixed golden dataset using RAGAS before merging. A drop of more than 2% in any metric blocks promotion to prod.

**1. HyDE - Hypothetical Document Embeddings**

Short or ambiguous queries (e.g. "revenue?") produce poor embeddings because the query vector is geometrically far from document chunk vectors in the embedding space. HyDE fixes this: use Claude to generate a hypothetical answer to the question first, then embed that hypothetical answer instead of the raw query. The generated text lives in the same semantic space as real document chunks, improving recall significantly on short queries without any changes to the index or Aurora.

**2. Semantic caching (ElastiCache Redis)**

Every `/query` call hits Bedrock - embedding generation + HNSW search + LLM inference - even for identical or near-identical questions. Semantic caching stores query embeddings and answers in Redis. On each new query: compute embedding, check Redis for a cached entry with cosine similarity > 0.95. If found, return instantly at zero Bedrock cost. At moderate volumes (1,000+ queries/day) this cuts Bedrock costs by 40-60% with no quality loss for repeated questions.

**3. Model routing (Haiku → Sonnet)**

Claude 3 Haiku handles simple factual questions well but struggles with complex multi-step reasoning or synthesis across many chunks. A lightweight classifier (query length + question word count + keyword heuristics) routes simple questions to Haiku ($0.00025/1K input) and complex ones to Claude 3 Sonnet ($0.003/1K input). Impact measured before/after via RAGAS Answer Relevancy - only ship if improvement justifies cost increase.

**4. RAGAS automated evaluation pipeline**

All Phase 3 changes must be validated against a fixed golden dataset before merging. CI/CD runs RAGAS after every staging deploy - Faithfulness, Answer Relevancy, Context Precision, Context Recall - and blocks prod if any metric drops more than 2% from the Phase 1 baseline.
