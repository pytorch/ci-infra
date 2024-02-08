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

KARPENTERCONTROLERROLEARN := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_controler_role_arn"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERNODEROLEARN := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_node_role_arn"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERNODEROLE := $(shell jq '.["$(EKS_CLUSTER_NAME)"]["karpenter_node_role_name"]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERSGIDS := $(shell jq -c '[.["$(EKS_CLUSTER_NAME)"]["security_group_ids"][] | {"id": .}]' <"${CLUSTER_CONFIG_FILE}")
KARPENTERSUBNETIDS := $(shell jq -c '[.["$(EKS_CLUSTER_NAME)"]["subnet_ids"][] | {"id": .}]' <"${CLUSTER_CONFIG_FILE}")

RUNNERS_NAMESPACE = actions-runners

.PHONY: clean-k8s-rds-state
clean-k8s-rds-state:
	rm -f $(K8S_RDS_STATE_FILE)
	rm -rf $(HELM_PKG_STATE_FILE)

delete-stale-rds:
	../../venv/bin/python3 ../../scripts/kubectl_delete_rds_resources.py --rds-state-file $(K8S_RDS_STATE_FILE)
	../../venv/bin/python3 ../../scripts/helm_uninstall_runner_templates.py --helm-pkg-state-file $(HELM_PKG_STATE_FILE)

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
	helm repo update

.PHONY: create-runner-namespace
create-runner-namespace:
	kubectl create namespace $(RUNNERS_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

.PHONY: create-gha-arc-secret
create-gha-arc-secret:
	kubectl create secret generic gha-arc-secret \
		--namespace=$(RUNNERS_NAMESPACE) \
		--from-literal=github_app_id="$(GHA_ID)" \
		--from-literal=github_app_installation_id="$(GHA_INST_ID)" \
		--from-literal=github_app_private_key="$$$(GHA_PRIVATE_KEY_VAR)" \
		--dry-run=client -o yaml | kubectl apply -f -

.PHONY: install-arc
install-arc: helm-repo-update create-gha-arc-secret create-runner-namespace
	[ "$(GHA_ID)" != "" ] || (echo "GHA_ID not set"; exit 1)
	[ "$(GHA_INST_ID)" != "" ] || (echo "GHA_INST_ID not set"; exit 1)
	[ "$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "GHA_PRIVATE_KEY_VAR not set"; exit 1)
	[ "$$$(GHA_PRIVATE_KEY_VAR)" != "" ] || (echo "$(GHA_PRIVATE_KEY_VAR) not set"; exit 1)
	helm upgrade --install arc oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
		--namespace actions-runner-system \
		--create-namespace \
		--set=replicaCount=3 \
		--set githubConfigSecret.create=true \
		--set githubConfigSecret.github_app_id="$(GHA_ID)" \
		--set githubConfigSecret.github_app_installation_id="$(GHA_INST_ID)" \
		--set githubConfigSecret.github_app_private_key="$$$(GHA_PRIVATE_KEY_VAR)" \
		--set tolerations[0].key=$(ARC_SYS_TAINT),tolerations[0].operator="Exists",tolerations[0].effect=NoSchedule \
		--wait
	# If the changes are not impacting the controller pod config, it won't restart, so we need to do it manually
	for pod in `kubectl get pod --namespace=actions-runner-system | grep 'arc-gha-rs-controller-' | cut -f 1 -d ' '` ; do \
		kubectl delete pod $$pod --namespace=actions-runner-system ; \
	done

.PHONY: install-karpenter
install-karpenter: helm-repo-update
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$(KARPENTERCONTROLERROLEARN)" != "" ] || (echo "KARPENTERCONTROLERROLEARN not set"; exit 1)
	[ "$(KARPENTERNODEROLEARN)" != "" ] || (echo "KARPENTERNODEROLEARN not set"; exit 1)
	helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
		--namespace karpenter \
		--create-namespace \
		--version v0.32.1 \
		--set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=$(KARPENTERCONTROLERROLEARN) \
		--set settings.clusterName="$(EKS_CLUSTER_NAME)" \
		--set settings.interruptionQueue="$(EKS_CLUSTER_NAME)" \
		--set controller.clusterEndpoint=$$(aws eks describe-cluster --name "$(EKS_CLUSTER_NAME)" --query "cluster.endpoint" --output json) \
		--wait
	if [ "$${NO_EKSCTL}" != "true" ] ; then \
		eksctl create iamidentitymapping --cluster '$(EKS_CLUSTER_NAME)' --arn $(KARPENTERNODEROLEARN) --no-duplicate-arns --username 'system:node:{{EC2PrivateDNSName}}' --group 'system:bootstrappers,system:nodes' ; \
	fi

.PHONY: setup-karpenter-autoscaler
setup-karpenter-autoscaler: install-karpenter
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
		--namespace karpenter \
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
		--root-classes nodeConfig \
		--label-property nodeType
	../../venv/bin/python3 ../../scripts/kubectl_apply_runner_templates.py \
		--template-name k8s/nodepool.yaml \
		--namespace karpenter \
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
		--root-classes nodeConfig \
		--label-property nodeType

.PHONY: k8s-runner-scaler
k8s-runner-scaler: install-arc
	[ "$(CLUSTER_CONFIG_FILE)" != "" ] || (echo "CLUSTER_CONFIG_FILE not set"; exit 1)
	[ "$(EKS_CLUSTER_NAME)" != "" ] || (echo "EKS_CLUSTER_NAME not set"; exit 1)
	[ "$(EKS_ENVIRONMENT)" != "" ] || (echo "EKS_ENVIRONMENT not set"; exit 1)
	[ "$(KARPENTERNODEROLE)" != "" ] || (echo "KARPENTERNODEROLE not set"; exit 1)
	[ '$(KARPENTERSGIDS)' != "" ] || (echo "KARPENTERSGIDS not set"; exit 1)
	[ '$(KARPENTERSUBNETIDS)' != "" ] || (echo "KARPENTERSUBNETIDS not set"; exit 1)
	[ "$(PROJECTTAG)" != "" ] || (echo "PROJECTTAG not set"; exit 1)
	[ "$(RUNNERSCOPE)" != "" ] || (echo "RUNNERSCOPE not set"; exit 1)
	../../venv/bin/python ../../scripts/helm_upgrade_runner_templates.py \
		--template-name k8s/runnerscaleset-values.yaml \
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
			$(ADDITIONAL_VALUES) \
		--root-classes nodeConfig runnerConfig \
		--label-property runnerLabel
