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


# ============================================================
# check_release_tag_currency (TB-F1 class — release-tag forms)
# ============================================================


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
