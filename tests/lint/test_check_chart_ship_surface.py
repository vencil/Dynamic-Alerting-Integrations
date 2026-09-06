"""Tests for scripts/tools/lint/check_chart_ship_surface.py (#1755).

The gate asserts that every file at the root of a chart this repo `helm
package`s is declared — SHIP, or EXCLUDE plus a matching `.helmignore` line.
Each test pins one branch of that contract:

  - the LIVE repo is green, and green for the right reason: the three charts
    the release pipeline packages are exactly the ones under scrutiny, and the
    one EXCLUDE declaration is honoured by a real `.helmignore` line;
  - counterfactual — replaying #1597 (the lint variant present, the
    `.helmignore` line absent) turns the gate RED, so the gate is demonstrably
    doing the catching;
  - a new undeclared root file is RED: that is the next `values-*.yaml`;
  - a declaration whose file is gone is RED, so the table cannot rot;
  - a chart that starts shipping without a declaration block is RED — which is
    what makes "derive which charts ship" worth doing at all;
  - fail-closed — an unresolvable Makefile target, a templated workflow target
    and a missing chart directory are caller errors, never silent passes.

⚠️ Boundary, stated in the gate's own docstring: this asserts the chart SOURCE
tree, not the bytes of the `.tgz`. Reading the tarball needs `helm`, which is
unavailable in this environment; no test here claims otherwise.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_chart_ship_surface.py"

_spec = importlib.util.spec_from_file_location("check_chart_ship_surface", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_chart_ship_surface"] = gate
assert _spec.loader is not None
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# fixture repo builder
# ---------------------------------------------------------------------------
def _make_repo(
    tmp_path: Path,
    *,
    chart: str = "helm/demo",
    root_files: dict[str, str] | None = None,
    helmignore: str | None = None,
    workflow: str | None = None,
    makefile: str | None = None,
) -> Path:
    repo = tmp_path / "repo"
    chart_dir = repo / chart
    (chart_dir / "templates").mkdir(parents=True)
    for name, body in (root_files or {"Chart.yaml": "name: demo\n"}).items():
        (chart_dir / name).write_text(body, encoding="utf-8")
    if helmignore is not None:
        (chart_dir / ".helmignore").write_text(helmignore, encoding="utf-8")

    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "release.yaml").write_text(
        workflow if workflow is not None else f"          helm package {chart} -d .build/\n",
        encoding="utf-8",
    )
    if makefile is not None:
        (repo / "Makefile").write_text(makefile, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# the live repo
# ---------------------------------------------------------------------------
class TestLiveRepoIsClean:
    def test_no_violations(self) -> None:
        assert gate.check(REPO_ROOT) == []

    def test_discovery_finds_the_packaged_charts(self) -> None:
        """Green for the right reason: the charts under scrutiny are the ones
        the release pipeline actually packages, not a hand-kept list."""
        sites = gate.discover_shipping_charts(REPO_ROOT)
        assert set(sites) == {
            "helm/threshold-exporter",
            "helm/recipe-preview",
            "helm/tenant-api",
        }
        # tenant-api is discovered from the release workflow, not from DECLARED.
        assert any(
            "release.yaml" in site for site in sites["helm/tenant-api"]
        ), sites["helm/tenant-api"]
        # threshold-exporter is packaged by the Makefile too, through $(CHART_DIR).
        assert any(
            site.startswith("Makefile:") for site in sites["helm/threshold-exporter"]
        ), sites["helm/threshold-exporter"]

    def test_the_one_exclude_is_honoured_by_a_real_helmignore_line(self) -> None:
        patterns = gate.helmignore_patterns(REPO_ROOT / "helm" / "tenant-api")
        assert gate._is_ignored("values-scope-enforce.yaml", patterns)


# ---------------------------------------------------------------------------
# counterfactuals
# ---------------------------------------------------------------------------
class TestViolations:
    def test_replay_of_1597_is_red(self, tmp_path: Path, monkeypatch) -> None:
        """The lint variant is present, the .helmignore line is not — exactly
        the state #1597 had to find by hand."""
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="# nothing excluded\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "values-scope-enforce.yaml" in violations[0]
        assert "no .helmignore line matches it" in violations[0]

    def test_the_same_file_declared_and_ignored_is_green(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Control for the test above: with the line present it passes, so the
        RED comes from the missing exclusion and nothing else."""
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="values-scope-enforce.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_a_glob_helmignore_line_also_satisfies_exclude(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="values-*.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_undeclared_root_file_is_red(self, tmp_path: Path, monkeypatch) -> None:
        """The next values-*.yaml, whatever it turns out to be called."""
        repo = _make_repo(
            tmp_path,
            root_files={"Chart.yaml": "name: demo\n", "values-debug.yaml": "x: 1\n"},
        )
        monkeypatch.setattr(
            gate, "DECLARED", {"helm/demo": {"Chart.yaml": (gate.SHIP, "metadata")}}
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "values-debug.yaml" in violations[0]
        assert "undeclared" in violations[0]

    def test_stale_declaration_is_red(self, tmp_path: Path, monkeypatch) -> None:
        repo = _make_repo(tmp_path, root_files={"Chart.yaml": "name: demo\n"})
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    "gone.yaml": (gate.SHIP, "deleted two releases ago"),
                }
            },
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "gone.yaml" in violations[0] and "stale" in violations[0]

    def test_newly_shipping_chart_without_declarations_is_red(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The point of deriving the chart list: a fourth chart starts shipping
        and the gate demands its declarations instead of ignoring it."""
        repo = _make_repo(tmp_path, root_files={"Chart.yaml": "name: demo\n"})
        monkeypatch.setattr(gate, "DECLARED", {})
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "no entry in DECLARED" in violations[0]


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------
class TestFailsClosed:
    def test_missing_chart_directory_is_a_caller_error(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".github" / "workflows" / "release.yaml").write_text(
            "          helm package helm/does-not-exist -d .build/\n", encoding="utf-8"
        )
        with pytest.raises(gate.CallerError, match="not a directory"):
            gate.check(repo)

    def test_templated_workflow_target_is_a_caller_error(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="          helm package helm/${{ matrix.chart }} -d .build/\n",
        )
        with pytest.raises(gate.CallerError, match="templated target"):
            gate.discover_shipping_charts(repo)

    def test_unresolvable_makefile_target_is_a_caller_error(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="# no helm package here\n",
            makefile="\t@helm package $(MYSTERY_DIR) -d .build/\n",
        )
        with pytest.raises(gate.CallerError, match="unresolvable target"):
            gate.discover_shipping_charts(repo)

    def test_makefile_variable_is_resolved(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="# packaged from the Makefile only\n",
            makefile="CHART_DIR  := helm/demo\n\t@helm package $(CHART_DIR) -d .build/\n",
        )
        sites = gate.discover_shipping_charts(repo)
        assert "helm/demo" in sites


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_on_the_live_repo_exits_zero() -> None:
    assert gate.main([]) == gate.EXIT_OK
