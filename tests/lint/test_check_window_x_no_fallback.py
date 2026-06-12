"""Tests for check_window_x_no_fallback.py — forbid module-scope no-fallback window-global reads (dev-rules.md §S6).

Pinned contracts
----------------
1. **Detection** — three rule classes:
   - `global-read`: `^const X = window.__Y;` / `globalThis` variant
   - `react-destructure`: `^const { useState } = React;`
   - `global-destructure`: `^const { a, b } = window.__Y;` incl. the
     MULTI-LINE form. Regression pin: the three self-service-portal
     Tab modules destructured `window.__portalShared` at module scope
     across 4 lines; the original single-identifier regex missed them
     and the committed bundle threw TypeError at load time while every
     smoke check stayed green.
2. **Allowed**:
   - fallback form `const t = window.__t || ((zh, en) => en);`
   - destructure with fallback `const { a } = window.__X || {};`
   - function-scope reads (indented — regexes anchor at column 0)
   - ESM imports
3. **Suppression**: `<!-- window-x-no-fallback: ignore -->` within
   3-line lookback; frontmatter `--- ... ---` stripped (line numbers
   preserved).
4. **Live dogfood**: default scan roots → 0 findings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_window_x_no_fallback as lint  # noqa: E402

import pytest  # noqa: E402

FAKE = Path("fake.jsx")


def _kinds(src: str):
    return [(kind, line_no) for line_no, kind, _ in lint.find_violations(src, FAKE)]


# ---------------------------------------------------------------------------
# Detection — global-read (pattern A)
# ---------------------------------------------------------------------------
class TestGlobalRead:
    @pytest.mark.parametrize(
        "src",
        [
            "const X = window.__portalShared;\n",
            "const X = globalThis.__portalShared;\n",
            "const engine = window.__alertEngine;\n",
        ],
    )
    def test_flagged(self, src):
        assert _kinds(src) == [("global-read", 1)]

    def test_fallback_form_allowed(self):
        assert _kinds("const t = window.__t || ((zh, en) => en);\n") == []

    def test_function_scope_allowed(self):
        assert _kinds("  const X = window.__X;\n") == []


# ---------------------------------------------------------------------------
# Detection — global-destructure (pattern C; the portal-shared regression)
# ---------------------------------------------------------------------------
class TestGlobalDestructure:
    def test_single_line_flagged(self):
        src = "const { a, b } = window.__portalShared;\n"
        assert _kinds(src) == [("global-destructure", 1)]

    def test_multi_line_flagged(self):
        # Exact shape of the AlertPreviewTab regression.
        src = (
            "const {\n"
            "  RULE_PACK_DATA, generateSampleYaml, parseYaml, simulateAlerts,\n"
            "  RulePackSelector,\n"
            "} = window.__portalShared;\n"
        )
        assert _kinds(src) == [("global-destructure", 1)]

    def test_globalthis_variant_flagged(self):
        src = "const { a } = globalThis.__shared;\n"
        assert _kinds(src) == [("global-destructure", 1)]

    def test_destructure_with_fallback_allowed(self):
        assert _kinds("const { a } = window.__X || {};\n") == []

    def test_function_scope_allowed(self):
        assert _kinds("  const { a } = window.__X;\n") == []

    def test_react_destructure_is_not_double_counted(self):
        # `= React;` matches pattern B only, not pattern C.
        src = "const { useState } = React;\n"
        assert _kinds(src) == [("react-destructure", 1)]


# ---------------------------------------------------------------------------
# Suppression + frontmatter
# ---------------------------------------------------------------------------
class TestSuppression:
    @pytest.mark.parametrize(
        "line",
        [
            "const { a } = window.__X;",        # pattern C
            "const X = window.__X;",            # pattern A
            "const { useState } = React;",      # pattern B
        ],
    )
    def test_escape_marker_lookback(self, line):
        src = f"// <!-- window-x-no-fallback: ignore -->\n{line}\n"
        assert _kinds(src) == []

    def test_frontmatter_stripped_line_numbers_preserved(self):
        src = (
            "---\n"
            "title: x\n"
            "---\n"
            "const { a } = window.__X;\n"
        )
        assert _kinds(src) == [("global-destructure", 4)]


# ---------------------------------------------------------------------------
# scan() — file-level plumbing
# ---------------------------------------------------------------------------
class TestScan:
    def test_outside_repo_path_keeps_absolute_display(self, tmp_path):
        f = tmp_path / "bad.jsx"
        f.write_text("const X = window.__X;\n", encoding="utf-8")
        findings = lint.scan([f])
        assert [(p, n, k) for p, n, k, _ in findings] == [(f, 1, "global-read")]

    def test_nonexistent_and_dir_paths_skipped(self, tmp_path):
        assert lint.scan([tmp_path / "missing.jsx", tmp_path]) == []

    def test_undecodable_file_skipped(self, tmp_path):
        f = tmp_path / "binary.js"
        f.write_bytes(b"\xff\xfe\x00const X = window.__X;\n")
        assert lint.scan([f]) == []


class TestCollectDefaultPaths:
    def test_missing_root_skipped(self, tmp_path, monkeypatch):
        root = tmp_path / "tools" / "portal" / "src" / "interactive"
        root.mkdir(parents=True)
        (root / "a.jsx").write_text("// ok\n", encoding="utf-8")
        # getting-started root deliberately absent → continue branch.
        monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
        paths = lint.collect_default_paths()
        assert [p.name for p in paths] == ["a.jsx"]


# ---------------------------------------------------------------------------
# main() — CLI exit codes + report rendering
# ---------------------------------------------------------------------------
class TestMain:
    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["check_window_x_no_fallback.py", *argv])
        return lint.main()

    def test_clean_paths_exit_ok(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "clean.jsx"
        f.write_text("const t = window.__t || ((zh, en) => en);\n", encoding="utf-8")
        assert self._run(monkeypatch, ["--ci", str(f)]) == 0
        assert "✓" in capsys.readouterr().out

    def test_violations_ci_exit_1_with_full_report(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "bad.jsx"
        f.write_text(
            "const X = window.__X;\n"
            "const { useState } = React;\n"
            "const { a, b } = window.__shared;\n",
            encoding="utf-8",
        )
        assert self._run(monkeypatch, ["--ci", str(f)]) == 1
        out = capsys.readouterr().out
        assert "3 violations" in out
        # All three report sections render.
        assert "Module-scope no-fallback global read (1)" in out
        assert "React destructure" in out
        assert "Module-scope destructure of a window global (1)" in out
        # Fix hints include the destructure form.
        assert "const { a, b } = window.__X;" in out

    def test_violations_without_ci_exit_ok(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "bad.jsx"
        f.write_text("const X = window.__X;\n", encoding="utf-8")
        assert self._run(monkeypatch, [str(f)]) == 0
        assert "1 violations" in capsys.readouterr().out

    def test_no_args_uses_default_scan(self, monkeypatch, capsys):
        # Live-repo default scan must be clean (the Tab ESM migration).
        assert self._run(monkeypatch, ["--ci"]) == 0
        assert "clean" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Live dogfood — repo must be clean after the Tab ESM migration
# ---------------------------------------------------------------------------
class TestLiveRepo:
    def test_default_scan_clean(self):
        findings = lint.scan(lint.collect_default_paths())
        assert findings == [], (
            "dev-rules §S6 violations in repo: "
            + "; ".join(f"{p}:{n} {s}" for p, n, _, s in findings)
        )
