# Helper scripts

## send_scale_message.py

This script is useful to manually tell the autoscaler to create an
GitHub runner instance from the project's
[scale-config.yml](https://github.com/pytorch/pytorch/blob/main/.github/lf-scale-config.yml)
file.

The script will send a message to the SQS queue for the scale up lambda
to pick up and autoscale a new instance.

To use set the `AWS_PROFILE` locally to the profile for the ci-infra
repo project to authenticate to the correct AWS account. Then execute the
script.

```
export AWS_PROFILE=<profile>

# For help
python send_scale_message.py -h

# Create a GitHub runner instance
python send_scale_message.py pytorch/pytorch lf.linux.large
```
