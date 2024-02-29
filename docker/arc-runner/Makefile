all: dind-rootless

# GHA_ID = 343735
# GHA_INST_ID = 38323217
# GHA_PRIVATE_KEY_VAR = GHA_PRIVATE_KEY
REPO_NAMESPACE_IMAGE = ghcr.io/pytorch/arc-runner-pytorch
RIGHT_NOW = $(shell date +'%Y%m%d%H%M%S')

.PHONY: dind-rootless
dind-rootless:
	# [ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	[ "$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" != "" ] || (echo "PUSH_PACKAGE_DOCKER_GITHUB_TOKEN not set"; exit 1)
	@echo "logging in to Github"
	# For now Github does not support logging in for package management with a Github App token
	# I am keeping it here, as we want to migrate as soon as this is supported IMO
	# ../../venv/bin/python ../../scripts/gh_app_get_github_token.py -i $(GHA_ID) -l $(GHA_INST_ID) -k "$$$(GHA_PRIVATE_KEY_VAR)" | docker login ghcr.io -u pytorch --password-stdin
	echo $$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN | docker login ghcr.io -u pytorch --password-stdin
	@echo "Building $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW)"
	docker build -t $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) -f dind-rootless.dockerfile .
	@echo "Pushing $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) and $(REPO_NAMESPACE_IMAGE):latest"
	docker tag $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW) $(REPO_NAMESPACE_IMAGE):latest
	docker push $(REPO_NAMESPACE_IMAGE):v$(RIGHT_NOW)
	docker push $(REPO_NAMESPACE_IMAGE):latest
