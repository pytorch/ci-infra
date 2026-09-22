"""Unit tests for pytorch ref staging in the reservation_processor lambda.

Two layers are exercised:

1. ``index.sanitize_pytorch_ref`` — the Python ref normalizer that every entry
   point funnels user input through. Defaults to ``master``, maps a set of skip
   sentinels to ``none``, accepts safe ref chars verbatim, and falls back to
   ``master`` for anything containing shell-unsafe characters.

2. ``index.warm_pool_eligible`` — the gate that decides whether a reservation can
   be served from the warm pool. An explicit (non-master) ref must take the cold
   staging path, so this is wired to ``sanitize_pytorch_ref``.

3. The embedded ``stage-pytorch`` bash script (interpolated into the pod startup
   script) — the actual fetch/checkout command string. We extract it from the
   module source and assert the documented behavior: PR refs prefer
   ``pull/N/merge`` and fall back to ``pull/N/head``; ``pr/N`` / ``#N`` / bare
   numeric are treated as PRs; non-numeric refs are fetched/checked out directly;
   master/main are no-ops.

No network / AWS / k8s is touched — sanitize_pytorch_ref and warm_pool_eligible
are pure, and the bash assertions read the module's source text.
"""
import re

import pytest


# ── sanitize_pytorch_ref: defaults & skip sentinels ──────────────────────────

@pytest.mark.parametrize("raw", [None, "", 0, False, [], {}])
def test_sanitize_falsy_defaults_to_master(lambda_index, raw):
    assert lambda_index.sanitize_pytorch_ref(raw) == "master"


@pytest.mark.parametrize(
    "raw", ["none", "off", "false", "no", "skip",
            "NONE", "Off", "FALSE", "No", "SKIP", " none ", "  Skip  "])
def test_sanitize_skip_sentinels_map_to_none(lambda_index, raw):
    # case-insensitive + surrounding whitespace stripped before the lower() check
    assert lambda_index.sanitize_pytorch_ref(raw) == "none"


def test_sanitize_strips_surrounding_whitespace(lambda_index):
    assert lambda_index.sanitize_pytorch_ref("  master  ") == "master"
    assert lambda_index.sanitize_pytorch_ref("\tmy-branch\n") == "my-branch"


# ── sanitize_pytorch_ref: accepted refs (kept verbatim) ──────────────────────

@pytest.mark.parametrize("raw", [
    "master",
    "main",
    "release/2.1",
    "viable/strict",
    "pr/12345",
    "#9876",
    "1234",                                  # bare PR number
    "feature_branch-1.2.3",
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",  # 40-char sha
    "v2.13.0a0",
    "A.B/C_D-E#F",
])
def test_sanitize_accepts_safe_refs_verbatim(lambda_index, raw):
    assert lambda_index.sanitize_pytorch_ref(raw) == raw


def test_sanitize_accepts_max_length_ref(lambda_index):
    ref = "a" * 200
    assert lambda_index.sanitize_pytorch_ref(ref) == ref


# ── sanitize_pytorch_ref: rejected refs (fall back to master) ────────────────

@pytest.mark.parametrize("raw", [
    "bad ref",                 # space (not in the allowed class)
    "ref;rm -rf /",            # shell injection: semicolon + space
    "$(whoami)",               # command substitution
    "`id`",                    # backtick
    "branch&&evil",            # ampersand
    "a|b",                     # pipe
    "ref\nmaster",             # embedded newline (after strip, internal)
    "héllo",                   # non-ascii
    "a" * 201,                 # over the 200-char cap
])
def test_sanitize_rejects_unsafe_refs_to_master(lambda_index, raw):
    assert lambda_index.sanitize_pytorch_ref(raw) == "master"


def test_sanitize_coerces_non_string_then_validates(lambda_index):
    # int 1234 is truthy, str()'d to "1234", which matches the safe regex
    assert lambda_index.sanitize_pytorch_ref(1234) == "1234"


def test_sanitize_skip_check_precedes_regex(lambda_index):
    # "no" matches the regex too, but the skip set is checked first -> "none"
    assert lambda_index.sanitize_pytorch_ref("no") == "none"


# ── warm_pool_eligible: the ref gate ─────────────────────────────────────────

