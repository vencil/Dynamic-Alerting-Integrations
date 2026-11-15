"""No workflow step reads a `$?` that can only ever be 0 (#3946).

The defect: a `run:` step with no `shell:` runs as `bash -e {0}` (and
`shell: bash` as `bash --noprofile --norc -eo pipefail {0}`), so

    some-command > out.txt 2>&1
    rc=$?

never gets to `rc=$?` when the command fails — the step ends on the command's
line, and everything written after it to report the failure (the log, the
summary, the step output, the `::error::`) is skipped on exactly the runs
that had something to report. `.github/workflows/required-checks-drift.yml`
had this shape; `.github/workflows/main-red-detector.yml` had it until #3944.

HOW IT IS DECIDED — by executing, not by matching text. For every `run:` step
whose text mentions `$?`, `tests/_workflow_steps.py::probe_status_reads` runs
the whole script under the step's own shell flags with every outside effect
removed (commands are skipped, which bash counts as success), then again once
per command with THAT command made to fail in place. bash itself decides what
errexit does with the failure. A command that mentions `$?` is

  * `live` — reached at least once with a non-zero `$?`;
  * `dead` — reached, the command before it was made to fail, and it still
    only ever read 0. This is the defect.
  * `undetermined` — reached with 0 only and nothing before it could be made
    to fail. Reported as a failure: nothing was concluded.

and a script line holding `$?` that no executed command accounts for is
`unseen` — also a failure. So what the walk cannot reach is red, not green.

WHAT IT SEES (`_VERDICTS` below, one row each): a `$?` in an assignment,
`exit`, `echo`, `[ … ]`, `case`, `local`; after a simple command, a command
substitution, a subshell, a pipeline, a function call; in a function body;
after `set +e` … `set -e`; the `!`-negated form that reads the negation and
not the command; and the forms that are fine (`|| rc=$?`, an `else` branch,
`set +e`, `&& … ||`).

WHAT IT DOES NOT SEE (`_BLIND` below — each row is pinned, so the day one of
them becomes visible the test says to move it):

  * a read that is live but reads the WRONG command: `cmd | tee log; rc=$?`
    with errexit off and no pipefail reads `tee`'s status. That is another
    defect (see the docstring of scripts/tools/lint/check_pipestatus_errexit.py
    for its `PIPESTATUS` sibling) and is not this guard's;
  * a read that is live on one path and dead on the path that matters (a
    function called once under `set +e` and once without);
  * `${PIPESTATUS[…]}` — not a `$?`; it has its own lint (TRK-382).

WHAT IS REFUSED rather than judged: a step that mentions `$?` under any
`shell:` other than none/`bash` (`sh`, `pwsh`, `python`, a custom template) —
its flags are not modelled; a `$?` inside a heredoc body or a single-quoted
string handed to `eval`/`bash -c`/`ssh`, or in a `trap '…' EXIT` (the line
is `unseen`); a read after a command that cannot fail or one the probe will
not run (an absolute path that exists); a workflow that does not parse; a
`$?` in a composite action under `.github/actions/`; and every step, on a
machine where the probe cannot confine its writes (it then fails on CI and
skips elsewhere — see `_confinement`).

OUT OF SCOPE: scripts a step calls (`bash scripts/…sh`) — they are files with
their own flags, not `run:` text.

There is no exemption table: nothing on the tree needs one. If a read is
ever flagged that is right as written, rewrite it as `|| rc=$?` first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from _workflow_steps import (
    REPO_ROOT,
    WORKFLOW_DIR,
    UnmodelledShell,
    effective_shell,
    probe_status_reads,
    run_steps,
    step_shell_argv,
)

ACTIONS_DIR = REPO_ROOT / ".github/actions"


def status_read_problems(workflow_dir: Path, tmp_path: Path) -> tuple[list[str], int, int]:
    """Every reason the tree is not known clean, the number of steps probed
    and the number of `$?` commands judged."""
    steps, unread = run_steps(workflow_dir)
    problems = [f"{entry} — not read as a workflow, so its steps were not checked"
                for entry in unread]
    probed = judged = 0
    for number, step in enumerate(steps):
        if "$?" not in step.run:
            continue
        try:
            probe = probe_status_reads(step.run, step.shell, tmp_path / str(number))
        except UnmodelledShell as err:
            problems.append(f"{step.label}: mentions `$?` under {err}")
            continue
        probed += 1
        judged += len(probe.sites)
        if probe.cut:
            problems.append(f"{step.label}: the walk was cut short ({probe.runs} runs) — "
                            "nothing is concluded about this step")
        for line in probe.unseen:
            text = step.run.splitlines()[line - 1].strip()
            problems.append(f"{step.label}: line {line} `{text}` holds `$?` and was never "
                            "reached as a command — not judged")
        for site in probe.sites:
            if site.verdict == "dead":
                problems.append(
                    f"{step.label}: line {site.line} `{site.command}` can only read 0 — when "
                    f"`{min(site.after_failed).partition(':')[2]}` fails, the read is "
                    "not reached with its status (errexit ends the step there, or the "
                    "status is replaced first). Write `rc=0` then `command || rc=$?`.")
            elif site.verdict == "undetermined":
                problems.append(
                    f"{step.label}: line {site.line} `{site.command}` was only reached with "
                    "0 and no command before it could be made to fail — not judged")
    return problems, probed, judged


# ── the tree ────────────────────────────────────────────────────────────────

def test_no_workflow_step_reads_a_status_that_is_always_zero(tmp_path) -> None:
    problems, probed, judged = status_read_problems(WORKFLOW_DIR, tmp_path)
    assert not problems, "\n  ".join([f"{len(problems)} problem(s):"] + problems)
    # Not a count to keep in step with the tree: only "the population was not
    # emptied" — a walk over nothing is green too.
    assert probed >= 5 and judged >= probed - 2, (probed, judged)


def test_the_two_steps_this_guard_exists_for_are_in_the_population(tmp_path) -> None:
    """Both were the defect; if either stops being probed (renamed away, `$?`
    gone) the guard may be looking at nothing that matters."""
    steps, _ = run_steps(WORKFLOW_DIR)
    for file, needle in [("required-checks-drift.yml", "required_checks_drift.py"),
                         ("main-red-detector.yml", "main_red_report.py")]:
        found = [s for s in steps if s.file == file and needle in s.run and "$?" in s.run]
        assert len(found) == 1, (file, [s.label for s in found])
        probe = probe_status_reads(found[0].run, found[0].shell, tmp_path / file)
        assert [s.verdict for s in probe.sites] == ["live"], (file, probe)
        assert not probe.unseen and not probe.cut


def test_no_composite_action_mentions_a_status_read() -> None:
    """Composite actions are not walked. None holds a `$?` today; the first
    that does has to be taught to `run_steps` (its steps live under
    `runs.steps` and each must name its `shell:`)."""
    holders = [str(p.relative_to(REPO_ROOT)) for p in sorted(ACTIONS_DIR.rglob("action.y*ml"))
               if "$?" in p.read_text(encoding="utf-8")]
    assert not holders, f"`$?` in composite actions, which this guard does not walk: {holders}"


# ── what the probe sees ─────────────────────────────────────────────────────

_PRE_FIX_DRIFT = """\
set -uo pipefail
if ! gh api "repos/${GITHUB_REPOSITORY}/branches/main" > "$RUNNER_TEMP/branch.json"; then
  echo "rc=2" >> "$GITHUB_OUTPUT"
  exit 2
