"""
AWS Architecture Diagram - RAG Production Pipeline
Run: pip install diagrams && python diagram.py
Requires Graphviz: brew install graphviz
Output: images/architecture_v2.png
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
    filename="images/architecture_v2",
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

    with Cluster("AWS Managed Services"):
        apigw_ingest = APIGateway("API Gateway\n(presigned URL)")
        apigw = APIGateway("API Gateway\n(retrieval)")
        titan = Bedrock("Titan Embeddings v2\n(1536 dims)")
        claude = Bedrock("Claude 3 Haiku\n(LLM)")

    with Cluster("VPC"):

        with Cluster("Public Subnet"):
            nat = NATGateway("NAT Gateway\n($0.045/hr)")

        with Cluster("Private Subnet"):

            with Cluster("FLOW 1 - Ingestion"):
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
                retrieval = Lambda("FastAPI\n(Lambda + Mangum)")

            with Cluster("Database"):
                aurora = Aurora("Aurora PostgreSQL\nServerless v2\n+ pgvector + HNSW")

    with Cluster("Security"):
        secrets = SecretsManager("Secrets Manager")
        kms = KMS("KMS")

    with Cluster("Observability"):
        cw = Cloudwatch("CloudWatch")
        xray = XRay("X-Ray")

    # User entry
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
    indexing >> aurora

    # Private → Bedrock via NAT GW
    embedding >> nat
    retrieval >> nat
    nat >> titan
    nat >> claude

    # Retrieval flow
    apigw >> retrieval
    retrieval >> aurora

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
