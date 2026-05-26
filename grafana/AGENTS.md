# Grafana Dashboard Resources

Required information from the user:
  * Environment variable `GRAFANA_TOKEN`. A private API token to access Grafana. NEVER print the value of environment variable. Only confirm if it's set.
    * If the token provides more than viewer access, warn the user that this is highly discouraged and dangerous.
  * Folder UID. The folder UID to publish dashboards to. This is required for each publish session. If the user provides a Grafana URL see how to get the ID in publish.py.

Use the `just` recipes from this directory; they run `gcx` via mise. `GRAFANA_SERVER` is set in `grafana/mise.toml`. How to validate and publish all `*.json` dashboards in the `grafana` folder:
```sh
# Assume GRAFANA_TOKEN is set in the environment already.

# Generate resource files
just generate "..."
# Validate resource files
just validate "..."
# Publish resource files
just push "..."
```

## Rules & Guidelines

* NEVER persist `GRAFANA_TOKEN` in files, shell profiles, logs, commits, or other durable storage; provide it only for the current publish session.
* NEVER hardcode the Grafana folder UID in committed files. It must be provided as the recipe argument for each session.
* When publishing, all top-level `*.json` dashboards under the `grafana` folder are pushed at once
  * The published dashboard can be found in https://pytorchci.grafana.net/d/ci-infra-<folder uid>-<file basename>/<kebab case dashboard title>
  * The folder can be found in https://pytorchci.grafana.net/dashboards/f/<folder uid>
* Use gcx (`mise exec -- gcx`) to interact with Grafana
  * NEVER make edits to any folders other than the folder UID provided to the recipe

## Orchestration

* `mise.toml` manages tool versions only (`gcx`, `just`). No tasks.
* `justfile` defines the recipes (`generate`, `validate`, `push-dry-run`, `push`) and their dependency chain. `validate` depends on `generate`; `push` and `push-dry-run` depend on `validate`. Running `just push <folder>` runs the full chain.
* CI (`.github/workflows/grafana-publish.yml`) installs mise via `jdx/mise-action`, which auto-installs `gcx` and `just`, then runs `just push <folder>`.

## Datasources

* grafana-clickhouse-datasource
  * Contains GitHub data
* grafanacloud-pytorchci-prom, grafanacloud-prom
  * Contains GitHub Actions Runner Controller data
  * Clone https://github.com/actions/actions-runner-controller to a temporary directory to understand the ARC provided data
