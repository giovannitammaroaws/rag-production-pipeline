"""
AWS Architecture Diagram - RAG Production Pipeline
Run: pip install diagrams && python diagram.py
Requires Graphviz: brew install graphviz
Output: images/architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.storage import S3
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Aurora
from diagrams.aws.ml import Bedrock
from diagrams.aws.integration import SQS, StepFunctions
from diagrams.aws.network import APIGateway, CloudFront, NATGateway
from diagrams.aws.security import Cognito, SecretsManager, KMS, WAF
from diagrams.aws.management import Cloudwatch
from diagrams.aws.devtools import XRay
from diagrams.onprem.client import Users
from diagrams.onprem.workflow import Airflow as Prefect

graph_attr = {
    "fontsize": "18",
    "bgcolor": "white",
    "pad": "0.8",
    "splines": "ortho",
}

with Diagram(
    "RAG Production Pipeline - AWS Architecture",
    filename="images/architecture",
    outformat="png",
    graph_attr=graph_attr,
    show=False,
):
    user = Users("User")

    with Cluster("Frontend (S3 + CloudFront)"):
        waf = WAF("WAF")
        cf = CloudFront("CloudFront CDN")
        fe_s3 = S3("React App (S3)")
        waf >> cf >> fe_s3

    cognito = Cognito("Cognito\nUser Pool")

    with Cluster("Security"):
        secrets = SecretsManager("Secrets Manager\n(DB credentials)")
        kms = KMS("KMS\n(encryption at rest)")

    with Cluster("Observability"):
        cw = Cloudwatch("CloudWatch\nLogs + Metrics + Alarms")
        xray = XRay("X-Ray\n(tracing)")

    with Cluster("VPC - Private Subnets"):

        nat = NATGateway("NAT Gateway\n($0.045/hr)")

        with Cluster("FLOW 1 - Ingestion"):
            apigw_ingest = APIGateway("API Gateway\n(presigned URL)")
            presigned_fn = Lambda("Lambda\n(generate presigned URL)")
            doc_s3 = S3("S3\n(raw documents)")
            sqs = SQS("SQS Queue\n(+ DLQ)")
            sf = StepFunctions("Step Functions\n(job state)")
            prefect = Prefect("Prefect Flow\n(Python orchestration)")

            with Cluster("Prefect Tasks"):
                chunking = Lambda("@task\nChunking")
                embedding = Lambda("@task\nEmbedding")
                indexing = Lambda("@task\nIndexing")

        with Cluster("FLOW 2 - Retrieval"):
            apigw_retrieval = APIGateway("API Gateway\n(retrieval)")
            retrieval = Lambda("FastAPI\n(Lambda + Mangum)")

        with Cluster("Shared"):
            aurora = Aurora("Aurora PostgreSQL\nServerless v2\n+ pgvector + HNSW")

        with Cluster("AI Models (Bedrock)"):
            titan = Bedrock("Titan Embeddings v2\n(1536 dims)")
            claude = Bedrock("Claude 3 Haiku\n(LLM inference)")

    # ── FLOW 1 - INGESTION ──
    # 1. user logs in
    user >> Edge(label="1. login") >> cognito
    # 2. user requests presigned URL (with JWT)
    user >> Edge(label="2. GET /upload-url + JWT") >> apigw_ingest
    apigw_ingest >> Edge(label="validate JWT") >> cognito
    apigw_ingest >> presigned_fn
    presigned_fn >> Edge(label="presigned URL") >> user
    # 3. user uploads directly to S3
    user >> Edge(label="3. upload document") >> doc_s3
    # 4. pipeline starts
    doc_s3 >> Edge(label="S3 Event") >> sqs
    sqs >> sf
    sf >> prefect
    prefect >> chunking >> embedding >> indexing
    embedding >> Edge(label="batch embed") >> titan
    indexing >> Edge(label="upsert + HNSW") >> aurora

    # ── FLOW 2 - RETRIEVAL ──
    user >> Edge(label="question + JWT") >> apigw_retrieval
    apigw_retrieval >> Edge(label="validate JWT") >> cognito
    apigw_retrieval >> retrieval
    retrieval >> Edge(label="1. embed query") >> titan
    retrieval >> Edge(label="2. HNSW search") >> aurora
    retrieval >> Edge(label="3. generate answer") >> claude
    retrieval >> Edge(label="answer") >> user

    # ── SECURITY ──
    presigned_fn >> secrets
    retrieval >> secrets
    chunking >> secrets
    aurora >> kms
    doc_s3 >> kms
    sqs >> kms

    # ── NAT GW outbound ──
    nat >> titan
    nat >> claude
    nat >> secrets

    # ── OBSERVABILITY ──
    prefect >> cw
    sf >> cw
    retrieval >> cw
    retrieval >> xray
    chunking >> xray
    embedding >> xray
