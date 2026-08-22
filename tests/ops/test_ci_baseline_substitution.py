r"""A comparison baseline must never be silently replaced by an empty one.

#1419. Three workflows build a BASELINE — the "before" side of a comparison —
and hand it to a consumer that cannot tell *empty because nothing was there*
from *empty because the command that built it failed*:

    .github/workflows/config-diff.yaml
        `Extract base branch configs`     -> /tmp/old-conf.d/
    .github/workflows/blast-radius.yml
        `Generate base effective configs` -> /tmp/base-effective.json
    .github/workflows/guard-defaults-impact.yml
        `Run da-guard`                    -> steps.guard.outputs.exit

An empty baseline is not a neutral value. `scripts/tools/_lib_io.py`
(`load_tenant_configs`, and its docstring says so explicitly) turns a
zero-byte `db-a.yaml` into a tenant with no thresholds, so every threshold in
the PR reads as newly added; a `{}` effective-config baseline does the same
for blast radius; and a missing `steps.guard.outputs.exit` used to take the
whole verdict step with it, so the job went green having validated nothing.
All three are reports a human reads to decide whether a config change is
safe. A report that is confidently wrong is worse than no report.

What this module asserts — and what it deliberately does not
-----------------------------------------------------------

It does NOT scan the shell for `|| true`. Enumerating forbidden spellings is
the failure mode this repo keeps getting burned by: the enumeration is always
one spelling short, and `.github/workflows/**` is full of swallows that are
correct and must stay (log capture, best-effort diagnostics, `git stash pop`
on a no-op stash). At `64a320ff`, counting lines inside `run:` blocks that
match `|| (true|:|exit 0|echo|cat|printf|cp|touch)`: 33 excluding comment-only
lines, 37 including them. ⚠️ Quote the definition with the number — an earlier
draft of this paragraph gave the 37 with no definition, and 37 is also exactly
how many workflow files there are, which is the kind of coincidence that gets
a number believed for the wrong reason.

It asks the measurable question instead:

    **if the command that builds the baseline fails, does the step fail?**

The answer is taken by running the step's real `run:` script under the shell
GitHub resolves for it, with the producer replaced by a stub that fails. Any
spelling of a swallow shows up, because what is measured is the verdict, not
the text. Each shape also gets its opposite: a healthy producer must still
exit 0, so the fix cannot degenerate into "always red".

⚠️ Two paths must STAY tolerant, and are pinned in both directions here:
a base branch with no `conf.d/` at all (an empty baseline is then correct),
and a base branch predating `describe_tenant.py` (pre-v2.7.0, `{}` is the
honest answer). Tightening those would be the same defect with the sign
flipped.

⚠️ Coverage boundary. `Run da-guard`'s verdict is a workflow-graph property,
not a shell one, so it is checked structurally — the step that renders the
verdict must not gate itself on the producer having reported. That rule is
asserted for THIS workflow only. Measured as a repo-wide rule — "a step whose
`if:` is gated on `steps.X.outputs.Y != ''` and which also consumes that same
output in its own `env:`/`with:`/`run:`" — it fires on EIGHT steps at
`64a320ff`, of which seven are correct. ⚠️ Five of those seven are the
`if: steps.detect.outputs.conf_d != ''` shape — work that genuinely has
nothing to do; the other two are `coverage-delta.yml` steps gated on
`steps.find-base.outputs.base_run_id != ''` and `steps.pr.outputs.number != ''`.
An earlier version of this paragraph offered the conf_d spelling as if it
described all seven, which is the same generalise-from-the-familiar-case
mistake the rule itself is about. So the general form is a
false-red machine and is not shipped. What separates the eighth is semantic —
it is the step that decides the job's verdict — and semantics are not a
selector. ⚠️ The count moves with the definition: dropping the "and consumes
it" clause gives TWELVE, and widening to job-level `if:` gates adds ZERO. An
earlier draft claimed "ten" for that last variant; no definition reproduces it.
That is this paragraph's own point turned on itself — print the predicate, or
do not print the number.

⚠️ None of the three workflows is a required status check (`gh api
repos/{owner}/{repo}/branches/main/protection` lists nineteen contexts, and the
one active ruleset carries only a `deletion` rule; `Config Diff`, `Blast Radius
Report` and `Defaults Impact Guard` are not among them). So the damage is a
wrong report in front of a reviewer, not a bypassed merge gate.

⚠️ Cost. The module spawns bash and git for real, so the figure is dominated
by process-spawn overhead and swings by more than an order of magnitude with
the platform AND the filesystem: single-digit seconds on container-native
storage, ~17s for the same container reading the mounted worktree, minutes on
the Windows host. The container number is the one that matters for `Python
Tests`; the host number is worth knowing before running this in a tight edit
loop.

Counterfactual
--------------

Reverting the three workflows to `64a320ff` and running the modules that
`git grep -l ".github/workflows" -- 'tests/**.py'` found **at that commit**,
excluding this one — 20 of them — gives 799 passed / 32 skipped: nothing already
in the tree could see any of these defects.

⚠️ Both numbers are anchored to `64a320ff` on purpose, and the drift is the
reason. The grep is prose-sensitive, so the module count moves with commits
that have nothing to do with this: 20 at `64a320ff`, 21 once a test elsewhere
grew a comment mentioning `ci.yml`, 22 by this branch's own base commit, 23
here. ⛔ Do not "update" it — it is a measurement of a past commit, and an
earlier version of this sentence said "already 21 on a later main" and was
stale by two within days of being written. That is the point it is making
about unanchored counts, demonstrated on itself. ⚠️ This module necessarily matches that grep (its own
docstring names the three workflow paths) and necessarily goes RED against the
pre-fix scripts; that is what it is for, so it cannot be part of the set that
is supposed to stay green. An earlier phrasing said "the 20 modules the grep
finds", which reads as though the grep returned 20 including this one.

The load-bearing shapes are paired with the pre-fix script text from
`origin/main` at `64a320ff`, run through the same harness, and required to
SWALLOW (exit 0, degraded artifact present). Without that pairing "the step
fails when the producer fails" would also pass on a step that fails for an
unrelated reason, and there would be no evidence the change bought any
detection at all.

⚠️ "The load-bearing shapes", not "every assertion" — an earlier draft claimed
the latter, and blind review counted the actual ratio. The controls below cover
the swallows that existed in the shipped scripts; the structural rules are
controlled instead by synthetic workflows, and the two shapes that only ever
existed as a possibility (an exit-0 tool writing nothing, a failing `ls-tree`)
are pinned forward-only.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The judgement "which interpreter does GitHub run this `run:` under" already
# exists, with the reasoning for modelling rather than refusing `shell: bash`.
# Importing it keeps one implementation instead of a second, drifting copy.
from test_ci_aggregate_gate_contract import (  # noqa: E402
    _SHELL_ARGV,
    BASH,
    _resolved_shell,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIFF = ROOT / ".github/workflows/config-diff.yaml"
BLAST_RADIUS = ROOT / ".github/workflows/blast-radius.yml"
GUARD_DEFAULTS = ROOT / ".github/workflows/guard-defaults-impact.yml"

needs_bash = pytest.mark.skipif(
    BASH is None, reason="needs bash to execute the step script")

EXPORTER_CONF_D = "components/threshold-exporter/config/conf.d"
DESCRIBE_TENANT = "scripts/tools/dx/describe_tenant.py"

# ── the step names this module pins, in ONE place ──────────────────────────
#
# ⛔ Renaming a step is an ordinary edit, and `_step` is fail-closed on purpose
# (a silent zero-assertion pass is the failure mode this whole module exists to
# detect). Those two facts are only compatible if the rename costs one line.
# Before this block existed, renaming one step turned tests red right across
# the file and the repair meant chasing that one string through every call site
# that spelled it out — the kind of tax that gets a guard deleted instead of
# updated.
# ⛔ NO count in this sentence. Counts belong in the commit message, where
# they are a dated snapshot; beside code they expire silently and then read as
# measured.
# ⚠️ These are pins, not a classifier — the closed thing is "the steps in these
# three files", so enumerating them here is the right shape (a new or renamed
# step forces one review of this block, which is the point).
#
# ⚠️ DISCLOSED ASYMMETRY, because the sibling PR answers the same edit
# differently: `tests/ops/test_blast_radius.py` locates its one step by an
# ANCHOR (a distinctive `changed_raw=` assignment) rather than by name, so
# renaming that step is green there and red here. Neither is the better design
# in general — an anchor is itself an identifier that can be renamed (measured
# on that side: renaming the variable reds), so the choice is which identifier
# to pin, not whether to pin one. This module locates seven distinct steps
# across three files and most of them have no comparably distinctive statement,
# which is why it pins names. The cost here is one constant per rename, and the
# failure message says which one.
_S_EXTRACT_BASE = "Extract base branch configs"
_S_RUN_CONFIG_DIFF = "Run config diff"
_S_BASE_EFFECTIVE = "Generate base effective configs"
_S_PR_EFFECTIVE = "Generate PR effective configs"
_S_COMPUTE_BLAST = "Compute blast radius"
_S_RUN_DA_GUARD = "Run da-guard"
# ⛔ Per workflow, not one shared string, even though both files spell it the
# same way today. Measured while checking the repair cost above: renaming the
# step in ONE file and updating a single shared constant left two tests red —
# for the workflow nobody had touched. Two files that happen to agree are not
# one fact, and a constant that pretends they are turns an edit to one file
# into a false red about the other.
_S_DETECT = {
    GUARD_DEFAULTS: "Detect conf.d path",
    BLAST_RADIUS: "Detect conf.d path",
}

# ⚠️ The scope step is deliberately NOT in that list: it is found by role (see
# `_scope_pipeline`), because splitting one `run:` block into two is an
# ordinary refactor and a name pin cannot survive it.
_SCOPE_HELPER = "guard_defaults_scopes.py"
# The `env:` keys the scope harness knows how to supply. Fail-closed rather
# than silently measuring a step whose inputs the harness never set.
_SCOPE_ENV_KEYS = frozenset({"BASE_REF", "CONF_D"})


# ── locating a step ────────────────────────────────────────────────────────

class Step:
    """One `run:` step, resolved to what CI would actually execute."""

    def __init__(self, workflow: Path, job_id: str, index: int, raw: dict,
                 doc: dict, job: dict) -> None:
        self.workflow = workflow
        self.job_id = job_id
        self.index = index
        self.raw = raw
        self.name = raw.get("name") or f"#{index}"
        self.condition = raw.get("if")
        self.env = {k: str(v) for k, v in (raw.get("env") or {}).items()}
        self.script = raw.get("run") or ""
        self.shell = _resolved_shell(doc, job, raw)

    @property
    def argv_prefix(self) -> tuple[str, ...]:
        # ⛔ Named, not `_SHELL_ARGV[self.shell]`. Declaring `shell: sh` on a
        # step — a legitimate portability choice — produced FIFTEEN failures,
        # every one of them a bare `KeyError: 'sh'`: a traceback naming a dict
        # lookup and telling the author nothing about where, why or how. The
        # sibling module answers the same edit with a full fail-closed message;
        # this one did not, in a file whose own comments call that shape out.
        assert self.shell in _SHELL_ARGV, (
            f"{self} resolves to `shell: {self.shell!r}`, which this harness "
            f"cannot reproduce, so these tests would measure an interpreter CI "
            f"does not run.\n"
            f"⛔ Adding a row to `_SHELL_ARGV` is NOT enough on its own: that "
            f"table holds FLAGS and the binary is hardcoded to bash, so a `sh` "
            f"row would make the harness simulate the runner's `dash` with "
            f"bash — green, and wrong in exactly the way this assertion exists "
            f"to prevent. Teaching another shell means teaching the binary too."
        )
        return (BASH,) + _SHELL_ARGV[self.shell]

    def __str__(self) -> str:
        return f"{self.workflow.name}::{self.job_id}::{self.name}"


def _steps(workflow: Path) -> list[Step]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    out: list[Step] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for index, raw in enumerate(job.get("steps") or []):
            if isinstance(raw, dict):
                out.append(Step(workflow, job_id, index, raw, doc, job))
    return out


def _step(workflow: Path, name: str) -> Step:
    """The step called *name*, or a hard failure naming what is there.

    ⛔ Fail-closed on purpose. Renaming a step must turn this module RED, not
    quietly reduce it to zero assertions — the whole point of the module is
    that a silent nothing reads exactly like a pass.
    """
    hits = [s for s in _steps(workflow) if s.raw.get("name") == name]
    assert len(hits) == 1, (
        f"expected exactly one step named {name!r} in {workflow.name}, "
        f"found {len(hits)}. Steps present: "
        f"{[s.name for s in _steps(workflow)]}.\n"
        f"If the step was RENAMED, edit the one constant that spells it — the "
        f"`_S_*` block near the top of this module — and every test that named "
        f"it goes green again. Do not delete the assertion, and do not relax "
        f"the arity: a zero-hit lookup that returns None would leave every "
        f"assertion below asserting nothing, which reads exactly like a pass."
    )
    assert hits[0].script.strip(), f"{hits[0]} has an empty `run:`"
    return hits[0]


def _detect_step(workflow: Path) -> Step:
    """The conf.d detection step of *workflow*, by that file's own spelling."""
    assert workflow in _S_DETECT, (
        f"{workflow.name} has no entry in `_S_DETECT`; add one rather than "
        f"borrowing another file's step name")
    return _step(workflow, _S_DETECT[workflow])


# ── rendering + retargeting ────────────────────────────────────────────────

_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _render(script: str, values: dict[str, str]) -> str:
    """Substitute `${{ ... }}`, refusing any expression we were not told about.

    Bash cannot evaluate Actions expressions, so they have to be filled in
    before the script runs. Refusing unknown ones keeps a newly introduced
    interpolation from being executed as literal text — which bash would
    happily do, and which would make the harness measure a different script
    than CI runs.
    """
    unknown = {m for m in _EXPRESSION.findall(script) if m not in values}
    assert not unknown, (
        f"unmapped Actions expressions in the step script: {sorted(unknown)}. "
        f"Add them to the harness rather than letting bash see them literally."
    )
    return _EXPRESSION.sub(lambda m: values[m.group(1)], script)


# A scratch artifact, in either of the two spellings these files use for one:
# a literal `/tmp/<name>`, or `$RUNNER_TEMP/<name>` — which is what GitHub's own
# documentation recommends and what `guard-defaults-impact.yml` already does.
_SCRATCH_PATH = re.compile(
    r"(?:/tmp|\$\{?RUNNER_TEMP\}?|\$\{\{\s*runner\.temp\s*\}\})/([A-Za-z0-9._-]+)")


def _artifact_names(script: str) -> set[str]:
    """The BASENAMES of the scratch artifacts a script names.

    Directory-agnostic on purpose: `/tmp/base-effective.json` and
    `$RUNNER_TEMP/base-effective.json` are the same artifact, and which
    spelling a file uses is a portability preference, not a contract.
    """
    return set(_SCRATCH_PATH.findall(script))


def _sandbox(script: str, tmp: Path) -> str:
    """Point every scratch artifact path at the test's own directory.

    Only paths move; every branch, test and exit code is the shipped text.

    ⛔ DERIVED, not a literal-to-literal mapping. The previous version took
    `{"/tmp/base-effective.json": …}` and asserted each key was present, so
    switching those two workflows to `$RUNNER_TEMP/…` — the spelling GitHub
    documents, that the sibling workflow in this same PR already uses, and that
    is strictly more correct on a self-hosted runner — turned most of this
    module's behavioural tests red, with a message that named neither the
    constant to edit nor anything that actually goes wrong. ⛔ No count here:
    the figures live in the commit message, where they are dated. Basenames are preserved, so every assertion that reads
    `tmp_path / "base-effective.json"` keeps working under either spelling.

    ⚠️ The fail-closed check that survives is the one that still has teeth: a
    path the rewriter cannot see (`old="/tmp/$name"`, `d=/tmp; "$d/x"`) would
    leave the harness writing into the real `/tmp`, where a test can pass for
    reasons that have nothing to do with its assertion.
    """
    # ⛔ The sandbox directory is masked out first. On Linux `tmp_path` IS under
    # `/tmp`, and `${{ github.workspace }}` renders to it, so without this the
    # rewriter treats the sandbox path as an artifact to sandbox and nests it
    # inside itself (measured in the container: two tests red pointing at
    # `…/test_x/pytest-of-root/pytest-3/test_x/diff-report.md`). The host never
    # sees it — `%TEMP%` there is not under `/tmp` — so this is a platform-only
    # false red of exactly the kind this round exists to remove.
    sandbox = tmp.as_posix()
    sentinel = "\x00SANDBOX\x00"
    masked = script.replace(sandbox, sentinel)
    # ⚠️ Executable lines only. A comment that merely says "/tmp" is not a
    # write, and failing on one would be this module's own false-red class.
    stray = [ln.replace(sentinel, sandbox) for ln in _executable_lines(masked)
             if "/tmp" in _SCRATCH_PATH.sub("", ln)]
    for full in sorted({m.group(0) for m in _SCRATCH_PATH.finditer(masked)},
                       key=len, reverse=True):
        masked = masked.replace(full, f"{sandbox}/{full.rsplit('/', 1)[-1]}")
    script = masked.replace(sentinel, sandbox)
    assert not stray, (
        "this script still names `/tmp` after rewriting, so the harness would "
        "write to the real one. That happens when the path is ASSEMBLED rather "
        "than written out (`f=/tmp/$name`, `d=/tmp` then `\"$d/x\"`); the "
        "rewriter only sees whole literal paths. Write the path out, or teach "
        "`_SCRATCH_PATH` the new shape — do not delete this assertion:\n"
        + "\n".join(stray))
    return script


# ── running ────────────────────────────────────────────────────────────────

def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(step: Step, script: str, cwd: Path, env: dict[str, str],
         bin_dir: Path | None = None) -> subprocess.CompletedProcess:
    target = cwd / "_step.sh"
    target.write_text(script, encoding="utf-8", newline="\n")
    full = dict(os.environ)
    if bin_dir is not None:
        full["PATH"] = f"{bin_dir}{os.pathsep}{full['PATH']}"
    full.update(env)
    # ⛔ encoding pinned: the workflows' `::error::` lines carry em dashes, and
    # on a cp950 Windows host the default decode raises inside subprocess's
    # reader thread, leaving `stdout` as None. The failure surfaces as a
    # TypeError in whichever assertion touches the output — i.e. as a broken
    # test rather than as the missing evidence it actually is.
    return subprocess.run(
        [*step.argv_prefix, str(target)],
        cwd=cwd, env=full, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True, timeout=120)


_TEMPLATE_ROOT: Path | None = None
_TEMPLATES: dict[tuple[bool, bool, bool], Path] = {}


def _template(conf_d: bool, base_ref: bool, describe_tenant: bool) -> Path:
    """Build each repo shape once per process, then hand out copies.

    Every fixture repo costs six to nine `git` spawns, and on a Windows host
    those dominated the module (~30s per blast-radius test, ~6 min overall).
    Copying a prepared tree is one filesystem operation. The shapes are
    immutable by construction — callers get their own copy and are free to
    commit into it.
    """
    global _TEMPLATE_ROOT
    if _TEMPLATE_ROOT is None:
        _TEMPLATE_ROOT = Path(tempfile.mkdtemp(prefix="ci-baseline-templates-"))
        atexit.register(shutil.rmtree, _TEMPLATE_ROOT, True)
    key = (conf_d, base_ref, describe_tenant)
    if key not in _TEMPLATES:
        dest = _TEMPLATE_ROOT / f"{int(conf_d)}{int(base_ref)}{int(describe_tenant)}"
        _build_repo(dest, conf_d=conf_d, base_ref=base_ref,
                    describe_tenant=describe_tenant)
        _TEMPLATES[key] = dest
    return _TEMPLATES[key]


def _base_repo(tmp: Path, *, conf_d: bool = True, base_ref: bool = True) -> Path:
    repo = tmp / "repo"
    shutil.copytree(_template(conf_d, base_ref, False), repo)
    return repo


def _build_repo(repo: Path, *, conf_d: bool, base_ref: bool,
                describe_tenant: bool) -> Path:
    """A repo whose `origin/main` looks like the base of a config PR."""
    (repo / EXPORTER_CONF_D / "examples").mkdir(parents=True)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    # ⛔ These repos are built once and then copied per test, so anything git
    # writes into `.git` afterwards races the copy. CI failed with
    # `[Errno 2] … .git/objects/maintenance.lock` (run 32558051713); turning
    # background maintenance off removes the writer. Not reproducible on the
    # Windows host, which does not have it enabled.
    _git(repo, "config", "maintenance.auto", "false")
    _git(repo, "config", "gc.auto", "0")
    if conf_d:
        base = repo / EXPORTER_CONF_D
        (base / "_defaults.yaml").write_text("defaults:\n  cpu: 80\n",
                                             encoding="utf-8", newline="\n")
        (base / "db-a.yaml").write_text("tenants:\n  db-a:\n    cpu: 90\n",
                                        encoding="utf-8", newline="\n")
        (base / "db-b.yaml").write_text("tenants:\n  db-b:\n    cpu: 70\n",
                                        encoding="utf-8", newline="\n")
        # A sub-directory: `git show <ref>:<dir>` succeeds on it and writes a
        # tree listing, which is how a non-config file used to land in the
        # baseline directory looking like a tenant.
        (base / "examples" / "redis.yaml").write_text(
            "tenants:\n  redis:\n    cpu: 60\n", encoding="utf-8", newline="\n")
    else:
        (repo / "README.md").write_text("no conf.d here\n",
                                        encoding="utf-8", newline="\n")
    if describe_tenant:
        target = repo / DESCRIBE_TENANT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stubbed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    if base_ref:
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


