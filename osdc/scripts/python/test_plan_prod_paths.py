"""Guard: osdc-plan-prod.yml's paths filter must cover the repo files `just plan` reads.

The filter is an allowlist: a PR changing an input it misses gets no prod plan, yet the prod deploy
still applies the change. The guard collects what tofu and the plan job read, and fails when the
filter misses one of them or when it recognizes a construct it cannot resolve. It does not see:
- the repo-root mise config;
- files read inside the scripts the recipe runs or sources (e.g. cluster-config.py, state-config.sh);
- files named by mise [env] _.file/_.source;
- config that tools discover themselves (uv's [tool.uv], .terraformrc);
- read(), shell(), sha256_file(), blake3_file() and backticks in justfile expressions and recipe
  attributes;
- a just-level `if` on the cluster name (it dry-runs a placeholder cluster);
- `just` calls that neither start a line nor follow ;, &, | or (: e.g. `then just x`, `VAR=1 just x`,
  `{{just_executable()}}`, or `just x` inside backticks in the recipe;
- paths built with $(dirname …) or used after a `cd` or under [working-directory];
- relative globs and directories in the recipe, and relative paths on its echo and printf lines;
- env set on the plan workflow, job or its steps;
- bare relative paths in resource attributes;
- symlinks;
- local module sources outside osdc/;
- the `run:` scripts of the plan job's other steps.
Runs in `just test` — no cluster; needs `just` and git.
"""

import functools
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = ".github/workflows/osdc-plan-prod.yml"
PLAN_WORKFLOW = ".github/workflows/_osdc-plan.yml"
JUSTFILE = "osdc/justfile"
EXTEND = "extend this guard"
TOFU_EXTS = (".tf", ".tf.json", ".tofu", ".tofu.json")
# The plan recipe runs tofu in modules/<name>/terraform: that dir is path.root and path.cwd.
TOFU_ROOT = re.compile(r"osdc/modules/[^/]+/terraform(?=/)")
# OpenTofu's file functions. Reads are their first argument and every "${path.*}" string.
READ = re.compile(
    r"\b(?P<fn>templatefile|file(?:exists|set|base64(?:sha256|sha512)?|md5|sha1|sha256|sha512)?)\s*\("
    r'\s*(?:"(?P<arg>[^"]*)")?|"(?P<str>\$\{path\.[^"]*)"'
)
PATH_STR = re.compile(r"(?:\$\{path\.(?P<base>module|root|cwd)\})?/?(?P<rel>[^$%]*)")
PATH_REF = re.compile(r"\bpath\.(?:module|root|cwd)\b")
COMMENT = re.compile(r"(?m)^[ \t]*(?:#|//).*$")
UNSUPPORTED_TOFU = re.compile(r'\bdata\s+"(?:local_file|local_sensitive_file|archive_file|external)"|\bprovisioner\s+"')
# Justfile lines that load other justfiles, change the working directory or the .env lookup, or
# enable unstable features.
UNSUPPORTED_JUST = re.compile(
    r"""^(?:import|mod)[\s?'"]|^set\s+(?:dotenv-(?:path|filename|command)|working-directory|no-cd|unstable)\b""", re.M
)
# Paths the plan recipe names that are not tracked files: the project root, and the dir, tofu root
# and main.tf of each module it picks from clusters.yaml at run time, which the tofu test covers.
RECIPE_REFS = {
    "osdc",
    "osdc/modules/$MODULE",
    "osdc/modules/$MODULE/terraform",
    "osdc/modules/$MODULE/terraform/main.tf",
}
# Config names mise loads (src/config/mod.rs, src/config/miserc.rs) from osdc/, for the mise-action
# step and mise-activate.sh's `mise env -C osdc`; they pin the tofu version.
MISE_CONFIG = re.compile(r"osdc/(?:\.config/)?(?:\.?mise(?:rc)?(?:\.[^/]*|/.*)|\.rtx\.[^/]*|\.tool-versions)")


