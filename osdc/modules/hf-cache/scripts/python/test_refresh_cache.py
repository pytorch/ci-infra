"""Unit tests for refresh_cache pure helpers (no network, no huggingface_hub)."""

import sys
import types
from pathlib import Path

import refresh_cache
from refresh_cache import build_rclone_args, download_models, main, parse_manifest, publish


def test_parse_manifest_basic():
    text = "meta-llama/Llama-3.1-8B\nopenai/clip-vit-base-patch32\n"
    assert parse_manifest(text) == [
        ("meta-llama/Llama-3.1-8B", None),
        ("openai/clip-vit-base-patch32", None),
    ]


def test_parse_manifest_revision_and_comments():
    text = "# curated models\n\norg/model-a@abc123\n   org/model-b   # trailing comment\n# full-line comment\n"
    assert parse_manifest(text) == [
        ("org/model-a", "abc123"),
        ("org/model-b", None),
    ]


def test_parse_manifest_empty():
    assert parse_manifest("\n#only comments\n   \n") == []


def test_build_rclone_args_copy_default():
    args = build_rclone_args(Path("/work/hub"), "my-bucket", "us-east-2", "hub", prune=False)
    assert args[0] == "rclone"
    assert args[1] == "copy"
    assert "-L" in args
    assert "blobs/**" in args
    assert args[-1] == ":s3,provider=AWS,env_auth=true,region=us-east-2:my-bucket/hub"
    assert args[-2] == "/work/hub"


def test_build_rclone_args_prune_uses_sync():
    args = build_rclone_args(Path("/work/hub"), "b", "us-west-2", "hub", prune=True)
    assert args[1] == "sync"


def test_download_models_calls_snapshot(monkeypatch, tmp_path):
    calls = []
    fake = types.ModuleType("huggingface_hub")
    fake.snapshot_download = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    entries = [("org/a", None), ("org/b", "rev1")]
    download_models(entries, tmp_path / "hub", token="tok")

    assert [c["repo_id"] for c in calls] == ["org/a", "org/b"]
    assert calls[1]["revision"] == "rev1"
    assert calls[0]["token"] == "tok"  # noqa: S105
    assert (tmp_path / "hub").is_dir()


def test_publish_invokes_rclone_copy(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, check):
        seen["args"] = args
        seen["check"] = check

    monkeypatch.setattr(refresh_cache.subprocess, "run", fake_run)
    publish(tmp_path, "b", "us-east-2", "hub", prune=False, dry_run=False)

    assert seen["args"][1] == "copy"
    assert seen["check"] is True
    assert "--dry-run" not in seen["args"]


def test_publish_dry_run_appends_flag(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(refresh_cache.subprocess, "run", lambda args, check: seen.setdefault("args", args))
    publish(tmp_path, "b", "r", "hub", prune=True, dry_run=True)

    assert seen["args"][1] == "sync"
    assert seen["args"][-1] == "--dry-run"


def test_main_empty_manifest_is_noop(tmp_path):
    manifest = tmp_path / "models.txt"
    manifest.write_text("# nothing here\n")
    rc = main(["--models", str(manifest), "--bucket", "b", "--region", "r", "--cache-dir", str(tmp_path / "hub")])
    assert rc == 0


def test_main_happy_path(monkeypatch, tmp_path):
    manifest = tmp_path / "models.txt"
    manifest.write_text("org/a\norg/b@rev\n")
    downloaded = []
    published = []
    monkeypatch.setattr(
        refresh_cache,
        "download_models",
        lambda entries, cache_dir, token: downloaded.append((entries, cache_dir, token)),
    )
    monkeypatch.setattr(refresh_cache, "publish", lambda *a, **k: published.append((a, k)))
    monkeypatch.setenv("HF_TOKEN", "secret")

    rc = main(
        ["--models", str(manifest), "--bucket", "bk", "--region", "us-east-2", "--cache-dir", str(tmp_path / "hub")]
    )

    assert rc == 0
    assert downloaded[0][2] == "secret"
    assert len(downloaded[0][0]) == 2
    assert published