_GIT_STUB = """#!/usr/bin/env bash
# Delegates to the real git except for the one subcommand under test.
# FAIL_GIT_SHOW_BLOB is deliberately narrower than FAIL_GIT_SHOW: the failure
# that actually happened in CI is per-FILE (one object unreadable), and the
# pre-fix script probed the directory with `git show` first, so failing every
# `show` exercises the probe rather than the extraction loop.
#
# ⛔ Leading `-c key=value` options are skipped before deciding what the
# subcommand is. Reading `$1` directly looked fine until the scripts grew a
# `git -c core.quotePath=false ls-tree`: the stub then saw `-c`, matched
# nothing, delegated to real git, and QUIETLY STOPPED INJECTING. An injection
# that no longer injects makes "the step failed" unprovable in one direction
# and "the step succeeded" meaningless in the other.
args=("$@")
i=0
while [ "${args[$i]:-}" = "-c" ]; do i=$((i + 2)); done
sub="${args[$i]:-}"
subarg="${args[$((i + 1))]:-}"
case "$sub" in
  show)
    [ -n "${FAIL_GIT_SHOW:-}" ] && { echo "fatal: injected" >&2; exit 128; }
    case "$subarg" in
      *.yaml) [ -n "${FAIL_GIT_SHOW_BLOB:-}" ] && { echo "fatal: injected" >&2; exit 128; } ;;
    esac
    ;;
  ls-tree)   [ -n "${FAIL_GIT_LS_TREE:-}" ]   && { echo "fatal: injected" >&2; exit 128; } ;;
  # ⛔ Writes a PARTIAL listing before dying, because that is the shape that
  # matters. A `git diff` that fails after emitting some records leaves a file
  # that is indistinguishable from a complete one, and the redirect has already
  # created it — so a step that does not check the exit code resolves scopes
  # from a truncated list and validates fewer trees while staying green.
  diff)      [ -n "${FAIL_GIT_DIFF:-}" ]      && { printf 'partial/conf.d/_defaults.yaml\\0'; echo "fatal: injected" >&2; exit 128; } ;;
  # ⛔ 128, not 1. `git merge-base` uses 1 for "these two share no history" and
  # 128 for "I could not answer"; a probe written as `! git merge-base …` reads
  # them as the same thing and then states the topology as fact in the PR
  # comment. Injecting the 128 is the only way a test can tell them apart.
  merge-base) [ -n "${FAIL_GIT_MERGE_BASE:-}" ] && { echo "fatal: injected bad object" >&2; exit 128; } ;;

  checkout)  [ -n "${FAIL_GIT_CHECKOUT:-}" ]  && { echo "fatal: injected" >&2; exit 128; } ;;
esac
exec "$REAL_GIT" "$@"
"""


def _git_stub_dir(tmp: Path) -> Path:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir, "git", _GIT_STUB)
    return bin_dir


# ══ config-diff.yaml — `Extract base branch configs` ═══════════════════════


def _extract_script(tmp: Path) -> tuple[Step, str]:
    step = _step(CONFIG_DIFF, _S_EXTRACT_BASE)
    script = _render(_sandbox(step.script, tmp), {"github.base_ref": "main"})
    return step, script


def _extract_env(tmp: Path) -> dict[str, str]:
    return {
        "BASE_REF": "main",
        "REAL_GIT": shutil.which("git") or "git",
        # The step publishes its blind-spot disclosure here. Under `set -u` an
        # unset GITHUB_OUTPUT aborts the step, which is how the harness noticed
        # the new write in the first place.
        "GITHUB_OUTPUT": str((tmp / "gh-output").as_posix()),
    }


@needs_bash
def test_config_diff_baseline_is_complete_on_a_healthy_base(tmp_path) -> None:
    """Positive control: the tolerant paths must not have become 'always red'."""
    repo = _base_repo(tmp_path)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))

    assert proc.returncode == 0, proc.stderr
    landed = sorted(p.name for p in (tmp_path / "old-conf.d").iterdir())
    assert landed == ["_defaults.yaml", "db-a.yaml", "db-b.yaml"], landed
    assert (tmp_path / "old-conf.d" / "db-a.yaml").read_text(
        encoding="utf-8") == "tenants:\n  db-a:\n    cpu: 90\n"
    # The sub-directory must not be materialised: the consumer reads conf.d/
    # flat, and `git show <ref>:<dir>` writes a tree listing that would sit in
    # the baseline looking like a config.
    assert not (tmp_path / "old-conf.d" / "examples").exists()


@needs_bash
@pytest.mark.parametrize("injection", ["FAIL_GIT_SHOW_BLOB", "FAIL_GIT_SHOW"])
def test_config_diff_baseline_fails_when_git_show_fails(
        tmp_path, injection: str) -> None:
    repo = _base_repo(tmp_path)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo,
                {**_extract_env(tmp_path), injection: "1"},
                _git_stub_dir(tmp_path))

    assert proc.returncode != 0, (
        "a failed `git show` left the baseline short and the step still "
        f"succeeded:\n{proc.stdout}\n{proc.stderr}")
    # And nothing half-written may be left standing in for a real config: an
    # empty file is read back as a tenant with zero thresholds, so every
    # threshold in the PR would be reported as newly added.
    stray = [p for p in (tmp_path / "old-conf.d").glob("*") if p.stat().st_size == 0]
    assert not stray, f"zero-byte files left in the baseline: {stray}"


@needs_bash
def test_config_diff_baseline_fails_when_the_tree_listing_fails(tmp_path) -> None:
    repo = _base_repo(tmp_path)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo,
                {**_extract_env(tmp_path), "FAIL_GIT_LS_TREE": "1"},
                _git_stub_dir(tmp_path))
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"


@needs_bash
def test_config_diff_baseline_fails_when_the_base_ref_is_missing(tmp_path) -> None:
    """A shallow clone: every git call below would fail, so none may be read
    as 'the base simply had no conf.d/'."""
    repo = _base_repo(tmp_path, base_ref=False)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert "fetch-depth" in (proc.stdout + proc.stderr), (
        "the failure must name the fix; a bare non-zero exit sends the reader "
        "looking in the wrong place")


@needs_bash
def test_config_diff_accepts_a_base_that_has_no_confd(tmp_path) -> None:
    """⚠️ MUST stay tolerant: an empty baseline is correct here."""
    repo = _base_repo(tmp_path, conf_d=False)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not any((tmp_path / "old-conf.d").iterdir())


# ══ blast-radius.yml — `Generate base effective configs` ═══════════════════

_PY_OK = """#!/usr/bin/env bash
# Records --conf-d as well as --output. Recording only --output made the
# rework's central change unfalsifiable: pointing the tool back at the PR
# worktree instead of the rebuilt baseline left the whole suite green, because
# every assertion looked at the directory that had been built rather than at
# which directory was then READ.
out=""
confd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--output" ]; then out="$2"; fi
  if [ "$1" = "--conf-d" ]; then confd="$2"; fi
  shift
done
[ -n "${PY_ARGV_LOG:-}" ] && printf '%s' "$confd" > "$PY_ARGV_LOG"
[ -n "${PY_WRITES_NOTHING:-}" ] || printf '{"db-a": {"cpu": 90}}' > "$out"
echo "wrote $out"
"""

_PY_FAIL = """#!/usr/bin/env bash
echo "Traceback (most recent call last): injected" >&2
exit 1
"""


def _base_effective_script(tmp: Path) -> tuple[Step, str]:
    step = _step(BLAST_RADIUS, _S_BASE_EFFECTIVE)
    script = _render(_sandbox(step.script, tmp), {
        "github.base_ref": "main",
        "steps.detect.outputs.conf_d": EXPORTER_CONF_D,
    })
    return step, script


def _blast_repo(tmp: Path, *, describe_tenant: bool = True,
                base_ref: bool = True, conf_d_in_base: bool = True) -> Path:
    """Like `_base_repo`, but `describe_tenant.py` is COMMITTED when asked.

    The base side now asks the base COMMIT whether the tool exists
    (`git cat-file -e origin/<base>:...`) rather than looking at the worktree,
    so a file that merely exists on disk no longer stands in for one the base
    branch actually shipped.
    """
    repo = tmp / "repo"
    shutil.copytree(_template(conf_d_in_base, base_ref, describe_tenant), repo)
    return repo


def _blast_env(tmp: Path) -> dict[str, str]:
    return {
        "BASE_REF": "main",
        "CONF_D": EXPORTER_CONF_D,
        "REAL_GIT": shutil.which("git") or "git",
    }


def _py_stub_dir(tmp: Path, body: str) -> Path:
    bin_dir = _git_stub_dir(tmp)
    _stub(bin_dir, "python3", body)
    return bin_dir


@needs_bash
def test_blast_radius_baseline_is_written_on_a_healthy_base(tmp_path) -> None:
    repo = _blast_repo(tmp_path)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "base-effective.json").read_text(
        encoding="utf-8") == '{"db-a": {"cpu": 90}}'


@needs_bash
@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX file modes; the runner is Linux")
def test_the_extracted_baseline_is_readable_by_the_consuming_container(
        tmp_path) -> None:
    """The baseline is mounted into an image that runs as a DIFFERENT uid.

    ⛔ `mktemp` creates 0600 and `mv` preserves it, so promoting through a
    scratch file quietly made every baseline config unreadable to
    `ghcr.io/vencil/da-tools` (uid 10001,
    `components/da-tools/app/Dockerfile:73-75`). The tool then dies with
    PermissionError → exit 1, which this workflow cannot distinguish from
    "changes detected" → empty report → `has_changes` unset → no comment →
    green. A silent green, created by the fix for silent greens, on every
    conf.d PR.

    ⚠️ This test is why it is checked on POSIX only: a Windows dev host's
    `mktemp` already yields 0644, so the whole class is invisible there —
    which is exactly how it got written.
    """
    repo = _base_repo(tmp_path)
    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    extracted = sorted(p for p in (tmp_path / "old-conf.d").iterdir() if p.is_file())
    assert extracted, "nothing was extracted, so this proves nothing"
    unreadable = [p.name for p in extracted if not (p.stat().st_mode & 0o044)]
    assert not unreadable, (
        f"baseline files are not readable by other users: {unreadable}. The "
        f"consuming container runs as uid 10001 and will fail to open them.")


@needs_bash
def test_config_diff_baseline_skips_entries_that_are_not_regular_files(
        tmp_path) -> None:
    """Same hardening as blast-radius, which had a test while this did not.

    Matching on `blob` alone accepts mode 120000, whose "content" is the
    symlink TARGET PATH. That lands in the baseline as a non-mapping document,
    the tenant drops out of it, and every one of its thresholds reports as
    newly added — a confidently wrong report, with nothing failing.
    """
    repo = _base_repo(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo",
         f"120000,{_blob(repo, '../../../../etc/passwd')},"
         f"{EXPORTER_CONF_D}/link.yaml")
    _git(repo, "commit", "-qm", "symlink in conf.d")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not (tmp_path / "old-conf.d" / "link.yaml").exists(), (
        "a symlink entry was materialised into the baseline as a regular file")


@needs_bash
def test_config_diff_names_a_nested_inherited_file_as_both_blind_spots(
        tmp_path) -> None:
    """`team-x/_defaults.yaml` is inherited AND nested.

    A single first-match `case` counted it only as a sub-directory, so the
    inherited half — the one that makes every tenant under it read as
    unchanged — was never named.
    """
    repo = _base_repo(tmp_path)
    shutil.rmtree(repo / EXPORTER_CONF_D / "examples")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base: flat conf.d")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    (repo / EXPORTER_CONF_D / "team-x").mkdir()
    (repo / EXPORTER_CONF_D / "team-x" / "_defaults.yaml").write_text(
        "defaults:\n  cpu: 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "PR adds a nested inherited file")

    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    published = (tmp_path / "gh-output").read_text(encoding="utf-8")
    assert "inherited file" in published, published
    assert "sub-directories" in published, published


@needs_bash
def test_config_diff_declares_what_it_cannot_compare(tmp_path) -> None:
    """"No changes detected." must not be the whole story.

    `config-diff` reads conf.d FLAT and skips `_`-prefixed files, so a change to
    `_defaults.yaml` — inherited by every tenant — or a tenant in a
    sub-directory produces a confident "no changes". No command fails; the
    report simply answers a question the tool never asked.

    ⚠️ The disclosure is emitted by the WORKFLOW rather than by
    `config_diff.py`, because the step runs a pinned published image and no
    released `tools/v*` tag contains `_lib_confd.warn_nested` (#1339). Fixing repo
    source would not reach CI.
    """
    repo = _base_repo(tmp_path)
    # ⛔ Flatten the BASE REF first, before anything the PR is supposed to own.
    # Two things had to be measured here, and both were wrong on the first try:
    # deleting `examples/` from HEAD alone leaves `origin/main` pointing at the
    # template commit that still has it (a base-scoped implementation reads the
    # REF), and doing this after the `_defaults.yaml` write folds that edit into
    # the base so the inherited-file arm stops firing.
    shutil.rmtree(repo / EXPORTER_CONF_D / "examples")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base: flat conf.d")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    (repo / EXPORTER_CONF_D / "_defaults.yaml").write_text(
        "tenants: {}\n", encoding="utf-8", newline="\n")
    # ⛔ The PR must ADD the nested file. An earlier version leaned on the
    # fixture's base-resident `examples/redis.yaml`, so this assertion passed
    # off something the PR never touched — the same base-scoped mistake the
    # disclosure itself had, which meant the test was pinning it in place.
    (repo / EXPORTER_CONF_D / "team-x").mkdir()
    (repo / EXPORTER_CONF_D / "team-x" / "db-z.yaml").write_text(
        "tenants:\n  db-z:\n    cpu: 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "PR edits inherited defaults, adds a nested tenant")

    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    published = (tmp_path / "gh-output").read_text(encoding="utf-8")
    assert "blind_spots" in published, (
        f"the step said nothing about the inherited file it cannot read:\n"
        f"{proc.stdout}\n{published}")
    # ⛔ The COUNTS, not the filename. `_defaults.yaml` is a hardcoded
    # `(e.g. …)` inside the disclosure, so `assert "_defaults.yaml" in
    # published` fired for ANY inherited file while reading as though the tool
    # had identified this PR's — the sibling test below deliberately asserts
    # only category words for exactly that reason. `$inherited` and `$nested`
    # ARE derived from the change list, so breaking the counting is what should
    # turn this red. This fixture changes one file of each kind.
    assert "1 `_`-prefixed inherited file(s)" in published, published
    assert "1 file(s) in sub-directories" in published, published


@needs_bash
def test_blast_radius_baseline_excludes_files_the_pr_added(tmp_path) -> None:
    """The baseline must be the BASE, not "the PR worktree with base painted on".

    `git checkout <ref> -- .` updates the paths that exist in <ref> and does not
    delete anything else, so a tenant the PR ADDED survived into the "base"
    side. Both sides then held the same file, the diff was empty, and the report
    told the reviewer that a brand-new tenant changed nothing. No command had to
    fail for this; it was the ordinary success path.
    """
    repo = _blast_repo(tmp_path)
    added = repo / EXPORTER_CONF_D / "db-new.yaml"
    added.write_text("tenants:\n  db-new:\n    cpu: 50\n",
                     encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "PR adds a tenant")   # origin/main deliberately NOT moved

    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    landed = sorted(p.name for p in (tmp_path / "base-conf.d").rglob("*") if p.is_file())
    assert "db-new.yaml" not in landed, (
        f"a tenant that exists only in the PR reached the baseline: {landed}. "
        f"The two sides then agree and the report says nothing changed.")
    assert "db-a.yaml" in landed, landed
    # Hierarchy matters: conf.d is an inheritance chain (ADR-016/017), so a
    # flat extraction would silently drop the sub-tree's defaults.
    assert (tmp_path / "base-conf.d" / "examples" / "redis.yaml").is_file()


@needs_bash
def test_blast_radius_reads_the_rebuilt_baseline_not_the_pr_worktree(
        tmp_path) -> None:
    """Building the baseline is worthless if the tool is then pointed elsewhere.

    Rebuilding the base tree and then running `describe_tenant --conf-d
    "$CONF_D"` restores the exact pre-fix behaviour (base ≡ PR) while leaving
    the extraction loop, and every assertion about it, perfectly green. The
    directory that gets READ is the load-bearing one.
    """
    repo = _blast_repo(tmp_path)
    argv_log = tmp_path / "conf-d-seen"
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo,
                {**_blast_env(tmp_path), "PY_ARGV_LOG": str(argv_log.as_posix())},
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    seen = argv_log.read_text(encoding="utf-8")
    assert seen.endswith("base-conf.d"), (
        f"describe-tenant was pointed at {seen!r}, not the rebuilt baseline. "
        f"If that is the PR's own conf.d, the two sides of the comparison are "
        f"the same tree again.")
    assert EXPORTER_CONF_D not in seen, seen


@needs_bash
def test_blast_radius_baseline_skips_entries_that_are_not_regular_files(
        tmp_path) -> None:
    """A gitlink has no blob; a symlink's blob is its target path.

    `--name-only` threw away the column that distinguishes them. A gitlink then
    killed the run with a message about a "truncated baseline", and a symlink
    would have been materialised as a regular file whose contents are a path —
    a config-shaped lie sitting in the baseline.
    """
    repo = _blast_repo(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo",
         f"120000,{_blob(repo, '../../../../etc/passwd')},"
         f"{EXPORTER_CONF_D}/link.yaml")
    _git(repo, "commit", "-qm", "symlink in conf.d")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not (tmp_path / "base-conf.d" / "link.yaml").exists(), (
        "a symlink entry was materialised into the baseline as a regular file")
    assert "skipping non-file entry" in proc.stdout + proc.stderr


def _blob(repo: Path, content: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=True).stdout.strip()


@needs_bash
def test_blast_radius_accepts_a_base_that_has_no_confd(tmp_path) -> None:
    """The other direction of the tolerant path, which was pinned for
    config-diff but not for this workflow.

    A base with no conf.d/ genuinely has no baseline, so `{}` is correct and
    the step must stay green — tightening it would be the same defect with the
    sign flipped.
    """
    repo = _blast_repo(tmp_path, describe_tenant=True, conf_d_in_base=False)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "base-effective.json").read_text(encoding="utf-8").strip() == "{}"


@needs_bash
def test_blast_radius_baseline_fails_when_the_base_ref_is_missing(
        tmp_path) -> None:
    """A shallow clone has no origin/<base>; that is not an empty baseline."""
    repo = _blast_repo(tmp_path, base_ref=False)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert "fetch-depth" in proc.stdout + proc.stderr


@needs_bash
def test_blast_radius_baseline_fails_when_git_cannot_answer(tmp_path) -> None:
    """"The base predates describe-tenant" must not absorb "git is broken".

    ⚠️ `git cat-file -e` cannot carry this distinction: it returns 128 for a
    MISSING PATH as well as for a broken object store. A first attempt at this
    branch classified rc from `cat-file` and was caught immediately by the
    opposite-direction pin (`test_blast_radius_keeps_the_pre_v270_empty_base`),
    which is exactly why that pin exists. The probe is `ls-tree` instead:
    absent is rc=0 with empty output, broken is rc!=0.
    """
    repo = _blast_repo(tmp_path)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo,
                {**_blast_env(tmp_path), "FAIL_GIT_LS_TREE": "1"},
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert "predates describe-tenant" not in proc.stdout, (
        "a broken git was reported as an old base branch")


@needs_bash
def test_blast_radius_baseline_leaves_the_worktree_alone(tmp_path) -> None:
    """The step must not rewrite the checkout it is running in.

    It used to overlay the base branch onto the worktree and then restore with
    `git checkout - -- .`, which needs an `@{-1}` a detached PR checkout does
    not have (measured: rc=128 every run). The tree stayed on BASE content, and
    the very next step runs `blast_radius.py` FROM the tree — so a PR touching
    both conf.d/ and that script had its report computed by the old tool.
    """
    def status() -> str:
        return subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120).stdout

    repo = _blast_repo(tmp_path)
    # An uncommitted PR-side edit: exactly what the old overlay clobbered.
    marker = repo / EXPORTER_CONF_D / "db-a.yaml"
    marker.write_text("tenants:\n  db-a:\n    cpu: 999\n",
                      encoding="utf-8", newline="\n")
    before = status()

    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert marker.read_text(encoding="utf-8") == "tenants:\n  db-a:\n    cpu: 999\n", (
        "the step rewrote the worktree it was running in")
    # Compare before/after rather than asserting a clean tree: the fixture edit
    # above is legitimately dirty, and `_step.sh` is the harness's own file.
    after = [ln for ln in status().splitlines() if "_step.sh" not in ln]
    assert after == before.splitlines(), (
        f"the step changed the worktree/index state:\nbefore={before!r}\nafter={after!r}")


@needs_bash
def test_blast_radius_baseline_survives_a_non_ascii_tenant_filename(tmp_path) -> None:
    """`git ls-tree` C-quotes non-ASCII paths unless told not to.

    The failure was loud rather than silent, but its message blamed the git
    object instead of the quoting — which sends the reader somewhere the bug
    is not.
    """
    repo = _blast_repo(tmp_path)
    exotic = repo / EXPORTER_CONF_D / "\u6e2c\u8a66\u79df\u6236.yaml"
    exotic.write_text("tenants:\n  exotic:\n    cpu: 42\n",
                      encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "non-ascii tenant")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    landed = sorted(p.name for p in (tmp_path / "base-conf.d").rglob("*") if p.is_file())
    assert "\u6e2c\u8a66\u79df\u6236.yaml" in landed, landed


@needs_bash
def test_blast_radius_pr_side_fails_when_the_tool_writes_nothing(tmp_path) -> None:
    """The two sides of a comparison need the same checks.

    Only the base side had the non-empty check, so an exit-0 run that wrote
    nothing on the PR side left it empty against a full baseline — flipping the
    report into "every tenant was deleted".
    """
    repo = _blast_repo(tmp_path)
    step = _step(BLAST_RADIUS, _S_PR_EFFECTIVE)
    script = _render(_sandbox(step.script, tmp_path),
                     {"steps.detect.outputs.conf_d": EXPORTER_CONF_D})
    proc = _run(step, script, repo,
                {**_blast_env(tmp_path), "PY_WRITES_NOTHING": "1"},
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"


@needs_bash
def test_blast_radius_baseline_fails_when_describe_tenant_crashes(tmp_path) -> None:
    repo = _blast_repo(tmp_path)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_FAIL))
    assert proc.returncode != 0, (
        "describe-tenant crashed and the step still succeeded — the blast "
        f"radius would be computed against an empty base:\n{proc.stdout}")
    written = tmp_path / "base-effective.json"
    if written.exists():
        assert written.read_text(encoding="utf-8").strip() != "{}", (
            "the crash must not leave the `{}` fallback behind: downstream "
            "reads it as 'the base had no tenants'")


@needs_bash
def test_blast_radius_baseline_does_not_go_through_git_checkout_at_all(
        tmp_path) -> None:
    """The nastiest shape, closed structurally rather than by failing loudly.

    This assertion used to be "if the base checkout fails, the step fails" —
    which pinned the injected-failure half of the problem and left the ordinary
    success path (an overlay that keeps PR-added files) wide open. The step now
    builds the baseline from git objects, so `git checkout` is not on its path
    at all: breaking checkout must change nothing.
    """
    repo = _blast_repo(tmp_path)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo,
                {**_blast_env(tmp_path), "FAIL_GIT_CHECKOUT": "1"},
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode == 0, (
        "the step still routes through `git checkout`, so it can still overlay "
        f"the base onto the PR worktree:\n{proc.stdout}\n{proc.stderr}")
    assert (tmp_path / "base-effective.json").exists()