fi
python3 -X utf8 scripts/ops/required_checks_drift.py "$RUNNER_TEMP/branch.json" \\
  > "$RUNNER_TEMP/drift.txt" 2>&1
rc=$?
cat "$RUNNER_TEMP/drift.txt"
exit "$rc"
"""

# (id, script, shell, {command text → verdict})
_VERDICTS = [
    ("bare-assignment", "cmd a\nrc=$?\n", None, {"rc=$?": "dead"}),
    ("the-drift-step-before-3946", _PRE_FIX_DRIFT, None, {"rc=$?": "dead"}),
    ("shell-bash", "cmd a\nrc=$?\n", "bash", {"rc=$?": "dead"}),
    ("set-uo-pipefail-is-not-set-plus-e", "set -uo pipefail\ncmd a\nrc=$?\n", None,
     {"rc=$?": "dead"}),
    ("exit", "cmd a\nexit $?\n", None, {"exit $?": "dead"}),
    ("echo", 'cmd a\necho "rc is $?"\n', None, {'echo "rc is $?"': "dead"}),
    ("test", "cmd a\nif [ $? -ne 0 ]; then echo no; fi\n", None, {"[ $? -ne 0 ]": "dead"}),
    ("case", "cmd a\ncase $? in 0) echo ok ;; *) echo no ;; esac\n", None,
     {"case $? in ": "dead"}),
    ("after-a-command-substitution", "out=$(cmd a)\nrc=$?\n", None, {"rc=$?": "dead"}),
    ("after-a-subshell", "( cmd a; cmd b )\nrc=$?\n", None, {"rc=$?": "dead"}),
    ("after-a-pipeline", "cmd a | tee log\nrc=$?\n", None, {"rc=$?": "dead"}),
    ("after-a-pipeline-with-pipefail", "cmd a | tee log\nrc=$?\n", "bash", {"rc=$?": "dead"}),
    ("after-a-function-call", "f() { cmd a; }\nf\nrc=$?\n", None, {"rc=$?": "dead"}),
    ("in-a-function", "f() {\n  cmd a\n  local rc=$?\n  echo $rc\n}\nf\n", None,
     {"local rc=$?": "dead"}),
    ("a-path-not-a-name", '"$RUNNER_TEMP/tool" --x\nrc=$?\n', None, {"rc=$?": "dead"}),
    ("after-errexit-is-turned-back-on", "set +e\ncmd a\nset -e\ncmd b\nrc=$?\n", None,
     {"rc=$?": "dead"}),
    ("local-masks-the-substitution", "f() {\n  set +e\n  local out=$(cmd a)\n  rc=$?\n}\nf\n",
     None, {"rc=$?": "dead"}),
    ("negation-reads-the-negation", "if ! cmd a; then\n  rc=$?\n  echo $rc\nfi\n", None,
     {"rc=$?": "dead"}),
    ("in-a-loop-over-command-output", "for f in $(ls); do\n  cmd \"$f\"\n  rc=$?\ndone\n", None,
     {}),  # see _REFUSED: the loop is not entered, so the line is unseen
    # ── fine as written ──
    ("or-assignment", "rc=0\ncmd a || rc=$?\necho $rc\n", None, {"rc=$?": "live"}),
    ("or-assignment-continued", "rc=0\ncmd a \\\n  > out 2>&1 || rc=$?\n", None,
     {"rc=$?": "live"}),
    ("or-assignment-in-a-substitution", "rc=0\nout=$(cmd a \\\n  --x) || rc=$?\n", None,
     {"rc=$?": "live"}),
    ("else-branch", "if cmd a; then\n  echo ok\nelse\n  rc=$?\n  echo \"failed $rc\"\nfi\n",
     None, {"rc=$?": "live"}),
    ("set-plus-e", "set +e\ncmd a\nrc=$?\nset -e\n", None, {"rc=$?": "live"}),
    ("set-plus-o-errexit", "set +o errexit\ncmd a\nrc=$?\n", None, {"rc=$?": "live"}),
    ("and-or-chain", "cmd a && echo ok || echo \"failed $?\"\n", None,
     {'echo "failed $?"': "live"}),
    ("or-exit", "cmd a || exit $?\n", None, {"exit $?": "live"}),
    ("after-an-early-exit", "if [ -z \"$TOKEN\" ]; then\n  exit 1\nfi\nrc=0\ncmd a || rc=$?\n",
     None, {"rc=$?": "live"}),
    # `exit` is walked past: no test or stand-in value gets around this one.
    ("after-an-exit-in-a-case",
     "case \"$RUN_ID\" in\n  ''|*[!0-9]*) echo bad; exit 2 ;;\nesac\nrc=0\ncmd a || rc=$?\n",
     None, {"rc=$?": "live"}),
    ("in-a-loop-over-a-variable", "list=$(ls)\nfor f in $list; do\n  cmd \"$f\" || rc=$?\ndone\n",
     None, {"rc=$?": "live"}),
    ("a-comment-line-is-not-a-read", "# never a bare rc=$? here\nrc=0\ncmd a || rc=$?\n", None,
     {"rc=$?": "live"}),
    ("single-quoted-is-not-a-read", "cmd a\necho 'literally $?'\n", None, {}),
    ("expression-placeholders", "rc=0\ncmd ${{ inputs.x }} || rc=$?\n", None, {"rc=$?": "live"}),
]

# Rows of _VERDICTS whose `$?` line is never reached: reported as `unseen`.
_REFUSED = {"in-a-loop-over-command-output": [3], "single-quoted-is-not-a-read": [2]}


@pytest.mark.parametrize("script, shell, expected", [row[1:] for row in _VERDICTS],
                         ids=[row[0] for row in _VERDICTS])
def test_what_the_probe_concludes(tmp_path, request, script, shell, expected) -> None:
    probe = probe_status_reads(script, shell, tmp_path)
    assert {s.command: s.verdict for s in probe.sites} == expected, probe
    name = request.node.callspec.id
    assert probe.unseen == _REFUSED.get(name, []), probe
    assert not probe.cut


# ── what it does not see ────────────────────────────────────────────────────

_BLIND = [
    # The read is reached with tee's failure, so it is live — but it can never
    # hold cmd's status, which is what the author wanted.
    ("reads-the-last-stage-of-a-pipeline", "set +e\ncmd a | tee log\nrc=$?\n", {"rc=$?": "live"}),
    # Live through the first call; dead through the second.
    ("live-on-one-path-only", "f() {\n  cmd a\n  rc=$?\n}\nset +e\nf\nset -e\nf\n",
     {"rc=$?": "live"}),
    # Not a `$?` at all.
    ("pipestatus", "cmd a | tee log\nrc=${PIPESTATUS[0]}\n", {}),
]


@pytest.mark.parametrize("script, verdicts", [row[1:] for row in _BLIND],
                         ids=[row[0] for row in _BLIND])
def test_shapes_the_probe_passes_today(tmp_path, script, verdicts) -> None:
    """⛔ These are defects (or may be) that this guard reports NOTHING for.
    A row that starts failing means the probe has learned to see it: move it
    to `_VERDICTS` with its new verdict and take it out of the module
    docstring's "does not see" list."""
    probe = probe_status_reads(script, None, tmp_path)
    assert {s.command: s.verdict for s in probe.sites} == verdicts, probe
    assert not probe.unseen and not probe.cut


