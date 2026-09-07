"""Tests for scripts/tools/lint/check_chart_package_contents.py (#1755).

This gate reads the bytes `helm package` produced. Its sibling
`check_chart_ship_surface.py` reads the source tree; the two share ONE table
(`DECLARED`), so these tests pin what only the archive can show:

  - a file at the archive root that nothing declared SHIP — the #1597 shape,
    and the shape a package-time working-tree contamination takes;
  - a declared SHIP file the archive does not actually contain;
  - a top-level subtree nobody declared, `charts/` (a subchart dependency now
    shipping inside the package) called out by name;
  - a stale subtree declaration.

Fixture archives are built in-process with `tarfile`, so the whole suite runs
without helm and is deterministic. One test does exercise real helm — see
`TestAgainstRealHelm`, which asserts in BOTH branches rather than skipping.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_chart_package_contents.py"

_spec = importlib.util.spec_from_file_location("check_chart_package_contents", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_chart_package_contents"] = gate
assert _spec.loader is not None
_spec.loader.exec_module(gate)


def _tgz(tmp_path: Path, members: list[str], *, name: str = "demo-0.1.0.tgz") -> Path:
    """Build a .tgz whose members are exactly `members` (paths, chart prefix and all)."""
    payload = tmp_path / "payload"
    payload.mkdir(exist_ok=True)
    out = tmp_path / name
    with tarfile.open(out, "w:gz") as tar:
        for i, member in enumerate(members):
            f = payload / f"f{i}"
            f.write_text("x\n", encoding="utf-8")
            tar.add(f, arcname=member)
    return out


@pytest.fixture()
def declared(monkeypatch):
    """One demo chart: two SHIP root files, templates/ only."""
    monkeypatch.setattr(
        gate,
        "DECLARED",
        {"helm/demo": {"Chart.yaml": (gate.SHIP, "meta"),
                       "values.yaml": (gate.SHIP, "defaults")}},
    )
    monkeypatch.setattr(gate, "DECLARED_SUBTREES", {"helm/demo": {"templates"}})


GOOD = ["demo/Chart.yaml", "demo/values.yaml", "demo/templates/deployment.yaml"]


class TestArchiveContents:
    def test_matching_archive_is_green(self, tmp_path, declared) -> None:
        assert gate.check_archive("helm/demo", _tgz(tmp_path, GOOD)) == []

    def test_undeclared_root_file_is_red(self, tmp_path, declared) -> None:
        v = gate.check_archive("helm/demo", _tgz(tmp_path, GOOD + ["demo/LICENSE"]))
        assert len(v) == 1
        assert "LICENSE" in v[0] and "not declared SHIP" in v[0]

    def test_a_values_variant_names_the_1597_shape(self, tmp_path, declared) -> None:
        """The incident this whole pair of gates exists for gets a pointed message."""
        v = gate.check_archive(
            "helm/demo", _tgz(tmp_path, GOOD + ["demo/values-scope-enforce.yaml"])
        )
        assert len(v) == 1
        assert "#1597" in v[0]

    def test_declared_ship_file_missing_from_archive_is_red(
        self, tmp_path, declared
    ) -> None:
        v = gate.check_archive(
            "helm/demo", _tgz(tmp_path, ["demo/Chart.yaml", "demo/templates/x.yaml"])
        )
        assert len(v) == 1
        assert "values.yaml" in v[0] and "does NOT contain" in v[0]

    def test_undeclared_subtree_is_red(self, tmp_path, declared) -> None:
        v = gate.check_archive("helm/demo", _tgz(tmp_path, GOOD + ["demo/hack/x.sh"]))
        assert len(v) == 1
        assert "'hack'/" in v[0]

    def test_charts_subtree_is_called_out_as_a_dependency(
        self, tmp_path, declared
    ) -> None:
        """⭐ `charts/` is the one that ships someone else's code to customers."""
        v = gate.check_archive(
            "helm/demo",
            _tgz(tmp_path, GOOD + ["demo/charts/sub/Chart.yaml",
                                   "demo/charts/sub/templates/x.yaml"]),
        )
        assert len(v) == 1
        assert "subchart dependency" in v[0] and "(2 file(s))" in v[0]

    def test_stale_subtree_declaration_is_red(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            gate, "DECLARED", {"helm/demo": {"Chart.yaml": (gate.SHIP, "meta")}}
        )
        monkeypatch.setattr(
            gate, "DECLARED_SUBTREES", {"helm/demo": {"templates", "crds"}}
        )
        v = gate.check_archive(
            "helm/demo", _tgz(tmp_path, ["demo/Chart.yaml", "demo/templates/x.yaml"])
        )
        assert len(v) == 1
        assert "'crds'/" in v[0] and "stale" in v[0]


