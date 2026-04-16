from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr, 
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    Duration,
    CfnOutput
)
from constructs import Construct

class EcsMultiContainerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. Creazione del VPC con sottoreti pubbliche e private
        vpc = ec2.Vpc(self, "MyVpc", max_azs=2)

        # 2. Cluster ECS
        cluster = ecs.Cluster(self, "MyCluster", vpc=vpc)

        # 3. Task Definition (Fargate)
        task_definition = ecs.FargateTaskDefinition(
            self, "MyTaskDef",
            memory_limit_mib=1024,
            cpu=512
        )


        # Immagini ECR fornite
        # 1. Recupera i riferimenti ai repository esistenti
        be_repo = ecr.Repository.from_repository_name(self, "BeRepo", "project/backend")
        fe_repo = ecr.Repository.from_repository_name(self, "FeRepo", "project/frontend")

        # 4. Container Backend (BE)
        backend_container = task_definition.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(be_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment={
                "PORT": "8000"
            }
        )
        backend_container.add_port_mappings(
            ecs.PortMapping(container_port=8000)
        )

        # 5. Container Frontend (FE)
        frontend_container = task_definition.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_ecr_repository(fe_repo),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
            environment={
                "BACKEND_URL": "http://localhost:8000" # Comunicazione interna
            }
        )
        frontend_container.add_port_mappings(
            ecs.PortMapping(container_port=80)
        )

        # 6. Creazione del Security Group per il Servizio ECS
        service_sg = ec2.SecurityGroup(self, "ServiceSG", vpc=vpc, allow_all_outbound=True)

        # 7. Servizio Fargate
        fargate_service = ecs.FargateService(
            self, "MyFargateService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            assign_public_ip=False, # Il servizio sta in rete privata
            security_groups=[service_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        
            circuit_breaker=ecs.DeploymentCircuitBreaker(
            rollback=True # Se fallisce, annulla tutto e torna alla versione precedente
            ),
            # Riduce il tempo di attesa prima di considerare un task fallito (default 60s)
            health_check_grace_period=Duration.seconds(30) 
            # -----------------------------
        )
        # 8. Application Load Balancer (Pubblico)
        alb = elbv2.ApplicationLoadBalancer(
            self, "MyALB",
            vpc=vpc,
            internet_facing=True # Esposto al pubblico
        )

        # 9. Listener e Target Group
        listener = alb.add_listener("PublicListener", port=80)
        
        # Il Load Balancer punta solo al container Frontend sulla porta 80
        target_group = listener.add_targets(
            "FargateTarget",
            port=80,
            targets=[fargate_service.load_balancer_target(
                container_name="FrontendContainer",
                container_port=80
            )],
            health_check=elbv2.HealthCheck(
                path="/", # Assicurati che il FE risponda qui
                interval=Duration.seconds(60)
            )
        )

        # 10. Sicurezza: Permetti traffico dall'ALB al FE sulla porta 80
        service_sg.connections.allow_from(alb, ec2.Port.tcp(80))
        # Nota: La porta 8000 del backend non è aperta nel Security Group, 
        # quindi è accessibile solo dall'interno del task (localhost).

        # Output dell'URL del Load Balancer
        CfnOutput(self, "ALBUrl", value=alb.load_balancer_dns_name)