"""Workflow instrumentation helpers for the OSDC workload test.

Pure functions for analyzing and transforming GitHub Actions workflow files:
- ARC label detection and job classification
- Non-ARC job removal (text-based)
- Cross-repo reference rewriting
- Repository guard rewriting
- Runner label prefix replacement
- Runner determinator stub generation
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger("osdc-workload-test")

# ── Constants ─────────────────────────────────────────────────────────

SOURCE_RUNNER_PREFIX = "mt-"

# ARC runner labels contain an OS char (l/w/m) followed by optional
# bare-metal flag (b) and architecture (x86/arm64).  This distinguishes
# them from GitHub-hosted labels (ubuntu-latest, linux.24_04.4x) and
# old-convention labels (linux.2xlarge, linux.g4dn.4xlarge.nvidia.gpu).
ARC_LABEL_PATTERN = re.compile(r"[lwm]-b?(?:x86|arm64)")


# ── ARC label detection ───────────────────────────────────────────────


def is_arc_label(label: str) -> bool:
    """Return True if *label* follows the OSDC ARC naming convention."""
    return bool(ARC_LABEL_PATTERN.search(label))


# ── YAML helpers ──────────────────────────────────────────────────────


def _safe_load_workflow(content: str) -> dict:
    """Load workflow YAML, working around PyYAML's ``on:`` -> ``True:`` bug."""
    modified = re.sub(r"^on:", "on_key:", content, count=1, flags=re.MULTILINE)
    return yaml.safe_load(modified) or {}


def _is_entry_point_workflow(workflow_path: Path) -> bool:
    """Return True if the workflow is an entry point (not purely reusable).

    A workflow is an entry point if its ``on:`` key contains any trigger
    other than ``workflow_call`` (e.g. ``pull_request``, ``push``, ``schedule``).
    """
    data = _safe_load_workflow(workflow_path.read_text())
    on_triggers = data.get("on_key", data.get(True, {}))
    if isinstance(on_triggers, dict):
        return set(on_triggers.keys()) != {"workflow_call"}
    if isinstance(on_triggers, str):
        return on_triggers != "workflow_call"
    if isinstance(on_triggers, list):
        return on_triggers != ["workflow_call"]
    return True  # unknown → treat as entry point (conservative)


# ── Job classification & removal ──────────────────────────────────────


def _classify_job(job_name: str, job_def: dict) -> str:
    """Classify a job as ``'arc'`` (keep), ``'non_arc'`` (remove), or ``'utility'`` (keep).

    Classification rules:
    1. Reusable workflow calls (``uses:`` at job level) → keep
    2. ``runs-on`` with ``${{ needs.* }}`` or ``${{ inputs.* }}`` → keep (dynamic)
    3. ``runs-on`` with literal string → check :func:`is_arc_label`
    4. ``runs-on`` with ``${{ matrix.* }}`` → resolve matrix values, check all
    5. Anything else → keep (conservative)
    """
    if "uses" in job_def:
        return "arc"

    runs_on = job_def.get("runs-on", "")

    if isinstance(runs_on, str):
        if "${{" in runs_on:
            matrix_match = re.search(r"\$\{\{\s*matrix\.(\w+)", runs_on)
            if matrix_match:
                return _classify_matrix_runner(matrix_match.group(1), job_def)
            return "arc"  # needs.* / inputs.* → keep
        return "arc" if is_arc_label(runs_on) else "non_arc"

    if isinstance(runs_on, list):
        labels = [lb for lb in runs_on if isinstance(lb, str)]
        if labels and not any(is_arc_label(lb) for lb in labels):
            return "non_arc"
        return "arc"

    return "arc"


def _classify_matrix_runner(matrix_key: str, job_def: dict) -> str:
    """Resolve a ``${{ matrix.<key> }}`` runner and classify it."""
    matrix = job_def.get("strategy", {}).get("matrix", {})
    includes = matrix.get("include", [])
    if includes:
        values = [e.get(matrix_key, "") for e in includes if matrix_key in e]
    else:
        values = matrix.get(matrix_key, [])

    if values and all(isinstance(v, str) for v in values):
        if not any(is_arc_label(v) for v in values):
            return "non_arc"
    return "arc"


