"""Tests for scripts/tools/lint/validate_docs_versions.py.

Gap 4 (TRK-007 backlog) — fifth lint self-test, the **P0** entry. The
file is 1109 LOC of mixed concerns (~25 check_* functions); rather
than chase 100% coverage, this PR focuses on the load-bearing
public surface:

  - The Issue dataclass shape (used by every check_* return path)
  - The parser-style helpers (read_source_versions, count_rule_packs,
    count_bilingual_pairs) — these define what the checks compare
    against, so a bug here cascades into every check_*
  - The shared IO helpers (_scan_file, _cached_rglob,
    _collect_scannable_files, _read_cached) — caching layer that
    every check_* relies on
  - Two representative check_* functions (check_da_tools_version,
    check_exporter_version) — covers the common pattern: "expected"
    arg + scan files + Issue list
  - main CLI smoke (--ci exit semantics, --json shape, repo regression)

Future PRs can extend this with the other 20+ check_* functions if
the validate_docs_versions decomp ever happens. For now the goal is
just "the load-bearing primitives are pinned" so any future regex /
cache regression is caught.

Pattern matches the prior Gap 4 PRs (#334, #335, #336, #337):
importlib.util loader + tmp_path + monkeypatch on module-level
constants.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "validate_docs_versions.py"

# The script imports `from _version_patterns import ...` — add the lint
# dir to sys.path so the import resolves when we exec via importlib.
_LINT_DIR = str(REPO_ROOT / "scripts" / "tools" / "lint")
if _LINT_DIR not in sys.path:
    sys.path.insert(0, _LINT_DIR)

# #1511: the tool-file predicate is shared with both writers now; see
# tests/shared/test_lib_toolcount.py for the synthetic-tree assertions.
_TOOLS_DIR = str(REPO_ROOT / "scripts" / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import _lib_toolcount as _toolcount  # noqa: E402

_spec = importlib.util.spec_from_file_location("validate_docs_versions", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["validate_docs_versions"] = mod
_spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level caches between tests so each test sees a clean state."""
    mod._FILE_CACHE.clear()
    mod._CONTENT_CACHE.clear()
    mod._RGLOB_CACHE.clear()
    yield
    mod._FILE_CACHE.clear()
    mod._CONTENT_CACHE.clear()
    mod._RGLOB_CACHE.clear()


def _unlisted_tool_subdirs(tools_dir: Path, listed) -> list:
    """Subdirectories that hold tools but are not in the counted scope.

    A directory carrying an `__init__.py` is a package — library code the
    "N 個 Python 工具" count has never included — so it is not reported.
    """
    out = []
    for sub in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
        if sub.name in listed or sub.name == "__pycache__":
            continue
        if (sub / "__init__.py").exists():
            continue
        if any(not any(f.name.startswith(p)
                       for p in _toolcount.TOOL_SKIP_PREFIXES)
               for f in sub.glob("*.py")):
            out.append(sub.name)
    return out


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ============================================================
# Issue dataclass
# ============================================================


class TestIssue:

    def test_basic_fields(self):
        i = mod.Issue("my-check", "error", "x.md", 42, "boom")
        assert i.check == "my-check"
        assert i.severity == "error"
        assert i.file == "x.md"
        assert i.line == 42
        assert i.message == "boom"

    def test_to_dict_round_trip(self):
        i = mod.Issue("c", "warn", "f", 1, "m")
        d = i.to_dict()
        assert d == {
            "check": "c",
            "severity": "warn",
            "file": "f",
            "line": 1,
            "message": "m",
        }

    def test_to_dict_is_json_serializable(self):
        # Property: every Issue can be json.dumps'd without conversion.
        i = mod.Issue("c", "error", "f.md", 10, "中文 message")
        s = json.dumps(i.to_dict(), ensure_ascii=False)
        assert "中文 message" in s


# ============================================================
# _scan_file
# ============================================================


class TestScanFile:

    def test_returns_empty_for_missing_file(self, tmp_path):
        # Property: nonexistent path → empty list (not raise).
        assert mod._scan_file(tmp_path / "nope.txt", r".*") == []

    def test_finds_matches_with_line_numbers(self, tmp_path):
        f = tmp_path / "x.txt"
        _write(f, "no match\nfound here v1.2.3 yes\nplain\nv2.5.0 too\n")
        result = mod._scan_file(f, r"v\d+\.\d+\.\d+")
        # Two lines have version-like patterns.
        assert len(result) == 2
        # (line_num, stripped_line)
        assert result[0][0] == 2
        assert "v1.2.3" in result[0][1]
        assert result[1][0] == 4
        assert "v2.5.0" in result[1][1]

    def test_no_matches_returns_empty(self, tmp_path):
        f = tmp_path / "x.txt"
        _write(f, "no version refs\nplain text\n")
        assert mod._scan_file(f, r"v\d+\.\d+\.\d+") == []

    def test_strips_line_whitespace(self, tmp_path):
        f = tmp_path / "x.txt"
        _write(f, "    indented v1.0.0    \n")
        result = mod._scan_file(f, r"v\d+\.\d+\.\d+")
        assert result[0][1] == "indented v1.0.0"


# ============================================================
# _cached_rglob — cache behavior
# ============================================================


class TestCachedRglob:

    def test_returns_matching_files(self, tmp_path):
        _write(tmp_path / "a.md", "")
        _write(tmp_path / "b.md", "")
        _write(tmp_path / "c.txt", "")
        result = mod._cached_rglob(tmp_path, "*.md")
        names = sorted(p.name for p in result)
        assert names == ["a.md", "b.md"]

    def test_caches_repeated_calls(self, tmp_path):
        # Property: second call doesn't re-walk the FS — it returns the
        # exact same list object from the cache.
        _write(tmp_path / "a.md", "")
        first = mod._cached_rglob(tmp_path, "*.md")
        # Add another file AFTER the first call.
        _write(tmp_path / "b.md", "")
        second = mod._cached_rglob(tmp_path, "*.md")
        # Second call returns the cached result (b.md not seen).
        assert first == second
        assert len(second) == 1


# ============================================================
# _read_cached
# ============================================================


class TestReadCached:

    def test_returns_file_content(self, tmp_path):
        f = tmp_path / "x.md"
        _write(f, "hello world")
        assert mod._read_cached(f) == "hello world"

    def test_caches_repeated_reads(self, tmp_path):
        # Property: cache means a mutation between reads is invisible
        # within the same run.
        f = tmp_path / "x.md"
        _write(f, "first")
        first = mod._read_cached(f)
        _write(f, "SECOND")
        second = mod._read_cached(f)
        assert first == second == "first"  # cached value wins


# ============================================================
# _collect_scannable_files
# ============================================================


class TestCollectScannableFiles:

    def test_finds_md_in_docs(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "guide.md", "")
        _write(docs / "guide.jsx", "")
        # Patch all pertinent module constants.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        # The cache key includes the extensions tuple — clear so monkeypatch
        # takes effect.
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()

        result = mod._collect_scannable_files(extensions=(".md",), include_ci=False)
        names = sorted(p.name for p in result)
        assert "guide.md" in names

    def test_include_ci_false_excludes_workflows(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        gh = tmp_path / ".github"
        gh.mkdir()
        _write(gh / "workflow.yaml", "")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()

        result = mod._collect_scannable_files(
            extensions=(".md",), include_ci=False)
        # workflow.yaml MUST NOT appear when include_ci=False.
        assert all(p.name != "workflow.yaml" for p in result)

    def test_root_files_added_when_present(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(tmp_path / "README.md", "")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ("README.md", "missing.md"))
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()

        result = mod._collect_scannable_files(
            extensions=(".md",), include_ci=False)
        names = [p.name for p in result]
        assert "README.md" in names
        # Missing files are silently skipped.
        assert "missing.md" not in names


# ============================================================
# read_source_versions
# ============================================================


class TestReadSourceVersions:

    def test_all_three_sources_present(self, tmp_path, monkeypatch):
        # Property: when all three SOT files exist with valid content,
        # all three keys appear in the result.
        # da-tools VERSION
        ver_file = tmp_path / "VERSION"
        _write(ver_file, "2.7.0")
        # Chart.yaml
        chart = tmp_path / "Chart.yaml"
        _write(chart, 'apiVersion: v2\nappVersion: "2.5.0"\n')
        # CLAUDE.md
        claude = tmp_path / "CLAUDE.md"
        _write(claude,
            "# CLAUDE.md\n\n"
            "## 專案概覽 (v2.7.0)\n\n"
            "Body...\n"
        )
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", ver_file)
        monkeypatch.setattr(mod, "CHART_YAML", chart)
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)

        result = mod.read_source_versions()
        assert result.get("tools") == "2.7.0"
        assert result.get("exporter") == "2.5.0"
        assert "platform" in result

    def test_missing_files_silently_skipped(self, tmp_path, monkeypatch):
        # Property: missing SOT files don't raise — they just don't
        # contribute their key to the dict.
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "no")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "no2")
        monkeypatch.setattr(mod, "CLAUDE_MD", tmp_path / "no3")

        result = mod.read_source_versions()
        assert result == {}

    def test_invalid_version_string_skipped(self, tmp_path, monkeypatch):
        # Property: a VERSION file whose content doesn't match the
        # numeric pattern is dropped.
        ver_file = tmp_path / "VERSION"
        _write(ver_file, "not-a-version")
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", ver_file)
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "no")
        monkeypatch.setattr(mod, "CLAUDE_MD", tmp_path / "no")

        result = mod.read_source_versions()
        assert "tools" not in result


# ============================================================
# count_rule_packs
# ============================================================


