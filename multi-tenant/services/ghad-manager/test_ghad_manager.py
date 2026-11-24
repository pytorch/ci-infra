"""Tests for ghad-manager.py. The cache tests are Linux only (O_PATH, /proc/self/fd).

Run from multi-tenant/ with `just test`; run as root to also cover the
root-owned-file check.
"""

import importlib.util
import os
import stat
import threading
from pathlib import Path

import pytest
from github import GithubException

_spec = importlib.util.spec_from_file_location(
    "ghad_manager", Path(__file__).with_name("ghad-manager.py")
)
ghad_manager = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ghad_manager)

TENANT_UID = 1000  # owner of the files jobs write, when the tests run as root
IS_ROOT = os.getuid() == 0


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def make(path: Path, perm: int, is_dir: bool = False) -> Path:
    if is_dir:
        path.mkdir()
    else:
        path.write_text("x")
    os.chmod(path, perm)
    if IS_ROOT:
        os.lchown(path, TENANT_UID, TENANT_UID)
    return path


@pytest.fixture
def tree(tmp_path: Path):
    cache = make(tmp_path / "cache", 0o755, is_dir=True)
    outside = make(tmp_path / "outside", 0o700, is_dir=True)
    secret = make(outside / "secret", 0o600)
    return cache, outside, secret


def test_opens_dirs_and_files(tree) -> None:
    cache, _, _ = tree
    sub = make(cache / "sub", 0o700, is_dir=True)
    f = make(sub / "model.bin", 0o600)
    ghad_manager.open_cache_permissions(str(cache))
    assert (mode(cache), mode(sub), mode(f)) == (0o777, 0o777, 0o777)


def test_does_not_follow_symlinks(tree) -> None:
    cache, outside, secret = tree
    os.symlink(secret, cache / "file-link")
    os.symlink(outside, cache / "dir-link")
    ghad_manager.open_cache_permissions(str(cache))
    assert (mode(outside), mode(secret)) == (0o700, 0o600)


@pytest.mark.skipif(not IS_ROOT, reason="needs root to create a root-owned file")
def test_skips_root_owned_files(tree) -> None:
    cache, _, _ = tree
    f = cache / "hardlink"
    f.write_text("x")
    os.chmod(f, 0o600)  # owned by root, as a hard link to a host file would be
    ghad_manager.open_cache_permissions(str(cache))
    assert mode(f) == 0o600


def test_missing_cache_dir_is_skipped(tmp_path: Path) -> None:
    # Before the playbook creates it, or on a dead mount: skip, don't crash
    ghad_manager.open_cache_permissions(str(tmp_path / "missing"))


def test_swapping_a_dir_for_a_symlink_mid_sweep(tree) -> None:
    """A job keeps swapping cache/race between a directory and a symlink to
    outside/ while the sweep runs; nothing outside may change."""
    cache, outside, secret = tree
    for i in range(50):  # widen the window: lots of entries to walk
        make(cache / f"f{i}", 0o600)
    race = make(cache / "race", 0o700, is_dir=True)
    make(race / "secret", 0o600)
    aside = cache / "race.dir"
    stop = threading.Event()

    def swap() -> None:
        # race/secret is the cache's file one moment, outside/secret the next
        while not stop.is_set():
            os.rename(race, aside)
            os.symlink(outside, race)
            os.unlink(race)
            os.rename(aside, race)

    swapper = threading.Thread(target=swap)
    swapper.start()
    try:
        for _ in range(2000):
            ghad_manager.open_cache_permissions(str(cache))
    finally:
        stop.set()
        swapper.join()
    assert (mode(outside), mode(secret)) == (0o700, 0o600)


class FakeRequester:
    """Stands in for PyGithub's requester; `responses` maps (verb, url) to a
    list of results, each a dict to return or an exception to raise."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def requestJsonAndCheck(self, verb, url, parameters=None, input=None):
        self.calls.append((verb, url, parameters, input))
        result = self.responses[(verb, url)].pop(0)
        if isinstance(result, Exception):
            raise result
        return {}, result


@pytest.fixture
def requester(monkeypatch):
    fake = FakeRequester({})

    class Org:
        login = "pytorch"
        _requester = fake

    class Client:
        def get_organization(self, name):
            assert name == "pytorch"
            return Org()

    monkeypatch.setattr(ghad_manager, "get_github_app_client", Client)
    return fake


RUNNERS = "/orgs/pytorch/actions/runners"
JIT = f"{RUNNERS}/generate-jitconfig"
BODY = {
    "name": "host-1001",
    "runner_group_id": 1,
    "labels": ["self-hosted", "Linux", "X64", "linux.dgx.b200"],
}


def test_jit_config(requester) -> None:
    requester.responses = {("POST", JIT): [{"encoded_jit_config": "cfg"}]}
    assert ghad_manager.get_gh_runner_jit_config("host-1001", "linux.dgx.b200") == "cfg"
    assert requester.calls == [("POST", JIT, None, BODY)]


def test_jit_config_replaces_a_leftover_runner(requester) -> None:
    requester.responses = {
        ("POST", JIT): [
            GithubException(409, {"message": "Already exists"}),
            {"encoded_jit_config": "cfg"},
        ],
        ("GET", RUNNERS): [
            {
                "runners": [
                    {"id": 5, "name": "host-10010"},
                    {"id": 6, "name": "host-1001"},
                ]
            }
        ],
        ("DELETE", f"{RUNNERS}/6"): [{}],
    }
    assert ghad_manager.get_gh_runner_jit_config("host-1001", "linux.dgx.b200") == "cfg"
    assert [c[:2] for c in requester.calls] == [
        ("POST", JIT),
        ("GET", RUNNERS),
        ("DELETE", f"{RUNNERS}/6"),
        ("POST", JIT),
    ]


def test_jit_config_raises_other_errors(requester) -> None:
    requester.responses = {("POST", JIT): [GithubException(403, {"message": "no"})]}
    with pytest.raises(GithubException):
        ghad_manager.get_gh_runner_jit_config("host-1001", "linux.dgx.b200")


def test_jit_config_error_skips_the_user(monkeypatch) -> None:
    """A runner GitHub won't replace yet, e.g. one still marked busy after its
    container died mid-job, must not stop ghad-manager for every user."""
    for name in ["stop_all_containers", "prune_images", "prune_networks"]:
        monkeypatch.setattr(ghad_manager, name, lambda *args: None)
    monkeypatch.setattr(ghad_manager, "login_to_ecr", lambda *args: None)
    monkeypatch.setattr(ghad_manager, "login_to_dockerhub", lambda *args: None)
    monkeypatch.setattr(ghad_manager, "is_container_running", lambda *args: False)
    monkeypatch.setattr(ghad_manager.subprocess, "run", lambda *args, **kwargs: None)

    def busy_runner(name, label):
        raise GithubException(422, {"message": "Runner is still running a job"})

    monkeypatch.setattr(ghad_manager, "get_gh_runner_jit_config", busy_runner)

    class Containers:
        def run(self, **kwargs):
            raise AssertionError("must not start a container without a JIT config")

    class Client:
        containers = Containers()

    ghad_manager.start_container_if_not_running(
        "linux.dgx.b200", 1008, Client(), 999, "c", "henry", "latest"
    )
