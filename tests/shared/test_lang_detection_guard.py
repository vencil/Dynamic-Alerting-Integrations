"""防回潮 gate：ops 工具禁止自建 `_detect_lang`，語言偵測一律走
`_lib_validation.detect_cli_lang`（經 `_lib_python` facade）。

Rationale（da-tools ROI r3 W2）：
`config_history.py` 曾自建 `_detect_lang()`——`for var in (DA_LANG, LC_ALL,
LANG)` 逐一 **只檢查 zh 前綴**，`DA_LANG=en` 不 return、落到下一個變數 →
**`DA_LANG=en` 輸給 `LC_ALL=zh`**，違反 canonical 契約（tests/shared/
test_property_tools.py 明釘「DA_LANG=en wins over LC_ALL=zh」；
`detect_cli_lang` 對 zh/en 前綴都會提早 return，顯式設定必勝）。其他 13
支 ops 工具皆循 `from _lib_python import detect_cli_lang` 慣例。本 gate
防止同型 shadow 再長回來。

Pattern 精準度：全文掃描（非 line-based）`def _detect_lang`——formatter
拆行（`def _detect_lang\n(...)`）一樣命中；合法的 `detect_cli_lang` import
與呼叫不帶 `def _`、不會誤殺。scope 限 `scripts/tools/ops/`（lint/ 下的
`detect_cli_lang` local 複本為既有另案、名稱亦不同）。
"""
from __future__ import annotations

import re
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"

# def 與函式名之間允許任意空白（含換行），抓 formatter 變體。
_ROGUE_SHAPE = re.compile(r"def\s+_detect_lang\b")


def test_rogue_shape_positive_control():
    """Positive control：regex 被誤改時本測試先紅，防 guard 靜默失效。"""
    assert _ROGUE_SHAPE.search("def _detect_lang(x):")
    assert _ROGUE_SHAPE.search("def  _detect_lang\n(")  # formatter 變體
    assert not _ROGUE_SHAPE.search("def _detect_langs(x):")  # \b 邊界
    assert not _ROGUE_SHAPE.search("from _lib_python import detect_cli_lang")


def test_no_rogue_detect_lang_in_ops_tools():
    """scripts/tools/ops/**/*.py 禁止 `def _detect_lang`（用 detect_cli_lang）。"""
    assert OPS_DIR.is_dir(), f"ops dir not found: {OPS_DIR}"
    offenders: list[str] = []
    for py in sorted(OPS_DIR.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for m in _ROGUE_SHAPE.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{py.relative_to(OPS_DIR).as_posix()}:{lineno}")
    assert offenders == [], (
        "ops 工具禁止自建 `_detect_lang`（DA_LANG=en 會輸給 LC_ALL=zh 的"
        " rogue 實作已在 r3 W2 移除）；請改 `from _lib_python import"
        " detect_cli_lang`。違規：\n" + "\n".join(offenders)
    )
