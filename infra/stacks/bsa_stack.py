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
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_destinations as destinations,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_secretsmanager as secretsmanager,
    BundlingOptions,
)
from constructs import Construct

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Named here so the stack, the API Lambda's env and any test agree on one
# string; a GSI cannot be renamed without replacing it.
OWNER_INDEX = "owner-created_at-index"


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
        # Listing a customer's uploads must be a query, not a filtered scan.
        # A scan returns items in key order, so once the table outgrows the
        # scanned page a customer's own jobs can fall outside it entirely and
        # their history appears empty.
        jobs_table.add_global_secondary_index(
            index_name=OWNER_INDEX,
            partition_key=ddb.Attribute(name="owner", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.NUMBER),
            projection_type=ddb.ProjectionType.ALL,
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
        # Every Lambda gets an explicit log group with a finite retention. The
        # default (never expire) contradicts the data lifecycle: a traceback
        # can carry statement text, and the source PDFs themselves are deleted
        # after 30 days — logs must not outlive the data they describe.
        processor = _lambda.Function(
            self, "Processor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            log_group=logs.LogGroup(
                self, "ProcessorLogs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
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
                # DATA RESIDENCY, closed by default. LLM_FALLBACK=off means an
                # unrecognised bank fails with "no layout yet" and no page of
                # the statement is ever put in a request body.
                # ALLOW_EXTERNAL_LLM=false means that even with the fallback on,
                # only an in-account provider may be called. Two switches, both
                # closed, because one flag is one accident away from sending
                # customer statements to a third party.
                "LLM_FALLBACK": "off",
                "ALLOW_EXTERNAL_LLM": "false",
                "LLM_PROVIDER": "bedrock",
                "LLM_API_KEY_SECRET": llm_secret.secret_name,
                # Layouts are read from S3 as well as the bundle, so a new bank
                # is a file upload rather than a release. This is how long an
                # already-warm container may keep serving the old set.
                "LAYOUTS_PREFIX": "layouts/",
                "LAYOUT_CACHE_TTL_S": "300",
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
        llm_secret.grant_read(processor)
        data_bucket.grant_read_write(processor)
        jobs_table.grant_read_write_data(processor)
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(processor),
            s3.NotificationKeyFilter(prefix="uploads/", suffix=".pdf"),
        )

        # ---------- sweeper: no job stays "processing" forever ----------
        # A hard-killed processor cannot write its own status, so something
        # outside it has to. This runs on both paths — driven by the
        # processor's failure queue (fast and exact) and on a schedule (the
        # backstop for abandoned uploads and undelivered events).
        #
        # The failure path goes through a QUEUE rather than naming the sweeper
        # as the processor's destination directly. Pointing the destination at
        # the function creates a dependency cycle CloudFormation refuses to
        # deploy: a destination grants the PROCESSOR permission to invoke the
        # sweeper, while the sweeper needs the processor's name to re-drive a
        # merge and permission to invoke it. A queue depends on neither, so it
        # breaks the cycle — and it is the better design anyway, because a
        # failure notification now survives the sweeper itself failing instead
        # of being dropped.
        # If the sweeper keeps failing on one message, the message must not be
        # retried forever and then silently expire — it parks in a DLQ that an
        # alarm watches (below), so a persistent poison message becomes an
        # email instead of a lost job.
        failures_dlq = sqs.Queue(
            self, "ProcessorFailuresDLQ",
            retention_period=Duration.days(14),
        )
        failures = sqs.Queue(
            self, "ProcessorFailures",
            retention_period=Duration.days(4),
            # Comfortably longer than the sweeper's own 2-minute timeout.
            visibility_timeout=Duration.minutes(6),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=failures_dlq),
        )

        sweeper = _lambda.Function(
            self, "Sweeper",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            memory_size=256,
            timeout=Duration.minutes(2),
            log_group=logs.LogGroup(
                self, "SweeperLogs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            code=_lambda.Code.from_asset(os.path.join(ROOT, "backend", "sweeper")),
            environment={
                "DATA_BUCKET": data_bucket.bucket_name,
                "JOBS_TABLE": jobs_table.table_name,
                "PROCESSOR_FUNCTION": processor.function_name,
                "UPLOAD_GRACE_S": "3600",
                # The processor's own ceiling is 15 minutes; this is measured
                # from the last progress stamp, so keep it above that.
                "PROCESS_GRACE_S": "1200",
            },
        )
        jobs_table.grant_read_write_data(sweeper)
        data_bucket.grant_read(sweeper)
        # The sweeper re-drives a partly-finished job by re-invoking the
        # processor, so there is exactly one merge implementation.
        processor.grant_invoke(sweeper)

        # S3 delivers this event asynchronously, so a timeout would otherwise be
        # retried twice — each retry re-running the whole paid LLM extraction.
        processor.configure_async_invoke(
            retry_attempts=0,
            on_failure=destinations.SqsDestination(failures),
        )
        sweeper.add_event_source(lambda_events.SqsEventSource(failures,
                                                              batch_size=1))

        # Every 15 minutes, not every 5. The on-failure destination already
        # settles a killed invocation within seconds, so this only has to catch
        # what the destination cannot see — an abandoned upload, an undelivered
        # event. Each run is a filtered scan of the whole jobs table, and the
        # table holds 180 days of them, so running it three times less often is
        # three times less to pay for the same guarantee.
        events.Rule(
            self, "SweepSchedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(sweeper)],
            description="Fail or re-drive jobs that stopped making progress",
        )

        # ---------- observability: the backstop must not fail silently ----------
        # The sweeper exists so a job cannot stay "processing" forever — but
        # nothing watched the sweeper itself, so its own failure was invisible
        # and every guarantee it provides quietly lapsed. These alarms all land
        # in one inbox; the address comes from CDK context (cdk.json ops_email,
        # or -c ops_email=... on the command line).
        ops_email = (self.node.try_get_context("ops_email")
                     or "praveen.kartha@highburyconsulting.in")
        ops_topic = sns.Topic(
            self, "OpsAlerts",
            display_name="Bank Statement Analyser ops alerts",
        )
        ops_topic.add_subscription(sns_subs.EmailSubscription(ops_email))
        alert = cw_actions.SnsAction(ops_topic)

        # (a) The most important alarm in the stack: the sweeper is the thing
        # that makes every other failure recoverable, so ANY error from it is a
        # page. One bad run means jobs can now get stuck the old way.
        sweeper_alarm = cw.Alarm(
            self, "SweeperErrors",
            metric=sweeper.metric_errors(
                period=Duration.minutes(15), statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator
            .GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="The sweeper (stuck-job backstop) itself errored;"
                              " stuck jobs are no longer being settled.",
        )
        sweeper_alarm.add_alarm_action(alert)

        # (b) Per-file parse failures are caught in code and settled per job,
        # so Errors on the processor means something structural — OOM, timeout,
        # a bad deploy. A small threshold keeps a single S3 retry from paging.
        processor_alarm = cw.Alarm(
            self, "ProcessorErrors",
            metric=processor.metric_errors(
                period=Duration.minutes(15), statistic="Sum"),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator
            .GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Processor Lambda is erroring at the invocation "
                              "level (OOM/timeout/bad deploy), not per-file.",
        )
        processor_alarm.add_alarm_action(alert)

        # (c) Failure notifications are supposed to be consumed within seconds.
        # One sitting for an hour means the sweeper is not draining its queue.
        failures_age_alarm = cw.Alarm(
            self, "FailureQueueStalled",
            metric=failures.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(15), statistic="Maximum"),
            threshold=3600,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A processor-failure notification has waited "
                              "over an hour; the sweeper is not consuming.",
        )
        failures_age_alarm.add_alarm_action(alert)

        # (d) A message the sweeper failed on five times is parked, not lost —
        # but only if a human hears about it.
        dlq_alarm = cw.Alarm(
            self, "FailureDlqNotEmpty",
            metric=failures_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(15), statistic="Maximum"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator
            .GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A failure notification exhausted its retries; "
                              "a job may be stuck until manually re-driven.",
        )
        dlq_alarm.add_alarm_action(alert)

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
            log_group=logs.LogGroup(
                self, "ApiLogs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            code=_lambda.Code.from_asset(os.path.join(ROOT, "backend", "api")),
            environment={
                "DATA_BUCKET": data_bucket.bucket_name,
                "JOBS_TABLE": jobs_table.table_name,
                "AUTH_TABLE": auth_table.table_name,
                "OWNER_INDEX": OWNER_INDEX,
                # admin categorisation playground invokes the processor to
                # classify a single narration through the real pipeline.
                "PROCESSOR_FUNCTION": processor.function_name,
            },
        )
        data_bucket.grant_read_write(api_fn)
        jobs_table.grant_read_write_data(api_fn)
        auth_table.grant_read_write_data(api_fn)
        processor.grant_invoke(api_fn)

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
            ("/auth/logout", [apigw.HttpMethod.POST]),
            ("/auth/password", [apigw.HttpMethod.POST]),
            ("/jobs", [apigw.HttpMethod.POST, apigw.HttpMethod.GET]),
            ("/jobs/{id}", [apigw.HttpMethod.GET]),
            ("/jobs/{id}/download", [apigw.HttpMethod.GET]),
            ("/jobs/{id}/review", [apigw.HttpMethod.POST]),
            ("/jobs/{id}/corrections",
             [apigw.HttpMethod.POST, apigw.HttpMethod.GET]),  # admin training data
            ("/admin/try-categorize", [apigw.HttpMethod.POST]),  # admin beta
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
        # Security headers on every response. The CSP is derived from what
        # index.html actually is: a single file whose only scripts are one
        # inline block, config.js and inline on* handlers (hence
        # 'unsafe-inline' — removing it bricks every button), one inline
        # <style> plus style="" attributes, system fonts only, inline SVG
        # charts. connect-src stays scheme-wide because the app fetches the
        # account-generated execute-api domain and PUTs to presigned S3 URLs,
        # neither of which is a stable hostname to pin at synth time.
        csp = "; ".join([
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src https:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ])
        site_headers = cf.ResponseHeadersPolicy(
            self, "SiteSecurityHeaders",
            security_headers_behavior=cf.ResponseSecurityHeadersBehavior(
                strict_transport_security=cf.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cf.ResponseHeadersContentTypeOptions(
                    override=True),                       # X-Content-Type-Options: nosniff
                frame_options=cf.ResponseHeadersFrameOptions(
                    frame_option=cf.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cf.ResponseHeadersReferrerPolicy(
                    referrer_policy=cf.HeadersReferrerPolicy
                    .STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                content_security_policy=cf.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp,
                    override=True,
                ),
            ),
        )
        dist = cf.Distribution(
            self, "Site",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=site_headers,
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
        CfnOutput(self, "SweeperFunctionName", value=sweeper.function_name)
        CfnOutput(self, "ProcessorFailureQueue", value=failures.queue_url)
