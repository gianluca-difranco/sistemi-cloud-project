from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_elasticloadbalancingv2 as elbv2,
    aws_rds as rds,
    aws_sqs as sqs,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_event_sources,
    Duration,
    CfnOutput,
)
from constructs import Construct

DATABASE_NAME = "fantadb"
BACKEND_URL = "http://localhost:8000"


class EcsMultiContainerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─────────────────────────────────────────────
        # 1. VPC
        # ─────────────────────────────────────────────
        vpc = ec2.Vpc(self, "MyVpc", max_azs=2)

        # ─────────────────────────────────────────────
        # 2. ECS Cluster
        # ─────────────────────────────────────────────
        cluster = ecs.Cluster(self, "MyCluster", vpc=vpc)

        # ─────────────────────────────────────────────
        # 3. Task Definition (Fargate)
        # ─────────────────────────────────────────────
        task_definition = ecs.FargateTaskDefinition(
            self, "MyTaskDef",
            memory_limit_mib=1024,
            cpu=512,
        )

        # ─────────────────────────────────────────────
        # 4. Aurora PostgreSQL (serverless v2)
        #    CDK crea automaticamente un Secret in Secrets Manager.
        #    enable_data_api=True permette alla Lambda di connettersi
        #    tramite RDS Data API senza essere nella stessa VPC.
        # ─────────────────────────────────────────────
        db_cluster = rds.DatabaseCluster(
            self, "MyAuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of('15.17', '15')
            ),
            vpc=vpc,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            default_database_name=DATABASE_NAME,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            # Abilita la RDS Data API (usata dalla Lambda tramite boto3 rds-data client)
            enable_data_api=True,
        )

        # ─────────────────────────────────────────────
        # 5. SQS Queue (messaggi TA → Lambda)
        #    Dead-letter queue: messaggi falliti dopo 3 tentativi
        # ─────────────────────────────────────────────
        dlq = sqs.Queue(
            self, "MessagesDeadLetterQueue",
            queue_name="fantacloud-messages-dlq",
        )

        messages_queue = sqs.Queue(
            self, "MessagesQueue",
            queue_name="fantacloud-messages",
            # visibility_timeout deve essere >= timeout Lambda
            visibility_timeout=Duration.seconds(300),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=dlq,
            ),
        )

        # ─────────────────────────────────────────────
        # 6. Lambda: SQS → Aurora (tramite RDS Data API)
        #    Il codice è preso dalla cartella lambdas/ nella root del repo
        # ─────────────────────────────────────────────
        lambda_write_db = _lambda.Function(
            self, "LambdaWriteDb",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="lambda_write_db.lambda_handler",
            code=_lambda.Code.from_asset("../lambdas"),
            timeout=Duration.seconds(60),
            environment={
                # Variabili usate da lambda_write_db.py
                "CLUSTER_ARN": db_cluster.cluster_arn,
                "SECRET_ARN":  db_cluster.secret.secret_arn,
                "DB_NAME":     DATABASE_NAME,
            },
        )

        # Permetti alla Lambda di usare la RDS Data API e leggere il secret
        db_cluster.grant_data_api_access(lambda_write_db)
        db_cluster.secret.grant_read(lambda_write_db)

        # Collega la SQS come trigger della Lambda
        lambda_write_db.add_event_source(
            lambda_event_sources.SqsEventSource(
                messages_queue,
                batch_size=10,
                report_batch_item_failures=True,
            )
        )

        # ─────────────────────────────────────────────
        # 7. ECR repositories
        # ─────────────────────────────────────────────
        be_repo = ecr.Repository.from_repository_name(self, "BeRepo", "project/backend")
        fe_repo = ecr.Repository.from_repository_name(self, "FeRepo", "project/frontend")

        # ─────────────────────────────────────────────
        # 8. Container Backend
        #    Riceve credenziali dal Secret di Aurora e altre config
        # ─────────────────────────────────────────────
        backend_container = task_definition.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(be_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment={
                "PORT":          "8000",
                "DB_HOST":       db_cluster.cluster_endpoint.hostname,
                "DB_NAME":       DATABASE_NAME,
                "DB_PORT":       "5432",
                "AWS_REGION":    self.region,
                "SQS_QUEUE_URL": messages_queue.queue_url,
            },
            secrets={
                "DB_USER":     ecs.Secret.from_secrets_manager(db_cluster.secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_cluster.secret, "password"),
            },
        )
        backend_container.add_port_mappings(ecs.PortMapping(container_port=8000))


        # Permetti al Task ECS (backend) di inviare messaggi sulla coda
        messages_queue.grant_send_messages(task_definition.task_role)

        # ─────────────────────────────────────────────
        # 9. Container Frontend
        # ─────────────────────────────────────────────
        frontend_container = task_definition.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_ecr_repository(fe_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
            environment={"BACKEND_URL": BACKEND_URL},
        )
        frontend_container.add_port_mappings(ecs.PortMapping(container_port=80))

        # ─────────────────────────────────────────────
        # 10. Security Group + Fargate Service
        # ─────────────────────────────────────────────
        service_sg = ec2.SecurityGroup(self, "ServiceSG", vpc=vpc, allow_all_outbound=True)

        fargate_service = ecs.FargateService(
            self, "MyFargateService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            assign_public_ip=False,
            security_groups=[service_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.seconds(30),
        )

        # ─────────────────────────────────────────────
        # 11. Application Load Balancer
        # ─────────────────────────────────────────────
        alb = elbv2.ApplicationLoadBalancer(
            self, "MyALB",
            vpc=vpc,
            internet_facing=True,
        )

        listener = alb.add_listener("PublicListener", port=80)
        listener.add_targets(
            "FargateTarget",
            port=80,
            targets=[fargate_service.load_balancer_target(
                container_name="FrontendContainer",
                container_port=80,
            )],
            health_check=elbv2.HealthCheck(
                path="/",
                interval=Duration.seconds(60),
            ),
        )

        # ─────────────────────────────────────────────
        # 12. Sicurezza
        # ─────────────────────────────────────────────
        service_sg.connections.allow_from(alb, ec2.Port.tcp(80))
        db_cluster.connections.allow_default_port_from(service_sg)

        # ─────────────────────────────────────────────
        # 13. CfnOutput — valori utili post-deploy
        # ─────────────────────────────────────────────
        CfnOutput(self, "ALBUrl",
                  value=alb.load_balancer_dns_name,
                  description="URL pubblico del Load Balancer")

        CfnOutput(self, "SqsQueueUrl",
                  value=messages_queue.queue_url,
                  description="URL coda SQS (SQS_QUEUE_URL del backend)")

        CfnOutput(self, "SqsQueueArn",
                  value=messages_queue.queue_arn,
                  description="ARN della coda SQS")

        CfnOutput(self, "LambdaName",
                  value=lambda_write_db.function_name,
                  description="Nome della Lambda SQS→Aurora")