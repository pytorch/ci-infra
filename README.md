# PyTorch ci-infra

The PyTorch ci-infra repo contains the terraform configuration for the PyTorch
Lambda based EC2 instance autoscaler.

## PyTorch CI Users

The PyTorch CI infrastructure is for the use of the github.com/pytorch/pytorch
project. It powers GitHub Actions self-hosted runners from the PyTorch
Foundation's Cloud Provider Accounts.

The CI Infrastructure is widely scoped, and related work is still in progress. As a result,
the overall documentation is spread out across various repositories and working groups. A summary
of the available learning documentation is provided below.

### Test Infra

The [PyTorch test-infra project](https://github.com/pytorch/test-infra) is collection of infrastructure components that are supporting the PyTorch CI/CD system.

#### Terraform AWS Github Runner

Learn about the [terraform module](https://github.com/pytorch/test-infra/tree/main/terraform-aws-github-runner) that sets up self hosted github runners on AWS along with the infra needed to autoscale them.

#### Partners CI Runners

If you are interested in contributing to the PyTorch CI providing runners, these [guidelines](https://github.com/pytorch/test-infra/blob/main/docs/partners_pytorch_ci_runners.md) can help you get started.

#### Wiki
The [test-infra wiki](https://github.com/pytorch/test-infra/wiki) serves to host all of the code used for testing infrastructure across the PyTorch organization.

### Monitoring and Observability

Learn more about [Terraform configurations for managing Datadog monitoring and observability](https://github.com/pytorch-fdn/monitoring-observability) infrastructure for the PyTorch Foundation.

### CI Working Group

The focus of the [PyTorch CI working group](https://github.com/pytorch-fdn/tac/tree/main/working-groups/ci-wg) is to maintain, improve, monitor, and cost manage the existing PyTorch CI infrastructure.

### Multi-Cloud Working Group

The [PyTorch multi-cloud working group](https://github.com/pytorch-fdn/tac/tree/main/working-groups/multi-cloud-wg) has been created to develop a sustainable, equitable, community managed approach to the CI/CD pipeline for PyTorch in a multi-cloud environment.

This working group complement the work done by the PyTorch CI Working Group by focusing on expanding the CI/CD infrastructure to a multi-cloud environment, in a way that is sustainable for the community and for the CI WG.

You can learn more about proposed architecture, guidelines, and other information [here](https://github.com/pytorch-fdn/multicloud-ci-infra).

To stay uptodated with the ongoing progress, refer to the working group [google doc](https://docs.google.com/document/d/1hJZfphY9Yx8PafkIDibN0Mn9oxltwUgKQVdRkkMvZhk/edit?tab=t.ype421gftork#heading=h.xzfgjlf77i1) and [here](https://docs.google.com/document/d/1hJZfphY9Yx8PafkIDibN0Mn9oxltwUgKQVdRkkMvZhk/edit?tab=t.40np4wx0o8i2#heading=h.8sg9bj263sk4).

### PyTorch HUD

Learn more about [PyTorch HUD](https://hud.pytorch.org/hud/pytorch/pytorch/main/1?per_page=50) and [services used by the HUD](https://github.com/pytorch/test-infra/tree/main/torchci).

### CI/CD Security Principles

Learn more about the [CI/CD security principles](https://github.com/pytorch/pytorch/blob/main/SECURITY.md#cicd-security-principles).
