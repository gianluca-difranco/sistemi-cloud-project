import aws_cdk as cdk
from cdk_project.cdk_project_stack import EcsMultiContainerStack

app = cdk.App()

# Definiamo l'ambiente esplicitamente
env_eu_south_1 = cdk.Environment(
    account="213295721392", 
    region="eu-south-1"
)

EcsMultiContainerStack(app, "EcsMultiContainerStack", env=env_eu_south_1)

app.synth()