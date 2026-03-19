# ADR 001 - Aurora pgvector vs OpenSearch Serverless

**Status:** Accepted
**Date:** 2026-03-19

---

## Context

Bedrock Knowledge Bases requires a vector store. AWS offers two managed options:

- **Amazon OpenSearch Serverless** - the default option in Bedrock KB wizard
- **Aurora PostgreSQL + pgvector** - supported but requires manual setup

We needed to choose one for the production RAG pipeline.

---

## Decision

**Aurora PostgreSQL Serverless v2 + pgvector.**

---

## Reasoning

### Cost

OpenSearch Serverless has a minimum billing unit of 2 OCUs (one indexing + one search), each at ~$0.24/hour.

```
OpenSearch Serverless minimum:
  2 OCU x $0.24/h x 720h = $345.60/month
  (regardless of traffic)
```

Aurora Serverless v2 minimum is 0.5 ACU at $0.12/ACU-hour:

```
Aurora Serverless v2 minimum:
  0.5 ACU x $0.12/h x 720h = $43.20/month
```

For a portfolio project or low-traffic production system, a $300/month difference is decisive.

### Operational Simplicity

Aurora is standard PostgreSQL. The full pgvector and HNSW documentation applies directly. Any PostgreSQL DBA or developer can work with it without learning OpenSearch query DSL.

Metadata filtering in Aurora is a standard SQL `WHERE` clause. In OpenSearch it is a separate query syntax with different behavior.

### HNSW Performance

pgvector's HNSW implementation achieves 10-50ms approximate nearest-neighbor search at 1M+ vectors:

```sql
CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

For the document volumes typical of this use case (up to ~5M chunks), HNSW on pgvector is sufficient and well-benchmarked.

### Bedrock KB Compatibility

Bedrock Knowledge Bases supports Aurora pgvector as a vector store via the `BEDROCK_KNOWLEDGE_BASE` schema. The KB creates and manages the table schema automatically - no manual DDL required after initial setup.

---

## Trade-offs Accepted

| | Aurora pgvector | OpenSearch Serverless |
|---|---|---|
| Min cost/month | ~$43 | ~$345 |
| Max vectors (practical) | ~10M | Virtually unlimited |
| Full-text search | Basic (`tsvector`) | Excellent (BM25) |
| Hybrid search (vector + BM25) | Complex to implement | Native |
| Operational familiarity | High (SQL) | Lower (OpenSearch DSL) |
| Cold start | Yes (0.5 ACU minimum helps) | No |

**Hybrid search** (vector similarity + BM25 keyword relevance) is the main capability we give up. It becomes relevant when queries contain specific product codes, names, or identifiers that pure vector search misses. This is a Phase 3 consideration if RAGAS Context Recall metrics show a gap.

---

## Consequences

- Aurora cluster must be provisioned before Bedrock KB is configured (KB needs the endpoint + credentials at creation time)
- Aurora min ACU = 0.5 to avoid cold starts on first query after idle period
- Phase 2 adds metadata filtering by `user_id` via standard SQL `WHERE` clause