@needs_bash
def test_blast_radius_baseline_fails_when_the_tool_writes_nothing(tmp_path) -> None:
    repo = _blast_repo(tmp_path)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo,
                {**_blast_env(tmp_path), "PY_WRITES_NOTHING": "1"},
                _py_stub_dir(tmp_path, _PY_OK))
    assert proc.returncode != 0, (
        "exit 0 with no file written is still an absent baseline:\n"
        f"{proc.stdout}\n{proc.stderr}")


@needs_bash
def test_blast_radius_keeps_the_pre_v270_empty_base(tmp_path) -> None:
    """⚠️ MUST stay tolerant: a base predating describe-tenant has no
    effective configs to compute, and `{}` is the honest answer."""
    repo = _blast_repo(tmp_path, describe_tenant=False)
    step, script = _base_effective_script(tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_FAIL))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "base-effective.json").read_text(
        encoding="utf-8").strip() == "{}"


# ══ guard-defaults-impact.yml — the verdict step ═══════════════════════════

_OUTPUT_REF = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
# GitHub documents exactly four status-check functions. `always()` and
# `!cancelled()` are the only two that evaluate true both after a successful
# step and after a failed one; `success()` (the implicit default) and
# `failure()` each cover one side only. That closed, externally-defined set is
# what makes listing them here a derivation rather than a guess.
_RUNS_AFTER_FAILURE = re.compile(r"always\s*\(\s*\)|!\s*cancelled\s*\(\s*\)")


_WRITES_OUTPUT = re.compile(r'>>\s*"?\$\{?GITHUB_OUTPUT')
_OUTPUT_NAME = re.compile(r'echo\s+"?([A-Za-z_][\w-]*)=')
_EXITS_NONZERO = re.compile(r"(?m)^\s*exit\s+[1-9]")


