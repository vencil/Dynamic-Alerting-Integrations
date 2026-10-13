"""`.github/workflows/config-diff-advisory.yml`: advisory, and read at the base's time.

ADR-037 decision 6, step 1: config-diff on rendered output runs on the
platform's PRs as an ADVISORY report before anyone may make it a required
check. Two properties make it advisory and comparable, and this module holds
both by RUNNING the steps' own `run:` scripts under the shell the runner uses
for a step that names none (`bash -e {0}`: no pipefail), not by matching
their text:

  * **It never fails on a verdict.** config-diff's exit 1 ("a change") and
    exit 2 ("no result") leave the step green; exit 2 is a `::warning::`.
    The runner's default `-e` is what makes this non-trivial: without the
    step's own `set +e`, exit 1 ends the step on the spot.
  * **Both sides are read at the base commit's time.** The base step checks
    the merge-base out into a worktree and writes that commit's time; the run
    step passes it as `--at` to both runs, and refuses to run without it.

The base step's failures are fatal in the other direction: a missing or empty
base side would make every tenant read as added (see
`test_ci_baseline_substitution.py`), so an unresolvable ref, no merge-base or
a worktree that cannot be created each fail the step, and so does a base
that lacks the conf.d tree (config-diff reads a missing `--old-dir` as a
first import).

The last test runs the two steps end to end, with the real config-diff and a
da-guard built from this checkout, on a copy of the platform conf.d tree: a
routing-default change must exit 1, and the unchanged control must exit 0.

What this does NOT show: that the job is absent from main's required checks
(branch protection is not in the repo), or that the runner resolves
`${{ }}` the way the `env:` assertions below assume.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _platform_fs import symlink_or_skip

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/config-diff-advisory.yml"
# What the runner runs a `run:` block with when `shell:` is not set. (`shell:
# bash` would add `--noprofile --norc -o pipefail`; these steps do not set it.)
_RUNNER_BASH = [shutil.which("bash") or "bash", "-e"]
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["config-diff"], f"jobs are now {list(jobs)}; re-read this module"
    return jobs["config-diff"]


def _step(step_id: str) -> dict:
    found = [s for s in _job()["steps"] if s.get("id") == step_id]
    assert len(found) == 1, f"expected one step with id {step_id!r}, found {len(found)}"
    return found[0]


def _conf_d() -> str:
    return _job()["env"]["CONF_D"]


def _run_step(step: dict, cwd: Path, env: dict) -> tuple[subprocess.CompletedProcess, dict]:
    """Run `step`'s script as the runner would; return the process and the
    step's `$GITHUB_OUTPUT` as a dict."""
    assert "shell" not in step, "the step names a shell; model it before trusting this harness"
    runner_temp = Path(env["RUNNER_TEMP"])
    out_file = runner_temp / "github_output"
    out_file.write_text("", encoding="utf-8")
    script = runner_temp / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    proc = subprocess.run(
        [*_RUNNER_BASH, str(script)], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, check=False,
        env={**os.environ, **_GIT_ENV, "CONF_D": _conf_d(),
             "GITHUB_OUTPUT": str(out_file), **env})
    outputs = {}
    for line in out_file.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        outputs[k] = v
    return proc, outputs


# ── the run step: never fails on a verdict ───────────────────────────────

_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
if [ "${STUB_PRINT:-1}" = 1 ]; then echo "REPORT"; fi
exit "$STUB_RC"
"""


@pytest.fixture
def stub_python(tmp_path):
    """A `python3` on PATH that records its arguments and exits $STUB_RC."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "STUB_LOG": str(log),
            "RUNNER_TEMP": str(temp), "BASE_TREE": str(tmp_path / "base"),
            "DA_GUARD_BINARY": "/nonexistent/da-guard"}, log


