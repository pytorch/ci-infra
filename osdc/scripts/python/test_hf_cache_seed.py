"""Unit tests for scripts/hf-cache-seed.py."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Filename has a hyphen, so load it via importlib (can't `import`).
_spec = importlib.util.spec_from_file_location(
    "hf_cache_seed", str(Path(__file__).resolve().parent.parent / "hf-cache-seed.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CLUSTERS = {
    "meta-staging-aws-ue1": {"region": "us-east-1", "modules": ["arc", "hf-cache"]},
    "meta-staging-aws-uw1": {"region": "us-west-1", "modules": ["arc", "hf-cache"]},
    "meta-prod-aws-ue1": {"region": "us-east-1", "modules": ["arc", "nodepools"]},
}


def test_bucket_for():
    assert _mod.bucket_for("meta-staging-aws-ue1") == "pytorch-hf-model-cache-meta-staging-aws-ue1"


def test_resolve_targets_all_selects_hf_cache_clusters():
    assert set(_mod.resolve_targets(CLUSTERS, None, True)) == {"meta-staging-aws-ue1", "meta-staging-aws-uw1"}


def test_resolve_targets_all_none_enabled_exits():
    with pytest.raises(SystemExit):
        _mod.resolve_targets({"c": {"region": "r", "modules": []}}, None, True)


def test_resolve_targets_explicit():
    assert _mod.resolve_targets(CLUSTERS, ["meta-staging-aws-ue1"], False) == ["meta-staging-aws-ue1"]


def test_resolve_targets_unknown_exits():
    with pytest.raises(SystemExit):
        _mod.resolve_targets(CLUSTERS, ["nope"], False)


def test_resolve_targets_warns_when_module_disabled(capsys):
    assert _mod.resolve_targets(CLUSTERS, ["meta-prod-aws-ue1"], False) == ["meta-prod-aws-ue1"]
    assert "does not enable" in capsys.readouterr().err


def test_load_clusters(tmp_path, monkeypatch):
    f = tmp_path / "clusters.yaml"
    f.write_text("clusters:\n  c1:\n    region: us-east-1\n")
    monkeypatch.setattr(_mod, "CLUSTERS_YAML", f)
    assert _mod.load_clusters() == {"c1": {"region": "us-east-1"}}


def test_download_models(monkeypatch, tmp_path):
    fake = types.ModuleType("huggingface_hub")
    fake.snapshot_download = lambda model, cache_dir: f"{cache_dir}/{model}"
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    _mod.download_models(["org/m"], tmp_path)
    assert (tmp_path / "hub").is_dir()


def test_sync_to_cluster_ok():
    with patch.object(_mod.subprocess, "run", return_value=MagicMock(returncode=0, stdout="done", stderr="")):
        cid, ok, _ = _mod.sync_to_cluster("c1", "us-east-1", Path("/tmp/x"))
    assert ok
    assert cid == "c1"


def test_sync_to_cluster_fail():
    with patch.object(_mod.subprocess, "run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
        _, ok, detail = _mod.sync_to_cluster("c1", "us-east-1", Path("/tmp/x"))
    assert not ok
    assert "boom" in detail


def _run_main(argv, sync=lambda cid, region, staging: (cid, True, "ok")):
    with (
        patch.object(_mod, "load_clusters", return_value=CLUSTERS),
        patch.object(_mod, "download_models"),
        patch.object(_mod.shutil, "which", return_value="/usr/bin/aws"),
        patch.object(_mod, "sync_to_cluster", side_effect=sync),
    ):
        return _mod.main(argv)


def test_main_success(capsys):
    assert _run_main(["-c", "meta-staging-aws-ue1", "org/m"]) == 0
    assert "Seeded" in capsys.readouterr().out


def test_main_all_success():
    assert _run_main(["--all", "org/m"]) == 0


def test_main_reports_failure(capsys):
    rc = _run_main(["-c", "meta-staging-aws-ue1", "org/m"], sync=lambda cid, region, staging: (cid, False, "e1\ne2"))
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_requires_aws():
    with (
        patch.object(_mod, "load_clusters", return_value=CLUSTERS),
        patch.object(_mod.shutil, "which", return_value=None),
        pytest.raises(SystemExit),
    ):
        _mod.main(["-c", "meta-staging-aws-ue1", "org/m"])
