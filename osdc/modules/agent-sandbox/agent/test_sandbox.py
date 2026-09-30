"""Tests for the sandbox agent worker (sandbox.py).

The worker is stdlib-only — it reaches Bedrock through the sigv4 proxy over plain
HTTP — so nothing needs stubbing. Everything runs for real: `clone_repo` against a
real git repo (redirected off the network), `invoke_bedrock` against a fake sigv4
proxy on a real socket, and the HTTP surface against a real socket.
"""

import http.client
import io
import json
import os
import subprocess
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
import sandbox


def _git_repo(path, entries):
    """A real git repo with `entries` (paths ending in / become directories)."""
    path.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        target = path / entry.rstrip("/")
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            (target / "keep.txt").write_text("x\n")
        else:
            target.write_text("x\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env={**env, "PATH": "/usr/bin:/bin"})
    return path


GIT_PROXY_HOST = "git-proxy.ai-sandbox.svc:8080"


def _insteadof(tmp_path, monkeypatch, rules):
    """Redirect URL prefixes to local paths with git's `insteadOf` rewrite.

    `rules` maps the prefix `clone_repo` builds to the local URI it should resolve to.
    A prefix left OUT of the mapping does not resolve at all, which is what lets a test
    assert WHICH url was built without inspecting the argv: the clone either lands or
    it fails."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("".join(f'[url "{target}"]\n\tinsteadOf = {prefix}\n' for prefix, target in rules.items()))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))


def _commit(repo, name, content="z\n"):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
    }
    (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", name], check=True, env=env)


class TestCloneRepo:
    """`clone_repo` builds https://github.com/<repo>.git, or http://$GIT_PROXY/<repo>.git
    for a private one. Point those at a local repo with git's `insteadOf` rewrite so the
    real clone path is exercised without touching the network."""

    @pytest.fixture
    def local_github(self, tmp_path, monkeypatch):
        origin = _git_repo(tmp_path / "origin" / "org" / "repo.git", ["README.md", "setup.py", "torch/"])
        _insteadof(tmp_path, monkeypatch, {"https://github.com/": f"{(tmp_path / 'origin').as_uri()}/"})
        return origin

    @pytest.fixture
    def local_git_proxy(self, tmp_path, monkeypatch):
        """The proxy prefix resolves and github.com does not, so a clone that went
        straight to github.com fails instead of quietly passing — and neither one
        leaves the machine."""
        origin = _git_repo(tmp_path / "origin" / "org" / "repo.git", ["README.md", "setup.py", "torch/"])
        _insteadof(
            tmp_path,
            monkeypatch,
            {
                f"http://{GIT_PROXY_HOST}/": f"{(tmp_path / 'origin').as_uri()}/",
                "https://github.com/": f"{(tmp_path / 'no-such-origin').as_uri()}/",
            },
        )
        monkeypatch.setattr(sandbox, "GIT_PROXY", GIT_PROXY_HOST)
        return origin

    def test_counts_tracked_files(self, local_github, tmp_path):
        # 3 tracked files: README.md, setup.py, torch/keep.txt
        assert sandbox.clone_repo("org/repo", "main", str(tmp_path / "dest")) == 3

    def test_missing_ref_raises(self, local_github, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            sandbox.clone_repo("org/repo", "no-such-branch", str(tmp_path / "dest"))

    def test_checks_out_a_pull_request_head_ref(self, local_github, tmp_path):
        """The ref a review needs, and the reason `git clone --branch` had to go: it
        resolves branches and tags only. A PR head ref lives in the BASE repository, so
        this reaches a fork's pull request without ever naming the fork."""
        subprocess.run(["git", "-C", str(local_github), "update-ref", "refs/pull/7/head", "HEAD"], check=True)
        assert sandbox.clone_repo("org/repo", "refs/pull/7/head", str(tmp_path / "dest")) == 3

    def test_checks_out_a_commit_sha(self, local_github, tmp_path):
        """Also unreachable through --branch. Fetching a bare sha needs the server to
        allow it. This is the CLIENT side only: the fixture's file:// transport ignores
        uploadpack.allowReachableSHA1InWant, so it cannot prove github.com permits it
        (checked by hand on 2026-09-23 — it does)."""
        sha = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert sandbox.clone_repo("org/repo", sha, str(tmp_path / "dest")) == 3

    def test_checks_out_the_ref_not_the_default_branch(self, local_github, tmp_path):
        """A fetch that lands but leaves the tree on some other commit would count the
        right number of files and review the wrong code.

        The origin's HEAD is put back on main afterwards, and that is the whole point:
        with HEAD left on `other`, a fetch that ignored the refspec entirely would still
        land on the same commit and this test would pass while asserting nothing. Both
        directions are checked for the same reason.
        """
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(["git", "-C", str(local_github), "checkout", "-q", "-b", "other"], check=True)
        (local_github / "only-on-other.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(local_github), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(local_github), "commit", "-qm", "second"], check=True, env=env)
        subprocess.run(["git", "-C", str(local_github), "checkout", "-q", "main"], check=True)
        subprocess.run(["git", "-C", str(local_github), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)

        other = tmp_path / "other"
        assert sandbox.clone_repo("org/repo", "other", str(other)) == 4
        assert (other / "only-on-other.txt").exists()
        main = tmp_path / "main"
        assert sandbox.clone_repo("org/repo", "main", str(main)) == 3
        assert not (main / "only-on-other.txt").exists(), "the refspec was ignored"

    def test_a_ref_cannot_smuggle_a_git_option(self, local_github, tmp_path):
        """`ref` is caller-supplied and git permutes arguments, so without `--` a ref of
        `--upload-pack=...` is read as an OPTION: over https it silently fetches the
        default branch, and over the file:// transport this fixture uses git EXECUTES it.
        `git clone --branch <ref>` was immune because it consumed ref as an option value.
        """
        marker = tmp_path / "pwned"
        with pytest.raises(ValueError, match="not a plain ref name"):
            sandbox.clone_repo("org/repo", f"--upload-pack=touch {marker}", str(tmp_path / "dest"))
        assert not marker.exists(), "a ref was parsed as a git option and executed"

    def test_the_dash_dash_still_holds_without_the_shape_check(self, local_github, tmp_path, monkeypatch):
        """The two guards are independent, and this one asserts the second alone.

        `_check_ref` is what refuses the option today, so with it neutered the `--`
        carries the whole weight — and this fails loudly if a later edit drops the `--`
        on the grounds that the regex already covers it. Under the file:// transport a
        ref read as `--upload-pack` is EXECUTED, so the marker is the proof."""
        monkeypatch.setattr(sandbox, "_check_ref", lambda ref: ref)
        marker = tmp_path / "pwned"
        with pytest.raises(subprocess.CalledProcessError):
            sandbox.clone_repo("org/repo", f"--upload-pack=touch {marker}", str(tmp_path / "dest"))
        assert not marker.exists(), "a ref was parsed as a git option and executed"

    def test_a_ref_cannot_smuggle_a_refspec(self, local_github, tmp_path):
        """`--` stops git reading `ref` as an OPTION; it does not stop git reading it as
        a REFSPEC. `refs/heads/*:refs/heads/*` fetches every branch and leaves FETCH_HEAD
        on one of them — the wrong tree, a plausible file count, and no error at all.
        Rejected on shape before the fetch runs."""
        subprocess.run(["git", "-C", str(local_github), "branch", "decoy"], check=True)
        with pytest.raises(ValueError, match="not a plain ref name"):
            sandbox.clone_repo("org/repo", "refs/heads/*:refs/heads/*", str(tmp_path / "dest"))

    @pytest.mark.parametrize(
        "ref",
        [
            "+refs/heads/main",  # force
            "refs/heads/main:refs/heads/main",  # explicit destination
            "main^{commit}",  # a revision expression, not a ref
            "v1..v2",  # a range
            "",  # nothing at all
            "main branch",  # whitespace
        ],
    )
    def test_refspec_shapes_are_refused(self, local_github, tmp_path, ref):
        with pytest.raises(ValueError, match="not a plain ref name"):
            sandbox.clone_repo("org/repo", ref, str(tmp_path / "dest"))

    @pytest.mark.parametrize("ref", ["main", "release/2.9", "v2.1.0", "refs/pull/7/head", "a" * 40])
    def test_real_refs_are_accepted(self, ref):
        """The shapes the callers actually send. Guarding the guard: a check that also
        rejected `refs/pull/<n>/head` would break the feature this stack exists for."""
        assert sandbox._check_ref(ref) == ref

    def test_a_git_proxy_replaces_the_github_url(self, local_git_proxy, tmp_path):
        """A private repo needs a credential this process must never hold, so the clone
        goes to the proxy that does — a real fetch over the URL clone_repo built, not an
        assertion about its argv. Under this fixture only the proxy prefix resolves."""
        assert sandbox.clone_repo("org/repo", "main", str(tmp_path / "dest")) == 3

    def test_without_a_git_proxy_it_talks_to_github(self, local_github, tmp_path, monkeypatch):
        """And the mirror image: `local_github` rewrites github.com and nothing else, so
        this passing means the unproxied URL is the one that was fetched."""
        monkeypatch.setattr(sandbox, "GIT_PROXY", "")
        assert sandbox.clone_repo("org/repo", "main", str(tmp_path / "dest")) == 3

    def test_checks_out_a_tag(self, local_github, tmp_path):
        subprocess.run(["git", "-C", str(local_github), "tag", "v1"], check=True)
        assert sandbox.clone_repo("org/repo", "v1", str(tmp_path / "dest")) == 3

    def test_an_annotated_tag_resolves_to_its_commit(self, local_github, tmp_path):
        env = {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "-C", str(local_github), "tag", "-a", "v2", "-m", "v2"], check=True, env=env)
        assert sandbox.clone_repo("org/repo", "v2", str(tmp_path / "dest")) == 3

    def test_a_non_utf8_change_stays_visible_in_the_diff(self, local_github, tmp_path):
        (local_github / "latin1.txt").write_bytes(b"caf\xe9\n")
        _commit(local_github, "setup.py")
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (local_github / "latin1.txt").write_bytes(b"caf\xe8\n")
        _commit(local_github, "README.md", "changed\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        _, _, patch, _ = sandbox.diff_against("org/repo", str(dest), base)
        assert "-caf\\xe9" in patch
        assert "+caf\\xe8" in patch

    def test_escaped_non_utf8_bytes_stay_within_the_prompt_budget(self, local_github, tmp_path, monkeypatch):
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (local_github / "bin.txt").write_bytes(b"\xff" * 3000 + b"\n")
        _commit(local_github, "README.md", "changed\n")
        monkeypatch.setattr(sandbox, "MAX_DIFF_BYTES", 2000)
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        _, _, patch, truncated = sandbox.diff_against("org/repo", str(dest), base)
        assert len(patch.encode()) <= 2000
        assert truncated is True

    def test_a_non_utf8_file_name_does_not_fail_the_checkout(self, local_github, tmp_path):
        import os

        os.close(os.open(os.fsencode(str(local_github)) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY))
        _commit(local_github, "README.md", "changed\n")
        dest = tmp_path / "dest"
        assert sandbox.clone_repo("org/repo", "main", str(dest)) == 4
        assert "caf\\xe9.txt" in sandbox.top_level_entries(str(dest))

    def test_odd_directory_names_keep_their_bytes_and_their_slash(self, local_github, tmp_path):
        import os

        for raw in (b"dir\rname", b"caf\xe9dir"):
            path = os.fsencode(str(local_github)) + b"/" + raw
            os.mkdir(path)
            os.close(os.open(path + b"/f.txt", os.O_CREAT | os.O_WRONLY))
        _commit(local_github, "README.md", "changed\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        entries = sandbox.top_level_entries(str(dest))
        assert "dir\rname/" in entries
        assert "caf\\xe9dir/" in entries

    def test_diff_against_a_base_lists_the_change(self, local_github, tmp_path):
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _commit(local_github, "new_file.py", "print('hi')\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        files, total, patch, truncated = sandbox.diff_against("org/repo", str(dest), base)
        assert files == ["new_file.py"]
        assert total == 1
        assert "+print('hi')" in patch
        assert truncated is False

    def test_a_large_diff_is_truncated(self, local_github, tmp_path, monkeypatch):
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _commit(local_github, "big.txt", "y\n" * 5000)
        monkeypatch.setattr(sandbox, "MAX_DIFF_BYTES", 1000)
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        _, _, patch, truncated = sandbox.diff_against("org/repo", str(dest), base)
        assert truncated is True
        assert len(patch.encode()) <= 1000

    def test_a_gitattributes_in_the_change_cannot_hide_the_diff(self, local_github, tmp_path):
        """The tree is the author's: `* -diff` in its .gitattributes would turn the whole
        patch, that file included, into "Binary files ... differ"."""
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (local_github / ".gitattributes").write_text("* -diff\n")
        _commit(local_github, "setup.py", "import evil\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        files, _, patch, _ = sandbox.diff_against("org/repo", str(dest), base)
        assert sorted(files) == [".gitattributes", "setup.py"]
        assert "+import evil" in patch
        assert "+* -diff" in patch
        assert "Binary files" not in patch

    def test_a_gitmodules_ignore_cannot_hide_a_submodule_bump(self, local_github, tmp_path):
        """`ignore = all` in the tree's .gitmodules would drop a submodule pointer change
        from both the file list and the patch."""
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def run(*args):
            subprocess.run(["git", "-C", str(local_github), *args], check=True, env={**os.environ, **env})

        (local_github / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = ../x\n\tignore = all\n')
        run("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
        run("add", ".gitmodules")
        run("commit", "-qm", "add sub")
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        run("update-index", "--cacheinfo", f"160000,{'2' * 40},sub")
        run("commit", "-qm", "bump sub")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        files, _, patch, _ = sandbox.diff_against("org/repo", str(dest), base)
        assert files == ["sub"]
        assert "+Subproject commit " + "2" * 40 in patch

    def test_a_file_named_head_does_not_make_the_diff_ambiguous(self, local_github, tmp_path):
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _commit(local_github, "HEAD", "not a revision\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        files, total, _, _ = sandbox.diff_against("org/repo", str(dest), base)
        assert files == ["HEAD"]
        assert total == 1

    def test_a_git_failure_raises_instead_of_an_empty_diff(self, local_github, tmp_path, monkeypatch):
        base = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        real_run = subprocess.run

        def failing_diff(cmd, **kwargs):
            if cmd[:2] == ["git", "diff"]:
                return real_run(["git", "diff", "no-such-rev", "HEAD", "--"], **kwargs)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(sandbox.subprocess, "run", failing_diff)
        with pytest.raises(subprocess.CalledProcessError):
            sandbox.diff_against("org/repo", str(dest), base)

    def test_a_slow_diff_is_bounded_by_its_timeout(self, tmp_path, monkeypatch):
        with pytest.raises(subprocess.TimeoutExpired):
            sandbox._git_to_file(
                ["-c", "alias.slow=!sleep 5", "slow"], str(tmp_path), str(tmp_path / "out"), timeout=0.2
            )

    @pytest.mark.parametrize("base", ["main", "abc", "-x" + "0" * 38, "a" * 40 + "\n"])
    def test_a_base_that_is_not_a_sha_is_refused(self, local_github, tmp_path, base):
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        with pytest.raises(ValueError, match="full 40-character"):
            sandbox.diff_against("org/repo", str(dest), base)

    def test_the_base_is_fetched_through_the_proxy_too(self, local_git_proxy, tmp_path):
        """A private repository's base commit needs the credential as much as its head
        does. Under this fixture only the proxy prefix resolves, so a base fetched from
        github.com would fail the diff."""
        base = subprocess.run(
            ["git", "-C", str(local_git_proxy), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _commit(local_git_proxy, "new_file.py", "print('hi')\n")
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "main", str(dest))
        files, _, _, _ = sandbox.diff_against("org/repo", str(dest), base)
        assert files == ["new_file.py"]

    def test_head_sha_names_the_checked_out_commit(self, local_github, tmp_path):
        """What a caller pins to: a pull request head or a branch can move after the
        review, the reported commit cannot."""
        subprocess.run(["git", "-C", str(local_github), "update-ref", "refs/pull/7/head", "HEAD"], check=True)
        want = subprocess.run(
            ["git", "-C", str(local_github), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dest = tmp_path / "dest"
        sandbox.clone_repo("org/repo", "refs/pull/7/head", str(dest))
        assert sandbox.head_sha(str(dest)) == want

    def test_terminal_prompts_stay_disabled(self, local_github, tmp_path, monkeypatch):
        """A credential prompt would hang the worker forever instead of failing."""
        captured = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            captured.setdefault("env", kwargs.get("env"))
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(sandbox.subprocess, "run", spy)
        sandbox.clone_repo("org/repo", "main", str(tmp_path / "dest"))
        assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


class TestTopLevelEntries:
    def test_marks_directories_with_slash(self, tmp_path):
        repo = _git_repo(tmp_path / "repo", ["README.md", "setup.py", "torch/"])
        assert sorted(sandbox.top_level_entries(str(repo))) == ["README.md", "setup.py", "torch/"]

    def test_lists_only_top_level(self, tmp_path):
        repo = _git_repo(tmp_path / "repo", ["aten/", "torch/"])
        entries = sandbox.top_level_entries(str(repo))
        assert entries == ["aten/", "torch/"], "nested files must not appear as top-level entries"

    def test_raises_outside_a_repo(self, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            sandbox.top_level_entries(str(tmp_path))


class TestBuildPrompt:
    def test_includes_the_real_listing(self):
        prompt = sandbox.build_prompt("pytorch/pytorch", "main", "List the files.", 21952, ["README.md", "torch/"])
        assert "pytorch/pytorch" in prompt
        assert "21952 tracked files" in prompt
        assert "README.md" in prompt
        assert "torch/" in prompt
        assert "List the files." in prompt

    def test_forbids_guessing(self):
        """Without this the model invents a plausible listing and the canary's
        'Bedrock returned a report' assertion proves nothing."""
        prompt = sandbox.build_prompt("r", "main", "t", 1, ["a"])
        assert "do not invent paths" in prompt

    def test_omits_the_listing_section_when_empty(self):
        prompt = sandbox.build_prompt("r", "main", "t", 0, [])
        assert "Top-level entries" not in prompt


@pytest.fixture
def fake_sigv4_proxy(monkeypatch):
    """Stand in for aws-sigv4-proxy: record what the worker sent, reply with a
    Bedrock-shaped body. The real proxy adds the SigV4 signature — the worker
    deliberately sends an unsigned request, which is the whole point of the
    design, so there is no credential here to assert on."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["headers"] = dict(self.headers)
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps(seen.get("reply", {"content": [{"type": "text", "text": "the report"}]})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(sandbox, "SIGV4_PROXY", f"127.0.0.1:{httpd.server_address[1]}")
    yield seen
    httpd.shutdown()
    httpd.server_close()


class TestInvokeBedrock:
    def test_posts_an_unsigned_messages_api_request_to_the_proxy(self, fake_sigv4_proxy):
        model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert sandbox.invoke_bedrock(model, "hello") == "the report"

        assert fake_sigv4_proxy["path"] == f"/model/{model}/invoke"
        body = fake_sigv4_proxy["body"]
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]

    def test_sets_the_bedrock_host_header_for_the_proxy_to_sign(self, fake_sigv4_proxy):
        """aws-sigv4-proxy signs for the upstream named in Host; without it the
        request would be signed for (and sent to) the wrong service."""
        sandbox.invoke_bedrock("m", "hello")
        assert fake_sigv4_proxy["headers"]["Host"] == f"bedrock-runtime.{sandbox.REGION}.amazonaws.com"

    def test_carries_no_credential(self, fake_sigv4_proxy):
        """The worker must never hold or send AWS credentials — signing is the
        proxy's job."""
        sandbox.invoke_bedrock("m", "hello")
        sent = {k.lower() for k in fake_sigv4_proxy["headers"]}
        assert "authorization" not in sent
        assert not any(h.startswith("x-amz-security-token") for h in sent)

    def test_empty_content_yields_empty_report(self, fake_sigv4_proxy):
        fake_sigv4_proxy["reply"] = {"content": []}
        assert sandbox.invoke_bedrock("m", "hello") == ""

    def test_model_is_one_percent_encoded_path_segment(self, fake_sigv4_proxy):
        """An ARN is a documented model identifier and contains "/" — unencoded it
        splits the path and the request stops naming a model invoke at all."""
        arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-v1:0"
        sandbox.invoke_bedrock(arn, "hello")
        path = fake_sigv4_proxy["path"]
        assert path.endswith("/invoke")
        assert path.count("/") == 3, f"model id must be a single path segment, got {path}"
        assert "inference-profile%2F" in path
        # ":" stays as sent today — a legal path character, and every model id in
        # use ends in "…-v1:0".
        assert path.startswith("/model/arn:aws:bedrock:")

    def test_model_cannot_steer_the_path_the_proxy_signs(self, fake_sigv4_proxy):
        """`model` comes from an unauthenticated request body, and the proxy runs
        with no --name: it signs and forwards whatever path it is handed."""
        sandbox.invoke_bedrock("../../async-invoke#", "hello")
        assert fake_sigv4_proxy["path"] == "/model/..%2F..%2Fasync-invoke%23/invoke"


class TestReadBounded:
    class _Trickle:
        """A body that never ends — one byte per read, as a stalled proxy would."""

        def read(self, _size):
            return b"x"

    def test_deadline_stops_a_trickling_response(self):
        """BEDROCK_TIMEOUT_S is urllib's per-operation socket timeout, not a wall
        clock: without a deadline this holds the single task slot indefinitely."""
        with pytest.raises(TimeoutError):
            sandbox._read_bounded(self._Trickle(), sandbox.MAX_RESPONSE_BYTES, time.monotonic() - 1)

    def test_oversized_response_is_rejected(self):
        with pytest.raises(ValueError, match="exceeded"):
            sandbox._read_bounded(self._Trickle(), 4, time.monotonic() + 30)

    def test_reads_to_end_of_body(self):
        chunks = iter([b"abc", b"def", b""])

        class Body:
            def read(self, _size):
                return next(chunks)

        assert sandbox._read_bounded(Body(), 1024, time.monotonic() + 30) == b"abcdef"


class TestBedrockErrorSummary:
    def _http_error(self, code, headers, body):
        return urllib.error.HTTPError("http://proxy/model/m/invoke", code, "Forbidden", headers, io.BytesIO(body))

    def test_error_code_from_body(self):
        """`str(HTTPError)` is only the status line, so "not authorised" and
        "throttled" — the two likeliest failures — read identically without this."""
        exc = self._http_error(
            403, {}, json.dumps({"__type": "com.amazon.coral.service#AccessDeniedException"}).encode()
        )
        summary = sandbox.bedrock_error_summary(exc)
        assert "403" in summary
        assert "AccessDeniedException" in summary

    def test_error_code_from_header(self):
        exc = self._http_error(400, {"x-amzn-errortype": "ThrottlingException:http://internal/"}, b"")
        assert "ThrottlingException" in sandbox.bedrock_error_summary(exc)

    def test_message_is_not_echoed_back(self):
        """An AccessDenied message names the role ARN the proxy signs with, and any
        caller the NetworkPolicy allows can read /run's response."""
        body = json.dumps(
            {
                "__type": "AccessDeniedException",
                "message": "User: arn:aws:sts::123456789012:assumed-role/sigv4-proxy/x is not authorized",
            }
        ).encode()
        summary = sandbox.bedrock_error_summary(self._http_error(403, {}, body))
        assert "assumed-role" not in summary
        assert "AccessDeniedException" in summary

    def test_unparseable_body_falls_back_to_the_status_line(self):
        exc = self._http_error(500, {}, b"<html>gateway</html>")
        assert sandbox.bedrock_error_summary(exc) == str(exc)


class TestRunTask:
    """Each stage's failure must be captured, never raised — callers need to see
    exactly which part of the credential path worked."""

    @pytest.fixture(autouse=True)
    def no_real_git(self, monkeypatch):
        """clone_repo is faked in these tests, so there is no checkout to read a sha from."""
        monkeypatch.setattr(sandbox, "head_sha", lambda dest: "f" * 40)

    @pytest.fixture(autouse=True)
    def one_turn_agent(self, monkeypatch):
        """These tests are about stages, not the loop: a one-turn agent that asks
        `invoke_bedrock` (which each test patches) keeps them readable. The loop is
        tested in test_agent_loop.py and TestRunTaskLoop."""
        monkeypatch.setattr(
            sandbox.agent_loop,
            "run_agent",
            lambda invoke, model, prompt, tools, **kw: {
                "report": sandbox.invoke_bedrock(model, prompt),
                "turns": 1,
                "tool_calls": 0,
            },
        )

    def test_a_base_puts_the_diff_in_the_prompt(self, monkeypatch):
        prompts = []
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "diff_against", lambda repo, dest, base: (["a.py"], 1, "+x = 1", True))
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: prompts.append(prompt) or "ok")
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "base": "a" * 40})
        assert result["changed_files"] == ["a.py"]
        assert result["changed_files_total"] == 1
        assert result["diff_truncated"] is True
        assert result["head_sha"] == "f" * 40
        for expected in ("+x = 1", "TRUNCATED", "a.py"):
            assert expected in prompts[0]

    def test_a_diff_too_large_for_the_window_is_cut_to_fit(self, monkeypatch):
        prompts = []
        monkeypatch.setattr(sandbox.agent_loop, "prompt_budget_bytes", lambda: 4000)
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "diff_against", lambda repo, dest, base: (["a.py"], 1, "+" + "é" * 5000, False))
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: prompts.append(prompt) or "ok")
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "base": "a" * 40})
        assert len(prompts[0].encode()) <= 4000
        assert "TRUNCATED" in prompts[0]
        assert "+é" in prompts[0]
        assert result["diff_truncated"] is True
        assert result["errors"] == {}

    def test_a_diff_that_cannot_be_computed_stops_the_task(self, monkeypatch):
        def boom(repo, dest, base):
            raise subprocess.CalledProcessError(128, "git fetch", stderr="not our ref")

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "diff_against", boom)
        monkeypatch.setattr(
            sandbox, "invoke_bedrock", lambda *a: pytest.fail("a review without the diff is not a review")
        )
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "base": "a" * 40})
        assert "not our ref" in result["errors"]["diff"]

    def test_the_whole_result_fits_the_log_transport(self, monkeypatch):
        """The dispatcher reads at most 1 MiB of the pod log; a longer result is lost.
        Both name lists are populated, and a huge git error rides along."""
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["t" * 250 + str(i) for i in range(3400)])
        names = ["é" * 200 + str(i) for i in range(5000)]
        monkeypatch.setattr(sandbox, "diff_against", lambda repo, dest, base: (names, 5000, "", True))
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: "ok")
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "base": "a" * 40})
        result["errors"]["x"] = sandbox._error_text(subprocess.CalledProcessError(1, "git", stderr="e" * 10**6))
        assert len(json.dumps(result)) < 1024 * 1024, "the result must fit what the dispatcher reads"
        assert result["top_level_total"] == 3400
        assert 0 < len(result["changed_files"]) < 5000
        assert result["changed_files_total"] == 5000

    def test_the_top_level_listing_is_bounded_in_the_prompt(self):
        prompt = sandbox.build_prompt("org/repo", "main", "t", 1, ["t" * 250 + str(i) for i in range(3400)])
        assert len(prompt.encode()) < 64 * 1024
        assert "of 3400" in prompt

    def test_the_changed_file_list_is_bounded_in_the_prompt(self, monkeypatch):
        """By bytes, not count: 500 long paths would otherwise fill the context window."""
        files = ["d/" * 1900 + str(i) for i in range(500)]
        change = {"base": "b", "files": files, "patch": "", "truncated": False}
        prompt = sandbox.build_prompt("org/repo", "main", "t", 1, [], change)
        assert len(prompt.encode()) < 64 * 1024
        assert "more" in prompt

    def test_a_bytes_stderr_is_decoded_so_the_result_serializes(self, monkeypatch):
        def boom(repo, dest, base):
            raise subprocess.CalledProcessError(128, "git diff", stderr=b"fatal: bad revision")

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "diff_against", boom)
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "base": "a" * 40})
        assert json.loads(json.dumps(result))["errors"]["diff"] == "fatal: bad revision"

    def test_a_pull_request_and_a_base_diff_the_pull_request_head(self, monkeypatch):
        """`pr` picks the head, `base` the other end of the diff: a PR review in one call."""
        seen = {}

        def fake_clone(repo, ref, dest):
            seen["ref"] = ref
            return 1

        def fake_diff(repo, dest, base):
            seen["base"] = base
            return ["a.py"], 1, "+x", False

        monkeypatch.setattr(sandbox, "clone_repo", fake_clone)
        monkeypatch.setattr(sandbox, "diff_against", fake_diff)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(
            sandbox.agent_loop,
            "run_agent",
            lambda invoke, model, prompt, tools: {"report": "ok", "turns": 1, "tool_calls": 0},
        )
        result = sandbox.run_task({"repo": "org/repo", "model": "m", "pr": 7, "base": "a" * 40})
        assert seen == {"ref": "refs/pull/7/head", "base": "a" * 40}
        assert result["pr"] == 7
        assert result["changed_files"] == ["a.py"]

    def test_clone_failure_stops_before_bedrock(self, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.CalledProcessError(128, "git", stderr="could not read Username")

        monkeypatch.setattr(sandbox, "clone_repo", boom)
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda *a: pytest.fail("must not call Bedrock"))

        result = sandbox.run_task({"repo": "org/private", "model": "m"})
        assert result["cloned"] is False
        assert "could not read Username" in result["errors"]["clone"]
        assert result["report"] == ""

    def test_missing_model_is_reported(self, monkeypatch):
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 7)
        monkeypatch.setattr(sandbox, "DEFAULT_MODEL", "")

        result = sandbox.run_task({"repo": "org/repo"})
        assert result["cloned"] is True
        assert result["file_count"] == 7
        assert result["errors"]["bedrock"].startswith("no model configured")

    def test_spec_model_overrides_the_default(self, monkeypatch):
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: f"used {model}")

        result = sandbox.run_task({"repo": "org/repo", "model": "us.anthropic.override"})
        assert result["report"] == "used us.anthropic.override"
        assert result["top_level"] == ["README.md"]
        assert result["errors"] == {}

    def test_listing_failure_still_asks_bedrock(self, monkeypatch):
        """Grounding is best-effort: a repo we can't list is still worth asking about."""

        def boom(dest):
            raise subprocess.CalledProcessError(128, "git ls-tree", stderr="not a repository")

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", boom)
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: "report anyway")

        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert result["report"] == "report anyway"
        assert result["top_level"] == []
        assert "not a repository" in result["errors"]["listing"]
        assert "bedrock" not in result["errors"]

    def test_bedrock_failure_is_captured(self, monkeypatch):
        def boom(model, prompt):
            raise urllib.error.URLError("sigv4-proxy unreachable")

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "invoke_bedrock", boom)

        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert result["cloned"] is True
        assert "sigv4-proxy unreachable" in result["errors"]["bedrock"]

    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b"11 bytes", 489),
            http.client.RemoteDisconnected("remote end closed connection"),
            TypeError("string indices must be integers"),
        ],
        ids=["truncated-body", "reset-status-line", "malformed-payload"],
    )
    def test_proxy_disconnect_mid_response_is_captured(self, monkeypatch, exc):
        """A proxy restart mid-response must still produce the `errors` object. An
        escaping exception closes the connection instead, and a closed connection
        cannot be told apart from the pod being gone."""

        def boom(model, prompt):
            raise exc

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "invoke_bedrock", boom)

        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert result["cloned"] is True
        assert result["errors"]["bedrock"]

    def test_http_error_reports_the_aws_error_code(self, monkeypatch):
        def boom(model, prompt):
            raise urllib.error.HTTPError(
                "http://proxy/model/m/invoke",
                403,
                "Forbidden",
                {},
                io.BytesIO(json.dumps({"__type": "AccessDeniedException"}).encode()),
            )

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(sandbox, "invoke_bedrock", boom)

        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert "AccessDeniedException" in result["errors"]["bedrock"]

    def test_invoke_model_summarises_an_http_error_inside_the_call(self, monkeypatch):
        """The error body is read before invoke_model returns, so the loop's wall clock
        covers it; run_task only formats the summary."""
        reads = []

        class Body(io.BytesIO):
            def read(self, *a):
                reads.append(a)
                return super().read(*a)

        def refuse(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, Body(json.dumps({"__type": "AccessDeniedException"}).encode())
            )

        monkeypatch.setattr(sandbox.urllib.request, "urlopen", refuse)
        with pytest.raises(sandbox.BedrockHTTPError, match="AccessDeniedException") as exc:
            sandbox.invoke_model("m", {"messages": []})
        assert reads, "the body was read inside the call"
        assert exc.value.status == 403, "the loop reads the status to decide on a retry"

    @pytest.mark.parametrize("ref", [None, 0, [], ""], ids=["null", "zero", "list", "empty"])
    def test_non_string_ref_falls_back_to_main(self, monkeypatch, ref):
        """`spec.get("ref", "main")` returns None for an explicit null, and None
        would reach git as a command argument."""
        seen = {}

        def spy_clone(repo, resolved_ref, dest):
            seen["ref"] = resolved_ref
            return 1

        monkeypatch.setattr(sandbox, "clone_repo", spy_clone)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(sandbox, "invoke_bedrock", lambda model, prompt: "ok")

        result = sandbox.run_task({"repo": "org/repo", "ref": ref, "model": "m"})
        assert seen["ref"] == "main"
        assert result["errors"] == {}


