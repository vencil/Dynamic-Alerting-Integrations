"""Tests for scripts/tools/lint/check_init_chart_pin_published.py (#1425).

The gate stops a `tools/v*` release whose `da-tools init` would pin a
published threshold-exporter chart that differs from the chart in the tree.
Each case builds a throwaway git repo with an `origin`, so the tag lookups,
the diff and `ls-remote` run for real. The gate is not run on this checkout:
CI clones are shallow and carry no tags, which is exactly the caller error
it reports.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# ⛔ One literal path string, so scripts/tools/dx/verify_diff.py maps a change
# to the gate onto this module.
_GATE = _REPO_ROOT / "scripts/tools/lint/check_init_chart_pin_published.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools"))

import check_init_chart_pin_published as gate  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
        timeout=60).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _set_pin(root: Path, pin: str) -> None:
    _write(root, "scripts/tools/ops/init_project.py",
           f"X = 1\nTHRESHOLD_EXPORTER_CHART_VERSION = '{pin}'\n")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A clone of an origin where exporter/v1.0.0 is published and pinned."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    root = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "remote", "add", "origin", str(origin))
    _set_pin(root, "1.0.0")
    _write(root, "helm/threshold-exporter/values.yaml", "a: 1\n")
    _write(root, "README.md", "x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c1")
    _git(root, "tag", "exporter/v1.0.0")
    _git(root, "push", "-q", "origin", "HEAD:refs/heads/main", "exporter/v1.0.0")
    return root


def _commit(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", rel)


def test_unchanged_chart_is_clean(repo: Path) -> None:
    _commit(repo, "README.md", "y\n")  # outside the chart
    rc, lines = gate.check(repo, require_tag=False)
    assert rc == gate.EXIT_OK, lines
    assert gate.check(repo, require_tag=True)[0] == gate.EXIT_OK


@pytest.mark.parametrize("require_tag", [False, True])
def test_chart_changed_since_the_pinned_tag_fails(repo: Path, require_tag: bool) -> None:
    _commit(repo, "helm/threshold-exporter/values.schema.json", "{}\n")
    rc, lines = gate.check(repo, require_tag=require_tag)
    assert rc == gate.EXIT_VIOLATION, lines
    assert "changed since exporter/v1.0.0" in lines[0]
    assert "  changed: helm/threshold-exporter/values.schema.json" in lines


def test_pin_moved_to_an_unpublished_chart_passes_pre_tag(repo: Path) -> None:
    """The wrap-up PR bumped --exporter; its tag comes with this release."""
    _commit(repo, "helm/threshold-exporter/values.yaml", "a: 2\n")
    _set_pin(repo, "1.1.0")
    rc, lines = gate.check(repo, require_tag=False)
    assert rc == gate.EXIT_OK, lines
    assert "must push it before tools/v*" in lines[0]


def test_pin_to_an_unpublished_chart_fails_at_release(repo: Path) -> None:
    _set_pin(repo, "1.1.0")
    rc, lines = gate.check(repo, require_tag=True)
    assert rc == gate.EXIT_VIOLATION, lines
    assert "does not exist" in lines[0]


def test_tag_on_origin_but_not_fetched_is_a_caller_error(repo: Path) -> None:
    """A shallow clone without tags must not read as 'not published yet'."""
    _git(repo, "tag", "exporter/v1.1.0")
    _git(repo, "push", "-q", "origin", "exporter/v1.1.0")
    _git(repo, "tag", "-d", "exporter/v1.1.0")
    _set_pin(repo, "1.1.0")
    with pytest.raises(gate.CallerError, match="exists on origin but not locally"):
        gate.check(repo, require_tag=False)


def test_unreachable_origin_is_a_caller_error(repo: Path) -> None:
    _git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    _set_pin(repo, "1.1.0")
    with pytest.raises(gate.CallerError, match="ls-remote"):
        gate.check(repo, require_tag=False)


@pytest.mark.parametrize("text,found", [
    ("X = 1\n", 0),
    ("THRESHOLD_EXPORTER_CHART_VERSION = '1.0.0'\n"
     "THRESHOLD_EXPORTER_CHART_VERSION = '1.0.1'\n", 2),
    ("THRESHOLD_EXPORTER_CHART_VERSION = \"1.0.0\"\n", 0),
])
def test_unreadable_pin_is_a_caller_error(tmp_path: Path, text: str, found: int) -> None:
    _write(tmp_path, "scripts/tools/ops/init_project.py", text)
    with pytest.raises(gate.CallerError, match=f"found {found}"):
        gate.read_pin(tmp_path)


def test_not_a_git_checkout_is_a_caller_error(tmp_path: Path) -> None:
    _set_pin(tmp_path, "1.0.0")
    with pytest.raises(gate.CallerError, match="not a git checkout"):
        gate.check(tmp_path, require_tag=False)


def test_cli_exit_codes(repo: Path) -> None:
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(_GATE), "--root", str(repo), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=60)
    assert run().returncode == 0
    _commit(repo, "helm/threshold-exporter/values.yaml", "a: 3\n")
    proc = run("--require-tag")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL:" in proc.stderr
    _git(repo, "remote", "remove", "origin")
    _set_pin(repo, "9.9.9")
    proc = run()
    assert proc.returncode == 2 and "ERROR:" in proc.stderr, proc.stderr


def test_the_real_pin_is_the_one_init_uses() -> None:
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
    import init_project
    assert gate.read_pin(_REPO_ROOT) == init_project.THRESHOLD_EXPORTER_CHART_VERSION


def test_a_change_helm_does_not_pack_does_not_block(repo: Path) -> None:
    """The chart README is .helmignore'd: it is not in the published .tgz."""
    _commit(repo, "helm/threshold-exporter/.helmignore", "README.md\napp/\n")
    _git(repo, "tag", "-f", "exporter/v1.0.0")
    _commit(repo, "helm/threshold-exporter/README.md", "docs\n")
    _commit(repo, "helm/threshold-exporter/app/main.go", "package main\n")
    rc, lines = gate.check(repo, require_tag=True)
    assert rc == gate.EXIT_OK, lines


