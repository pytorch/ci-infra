#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Give every deployed ARC scale set its own GitHub Actions client.

The ARC controller (actions/scaleset v0.3.0) holds one mutex across every
JIT-config and RemoveRunner HTTP call per client, and caches one client per
(GitHub config URL, namespace, credentials), with the raw private-key string as
part of the cache key. All scale sets sharing an org secret therefore serialize
their GitHub calls behind one lock, capping runner creation cluster-wide.

For each scale set this keeps a Secret ``<org-secret>-split-<ars>`` holding the
org GitHub App credential with ``# osdc-split: <ars>`` appended after the PEM END
line (ignored by the PEM parser, but a distinct client cache key), points the
scale set's live EphemeralRunnerSet at it, and repoints the set's existing
EphemeralRunners (including those of a terminating predecessor) still on the
org secret. Split Secrets whose scale set is gone and that nothing references
any more are deleted.

The AutoscalingRunnerSet keeps the org secret on purpose: changing its
githubConfigSecret changes the runner-set spec hash, which recreates the
EphemeralRunnerSet and triggers ARC's serial RemoveRunner cleanup of every old
runner on the single EphemeralRunnerSet worker. A deploy that changes the spec
hash for another reason recreates the EphemeralRunnerSet on the org secret
shortly after ``helm upgrade`` returns, so passes repeat (bounded) until every
scale set has settled on one live, split EphemeralRunnerSet.

Org secrets that are not GitHub App secrets are skipped. Split Secrets copy the
org key: after rotating an org secret, redeploy every arc-runners* module before
revoking the old key.

Usage: split_scaleset_clients.py <generated-runner-configs-dir>
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NAMESPACE = "arc-runners"
SPLIT_LABEL = "osdc.io/arc-client-split"
SCALE_SET_ANNOTATION = "osdc.io/split-scale-set"
SCALE_SET_LABEL = "actions.github.com/scale-set-name"
FIELD_MANAGER = "osdc-arc-client-split"
SECRET_KEYS = ("github_app_id", "github_app_installation_id", "github_app_private_key")
PARALLEL = 16
MAX_PASSES = 5
SETTLE_SECONDS = 20

_RESOURCE_NAME_RE = re.compile(r'^resourceName:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)
_SCALE_SET_NAME_RE = re.compile(r'^runnerScaleSetName:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")

OK, SKIP, FAIL = "ok", "skip", "fail"


def scale_set_name_from_values(values_text: str) -> str | None:
    """AutoscalingRunnerSet name the chart creates from rendered values.

    The chart names the resource ``resourceName | default runnerScaleSetName``
    with underscores replaced by dashes.
    """
    for pattern in (_RESOURCE_NAME_RE, _SCALE_SET_NAME_RE):
        match = pattern.search(values_text)
        if match:
            return match.group(1).replace("_", "-")
    return None


def split_secret_name(org_secret: str, scale_set: str) -> str:
    return f"{org_secret}-split-{scale_set}"


def split_private_key(private_key: str, scale_set: str) -> str:
    """Same PEM key with a per-scale-set comment line after the END marker."""
    stripped = private_key.rstrip("\n")
    return f"{stripped}\n# osdc-split: {scale_set}\n"


def build_split_secret(org_secret: dict, scale_set: str) -> dict:
    """Secret manifest for the scale set's split credential.

    Raises ValueError when the org secret is not a GitHub App secret.
    """
    data = org_secret.get("data") or {}
    missing = [k for k in SECRET_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"secret {org_secret['metadata']['name']} lacks {', '.join(missing)}")
    key = base64.b64decode(data["github_app_private_key"]).decode()
    if "PRIVATE KEY-----" not in key:
        raise ValueError(f"secret {org_secret['metadata']['name']} github_app_private_key is not PEM")
    org_name = org_secret["metadata"]["name"]
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": split_secret_name(org_name, scale_set),
            "namespace": NAMESPACE,
            "labels": {SPLIT_LABEL: "true"},
            "annotations": {
                "osdc.io/split-source-secret": org_name,
                SCALE_SET_ANNOTATION: scale_set,
            },
        },
        "data": {
            "github_app_id": data["github_app_id"],
            "github_app_installation_id": data["github_app_installation_id"],
            "github_app_private_key": base64.b64encode(split_private_key(key, scale_set).encode()).decode(),
        },
    }


def secret_needs_apply(existing: dict | None, manifest: dict) -> bool:
    return existing is None or existing.get("data") != manifest["data"]


def redact(stderr: str) -> str:
    """First line of kubectl stderr with long tokens masked.

    A failed client-side apply echoes the patch, which for a Secret carries the
    base64 private key; only the first line is kept and anything key-shaped is
    masked before it reaches deploy logs.
    """
    lines = stderr.strip().splitlines()
    return _LONG_TOKEN_RE.sub("<redacted>", lines[0]) if lines else ""


