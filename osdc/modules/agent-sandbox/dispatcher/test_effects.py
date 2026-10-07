"""The effect policy: what a task may propose, and what survives to the applier."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import effects
import pytest
import test_authorize
from authorize import Grant
from manifest import EffectSpec

HEAD = "c" * 40


DEFAULT_ALLOWED = (
    EffectSpec("pr_comment", 100),
    EffectSpec("check_run", name="ai-review", conclusions=frozenset({"neutral"})),
)


def a_grant(allowed=DEFAULT_ALLOWED, ref=HEAD):
    return Grant(
        caller="pytorch/ciforge",
        manifest="ciforge-pr-review",
        workflow_ref="",
        clone_repo="pytorch/pytorch",
        model="",
        task="",
        ref=ref,
        pr=0,
        effects=tuple(allowed),
    )


def screened(proposals, grant=None, head=HEAD):
    return effects.screen({"head_sha": head, "effects": proposals, "errors": {}}, grant or a_grant())


ATTRIBUTION = effects.attribution(HEAD, "ciforge-pr-review")


def test_an_allowed_comment_is_pinned_to_the_commit_and_the_cloned_repo():
    out = screened([{"effect": "pr_comment", "body": "ok"}])
    assert out["effects"] == [
        {"effect": "pr_comment", "repo": "pytorch/pytorch", "head_sha": HEAD, "body": ATTRIBUTION + "ok"}
    ]
    assert "effects" not in out["errors"]


def test_the_attribution_fits_inside_the_size_limit():
    room = 100 - len(ATTRIBUTION.encode())
    assert len(screened([{"effect": "pr_comment", "body": "x" * room}])["effects"][0]["body"].encode()) == 100
    assert screened([{"effect": "pr_comment", "body": "x" * (room + 1)}])["effects"] == []


def test_untrusted_markdown_cannot_hide_the_attribution():
    body = screened([{"effect": "pr_comment", "body": "fine\n\n<!--"}])["effects"][0]["body"]
    assert body.startswith(ATTRIBUTION)


def test_a_task_claiming_another_commit_gets_nothing():
    """The commit comes from the grant: a task reporting a different head is refused."""
    out = screened([{"effect": "pr_comment", "body": "ok"}], head="d" * 40)
    assert out["effects"] == []
    assert "requested commit" in out["errors"]["effects"]


def test_a_branch_ref_cannot_carry_effects():
    out = screened([{"effect": "pr_comment", "body": "ok"}], grant=a_grant(ref="main"))
    assert out["effects"] == []
    assert "full commit sha" in out["errors"]["effects"]


@pytest.mark.parametrize(
    "result",
    [
        {"head_sha": HEAD, "effects": [{"effect": [], "body": "x"}], "errors": {}},
        {"head_sha": HEAD, "effects": [{"effect": "merge", "body": "x"}], "errors": None},
        {"head_sha": HEAD, "effects": [{"effect": "merge", "body": "x"}], "errors": []},
        {"head_sha": HEAD, "effects": [{"effect": {"a": 1}}]},
    ],
    ids=["unhashable-kind", "errors-null", "errors-empty-list", "dict-kind"],
)
def test_malformed_results_never_raise(result):
    out = effects.screen(result, a_grant())
    assert out["effects"] == []
    assert out["errors"]["effects"]


def test_malformed_errors_are_kept_and_block_effects():
    out = effects.screen(
        {"head_sha": HEAD, "effects": [{"effect": "pr_comment", "body": "ok"}], "errors": "agent failed"}, a_grant()
    )
    assert out["effects"] == []
    assert out["errors"]["result"] == "agent failed"


@pytest.mark.parametrize("key", ["agent", "bedrock", "clone", "diff", "dispatch", "task"])
def test_a_run_with_errors_writes_nothing(key):
    result = {"head_sha": HEAD, "effects": [{"effect": "pr_comment", "body": "ok"}], "errors": {key: "x"}}
    out = effects.screen(result, a_grant())
    assert out["effects"] == []
    assert "reported errors" in out["errors"]["effects"]


def test_an_internal_error_fails_closed(monkeypatch):
    def broken(result, grant):
        raise RuntimeError("bug")

    monkeypatch.setattr(effects, "_screen", broken)
    out = effects.screen({"effects": [{"effect": "pr_comment", "body": "ok"}], "errors": None}, a_grant())
    assert out["effects"] == []
    assert "RuntimeError" in out["errors"]["effects"]


def test_a_check_run_takes_its_name_from_the_manifest_not_the_proposal():
    out = screened([{"effect": "check_run", "body": "s", "conclusion": "neutral", "name": "evil", "title": "T"}])
    assert out["effects"][0]["name"] == "ai-review"
    assert out["effects"][0]["title"] == "T"


def test_the_check_run_title_defaults_to_its_name():
    out = screened([{"effect": "check_run", "body": "s", "conclusion": "neutral"}])
    assert out["effects"][0]["title"] == "ai-review"


@pytest.mark.parametrize(
    ("proposal", "why"),
    [
        ({"effect": "merge", "body": "x"}, "not allowed"),
        ({"effect": "pr_comment", "body": ""}, "body must be"),
        ({"effect": "pr_comment", "body": "x" * 101}, "at most"),
        ({"effect": "pr_comment"}, "body must be"),
        ({"effect": "check_run", "body": "s", "conclusion": "success"}, "conclusion"),
        ({"effect": "check_run", "body": "s", "conclusion": "neutral", "title": ""}, "title"),
        ({"effect": "check_run", "body": "s", "conclusion": "neutral", "title": "t" * 201}, "title"),
        ("text", "not an effect object"),
    ],
)
def test_a_proposal_outside_the_grant_is_dropped_with_a_reason(proposal, why):
    out = screened([proposal])
    assert out["effects"] == []
    assert why in out["errors"]["effects"]


def test_a_grant_without_effects_drops_everything():
    out = screened([{"effect": "pr_comment", "body": "ok"}], grant=a_grant(allowed=()))
    assert out["effects"] == []
    assert "not allowed" in out["errors"]["effects"]


def test_no_commit_means_no_effects():
    out = screened([{"effect": "pr_comment", "body": "ok"}], head="main")
    assert out["effects"] == []
    assert "requested commit" in out["errors"]["effects"]


def test_the_number_of_effects_is_capped():
    out = screened([{"effect": "pr_comment", "body": str(i)} for i in range(5)])
    assert len(out["effects"]) == effects.MAX_PROPOSALS
    assert "more than" in out["errors"]["effects"]


def test_effects_that_are_not_a_list_are_refused():
    out = screened("post everything")
    assert out["effects"] == []
    assert "must be a list" in out["errors"]["effects"]


def test_a_result_without_effects_is_untouched():
    assert effects.screen({"report": "r"}, a_grant()) == {"report": "r"}


def test_the_pr_review_manifest_allows_an_advisory_check_only():
    m = test_authorize.MANIFESTS["ciforge-pr-review"]
    kinds = {e.kind: e for e in m.effects}
    assert set(kinds) == {"pr_comment", "check_run"}
    assert kinds["check_run"].conclusions == {"neutral"}


@pytest.mark.parametrize(
    ("body", "posted"),
    [
        ("@pytorchbot merge", "`@pytorchbot` merge"),
        ("see\n@PyTorchMergeBot revert -m x", "see\n`@PyTorchMergeBot` revert -m x"),
        ("already `@pytorchbot merge` quoted", "already `@pytorchbot merge` quoted"),
        ("mail me@pytorchbots.example", "mail me@pytorchbots.example"),
    ],
    ids=["line-leading", "case-and-mergebot", "already-quoted", "not-a-mention"],
)
def test_bot_commands_are_put_in_code_spans(body, posted):
    """pytorch-bot reads commands from review bodies; a code span is not a command."""
    out = screened([{"effect": "pr_comment", "body": body}], grant=a_grant(allowed=(EffectSpec("pr_comment", 1000),)))
    assert out["effects"][0]["body"] == ATTRIBUTION + posted


def test_the_agent_neutralises_bot_commands_the_same_way():
    """The tool neutralises before its size check, so the two rules must be one rule."""
    here = Path(__file__).resolve().parent

    def rule(path):
        return next(line for line in path.read_text().splitlines() if line.startswith("BOT_MENTION_RE = "))

    assert rule(here / "effects.py") == rule(here.parent / "agent" / "agent_loop.py")


def test_a_pull_request_selector_alone_gets_no_effects():
    """`pr` checks out whatever the head is at fetch time; effects need a commit the
    caller pinned, so a run selected only by `pr` proposes nothing that is applied."""
    grant = dataclasses.replace(a_grant(ref=""), pr=7)
    out = screened([{"effect": "pr_comment", "body": "hi"}], grant=grant)
    assert out["effects"] == []
    assert "full commit sha" in out["errors"]["effects"]
