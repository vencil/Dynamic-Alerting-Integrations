"""Tests for gen_adr_checks.py — an ADR's list of checks is generated from the code.

An ADR that states its decisions as checks numbers each clause (``- **3b** …``
under a numbered section) and holds a generated block naming that section
(``<!-- BEGIN GENERATED: adr-checks section=9 -->``). A test or CI step that
implements a clause carries a marker comment ``adr-check: ADR-NNN/S.C`` (S the
section, C the clause). The block lists, per clause, the files (and test
names) that carry its marker, and says "no check yet" for a clause nobody
marks — so the list cannot drift from the code, and a gap cannot hide.

Covered here: clause parsing, marker parsing (several ids on one line, the
name of the test that follows), rendering per language, the block being
replaced in place, and the failures: a marker naming a clause or ADR that
does not exist, a duplicate clause id, a marked ADR with no block, drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "dx"))

import gen_adr_checks as gen  # noqa: E402

BEGIN, END = gen.begin_marker("9"), gen.END

ADR_ZH = f"""# ADR-042: 範例

### 9. 決定以測試表達

1. 平面：
   - **1a** 只註冊列舉的路徑。
   - **1b** 其餘回 404。
2. 其他：
   - **2a** 沒有轉送。

### 10. 別的小節

- **1a** 不在 §9 裡，不算子句。

## 實作位置

{BEGIN}
(stale)
{END}

## 相關
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def test_clauses_are_read_from_the_numbered_section_only() -> None:
    assert gen.parse_clauses(ADR_ZH, "9") == ["1a", "1b", "2a"]


def test_a_duplicate_clause_id_is_an_error() -> None:
    text = ADR_ZH.replace("**2a**", "**1a**")
    with pytest.raises(gen.AdrChecksError, match="1a"):
        gen.parse_clauses(text, "9")


@pytest.mark.parametrize(
    "rel, body, want",
    [
        ("a_test.go", "// adr-check: ADR-042/9.1a\nfunc TestRoutes(t *testing.T) {}\n", "TestRoutes"),
        ("a_test.go", "// adr-check: ADR-042/9.1a\nfunc (s *suite) TestM() {}\n", "TestM"),
        ("t.py", "# adr-check: ADR-042/9.1a\n@pytest.mark.parametrize('x', [1])\ndef test_x(x):\n", "test_x"),
        ("t.test.ts", "// adr-check: ADR-042/9.1a\ndescribe('apiFetch: the path', () => {\n", "apiFetch: the path"),
        ("t.test.ts", "// adr-check: ADR-042/9.1a\n  it.each([1])('refuses %s', () => {\n", "refuses %s"),
        (".github/workflows/ci.yml", "      # adr-check: ADR-042/9.1a\n      - name: \"Scope check (x)\"\n", "Scope check (x)"),
        ("run.sh", "#!/bin/bash\n# adr-check: ADR-042/9.1a\nset -e\n", None),
    ],
)
def test_a_marker_names_the_test_that_follows(tmp_path, rel, body, want) -> None:
    root = _tree(tmp_path, {rel: body})
    (m,) = gen.collect_markers(root, [rel])
    assert (m.adr, m.section, m.clause, m.path, m.name) == ("042", "9", "1a", rel, want)