def test_editing_helmignore_itself_blocks(repo: Path) -> None:
    """`.helmignore` ships and changes what is packed."""
    _commit(repo, "helm/threshold-exporter/.helmignore", "README.md\n")
    rc, lines = gate.check(repo, require_tag=True)
    assert rc == gate.EXIT_VIOLATION, lines
    assert "  changed: helm/threshold-exporter/.helmignore" in lines


@pytest.mark.parametrize("rel,packed", [
    ("README.md", False),            # basename rule at the root
    ("templates/README.md", False),  # ... and at any depth
    ("app/main.go", False),          # directory rule
    ("templates/app", True),         # a directory rule never matches a file
    ("x.swp", False),                # glob
    ("templates/deployment.yaml", True),
    ("ci/values.yaml", False),       # rule with a slash: path from the root
    ("templates/ci/values.yaml", True),
])
def test_is_packed_follows_helm_rules(rel: str, packed: bool) -> None:
    rules = ["README.md", "app/", "*.swp", "/ci/values.yaml"]
    assert gate.is_packed(rel, rules) is packed


def test_the_tools_release_job_runs_the_gate_with_require_tag() -> None:
    """Dropping the release.yaml step or its flag must turn this red."""
    import yaml
    wf = yaml.safe_load((_REPO_ROOT / ".github/workflows/release.yaml")
                        .read_text(encoding="utf-8"))
    steps = wf["jobs"]["release-da-tools"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    gate_at = [i for i, run in enumerate(runs)
               if "scripts/tools/lint/check_init_chart_pin_published.py --require-tag" in run]
    assert len(gate_at) == 1, runs
    assert "if" not in steps[gate_at[0]], steps[gate_at[0]]
    pushes = [i for i, s in enumerate(steps)
              if "docker/build-push-action" in str(s.get("uses", ""))
              or "docker push" in str(s.get("run", ""))
              or "build.sh" in str(s.get("run", ""))]
    assert pushes and gate_at[0] < min(pushes), (gate_at, pushes)
