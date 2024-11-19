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
  (arn:aws:iam::aws:policy/AdministratorAccess).
* Permissions automatically expire after 6 months if not renewed.
* Permissions will be reviewed and extended on a quarterly cadence by existing
  pytorch-infra-administrators with advisory of regular attendees of the
  PyTorch Infrastructure weekly meeting or the PyTorch TAC.
* MFA-Required policy will be configured on the permissions group.

### Infra Contributor Level Access: pytorch-infra-contributors

This permission level access is granted to those participating in the PyTorch
Infrastructure that may need access to work on features that need access to
AWS to develop.

Policy:

* Power User level permissions will be granted
  (arn:aws:iam::aws:policy/PowerUserAccess).
* Permissions automatically expire after 6 months if not renewed.
* Permissions will be reviewed and extended on a quarterly cadence by
  pytorch-infra-admins.
* MFA-Required policy will be configured on the permissions group.

### Infra Advisory Level Access: pytorch-infra-advisors

This permission level access is granted to those participating in the PyTorch
Infrastructure that do not need write permissions to the infrastructure but are
interested in read only access.

Policy:

* Read-Only level permissions will be granted
  (arn:aws:iam::aws:policy/ReadOnlyAccess).
* Permissions automatically expire after 6 months if not renewed.
* Permissions will be reviewed an extended on a quarterly cadence by existing
  pytorch-infra-admins.

## Implementation

The implementation of the account access will be defined via Terraform
configuration in the **access** directory of this repo.

Automation will be put in place to automatically create a pull request to
extend existing user access on a quarterly basis that need to be reviewed by
the pytorch-infra-admins team to continue extended permissions for any active
user accounts.
