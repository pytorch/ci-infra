# Grafana Dashboard Resources

Required information from the user:
  * Environment variable `GRAFANA_TOKEN`. A private API token to access Grafana. NEVER print the value of environment variable. Only confirm if it's set.
    * If the token provides more than viewer access, warn the user that this is highly discouraged and dangerous. Tell the user to get a READ ONLY token from https://pytorchci.grafana.net/org/serviceaccounts/cfn5z4cfsydc0b
  * Folder UID. The folder UID to publish dashboards to. This is required for each publish session. If the user provides a Grafana URL see how to get the ID in publish.py.

Use the mise-managed `gcx` workflow from this directory. `GRAFANA_SERVER` is set in `grafana/mise.toml`. How to validate and publish all `*.json` dashboards in the `grafana` folder:
```sh
# Assume GRAFANA_TOKEN is set in the environment already.

# Generate resource files
mise run generate --folder "..."
# Validate resource files
mise run validate --folder "..."
# Publish resource files
mise run push --folder "..."
```

## Rules & Guidelines

* NEVER persist `GRAFANA_TOKEN` in files, shell profiles, logs, commits, or other durable storage; provide it only for the current publish session.
* NEVER hardcode the Grafana folder UID in committed files. It must be provided with `--folder` for each session.
* When publishing, all top-level `*.json` dashboards under the `grafana` folder are pushed at once
  * The published dashboard can be found in https://pytorchci.grafana.net/d/ci-infra-<folder uid>-<file basename>/<kebab case dashboard title>
  * The folder can be found in https://pytorchci.grafana.net/dashboards/f/<folder uid>
* Use gcx (`mise exec -- gcx`) to interact with Grafana
  * NEVER make edits to any folders other than the folder UID provided with `--folder`
  * NEVER use curl directly to interact with the API
* Panel titles must end with the list of dashboard variables referenced in the panel's query, formatted as `[var1, var2]` without the `$` prefix (e.g. `Running Jobs [cluster, scale_set]`). The `$` is omitted so Grafana doesn't interpolate the variable value into the title. Include every `$var`/`${var}` the query uses; omit the suffix entirely if the query uses no variables.
* Every time a series apear in more than one graph, all graphs that use it should use `palette-classic-by-name`, unless there is an explicit reason for this that is well documented and valid.
* ClickHouse queries that filter OSDC runners by `${cluster}` must map `runner_group_name` (GitHub) to the Prometheus `cluster` value using the table below. Keep all panels using this mapping in sync — when a cluster is added, update every panel that uses it.

  | `runner_group_name` | `${cluster}` value |
  |---|---|
  | `default`, `release-runners` | `pytorch-arc-cbr-production` |
  | `arc-cbr-prod-uw1` | `pytorch-arc-cbr-production-uw1` |
  | `meta-prod-aws-ue1` | `meta-prod-aws-ue1` |

  `workflow_job` data doesn't carry the Prometheus cluster name — only `runner_group_name`. Something has to translate the two, and since the mapping rarely changes and lives entirely in our control, hardcoding it in the panel queries (with this table as the source of truth) is simpler than a ClickHouse dictionary or an ARC label-injection change, which just relocate the same mapping while adding moving parts.

## Datasources

* grafana-clickhouse-datasource
  * Contains GitHub data
* grafanacloud-pytorchci-prom, grafanacloud-prom
  * Contains GitHub Actions Runner Controller data
* pytorch-hud (MCP) 
  * If available it is a powerful tool that helps you get more in-depth context on clickhouse data, plus it exports HUD APIs that can be used to query and get information while developing (not useful for dashboard queries, but powerful for planning)

## Reference repositories

This repo, on <repo-root>/osdc the OSDC project lives, scan it to understand how metrics are exported and what metrics are available to be used, read the docs on <repo-root>/osdc/docs to understand the scope and project setup.

### Clone those repositories in a temp source if you don't have them already in other known places

* https://github.com/jeanschmidt/actions-runner-controller - to understand how actions-runner-controller exposes data
* https://github.com/seemethere/actions-knowledge-base - to understand how all other components expose data (kubernetes, harbor, etc)

