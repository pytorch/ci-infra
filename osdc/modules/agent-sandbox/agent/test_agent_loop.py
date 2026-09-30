"""The agent loop and its read-only tools, against a real git repository."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time

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

    @pytest.mark.parametrize("name", ["café.txt", 'q"uote.txt', "back\\slash.txt"])
    def test_search_names_a_path_the_way_the_other_tools_read_it(self, tmp_path, name):
        """git quotes an unusual name by default (`"caf\\303\\251.txt"`); a path copied
        from a match must open with read_file and match list_dir."""
        tools = _repo_with(tmp_path, {name: "needle\n"})
        assert tools.search("needle") == f"{name}:1:needle"
        assert name in tools.list_dir("").split("\n")
        assert tools.read_file(name).endswith("1: needle")

    def test_an_escaped_long_line_keeps_its_location_and_the_matches_after_it(self, tmp_path):
        """backslashreplace writes an invalid byte as four characters, so 8 KiB of raw
        Latin-1 renders to about 32 KiB; the rendered line is cut, not the whole result."""
        path = tmp_path / "r5"
        path.mkdir()
        (path / "a.txt").write_bytes(b"hit" + b"\xe9" * 9000 + b"\nhit two\n")
        (path / "b.txt").write_bytes(b"hit three\n")
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "c"], check=True, env={**os.environ, **env})
        out = RepoTools(str(path)).search("hit").split("\n")
        assert out[0].startswith("a.txt:1:hit\\xe9")
        assert out[0].endswith("[line cut]")
        assert out[1:3] == ["a.txt:2:hit two", "b.txt:1:hit three"]

    def test_a_long_matching_line_keeps_its_location_and_the_matches_after_it(self, tmp_path):
        tools = _repo_with(tmp_path, {"a.min.js": "needle" + "x" * 100_000 + "\n", "b.txt": "needle\n"})
        out = tools.search("needle").split("\n")
        assert out[0].startswith("a.min.js:1:needle")
        assert out[0].endswith("[line cut]")
        assert len(out[0].encode()) < agent_loop.MAX_LINE_BYTES + 64
        assert out[1] == "b.txt:1:needle"

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

    def test_calling_tools_past_the_turn_budget_ends_the_loop_with_an_error(self, repo, monkeypatch):
        """The next-to-last turn's reads are refused so the last can answer; a model that
        asks for tools again on the last turn ends the run with an error."""
        monkeypatch.setattr(agent_loop, "MAX_TURNS", 3)
        invoke = scripted(*[tool_use("list_dir", {}) for _ in range(3)])
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["turns"] == 3
        assert out["error"] == "the model kept calling tools after its budget ran out"
        assert out["tools_refused"] == "3 turns"

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

    def test_a_shorter_time_limit_caps_the_deadline(self, repo):
        clock = iter([0.0, 290.0]).__next__
        invoke = scripted(answer("ok"))
        agent_loop.run_agent(invoke, "m", "p", repo, clock=clock, time_limit_s=300)
        assert invoke.timeouts == [10.0]

    def test_a_longer_time_limit_never_exceeds_the_loop_deadline(self, repo):
        clock = iter([0.0, 590.0]).__next__
        invoke = scripted(answer("ok"))
        agent_loop.run_agent(invoke, "m", "p", repo, clock=clock, time_limit_s=10_000)
        assert invoke.timeouts == [10.0]

    def test_no_time_left_is_the_time_limit_before_any_call(self, repo):
        invoke = scripted(answer("never"))
        out = agent_loop.run_agent(invoke, "m", "p", repo, time_limit_s=0)
        assert invoke.seen == []
        assert out["error"] == "time limit of 0s reached"

    def test_a_nearly_full_context_stops_the_tools_and_asks_for_an_answer(self, repo):
        full = agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS - 1000
        first = dict(tool_use("list_dir", {}), usage={"input_tokens": full, "output_tokens": 10})
        invoke = scripted(first, answer("ok"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["tool_calls"] == 0
        assert invoke.seen[1]["messages"][-1]["content"][0]["content"].startswith("error: tool budget")

    def test_cached_tokens_count_toward_the_context(self, repo):
        half = (agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS) // 2
        usage = {"input_tokens": 10, "cache_read_input_tokens": half, "cache_creation_input_tokens": half}
        invoke = scripted(dict(tool_use("list_dir", {}), usage=usage), answer("ok"))
        assert agent_loop.run_agent(invoke, "m", "p", repo)["tool_calls"] == 0

    def test_room_in_the_context_is_spent_by_each_result_in_a_turn(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_OUTPUT_BYTES", 1000)
        # Room for one worst-case result (1000 + 1024 bytes) plus less than the framing
        # (256 bytes) that any result adds, so the second call no longer fits.
        room_tokens = (1000 + 1024 + 100) // agent_loop.MIN_BYTES_PER_TOKEN
        used = agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS - room_tokens
        both = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": str(i), "name": "list_dir", "input": {}} for i in range(2)],
            "usage": {"input_tokens": used, "output_tokens": 0},
        }
        invoke = scripted(both, answer("ok"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["tool_calls"] == 1
        results = invoke.seen[1]["messages"][-1]["content"]
        assert results[1]["content"].startswith("error: tool budget")

    def test_tools_requested_after_a_refusal_end_the_loop(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 1)
        invoke = scripted(*[tool_use("list_dir", {}) for _ in range(5)])
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert len(invoke.seen) == 3  # ran, refused, then asked again
        assert out["tool_calls"] == 1
        assert out["error"] == "the model kept calling tools after its budget ran out"

    def test_an_answer_after_a_refusal_is_accepted_and_says_what_was_refused(self, repo, monkeypatch):
        """The answer stands, but a caller can tell it from one written after complete
        reading."""
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 0)
        invoke = scripted(tool_use("list_dir", {}), answer("from what I had"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out == {"report": "from what I had", "turns": 2, "tool_calls": 0, "tools_refused": "0 tool calls"}

    def test_a_full_context_is_named_as_the_refusal(self, repo):
        full = {
            **tool_use("list_dir", {}),
            "usage": {"input_tokens": agent_loop.CONTEXT_WINDOW_TOKENS, "output_tokens": 0},
        }
        out = agent_loop.run_agent(scripted(full, answer("ok")), "m", "p", repo)
        assert out["tools_refused"] == "context window"
        assert "error" not in out

    def test_a_complete_run_names_no_refusal(self, repo):
        out = agent_loop.run_agent(scripted(tool_use("list_dir", {}), answer("ok")), "m", "p", repo)
        assert "tools_refused" not in out

    def test_dense_content_cannot_overflow_the_window_in_one_turn(self, repo, monkeypatch):
        """Five worst-case results in one turn, at one token per byte, must still fit."""
        monkeypatch.setattr(
            agent_loop.RepoTools, "run", lambda self, name, args: "x" * agent_loop.MAX_TOOL_OUTPUT_BYTES
        )
        used = 95_000
        many = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": str(i), "name": "list_dir", "input": {}} for i in range(5)],
            "usage": {"input_tokens": used, "output_tokens": 0},
        }
        invoke = scripted(many, answer("ok"))
        agent_loop.run_agent(invoke, "m", "p", repo)
        added = sum(len(r["content"].encode()) for r in invoke.seen[1]["messages"][-1]["content"])
        assert used + added <= agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS

    def test_a_model_call_that_outlives_the_deadline_is_abandoned(self, repo):
        def slow(model, fields, timeout=None):
            time.sleep(5)
            return answer("too late")

        started = time.monotonic()
        out = agent_loop.run_agent(slow, "m", "p", repo, time_limit_s=1.5)
        assert time.monotonic() - started < 4
        assert out["error"] == "time limit of 1s reached during a model call"

    def test_a_failed_model_call_keeps_the_progress_so_far(self, repo):
        """The error ends the run, but the turns and tool calls before it are reported."""
        calls = []

        def invoke(model, fields, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                return tool_use("list_dir", {})
            raise ValueError("bad response")

        out = agent_loop.run_agent(invoke, "m", "p", repo, sleep=lambda s: None)
        assert out == {"report": "", "turns": 1, "tool_calls": 1, "model_error": "bad response"}
        assert len(calls) == 2, "a malformed body is not retried"

    def test_a_throttled_call_is_retried(self, repo):
        slept = []
        answers = iter([agent_loop.ModelHTTPError("HTTP 429 (ThrottlingException)", 429), answer("ok")])

        def invoke(model, fields, timeout=None):
            item = next(answers)
            if isinstance(item, Exception):
                raise item
            return item

        out = agent_loop.run_agent(invoke, "m", "p", repo, sleep=slept.append)
        assert out == {"report": "ok", "turns": 1, "tool_calls": 0}
        assert slept == [agent_loop.RETRY_BASE_S]

    @pytest.mark.parametrize(
        "error",
        [agent_loop.ModelHTTPError("HTTP 503", 503), ConnectionResetError("reset")],
        ids=["5xx", "connection"],
    )
    def test_transient_failures_are_retried_a_bounded_number_of_times(self, repo, error):
        calls, slept = [], []

        def invoke(model, fields, timeout=None):
            calls.append(1)
            raise error

        out = agent_loop.run_agent(invoke, "m", "p", repo, sleep=slept.append)
        assert len(calls) == agent_loop.MAX_CALL_RETRIES + 1
        assert slept == [agent_loop.RETRY_BASE_S * 2**i for i in range(agent_loop.MAX_CALL_RETRIES)]
        assert out["model_error"] == str(error)
        assert out["turns"] == 0

    def test_a_client_error_is_not_retried(self, repo):
        calls = []

        def invoke(model, fields, timeout=None):
            calls.append(1)
            raise agent_loop.ModelHTTPError("HTTP 400 (ValidationException)", 400)

        out = agent_loop.run_agent(invoke, "m", "p", repo, sleep=lambda s: pytest.fail("must not wait"))
        assert len(calls) == 1
        assert out["model_error"] == "HTTP 400 (ValidationException)"

    def test_a_sleep_that_overruns_does_not_start_a_retry_it_cannot_finish(self, repo):
        """Checked before sleeping and again after: a sleep that overran leaves less time
        than the retry needs, and the original error stands."""
        calls = []
        ticks = iter([0.0, 0.0, 0.0, 580.0])

        def clock():
            return next(ticks, 580.0)

        def invoke(model, fields, timeout=None):
            calls.append(1)
            raise agent_loop.ModelHTTPError("HTTP 429", 429)

        out = agent_loop.run_agent(invoke, "m", "p", repo, clock=clock, time_limit_s=600, sleep=lambda s: None)
        assert len(calls) == 1
        assert out["model_error"] == "HTTP 429"

    def test_one_call_is_bounded_by_its_own_limit_and_then_retried(self, repo, monkeypatch):
        """A call that hangs past MAX_CALL_S with loop time left is abandoned as a timeout
        and retried; it does not spend the whole loop."""
        monkeypatch.setattr(agent_loop, "MAX_CALL_S", 0.2)
        calls = []

        def hang(model, fields, timeout=None):
            calls.append(timeout)
            time.sleep(5)

        started = time.monotonic()
        out = agent_loop.run_agent(hang, "m", "p", repo, sleep=lambda s: None)
        assert time.monotonic() - started < 3
        assert len(calls) == agent_loop.MAX_CALL_RETRIES + 1
        assert all(t == 0.2 for t in calls), "the call's own timeout is the per-call limit"
        assert out["model_error"] == "model call still running after 0.2s"

    def test_no_retry_starts_without_time_for_a_call_to_finish(self, repo):
        """A retry that could not finish before the deadline only delays the report."""
        calls = []

        def invoke(model, fields, timeout=None):
            calls.append(1)
            raise agent_loop.ModelHTTPError("HTTP 429", 429)

        limit = agent_loop.MIN_RETRY_REMAINING_S + agent_loop.RETRY_BASE_S - 1
        out = agent_loop.run_agent(invoke, "m", "p", repo, time_limit_s=limit, sleep=lambda s: pytest.fail("no wait"))
        assert len(calls) == 1
        assert out["model_error"] == "HTTP 429"

    def test_a_first_prompt_too_large_for_the_window_is_never_sent(self, repo):
        invoke = scripted(answer("never"))
        out = agent_loop.run_agent(invoke, "m", "x" * (agent_loop.prompt_budget_bytes() + 1), repo)
        assert invoke.seen == []
        assert out["error"] == "the prompt is too large for the model's context window"

    def test_without_usage_the_context_is_estimated_high(self, repo):
        huge = {"type": "text", "text": "x" * (2 * agent_loop.CONTEXT_WINDOW_TOKENS)}
        first = {
            "stop_reason": "tool_use",
            "content": [huge, {"type": "tool_use", "id": "t", "name": "list_dir", "input": {}}],
        }
        invoke = scripted(first, answer("ok"))
        assert agent_loop.run_agent(invoke, "m", "p", repo)["tool_calls"] == 0

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

    def test_running_short_of_time_leaves_a_turn_to_answer(self, repo):
        """Time is refused while the answer reserve is still left, like the other budgets:
        the model answers from what it read, and the answer carries tools_refused."""
        ticks = iter([0.0, 1.0, 590.0, 591.0])

        def clock():
            return next(ticks, 591.0)

        invoke = scripted(tool_use("list_dir", {}), answer("from what I read"))
        out = agent_loop.run_agent(invoke, "m", "p", repo, clock=clock, time_limit_s=600)
        assert out == {"report": "from what I read", "turns": 2, "tool_calls": 0, "tools_refused": "time"}
        assert invoke.seen[1]["messages"][-1]["content"][0]["content"].startswith("error: tool budget exhausted")

    def test_a_read_is_bounded_by_the_answer_reserve(self, repo, monkeypatch):
        """Admitted with a little more than the reserve left, a read may not spend it."""
        ticks = iter([0.0, 1.0, 600 - 70.0])

        def clock():
            return next(ticks, 600 - 70.0)

        timeouts = []

        def run(self, name, args):
            timeouts.append(self.timeout)
            return "README.md"

        monkeypatch.setattr(agent_loop.RepoTools, "run", run)
        agent_loop.run_agent(scripted(tool_use("list_dir", {}), answer("ok")), "m", "p", repo, clock=clock)
        assert timeouts == [10], "70 s left less the 60 s reserve"

    def test_the_last_turn_is_kept_for_the_answer(self, repo):
        """A model that reads on every turn is refused on the next-to-last one, so the
        last turn can still answer instead of ending at the turn limit."""
        reads = [tool_use("list_dir", {}, id_=f"r{i}") for i in range(agent_loop.MAX_TURNS - 1)]
        invoke = scripted(*reads, answer("from what I read"))
        out = agent_loop.run_agent(invoke, "m", "p", repo)
        assert out["report"] == "from what I read"
        assert "error" not in out
        assert out["tools_refused"] == f"{agent_loop.MAX_TURNS} turns"
        assert out["tool_calls"] == agent_loop.MAX_TURNS - 2

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


ALLOWED = [
    {"effect": "pr_comment", "max_bytes": 50},
    {"effect": "check_run", "max_bytes": 100, "conclusions": ["neutral"]},
]


class TestProposals:
    def test_proposing_stays_open_after_the_read_budget_runs_out(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 0)
        tools = RepoTools(repo.dest, ALLOWED)
        propose = tool_use("propose_effect", {"effect": "pr_comment", "body": "LGTM"}, id_="p")
        invoke = scripted(tool_use("list_dir", {}), propose, answer("done"))
        out = agent_loop.run_agent(invoke, "m", "p", tools)
        assert "error" not in out
        assert tools.proposals == [{"effect": "pr_comment", "body": "LGTM"}]

    def test_reading_again_after_the_budget_ends_the_loop_even_with_a_proposal(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 0)
        tools = RepoTools(repo.dest, ALLOWED)
        both = {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "p",
                    "name": "propose_effect",
                    "input": {"effect": "pr_comment", "body": "x"},
                },
                {"type": "tool_use", "id": "r", "name": "list_dir", "input": {}},
            ],
        }
        out = agent_loop.run_agent(scripted(tool_use("list_dir", {}), both), "m", "p", tools)
        assert out["error"] == "the model kept calling tools after its budget ran out"
        assert tools.proposals == []

    def test_the_first_prompt_budget_counts_the_propose_tool(self):
        tools = [*agent_loop.TOOLS, agent_loop.PROPOSE_EFFECT]
        fixed = len(json.dumps({"system": agent_loop.SYSTEM, "tools": tools}).encode()) + 1024
        whole = (agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS) * agent_loop.MIN_BYTES_PER_TOKEN
        assert agent_loop.prompt_budget_bytes() == whole - fixed

    def test_a_full_window_is_announced_before_the_last_proposal_turn(self, repo):
        """The window filled without a read being refused, so the model was never told.
        The note on its proposal result tells it; its next turn may still propose, and
        only a turn after that ends the loop."""
        tools = RepoTools(repo.dest, ALLOWED)
        full = agent_loop.CONTEXT_WINDOW_TOKENS - agent_loop.CONTEXT_RESERVE_TOKENS - 1000
        usage = {"input_tokens": full, "output_tokens": 0}
        first = dict(tool_use("propose_effect", {"effect": "pr_comment", "body": "a"}, id_="p1"), usage=usage)
        second = dict(tool_use("propose_effect", {"effect": "pr_comment", "body": "b"}, id_="p2"), usage=usage)
        third = dict(tool_use("propose_effect", {"effect": "pr_comment", "body": "c"}, id_="p3"), usage=usage)
        invoke = scripted(first, second, third, answer("never"))
        out = agent_loop.run_agent(invoke, "m", "p", tools)
        assert agent_loop.BUDGET_SPENT_NOTE in invoke.seen[1]["messages"][-1]["content"][-1]["content"]
        assert len(invoke.seen) == 3
        assert out["error"] == "the model kept calling tools after its budget ran out"
        assert [p["body"] for p in tools.proposals] == ["a", "b"]
        assert out["tools_refused"] == "context window"

    def test_proposals_split_over_two_turns_survive_a_budget_spent_without_a_refusal(self, repo, monkeypatch):
        """The review's case: the last allowed read spends the budget, the model proposes
        its comment, then its check run in the next turn. Both are kept."""
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 1)
        tools = RepoTools(repo.dest, ALLOWED)
        comment = tool_use("propose_effect", {"effect": "pr_comment", "body": "LGTM"}, id_="p1")
        check = tool_use("propose_effect", {"effect": "check_run", "body": "ok", "conclusion": "neutral"}, id_="p2")
        invoke = scripted(tool_use("list_dir", {}), comment, check, answer("done"))
        out = agent_loop.run_agent(invoke, "m", "p", tools)
        assert "error" not in out
        assert [p["effect"] for p in tools.proposals] == ["pr_comment", "check_run"]
        assert out["tools_refused"] == "1 tool calls"

    def test_only_one_turn_of_proposals_is_accepted_after_the_budget_runs_out(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 0)
        tools = RepoTools(repo.dest, ALLOWED)
        first = tool_use("propose_effect", {"effect": "pr_comment", "body": "a"}, id_="p1")
        again = tool_use("propose_effect", {"effect": "pr_comment", "body": "b"}, id_="p2")
        invoke = scripted(tool_use("list_dir", {}), first, again, answer("never"))
        out = agent_loop.run_agent(invoke, "m", "p", tools)
        assert len(invoke.seen) == 3
        assert out["error"] == "the model kept calling tools after its budget ran out"

    def test_a_refused_read_says_one_proposal_turn_is_left(self, repo, monkeypatch):
        """The refusal is the only thing the model sees; with effects allowed it must also
        say proposing is open for one more turn, or a review split over two is lost."""
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 1)
        tools = RepoTools(repo.dest, ALLOWED)
        invoke = scripted(tool_use("list_dir", {}), tool_use("list_dir", {}, id_="r2"), answer("done"))
        agent_loop.run_agent(invoke, "m", "p", tools)
        refusal = invoke.seen[2]["messages"][-1]["content"][0]["content"]
        assert refusal.startswith("error: tool budget exhausted")
        assert agent_loop.BUDGET_SPENT_NOTE in refusal

    def test_without_effects_the_refusal_says_nothing_about_proposing(self, repo, monkeypatch):
        monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS", 0)
        invoke = scripted(tool_use("list_dir", {}), answer("done"))
        agent_loop.run_agent(invoke, "m", "p", repo)
        assert agent_loop.BUDGET_SPENT_NOTE not in invoke.seen[1]["messages"][-1]["content"][0]["content"]

    def test_the_propose_tool_lists_its_limits(self, repo):
        """In the last proposal turn a rejected call cannot be retried, so the first
        attempt must be able to be valid: conclusions and sizes are in the schema."""
        propose = RepoTools(repo.dest, ALLOWED).specs()[-1]
        assert propose["input_schema"]["properties"]["conclusion"]["enum"] == ["neutral"]
        assert "pr_comment body at most 50 bytes" in propose["description"]
        assert "check_run body at most 100 bytes" in propose["description"]
        assert "enum" not in agent_loop.PROPOSE_EFFECT["input_schema"]["properties"]["conclusion"], (
            "the shared template is not edited"
        )

    def test_the_propose_tool_is_offered_only_when_effects_are_allowed(self, repo):
        assert [t["name"] for t in repo.specs()] == ["list_dir", "read_file", "search"]
        allowed = RepoTools(repo.dest, ALLOWED)
        propose = allowed.specs()[-1]
        assert propose["name"] == "propose_effect"
        assert propose["input_schema"]["properties"]["effect"]["enum"] == ["check_run", "pr_comment"]
        assert agent_loop.PROPOSE_EFFECT["input_schema"]["properties"]["effect"]["enum"] == [], (
            "the template is not mutated"
        )

    def test_a_valid_proposal_is_recorded_not_performed(self, repo):
        tools = RepoTools(repo.dest, ALLOWED)
        assert "proposed pr_comment" in tools.run("propose_effect", {"effect": "pr_comment", "body": "LGTM"})
        assert tools.run(
            "propose_effect", {"effect": "check_run", "body": "s", "conclusion": "neutral", "title": "T"}
        ).startswith("proposed")
        assert tools.proposals == [
            {"effect": "pr_comment", "body": "LGTM"},
            {"effect": "check_run", "body": "s", "title": "T", "conclusion": "neutral"},
        ]

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ({"effect": "merge", "body": "x"}, "effect must be one of"),
            ({"effect": "pr_comment", "body": ""}, "non-empty"),
            ({"effect": "pr_comment", "body": "x" * 51}, "longer than 50"),
            ({"effect": "check_run", "body": "s", "conclusion": "success"}, "conclusion must be"),
        ],
    )
    def test_a_bad_proposal_is_an_error_the_model_can_fix(self, repo, args, message):
        tools = RepoTools(repo.dest, ALLOWED)
        assert message in tools.run("propose_effect", args)
        assert tools.proposals == []

    def test_a_title_screening_would_drop_is_refused_by_the_tool(self, repo):
        tools = RepoTools(repo.dest, ALLOWED)
        args = {"effect": "check_run", "body": "ok", "conclusion": "neutral", "title": "t" * 201}
        assert tools.run("propose_effect", args).startswith("error: title must be")
        assert tools.proposals == []

    def test_bot_commands_are_neutralised_before_the_size_check(self, repo):
        """So a body the tool accepts is exactly what screening posts, with no growth."""
        tools = RepoTools(repo.dest, ALLOWED)
        body = "@pytorchbot merge"
        assert not tools.run("propose_effect", {"effect": "pr_comment", "body": body}).startswith("error")
        assert tools.proposals[0]["body"] == "`@pytorchbot` merge"

    def test_proposals_are_capped(self, repo):
        tools = RepoTools(repo.dest, ALLOWED)
        for _ in range(agent_loop.MAX_PROPOSALS):
            tools.run("propose_effect", {"effect": "pr_comment", "body": "x"})
        assert "no more effects" in tools.run("propose_effect", {"effect": "pr_comment", "body": "x"})

    def test_without_allowed_effects_the_tool_does_not_exist(self, repo):
        assert "unknown tool" in repo.run("propose_effect", {"effect": "pr_comment", "body": "x"})

    def test_malformed_allowed_effects_are_ignored(self, repo):
        assert RepoTools(repo.dest, ["junk", {"no": "effect"}]).specs() == agent_loop.TOOLS
