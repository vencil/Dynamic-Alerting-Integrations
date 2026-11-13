"""Tests for scripts/tools/lint/check_chart_version_bump.py (#1269).

Each case builds a throwaway git repo holding one tiny chart, commits a base,
changes the working tree, and runs the gate's `main()` against it with real
helm — the gate's whole point is to compare what `helm package` produces, so a
helm-free fake would test a different thing. Without helm these tests fail
(not skip) in CI, through tests/_helm_gate.py's rule.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from _helm_gate import HAS_HELM, helm_required  # noqa: E402

_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_chart_version_bump.py"
_spec = importlib.util.spec_from_file_location("check_chart_version_bump", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_chart_version_bump"] = gate
assert _spec.loader is not None
_spec.loader.exec_module(gate)

needs_helm = pytest.mark.skipif(not HAS_HELM, reason="helm CLI not on PATH")

CHART_YAML = """\
apiVersion: v2
name: demo
# history: 0.1.0 initial
version: {version}
"""


def test_helm_present_when_required() -> None:
    reason = helm_required()
    if reason:
        assert HAS_HELM, f"{reason}: but `helm` is not on PATH"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=60).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    chart = tmp_path / "helm" / "demo"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(CHART_YAML.format(version="0.1.0"), encoding="utf-8")
    (chart / "values.yaml").write_text("replicas: 1\n", encoding="utf-8")
    (chart / "templates" / "cm.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    (chart / ".helmignore").write_text("NOTES.md\n", encoding="utf-8")
    (chart / "NOTES.md").write_text("not shipped\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _run(repo: Path, capsys, *extra: str) -> tuple[int, str]:
    rc = gate.main(["--repo", str(repo), "--base", "main", *extra])
    out = capsys.readouterr()
    return rc, out.out + out.err


def _set_version(repo: Path, version: str) -> None:
    (repo / "helm/demo/Chart.yaml").write_text(CHART_YAML.format(version=version), encoding="utf-8")


@needs_helm
@pytest.mark.parametrize("change", [
    lambda r: (r / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8"),
    lambda r: (r / "helm/demo/templates/cm.yaml").unlink(),                 # a deletion ships too
    lambda r: (r / "helm/demo/templates/new.yaml").write_text("kind: Secret\n", encoding="utf-8"),  # untracked
], ids=["edited-values", "deleted-template", "untracked-template"])
def test_a_changed_package_without_a_bump_is_refused(repo: Path, capsys, change) -> None:
    change(repo)
    rc, out = _run(repo, capsys)
    assert rc == 1 and "helm/demo: the package changed" in out and "still 0.1.0" in out, out


@needs_helm
def test_a_bump_lets_the_change_through(repo: Path, capsys) -> None:
    (repo / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    _set_version(repo, "0.1.1")
    rc, out = _run(repo, capsys)
    assert rc == 0, out


@needs_helm
def test_a_lower_version_is_refused(repo: Path, capsys) -> None:
    _git(repo, "checkout", "-q", "-b", "work")
    _set_version(repo, "0.2.0")
    _git(repo, "commit", "-qam", "0.2.0")
    _git(repo, "branch", "-f", "main")
    (repo / "helm/demo/values.yaml").write_text("replicas: 3\n", encoding="utf-8")
    _set_version(repo, "0.1.9")
    rc, out = _run(repo, capsys)
    assert rc == 1 and "went DOWN to 0.1.9" in out, out


@needs_helm
@pytest.mark.parametrize("change", [
    # helm re-serialises Chart.yaml: a comment never reaches the archive
    lambda r: (r / "helm/demo/Chart.yaml").write_text(
        CHART_YAML.format(version="0.1.0") + "# another history line\n", encoding="utf-8"),
    # .helmignore'd
    lambda r: (r / "helm/demo/NOTES.md").write_text("still not shipped, edited\n", encoding="utf-8"),
], ids=["chart-yaml-comment", "helmignored-file"])
def test_an_edit_that_does_not_ship_needs_no_bump(repo: Path, capsys, change) -> None:
    change(repo)
    rc, out = _run(repo, capsys)
    assert rc == 0 and "1 git-only chart(s) checked" in out, out


@needs_helm
def test_a_new_chart_needs_nothing(repo: Path, capsys) -> None:
    new = repo / "helm" / "fresh"
    new.mkdir()
    (new / "Chart.yaml").write_text("apiVersion: v2\nname: fresh\nversion: 0.0.1\n", encoding="utf-8")
    rc, out = _run(repo, capsys)
    assert rc == 0, out


@needs_helm
def test_a_chart_a_release_line_pushes_is_skipped_and_said_so(repo: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(gate, "discover_pushed_charts", lambda _repo: {"demo": ["release.yaml:1"]})
    (repo / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    rc, out = _run(repo, capsys)
    assert rc == 0 and "helm/demo: skipped — a release line pushes 'demo'" in out, out


@needs_helm
def test_drift_already_on_the_base_is_caught(repo: Path, capsys) -> None:
    """A content change committed without a bump (Renovate's image bumps did
    this) is red on every later run, not only in the PR that made it."""
    (repo / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "drift")
    rc, out = _run(repo, capsys)
    assert rc == 1 and "since" in out and "introduced version 0.1.0" in out, out


@needs_helm
def test_a_superseded_main_run_leaves_nothing_unchecked(repo: Path, capsys, monkeypatch) -> None:
    """Push A changes the chart without a bump, push B only touches docs, and
    A's CI run is superseded. B's run (base = HEAD on main) still catches A."""
    (repo / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "A")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "B")
    monkeypatch.setattr(gate, "resolve_diff_base_ref",
                        lambda: ("origin/main", _git(repo, "rev-parse", "HEAD")))
    rc = gate.main(["--repo", str(repo)])
    out = capsys.readouterr()
    assert rc == 1 and "still 0.1.0" in out.err, out


@needs_helm
def test_a_version_introduced_on_the_branch_may_change_until_merged(repo: Path, capsys) -> None:
    """Bump, then more edits in later commits of the same PR: the version is
    new relative to the base, so it does not yet name a package anyone has."""
    _git(repo, "checkout", "-q", "-b", "pr")
    _set_version(repo, "0.1.1")
    _git(repo, "commit", "-qam", "bump")
    (repo / "helm/demo/values.yaml").write_text("replicas: 9\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "more")
    rc, out = _run(repo, capsys)
    assert rc == 0, out


@needs_helm
def test_files_git_does_not_see_are_not_packaged(repo: Path, capsys) -> None:
    """A __pycache__ a local test run left (gitignored) is not what a clean
    checkout ships, so it must not read as a change."""
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore")
    cache = repo / "helm/demo/templates/__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"\x00junk")
    rc, out = _run(repo, capsys)
    assert rc == 0, out


@needs_helm
def test_a_move_and_change_in_one_commit_is_caught(repo: Path, capsys) -> None:
    """Renames are followed back to where the version was introduced."""
    _git(repo, "mv", "helm/demo", "helm/demo-moved")
    (repo / "helm/demo-moved/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move and change")
    rc, out = _run(repo, capsys)
    assert rc == 1 and "helm/demo-moved: the package changed" in out, out


@needs_helm
def test_a_shallow_history_that_hides_the_first_commit_is_a_caller_error(repo: Path, tmp_path_factory, capsys) -> None:
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "unrelated")
    (repo / "helm/demo/NOTES.md").write_text("still not shipped\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "touch the chart, same version")
    shallow = tmp_path_factory.mktemp("shallow")
    subprocess.run(["git", "clone", "-q", "--depth=1", "-b", "main", f"file://{repo}", str(shallow)],
                   check=True, capture_output=True, timeout=60)
    rc = gate.main(["--repo", str(shallow), "--base", "HEAD"])
    out = capsys.readouterr()
    assert rc == 2 and "shallow" in out.err, out


@needs_helm
def test_moving_a_chart_does_not_reset_its_version(repo: Path, capsys) -> None:
    """Paired by `name`: a new directory with the same chart name and changed
    content is the same chart, not a new one."""
    _git(repo, "mv", "helm/demo", "helm/demo-moved")
    (repo / "helm/demo-moved/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    rc, out = _run(repo, capsys)
    assert rc == 1 and "helm/demo-moved: the package changed" in out and "still 0.1.0" in out, out


@needs_helm
def test_renaming_the_chart_makes_it_a_new_one(repo: Path, capsys) -> None:
    (repo / "helm/demo/Chart.yaml").write_text(
        CHART_YAML.format(version="0.1.0").replace("name: demo", "name: demo2"), encoding="utf-8")
    rc, out = _run(repo, capsys)
    assert rc == 0, out


def test_ci_runs_the_hook_with_no_push_specific_base() -> None:
    """The check is stateless: no `before` plumbing that a superseded run could drop."""
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "CHART_VERSION_BUMP_BASE" not in ci


def test_semver_precedence() -> None:
    k = gate.semver_key
    assert k("0.10.0") > k("0.9.9")
    assert k("1.0.0") > k("1.0.0-rc.1") > k("1.0.0-beta.2") > k("1.0.0-beta.1")
    assert k("1.0.0-rc.10") > k("1.0.0-rc.9")
    with pytest.raises(gate.CallerError):
        k("1.0")


def test_an_unresolvable_base_is_a_caller_error(repo: Path, capsys) -> None:
    rc = gate.main(["--repo", str(repo), "--base", "no-such-ref"])
    assert rc == 2, capsys.readouterr()


def test_without_helm_it_cannot_pass(repo: Path, capsys, monkeypatch) -> None:
    """A changed chart and no helm is 'could not measure', never a pass."""
    monkeypatch.setattr(gate, "package", lambda *_a: (_ for _ in ()).throw(gate.CallerError("no helm")))
    (repo / "helm/demo/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    rc = gate.main(["--repo", str(repo), "--base", "main"])
    assert rc == 2, capsys.readouterr()
