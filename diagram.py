"""
AWS Architecture Diagram - RAG Production Pipeline
Run: pip install diagrams && python diagram.py
Requires Graphviz: brew install graphviz
Output: images/architecture_v3.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.storage import S3
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Aurora
from diagrams.aws.ml import Bedrock
from diagrams.aws.integration import SQS
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
    filename="images/architecture_v4",
    outformat="png",
    graph_attr=graph_attr,
    show=False,
):
    user = Users("User")

    # ── OUTSIDE VPC (AWS Managed) ──
    with Cluster("Frontend"):
        waf = WAF("WAF")
        cf = CloudFront("CloudFront CDN")
        fe_s3 = S3("React App (S3)")
        waf >> cf >> fe_s3

    cognito = Cognito("Cognito\nUser Pool")
    apigw = APIGateway("API Gateway\nPOST /upload-url\nPOST /query")

    with Cluster("AWS Managed - Pipeline"):
        doc_s3 = S3("S3\n(raw documents)")
        sqs = SQS("SQS Queue\n(+ DLQ)")

    with Cluster("AWS Managed - AI"):
        titan = Bedrock("Titan Embeddings v2\n(1536 dims)")
        claude = Bedrock("Claude 3 Haiku\n(LLM)")

    with Cluster("Security"):
        secrets = SecretsManager("Secrets Manager")
        kms = KMS("KMS")

    with Cluster("Observability"):
        cw = Cloudwatch("CloudWatch")
        xray = XRay("X-Ray")

    # ── VPC ──
    with Cluster("VPC"):

        with Cluster("Public Subnet"):
            nat = NATGateway("NAT Gateway\n($0.045/hr)")

        with Cluster("Private Subnet"):
            presigned_fn = Lambda("Lambda\n(presigned URL)")

            with Cluster("Prefect Flow"):
                chunking = Lambda("@task\nChunking")
                embedding = Lambda("@task\nEmbedding")
                indexing = Lambda("@task\nIndexing")

            retrieval = Lambda("FastAPI Lambda\n(retrieval)")
            aurora = Aurora("Aurora PostgreSQL\nServerless v2\n+ pgvector + HNSW")

    # ── USER ──
    user >> waf
    waf >> cognito
    waf >> apigw

    # ── FLOW 1 - INGESTION ──
    apigw >> presigned_fn
    presigned_fn >> doc_s3
    doc_s3 >> Edge(label="S3 Event") >> sqs
    sqs >> chunking
    chunking >> embedding >> indexing
    indexing >> aurora

    # ── FLOW 2 - RETRIEVAL ──
    apigw >> retrieval
    retrieval >> aurora

    # ── Lambda → AWS Services via NAT GW ──
    presigned_fn >> nat
    chunking >> nat
    embedding >> nat
    indexing >> nat
    retrieval >> nat
    nat >> titan
    nat >> claude
    nat >> secrets

    # ── SECURITY ──
    aurora >> kms
    doc_s3 >> kms
    sqs >> kms
    retrieval >> secrets
    chunking >> secrets

    # ── OBSERVABILITY ──
    retrieval >> cw
    embedding >> cw
    retrieval >> xray
    embedding >> xray