class TestPullRequestCheckout:
    """`{"pr": N}` is a selector that resolves to a ref — no separate code path."""

    @pytest.fixture(autouse=True)
    def no_real_git(self, monkeypatch):
        """clone_repo is faked in these tests, so there is no checkout to read a sha from."""
        monkeypatch.setattr(sandbox, "head_sha", lambda dest: "f" * 40)

    def _ref_used(self, monkeypatch, spec):
        seen = {}

        def fake_clone(repo, ref, dest):
            seen["ref"] = ref
            return 1

        monkeypatch.setattr(sandbox, "clone_repo", fake_clone)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(
            sandbox.agent_loop,
            "run_agent",
            lambda invoke, model, prompt, tools: {"report": "report", "turns": 1, "tool_calls": 0},
        )
        result = sandbox.run_task({"repo": "org/repo", "model": "m", **spec})
        return seen.get("ref"), result

    def test_a_pr_becomes_the_head_ref(self, monkeypatch):
        ref, result = self._ref_used(monkeypatch, {"pr": 1234})
        assert ref == "refs/pull/1234/head"
        assert result["pr"] == 1234

    def test_a_pr_wins_over_an_explicit_ref(self, monkeypatch):
        """The two name different commits. Reviewing the branch instead would be the
        wrong tree with nothing in the result to say anything was ignored."""
        ref, _ = self._ref_used(monkeypatch, {"pr": 1234, "ref": "main"})
        assert ref == "refs/pull/1234/head"

    def test_a_pr_number_as_a_string_still_resolves(self, monkeypatch):
        """task.py reads env vars, so it arrives as a string in production — an int-only
        path would silently check out the default branch instead."""
        ref, _ = self._ref_used(monkeypatch, {"pr": "1234"})
        assert ref == "refs/pull/1234/head"

    def test_a_true_pr_flag_is_not_pull_request_one(self, monkeypatch):
        """isinstance(True, int) is True, so an unguarded coercion checks out PR #1."""
        ref, result = self._ref_used(monkeypatch, {"pr": True, "ref": "main"})
        assert ref == "main"
        assert "pr" not in result

    def test_without_a_pr_the_ref_is_untouched(self, monkeypatch):
        ref, result = self._ref_used(monkeypatch, {"ref": "v2.9.0"})
        assert ref == "v2.9.0"
        assert "pr" not in result