# ── what is refused rather than judged ──────────────────────────────────────

@pytest.mark.parametrize("script, unseen, undetermined", [
    # The read is judged (the heredoc is part of the command, on line 2), but
    # the line the `$?` is written on is not where a command starts.
    ("cmd a\ncat <<EOF\nstatus was $?\nEOF\n", [3], []),
    ("ssh host 'cmd a; echo $?'\n", [1], []),
    ("bash -c 'cmd a\nrc=$?'\n", [2], []),
    ("cmd a  # then $? is read\n", [1], []),
    ("trap 'rc=$?; echo \"exit $rc\"' EXIT\ncmd a\n", [1], []),
    ("/bin/sh -c true\nrc=$?\n", [], ["rc=$?"]),
    ("true\nrc=$?\n", [], ["rc=$?"]),
    ("rc=$?\n", [], ["rc=$?"]),
], ids=["heredoc", "quoted-for-another-shell", "quoted-script", "trailing-comment",
        "exit-trap", "a-real-path-is-never-run", "after-what-cannot-fail", "nothing-before-it"])
def test_what_cannot_be_judged_is_reported_not_passed(tmp_path, script, unseen,
                                                      undetermined) -> None:
    """Each of these comes out as a problem in `status_read_problems`: an
    unseen line or an undetermined read — never as nothing."""
    probe = probe_status_reads(script, None, tmp_path)
    assert probe.unseen == unseen, probe
    assert [s.command for s in probe.sites if s.verdict == "undetermined"] == undetermined, probe
    assert unseen or undetermined


