"""Execute the GitLab blast-radius job that `da-tools init --ci gitlab` emits.

issue 1444 brought this job back (it was removed under #1358 option C because
the da-tools image had no `git`). The text is pinned line by line in
`test_generated_ci_artifacts.py` (`_EXPECTED_GL_GENERATE`); this module runs
it, because a pin cannot tell a script that reads correctly from one that
works — #1358 itself shipped a baseline step whose every line looked right and
whose healthy path produced an empty baseline with exit 0.

How the job is stood in for:

* GitLab concatenates every `script:` entry into ONE shell script and runs it
  inside `$DA_TOOLS_IMAGE`. That image is python:alpine, whose `sh` is
  busybox; here the host's `sh -e` plays it (POSIX sh, no bash-isms).
* `da-tools` is an executable ahead of PATH: `generate-routes` is a no-op
  (this module is about the baseline and the diff), and `config-diff` runs the
  repository's real `config_diff.py`, so the report is computed from the
  baseline the script actually extracted — a wrong baseline shows up as a
  wrong report, not as a passing stub.
* The predefined variables the job reads (`CI_MERGE_REQUEST_DIFF_BASE_SHA`,
  `CI_PROJECT_DIR`) are supplied here; `CONFIG_DIR` and `DA_TOOLS_IMAGE` are
  read from the generated pipeline's own `variables:`.

⚠️ Not covered here, and not measurable on this host: a real GitLab runner
(clone ownership, `GIT_DEPTH` honoured, the MR pipeline existing at all).
`GIT_DEPTH` is pinned as text in `test_generated_ci_artifacts.py`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TESTS_DIR.parent

# ⛔ One literal path string each, so scripts/tools/dx/verify_diff.py maps a
# change in either file to this module (see test_generated_ci_artifacts.py).
_INIT_PROJECT = _REPO_ROOT / "scripts/tools/ops/init_project.py"
_CONFIG_DIFF = _REPO_ROOT / "scripts/tools/ops/config_diff.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_TESTS_DIR / "ops"))

import init_project as ip  # noqa: E402
from test_generated_ci_artifacts import (  # noqa: E402
    _GL_BLAST_RADIUS_JOB,
    _GL_PIPELINE,
    _git,
    _synthetic_repo,
)

_SH = shutil.which("sh")
_needs_tools = pytest.mark.skipif(
    _SH is None or shutil.which("git") is None,
    reason="executing the generated job needs `sh` and `git` on PATH",
)


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> dict:
    """The generated GitLab pipeline, through the real write path."""
    target = tmp_path_factory.mktemp("init-gitlab")
    ip.run_init(
        {
            "ci": "gitlab",
            "deploy": "kustomize",
            "rule_packs": ["mariadb"],
            "tenants": ["db-a"],
            "namespace": "monitoring",
            "da_tools_image": ip.DA_TOOLS_IMAGE,
        },
        str(target),
    )
    return yaml.safe_load((target / _GL_PIPELINE).read_text(encoding="utf-8"))


def _job_script(pipeline: dict) -> str:
    job = pipeline[_GL_BLAST_RADIUS_JOB]
    # What GitLab does with a list-valued `script:`: one shell, entries in
    # order, so a variable set in one entry is visible in the next.
    return "\n".join(str(line) for line in job["script"]) + "\n"


def _install_da_tools(bindir: Path, work: Path,
                      diff_stub: tuple[int, str] | None) -> None:
    """Put a `da-tools` executable on PATH, as the image does.

    `diff_stub=None` runs the real config_diff.py; `(rc, payload)` replaces
    config-diff with a canned exit code and stdout, for the contract paths the
    real tool will not produce on demand (rc 2, an empty report). An
    unexpected subcommand exits 99 so a script that grows a new call fails
    here instead of passing through a stub that ignores it.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    if diff_stub is None:
        diff = f'exec "{sys.executable}" "{_CONFIG_DIFF.as_posix()}" "$@"'
    else:
        (work / "_stub_stdout").write_text(diff_stub[1], encoding="utf-8",
                                           newline="\n")
        diff = f'cat "{(work / "_stub_stdout").as_posix()}"; exit {diff_stub[0]}'
    shim = bindir / "da-tools"
    shim.write_text(
        "#!/bin/sh\n"
        "sub=$1; shift\n"
        "case $sub in\n"
        "  generate-routes) exit 0 ;;\n"
        f"  config-diff) {diff} ;;\n"
        '  *) echo "stub: unexpected da-tools $sub" >&2; exit 99 ;;\n'
        "esac\n",
        encoding="utf-8", newline="\n",
    )
    shim.chmod(0o755)