def test_a_marker_in_markdown_is_an_example_not_a_marker(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/x.md": "```go\n// adr-check: ADR-042/9.1a\nfunc TestX() {}\n```\n"})
    assert gen.collect_markers(root, ["docs/x.md"]) == []


def test_one_marker_line_can_name_several_clauses(tmp_path) -> None:
    root = _tree(tmp_path, {"x_test.go": "// adr-check: ADR-042/9.1a ADR-042/9.2a\nfunc TestBoth() {}\n"})
    got = sorted((m.clause, m.name) for m in gen.collect_markers(root, ["x_test.go"]))
    assert got == [("1a", "TestBoth"), ("2a", "TestBoth")]


def test_render_lists_marked_clauses_and_names_the_gaps() -> None:
    markers = [
        gen.Marker("042", "9", "1a", "b/x_test.go", "TestB"),
        gen.Marker("042", "9", "1a", "a/y.py", "test_a"),
        gen.Marker("042", "9", "1a", "a/y.py", "test_b"),
        gen.Marker("042", "9", "2a", "run.sh", None),
    ]
    zh = gen.render(["1a", "1b", "2a"], markers, "zh")
    assert zh.splitlines() == [
        "- **1a**：`a/y.py`（`test_a`、`test_b`）；`b/x_test.go`（`TestB`）",
        "- **1b**：尚無檢查",
        "- **2a**：`run.sh`",
    ]
    en = gen.render(["1a", "1b", "2a"], markers, "en")
    assert en.splitlines()[0] == "- **1a**: `a/y.py` (`test_a`, `test_b`); `b/x_test.go` (`TestB`)"
    assert en.splitlines()[1] == "- **1b**: no check yet"


def _adr_tree(tmp_path: Path, extra: dict[str, str]) -> Path:
    files = {"docs/adr/042-example.md": ADR_ZH,
             "docs/adr/042-example.en.md": ADR_ZH.replace("決定以測試表達", "Decisions as tests")}
    files.update(extra)
    return _tree(tmp_path, files)


def test_write_then_check_is_clean(tmp_path) -> None:
    root = _adr_tree(tmp_path, {"t_test.go": "// adr-check: ADR-042/9.1a\nfunc TestRoutes() {}\n"})
    paths = ["t_test.go"]
    changed = gen.run(root, paths, write=True)
    assert sorted(changed) == ["docs/adr/042-example.en.md", "docs/adr/042-example.md"]
    zh = (root / "docs/adr/042-example.md").read_text(encoding="utf-8")
    assert "- **1a**：`t_test.go`（`TestRoutes`）" in zh
    assert "- **1b**：尚無檢查" in zh
    assert "(stale)" not in zh
    assert "- **1b**: no check yet" in (root / "docs/adr/042-example.en.md").read_text(encoding="utf-8")
    assert gen.run(root, paths, write=False) == []


def test_check_reports_drift(tmp_path) -> None:
    root = _adr_tree(tmp_path, {"t_test.go": "// adr-check: ADR-042/9.1a\nfunc TestRoutes() {}\n"})
    assert sorted(gen.run(root, ["t_test.go"], write=False)) == [
        "docs/adr/042-example.en.md", "docs/adr/042-example.md"]


@pytest.mark.parametrize("marker, needle", [
    ("ADR-042/9.9z", "9.9z"),   # no such clause
    ("ADR-043/9.1a", "ADR-043"),  # no such ADR
    ("ADR-042/10.1a", "section=10"),  # the ADR has no block for section 10
])
def test_a_marker_naming_nothing_is_an_error(tmp_path, marker, needle) -> None:
    root = _adr_tree(tmp_path, {"t_test.go": f"// adr-check: {marker}\nfunc TestX() {{}}\n"})
    with pytest.raises(gen.AdrChecksError, match=needle):
        gen.run(root, ["t_test.go"], write=False)


def test_a_marked_adr_without_a_block_is_an_error(tmp_path) -> None:
    root = _adr_tree(tmp_path, {"t_test.go": "// adr-check: ADR-042/9.1a\nfunc TestX() {}\n"})
    p = root / "docs/adr/042-example.en.md"
    p.write_text(p.read_text(encoding="utf-8").replace(BEGIN, "").replace(END, ""), encoding="utf-8")
    with pytest.raises(gen.AdrChecksError, match="042-example.en.md"):
        gen.run(root, ["t_test.go"], write=False)


def test_a_malformed_marker_is_an_error(tmp_path) -> None:
    root = _adr_tree(tmp_path, {"t_test.go": "// adr-check: ADR-42/9\nfunc TestX() {}\n"})
    with pytest.raises(gen.AdrChecksError, match="t_test.go:1"):
        gen.run(root, ["t_test.go"], write=False)


def test_the_repository_is_in_sync() -> None:
    assert gen.main(["--check"]) == 0


@pytest.mark.parametrize("variant", [
    "   * **1c** star bullet.",
    "   - **1c**: colon after the id.",
    "   - **1c**：fullwidth colon.",
    "   - **1C** capital letter.",
])
def test_a_clause_written_off_format_is_an_error_not_a_silent_drop(variant) -> None:
    text = ADR_ZH.replace("   - **2a** 沒有轉送。", "   - **2a** 沒有轉送。\n" + variant)
    with pytest.raises(gen.AdrChecksError, match="clause"):
        gen.parse_clauses(text, "9")


def test_a_code_fence_inside_the_section_neither_ends_it_nor_adds_clauses() -> None:
    text = ADR_ZH.replace("2. 其他：", "```sh\n# a comment, not a heading\n- **9z** not a clause\n```\n2. 其他：")
    assert gen.parse_clauses(text, "9") == ["1a", "1b", "2a"]


def test_the_languages_must_declare_the_same_clauses(tmp_path) -> None:
    root = _adr_tree(tmp_path, {})
    p = root / "docs/adr/042-example.en.md"
    p.write_text(p.read_text(encoding="utf-8").replace("   - **1b** 其餘回 404。\n", ""), encoding="utf-8")
    with pytest.raises(gen.AdrChecksError, match="1b"):
        gen.run(root, [], write=False)


def test_two_blocks_for_one_section_are_an_error(tmp_path) -> None:
    root = _adr_tree(tmp_path, {})
    p = root / "docs/adr/042-example.md"
    p.write_text(p.read_text(encoding="utf-8") + f"\n{BEGIN}\n{END}\n", encoding="utf-8")
    with pytest.raises(gen.AdrChecksError, match="twice"):
        gen.run(root, [], write=False)


@pytest.mark.parametrize("line", [
    "// adr-check：ADR-042/9.1a",      # fullwidth colon
    "// adr-check : ADR-042/9.1a",     # space before the colon
    "// ADR-CHECK: ADR-042/9.1a",      # upper case
    "// adr_check: ADR-042/9.1a",      # underscore
    "x := 1 // adr-check: ADR-042/9.1a",  # trailing a statement
])
def test_a_near_miss_marker_is_an_error_not_ignored(tmp_path, line) -> None:
    root = _tree(tmp_path, {"t_test.go": line + "\nfunc TestX() {}\n"})
    with pytest.raises(gen.AdrChecksError, match="t_test.go:1"):
        gen.collect_markers(root, ["t_test.go"])


@pytest.mark.parametrize("rel, line", [
    ("q.sql", "-- adr-check: ADR-042/9.1a"),
    ("t.tpl", "{{/* adr-check: ADR-042/9.1a */}}"),
    ("x_test.go", "/// adr-check: ADR-042/9.1a"),
])
def test_other_comment_styles_are_read(tmp_path, rel, line) -> None:
    root = _tree(tmp_path, {rel: line + "\n"})
    (m,) = gen.collect_markers(root, [rel])
    assert m.clause == "1a"


def test_a_blank_line_ends_the_search_for_the_name(tmp_path) -> None:
    root = _tree(tmp_path, {"x_test.go": "// adr-check: ADR-042/9.1a\n\nfunc helper() {}\n"})
    (m,) = gen.collect_markers(root, ["x_test.go"])
    assert m.name is None


def test_a_yaml_name_key_outside_ci_is_not_a_step_name(tmp_path) -> None:
    root = _tree(tmp_path, {"helm/x/Chart.yaml": "# adr-check: ADR-042/9.1a\nname: chart\n"})
    (m,) = gen.collect_markers(root, ["helm/x/Chart.yaml"])
    assert m.name is None


def test_a_longer_fence_is_closed_only_by_a_matching_fence() -> None:
    # A four-backtick block may show a three-backtick fence and a heading;
    # neither ends the block, and the clause after the real close is read.
    text = ADR_ZH.replace("2. 其他：", "````md\n```\n### 10. 範例標題\n```\n````\n2. 其他：")
    assert gen.parse_clauses(text, "9") == ["1a", "1b", "2a"]


def test_a_tilde_fence_is_not_closed_by_backticks() -> None:
    text = ADR_ZH.replace("2. 其他：", "~~~\n```\n### 10. 範例\n~~~\n2. 其他：")
    assert gen.parse_clauses(text, "9") == ["1a", "1b", "2a"]
