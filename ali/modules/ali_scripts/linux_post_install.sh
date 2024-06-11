#!/usr/bin/env bash

set +e

# Log in to our ECR instance
if uname -a | grep 'amzn2023' > /dev/null ; then
    echo "New amazon linux"
    aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 308535385114.dkr.ecr.us-east-1.amazonaws.com
else
    echo "Old amazon linux"
    $(aws ecr get-login --no-include-email --region us-east-1)
fi

