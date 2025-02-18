# ARC runners config for Pytorch

## Dependencies

This project depends on:

  * python 3.10
  * virtualenv
  * aws cli
  * terraform
  * kubectl cli
  * helm cli
  * CMake
  * [1Password CLI](https://developer.1password.com/docs/cli/)

## Design

It creates a VPC and a EKS cluster. On that it then setups the Github first party ARC solution for GHA runners using helm

## Setup
In order to deploy, you'll need to setup the AWS CLI and 1Password CLI

### AWS CLI Setup
1. Get an AWS account. You may need to contact someone with admin access to send you an invite
2. Ensure 2FA is setup on your AWS account
3. Install the AWS CLI
4. To Auth into the AWS CLI, get a new AWS Access Key ID and Secret Access Key. On the AWS console go to IAM->Users->Your user->Security credentials->Create access key.
5. In your terminal, run `aws configure --profile {account}` to setup your login (currently `{account}` is always `391835788720`). It'll ask you for the AWS access key id and secret access key from the previous step.  For default region name say `us-east-1`. For default output format say `json`.
    1. This will setup your `.aws` folder and create `config` and `credentials` files in there.
    2. Here we have [AWS CLI set up with a profile](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) named with the account where the target will be deployed (based on the path for each account module on `aws/<acc-id>/<region>/`) with all the permissions and keys set up locally.
6. To use the above config as your default setup, you can run `aws configure` a second time, but without the `--profile` param.
7. Run `aws ec2 describe-instances` to verify that you're properly authenticated.

### 1Password setup
You need 1Password to fetch environment secrets and pass them to `make`.

1. Create a 1Password account. Linux Foundation owns 1Password. Ask teammember from there to invite you to create a 1Password account
2. Install and setup the [1Password CLI](https://developer.1password.com/docs/cli/) as per their docs.

The root folder's `make.env` contains paths to various secrets defined in 1Password. To actually use those secrets, you'll want to prefix any command you run with `op run --env-file make.env -- [YOUR_COMMAND]`. This is particularly important for the `make` commands.

You can see what your combined your environment contains by running `op run --env-file make.env -- env`

#### Invoke 1Password more easily
You can add the following function to your `.bashrc` or `.zshrc` file to simplify adding the op prefix. It'll traverse up the tree to find the first file named `make.env` and pass the path to that into `op`.

```
# Alias the 1Password cli. This is for the ci-tools repo. See https://support.1password.com/command-line-getting-started/
# It makes calling "op make" equivalent to "op run --env-file PATH_TO_make.env -- make"
op() {
    command op run --env-file $(file="make.env"; pushd . > /dev/null 2>&1; while [[ "$PWD" != "/" && ! -e "$file" ]]; do cd ..; done; if [[ -e "$file" ]]; then echo "$PWD/$file"; fi; popd > /dev/null 2>&1) -- "$@"
}
```

### Python setup
Ensure you have python 3.10 installed.

### Terraform Lint setup
Optional, but this lets you run terraform lint locally via `make tflint`

#### Setup Terraform
1. Install Terraform: https://developer.hashicorp.com/terraform/tutorials/aws-get-started/install-cli
2. Enable tab completion on bash/zsh: `terraform -install-autocomplete` (optional)

#### Instal TFLint
Instructions: https://github.com/terraform-linters/tflint

Run tflint using the `op` prefix: `op run --env-file make.env -- make tflint`
- Or if you setup the shortcut function, you can run `op make tflint`


### Install eksctl
Instructions: https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/setting-up-eksctl.html

### Authenticate to the Canary clusters
To get authenticated to the kubernetes cluster, you need to be added to the list of authorized users: the EKS list. It's a somewhat complicated process:

1. Get the current EKS_USERS list from 1Password. It's stored as a base64 string.
2. Use `base64` to decode it. (e.g. `echo "the_string" | base64 --decode > users.txt`).
3. Add a line for yourself to the resulting users list
4. Encode the new list back to base64: `base64 -i user.txt`
5. Replace the old EKS_USERS value in 1Password with this new value
6. Go to the Github secrets for this repo. Replace the EKS_USERS secret with this base64 encoded value
7. Get someone who already has access to the clusters to run `CLUSTER_TARGET=[cluster-you-want] make clean apply-arc-canary` to actually propate your name to the relevant clusters

Now you can deploy bits to the cluster

## Deploy
Once the above setup steps are complete you can run make as follows:

```
$ op run --env-file make.env -- make
```

Or if invoking make from a different folder, pass a path to the `make.env` file:
```
# If invoking from aws/<acc-id>/<region>
$ op run --env-file ../../../make.env -- make
```

## Debug/develop

If you're testing changes in packages and want to force make to install newer dependencies, just trigger a `make clean`, it should remove any installed dependency or package locally in the project;

It can be the case that kubectl/helm fail to detect changes in some situations, except from fixing it up and submiting a PR to it and wait to the newer version, you have the option to delete some K8s setup in order to force-replace with `make delete`

There are canary environments to help develop, to update terraform in all canary environments:

```
$ cd aws/<acc-id>/<region>
$ op run --env-file ../../../make.env -- make apply-arc-canary
```

There are 3 canary environments and they can be deployed in steps, the variable `CLUSTER_TARGET` is optional and used to specify one of the environments:

```
# installs/update docker registry and mirrors
$ cd aws/<acc-id>/<region>
$ CLUSTER_TARGET="ghci-arc-c-runners-eks-I" op run --env-file ../../../make.env -- make install-docker-registry-canary

# installs/update karpenter and node config
$ cd aws/<acc-id>/<region>
$ CLUSTER_TARGET="ghci-arc-c-runners-eks-I" op run --env-file ../../../make.env -- make karpenter-autoscaler-canary

# installs/update ARC and runner config
$ cd aws/<acc-id>/<region>
$ CLUSTER_TARGET="ghci-arc-c-runners-eks-I" op run --env-file ../../../make.env -- make k8s-runner-scaler-canary

# do it all inside K8s
$ cd aws/<acc-id>/<region>
$ CLUSTER_TARGET="ghci-arc-c-runners-eks-I" op run --env-file ../../../make.env -- make arc-canary
```

In order to save resources, by default in the canary cluster the minimum number of runners are set to 0 for all runner types. But if other values are needed in order to conduct testing, it is possible to set this number to any other value by setting the variable `CANARY_MIN_RUNNERS`:

```
$ CANARY_MIN_RUNNERS=1 CLUSTER_TARGET="ghci-arc-c-runners-eks-I" op run --env-file ../../../make.env -- make k8s-runner-scaler-canary
```

## Upgrading EKS clusters

To upgrade EKS clusters to a new version:

1. Go to the AWS Console (https://us-east-1.console.aws.amazon.com/eks/home?region=us-east-1#/clusters)
2. For the Cluster(s) you wish to upgrade delete the node groups associated with them
3. Delete the Cluster
4. Run `make apply`  # more specifically apply-canary apply-vanguard apply-prod

## Release to Prod

To release the latest `main` branch to prod do the following:
1. Trigger the ["Runners Open Release PR" workflow](https://github.com/pytorch/ci-infra/actions/workflows/release-do-open-pr.yml) to create a release PR. This PR will be used to manage the deployment
2. Once that PR is ready, get it approved by teammates
3. On that PR, comment `PROCEED_TO_VANGUARD` to deploy the bits to Vanguard (our staging environment)
4. Once vanguard has ben successfully deployed, comment `PROCEED_TO_PRODUCTION` to deploy to prod
5. Once that's done, finally comment `CLEANUP_DEPLOYMENT` to finish up the deployment

## Project organization decision

On the path starting with `aws/` everything that is considered critical and secret should be placed. The idea is that all the other paths could be OpenSourced and any config that is only specific for the cluster being deployed or the account being managed for the responsible team should be placed there. Eventually those configs should be broken into different repositories. Enabling collaborators to reuse the project in a modular approach.

## Update/Upgrade/Deploy monitoring infra
The monitoring infrastructure (except scrappers) is deployed in a separate cluster and is not integrated with the current Makefile targets nor is integrated in the rollout procedure. The reasoning is that it is assumed that there won't be required frequent updates in it and that deploying both in symultaneously can create problems of becoming blind right when monitoring is the most important for infra. So, to update, after communicating with everyone on slack, get a pair programming session with another person in the team and run:

```
$ cd aws/391835788720/us-east-1 && make clean && op run --env-file ../../../make.env -- make apply-arc-canary-monitoring arc-canary-monitoring apply-arc-prod-monitoring arc-prod-monitoring
```
