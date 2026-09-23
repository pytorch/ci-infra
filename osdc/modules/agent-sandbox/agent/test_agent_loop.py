"""The agent loop and its read-only tools, against a real git repository."""

from __future__ import annotations

import copy
import os
import subprocess

import agent_loop
import pytest
from agent_loop import RepoTools


@pytest.fixture
def repo(tmp_path):
    """A committed repository whose working tree then diverges, so a test can tell
    reads-through-git from reads-of-the-filesystem."""
    path = tmp_path / "repo"
    (path / "src").mkdir(parents=True)
    (path / "README.md").write_text("hello\nworld\n")
    (path / "src" / "main.py").write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env={**os.environ, **env})
    (path / "README.md").write_text("uncommitted\n")
    (path / "untracked.txt").write_text("not in git\n")
    return RepoTools(str(path))


def _repo_with(tmp_path, files: dict[str, str]) -> RepoTools:
    path = tmp_path / "r2"
    path.mkdir()
    for name, content in files.items():
        (path / name).write_text(content)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env={**os.environ, **env})
    return RepoTools(str(path))


class TestTools:
    def test_list_dir_marks_directories(self, repo):
        assert repo.list_dir() == "README.md\nsrc/"
        assert repo.list_dir("src") == "main.py"
        assert repo.list_dir("./src/") == "main.py"

    def test_read_file_reads_the_commit_not_the_working_tree(self, repo):
        assert "1: hello" in repo.read_file("README.md")
        assert "uncommitted" not in repo.read_file("README.md")

    def test_read_file_pages_by_line(self, repo):
        out = repo.read_file("src/main.py", start_line=3, end_line=4)
        assert out.splitlines()[1:] == ["3: line 3", "4: line 4"]
        assert "(at least 4 lines)" in out, "reading stops at end_line"

    def test_search_finds_matches_with_paths_and_lines(self, repo):
        assert repo.search("line 7") == "src/main.py:7:line 7"
        assert repo.search("hello", "src") == "no matches"

    def test_a_long_read_is_cut_and_says_how_to_page(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 30)
        out = repo.read_file("src/main.py")
        assert "output budget reached after line" in out
        assert "page with start_line" in out
        assert "5: line 5" in repo.read_file("src/main.py", start_line=5, end_line=5)

    def test_one_enormous_line_is_cut_not_read_whole(self, tmp_path, monkeypatch):
        tools = _repo_with(tmp_path, {"min.js": "x" * 50_000 + "\nsecond\n"})
        monkeypatch.setattr(agent_loop, "MAX_LINE_BYTES", 1000)
        out = tools.read_file("min.js")
        assert "1: " + "x" * 1000 + " [line cut]" in out
        assert "2: second" in out
        assert "(2 lines)" in out

    def test_a_short_read_of_a_huge_file_stops_early(self, tmp_path):
        tools = _repo_with(tmp_path, {"big.txt": "row\n" * 200_000})
        assert "(at least 1 lines)" in tools.read_file("big.txt", start_line=1, end_line=1)
        assert "(200000 lines)" in tools.read_file("big.txt", start_line=199_999)

    def test_a_line_that_escapes_past_the_budget_is_shown_in_part(self, tmp_path):
        path = tmp_path / "r3"
        path.mkdir()
        (path / "bin.txt").write_bytes(b"\xff" * 8192 + b"\nnext\n")
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env={**os.environ, **env})
        out = RepoTools(str(path)).read_file("bin.txt", start_line=1, end_line=1)
        assert "1: \\xff" in out
        assert "[line cut]" in out
        assert len(out.encode()) <= agent_loop.MAX_TOOL_OUTPUT_BYTES + 200

    def test_a_read_that_runs_out_of_time_says_so(self, repo, monkeypatch):
        ticks = iter([0.0] + [10_000.0] * 10)
        monkeypatch.setattr(agent_loop.time, "monotonic", lambda: next(ticks))
        assert "timed out" in repo.read_file("src/main.py")

    def test_search_output_is_bounded_after_decoding(self, tmp_path, monkeypatch):
        path = tmp_path / "r4"
        path.mkdir()
        (path / "a.txt").write_bytes(b"hit" + b"\xff" * 3000 + b"\n")
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env={**os.environ, **env})
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 4000)
        out = RepoTools(str(path)).search("hit", path=None)
        assert len(out.encode()) <= 4000 + len("\n[output truncated]")

    def test_a_non_utf8_name_is_marked_not_aliased(self, tmp_path):
        path = tmp_path / "r5"
        path.mkdir()
        os.close(os.open(os.fsencode(str(path)) + b"/\xff.txt", os.O_CREAT | os.O_WRONLY))
        (path / "\\xff.txt").write_text("literal\n")
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env={**os.environ, **env})
        listing = RepoTools(str(path)).list_dir().split("\n")
        assert "\\xff.txt" in listing
        assert "\\xff.txt [non-UTF-8 name; not readable with these tools]" in listing

    def test_git_stderr_is_bounded(self, repo):
        out = repo.run("search", {"pattern": "("})
        assert out.startswith("error: search failed")
        assert len(out) < 700

    def test_whitespace_and_pattern_characters_are_part_of_the_name(self, tmp_path):
        tools = _repo_with(
            tmp_path, {" spaced.txt": "inner\n", "spaced.txt": "outer\n", "[a]b.txt": "hit\n", "ab.txt": "hit\n"}
        )
        assert "1: inner" in tools.read_file(" spaced.txt")
        assert tools.search("hit", "[a]b.txt") == "[a]b.txt:1:hit"

    def test_a_long_listing_is_cut_with_a_marker_and_no_partial_name(self, tmp_path, monkeypatch):
        tools = _repo_with(tmp_path, {f"file_{i:05d}.txt": "" for i in range(500)})
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 2000)
        lines = tools.list_dir().split("\n")
        assert lines[-1].startswith("[listing cut")
        assert all(line.startswith("file_") and line.endswith(".txt") for line in lines[:-1])

    def test_search_says_when_the_per_file_limit_hid_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_SEARCH_MATCHES", 3)
        tools = _repo_with(tmp_path, {"a.txt": "hit\n" * 10})
        out = tools.search("hit")
        assert out.count("a.txt:") == 3
        assert "at most 3 matches per file" in out

    def test_search_says_when_the_output_budget_hid_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 200)
        tools = _repo_with(tmp_path, {f"f{i}.txt": "hit\n" for i in range(100)})
        assert "more matches not shown" in tools.search("hit")

    def test_output_is_clipped(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 20)
        assert agent_loop._clip("x" * 100).endswith("[output truncated]")

    @pytest.mark.parametrize(
        ("name", "args", "message"),
        [
            ("read_file", {"path": "../etc/passwd"}, "invalid path"),
            ("read_file", {"path": "/etc/passwd"}, "no such file"),
            ("read_file", {"path": "untracked.txt"}, "no such file"),
            ("read_file", {"path": "src"}, "no such file"),
            ("read_file", {"path": ""}, "invalid path"),
            ("read_file", {"path": 5}, "must be a string"),
            ("read_file", {"path": "README.md", "start_line": 0}, "start_line"),
            ("read_file", {"path": "README.md", "start_line": 3, "end_line": 2}, "end_line"),
            ("list_dir", {"path": "README.md"}, "no such directory"),
            ("list_dir", {"path": "a/../b"}, "invalid path"),
            ("search", {"pattern": ""}, "pattern"),
            ("search", {"pattern": "("}, "search failed"),
            ("search", {"pattern": "x", "extra": 1}, "unexpected arguments"),
            ("delete_everything", {}, "unknown tool"),
            ("search", {"pattern": "a\x00b"}, "without NUL"),
            ("search", {"pattern": "\ud800"}, "not valid text"),
            ("read_file", {"path": "a\x00b"}, "path must be"),
            ("read_file", {"path": "\udcff.txt"}, "not valid text"),
        ],
    )
    def test_bad_calls_come_back_as_text_not_exceptions(self, repo, name, args, message):
        assert message in repo.run(name, args)

    def test_an_unexpected_os_error_still_comes_back_as_text(self, repo, monkeypatch):
        def broken(self, args, out):
            raise OSError("no space left on device")

        monkeypatch.setattr(RepoTools, "_git", broken)
        assert repo.run("list_dir", {}) == "error: list_dir failed: no space left on device"

    def test_non_object_input_is_an_error(self, repo):
        assert "must be an object" in repo.run("list_dir", "src")

    def test_a_timeout_is_an_error_not_a_crash(self, repo, monkeypatch):
        def slow(self, args, out):
            raise subprocess.TimeoutExpired("git", 60)

        monkeypatch.setattr(RepoTools, "_git", slow)
        assert repo.run("list_dir", {}) == "error: list_dir timed out"

    def test_a_symlink_is_read_as_its_target_path_not_followed(self, tmp_path):
        path = tmp_path / "r"
        path.mkdir()
        os.symlink("/etc/hostname", path / "link")
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "l"], check=True, env={**os.environ, **env})
        assert "1: /etc/hostname" in RepoTools(str(path)).read_file("link")


