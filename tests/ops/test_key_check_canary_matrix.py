"""#3703：key verdict 文件的 `key_checks`（`version` label、已改名的鍵）不把作者寫的鍵與標籤值帶進產生器的訊息。

`test_reserved_value_canary_matrix.py` 守保留鍵的值；這裡守 `key_checks` 這條通道。Go 交出的是結構化記錄
（kind、key、head、replacement_head、reason），`reason` 是每個 kind 一句的常數句；產生器那一行由
Python 用常數句型組成（`_lib_key_verdict.key_check_line`），鍵與指標名只經 `shown_key` 出去。

* 矩陣由 kind 的封閉集合推導：`_lib_tenant_values.KEY_CHECK_KINDS`，它由
  `tests/shared/test_key_check_kinds_parity.py` 對照 Go 的 `config.KeyCheckKinds` 常數。Go 多一個
  kind 而這裡沒有寫法 → `test_the_matrix_is_every_kind` 紅。
* 每個 kind 把 canary 放在三個位置：標籤值、標籤名、指標名。指標名那格用超過 64 字元的名字——
  名稱形的指標名本來就照印（`shown_key` 的規則），長名字則只能以替身出現。renamed 類的指標名
  是登錄表裡的舊名，作者放不進 canary，那三格逐一列在 `_NOT_APPLICABLE` 並寫理由。
* 跑 `--validate --strict --findings-json`，stdout、stderr、findings 檔一起比對、不分大小寫；並證明
  那一格真的被判了：對應的 finding kind 剛好一筆、`field` 是鍵的替身、訊息帶 kind、指標名
  （名稱形時）與同一個替身。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from test_routing_value_canary_matrix import validate_run  # noqa: E402
from _lib_validation import shown_key  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

# A test canary, not a credential: no real host, user or password. Mixed case
# on purpose; no `,`, `"` or `}` — they would end the label.
_CANARY = "CaNaRy-https://u:PW@hooks.example/x"  # trufflehog:ignore
_STAND_IN = re.compile(r"<key:[0-9a-f]{6}>")
_LONG_HEAD = "canary_" + "m" * 70  # not name-like: longer than 64 chars


def _no_canary(text: str) -> None:
    assert "canary" not in text.lower(), text


#: Where the canary goes: (metric name, label name, label value).
_PLACES = {
    "label_value": ("container_cpu", "env", _CANARY),
    "label_name": ("container_cpu", "canary_lbl", "x"),
    "metric_name": (_LONG_HEAD, "env", "x"),
}


def _version(head: str, name: str, value: str, version: str) -> dict[str, str]:
    return {f'{head}{{{name}="{value}",{version}}}': "1"}


#: Per kind: the tenant keys that cause it, from (metric name, label name,
#: label value). A renamed kind's metric name is the alias table's.
_KEYS = {
    "version_label_regex": lambda h, n, v: _version(h, n, v, 'version=~"v.*"'),
    "version_label_empty": lambda h, n, v: _version(h, n, v, 'version=""'),
    "version_label_default": lambda h, n, v: _version(h, n, v, 'version="default"'),
    "version_label_charset": lambda h, n, v: _version(h, n, "x", f'version="{v} BAD"'),
    "renamed": lambda _h, n, v: {f'mysql_cpu{{{n}="{v}"}}': "1"},
    "renamed_shadowed": lambda _h, n, v: {f'mysql_cpu{{{n}="{v}"}}': "1",
                                          f'mysql_threads_running{{{n}="{v}"}}': "2"},
    "renamed_critical_dimensional": lambda _h, n, v: {f'mysql_cpu_critical{{{n}="{v}"}}': "1"},
}

#: The metric name of each kind's key, where it is not the place's.
_HEADS = {"renamed": "mysql_cpu", "renamed_shadowed": "mysql_cpu",
          "renamed_critical_dimensional": "mysql_cpu_critical"}

_NOT_APPLICABLE = {
    (k, "metric_name"): "a retired spelling is one the alias table lists: its metric name is "
                        "the table's, never the author's"
    for k in tv.KEY_CHECK_RENAMED_KINDS
}

_CASES = [(kind, place) for kind in sorted(tv.KEY_CHECK_KINDS) for place in _PLACES
          if (kind, place) not in _NOT_APPLICABLE]


def test_the_matrix_is_every_kind():
    """每個 kind 都有寫法，沒有多的；不適用的格子都附理由。"""
    assert set(_KEYS) == set(tv.KEY_CHECK_KINDS)
    assert set(_HEADS) == set(tv.KEY_CHECK_RENAMED_KINDS) <= set(tv.KEY_CHECK_KINDS)
    assert all(kind in tv.KEY_CHECK_KINDS and place in _PLACES and reason.strip()
               for (kind, place), reason in _NOT_APPLICABLE.items())


def _files(keys: dict[str, str]) -> dict[str, dict]:
    root = {"defaults": {"container_cpu": 80, "mysql_threads_running": 70,
                         "mysql_threads_running_critical": 90}}
    return {"_defaults.yaml": root, "t-a.yaml": {"tenants": {"t-a": {"container_cpu": "85", **keys}}}}


@pytest.mark.parametrize("kind,place", _CASES, ids=[f"{k}-{p}" for k, p in _CASES])
def test_no_key_check_carries_the_key_as_written(tmp_path, kind, place):
    head, name, value = _PLACES[place]
    findings, text = validate_run(tmp_path, _files(_KEYS[kind](head, name, value)))
    _no_canary(text)
    finding_kind = "key_renamed" if kind in tv.KEY_CHECK_RENAMED_KINDS else "key_version_label"
    checks = [f for f in findings if f["kind"] in ("key_renamed", "key_version_label")]
    assert [f["kind"] for f in checks] == [finding_kind], findings
    [f] = checks
    assert f["tenant"] == "t-a" and f["blocks"] == "never", f
    assert _STAND_IN.fullmatch(f["field"]), f
    message = f["message"]
    assert f"key check {kind}: " in message and f"key {f['field']}" in message, f
    shown_head = shown_key(_HEADS.get(kind, head))
    assert f"metric {shown_head}," in message, f
    if place == "metric_name":
        assert _STAND_IN.fullmatch(shown_head), shown_head
    if kind in tv.KEY_CHECK_RENAMED_KINDS:
        assert "replacement metric mysql_threads_running" in message, f