def _swallowing_steps(workflow: Path) -> list[tuple[Step, frozenset[str]]]:
    """EVERY step shaped like an exit-code swallower. No claim about how many.

    The shape: the script ends in an unconditional `exit 0` so later steps
    still run, and something is published through `$GITHUB_OUTPUT`.

    ⛔ The shape ALONE is not the verdict producer, and asserting that it was
    is what made this rule fire on an innocent edit. A perfectly ordinary step
    — write a line to `$GITHUB_OUTPUT`, end with `exit 0` — matched it, and
    SEVEN tests went red with "expected exactly one exit-code-swallowing step",
    a message that names no repair and whose seven test names mention nothing
    the author had touched. What distinguishes the real one is not its shape
    but its ROLE: some other step turns its output back into a job failure.
    That pairing is what `_verdict_pairs` derives, and it is derived from the
    workflow rather than declared anywhere.
    """
    found: list[tuple[Step, frozenset[str]]] = []
    for step in _steps(workflow):
        if not _WRITES_OUTPUT.search(step.script):
            continue
        statements = [ln.strip() for ln in step.script.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        if not statements or statements[-1] != "exit 0":
            continue
        found.append((step, frozenset(_OUTPUT_NAME.findall(step.script))))
    return found


def _verdict_pairs(workflow: Path) -> list[tuple[Step, str, frozenset[str],
                                                 Step, tuple[str, str]]]:
    """(producer, its id, its output names, consumer, the ref) for each pair.

    A pair exists when a swallowing step publishes under an `id:` AND a step
    that can exit non-zero reads one of those outputs. Both halves are needed:
    a swallower nobody reads decides nothing, and a reader with no swallower
    upstream is just a step that can fail.
    """
    out = []
    for producer, names in _swallowing_steps(workflow):
        producer_id = producer.raw.get("id")
        if not producer_id or not names:
            continue
        for step in _steps(workflow):
            if step is producer or not _EXITS_NONZERO.search(step.script):
                continue
            refs = _verdict_refs(step, str(producer_id), names)
            if refs:
                out.append((producer, str(producer_id), names, step, refs[0]))
    return out


def _one_verdict_pair(workflow: Path):
    """The single producer/consumer pair, or a failure that says which half.

    Fail-closed on both sides: a silent zero here would leave every rule below
    asserting nothing, which is the very failure mode this module is about.

    ⚠️ `workflow` is a parameter so the fail-closed behaviour itself has
    something to test against. Mutating `== 1` to `>= 0` survived a sweep
    precisely because the shipped workflow has exactly one match, so the
    weakened form never got a chance to be wrong.
    """
    swallowers = _swallowing_steps(workflow)
    pairs = _verdict_pairs(workflow)
    if len(pairs) == 1:
        return pairs[0]
    ids = sorted({str(s.raw.get("id")) for s, _ in swallowers if s.raw.get("id")})
    who = f"{ids[0]}'s" if len(ids) == 1 else "the swallowing step's"
    raise AssertionError(
        f"expected exactly one step rendering {who} verdict in "
        f"{workflow.name}, found {[str(s) for *_, s, _r in pairs]}.\n"
        f"exit-code-swallowing steps present: "
        f"{[str(s) for s, _ in swallowers]} (ids {ids}).\n"
        f"⛔ If you added a step that legitimately reads "
        f"`steps.<id>.outputs.*` and can exit non-zero — a job-summary "
        f"renderer, say — this rule has to learn about it rather than be "
        f"deleted: the gating assertions below must run against EVERY such "
        f"step, because a second one gated on the output being present loses "
        f"the verdict exactly as the first one would.\n"
        f"⚠️ Do NOT get green by moving the `${{{{ }}}}` reference inline into "
        f"`run:` — that hides the step from this scan and is the injection "
        f"pattern this workflow forbids. It is also no longer possible: the "
        f"scan reads the `run:` body too.\n"
        f"⚠️ If NOTHING is listed above, the producer lost its `id:` — an "
        f"output nobody can address is a verdict nobody can read.")


def _swallowing_producer(workflow: Path) -> tuple[Step, str, frozenset[str]]:
    producer, producer_id, names, _consumer, _ref = _one_verdict_pair(workflow)
    return producer, producer_id, names


def _verdict_refs(step: Step, producer_id: str,
                  names: frozenset[str]) -> list[tuple[str, str]]:
    """The producer outputs this step reads, from `env:` AND the `run:` body.

    ⛔ `env:` AND the `run:` body. Scanning only `env:` left the cheapest
    escape from this whole rule wide open, and it is an escape that makes the
    workflow WORSE: move `${{ steps.guard.outputs.exit }}` out of `env:` and
    interpolate it straight into the shell, and this step stops being "the
    verdict step" — measured green, with the gating assertions no longer
    applied to anything. That is also the script-injection shape
    `guard-defaults-impact.yml` forbids in its own comments, so the guard was
    rewarding the thing the file bans.
    """
    sources = list(step.env.values()) + [step.script]
    return sorted({
        (job, out)
        for value in sources
        for job, out in _OUTPUT_REF.findall(value)
        if job == producer_id and out in names
    })


def _verdict_step(workflow: Path = GUARD_DEFAULTS) -> tuple[Step, tuple[str, str]]:
    """The step that turns the swallowed exit code into the job's verdict."""
    *_producer, consumer, ref = _one_verdict_pair(workflow)
    return consumer, ref


def _verdict_gating_problems(workflow: Path = GUARD_DEFAULTS) -> list[str]:
    """The whole #1419 rule for this workflow, as one callable predicate.

    `Run da-guard` always exits 0 and publishes its real verdict through
    `$GITHUB_OUTPUT`. If the step that reads that output also *gates itself* on
    the output being present, a guard that never got that far — skipped because
    no conf.d was detected, or dead before the final echo — silently takes the
    verdict with it and the job goes green.

    ⛔ Production and the negative controls must both call THIS function. The
    first version of the control rebuilt the predicate from an f-string and
    asserted a substring of its own construction — a tautology that stayed
    green while half the real rule was deleted.
    """
    producer_step, (producer, output) = _swallowing_producer(workflow)[0],         _verdict_step(workflow)[1]
    # ⛔ NORMALISED, exactly like the sibling upload rule. `_normalise_expression`
    # was written in this module for that rule and never applied here — so the
    # gate this function's own message says "do not re-add" could be re-added
    # verbatim in index form (`steps['guard'].outputs.exit`) with all 196 tests
    # green, while the dot form beside it went red. One module, two rules
    # reading the same language, and only one of them knew it had a spelling
    # problem: the fix for a class, applied to the cited instance only.
    raw_condition = _verdict_step(workflow)[0].condition or ""
    condition = _normalise_expression(raw_condition)
    problems: list[str] = []
    if f"steps.{producer}.outputs.{output}" in condition:
        problems.append(
            f"gates itself on `steps.{producer}.outputs.{output}`, the very "
            f"value it consumes — an absent verdict then reads as a clean one. "
            f"⛔ Do not re-add this gate to silence a red.")
    if not _RUNS_AFTER_FAILURE.search(condition):
        problems.append(
            f"`if: {condition!r}` does not guarantee the verdict runs after an "
            f"earlier step failed. Use `always()` or `!cancelled()`.")
    # The producer's own precondition has to be carried over. Without this the
    # rule accepts a bare `always()`, which runs the verdict even when the
    # producer was skipped because there was legitimately nothing to validate —
    # a red whose message blames a step failure that never happened, and whose
    # cheapest cure is the gate the first rule forbids.
    precondition = _normalise_expression(
        (producer_step.condition or "").strip())
    if precondition and precondition not in condition:
        problems.append(
            f"`if: {condition!r}` does not carry the producer's own "
            f"precondition ({precondition!r}). Gate on the producer's "
            f"PRECONDITION, never on its RESULT: the first keeps the "
            f"legitimately-nothing-to-do path green, the second is the bypass.")
    # ⛔ …and the SAME event-narrowing analysis the upload rule got. This one
    # guards the merge signal and had none: adding `github.event_name ==
    # 'workflow_dispatch'` to a workflow that also accepts `pull_request` made
    # the verdict skip on every PR — permanently green — with all 196 tests
    # passing. The capability existed in this module the whole time; it had
    # simply never been pointed at the step that decides whether the job fails.
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    problems += _event_predicates_that_lose_runs(
        condition, _workflow_triggers(doc), precondition)
    # ⛔ …and a step that cannot fail is not a verdict. `continue-on-error`
    # leaves the `exit 1` in place and turns the job green anyway — the #1419
    # symptom exactly, and reached without touching the condition at all, so
    # every assertion above is blind to it (measured: nothing went red).
    # ⚠️ The upload is checked only where there is one: the synthetic fixtures
    # the control table feeds this function are verdict-only by design, and
    # demanding an upload step there would make the rule fail on inputs it is
    # not about — a false red inside its own control.
    uploads = [s for s in _steps(workflow)
               if "actions/upload-artifact" in str(s.raw.get("uses") or "")]
    checked = [(_verdict_step(workflow)[0], "verdict")]
    checked += [(s, "evidence upload") for s in uploads]
    for step, role in checked:
        if step.raw.get("continue-on-error"):
            problems.append(
                f"the {role} step carries `continue-on-error: "
                f"{step.raw['continue-on-error']!r}`, so its failure is a "
                f"suggestion — the job goes green with the verdict ignored")
    return problems


def test_the_verdict_step_is_not_gated_on_the_producer_reporting() -> None:
    assert _verdict_gating_problems() == [], _verdict_gating_problems()


@needs_bash
@pytest.mark.parametrize(
    "guard_exit, must_fail",
    [("0", False), ("1", True), ("2", True), ("", True), ("7", True)],
)
def test_the_verdict_step_treats_an_absent_exit_code_as_a_failure(
        tmp_path, guard_exit: str, must_fail: bool) -> None:
    step, _ = _verdict_step()
    proc = _run(step, step.script, tmp_path, {"GUARD_EXIT": guard_exit})
    assert (proc.returncode != 0) is must_fail, (
        f"GUARD_EXIT={guard_exit!r} -> rc={proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}")


# ══ negative controls ══════════════════════════════════════════════════════
#
# Everything above would also pass on a harness that cannot tell the two
# outcomes apart. These run the PRE-FIX text of the same steps — quoted from
# `origin/main` at 64a320ff — through the same harness and require it to
# report the swallow. They are historical fixtures on purpose: their job is to
# prove this module discriminates, not to track the workflows.

_PRE_FIX_EXTRACT = """\
mkdir -p /tmp/old-conf.d
git show "origin/$BASE_REF:components/threshold-exporter/config/conf.d/" > /dev/null 2>&1 || exit 0
for f in $(git ls-tree --name-only "origin/$BASE_REF" components/threshold-exporter/config/conf.d/); do
  fname=$(basename "$f")
  git show "origin/$BASE_REF:$f" > "/tmp/old-conf.d/$fname" 2>/dev/null || true
done
"""

_PRE_FIX_BASE_EFFECTIVE = """\
git stash --include-untracked || true
git checkout "origin/$BASE_REF" -- . 2>/dev/null || true

if [ -f "scripts/tools/dx/describe_tenant.py" ]; then
  python3 scripts/tools/dx/describe_tenant.py \\
    --all \\
    --conf-d "CONF_D_PLACEHOLDER" \\
    --output /tmp/base-effective.json || echo "{}" > /tmp/base-effective.json
else
  echo "{}" > /tmp/base-effective.json
fi

git checkout - -- . 2>/dev/null || true
git stash pop 2>/dev/null || true
"""


@needs_bash
def test_control_the_pre_fix_extraction_left_zero_byte_configs(tmp_path) -> None:
    """`> "$dest"` created the file before git ran, `|| true` hid the error."""
    repo = _base_repo(tmp_path)
    step = _step(CONFIG_DIFF, _S_EXTRACT_BASE)
    old = (tmp_path / "old-conf.d").as_posix()
    script = _sandbox(_PRE_FIX_EXTRACT, tmp_path)
    proc = _run(step, script, repo,
                {**_extract_env(tmp_path), "FAIL_GIT_SHOW_BLOB": "1"},
                _git_stub_dir(tmp_path))

    assert proc.returncode == 0, (
        "the pre-fix control did not reproduce the swallow, so the assertions "
        f"above prove nothing:\n{proc.stdout}\n{proc.stderr}")
    zero_byte = sorted(p.name for p in Path(old).glob("*") if p.stat().st_size == 0)
    assert zero_byte == ["_defaults.yaml", "db-a.yaml", "db-b.yaml"], (
        f"the control must show every tenant config reduced to zero bytes; "
        f"got {zero_byte}")


@needs_bash
def test_control_the_pre_fix_probe_collapsed_git_failure_into_no_confd(
        tmp_path) -> None:
    """`git show <dir> ... || exit 0` read "git is broken" as "the base had no
    conf.d/", and an empty baseline makes every tenant look brand new."""
    repo = _base_repo(tmp_path)
    step = _step(CONFIG_DIFF, _S_EXTRACT_BASE)
    old = (tmp_path / "old-conf.d").as_posix()
    script = _sandbox(_PRE_FIX_EXTRACT, tmp_path)
    proc = _run(step, script, repo,
                {**_extract_env(tmp_path), "FAIL_GIT_SHOW": "1"},
                _git_stub_dir(tmp_path))

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not list(Path(old).glob("*")), (
        "the control must show the empty baseline the probe's `exit 0` left "
        "behind on a base branch that really did have three configs")


@needs_bash
def test_control_the_pre_fix_base_effective_swallowed_a_crash(tmp_path) -> None:
    repo = _blast_repo(tmp_path)
    step = _step(BLAST_RADIUS, _S_BASE_EFFECTIVE)
    out = (tmp_path / "base-effective.json").as_posix()
    script = _sandbox(
        _PRE_FIX_BASE_EFFECTIVE.replace("CONF_D_PLACEHOLDER", EXPORTER_CONF_D),
        tmp_path)
    proc = _run(step, script, repo, _blast_env(tmp_path),
                _py_stub_dir(tmp_path, _PY_FAIL))

    assert proc.returncode == 0, (
        f"the pre-fix control did not reproduce the swallow:\n{proc.stdout}")
    assert Path(out).read_text(encoding="utf-8").strip() == "{}", (
        "the control must show the `{}` baseline that made every tenant read "
        "as brand new")


@needs_bash
def test_the_verdict_step_names_an_absent_verdict_as_its_own_case(
        tmp_path) -> None:
    """An empty exit code and a nonsense one are different accidents.

    Both must fail, and the rc-only assertions above cannot tell them apart —
    deleting the `""` branch and letting the catch-all handle it survived a
    mutation sweep for exactly that reason. What is asserted here is that the
    two produce *different* text, not any particular wording: pinning the
    prose would just be a second place to update.
    """
    step, _ = _verdict_step()
    absent = _run(step, step.script, tmp_path, {"GUARD_EXIT": ""})
    nonsense = _run(step, step.script, tmp_path, {"GUARD_EXIT": "7"})

    assert absent.returncode != 0 and nonsense.returncode != 0
    # Comparing the two messages directly is not enough: a shared catch-all
    # branch already yields different text, because one interpolates "7" and
    # the other interpolates nothing. Blanking the value out is what makes the
    # comparison mean "the absent case has its own branch" — the first draft
    # of this assertion missed that and survived the mutation it was written
    # for.
    catch_all_with_value_blanked = (nonsense.stdout + nonsense.stderr).replace("7", "")
    assert (absent.stdout + absent.stderr) != catch_all_with_value_blanked, (
        "'the guard never reported' is being rendered by the same catch-all as "
        "'the guard returned 7', so the log describes an unexpected value "
        "rather than a verdict that never arrived")


def _synthetic(tmp_path: Path, steps: str) -> Path:
    path = tmp_path / "synthetic.yml"
    path.write_text(
        "name: Synthetic\non: [push]\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n" + steps,
        encoding="utf-8", newline="\n")
    return path


_PRODUCER = """\
      - name: Producer
        id: guard
        run: |
          echo "exit=0" >> "$GITHUB_OUTPUT"
          exit 0
"""
_CONSUMER = """\
      - name: {name}
        env:
          GUARD_EXIT: ${{{{ steps.guard.outputs.exit }}}}
        run: |
          exit 1
"""


@pytest.mark.parametrize("steps, expected", [
    ("", "exit-code-swallowing"),
    (_PRODUCER * 2, "exit-code-swallowing"),
    (_PRODUCER, "rendering guard's verdict"),
    (_PRODUCER + _CONSUMER.format(name="A") + _CONSUMER.format(name="B"),
     "rendering guard's verdict"),
])
def test_control_the_verdict_discovery_is_fail_closed(
        tmp_path, steps: str, expected: str) -> None:
    """Zero matches and two matches must both raise.

    Without this the cardinality check is unfalsifiable: the shipped workflow
    has exactly one of each, so weakening `== 1` to `>= 0` changes nothing
    observable and the assertion is decoration.
    """
    with pytest.raises(AssertionError, match=re.escape(expected)):
        _verdict_step(_synthetic(tmp_path, steps))


def test_control_the_verdict_discovery_accepts_a_well_formed_pair(
        tmp_path) -> None:
    """…and the rejection above must not be "refuse everything"."""
    workflow = _synthetic(tmp_path, _PRODUCER + _CONSUMER.format(name="Verdict"))
    step, ref = _verdict_step(workflow)
    assert step.name == "Verdict" and ref == ("guard", "exit")


# A perfectly ordinary step: publish something for a later step to render, end
# with `exit 0` so a non-zero from the tool does not fail the job here. It has
# the swallower's SHAPE and none of its role — nothing turns `summary.rows`
# back into a verdict.
_INNOCENT = """\
      - name: Publish a job summary
        id: summary
        run: |
          echo "rows=3" >> "$GITHUB_OUTPUT"
          exit 0
"""


def test_control_an_output_writing_step_is_not_mistaken_for_the_verdict(
        tmp_path) -> None:
    """⛔ The false red this pairing was introduced to remove.

    Shape alone made this step "a second exit-code-swallowing step" and turned
    SEVEN tests red — none of whose names mention job summaries — with a
    message that named no repair. The role is what distinguishes them: nothing
    reads `steps.summary.outputs.*` and then exits non-zero, so it decides
    nothing.

    ⚠️ Two-sided with the row above it: if a step DOES read the innocent
    producer's output and can fail, that is a second verdict path and the
    fail-closed message is correct to fire — `_CONSUMER` is parametrised on the
    id precisely so this control cannot be satisfied by refusing everything.
    """
    workflow = _synthetic(
        tmp_path, _PRODUCER + _INNOCENT + _CONSUMER.format(name="Verdict"))
    step, ref = _verdict_step(workflow)
    assert step.name == "Verdict" and ref == ("guard", "exit")
    assert [s.name for s, _ in _swallowing_steps(workflow)] == [
        "Producer", "Publish a job summary"], (
        "the shape scan must still SEE both steps — this control has to be "
        "about which one is the verdict, not about hiding one of them")


# The same consumer, with the reference interpolated straight into `run:`
# instead of bound through `env:`.
_CONSUMER_INLINE = """\
      - name: {name}
        run: |
          echo "guard said ${{{{ steps.guard.outputs.exit }}}}"
          exit 1
"""


def test_control_a_verdict_referenced_only_in_run_is_still_found(
        tmp_path) -> None:
    """⛔ The escape `_verdict_refs` says it closed, with something watching it.

    Scanning only `env:` left the cheapest way out of this whole rule open,
    and it is an escape that makes the workflow WORSE — moving the reference
    inline is the script-injection shape `guard-defaults-impact.yml` forbids in
    its own comments. The fix (reading the `run:` body too) had NO control:
    blind review reverted it and the module stayed green, while the
    failure message went on promising maintainers "it is also no longer
    possible: the scan reads the `run:` body too". A promise nothing tests is
    the same thing as no promise.

    ⚠️ Both halves, deliberately: the sibling control above uses the `env:`
    binding, so between them neither source can be dropped silently (measured
    — dropping `env:` instead reddens fifteen tests, dropping `run:` reddened
    none until this test existed).
    """
    workflow = _synthetic(
        tmp_path, _PRODUCER + _CONSUMER_INLINE.format(name="Verdict"))
    step, ref = _verdict_step(workflow)
    assert step.name == "Verdict" and ref == ("guard", "exit")


def test_control_the_detect_step_lookup_refuses_a_file_it_has_no_name_for(
) -> None:
    """`_S_DETECT` is per workflow; borrowing another file's name is the bug.

    ⛔ Without this, replacing `_S_DETECT[workflow]` with
    `_S_DETECT.get(workflow, "Detect conf.d path")` — a silent fallback onto
    another file's spelling — left the module green.
    """
    with pytest.raises(AssertionError, match="no entry in `_S_DETECT`"):
        _detect_step(CONFIG_DIFF)


# ⚠️ DISCLOSED, once, for every `parametrize` control in this file: emptying a
# sample table does not turn anything red. pytest renders a zero-row
# parametrize as `1 skipped` with rc=0, so a merge conflict resolved by
# deleting both sides, or a tidy-up of "stale" rows, silently removes the
# control while CI stays green. Measured on one table as representative: the
# module went from all-passing to a smaller total with a `1 skipped` line and
# rc=0. The visible signals are that skip line and the smaller total; neither
# is asserted. ⛔ No module count in this sentence either — the count that used
# to be here was added AFTER the sweep that claimed none were left, which is
# how it survived: a class scan is only true as of the edit it ran on. This is a
# property of pytest rather than of any rule here, and it is written down
# instead of guarded because a guard for it would be a floor — either a number
# that expires or one derived from the table it is protecting.


# ── controls for the scope chain, on synthetic workflows ───────────────────
#
# ⛔ `_scope_pipeline` exists for a world that does not exist yet: the chain is
# one step today, so every derivation in it is exercised only in its trivial
# case. Blind review collapsed the reach-back to `first = last` and weakened
# all three fail-closed assertions, and each time nothing went red. The
# mechanism was born unwatched — for the fifth time on this line —
# so the controls below feed it the shapes it was written for.
_SCOPE_LIST = """\
      - name: List changed defaults
        run: |
          git diff -z --name-only a...b > "$RUNNER_TEMP/changed.txt"
"""
_SCOPE_RESOLVE = """\
      - name: {name}
        run: |
          python3 scripts/ops/guard_defaults_scopes.py --null - \\
            < "$RUNNER_TEMP/changed.txt" > "$RUNNER_TEMP/targets.tsv"
"""
_SCOPE_RESOLVE_SHELL = """\
      - name: Resolve
        shell: bash
        run: |
          python3 scripts/ops/guard_defaults_scopes.py --null - \\
            < "$RUNNER_TEMP/changed.txt" > "$RUNNER_TEMP/targets.tsv"
"""
_SCOPE_RESOLVE_ENV = """\
      - name: Resolve
        env:
          SOMETHING_NEW: ${{ github.sha }}
        run: |
          python3 scripts/ops/guard_defaults_scopes.py --null - \\
            < "$RUNNER_TEMP/changed.txt" > "$RUNNER_TEMP/targets.tsv"
"""
# …and the same key with a LITERAL value, which is reproduced rather than
# refused: the harness can set it, so there is nothing to fail closed about.
_SCOPE_RESOLVE_LITERAL_ENV = """\
      - name: Resolve
        env:
          PYTHONUNBUFFERED: "1"
        run: |
          python3 scripts/ops/guard_defaults_scopes.py --null - \\
            < "$RUNNER_TEMP/changed.txt" > "$RUNNER_TEMP/targets.tsv"
"""


def test_control_the_scope_chain_reaches_back_to_the_listing(tmp_path) -> None:
    """A split step must give a TWO-step chain, in job order.

    This is the whole point of the reach-back: after the split, running only
    the helper step measures a listing nothing produced.
    """
    workflow = _synthetic(
        tmp_path, _SCOPE_LIST + _SCOPE_RESOLVE.format(name="Resolve"))
    chain, _env = _scope_pipeline(workflow)
    assert [s.name for s in chain] == ["List changed defaults", "Resolve"]


@pytest.mark.parametrize("steps, expected", [
    (_SCOPE_LIST, "expected exactly one step invoking"),
    (_SCOPE_LIST + _SCOPE_RESOLVE.format(name="A")
     + _SCOPE_RESOLVE.format(name="B"), "expected exactly one step invoking"),
    (_SCOPE_LIST + _SCOPE_RESOLVE_SHELL, "spans more than one"),
    (_SCOPE_LIST + _SCOPE_RESOLVE_ENV, "Actions expressions the harness has"),
], ids=["no-consumer", "two-consumers", "mixed-shells", "templated-env-key"])
def test_control_the_scope_chain_is_fail_closed(tmp_path, steps, expected):
    """Each of the three refusals, on the shape it was written for."""
    with pytest.raises(AssertionError, match=re.escape(expected)):
        _scope_pipeline(_synthetic(tmp_path, steps))


def test_control_a_literal_env_key_is_reproduced_not_refused(tmp_path) -> None:
    """⛔ The other half of the `env:` contract, and the false red it removes.

    Refusing every unfamiliar key made an inert addition — `PYTHONUNBUFFERED`
    on a step that runs python3 — cost a workflow edit plus two test edits, and
    the message justified it with `set -u`, which is false for a key the script
    never references. A literal value is one the harness can simply apply, so
    it does: CI sets it, the harness sets it, and there is nothing left to fail
    closed about.

    ⚠️ The narrower alternative ("refuse only keys the script mentions") was
    rejected rather than untried: this step invokes a Python program, so
    `PYTHONHASHSEED` and friends are consumed without appearing in the shell
    text — that predicate would wave through the ones that matter.
    """
    chain, literal = _scope_pipeline(
        _synthetic(tmp_path, _SCOPE_LIST + _SCOPE_RESOLVE_LITERAL_ENV))
    assert [s.name for s in chain] == ["List changed defaults", "Resolve"]
    assert literal == {"PYTHONUNBUFFERED": "1"}, literal


_VERDICT_CONDITIONS = """\
      - name: Producer
        id: guard
        if: steps.detect.outputs.conf_d != ''
        run: |
          echo "exit=0" >> "$GITHUB_OUTPUT"
          exit 0
      - name: Verdict
        if: {cond}
        env:
          GUARD_EXIT: ${{{{ steps.guard.outputs.exit }}}}
        run: |
          exit 1
"""


@pytest.mark.parametrize("condition, must_be_rejected", [
    # the pre-fix shape, verbatim — must fail BOTH halves
    ("steps.guard.outputs.exit != ''", True),
    # the #1419 hole that the FIRST half cannot see: it names a different
    # step's output, so only the runs-after-failure half rejects it
    ("steps.detect.outputs.conf_d != ''", True),
    # implicit success() — skipped exactly when an earlier step failed
    ("github.event_name == 'pull_request'", True),
    # bare always(): survives a failure and names no output, but drops the
    # producer's precondition — the benign "nothing to validate" path then
    # fails with a message blaming a step failure that never happened
    ("${{ always() }}", True),
    # the shipped shape
    ("${{ !cancelled() && steps.detect.outputs.conf_d != '' }}", False),
    ("${{ always() && steps.detect.outputs.conf_d != '' }}", False),
])
def test_control_the_verdict_rule_rejects_the_shapes_it_was_written_for(
        tmp_path, condition: str, must_be_rejected: bool) -> None:
    """Both halves of the rule need a synthetic input that exercises them.

    Deleting the runs-after-failure half used to leave the suite green, because
    nothing fed the rule a condition that only that half rejects — and that
    half is the only one that sees a verdict gated on the DETECT step's output,
    which is the #1419 hole in its most natural disguise.
    """
    workflow = _synthetic(tmp_path, _VERDICT_CONDITIONS.format(cond=condition))
    problems = _verdict_gating_problems(workflow)
    assert bool(problems) is must_be_rejected, (
        f"condition {condition!r} -> problems={problems}")


@needs_bash
@pytest.mark.parametrize("workflow, event, must_fail", [
    # blast-radius accepts pull_request only, so it has no second arm and no
    # other event to assert. Parametrising it over workflow_dispatch/schedule
    # pinned behaviour for events that workflow cannot receive.
    (BLAST_RADIUS, "pull_request", True),
    (GUARD_DEFAULTS, "pull_request", True),
    (GUARD_DEFAULTS, "workflow_dispatch", False),
], ids=["blast-radius-pr", "guard-defaults-pr", "guard-defaults-dispatch"])
def test_detect_refuses_to_find_nothing_on_a_pull_request(
        tmp_path, workflow: Path, event: str, must_fail: bool) -> None:
    """"Nothing to validate" is a manual-run answer, not a PR answer.

    Both workflows gate every later step on this output, and neither has any
    step that fires when it is empty. So on a PR — where the `paths:` filter
    already established that the very tree in question changed — an empty
    value meant a green run that analysed nothing. Manual runs against a repo
    with a different layout keep the old warning.
    """
    step = _detect_step(workflow)
    script = _render(step.script, {"github.event.inputs.config_dir": ""})
    empty = tmp_path / "no-conf-d"
    empty.mkdir()
    proc = _run(step, script, empty, {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_OUTPUT": str((tmp_path / "gh-output").as_posix()),
        "INPUT_DIR": "",
    })
    assert (proc.returncode != 0) is must_fail, (
        f"event={event} -> rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")


@needs_bash
@pytest.mark.parametrize("workflow", [BLAST_RADIUS, GUARD_DEFAULTS],
                         ids=["blast-radius", "guard-defaults"])
def test_detect_still_resolves_a_present_tree_on_a_pull_request(
        tmp_path, workflow: Path) -> None:
    """…and the refusal above must not fire when the tree is there."""
    step = _detect_step(workflow)
    script = _render(step.script, {"github.event.inputs.config_dir": ""})
    tree = tmp_path / "present"
    (tree / EXPORTER_CONF_D).mkdir(parents=True)
    out = tmp_path / "gh-output-present"
    proc = _run(step, script, tree, {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_OUTPUT": str(out.as_posix()),
        "INPUT_DIR": "",
    })
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert f"conf_d={EXPORTER_CONF_D}" in out.read_text(encoding="utf-8")


def _step_using(workflow: Path, action: str) -> Step:
    hits = [s for s in _steps(workflow) if action in str(s.raw.get("uses") or "")]
    assert len(hits) == 1, (
        f"expected exactly one step using {action} in {workflow.name}, "
        f"found {[s.name for s in hits]}")
    return hits[0]


@needs_bash
def test_da_guard_flags_an_unrecognised_scope_kind_instead_of_dropping_it(
        tmp_path) -> None:
    """A `case` with no catch-all drops a row and leaves the verdict at 0.

    `guard_defaults_scopes.py` emits two kinds today, so this is latent — but
    the whole reason that script exists is that a file it cannot place gets
    REPORTED rather than skipped, and the caller had a path that silently
    undid it.
    """
    runner_temp, targets = _guard_fixture(tmp_path, "surprise\ttree-z\ttree-z\n")
    step = _step(GUARD_DEFAULTS, _S_RUN_DA_GUARD)
    script = _render(step.script, {
        "runner.temp": str(runner_temp.as_posix()),
        "steps.detect.outputs.conf_d": "tree-a",
    })
    out = tmp_path / "gh-output"
    proc = _run(step, script, tmp_path, {
        "RUNNER_TEMP": str(runner_temp.as_posix()),
        "TARGETS": str(targets.as_posix()),
        "CONF_D": "tree-a",
        "GITHUB_OUTPUT": str(out.as_posix()),
    })
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "exit=0" not in out.read_text(encoding="utf-8"), (
        "an unrecognised scope kind left the verdict at 0, so the row vanished "
        "and the job stayed green")


@needs_bash
def test_da_guard_names_a_tree_it_could_not_validate(tmp_path) -> None:
    """An empty section under a heading reads as "no findings".

    da-guard writes `--output` only once it reaches writeReport
    (`cmd/da-guard/main.go:191`), so a run that caller-errors before that point
    produces no report at all. ⛔ The count of those early returns is
    DELIBERATELY not restated here: it has been written down wrong four times
    (two drafts of the workflow comment said six then seven; this module then
    said seven in one docstring and six in another, for the same returns).
    `guard-defaults-impact.yml` carries the enumeration with line numbers and is
    the only place that should — a second copy of a count is a second thing to
    get stale. So the tree whose run died early gets a heading and, without
    this, nothing under it — which is the #1219 shape wearing a different hat.
    """
    runner_temp, targets = _guard_fixture(
        tmp_path, "target\ttree-a\ttree-a\ntarget\ttree-b\ttree-b\n")
    step = _step(GUARD_DEFAULTS, _S_RUN_DA_GUARD)
    script = _render(step.script, {
        "runner.temp": str(runner_temp.as_posix()),
        "steps.detect.outputs.conf_d": "tree-a",
    })
    proc = _run(step, script, tmp_path, {
        "RUNNER_TEMP": str(runner_temp.as_posix()),
        "TARGETS": str(targets.as_posix()),
        "CONF_D": "tree-a",
        "GITHUB_OUTPUT": str((tmp_path / "gh-output").as_posix()),
    })
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    report = (runner_temp / "guard-report.md").read_text(encoding="utf-8")
    tree_b = report.split("`tree-b`", 1)[-1]
    assert "NOT VALIDATED" in tree_b, (
        f"tree-b was never validated and its section says nothing:\n{report}")
    # …and the worst exit code across the trees has to reach the verdict, or the
    # tree that failed is described in the comment while the job stays green.
    assert "exit=2" in (tmp_path / "gh-output").read_text(encoding="utf-8"), (
        "tree-b's caller error did not propagate to the published verdict")


def _guard_fixture(tmp_path: Path, targets_text: str) -> tuple[Path, Path]:
    """A da-guard that reports for tree-a and caller-errors for anything else."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    (runner_temp / "da-guard").write_text(
        "#!/usr/bin/env bash\n"
        "out=\"\"; dir=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in --output) out=\"$2\";; --config-dir) dir=\"$2\";; esac\n"
        "  shift\n"
        "done\n"
        "case \"$dir\" in\n"
        "  *tree-a) printf 'ERROR: dangling default in tree-a\\n' > \"$out\"; exit 0 ;;\n"
        "  *) echo 'caller error' >&2; exit 2 ;;\n"
        "esac\n",
        encoding="utf-8", newline="\n")
    (runner_temp / "da-guard").chmod(0o755)
    targets = runner_temp / "guard-targets.tsv"
    targets.write_text(targets_text, encoding="utf-8", newline="\n")
    return runner_temp, targets


@needs_bash
def test_da_guard_report_does_not_carry_findings_between_trees(tmp_path) -> None:
    """One scratch file, reused per tree, never truncated.

    da-guard only writes `--output` once it reaches `writeReport`
    (`cmd/da-guard/main.go:191`); the caller-error returns above it are
    enumerated once, in `guard-defaults-impact.yml` — see the sibling test for
    why this docstring does not repeat the number. So a tree whose run died
    early appended the PREVIOUS tree's findings under its own heading — a report describing a tree that was never validated, which is
    exactly the #1219 shape. The run is red in that case, but the reviewer is
    reading a comment that names the wrong tree.
    """
    step = _step(GUARD_DEFAULTS, _S_RUN_DA_GUARD)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # tree-a: writes findings, exits 0.  tree-b: caller error, writes nothing.
    _stub(runner_temp / "da-guard" if False else bin_dir, "da-guard-shim", "")
    (runner_temp / "da-guard").write_text(
        "#!/usr/bin/env bash\n"
        "out=\"\"; dir=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in --output) out=\"$2\";; --config-dir) dir=\"$2\";; esac\n"
        "  shift\n"
        "done\n"
        "case \"$dir\" in\n"
        "  *tree-a) printf 'ERROR: dangling default in tree-a\\n' > \"$out\"; exit 0 ;;\n"
        "  *) echo 'caller error' >&2; exit 2 ;;\n"
        "esac\n",
        encoding="utf-8", newline="\n")
    (runner_temp / "da-guard").chmod(0o755)

    targets = runner_temp / "guard-targets.tsv"
    targets.write_text("target\ttree-a\ttree-a\ntarget\ttree-b\ttree-b\n",
                       encoding="utf-8", newline="\n")

    script = _render(step.script, {
        "runner.temp": str(runner_temp.as_posix()),
        "steps.detect.outputs.conf_d": "tree-a",
    })
    proc = _run(step, script, tmp_path, {
        "RUNNER_TEMP": str(runner_temp.as_posix()),
        "TARGETS": str(targets.as_posix()),
        "CONF_D": "tree-a",
        "GITHUB_OUTPUT": str((tmp_path / "gh-output").as_posix()),
    })
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    report = (runner_temp / "guard-report.md").read_text(encoding="utf-8")
    tree_b_section = report.split("`tree-b`", 1)[-1]
    assert "tree-a" not in tree_b_section, (
        "tree-b's section carries tree-a's findings — the comment describes a "
        f"tree that was never validated:\n{report}")


@needs_bash
@pytest.mark.parametrize("injection, subcommand", [
    ("FAIL_GIT_LS_TREE", "ls-tree"),
    ("FAIL_GIT_SHOW", "show"),
    # ⛔ Born in the same edit as the `diff` arm itself. A stub arm with no
    # control of its own is how this module already lost an injector once —
    # it kept being passed and kept injecting nothing.
    ("FAIL_GIT_DIFF", "diff"),
])
def test_control_the_git_stub_still_injects_behind_a_dash_c_option(
        tmp_path, injection: str, subcommand: str) -> None:
    """The injector needs an injector-check of its own.

    When the scripts grew `git -c core.quotePath=false ls-tree`, the stub was
    reading `$1` and saw `-c`, matched nothing, and delegated to real git — it
    stopped injecting entirely. Nothing noticed, because a stub that does not
    fail simply makes every "must fail" assertion prove a different thing.
    """
    repo = _base_repo(tmp_path, conf_d=False)
    bin_dir = _git_stub_dir(tmp_path)
    target = {"ls-tree": "HEAD", "show": "HEAD:README.md",
              "diff": "--name-only HEAD"}[subcommand]
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f'git -c core.quotePath=false {subcommand} {target} >/dev/null\n',
        encoding="utf-8", newline="\n")

    def rc(extra: dict[str, str]) -> int:
        # ⛔ Through bash, not through subprocess directly: the stub is an
        # extensionless shell script, which Windows will not exec on its own.
        # Calling it the way the harness does is the only measurement that
        # says anything about the harness.
        env = {**os.environ, "REAL_GIT": shutil.which("git") or "git",
               "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **extra}
        return subprocess.run([BASH, "-e", str(probe)], cwd=repo, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120).returncode

    assert rc({}) == 0, "the stub broke the subcommand even without an injection"
    assert rc({injection: "1"}) != 0, (
        f"{injection} did not reach `{subcommand}` behind `-c` — the stub is "
        f"delegating to real git and injecting nothing")


@pytest.mark.parametrize("line, expected", [
    # ⛔ Must be seen. Each of the first three was measured escaping the rule:
    # injecting them into blast-radius.yml left every assertion about `-z`
    # green and only unrelated behavioural tests went red.
    ("git ls-tree -r -z origin/main -- conf.d/ > f", True),
    ("git -c core.quotePath=false ls-tree -r origin/main -- conf.d/ > f", True),
    ("/usr/bin/git diff --name-only a...b > f", True),
    ("if git diff --name-only a...b > f; then :; fi", True),
    ("git -C /src -c x.y=1 ls-files -z > f", True),
    ("GIT_DIR=/x git diff --name-only a > f", True),
    ("env FOO=1 git diff --name-only a > f", True),
    ("eval git ls-files -z > f", True),
    ("cat x | git diff --name-only a > f", True),
    # ⛔ Must NOT be seen — the rule drifting into "see everything" would
    # demand `-z` on an echo, which is where it started.
    ('echo "::error::git ls-tree exited $rc for origin/$BASE_REF"', False),
    ('echo "run git diff --name-only to reproduce"', False),
    ("git commit -qm base", False),
    ("git rev-parse --verify --quiet origin/main", False),
    ("python3 tool.py --mode diff > f", False),
    # ⛔ …and the diagnostics `-z` does nothing to. Demanding the flag on these
    # was a false red whose cheapest repair (`git diff -z --stat`) is a no-op
    # by git's own documentation — after which the flag's presence stops being
    # evidence anywhere. Measured: adding `git diff --stat` to a workflow as a
    # debugging aid turned this rule red.
    ("git diff --stat origin/main...HEAD", False),
    ("git diff --shortstat origin/main...HEAD", False),
    ("git diff --quiet origin/main...HEAD -- conf.d/", False),
    ("git diff origin/main...HEAD -- conf.d/ > patch", False),
    # …while every format `-z` DOES act on stays in scope, including alongside
    # a summary flag.
    ("git diff --stat --name-only a...b > f", True),
    ("git diff --numstat a...b > f", True),
    ("git diff --name-status a...b > f", True),
    ("git diff --raw a...b > f", True),
    # ⚠️ Disclosed rather than exempted: `ls-files --error-unmatch` is usually
    # an existence probe whose output nobody reads, and it is still asked for
    # `-z`. That red is accepted because the cheapest repair — adding `-z` —
    # genuinely changes the output there, so unlike the `--stat` case it costs
    # the rule nothing. There is no shape that answers "is this output parsed?";
    # `_DISPLAY_ONLY_LISTINGS` is where a measured exception goes.
    ("git ls-files --error-unmatch conf.d/x >/dev/null", True),
    # ⛔ Command substitution. `blast-radius.yml` writes its header listing as
    # `changed_raw=$(git diff --name-only …)`, so this is not a hypothetical
    # spelling — it is the shipped one. Blind review removed `\$\(` from the
    # segment splitter and the module stayed green, because that one
    # live line is covered by the verbatim exemption further down: the rule
    # would have gone blind to every FUTURE `x=$(git ls-files …)` while today's
    # tree looked fine.
    ("changed_raw=$(git diff -z --name-only a...b)", True),
    ("out=$(git ls-files -z)", True),
], ids=["plain", "dash-c", "absolute-path", "if-prefix", "dash-C-and-c",
        "var-assignment-prefix", "env-prefix", "eval-prefix", "after-pipe",
        "echo-about-it", "echo-instructions", "other-subcommand", "rev-parse",
        "not-git", "diff-stat", "diff-shortstat", "diff-quiet", "diff-patch",
        "stat-and-name-only", "numstat", "name-status", "raw",
        "ls-files-error-unmatch", "command-substitution", "capture-ls-files"])
def test_control_the_listing_detector_sees_the_shapes_it_claims(
        line: str, expected: bool) -> None:
    """Two-sided samples for the predicate that decides WHAT gets checked.

    ⛔ A classifier's misses look like passes: a listing the detector does not
    recognise is not judged non-compliant, it is never judged at all. This
    table is the control that the rule's three previous versions never had —
    every one of them was extended after being punched through, and none of
    them grew a sample of the shape that punched it.
    """
    assert _lists_paths(line) is expected, line


@pytest.mark.parametrize("line, expected", [
    ("python3 scripts/ops/guard_defaults_scopes.py --null - < f > t", True),
    ("python scripts/ops/guard_defaults_scopes.py --null -", True),
    ("./scripts/ops/guard_defaults_scopes.py --null -", True),
    ("cat f | python3 scripts/ops/guard_defaults_scopes.py --null -", True),
    # ⛔ Must NOT be seen. All four of these are live lines in `Run da-guard`,
    # and substring matching counted them: the step that REPORTS a malformed
    # row names the producer while explaining whose bug it is. Discovery that
    # counts them finds two "scope steps" and fails closed on a workflow that
    # is perfectly correct.
    ('echo "::error::unreadable row — produced by guard_defaults_scopes.py, '
     'so this is a producer bug"', False),
    ('echo "::error::unrecognised scope kind — guard_defaults_scopes.py and '
     'this workflow disagree"', False),
    ('echo "align \\`guard_defaults_scopes.py\\` with this file" >> "$REPORT"',
     False),
    ("grep -c guard_defaults_scopes.py Makefile", False),
], ids=["python3", "python", "direct", "after-pipe", "error-message",
        "second-error-message", "report-body", "greps-for-it"])
def test_control_the_scope_helper_detector_separates_running_from_naming(
        line: str, expected: bool) -> None:
    """Command position, both directions.

    ⚠️ `grep -c …` is the row that keeps this from being "anything with the
    filename in argv": naming the helper as an ARGUMENT to something else is
    not running it.
    """
    assert _invokes_scope_helper(line) is expected, line


def test_control_the_step_locator_is_fail_closed() -> None:
    with pytest.raises(AssertionError, match="exactly one step named"):
        _step(CONFIG_DIFF, "a step that does not exist")


def test_control_the_expression_renderer_is_fail_closed() -> None:
    with pytest.raises(AssertionError, match="unmapped Actions expressions"):
        _render("echo ${{ github.sha }}", {})


@pytest.mark.parametrize("script, expected", [
    ("echo x > /tmp/report.md", "echo x > SANDBOX/report.md"),
    # ⛔ The spelling GitHub documents, and the one the sibling workflow in this
    # same PR already uses. It must rewrite to the SAME sandboxed name, or
    # adopting it turns this module's behavioural tests red for no behaviour
    # change at all.
    ('echo x > "$RUNNER_TEMP/report.md"', 'echo x > "SANDBOX/report.md"'),
    ('echo x > "${RUNNER_TEMP}/report.md"', 'echo x > "SANDBOX/report.md"'),
    ("echo x > ${{ runner.temp }}/report.md", "echo x > SANDBOX/report.md"),
    # …and a comment naming the path is rewritten too, harmlessly, because the
    # rewrite is textual. What matters is the negative case below.
    ("# writes /tmp/report.md\ncat /tmp/report.md",
     "# writes SANDBOX/report.md\ncat SANDBOX/report.md"),
], ids=["tmp", "runner-temp-var", "runner-temp-braced", "runner-temp-expr",
        "comment-and-command"])
def test_control_the_sandbox_rewrites_every_spelling(tmp_path, script, expected):
    """Both spellings name one artifact, so both must land in one sandbox."""
    got = _sandbox(script, tmp_path)
    assert got == expected.replace("SANDBOX", tmp_path.as_posix())


@pytest.mark.parametrize("script", [
    'f=/tmp/$name; echo x > "$f"',
    'd=/tmp\necho x > "$d/report.md"',
], ids=["interpolated-basename", "assembled-from-a-directory-var"])
def test_control_the_sandbox_is_fail_closed_on_an_assembled_path(tmp_path, script):
    """The other direction: a path the rewriter cannot see must not run.

    ⛔ This is the assertion the literal-mapping version bought with its false
    reds, and it is kept because it is the one with teeth — an un-sandboxed run
    writes into the real `/tmp`, where a test can pass for reasons that have
    nothing to do with what it claims to measure.
    """
    with pytest.raises(AssertionError, match="still names `/tmp`"):
        _sandbox(script, tmp_path)


# ── the producer and the consumer must agree on where the baseline is ──────

@pytest.mark.parametrize("workflow, producer_name, consumer_name", [
    (CONFIG_DIFF, _S_EXTRACT_BASE, _S_RUN_CONFIG_DIFF),
    (BLAST_RADIUS, _S_BASE_EFFECTIVE, _S_COMPUTE_BLAST),
], ids=["config-diff", "blast-radius"])
def test_the_baseline_the_producer_writes_is_the_one_the_consumer_reads(
        workflow: Path, producer_name: str, consumer_name: str) -> None:
    """A producer/consumer split is invisible to every behavioural test above.

    The harness sandboxes both ends into one directory, so a run where the
    producer writes A and the consumer reads B would still be green there.
    This is the assertion that says they are one artifact.

    ⛔ Compared as artifact NAMES, not as literal paths. Pinning
    `"/tmp/base-effective.json"` made an ordinary portability edit — moving to
    `$RUNNER_TEMP`, which the third workflow in this same PR already uses —
    red here as well as across this module's behavioural tests, and the
    message named the constant nowhere.

    ⚠️ What this does NOT catch, stated rather than left to be discovered: if
    the producer names two artifacts and only one of them moves, the
    intersection can stay non-empty. There is no shape that answers "which of
    these paths is THE output" without re-implementing each tool's argv, so the
    behavioural tests above — which run the step and read the file — are what
    covers that; this one covers the split.
    """
    produced = _artifact_names(_step(workflow, producer_name).script)
    consumed = _artifact_names(_step(workflow, consumer_name).script)
    assert produced, (
        f"{producer_name} names no scratch artifact at all, so this assertion "
        f"would be comparing two empty sets and passing")
    assert produced & consumed, (
        f"{producer_name} writes {sorted(produced)} and {consumer_name} reads "
        f"{sorted(consumed)} — they no longer share an artifact, so the "
        f"consumer is reading something this workflow never wrote. Either end "
        f"may be renamed; both ends have to move together")


# ── hardening added after this module was written ──────────────────────────
#
# ⛔ Every assertion below exists because the branch it covers was added to a
# workflow WITHOUT a sample, and blind review then reverted all four in one
# edit and nothing went red. The mechanical version of that measurement is
# cheaper than a mutation run and is the one to repeat: grep each new
# identifier and see how many times it appears on the negative-control side.
# Zero means the branch was born unguarded. It was zero for `diff-report`,
# for the override rejection, for the unreadable-row and unrecognised-kind
# report bodies, and for the blast-radius upload condition.


@needs_bash
@pytest.mark.parametrize("rc, body, must_fail", [
    # Both accepted exit codes, with nothing written. rc=1 is the live one:
    # the pinned image tracebacks with rc=1 on malformed YAML, and rc=1 is
    # also "changes detected", so the exit code cannot separate them.
    ("0", "", True),
    ("1", "", True),
    # …and the same two codes WITH a report must stay green, or the check is
    # just a way to make every run red.
    ("0", "# Config Diff Report\n\nNo changes detected.\n", False),
    ("1", "# Config Diff Report\n\n- db-a: mysql_connections 70 -> 35\n", False),
    # Control that the harness can see a failure at all: the pre-existing
    # `rc > 1` branch, which is not what this test is about.
    ("2", "irrelevant\n", True),
], ids=["rc0-empty", "rc1-empty", "rc0-report", "rc1-report", "rc2-control"])
def test_config_diff_fails_when_an_accepted_rc_wrote_no_report(
        tmp_path, rc: str, body: str, must_fail: bool) -> None:
    """An accepted exit code with an empty report is not "no changes".

    "The comparison produced nothing" and "the comparison found nothing" reach
    the reviewer as the same green check with no comment, which is this
    workflow's own defect class.
    """
    step = _step(CONFIG_DIFF, _S_RUN_CONFIG_DIFF)
    script = _render(_sandbox(step.script, tmp_path),
                     {"github.workspace": str(tmp_path.as_posix())})
    report = tmp_path / "diff-report.md"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The step redirects the tool's stdout into the report, so the stub only
    # has to print and pick an exit code — no knowledge of docker's argv.
    _stub(bin_dir, "docker",
          '#!/usr/bin/env bash\nprintf "%s" "$FAKE_BODY"\nexit "$FAKE_RC"\n')
    out = tmp_path / "gh-output"
    proc = _run(step, script, tmp_path, {
        "GITHUB_OUTPUT": str(out.as_posix()),
        "FAKE_RC": rc,
        "FAKE_BODY": body,
    }, bin_dir)
    assert (proc.returncode != 0) is must_fail, (
        f"rc={rc} body={body!r} -> step rc={proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}")
    published = out.read_text(encoding="utf-8") if out.exists() else ""
    if must_fail:
        assert "has_changes=true" not in published, (
            "the step failed but still told the comment step there is a report")
    else:
        assert "has_changes=true" in published, (
            f"a real report did not reach the comment step: {published!r}")


@needs_bash
@pytest.mark.parametrize("supplied, resolves_to", [
    # An override that does not resolve must NOT fall through to
    # auto-detection: the operator named a tree, and validating a different one
    # while reporting success is the shape this whole ticket is about.
    ("MISSING", None),
    ("FILE", None),
    # …and the shapes that must keep working. The padded one is the false red:
    # pasting into the dispatch box is how this input is used, and the cheapest
    # recovery from a rejected paste is to drop the override — i.e. exactly the
    # fall-through the rejection exists to prevent.
    ("PRESENT", "PRESENT"),
    ("  PRESENT  ", "PRESENT"),
    ("PRESENT/", "PRESENT/"),
    # ⛔ A leading NEWLINE, not just a leading space. The first trim used `sed`,
    # which is line-oriented, so a value pasted out of a wiki still false-red —
    # and the operator's cheapest recovery from that is to drop the override,
    # i.e. the fall-through this branch exists to prevent.
    ("\nPRESENT", "PRESENT"),
    ("PRESENT\n", "PRESENT"),
    # ⛔ …and whitespace-ONLY must stay an error. Trimming alone turned "   "
    # into "" and let it fall through to auto-detection with a green verdict:
    # the false-red fix re-created the exact silent fall-through it sits next to.
    ("   ", None),
    # ⛔ The comment promises INTERIOR whitespace still resolves, and that
    # promise had no sample: swapping the trim for `tr -d '[:space:]'` kept the
    # suite green while turning `over ride` into `override` — a silent validate
    # of a different tree, the same class as the fall-through above.
    ("SPACED", "SPACED"),
], ids=["missing", "is-a-file", "present", "padded", "trailing-slash",
        "leading-newline", "trailing-newline", "whitespace-only",
        "interior-space"])
def test_the_config_dir_override_never_falls_through_to_autodetection(
        tmp_path, supplied: str, resolves_to: str | None) -> None:
    """Rejecting is only half of it — the other half is not resolving anyway."""
    tree = tmp_path / "checkout"
    (tree / EXPORTER_CONF_D).mkdir(parents=True)      # auto-detection WOULD work
    (tree / "override-tree").mkdir()
    (tree / "over ride").mkdir()
    (tree / "a-file").write_text("x", encoding="utf-8")
    concrete = {"PRESENT": "override-tree", "MISSING": "no-such-dir",
                "FILE": "a-file", "SPACED": "over ride"}
    def _fill(text: str) -> str:
        for token, real in concrete.items():
            text = text.replace(token, real)
        return text

    value = _fill(supplied)
    # ⛔ The SAME substitution on both sides. An earlier version filled only
    # `PRESENT` in the expectation, so a new token compared against its own
    # placeholder — the assertion would have been unsatisfiable rather than
    # wrong, but it is the shape where an expectation quietly stops describing
    # the input.
    expected = _fill(resolves_to) if resolves_to else None

    step = _detect_step(GUARD_DEFAULTS)
    script = _render(step.script, {"github.event.inputs.config_dir": ""})
    out = tmp_path / "gh-output"
    proc = _run(step, script, tree, {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_OUTPUT": str(out.as_posix()),
        "INPUT_DIR": value,
    })
    published = out.read_text(encoding="utf-8") if out.exists() else ""
    if expected is None:
        assert proc.returncode != 0, (
            f"override {value!r} did not resolve and the step still succeeded\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "conf_d=" not in published, (
            f"override {value!r} was rejected but a tree was published anyway: "
            f"{published!r} — this is the fall-through, not the rejection")
    else:
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert f"conf_d={expected}\n" in published, (
            f"override {value!r} should resolve to {expected!r}; got "
            f"{published!r}")


@needs_bash
@pytest.mark.skipif(os.name == "nt",
                    reason="NTFS will not create a name ending in a space")
def test_a_directory_whose_name_ends_in_a_blank_is_not_trimmed_away(
        tmp_path) -> None:
    """⛔ The try-the-raw-value-first branch had NO test at all.

    `RAW_INPUT_DIR` appears zero times under `tests/`, so removing the whole
    hunk — trim unconditionally, and drop the message that shows both the value
    as given and the value after trimming — left the module green (measured).
    Both halves it restored are then free to come back: a directory legitimately
    named with a trailing blank is a false red, and the annotation quotes a path
    the operator never typed, which is the same defect twice on one line.
    """
    tree = tmp_path / "checkout"
    (tree / EXPORTER_CONF_D).mkdir(parents=True)      # auto-detection WOULD work
    (tree / "trailing ").mkdir()
    step = _detect_step(GUARD_DEFAULTS)
    script = _render(step.script, {"github.event.inputs.config_dir": ""})
    out = tmp_path / "gh-output"
    proc = _run(step, script, tree, {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_OUTPUT": str(out.as_posix()),
        "INPUT_DIR": "trailing ",
    })
    published = out.read_text(encoding="utf-8") if out.exists() else ""
    assert proc.returncode == 0, (
        f"a directory that exists was rejected because its name ends in a "
        f"blank:\n{proc.stdout}\n{proc.stderr}")
    assert "conf_d=trailing \n" in published, (
        f"the trailing blank was trimmed off a name that really has one: "
        f"{published!r}")


@needs_bash
def test_the_override_error_shows_both_the_raw_and_the_trimmed_value(
        tmp_path) -> None:
    """The other half of the same hunk: the message must name what was typed.

    With only the trimmed value in it, an operator who pasted `weird ` was told
    `'weird' is not a directory` — a path they never entered, about a directory
    that may well exist under the name they did enter.
    """
    tree = tmp_path / "checkout"
    (tree / EXPORTER_CONF_D).mkdir(parents=True)
    step = _detect_step(GUARD_DEFAULTS)
    script = _render(step.script, {"github.event.inputs.config_dir": ""})
    out = tmp_path / "gh-output"
    proc = _run(step, script, tree, {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_OUTPUT": str(out.as_posix()),
        "INPUT_DIR": "no-such-dir ",
    })
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert "'no-such-dir '" in proc.stdout, (
        f"the annotation does not show the value as given:\n{proc.stdout}")
    assert "'no-such-dir'" in proc.stdout, (
        f"the annotation does not show what it actually tried:\n{proc.stdout}")


@needs_bash
def test_the_blind_spot_disclosure_survives_a_base_with_no_confd(
        tmp_path) -> None:
    """The bootstrap PR is the one that needs the disclosure most.

    A PR that CREATES the first `conf.d/` adds an inherited `_defaults.yaml`
    every future tenant picks up, and `config-diff` cannot read it. The step
    short-circuited before the disclosure block whenever the base had no tree,
    so this shape got a green run and a posted comment reading "No changes
    detected." with nothing said about what was not compared.

    ⚠️ The sibling shape (first nested tenant against a flat base) already had
    a test; the early exit above it did not. Removing the exit is the fix, and
    this is the assertion that stops it coming back as an optimisation.
    """
    repo = _base_repo(tmp_path, conf_d=False)
    base = repo / EXPORTER_CONF_D
    (base / "_defaults.yaml").write_text("defaults:\n  cpu: 80\n",
                                         encoding="utf-8", newline="\n")
    (base / "team-x").mkdir(parents=True, exist_ok=True)
    (base / "team-x" / "db-z.yaml").write_text(
        "tenants:\n  db-z:\n    cpu: 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "PR creates the first conf.d")

    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    published = (tmp_path / "gh-output").read_text(encoding="utf-8")
    assert "blind_spots" in published, (
        f"a PR that created conf.d/ from nothing was told nothing about the "
        f"inherited defaults and nested tenant it just added:\n"
        f"{proc.stdout}\n{published}")
    assert "_defaults.yaml" in published, published
    assert "sub-directories" in published, published


@needs_bash
def test_a_pr_with_no_merge_base_says_unknown_rather_than_failing(
        tmp_path) -> None:
    """Branch topology is not a broken git, and must not be answered with red.

    A PR whose branch shares no history with the base (orphan branch, or a
    re-imported repo) makes `git diff A...B` exit 128. Removing the early
    `exit 0` above dropped that shape into the disclosure block, turning a
    previously-green run red — for which the cheapest fix is to put the early
    exit back, i.e. revert the change that made the disclosure reachable at all.

    ⛔ The other direction matters as much: it must not go SILENT. "Could not
    compute the disclosure" and "nothing to disclose" are different answers and
    the comment has to carry which one it is.
    """
    repo = _base_repo(tmp_path, conf_d=False)
    _git(repo, "checkout", "-q", "--orphan", "pr-branch")
    base = repo / EXPORTER_CONF_D
    base.mkdir(parents=True, exist_ok=True)
    (base / "_defaults.yaml").write_text("defaults:\n  cpu: 80\n",
                                         encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated history")

    step, script = _extract_script(tmp_path)
    proc = _run(step, script, repo, _extract_env(tmp_path))
    assert proc.returncode == 0, (
        f"a PR with no merge base turned the step red:\n"
        f"{proc.stdout}\n{proc.stderr}")
    published = (tmp_path / "gh-output").read_text(encoding="utf-8")
    assert "blind_spots" in published, (
        f"the disclosure went silent instead of saying it could not be "
        f"computed:\n{proc.stdout}\n{published}")
    assert "no history" in published, published


@needs_bash
def test_a_broken_merge_base_is_not_reported_as_branch_topology(
        tmp_path) -> None:
    """⛔ rc=128 is "I cannot answer", not "no common ancestor".

    The probe that keeps an orphan-branch PR green was first written as
    `! git merge-base … 2>&1`, which reads both exit codes as the same answer
    and throws the `fatal:` away. On a HEALTHY PR whose git is broken, that
    published a claim about the branch's history — in the comment body, to the
    reviewer — that it had not established. Same shape as the `|| true`
    wrappers condemned at the top of that same block.
    """
    repo = _base_repo(tmp_path)                    # genuinely shares history
    step, script = _extract_script(tmp_path)
    env = dict(_extract_env(tmp_path))
    env["FAIL_GIT_MERGE_BASE"] = "1"
    proc = _run(step, script, repo, env, _git_stub_dir(tmp_path))
    published = (tmp_path / "gh-output").read_text(encoding="utf-8") \
        if (tmp_path / "gh-output").exists() else ""
    assert proc.returncode != 0, (
        f"a broken `git merge-base` was accepted as an answer:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "no history" not in published, (
        f"the reviewer was told this branch shares no history, which git never "
        f"said:\n{published}")
    assert "merge-base exited" in proc.stdout, (
        f"the failure was not attributed to merge-base:\n{proc.stdout}")


_HELPER_MARKER = "STUB-SAW-INPUT"

# ⛔ A stand-in that SPEAKS. The step shells out to the repo's own resolver,
# which a fixture checkout does not carry — the same second repo dependency the
# customer-template note names — so it has to be stubbed. The first version was
# `cat >/dev/null`, and that made every assertion about the resulting TSV
# vacuous: the step's own redirect creates the file, the stub never writes to
# it, so "the target list is empty" was true for a step that had just computed
# a full change list. Emitting a row whenever it is handed any bytes turns the
# emptiness back into evidence about the step.
_HELPER_STUB = f"""#!/usr/bin/env bash
n=$(cat | wc -c)
if [ "$n" -gt 0 ]; then
  printf 'target\\t{_HELPER_MARKER}\\t{_HELPER_MARKER}\\n'
fi
"""


def _scope_pipeline(
        workflow: Path = GUARD_DEFAULTS) -> tuple[list[Step], dict[str, str]]:
    """The steps that build the changed-`_defaults.yaml` listing and hand it on.

    ⛔ Found by ROLE, and returned as a LIST, because splitting one `run:`
    block into two is an ordinary refactor. This used to be a single lookup by
    step name, and a split made four tests red — one of them reporting "no
    longer invokes the scope helper" while the very next step invoked it, and
    one of them raising a bare `FileNotFoundError` with no message at all. A
    message that contradicts the file in front of the author is worse than no
    message: it sends them to undo a correct edit.

    The chain is derived from the artifacts rather than declared: the consumer
    is the step that runs the helper, and the chain reaches back to the
    earliest step in the same job that names one of the same scratch files.
    """
    steps = _steps(workflow)
    consumers = [i for i, s in enumerate(steps)
                 if any(_invokes_scope_helper(ln)
                        for ln in _executable_lines(s.script))]
    assert len(consumers) == 1, (
        f"expected exactly one step invoking {_SCOPE_HELPER} in "
        f"{workflow.name}, found {[str(steps[i]) for i in consumers]}. "
        f"Zero means the scope resolution moved somewhere this module cannot "
        f"see it (a composite action, another job) and every assertion below "
        f"would silently stop testing anything; two means the listing is "
        f"resolved twice and only one of them is measured here.")
    last = consumers[0]
    shared = _artifact_names(steps[last].script)
    first = min((i for i in range(last + 1)
                 if steps[i].job_id == steps[last].job_id
                 and _artifact_names(steps[i].script) & shared),
                default=last)
    chain = [s for s in steps[first:last + 1]
             if s.job_id == steps[last].job_id]
    shells = {s.shell for s in chain}
    # ⚠️ `key=str`: an undeclared `shell:` is None, so a chain that mixes one
    # declared and one undeclared step sorts a str against a None and dies with
    # a bare `TypeError` — no message, in the branch whose entire job is to
    # produce one. Caught by this rule's own control on its first run.
    assert len(shells) == 1, (
        f"the scope chain {[str(s) for s in chain]} spans more than one "
        f"`shell:` ({sorted(shells, key=str)}), so running them as one script "
        f"would measure an interpreter CI does not use")
    # ⛔ REPRODUCE, do not refuse. This used to reject every `env:` key it did
    # not already know, and the message justified it with `set -u` — which is
    # false for a key the script never references. Measured: adding an inert
    # `PYTHONUNBUFFERED: "1"` to this step, a textbook-harmless addition for a
    # step that runs python3, turned six tests red and cost a workflow edit
    # plus two test edits.
    # ⚠️ The narrower predicate "only refuse keys the script mentions" was
    # considered and rejected: this step invokes `guard_defaults_scopes.py`, so
    # `PYTHONHASHSEED` / `PYTHONPATH` / `PYTHONIOENCODING` are consumed by the
    # PROGRAM and never appear in the shell text — that predicate would wave
    # through exactly the variables that do change behaviour.
    # ⚠️ Reproduction also removes the need for a separate startup-variable
    # refusal here: applying `BASH_ENV` is what CI does, so applying it too is
    # fidelity rather than exposure. The sibling module refuses instead, because
    # it executes a slice under a fixed environment and cannot apply them. Two
    # answers to one problem, and the difference is which harness can reproduce.
    declared: dict[str, str] = {}
    for step in chain:
        declared.update(step.env)
    templated = sorted(k for k, v in declared.items() if "${{" in v)
    missing = sorted(set(templated) - _SCOPE_ENV_KEYS)
    assert not missing, (
        f"the scope chain declares `env:` keys whose values are Actions "
        f"expressions the harness has no value for: {missing}. bash cannot "
        f"evaluate `${{{{ }}}}`, so these have to be supplied by the callers — "
        f"add them to `_SCOPE_ENV_KEYS` and to the callers below. Keys with "
        f"LITERAL values need no such thing: they are reproduced verbatim.")
    literal = {k: v for k, v in declared.items() if "${{" not in v}
    return chain, literal


def _guard_scope_harness(tmp: Path) -> tuple[Step, str, Path, Path, dict]:
    """The scope chain as one script, its RUNNER_TEMP, PATH dir and `env:`.

    ⛔ The chain's LITERAL `env:` comes back with it and is applied by the
    callers, so a key the workflow sets is one this harness sets too. Callers
    override it, because the values they pass are what their scenario is about.

    ⚠️ The steps communicate through files under `$RUNNER_TEMP`, not through
    shell state, so concatenating their bodies runs the same program CI runs
    across two step boundaries. `set -euo pipefail` appearing twice is a no-op.
    """
    chain, literal_env = _scope_pipeline()
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    bin_dir = _git_stub_dir(tmp)
    _stub(bin_dir, "python3", _HELPER_STUB)
    script = _render("\n".join(s.script for s in chain), {})
    return chain[-1], script, runner_temp, bin_dir, literal_env


@needs_bash
def test_the_guard_survives_a_pr_with_no_merge_base(tmp_path) -> None:
    """…and the sibling workflow must not die bare on the same shape.

    Before this, `Determine scope` ran under `set -euo pipefail` with no probe
    at all: an orphan-branch PR produced `fatal: … no merge base`, rc=128, no
    annotation, and a second red from the verdict step on top. The answer is a
    warning and a WIDER scope — an empty target list routes this workflow to
    whole-tree validation, which validates more rather than less.
    """
    repo = _base_repo(tmp_path, conf_d=True)
    _git(repo, "checkout", "-q", "--orphan", "pr-branch")
    (repo / EXPORTER_CONF_D / "_defaults.yaml").write_text(
        "defaults:\n  cpu: 99\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated history")

    step, script, runner_temp, bin_dir, wf_env = _guard_scope_harness(tmp_path)
    proc = _run(step, script, repo, {
        **wf_env,
        "BASE_REF": "main",
        "CONF_D": EXPORTER_CONF_D,
        "REAL_GIT": shutil.which("git") or "git",
        "RUNNER_TEMP": str(runner_temp.as_posix()),
    }, bin_dir)
    assert proc.returncode == 0, (
        f"an orphan-branch PR killed the scope step:\n{proc.stdout}\n{proc.stderr}")
    assert "share no history" in proc.stdout, (
        f"the fallback was silent; 'could not compute' must be audible:\n"
        f"{proc.stdout}")
    targets = runner_temp / "guard-targets.tsv"
    # ⛔ This assertion used to be decided by the STUB, not by the step. The
    # helper stand-in was `cat >/dev/null`, so the TSV — created by the step's
    # own redirect — was empty no matter which branch the workflow took; the
    # same three lines passed for a step that had computed a full change list.
    # `_HELPER_STUB` now echoes a row whenever it is handed any bytes, so an
    # empty TSV means the step fed it nothing, which is the claim being made.
    assert targets.exists() and not targets.read_text(encoding="utf-8").strip(), (
        "the empty target list is what routes this to whole-tree validation")


@needs_bash
def test_control_the_helper_stub_speaks_when_it_is_given_anything(
        tmp_path) -> None:
    """The other state of the test above: same stub, non-empty input, row out.

    Two states of one input, opposite answers — a stub that is silent by
    construction cannot produce this one, which is exactly what made the
    orphan-branch assertion vacuous.
    """
    repo = _base_repo(tmp_path, conf_d=True)
    _git(repo, "checkout", "-q", "-b", "pr-branch")
    (repo / EXPORTER_CONF_D / "_defaults.yaml").write_text(
        "defaults:\n  cpu: 99\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change a defaults file")

    step, script, runner_temp, bin_dir, wf_env = _guard_scope_harness(tmp_path)
    proc = _run(step, script, repo, {
        **wf_env,
        "BASE_REF": "main",
        "CONF_D": EXPORTER_CONF_D,
        "REAL_GIT": shutil.which("git") or "git",
        "RUNNER_TEMP": str(runner_temp.as_posix()),
    }, bin_dir)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "share no history" not in proc.stdout, (
        f"a branch that DOES share history took the fallback:\n{proc.stdout}")
    # ⛔ Existence first, and named. This was a bare `.read_text()`, so a chain
    # that never reached the helper — which is what splitting the step in two
    # used to produce — surfaced as an uncaught `FileNotFoundError` carrying no
    # message at all: a stack trace pointing at a path, with nothing to say
    # what the harness expected to find there or why it was not written.
    targets = runner_temp / "guard-targets.tsv"
    assert targets.exists(), (
        f"the scope chain never wrote {targets.name} — it was not reached, or "
        f"it now writes somewhere else:\n{proc.stdout}\n{proc.stderr}")
    assert _HELPER_MARKER in targets.read_text(encoding="utf-8"), (
        "the helper was handed nothing on a PR that changed a _defaults.yaml — "
        "so the sibling test's empty TSV would prove nothing")


@needs_bash
def test_the_guard_names_a_broken_merge_base_rather_than_dying_bare(
        tmp_path) -> None:
    """⛔ rc=128 is a different answer from rc=1, and only one of them is benign.

    `git merge-base` returns 1 for "no common ancestor" — handled above with a
    warning and a wider scope — and 128 for "I could not answer" (bad ref,
    corrupt object, a fetch that did not happen). The `-gt 1` arm is what keeps
    the second from being read as the first, and it had NO test: raising the
    threshold to `-gt 999`, so that a broken git falls into the benign warning
    branch and the step reports a computable-and-empty change list, left the
    whole module green.
    """
    repo = _base_repo(tmp_path, conf_d=True)          # genuinely shares history
    step, script, runner_temp, bin_dir, wf_env = _guard_scope_harness(tmp_path)
    proc = _run(step, script, repo, {
        **wf_env,
        "BASE_REF": "main",
        "CONF_D": EXPORTER_CONF_D,
        "REAL_GIT": shutil.which("git") or "git",
        "FAIL_GIT_MERGE_BASE": "1",
        "RUNNER_TEMP": str(runner_temp.as_posix()),
    }, bin_dir)
    assert proc.returncode != 0, (
        f"a git that could not answer was accepted as an answer:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "merge-base exited" in proc.stdout, (
        f"the failure was not attributed to merge-base:\n{proc.stdout}")
    assert "share no history" not in proc.stdout, (
        f"a broken git was reported to the reviewer as branch topology, which "
        f"git never said:\n{proc.stdout}")
    targets = runner_temp / "guard-targets.tsv"
    assert not targets.exists() or _HELPER_MARKER not in targets.read_text(
        encoding="utf-8"), (
        "scopes were resolved from a listing git refused to produce")


@needs_bash
def test_the_guard_refuses_a_truncated_change_listing(tmp_path) -> None:
    """⛔ The redirect creates the file, so a partial listing looks complete.

    This step ran `git diff` bare under `set -euo pipefail` while the comment
    at the top of the block claimed it was "the same as config-diff.yaml",
    which wraps its own listing in `set +e` / `rc=$?` / `::error::`. The gap
    is not the bare death — it is that a `git diff` dying after some records
    leaves a TRUNCATED `changed-defaults.txt` that the next step resolves
    scopes from: fewer trees validated, no annotation, and a comment that
    reads as a clean run.
    """
    repo = _base_repo(tmp_path, conf_d=True)
    step, script, runner_temp, bin_dir, wf_env = _guard_scope_harness(tmp_path)
    proc = _run(step, script, repo, {
        **wf_env,
        "BASE_REF": "main",
        "CONF_D": EXPORTER_CONF_D,
        "REAL_GIT": shutil.which("git") or "git",
        "FAIL_GIT_DIFF": "1",
        "RUNNER_TEMP": str(runner_temp.as_posix()),
    }, bin_dir)
    assert proc.returncode != 0, (
        f"a failed listing was carried forward as the change set:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "git diff exited" in proc.stdout, (
        f"the step died without saying what failed — the bare `fatal:` names "
        f"git, not the fact that the scope set is now partial:\n{proc.stdout}")
    targets = runner_temp / "guard-targets.tsv"
    assert not targets.exists() or _HELPER_MARKER not in targets.read_text(
        encoding="utf-8"), (
        "the helper was still fed the truncated listing")


def _report_sections(report: str) -> dict[str, list[str]]:
    """`### heading` -> the lines under it, up to the next heading."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in report.splitlines():
        if line.startswith("### "):
            current = line
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


@needs_bash
@pytest.mark.parametrize("targets_text, expect_heading_contains", [
    ("target\ttree-a\ttree-a\n", "tree-a"),               # findings
    ("target\ttree-b\ttree-b\n", "tree-b"),               # NOT VALIDATED
    ("unmanaged\tsome/_defaults.yaml\n", "_defaults.yaml"),
    ("surprise\ttree-z\ttree-z\n", "unrecognised scope kind"),
    # Three ways a row can be unreadable rather than unrecognised. `read`
    # cannot separate them from a third kind, so the shape is checked first —
    # a leading tab shifts the path into $kind, and `target` without a scope
    # would otherwise run da-guard with an empty `--scope`.
    ("\n", "unreadable row"),
    ("\tstray/path.yaml\n", "unreadable row"),
    ("target\ttree-a\n", "unreadable row"),
], ids=["findings", "not-validated", "unmanaged", "unknown-kind",
        "blank-row", "field-shifted-row", "target-without-scope"])
def test_no_report_section_is_left_without_a_body(
        tmp_path, targets_text: str, expect_heading_contains: str) -> None:
    """A heading with nothing under it reads to a reviewer as "no findings".

    ⛔ Asserted over EVERY section the step emits rather than over the one
    branch that was reported: the rule already existed in `run_one`'s comment
    and the branch added next to it broke it anyway, so the assertion has to be
    the rule, not an instance of it. In the isolated case that section IS the
    entire PR comment.
    """
    runner_temp, targets = _guard_fixture(tmp_path, targets_text)
    step = _step(GUARD_DEFAULTS, _S_RUN_DA_GUARD)
    script = _render(step.script, {
        "runner.temp": str(runner_temp.as_posix()),
        "steps.detect.outputs.conf_d": "tree-a",
    })
    proc = _run(step, script, tmp_path, {
        "RUNNER_TEMP": str(runner_temp.as_posix()),
        "TARGETS": str(targets.as_posix()),
        "CONF_D": "tree-a",
        "GITHUB_OUTPUT": str((tmp_path / "gh-output").as_posix()),
    })
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # ⛔ The verdict, not just the prose. Deleting the `WORST=2` on the
    # unreadable-row branch left every assertion in this module green while the
    # job went back to printing "✅ clean" for a TSV it could not read — the
    # report said NOT CHECKED, the annotation was there, and the merge-facing
    # answer was still success. A section body without a verdict is the same
    # silent green in a longer form.
    report = (runner_temp / "guard-report.md").read_text(encoding="utf-8")
    published = (tmp_path / "gh-output").read_text(encoding="utf-8")
    # ⚠️ Only the two CONTRACT-BREAK branches, not every "NOT CHECKED". The
    # `unmanaged` branch is deliberately a warning: a `_defaults.yaml` outside
    # every conf.d tree is reported and the job stays green, which is a design
    # decision this test must not overturn. An unreadable row and an
    # unrecognised kind both mean the producer and this workflow disagree, and
    # that has to reach the verdict.
    if "(unreadable row)" in report or "unrecognised scope kind" in report:
        assert "exit=0" not in published, (
            f"a row that broke the producer contract left the published verdict "
            f"at 0, so the job prints '✅ clean':\n{report}\n{published}")
    sections = _report_sections(report)
    # Anti-vacuity: "every section has a body" is trivially true of no sections,
    # and a row silently dropped by the `case` is exactly how that happens.
    assert sections, (
        f"no `### ` section was written at all for {targets_text!r} — the row "
        f"was dropped, which is the failure this parametrize is about:\n{report}")
    assert any(expect_heading_contains in h for h in sections), (
        f"no heading mentioned {expect_heading_contains!r}:\n{report}")
    for heading, lines in sections.items():
        assert any(ln.strip() for ln in lines), (
            f"section {heading!r} has a heading and nothing under it, which "
            f"reads as 'no findings':\n{report}")


_RESULT_CONTEXTS = ("steps.", "job.", "needs.", "env.",
                    "success(", "failure(", "cancelled(")

# `x['y']` and `x.y` are the SAME expression in the Actions language — the
# docs say so — so a checker that matches on the dot is matching a spelling,
# not a reference. Measured: `steps['blast'].outputs.has_report == 'true'`
# passed cleanly while the dot form two characters away was rejected, and the
# synthetic case table contains that dot form verbatim as a must-reject.
# Rewriting to one spelling first is an authoritative normalisation, not
# another name on a list.
_INDEX_ACCESS = re.compile(r"\[\s*'([A-Za-z_][A-Za-z0-9_-]*)'\s*\]")


def _normalise_expression(condition: str) -> str:
    return _INDEX_ACCESS.sub(r".\1", condition)


def _workflow_triggers(workflow_doc: dict) -> set[str]:
    """The events this workflow accepts, from its `on:` block.

    ⛔ Both spellings. YAML 1.1 turns a bare `on:` into the boolean `True`,
    which is why `.get(True)` works — but `"on":` is the standard way to avoid
    that and is equally valid. Reading only the boolean key made a legal
    rewrite fail with "workflow has no `on:` triggers to reason from", a
    message that diagnoses the wrong thing, and whose cheapest cure is deleting
    the assert — after which `triggers` is empty and the `!=` branch of the
    narrowing check silently stops firing. A false red that rewards disarming
    the guard is worse than no red.
    """
    block = workflow_doc.get(True)
    if block is None:
        block = workflow_doc.get("on")
    assert block, (
        "cannot find this workflow's `on:` block under either the YAML-1.1 "
        "boolean key or the quoted string key; without it there is nothing to "
        "reason about which events the evidence can go missing on")
    return {str(k).lower() for k in block}


# `github.event_name == 'x'` / `!= 'x'`, in either operand order — the only
# comparison in these files whose satisfiability can be decided from the
# workflow itself. ⚠️ Order matters because `'push' == github.event_name` is
# the same expression and escaped the one-sided pattern (measured).
_EVENT_NAME_EQ_L = re.compile(
    r"github\.event_name\s*(==|!=)\s*'([a-z_]+)'", re.IGNORECASE)
_EVENT_NAME_EQ_R = re.compile(
    r"'([a-z_]+)'\s*(==|!=)\s*github\.event_name", re.IGNORECASE)


def _event_comparisons(condition: str) -> list[tuple[str, str]]:
    """(operator, value) for every `github.event_name` comparison, either way.

    ⚠️ SCOPE, stated because six rounds of this rule have each been escaped by
    a rewrite rather than by a new idea. This reads the condition as TEXT. It
    does not parse the Actions expression language, so anything that wraps the
    reference in a function — `contains(github.event_name, 'x')`,
    `startsWith(...)`, `format(...)` — is invisible to it, and that is a
    parser's job rather than a pattern's (measured escaping: `contains(...)`).
    Treat a pass here as "no comparison written the two plain ways", never as
    "the condition does not narrow the event".
    """
    text = _normalise_expression(condition)
    out = [(op, v) for v, op in
           ((m.group(2), m.group(1)) for m in _EVENT_NAME_EQ_L.finditer(text))]
    out += [(op, v) for v, op in
            ((m.group(1), m.group(2)) for m in _EVENT_NAME_EQ_R.finditer(text))]
    return out


def _event_predicates_that_lose_runs(condition: str, triggers: set[str],
                                     producer_condition: str = "") -> list[str]:
    """Event clauses that stop the upload running on runs the producer DOES.

    ⛔ Two versions of this have now been too weak in the same direction.
    The first asked only "does the condition consult a RESULT" — and
    `always() && event_name == 'push'` consults none, so on a
    `pull_request`-only workflow it passed while never evaluating true.
    The second asked "is it satisfiable ANYWHERE in this file's `on:` block",
    which closes that on a single-trigger workflow and not on a multi-trigger
    one: `guard-defaults-impact.yml` accepts `pull_request` AND
    `workflow_dispatch`, so pinning its upload to `workflow_dispatch` is
    "satisfiable", passes, and silently uploads nothing on every PR — measured,
    the whole module stayed green.

    The property is not satisfiability, it is: **the evidence must not go
    missing on a run the producer performed**. So any event clause that
    narrows the trigger set is a problem, whatever it narrows to.

    ⚠️ …unless the PRODUCER narrows the same way. If the step that writes the
    artifact is itself gated on that event, the upload is not losing anything,
    and refusing it would be a false red whose cheapest fix is deleting the
    check. That is why the producer's own condition is an argument here rather
    than something this function assumes.
    """
    # ⛔ The exemption is a SET RELATION, not membership. Written as
    # `value in producer_events` it let two shapes through, neither of which is
    # "the producer narrows the same way":
    #   * producer accepts several events (`== 'push' || == 'pull_request'`)
    #     while the upload keeps only one — the evidence still goes missing on
    #     the other, which is the whole property;
    #   * producer `== 'push'` against upload `!= 'push'` — opposite polarity,
    #     the two never intersect, so the artifact is uploaded on exactly the
    #     runs the producer did NOT perform. Total loss, silently allowed.
    # Both measured returning no problems. So: an `==` narrowing is exempt only
    # when the producer runs on that event and nothing else, and a `!=`
    # narrowing only when the producer excludes the same event too.
    producer_eq = {v.lower() for op, v in
                   _event_comparisons(producer_condition) if op == "=="}
    producer_ne = {v.lower() for op, v in
                   _event_comparisons(producer_condition) if op == "!="}
    problems = []
    for op, value in _event_comparisons(condition):
        value = value.lower()
        if op == "==" and producer_eq == {value}:
            continue          # producer runs on that event only; nothing lost
        if op == "!=" and value in producer_ne:
            continue          # producer excludes it too; nothing lost
        if op == "==":
            missed = sorted(triggers - {value})
            if value not in triggers:
                problems.append(
                    f"requires github.event_name == {value!r}, which this "
                    f"workflow never receives (on: {sorted(triggers)}) — the "
                    f"condition is constant-false and the upload never runs")
            elif missed:
                problems.append(
                    f"requires github.event_name == {value!r}, so the evidence "
                    f"is never uploaded on {missed} — runs the producer still "
                    f"performs. Gate the PRODUCER on the event if it should "
                    f"not run there; do not drop the evidence for it")
        if op == "!=" and value in triggers:
            problems.append(
                f"excludes github.event_name == {value!r}, a trigger this "
                f"workflow accepts — the evidence goes missing on exactly "
                f"those runs")
    return problems


def _artifact_upload_problems(workflow_doc: dict, condition: str,
                              producer_condition: str = "") -> list[str]:
    """Everything wrong with an evidence upload's `if:`, in ONE function.

    ⛔ And there is exactly ONE call site, which is the part the previous
    version got wrong. It said "production and control call this same name" —
    and then the control tests called this function DIRECTLY while the live
    assertion called it separately, so the live call could be reverted to the
    weaker helper and every table stayed green (measured, with a
    constant-false upload condition reinstated on top). A control that calls
    the helper proves the helper; only a control that goes through the same
    call site proves the wiring. The synthetic cases are therefore rows of the
    live test, not a test of their own.
    """
    triggers = _workflow_triggers(workflow_doc)
    return (_upload_condition_problems(condition)
            + _event_predicates_that_lose_runs(
                condition, triggers, producer_condition))


def _upload_condition_problems(condition: str) -> list[str]:
    """Why this `if:` may not guard an evidence upload. Pure, so it is sampled.

    ⛔ Extracted because the inline version had no control at all: replacing it
    with a constant left the whole suite green, while the sibling rule in this
    module (`_verdict_gating_problems`) has had a sample table since it was
    written. Two rules of the same shape in one file, one guarded and one not.

    ⚠️ Case-insensitive: GitHub expression functions are, so `ALWAYS()` is a
    legal spelling and rejecting it is a false red with a message that
    diagnoses the wrong thing.
    """
    # ⛔ Normalised FIRST. Matching `"steps."` against the raw string is
    # matching a spelling: `steps['guard'].outputs.exit` contains no
    # `steps.` and sailed through (measured: nothing went red) while the dot
    # form beside it in the sample table is a must-reject.
    lowered = _normalise_expression(condition).lower()
    problems: list[str] = []
    if not _RUNS_AFTER_FAILURE.search(lowered):
        problems.append("must run after a failure (always() / !cancelled())")
    remainder = _RUNS_AFTER_FAILURE.sub("", lowered)
    consulted = [c for c in _RESULT_CONTEXTS if c in remainder]
    if consulted:
        problems.append(
            f"consults {consulted}, so a run that died can lose the artifact "
            f"that is the evidence of it")
    return problems


@pytest.mark.parametrize("condition, triggers, producer, ok", [
    # Constant-false: the event never occurs in this file at all.
    ("always() && github.event_name == 'push'", {"pull_request"}, "", False),
    ("always() && github.event_name != 'pull_request'", {"pull_request"}, "",
     False),
    # ⛔ Narrowing to ONE of several real triggers. "Satisfiable somewhere in
    # `on:`" is true here, and every pull_request run still uploads nothing —
    # this is the row the previous, weaker predicate passed.
    ("always() && github.event_name == 'workflow_dispatch'",
     {"pull_request", "workflow_dispatch"}, "", False),
    ("always() && github.event_name != 'pull_request'",
     {"pull_request", "workflow_dispatch"}, "", False),
    # …and the same clause is fine when the PRODUCER narrows identically:
    # nothing is lost, and refusing it would be a false red.
    ("always() && github.event_name == 'workflow_dispatch'",
     {"pull_request", "workflow_dispatch"},
     "github.event_name == 'workflow_dispatch'", True),
    ("always() && github.event_name == 'pull_request'", {"pull_request"}, "",
     True),
    # Reverse samples: it must not fire on conditions it cannot decide.
    ("always()", {"pull_request"}, "", True),
    ("always() && inputs.upload == 'yes'", {"workflow_dispatch"}, "", True),
    # ⛔ NO `github.ref` row here, and its absence is the point. This table
    # used to carry `("always() && github.ref == 'refs/heads/main'",
    # {"pull_request"}, "", True)` under the id `undecidable-ref` — asserting
    # that the predicate MUST ACCEPT it. On a pull_request-only workflow that
    # condition is constant-false (a PR run's `github.ref` is
    # `refs/pull/<n>/merge`), so the table was certifying a bypass of exactly
    # the kind the `event_name == 'push'` row above forbids. A control that
    # blesses a shape it has not reasoned about is worse than no control.
    # This predicate decides `github.event_name` and nothing else; "not
    # decided" is recorded in its docstring, not asserted as "safe".
    # Two spellings that ARE the same expression, kept as must-reject so the
    # normaliser cannot be dropped:
    ("always() && github['event_name'] == 'push'", {"pull_request"}, "", False),
    ("always() && 'push' == github.event_name", {"pull_request"}, "", False),
], ids=["constant-false-eq", "constant-false-ne", "narrowed-one-of-two",
        "excludes-a-real-trigger", "producer-narrows-too", "own-event",
        "bare-always", "input-predicate", "index-syntax", "reversed-operands"])
def test_control_the_event_narrowing_check_is_two_sided(
        condition, triggers, producer, ok):
    """⛔ It must refuse only what it can PROVE loses runs.

    A check that guesses is a false-red machine, and the cheapest way to turn a
    false red green is to delete the guard. Only `github.event_name`
    comparisons are decided here, because only they have an authority in the
    file itself; anything else is left alone on purpose.

    ⚠️ This is a UNIT sample of the predicate. It says nothing about whether
    the live assertion still calls it — that is what the synthetic rows of
    `test_the_report_artifact_is_not_gated_on_the_producer_reporting` are for,
    and the two must not be confused again: a previous round had only this kind
    of table, and the production call site was reverted with the module green.
    """
    got = not _event_predicates_that_lose_runs(condition, triggers, producer)
    assert got is ok, (
        f"{condition!r} vs {sorted(triggers)} (producer {producer!r}) -> "
        f"{_event_predicates_that_lose_runs(condition, triggers, producer)}")




def _normalise_runner_spelling(text: str) -> str:
    """`${{ runner.temp }}` and `$RUNNER_TEMP` name the same directory.

    Actions expands the first; the shell expands the second. Comparing them
    needs one spelling, and this is a documented equivalence rather than a
    convention of mine. Applied to whole LINES as well as to paths, because
    the two ends of the comparison are free to differ (`with.path:` is read by
    Actions, the `run:` body by bash) and often do.
    """
    return re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", "$RUNNER_TEMP", text)


def _normalise_runner_path(path: str) -> str:
    return _normalise_runner_spelling(path).strip().rstrip("/")


def _shared_producer_condition(producers) -> str:
    """The `if:` every producer agrees on, or "" when they disagree.

    The narrowing exemption downstream exists because evidence is not lost
    when the producer skips the same runs the upload skips. That argument only
    holds if EVERY step writing the artifact skips them, so disagreement must
    grant no exemption rather than the first producer's.

    ⛔ A function with its own control test, not three inline lines. Inline, the
    property had zero coverage: both shipped workflows have exactly one
    producer per upload, and the synthetic rows feed `producer_condition`
    straight into the judge, so the reduction was on no path any test took.
    Measured — replacing it with `producers[0].raw.get("if") or ""`, the exact
    regression its comment warned about, left the module green.
    """
    conditions = {s.raw.get("if") or "" for s in producers}
    return conditions.pop() if len(conditions) == 1 else ""


class _FakeProducer:
    """Just enough of `Step` for the control below: an `if:` and a name."""

    def __init__(self, condition: str | None) -> None:
        self.raw = {"if": condition} if condition is not None else {}


@pytest.mark.parametrize("conditions, expected", [
    # one producer — its condition is the exemption's basis
    ([None], ""),
    (["github.event_name == 'pull_request'"], "github.event_name == 'pull_request'"),
    # several producers that agree — still a basis
    (["always()", "always()"], "always()"),
    # ⛔ the case with no coverage before: they disagree, so NO exemption.
    # Returning the first one's condition here is what lets an upload narrow
    # to a trigger that only ONE of its producers skips, and the evidence goes
    # missing on every run the other producer performed.
    (["always()", "github.event_name == 'workflow_dispatch'"], ""),
    ([None, "always()"], ""),
    (["a", "b", "a"], ""),
], ids=["single-bare", "single-narrowed", "agreeing", "disagreeing",
        "one-bare-one-not", "three-two-distinct"])
def test_control_disagreeing_producers_grant_no_narrowing_exemption(
        conditions, expected):
    """⚠️ Two of these six rows carry the regression, not all six.

    Measured against the exact regression this guards (`producers[0].raw.get
    ("if") or ""`): `disagreeing` and `three-two-distinct` go red, the other
    four stay green. `one-bare-one-not` in particular CANNOT catch it — the
    first producer has no `if:`, so `or ""` returns the same "" the correct
    reduction returns. It is kept as a shape sample, not counted as evidence.
    A parametrize table reads as six witnesses; saying which two actually
    testify is the difference between coverage and the appearance of it.
    """
    producers = [_FakeProducer(c) for c in conditions]
    assert _shared_producer_condition(producers) == expected


def _upload_steps(workflow: Path) -> list[Step]:
    """EVERY upload step, because a second one is an ordinary edit.

    ⛔ This used to be `_step_using`, which asserts exactly one hit. Adding a
    second `actions/upload-artifact` step — shipping a log next to the report,
    say — is a normal thing to do, and it turned this rule red on a REQUIRED
    check with a message about "expected exactly one", naming no fix. The
    author's cheapest way out is to delete the check or to make it `[0]`, and
    `[0]` silently stops governing the second upload, which is the one most
    likely to be gated wrong. Judging each upload independently costs nothing
    and has no cheapest-wrong-fix.
    """
    return [s for s in _steps(workflow)
            if "actions/upload-artifact" in str(s.raw.get("uses") or "")]


def _upload_path_entries(upload: Step) -> list[str]:
    """The INCLUDE entries of an upload's `path:`, one per line, normalised.

    ⛔ `actions/upload-artifact` documents `path:` as a multi-line input where
    every line is a glob and a leading `!` makes it an exclusion. The previous
    version took the whole scalar as one literal path, so two ordinary edits
    reported "no step in <file> names '<the whole block>'" — a message whose
    plain meaning is "the artifact moved" — about artifacts that had not moved:
      * shipping a second file alongside the report (a two-line `path:`);
      * writing `guard-report*.md`, which is how you ship a set.
    Both then push the author toward the repair that reads cheapest, which is
    to delete the assertion.
    """
    raw = str(upload.raw.get("with", {}).get("path") or "")
    entries = [_normalise_runner_path(ln) for ln in raw.splitlines()]
    return [e for e in entries if e and not e.startswith("!")]


def _glob_to_regex(entry: str) -> str:
    """An upload `path:` glob as a regex, refusing what it cannot express.

    ⛔ Refuse rather than mistranslate: a bracket expression silently read as
    literal characters would make this rule stop matching the producer while
    still looking like it covers it, and "the classifier's miss looks like a
    pass" is this module's own recurring failure mode.
    """
    assert "[" not in entry, (
        f"{entry!r} uses a bracket expression, which this matcher does not "
        f"model. Teach `_glob_to_regex` the syntax — do not let it be compared "
        f"as literal characters, which would silently stop finding the producer")
    out, i = [], 0
    while i < len(entry):
        if entry.startswith("**", i):
            out.append(".*")
            i += 2
        elif entry[i] == "*":
            out.append("[^/]*")
            i += 1
        elif entry[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(entry[i]))
            i += 1
    return "".join(out)


def _artifact_producers(workflow: Path, upload: Step) -> list[Step]:
    """Every step that names a file the upload step ships.

    Derived from the artifact path, not from a step name: naming it would have
    to be repeated per workflow, and repeating it is how the sibling ended up
    with the assertion and this one without it.

    ⛔ The WHOLE path, not the basename. Matching on `rsplit("/", 1)[-1]` meant
    an ordinary tidy-up — moving the artifact into a sub-directory while the
    producer still writes it where it always did — kept this green while CI
    uploaded nothing on every run, forever (measured: nothing went red). The rule
    that governs the upload's `if:` cannot see that at all, because the file
    never exists to be gated on, and `if-no-files-found: warn` makes uploading
    nothing a non-event.

    ⛔ EVERY include entry needs a writer, not just one of them. An entry
    nobody writes IS the defect this rule exists to catch, and it does not stop
    being one because a sibling entry happens to be written.
    """
    entries = _upload_path_entries(upload)
    assert entries, f"{upload} uploads no path"
    # ⛔ Executable lines only. Matching the whole `run:` text counted COMMENTS,
    # so adding a line like `# The guard writes its report to $RUNNER_TEMP/…`
    # to an earlier step, and building the real path from parts in the actual
    # producer, moved "the producer" onto the commented step — after which the
    # ordering and same-job assertions were checked against the wrong step and
    # an upload placed before the real producer passed (measured: nothing went red).
    # ⚠️ The producer must therefore contain the path LITERALLY. Assembling it
    # from fragments turns this red, and the message says so, because the only
    # other way to find the writer is to go back to basename matching — which
    # is the hole this replaced.
    hits: list[Step] = []
    for entry in entries:
        pattern = _glob_to_regex(entry)
        writers = [s for s in _steps(workflow)
                   if any(re.search(pattern, _normalise_runner_spelling(ln))
                          for ln in _executable_lines(s.script))]
        assert writers, (
            f"no step in {workflow.name} names {entry!r}, so {upload.name} "
            f"ships a file nobody writes — silent, not red, because "
            f"if-no-files-found is `warn`. If the artifact moved, the producer "
            f"has to move with it; if the entry is obsolete, drop it from "
            f"`path:` rather than deleting this assertion")
        hits.extend(s for s in writers if s not in hits)
    # ⛔ ALL of them must precede the upload, and `len(hits) == 1` is NOT the
    # way to get that. Requiring exactly one turned this red for an ordinary
    # edit — an earlier step that merely MENTIONS the path (`echo "report:
    # $REPORT"`, `ls -l "$REPORT"`) is a second hit, and the message
    # misdiagnosed it as "the artifact moved".
    # ⚠️ Naming the blocking check precisely, because this PR argues elsewhere
    # that the three workflows are NOT required and that is still true: the red
    # lands on `Python Tests (3.13)`, which IS a required context (measured via
    # the protection API), not on the workflow being described. So a false red
    # HERE blocks a merge that a real failure of the workflow itself would not.
    # The obvious
    # repair is `>= 1` plus `hits[0]`, and that is measurably worse: it hands
    # the decoy attack back its win, because a diagnostic `echo` placed early
    # becomes "the producer" while the real writer moves BELOW the upload and
    # the ordering assertion is checked against the wrong step.
    # ⚠️ Boundary, stated rather than discovered later: a step that names the
    # path AFTER the upload is refused even when it is only cleaning up. On an
    # ephemeral runner that cleanup does nothing, so the false red is a shape
    # the shipped files do not have and, measured, neither does any other
    # workflow in this repo (23 upload steps in 16 files, 0 post-upload
    # mentions).
    late = [s for s in hits if s.index > upload.index or s.job_id != upload.job_id]
    assert not late, (
        f"{upload.name} in {workflow.name} ships {entries}, but "
        f"{[s.name for s in late]} names one of those paths after it (or in "
        f"another job). The upload cannot ship a file written later, and an "
        f"empty upload is not red")
    return hits


class _FakeUpload:
    """Just enough of `Step` for the two controls below: a `with.path`."""

    def __init__(self, path: str) -> None:
        self.raw = {"with": {"path": path}}


@pytest.mark.parametrize("path, expected", [
    ("/tmp/report.json", ["/tmp/report.json"]),
    ("${{ runner.temp }}/guard-report.md", ["$RUNNER_TEMP/guard-report.md"]),
    # ⛔ The multi-line form the action documents, with the trailing newline a
    # YAML block scalar always leaves behind.
    ("$RUNNER_TEMP/a.md\n$RUNNER_TEMP/b.json\n",
     ["$RUNNER_TEMP/a.md", "$RUNNER_TEMP/b.json"]),
    # …and `!` lines are exclusions: they remove files, they do not need a
    # writer, and demanding one for them would be a false red by construction.
    ("$RUNNER_TEMP/*.md\n!$RUNNER_TEMP/scratch.md\n", ["$RUNNER_TEMP/*.md"]),
    # ⛔ A trailing slash is the same directory. Blind review removed the
    # `.rstrip("/")` and the module stayed green, so nothing held the
    # two sides of the comparison to one spelling: `path: $RUNNER_TEMP/x/`
    # against a producer writing `$RUNNER_TEMP/x` is a pure formatting edit
    # that would have started reporting "the upload ships a file nobody
    # writes".
    ("$RUNNER_TEMP/reports/", ["$RUNNER_TEMP/reports"]),
], ids=["single", "actions-spelling", "multi-line", "with-an-exclusion",
        "trailing-slash"])
def test_control_the_upload_path_is_read_as_the_action_reads_it(path, expected):
    assert _upload_path_entries(_FakeUpload(path)) == expected


@pytest.mark.parametrize("entry, line, matches", [
    ("$RUNNER_TEMP/guard-report.md",
     'echo x > "$RUNNER_TEMP/guard-report.md"', True),
    # the glob form, and the Actions spelling on the OTHER side of the compare
    ("$RUNNER_TEMP/guard-report*.md",
     'echo x > "${{ runner.temp }}/guard-report-2.md"', True),
    ("$RUNNER_TEMP/report?.json", 'echo x > "$RUNNER_TEMP/report1.json"', True),
    ("$RUNNER_TEMP/**/report.json",
     'echo x > "$RUNNER_TEMP/nested/dir/report.json"', True),
    # ⛔ and the direction that keeps this from becoming "matches anything":
    # `*` must not cross a directory boundary, or moving the artifact into a
    # sub-directory — the tidy-up that once left CI uploading nothing forever —
    # would still look covered.
    ("$RUNNER_TEMP/*.json",
     'echo x > "$RUNNER_TEMP/nested/report.json"', False),
    ("$RUNNER_TEMP/report.json", 'echo x > "$RUNNER_TEMP/other.json"', False),
], ids=["literal", "star", "question", "globstar", "star-stops-at-slash",
        "different-file"])
def test_control_the_upload_glob_matches_what_the_action_would_ship(
        entry, line, matches):
    """Two-sided: the shapes that must be found, and one that must not be."""
    found = re.search(_glob_to_regex(entry),
                      _normalise_runner_spelling(line)) is not None
    assert found is matches, (entry, line)


def test_control_the_upload_glob_refuses_syntax_it_cannot_express():
    with pytest.raises(AssertionError, match="bracket expression"):
        _glob_to_regex("$RUNNER_TEMP/report[0-9].json")


# ⛔ Synthetic rows of the LIVE test, deliberately not a test of their own.
# `(label, on-block, upload condition, producer condition, must be rejected)`.
# Every one of these was measured green at some point in this PR's history,
# each after a one-token edit, so they are the shapes the single call site
# below has to keep catching. Putting them here rather than in a sibling
# control is the whole point: a control with its own call site proves the
# helper and says nothing about whether the live assertion still uses it.
_PR_ONLY = {"pull_request": {"branches": ["main"]}}
_PR_AND_DISPATCH = {"pull_request": {"branches": ["main"]},
                    "workflow_dispatch": None}
_SYNTHETIC_UPLOAD_CASES = [
    ("clean", _PR_ONLY, "always()", "", False),
    ("consults-a-step-output", _PR_ONLY,
     "always() && steps.blast.outputs.has_report == 'true'", "", True),
    # `$GITHUB_ENV` is the standard way to launder a step's outcome into a
    # context that reads as static.
    ("laundered-through-env", _PR_ONLY,
     "always() && env.HAS_REPORT == 'true'", "", True),
    ("constant-false-event", _PR_ONLY,
     "always() && github.event_name == 'push'", "", True),
    # ⛔ The multi-trigger hole: 'satisfiable somewhere in on:' is true here,
    # and every pull_request run still uploads nothing.
    ("narrowed-to-one-of-two-triggers", _PR_AND_DISPATCH,
     "always() && github.event_name == 'workflow_dispatch'", "", True),
    ("excludes-a-real-trigger", _PR_AND_DISPATCH,
     "always() && github.event_name != 'pull_request'", "", True),
    # …and the reverse sample: narrowing is fine when the PRODUCER narrows the
    # same way, because then no evidence is being lost. Refusing this would be
    # a false red whose cheapest fix is deleting the check.
    ("producer-narrows-identically", _PR_AND_DISPATCH,
     "always() && github.event_name == 'workflow_dispatch'",
     "github.event_name == 'workflow_dispatch'", False),
    # ⛔ …but "the producer mentions this event" is NOT "the producer narrows
    # the same way". Both of these read as exempt under a membership test and
    # both lose evidence; the second loses ALL of it, because the two
    # conditions never hold at the same time.
    ("producer-accepts-more-than-the-upload", _PR_AND_DISPATCH,
     "always() && github.event_name == 'workflow_dispatch'",
     "github.event_name == 'workflow_dispatch' || "
     "github.event_name == 'pull_request'", True),
    ("producer-and-upload-have-opposite-polarity", _PR_AND_DISPATCH,
     "always() && github.event_name != 'pull_request'",
     "github.event_name == 'pull_request'", True),
    # …and the reverse sample for `!=`: exempt when the producer excludes it too.
    ("both-exclude-the-same-event", _PR_AND_DISPATCH,
     "always() && github.event_name != 'pull_request'",
     "github.event_name != 'pull_request'", False),
]


@pytest.mark.parametrize(
    "workflow, synthetic",
    [(GUARD_DEFAULTS, None), (BLAST_RADIUS, None)]
    + [(None, case) for case in _SYNTHETIC_UPLOAD_CASES],
    ids=["guard-defaults", "blast-radius"]
    + [case[0] for case in _SYNTHETIC_UPLOAD_CASES])
def test_the_report_artifact_is_not_gated_on_the_producer_reporting(
        workflow: Path | None, synthetic) -> None:
    """The artifact must survive the failure it is evidence for.

    ⚠️ Both workflows, because the single-workflow version of this assertion is
    what let the sibling ship the same condition unguarded. The two files fail
    differently — one builds its report incrementally, the other writes it in
    one shot from an EARLIER call than the output being tested — but the rule
    is the same either way: a step's own verdict must not decide whether its
    evidence is kept.
    """
    # ⛔ The two kinds of case are NORMALISED here and judged below by ONE
    # statement. The first attempt at this gave the synthetic rows their own
    # call plus an early `return` — two call sites in one function — and the
    # production line could still be reverted to the weaker helper with the
    # module green (measured). "Same function" is not "same call
    # site"; only a single `problems = …` line makes the synthetic rows
    # evidence about the wiring.
    uploads: list[Step] = []
    if synthetic is not None:
        label, on_block, condition, producer_condition, must_reject = synthetic
        doc = {True: on_block}
        cases = [(condition, producer_condition)]
    else:
        label = str(workflow.name)
        uploads = _upload_steps(workflow)
        assert uploads, (
            f"{workflow.name} uploads no artifact — either the evidence stopped "
            f"being kept or this rule stopped having a subject; both are holes")
        cases = []
        for upload in uploads:
            producers = _artifact_producers(workflow, upload)
            # ⛔ At least one, not every one. A diagnostic step that merely
            # names the path has no `id:` and should not need one; the point
            # is that SOMETHING identifiable writes the file.
            assert any(s.raw.get("id") for s in producers), (
                f"nothing writing {upload.name}'s artifact in "
                f"{workflow.name} has an `id:`")
        # ⛔ ORDER, and in the same job. Everything else on this test reads the
        # `if:` string, and an upload that runs BEFORE its producer needs no
        # `if:` at all to lose the evidence — the file simply does not exist
        # yet, `if-no-files-found: warn` keeps that quiet, and the job is
        # green. Measured: moving the upload step above the producer left all
        # 192 assertions passing. A step reordering is an ordinary edit, which
        # is exactly why it has to be the structural check rather than another
        # spelling in the condition rule.
            # ⛔ The same-job and ordering properties are asserted inside
            # `_artifact_producers` now, over EVERY step naming the path
            # rather than over one chosen step — choosing was what the decoy
            # attacked.
            #
            # The narrowing exemption is fail-closed across them: it exists
            # because evidence is not lost when the producer skips the same
            # runs, and that argument only holds if they ALL skip those runs.
            # Disagreeing producers grant no exemption rather than the first
            # one's.
            producer_condition = _shared_producer_condition(producers)
            cases.append((upload.raw.get("if") or "", producer_condition))
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        must_reject = False
    # ⛔ Two properties, not one literal. Two narrower spellings of this rule
    # were each defeated by one edit (`steps.<id>.outputs` fell to
    # `steps.<id>.conclusion`, `steps.<id>.` fell to `job.status`), so the next
    # version pinned the condition to exactly `always()` / `!cancelled()` — and
    # that over-corrected: it rejected a whole shape of extra predicate on
    # principle, while the SIBLING rule in this module REQUIRES the verdict
    # step to carry its producer's precondition.
    # ⚠️ The example this sentence used to give — "`always() && github
    # .event_name == 'pull_request'`, which IS legitimate" — is wrong about the
    # SHIPPED files and was measured so: on `guard-defaults-impact.yml`, whose
    # producer does no event narrowing, that condition is REJECTED, because it
    # drops the evidence on every `workflow_dispatch` run the producer still
    # performs. It is legitimate only where the producer narrows the same way.
    # A comment that hands the reader an example the code refuses is worse than
    # one that gives none.
    # ⚠️ An earlier version of this sentence also called `success() ||
    # failure()` legitimate, while the control table three screens down lists
    # it as must-reject — the prose and the table contradicted each other
    # inside one hunk. The table is right: those two functions are false on a
    # CANCELLED run, so that spelling is not a synonym for `always()`, and an
    # upload gated on it loses the evidence exactly when a run is killed.
    # Two rules in one module disagreeing about
    # whether a precondition is mandatory or forbidden is a false red waiting
    # for whoever writes the third one.
    #
    # So: it must run after failure, AND what remains must not consult any
    # RESULT. The result contexts are a documented set (GitHub exposes `steps`,
    # `job`, `needs` plus the status functions) — enumeration is legitimate
    # here because the authority for the set is not me. ⚠️ `env.` is in it too:
    # writing to `$GITHUB_ENV` is the standard way to launder one step's
    # outcome into a context that looks static.
    #
    # ⛔ …and "consults no result" is NOT sufficient, which is how this rule was
    # punched through a third and a fourth time. `always() && github.event_name
    # == 'push'` consults nothing and is constant-FALSE on a `pull_request`-only
    # workflow. Deciding satisfiability against the file's own `on:` block fixed
    # that — and then failed on the OTHER file: `guard-defaults-impact.yml`
    # accepts two triggers, so pinning its upload to `workflow_dispatch` is
    # "satisfiable", passes, and uploads nothing on every PR. The property is
    # not satisfiability but "the evidence must not go missing on a run the
    # producer performed", so the producer's own condition is passed in and any
    # NARROWING that the producer does not share is a problem.
    # ⛔ ONE `problems = …` line, still. The loop is over ROWS — synthetic rows
    # and live uploads alike — not a second call site: reverting this line to a
    # weaker helper has to break the synthetic rows too, which is the only
    # reason they are evidence about the wiring rather than about the helper.
    for condition, producer_condition in cases:
        problems = _artifact_upload_problems(doc, condition, producer_condition)
        assert bool(problems) is must_reject, (
            f"{label}: {'; '.join(problems) or 'no problems'}. "
            f"Condition: {condition!r}")
    # An absent file must not add a second red on top of the real failure.
    # `warn` is the action's current default, so this pins the intent against a
    # future major changing it — and against the two files drifting apart again.
    for upload in uploads:
        assert upload.raw.get("with", {}).get("if-no-files-found") == "warn", (
            f"{upload.name} does not pin if-no-files-found; on the (common) "
            f"paths where the producer never wrote the file this decides "
            f"whether the absence becomes a second failure")


# `git ls-tree` and `git ls-files` emit path names and nothing else, so `-z`
# always changes how those names are encoded. `git diff` is the one that has to
# be asked: git-diff(1) documents `-z` as "When --raw, --numstat, --name-only or
# --name-status has been given, do not munge pathnames …" — outside those four
# formats the flag does nothing at all. That sentence is the authority for this
# set, which is what makes listing them a derivation rather than a guess.
_PATH_LISTING_SUBCOMMANDS = ("diff", "ls-tree", "ls-files")
_DIFF_NAME_FORMATS = frozenset({
    "--raw", "--numstat", "--name-only", "--name-status",
})


# Pre-command options that CONSUME THE NEXT WORD. Enumerating is legitimate
# here for the reason the enumeration lesson allows: the authority for this set
# is `git(1)`, not me. ⚠️ `-c` takes `key=value`, which does NOT start with `-`
# — that is precisely how the earlier version mistook it for the subcommand.
_GIT_OPTS_WITH_VALUE = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env",
})
# Words that can precede a command in a `run:` block without changing which
# command it is.
_SHELL_PREFIXES = frozenset({"if", "elif", "while", "until", "then", "do",
                             "!", "time", "command", "exec", "env", "eval"})


