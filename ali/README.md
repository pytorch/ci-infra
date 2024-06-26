# Dev setup

The ALI build pulls in the [terraform-aws-github-runner terraform module](https://github.com/pytorch/test-infra/tree/main/terraform-aws-github-runner) from the pytorch/test-infra repo's main branch.

To test local changes to that branch, you can link your local test-infra repo instead:

```bash
# From this folder
cd aws/391835788720/us-east-1
TEST_INFRA_DIR=[PATH_TO_YOUR_LOCAL_TEST_INFRA_REPO_DIR] make link-test-infra-canary
```

# Troubleshooting

Useful tools for troubleshooting:

- [Send Scale Message](../helpers/README.md)
