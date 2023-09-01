ARC runners config for Pytorch

## Dependencies

This project depends on:
    * python 3.10
    * virtualenv
    * aws cli
    * terraform
    * kubectl cli
    * heml cli
    * CMake

## Design

It creates a VPC and a EKS cluster. On that it then setups the Github first party ARC solution for GHA runners using helm

## Deploy

In order to deploy, first make sure your environment is set up so you have your [AWS CLI set up with a profile](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) named with the account where the target will be deployed (currently `391835788720`) and all the permissions and keys are set up; 

Next, you'll need to setup as an environment variable the Github API private key:

```
export GHA_PRIVATE_KEY='the private key here'
```

You should be ready to deploy:

```
cd aws/391835788720/us-east-1
make
```

## Debug/develop

If you're testing changes in packages and want to force make to install newer dependencies, just trigger a `make clean`, it should remove any installed dependency or package locally in the project;

It can be the case that kubectl/helm fail to detect changes in some situations, except from fixing it up and submiting a PR to it and wait to the newer version, you have the option to delete some K8s setup in order to force-replace with `make delete`