def _command_segments(line: str) -> list[list[str]]:
    """argv per command in one shell line, prefixes and `VAR=` words stripped.

    ⛔ The single implementation of "which word is in command position". Two
    rules need it — the `-z` listing rule and the scope-chain discovery — and
    the second one was written without it, so `guard_defaults_scopes.py`
    appearing inside an `::error::` message counted as invoking the helper.
    That is the same "read or written?" confusion this module has already been
    punched through on once, in the rule directly below.
    """
    out: list[list[str]] = []
    for segment in re.split(r"\|\||&&|[|;]|\$\(", line):
        words = segment.strip().split()
        # `if git …`, `! git …`, `command git …`, `env … git …`
        # ⚠️ `env` and `eval` are here because the comment below used to claim
        # they were handled while only bare `VAR=…` words were skipped —
        # measured, `env FOO=1 git diff …` came back False. The claim was in
        # the prose and the sample row labelled `env-prefix` actually sampled
        # the bare-VAR form, so nothing contradicted it.
        while words and words[0] in _SHELL_PREFIXES:
            words = words[1:]
        # leading VAR=... assignments (`GIT_DIR=/x git …`)
        while words and ("=" in words[0] and not words[0].startswith("-")):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _invokes_scope_helper(line: str) -> bool:
    """Does this line RUN the scope helper (rather than name it in a message)?

    ⛔ Command position, both directly and behind an interpreter. Substring
    matching reported `Run da-guard` as a second invocation — it mentions the
    helper in four `::error::` and report strings, all of them explaining that
    a malformed row came from the producer.
    """
    for words in _command_segments(line):
        head = words[0].rsplit("/", 1)[-1]
        if head == _SCOPE_HELPER:
            return True
        if head.startswith("python") and any(
                w.rsplit("/", 1)[-1] == _SCOPE_HELPER for w in words[1:]):
            return True
    return False


