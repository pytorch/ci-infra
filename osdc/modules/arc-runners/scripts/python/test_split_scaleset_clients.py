"""Tests for split_scaleset_clients.py."""

import base64
import json
import subprocess

import pytest
import split_scaleset_clients as ssc

PEM = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def org_secret(name="org", key=PEM):
    return {
        "metadata": {"name": name},
        "data": {
            "github_app_id": b64("123"),
            "github_app_installation_id": b64("456"),
            "github_app_private_key": b64(key),
        },
    }


def ars(name, config_ref="org"):
    return {"metadata": {"name": name}, "spec": {"githubConfigSecret": config_ref}}


def ers(name, scale_set, config_ref="org", deleting=False):
    item = {
        "metadata": {"name": name, "labels": {ssc.SCALE_SET_LABEL: scale_set}},
        "spec": {"ephemeralRunnerSpec": {"githubConfigSecret": config_ref}},
    }
    if deleting:
        item["metadata"]["deletionTimestamp"] = "2026-11-03T00:00:00Z"
    return item


def runner(name, owner, config_ref="org", deleting=False, runner_id=None):
    er = {
        "metadata": {"name": name, "ownerReferences": [{"kind": "EphemeralRunnerSet", "name": owner}]},
        "spec": {"githubConfigSecret": config_ref},
        "status": {},
    }
    if deleting:
        er["metadata"]["deletionTimestamp"] = "2026-11-03T00:00:00Z"
    if runner_id:
        er["status"]["runnerId"] = runner_id
    return er


def split_secret(name, scale_set):
    return {"metadata": {"name": name, "annotations": {ssc.SCALE_SET_ANNOTATION: scale_set}}, "data": {}}


def done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestScaleSetName:
    def test_resource_name_wins(self):
        text = 'githubConfigSecret: "s"\nrunnerScaleSetName: "mt-l-a"\nresourceName: "mp-l-a"\n'
        assert ssc.scale_set_name_from_values(text) == "mp-l-a"

    def test_falls_back_to_scale_set_name(self):
        assert ssc.scale_set_name_from_values('runnerScaleSetName: "mt-l-a"\n\nminRunners: 0\n') == "mt-l-a"

    def test_underscores_become_dashes(self):
        assert ssc.scale_set_name_from_values('runnerScaleSetName: "mt-l_a_b"\n') == "mt-l-a-b"

    def test_none_when_absent(self):
        assert ssc.scale_set_name_from_values("minRunners: 0\n") is None


class TestSplitKey:
    def test_appends_comment_after_end_marker(self):
        assert ssc.split_private_key(PEM + "\n", "mt-x") == PEM + "\n# osdc-split: mt-x\n"

    def test_without_trailing_newline(self):
        assert ssc.split_private_key(PEM, "mt-x") == PEM + "\n# osdc-split: mt-x\n"

    def test_distinct_per_scale_set(self):
        assert ssc.split_private_key(PEM, "a") != ssc.split_private_key(PEM, "b")


class TestBuildSplitSecret:
    def test_manifest(self):
        m = ssc.build_split_secret(org_secret(), "mt-x")
        assert m["metadata"]["name"] == "org-split-mt-x"
        assert m["metadata"]["namespace"] == ssc.NAMESPACE
        assert m["metadata"]["labels"] == {ssc.SPLIT_LABEL: "true"}
        assert m["metadata"]["annotations"][ssc.SCALE_SET_ANNOTATION] == "mt-x"
        assert m["data"]["github_app_id"] == b64("123")
        assert m["data"]["github_app_installation_id"] == b64("456")
        assert base64.b64decode(m["data"]["github_app_private_key"]).decode() == PEM + "\n# osdc-split: mt-x\n"

    def test_missing_key_rejected(self):
        secret = org_secret()
        del secret["data"]["github_app_installation_id"]
        with pytest.raises(ValueError, match="github_app_installation_id"):
            ssc.build_split_secret(secret, "mt-x")

    def test_no_data_rejected(self):
        with pytest.raises(ValueError, match="lacks"):
            ssc.build_split_secret({"metadata": {"name": "org"}}, "mt-x")

    def test_non_pem_rejected(self):
        with pytest.raises(ValueError, match="not PEM"):
            ssc.build_split_secret(org_secret(key="ghp_token"), "mt-x")


