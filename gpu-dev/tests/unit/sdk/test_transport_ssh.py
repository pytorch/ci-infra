"""Unit tests for gpu_dev._transport.ssh.SshTransport.

Pure command-construction + exec/result-parsing tests. subprocess is fully
mocked so nothing connects anywhere.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev._transport.ssh import SshTransport
from gpu_dev.common.errors import GpuDevConnectionError, GpuDevTimeoutError
from gpu_dev.common.models import ExecResult

MOD = "gpu_dev._transport.ssh"


def _completed(returncode=0, stdout="", stderr=""):
    """A stand-in for subprocess.CompletedProcess returned by subprocess.run."""
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# --------------------------------------------------------------------------- #
# _ssh_base                                                                    #
# --------------------------------------------------------------------------- #
class TestSshBase:
    def test_no_fqdn_uses_pod_name_as_host(self):
        t = SshTransport("mypod")
        assert t._ssh_base() == [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "mypod",
        ]

    def test_no_fqdn_has_no_proxycommand_and_no_dev_user(self):
        base = SshTransport("mypod")._ssh_base()
        assert not any("ProxyCommand" in a for a in base)
        assert not any(a.startswith("dev@") for a in base)

    def test_fqdn_adds_proxycommand_and_dev_user_host(self):
        t = SshTransport("mypod", fqdn="host.example.com")
        base = t._ssh_base()
        assert base == [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ProxyCommand=gpu-dev-ssh-proxy %h %p",
            "dev@host.example.com",
        ]

    def test_fqdn_takes_precedence_over_pod_name_for_host(self):
        # With fqdn set, the dev@<host> entry uses the fqdn, not the pod name.
        base = SshTransport("mypod", fqdn="h.example.com")._ssh_base()
        assert base[-1] == "dev@h.example.com"
        assert "mypod" not in base

    def test_common_options_present_in_both_modes(self):
        for t in (SshTransport("p"), SshTransport("p", fqdn="f")):
            base = t._ssh_base()
            assert base[0] == "ssh"
            assert "StrictHostKeyChecking=no" in base
            assert "UserKnownHostsFile=/dev/null" in base
            assert "LogLevel=ERROR" in base


# --------------------------------------------------------------------------- #
# exec                                                                         #
# --------------------------------------------------------------------------- #
class TestExec:
    def test_command_appended_with_double_dash_separator(self):
        t = SshTransport("mypod")
        with patch(f"{MOD}.subprocess.run", return_value=_completed()) as run:
            t.exec("ls -la")
        argv = run.call_args.args[0]
        # base ... then "--" then the command as one element
        assert argv[:-2] == t._ssh_base()
        assert argv[-2] == "--"
        assert argv[-1] == "ls -la"

    def test_command_is_single_arg_not_split(self):
        # The whole command string is passed as one argv element (no shell split).
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed()) as run:
            t.exec("echo 'a b c' | wc -l")
        argv = run.call_args.args[0]
        assert argv[-1] == "echo 'a b c' | wc -l"

    def test_returns_execresult_with_parsed_fields(self):
        t = SshTransport("p")
        cp = _completed(returncode=3, stdout="out-data", stderr="err-data")
        with patch(f"{MOD}.subprocess.run", return_value=cp):
            res = t.exec("whatever")
        assert isinstance(res, ExecResult)
        assert res.exit_code == 3
        assert res.stdout == "out-data"
        assert res.stderr == "err-data"

    def test_success_zero_exit(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0, "hi", "")):
            res = t.exec("true")
        assert res.exit_code == 0
        assert res.stdout == "hi"
        assert res.stderr == ""

    def test_nonzero_exit_does_not_raise(self):
        # A failing remote command is a normal result, not a connection error.
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(127, "", "not found")):
            res = t.exec("missingcmd")
        assert res.exit_code == 127
        assert res.stderr == "not found"

    def test_run_called_with_capture_text_and_timeout(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed()) as run:
            t.exec("cmd", timeout=42)
        kwargs = run.call_args.kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 42

    def test_default_timeout_is_none(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed()) as run:
            t.exec("cmd")
        assert run.call_args.kwargs["timeout"] is None

    def test_timeout_raises_gpudevtimeouterror_with_seconds(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=5)):
            with pytest.raises(GpuDevTimeoutError) as ei:
                t.exec("sleep 100", timeout=5)
        assert "5s" in str(ei.value)

    def test_missing_ssh_binary_raises_connectionerror(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", side_effect=FileNotFoundError("ssh")):
            with pytest.raises(GpuDevConnectionError) as ei:
                t.exec("cmd")
        assert "ssh binary not found" in str(ei.value)

    def test_generic_exception_wrapped_as_connectionerror(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", side_effect=OSError("boom")):
            with pytest.raises(GpuDevConnectionError) as ei:
                t.exec("cmd")
        assert "SSH failed" in str(ei.value)
        assert "boom" in str(ei.value)

    def test_fqdn_mode_argv_includes_proxycommand(self):
        t = SshTransport("p", fqdn="f.example")
        with patch(f"{MOD}.subprocess.run", return_value=_completed()) as run:
            t.exec("hostname")
        argv = run.call_args.args[0]
        assert "ProxyCommand=gpu-dev-ssh-proxy %h %p" in argv
        assert "dev@f.example" in argv


# --------------------------------------------------------------------------- #
# upload                                                                       #
# --------------------------------------------------------------------------- #
class TestUpload:
    def test_uses_rsync_with_resolved_src_and_pod_dest(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_text("x")
        t = SshTransport("mypod")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.upload(str(local), "/remote/dst")
        argv = run.call_args.args[0]
        assert argv[0] == "rsync"
        assert "-az" in argv
        assert "-e" in argv
        # src is absolutized via Path.resolve()
        assert str(Path(local).resolve()) in argv
        # without fqdn, remote host is the bare pod name
        assert "mypod:/remote/dst" in argv

    def test_src_is_resolved_to_absolute(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.upload("rel/path", "/dst")
        argv = run.call_args.args[0]
        assert str(Path("rel/path").resolve()) in argv

    def test_ssh_e_arg_has_no_proxycommand_without_fqdn(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.upload("/a", "/b")
        argv = run.call_args.args[0]
        ssh_cmd = argv[argv.index("-e") + 1]
        assert ssh_cmd.startswith("ssh ")
        assert "ProxyCommand" not in ssh_cmd
        assert "StrictHostKeyChecking=no" in ssh_cmd

    def test_fqdn_adds_proxycommand_to_e_arg_and_dev_host(self):
        t = SshTransport("p", fqdn="host.x")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.upload("/a", "/b")
        argv = run.call_args.args[0]
        ssh_cmd = argv[argv.index("-e") + 1]
        assert "ProxyCommand=gpu-dev-ssh-proxy %h %p" in ssh_cmd
        assert "dev@host.x:/b" in argv

    def test_nonzero_returncode_raises_connectionerror_with_stderr(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run",
                   return_value=_completed(23, stderr="rsync: link failed")):
            with pytest.raises(GpuDevConnectionError) as ei:
                t.upload("/a", "/b")
        assert "Upload failed" in str(ei.value)
        assert "rsync: link failed" in str(ei.value)

    def test_timeout_raises_gpudevtimeouterror(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="rsync", timeout=300)):
            with pytest.raises(GpuDevTimeoutError) as ei:
                t.upload("/a", "/b")
        assert "Upload timed out" in str(ei.value)

    def test_success_returns_none(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)):
            assert t.upload("/a", "/b") is None

    def test_uses_300s_timeout(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.upload("/a", "/b")
        assert run.call_args.kwargs["timeout"] == 300


# --------------------------------------------------------------------------- #
# download                                                                     #
# --------------------------------------------------------------------------- #
class TestDownload:
    def test_uses_rsync_with_pod_src_and_resolved_local_dest(self, tmp_path):
        dest = tmp_path / "out"
        t = SshTransport("mypod")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.download("/remote/src", str(dest))
        argv = run.call_args.args[0]
        assert argv[0] == "rsync"
        assert "-az" in argv
        # source is pod:remote_path; dest is absolutized
        assert "mypod:/remote/src" in argv
        assert str(Path(dest).resolve()) in argv

    def test_local_dest_resolved_to_absolute(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.download("/remote", "rel/local")
        argv = run.call_args.args[0]
        assert str(Path("rel/local").resolve()) in argv

    def test_argument_order_source_before_dest(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.download("/remote/src", "/local/dst")
        argv = run.call_args.args[0]
        src = "p:/remote/src"
        dst = str(Path("/local/dst").resolve())
        assert argv.index(src) < argv.index(dst)

    def test_fqdn_adds_proxycommand_and_dev_host(self):
        t = SshTransport("p", fqdn="host.y")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.download("/remote", "/local")
        argv = run.call_args.args[0]
        ssh_cmd = argv[argv.index("-e") + 1]
        assert "ProxyCommand=gpu-dev-ssh-proxy %h %p" in ssh_cmd
        assert "dev@host.y:/remote" in argv

    def test_no_proxycommand_without_fqdn(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)) as run:
            t.download("/r", "/l")
        ssh_cmd = run.call_args.args[0][run.call_args.args[0].index("-e") + 1]
        assert "ProxyCommand" not in ssh_cmd

    def test_nonzero_returncode_raises_connectionerror_with_stderr(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run",
                   return_value=_completed(12, stderr="No such file")):
            with pytest.raises(GpuDevConnectionError) as ei:
                t.download("/r", "/l")
        assert "Download failed" in str(ei.value)
        assert "No such file" in str(ei.value)

    def test_timeout_raises_gpudevtimeouterror(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="rsync", timeout=300)):
            with pytest.raises(GpuDevTimeoutError) as ei:
                t.download("/r", "/l")
        assert "Download timed out" in str(ei.value)

    def test_success_returns_none(self):
        t = SshTransport("p")
        with patch(f"{MOD}.subprocess.run", return_value=_completed(0)):
            assert t.download("/r", "/l") is None


# --------------------------------------------------------------------------- #
# constructor                                                                  #
# --------------------------------------------------------------------------- #
class TestInit:
    def test_defaults_fqdn_none(self):
        t = SshTransport("pod-abc")
        assert t.pod_name == "pod-abc"
        assert t.fqdn is None

    def test_stores_fqdn(self):
        t = SshTransport("pod-abc", fqdn="f.q.dn")
        assert t.fqdn == "f.q.dn"