class TestLoopTimeLimit:
    @pytest.mark.parametrize(
        ("deadline", "now", "expected"),
        [
            (None, 0.0, 600),  # no deadline: the loop's own
            ("", 0.0, 600),
            ("junk", 0.0, 600),
            ("nan", 0.0, 600),
            ("inf", 0.0, 600),
            ("1000900", 1000000.0, 600),  # 900 s left: the loop's own 600 s is nearer
            ("1000900", 1000500.0, 370),  # 400 s left, less the result margin
            ("1000900", 1000880.0, 0),  # already inside the margin
            ("1000900", 1002000.0, 0),  # past it
        ],
    )
    def test_the_loop_ends_before_the_task_deadline(self, deadline, now, expected):
        assert sandbox.loop_time_limit(deadline, now) == expected


class TestRunTaskLoop:
    @pytest.fixture(autouse=True)
    def no_real_git(self, monkeypatch):
        monkeypatch.setattr(sandbox, "head_sha", lambda dest: "f" * 40)

    def _run(self, monkeypatch, outcome):
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(sandbox.agent_loop, "run_agent", lambda *a, **kw: outcome)
        return sandbox.run_task({"repo": "org/repo", "model": "m"})

    def test_a_failed_model_call_is_a_bedrock_error_with_the_progress_so_far(self, monkeypatch):
        result = self._run(monkeypatch, {"report": "", "turns": 3, "tool_calls": 5, "model_error": "HTTP 503"})
        assert result["errors"] == {"bedrock": "HTTP 503"}
        assert (result["turns"], result["tool_calls"]) == (3, 5)

    def test_a_refused_tool_budget_reaches_the_result(self, monkeypatch):
        result = self._run(monkeypatch, {"report": "r", "turns": 4, "tool_calls": 9, "tools_refused": "context window"})
        assert result["tools_refused"] == "context window"
        assert result["errors"] == {}

    def test_a_slow_error_body_is_bounded_by_the_loop_deadline(self, monkeypatch):
        import time as _time

        def refuse(req, timeout):
            class Slow(io.BytesIO):
                def read(self, *a):
                    _time.sleep(5)
                    return b"{}"

            raise urllib.error.HTTPError(req.full_url, 500, "Error", {}, Slow())

        monkeypatch.setattr(sandbox.urllib.request, "urlopen", refuse)
        tools = sandbox.agent_loop.RepoTools("/nonexistent")
        started = _time.monotonic()
        out = sandbox.agent_loop.run_agent(sandbox.invoke_model, "m", "p", tools, time_limit_s=1.5)
        assert _time.monotonic() - started < 4
        assert "during a model call" in out["error"]

    def test_run_task_passes_the_deadline_to_the_loop(self, monkeypatch):
        seen = {}

        def fake_loop(invoke, model, prompt, tools, time_limit_s=None):
            seen["limit"] = time_limit_s
            return {"report": "ok", "turns": 1, "tool_calls": 0}

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(sandbox.time, "time", lambda: 1_000_500.0)
        monkeypatch.setattr(sandbox.agent_loop, "run_agent", fake_loop)
        sandbox.run_task({"repo": "org/repo", "model": "m", "deadline": "1000900"})
        assert seen["limit"] == 400 - sandbox.RESULT_MARGIN_S

    def test_run_task_drives_the_tool_loop(self, monkeypatch):
        calls = []

        def fake_invoke(model, fields, timeout=None):
            calls.append(json.loads(json.dumps(fields)))  # a snapshot; the loop keeps appending
            if len(calls) == 1:
                return {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t1", "name": "list_dir", "input": {}}],
                }
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "all read"}]}

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(sandbox, "invoke_model", fake_invoke)
        monkeypatch.setattr(sandbox.agent_loop.RepoTools, "run", lambda self, name, args: "README.md")
        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert result["report"] == "all read"
        assert (result["turns"], result["tool_calls"]) == (2, 1)
        assert calls[1]["messages"][-1]["content"][0]["content"] == "README.md"

    def test_a_budget_exhausted_by_the_loop_is_reported(self, monkeypatch):
        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(
            sandbox.agent_loop,
            "run_agent",
            lambda invoke, model, prompt, tools, **kw: {
                "report": "partial",
                "turns": 24,
                "tool_calls": 40,
                "error": "turn limit",
            },
        )
        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert result["report"] == "partial"
        assert result["errors"]["agent"] == "turn limit"

    def test_a_deeply_nested_model_response_is_an_error_not_a_crash(self, monkeypatch):
        """json.loads raises RecursionError on deep enough nesting; that must become an
        error in the result, not a pod that exits without one."""

        def fake_invoke(model, fields, timeout=None):
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

        monkeypatch.setattr(sandbox, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(sandbox, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(sandbox, "invoke_model", fake_invoke)
        result = sandbox.run_task({"repo": "org/repo", "model": "m"})
        assert "recursion" in result["errors"]["bedrock"]