class TestSecretNeedsApply:
    def test_missing(self):
        assert ssc.secret_needs_apply(None, {"data": {"a": "1"}}) is True

    def test_identical(self):
        assert ssc.secret_needs_apply({"data": {"a": "1"}}, {"data": {"a": "1"}}) is False

    def test_rotated(self):
        assert ssc.secret_needs_apply({"data": {"a": "1"}}, {"data": {"a": "2"}}) is True


class TestRedact:
    def test_first_line_only_and_masks_tokens(self):
        key_b64 = b64(PEM * 3)
        stderr = f'error when applying patch: {key_b64}\n{{"data":{{"github_app_private_key":"{key_b64}"}}}}\n'
        out = ssc.redact(stderr)
        assert key_b64 not in out
        assert "<redacted>" in out
        assert "\n" not in out

    def test_keeps_short_messages(self):
        assert ssc.redact("Error from server (Forbidden): nope\nmore") == "Error from server (Forbidden): nope"

    def test_empty(self):
        assert ssc.redact("") == ""


class TestSecretPatch:
    def test_test_then_replace(self):
        ops = json.loads(ssc.secret_patch("/spec/x", "old", "new"))
        assert ops == [
            {"op": "test", "path": "/spec/x", "value": "old"},
            {"op": "replace", "path": "/spec/x", "value": "new"},
        ]


class TestRunnersToMigrate:
    def test_filters_and_orders(self):
        runners = [
            runner("running", "ers1", runner_id=7),
            runner("unregistered", "ers1"),
            runner("deleting", "ers1", deleting=True, runner_id=8),
            runner("already-split", "ers1", config_ref="org-split-a"),
            runner("other-set", "ers2"),
            {"metadata": {"name": "orphan"}, "spec": {"githubConfigSecret": "org"}},
        ]
        assert ssc.runners_to_migrate(runners, "ers1", "org") == ["deleting", "unregistered", "running"]

    def test_empty(self):
        assert ssc.runners_to_migrate([], "ers1", "org") == []


class TestBenignErrors:
    @pytest.mark.parametrize(
        ("stderr", "benign"),
        [
            ('Error from server (NotFound): ephemeralrunners "x" not found', True),
            ("The request is invalid: testing value /spec/githubConfigSecret failed: test failed", True),
            ("Error from server (Conflict): try again", False),
            ("Error from server (Forbidden): nope", False),
        ],
    )
    def test_classification(self, stderr, benign):
        assert ssc.is_benign_patch_error(stderr) is benign


class TestPlanScaleSet:
    def test_plan(self):
        plan, msg, pending = ssc.plan_scale_set("a", {"a": ars("a")}, [ers("ers-a", "a")])
        assert (msg, pending) == ("", False)
        assert plan == {
            "scale_set": "a",
            "org_secret": "org",
            "split_secret": "org-split-a",
            "ers": "ers-a",
            "ers_secret": plan["org_secret"],
            "dying_ers": [],
        }

    def test_already_split(self):
        plan, _, _ = ssc.plan_scale_set("a", {"a": ars("a")}, [ers("ers-a", "a", config_ref="org-split-a")])
        assert plan["ers_secret"] == plan["split_secret"]

    def test_recreated_set_plans_live_one(self):
        sets = [ers("ers-old", "a", config_ref="org-split-a", deleting=True), ers("ers-new", "a")]
        plan, _, pending = ssc.plan_scale_set("a", {"a": ars("a")}, sets)
        assert pending is False
        assert plan["ers"] == "ers-new"
        assert plan["ers_secret"] == plan["org_secret"]
        assert plan["dying_ers"] == ["ers-old"]

    def test_missing_ars(self):
        plan, msg, pending = ssc.plan_scale_set("a", {}, [])
        assert plan is None
        assert pending is False
        assert "no AutoscalingRunnerSet" in msg

    @pytest.mark.parametrize(
        "sets",
        [[], [ers("e1", "a", deleting=True)], [ers("e1", "a"), ers("e2", "a")]],
    )
    def test_not_exactly_one_live_set_is_transitional(self, sets):
        plan, msg, pending = ssc.plan_scale_set("a", {"a": ars("a")}, sets)
        assert plan is None
        assert pending is True
        assert "live EphemeralRunnerSets" in msg

    def test_unexpected_secret(self):
        plan, msg, pending = ssc.plan_scale_set("a", {"a": ars("a")}, [ers("ers-a", "a", config_ref="other")])
        assert plan is None
        assert pending is False
        assert "unexpected secret other" in msg