def test_a_script_that_never_ends_is_cut_and_reported(tmp_path, monkeypatch) -> None:
    import _workflow_steps
    monkeypatch.setattr(_workflow_steps, "MAX_COMMANDS", 200)
    probe = probe_status_reads("while true; do\n  cmd a\ndone\nrc=$?\n", None, tmp_path)
    assert probe.cut and probe.unseen == [4], probe


def test_the_run_limit_is_reported(tmp_path, monkeypatch) -> None:
    import _workflow_steps
    monkeypatch.setattr(_workflow_steps, "MAX_RUNS", 2)
    probe = probe_status_reads("cmd a\ncmd b\ncmd c\nrc=$?\n", None, tmp_path)
    assert probe.cut, probe


def test_the_probe_runs_nothing_and_writes_nowhere(tmp_path) -> None:
    """Commands are skipped before bash expands or redirects them: a step's
    `rm`, `curl` and `> file` must not happen on the machine running the test."""
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    made = tmp_path / "made"
    script = (f'rm -f "{victim}"\n/bin/rm -f "{victim}"\necho x > "{made}"\n'
              f'tool --x > "{victim}"\nout=$(/bin/rm -f "{victim}")\n'
              f'{{ echo a; echo b; }} > "{victim}"\ncmd a || rc=$?\n')
    probe = probe_status_reads(script, None, tmp_path / "work")
    assert victim.read_text(encoding="utf-8") == "keep" and not made.exists()
    # The group's redirection is refused, the group fails, errexit ends the
    # script: what follows a write to a literal outside path is not walked —
    # and is reported (line 7 is unseen), not passed.
    assert probe.sites == [] and probe.unseen == [7], probe