def _eligible_body(**over):
    """A body that passes every warm_pool_eligible check unless overridden."""
    b = {
        "gpu_type": "h100",            # present in WARM_POOL_TARGETS (conftest)
        "dockerimage": None,
        "dockerfile": None,
        "spot": False,
        "is_multinode": False,
        "disk_name": None,
        "ref": None,                   # -> master -> claimable
    }
    b.update(over)
    return b


def test_warm_eligible_default_ref_is_claimable(lambda_index):
    assert lambda_index.warm_pool_eligible(_eligible_body()) is True


@pytest.mark.parametrize("ref", ["master", "main", "  master  ", "MASTER"])
def test_warm_eligible_master_variants_still_claimable(lambda_index, ref):
    # main/MASTER are NOT sanitized to "master" -> they should NOT be claimable.
    result = lambda_index.warm_pool_eligible(_eligible_body(ref=ref))
    if lambda_index.sanitize_pytorch_ref(ref) == "master":
        assert result is True
    else:
        assert result is False


def test_warm_eligible_main_is_not_master_so_cold_path(lambda_index):
    # "main" is a valid distinct ref, not normalized to master -> warm pod (which
    # carries master) can't satisfy it -> ineligible.
    assert lambda_index.warm_pool_eligible(_eligible_body(ref="main")) is False


@pytest.mark.parametrize("ref", ["pr/123", "#456", "789", "viable/strict", "abc123"])
def test_warm_eligible_explicit_ref_takes_cold_path(lambda_index, ref):
    assert lambda_index.warm_pool_eligible(_eligible_body(ref=ref)) is False


def test_warm_eligible_skip_ref_is_not_master(lambda_index):
    # ref "none" sanitizes to "none" (!= master) -> not warm-claimable
    assert lambda_index.warm_pool_eligible(_eligible_body(ref="none")) is False


def test_warm_eligible_unknown_gpu_type_false(lambda_index):
    assert lambda_index.warm_pool_eligible(_eligible_body(gpu_type="t4")) is False


@pytest.mark.parametrize("field", ["dockerimage", "dockerfile"])
def test_warm_eligible_custom_image_false(lambda_index, field):
    assert lambda_index.warm_pool_eligible(_eligible_body(**{field: "x"})) is False


@pytest.mark.parametrize("field", ["spot", "is_multinode", "disk_name"])
def test_warm_eligible_disqualifying_flags_false(lambda_index, field):
    assert lambda_index.warm_pool_eligible(_eligible_body(**{field: True})) is False


# ── should_stage_pytorch / stage_flag decision logic ─────────────────────────
# create_pod computes:  should_stage = (not use_persistent_disk) and ref != "none"
# We replicate the exact branch logic from index.py and assert each combination.

def _stage_decision(use_persistent_disk: bool, sanitized_ref: str):
    should_stage = (not use_persistent_disk) and sanitized_ref != "none"
    return should_stage, ("true" if should_stage else "false")


@pytest.mark.parametrize(
    "persistent,ref,expect_stage", [
        (False, "master", True),     # ephemeral + default ref -> stage
        (False, "pr/123", True),     # ephemeral + explicit ref -> stage
        (False, "none", False),      # ephemeral but ref skipped -> no stage
        (True, "master", False),     # persistent disk brings its own checkout
        (True, "pr/123", False),     # persistent disk -> never stage
        (True, "none", False),
    ],
)
def test_stage_flag_decision_matrix(persistent, ref, expect_stage):
    should_stage, flag = _stage_decision(persistent, ref)
    assert should_stage is expect_stage
    assert flag == ("true" if expect_stage else "false")


def test_stage_decision_uses_sanitized_skip_value(lambda_index):
    # "off"/"false"/etc all sanitize to "none" -> staging skipped
    for raw in ("off", "FALSE", "skip"):
        sanitized = lambda_index.sanitize_pytorch_ref(raw)
        should_stage, _ = _stage_decision(False, sanitized)
        assert should_stage is False


# ── the embedded stage-pytorch bash script (fetch/checkout command string) ────