class TestCountRulePacks:

    def test_recording_and_alert_separation(self, tmp_path, monkeypatch):
        rule_packs = tmp_path / "rule-packs"
        rule_packs.mkdir()
        _write(rule_packs / "rule-pack-db.yaml",
            "groups:\n"
            "  - name: db-rec\n"
            "    rules:\n"
            "      - record: db_metric:rate\n"
            "        expr: rate(x[5m])\n"
            "      - record: db_metric:max\n"
            "        expr: max_over_time(x[5m])\n"
            "  - name: db-alert\n"
            "    rules:\n"
            "      - alert: HighLatency\n"
            "        expr: x > 100\n"
        )
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", rule_packs)
        monkeypatch.setattr(mod, "K8S_RULES_DIR", tmp_path / "no-k8s")

        counts = mod.count_rule_packs()
        assert counts["pack_count"] == 1
        assert counts["recording"] == 2
        assert counts["alert"] == 1
        assert counts["total"] == 3
        assert "db" in counts["per_pack"]

    def test_k8s_configmap_merged(self, tmp_path, monkeypatch):
        # Property: when both rule-packs/ source AND k8s ConfigMap
        # have rules for the same pack, the larger count wins per
        # category (handles partial-shipping packs).
        rule_packs = tmp_path / "rule-packs"
        k8s = tmp_path / "k8s"
        rule_packs.mkdir()
        k8s.mkdir()
        # Source has 1 alert
        _write(rule_packs / "rule-pack-db.yaml",
            "groups:\n  - name: db\n    rules:\n"
            "      - alert: A1\n        expr: x > 1\n"
        )
        # ConfigMap has 3 alerts (more than source)
        _write(k8s / "configmap-rules-db.yaml",
            "kind: ConfigMap\n"
            "metadata: { name: rules-db }\n"
            "data:\n"
            "  rules.yaml: |\n"
            "    groups:\n"
            "      - name: db\n"
            "        rules:\n"
            "          - alert: A1\n"
            "            expr: x > 1\n"
            "          - alert: A2\n"
            "            expr: x > 2\n"
            "          - alert: A3\n"
            "            expr: x > 3\n"
        )
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", rule_packs)
        monkeypatch.setattr(mod, "K8S_RULES_DIR", k8s)

        counts = mod.count_rule_packs()
        # max(1, 3) = 3 alerts
        assert counts["per_pack"]["db"]["alert"] == 3

    def test_empty_dirs_yield_zero(self, tmp_path, monkeypatch):
        rule_packs = tmp_path / "rule-packs"
        k8s = tmp_path / "k8s"
        rule_packs.mkdir()
        k8s.mkdir()
        monkeypatch.setattr(mod, "RULE_PACKS_DIR", rule_packs)
        monkeypatch.setattr(mod, "K8S_RULES_DIR", k8s)

        counts = mod.count_rule_packs()
        assert counts["pack_count"] == 0
        assert counts["recording"] == 0
        assert counts["alert"] == 0
        assert counts["total"] == 0


# ============================================================
# count_bilingual_pairs
# ============================================================