class TestOrphans:
    def test_only_unreferenced_secrets_of_gone_scale_sets(self):
        secrets = {
            "org-split-live": split_secret("org-split-live", "live"),
            "org-split-gone": split_secret("org-split-gone", "gone"),
            "org-split-gone-busy": split_secret("org-split-gone-busy", "gone-busy"),
            "org-split-unannotated": {"metadata": {"name": "org-split-unannotated"}},
        }
        assert ssc.orphan_split_secrets(secrets, {"live"}, {"org-split-gone-busy"}) == [
            "org-split-gone",
            "org-split-unannotated",
        ]

    def test_referenced_secrets(self):
        refs = ssc.referenced_secrets([ers("e", "a", config_ref="s1")], [runner("r", "e", config_ref="s2")])
        assert {"s1", "s2"} <= refs


@pytest.fixture
def calls(monkeypatch):
    """Record kubectl invocations; responses keyed by argument prefix."""
    recorded = []
    responses = {}

    def fake_run(cmd, input=None, capture_output=None, text=None):
        recorded.append((cmd, input))
        args = cmd[3:]
        for prefix, response in responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                return response(args) if callable(response) else response
        return done()

    monkeypatch.setattr(ssc.subprocess, "run", fake_run)
    return recorded, responses


class TestKubectl:
    def test_namespace_and_stdin(self, calls):
        recorded, _ = calls
        ssc.kubectl("apply", "-f", "-", stdin="{}")
        assert recorded == [(["kubectl", "-n", ssc.NAMESPACE, "apply", "-f", "-"], "{}")]

    def test_json(self, calls):
        _, responses = calls
        responses[("get", "x")] = done(stdout='{"items": []}')
        assert ssc.kubectl_json("get", "x") == {"items": []}

    def test_json_error_is_redacted(self, calls):
        _, responses = calls
        responses[("get", "x")] = done(1, stderr="boom " + "A" * 60 + "\nsecond line")
        with pytest.raises(RuntimeError, match="boom <redacted>") as err:
            ssc.kubectl_json("get", "x")
        assert "second line" not in str(err.value)


PLAN = {
    "scale_set": "a",
    "org_secret": "org",
    "split_secret": "org-split-a",
    "ers": "ers-a",
    "ers_secret": "org",
    "dying_ers": [],
}


class TestConverge:
    def test_applies_secret_server_side_and_patches_ers(self, calls):
        recorded, _ = calls
        assert ssc.converge_scale_set(PLAN, {"org": org_secret()}, {}) == (ssc.OK, True)
        apply_args = recorded[0][0][3:]
        assert apply_args[:2] == ["apply", "--server-side"]
        assert f"--field-manager={ssc.FIELD_MANAGER}" in apply_args
        assert json.loads(recorded[0][1])["metadata"]["name"] == "org-split-a"
        assert recorded[1][0][3:6] == ["patch", "ephemeralrunnerset", "ers-a"]
        assert b64(PEM) not in " ".join(" ".join(c) for c, _ in recorded)

    def test_identical_secret_not_reapplied(self, calls):
        recorded, _ = calls
        existing = ssc.build_split_secret(org_secret(), "a")
        plan = {**PLAN, "ers_secret": PLAN["split_secret"]}
        assert ssc.converge_scale_set(plan, {"org": org_secret()}, {"org-split-a": existing}) == (ssc.OK, False)
        assert recorded == []

    def test_unreadable_org_secret_skips(self, calls, capsys):
        assert ssc.converge_scale_set(PLAN, {}, {}) == (ssc.SKIP, False)
        assert "unreadable" in capsys.readouterr().out

    def test_non_app_secret_skips(self, calls, capsys):
        recorded, _ = calls
        assert ssc.converge_scale_set(PLAN, {"org": org_secret(key="nope")}, {}) == (ssc.SKIP, False)
        assert recorded == []
        assert "not PEM" in capsys.readouterr().out

    def test_apply_failure_never_prints_key(self, calls, capsys):
        _, responses = calls
        leaked = b64(PEM * 3)
        responses[("apply",)] = done(1, stderr=f"error when applying patch:\n{leaked}\nto: Secret")
        assert ssc.converge_scale_set(PLAN, {"org": org_secret()}, {}) == (ssc.FAIL, False)
        out = capsys.readouterr().out
        assert "secret apply failed (exit 1)" in out
        assert leaked not in out

    def test_ers_changed_meanwhile_retries(self, calls, capsys):
        _, responses = calls
        responses[("patch", "ephemeralrunnerset")] = done(1, stderr="testing value failed")
        assert ssc.converge_scale_set(PLAN, {"org": org_secret()}, {}) == (ssc.SKIP, True)
        assert "retrying next pass" in capsys.readouterr().out

    def test_ers_patch_failure(self, calls, capsys):
        _, responses = calls
        responses[("patch", "ephemeralrunnerset")] = done(1, stderr="Error from server (Forbidden)")
        assert ssc.converge_scale_set(PLAN, {"org": org_secret()}, {}) == (ssc.FAIL, False)
        assert "EphemeralRunnerSet patch failed" in capsys.readouterr().out


