# ADR 003 - Session Deletion Strategy on Right to Be Forgotten

**Status:** Accepted
**Date:** 2026-03-19

---

## Context

When a user invokes `DELETE /documents/{doc_id}`, all data related to that document must be erased (GDPR art. 17). Four stores are involved: S3, Aurora pgvector, DynamoDB `documents`, DynamoDB `jobs`.

The problem is **sessions**. Conversation history is stored in DynamoDB with:

```
Partition key: session_id
Sort key:      turn_id (ISO timestamp)
```

A session turn contains the assistant's answer, which may have been generated using chunks from the deleted document. There is no `doc_id` field in the sessions table - there is no direct link between a session turn and the source document.

Three options exist to handle this.

---

## Options

### Option A - TTL expiry (chosen)

Do nothing at delete time. Session turns already have a `ttl` attribute set to 24 hours from creation. DynamoDB deletes expired items automatically.

```python
# at session write time (already in place)
item["ttl"] = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
```

**Pro:**
- Zero additional code in the delete flow
- Zero cost - no scan, no extra writes
- Simple to reason about

**Con:**
- Window of up to 24 hours where a user could still see answers derived from deleted content
- DynamoDB TTL deletion is not guaranteed to the second - AWS processes TTL deletions within 48 hours in practice (usually much faster, but not instantaneous)

**Acceptable because:** session history is conversational context, not stored documents. A 24-hour window before expiry is reasonable for a chat history. The deleted document is immediately removed from S3 and Aurora - new queries will never retrieve its content. Only pre-existing session turns may reference it briefly.

---

### Option B - Store doc_ids in session turns at query time

At retrieval time, record which `doc_ids` contributed to each answer:

```json
{
  "session_id": "sess-abc",
  "turn_id": "2026-03-19T10:05:00.000Z",
  "role": "assistant",
  "content": "The 2025 revenue was $4.2B...",
  "source_doc_ids": ["uuid-1234", "uuid-5678"],
  "ttl": 1742486700
}
```

At delete time, query a GSI on `source_doc_ids` to find and delete affected turns.

**Pro:**
- Immediate deletion of turns that reference the deleted document
- Auditable - you know exactly which answers were derived from which documents

**Con:**
- Schema change required - adds `source_doc_ids` to every session turn
- Requires a GSI on `source_doc_ids` (DynamoDB does not support array-valued GSIs natively - would need a separate mapping table)
- Bedrock `retrieve_and_generate` does not return which doc_ids contributed to an answer without extra parsing of the citations field
- Added complexity in the retrieval Lambda

**When this becomes necessary:** regulated environments where GDPR auditors require proof of immediate erasure, or where session TTL > 24 hours.

---

### Option C - Full DynamoDB scan at delete time

Scan the entire sessions table, check each turn for references to the deleted document, delete matching items.

```python
response = table.scan(
    FilterExpression=Attr("content").contains(doc_id)
)
```

**Pro:**
- Immediate deletion, no schema changes

**Con:**
- Full table scan costs money at scale ($0.25 per million read capacity units)
- `content` is free text - a doc_id string in the answer text is not a reliable signal
- Scan latency grows linearly with table size
- Completely impractical at production scale

**Verdict:** rejected.

---

## Decision

**Option A - TTL expiry.**

The 24-hour window is acceptable because:
1. The source document is deleted from S3 and Aurora immediately - no new query can retrieve its content
2. Session history is ephemeral conversational context, not a document store
3. Zero implementation cost preserves simplicity of the deletion flow

If session TTL is extended beyond 24 hours in the future, or if compliance requirements mandate immediate erasure of session content, migrate to Option B by adding `source_doc_ids` to the session schema and a secondary mapping table.

---

## Consequences

- `DELETE /documents/{doc_id}` does not touch the sessions table
- Session turns derived from deleted documents expire within 24 hours via DynamoDB TTL
- The audit event written at delete time records the decision: `{ event: "document_deleted", session_strategy: "ttl_expiry", ttl_hours: 24 }`
- This decision must be disclosed in the privacy policy: "conversation history referencing deleted documents may persist for up to 24 hours"