class TestCountBilingualPairs:

    def test_zero_when_no_pairs(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        # No .en.md anywhere.
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        mod._RGLOB_CACHE.clear()
        assert mod.count_bilingual_pairs() == 0

    def test_counts_docs_and_rule_packs_and_root(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        rule_packs = tmp_path / "rule-packs"
        docs.mkdir()
        rule_packs.mkdir()
        # docs/
        _write(docs / "guide.en.md", "")
        _write(docs / "intro.en.md", "")
        # rule-packs/
        _write(rule_packs / "db.en.md", "")
        # root README
        _write(tmp_path / "README.en.md", "")
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        mod._RGLOB_CACHE.clear()
        assert mod.count_bilingual_pairs() == 4


# ============================================================
# check_da_tools_version (representative check_*)
# ============================================================


class TestCheckDaToolsVersion:

    def test_no_drift_yields_no_issues(self, tmp_path, monkeypatch):
        # Property: when every reference matches expected version, no issues.
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "guide.md",
            "Use `da-tools:v2.7.0` to run.\n"
            "Or `da-tools:2.7.0` (no v prefix).\n"
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()

        issues = mod.check_da_tools_version("2.7.0")
        assert issues == []

    def test_old_version_drift_flagged(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "guide.md",
            "Use `da-tools:v2.5.0` (old version).\n"
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()

        issues = mod.check_da_tools_version("2.7.0")
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "2.5.0" in issues[0].message
        assert "2.7.0" in issues[0].message

    def test_changelog_skipped(self, tmp_path, monkeypatch):
        # Property: CHANGELOG.md is in skip_names — historical refs
        # to old versions there are NOT flagged (rewriting them would
        # rewrite history).
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "CHANGELOG.md",
            "## v2.5.0\nUsed `da-tools:v2.5.0`.\n"
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()

        issues = mod.check_da_tools_version("2.7.0")
        assert issues == []


# ============================================================
# check_exporter_version (representative check_*)
# ============================================================


class TestCheckExporterVersion:

    def test_no_drift_yields_no_issues(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "guide.md",
            "Use `threshold-exporter:v2.5.0`.\n"
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()

        issues = mod.check_exporter_version("2.5.0")
        assert issues == []

    def test_drift_flagged(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "guide.md",
            "Use `threshold-exporter:v2.4.0` (old).\n"
        )
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()

        issues = mod.check_exporter_version("2.5.0")
        assert len(issues) == 1
        assert "2.4.0" in issues[0].message
        assert issues[0].severity == "error"


# ============================================================
# Repo-level smoke regression guard
# ============================================================


class TestRepoSmoke:

    def test_repo_actually_runs(self, monkeypatch):
        """The shipped repo must run validate_docs_versions to completion.

        We don't assert a specific exit code (the repo currently has
        legitimate warnings), only that the lint runs cleanly without
        crashing on the real files. Belt-and-suspenders alongside the
        pre-commit hook.
        """
        # The lint sets sys.exit(1) on errors; we accept both 0 and 1
        # but reject crashes / SystemExit(2+).
        monkeypatch.setattr(sys, "argv", ["validate_docs_versions"])
        # The default code path doesn't call sys.exit unless --ci is set.
        # Just running main() should complete without SystemExit on the
        # default path.
        try:
            mod.main()
        except SystemExit as e:
            # Acceptable codes: None / 0 / 1. Not 2+ (config error).
            assert e.code in (None, 0, 1), (
                f"unexpected exit code {e.code} from repo scan"
            )

    def test_repo_json_output_parseable(self, monkeypatch, capsys):
        """`--json` against the real repo must emit valid JSON with the
        expected top-level keys."""
        monkeypatch.setattr(sys, "argv", ["validate_docs_versions", "--json"])
        try:
            mod.main()
        except SystemExit:
            pass
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "source_of_truth" in payload
        assert "issues" in payload
        assert "summary" in payload
        assert "errors" in payload["summary"]
        assert "warnings" in payload["summary"]


class TestPlatformVersionSourceIsLoadBearing:
    """#1480 — losing the source of truth must be loud, not silent.

    (Surfaced during the #1447 sweep; it is maintainer-side, not one of
    that issue's six customer-path items, so it is tracked separately.)

    The platform version used to be read here by a local regex anchored on
    ``## 專案概覽 (vX.Y.Z)``.  `abe27478` (2026-04-09) rewrote that heading
    and moved the version to the line below; the regex stopped matching, and
    because both of its consumers were guarded by ``if "platform" in
    versions`` they simply stopped running.  The tool printed
    ``platform:  v???`` and exited 0 for five releases (v2.7.0..v2.9.1).
    """

    @staticmethod
    def _claude(tmp_path, body):
        path = tmp_path / "CLAUDE.md"
        path.write_bytes(body.encode("utf-8"))
        return path

    def test_reads_the_current_lead_in_line_spelling(self, tmp_path,
                                                     monkeypatch):
        claude = self._claude(tmp_path, (
            "# CLAUDE.md\n\n## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v3.4.5)** — ...\n"))
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert mod.read_source_versions().get("platform") == "3.4.5"

    def test_still_reads_the_historical_inline_spelling(self, tmp_path,
                                                        monkeypatch):
        """Both spellings resolve, so moving the version again is survivable."""
        claude = self._claude(tmp_path,
                              "# CLAUDE.md\n\n## 專案概覽 (v3.4.5)\n\nBody\n")
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert mod.read_source_versions().get("platform") == "3.4.5"

    def test_a_bare_version_below_the_heading_is_not_the_platform_version(
            self, tmp_path, monkeypatch):
        r"""⛔ Must-not-fire half of the two spellings above.

        The inline pattern originally joined the heading text to `(v` with
        `\s*`, and `\s` matches newlines — so a 'precise phrase' silently
        spanned any number of blank lines and read a stray `(v9.9.9)`
        further down the file as the platform version. The class is
        horizontal-only now. Nothing in this module caught it; the existing
        `check_frontmatter_versions` test did, once #1480 pointed both at
        the same reader.
        """
        claude = self._claude(
            tmp_path, "# CLAUDE.md\n\n## 專案概覽\n" + "\n" * 10 + "(v9.9.9)\n")
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert "platform" not in mod.read_source_versions()

    def test_a_stray_product_line_above_the_heading_does_not_win(
            self, tmp_path, monkeypatch):
        """⛔ The search is anchored at the heading, not at the top of file.

        An earlier revision searched the whole document because the product
        name plus `(v` "cannot be satisfied by an unrelated mention". A
        quoted release note above the heading satisfies it exactly, and the
        reader this replaced was anchored — so the unanchored version was a
        regression, justified by a property of that day's CLAUDE.md rather
        than of the pattern.
        """
        claude = self._claude(tmp_path, (
            "# CLAUDE.md\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v1.0.0)** — old release note\n\n"
            "## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v3.4.5)** — the real one\n"))
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert mod.read_source_versions().get("platform") == "3.4.5"

    def test_a_toc_entry_cannot_move_the_anchor(self, tmp_path, monkeypatch):
        """The anchor must be a heading line.

        The reader this replaced anchored on *any* line containing 專案概覽,
        so a table-of-contents entry hijacked it. Requiring `^#{1,6}` costs
        nothing and removes that whole family.
        """
        claude = self._claude(tmp_path, (
            "- [專案概覽](#overview)\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v1.0.0)** — stray\n\n"
            "## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v3.4.5)** — the real one\n"))
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert mod.read_source_versions().get("platform") == "3.4.5"

    def test_no_overview_heading_yields_no_platform_key(
            self, tmp_path, monkeypatch):
        """No heading to anchor on ⇒ unreadable ⇒ the fail-closed path."""
        claude = self._claude(tmp_path, (
            "# CLAUDE.md\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v1.0.0)** — stray\n"))
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert "platform" not in mod.read_source_versions()

    def test_reads_the_file_it_is_told_to_read(self, tmp_path, monkeypatch):
        """⛔ The injection seam itself.

        An earlier draft of this fix called the shared reader with no path
        argument, so it read the *real* CLAUDE.md no matter what a test
        injected — and the existing seam test kept passing for that reason
        while testing nothing. Asserting a version this repository does not
        have is what makes that impossible to repeat.
        """
        claude = self._claude(tmp_path, (
            "# CLAUDE.md\n\n## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v99.98.97)** — ...\n"))
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert mod.read_source_versions().get("platform") == "99.98.97"

    def test_absent_anchor_yields_no_platform_key(self, tmp_path, monkeypatch):
        claude = self._claude(tmp_path, "# CLAUDE.md\n\nNo overview heading.\n")
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", tmp_path / "absent")
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent2")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        assert "platform" not in mod.read_source_versions()

    def test_every_source_of_truth_fails_closed_not_just_the_platform_one(
            self, tmp_path, monkeypatch, capsys):
        """⛔ The generalisation itself, pinned.

        The first fix guarded only `platform`, because `platform` was the key
        the issue happened to name — while `if "tools" in versions` and
        `if "exporter" in versions` sat two lines away, unguarded. Measured
        then: one stray `v` in the da-tools VERSION file dropped the `tools`
        key and took 161 errors down to 0 at rc=0.

        ⚠️ Without this test the loop can be narrowed back to `["platform"]`
        and every other test still passes — measured by mutation, that edit
        SURVIVED. A guard that does not pin its own generality is one edit
        away from the bug it replaced.

        This is also the must-not-fire half: `platform` is readable here, so
        it must NOT report a source failure.
        """
        claude = self._claude(tmp_path, (
            "# CLAUDE.md\n\n## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v3.4.5)** — ...\n"))
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        # The shape that actually happens: a stray `v` the reader refuses.
        bad = tmp_path / "VERSION"
        bad.write_text("v9.9.9\n", encoding="utf-8")
        monkeypatch.setattr(mod, "DA_TOOLS_VERSION", bad)
        monkeypatch.setattr(mod, "CHART_YAML", tmp_path / "absent-chart.yaml")

        versions = mod.read_source_versions()
        assert "platform" in versions, versions
        assert "tools" not in versions, versions
        assert "exporter" not in versions, versions

        monkeypatch.setattr(sys, "argv",
                            ["validate_docs_versions", "--json", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        payload = json.loads(capsys.readouterr().out)
        raised = {i["check"] for i in payload["issues"]
                  if i["check"].endswith("-version-source")}
        assert "tools-version-source" in raised, raised
        assert "exporter-version-source" in raised, raised
        assert "platform-version-source" not in raised, (
            "platform was readable, so it must not report a source failure")

        # ⛔ Key presence alone does not pin the guarantee. It would still
        # hold if the dependent checks had ALSO run — against a guessed
        # version. What the guarantee says is "they did not run, and that
        # fact is loud", so assert the other half too. `da-tools-version`
        # alone was 130 of the 161 errors that silently became 0 before this
        # loop existed.
        exclusive = sorted(i["check"] for i in payload["issues"]
                           if i["check"] in ("da-tools-version",
                                             "release-tag-version",
                                             "exporter-version"))
        assert not exclusive, (
            "checks comparing against an unreadable source must not run at "
            "all; these did: %s" % exclusive)
        # `mkdocs-extra-version` is ONE check id covering all three keys, so
        # it legitimately still runs here — for `platform`, which is readable
        # in this fixture. ⚠️ Asserting it away wholesale would have been a
        # loosening dressed as a fix; assert instead that neither unreadable
        # key produced a row under it.
        mkdocs_rows = [i["message"] for i in payload["issues"]
                       if i["check"] == "mkdocs-extra-version"]
        assert not [m for m in mkdocs_rows
                    if "tools_version" in m or "exporter_version" in m], (
            "mkdocs rows were emitted for an unreadable source: %s"
            % mkdocs_rows)
        assert payload["summary"]["errors"] >= len(raised), payload["summary"]
        # ⚠️ Deliberately no `errors == baseline + len(raised)`: losing a
        # source also REMOVES whatever its dependants were reporting, so the
        # delta is not a constant. The sibling test below pins the exit-code
        # causation differentially; this one pins the skip-vs-report half.
        assert exc.value.code != 0, (
            "an unreadable source of truth must not exit 0 under --ci")

    @staticmethod
    def _run_json(monkeypatch, capsys, ci=True):
        """Run main() with --json, return (payload, exit_code_or_None)."""
        argv = ["validate_docs_versions", "--json"] + (["--ci"] if ci else [])
        monkeypatch.setattr(sys, "argv", argv)
        code = None
        try:
            mod.main()
        except SystemExit as exc:
            code = exc.value if not isinstance(exc, SystemExit) else exc.code
        return json.loads(capsys.readouterr().out), code

    def test_unreadable_source_is_an_error_not_a_skip(self, tmp_path,
                                                      monkeypatch, capsys):
        """The whole point: no platform version ⇒ an ERROR, not a silent skip.

        ⛔ The exit-code half is asserted **differentially**, against a control
        run on the same tree with a readable CLAUDE.md. A bare
        `assert exit_code != 0` would pass whenever the repository happens to
        hold any unrelated error, so it proves nothing about this issue —
        raised by CodeRabbit on #1493, and correct. Subtracting the control
        cancels whatever unrelated errors exist, in either direction: this
        stays honest on a dirty tree and does not go red because of one.
        """
        # Control first: the real CLAUDE.md, same tree, same checks.
        before, _ = self._run_json(monkeypatch, capsys)
        assert not [i for i in before["issues"]
                    if i["check"].endswith("-version-source")], before["summary"]
        baseline_errors = before["summary"]["errors"]

        claude = self._claude(tmp_path, "# CLAUDE.md\n\nNo overview heading.\n")
        monkeypatch.setattr(mod, "CLAUDE_MD", claude)
        after, code = self._run_json(monkeypatch, capsys)

        sources = [i for i in after["issues"]
                   if i["check"] == "platform-version-source"]
        assert sources, "no platform-version-source issue was raised"
        assert sources[0]["severity"] == "error"
        # The delta is exactly the source failures — the dependent checks did
        # not quietly contribute (or suppress) anything else.
        assert after["summary"]["errors"] == baseline_errors + len(sources), (
            "errors moved by something other than the source failure: "
            "%s -> %s" % (baseline_errors, after["summary"]["errors"]))
        assert code not in (0, None), (
            "--ci must exit non-zero once the source of truth is unreadable")

    def test_readable_source_raises_no_such_error(self, monkeypatch, capsys):
        """⛔ Must-tolerate control.

        A check that only ever fires cannot be told apart from one wired to
        fire unconditionally; this runs against the real CLAUDE.md, which
        does carry a platform version.
        """
        monkeypatch.setattr(sys, "argv", ["validate_docs_versions", "--json"])
        try:
            mod.main()
        except SystemExit:
            pass
        payload = json.loads(capsys.readouterr().out)
        assert payload["source_of_truth"]["platform"], (
            "the real CLAUDE.md should yield a platform version")
        assert not [i for i in payload["issues"]
                    if i["check"] == "platform-version-source"]


# ============================================================
# check_release_tag_currency (TB-F1 class — release-tag forms)
# ============================================================


class TestNeitherHalfOfTheVersionCheckCanBeSwitchedOffInSilence:
    """#1484 — `check_e2e_and_jsx_versions` is two halves and both went quiet.

    The e2e half is the one that caught #1480's real damage (`package.json`
    frozen at the version the gate died on), and six input shapes switched
    it off. The JSX half scanned `docs/interactive/tools`, a directory that
    stopped existing on 2026-05-07 (`b439c427`), so it checked nothing at
    all for three and a half months while one file sat at 2.7.0.

    ⚠️ NOT "the only check that ever caught drift" — an earlier draft of this
    class said that and it is false: on the merge-base tree `bilingual-count`,
    `tool-count` and `bilingual-numbers` were between them reporting six real
    drifts. The accurate claim is narrower: of the two checks #1480 brought
    back to life, this is the one that caught damage.
    """

    @staticmethod
    def _tree(tmp_path, body=None, jsx=None):
        (tmp_path / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs").mkdir(exist_ok=True)
        if body is not None:
            (tmp_path / "tests" / "e2e" / "package.json").write_text(
                body, encoding="utf-8")
        src = tmp_path / "tools" / "portal" / "src"
        src.mkdir(parents=True, exist_ok=True)
        if jsx is not None:
            (src / "probe.jsx").write_text(jsx, encoding="utf-8")
        return tmp_path

    def _run(self, tmp_path, monkeypatch, **kw):
        monkeypatch.setattr(mod, "REPO_ROOT", self._tree(tmp_path, **kw))
        monkeypatch.setattr(mod, "DOCS_DIR", tmp_path / "docs")
        return mod.check_e2e_and_jsx_versions("2.9.0")

    # ---- e2e half -------------------------------------------------------
    @pytest.mark.parametrize("body,label", [
        ('{"name": "e2e"}', "version key absent"),
        ('{"version": ""}', "empty string"),
        ('{"version": null}', "null version"),
        ('{"version": 290}', "not a string"),
        ('{"version": "2.6.0",}', "malformed JSON"),
        ('null', "the whole document is null"),
        ('[]', "top level is an array"),
        ('"2.9.0"', "top level is a string"),
    ])
    def test_an_unusable_package_json_is_an_error_not_a_pass(
            self, tmp_path, monkeypatch, body, label):
        """⛔ Every unusable shape names the file and errors.

        ⚠️ `null`, `[]` and `"2.9.0"` were added after blind review: the
        first fix used `pkg = None` both as the "not parsed yet" sentinel
        and as a parse result, so a file containing exactly `null` sailed
        through — a hole introduced by the fix for this very issue.
        """
        issues = self._run(tmp_path, monkeypatch, body=body)
        # ⛔ Select by check id, not `issues[0]`. Indexing only works while
        # the e2e block happens to append before the JSX block; this pins
        # the behaviour the test describes rather than that ordering.
        e2e = [i for i in issues if i.check == "e2e-package-version"]
        assert e2e, "%s produced no e2e issue at all (got %r)" % (
            label, [i.check for i in issues])
        assert e2e[0].severity == "error", label
        assert "package.json" in e2e[0].file, e2e[0].file

    def test_an_absent_package_json_is_an_error_not_a_pass(
            self, tmp_path, monkeypatch):
        """⛔ `if e2e_pkg.exists():` with no else is the quietest hole of all.

        Renaming or moving the file reproduces #1480's damage exactly, with
        even less trace than the original.
        """
        issues = self._run(tmp_path, monkeypatch, body=None)
        assert issues, "an absent package.json produced no issue"
        assert issues[0].severity == "error"
        assert "package.json" in issues[0].file

    def test_a_correct_version_passes_in_silence(self, tmp_path, monkeypatch):
        """⛔ Must-not-fire. A check that only ever fires is not a check."""
        assert self._run(tmp_path, monkeypatch,
                         body='{"version": "2.9.0"}') == []

    def test_real_drift_is_still_reported(self, tmp_path, monkeypatch):
        """The behaviour #1480 measured: frozen at the version it died on.

        ⚠️ Asserts the two versions appear, not how they are punctuated.
        Blind review reworded the message without changing its meaning and
        turned an earlier version of this test red under the name "real
        drift is still caught" — while the drift was, in fact, still caught.
        """
        issues = self._run(tmp_path, monkeypatch, body='{"version": "2.6.0"}')
        assert len(issues) == 1, issues
        assert "2.6.0" in issues[0].message and "2.9.0" in issues[0].message

    # ---- JSX half -------------------------------------------------------
    def test_jsx_drift_is_reported_as_an_error(self, tmp_path, monkeypatch):
        """⛔ `error`, not `warn`: as a warning it could never fail `--ci`."""
        issues = self._run(
            tmp_path, monkeypatch, body='{"version": "2.9.0"}',
            jsx="---\ntitle: probe\nversion: v2.7.0\n---\nbody\n")
        assert len(issues) == 1, issues
        assert issues[0].check == "jsx-frontmatter-version"
        assert issues[0].severity == "error"
        assert "2.7.0" in issues[0].message

    def test_a_missing_jsx_tree_is_an_error_not_a_skip(
            self, tmp_path, monkeypatch):
        """⛔ How this half died: the sources moved and the check went quiet.

        `docs/interactive/tools` stopped existing on 2026-05-07 and the
        `if ...exists():` turned three and a half months of drift into
        nothing at all.
        """
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", tmp_path / "docs")
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "e2e" / "package.json").write_text(
            '{"version": "2.9.0"}', encoding="utf-8")
        issues = mod.check_e2e_and_jsx_versions("2.9.0")
        jsx = [i for i in issues if i.check == "jsx-frontmatter-version"]
        assert jsx, "a missing JSX tree produced no issue"
        assert jsx[0].severity == "error"

    def test_a_matching_jsx_version_passes_in_silence(
            self, tmp_path, monkeypatch):
        """⛔ Must-not-fire for the JSX half too."""
        assert self._run(
            tmp_path, monkeypatch, body='{"version": "2.9.0"}',
            jsx="---\ntitle: probe\nversion: v2.9.0\n---\nbody\n") == []

    def test_an_unreadable_jsx_file_is_an_error_not_a_traceback(
            self, tmp_path, monkeypatch):
        """⛔ The e2e half names an unreadable file; this half used to raise.

        A file that disappears between the `rglob` and the read, or that
        cannot be opened, took the whole validator down with a traceback
        and produced no Issue — louder than a silent skip, but it still
        ends with nothing checked and, under `--fix`, with the traceback
        landing in the middle of a repair run.
        """
        tmp_path = self._tree(tmp_path, body='{"version": "2.9.0"}',
                              jsx="---\nversion: v2.9.0\n---\n")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", tmp_path / "docs")
        target = tmp_path / "tools" / "portal" / "src" / "probe.jsx"

        real_read = Path.read_text

        def _boom(self, *a, **kw):
            if self == target:
                raise OSError(13, "Permission denied")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _boom)
        issues = mod.check_e2e_and_jsx_versions("2.9.0")
        jsx = [i for i in issues if i.check == "jsx-frontmatter-version"]
        assert jsx, "the unreadable file produced no issue at all"
        assert jsx[0].severity == "error"
        assert "probe.jsx" in jsx[0].file
        assert "never compared" in jsx[0].message, jsx[0].message

class TestFixDoesNotTurnTheGateOff:
    """#1483 — `--ci --fix` exited 0 no matter what.

    ⛔ The exit code is decided from the issues `_auto_fix` reports it
    actually repaired, never from a list of "fixable" check ids. Blind review
    killed the id-list version: `rule-pack-count` is a repairable id, but the
    repair only rewrites badge patterns, so the same id occurring in prose was
    dropped out of the exit-code decision AND out of the report — measured,
    `--ci` rc=1 and `--ci --fix` rc=0 with the message reading "0 error(s)".
    An id cannot answer "was this occurrence repaired"; only the repair can.
    """

    def test_ci_fix_exits_nonzero_when_an_unrepaired_error_stands(
            self, monkeypatch, capsys):
        """⛔ Must-fire, driving the real `main()` with a real unrepaired error.

        `_auto_fix` is stubbed to repair nothing, so the injected error is
        unrepaired by construction and no file is written.
        """
        injected = [mod.Issue("e2e-package-version", "error",
                              "tests/e2e/package.json", 0, "frozen at 2.6.0")]
        monkeypatch.setattr(mod, "_auto_fix", lambda *a, **k: [])
        monkeypatch.setattr(mod, "check_e2e_and_jsx_versions",
                            lambda *a, **k: injected)
        monkeypatch.setattr(sys, "argv",
                            ["validate_docs_versions", "--ci", "--fix"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, out
        assert "e2e-package-version" in out, (
            "the error that kept the exit non-zero was never printed:\n%s" % out)

    def test_ci_fix_exits_zero_when_the_error_really_was_repaired(
            self, monkeypatch, capsys):
        """⛔ Must-not-fire: an error `_auto_fix` DID repair must not fail.

        ⚠️ The injected issue is returned by the stubbed `_auto_fix` as
        repaired — that is the whole distinction this change rests on, and
        the reason the exit code is not derived from check ids. Without this
        control the change could simply have made `--ci --fix` always fail.
        """
        repaired = [mod.Issue("tool-count", "error", "README.md", 0,
                              "count drift that --fix repairs")]
        monkeypatch.setattr(mod, "_auto_fix", lambda *a, **k: list(repaired))
        monkeypatch.setattr(mod, "check_tool_count_in_docs",
                            lambda *a, **k: list(repaired))
        monkeypatch.setattr(mod, "check_e2e_and_jsx_versions",
                            lambda *a, **k: [])
        monkeypatch.setattr(sys, "argv",
                            ["validate_docs_versions", "--ci", "--fix"])
        code = None
        try:
            mod.main()
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr().out
        # ⚠️ Only assert about the injected issue. Asserting `code == 0`
        # outright couples this test to the repository being free of every
        # other unrepaired error — blind review demonstrated that by adding
        # one ordinary doc and turning this control red for an unrelated
        # reason.
        assert "tool-count" not in out.split("still standing")[-1], (
            "a repaired issue must not be listed as still standing:\n%s" % out)

    def test_auto_fix_reports_only_what_it_really_repaired(
            self, tmp_path, monkeypatch):
        """⛔ Drives the real `_auto_fix`, not a stub.

        The exit-code decision now trusts `_auto_fix`'s return value, so that
        value has to be earned. Mutation showed the gap: making `_auto_fix`
        append every issue it looked at — repaired or not — left every test
        green, because all of them stubbed it out.

        Two issues here: one whose pattern is present (repairable) and one
        whose pattern is absent from the file (nothing to rewrite). Only the
        first may come back.
        """
        f = tmp_path / "README.md"
        f.write_text("badge bilingual-1%20pairs here\n", encoding="utf-8")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        repairable = mod.Issue("bilingual-count", "error", "README.md", 0, "x")
        untouched = mod.Issue("rule-pack-count", "error", "README.md", 0, "y")
        got = mod._auto_fix([repairable, untouched], 96,
                            {"pack_count": 16, "alert": 161})
        assert got == [repairable], (
            "_auto_fix must return the issues it actually repaired, not the "
            "ones it merely considered: %s" % [i.check for i in got])
        assert "bilingual-96%20pairs" in f.read_text(encoding="utf-8")

    def test_a_second_repair_does_not_revert_the_first(
            self, tmp_path, monkeypatch):
        """⛔ Two issues, one file — the stale-cache bug.

        `_read_cached` is never invalidated on write, so the second issue on
        a file used to read the pre-repair text and write it back, silently
        undoing the first repair while printing `Fixed` for both. Mutation
        confirmed nothing covered this: reverting the fresh read left every
        test green.
        """
        f = tmp_path / "README.md"
        f.write_text("bilingual-1%20pairs and rule%20packs-99- badges\n",
                     encoding="utf-8")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        # Warm the cache the way a real run does, before any repair.
        mod._read_cached(f)
        got = mod._auto_fix(
            [mod.Issue("bilingual-count", "error", "README.md", 0, "x"),
             mod.Issue("rule-pack-count", "error", "README.md", 0, "y")],
            96, {"pack_count": 16, "alert": 161})
        text = f.read_text(encoding="utf-8")
        assert len(got) == 2, [i.check for i in got]
        assert "bilingual-96%20pairs" in text, (
            "the first repair was reverted by the second:\n%s" % text)
        assert "rule%20packs-16-" in text, text

    def test_the_repair_and_the_check_count_tools_the_same_way(self):
        """⛔ One implementation of "how many Python tools", and the right one.

        Measured before this change: the check scanned root + ops/dx/lint
        (220) while the repair scanned the root only (1), so `--fix` wrote
        "1 個 Python 工具" into README.md and printed that it had fixed it.
        `tool-count` is a warning, so nothing went red afterwards.

        ⚠️ The first repair unified both halves on the checker's number,
        which is itself wrong — see `_count_python_tools`. `n > 100` was
        the original assertion here and it could not tell the candidates
        apart: every one of them passes it. This compares against the
        scope the counted sentence states instead, re-derived here rather
        than read back from the module under test.
        """
        n = mod._count_python_tools()
        tools_dir = mod.REPO_ROOT / "scripts" / "tools"

        def _keep(f):
            return not any(f.name.startswith(p)
                           for p in _toolcount.TOOL_SKIP_PREFIXES)

        documented = sum(1 for s in ("ops", "dx", "lint")
                         for f in (tools_dir / s).glob("*.py") if _keep(f))
        root_only = sum(1 for f in tools_dir.glob("*.py") if _keep(f))
        deep = sum(1 for f in tools_dir.rglob("*.py")
                   if "__pycache__" not in f.parts and _keep(f))

        assert n == documented, (
            "the counter no longer matches ``scripts/tools/{ops,dx,lint}``, "
            "the scope the counted sentence states: %d vs %d" % (n, documented))
        # Must-fire controls: without these the equality above would also
        # hold for a tree where the root and the nested packages are empty,
        # and the assertion would prove nothing.
        assert root_only, (
            "no countable .py at scripts/tools/ root, so 'the root is "
            "excluded' is vacuous here — re-derive this test")
        assert deep > documented, (
            "no countable .py below the three subdirectories, so 'rglob "
            "would over-count' is vacuous here — re-derive this test")

        issues = mod.check_tool_count_in_docs()
        for i in issues:
            assert "actual is %d" % n in i.message, (
                "check and repair disagree about the count: %s" % i.message)

    def test_the_checker_and_the_writer_agree_about_the_count(self):
        """⛔ The number is produced by one module and checked by another.

        `bump_docs._count_python_tools` writes it into README.md and
        README.en.md; this module checks it. When they disagree the gate
        emits a warning that neither `--fix` nor `bump_docs --sync-counts`
        can settle — they rewrite the same line to different values.

        ⚠️ Since #1511 both sides call `_lib_toolcount.count_scope`, so on
        the repo's own tree this can only fail if someone reintroduces a
        private scanner. It stays because that is exactly the regression
        worth catching; the predicate itself is exercised against a
        synthetic tree in tests/shared/test_lib_toolcount.py.

        ⛔ `generate_tool_map.gather_tools` is deliberately NOT compared
        here. It scans a wider scope — the same three subdirectories plus
        the repo root, which `check_tool_map_coverage` requires it to list
        — and has never produced this number. The ticket's own table said
        it did; it never has.
        """
        spec = importlib.util.spec_from_file_location(
            "bump_docs_for_count_check",
            REPO_ROOT / "scripts" / "tools" / "dx" / "bump_docs.py")
        bump = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bump)
        writer_total = bump._count_python_tools()[0]
        assert mod._count_python_tools() == writer_total, (
            "the gate checks %d but bump_docs writes %d, so the warning it "
            "raises cannot be repaired by either tool"
            % (mod._count_python_tools(), writer_total))

    def test_a_new_tool_subdirectory_cannot_be_dropped_in_silence(self):
        """⛔ The tuple is the documented scope, so drift must be loud.

        Adding `scripts/tools/<new>/` with tools in it would otherwise lower
        the count with nothing going red — the same shape as the bug this
        cohort is about. A directory holding an `__init__.py` is a package,
        i.e. library code the count has never included (`dx/custom_alerts/`
        is the live example: three modules, no `main()` in any of them).

        ⚠️ Residual, disclosed rather than papered over: a package
        directory that really does hold tools still passes this in silence.
        Classifying by `__init__.py` is the repo's own convention, not a
        proof, and no check here can tell a library from a tool.
        """
        assert _unlisted_tool_subdirs(
            REPO_ROOT / "scripts" / "tools",
            _toolcount.COUNT_SUBDIRS) == []

    def test_the_subdirectory_tripwire_actually_fires(self, tmp_path):
        """Must-fire control for the test above.

        A tripwire that never fires and a tree that never drifts look
        identical from the assertion alone.
        """
        (tmp_path / "ops").mkdir()
        (tmp_path / "ops" / "a.py").write_text("", encoding="utf-8")
        pkg = tmp_path / "shared"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helper.py").write_text("", encoding="utf-8")
        newdir = tmp_path / "release"
        newdir.mkdir()
        (newdir / "cut_release.py").write_text("", encoding="utf-8")

        assert _unlisted_tool_subdirs(tmp_path, ("ops",)) == ["release"], (
            "the tripwire missed a new non-package tool directory")
        assert _unlisted_tool_subdirs(tmp_path, ("ops", "release")) == [], (
            "the tripwire fires on a directory that is listed")

    def test_the_cache_does_not_outlive_the_write(self, tmp_path, monkeypatch):
        """A repaired file must not leave pre-repair text in the cache."""
        f = tmp_path / "README.md"
        _write(f, "badge bilingual-94%20pairs here\n")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        mod._read_cached(f)
        mod._auto_fix(
            [mod.Issue("bilingual-count", "error", "README.md", 0, "x")],
            96, {"pack_count": 16, "alert": 161})
        assert "bilingual-96%20pairs" in mod._read_cached(f), (
            "the cache still holds the pre-repair text after a write")


_AUDIT_DRIVER = '''\
import json, os, runpy, sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
GATE = ROOT / "scripts" / "tools" / "lint" / "validate_docs_versions.py"
OUT = Path(sys.argv[2])
SKIP = ("__pycache__/", ".venv/", "node_modules/", "site-packages/")

opened = set()


def _hook(event, args):
    if event != "open" or not args:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    try:
        rel = Path(os.fsdecode(target)).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return
    if not any(part in rel for part in SKIP):
        opened.add(rel)


sys.addaudithook(_hook)
# What `python scripts/tools/lint/validate_docs_versions.py` gives it: the
# script's own directory as sys.path[0]. runpy does not do that for us, and
# without it the gate cannot import `_version_patterns`.
sys.path.insert(0, str(GATE.parent))
sys.argv = [str(GATE), "--ci"]
_stdout = sys.stdout
sys.stdout = open(os.devnull, "w", encoding="utf-8")
try:
    runpy.run_path(str(GATE), run_name="__main__")
except SystemExit:
    pass
finally:
    sys.stdout.close()
    sys.stdout = _stdout

modules = set()
for _mod in list(sys.modules.values()):
    origin = getattr(_mod, "__file__", None)
    if not origin:
        continue
    try:
        rel = Path(origin).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        continue
    if not any(part in rel for part in SKIP):
        modules.add(rel)
modules.add(GATE.relative_to(ROOT).as_posix())

OUT.write_text(json.dumps({"opened": sorted(opened),
                           "modules": sorted(modules)}), encoding="utf-8")
'''


class TestTheHookFiresOnEverythingTheGateReads:
    """#1508 — `files:` decides whether this gate runs at all.

    With `pass_filenames: false` the pattern is not a scan scope; it is the
    trigger. Anything the tool reads that the pattern does not match gives a
    commit touching only that file no local signal whatsoever.

    ⛔ The input set is MEASURED, not listed: an audit hook records what the
    process really opens, and `sys.modules` records what it really imported.
    A hand-written list here would be a second declaration of the same thing
    and would drift out the same way the pattern did — measured on
    `5b7f6c35`, the shipped pattern missed 25 data inputs (`.json` 16,
    `.yml` 8, `VERSION` 1) plus all 6 of its own modules.

    ⚠️ Honest boundaries. (a) This sees files the gate OPENS. Inputs it only
    stats or globs by name — `scripts/tools/**/*.py`, counted but never read —
    are outside it, and deliberately so: a tool count moving is already the
    business of `tool-map-check`, and matching every Python file here would
    turn this into a hook that runs on every commit in the repository.
    (b) A coverage assertion is satisfied by any pattern wide enough, so
    `test_the_coverage_check_can_actually_fail` pins one path the pattern must
    NOT match. Measured: `files: .*` turns that test red. What is left
    unguarded is the band in between — a pattern wider than it needs to be but
    still not matching an unrelated tool — because an over-wide trigger costs
    time and nothing else, whereas the failure this exists for is silent.
    """

    @staticmethod
    def _hook_pattern():
        import yaml
        cfg = yaml.safe_load(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        hooks = [h for repo in cfg["repos"] for h in repo["hooks"]
                 if h["id"] == "version-consistency"]
        assert len(hooks) == 1, "hook id moved or was duplicated"
        assert hooks[0].get("pass_filenames") is False, (
            "this test assumes `files:` is a trigger, not a scan scope")
        return re.compile(hooks[0]["files"])

    @staticmethod
    def _measure(tmp_path):
        driver = tmp_path / "audit_driver.py"
        driver.write_text(_AUDIT_DRIVER, encoding="utf-8")
        out = tmp_path / "inputs.json"
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(driver), str(REPO_ROOT), str(out)],
            capture_output=True, timeout=600)
        assert out.exists(), (
            "the audit driver produced nothing — measure the measurer before "
            "believing an empty result:\n%s"
            % proc.stderr.decode("utf-8", "replace")[-2000:])
        return json.loads(out.read_text(encoding="utf-8"))

    def test_the_measurement_itself_is_not_empty(self, tmp_path):
        """⛔ Positive control. A broken driver reports nothing to cover.

        Without this, deleting the audit hook, resolving paths wrongly, or
        having `runpy` fail early all look identical to "the pattern already
        covers everything".
        """
        measured = self._measure(tmp_path)
        opened, modules = measured["opened"], measured["modules"]
        assert len(opened) >= 100, (
            "only %d files recorded — the driver, not the tree, is the "
            "likely explanation: %s" % (len(opened), opened[:20]))
        for expected in ("CLAUDE.md",
                         "components/da-tools/app/VERSION",
                         "helm/threshold-exporter/Chart.yaml",
                         "mkdocs.yml",
                         "tests/e2e/package.json"):
            assert expected in opened, (expected, len(opened))
        assert "scripts/tools/lint/validate_docs_versions.py" in modules, modules
        assert "scripts/tools/lint/_version_patterns.py" in modules, modules

        # ⛔ A count floor measures how much came back, not whether the walk
        # still reaches. Blind review demonstrated the difference: turning the
        # gate's `rglob` into `glob` collapses its corpus from 382 files to 51
        # — every one of the five names above survives, `len(opened)` stays
        # over 100, and all four tests in this class stay green while the
        # coverage claim below is being made about a third of the tree.
        #
        # Nesting is the property that dies, so nesting is what is asserted.
        # ⚠️ The margin is narrow and saying so is the point. Measured with
        # THESE two expressions on this tree: 408 nested inputs and 44
        # directories; with `rglob` turned into `glob`, 97 and 17. The floors
        # sit at 100 and 20, i.e. three clear of the failure mode — not the
        # comfortable gap an earlier revision of this comment claimed ("a flat
        # walk leaves single digits of both", which was simply wrong). If a
        # future change removes a directory's worth of inputs for a legitimate
        # reason, expect to re-measure rather than to have room.
        nested = [p for p in opened if p.count("/") >= 2]
        directories = {p.rsplit("/", 1)[0] for p in opened if "/" in p}
        assert len(nested) >= 100, (
            "only %d of %d recorded inputs are nested — the gate has stopped "
            "walking into subdirectories, and a coverage assertion made over "
            "what is left would be true of a fraction of the tree"
            % (len(nested), len(opened)))
        assert len(directories) >= 20, (
            "inputs come from only %d directories: %s"
            % (len(directories), sorted(directories)))

    def test_every_file_the_gate_opens_can_trigger_it(self, tmp_path):
        """⛔ Must-fire: the pattern has to cover the measured input set."""
        pattern = self._hook_pattern()
        measured = self._measure(tmp_path)
        uncovered = sorted(p for p in measured["opened"]
                           if not pattern.search(p))
        assert uncovered == [], (
            "version-consistency reads these but its `files:` pattern does "
            "not fire on them, so a commit touching only one of them skips "
            "the gate entirely.\n⛔ Widen `files:` — do NOT make the tool "
            "stop reading them; those reads are what the gate is for.\n"
            "%s" % uncovered)

    def test_editing_the_gate_itself_can_trigger_it(self, tmp_path):
        """⛔ Editing the reader changes what the gate decides.

        Without this, the commit that edits the reader never runs it once
        before landing. The closure is measured from `sys.modules`, so a
        helper split out of the gate is covered on the day it lands.

        ⚠️ This is a smoke test, not the enforcement: the gate's own tests run
        in CI on every PR (`ci.yml`'s `python` filter opens with a catch-all
        `**`). What is missing without it is the LOCAL run.

        ⛔ ERRATA — an earlier revision of this docstring said the reader
        "was changed by a commit that touched no `.md` at all", citing #1480.
        False: `abe27478` touched 11 `.md` files and never touched the reader.
        #1480 was a fail-open (source re-spelled, two consumers skipped in
        silence behind `if "platform" in versions`), repaired by #1493.

        ⚠️ The two bad repairs this failure invites — moving the helper back
        into a covered file, or dropping the import — are deliberately NOT
        named in the failure message. Both make it green by shrinking the
        gate, and the second is invisible here by construction (a module that
        is not imported is not in the closure). Naming them in a message read
        by someone racing to green would be handing out the recipe; naming
        them here, where the guard is designed rather than satisfied, is not.
        """
        pattern = self._hook_pattern()
        modules = self._measure(tmp_path)["modules"]
        uncovered = sorted(p for p in modules if not pattern.search(p))
        assert uncovered == [], (
            "the gate imports these, so editing them changes what it decides "
            "— yet none of them makes it run.\n"
            "⛔ Add the path to `files:` in .pre-commit-config.yaml.\n"
            "⚠️ Known shape: the `_lib_.*` branch only reaches "
            "`scripts/tools/` itself, so a helper under `scripts/tools/lint/` "
            "needs its own path here even if it is named `_lib_*`.\n"
            "%s" % uncovered)

    def test_the_coverage_check_can_actually_fail(self):
        """⛔ Positive control for the comparison, not for the measurement.

        `uncovered == []` is satisfied by any pattern-plus-input pair where
        the pattern is wide enough — including a pattern that is wide because
        the input list is empty. This feeds the SHIPPED pattern a file it must
        not match, so the assertion above is known to have teeth.
        """
        pattern = self._hook_pattern()
        assert not pattern.search("scripts/tools/ops/scaffold_tenant.py"), (
            "the pattern matches an unrelated tool — it has been widened past "
            "the point where the coverage assertion means anything")
        assert pattern.search("components/da-tools/app/VERSION")
        assert pattern.search("mkdocs.yml")
        assert pattern.search("tests/e2e/package.json")


class TestToolCountReadsBothLanguages:
    """#1540 — the English half had a writer and no reader.

    `TOOL_COUNT_PATTERNS` demanded a particular word right after the noun
    (`tools(` / `tools in`) while `README.en.md` says "tools **under**". Both
    patterns therefore scored 0 hits across all three TOOL_COUNT_CHECK_FILES,
    measured on `5b7f6c35`, while `bump_docs`' `readme-en-python-tools` rule
    kept writing the number. Rewriting it to 999 gave 0 findings.

    ⚠️ Not rc=0 — `tool-count` is a warning, so the exit code was never the
    signal here. An earlier revision of this docstring cited it as if it were.
    """

    @staticmethod
    def _tree(tmp_path, monkeypatch, tools=3, docs=()):
        """A repo with *tools* countable tools and the given doc bodies."""
        for i in range(tools):
            d = tmp_path / "scripts" / "tools" / "ops"
            d.mkdir(parents=True, exist_ok=True)
            _write(d / f"t{i}.py", "")
        names = []
        for name, body in docs:
            _write(tmp_path / name, body)
            names.append(tmp_path / name)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "TOOL_COUNT_CHECK_FILES", names)
        return names

    # The declared scope, spelled exactly as the shipped sentences spell it.
    SCOPE = "`scripts/tools/{ops,dx,lint}`"

    def test_the_english_sentence_is_read_at_all(self, tmp_path, monkeypatch):
        """⛔ Must-fire on the defect, in the exact shipped phrasing."""
        self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.en.md",
             f"| Shell entrypoints + 999 Python tools under {self.SCOPE} |\n")])
        found = mod.check_tool_count_in_docs()
        assert [i.check for i in found] == ["tool-count"], (
            "the English count sentence is still unread: %s" % found)
        assert "999" in found[0].message and "3" in found[0].message

    def test_any_preposition_at_all(self, tmp_path, monkeypatch):
        """⛔ The property is preposition-INDEPENDENCE, not a longer list.

        One of these words is deliberately absurd. A repair that merely added
        `under` to the two existing spellings would pass the shipped phrasing
        and fail here — which is the whole point of including a preposition
        nobody would ever enumerate.
        """
        for prep in ("under", "in", "(", "across", "beneath", "zzqx"):
            body = f"+ 999 Python tools {prep} {self.SCOPE}\n"
            names = self._tree(tmp_path, monkeypatch, tools=3,
                               docs=[("README.en.md", body)])
            assert mod.check_tool_count_in_docs(), (
                "not caught with preposition %r — the check is enumerating "
                "spellings again: %s" % (prep, body))
            for p in names:
                p.unlink()

    def test_the_chinese_half_still_works(self, tmp_path, monkeypatch):
        """⚠️ Control: the language that already worked must keep working."""
        self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.md", f"{self.SCOPE} 下 999 個 Python 工具\n")])
        assert [i.check for i in mod.check_tool_count_in_docs()] == ["tool-count"]

    def test_a_correct_count_is_not_flagged_in_either_language(
            self, tmp_path, monkeypatch):
        """⚠️ Must-not-fire control against a check that just always fires."""
        self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.md", f"{self.SCOPE} 下 3 個 Python 工具\n"),
            ("README.en.md", f"+ 3 Python tools under {self.SCOPE}\n")])
        assert mod.check_tool_count_in_docs() == []

    @pytest.mark.parametrize("lang,body", [
        ("en", "The try-local/ showcase bundle ships 2 Python tools of its own.\n"),
        ("zh", "try-local 另外附 2 個 Python 工具。\n"),
    ])
    def test_a_count_about_another_scope_is_none_of_its_business(
            self, tmp_path, monkeypatch, lang, body):
        """⛔ Must-not-fire: a true sentence about a DIFFERENT scope.

        Blind review measured what happens without the scope anchor: this
        exact sentence produced `found 2, actual is 221`, `--fix` rewrote it
        to `ships 221 Python tools of its own`, and the run finished with
        `✅ All version references and counts are consistent.` A repair that
        turns a true sentence false, reporting success.

        ⚠️ Both languages, because the Chinese half had the same hazard on
        `origin/main` — the anchor is what closes it, not the English repair.
        """
        name = "README.en.md" if lang == "en" else "README.md"
        good = (f"+ 3 Python tools under {self.SCOPE}\n" if lang == "en"
                else f"{self.SCOPE} 下 3 個 Python 工具\n")
        names = self._tree(tmp_path, monkeypatch, tools=3,
                           docs=[(name, good + body)])
        assert mod.check_tool_count_in_docs() == [], (
            "a sentence that never names the counted scope was read as this "
            "count")
        mod._auto_fix([mod.Issue("tool-count", "warn", name, 1, "x")],
                      96, {"pack_count": 16, "alert": 161})
        assert body.strip() in names[0].read_text(encoding="utf-8"), (
            "the repair rewrote a sentence about another scope")

    def test_two_counts_on_the_anchored_line_is_refused_not_guessed(
            self, tmp_path, monkeypatch):
        """⛔ The anchor narrowed file→line; it does NOT narrow within a line.

        Measured on the shipped `README.en.md` before this: adding a TRUE
        clause to the counted row —

            + 221 Python tools under `<scope>`, incl. 105 Python tools in lint/

        — got `found 105, actual is 221` reported and then rewritten to 221 by
        `--fix`, which finished with "✅ All version references and counts are
        consistent." The clause was true; the tool made it false and called
        that success. Nothing on the line says which count the scope governs,
        so the check refuses to judge and the repair refuses to touch it.
        """
        body = (f"+ 999 Python tools under {self.SCOPE}, "
                f"incl. 105 Python tools in lint/\n")
        names = self._tree(tmp_path, monkeypatch, tools=3,
                           docs=[("README.en.md", body)])
        found = mod.check_tool_count_in_docs()
        assert len(found) == 1, ("expected exactly one ambiguity finding, got "
                                 "%s" % [i.message for i in found])
        assert "line states 2 counts" in found[0].message, found[0].message
        assert "999" in found[0].message and "105" in found[0].message

        mod._auto_fix(found, 96, {"pack_count": 16, "alert": 161})
        assert names[0].read_text(encoding="utf-8") == body, (
            "the repair rewrote a line the check refused to judge:\n%s"
            % names[0].read_text(encoding="utf-8"))

    def test_the_repair_refuses_even_when_handed_an_ambiguous_line(
            self, tmp_path, monkeypatch):
        """⚠️ The repair re-derives; it does not trust the issue it is given.

        A stale or hand-made `Issue` pointing at an ambiguous line must not be
        a way in — the two halves used to ask the question differently, which
        is the whole defect above.
        """
        body = (f"+ 999 Python tools under {self.SCOPE}, "
                f"incl. 105 Python tools in lint/\n")
        names = self._tree(tmp_path, monkeypatch, tools=3,
                           docs=[("README.en.md", body)])
        issue = mod.Issue("tool-count", "warn", "README.en.md", 1,
                          "Python tool count (en): found 999, actual is 3")
        assert mod._auto_fix([issue], 96, {"pack_count": 16, "alert": 161}) == []
        assert names[0].read_text(encoding="utf-8") == body

    def test_a_single_count_on_the_anchored_line_still_repairs(
            self, tmp_path, monkeypatch):
        """⚠️ Paired control: fail-closed must not become fail-always.

        Without this, refusing every anchored line would satisfy the two
        assertions above and silently retire the whole check.
        """
        names = self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.en.md", f"+ 999 Python tools under {self.SCOPE}\n")])
        found = mod.check_tool_count_in_docs()
        assert [i.check for i in found] == ["tool-count"], found
        assert "line states" not in found[0].message, found[0].message
        assert mod._auto_fix(found, 96, {"pack_count": 16, "alert": 161}) == found
        assert "3 Python tools under" in names[0].read_text(encoding="utf-8")

    def test_the_repair_cannot_reach_past_the_line_it_was_given(
            self, tmp_path, monkeypatch):
        """⛔ Whole-file `re.sub` + `\\s*` matching a newline = invisible lies.

        Measured before the repair was keyed to the finding: a wrapped
        sentence (`removed 40\\nPython tools that had no callers`) was
        rewritten to `removed 221\\n…` while the per-line checker never
        reported it — before or after. The falsehood lands under a green
        light and no future run can find it.
        """
        wrapped = "During the v2.6 cleanup we removed 40\nPython tools.\n"
        names = self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.en.md",
             f"+ 999 Python tools under {self.SCOPE}\n" + wrapped)])
        found = mod.check_tool_count_in_docs()
        assert len(found) == 1 and found[0].line == 1, found
        mod._auto_fix(found, 96, {"pack_count": 16, "alert": 161})
        text = names[0].read_text(encoding="utf-8")
        assert f"+ 3 Python tools under {self.SCOPE}" in text, text
        assert "removed 40\nPython tools." in text, (
            "the repair reached a wrapped occurrence the checker cannot see")

    def test_the_repair_reaches_the_english_half_too(self, tmp_path,
                                                     monkeypatch):
        """⛔ A reader without a writer is a finding that can never be cleared.

        `AUTO_FIX_PATTERNS["tool-count"]` was Chinese-only, so once the
        English number became visible it would have been reported every run
        and repaired never — the shape #1504 named: checker and repair
        disagreeing produces a warning no tool can satisfy.
        """
        names = self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.en.md", f"+ 999 Python tools under {self.SCOPE}\n")])
        issue = mod.Issue("tool-count", "warn", "README.en.md", 1, "x")
        assert mod._auto_fix([issue], 96, {"pack_count": 16, "alert": 161}) == [issue]
        assert "3 Python tools under" in names[0].read_text(encoding="utf-8")
        assert mod.check_tool_count_in_docs() == [], (
            "repaired, yet the checker still reports it")

    def test_checker_and_repair_agree_on_case(self, tmp_path, monkeypatch):
        """⛔ The checker matches case-insensitively; the repair must too.

        Mutation target: dropping `flags=re.IGNORECASE` from the repair. The
        checker would still report `python tools`, the repair would decline to
        touch it, and the finding would stand forever.
        """
        names = self._tree(tmp_path, monkeypatch, tools=3, docs=[
            ("README.en.md", f"+ 999 python tools under {self.SCOPE}\n")])
        assert mod.check_tool_count_in_docs(), "checker missed the lowercase form"
        mod._auto_fix([mod.Issue("tool-count", "warn", "README.en.md", 1, "x")],
                      96, {"pack_count": 16, "alert": 161})
        assert "3 python tools under" in names[0].read_text(encoding="utf-8"), (
            "the repair is stricter than its checker — an unclearable warning")

    def test_both_readers_are_alive_on_the_shipped_files(self):
        """⛔ The one assertion here that touches the files this is about.

        #1540's defect was that the English pattern scored ZERO hits on the
        SHIPPED files while looking perfectly reasonable. Every other test in
        this class builds a synthetic tree and monkeypatches
        `TOOL_COUNT_CHECK_FILES`, so none of them can see that failure return
        — measured: setting that list to `[]` leaves the whole module green
        at 107 passed while the checker reads nothing at all (measured on
        `ed43e772`; this module holds 116 tests today, and the same mutation
        is killed by this test).

        ⚠️ Deliberately asserts that each pattern READS something, not what
        the number is. A drifted count is already `bump_docs
        --sync-counts --check`'s red and this check's warning; a dead reader
        is silent everywhere, and that is what this is for.
        """
        alive = {}
        for fpath in mod.TOOL_COUNT_CHECK_FILES:
            if not fpath.exists():
                continue
            for line in fpath.read_text(encoding="utf-8").splitlines():
                if mod.TOOL_COUNT_SCOPE_ANCHOR not in line:
                    continue
                for pat, desc in mod.TOOL_COUNT_PATTERNS:
                    if re.search(pat, line, re.IGNORECASE):
                        alive.setdefault(desc, set()).add(fpath.name)
        assert set(alive) == {d for _, d in mod.TOOL_COUNT_PATTERNS}, (
            "a tool-count pattern reads nothing in any shipped file — that is "
            "the #1540 failure itself, and it is silent everywhere else.\n"
            "⚠️ Check TOOL_COUNT_SCOPE_ANCHOR before the patterns: if the "
            "sentence no longer names the scope verbatim, no pattern change "
            "makes this green, and widening this to `assert alive` would drop "
            "one language for good.\n"
            "alive: %s" % {k: sorted(v) for k, v in alive.items()})

    def test_the_checker_is_looser_than_the_writer_on_purpose(self):
        """⚠️ Pins an asymmetry blind review flagged, as intentional.

        The checker tolerates any preposition; `bump_docs`' writer rule pins
        `Python tools under` verbatim. Measured: changing `under` to `across`
        in README.en.md leaves `validate_docs_versions --ci` silent (the
        number is still right) and turns `bump_docs --sync-counts --check`
        rc=1 with a DEAD diagnosis naming the pattern to fix.

        That is the correct split — a rewrite rule has to know exactly what to
        replace, a checker has to see every phrasing — and it is pinned here
        so nobody "fixes" the asymmetry by narrowing the checker back down.
        ⚠️ It also means a rephrasing is caught only by the writer's gate,
        which is CI-only (`validate.yaml`'s `version-check`), not local.
        """
        import bump_docs  # noqa: E402  (path via conftest)
        rule = {r["id"]: r for r in bump_docs._build_count_rules()}[
            "readme-en-python-tools"]
        assert "Python tools under" in rule["pattern"], (
            "the writer rule no longer pins the preposition; if that is "
            "deliberate, this test and its rationale need rewriting: %s"
            % rule["pattern"])
        assert not any("under" in pat for pat, _ in mod.TOOL_COUNT_PATTERNS), (
            "the checker has grown a preposition requirement — that is the "
            "#1540 defect returning: %s" % mod.TOOL_COUNT_PATTERNS)


