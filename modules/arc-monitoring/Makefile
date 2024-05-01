SHELL := /bin/bash -o pipefail

CLUSTERENVIRONMENT := $(shell jq '.["environment"]' <"${CLUSTER_CONFIG_FILE}")
CLUSTERSUFFIX := $(shell jq '.["aws_vpc_suffix"]' <"${CLUSTER_CONFIG_FILE}")

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
	helm repo add eks https://aws.github.io/eks-charts
	helm repo add grafana https://grafana.github.io/helm-charts
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update

.PHONY: install-prometheus
install-prometheus: helm-repo-update
	helm upgrade --install prometheus \
		prometheus-community/prometheus \
		--namespace prometheus \
		--create-namespace \
		--wait \
		--values k8s/prometheus-values.yaml

.PHONY: update-prometheus-url
update-prometheus-url: install-prometheus
	echo $$(kubectl get node -o json | jq '.items[0].metadata.annotations["alpha.kubernetes.io/provided-node-ip"]' | sed 's/"//g'):$$(kubectl get svc prometheus-server --namespace=prometheus -o json | jq '.spec.ports[0].nodePort') >prometheus.url

.PHONY: install-grafana
install-grafana: helm-repo-update update-prometheus-url
	helm upgrade --install grafana \
		grafana/grafana \
		--namespace grafana \
		--create-namespace \
		--wait \
		--values k8s/grafana-values.yaml \
		--set datasources."datasources\.yaml".datasources[0].url="http://$$(cat prometheus.url)"
	# to get the admin password: kubectl get secret --namespace grafana grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo

.PHONY: update-grafana-url
update-grafana-url: install-grafana
	kubectl get svc grafana --namespace=grafana -o json | jq ".status.loadBalancer.ingress[0].hostname" | sed 's/"//g' >grafana.url
