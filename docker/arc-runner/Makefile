all: arc-runner

REPO_NAMESPACE_IMAGE = ghcr.io/pytorch/arc-runner-pytorch
RIGHT_NOW = $(shell date +'%Y%m%d%H%M%S')

.PHONY: arc-runner
arc-runner:
	[ "$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" != "" ] || (echo "PUSH_PACKAGE_DOCKER_GITHUB_TOKEN not set"; exit 1)
	echo $$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN | docker login ghcr.io -u pytorch --password-stdin
	@echo "Building $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW)"
	docker build -t $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) -f arc-runner.dockerfile .
	@echo "Pushing $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) and $(REPO_NAMESPACE_IMAGE):latest"
	docker tag $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) $(REPO_NAMESPACE_IMAGE):latest
	docker push $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW)
	docker push $(REPO_NAMESPACE_IMAGE):latest