def test_inside_its_own_directory_the_script_can_write(tmp_path) -> None:
    """…so `>> "$GITHUB_OUTPUT"` and a `{ …; } >> "$GITHUB_STEP_SUMMARY"` group
    do not fail for a reason a runner would not have, and what follows them
    is still walked."""
    script = ('echo "a=b" >> "$GITHUB_OUTPUT"\n{ echo x; } >> "$GITHUB_STEP_SUMMARY"\n'
              '{ echo y; } > "$RUNNER_TEMP/report.txt"\ncmd a\nrc=$?\n')
    probe = probe_status_reads(script, None, tmp_path)
    assert [(s.line, s.verdict) for s in probe.sites] == [(5, "dead")], probe


@pytest.mark.parametrize("on_ci, outcome", [("true", "Failed"), ("", "Skipped")])
def test_without_confinement_the_probe_does_not_run(tmp_path, monkeypatch, on_ci, outcome) -> None:
    """Not run unconfined (it would write wherever the script redirects), and
    on CI not skipped either: a guard that could not look must be red there."""
    import _workflow_steps

    def unavailable():
        raise _workflow_steps.NoConfinement("stand-in: no Landlock")
    monkeypatch.setattr(_workflow_steps, "_confinement", unavailable)
    monkeypatch.setenv("CI", on_ci)
    with pytest.raises(BaseException) as caught:
        probe_status_reads("cmd a\nrc=$?\n", None, tmp_path)
    assert type(caught.value).__name__ == outcome, caught.value
    assert not (tmp_path / "step.sh").exists(), "nothing may be prepared, let alone run"


# ── shells, defaults, and files that do not parse ───────────────────────────

_DEAD = "cmd a\n          rc=$?\n"
_LIVE = "cmd a || rc=$?\n"


def _workflow(tmp_path: Path, text: str, name: str = "w.yml") -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def _one_step(run: str, *, step_shell="", job_default="", top_default="") -> str:
    def default(value: str, indent: str) -> str:
        return f"{indent}defaults:\n{indent}  run:\n{indent}    shell: {value}\n" if value else ""
    return (f"on: push\n{default(top_default, '')}jobs:\n  j:\n{default(job_default, '    ')}"
            "    runs-on: ubuntu-24.04\n    steps:\n      - name: s\n"
            + (f"        shell: {step_shell}\n" if step_shell else "")
            + f"        run: |\n          {run}")


@pytest.mark.parametrize("kwargs", [
    {}, {"step_shell": "bash"}, {"job_default": "bash"}, {"top_default": "bash"},
], ids=["no-shell", "step-bash", "job-default-bash", "workflow-default-bash"])
def test_a_dead_read_is_reported_under_every_modelled_shell(tmp_path, kwargs) -> None:
    problems, probed, _ = status_read_problems(
        _workflow(tmp_path, _one_step(_DEAD, **kwargs)), tmp_path / "work")
    assert probed == 1 and len(problems) == 1 and "can only read 0" in problems[0], problems
    problems, probed, _ = status_read_problems(
        _workflow(tmp_path, _one_step(_LIVE, **kwargs)), tmp_path / "work2")
    assert (problems, probed) == ([], 1)


