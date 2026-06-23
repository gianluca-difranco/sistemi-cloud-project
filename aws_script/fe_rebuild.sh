AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=eu-south-1

aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

cd /mnt/d/UNI/Sistemi-Cloud/Progetto/repos/fe

docker build \
  --build-arg REACT_APP_API_URL=/api \
  -t project/frontend .
docker tag project/frontend \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/project/frontend:latest
docker push \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/project/frontend:latest
