"""test_operator_generate_doc_flags.py — operator-generate 文件旗標 ↔ parser（#2090）

英文 cli-reference 與中英速查表列了 operator-generate 沒有的 `--split`／
`--apply`／`--include-servicemonitor`，照打會被 argparse 以 exit 2 拒絕；
實際存在的旗標反而查不到。

指令列（fenced block 與 inline code）裡的旗標由 `check_cli_contract.py`
（pre-commit `cli-contract-check`）負責，這裡不重複。這裡補它看不到的兩件事：
  * 速查表那一列的旗標是純文字、不在 code span 裡，須存在於 parser；
  * cli-reference 中英兩版的選項表各自列齊 parser 的全部旗標（`--help` 除外），
    否則實際存在的旗標查不到說明。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_REPO / "scripts" / "tools"))

import operator_generate as og  # noqa: E402

_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*|-[a-zA-Z])(?![\w-])")
_CLI_REFS = ["docs/cli-reference.md", "docs/cli-reference.en.md"]
_CHEAT_SHEETS = ["docs/cheat-sheet.md", "docs/cheat-sheet.en.md"]


def _parser_flags() -> set:
    return {s for a in og.build_arg_parser()._actions for s in a.option_strings}


def _section(path: str) -> str:
    text = (_REPO / path).read_text(encoding="utf-8")
    start = text.index("#### operator-generate\n")
    return text[start:text.index("\n---\n", start)]


def _table_flags(section: str) -> set:
    return {m for line in section.splitlines() if line.startswith("| `-")
            for m in _FLAG.findall(line.split("|")[1])}


def _cheat_sheet_flags(path: str) -> set:
    for line in (_REPO / path).read_text(encoding="utf-8").splitlines():
        if line.startswith("| `operator-generate` |"):
            return set(_FLAG.findall(line.split("|")[3]))
    raise AssertionError(f"{path}: no operator-generate row")


@pytest.mark.parametrize("path", _CLI_REFS)
def test_cli_reference_table_lists_every_flag(path):
    missing = _parser_flags() - {"-h", "--help"} - _table_flags(_section(path))
    assert not missing, f"{path} operator-generate options table omits: {sorted(missing)}"


@pytest.mark.parametrize("path", _CHEAT_SHEETS)
def test_cheat_sheet_mentions_only_real_flags(path):
    flags = _cheat_sheet_flags(path)
    assert flags, f"{path}: extracted no flags from the operator-generate row"
    unknown = flags - _parser_flags()
    assert not unknown, f"{path} operator-generate row lists flags the tool does not have: {sorted(unknown)}"
