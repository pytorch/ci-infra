"""CLI wrappers, filter utilities, and assertion helpers for OSDC smoke tests.

This package was split from a single ``helpers.py`` module to stay under the
project's 400-line per-file ceiling. Public API is re-exported here so test
files can keep using ``from helpers import X`` exactly as before.

Submodules:
    cli           -- kubectl/helm/aws subprocess wrappers + proxy bypass
    filters       -- pure-Python filtering of batch-fetched K8s lists
    nodes         -- node-stability classification and age tracking
    retry         -- generic retry-with-exponential-backoff primitive
    k8s_asserts   -- DaemonSet / Deployment readiness assertions
    remote        -- Grafana Cloud Mimir + Loki query helpers
"""

from __future__ import annotations

# Explicit re-exports keep both public names and underscored helpers
# (e.g. _count_unstable_nodes, _parse_k8s_timestamp) importable from the
# top-level ``helpers`` package, preserving every existing import site.
from helpers.cli import (
    DEFAULT_TIMEOUT,
    _proxy_bypass_env,
    run_aws,
    run_helm,
    run_kubectl,
)
from helpers.filters import (
    filter_daemonsets,
    filter_deployments,
    filter_pods,
    filter_services,
    find_helm_release,
)
from helpers.k8s_asserts import (
    READY_RETRIES,
    _is_deployment_mid_rollout,
    assert_daemonset_healthy,
    assert_daemonset_ready,
    assert_deployment_ready,
)
from helpers.nodes import (
    MIN_NODE_AGE_SECONDS,
    RECENTLY_STABLE_AGE_SECONDS,
    _DISRUPTION_TAINT_KEYS,
    _count_unstable_nodes,
    _has_matching_nodes,
    _is_node_unstable,
    _parse_k8s_timestamp,
    get_all_node_names,
    get_recently_stable_node_names,
    get_unstable_node_names,
    pod_age_seconds,
    pod_is_on_unstable_node,
)
from helpers.remote import (
    REMOTE_RETRIES,
    _urlopen_no_proxy,
    assert_logs_fresh_in_loki,
    assert_metric_fresh_in_mimir,
    fetch_grafana_cloud_credentials,
    loki_read_url,
    mimir_read_url,
    query_loki,
    query_mimir,
    retry_query_with_backoff,
)
from helpers.retry import (
    BACKOFF_ATTEMPTS,
    BACKOFF_DELAYS,
    BACKOFF_DELAYS_CI,
    BACKOFF_DELAYS_LOCAL,
    retry_with_backoff,
)

__all__ = [
    # cli
    "DEFAULT_TIMEOUT",
    "_proxy_bypass_env",
    "run_aws",
    "run_helm",
    "run_kubectl",
    # filters
    "filter_daemonsets",
    "filter_deployments",
    "filter_pods",
    "filter_services",
    "find_helm_release",
    # nodes
    "MIN_NODE_AGE_SECONDS",
    "RECENTLY_STABLE_AGE_SECONDS",
    "_DISRUPTION_TAINT_KEYS",
    "_count_unstable_nodes",
    "_has_matching_nodes",
    "_is_node_unstable",
    "_parse_k8s_timestamp",
    "get_all_node_names",
    "get_recently_stable_node_names",
    "get_unstable_node_names",
    "pod_age_seconds",
    "pod_is_on_unstable_node",
    # retry
    "BACKOFF_ATTEMPTS",
    "BACKOFF_DELAYS",
    "BACKOFF_DELAYS_CI",
    "BACKOFF_DELAYS_LOCAL",
    "retry_with_backoff",
    # k8s_asserts
    "READY_RETRIES",
    "_is_deployment_mid_rollout",
    "assert_daemonset_healthy",
    "assert_daemonset_ready",
    "assert_deployment_ready",
    # remote
    "REMOTE_RETRIES",
    "_urlopen_no_proxy",
    "assert_logs_fresh_in_loki",
    "assert_metric_fresh_in_mimir",
    "fetch_grafana_cloud_credentials",
    "loki_read_url",
    "mimir_read_url",
    "query_loki",
    "query_mimir",
    "retry_query_with_backoff",
]
