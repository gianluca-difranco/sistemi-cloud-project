AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=eu-south-1

aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com


cd /mnt/d/UNI/Sistemi-Cloud/Progetto/repos/backend-fastapi

docker build -t project/backend .

docker tag project/backend \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/project/backend:latest

docker push \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/project/backend:latest
