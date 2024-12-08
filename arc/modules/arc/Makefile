SHELL := /bin/bash -o pipefail
ARC_SYS_TAINT = "CriticalAddonsOnly"
K8S_RDS_STATE_FILE = ".k8s-rds-state"
HELM_PKG_STATE_FILE = ".helm-pkg-state"

ifneq ($(strip $(MINRUNNERS)),)
  ADDITIONAL_VALUES := minRunners=$(MINRUNNERS)
endif
ifneq ($(strip $(MAXRUNNERS)),)
  ADDITIONAL_VALUES := $(ADDITIONAL_VALUES) maxRunners=$(MAXRUNNERS)
endif

DOCKERREGISTRYBUCKET := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["docker_registry_bucket"]' <"${CLUSTER_CONFIG_FILE}")
DOCKERREGISTRYINTERNALSECRETARN := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["internal_registry_secret_arn"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERCONTROLERROLEARN := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_controler_role_arn"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERNODEROLE := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_node_role_name"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERNODEROLEARN := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_node_role_arn"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERSGIDS := $(shell jq -c '[.["$(EKS_CLUSTER_NAME)"]["security_group_ids"][] | {"id": .}]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERSUBNETIDS := $(shell jq -c '[.["$(EKS_CLUSTER_NAME)"]["subnet_ids"][] | {"id": .}]' <"${CLUSTER_CONFIG_FILE}")

ARC_VERSION = 0.8.3  # DO NOT use 0.9.1 as it breaks on ON_DEMAND instances (https://github.com/actions/actions-runner-controller/issues/3450)
RUNNERS_NAMESPACE = actions-runners
RUNNERS_SYSTEM_NAMESPACE = actions-runner-system
KARPENTER_NAMESPACE = karpenter
KARPENTER_VERSION = v0.32.1
DOCKER_REGISTRY_NAMESPACE = docker-registry
DOCKER_REGISTRY_INTERNAL_NAME = pytorch-internal
DOCKER_REGISTRY_TLS_SECRET_NAME := $(DOCKER_REGISTRY_INTERNAL_NAME)-tls
NVIDIA_NAMESPACE = nvidia-device-plugin

.latest-arc-runner-pytorch:
	[ "$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" != "" ] || (echo "PUSH_PACKAGE_DOCKER_GITHUB_TOKEN not set"; exit 1)
	curl -L \
		-H "Accept: application/vnd.github+json" \
		-H "Authorization: Bearer $$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" \
		-H "X-GitHub-Api-Version: 2022-11-28" \
		https://api.github.com/orgs/pytorch/packages/container/arc-runner-pytorch/versions | \
		jq -r '.[0].metadata.container.tags[]' | \
		grep -v "latest" | \
		sort -r | \
		head -n 1 > .latest-arc-runner-pytorch

.latest-arc-dind-pytorch:
	[ "$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" != "" ] || (echo "PUSH_PACKAGE_DOCKER_GITHUB_TOKEN not set"; exit 1)
	curl -L \
		-H "Accept: application/vnd.github+json" \
		-H "Authorization: Bearer $$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" \
		-H "X-GitHub-Api-Version: 2022-11-28" \
		https://api.github.com/orgs/pytorch/packages/container/arc-dind-pytorch/versions | \
		jq -r '.[0].metadata.container.tags[]' | \
		grep -v "latest" | \
		sort -r | \
		head -n 1 > .latest-arc-dind-pytorch

.PHONY: clean-k8s-rds-state
clean-k8s-rds-state:
	rm -f $(K8S_RDS_STATE_FILE)
	rm -rf $(HELM_PKG_STATE_FILE)

.PHONY: clean
clean: clean-k8s-rds-state
	$(RM) .latest-arc-runner-pytorch
	$(RM) .latest-arc-dind-pytorch
	$(RM) -r ./tls

.PHONY: delete-stale-resources
delete-stale-resources: delete-stale-rds delete-stale-helm-pkgs

.PHONY: delete-stale-rds
delete-stale-rds:
	../../venv/bin/python3 ../../scripts/kubectl_delete_rds_resources.py --rds-state-file $(K8S_RDS_STATE_FILE)

.PHONY: delete-stale-helm-pkgs
delete-stale-helm-pkgs:
	[ "$(EKS_ENVIRONMENT)" != "" ] || (echo "EKS_ENVIRONMENT not set"; exit 1)
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	../../venv/bin/python3 ../../scripts/helm_uninstall_runner_templates.py \
		--eks-environment $(EKS_ENVIRONMENT) \
		--github-app-id $(GHA_ID) \
		--github-app-installation-id $(GHA_INST_ID) \
		--github-app-key "$$$(GHA_PRIVATE_KEY_VAR)" \
		--helm-pkg-state-file $(HELM_PKG_STATE_FILE)

.PHONY: add-eksctl-identity-mappings
add-eksctl-identity-mappings:
	[ "$(EKS_USERS_PATH)" != "" ] || (echo "EKS_USERS_PATH not set"; exit 1)
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	if [ "$${NO_EKSCTL}" != "true" ] ; then \
		cat "$$EKS_USERS_PATH" | while read line ; do \
			eksctl create iamidentitymapping --cluster '$(EKS_CLUSTER_NAME)' --arn $$line --group 'system:masters' --no-duplicate-arns --username 'admin-user1' || exit 1 ; \
		done ; \
	fi

.PHONY: do-update-kubectl
do-update-kubectl:
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	aws eks update-kubeconfig --region "us-east-1" --name "$(EKS_CLUSTER_NAME)"

.PHONY: update-kubectl
update-kubectl: do-update-kubectl add-eksctl-identity-mappings

.PHONY: helm-repo-update
helm-repo-update: update-kubectl
	helm repo add docker-registry-mirror https://t83714.github.io/docker-registry-mirror
	helm repo add grafana https://grafana.github.io/helm-charts
	helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
	helm repo add twuni https://helm.twun.io
	helm repo update

.PHONY: create-runner-namespace
create-runner-namespace:
	kubectl create namespace $(RUNNERS_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

.PHONY: create-gha-arc-secret
create-gha-arc-secret: create-runner-namespace
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(RUNNERS_NAMESPACE)" != "" ] || (echo "RUNNERS_NAMESPACE not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	kubectl create secret generic gha-arc-secret \
		--namespace=$(RUNNERS_NAMESPACE) \
		--from-literal=github_app_id="$(GHA_ID)" \
		--from-literal=github_app_installation_id="$(GHA_INST_ID)" \
		--from-literal=github_app_private_key="$$$(GHA_PRIVATE_KEY_VAR)" \
		--dry-run=client -o yaml | kubectl apply -f -

.PHONY: create-docker-registry-tls-secret
create-docker-registry-tls-secret:
	@echo "Checking if secret $(DOCKER_REGISTRY_TLS_SECRET_NAME) exists, if not creating it"
	if kubectl get secret --namespace=$(DOCKER_REGISTRY_NAMESPACE) $(DOCKER_REGISTRY_TLS_SECRET_NAME) ; then \
		echo "Secret $(DOCKER_REGISTRY_TLS_SECRET_NAME) found, not changing it" ; \
	else \
		echo "Secret $(DOCKER_REGISTRY_TLS_SECRET_NAME) not found, creating it" ; \
		mkdir -p ./tls ; \
		openssl req \
			-new -newkey rsa:4096 -x509 -sha256 \
			-days 3650 -nodes \
			-out tls/tls.crt \
			-keyout tls/tls.key \
			-addext "subjectAltName = DNS:$(DOCKER_REGISTRY_INTERNAL_NAME).$(DOCKER_REGISTRY_NAMESPACE).svc.cluster.local" \
			-subj "/CN=$(DOCKER_REGISTRY_INTERNAL_NAME).$(DOCKER_REGISTRY_NAMESPACE).svc.cluster.local" ; \
		kubectl create namespace $(DOCKER_REGISTRY_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f - ; \
		kubectl create secret tls $(DOCKER_REGISTRY_TLS_SECRET_NAME) \
			--namespace=$(DOCKER_REGISTRY_NAMESPACE) \
			--cert=tls/tls.crt \
			--key=tls/tls.key \
			--dry-run=client -o yaml | kubectl apply -f - ; \
	fi

.PHONY: install-docker-registry
install-docker-registry: helm-repo-update create-docker-registry-tls-secret
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	[ "$(REGION)" != "" ] || (echo "REGION not set"; exit 1)
	[ "$(CLUSTER_CONFIG_FILE)" != "" ] || (echo "CLUSTER_CONFIG_FILE not set"; exit 1)
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$$DOCKER_REGISTRY_HTPASSWD" != "" ] || (echo "DOCKER_REGISTRY_HTPASSWD not set"; exit 1)
	[ "$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" != "" ] || (echo "PUSH_PACKAGE_DOCKER_GITHUB_TOKEN not set"; exit 1)
	helm upgrade --install docker-registry-mirror-docker-io docker-registry-mirror/docker-registry-mirror \
		--namespace=$(DOCKER_REGISTRY_NAMESPACE) \
		--create-namespace \
		--wait --debug \
		--set service.type=ClusterIP \
		--set service.port=5000 \
		--set service.clusterIP=172.20.56.113 \
		--set replicaCount=3 \
		--set tolerations[0].key=$(ARC_SYS_TAINT),tolerations[0].operator="Exists",tolerations[0].effect=NoSchedule
	helm upgrade --install docker-registry-mirror-ghcr-io docker-registry-mirror/docker-registry-mirror \
		--namespace=$(DOCKER_REGISTRY_NAMESPACE) \
		--create-namespace \
		--wait --debug \
		--set proxy.remoteurl=https://ghcr.io \
		--set proxy.username=pytorch \
		--set proxy.password="$$PUSH_PACKAGE_DOCKER_GITHUB_TOKEN" \
		--set service.type=ClusterIP \
		--set service.port=5000 \
		--set service.clusterIP=172.20.56.114 \
		--set replicaCount=3 \
		--set tolerations[0].key=$(ARC_SYS_TAINT),tolerations[0].operator="Exists",tolerations[0].effect=NoSchedule
	helm upgrade --install $(DOCKER_REGISTRY_INTERNAL_NAME) twuni/docker-registry \
		--namespace=$(DOCKER_REGISTRY_NAMESPACE) \
		--create-namespace \
		--wait --debug \
		--set tlsSecretName=$(DOCKER_REGISTRY_TLS_SECRET_NAME) \
		--set s3.region=$(REGION) \
		--set s3.bucket=$(DOCKERREGISTRYBUCKET) \
		--set secrets.s3.accessKey=`jq '.["$(EKS_CLUSTER_NAME)"]["docker_registry_user_access_key"]' <"${CLUSTER_CONFIG_FILE}"` \
		--set secrets.s3.secretKey=`jq '.["$(EKS_CLUSTER_NAME)"]["docker_registry_user_secret"]' <"${CLUSTER_CONFIG_FILE}"` \
		--set secrets.htpasswd="$$DOCKER_REGISTRY_HTPASSWD" \
		--set tolerations[0].key=$(ARC_SYS_TAINT),tolerations[0].operator="Exists",tolerations[0].effect=NoSchedule \
		--set fullnameOverride=$(DOCKER_REGISTRY_INTERNAL_NAME) \
		--values k8s/internal-docker-registry.yaml
	kubectl patch svc $(DOCKER_REGISTRY_INTERNAL_NAME) --namespace=$(DOCKER_REGISTRY_NAMESPACE) --patch-file k8s/pytorch-internal-svc-patch.yaml

.PHONY: install-arc
install-arc: helm-repo-update create-gha-arc-secret
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	helm upgrade --install arc oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
		--namespace $(RUNNERS_SYSTEM_NAMESPACE) \
		--create-namespace \
		--version ${ARC_VERSION} \
		--set=replicaCount=3 \
		--set githubConfigSecret.create=true \
		--set githubConfigSecret.github_app_id="$(GHA_ID)" \
		--set githubConfigSecret.github_app_installation_id="$(GHA_INST_ID)" \
		--set githubConfigSecret.github_app_private_key="$$$(GHA_PRIVATE_KEY_VAR)" \
		--set tolerations[0].key=$(ARC_SYS_TAINT),tolerations[0].operator="Exists",tolerations[0].effect=NoSchedule \
		--wait --debug

.PHONY: install-karpenter
install-karpenter: helm-repo-update
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$(KARPENTERCONTROLERROLEARN)" != "" ] || (echo "KARPENTERCONTROLERROLEARN not set"; exit 1)
	[ "$(KARPENTERNODEROLEARN)" != "" ] || (echo "KARPENTERNODEROLEARN not set"; exit 1)
	helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
		--namespace $(KARPENTER_NAMESPACE) \
		--create-namespace \
		--version $(KARPENTER_VERSION) \
		--set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=$(KARPENTERCONTROLERROLEARN) \
		--set settings.clusterName="$(EKS_CLUSTER_NAME)" \
		--set settings.interruptionQueue="$(EKS_CLUSTER_NAME)" \
		--set controller.clusterEndpoint=$$(aws eks describe-cluster --name "$(EKS_CLUSTER_NAME)" --query "cluster.endpoint" --output json) \
		--wait
	if [ "$${NO_EKSCTL}" != "true" ] ; then \
		eksctl create iamidentitymapping --cluster '$(EKS_CLUSTER_NAME)' --arn $(KARPENTERNODEROLEARN) --no-duplicate-arns --username 'system:node:{{EC2PrivateDNSName}}' --group 'system:bootstrappers,system:nodes' ; \
	fi

.PHONY: setup-karpenter-autoscaler
setup-karpenter-autoscaler: do-update-kubectl
	[ "$(CLUSTER_CONFIG_FILE)" != "" ] || (echo "CLUSTER_CONFIG_FILE not set"; exit 1)
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$(EKS_ENVIRONMENT)" != "" ] || (echo "EKS_ENVIRONMENT not set"; exit 1)
	[ "$(KARPENTERNODEROLE)" != "" ] || (echo "KARPENTERNODEROLE not set"; exit 1)
	[ '$(KARPENTERSGIDS)' != "" ] || (echo "KARPENTERSGIDS not set"; exit 1)
	[ '$(KARPENTERSUBNETIDS)' != "" ] || (echo "KARPENTERSUBNETIDS not set"; exit 1)
	[ "$(PROJECTTAG)" != "" ] || (echo "PROJECTTAG not set"; exit 1)
	[ "$(RUNNERSCOPE)" != "" ] || (echo "RUNNERSCOPE not set"; exit 1)
	../../venv/bin/python3 ../../scripts/kubectl_apply_runner_templates.py \
		--template-name k8s/nodeclass.yaml \
		--namespace $(KARPENTER_NAMESPACE) \
		--arc-runner-config-files $(ARC_CFG_FILE_FOLDER)/ARC_NODE_CONFIG.yaml \
		--rds-state-file $(K8S_RDS_STATE_FILE) \
		--runner-scope $(RUNNERSCOPE) \
		--additional-values \
			eksclustername=$(EKS_CLUSTER_NAME) \
			environment=$(EKS_ENVIRONMENT) \
			karpenternoderole=$(KARPENTERNODEROLE) \
			karpentersgids='$(KARPENTERSGIDS)' \
			karpentersubnetids='$(KARPENTERSUBNETIDS)' \
			project=gh-ci-$(EKS_ENVIRONMENT)-arc \
			projecttag=$(PROJECTTAG) \
			dockerregistrymirror=`kubectl get svc --namespace=docker-registry docker-registry-mirror-docker-io -o json | jq ".spec.clusterIP" | sed 's/"//g'` \
			githubregistrymirror=`kubectl get svc --namespace=docker-registry docker-registry-mirror-ghcr-io -o json | jq ".spec.clusterIP" | sed 's/"//g'` \
			pytorchregistrymirror=`kubectl get svc --namespace=docker-registry $(DOCKER_REGISTRY_INTERNAL_NAME) -o json | jq ".spec.clusterIP" | sed 's/"//g'` \
			DOCKERREGISTRYINTERNALSECRETARN=$(DOCKERREGISTRYINTERNALSECRETARN) \
		--root-classes nodeConfig \
		--label-property nodeType
	../../venv/bin/python3 ../../scripts/kubectl_apply_runner_templates.py \
		--template-name k8s/nodepool.yaml \
		--namespace $(KARPENTER_NAMESPACE) \
		--arc-runner-config-files $(ARC_CFG_FILE_FOLDER)/ARC_NODE_CONFIG.yaml \
		--rds-state-file $(K8S_RDS_STATE_FILE) \
		--runner-scope $(RUNNERSCOPE) \
		--additional-values \
			eksclustername=$(EKS_CLUSTER_NAME) \
			environment=$(EKS_ENVIRONMENT) \
			karpenternoderole=$(KARPENTERNODEROLE) \
			karpentersgids='$(KARPENTERSGIDS)' \
			karpentersubnetids='$(KARPENTERSUBNETIDS)' \
			project=gh-ci-$(EKS_ENVIRONMENT)-arc \
			projecttag=$(PROJECTTAG) \
			DOCKERREGISTRYINTERNALSECRETARN=$(DOCKERREGISTRYINTERNALSECRETARN) \
		--root-classes nodeConfig \
		--label-property nodeType

.PHONY: k8s-runner-scaler
k8s-runner-scaler: .latest-arc-runner-pytorch .latest-arc-dind-pytorch
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$(EKS_ENVIRONMENT)" != "" ] || (echo "EKS_ENVIRONMENT not set"; exit 1)
	[ "$(KARPENTERNODEROLE)" != "" ] || (echo "KARPENTERNODEROLE not set"; exit 1)
	[ '$(KARPENTERSGIDS)' != "" ] || (echo "KARPENTERSGIDS not set"; exit 1)
	[ '$(KARPENTERSUBNETIDS)' != "" ] || (echo "KARPENTERSUBNETIDS not set"; exit 1)
	[ "$(PROJECTTAG)" != "" ] || (echo "PROJECTTAG not set"; exit 1)
	[ "$(RUNNERSCOPE)" != "" ] || (echo "RUNNERSCOPE not set"; exit 1)
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	[ "$(ARC_VERSION)" != "" ] || (echo "ARC_VERSION not set"; exit 1)
	../../venv/bin/python3 ../../scripts/helm_upgrade_runner_templates.py \
		--github-app-id $(GHA_ID) \
		--github-app-installation-id $(GHA_INST_ID) \
		--github-app-key "$$$(GHA_PRIVATE_KEY_VAR)" \
		--arc-version $(ARC_VERSION) \
		--template-name runnerscaleset/values.yaml \
		--namespace $(RUNNERS_NAMESPACE) \
		--arc-runner-config-files $(ARC_CFG_FILE_FOLDER)/ARC_NODE_CONFIG.yaml $(ARC_CFG_FILE_FOLDER)/ARC_RUNNER_CONFIG.yaml \
		--helm-pkg-state-file $(HELM_PKG_STATE_FILE) \
		--runner-scope $(RUNNERSCOPE) \
		--additional-values \
			eksclustername=$(EKS_CLUSTER_NAME) \
			environment=$(EKS_ENVIRONMENT) \
			karpenternoderole=$(KARPENTERNODEROLE) \
			karpentersgids='$(KARPENTERSGIDS)' \
			karpentersubnetids='$(KARPENTERSUBNETIDS)' \
			project=gh-ci-$(EKS_ENVIRONMENT)-arc \
			projecttag=$(PROJECTTAG) \
			latestrunnerimg=`cat .latest-arc-runner-pytorch` \
			latestdindimg=`cat .latest-arc-dind-pytorch` \
			$(ADDITIONAL_VALUES) \
		--root-classes nodeConfig runnerConfig \
		--label-property runnerLabel

.PHONY: setup-nvidia-device-plugin
setup-nvidia-device-plugin: helm-repo-update
	helm upgrade --install nvdp nvdp/nvidia-device-plugin \
		--namespace $(NVIDIA_NAMESPACE) \
		--create-namespace \
		--values runnerscaleset/nvidia-device-plugin-values.yaml
		--wait --debug

.PHONY: install-grafana-scrappers
install-grafana-scrappers: helm-repo-update
	[ "$(LOKI_URL)" != "" ] || (echo "LOKI_URL not set"; exit 1)
	[ "$(PROMETHEUS_URL)" != "" ] || (echo "PROMETHEUS_URL not set"; exit 1)
	helm upgrade --install \
		grafana-k8s-monitoring grafana/k8s-monitoring \
		--set alloy-events.controller.tolerations[0].key=$(ARC_SYS_TAINT),alloy-events.controller.tolerations[0].operator="Exists",alloy-events.controller.tolerations[0].effect=NoSchedule \
		--set alloy-logs.controller.tolerations[0].key=$(ARC_SYS_TAINT),alloy-logs.controller.tolerations[0].operator="Exists",alloy-logs.controller.tolerations[0].effect=NoSchedule \
		--set alloy-profiles.controller.tolerations[0].key=$(ARC_SYS_TAINT),alloy-profiles.controller.tolerations[0].operator="Exists",alloy-profiles.controller.tolerations[0].effect=NoSchedule \
		--set alloy.controller.tolerations[0].key=$(ARC_SYS_TAINT),alloy.controller.tolerations[0].operator="Exists",alloy.controller.tolerations[0].effect=NoSchedule \
		--set alloy.tolerations[0].key=$(ARC_SYS_TAINT),alloy.tolerations[0].operator="Exists",alloy.tolerations[0].effect=NoSchedule \
		--set cluster.name=$(EKS_CLUSTER_NAME) \
		--set configAnalysis.tolerations[0].key=$(ARC_SYS_TAINT),configAnalysis.tolerations[0].operator="Exists",configAnalysis.tolerations[0].effect=NoSchedule \
		--set configValidator.tolerations[0].key=$(ARC_SYS_TAINT),configValidator.tolerations[0].operator="Exists",configValidator.tolerations[0].effect=NoSchedule \
		--set externalServices.loki.host="http://$(LOKI_URL)" \
		--set externalServices.prometheus.host="http://$(PROMETHEUS_URL)" \
		--set externalServices.prometheus.queryEndpoint="/api/v1/query" \
		--set externalServices.prometheus.writeEndpoint="/api/v1/write" \
		--set kube-state-metrics.tolerations[0].key=$(ARC_SYS_TAINT),kube-state-metrics.tolerations[0].operator="Exists",kube-state-metrics.tolerations[0].effect=NoSchedule \
		--set test.tolerations[0].key=$(ARC_SYS_TAINT),test.tolerations[0].operator="Exists",test.tolerations[0].effect=NoSchedule \
		--wait --timeout 300s --debug

# This is required, as failed ephemeral runners, even when not created, count towards the total number of runners both for quota (max) and for the numeb of runners
# so arc won't create them, but they will never go live, ever.
.PHONY: delete-stuck-ephemeral-runners
delete-stuck-ephemeral-runners: do-update-kubectl
	if kubectl get ephemeralrunners --namespace=actions-runners | grep Failed ; then \
		kubectl delete ephemeralrunners --namespace=actions-runners $$(kubectl get ephemeralrunners --namespace=actions-runners | grep Failed | cut -d ' ' -f 1) ; \
	fi

# This is required, as nodes that aws-node pods are stuck on, will never be deleted by karpenter, so we need to delete them manually
.PHONY: delete-stuck-aws-node-pods
delete-stuck-aws-node-pods: do-update-kubectl
	if kubectl get pods -n kube-system -l k8s-app=aws-node -o wide | grep 'Init:' ; then \
		kubectl delete pod -n kube-system $$(kubectl get pods -n kube-system -l k8s-app=aws-node -o wide | grep 'Init:' | cut -d ' ' -f 1) ; \
	fi