@pytest.mark.parametrize("rc", [0, 1, 2, 3])
def test_no_config_diff_exit_code_fails_the_step(tmp_path, stub_python, rc) -> None:
    env, _ = stub_python
    proc, out = _run_step(_step("diff"), tmp_path,
                          {**env, "AT": "2026-10-01T00:00:00Z", "STUB_RC": str(rc)})
    assert proc.returncode == 0, (
        f"config-diff exit {rc} failed the advisory step (rc {proc.returncode}); "
        f"an advisory run must never turn a PR red on a verdict.\n{proc.stdout}{proc.stderr}")
    assert out.get("rc") == str(rc), f"the step reported rc {out.get('rc')!r}, config-diff exited {rc}"
    warned = "::warning::" in proc.stdout
    assert warned == (rc >= 2), (
        f"exit {rc}: expected a ::warning:: only for 'no result' (2) or an exit outside "
        f"the contract, got warned={warned}.\n{proc.stdout}")


def test_an_empty_report_is_warned(tmp_path, stub_python) -> None:
    env, _ = stub_python
    proc, _ = _run_step(_step("diff"), tmp_path,
                        {**env, "AT": "2026-10-01T00:00:00Z", "STUB_RC": "1", "STUB_PRINT": "0"})
    assert proc.returncode == 0
    assert "wrote no Markdown report" in proc.stdout, proc.stdout


def test_both_runs_get_the_base_time_as_at(tmp_path, stub_python) -> None:
    env, log = stub_python
    at = "2026-10-01T08:30:00+08:00"
    proc, _ = _run_step(_step("diff"), tmp_path, {**env, "AT": at, "STUB_RC": "0"})
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, calls
    for fmt, call in zip(("markdown", "json"), calls):
        args = call.split()
        assert args[0] == "scripts/tools/ops/config_diff.py", call
        assert args[args.index("--at") + 1] == at, call
        assert args[args.index("--format") + 1] == fmt, call
        assert args[args.index("--old-dir") + 1] == f"{env['BASE_TREE']}/{_conf_d()}", call
        assert args[args.index("--new-dir") + 1] == _conf_d(), call
    assert _step("diff")["env"]["AT"] == "${{ steps.base.outputs.at }}", (
        "the run step no longer takes --at from the base step's output")


def test_no_base_time_means_no_run(tmp_path, stub_python) -> None:
    env, log = stub_python
    proc, _ = _run_step(_step("diff"), tmp_path, {**env, "AT": "", "STUB_RC": "0"})
    assert proc.returncode != 0, "the step ran config-diff without --at"
    assert not log.exists(), "config-diff ran although there was no base time"


# ── the base step: the merge-base, its time, or a red step ───────────────

