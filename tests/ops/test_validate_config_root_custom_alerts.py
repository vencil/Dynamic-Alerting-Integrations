"""#3042：根目錄 `_defaults.yaml` 的 `_custom_alerts` 沒有讀取端讀（exporter 從不
送出，編譯器照 exporter 送出的編），validate-config 的兩種包裝檢查都會報出，並
寫出清單該放的地方；句子與 da-guard 的相同。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import validate_config as vc

_REPO = Path(__file__).resolve().parents[2]


def _tree(root: Path, body: str) -> str:
    (root / "_defaults.yaml").write_text(body, encoding="utf-8")
    return str(root)


@pytest.mark.parametrize("body, check, want", [
    ("_custom_alerts: []\nmysql_connections: 70\n", "root_defaults", True),
    ("defaults:\n  x: 1\n_custom_alerts: []\n", "defaults_wrapper", True),
    ("defaults:\n  x: 1\nmysql_connections: 70\n", "defaults_wrapper", False),
], ids=["unwrapped", "wrapped", "wrapped-no-alerts"])
def test_a_root_custom_alerts_line_says_where_the_list_goes(tmp_path, body, check, want):
    d = _tree(tmp_path, body)
    r = vc.check_root_defaults(d) if check == "root_defaults" else vc.check_defaults_wrapper(d)
    assert r["status"] == vc.FAIL, r
    assert any(vc._CUSTOM_ALERTS_ROOT_FIX in line for line in r["details"]) is want, r["details"]


def test_the_sentence_is_da_guards():
    go = (_REPO / "components/threshold-exporter/app/internal/guard/rootdefaults.go").read_text(encoding="utf-8")
    m = re.search(r'const customAlertsRootFix = ((?:"[^"]*"\s*\+?\s*)+)', go)
    assert m, "customAlertsRootFix not found"
    assert "".join(re.findall(r'"([^"]*)"', m.group(1))) == vc._CUSTOM_ALERTS_ROOT_FIX