def filter_non_arc_jobs(content: str) -> str:
    """Remove non-ARC jobs from a workflow file (text-based)."""
    data = _safe_load_workflow(content)
    jobs = data.get("jobs", {})
    if not jobs:
        return content

    to_remove: set[str] = set()
    for name, defn in jobs.items():
        if not isinstance(defn, dict):
            continue
        if _classify_job(name, defn) == "non_arc":
            log.info("    Removing non-ARC job: %s", name)
            to_remove.add(name)

    if not to_remove:
        return content
    return _cleanup_needs_references(_remove_jobs_from_content(content, to_remove), to_remove)


def _remove_jobs_from_content(content: str, jobs_to_remove: set[str]) -> str:
    """Remove named job blocks from the ``jobs:`` section via text scanning."""
    lines = content.split("\n")
    result: list[str] = []
    in_jobs = False
    removing = False

    for line in lines:
        stripped = line.strip()

        if stripped == "jobs:":
            in_jobs = True
            result.append(line)
            continue

        if in_jobs:
            # Top-level key ends the jobs section
            if line and not line[0].isspace() and stripped and not stripped.startswith("#"):
                in_jobs = False
                removing = False

            # Job-level key at indent 2
            if stripped and not stripped.startswith("#"):
                indent = len(line) - len(line.lstrip())
                if indent == 2 and ":" in stripped:
                    job_key = stripped.split(":")[0]
                    if job_key in jobs_to_remove:
                        removing = True
                        continue
                    else:
                        removing = False

            if removing:
                continue

        result.append(line)

    return "\n".join(result)


def _cleanup_needs_references(content: str, removed_jobs: set[str]) -> str:
    """Remove references to deleted jobs from ``needs:`` declarations.

    Handles three YAML forms:
    - Inline list: ``needs: [a, b, c]``
    - Single value: ``needs: some-job``
    - Block list: ``needs:\\n  - a\\n  - b``
    """
    lines = content.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#") or "needs:" not in stripped:
            result.append(line)
            i += 1
            continue

        # Inline list: needs: [a, b, c]
        bracket_match = re.match(r"^(\s*needs:\s*)\[([^\]]*)\](.*)$", line)
        if bracket_match:
            prefix, items_str, suffix = bracket_match.groups()
            kept = [it.strip() for it in items_str.split(",") if it.strip() and it.strip() not in removed_jobs]
            if kept:
                result.append(f"{prefix}[{', '.join(kept)}]{suffix}")
            i += 1
            continue

        # Check for block-style list: needs: (bare) followed by "- item" lines
        bare_match = re.match(r"^(\s*)needs:\s*$", line)
        if bare_match:
            needs_indent = len(bare_match.group(1))
            kept_items: list[str] = []
            j = i + 1
            while j < len(lines):
                child = lines[j]
                child_stripped = child.strip()
                if not child_stripped or child_stripped.startswith("#"):
                    j += 1
                    continue
                child_indent = len(child) - len(child.lstrip())
                if child_indent <= needs_indent:
                    break
                item_match = re.match(r"^\s*-\s*(.+)$", child_stripped)
                if item_match:
                    val = item_match.group(1).strip()
                    if val not in removed_jobs:
                        kept_items.append(child)
                j += 1
            if kept_items:
                result.append(line)
                result.extend(kept_items)
            i = j
            continue

        # Single value: needs: some-job
        single_match = re.match(r"^(\s*needs:\s*)(\S+)(.*)$", line)
        if single_match and single_match.group(2) not in ("[",):
            if single_match.group(2) in removed_jobs:
                i += 1
                continue
            result.append(line)
            i += 1
            continue

        result.append(line)
        i += 1

    return "\n".join(result)


# ── Workflow rewriting ────────────────────────────────────────────────


def rewrite_cross_repo_refs(content: str) -> str:
    """Rewrite ``pytorch/pytorch`` cross-repo workflow references to local paths.

    ``uses: pytorch/pytorch/.github/workflows/<X>@<ref>`` becomes
    ``uses: ./.github/workflows/<X>``.

    Action references (``pytorch/pytorch/.github/actions/<X>@<ref>``) are
    intentionally **not** rewritten.  Local action refs (``uses: ./.github/…``)
    require the repository to be already checked out, but
    ``checkout-pytorch`` *is* the checkout action — rewriting it to a local
    path creates a circular dependency.  Remote action refs work because
    GitHub fetches them directly from the source repo.

    Third-party refs (``pytorch/test-infra``, ``actions/*``) are unchanged.
    """
    content = re.sub(
        r"uses:\s*pytorch/pytorch/\.github/workflows/([^@]+)@\S+",
        r"uses: ./.github/workflows/\1",
        content,
    )
    return content


