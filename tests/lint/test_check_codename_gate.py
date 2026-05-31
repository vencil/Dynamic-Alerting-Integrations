"""Tests for scripts/tools/lint/check_codename_gate.py.

Layer 2 glossary-driven codename gate (#469). The lint replaces Layer 1's
hard-coded regex catalog with a glossary-sourced SSOT: internal codename
templates + approved customer-facing terms are parsed from docs/glossary.md,
and customer-facing files are scanned for (a) confirmed internal leaks and
(b) unregistered shape tokens to seed the glossary during the warn-mode soak.

Coverage:
  - compile_template: placeholder→regex, left look-behind (AB-1 ≠ {AE}-{N}),
    A–E bound, literal-escape of '.', '#'
  - load_glossary: **Term** approved extraction incl. FULLWIDTH （）parens,
    parenthetical abbreviation, Internal-table template parse, header/sep skip
  - _is_safe_token: ADR/TRK/CVE/SHA/UTF/semver pass; codenames don't
  - scan_line: internal hit (hard), approved pass, safe pass, unregistered
    discovery, internal span not double-counted as unregistered
  - real repo glossary: parses to a non-empty internal set (guards against a
    section rename silently disarming the gate)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_codename_gate.py"

_spec = importlib.util.spec_from_file_location("check_codename_gate", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_codename_gate"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# compile_template
# ============================================================

@pytest.mark.parametrize("template,should_match,should_not", [
    ("TRK-{N}", ["TRK-1", "TRK-301"], ["TRK-", "ATRK-1", "TRKX-1"]),
    ("DEC-{X}", ["DEC-B", "DEC-f"], ["DEC-12", "DEC-"]),
    ("{AE}-{N}", ["B-1", "C-12", "E-9"], ["F-1", "Z-1", "AB-1", "1-1"]),
    ("S#{N}", ["S#74", "S#101"], ["S#", "XS#1"]),
    ("Phase .{x}", ["Phase .a", "Phase .c"], ["Phase .A", "Phase a", "Phase 1"]),
    ("Track {X}", ["Track A", "Track b"], ["Track 1", "Tracking A"]),
    ("Wave {N}", ["Wave 3", "Wave 12"], ["Wave A", "Wavelet 3"]),
    ("v{N}.{N}.{N}-final", ["v2.8.0-final"], ["v2.8.0", "v2.8.0-rc1"]),
])
def test_compile_template(template, should_match, should_not):
    rx = mod.compile_template(template)
    for s in should_match:
        assert rx.search(s), f"{template!r} should match {s!r}"
    for s in should_not:
        assert not rx.search(s), f"{template!r} should NOT match {s!r}"


def test_compile_template_left_boundary_blocks_embedded():
    # The look-behind must prevent matching inside a longer alnum run.
    rx = mod.compile_template("{AE}-{N}")
    assert not rx.search("FOOB-1")  # B preceded by word char → no match


# ============================================================
# load_glossary
# ============================================================

_GLOSSARY_FIXTURE = """\
# 術語表

## A

**Alertmanager**
:   routing component.

**AST 遷移引擎 (AST Migration Engine)**
:   migrate core.

## M

**Maintenance Mode（維護模式）**
:   one of three states.

## 內部代號 — 禁止用於對外文件

> warning prose, template syntax explained here.

| 代號模式 | 說明 | 對外應改用 |
|---|---|---|
| `TD-{N}` | legacy ticket | feature name |
| `DEC-{X}` | decision tag | outcome |
| `{AE}-{N}` | letter-prefix id | feature name |

<!-- comment row should be ignored -->

## 相關資源