def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, **_GIT_ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True, timeout=60, env=env).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def pr_repo(tmp_path):
    """A repo whose `origin/main` is the base commit and whose HEAD is one
    PR commit on top of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, f"{_conf_d()}/marker.yaml", "side: base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base", date="2026-09-30T12:00:00+0800")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _git(repo, "checkout", "-q", "-b", "pr")
    _write(repo, f"{_conf_d()}/marker.yaml", "side: pr\n")
    _git(repo, "commit", "-q", "-am", "pr", date="2026-10-05T00:00:00+0000")
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    return repo, base, {"RUNNER_TEMP": str(temp), "BASE_REF": "main"}


def test_base_step_checks_out_the_merge_base_and_writes_its_time(pr_repo) -> None:
    repo, base, env = pr_repo
    proc, out = _run_step(_step("base"), repo, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["sha"] == base
    assert out["at"] == _git(repo, "show", "-s", "--format=%cI", base) == "2026-09-30T12:00:00+08:00"
    marker = Path(env["RUNNER_TEMP"]) / "base" / _conf_d() / "marker.yaml"
    assert marker.read_text(encoding="utf-8") == "side: base\n", "the base worktree is not the base side"


def test_base_step_fails_on_an_unresolvable_base_ref(pr_repo) -> None:
    repo, _, env = pr_repo
    proc, out = _run_step(_step("base"), repo, {**env, "BASE_REF": "no-such-branch"})
    assert proc.returncode != 0 and "at" not in out, proc.stdout


def test_base_step_fails_without_a_merge_base(pr_repo) -> None:
    repo, _, env = pr_repo
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _git(repo, "commit", "-q", "-m", "unrelated")
    proc, out = _run_step(_step("base"), repo, env)
    assert proc.returncode != 0 and "at" not in out, proc.stdout


def test_base_step_fails_when_the_worktree_cannot_be_created(pr_repo) -> None:
    repo, _, env = pr_repo
    blocker = Path(env["RUNNER_TEMP"]) / "base"
    blocker.mkdir()
    (blocker / "occupied").write_text("x", encoding="utf-8")
    proc, out = _run_step(_step("base"), repo, env)
    assert proc.returncode != 0 and "at" not in out, proc.stdout


def test_base_step_fails_when_the_base_has_no_conf_d(pr_repo) -> None:
    repo, _, env = pr_repo
    _git(repo, "checkout", "-q", "main")
    _git(repo, "rm", "-q", "-r", _conf_d())
    _git(repo, "commit", "-q", "-m", "tree moved")
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "-b", "pr2")
    _write(repo, f"{_conf_d()}/marker.yaml", "side: pr\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pr")
    proc, out = _run_step(_step("base"), repo, env)
    assert proc.returncode != 0 and "at" not in out, proc.stdout
    assert "has no" in proc.stdout, proc.stdout


# ── end to end: the real config-diff on the platform tree ───────────────

def _e2e(tmp_path: Path, da_guard: str, edit) -> tuple[subprocess.CompletedProcess, dict, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shutil.copytree(ROOT / _conf_d(), repo / _conf_d())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base", date="2026-09-30T12:00:00+0800")
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "-b", "pr")
    edit(repo / _conf_d())
    _git(repo, "commit", "-q", "--allow-empty", "-am", "pr")
    # The run step calls the tool by its repo path; the tool is this checkout's.
    symlink_or_skip(ROOT / "scripts", repo / "scripts", target_is_directory=True)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    env = {"RUNNER_TEMP": str(temp), "BASE_REF": "main"}
    proc, base_out = _run_step(_step("base"), repo, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc, out = _run_step(_step("diff"), repo, {
        **env, "AT": base_out["at"], "BASE_TREE": str(temp / "base"), "DA_GUARD_BINARY": da_guard})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc, {**base_out, **out}, temp / "config-diff"


def _change_platform_pager(conf_d: Path) -> None:
    p = conf_d / "_defaults.yaml"
    text = p.read_text(encoding="utf-8")
    assert text.count('"dba-oncall@example.com"') == 1, "the fixture's anchor moved"
    p.write_text(text.replace('"dba-oncall@example.com"', '"platform-oncall@example.com"'),
                 encoding="utf-8")


@pytest.mark.parametrize("edit, want_rc", [
    (_change_platform_pager, "1"),
    (lambda conf_d: None, "0"),  # control: nothing changed must read as nothing changed
])
def test_end_to_end_on_the_platform_tree(tmp_path, da_guard_binary, edit, want_rc) -> None:
    proc, out, reports = _e2e(tmp_path, da_guard_binary, edit)
    assert out["rc"] == want_rc, f"config-diff exited {out['rc']}, expected {want_rc}\n{proc.stdout}{proc.stderr}"
    doc = json.loads((reports / "report.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "da-tools.config-diff/v1"
    assert doc["at"] == out["at"], "the JSON report was not rendered at the base commit's time"
    assert doc["exit_code"] == int(want_rc)
    assert all(v["status"] == "computed" for v in doc["views"].values()), doc["views"]
    if want_rc == "1":
        assert doc["receiver_changes"], f"the platform pager change is not in the report: {doc}"
    assert (reports / "report.md").read_text(encoding="utf-8").startswith("# Config Diff Report")