def secret_patch(path: str, old: str, new: str) -> str:
    """JSON patch that only applies while the field still holds ``old``."""
    return json.dumps(
        [
            {"op": "test", "path": path, "value": old},
            {"op": "replace", "path": path, "value": new},
        ]
    )


def runners_to_migrate(runners: list[dict], ers_name: str, from_secret: str) -> list[str]:
    """Names of the set's runners still on ``from_secret``.

    Terminating runners come first (they only wait for RemoveRunner), then runners
    without a JIT config yet, then the rest.
    """
    selected = []
    for er in runners:
        owners = er["metadata"].get("ownerReferences") or []
        if not any(o.get("kind") == "EphemeralRunnerSet" and o.get("name") == ers_name for o in owners):
            continue
        if er.get("spec", {}).get("githubConfigSecret") != from_secret:
            continue
        if er["metadata"].get("deletionTimestamp"):
            rank = 0
        elif not (er.get("status") or {}).get("runnerId"):
            rank = 1
        else:
            rank = 2
        selected.append((rank, er["metadata"]["name"]))
    return [name for _, name in sorted(selected)]


def is_benign_patch_error(stderr: str) -> bool:
    """Object deleted meanwhile, or the test op saw it already moved."""
    return "NotFound" in stderr or "testing value" in stderr


def plan_scale_set(scale_set: str, ars_by_name: dict, ers_items: list[dict]) -> tuple[dict | None, str, bool]:
    """Return (plan, message, transitional).

    plan is None when the scale set is skipped; transitional marks skips that a
    later pass may resolve (no live EphemeralRunnerSet yet, or more than one).
    Terminating EphemeralRunnerSets are not planned against, but their runners
    are repointed so their RemoveRunner cleanup leaves the shared client too.
    """
    ars = ars_by_name.get(scale_set)
    if ars is None:
        return None, f"skip {scale_set}: no AutoscalingRunnerSet", False
    org_secret = ars["spec"]["githubConfigSecret"]
    sets = [e for e in ers_items if e["metadata"].get("labels", {}).get(SCALE_SET_LABEL) == scale_set]
    live = [e for e in sets if not e["metadata"].get("deletionTimestamp")]
    dying = [e["metadata"]["name"] for e in sets if e["metadata"].get("deletionTimestamp")]
    if len(live) != 1:
        return None, f"skip {scale_set}: {len(live)} live EphemeralRunnerSets", True
    ers = live[0]
    split = split_secret_name(org_secret, scale_set)
    current = ers["spec"]["ephemeralRunnerSpec"]["githubConfigSecret"]
    if current not in (org_secret, split):
        return None, f"skip {scale_set}: EphemeralRunnerSet uses unexpected secret {current}", False
    return (
        {
            "scale_set": scale_set,
            "org_secret": org_secret,
            "split_secret": split,
            "ers": ers["metadata"]["name"],
            "ers_secret": current,
            "dying_ers": dying,
        },
        "",
        False,
    )


def orphan_split_secrets(split_secrets: dict, ars_names: set[str], referenced: set[str]) -> list[str]:
    """Split Secrets whose scale set is gone and that nothing references."""
    return sorted(
        name
        for name, secret in split_secrets.items()
        if (secret["metadata"].get("annotations") or {}).get(SCALE_SET_ANNOTATION) not in ars_names
        and name not in referenced
    )


def referenced_secrets(ers_items: list[dict], runners: list[dict]) -> set[str]:
    refs = {e["spec"]["ephemeralRunnerSpec"].get("githubConfigSecret") for e in ers_items}
    refs |= {r.get("spec", {}).get("githubConfigSecret") for r in runners}
    return refs


def kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], input=stdin, capture_output=True, text=True)


def kubectl_json(*args: str) -> dict:
    result = kubectl(*args, "-o", "json")
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {redact(result.stderr)}")
    return json.loads(result.stdout)


def converge_scale_set(plan: dict, org_secrets: dict, split_secrets: dict) -> tuple[str, bool]:
    """Ensure the split Secret and repoint the live EphemeralRunnerSet.

    Returns (status, ers_patched).
    """
    name = plan["scale_set"]
    org = org_secrets.get(plan["org_secret"])
    if org is None:
        print(f"  ⚠ skip {name}: org secret {plan['org_secret']} unreadable")
        return SKIP, False
    try:
        manifest = build_split_secret(org, name)
    except ValueError as e:
        print(f"  ⚠ skip {name}: {e}")
        return SKIP, False
    if secret_needs_apply(split_secrets.get(plan["split_secret"]), manifest):
        apply = ("apply", "--server-side", "--force-conflicts", f"--field-manager={FIELD_MANAGER}", "-f", "-")
        result = kubectl(*apply, stdin=json.dumps(manifest))
        if result.returncode != 0:
            print(f"  ✗ {name}: secret apply failed (exit {result.returncode}): {redact(result.stderr)}")
            return FAIL, False
    if plan["ers_secret"] == plan["split_secret"]:
        return OK, False
    patch = secret_patch("/spec/ephemeralRunnerSpec/githubConfigSecret", plan["ers_secret"], plan["split_secret"])
    result = kubectl("patch", "ephemeralrunnerset", plan["ers"], "--type=json", "-p", patch)
    if result.returncode == 0:
        return OK, True
    if is_benign_patch_error(result.stderr):
        print(f"  ⚠ {name}: EphemeralRunnerSet changed meanwhile; retrying next pass")
        return SKIP, True
    print(f"  ✗ {name}: EphemeralRunnerSet patch failed: {redact(result.stderr)}")
    return FAIL, False