@pytest.mark.parametrize("kwargs", [
    {"step_shell": "sh"}, {"step_shell": "pwsh"}, {"step_shell": "python"},
    {"step_shell": "'bash -euo pipefail {0}'"}, {"job_default": "sh"}, {"top_default": "pwsh"},
    {"step_shell": "sh", "job_default": "bash"},
], ids=["sh", "pwsh", "python", "custom-template", "job-default-sh", "workflow-default-pwsh",
        "step-overrides-job"])
@pytest.mark.parametrize("run", [_DEAD, _LIVE], ids=["dead", "live"])
def test_a_status_read_under_an_unmodelled_shell_is_refused(tmp_path, kwargs, run) -> None:
    """Not judged and not passed: `$?` means something else in pwsh, `sh` has
    no DEBUG trap to walk with, and a custom template sets its own flags."""
    problems, probed, _ = status_read_problems(
        _workflow(tmp_path, _one_step(run, **kwargs)), tmp_path / "work")
    assert probed == 0 and len(problems) == 1 and "unmodelled `shell:" in problems[0], problems


@pytest.mark.parametrize("run, needle", [
    ("cat <<EOF\n          status $?\n          EOF\n", "never reached as a command"),
    ("true\n          rc=$?\n", "no command before it could be made to fail"),
    ("for f in $(ls); do\n            cmd $f || rc=$?\n          done\n", "never reached"),
    ("while true; do\n            cmd a\n          done\n          cmd b || rc=$?\n",
     "cut short"),
], ids=["unseen-heredoc", "undetermined", "unseen-loop", "cut"])
def test_every_way_of_not_judging_is_a_problem(tmp_path, monkeypatch, run, needle) -> None:
    import _workflow_steps
    monkeypatch.setattr(_workflow_steps, "MAX_COMMANDS", 300)
    problems, probed, _ = status_read_problems(
        _workflow(tmp_path, _one_step(run)), tmp_path / "work")
    assert probed == 1 and problems, problems
    assert any(needle in problem for problem in problems), problems


def test_an_unmodelled_shell_without_a_status_read_is_not_this_guards_business(tmp_path) -> None:
    problems, probed, _ = status_read_problems(
        _workflow(tmp_path, _one_step("Write-Host hi\n", step_shell="pwsh")), tmp_path / "work")
    assert (problems, probed) == ([], 0)


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


@pytest.mark.parametrize("text", [
    "on: [\n", "", "- a\n- b\n", "on: push\n", "on: push\njobs: []\n",
    "on: push\njobs:\n  j:\n    steps:\n      - run: [a, b]\n",
    "on: push\njobs:\n  j: nope\n",
], ids=["not-yaml", "empty", "a-list", "no-jobs", "jobs-is-a-list", "run-is-a-list",
        "job-is-a-string"])
def test_a_workflow_that_cannot_be_read_is_a_problem_not_a_skip(tmp_path, text) -> None:
    directory = _workflow(tmp_path, _one_step(_LIVE), "good.yaml")
    _workflow(tmp_path, text, "bad.yml")
    problems, probed, _ = status_read_problems(directory, tmp_path / "work")
    assert probed == 1, "the readable file is still checked"
    assert len(problems) == 1 and problems[0].startswith("bad.yml: "), problems


def test_both_extensions_are_walked(tmp_path) -> None:
    directory = _workflow(tmp_path, _one_step(_DEAD), "a.yml")
    _workflow(tmp_path, _one_step(_DEAD), "b.yaml")
    problems, probed, _ = status_read_problems(directory, tmp_path / "work")
    assert probed == 2 and sorted(p.split(" ")[0] for p in problems) == ["a.yml", "b.yaml"]


def test_every_workflow_on_the_tree_is_read() -> None:
    steps, unread = run_steps(WORKFLOW_DIR)
    assert not unread, unread
    on_disk = {p.name for p in WORKFLOW_DIR.iterdir() if p.suffix in (".yml", ".yaml")}
    with_run = {s.file for s in steps}
    without = {name for name in on_disk - with_run
               if any("run" in step for job in yaml.safe_load(
                   (WORKFLOW_DIR / name).read_text(encoding="utf-8"))["jobs"].values()
                   for step in job.get("steps") or [])}
    assert not without, f"workflows with `run:` steps that the walk returned nothing for: {without}"
