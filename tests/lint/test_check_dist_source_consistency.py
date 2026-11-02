"""check_dist_source_consistency.py — the esbuild-bump rebuild signal (#3521).

The hook blocks a commit that stages portal dist with no source-of-rebuild
signal. A compiler bump re-emits every bundle with no source change, so the
lockfile counts as a signal — but ONLY when the esbuild version it resolves
moved. These tests pin both halves: the bump is accepted, and any other
lockfile change (or an unreadable one) is not, so the original TRK-239 guard
keeps its teeth.

Pure functions only: the staged-mode wiring reads HEAD/index through git and
is exercised by the hook itself on every commit.
"""
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _hook():
    """Import the hook by path (scripts/ is not an importable package)."""
    path = REPO_ROOT / "scripts" / "tools" / "lint" / "check_dist_source_consistency.py"
    spec = importlib.util.spec_from_file_location("_dist_consistency_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lock(esbuild=None, **others):
    packages = {"": {"name": "portal"}}
    if esbuild is not None:
        packages["node_modules/esbuild"] = {"version": esbuild}
    for name, version in others.items():
        packages[f"node_modules/{name}"] = {"version": version}
    return json.dumps({"lockfileVersion": 3, "packages": packages})


def test_esbuild_version_is_read_from_the_lockfile():
    assert _hook().esbuild_version(_lock("0.28.2")) == "0.28.2"


def test_unreadable_or_esbuild_less_lockfile_has_no_version():
    hook = _hook()
    assert hook.esbuild_version("") is None
    assert hook.esbuild_version("not json") is None
    assert hook.esbuild_version("[]") is None
    assert hook.esbuild_version(_lock(None, vitest="5.0.5")) is None


def test_an_esbuild_bump_is_a_rebuild_signal():
    assert _hook().esbuild_bumped(_lock("0.24.2"), _lock("0.28.2"))


def test_a_lockfile_change_that_leaves_esbuild_alone_is_not():
    # The #3513 shape: npm audit fix moved transitive deps, esbuild stayed put.
    old = _lock("0.24.2", postcss="8.5.14", ws="8.20.0")
    new = _lock("0.24.2", postcss="8.5.32", ws="8.22.1")
    assert not _hook().esbuild_bumped(old, new)


def test_a_missing_side_is_not_evidence_of_a_bump():
    hook = _hook()
    assert not hook.esbuild_bumped("", _lock("0.28.2"))
    assert not hook.esbuild_bumped(_lock("0.24.2"), "")
    assert not hook.esbuild_bumped(_lock(None), _lock("0.28.2"))


def test_explicit_dist_only_files_are_still_blocked(capsys, monkeypatch):
    # Explicit-file mode has no HEAD/index pair, so the lockfile alone can
    # never vouch for dist there: the original guard is unchanged.
    hook = _hook()
    monkeypatch.delenv("BYPASS_DIST_CHECK", raising=False)
    # hook.LOCKFILE, not a literal: the file is only passed as an argument,
    # never opened, so it must not read as a pytest input to the CI path
    # filter guard (test_ci_path_filter_coverage).
    monkeypatch.setattr("sys.argv", ["hook", "docs/assets/dist/wizard.js", hook.LOCKFILE])
    assert hook.main() == hook.EXIT_VIOLATION
    assert "without any source-of-rebuild signal" in capsys.readouterr().out
