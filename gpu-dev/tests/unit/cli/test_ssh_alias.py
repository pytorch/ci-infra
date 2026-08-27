"""Unit tests for the reservation-id-based SSH host alias.

The SSH host alias (the `Host` line in ~/.gpu-dev/<id>-sshconfig) and every
user-facing ssh/vscode/cursor display string must key off the RESERVATION ID
(`gpu-dev-<resid8>`), not the k8s pod name. Warm-claimed pods have a pod_name
like `gpu-dev-h100-1e3f9c` (!= gpu-dev-<resid>), so using pod_name as the alias
would make `ssh gpu-dev-<resid>` / `gpu-dev connect <resid>` fail for them.

Routing is unaffected: the ProxyCommand routes on the FQDN (HostName), so the
Host alias is a purely local label (see ssh_proxy.py / _generate_ssh_config).
"""
import re
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from gpu_dev_cli.reservations import create_ssh_config_for_reservation

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean(output: str) -> str:
    return _ANSI_RE.sub("", output)


def _write_config(tmp_path, pod_name, reservation_id):
    """Create the SSH config under a tmp HOME and return its parsed lines."""
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("gpu_dev_cli.reservations._ensure_ssh_config_includes_devgpu", return_value=True):
        config_path, use_include = create_ssh_config_for_reservation(
            "magic_hawk.devservers.io", pod_name, reservation_id, None)
    assert config_path is not None
    return Path(config_path).read_text()


def _host_line(text):
    return next(l.strip() for l in text.splitlines() if l.strip().startswith("Host "))


def _hostname_line(text):
    return next(l.strip() for l in text.splitlines() if l.strip().startswith("HostName "))


class TestSshConfigAlias:
    def test_warm_pod_name_does_not_leak_into_alias(self, tmp_path):
        """Warm-claimed pod: pod_name != gpu-dev-<resid8>; alias keys off resid."""
        rid = "404e4039-44a5-4562-9d4a-deadbeefcafe"
        text = _write_config(tmp_path, "gpu-dev-h100-1e3f9c", rid)
        assert _host_line(text) == "Host gpu-dev-404e4039"
        assert _hostname_line(text) == "HostName magic_hawk.devservers.io"
        # the raw warm pod name must never appear as the alias
        assert "gpu-dev-h100-1e3f9c" not in text

    def test_cold_pod_name_still_yields_resid_alias(self, tmp_path):
        """Cold pod (pod_name == gpu-dev-<resid8>): no regression."""
        rid = "abcd1234-0000-0000-0000-000000000000"
        text = _write_config(tmp_path, "gpu-dev-abcd1234", rid)
        assert _host_line(text) == "Host gpu-dev-abcd1234"
        assert _hostname_line(text) == "HostName magic_hawk.devservers.io"

    def test_config_filename_uses_short_id(self, tmp_path):
        rid = "404e4039-44a5-4562-9d4a-deadbeefcafe"
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("gpu_dev_cli.reservations._ensure_ssh_config_includes_devgpu", return_value=True):
            config_path, _ = create_ssh_config_for_reservation(
                "magic_hawk.devservers.io", "gpu-dev-h100-1e3f9c", rid, None)
        assert config_path.endswith("404e4039-sshconfig")


class TestDisplayUsesResid:
    def _rec_console(self):
        return Console(width=400, force_terminal=False, record=True)

    def test_show_single_reservation_warm_pod(self, tmp_path):
        """_show_single_reservation for a warm-claimed reservation displays the
        resid-based ssh alias, NOT the k8s pod name (but still shows Pod Name)."""
        from gpu_dev_cli import cli as cli_mod
        rid = "404e4039-44a5-4562-9d4a-deadbeefcafe"
        conn = {
            "reservation_id": rid,
            "status": "active",
            "gpu_count": 1,
            "gpu_type": "h100",
            "instance_type": "p5.48xlarge",
            "pod_name": "gpu-dev-h100-1e3f9c",
            "fqdn": "magic_hawk.devservers.io",
            "ssh_command": "ssh dev@magic_hawk.devservers.io",
            "launched_at": "2026-06-01T00:00:00+00:00",
            "expires_at": "2026-06-02T00:00:00+00:00",
        }
        # Pre-create the per-reservation SSH config so the alias display branch is hit.
        gpu_dev_dir = tmp_path / ".gpu-dev"
        gpu_dev_dir.mkdir()
        (gpu_dev_dir / "404e4039-sshconfig").write_text("Host gpu-dev-404e4039\n")

        console = self._rec_console()
        with patch("gpu_dev_cli.cli.console", console), \
             patch("pathlib.Path.home", return_value=tmp_path), \
             patch("gpu_dev_cli.cli.is_ssh_include_enabled", return_value=True):
            cli_mod._show_single_reservation(conn)
        out = _clean(console.export_text())
        assert "ssh gpu-dev-404e4039" in out
        assert "ssh-remote+gpu-dev-404e4039" in out
        # pod name must NOT be used as the ssh alias
        assert "ssh gpu-dev-h100-1e3f9c" not in out
        # but the informational Pod Name line is still shown
        assert "gpu-dev-h100-1e3f9c" in out

    def test_show_direct_success_warm_claim(self, tmp_path):
        """_show_direct_success (the warm-pool instant-claim block) shows the
        resid-based ssh/vscode/cursor commands, not the pod name."""
        from gpu_dev_cli import cli as cli_mod
        rid = "404e4039-44a5-4562-9d4a-deadbeefcafe"
        res = {
            "reservation_id": rid,
            "pod_name": "gpu-dev-h100-1e3f9c",
            "fqdn": "magic_hawk.devservers.io",
            "ssh_command": "ssh dev@magic_hawk.devservers.io",
            "expires_at": "2026-06-02T00:00:00+00:00",
        }
        console = self._rec_console()
        with patch("gpu_dev_cli.cli.rprint", lambda *a, **k: console.print(*a, **k)), \
             patch("pathlib.Path.home", return_value=tmp_path), \
             patch("gpu_dev_cli.reservations._ensure_ssh_config_includes_devgpu", return_value=True):
            cli_mod._show_direct_success(res, 0.5)
        out = _clean(console.export_text())
        assert "ssh gpu-dev-404e4039" in out
        assert "ssh-remote+gpu-dev-404e4039" in out
        assert "gpu-dev-h100-1e3f9c" not in out
