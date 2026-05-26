# Grafana Dashboard Resources

Required information from the user:
  * Environment variable `GRAFANA_TOKEN`. A private API token to access Grafana. NEVER print the value of environment variable. Only confirm if it's set.
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

* NEVER persist `GRAFANA_TOKEN` in files, shell profiles, logs, commits, or other durable storage; provide it only for the current publish session.
* NEVER hardcode the Grafana folder UID in committed files. It must be provided with `--folder` for each session.
* When publishing, all top-level `*.json` dashboards under the `grafana` folder are pushed at once
  * The published dashboard can be found in https://pytorchci.grafana.net/d/ci-infra-<folder uid>-<file basename>/<kebab case dashboard title>
  * The folder can be found in https://pytorchci.grafana.net/dashboards/f/<folder uid>
* Use gcx (`mise exec -- gcx`) to interact with Grafana
  * NEVER make edits to any folders other than the folder UID provided with `--folder`
