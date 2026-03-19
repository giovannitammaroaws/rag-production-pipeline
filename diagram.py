"""
AWS Architecture Diagram - RAG Production Pipeline
Run: pip install diagrams && python diagram.py
Requires Graphviz: brew install graphviz
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.storage import S3
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Aurora
from diagrams.aws.ml import Bedrock
from diagrams.aws.integration import SQS, StepFunctions
from diagrams.aws.network import APIGateway, CloudFront, NATGateway, VPC
from diagrams.aws.security import Cognito, SecretsManager, KMS, WAF
from diagrams.aws.management import Cloudwatch
from diagrams.aws.devtools import XRay
from diagrams.onprem.client import Users
from diagrams.onprem.workflow import Airflow as Prefect

graph_attr = {
    "fontsize": "20",
    "bgcolor": "white",
    "pad": "0.5",
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

    with Cluster("Frontend"):
        cf = CloudFront("CloudFront CDN")
        fe_s3 = S3("React App (S3)")
        waf = WAF("WAF")
        waf >> cf >> fe_s3

    with Cluster("Security"):
        cognito = Cognito("Cognito\nUser Pool")
        secrets = SecretsManager("Secrets Manager\n(DB credentials)")
        kms = KMS("KMS\n(encryption at rest)")

    with Cluster("VPC - Private Subnets"):

        nat = NATGateway("NAT Gateway\n($0.045/hr)")

        with Cluster("FLOW 1 - Ingestion"):
            doc_s3 = S3("S3\n(raw documents)")
            sqs = SQS("SQS Queue\n(+ DLQ)")
            sf = StepFunctions("Step Functions\n(job state)")
            prefect = Prefect("Prefect Flow\n(Python orchestration)")

            with Cluster("Prefect Tasks"):
                chunking = Lambda("@task\nChunking")
                embedding = Lambda("@task\nEmbedding")
                indexing = Lambda("@task\nIndexing")

        with Cluster("FLOW 2 - Retrieval"):
            apigw = APIGateway("API Gateway")
            retrieval = Lambda("FastAPI\n(Lambda + Mangum)")

        with Cluster("Shared"):
            aurora = Aurora("Aurora PostgreSQL\nServerless v2\n+ pgvector + HNSW")

        with Cluster("AI Models (Bedrock)"):
            titan = Bedrock("Titan Embeddings v2\n(1536 dims)")
            claude = Bedrock("Claude 3 Haiku\n(LLM inference)")

    with Cluster("Observability"):
        cw = Cloudwatch("CloudWatch\nLogs + Metrics + Alarms")
        xray = XRay("X-Ray\n(distributed tracing)")

    # User flows
    user >> waf
    user >> Edge(label="upload doc\n(presigned URL)") >> doc_s3
    user >> Edge(label="question\n(JWT token)") >> apigw

    # Ingestion flow
    doc_s3 >> Edge(label="S3 Event") >> sqs
    sqs >> sf
    sf >> prefect
    prefect >> chunking >> embedding >> indexing
    embedding >> Edge(label="batch embed") >> titan
    indexing >> Edge(label="upsert + HNSW") >> aurora

    # Retrieval flow
    apigw >> cognito
    apigw >> retrieval
    retrieval >> Edge(label="1. embed query") >> titan
    retrieval >> Edge(label="2. HNSW search") >> aurora
    retrieval >> Edge(label="3. generate answer") >> claude
    retrieval >> Edge(label="answer") >> user

    # Security
    retrieval >> secrets
    chunking >> secrets
    aurora >> kms
    doc_s3 >> kms
    sqs >> kms

    # NAT GW (outbound from private subnet)
    nat >> titan
    nat >> claude
    nat >> secrets

    # Observability
    retrieval >> cw
    prefect >> cw
    sf >> cw
    retrieval >> xray
    chunking >> xray
    embedding >> xray
