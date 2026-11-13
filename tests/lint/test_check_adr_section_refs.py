"""Self-test for check_adr_section_refs.py (ADR-027 §9 item 8).

An ``ADR-NNN §<section>`` reference must name a heading of that ADR, by its
number, code or name; ``ADR-027`` followed by a section code of the withdrawn
2026-07 draft fails whatever follows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_adr_section_refs as lint  # noqa: E402

ADR_024 = """# ADR-024: an example

## 狀態

## 決策

### 3. Custom Alerts：平台 authored 的 recipe

### 5. 驗證雙層

## 相鄰破口：gateway fail-open 的顯式風險接受

### D3 honest boundary + PII 最小化

#### ⚠️ Implementation gotcha：Regex 邊界

```yaml
## 9. a heading inside a code block is not a heading
```
"""


@pytest.fixture()
def adrs(tmp_path: Path) -> dict[str, list[lint.Heading]]:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "024-example.md").write_text(ADR_024, encoding="utf-8")
    (tmp_path / "docs" / "adr" / "024-example.en.md").write_text(
        "# ADR-024\n\n## Consequences\n\n### 7. Forecast recipe\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "adr" / "027-identity.md").write_text("# ADR-027\n\n## 決定\n\n### 7. 範圍\n", encoding="utf-8")
    return lint.load_adrs(tmp_path)


def _sections(adrs, line: str) -> list[str]:
    return [f.section for f in lint.scan_text("x.go", line, adrs)]


@pytest.mark.parametrize(
    "line",
    [
        "see ADR-024 §3 for the recipe library",
        "(ADR-024 §5.)",
        "ADR-024 §7 and ADR-024 §Consequences",  # the English file's headings count too
        "ADR-024 §Custom alerts",  # name after the leading number
        "ADR-024 §驗證雙層的兩層",  # CJK running text: the heading is a prefix
        "ADR-024 §fail-open",  # a name inside the heading
        "ADR-024 §D3",  # a leading code
        "ADR-024 §Implementation gotcha",  # leading symbols are not the name
        "ADR-024 §3、§5 and ADR-024's §7",
        "ADR-027 §7",
    ],
)
# adr-check: ADR-027/9.8a
def test_existing_sections_pass(adrs, line):
    assert _sections(adrs, line) == []


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("ADR-024 §S6b-2 (#741)", ["S6b-2"]),
        ("ADR-024 §9", ["9"]),  # only inside a code block
        ("ADR-024 §4", ["4"]),
        ("ADR-024 §3、§8", ["8"]),  # a chained section is checked too
        ("ADR-024 §3 / §Nonexistent", ["Nonexistent"]),
        ("ADR-099 §1", ["1"]),  # no such ADR
    ],
)
# adr-check: ADR-027/9.8a
def test_missing_sections_fail(adrs, line, want):
    assert _sections(adrs, line) == want


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("ADR-027 / LD-6 P4b): org scope", "LD-6 P4b"),
        ("ADR-027 D2-B O1, GHSA", "D2-B"),
        ("per ADR-027 MED-8, not open", "MED-8"),
        ("(ADR-027 L7 identity)", "L7"),
        ("ADR-027 / LD-6)", "LD-6"),
    ],
)
# adr-check: ADR-027/9.8b
def test_withdrawn_draft_codes_fail(adrs, line, code):
    assert _sections(adrs, line) == [code]


# adr-check: ADR-027/9.8b
def test_draft_code_needs_adr_027_before_it(adrs):
    # MED-8 alone is a security-finding ID, not an ADR reference.
    assert _sections(adrs, "MED-8: an empty _rbac.yaml denies everyone") == []


def test_find_findings_scans_tracked_files_and_skips_changelog(tmp_path: Path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "024-example.md").write_text(ADR_024, encoding="utf-8")
    (tmp_path / "a.go").write_text("// ADR-024 §S5 validation\n// ADR-024 §5 validation\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("- ADR-024 §S5 (released)\n", encoding="utf-8")
    (tmp_path / "untracked.md").write_text("ADR-024 §S5\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=30)
    subprocess.run(["git", "add", "docs", "a.go", "CHANGELOG.md"], cwd=tmp_path, check=True, timeout=30)
    found = lint.find_findings(tmp_path)
    assert [(f.path, f.line, f.section) for f in found] == [("a.go", 1, "S5")]


def test_an_untracked_adr_does_not_satisfy_a_reference(tmp_path: Path):
    # A new ADR left unstaged must not make a reference pass locally that
    # fails on CI's clean checkout.
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "024-example.md").write_text(ADR_024, encoding="utf-8")
    (tmp_path / "docs" / "adr" / "099-new.md").write_text("# ADR-099\n\n## 1. New\n", encoding="utf-8")
    (tmp_path / "a.go").write_text("// ADR-099 §1 new decision\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=30)
    subprocess.run(["git", "add", "docs/adr/024-example.md", "a.go"], cwd=tmp_path, check=True, timeout=30)
    found = lint.find_findings(tmp_path)
    assert [(f.path, f.adr, f.reason) for f in found] == [("a.go", "099", "no such ADR under docs/adr/")]


def test_find_findings_without_adrs_cannot_run(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=30)
    with pytest.raises(FileNotFoundError):
        lint.find_findings(tmp_path)


_ONE = [lint.Finding("a.go", 1, "024", "S5", "no heading of the ADR has this number or name")]


@pytest.mark.parametrize("argv, findings, want", [
    ([], _ONE, lint.EXIT_OK),             # report-only without --ci
    (["--ci"], _ONE, lint.EXIT_VIOLATION),
    (["--ci"], [], lint.EXIT_OK),
])
def test_main_exit_code(monkeypatch, capsys, argv, findings, want):
    monkeypatch.setattr(sys, "argv", ["check_adr_section_refs.py", *argv])
    monkeypatch.setattr(lint, "find_findings", lambda: findings)
    assert lint.main() == want
    if findings:
        assert "a.go:1: ADR-024 S5" in capsys.readouterr().out


def test_main_without_adrs_is_a_caller_error(monkeypatch):
    def no_adrs():
        raise FileNotFoundError("no tracked ADR")
    monkeypatch.setattr(sys, "argv", ["check_adr_section_refs.py", "--ci"])
    monkeypatch.setattr(lint, "find_findings", no_adrs)
    assert lint.main() == lint.EXIT_CALLER_ERROR


def test_main_json_report_carries_the_ci_status(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_adr_section_refs.py", "--json", "--ci"])
    monkeypatch.setattr(lint, "find_findings", lambda: _ONE)
    rep = lint.main()
    assert isinstance(rep, lint.Report)
    assert rep.status == lint.EXIT_VIOLATION
    assert rep.document == [{"path": "a.go", "line": 1, "adr": "024", "section": "S5",
                        "reason": "no heading of the ADR has this number or name"}]


# adr-check: ADR-027/9.8a
def test_the_repository_is_clean():
    assert lint.find_findings() == []
