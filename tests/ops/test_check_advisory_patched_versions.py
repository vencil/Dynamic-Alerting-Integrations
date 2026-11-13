"""Tests for scripts/tools/ops/check_advisory_patched_versions.py (#1269).

Registry spaces are injected (no network); the git-only space runs against a
throwaway repo; the platform space against that repo's tags. The live API path
is exercised by `make advisory-patched-check`, not here.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "ops" / "check_advisory_patched_versions.py"
_spec = importlib.util.spec_from_file_location("check_advisory_patched_versions", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_advisory_patched_versions"] = gate
assert _spec.loader is not None
_spec.loader.exec_module(gate)

IMAGE = "tenant-api (container image: ghcr.io/vencil/tenant-api)"
OCI = "tenant-api (Helm chart: oci://ghcr.io/vencil/charts/tenant-api)"
GIT_ONLY = "demo (Helm chart, git-only: helm/demo)"
PLATFORM = "Dynamic Alerting (platform release: git tag v*)"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    chart = tmp_path / "helm" / "demo" / "Chart.yaml"
    chart.parent.mkdir(parents=True)
    for v in ("0.3.0", "0.3.1", "0.4.0"):
        chart.write_text(f"apiVersion: v2\nname: demo\n# 0.9.9 — a history line, not a version\nversion: {v}\n",
                         encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", v)
    _git(tmp_path, "tag", "v2.9.0")
    return tmp_path


def _spaces(repo: Path, registry: dict | None = None) -> "gate.Spaces":
    reg = registry if registry is not None else {
        "ghcr.io/vencil/tenant-api": {"v2.9.7", "v2.9.8", "latest", "candidate-1-1"},
        "oci://ghcr.io/vencil/charts/tenant-api": {"2.9.7", "2.9.8"},
    }
    return gate.Spaces(repo, registry=lambda where: reg[where])


def _adv(name: str, vulnerable: str, patched: str, state: str = "draft") -> dict:
    return {"ghsa_id": "GHSA-test", "state": state, "vulnerabilities": [
        {"package": {"ecosystem": "other", "name": name},
         "vulnerable_version_range": vulnerable, "patched_versions": patched}]}


@pytest.mark.parametrize("name, vulnerable, patched", [
    (IMAGE, "<= 2.9.7", "2.9.8"),        # image tags carry a `v`; the row need not
    (IMAGE, "<= 2.9.7", "v2.9.8"),
    (OCI, "< 2.9.8", "2.9.8"),
    (GIT_ONLY, "= 0.3.1", "0.4.0"),
    (PLATFORM, "< 2.9.0", "2.9.0"),
])
def test_a_patched_version_that_exists_passes(repo, name, vulnerable, patched) -> None:
    violations, _ = gate.run([_adv(name, vulnerable, patched)], _spaces(repo))
    assert violations == []


@pytest.mark.parametrize("name, vulnerable, patched", [
    (IMAGE, "<= 2.9.8", "2.9.9"),        # not published
    (OCI, "< 2.9.8", "2.9.80"),
    (GIT_ONLY, "= 0.3.1", "0.9.9"),      # only in a Chart.yaml COMMENT, never a version
    (PLATFORM, "< 2.9.0", "2.9.1"),
])
def test_a_patched_version_that_does_not_exist_is_refused(repo, name, vulnerable, patched) -> None:
    violations, _ = gate.run([_adv(name, vulnerable, patched)], _spaces(repo))
    assert len(violations) == 1 and "does not exist in" in violations[0], violations


def test_published_advisories_are_checked_too(repo) -> None:
    violations, _ = gate.run([_adv(OCI, "< 2.9.8", "2.9.9", state="published")], _spaces(repo))
    assert violations, "a published advisory naming an unpublished version passed"


def test_patched_must_be_concrete_versions(repo) -> None:
    violations, _ = gate.run([_adv(OCI, "< 2.9.8", ">= 2.9.8")], _spaces(repo))
    assert len(violations) == 1 and "concrete versions" in violations[0], violations


def test_empty_patched_after_a_fix_was_published_is_refused(repo) -> None:
    violations, _ = gate.run([_adv(IMAGE, "<= 2.9.7", "")], _spaces(repo))
    assert len(violations) == 1 and "earliest: 2.9.8" in violations[0], violations


def test_empty_patched_before_any_fix_is_only_listed(repo) -> None:
    violations, notes = gate.run([_adv(IMAGE, "<= 2.9.8", "")], _spaces(repo))
    assert violations == [] and len(notes) == 1 and "not yet patched" in notes[0]


def test_an_older_version_outside_an_exact_range_is_no_fix(repo) -> None:
    """`= 0.3.1` with 0.3.0 published and nothing after: 0.3.0 is outside the
    range but it is not a fix, so an empty row is only listed."""
    registry = {"ghcr.io/vencil/tenant-api": {"0.3.0", "0.3.1"}}
    violations, notes = gate.run([_adv(IMAGE, "= 0.3.1", "")], _spaces(repo, registry))
    assert violations == [] and notes, (violations, notes)


def test_a_package_name_without_a_space_is_refused(repo) -> None:
    violations, _ = gate.run([_adv("tenant-api", "<= 2.9.8", "")], _spaces(repo))
    assert len(violations) == 1 and "which version space" in violations[0], violations


@pytest.mark.parametrize("name", [
    "x (platform release: v2.9.0)",           # platform must say `git tag v*`
    "x (Helm chart, git-only: charts/demo)",  # git-only must be helm/<dir>
    "x (docker image: ghcr.io/vencil/x)",
])
def test_malformed_spaces_are_refused(repo, name) -> None:
    violations, _ = gate.run([_adv(name, "<= 1.0.0", "")], _spaces(repo))
    assert violations, name


def test_an_advisory_with_no_rows_is_refused(repo) -> None:
    violations, _ = gate.run([{"ghsa_id": "GHSA-x", "state": "draft", "vulnerabilities": []}], _spaces(repo))
    assert violations


def test_closed_advisories_are_skipped(repo) -> None:
    violations, _ = gate.run([_adv("free text", "", "", state="closed")], _spaces(repo))
    assert violations == []


def test_a_shallow_clone_cannot_answer_a_git_only_row(repo, tmp_path_factory) -> None:
    shallow = tmp_path_factory.mktemp("shallow")
    subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{repo}", str(shallow)],
                   check=True, capture_output=True, timeout=60)
    with pytest.raises(gate.CallerError, match="shallow"):
        gate.run([_adv(GIT_ONLY, "= 0.3.1", "0.4.0")], _spaces(shallow))


def test_an_unreadable_range_cannot_be_answered(repo) -> None:
    with pytest.raises(gate.CallerError):
        gate.run([_adv(IMAGE, "~> 2.9", "")], _spaces(repo))


def test_semver_range_semantics() -> None:
    assert gate.in_range("2.9.8", "<= 2.9.8") and not gate.in_range("2.9.9", "<= 2.9.8")
    assert gate.in_range("1.5.0", ">= 1.0.0, < 2.0.0") and not gate.in_range("2.0.0", ">= 1.0.0, < 2.0.0")
    assert gate.in_range("0.3.1", "= 0.3.1") and not gate.in_range("0.10.0", "= 0.3.1")


def test_api_pages_are_followed_by_hand() -> None:
    """Page 1 full (100) → page 2 asked for; a short page ends it."""
    calls = []

    def runner(argv, **_kw):
        calls.append(argv[-1])
        n = 100 if len(calls) == 1 else 3
        return subprocess.CompletedProcess(argv, 0, json.dumps([{}] * n), "")

    assert len(gate.fetch_advisories(runner)) == 103
    assert [c.rsplit("page=", 1)[1] for c in calls] == ["1", "2"]
    assert all(c.startswith("repos/{owner}/{repo}/") for c in calls), calls


def test_an_api_failure_is_a_caller_error() -> None:
    def runner(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
    with pytest.raises(gate.CallerError, match="403"):
        gate.fetch_advisories(runner)


def test_main_exit_codes(repo, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    monkeypatch.setattr(gate, "ghcr_tags", lambda where: {"v2.9.8", "2.9.8"})
    f = tmp_path / "a.json"
    f.write_text(json.dumps([_adv(IMAGE, "<= 2.9.7", "2.9.8")]), encoding="utf-8")
    assert gate.main(["--advisories-json", str(f)]) == 0
    f.write_text(json.dumps([_adv(IMAGE, "<= 2.9.7", "")]), encoding="utf-8")
    assert gate.main(["--advisories-json", str(f)]) == 1
    f.write_text("{not json", encoding="utf-8")
    assert gate.main(["--advisories-json", str(f)]) == 2
    capsys.readouterr()


def test_pre_tag_runs_the_check() -> None:
    """Wired into `make pre-tag`, and not behind ADVISORY_ACK."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    pre_tag = next(line for line in makefile.splitlines() if line.startswith("pre-tag:"))
    assert " advisory-patched-check " in pre_tag, pre_tag
    recipe = makefile.split("advisory-patched-check: ##", 1)[1].split("\n\n", 1)[0]
    assert "ADVISORY_ACK" not in recipe and "check_advisory_patched_versions.py" in recipe, recipe
