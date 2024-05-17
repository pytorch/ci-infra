#!/bin/bash

REGISTRY_FILTER="ghcr.io"
if [ -n "$1" ]; then
    REGISTRY_FILTER=$1
fi

for pod in `kubectl get pod --namespace=docker-registry | grep "${REGISTRY_FILTER}" | cut -f 1 -d' '` ; do
    kubectl logs --namespace=docker-registry $pod -c docker-registry-mirror | grep -v '"GET / HTTP/1.1"'
done
