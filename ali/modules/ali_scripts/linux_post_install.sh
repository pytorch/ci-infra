#!/usr/bin/env bash

# Log in to our ECR instance
$(aws ecr get-login --no-include-email --region us-east-1)
