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
        waf = WAF("WAF")
        cf = CloudFront("CloudFront CDN")
        fe_s3 = S3("React App (S3)")
        waf >> cf >> fe_s3

    cognito = Cognito("Cognito\nUser Pool")

    with Cluster("VPC - Private Subnets"):

        nat = NATGateway("NAT Gateway")

        with Cluster("FLOW 1 - Ingestion"):
            apigw_ingest = APIGateway("API Gateway\n(presigned URL)")
            presigned_fn = Lambda("Lambda\n(presigned URL)")
            doc_s3 = S3("S3\n(raw documents)")
            sqs = SQS("SQS Queue\n(+ DLQ)")
            sf = StepFunctions("Step Functions\n(job state)")
            prefect = Prefect("Prefect Flow")

            with Cluster("Prefect Tasks"):
                chunking = Lambda("@task\nChunking")
                embedding = Lambda("@task\nEmbedding")
                indexing = Lambda("@task\nIndexing")

        with Cluster("FLOW 2 - Retrieval"):
            apigw = APIGateway("API Gateway\n(retrieval)")
            retrieval = Lambda("FastAPI\n(Lambda + Mangum)")

        with Cluster("Shared"):
            aurora = Aurora("Aurora PostgreSQL\nServerless v2\n+ pgvector + HNSW")

        with Cluster("AI Models (Bedrock)"):
            titan = Bedrock("Titan Embeddings v2")
            claude = Bedrock("Claude 3 Haiku")

    with Cluster("Security"):
        secrets = SecretsManager("Secrets Manager")
        kms = KMS("KMS")

    with Cluster("Observability"):
        cw = Cloudwatch("CloudWatch")
        xray = XRay("X-Ray")

    # User entry point
    user >> waf
    waf >> cognito
    waf >> apigw_ingest
    waf >> apigw

    # Ingestion flow
    apigw_ingest >> presigned_fn
    presigned_fn >> doc_s3
    doc_s3 >> Edge(label="S3 Event") >> sqs
    sqs >> sf >> prefect
    prefect >> chunking >> embedding >> indexing
    embedding >> titan
    indexing >> aurora

    # Retrieval flow
    apigw >> retrieval
    retrieval >> titan
    retrieval >> aurora
    retrieval >> claude

    # NAT outbound
    nat >> titan
    nat >> claude
    nat >> secrets

    # Security
    retrieval >> secrets
    chunking >> secrets
    aurora >> kms
    doc_s3 >> kms

    # Observability
    sf >> cw
    prefect >> cw
    retrieval >> cw
    retrieval >> xray
    embedding >> xray