def _lists_paths(line: str) -> bool:
    """Does this line INVOKE a git path listing (rather than mention one)?

    ⛔ Command position, not substring. `"git ls-tree" in line` also matched
    `echo "::error::git ls-tree exited $rc …"` — an error message ABOUT the
    command — and the rule then demanded `-z` on an `echo`.

    ⚠️ SCOPE, stated because the previous three versions of this rule each read
    as an invariant and each was punched through by one edit. This is defence
    in depth over a SYNTACTIC surface: it sees the shapes below and nothing
    guarantees it sees every shape a shell can express. The load-bearing guard
    is behavioural — `test_the_listing_reaches_the_helper_byte_for_byte` runs
    the step and reads what the consumer actually received. Treat a green here
    as "no obvious listing lost its `-z`", never as "the property holds".
    """
    for words in _command_segments(line):
        # `/usr/bin/git` is the same command as `git`
        if words[0].rsplit("/", 1)[-1] != "git":
            continue
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            takes_value = rest[0] in _GIT_OPTS_WITH_VALUE
            rest = rest[1:]
            if takes_value and rest:
                rest = rest[1:]
        if not rest or rest[0] not in _PATH_LISTING_SUBCOMMANDS:
            continue
        # ⛔ `git diff` only lists paths in the four formats `-z` acts on.
        # Without this, an ordinary diagnostic — `git diff --stat`, `--quiet`,
        # `--shortstat` — was demanded to carry `-z`, and the cheapest way to
        # green that demand is to add the flag where git documents it as doing
        # NOTHING. After which `-z` is no longer evidence of anything, here or
        # anywhere else this rule looks. A guard whose cheapest repair destroys
        # the property it checks is worse than no guard.
        if rest[0] == "diff" and not (set(rest[1:]) & _DIFF_NAME_FORMATS):
            continue
        return True
    return False


