# Cost

All prices are us-east-1. Last updated: March 2026.

---

## Monthly Estimate (Portfolio / Low Traffic)

| Service | Cost/month | Note |
|---|---|---|
| Aurora Serverless v2 | ~$43 | Min 0.5 ACU always active |
| NAT Gateway | ~$33 | Fixed $0.045/hr + data processing |
| Lambda (3 functions) | ~$0 | Near zero at low traffic |
| Bedrock KB ingestion | ~$0 | Pay per job, near zero at low volume |
| Bedrock queries | ~$1 | ~$0.001 per query at Haiku pricing |
| DynamoDB | ~$0 | On-demand, near zero at low volume |
| S3 + CloudFront | ~$1 | Storage + CDN |
| API Gateway | ~$1 | $3.50 per million requests |
| SQS FIFO + DLQ | ~$0 | Near zero at low volume |
| CloudWatch + X-Ray | ~$2 | Log storage + traces |
| Cognito | ~$0 | Free up to 50,000 MAU |
| Secrets Manager | ~$1 | $0.40 per secret |
| KMS | ~$1 | $1 per key/month |
| **Total** | **~$83/month** | |

The two cost drivers are Aurora (unavoidable minimum ACU) and NAT Gateway (Phase 2 replaces with VPC Endpoints).

---

## Reducing Cost When Not in Use

**Destroy NAT Gateway between demos:**
```bash
terraform destroy -target=module.networking.aws_nat_gateway.main
# saves $33/month when not running queries
```

**Snapshot Aurora and delete cluster:**
```bash
# create snapshot
aws rds create-db-cluster-snapshot \
  --db-cluster-identifier rag-pipeline-prod \
  --db-cluster-snapshot-identifier rag-pipeline-snapshot-$(date +%Y%m%d)

# delete cluster (snapshot is kept, no compute cost)
terraform destroy -target=module.knowledge_base.aws_rds_cluster.aurora

# restore when needed (~5 min)
terraform apply -target=module.knowledge_base.aws_rds_cluster.aurora
```

With this approach: **~$5/month** when idle (S3 + snapshot storage only).

---

## One-Day Ingestion Test (200 PDF documents)

| Item | Calculation | Cost |
|---|---|---|
| Titan Embeddings | ~4,400 chunks x 300 tokens = 1.3M tokens x $0.00002/1K | ~$0.03 |
| Aurora (24h active) | 24h x 0.5 ACU x $0.12/ACU-h | $1.44 |
| NAT Gateway (24h) | 24h x $0.045/h | $1.08 |
| 100 test queries | 100 x ~$0.001 | $0.10 |
| **Total** | | **~$2.65** |

200 PDFs is enough to demonstrate the full pipeline. The code is identical for 10,000 PDFs.

---

## Phase 2: VPC Endpoints vs NAT Gateway

Replacing NAT Gateway with VPC Endpoints changes the cost structure:

| | NAT Gateway | VPC Endpoints |
|---|---|---|
| Fixed cost | $32.40/month | ~$29.20/month (4 endpoints x 2 AZ x $0.01/h) |
| Data processing | $0.045/GB | $0.01/GB |
| SPOF | Yes | No |
| Internet path | Yes | No |
| Break-even volume | - | ~800 GB/month |

At low volume NAT Gateway is slightly cheaper. VPC Endpoints win on security, resilience, and compliance. See [ADR 002](adr/002-nat-gateway-vs-vpc-endpoints.md).

---

## Bedrock Pricing Reference

| Model | Input | Output |
|---|---|---|
| Titan Embeddings v2 | $0.00002 / 1K tokens | - |
| Claude 3 Haiku | $0.00025 / 1K tokens | $0.00125 / 1K tokens |
| Claude 3 Sonnet | $0.003 / 1K tokens | $0.015 / 1K tokens |

A typical retrieval query: ~2,000 input tokens + ~500 output tokens = ~$0.0011 on Haiku.
