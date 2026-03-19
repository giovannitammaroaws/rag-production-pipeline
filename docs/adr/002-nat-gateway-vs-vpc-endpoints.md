# ADR 002 - NAT Gateway vs VPC Endpoints for Bedrock

**Status:** Phase 1 - NAT Gateway. Phase 2 - migrate to VPC Endpoints.
**Date:** 2026-03-19

---

## Context

Lambda functions run in a private VPC subnet. They need outbound access to:
- Amazon Bedrock Knowledge Bases API (outside VPC, AWS managed)
- AWS Secrets Manager (outside VPC)

Two options exist for routing this traffic.

---

## Phase 1 Decision: NAT Gateway

Simple to provision, well understood, one Terraform resource.

```hcl
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
}
```

### Problems with this approach

**Single point of failure.** One NAT Gateway serves all Lambda traffic. If it becomes unavailable, both ingestion and retrieval fail completely. There is no automatic failover.

**Internet path.** Traffic exits the VPC, traverses the public internet to reach Bedrock APIs, then returns. Data leaves the AWS private network even though source and destination are both AWS services.

**Fixed cost.** $0.045/hour = $32.40/month regardless of whether any Lambda is running.

**Compliance blocker.** HIPAA, PCI-DSS, SOC2, and FedRAMP require that sensitive data never leave the AWS private network. NAT Gateway makes compliance impossible for regulated workloads.

---

## Phase 2 Decision: VPC Interface Endpoints (PrivateLink)

Replace NAT Gateway with dedicated VPC endpoints per service.

```hcl
resource "aws_vpc_endpoint" "bedrock" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-east-1.bedrock-agent-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "secrets_manager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-east-1.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}
```

No code changes needed in Lambda - `private_dns_enabled = true` means the same boto3 client calls resolve to the private endpoint automatically.

### Benefits

- **No SPOF.** Each endpoint is an ENI in each AZ - inherently multi-AZ.
- **No internet path.** Traffic stays on the AWS backbone from Lambda to Bedrock.
- **Compliance ready.** Satisfies data-in-transit requirements for HIPAA/SOC2/PCI-DSS.
- **Lower data cost at volume.** $0.01/GB vs $0.045/GB for NAT data processing.

### Endpoints required

| Service | Endpoint name |
|---|---|
| Bedrock Agent Runtime | `com.amazonaws.{region}.bedrock-agent-runtime` |
| Bedrock Runtime | `com.amazonaws.{region}.bedrock-runtime` |
| Secrets Manager | `com.amazonaws.{region}.secretsmanager` |
| CloudWatch Logs | `com.amazonaws.{region}.logs` |
| S3 (Gateway type, free) | `com.amazonaws.{region}.s3` |

---

## Cost Comparison

| | NAT Gateway | VPC Endpoints (4 Interface + 1 Gateway) |
|---|---|---|
| Fixed/month | $32.40 | ~$29.20 (2 AZ x 4 endpoints x $0.01/h x 730h) |
| Data processing | $0.045/GB | $0.01/GB |
| Break-even | - | ~800 GB/month data |

At low volume: NAT Gateway is slightly cheaper. At production scale or with compliance requirements: VPC Endpoints win on every dimension.

---

## Migration Path (Phase 1 → Phase 2)

```bash
# 1. provision endpoints (no downtime, DNS resolves immediately)
terraform apply -target=module.networking.aws_vpc_endpoint.bedrock
terraform apply -target=module.networking.aws_vpc_endpoint.secrets_manager

# 2. verify Lambda can reach Bedrock through endpoint (test query)
python scripts/smoke_test.py

# 3. destroy NAT Gateway
terraform destroy -target=module.networking.aws_nat_gateway.main
terraform destroy -target=module.networking.aws_eip.nat
```

Zero-downtime migration: endpoints are provisioned before NAT Gateway is removed.