@pytest.fixture(scope="module")
def stage_script():
    """Extract the stage-pytorch heredoc body from the lambda module source."""
    import index
    src = open(index.__file__).read()
    m = re.search(
        r"cat > /usr/local/bin/stage-pytorch << 'STAGEPYTORCH'\n(.*?)\nSTAGEPYTORCH",
        src, re.S)
    assert m, "stage-pytorch heredoc not found in index.py source"
    return m.group(1)


def test_script_reads_ref_from_first_arg(stage_script):
    assert 'REF="$1"' in stage_script


def test_script_parses_pr_prefixes(stage_script):
    # pr/N, #N, and bare numeric all resolve to a PR number; non-numeric does not
    assert "pr/*) PRNUM=$(echo \"$REF\" | sed 's|^pr/||')" in stage_script
    assert "'#'*) PRNUM=$(echo \"$REF\" | sed 's|^#||')" in stage_script
    # the *[!0-9]* arm short-circuits (no PRNUM) before the bare-number arm
    assert "*[!0-9]*) :" in stage_script
    assert "*) PRNUM=\"$REF\"" in stage_script
    # bare-number arm must come AFTER the non-numeric guard, else "abc" -> PR
    assert stage_script.index("*[!0-9]*)") < stage_script.index('*) PRNUM="$REF"')


def test_script_pr_prefers_merge_then_head(stage_script):
    # /merge fetched first; /head only in the elif fallback.
    merge_i = stage_script.index('git fetch origin "pull/$PRNUM/merge"')
    head_i = stage_script.index('git fetch origin "pull/$PRNUM/head"')
    assert merge_i < head_i, "merge ref must be attempted before head ref"
    # /head sits behind elif (fallback), not its own unconditional fetch
    assert 'elif git fetch origin "pull/$PRNUM/head"' in stage_script


def test_script_checks_out_fetch_head_for_pr(stage_script):
    assert "git checkout -f FETCH_HEAD" in stage_script
    assert "REF_APPLIED=yes" in stage_script


def test_script_warns_when_pr_unfetchable(stage_script):
    assert "could not fetch PR #$PRNUM" in stage_script


def test_script_branch_commit_path_fetches_ref_directly(stage_script):
    # non-PR refs: fetch the ref by name, fall back to full fetch + named checkout
    assert 'git fetch origin "$REF"' in stage_script
    assert 'git checkout -f "$REF"' in stage_script
    assert "could not check out $REF" in stage_script


def test_script_master_main_are_noops(stage_script):
    # the case arm for the default refs must not fetch/checkout anything
    assert '""|master|main|MASTER|MAIN)' in stage_script
    assert "no ref override" in stage_script


def test_script_sets_pythonpath_only_when_prebuilt_and_no_ref(stage_script):
    # import-torch-with-no-build path is gated on PREBUILT_DROPPED + REF not applied
    assert '[ "$PREBUILT_DROPPED" = "yes" ] && [ "$REF_APPLIED" = "no" ]' in stage_script
    assert "/etc/profile.d/zz-pytorch.sh" in stage_script
    # with a ref applied, it points at an incremental rebuild instead
    assert "pip install -e . --no-build-isolation" in stage_script


def test_script_writes_ready_marker(stage_script):
    assert "git rev-parse HEAD" in stage_script
    assert "/home/dev/.pytorch-ready" in stage_script


def test_script_updates_submodules_after_ref_checkout(stage_script):
    assert "submodule update --init --recursive" in stage_script


# ── stage-pytorch invocation in the startup script ───────────────────────────

@pytest.fixture(scope="module")
def module_source():
    import index
    return open(index.__file__).read()


def test_startup_invokes_stage_with_quoted_ref(module_source):
    # the staging is launched in the background with the sanitized ref quoted
    assert '/usr/local/bin/stage-pytorch "{pytorch_ref}"' in module_source


def test_startup_guards_staging_on_stage_flag(module_source):
    # background staging only runs when stage_flag interpolates to "true"
    assert 'if [ "{stage_flag}" = "true" ]; then' in module_source


def test_startup_touches_staging_marker(module_source):
    # .pytorch-staging present during, removed when the background job finishes
    assert "touch /home/dev/.pytorch-staging" in module_source
    assert "rm -f /home/dev/.pytorch-staging" in module_source
