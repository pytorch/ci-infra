# How-to dev

If you need to firefight or develop / test the playbooks against a single B200
host, log in as the `pytorch` user and target just that host with the `limit`
argument on the relevant `just` recipe:

```
ssh pytorch@<host-ip>
```

and then, from your machine, run the playbook against that single host (each
recipe prompts for the sudo password with `-K`):

```
just restart-services dgxb200-03
```

For an ad-hoc playbook not wired to a recipe, invoke ansible through uv so it
uses the project environment:

```
uv run ansible-playbook -i inventory/manual_inventory --limit dgxb200-03 -u pytorch -K playbooks/restart-services.yml -vv
```

please remember to update `num_users` and `instance_label` according to the
configs in the inventory `multi-tenant/inventory/manual_inventory`.