class TestFailsClosed:
    """⛔ Every one of these must be exit 2, never a quiet pass."""

    def test_missing_archive(self, tmp_path, declared) -> None:
        with pytest.raises(gate.CallerError, match="no such archive"):
            gate.check_archive("helm/demo", tmp_path / "nope.tgz")

    def test_empty_archive(self, tmp_path, declared) -> None:
        out = tmp_path / "empty.tgz"
        with tarfile.open(out, "w:gz"):
            pass
        with pytest.raises(gate.CallerError, match="contains no files"):
            gate.check_archive("helm/demo", out)

    def test_two_top_level_entries(self, tmp_path, declared) -> None:
        with pytest.raises(gate.CallerError, match="top-level entries"):
            gate.check_archive("helm/demo", _tgz(tmp_path, GOOD + ["other/Chart.yaml"]))

    def test_chart_absent_from_declared(self, tmp_path, declared) -> None:
        with pytest.raises(gate.CallerError, match="no entry in .*DECLARED"):
            gate.check_archive("helm/unknown", _tgz(tmp_path, GOOD))

    def test_chart_absent_from_declared_subtrees(
        self, tmp_path, monkeypatch
    ) -> None:
        """Half-registered is still unrunnable — and says which half is missing."""
        monkeypatch.setattr(
            gate, "DECLARED", {"helm/demo": {"Chart.yaml": (gate.SHIP, "meta")}}
        )
        monkeypatch.setattr(gate, "DECLARED_SUBTREES", {})
        with pytest.raises(gate.CallerError, match="DECLARED_SUBTREES"):
            gate.check_archive("helm/demo", _tgz(tmp_path, ["demo/Chart.yaml"]))

    def test_unreadable_archive(self, tmp_path, declared) -> None:
        bad = tmp_path / "notatarball.tgz"
        bad.write_text("this is not gzip\n", encoding="utf-8")
        with pytest.raises(gate.CallerError, match="cannot read"):
            gate.check_archive("helm/demo", bad)

    def test_helm_package_exiting_nonzero(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(gate.shutil, "which", lambda _n: "/usr/bin/helm")

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "Error: Chart.yaml file is missing"

        monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Proc())
        with pytest.raises(gate.CallerError, match="Chart.yaml file is missing"):
            gate.package(tmp_path, tmp_path)

    def test_helm_binary_that_cannot_be_executed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(gate.shutil, "which", lambda _n: "/usr/bin/helm")

        def boom(*_a, **_k):
            raise OSError(8, "Exec format error")

        monkeypatch.setattr(gate.subprocess, "run", boom)
        with pytest.raises(gate.CallerError, match="failed to run"):
            gate.package(tmp_path, tmp_path)

    def test_ambiguous_package_output(self, tmp_path, monkeypatch) -> None:
        """Two archives in the destination means we cannot tell which one helm
        just built — inspecting the wrong one is worse than not inspecting."""
        monkeypatch.setattr(gate.shutil, "which", lambda _n: "/usr/bin/helm")

        class _Proc:
            returncode = 0
            stdout = stderr = ""

        def fake_run(*_a, **_k):
            (tmp_path / "a-1.0.0.tgz").write_bytes(b"")
            (tmp_path / "b-1.0.0.tgz").write_bytes(b"")
            return _Proc()

        monkeypatch.setattr(gate.subprocess, "run", fake_run)
        with pytest.raises(gate.CallerError, match="exactly one .tgz"):
            gate.package(tmp_path, tmp_path)

    def test_member_with_no_path_separator_is_skipped(
        self, tmp_path, declared
    ) -> None:
        """A bare top-level entry carries no chart-relative path; it must not be
        mistaken for a root file."""
        assert gate.check_archive("helm/demo", _tgz(tmp_path, GOOD + ["demo"])) == []

    def test_main_on_a_chart_dir_that_does_not_exist(self, capsys) -> None:
        assert gate.main(["--chart", "helm/nope"]) == gate.EXIT_CALLER_ERROR
        assert "not a directory" in capsys.readouterr().err

    def test_package_without_helm_is_a_caller_error(self, tmp_path, monkeypatch):
        """⛔ 'helm missing' must never become 'nothing to report'."""
        monkeypatch.setattr(gate.shutil, "which", lambda _n: None)
        with pytest.raises(gate.CallerError, match="could not measure"):
            gate.package(tmp_path, tmp_path)