| 資源 | 用途 |
|------|------|
| x | y |
"""


@pytest.fixture()
def glossary(tmp_path):
    p = tmp_path / "glossary.md"
    p.write_text(_GLOSSARY_FIXTURE, encoding="utf-8")
    return p


def test_load_glossary_approved_terms(glossary):
    approved, _internal = mod.load_glossary(glossary)
    # ASCII-paren abbreviation + main term
    assert "alertmanager" in approved
    assert "ast migration engine" in approved  # parenthetical
    # FULLWIDTH-paren ZH term → clean English token registered
    assert "maintenance mode" in approved


def test_load_glossary_internal_templates(glossary):
    _approved, internal = mod.load_glossary(glossary)
    templates = {t for t, _rx in internal}
    assert templates == {"TD-{N}", "DEC-{X}", "{AE}-{N}"}
    # The "## 相關資源" table rows must NOT leak in as templates.
    assert all("|" not in t for t in templates)


def test_load_glossary_internal_regex_works(glossary):
    _approved, internal = mod.load_glossary(glossary)
    by_tmpl = {t: rx for t, rx in internal}
    assert by_tmpl["TD-{N}"].search("see TD-30 here")
    assert by_tmpl["{AE}-{N}"].search("B-4 leaked")


# ============================================================
# _is_safe_token
# ============================================================

@pytest.mark.parametrize("token,safe", [
    ("ADR-024", True),
    ("TRK-301", True),
    ("CVE-2024-1234", True),
    ("SHA-256", True),
    ("UTF-8", True),
    ("v2.8.0", True),
    ("DEC-B", False),
    ("B-1", False),
    ("Migration Toolkit", False),
])
def test_is_safe_token(token, safe):
    assert mod._is_safe_token(token) is safe


# ============================================================
# scan_line
# ============================================================

def _internal(templates):
    return [(t, mod.compile_template(t)) for t in templates]


def test_scan_line_internal_hit_is_hard():
    internal = _internal(["TD-{N}", "{AE}-{N}"])
    hits, unreg = mod.scan_line("regression in TD-30 and B-4", internal, set())
    matched = {m for _t, m in hits}
    assert matched == {"TD-30", "B-4"}
    # Internal spans must not double-report as unregistered.
    assert "TD-30" not in unreg and "B-4" not in unreg


def test_scan_line_approved_passes():
    internal = _internal(["TD-{N}"])
    approved = {"rule pack", "tenant manager"}
    hits, unreg = mod.scan_line("the Rule Pack and Tenant Manager", internal, approved)
    assert hits == []
    assert unreg == []  # both two-word-cap tokens are approved


def test_scan_line_safe_token_not_unregistered():
    internal = _internal(["TD-{N}"])
    hits, unreg = mod.scan_line("see ADR-019 and TRK-301 and CVE-2024-1", internal, set())
    assert hits == []
    assert unreg == []  # all three are built-in safe identifiers


def test_scan_line_unregistered_discovery():
    internal = _internal(["TD-{N}"])
    hits, unreg = mod.scan_line("the Quantum Sync feature", internal, set())
    assert hits == []
    assert "Quantum Sync" in unreg


def test_scan_line_adjacent_family_extension_surfaces():
    # Regression for the substring-suppression FN: a registered internal
    # codename (DEC-B) must NOT hide a distinct adjacent shape token that
    # merely shares its prefix (DEC-Beta) — that family-extension is exactly
    # what discovery exists to catch.
    internal = _internal(["DEC-{X}"])
    hits, unreg = mod.scan_line("DEC-B and DEC-Beta ship", internal, set())
    assert {m for _t, m in hits} == {"DEC-B"}
    assert "DEC-Beta" in unreg


def test_scan_line_plural_fold():
    # "Rule Packs" (plural) should pass when "Rule Pack" (singular) is approved.
    internal = _internal(["TD-{N}"])
    _hits, unreg = mod.scan_line("the Rule Packs section", internal, {"rule pack"})
    assert unreg == []


def test_scan_line_internal_not_double_reported():
    internal = _internal(["TD-{N}"])
    hits, unreg = mod.scan_line("regression in TD-30", internal, set())
    assert {m for _t, m in hits} == {"TD-30"}
    assert "TD-30" not in unreg  # exact-match de-dup, not surfaced as unregistered


# ============================================================
# Real repo glossary smoke test
# ============================================================

def test_real_glossary_has_internal_templates():
    """If the Internal section is renamed/removed, the gate silently disarms.

    main() returns exit 2 on an empty internal set; this asserts the live
    glossary still parses, so a heading rename can't pass CI unnoticed.
    """
    approved, internal = mod.load_glossary()
    assert internal, "live docs/glossary.md must yield internal codename templates"
    assert len(approved) > 20


def test_zh_en_internal_table_parity():
    """ZH↔EN internal-codename table parity guard (review M3).

    The gate parses only the ZH SSOT; a maintainer editing only the EN table
    would silently diverge (check_bilingual_structure diffs headings, not table
    rows). Assert both editions register the identical template set so the EN
    mirror can't drift out of sync unnoticed.
    """
    import re as _re
    en_path = REPO_ROOT / "docs" / "glossary.en.md"
    en_section_re = _re.compile(r"^##\s+Explicitly Internal")
    _zh_appr, zh_int = mod.load_glossary()
    _en_appr, en_int = mod.load_glossary(en_path, internal_section_re=en_section_re)
    zh_templates = {t for t, _rx in zh_int}
    en_templates = {t for t, _rx in en_int}
    assert zh_templates == en_templates, (
        f"ZH/EN internal codename tables diverged: "
        f"ZH-only={zh_templates - en_templates}, EN-only={en_templates - zh_templates}"
    )