class TestRepointRunner:
    def test_success(self, calls):
        assert ssc.repoint_runner(("a", "r1", "[]")) is True

    def test_benign_failure(self, calls):
        _, responses = calls
        responses[("patch",)] = done(1, stderr="Error from server (NotFound)")
        assert ssc.repoint_runner(("a", "r1", "[]")) is True

    def test_real_failure(self, calls, capsys):
        _, responses = calls
        responses[("patch",)] = done(1, stderr="Error from server (Forbidden)")
        assert ssc.repoint_runner(("a", "r1", "[]")) is False
        assert "runner r1" in capsys.readouterr().out


class TestRunnerTasks:
    def test_live_and_dying_sets(self):
        runners = [runner("r1", "ers-a"), runner("r-old", "ers-a-old", deleting=True), runner("r2", "ers-b")]
        plan_a = {**PLAN, "dying_ers": ["ers-a-old"]}
        plan_b = {**PLAN, "scale_set": "b", "split_secret": "org-split-b", "ers": "ers-b"}
        tasks = ssc.runner_tasks([plan_a, plan_b], runners)
        assert [(t[0], t[1]) for t in tasks] == [("a", "r1"), ("a", "r-old"), ("b", "r2")]
        assert json.loads(tasks[2][2])[1]["value"] == "org-split-b"


ORPHAN_STATE = {
    "split_secrets": {"org-split-gone": split_secret("org-split-gone", "gone")},
    "ars": set(),
    "ers": [],
    "runners": [],
}


class TestDeleteOrphans:
    def test_deletes(self, calls, capsys):
        recorded, _ = calls
        assert ssc.delete_orphans(ORPHAN_STATE) == 0
        assert recorded[0][0][3:] == ["delete", "secret", "org-split-gone", "--ignore-not-found"]
        assert "deleted orphan" in capsys.readouterr().out

    def test_delete_failure(self, calls, capsys):
        _, responses = calls
        responses[("delete",)] = done(1, stderr="Forbidden")
        assert ssc.delete_orphans(ORPHAN_STATE) == 1
        assert "orphan split secret org-split-gone" in capsys.readouterr().out