def repoint_runner(task: tuple[str, str, str]) -> bool:
    scale_set, er_name, patch = task
    result = kubectl("patch", "ephemeralrunner", er_name, "--type=json", "-p", patch)
    if result.returncode != 0 and not is_benign_patch_error(result.stderr):
        print(f"  ✗ {scale_set}: runner {er_name}: {redact(result.stderr)}")
        return False
    return True


def runner_tasks(plans: list[dict], runners: list[dict]) -> list[tuple[str, str, str]]:
    """Runner patches for the live and terminating sets of converged scale sets."""
    tasks = []
    for plan in plans:
        patch = secret_patch("/spec/githubConfigSecret", plan["org_secret"], plan["split_secret"])
        for ers in [plan["ers"], *plan["dying_ers"]]:
            tasks.extend((plan["scale_set"], n, patch) for n in runners_to_migrate(runners, ers, plan["org_secret"]))
    return tasks


def run_pass(scale_sets: list[str], pool: ThreadPoolExecutor) -> tuple[int, bool, dict]:
    """One converge pass; returns (failures, settle_again, cluster_state)."""
    ars_by_name = {a["metadata"]["name"]: a for a in kubectl_json("get", "autoscalingrunnersets")["items"]}
    ers_items = kubectl_json("get", "ephemeralrunnersets")["items"]
    runners = kubectl_json("get", "ephemeralrunners")["items"]
    split_secrets = {
        s["metadata"]["name"]: s for s in kubectl_json("get", "secrets", "-l", f"{SPLIT_LABEL}=true")["items"]
    }

    plans, transitional = [], False
    for scale_set in scale_sets:
        plan, message, pending = plan_scale_set(scale_set, ars_by_name, ers_items)
        if plan is None:
            print(f"  ⚠ {message}")
            transitional |= pending
        else:
            plans.append(plan)
    org_secrets = {}
    for org in {p["org_secret"] for p in plans}:
        try:
            org_secrets[org] = kubectl_json("get", "secret", org)
        except RuntimeError as e:
            print(f"  ⚠ {e}")

    results = list(pool.map(lambda p: converge_scale_set(p, org_secrets, split_secrets), plans))
    done = [p for p, (status, _) in zip(plans, results, strict=True) if status == OK]
    tasks = runner_tasks(done, runners)
    repointed = list(pool.map(repoint_runner, tasks))
    failures = sum(status == FAIL for status, _ in results) + repointed.count(False)
    patched = any(ers_patched for _, ers_patched in results)
    print(f"  {len(done)}/{len(plans)} scale sets split, {repointed.count(True)}/{len(tasks)} runners repointed")
    state = {"ars": set(ars_by_name), "ers": ers_items, "runners": runners, "split_secrets": split_secrets}
    return failures, transitional or patched, state


def delete_orphans(state: dict) -> int:
    orphans = orphan_split_secrets(
        state["split_secrets"], state["ars"], referenced_secrets(state["ers"], state["runners"])
    )
    failures = 0
    for name in orphans:
        result = kubectl("delete", "secret", name, "--ignore-not-found")
        if result.returncode != 0:
            print(f"  ✗ orphan split secret {name}: {redact(result.stderr)}")
            failures += 1
        else:
            print(f"  🗑 deleted orphan split secret {name}")
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    scale_sets = []
    for path in sorted(Path(sys.argv[1]).glob("*.yaml")):
        name = scale_set_name_from_values(path.read_text().split("\n---", 1)[0])
        if name:
            scale_sets.append(name)
    if not scale_sets:
        print("No scale sets found in generated configs; nothing to split.")
        return 0

    print(f"Splitting GitHub clients for {len(scale_sets)} scale set(s)...")
    try:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            for attempt in range(1, MAX_PASSES + 1):
                failures, settle_again, state = run_pass(scale_sets, pool)
                if not settle_again or attempt == MAX_PASSES:
                    break
                print(f"  ⏳ EphemeralRunnerSets still settling; pass {attempt + 1} in {SETTLE_SECONDS}s")
                time.sleep(SETTLE_SECONDS)
        failures += delete_orphans(state)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    if failures:
        print(f"ERROR: {failures} split operation(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
