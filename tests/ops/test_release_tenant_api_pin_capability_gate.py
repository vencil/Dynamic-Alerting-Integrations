"""release-tenant-api refuses the image-pin capability exemptions before it publishes (#3228).

The PR gate (`image-pin-capability-check`) honours `EXEMPTIONS`: while no
`tools/v*` tag ships `generator-service` / the recipe-preview image, the
tenant-api chart's default pin is registered as incapable so PRs stay green.
That licence must not reach a RELEASE: a chart published with such a pin
crash-loops on a default install. So the release job runs the same gate,
scoped to `helm/tenant-api`, with `--refuse-exemptions`, before anything is
published.

Read as structure, not as text: the workflow is parsed as YAML and every
`run:` as shell words (`shlex`), so a commented-out command or the flag on
another script does not count. A gate that runs but whose failure does not
fail the job (`continue-on-error`, a step `if:`, `|| true`, a pipe, `set +e`,
`echo …`, `exit 0`, a heredoc) does not count either: the gate's step must
hold exactly the tag fetch and the gate. A workflow that does not parse fails the
test — there is no skip path.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RELEASE = REPO / ".github" / "workflows" / "release.yaml"
JOB = "release-tenant-api"
GATE = "scripts/tools/lint/check_image_pin_capability.py"


def _steps() -> list[dict]:
    wf = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    steps = wf["jobs"][JOB]["steps"]
    assert isinstance(steps, list) and steps, f"{JOB} has no steps"
    return steps


def _commands(step: dict) -> list[list[str]]:
    """Each shell command of a step's `run:` as argv (line continuations
    joined, comments dropped, split on ; && || |)."""
    run = step.get("run")
    if not isinstance(run, str):
        return []
    # Newlines separate commands, and shlex treats them as whitespace, so lex
    # one (continuation-joined) line at a time.
    out: list[list[str]] = []
    for line in run.replace("\\\n", " ").splitlines():
        lx = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lx.whitespace_split = True
        lx.commenters = "#"
        cmd: list[str] = []
        for token in lx:
            if set(token) <= set(";&|"):
                if cmd:
                    out.append(cmd)
                cmd = []
            else:
                cmd.append(token)
        if cmd:
            out.append(cmd)
    return out


def _indices(steps: list[dict], pred) -> list[int]:
    return [i for i, s in enumerate(steps) if any(pred(c) for c in _commands(s))]


def _gate_invocations(steps: list[dict]) -> list[tuple[int, list[str]]]:
    found = []
    for i, step in enumerate(steps):
        for cmd in _commands(step):
            if any(tok == GATE for tok in cmd):
                found.append((i, cmd[cmd.index(GATE) + 1:]))
    return found


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, tok in enumerate(args):
        if tok == flag:
            return args[i + 1] if i + 1 < len(args) else None
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def test_release_runs_the_capability_gate_once_scoped_and_refusing_exemptions() -> None:
    calls = _gate_invocations(_steps())
    assert len(calls) == 1, f"expected one {GATE} call in {JOB}, got {calls}"
    _, args = calls[0]
    assert "--refuse-exemptions" in args, args
    # argparse keeps the LAST --chart: a second one would silently re-scope it.
    charts = [t for t in args if t == "--chart" or t.startswith("--chart=")]
    assert len(charts) == 1, args
    assert _flag_value(args, "--chart") == "helm/tenant-api", args


def test_the_gate_runs_before_anything_is_published() -> None:
    """Before `helm push` (the chart) AND before the image build (a failing
    gate must not leave a pushed candidate / promoted `:latest` behind — it
    needs neither, unlike the resolve check, which needs the promoted tag)."""
    steps = _steps()
    (gate_i, _), = _gate_invocations(steps)
    # #3848: `helm push` runs inside release_publish.py chart.
    push = _indices(steps, lambda c: c[:2] == ["helm", "push"]
                    or (len(c) > 4 and c[3].endswith("release_publish.py") and c[4] == "chart"))
    assert len(push) == 1, push
    build = [i for i, s in enumerate(steps)
             if str(s.get("uses", "")).startswith("docker/build-push-action@")]
    assert len(build) == 1, build
    assert gate_i < build[0] < push[0], (gate_i, build, push)


def test_the_gate_has_what_it_needs_before_it_runs() -> None:
    """Fail-closed tool: without tags, helm or da-crdecode it exits 2 — a red
    release, but for the wrong reason. Assert each prerequisite precedes it."""
    steps = _steps()
    (gate_i, _), = _gate_invocations(steps)
    gate_j = next(j for j, c in enumerate(_commands(steps[gate_i])) if GATE in c)

    def before(pred) -> bool:
        """A matching command in an earlier step, or earlier in the gate's own
        step (a failing command there aborts the step: GitHub runs `run:`
        with `bash -e`)."""
        return any((i, j) < (gate_i, gate_j)
                   for i, step in enumerate(steps)
                   for j, c in enumerate(_commands(step)) if pred(c))

    # tools/v* tags: the gate reads each pinned tag's tree with `git show`.
    assert before(lambda c: c[:2] == ["git", "fetch"] and "--tags" in c)
    # PyYAML (the gate imports it; the runner's python has no guarantee).
    assert before(lambda c: "pip" in c and "install" in c and "pyyaml" in c
                  and "requirements/ci-constraints.txt" in c)
    uses = [(i, str(s.get("uses", ""))) for i, s in enumerate(steps)]
    assert any(i < gate_i and u.startswith("azure/setup-helm@") for i, u in uses)
    assert any(i < gate_i and u.startswith("actions/setup-go@") for i, u in uses)
    assert any(i < gate_i and u.startswith("actions/setup-python@") for i, u in uses)
    # da-crdecode: the gate's manifest / render reads go through it.
    gate_cmd = _commands(steps[gate_i])[gate_j]
    assert "scripts/ops/with_da_crdecode.sh" in gate_cmd[:gate_cmd.index(GATE)], gate_cmd


def test_a_failing_gate_fails_the_job() -> None:
    """The gate's non-zero exit must stop the release. GitHub runs `run:` with
    `bash -e` and no pipefail: anything after the gate on its line (`|| true`,
    `| tee log`) or a `set +e` before it swallows the exit code."""
    wf = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    job = wf["jobs"][JOB]
    assert not job.get("continue-on-error"), "the job must not continue on error"
    steps = job["steps"]
    (gate_i, _), = _gate_invocations(steps)
    step = steps[gate_i]
    for key in ("continue-on-error", "if", "shell"):
        assert key not in step, f"gate step sets {key!r}: {step.get(key)!r}"
    run = step["run"]
    # The step is exactly the tag fetch and the gate: nothing that could end
    # the step early (`exit 0`), skip the gate (`if false; then`), turn off
    # errexit (`set +e`, `set +o errexit`), or make the gate mere text (a
    # heredoc) fits in it.
    cmds = _commands(step)
    assert len(cmds) == 2, cmds
    assert cmds[0] == ["git", "fetch", "--quiet", "--tags", "origin"], cmds[0]
    assert GATE in cmds[1], cmds[1]
    lines = [ln for ln in run.replace("\\\n", " ").splitlines() if GATE in ln]
    assert len(lines) == 1, lines
    lx = shlex.shlex(lines[0], posix=True, punctuation_chars=";&|")
    lx.whitespace_split = True
    lx.commenters = "#"
    tokens = list(lx)
    assert not any(set(t) <= set(";&|") for t in tokens), (
        f"the gate shares its line with another command: {lines[0]!r}")
    # The wrapper runs the gate itself: not `echo …`, not another program.
    assert tokens[:3] == ["bash", "scripts/ops/with_da_crdecode.sh", "python3"], tokens
