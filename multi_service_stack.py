from aws_cdk import (
    Stack,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_rds as rds,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_sources,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
    RemovalPolicy
)
from constructs import Construct

class MultiServiceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. VPC con Subnet Pubbliche e Private
        vpc = ec2.Vpc(self, "Vpc", max_azs=2)

        # 2. Database Aurora PostgreSQL (Rete Privata)
        db_cluster = rds.DatabaseCluster(
            self, "AuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgresql(
                version=rds.AuroraPostgresEngineVersion.VER_15_4
            ),
            writer=rds.ClusterInstance.provisioned("writer",
                instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MEDIUM),
                publicly_accessible=False,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            removal_policy=RemovalPolicy.DESTROY # Cambiare in RETAIN per produzione
        )

        # 3. ECS Cluster
        cluster = ecs.Cluster(self, "EcsCluster", vpc=vpc)

        # --- SERVIZI ECS ---

        # FE (Front-End) - Porta 80/443 su ALB
        fe_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "FeService",
            cluster=cluster,
            cpu=256, memory_limit_mib=512,
            desired_count=1,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry("amazon/amazon-ecs-sample"), # Sostituire con ECR image
                container_port=80,
            ),
            public_load_balancer=True,
            assign_public_ip=False # Gira in rete privata
        )

        # BA (Business Admin?) - Porta 80 su ALB
        # Usiamo lo stesso bilanciatore del FE per efficienza (Aggiungiamo un listener o una regola)
        ba_task = ecs.FargateTaskDefinition(self, "BaTask", cpu=256, memory_limit_mib=512)
        ba_container = ba_task.add_container("BaContainer",
            image=ecs.ContainerImage.from_registry("amazon/amazon-ecs-sample"), # Sostituire con ECR
            port_mappings=[ecs.PortMapping(container_port=80)]
        )
        ba_service = ecs.FargateService(self, "BaService", cluster=cluster, task_definition=ba_task)
        
        # Aggiungiamo BA al listener dell'ALB creato da FE (su una porta diversa o path diverso)
        # Qui lo configuriamo sulla porta 8080 dell'ALB per distinguerlo, o potresti usare path-routing
        fe_service.load_balancer.add_listener("BaListener",
            port=8080, # Esempio: BA risponde sulla 8080 del LB pubblico
            default_action=ecs_patterns.ApplicationLoadBalancedFargateService.load_balancer.add_listener("L1", port=80).connections.allow_default_port_from_anywhere()
        ).add_targets("BaTarget",
            port=80,
            targets=[ba_service]
        )

        # BE (Back-End) - Porta 8000 (Solo interno o diretto)
        be_task = ecs.FargateTaskDefinition(self, "BeTask", cpu=256, memory_limit_mib=512)
        be_container = be_task.add_container("BeContainer",
            image=ecs.ContainerImage.from_registry("amazon/amazon-ecs-sample"),
            port_mappings=[ecs.PortMapping(container_port=8000)]
        )
        be_service = ecs.FargateService(self, "BeService", cluster=cluster, task_definition=be_task)

        # 4. SQS e SNS
        queue = sqs.Queue(self, "MainQueue", visibility_timeout=Duration.seconds(300))
        topic = sns.Topic(self, "EmailTopic")
        # Iscrizione email (da confermare via mail dopo il deploy)
        topic.add_subscription(subs.EmailSubscription("tuo-email@esempio.com"))

        # 5. Lambda Functions
        
        # Lambda 1: SQS -> Aurora
        lambda_to_db = _lambda.Function(
            self, "LambdaToDb",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=_lambda.Code.from_inline("# Codice per scrivere su Aurora"),
            vpc=vpc, # Necessario per parlare con Aurora in rete privata
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        )
        lambda_to_db.add_event_source(lambda_sources.SqsEventSource(queue))

        # Lambda 2: SQS -> SNS
        lambda_to_sns = _lambda.Function(
            self, "LambdaToSns",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=_lambda.Code.from_inline("# Codice per inviare a SNS"),
            vpc=vpc
        )
        lambda_to_sns.add_event_source(lambda_sources.SqsEventSource(queue))
        topic.grant_publish(lambda_to_sns)

        # --- PERMESSI E SICUREZZA (Security Groups) ---

        # Permessi Aurora: BE, BA e Lambdas possono connettersi sulla 5432
        db_cluster.connections.allow_from(be_service, ec2.Port.tcp(5432), "BE to Aurora")
        db_cluster.connections.allow_from(ba_service, ec2.Port.tcp(5432), "BA to Aurora")
        db_cluster.connections.allow_from(lambda_to_db, ec2.Port.tcp(5432), "Lambda to Aurora")
        
        # Permessi SQS: Lambda devono leggere
        queue.grant_consume_messages(lambda_to_db)
        queue.grant_consume_messages(lambda_to_sns)