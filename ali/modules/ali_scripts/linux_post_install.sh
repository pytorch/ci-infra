#!/usr/bin/env bash

set +e

# Log in to docker.io, it's ok if this fails, we will just fallback to an anonymous user then.
# This is to mitigate https://docs.docker.com/docker-hub/download-rate-limit/#rate-limit
aws secretsmanager get-secret-value --secret-id docker_hub_readonly_token | jq --raw-output '.SecretString' | jq -r .docker_hub_readonly_token | docker login --username pytorchbot --password-stdin || true

# Log in to our ECR instance
if uname -a | grep 'amzn2023' > /dev/null ; then
    echo "New amazon linux"
    aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 308535385114.dkr.ecr.us-east-1.amazonaws.com
else
    echo "Old amazon linux"
    $(aws ecr get-login --no-include-email --region us-east-1)
fi

# copy the docker config from root to ec2-user, so both users can access the same registries
mkdir -p /home/ec2-user/.docker
cat </root/.docker/config.json >/home/ec2-user/.docker/config.json
chown -R ec2-user:ec2-user /home/ec2-user/.docker 
