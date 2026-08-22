"""Bank Statement Analyser — single-stack MVP.

Frontend : S3 + CloudFront (OAC), config.js injected with the API URL
API      : HTTP API + one Python Lambda (boto3 only)
Pipeline : one processor Lambda (bsa package bundled with its deps),
           triggered by S3 uploads/ events
State    : DynamoDB jobs table; data bucket holds uploads/ and outputs/

MVP simplifications vs the target architecture (documented in the project
doc): no Cognito yet, single Lambda instead of Step Functions. Both are
additive changes later.
"""
import os

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as apigw_int,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_secretsmanager as secretsmanager,
    BundlingOptions,
)
from constructs import Construct

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


class BsaStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # ---------- data ----------
        data_bucket = s3.Bucket(
            self, "DataBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            cors=[s3.CorsRule(               # browser PUTs via presigned URL
                allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                allowed_origins=["*"],
                allowed_headers=["*"],
                max_age=3600,
            )],
            lifecycle_rules=[
                s3.LifecycleRule(            # auto-delete source PDFs
                    prefix="uploads/", expiration=Duration.days(30)),
                s3.LifecycleRule(
                    prefix="outputs/", expiration=Duration.days(180)),
                s3.LifecycleRule(            # per-file extraction results, kept
                    prefix="work/",          # only long enough to retry a merge
                    expiration=Duration.days(7)),
            ],
        )

        jobs_table = ddb.Table(
            self, "JobsTable",
            partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # ---------- llm provider credentials ----------
        # CDK seeds a random value; put the real key in after deploy with
        #   aws secretsmanager put-secret-value --secret-id bsa/llm-api-key \
        #     --secret-string '{"gemini":"...","anthropic":"...","openai":"..."}'
        # A bare string works too. Keyed JSON lets one secret serve every
        # provider, so switching LLM_PROVIDER needs no credential change.
        llm_secret = secretsmanager.Secret(
            self, "LlmApiKey",
            secret_name="bsa/llm-api-key",
            description="API keys for the statement-extraction LLM providers",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------- processor lambda (bsa pipeline + deps, docker-bundled) ----------
        processor = _lambda.Function(
            self, "Processor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            # ARM matches the wheels the bundling container downloads on
            # Apple-Silicon Macs (and is cheaper); keep these two in sync.
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            memory_size=2048,
            # A 978-row statement took 7m13s via the LLM path; 10 min was not
            # enough for a larger one. 15 min is Lambda's ceiling — past that
            # the work has to be split (Step Functions) rather than stretched.
            timeout=Duration.minutes(15),
            code=_lambda.Code.from_asset(
                os.path.join(ROOT, "backend", "processor"),
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                    command=["bash", "-c",
                             "pip install --retries 10 --timeout 60 "
                             "-r requirements.txt -t /asset-output "
                             "&& cp -r . /asset-output"],
                ),
            ),
            environment={
                "DATA_BUCKET": data_bucket.bucket_name,
                "JOBS_TABLE": jobs_table.table_name,
                # LLM extraction provider. Switch provider or model here (or
                # straight on the Lambda in the console) with no code change —
                # llm_providers.py resolves both at call time.
                #   LLM_PROVIDER: gemini | anthropic | openai | bedrock
                #   LLM_MODEL   : optional; omit to use that provider's default
                "LLM_PROVIDER": "anthropic",
                "LLM_MODEL": "claude-sonnet-5",
                "LLM_API_KEY_SECRET": llm_secret.secret_name,
                # Bedrock is kept as a selectable provider, but is currently
                # blocked on this account: every Anthropic model fails its AWS
                # Marketplace subscription (INVALID_PAYMENT_INSTRUMENT on an
                # AWS India / AISPL account). ACTIVE in list-inference-profiles
                # does NOT mean invocable — smoke-test with
                # `aws bedrock-runtime converse` before relying on it.
                "BEDROCK_MODEL_ID":
                    "apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
                "BEDROCK_REGION": "ap-south-1",
            },
        )
        processor.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["*"],   # global profile fans out across regions; MVP scope
        ))
        # S3 delivers this event asynchronously, so a timeout would otherwise be
        # retried twice — each retry re-running the whole paid LLM extraction.
        processor.configure_async_invoke(retry_attempts=0)
        llm_secret.grant_read(processor)
        data_bucket.grant_read_write(processor)
        jobs_table.grant_read_write_data(processor)
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(processor),
            s3.NotificationKeyFilter(prefix="uploads/", suffix=".pdf"),
        )

        # ---------- auth ----------
        # Requirement: no external auth service. Users and sessions live in
        # DynamoDB; the API Lambda verifies a bearer token on every request.
        # pk holds either USER#<email> or SESSION#<token>; both expire via ttl.
        auth_table = ddb.Table(
            self, "AuthTable",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # ---------- api lambda ----------
        api_fn = _lambda.Function(
            self, "ApiFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            memory_size=256,
            timeout=Duration.seconds(15),
            code=_lambda.Code.from_asset(os.path.join(ROOT, "backend", "api")),
            environment={
                "DATA_BUCKET": data_bucket.bucket_name,
                "JOBS_TABLE": jobs_table.table_name,
                "AUTH_TABLE": auth_table.table_name,
            },
        )
        data_bucket.grant_read_write(api_fn)
        jobs_table.grant_read_write_data(api_fn)
        auth_table.grant_read_write_data(api_fn)

        api = apigw.HttpApi(
            self, "Api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST],
                allow_headers=["content-type", "authorization"],
            ),
        )
        integ = apigw_int.HttpLambdaIntegration("ApiInteg", api_fn)
        for route, methods in [
            ("/auth/login", [apigw.HttpMethod.POST]),   # public by design
            ("/auth/me", [apigw.HttpMethod.GET]),
            ("/jobs", [apigw.HttpMethod.POST, apigw.HttpMethod.GET]),
            ("/jobs/{id}", [apigw.HttpMethod.GET]),
            ("/jobs/{id}/download", [apigw.HttpMethod.GET]),
        ]:
            api.add_routes(path=route, methods=methods, integration=integ)

        # ---------- frontend ----------
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        dist = cf.Distribution(
            self, "Site",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
        )
        s3deploy.BucketDeployment(
            self, "SiteDeploy",
            destination_bucket=site_bucket,
            distribution=dist,                       # invalidate on deploy
            distribution_paths=["/*"],
            # index.html and config.js are the whole app, and both change every
            # deploy. Without this they inherit CloudFront's 24h default TTL and
            # browsers keep serving a stale build long after a fix ships — which
            # is exactly how a fixed login screen kept looking broken.
            cache_control=[
                s3deploy.CacheControl.no_cache(),
                s3deploy.CacheControl.must_revalidate(),
            ],
            sources=[
                s3deploy.Source.asset(os.path.join(ROOT, "frontend")),
                s3deploy.Source.data(
                    "config.js",
                    f"window.BSA_API = '{api.api_endpoint}';"),
            ],
        )

        CfnOutput(self, "SiteUrl", value=f"https://{dist.distribution_domain_name}")
        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        CfnOutput(self, "JobsTableName", value=jobs_table.table_name)
        CfnOutput(self, "LlmApiKeySecret", value=llm_secret.secret_name)
        CfnOutput(self, "AuthTableName", value=auth_table.table_name)
