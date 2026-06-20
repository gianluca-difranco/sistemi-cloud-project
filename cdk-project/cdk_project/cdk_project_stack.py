from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_elasticloadbalancingv2 as elbv2,
    aws_rds as rds,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_event_sources,
    Duration,
    CfnOutput,
)
from constructs import Construct
import os

# Legge variabili dal file .env per non pusharle su git
env_vars = {}
for p in ['../.env', '../../.env']:
    env_path = os.path.join(os.path.dirname(__file__), p)
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    try:
                        key, value = line.strip().split('=', 1)
                        env_vars[key] = value
                    except ValueError:
                        pass

admin_email = env_vars.get('ADMIN_EMAIL', 'admin@supremo.com')
admin_password = env_vars.get('ADMIN_PASSWORD', 'password_suprema')

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
            memory_limit_mib=2048,
            cpu=1024,
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
        # 4b. Bastion Host (per accesso remoto sicuro al DB tramite SSM)
        # ─────────────────────────────────────────────
        bastion = ec2.BastionHostLinux(
            self, "DbBastion",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.NANO),
            subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        )
        db_cluster.connections.allow_default_port_from(bastion)

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
        # NOTIFICHE FINE GIORNATA: SNS, SQS e Lambda
        # ─────────────────────────────────────────────
        # Topic SNS Globale per le notifiche
        match_notifications_topic = sns.Topic(
            self, "MatchNotificationsTopic",
            topic_name="fantacloud-match-notifications"
        )

        matchday_dlq = sqs.Queue(
            self, "MatchdayDeadLetterQueue",
            queue_name="fantacloud-matchday-dlq",
        )
        # Coda SQS per disaccoppiare il calcolo dalla notifica
        matchday_calculated_queue = sqs.Queue(
            self, "MatchdayCalculatedQueue",
            queue_name="fantacloud-matchday-calculated",
            visibility_timeout=Duration.seconds(120),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=matchday_dlq,
            ),
        )

        # Lambda per processare il calcolo e inviare a SNS
        lambda_notify_matchday = _lambda.Function(
            self, "LambdaNotifyMatchday",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="lambda_notify_matchday.lambda_handler",
            code=_lambda.Code.from_asset("../lambdas"),
            timeout=Duration.seconds(60),
            environment={
                "CLUSTER_ARN": db_cluster.cluster_arn,
                "SECRET_ARN":  db_cluster.secret.secret_arn,
                "DB_NAME":     DATABASE_NAME,
                "SNS_TOPIC_ARN": match_notifications_topic.topic_arn,
            },
        )

        # Permessi alla Lambda Notify Matchday
        db_cluster.grant_data_api_access(lambda_notify_matchday)
        db_cluster.secret.grant_read(lambda_notify_matchday)
        match_notifications_topic.grant_publish(lambda_notify_matchday)

        # Trigger SQS per la Lambda
        lambda_notify_matchday.add_event_source(
            lambda_event_sources.SqsEventSource(
                matchday_calculated_queue,
                batch_size=1,
            )
        )

        # ─────────────────────────────────────────────
        # 7. ECR repositories
        # ─────────────────────────────────────────────
        be_repo = ecr.Repository.from_repository_name(self, "BeRepo", "project/backend")
        fe_repo = ecr.Repository.from_repository_name(self, "FeRepo", "project/frontend")
        board_repo = ecr.Repository.from_repository_name(self, "BoardRepo", "project/board")

        backend_env = {
            "PORT":          "8000",
            "DB_HOST":       db_cluster.cluster_endpoint.hostname,
            "DB_NAME":       DATABASE_NAME,
            "DB_PORT":       "5432",
            "AWS_REGION":    self.region,
            "SQS_QUEUE_URL": messages_queue.queue_url,
            "SQS_MATCHDAY_QUEUE_URL": matchday_calculated_queue.queue_url,
            "SNS_TOPIC_ARN": match_notifications_topic.topic_arn,
            "ADMIN_EMAIL":   admin_email,
            "ADMIN_PASSWORD": admin_password,
        }

        # Aggiungi variabili opzionali per localstack/email/SES
        for key in ["AWS_ENDPOINT_URL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "USE_SES", "AWS_SES_REGION", "SES_SENDER_EMAIL"]:
            if key in env_vars:
                backend_env[key] = env_vars[key]

        # ─────────────────────────────────────────────
        # 8. Container Backend
        #    Riceve credenziali dal Secret di Aurora e altre config
        # ─────────────────────────────────────────────
        backend_container = task_definition.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(be_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment=backend_env,
            secrets={
                "DB_USER":     ecs.Secret.from_secrets_manager(db_cluster.secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_cluster.secret, "password"),
            },
        )
        backend_container.add_port_mappings(ecs.PortMapping(container_port=8000))

        # Permetti al Task ECS (backend) di inviare messaggi sulla coda e interagire con SNS
        messages_queue.grant_send_messages(task_definition.task_role)
        matchday_calculated_queue.grant_send_messages(task_definition.task_role)
        # Il backend necessita di sottoscrivere utenti al Topic SNS e inviare email con SES
        task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Subscribe"],
                resources=[match_notifications_topic.topic_arn]
            )
        )
        task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"]
            )
        )

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
        # 9b. Container Board
        # ─────────────────────────────────────────────
        board_container = task_definition.add_container(
            "BoardContainer",
            image=ecs.ContainerImage.from_ecr_repository(board_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Board"),
            environment={
                "DB_HOST": db_cluster.cluster_endpoint.hostname,
                "DB_NAME": DATABASE_NAME,
                "DB_PORT": "5432",
            },
            secrets={
                "DB_USER": ecs.Secret.from_secrets_manager(db_cluster.secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_cluster.secret, "password"),
            }
        )
        board_container.add_port_mappings(ecs.PortMapping(container_port=8080))

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
            health_check_grace_period=Duration.seconds(120),
        )

        # ─────────────────────────────────────────────
        # 11. Application Load Balancer
        # ─────────────────────────────────────────────
        alb = elbv2.ApplicationLoadBalancer(
            self, "MyALB",
            vpc=vpc,
            internet_facing=True,
        )

        # Inserisci la URL del frontend nel backend
        backend_container.add_environment("FRONTEND_URL", f"http://{alb.load_balancer_dns_name}")

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

        board_listener = alb.add_listener("BoardListener", port=8080)
        board_listener.add_targets(
            "BoardTarget",
            port=8080,
            targets=[fargate_service.load_balancer_target(
                container_name="BoardContainer",
                container_port=8080,
            )],
            health_check=elbv2.HealthCheck(
                path="/login/",
                interval=Duration.seconds(60),
            ),
        )

        # ─────────────────────────────────────────────
        # 12. Sicurezza
        # ─────────────────────────────────────────────
        service_sg.connections.allow_from(alb, ec2.Port.tcp(80))
        service_sg.connections.allow_from(alb, ec2.Port.tcp(8080))
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

        CfnOutput(self, "EcsClusterName",
                  value=cluster.cluster_name,
                  description="Nome del cluster ECS")
                  
        CfnOutput(self, "EcsServiceName",
                  value=fargate_service.service_name,
                  description="Nome del servizio ECS")

        CfnOutput(self, "LambdaName",
                  value=lambda_write_db.function_name,
                  description="Nome della Lambda SQS→Aurora")

        CfnOutput(self, "BoardUrl",
                  value=f"http://{alb.load_balancer_dns_name}:8080",
                  description="URL pubblico della bacheca (Board)")

        CfnOutput(self, "BastionInstanceId",
                  value=bastion.instance_id,
                  description="ID dell'istanza del Bastion Host")