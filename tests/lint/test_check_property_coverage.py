"""Tests for scripts/tools/lint/check_property_coverage.py.

Process-enforcement lint that ratchets the property + mutation pilot
(see Gap 2 of testing-quality memory roadmap closure). Without this
lint, new pure helpers added to in-scope modules wouldn't get
flagged for property tests; the pilot's 31-function snapshot would
silently drift.

Coverage:
  - _module_top_level_functions: top-level def, class methods as
    Class.method, dunder skip, public + _private helpers
  - validate_manifest: clean / missing-source / bad-scope shapes /
    bad-covered / bad-excluded / missing-triage / stale-manifest /
    no-test-ref / no-reason / bare method-name acceptance for
    Class.method covered claims
  - main CLI: missing manifest / bad YAML / valid run / drift
    detection / --json shape / repo smoke
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_property_coverage.py"

_spec = importlib.util.spec_from_file_location("check_property_coverage", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_property_coverage"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# Helpers
# ============================================================


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


# ============================================================
# _module_top_level_functions — AST walker
# ============================================================


class TestModuleTopLevelFunctions:

    def test_top_level_def(self, tmp_path):
        f = tmp_path / "m.py"
        _write(f, """
            def foo(): pass
            def bar(): pass
        """)
        assert sorted(mod._module_top_level_functions(f)) == ["bar", "foo"]

    def test_class_methods_use_dotted_form(self, tmp_path):
        f = tmp_path / "m.py"
        _write(f, """
            class Dispatcher:
                def resolve(self): pass
                def dispatch(self): pass
        """)
        names = mod._module_top_level_functions(f)
        assert sorted(names) == ["Dispatcher.dispatch", "Dispatcher.resolve"]

    def test_dunder_methods_excluded(self, tmp_path):
        f = tmp_path / "m.py"
        _write(f, """
            class Version:
                def __init__(self): pass
                def __lt__(self, other): pass
                def compare(self, other): pass
        """)
        names = mod._module_top_level_functions(f)
        assert names == ["Version.compare"]

    def test_public_and_private_helpers_both_included(self, tmp_path):
        # Property: leading-underscore helpers (`_audience_str` style)
        # are in scope by codebase convention.
        f = tmp_path / "m.py"
        _write(f, """
            def public_fn(): pass
            def _private_helper(): pass
            def _audience_str(audience_list): pass
        """)
        names = mod._module_top_level_functions(f)
        assert sorted(names) == ["_audience_str", "_private_helper", "public_fn"]

    def test_async_def_recognized(self, tmp_path):
        f = tmp_path / "m.py"
        _write(f, """
            async def fetch_url(url): pass
            def sync_helper(): pass
        """)
        names = mod._module_top_level_functions(f)
        assert sorted(names) == ["fetch_url", "sync_helper"]

    def test_top_level_assignments_skipped(self, tmp_path):
        # Property: only `def` constructs count, not constants /
        # imports / dataclass field defaults.
        f = tmp_path / "m.py"
        _write(f, """
            from dataclasses import dataclass

            CONSTANT = 42

            @dataclass
            class Config:
                threshold: int = 100

            def real_function(): pass
        """)
        names = mod._module_top_level_functions(f)
        assert names == ["real_function"]

    def test_syntax_error_raises_value_error(self, tmp_path):
        f = tmp_path / "m.py"
        _write(f, "def foo(:\n    bad syntax\n")
        with pytest.raises(ValueError, match="cannot parse"):
            mod._module_top_level_functions(f)


# ============================================================
# validate_manifest — top-level orchestration
# ============================================================


class TestValidateManifest:

    @pytest.fixture
    def fake_repo(self, tmp_path):
        """Create a tmp repo with a sample source module + test file."""
        src = tmp_path / "scripts" / "tools" / "_lib_demo.py"
        _write(src, """
            def covered_fn(x):
                return x + 1

            def excluded_fn(path):
                with open(path) as f:
                    return f.read()
        """)
        test = tmp_path / "tests" / "shared" / "test_property_tools.py"
        _write(test, """
            def test_covered_fn():
                # references covered_fn name
                pass
        """)
        return tmp_path, src, test

    def test_clean_manifest(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn"],
                    "excluded": {"excluded_fn": "I/O-bound"},
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        assert issues == []

    def test_missing_source_flagged(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/nonexistent.py": {"covered": [], "excluded": {}},
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        assert len(issues) == 1
        assert issues[0].kind == "missing-source"

    def test_untriaged_function_flagged(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn"],
                    # excluded_fn deliberately missing
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        triage_issues = [i for i in issues if i.kind == "missing-triage"]
        assert len(triage_issues) == 1
        assert "excluded_fn" in triage_issues[0].detail

    def test_stale_covered_entry_flagged(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn", "ghost_fn"],
                    "excluded": {"excluded_fn": "I/O"},
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        stale = [i for i in issues if i.kind == "stale-manifest"]
        assert len(stale) == 1
        assert "ghost_fn" in stale[0].detail

    def test_no_test_ref_flagged(self, fake_repo):
        # Property: covered:foo but foo never appears in test file.
        repo, src, test = fake_repo
        # Test file references covered_fn but NOT excluded_fn — so
        # if we list excluded_fn under `covered:`, it should fail no-test-ref.
        # Build a fresh source where excluded_fn isn't in test.
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn", "excluded_fn"],  # both claimed
                    # No excluded section: must triage every function
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        no_ref = [i for i in issues if i.kind == "no-test-ref"]
        assert len(no_ref) == 1
        assert "excluded_fn" in no_ref[0].detail

    def test_empty_excluded_reason_flagged(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn"],
                    "excluded": {"excluded_fn": ""},  # empty reason
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        no_reason = [i for i in issues if i.kind == "no-reason"]
        assert len(no_reason) == 1
        assert "excluded_fn" in no_reason[0].detail

    def test_non_string_reason_flagged(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["covered_fn"],
                    "excluded": {"excluded_fn": None},  # null reason
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        no_reason = [i for i in issues if i.kind == "no-reason"]
        assert len(no_reason) == 1

    def test_bad_modules_top_level(self, tmp_path):
        repo = tmp_path
        manifest = {"modules": ["should-be-mapping"]}
        issues = mod.validate_manifest(manifest, repo, tmp_path / "x")
        assert len(issues) == 1
        assert issues[0].kind == "bad-manifest"

    def test_bad_covered_type(self, fake_repo):
        repo, _, test = fake_repo
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": "should-be-list",
                    "excluded": {},
                },
            },
        }
        issues = mod.validate_manifest(manifest, repo, test)
        assert any(i.kind == "bad-covered" for i in issues)

    def test_class_method_dotted_form_accepted(self, tmp_path):
        # Property: covered:Class.method must accept a test ref to
        # bare `method` (test code calls `instance.method(...)`).
        src = tmp_path / "scripts" / "tools" / "_lib_demo.py"
        _write(src, """
            class Dispatcher:
                def resolve(self):
                    pass
        """)
        test = tmp_path / "tests" / "shared" / "test_property_tools.py"
        _write(test, """
            def test_resolve_property():
                d = make_dispatcher()
                d.resolve()  # bare 'resolve' reference
        """)
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["Dispatcher.resolve"],
                },
            },
        }
        issues = mod.validate_manifest(manifest, tmp_path, test)
        assert issues == []

    def test_class_method_no_test_ref_flagged(self, tmp_path):
        # Property: if NEITHER `Class.method` NOR bare `method` appears
        # in the test file, no-test-ref still fires.
        src = tmp_path / "scripts" / "tools" / "_lib_demo.py"
        _write(src, """
            class Dispatcher:
                def untested_method(self):
                    pass
        """)
        test = tmp_path / "tests" / "shared" / "test_property_tools.py"
        _write(test, "# no references here\n")
        manifest = {
            "modules": {
                "scripts/tools/_lib_demo.py": {
                    "covered": ["Dispatcher.untested_method"],
                },
            },
        }
        issues = mod.validate_manifest(manifest, tmp_path, test)
        no_ref = [i for i in issues if i.kind == "no-test-ref"]
        assert len(no_ref) == 1


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    def test_missing_manifest_exits_two(self, tmp_path, capsys):
        rc = mod.main([
            "--manifest", str(tmp_path / "nope.yaml"),
            "--test-file", str(tmp_path / "no.py"),
        ])
        assert rc == 2
        assert "ERROR" in capsys.readouterr().err

    def test_clean_manifest_exits_zero(self, tmp_path, capsys, monkeypatch):
        # Build a fake repo with manifest + clean source + test
        manifest_path = tmp_path / "manifest.yaml"
        src_path = tmp_path / "scripts" / "tools" / "_lib_demo.py"
        test_path = tmp_path / "tests" / "shared" / "test_property_tools.py"
        _write(src_path, "def foo(): pass\n")
        _write(test_path, "# foo is referenced here in code-comment form\ndef test_foo(): pass\n")
        manifest_path.write_text(
            "modules:\n"
            "  scripts/tools/_lib_demo.py:\n"
            "    covered:\n"
            "      - foo\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        rc = mod.main([
            "--manifest", str(manifest_path),
            "--test-file", str(test_path),
        ])
        assert rc == 0

    def test_drift_exits_one(self, tmp_path, capsys, monkeypatch):
        manifest_path = tmp_path / "manifest.yaml"
        src_path = tmp_path / "scripts" / "tools" / "_lib_demo.py"
        test_path = tmp_path / "tests" / "shared" / "test_property_tools.py"
        _write(src_path, "def foo(): pass\ndef untriaged_bar(): pass\n")
        _write(test_path, "def test_foo(): pass\n")
        manifest_path.write_text(
            "modules:\n"
            "  scripts/tools/_lib_demo.py:\n"
            "    covered:\n"
            "      - foo\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        rc = mod.main([
            "--manifest", str(manifest_path),
            "--test-file", str(test_path),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "drift detected" in err
        assert "untriaged_bar" in err

    def test_json_shape(self, tmp_path, capsys, monkeypatch):
        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text(
            "modules:\n"
            "  scripts/tools/nonexistent.py:\n"
            "    covered: []\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        rc = mod.main([
            "--manifest", str(manifest_path),
            "--test-file", str(tmp_path / "no.py"),
            "--json",
        ])
        assert rc == 1  # missing source = 1 issue
        payload = json.loads(capsys.readouterr().out)
        assert payload["check"] == "property-coverage"
        assert "modules_scanned" in payload
        assert "issues" in payload
        assert "summary" in payload
        assert payload["summary"]["errors"] >= 1

    def test_malformed_yaml_exits_two(self, tmp_path, capsys):
        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text("not: [yaml\n", encoding="utf-8")
        rc = mod.main([
            "--manifest", str(manifest_path),
            "--test-file", str(tmp_path / "no.py"),
        ])
        assert rc == 2
        assert "cannot parse" in capsys.readouterr().err


# ============================================================
# Repo-level smoke regression guard
# ============================================================


class TestRepoSmoke:

    def test_repo_manifest_passes(self):
        """The shipped tests/shared/property-coverage.yaml must validate
        against the actual repo source + test files. Belt-and-suspenders
        alongside the pre-commit hook: any drift introduced by adding a
        new function (or removing one) trips this test locally before
        pre-commit fires.
        """
        rc = mod.main([])
        assert rc == 0, "shipped property-coverage.yaml has drift"