class TestFixDoesNotSwitchOffJson:
    """#1506 — `--fix` used to make `--json` inert.

    The repair branch was its own exit (repair, print, `return`) sitting ahead
    of the only block that reads `args.json`, so `--ci --fix --json` printed
    `🔧 Fixed …` prose and a caller parsing it got a decode error on line 1.
    #1483 repaired the exit-code half of that same early return; the format
    half survived because the two halves are decided in two different places.

    ⚠️ These drive the real `main()` and assert on `json.loads`, so a
    reintroduced early return fails them regardless of how it is spelled —
    the assertion is about the output being a document, not about control
    flow. The check functions are stubbed so no repository file is written.
    """

    @staticmethod
    def _run(monkeypatch, capsys, argv, issues, repaired):
        """Drive `main()` with a fixed issue set; return (exit_code, stdout)."""
        monkeypatch.setattr(mod, "_auto_fix", lambda *a, **k: list(repaired))
        monkeypatch.setattr(mod, "check_e2e_and_jsx_versions",
                            lambda *a, **k: list(issues))
        for name in ("check_bilingual_badge", "check_bilingual_number_consistency",
                     "check_rule_pack_counts", "check_tool_count_in_docs",
                     "check_roadmap_changelog_overlap", "check_doc_map_coverage",
                     "check_tool_map_coverage", "check_adr_count_in_docs",
                     "check_doc_file_count_in_docs", "check_scenario_count_in_docs",
                     "check_image_tag_v_prefix", "check_mkdocs_extra_versions",
                     "check_da_tools_version", "check_exporter_version",
                     "check_release_tag_currency", "check_platform_version"):
            monkeypatch.setattr(mod, name, lambda *a, **k: [])
        monkeypatch.setattr(sys, "argv", ["validate_docs_versions", *argv])
        code = 0
        try:
            mod.main()
        except SystemExit as exc:
            code = exc.code
        return code, capsys.readouterr().out

    def test_fix_and_json_together_still_emit_one_json_document(
            self, monkeypatch, capsys):
        """⛔ Must-fire on the defect: the whole of stdout has to parse."""
        repaired = [mod.Issue("bilingual-count", "warn", "README.md", 0, "x")]
        code, out = self._run(monkeypatch, capsys,
                              ["--ci", "--fix", "--json"], repaired, repaired)
        doc = json.loads(out)  # the regression: this raised before #1506
        assert code == 0, out
        assert [i["check"] for i in doc["repaired"]] == ["bilingual-count"], doc
        assert doc["issues"] == [], (
            "a repaired issue must not also be reported as standing: %s" % doc)
        assert doc["summary"]["repaired"] == 1, doc

    def test_json_survives_the_failing_path_too(self, monkeypatch, capsys):
        """⛔ The path most likely to regress: `--fix` leaves a real error.

        An unrepaired error has to keep the exit code non-zero AND still be
        described by a parseable document — a fix that emitted JSON only on
        the happy path would leave every failing CI run unparseable.
        """
        standing = [mod.Issue("e2e-package-version", "error",
                              "tests/e2e/package.json", 0, "frozen at 2.6.0")]
        code, out = self._run(monkeypatch, capsys,
                              ["--ci", "--fix", "--json"], standing, [])
        doc = json.loads(out)
        assert code != 0, out
        assert doc["summary"]["errors"] == 1, doc
        assert [i["check"] for i in doc["issues"]] == ["e2e-package-version"]

    def test_fix_without_json_still_speaks_prose(self, monkeypatch, capsys):
        """⚠️ Must-not-fire control against over-correcting into silence.

        The obvious way to make `--json` clean is to stop printing during
        repair. Without this, `--fix` alone could go mute and every assertion
        above would still pass.
        """
        standing = [mod.Issue("e2e-package-version", "error",
                              "tests/e2e/package.json", 0, "frozen at 2.6.0")]
        repaired = [mod.Issue("bilingual-count", "warn", "README.md", 0, "x")]
        code, out = self._run(monkeypatch, capsys, ["--ci", "--fix"],
                              standing + repaired, repaired)
        assert code != 0, out
        assert "Auto-fixed 1 issue(s)" in out, out
        assert "still standing" in out, out
        assert "e2e-package-version" in out, out

    def test_auto_fix_quiet_writes_the_file_but_prints_nothing(
            self, tmp_path, monkeypatch, capsys):
        """⛔ `quiet` must suppress only the prose, never the repair itself.

        A `quiet` that also skipped the write would make the JSON document
        describe a repair that did not happen.
        """
        f = tmp_path / "README.md"
        _write(f, "badge bilingual-1%20pairs here\n")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        issue = mod.Issue("bilingual-count", "warn", "README.md", 0, "x")
        got = mod._auto_fix([issue], 96, {"pack_count": 16, "alert": 161},
                            quiet=True)
        assert got == [issue]
        assert "bilingual-96%20pairs" in f.read_text(encoding="utf-8"), (
            "quiet suppressed the repair, not just the message")
        assert capsys.readouterr().out == "", (
            "quiet must print nothing; anything ahead of the JSON document "
            "makes it unparseable")

    def test_not_quiet_still_prints_the_repair_line(
            self, tmp_path, monkeypatch, capsys):
        """⚠️ Paired control: the default must keep the human-facing line."""
        f = tmp_path / "README.md"
        _write(f, "badge bilingual-1%20pairs here\n")
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        mod._auto_fix([mod.Issue("bilingual-count", "warn", "README.md", 0, "x")],
                      96, {"pack_count": 16, "alert": 161})
        assert "Fixed bilingual-count" in capsys.readouterr().out