class TestCLI:
    def test_exit_ok(self, tmp_path, declared, capsys) -> None:
        tgz = _tgz(tmp_path, GOOD)
        assert gate.main(["--chart", "helm/demo", "--tgz", str(tgz)]) == gate.EXIT_OK
        assert "contents match" in capsys.readouterr().out

    def test_exit_violation(self, tmp_path, declared, capsys) -> None:
        tgz = _tgz(tmp_path, GOOD + ["demo/LICENSE"])
        assert (
            gate.main(["--chart", "helm/demo", "--tgz", str(tgz)])
            == gate.EXIT_VIOLATION
        )
        assert "LICENSE" in capsys.readouterr().err

    def test_exit_caller_error(self, tmp_path, declared, capsys) -> None:
        assert (
            gate.main(["--chart", "helm/demo", "--tgz", str(tmp_path / "gone.tgz")])
            == gate.EXIT_CALLER_ERROR
        )
        assert "could not run" in capsys.readouterr().err

    def test_trailing_slash_on_chart_is_tolerated(self, tmp_path, declared) -> None:
        """Call sites spell it `helm/demo`; a stray slash must not read as a
        different, unregistered chart."""
        tgz = _tgz(tmp_path, GOOD)
        assert gate.main(["--chart", "helm/demo/", "--tgz", str(tgz)]) == gate.EXIT_OK


class TestAgainstRealHelm:
    """⛔ No skip. Both branches assert — 'could not measure' and 'measured and
    found nothing' must stay distinguishable, and a silently-skipped test is
    indistinguishable from a passing one.

    helm is present in the dev container (devcontainer.json:
    kubectl-helm-minikube) and in CI's `python-tests-run` job
    (azure/setup-helm@v4), so the green branch is the one that runs where it
    matters.
    """

    @pytest.mark.parametrize(
        "chart",
        ["helm/threshold-exporter", "helm/recipe-preview", "helm/tenant-api"],
    )
    def test_live_chart(self, chart: str) -> None:
        if shutil.which("helm") is None:
            assert gate.main(["--chart", chart]) == gate.EXIT_CALLER_ERROR
        else:
            assert gate.main(["--chart", chart]) == gate.EXIT_OK

    def test_the_two_tables_cover_the_same_charts(self) -> None:
        """DECLARED and DECLARED_SUBTREES must not drift apart: a chart in one
        and not the other is an unrunnable check, not a lenient one."""
        from check_chart_ship_surface import DECLARED  # noqa: PLC0415

        assert set(DECLARED) == set(gate.DECLARED_SUBTREES)