def _strip_shell_comment(line: str) -> str:
    """Drop a `#` comment, honouring quotes. Pure, so it can be sampled.

    ⛔ Extracted because its previous control was vacuous: it asserted that a
    string which never appears in the artifact does not appear in the artifact.
    Replacing the whole quote-aware loop with a naive "drop lines starting with
    #" left the suite green, and the naive version cannot see a trailing
    comment — which is the entire attack this reader exists to stop.
    """
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _executable_lines(script: str) -> list[str]:
    """One `run:` body as the STATEMENTS it runs: folded, de-commented, non-empty.

    ⛔ The single implementation of "what does this script actually execute".
    It used to exist twice: this folding lived inside `_shell_commands` while
    `test_the_scope_helper_is_told_that_its_input_is_nul_separated` walked
    `splitlines()` raw. Measured — moving `--null -` onto a continuation line,
    a formatting edit that changes nothing git or the helper sees, made that
    test report "the helper is fed NUL-separated input but not told" while the
    very next physical line said `--null`. A predicate that disagrees with the
    shell about where a statement ends produces messages that are not merely
    unhelpful but false.
    """
    folded = re.sub(r"\\\n[ \t]*", " ", script)
    return [text for text in (_strip_shell_comment(ln)
                              for ln in folded.splitlines()) if text]