class TestCheckReleaseTagCurrency:
    """Pins the release-tag currency check added after #141 Track B / TB-F1:
    stale `tools/v2.7.0` install examples drifted because bump_docs' bold-only
    rewrite + the image-tag-only checks left the release-tag form unguarded."""

    def _scan(self, tmp_path, monkeypatch, body, tools="2.8.0", exporter="2.8.0"):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "install.md", body)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()
        return mod.check_release_tag_currency(tools, exporter)

    def test_flags_stale_tools_release_tag(self, tmp_path, monkeypatch):
        issues = self._scan(tmp_path, monkeypatch, "TAG=tools/v2.7.0    # pin\n")
        assert len(issues) == 1
        assert issues[0].check == "release-tag-version"
        assert "tools/v2.7.0" in issues[0].message

    def test_flags_stale_da_binary_version_output(self, tmp_path, monkeypatch):
        issues = self._scan(tmp_path, monkeypatch,
                            "da-guard --version    # should print da-guard v2.7.0\n")
        assert len(issues) == 1
        assert "v2.7.0" in issues[0].message

    def test_flags_stale_set_image_tag(self, tmp_path, monkeypatch):
        issues = self._scan(tmp_path, monkeypatch, "  --set image.tag=v2.7.0 \\\n")
        assert len(issues) == 1
        assert "image.tag" in issues[0].message

    def test_passes_current_versions(self, tmp_path, monkeypatch):
        body = ("TAG=tools/v2.8.0\nda-guard v2.8.0\n  --set image.tag=v2.8.0 \\\n")
        assert self._scan(tmp_path, monkeypatch, body) == []

    def test_skips_historical_marker_line(self, tmp_path, monkeypatch):
        # "older releases (≤ tools/v2.7.0)" is a deliberate past-version cite.
        body = "> Older releases (≤ `tools/v2.7.0`) only ship the Docker path.\n"
        assert self._scan(tmp_path, monkeypatch, body) == []

    def test_respects_inline_ignore(self, tmp_path, monkeypatch):
        body = "TAG=tools/v2.7.0  # version-currency-ignore: pinned on purpose\n"
        assert self._scan(tmp_path, monkeypatch, body) == []

    def test_set_image_tag_unchecked_without_exporter(self, tmp_path, monkeypatch):
        # When exporter version can't be resolved, the --set image.tag spec is
        # skipped (tools-line checks still run).
        issues = self._scan(tmp_path, monkeypatch,
                            "  --set image.tag=v2.7.0 \\\n", exporter=None)
        assert issues == []