def _run_job(pipeline: dict, work: Path, *, base_sha: str | None,
             diff_stub: tuple[int, str] | None = None,
             path: str | None = None) -> subprocess.CompletedProcess:
    variables = {k: str(v) for k, v in (pipeline.get("variables") or {}).items()}
    bindir = work.parent / f"{work.name}-bin"
    _install_da_tools(bindir, work, diff_stub)
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": (f"{bindir}{os.pathsep}{os.environ['PATH']}"
                 if path is None else path),
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "CI_PROJECT_DIR": str(work),
        "CONFIG_DIR": variables["CONFIG_DIR"],
        "DA_TOOLS_IMAGE": variables["DA_TOOLS_IMAGE"],
    }
    if base_sha is not None:
        env["CI_MERGE_REQUEST_DIFF_BASE_SHA"] = base_sha
    (work / "_job.sh").write_text(_job_script(pipeline), encoding="utf-8",
                                  newline="\n")
    return subprocess.run(
        [_SH, "-e", "_job.sh"], cwd=work, env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _baseline(work: Path) -> list[str]:
    base = work / ".output" / "base" / "conf.d"
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


@_needs_tools
@pytest.mark.parametrize(
    "state,base_has_config,base_exists,export_ignore,want_rc,want_files,want_text", [
        # ⚠️ `_synthetic_repo` writes the SAME tenant key into both files, so
        # the loaded config does not change between base and head and the
        # report's content says nothing here; the report is checked for
        # content in `..._report_compares_base_with_head` below.
        ("normal", True, True, None, 0, ["db-a.yaml", "db-b.yaml"],
         "Config Diff Report"),
        # No conf.d at the base: every tenant is new, and that is the answer.
        ("first_import", False, True, None, 0, [], "db-a"),
        # The object is not in the store — what a shallow clone looks like.
        ("base_not_in_clone", True, False, None, 1, [], "is not in this clone"),
        # IMMUNITY, not detection: `export-ignore` only affects `git archive`,
        # which this job does not use.
        ("export_ignored_all", True, True, "all", 0,
         ["db-a.yaml", "db-b.yaml"], "Config Diff Report"),
        ("config_dir_is_a_submodule", True, True, None, 1, [], "is a commit"),
    ])
def test_gitlab_blast_radius_tells_a_fault_from_a_first_import(
    pipeline, tmp_path, state, base_has_config, base_exists, export_ignore,
    want_rc, want_files, want_text,
) -> None:
    work = tmp_path / state
    real_base = _synthetic_repo(
        work, base_has_config=base_has_config, export_ignore=export_ignore,
        submodule=(state == "config_dir_is_a_submodule"))
    base_sha = real_base if base_exists else "0" * 40
    if state == "config_dir_is_a_submodule":
        # A real checkout leaves an empty directory where a gitlink is
        # (submodules are not cloned unless GIT_SUBMODULE_STRATEGY says so);
        # the fixture's worktree has none, which would stop the job at the
        # head-side check before it reaches the base-side lookup under test.
        (work / "conf.d").mkdir(exist_ok=True)

    p = _run_job(pipeline, work, base_sha=base_sha)

    assert p.returncode == want_rc, (
        f"{state}: rc={p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    assert _baseline(work) == want_files, (state, _baseline(work))
    assert want_text in (p.stdout + p.stderr), (
        f"{state}: expected {want_text!r} in the output\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    report = work / ".output" / "blast-radius.md"
    if want_rc == 0:
        # The report is the job's product and it is printed to the log too.
        assert report.is_file() and report.stat().st_size > 0, state
        assert report.read_text(encoding="utf-8").strip() in p.stdout
    else:
        assert not report.exists(), (
            f"{state}: a failed run left a report behind: "
            f"{report.read_text(encoding='utf-8')!r}"
        )


@_needs_tools
def test_gitlab_blast_radius_baseline_carries_the_base_content(
    pipeline, tmp_path,
) -> None:
    """The baseline must be the BASE commit, not the head: a filename check
    would pass just as happily if the job extracted HEAD."""
    work = tmp_path / "content"
    base_sha = _synthetic_repo(work, base_has_config=True)
    p = _run_job(pipeline, work, base_sha=base_sha)
    assert p.returncode == 0, p.stderr
    text = (work / ".output" / "base" / "conf.d" / "db-a.yaml").read_text(
        encoding="utf-8")
    assert '"70"' in text and '"90"' not in text, text


@_needs_tools
def test_gitlab_blast_radius_report_compares_base_with_head(
    pipeline, tmp_path,
) -> None:
    """End to end through the real config_diff.py: the report names the
    value at the base commit and the value at the head, so it was computed
    from the extracted baseline and not from an empty or head-side one."""
    work = tmp_path / "report"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "test")
    conf = work / "conf.d"
    conf.mkdir()
    tenant = conf / "tenant-x.yaml"
    tenant.write_text('tenants:\n  tenant-x:\n    container_cpu: "70"\n',
                      encoding="utf-8", newline="\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    base_sha = _git(work, "rev-parse", "HEAD")
    tenant.write_text('tenants:\n  tenant-x:\n    container_cpu: "90"\n',
                      encoding="utf-8", newline="\n")
    _git(work, "commit", "-qam", "head")

    p = _run_job(pipeline, work, base_sha=base_sha)

    assert p.returncode == 0, (p.stdout, p.stderr)
    report = (work / ".output" / "blast-radius.md").read_text(encoding="utf-8")
    assert "| container_cpu | 70 | 90 |" in report, report


@_needs_tools
@pytest.mark.parametrize("diff_stub,want_rc,want_err", [
    # 1 is config-diff's ORDINARY "changes found" answer, not a failure.
    ((1, "# Config Diff Report\n\nchanged\n"), 0, None),
    ((0, "# Config Diff Report\n\nno changes\n"), 0, None),
    # 2 and above: the run did not complete — fail with the tool's code.
    ((2, ""), 2, "config-diff exited 2"),
    # rc alone is not enough: rc 1 with nothing on stdout is not a report.
    ((1, ""), 1, "produced an empty report"),
])
def test_gitlab_blast_radius_honours_the_config_diff_exit_contract(
    pipeline, tmp_path, diff_stub, want_rc, want_err,
) -> None:
    work = tmp_path / f"rc{diff_stub[0]}-{len(diff_stub[1])}"
    base_sha = _synthetic_repo(work, base_has_config=True)
    p = _run_job(pipeline, work, base_sha=base_sha, diff_stub=diff_stub)
    assert p.returncode == want_rc, (
        f"rc={p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    if want_err:
        assert want_err in p.stderr, p.stderr


@_needs_tools
def test_gitlab_blast_radius_names_an_image_without_git(
    pipeline, tmp_path,
) -> None:
    """A customer still pinned to an image from before issue 1444 has no git.

    The job must say THAT, not fail on `git: not found` three lines later,
    which reads like a broken repository. PATH is emptied so `command -v git`
    finds nothing; the check is the job's first line, so nothing else in the
    script needs PATH before it exits.
    """
    work = tmp_path / "nogit"
    base_sha = _synthetic_repo(work, base_has_config=True)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    p = _run_job(pipeline, work, base_sha=base_sha, path=str(empty))
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert "needs git inside the da-tools image" in p.stderr, p.stderr
    assert "issue 1444" in p.stderr, p.stderr


@_needs_tools
def test_gitlab_blast_radius_names_an_unreadable_checkout(
    pipeline, tmp_path,
) -> None:
    """A checkout git cannot read must not be reported as a missing commit.

    The base-commit lookup silences git on purpose, so without a separate,
    unsilenced check a checkout git refuses outright — "dubious ownership"
    when the image user does not own it and safe.directory does not match —
    was reported as "base commit is not in this clone", pointing at clone
    depth. Measured that way in a busybox image as uid 10001. Stood in for
    here by a directory that is not a repository at all: the same class
    (git cannot read it), reachable without changing file ownership.
    """
    work = tmp_path / "unreadable"
    work.mkdir()
    (work / "conf.d").mkdir()
    p = _run_job(pipeline, work, base_sha="1" * 40)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "git cannot read the checkout" in p.stderr, p.stderr
    assert "is not in this clone" not in p.stderr, p.stderr


@_needs_tools
def test_gitlab_blast_radius_refuses_to_run_outside_a_merge_request(
    pipeline, tmp_path,
) -> None:
    """No CI_MERGE_REQUEST_DIFF_BASE_SHA means no baseline: fail loudly.

    The job's `rules:` keep it to merge-request pipelines; this is the guard
    for a customer who widens them.
    """
    work = tmp_path / "branch"
    _synthetic_repo(work, base_has_config=True)
    p = _run_job(pipeline, work, base_sha=None)
    assert p.returncode != 0, p.stdout
    assert "not a merge request pipeline" in p.stderr, p.stderr
    assert not (work / ".output" / "blast-radius.md").exists()
