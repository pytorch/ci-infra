"""Tests for ghad-manager.py. Linux only (O_PATH, /proc/self/fd).

Run as root to also cover the root-owned-file check:
    pip install -r requirements.txt pytest && python -m pytest test_ghad_manager.py
"""

import importlib.util
import os
import stat
import threading
from pathlib import Path

import pytest

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