@functools.cache
def tracked() -> frozenset[str]:
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, text=True, check=True)
    # The index lists a file deleted from the work tree until the deletion is staged.
    return frozenset(f for f in out.stdout.split("\0") if f and (REPO / f).exists())


def read(path: str) -> str:
    return (REPO / path).read_text()


def to_regex(pattern: str) -> re.Pattern[str]:
    unsupported = sorted(set(pattern) & set("?+[]!\\{}"))
    assert not unsupported, f"{WORKFLOW}: {pattern!r} uses {unsupported}; this guard only evaluates * and **"
    return re.compile(re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*"))


def expand(path: str) -> set[str]:
    path = os.path.normpath(path)
    return {f for f in tracked() if f == path or f.startswith(path + "/")} or {path}


@functools.cache
def tofu_dirs() -> frozenset[str]:
    return frozenset(os.path.dirname(f) for f in tracked() if f.startswith("osdc/") and f.endswith(TOFU_EXTS))


def lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check(reads: set[str], problems: list[str], patterns: list[re.Pattern[str]], reader: str) -> None:
    unmatched = sorted(p for p in reads if not any(rx.fullmatch(p) for rx in patterns))
    failures = problems + [f"{p}: read by {reader}, not matched by {WORKFLOW}" for p in unmatched]
    assert not failures, "\n".join(sorted(set(failures)))


@pytest.fixture(scope="module")
def patterns() -> list[re.Pattern[str]]:
    doc = yaml.safe_load(read(WORKFLOW))
    on = doc.get(True, doc.get("on"))  # PyYAML (YAML 1.1) loads an unquoted `on` key as True
    return [to_regex(p) for p in on["pull_request"]["paths"]]


class TestMatcher:
    """A matcher looser than GitHub's filter syntax would pass reads the real filter misses."""

    @pytest.mark.parametrize(
        ("pattern", "path", "matched"),
        [
            ("**/README.md", "README.md", True),
            ("docs/*", "docs/a/b.md", False),
            ("docs/**/*.md", "docs/README.md", True),
            ("osdc/**/terraform/**", "osdc/x/terraform-old/y.tf", False),
            ("osdc/mise.*", "osdc/mise.lock", True),
            ("osdc/mise.*", "osdc/x/mise.toml", False),
        ],
    )
    def test_cheat_sheet_cases(self, pattern, path, matched):
        assert bool(to_regex(pattern).fullmatch(path)) is matched

    def test_rejects_unsupported_syntax(self):
        with pytest.raises(AssertionError, match="only evaluates"):
            to_regex("osdc/**/*.tf?")


class TestPlanProdPaths:
    def test_tofu_reads_are_matched(self, patterns):
        problems, reads = [], {f for f in tracked() if os.path.dirname(f) in tofu_dirs()}
        assert reads, "git ls-files found no tofu files"
        for f in sorted(f for f in reads if f.endswith(TOFU_EXTS)):
            text, root = read(f), TOFU_ROOT.match(f)
            # A "#" or "//" line inside a heredoc is content that tofu interpolates.
            text = text if "<<" in text else COMMENT.sub("", text)
            bases = dict.fromkeys(("root", "cwd", None), root and root[0]) | {"module": os.path.dirname(f)}
            if f.endswith(".json"):
                problems.append(f"{f}: JSON configuration; {EXTEND}")
            problems += [f"{f}:{lineno(text, m.start())}: {m[0]}; {EXTEND}" for m in UNSUPPORTED_TOFU.finditer(text)]
            unread = READ.sub(lambda m: "\n" * m[0].count("\n"), text)
            problems += [f"{f}:{lineno(unread, m.start())}: bare {m[0]}; {EXTEND}" for m in PATH_REF.finditer(unread)]
            for m in READ.finditer(text):
                where, arg = f"{f}:{lineno(text, m.start())}", m["arg"] or m["str"]
                s = PATH_STR.fullmatch(arg) if arg else None
                if not s or not bases[s["base"]]:
                    problems.append(f"{where}: cannot resolve {m[0]!r}; {EXTEND}")
                    continue
                found = expand(os.path.join(bases[s["base"]], s["rel"]))
                reads |= found
                if m["fn"] == "templatefile":
                    for t in sorted(found & tracked()):
                        body = read(t)
                        problems += [
                            f"{t}:{lineno(body, r.start())}: template reads files itself ({r[0]!r}); {EXTEND}"
                            for r in READ.finditer(body)
                        ]
        check(reads, problems, patterns, "tofu")

    def test_plan_job_reads_are_matched(self, patterns):
        jobs = yaml.safe_load(read(WORKFLOW))["jobs"]
        others = sorted(n for n, job in jobs.items() if job.get("uses") != f"./{PLAN_WORKFLOW}" or "steps" in job)
        assert not others, f"{WORKFLOW}: jobs {others} must only call {PLAN_WORKFLOW}; {EXTEND}"
        steps = [step for job in yaml.safe_load(read(PLAN_WORKFLOW))["jobs"].values() for step in job["steps"]]
        runs = [r for s in steps for r in re.findall(r"(?m)(?:^|[;&|(`])\s*just[ \t]+([\w-]+)", s.get("run", ""))]
        assert runs == ["plan"], f"{PLAN_WORKFLOW} runs just recipes {runs}; this guard only follows `plan`"
        problems = [f"{JUSTFILE} uses {m.strip()!r}; {EXTEND}" for m in UNSUPPORTED_JUST.findall(read(JUSTFILE))]
        assert not problems, "\n".join(problems)
        # just's dotenv-load reads the first .env from osdc/ upward; osdc/.env is gitignored, the root one is not.
        reads = {WORKFLOW, PLAN_WORKFLOW, JUSTFILE, "osdc/.env", ".env"}
        reads |= {f for f in tracked() if MISE_CONFIG.fullmatch(f)}
        for action in (s["uses"][2:] for s in steps if s.get("uses", "").startswith("./")):
            reads |= expand(action)
        # OSDC_UPSTREAM moves {{UPSTREAM}} out of this checkout, and JUST_* variables set just's options.
        env = {k: v for k, v in os.environ.items() if k != "OSDC_UPSTREAM" and not k.startswith("JUST_")}
        # Any cluster name works: the recipe looks the cluster up only at run time, in cluster-config.py.
        cmd = ["just", "-f", REPO / JUSTFILE, "--no-dotenv", "--color", "never", "--dry-run", "plan", "guard"]
        run = subprocess.run(cmd, env=env, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        script = re.sub(r"(?m)^\s*#.*$", "", run.stderr)
        if re.search(r"(?:^|[;&|(])\s*just\s", script, re.M):
            problems.append("`just plan` runs another recipe, which --dry-run does not expand")
        # A shell variable the script assigns a repo path to (e.g. $MODULE_DIR) resolves to that path.
        for name, _, value in re.findall(rf"""(\w+)=(["']?)({re.escape(str(REPO))}[^"'\s;]*)\2""", script):
            script = re.sub(rf"\$(?:{name}\b|\{{{name}\}})", lambda _, v=value: v, script)
        ref = re.compile(re.escape(str(REPO)) + r"((?:/[^\s\"';)|&<>]*)?)")
        assert ref.search(script), f"no {REPO} paths in `just --dry-run plan` output:\n{script}"
        for suffix in ref.findall(script):
            if (path := os.path.normpath("." + suffix)) in tracked():
                reads.add(path)
            elif path not in RECIPE_REFS | tofu_dirs():
                problems.append(
                    f"`just plan` names {suffix!r}: not a tracked file or known path (untracked or deleted?); {EXTEND}"
                )
        # Skip echo and printf lines: the paths in messages are for people, not reads.
        relative = re.sub(r"(?m)^\s*(?:echo|printf)\b.*$", "", ref.sub(" ", script))
        for token in re.split(r"[\s\"'`=;()|&<>]+", relative):
            if (path := os.path.normpath(os.path.join("osdc", token))) in tracked():  # recipes run in osdc/
                reads.add(path)
        check(reads, problems, patterns, "the plan job")
