"""A `run:` step may read `$?` in one way only (#3946).

The defect: a step with no `shell:` runs as `bash -e {0}` (and `shell: bash`
as `bash --noprofile --norc -eo pipefail {0}`), so

    some-command > out.txt 2>&1
    rc=$?

never gets to `rc=$?` when the command fails — the step ends on the command's
line, and whatever was written after it to report the failure is skipped on
exactly the runs that had something to report.
`.github/workflows/required-checks-drift.yml` had this shape;
`.github/workflows/main-red-detector.yml` had it until #3944.

THE RULE — text only; nothing is executed and no shell flag is consulted. In
every `run:` of a workflow or a composite action, `$?` / `${?}` may appear
only at the end of a logical line (backslash continuations joined) as

    … || NAME=$?        … || local NAME=$?        … || exit $?        … || return $?

That form is right with errexit on or off, which is why the rule does not
need to know which it is. Every other appearance is a violation — inside a
string, a `$(…)`, a heredoc, a test, after a `;`, with a trailing comment —
unless it is a row of `_BARE_READS`, the reads that predate the rule. That
table may only get shorter: a row that no longer matches a line fails, and a
read that is not in it fails.

Only whole-line comments are passed over. A `$?` in a step whose shell is not
sh-like (pwsh, python, cmd, …) fails and cannot be listed.

NOT SEEN, by construction:
  * `a | b || rc=$?` without pipefail reads `b`, not `a`. (`PIPESTATUS` has
    its own lint: scripts/tools/lint/check_pipestatus_errexit.py.)
  * `echo "$(cmd)" || rc=$?` reads `echo`, not `cmd` — the read is of the
    command left of `||`, whatever that command hides. (The one shape of this
    that is refused is a declaration: `local x=$(cmd) || rc=$?` can only
    read 0.)
  * a row of `_BARE_READS` is keyed by its text, not by why it is safe: move
    `set +e` away from a listed read and the row still matches.
  * text that is not this script's own command line: a heredoc that writes
    another script, a multi-line quoted string handed to `bash -c`.
  * scripts a step calls (`scripts/**/*.sh`) — files with their own flags.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

import pytest
from _workflow_steps import (
    ACTIONS_DIR,
    WORKFLOW_DIR,
    UnmodelledShell,
    effective_shell,
    run_steps,
    step_shell_argv,
)

# `${?` and not `${?}`: `${?:-0}`, `${?#x}` are reads too, and none is the allowed form.
READ = re.compile(r"\$\?|\$\{\?")
ALLOWED = re.compile(
    r"\|\|\s*(?:(?:local\s+)?[A-Za-z_][A-Za-z0-9_]*=|exit\s+|return\s+)(?:\$\?|\$\{\?\})\s*\Z")
# `local x=$(cmd) || rc=$?` reads the declaration's status, which is 0 whatever
# `cmd` did — the allowed form on paper, a read that can never be non-zero.
DECLARES = re.compile(r"\s*(?:local|export|declare|typeset|readonly)\s[^|]*\$\(")
# No `shell:` is bash. A custom template counts when its program is one of these.
SH_LIKE = {"bash", "sh"}
# Fewer than this many `run:` steps means the walk lost most of the tree.
MIN_STEPS = 300

# The bare reads that predate the rule, each read against its script:
# (file, job, step, the line stripped, how many times in that step, why a
#  non-zero status reaches it).
# ⛔ Only ever shortened. Do not add a row — write `rc=0` then `cmd || rc=$?`.
_BARE_READS = [
    ("workflows/bench-record.yaml", "bench-record",
     "Paired interleaved measurement (main vs pinned reference)", "rc=$?", 1,
     "first command of the `else` of `if bash …; then`: it reads the condition's status"),
    ("workflows/bench-workload-effect.yaml", "workload-effect",
     "Probe the pinned nightly reference (self-test, notice only)", "rc=$?", 1,
     "the step opens with `set +e -uo pipefail` and never turns errexit back on"),
    ("workflows/component-docker-build.yaml", "build",
     "da-tools image reads a checkout it does not own (issue 1444)", "rc=$?", 1,
     "`set +e` two lines above, `set -e` on the line after"),
    ("workflows/config-diff-advisory.yml", "config-diff", "Run config-diff", "rc=$?", 1,
     "the step opens with `set +e -uo pipefail`; only an `if … exit 1` sits in between"),
    ("workflows/config-diff-advisory.yml", "config-diff", "Run config-diff", "json_rc=$?", 1,
     "same step, same `set +e`; the line before is the second config_diff.py call"),
    ("workflows/guard-defaults-impact.yml", "guard",
     "Determine scope from changed _-prefixed YAML", "mb_rc=$?", 1,
     "`set +e` directly above `git merge-base`, `set -e` directly below the read"),
    ("workflows/guard-defaults-impact.yml", "guard",
     "Determine scope from changed _-prefixed YAML", "diff_rc=$?", 2,
     "both follow a `git diff` wrapped in its own `set +e` … `set -e`"),
    ("workflows/guard-defaults-impact.yml", "guard", "Run da-guard", "local rc=$?", 1,
     "`set +e` above the da-guard call, `set -e` below; `local` expands `$?` before it runs"),
    ("workflows/guard-defaults-impact.yml", "guard", "Run da-guard", "gen_rc=$?", 1,
     "`set +e` above the route generator call, `set -e` below the read"),
]


def logical_lines(run: str) -> list[str]:
    """The script's lines with whole-line comments dropped and backslash
    continuations joined."""
    joined: list[str] = []
    pending = ""
    # Not `splitlines()`: it also breaks on U+2028 and friends, which bash
    # does not — the rest of such a line would be read as a line of its own.
    for line in run.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1]
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def bare_reads(run: str) -> list[str]:
    """Each logical line (stripped) that holds a `$?` outside the allowed form."""
    return [line.strip() for line in logical_lines(run)
            if READ.search(line) and not (len(READ.findall(line)) == 1
                                          and ALLOWED.search(line)
                                          and not DECLARES.match(line))]


def sh_like(shell: str | None) -> bool:
    if shell is None:
        return True
    words = shell.split()
    return bool(words) and Path(words[0]).name in SH_LIKE


def status_read_problems(workflow_dir: Path, actions_dir: Path | None,
                         ledger: list[tuple]) -> tuple[list[str], int]:
    """Every reason the tree does not pass, and the number of steps walked."""
    steps, unread = run_steps(workflow_dir, actions_dir)
    problems = [f"{entry} — not read, so its steps were not checked" for entry in unread]
    found: collections.Counter = collections.Counter()
    for step in steps:
        reads = bare_reads(step.run)
        if reads and not sh_like(step.shell):
            problems.append(f"{step.label}: `$?` under `shell: {step.shell}` — not a POSIX "
                            f"shell, cannot be listed: {reads}")
            continue
        found.update((step.file, step.job, step.step, text) for text in reads)
    listed: collections.Counter = collections.Counter()
    for file, job, step, text, count, why in ledger:
        key = (file, job, step, text)
        if key in listed or count < 1 or not why.strip():
            problems.append(f"_BARE_READS: {key} is listed twice, or has no count or reason")
        listed[key] += count
    for key in sorted(set(found) | set(listed)):
        have, want = found[key], listed[key]
        where = f"{key[0]} · {key[1]} · {key[2]}: `{key[3]}`"
        if have > want:
            problems.append(
                f"{where} reads `$?` outside the one allowed form ({have} time(s), {want} "
                "listed). Write `rc=0` on its own line, then `command || rc=$?` — a bare "
                "read is never reached when the step runs under errexit.")
        elif have < want:
            problems.append(f"{where} is listed {want} time(s) in _BARE_READS and found "
                            f"{have} — it was rewritten or removed: shorten the table.")
    return problems, len(steps)


# ── the tree ────────────────────────────────────────────────────────────────

def test_every_status_read_on_the_tree_is_in_the_allowed_form_or_listed() -> None:
    problems, walked = status_read_problems(WORKFLOW_DIR, ACTIONS_DIR, _BARE_READS)
    assert not problems, "\n  ".join([f"{len(problems)} problem(s):"] + problems)
    assert walked >= MIN_STEPS, f"only {walked} `run:` steps were walked"


def test_the_two_steps_this_rule_exists_for_use_the_allowed_form() -> None:
    """Both were the defect. If either stops reading `$?` the rule may be
    looking at nothing that matters; neither may ever be a row of the table."""
    steps, _ = run_steps()
    for file, needle in [("workflows/required-checks-drift.yml", "required_checks_drift.py"),
                         ("workflows/main-red-detector.yml", "main_red_report.py")]:
        found = [s for s in steps if s.file == file and needle in s.run]
        assert len(found) == 1, (file, [s.label for s in found])
        lines = [line for line in logical_lines(found[0].run) if READ.search(line)]
        assert len(lines) == 1 and ALLOWED.search(lines[0]), (file, lines)
        assert bare_reads(found[0].run) == []
        assert not [row for row in _BARE_READS if row[0] == file]


# ── the rule ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("script", [
    "rc=0\ncmd a || rc=$?\n",
    "cmd a || rc=${?}\n",
    "cmd a > out 2>&1 || rc=$?   \n",
    "cmd a \\\n  --flag \\\n  > out 2>&1 || rc=$?\n",
    "out=$(cd dir && cmd a \\\n  $targets) || rc=$?\n",
    "f() {\n  cmd a || local rc=$?\n  cmd b || return $?\n}\n",
    "cmd a || exit $?\n",
    "cmd a ||rc=$?\n",
    "cmd a | tee log || rc=$?\n",
    "# never a bare rc=$? here\n  # nor echo $? there\ncmd a || rc=$?\n",
    "echo 'no status read at all'\n",
], ids=["or-assignment", "braced", "trailing-space", "continued", "in-a-substitution",
        "local-and-return", "exit", "no-space", "pipeline", "comment-lines", "none"])
def test_the_allowed_forms_pass(script) -> None:
    assert bare_reads(script) == []


@pytest.mark.parametrize("script, flagged", [
    ("cmd a\nrc=$?\n", ["rc=$?"]),
    ("cmd a\nrc=${?}\n", ["rc=${?}"]),
    ("cmd a\nrc=${?:-0}\n", ["rc=${?:-0}"]),
    ("cmd a || rc=${?#x}\n", ["cmd a || rc=${?#x}"]),
    ("local out=$(cmd a) || rc=$?\n", ["local out=$(cmd a) || rc=$?"]),
    ("  export OUT=$(cmd a) || rc=$?\n", ["export OUT=$(cmd a) || rc=$?"]),
    ("x=\u2028# not a comment; rc=$?\n", ["x=\u2028# not a comment; rc=$?"]),
    ("cmd a\n  local rc=$?\n", ["local rc=$?"]),
    ('cmd a\necho "rc=$?"\n', ['echo "rc=$?"']),
    ("cmd a\nif [ $? -ne 0 ]; then echo no; fi\n", ["if [ $? -ne 0 ]; then echo no; fi"]),
    ("cmd a\ncase $? in\n  0) ;;\nesac\n", ["case $? in"]),
    ("cmd a\nexit $?\n", ["exit $?"]),
    ("cmd a\nreturn $?\n", ["return $?"]),
    ('cmd a || echo "$(date) rc=$?"\n', ['cmd a || echo "$(date) rc=$?"']),
    ('cmd a || echo "failed $?"\n', ['cmd a || echo "failed $?"']),
    ("cmd a || rc=$?  # keep the status\n", ["cmd a || rc=$?  # keep the status"]),
    ("cmd a || rc=$? || other=$?\n", ["cmd a || rc=$? || other=$?"]),
    ("cmd a || rc=$?; echo done\n", ["cmd a || rc=$?; echo done"]),
    ('cmd a || rc="$?"\n', ['cmd a || rc="$?"']),
    ("cmd a && rc=$?\n", ["cmd a && rc=$?"]),
    ("cmd a | rc=$?\n", ["cmd a | rc=$?"]),
    ("cmd a; rc=$?\n", ["cmd a; rc=$?"]),
    ("cmd a || exit $?x\n", ["cmd a || exit $?x"]),
    ("cmd a || rc=$?\\\n  more\n", ["cmd a || rc=$?  more"]),
    ("cat <<EOF\nstatus $?\nEOF\n", ["status $?"]),
    ("ssh host 'cmd a; echo $?'\n", ["ssh host 'cmd a; echo $?'"]),
    ("x=$(cmd a; echo $?)\n", ["x=$(cmd a; echo $?)"]),
    ("trap 'rc=$?; echo $rc' EXIT\n", ["trap 'rc=$?; echo $rc' EXIT"]),
    ("set +e\ncmd a\nrc=$?\n", ["rc=$?"]),
    ("if cmd a; then :; else\n  rc=$?\nfi\n", ["rc=$?"]),
], ids=["bare", "braced", "braced-with-a-default", "braced-with-a-trim",
        "declaration-hides-the-command", "export-hides-the-command", "u2028-is-not-a-newline",
        "local", "echo", "test", "case", "exit-without-or",
        "return-without-or", "substitution-in-the-message", "echo-after-or", "trailing-comment",
        "two-reads", "more-after", "quoted", "and", "pipe", "semicolon", "glued", "continued-on",
        "heredoc", "for-another-shell", "in-a-substitution", "trap", "after-set-plus-e",
        "else-branch"])
def test_every_other_appearance_is_flagged(script, flagged) -> None:
    """The last two are right as written — the rule does not look at errexit,
    so they are flagged too and can only exist as rows of the table."""
    assert bare_reads(script) == flagged


# ── the walk, the table, and what cannot be measured ────────────────────────

def _tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    workflows, actions = tmp_path / "workflows", tmp_path / "actions"
    workflows.mkdir()
    actions.mkdir()
    for name, text in files.items():
        path = (actions if "/" in name else workflows) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return workflows, actions


def _workflow(run: str, *, step_shell="", job_default="", top_default="") -> str:
    def default(value: str, indent: str) -> str:
        return f"{indent}defaults:\n{indent}  run:\n{indent}    shell: {value}\n" if value else ""
    body = "\n".join("          " + line for line in run.splitlines())
    return (f"on: push\n{default(top_default, '')}jobs:\n  j:\n{default(job_default, '    ')}"
            "    runs-on: ubuntu-24.04\n    steps:\n      - name: s\n"
            + (f"        shell: {step_shell}\n" if step_shell else "")
            + f"        run: |\n{body}\n")


def _action(run: str, shell: str = "bash") -> str:
    body = "\n".join("        " + line for line in run.splitlines())
    return ("name: a\nruns:\n  using: composite\n  steps:\n    - name: s\n"
            f"      shell: {shell}\n      run: |\n{body}\n")


_BARE, _FINE = "cmd a\nrc=$?", "cmd a || rc=$?"


def _problems(tmp_path, files, ledger=()):
    workflows, actions = _tree(tmp_path, files)
    return status_read_problems(workflows, actions, list(ledger))[0]


def test_a_bare_read_outside_the_table_fails_and_names_the_form(tmp_path) -> None:
    problems = _problems(tmp_path, {"w.yml": _workflow(_BARE)})
    assert len(problems) == 1, problems
    assert problems[0].startswith("workflows/w.yml · j · s: `rc=$?` reads `$?` outside")
    assert "`command || rc=$?`" in problems[0]
    (tmp_path / "ok").mkdir()
    assert _problems(tmp_path / "ok", {"w.yml": _workflow(_FINE)}) == []


@pytest.mark.parametrize("kwargs", [
    {}, {"step_shell": "bash"}, {"step_shell": "sh"}, {"job_default": "bash"},
    {"top_default": "bash"}, {"step_shell": "'bash -euo pipefail {0}'"},
], ids=["none", "bash", "sh", "job-default", "workflow-default", "template"])
def test_the_rule_does_not_depend_on_the_shell_flags(tmp_path, kwargs) -> None:
    problems = _problems(tmp_path, {"w.yml": _workflow(_BARE, **kwargs)})
    assert len(problems) == 1 and "outside the one allowed form" in problems[0], problems


def test_a_listed_read_passes_and_the_table_is_exact(tmp_path) -> None:
    row = ("workflows/w.yml", "j", "s", "rc=$?", 1, "set +e above")
    files = {"w.yml": _workflow(_BARE)}
    (tmp_path / "a").mkdir()
    assert _problems(tmp_path / "a", files, [row]) == []
    # A second bare read with the same text is one more than listed.
    (tmp_path / "b").mkdir()
    twice = _problems(tmp_path / "b", {"w.yml": _workflow(_BARE + "\ncmd b\nrc=$?")}, [row])
    assert len(twice) == 1 and "(2 time(s), 1 listed)" in twice[0], twice


@pytest.mark.parametrize("files, row", [
    ({"w.yml": _workflow(_FINE)}, ("workflows/w.yml", "j", "s", "rc=$?", 1, "why")),
    ({"w.yml": _workflow(_BARE)}, ("workflows/w.yml", "j", "s", "rc=$?", 2, "why")),
    ({"w.yml": _workflow(_BARE)}, ("workflows/w.yml", "j", "renamed", "rc=$?", 1, "why")),
    ({"w.yml": _workflow(_BARE)}, ("workflows/gone.yml", "j", "s", "rc=$?", 1, "why")),
], ids=["rewritten", "one-of-two-removed", "step-renamed", "file-removed"])
def test_a_row_that_no_longer_matches_fails(tmp_path, files, row) -> None:
    problems = _problems(tmp_path, files, [row])
    assert any("shorten the table" in p for p in problems), problems


@pytest.mark.parametrize("rows", [
    [("workflows/w.yml", "j", "s", "rc=$?", 1, "")],
    [("workflows/w.yml", "j", "s", "rc=$?", 0, "why")],
    [("workflows/w.yml", "j", "s", "rc=$?", 1, "why")] * 2,
], ids=["no-reason", "zero", "twice"])
def test_a_malformed_row_fails(tmp_path, rows) -> None:
    problems = _problems(tmp_path, {"w.yml": _workflow(_BARE)}, rows)
    assert any(p.startswith("_BARE_READS: ") for p in problems), problems


@pytest.mark.parametrize("kwargs", [
    {"step_shell": "pwsh"}, {"step_shell": "python"}, {"step_shell": "cmd"},
    {"step_shell": "powershell"}, {"job_default": "pwsh"}, {"top_default": "python"},
    {"step_shell": "'pwsh -command \". {0}\"'"},
], ids=["pwsh", "python", "cmd", "powershell", "job-default", "workflow-default", "template"])
@pytest.mark.parametrize("run", [_BARE, "cmd a; $ok = $?"], ids=["bare", "pwsh-idiom"])
def test_a_status_read_under_a_shell_that_is_not_sh_cannot_be_listed(tmp_path, kwargs,
                                                                     run) -> None:
    text = run.splitlines()[-1].strip()
    row = ("workflows/w.yml", "j", "s", text, 1, "why")
    problems = _problems(tmp_path, {"w.yml": _workflow(run, **kwargs)}, [row])
    assert any("not a POSIX shell, cannot be listed" in p for p in problems), problems


def test_another_shell_without_a_status_read_is_not_this_rules_business(tmp_path) -> None:
    assert _problems(tmp_path, {"w.yml": _workflow("Write-Host hi", step_shell="pwsh")}) == []


def test_composite_actions_are_walked(tmp_path) -> None:
    problems = _problems(tmp_path, {"w.yml": _workflow(_FINE), "x/action.yml": _action(_BARE),
                                    "y/deep/action.yaml": _action(_BARE, "pwsh")})
    assert len(problems) == 2, problems
    assert problems[0].startswith("actions/y/deep/action.yaml · runs · s: `$?` under `shell: pwsh`")
    assert problems[1].startswith("actions/x/action.yml · runs · s: `rc=$?` reads")


@pytest.mark.parametrize("name, text", [
    ("bad.yml", "on: [\n"), ("bad.yml", ""), ("bad.yml", "- a\n"), ("bad.yml", "on: push\n"),
    ("bad.yml", "on: push\njobs: []\n"), ("bad.yml", "on: push\njobs:\n  j: nope\n"),
    ("bad.yml", "on: push\njobs:\n  j:\n    steps:\n      - run: [a, b]\n"),
    ("bad.yml", "on: push\njobs:\n  j:\n    steps:\n      - run: 3\n"),
    ("bad.yml", "on: push\njobs:\n  j:\n    steps:\n      a: {run: 'x'}\n"),
    ("bad.yml", "on: push\njobs:\n  j:\n    steps: nope\n"),
    ("bad.yml", "on: push\njobs:\n  j:\n    steps:\n      - nope\n"),
    ("z/action.yml", "runs: [\n"), ("z/action.yml", "name: a\n"),
    ("z/action.yml", "runs:\n  steps:\n    - run: {a: b}\n"),
], ids=["not-yaml", "empty", "a-list", "no-jobs", "jobs-is-a-list", "job-is-a-string",
        "run-is-a-list", "run-is-a-number", "steps-is-a-map", "steps-is-a-string",
        "step-is-a-string", "action-not-yaml", "action-no-runs",
        "action-run-is-a-map"])
def test_a_file_that_cannot_be_read_is_a_problem_not_a_skip(tmp_path, name, text) -> None:
    problems = _problems(tmp_path, {"good.yaml": _workflow(_BARE), name: text})
    assert len(problems) == 2, problems
    assert any(p.startswith(("workflows/bad.yml: ", "actions/z/action.yml: ")) for p in problems)
    assert any(p.startswith("workflows/good.yaml · j · s") for p in problems), (
        "the readable file is still checked")


def test_both_extensions_and_unnamed_steps_are_walked(tmp_path) -> None:
    unnamed = _workflow(_BARE).replace("      - name: s\n        run:", "      - run:")
    problems = _problems(tmp_path, {"a.yml": _workflow(_BARE), "b.yaml": unnamed})
    assert [p.split(":")[0] for p in problems] == [
        "workflows/a.yml · j · s", "workflows/b.yaml · j · #0"], problems


def test_every_workflow_and_action_on_the_tree_is_read() -> None:
    steps, unread = run_steps()
    assert not unread, unread
    files = {s.file for s in steps}
    assert any(f.startswith("actions/") for f in files), "no composite action was walked"
    with_run = {f"workflows/{p.name}" for p in WORKFLOW_DIR.iterdir()
                if p.suffix in (".yml", ".yaml") and "run: " in p.read_text(encoding="utf-8")}
    assert with_run <= files, f"`run:` text but no step returned: {sorted(with_run - files)}"


@pytest.mark.parametrize("shell, expected", [
    (None, True), ("bash", True), ("sh", True), ("bash -euo pipefail {0}", True),
    ("/usr/bin/bash {0}", True), ("pwsh", False), ("python", False), ("cmd", False),
    ("powershell", False), ("pwsh -command {0}", False), ("", False),
])
def test_which_shells_are_sh_like(shell, expected) -> None:
    assert sh_like(shell) is expected


# ── the helper's shell model (used by the tests that run a step whole) ──────

@pytest.mark.parametrize("step, job, top, expected", [
    ({}, {}, {}, None),
    ({"shell": "bash"}, {"defaults": {"run": {"shell": "sh"}}}, {}, "bash"),
    ({}, {"defaults": {"run": {"shell": "sh"}}}, {"defaults": {"run": {"shell": "pwsh"}}}, "sh"),
    ({}, {}, {"defaults": {"run": {"shell": "pwsh"}}}, "pwsh"),
    ({}, {"defaults": {"run": {"working-directory": "x"}}}, {"defaults": None}, None),
], ids=["none", "step-wins", "job-over-workflow", "workflow", "defaults-without-shell"])
def test_the_effective_shell_follows_githubs_precedence(step, job, top, expected) -> None:
    assert effective_shell(top, job, step) == expected


def test_the_modelled_flags_are_githubs() -> None:
    """https://docs.github.com/actions/reference/workflow-syntax — `shell`."""
    assert step_shell_argv(None)[1:] == ["-e"]
    assert step_shell_argv("bash")[1:] == ["--noprofile", "--norc", "-eo", "pipefail"]
    with pytest.raises(UnmodelledShell):
        step_shell_argv("sh")