def scripted(*responses):
    """An `invoke` that returns the given responses in order and records requests."""
    seen = []

    def invoke(model, fields, timeout=None):
        invoke.timeouts.append(timeout)
        seen.append(copy.deepcopy(fields))  # the loop keeps appending to its messages
        return responses[len(seen) - 1]

    invoke.seen = seen
    invoke.timeouts = []
    return invoke


def tool_use(name, arguments, id_="t"):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": id_, "name": name, "input": arguments}]}


def answer(text):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


class TestLoop:
    def test_answers_after_reading(self, repo):
        invoke = scripted(tool_use("read_file", {"path": "README.md"}), answer("It says hello."))
        out = agent_loop.run_agent(invoke, "m", "What does README say?", repo)
        assert out == {"report": "It says hello.", "turns": 2, "tool_calls": 1}
        result = invoke.seen[1]["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "t"
        assert "1: hello" in result["content"]
        assert invoke.seen[0]["tools"] == agent_loop.TOOLS
        assert invoke.seen[0]["system"] == agent_loop.SYSTEM

    def test_an_immediate_answer_takes_one_turn(self, repo):
        assert agent_loop.run_agent(scripted(answer("done")), "m", "p", repo)["turns"] == 1

    def test_several_tool_calls_in_one_turn_all_run(self, repo):
        both = {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Reading two files."},
                {"type": "tool_use", "id": "a", "name": "list_dir", "input": {}},
                {"type": "tool_use", "id": "b", "name": "search", "input": {"pattern": "world"}},
            ],
        }
        invoke = scripted(both, answer("ok"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["tool_calls"] == 2
        assert [r["tool_use_id"] for r in invoke.seen[1]["messages"][-1]["content"]] == ["a", "b"]

    def test_the_turn_limit_ends_the_loop_with_an_error(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TURNS", 3)
        invoke = scripted(*[tool_use("list_dir", {}) for _ in range(3)])
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["turns"] == 3
        assert "turn limit" in out["error"]

    def test_the_time_limit_ends_the_loop_with_an_error(self, repo):
        clock = iter([0.0, 1.0, 10_000.0, 10_001.0]).__next__  # start, turn 1, tool call, turn 2
        out = agent_loop.run_agent(scripted(tool_use("list_dir", {})), "m", "p", repo, clock=clock)
        assert (out["turns"], out["tool_calls"]) == (1, 0)
        assert "time limit" in out["error"]

    def test_a_malformed_response_is_an_error_not_an_answer(self, repo):
        out = agent_loop.run_agent(scripted({"stop_reason": "tool_use", "content": ["junk", None]}), "m", "p", repo)
        assert "unexpected model response" in out["error"]

    def test_valid_text_beside_a_malformed_block_is_still_an_error(self, repo):
        mixed = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "No findings."}, "junk"]}
        assert "malformed" in agent_loop.run_agent(scripted(mixed), "m", "p", repo)["error"]

    @pytest.mark.parametrize(
        "block",
        [
            {"type": "text"},
            {"type": "tool_use", "name": "list_dir", "input": {}},
            {"type": "tool_use", "id": "", "name": "list_dir", "input": {}},
            {"type": "tool_use", "id": "x", "name": "list_dir", "input": "src"},
            {"type": "thinking"},
            {"type": "image", "source": {}},
        ],
        ids=[
            "text-without-text",
            "tool-without-id",
            "tool-empty-id",
            "tool-input-not-object",
            "thinking-without-text",
            "unknown-type",
        ],
    )
    def test_a_block_missing_required_fields_is_an_error(self, repo, block):
        response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "No findings."}, block]}
        assert "malformed" in agent_loop.run_agent(scripted(response), "m", "p", repo)["error"]

    def test_duplicate_tool_ids_are_an_error(self, repo):
        dup = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "x", "name": "list_dir", "input": {}} for _ in range(2)],
        }
        assert "malformed" in agent_loop.run_agent(scripted(dup), "m", "p", repo)["error"]

    def test_thinking_blocks_are_accepted_and_replayed(self, repo):
        """Opus 5.5 returns thinking blocks beside tool calls; they must go back unchanged."""
        thinking = {"type": "thinking", "thinking": "Let me look.", "signature": "sig"}
        first = {
            "stop_reason": "tool_use",
            "content": [thinking, {"type": "tool_use", "id": "t", "name": "list_dir", "input": {}}],
        }
        invoke = scripted(first, answer("done"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["report"] == "done"
        assert invoke.seen[1]["messages"][1]["content"][0] == thinking

    def test_a_tool_use_without_uses_is_an_error(self, repo):
        empty = {"stop_reason": "tool_use", "content": [{"type": "text", "text": "hm"}]}
        assert "unexpected model response" in agent_loop.run_agent(scripted(empty), "m", "p", repo)["error"]

    def test_a_truncated_answer_is_an_error(self, repo):
        cut = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Finding 1: ..."}]}
        out = agent_loop.run_agent(scripted(cut), "m", "p", repo)
        assert out["report"] == "Finding 1: ..."
        assert "cut at" in out["error"]

    def test_an_end_turn_with_pending_tool_calls_is_an_error(self, repo):
        mixed = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "Reading the file now."},
                {"type": "tool_use", "id": "x", "name": "list_dir", "input": {}},
            ],
        }
        assert "pending" in agent_loop.run_agent(scripted(mixed), "m", "p", repo)["error"]

    def test_an_empty_answer_is_an_error(self, repo):
        assert "no answer" in agent_loop.run_agent(scripted(answer("  ")), "m", "p", repo)["error"]

    def test_the_tool_budget_stops_running_tools(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 1)
        many = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": str(i), "name": "list_dir", "input": {}} for i in range(3)],
        }
        invoke = scripted(many, answer("ok"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        results = invoke.seen[1]["messages"][-1]["content"]
        assert out["tool_calls"] == 1
        assert [r["content"].startswith("error: tool budget") for r in results] == [False, True, True]

    def test_the_transcript_budget_stops_running_tools(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TRANSCRIPT_TOOL_BYTES", 1)
        invoke = scripted(tool_use("list_dir", {}), tool_use("list_dir", {}), answer("ok"))
        agent_loop.run_agent(invoke, "m", "p", repo)
        assert invoke.seen[2]["messages"][-1]["content"][0]["content"].startswith("error: tool budget")

    def test_the_model_call_timeout_is_capped_by_the_deadline(self, repo):
        clock = iter([0.0, 590.0]).__next__
        invoke = scripted(answer("ok"))
        agent_loop.run_agent(invoke, "m", "p", repo, clock=clock)
        assert invoke.timeouts == [10.0]

    def test_less_than_a_second_left_is_the_time_limit(self, repo):
        clock = iter([0.0, 599.5]).__next__
        out = agent_loop.run_agent(scripted(answer("ok")), "m", "p", repo, clock=clock)
        assert "time limit" in out["error"]

    def test_an_oversized_request_is_never_sent(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_REQUEST_BYTES", 2000)
        big = {"type": "thinking", "thinking": "x" * 5000, "signature": "s"}
        first = {
            "stop_reason": "tool_use",
            "content": [big, {"type": "tool_use", "id": "t", "name": "list_dir", "input": {}}],
        }
        invoke = scripted(first, answer("never"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert len(invoke.seen) == 1
        assert "grew past" in out["error"]

    def test_scratch_files_are_named_and_cleaned_up(self, repo):
        repo.read_file("README.md")
        assert os.path.isdir(repo.scratch)
        assert os.listdir(repo.scratch) == []

    def test_the_deadline_is_checked_between_tool_calls(self, repo):
        both = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": str(i), "name": "list_dir", "input": {}} for i in range(2)],
        }
        clock = iter([0.0, 1.0, 2.0, 10_000.0, 10_001.0]).__next__
        invoke = scripted(both, answer("ok"))
        out = agent_loop.run_agent(invoke, "m", "p", repo, clock=clock)
        assert out["tool_calls"] == 1
        assert "time limit" in out["error"]