def _shell_commands(workflow: Path) -> list[str]:
    """Every `run:` line with shell comments removed, folded on continuations.

    ⛔ Comments are stripped BEFORE any rule looks at the text. A rule that
    substring-matches the raw YAML line is satisfied by a trailing comment:
    restoring the pre-fix command and appending `# dropped -z on purpose` kept
    the whole module green while the line git actually runs was byte-for-byte
    the old one (measured). `#` inside a quoted string is not a comment, so the
    strip is quote-aware rather than a regex.
    """
    out: list[str] = []
    for step in _steps(workflow):
        out.extend(_executable_lines(step.script))
    return out


# ⛔ The ONE listing in these three files whose output is not read back path by
# path: it is truncated to five entries, comma-joined and printed in a comment
# header. It is exempted verbatim rather than by a shape, because every shape
# that tried to describe "is this parsed?" turned out to be an enumeration in
# disguise — first the command name, then the redirect form (`> "$VAR"`), which
# `| tee "$f" >/dev/null` and a one-line shell function both walk past while
# staying byte-identical in behaviour. A single literal is closed: any new or
# reworded listing fails this rule until someone amends this constant, and that
# amendment is the review trigger. The measurement behind the exemption is on
# the line itself in blast-radius.yml — adding `-z` there crashes the consumer.
#
# ⛔⛔ THIS WHOLE EXEMPTION DIES WHEN THE TIER-SEMANTICS PR MERGES, and the
# instruction is DELETE, not update. That PR replaces this exact statement with
# one that carries `-z` and routes the bytes through `_printable()`, so both
# sentences above stop being true: the output IS NUL-separated there, and
# adding `-z` no longer crashes anything. Measured on the merged tree: this
# control fails with "no longer exist verbatim", and of the two ways to green
# it, re-pointing the literal at the merged line is the WORSE one — it parks a
# now-compliant listing in an exemption slot and leaves the paragraph above
# asserting two things that are false. Emptying the set is the better one: the
# rule then covers that line for real, and the suite is green (measured, both
# ways).
_DISPLAY_ONLY_LISTINGS = {
    'changed_raw=$(git diff --name-only "origin/$BASE_REF"...HEAD -- '
    '"${{ steps.detect.outputs.conf_d }}")',
}


@pytest.mark.parametrize("workflow", [CONFIG_DIFF, BLAST_RADIUS, GUARD_DEFAULTS],
                         ids=["config-diff", "blast-radius", "guard-defaults"])
def test_no_path_listing_lets_git_encode_the_paths(workflow: Path):
    """⛔ EVERY path listing in all three files, exemptions named one by one.

    git encodes non-ASCII paths unless given `-z`, and the readers here split
    on the raw bytes — measured on the scope helper, that yields a target on a
    directory which does not exist (red, blaming da-guard) or an `unmanaged`
    row that keeps the job GREEN while asserting the file is outside every
    conf.d tree.

    ⚠️ Three earlier versions of this rule were each an enumeration wearing a
    different hat. One named a single FILE. One discovered listings with a
    precedence bug, so an `ls-tree` line was only discovered once it already
    complied. One filtered on the REDIRECT SHAPE `> "$VAR"`, and `| tee` or a
    shell function made the listing vanish from the rule while the behaviour
    was unchanged — measured, and the resulting non-compliance was caught by
    unrelated behavioural tests rather than by the rule claiming to cover it.
    There is no shape that answers "is this output parsed?"; the closed thing
    is the SET of listings in three files, so that is what this pins.
    """
    listings = [ln for ln in _shell_commands(workflow) if _lists_paths(ln)]
    assert listings, (
        f"{workflow.name}: no path listing was discovered — either the reader "
        f"broke or the file stopped listing paths; both leave this rule "
        f"asserting nothing")
    for line in listings:
        if line in _DISPLAY_ONLY_LISTINGS:
            continue
        assert " -z " in f" {line} ", (
            f"{workflow.name}: without -z git C-quotes non-ASCII names, and "
            f"every consumer here splits on the raw bytes. If this listing is "
            f"genuinely display-only, add it to _DISPLAY_ONLY_LISTINGS with the "
            f"measurement that justifies it — do not widen the rule: {line!r}")
        # ⛔ `-z` and `$( )` are mutually exclusive, and this is the half the
        # rule above was missing. `-z` makes git separate paths with NUL; bash
        # DROPS NUL bytes from a command substitution, so a `-z` listing
        # captured that way comes back as every path concatenated into one
        # string. Measured: `printf 'a\0b\0c\0' | $(cat)` yields `abc`.
        # ⚠️ This is not hypothetical politeness. The sibling exemption's
        # failure message tells a maintainer to check whether the replacement
        # "carries -z" and to delete the exemption if it does. Following that
        # literally — adding `-z` to the variable-capture form and deleting
        # the exemption — measured green: the listing was silently broken
        # and nothing in the module noticed, because nothing reads the runtime
        # value. A repair recipe the guard cannot check is a hole with an
        # instruction attached, so the missing half is asserted here rather
        # than added to the prose.
        assert not re.search(r"=\s*\$\(\s*(?:\S*/)?git\b", line), (
            f"{workflow.name}: this listing passes `-z` and captures the "
            f"result with `$( )`. Bash drops NUL bytes from a command "
            f"substitution, so every path arrives concatenated into one "
            f"string. Route it through a file instead — `git diff -z … > "
            f'"$f"` then read `$f`: {line!r}')


def test_control_the_display_only_exemption_is_not_stale():
    """An exemption pointing at a line nobody writes any more is a hole.

    ⛔ The message below says DELETE rather than update, deliberately. The
    obvious reading of "no longer exists verbatim" is "re-point the literal",
    and on the merged tree that is the wrong move — measured: it parks a line
    that now carries `-z` into an exemption slot and leaves this file asserting
    two things that stopped being true. A failure message that names the
    cheaper-but-worse repair is how a guard gets disarmed by someone doing what
    it told them.
    """
    live = {ln for wf in (CONFIG_DIFF, BLAST_RADIUS, GUARD_DEFAULTS)
            for ln in _shell_commands(wf)}
    assert _DISPLAY_ONLY_LISTINGS <= live, (
        f"exempted listing(s) no longer exist verbatim: "
        f"{sorted(_DISPLAY_ONLY_LISTINGS - live)}.\n"
        f"⛔ Do NOT re-point the literal at whatever replaced them. Check "
        f"whether the replacement now carries `-z` AND routes the output "
        f"through a FILE; if both, EMPTY the set — "
        f"`_DISPLAY_ONLY_LISTINGS: set[str] = set()` — and delete the "
        f"paragraph above it, so the rule covers that line for real. "
        f"⚠️ EMPTY, not delete the name: the constant is read by this test and "
        f"by the rule below, and removing it raises `NameError` in both. "
        f"Emptying is also the form that was actually measured on the merged "
        f"tree. Re-pointing keeps a compliant listing exempt and leaves the "
        f"stated rationale false.\n"
        f"⚠️ `-z` with a `$( )` capture is NOT the fixed shape — bash drops "
        f"NUL, so the paths come back concatenated. The sibling rule "
        f"`test_no_path_listing_lets_git_encode_the_paths` now refuses that "
        f"combination, so you cannot reach a green suite through it.")


@pytest.mark.parametrize("line, expected", [
    ("git diff -z --name-only x > \"$f\"", "git diff -z --name-only x > \"$f\""),
    # the attack the previous control could not see
    ("git diff --name-only x > \"$f\"  # dropped -z on purpose",
     "git diff --name-only x > \"$f\""),
    ("# a whole-line comment", ""),
    ("   # an indented whole-line comment", ""),
    # a `#` inside quotes is not a comment and must survive
    ("echo \"issue #1419\"", "echo \"issue #1419\""),
    ("echo 'sha#deadbeef' # trailing", "echo 'sha#deadbeef'"),
], ids=["plain", "trailing-comment", "whole-line", "indented",
        "hash-in-double-quotes", "hash-in-single-quotes"])
def test_control_the_comment_strip_is_quote_aware(line: str, expected: str):
    """⛔ Sampled on synthetic input, both directions.

    The previous control asserted that a string which never appears in the
    artifact does not appear in the artifact — vacuous. Replacing the whole
    quote-aware loop with a naive "drop lines starting with #" left the suite
    green while the trailing-comment attack walked straight through.
    """
    assert _strip_shell_comment(line) == expected


@pytest.mark.parametrize("script, expected", [
    # ⛔ The comment strip must be WIRED IN, not merely present. Blind review
    # replaced the `_strip_shell_comment(ln)` call inside `_executable_lines`
    # with a bare `ln.strip()` and the module stayed green: the sibling
    # control above proves the stripper works and says nothing about whether
    # anything calls it. With it bypassed, comment text becomes "an executable
    # line", which is the trailing-comment attack `_shell_commands` exists to
    # stop — a `# … -z …` comment would satisfy the `-z` rule for a command
    # that does not carry it.
    ("git diff --name-only x > f  # dropped -z on purpose",
     ["git diff --name-only x > f"]),
    ("# a whole-line comment\ngit status", ["git status"]),
    # ⛔ …and the continuation fold, for the same reason. Removing it also left
    # green, and the false red it prevents is the one this round measured:
    # a helper call split across a `\` continuation reads as two statements,
    # so a flag on the second line is invisible to every rule here.
    # ⚠️ The fold leaves the space that preceded the backslash, so the words
    # are joined with two spaces. Written out as measured rather than as
    # imagined — an expectation that quietly "tidies" the output is an
    # expectation copied from what I meant instead of from what runs.
    ('python3 helper.py \\\n  --null - \\\n  < in > out',
     ["python3 helper.py  --null -  < in > out"]),
    # both at once, and blank lines dropped
    ("a \\\n  b  # tail\n\n# whole\nc", ["a  b", "c"]),
], ids=["trailing-comment", "whole-line-comment", "continuation",
        "both-and-blanks"])
def test_control_the_executable_line_reader_folds_and_strips(script, expected):
    """The two pre-processing steps every rule in this module reads through."""
    assert _executable_lines(script) == expected


def test_the_scope_helper_is_told_that_its_input_is_nul_separated():
    """The producer's `-z` and the consumer's `--null` are one decision.

    ⛔ Asserted through the DATA PATH, not by two independent string searches:
    the earlier version grepped for `-z` and for `--null` in the same script
    and passed while the helper read `/dev/null`, or read a file nothing wrote.

    ⛔ Statements, not physical lines. This walked `splitlines()` raw, so
    putting `--null -` on a continuation line — a formatting edit that changes
    nothing the shell or the helper sees — produced "the helper is fed
    NUL-separated input but not told" about a call whose next physical line
    said `--null`. `_executable_lines` folds continuations exactly as the
    `-z` rule already did.
    """
    chain, _env = _scope_pipeline()
    calls = [ln for s in chain for ln in _executable_lines(s.script)
             if _invokes_scope_helper(ln)]
    assert calls, (
        f"{[str(s) for s in chain]} no longer invokes {_SCOPE_HELPER} — "
        f"unreachable, since the chain is discovered BY that invocation, but "
        f"asserted so that a future change to the discovery cannot leave this "
        f"test looping over nothing")
    for line in calls:
        assert "--null" in line or " -0 " in f" {line} ", (
            f"the helper is fed NUL-separated input but not told: {line!r}")

@needs_bash
def test_the_listing_reaches_the_helper_byte_for_byte(tmp_path) -> None:
    """⛔ RUN the step and read what the helper was actually handed.

    The previous version of this compared the two filenames the script mentions
    and called that "the DATA PATH". It is not: replacing the echo line with
    `: > "$RUNNER_TEMP/changed-defaults.txt"` truncates the listing between the
    two ends, the helper reads an empty file, `guard-targets.tsv` comes out
    empty, `Run da-guard` takes its whole-tree branch — green, with the PR's
    changed `_defaults.yaml` never scoped, which is the #1219 shape. The whole
    module stayed green (measured). Only executing the step can see that.
    """
    chain, _env = _scope_pipeline()
    step = chain[-1]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    repo = tmp_path / "repo"
    (repo / "conf.d").mkdir(parents=True)
    (repo / "conf.d" / "_defaults.yaml").write_text("defaults:\n  cpu: 1\n",
                                                    encoding="utf-8", newline="\n")
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "conf.d" / "_defaults.yaml").write_text("defaults:\n  cpu: 2\n",
                                                    encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr edits the defaults")

    # A stub helper that records its stdin verbatim, so the assertion is about
    # what crossed the boundary rather than about what the script says.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = runner_temp / "helper-stdin.bin"
    _stub(bin_dir, "python3",
          "#!/usr/bin/env bash\n"
          f'cat > "{seen.as_posix()}"\n'
          'printf "target\\tconf.d\\tconf.d\\n"\n')
    script = _render("\n".join(s.script for s in chain), {})
    proc = _run(step, script, repo, {
        "BASE_REF": "main",
        "CONF_D": "conf.d",
        "RUNNER_TEMP": str(runner_temp.as_posix()),
    }, bin_dir)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert seen.exists(), (
        f"the helper was never invoked by {[str(s) for s in chain]}:\n"
        f"{proc.stdout}\n{proc.stderr}")
    payload = seen.read_bytes()
    assert payload, (
        "the helper received an EMPTY stream — the listing and the helper name "
        "the same file, but nothing travelled between them")
    assert b"conf.d/_defaults.yaml" in payload, (
        f"the helper did not receive the changed path: {payload!r}")
    assert b"\0" in payload, (
        f"the helper received newline-separated input, so the producer is not "
        f"passing -z: {payload!r}")
