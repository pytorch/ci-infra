# Cloud Account Access

This document describes the access policy for the Cloud accounts used by this 
repo. While examples will be in AWS, these policies should be replicated to
any other cloud provider we support.

## Access Roles

### Infra Administrator Level Access: pytorch-infra-admins

The Administrator level access is granted to a small set of trusted
contributors to the [pytorch/ci-infra](github.com/pytorch/ci-infra) repository.
These are a small subset of trusted folks who regularly attend the PyTorch
Infrastructure weekly meeting and are able to administer the account.

Policy:

* Administrator level permissions will be granted
  (arn:aws:iam::aws:policy/PowerUserAccess).
* Permissions automatically expire after 6 months if not renewed.
* Permissions will be reviewed and extended on a quarterly cadence by existing
  pytorch-infra-admins with advisory of regular attendees of the weekly
  PyTorch CI Sync meeting or the PyTorch TAC.
* pytorch-infra-admins should themselves be regular attendees of the weekly
  PyTorch CI Sync meeting.
* MFA-Required policy will be configured on the permissions group.

### Infra Advisory Level Access: pytorch-infra-advisors

This permission level access is granted to those participating in the PyTorch
Infrastructure that do not need write permissions to the infrastructure but are
interested in read only access.

Policy:

* Read-Only level permissions will be granted
  (arn:aws:iam::aws:policy/ReadOnlyAccess).
* Permissions automatically expire after 6 months if not renewed.
* Permissions will be reviewed and extended on a quarterly cadence by existing
  pytorch-infra-admins.

## How to request access permissions for new contributors

To request Cloud Account Access for someone who does not already have access
this can be done via one of 2 methods:

1. Request access at the weekly PyTorch CI Sync meeting.
2. At the discretion of one of the existing pytorch-infra-admins.

LFID account access will be defined via Terraform configuration and is managed
by LFIT staff in a private repo. Please contact a PyTorch LFIT staff member to
request access for a new account.

## Access AWS Console

To access AWS Console navigate to the login portal at
https://lfstrategic.awsapps.com/start

Enter your LFID authentication details and you should be directed back to AWS
Console logged in.

## Access AWS CLI

To use the AWS CLI, SSO configuration needs to be setup. Start by running
`aws configure sso` and answer the following questions:

```
SSO session name (Recommended): pytorch-ci
SSO start URL [None]: https://lfstrategic.awsapps.com/start
SSO region [None]: us-west-2

# Leave the next one blank to accept the defaults.
SSO registration scopes [sso:account:access]:
# Web URL will open to authenticate you

Default client Region [None]: us-east-1
Profile name [AWSPowerUserAccess-391835788720]: pytorch-ci
```

Recommended: Export the AWS_PROFILE when using the CLI `export AWS_PROFILE=pytorch-ci`

Once configured you will be able to use the AWS CLI with access tokens. If your
access token requires a refresh. Simply run `aws sso login`.