class TestMain:
    def write_values(self, tmp_path, name, body):
        (tmp_path / f"{name}.yaml").write_text(body + "\n---\napiVersion: v1\nkind: ConfigMap\n")

    def setup_cluster(self, responses, runners, ers_items=None, split_secrets=()):
        responses[("get", "autoscalingrunnersets")] = done(stdout=json.dumps({"items": [ars("mt-a"), ars("mt-b")]}))
        ers_items = ers_items or [ers("ers-a", "mt-a"), ers("ers-b", "mt-b", config_ref="org-split-mt-b")]
        responses[("get", "ephemeralrunnersets")] = done(stdout=json.dumps({"items": ers_items}))
        responses[("get", "ephemeralrunners")] = done(stdout=json.dumps({"items": runners}))
        responses[("get", "secrets")] = done(stdout=json.dumps({"items": list(split_secrets)}))
        responses[("get", "secret", "org")] = done(stdout=json.dumps(org_secret()))

    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(ssc.time, "sleep", sleeps.append)
        return sleeps

    def test_usage(self, monkeypatch):
        monkeypatch.setattr(ssc.sys, "argv", ["split_scaleset_clients.py"])
        assert ssc.main() == 2

    def test_no_scale_sets(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 0
        assert "nothing to split" in capsys.readouterr().out

    def test_full_run_settles_then_cleans_orphans(self, tmp_path, monkeypatch, calls, capsys, no_sleep):
        recorded, responses = calls
        self.write_values(tmp_path, "a", 'runnerScaleSetName: "mt-a"')
        self.write_values(tmp_path, "b", 'runnerScaleSetName: "mt-b"')
        self.write_values(tmp_path, "c", 'runnerScaleSetName: "mt-missing"')
        self.write_values(tmp_path, "d", "minRunners: 0")
        self.setup_cluster(
            responses,
            [runner("r1", "ers-a", deleting=True), runner("r2", "ers-b")],
            split_secrets=[split_secret("org-split-gone", "gone")],
        )
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 0
        out = capsys.readouterr().out
        assert "skip mt-missing" in out
        assert "2/2 scale sets split, 2/2 runners repointed" in out
        assert no_sleep == [ssc.SETTLE_SECONDS] * (ssc.MAX_PASSES - 1)
        patched = [c[3:6] for c, _ in recorded if c[3] == "patch"]
        assert ["patch", "ephemeralrunnerset", "ers-a"] in patched
        assert ["patch", "ephemeralrunner", "r1"] in patched
        assert ["patch", "ephemeralrunner", "r2"] in patched
        assert ["delete", "secret", "org-split-gone", "--ignore-not-found"] in [c[3:] for c, _ in recorded]

    def test_settled_cluster_single_pass(self, tmp_path, monkeypatch, calls, no_sleep):
        _, responses = calls
        self.write_values(tmp_path, "b", 'runnerScaleSetName: "mt-b"')
        existing = ssc.build_split_secret(org_secret(), "mt-b")
        self.setup_cluster(responses, [], split_secrets=[existing])
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 0
        assert no_sleep == []

    def test_transitional_skip_retries(self, tmp_path, monkeypatch, calls, no_sleep):
        _, responses = calls
        self.write_values(tmp_path, "a", 'runnerScaleSetName: "mt-a"')
        self.setup_cluster(responses, [], ers_items=[ers("ers-a", "mt-a", deleting=True)])
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 0
        assert len(no_sleep) == ssc.MAX_PASSES - 1

    def test_failures_exit_nonzero(self, tmp_path, monkeypatch, calls, capsys):
        _, responses = calls
        self.write_values(tmp_path, "a", 'runnerScaleSetName: "mt-a"')
        self.setup_cluster(responses, [runner("r1", "ers-a")])
        responses[("patch", "ephemeralrunner", "r1")] = done(1, stderr="Error from server (Forbidden)")
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 1
        assert "split operation(s) failed" in capsys.readouterr().out

    def test_unreadable_org_secret_warns(self, tmp_path, monkeypatch, calls, capsys):
        _, responses = calls
        self.write_values(tmp_path, "a", 'runnerScaleSetName: "mt-a"')
        self.setup_cluster(responses, [])
        responses[("get", "secret", "org")] = done(1, stderr="NotFound")
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 0
        assert "unreadable" in capsys.readouterr().out

    def test_list_failure_is_clean_error(self, tmp_path, monkeypatch, calls, capsys):
        _, responses = calls
        self.write_values(tmp_path, "a", 'runnerScaleSetName: "mt-a"')
        responses[("get", "autoscalingrunnersets")] = done(1, stderr="connection refused")
        monkeypatch.setattr(ssc.sys, "argv", ["x", str(tmp_path)])
        assert ssc.main() == 1
        assert "ERROR: kubectl get autoscalingrunnersets failed: connection refused" in capsys.readouterr().out
