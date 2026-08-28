"""The judge's door, as infrastructure.

One CloudFront distribution over two origins, exactly as ADR-0006 decided:

    CloudFront
      ├─ default behavior  ->  S3 (private, Origin Access Control) : the console
      └─ /api/*            ->  Lambda Function URL (AuthType NONE) : the door

The Function URL stays public rather than IAM-signed because Origin Access
Control on a Function URL forces `AWS_IAM`, after which a browser POST needs
`x-amz-content-sha256`, which a plain `fetch` will not send. The documented
alternative is a shared secret header that CloudFront adds and the function
requires, so a caller who finds the Function URL directly is refused.

Nothing here bills by the hour. DynamoDB is on-demand, Lambda and CloudFront
are per-request, S3 holds a few hundred kilobytes. The account already carries
another project's idle stack, so an hourly-billed resource in this design would
be a mistake, not a tradeoff.
"""

from __future__ import annotations

import json
import os

import aws_cdk as cdk
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct


class JudgeDoorStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        origin_secret = os.environ.get("DOOR_ORIGIN_SECRET", "")
        if not origin_secret:
            # Fail at synth, not at runtime. A door deployed with an empty
            # secret silently accepts direct origin access, which is exactly
            # the bypass the secret exists to close.
            raise ValueError(
                "DOOR_ORIGIN_SECRET is unset. Generate one and export it:\n"
                "  export DOOR_ORIGIN_SECRET=$(python3 -c "
                "'import secrets;print(secrets.token_urlsafe(32))')"
            )

        agent_runtime_arn = os.environ.get("AGENT_RUNTIME_ARN", "")
        if not agent_runtime_arn:
            # Fail at synth. An empty value overwrites a working door with a
            # stats-only function, and POST /api/run then 503s.
            raise ValueError(
                "AGENT_RUNTIME_ARN is unset. Export the deployed triage runtime before cdk deploy."
            )
        git_sha = os.environ.get("GIT_SHA", "unknown")

        # ---------------------------------------------------------- state
        runs = dynamodb.TableV2(
            self,
            "RunTable",
            partition_key=dynamodb.Attribute(name="run_id", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expires_at",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="status-created_at-index",
                    partition_key=dynamodb.Attribute(
                        name="status", type=dynamodb.AttributeType.STRING
                    ),
                    sort_key=dynamodb.Attribute(
                        name="created_at", type=dynamodb.AttributeType.NUMBER
                    ),
                    # A sparse index: the daily counter rows carry no status, so
                    # they never enter it, and "which runs are waiting on an
                    # attorney" is a query rather than a scan with a filter.
                    projection_type=dynamodb.ProjectionType.INCLUDE,
                    non_key_attributes=["origin", "result"],
                )
            ],
        )

        push = dynamodb.TableV2(
            self,
            "PushTable",
            partition_key=dynamodb.Attribute(name="endpoint", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            removal_policy=cdk.RemovalPolicy.DESTROY,
            # Push endpoints churn (reinstalls, revoked permission) and the
            # subscribe cap is a hard 200, so without expiry the table fills
            # with corpses until every new subscription is refused and every
            # ping walks 200 dead endpoints. Rows also die early when the push
            # service says Gone (push.py deletes them).
            time_to_live_attribute="expires_at",
        )

        audit_lock = s3.Bucket(
            self,
            "AuditLock",
            object_lock_enabled=True,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        cfn_lock = audit_lock.node.default_child
        assert isinstance(cfn_lock, s3.CfnBucket)
        cfn_lock.object_lock_configuration = s3.CfnBucket.ObjectLockConfigurationProperty(
            object_lock_enabled="Enabled",
            rule=s3.CfnBucket.ObjectLockRuleProperty(
                default_retention=s3.CfnBucket.DefaultRetentionProperty(mode="COMPLIANCE", days=30)
            ),
        )

        agent_role_arn = os.environ.get(
            "AGENT_RUNTIME_ROLE_ARN",
            "arn:aws:iam::741030561008:role/AgentCore-instanteragent--ApplicationAgentTriageRun-T1TMj581A16h",
        )
        if agent_role_arn:
            # Imported roles are often immutable, so identity policies here
            # can no-op. The bucket policy is the grant that actually lands.
            audit_lock.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AgentRuntimePutLockedAudit",
                    principals=[iam.ArnPrincipal(agent_role_arn)],
                    actions=["s3:PutObject", "s3:PutObjectRetention"],
                    resources=[audit_lock.arn_for_objects("*")],
                )
            )

        # ----------------------------------------------------------- door
        door = lambda_.Function(
            self,
            "DoorFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("build/door"),
            timeout=cdk.Duration.seconds(120),
            memory_size=1024,
            architecture=lambda_.Architecture.X86_64,
            # NO reserved concurrency, and this is a constraint rather than a
            # preference. ADR-0006 chose reserved concurrency as the abuse
            # control over a $6/month WAF, but this account's TOTAL Lambda
            # concurrency limit is 10 (the new-account default is not the
            # familiar 1000), and reserving any of it drops unreserved
            # concurrency below the required minimum of 10. The deploy fails
            # outright with InvalidRequest. The account ceiling is therefore
            # already the concurrency cap, and the spend cap lives in the
            # handler, on /api/run, which is the only endpoint that can cost
            # money.
            environment={
                "RUN_TABLE": runs.table_name,
                "ORIGIN_SECRET": origin_secret,
                "AGENT_RUNTIME_ARN": agent_runtime_arn,
                "GIT_SHA": git_sha,
                "MAX_RUNS_PER_DAY": os.environ.get("MAX_RUNS_PER_DAY", "200"),
                "MAX_SCHEDULED_RUNS_PER_DAY": os.environ.get("MAX_SCHEDULED_RUNS_PER_DAY", "2"),
                "AWAITING_INDEX": "status-created_at-index",
                "AUDIT_LOCK_BUCKET": audit_lock.bucket_name,
                "PUSH_TABLE": push.table_name,
                "VAPID_PUBLIC_KEY": os.environ.get("VAPID_PUBLIC_KEY", ""),
                "VAPID_PRIVATE_KEY": os.environ.get("VAPID_PRIVATE_KEY", ""),
                "VAPID_MAILTO": os.environ.get("VAPID_MAILTO", "mailto:stephensookra@gmail.com"),
            },
        )
        # FUNCTION-ERROR retries for the scheduled sweep, explicit rather than
        # implied. Scheduler's own retry_policy (below) covers only DELIVERY
        # failures; a raise inside scheduled_sweep lands here. Two retries
        # inside the hour: spend stays bounded by the scheduled daily cap of 2,
        # and the occurrence claim is released on failure so a retry actually
        # reruns instead of reporting duplicate: True.
        door.configure_async_invoke(
            retry_attempts=2,
            max_event_age=cdk.Duration.hours(1),
        )
        runs.grant_read_write_data(door)
        push.grant_read_write_data(door)
        audit_lock.grant_put(door)
        door.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:PutObjectRetention"],
                resources=[audit_lock.arn_for_objects("*")],
            )
        )
        door.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:GetInferenceProfile",
                ],
                resources=[
                    f"arn:aws:bedrock:us-east-1:{self.account}:inference-profile/us.amazon.nova-pro-v1:0",
                    "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
                    "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-pro-v1:0",
                    "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-pro-v1:0",
                ],
            )
        )
        if agent_runtime_arn:
            door.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    # Scoped to the one runtime, plus its endpoints, rather
                    # than a wildcard over every runtime in the account.
                    resources=[agent_runtime_arn, f"{agent_runtime_arn}/*"],
                )
            )

        door_url = door.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        # ------------------------------------------------- the morning sweep
        # The product's claim is that a walk-in clinic cannot watch every
        # clock. Until this existed the agent only ran when somebody pressed a
        # button, which is precisely the thing the pitch says nobody has time
        # to do. EventBridge Scheduler fires the sweep at 7am in the court's
        # own timezone; it ends where a visitor run ends, at the attorney
        # interrupt, with nothing committed.
        sweep_role = iam.Role(
            self,
            "SweepSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        door.grant_invoke(sweep_role)
        scheduler.CfnSchedule(
            self,
            "MorningSweep",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                # A quarter hour of slack: nothing here is time-critical to the
                # minute, and a window lets AWS spread scheduled load.
                mode="FLEXIBLE",
                maximum_window_in_minutes=15,
            ),
            schedule_expression="cron(0 7 ? * MON-FRI *)",
            # Deadlines are counted in the court's calendar, so the sweep runs
            # on the court's clock rather than UTC. Fulton County State Court
            # sits in America/New_York, and this also means the schedule
            # follows daylight saving without a code change.
            schedule_expression_timezone="America/New_York",
            state="ENABLED",
            description="Instanter: the 7am triage sweep, weekdays, court time.",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=door.function_arn,
                role_arn=sweep_role.role_arn,
                # This marker is what the handler routes on. It cannot arrive
                # over HTTP: a Function URL event always carries rawPath, and a
                # POST body is a string rather than merged into the event.
                input=json.dumps(
                    {
                        "instanter_scheduled_sweep": True,
                        "capacity": 2,
                        # Scheduler substitutes its own occurrence time here.
                        # It does two jobs: the sweep triages the day it was
                        # FOR rather than the day a retry happened, and it is
                        # the idempotency key that stops an at-least-once
                        # redelivery becoming a second paid run.
                        "scheduled_time": "<aws.scheduler.scheduled-time>",
                    }
                ),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    # DELIVERY retries only. Scheduler invokes Lambda
                    # asynchronously, so this policy governs failures to HAND
                    # the event to Lambda (throttles when public traffic has
                    # the account's 10 concurrent executions busy at 7am),
                    # NOT function errors: a raise inside scheduled_sweep is
                    # retried by Lambda's own async config, set explicitly on
                    # the function below. Retries are idempotent-safe (the
                    # occurrence claim deduplicates redelivery and is released
                    # when a start fails) and spend stays bounded by the
                    # scheduled cap of 2 regardless of retry count.
                    maximum_retry_attempts=5,
                    maximum_event_age_in_seconds=3600,
                ),
            ),
        )

        # -------------------------------------------------------- console
        console = s3.Bucket(
            self,
            "ConsoleBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "Door",
            comment="Instanter judge door",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(console),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin(
                        door_url,
                        custom_headers={"x-instanter-origin": origin_secret},
                        # A sweep runs 25 to 35 seconds end to end, right at
                        # CloudFront's 30-second default origin timeout, so
                        # whether a judge's first click succeeded was decided
                        # by variance: two live runs 504ed at the viewer while
                        # the Lambda (120s) finished and parked the interrupt
                        # where nobody could answer it. 60 is CloudFront's
                        # no-quota-increase ceiling and clears the observed
                        # worst case with the cold start included.
                        read_timeout=cdk.Duration.seconds(60),
                        keepalive_timeout=cdk.Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    # /api/stats recomputes on every request. Caching it would
                    # turn a live computation into a stored number, which is
                    # the one thing it must not be.
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=(
                        cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
                    ),
                ),
            },
            error_responses=[
                # The console is a single-page app: a deep link must reach it
                # rather than S3's XML error document.
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        s3deploy.BucketDeployment(
            self,
            "ConsoleDeployment",
            sources=[s3deploy.Source.asset("build/console")],
            destination_bucket=console,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        cdk.CfnOutput(self, "DoorUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(
            self, "StatsUrl", value=f"https://{distribution.distribution_domain_name}/api/stats"
        )
        cdk.CfnOutput(self, "FunctionUrl", value=door_url.url)
        cdk.CfnOutput(self, "ConsoleBucketName", value=console.bucket_name)
        cdk.CfnOutput(self, "RunTableName", value=runs.table_name)
        cdk.CfnOutput(self, "AuditLockBucket", value=audit_lock.bucket_name)
        cdk.CfnOutput(self, "PushTableName", value=push.table_name)