def rewrite_repo_guards(content: str) -> str:
    """Rewrite ``github.repository`` guards to ``repository_owner``.

    Prevents silent job skipping on pytorch-canary.
    """
    for quote in ("'", '"'):
        content = content.replace(
            f"github.repository == {quote}pytorch/pytorch{quote}",
            "github.repository_owner == 'pytorch'",
        )
        content = content.replace(
            f"github.repository != {quote}pytorch/pytorch{quote}",
            "github.repository_owner != 'pytorch'",
        )
    return content


def replace_runner_prefix(content: str, source_prefix: str, target_prefix: str) -> str:
    """Replace runner label prefixes for each OS character (l, w, m).

    Handles both standard (``mt-l-``) and bare-metal (``mt-bl-``) variants.
    """
    if source_prefix == target_prefix:
        return content
    for os_char in ("l", "w", "m"):
        content = content.replace(f"{source_prefix}{os_char}-", f"{target_prefix}{os_char}-")
        content = content.replace(f"{source_prefix}b{os_char}-", f"{target_prefix}b{os_char}-")
    return content


# ── Determinator stub ─────────────────────────────────────────────────

_DETERMINATOR_STUB = """\
name: runner-determinator
on:
  workflow_call:
    inputs:
      triggering_actor: { required: true, type: string }
      issue_owner: { required: true, type: string }
      curr_branch: { required: true, type: string }
      curr_ref_type: { required: false, type: string, default: "branch" }
      issue_number: { required: false, type: string, default: "0" }
      check_experiments: { required: false, type: string, default: "" }
      opt_out_experiments: { required: false, type: string, default: "" }
    outputs:
      label-type:
        value: ${{ jobs.determine.outputs.label-type }}
      use-arc:
        value: "true"
jobs:
  determine:
    runs-on: ubuntu-latest
    outputs:
      label-type: ${{ steps.set-prefix.outputs.label-type }}
    steps:
      - id: set-prefix
        run: echo "label-type=TARGET_PREFIX_PLACEHOLDER" >> "$GITHUB_OUTPUT"
"""

_DETERMINATOR_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"Stub runner_determinator.py for OSDC workload test.\"\"\"
import os

with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write("label-type=TARGET_PREFIX_PLACEHOLDER\\n")
"""


def generate_determinator_stub(target_prefix: str) -> str:
    """Return a minimal ``_runner-determinator.yml`` that forces *target_prefix*."""
    return _DETERMINATOR_STUB.replace("TARGET_PREFIX_PLACEHOLDER", target_prefix)


def generate_determinator_script(target_prefix: str) -> str:
    """Return a minimal ``runner_determinator.py`` that forces *target_prefix*."""
    return _DETERMINATOR_SCRIPT.replace("TARGET_PREFIX_PLACEHOLDER", target_prefix)


# ── PyPI cache injection ─────────────────────────────────────────────


def inject_pypi_cache_step(content: str, action_ref: str) -> str:
    """Inject a ``setup-pypi-cache`` step as the first step in every job.

    Finds each ``steps:`` key in the workflow and inserts the action
    immediately after it, before any existing steps.  Idempotent — skips
    files that already reference the action.
    """
    if "setup-pypi-cache" in content:
        return content

    lines = content.split("\n")
    result: list[str] = []
    for line in lines:
        result.append(line)
        if line.strip() == "steps:":
            indent = len(line) - len(line.lstrip())
            si = " " * (indent + 2)  # step-item indent
            result.append(f"{si}- name: Setup PyPI cache")
            result.append(f"{si}  uses: {action_ref}")
            result.append(f"{si}  with:")
            result.append(f"{si}    build-environment: ${{{{ inputs.build-environment || inputs.build_environment || '' }}}}")
    return "\n".join(result)


# ── Helpers for instrument_workflows ──────────────────────────────────


def apply_to_all_workflows(workflows_dir: Path, transform) -> None:
    """Apply a text transform to every .yml/.yaml in *workflows_dir*."""
    for wf in sorted(workflows_dir.iterdir()):
        if not wf.name.endswith((".yml", ".yaml")):
            continue
        content = wf.read_text()
        modified = transform(content)
        if modified != content:
            wf.write_text(modified)
