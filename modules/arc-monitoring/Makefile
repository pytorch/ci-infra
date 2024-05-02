SHELL := /bin/bash -o pipefail

CLUSTERENVIRONMENT := $(shell jq '.["environment"]' <"${CLUSTER_CONFIG_FILE}")
CLUSTERSUFFIX := $(shell jq '.["aws_vpc_suffix"]' <"${CLUSTER_CONFIG_FILE}")
LOKIACCESSKEYID := $(shell jq '.["loki_access_key_id"]' <"${CLUSTER_CONFIG_FILE}")
LOKIADMINBUCKET := $(shell jq '.["loki_admin_bucket"]' <"${CLUSTER_CONFIG_FILE}")
LOKICHUNKSBUCKET := $(shell jq '.["loki_chunks_bucket"]' <"${CLUSTER_CONFIG_FILE}")
LOKIRULERBUCKET := $(shell jq '.["loki_ruler_bucket"]' <"${CLUSTER_CONFIG_FILE}")
LOKISECRETACCESSKEY := $(shell jq '.["loki_secret_access_key"]' <"${CLUSTER_CONFIG_FILE}")

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
		--wait --debug \
		--values k8s/prometheus-values.yaml

.PHONY: update-prometheus-url
update-prometheus-url: do-update-kubectl
	echo $$(kubectl get node -o json | jq '.items[0].metadata.annotations["alpha.kubernetes.io/provided-node-ip"]' | sed 's/"//g'):$$(kubectl get svc prometheus-server --namespace=prometheus -o json | jq '.spec.ports[0].nodePort') >prometheus.url

.PHONY: install-loki
install-loki: helm-repo-update
	cat k8s/loki-values.yaml \
		| sed "s/LOKIACCESSKEYID/$(LOKIACCESSKEYID)/g" \
		| sed "s/LOKIADMINBUCKET/$(LOKIADMINBUCKET)/g" \
		| sed "s/LOKICHUNKSBUCKET/$(LOKICHUNKSBUCKET)/g" \
		| sed "s/LOKIRULERBUCKET/$(LOKIRULERBUCKET)/g" \
		| sed "s#LOKISECRETACCESSKEY#$(LOKISECRETACCESSKEY)#g" \
		| sed "s/REGION/$(REGION)/g" \
		>k8s/.loki-values.yaml
	helm upgrade --install loki \
		grafana/loki-stack \
		--namespace loki \
		--create-namespace \
		--wait --debug \
		--values k8s/.loki-values.yaml

.PHONY: update-loki-url
update-loki-url: do-update-kubectl
	echo $$(kubectl get node -o json | jq '.items[0].metadata.annotations["alpha.kubernetes.io/provided-node-ip"]' | sed 's/"//g'):$$(kubectl get svc --namespace=loki loki -o json | jq '.spec.ports[0].nodePort') >loki.url

.PHONY: install-grafana
install-grafana: helm-repo-update update-prometheus-url update-loki-url
	helm upgrade --install grafana \
		grafana/grafana \
		--namespace grafana \
		--create-namespace \
		--wait --debug \
		--values k8s/grafana-values.yaml \
		--set datasources."datasources\.yaml".datasources[0].url="http://$$(cat prometheus.url)" \
		--set datasources."datasources\.yaml".datasources[1].url="http://$$(cat loki.url)"

.PHONY: update-grafana-url
update-grafana-url: do-update-kubectl
	kubectl get svc grafana --namespace=grafana -o json | jq ".status.loadBalancer.ingress[0].hostname" | sed 's/"//g' >grafana.url

.PHONY: update-grafana-password
update-grafana-password: do-update-kubectl
	kubectl get secret --namespace grafana grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo >grafana.password