class TestCheckImageTagVPrefix:
    """Pins the v-prefix check across all 4 component images. tenant-api /
    da-portal were added after #682 (Option X): a no-v `tenant-api:2.7.0` had
    drifted uncaught in a shipped k8s manifest because the pattern previously
    only recognised da-tools / threshold-exporter."""

    def _scan(self, tmp_path, monkeypatch, body):
        docs = tmp_path / "docs"
        docs.mkdir()
        _write(docs / "deploy.md", body)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "DOCS_DIR", docs)
        monkeypatch.setattr(mod, "ROOT_FILES", ())
        mod._FILE_CACHE.clear()
        mod._RGLOB_CACHE.clear()
        mod._CONTENT_CACHE.clear()
        return mod.check_image_tag_v_prefix()

    def test_flags_tenant_api_missing_v(self, tmp_path, monkeypatch):
        issues = self._scan(tmp_path, monkeypatch,
                            "    image: ghcr.io/vencil/tenant-api:2.7.0\n")
        assert len(issues) == 1
        assert issues[0].check == "image-tag-v-prefix"
        assert "tenant-api:2.7.0" in issues[0].message
        assert "tenant-api:v2.7.0" in issues[0].message

    def test_flags_da_portal_missing_v(self, tmp_path, monkeypatch):
        issues = self._scan(tmp_path, monkeypatch,
                            "  ghcr.io/vencil/da-portal:2.8.0\n")
        assert len(issues) == 1
        assert "da-portal:2.8.0" in issues[0].message
        assert "da-portal:v2.8.0" in issues[0].message

    def test_v_prefixed_passes(self, tmp_path, monkeypatch):
        body = ("image: ghcr.io/vencil/tenant-api:v2.7.0\n"
                "image: ghcr.io/vencil/da-portal:v2.8.0\n")
        assert self._scan(tmp_path, monkeypatch, body) == []

    def test_oci_chart_ref_not_flagged(self, tmp_path, monkeypatch):
        # OCI *chart* refs legitimately use bare SemVer (no v-prefix).
        body = "helm pull oci://ghcr.io/vencil/charts/da-portal:2.8.0\n"
        assert self._scan(tmp_path, monkeypatch, body) == []
