"""validate-config `platform_defaults`（#3832 決策 8）：這棵樹怎麼處理平台預設啟用的閾值。

讀取端內建預設啟用鍵與平台值，墊在根 `_defaults.yaml` 之下。這一列逐鍵說明狀態：`builtin`
（根層沒給值，由內建層供給，附平台值與受影響的租戶數）、`overridden`（根層給了數字；與平台值
相同時註明），並印出內建表的摘要。答案一律來自 da-guard（effective 的 `builtin_defaults` 與
每個租戶的 `key_sources`），這支工具不讀登錄表、不自己推算。這一列是說明不是警告：樹裡沒寫
某個預設啟用鍵是正常狀態，所以是 PASS；根層讀不了時說內建層已撤掉，判不了時是 WARN「not
checked」。鍵數與值一律讀生成表（`tests/_builtin_layer.py`），不寫死。租戶 id 是 fixture 名
（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc
from _builtin_layer import BUILTIN_DEFAULTS

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_GEN = _REPO / "components/threshold-exporter/app/pkg/config/builtin_defaults_gen.go"
#: The digest the reader was compiled with, as the generated table names it.
_DIGEST = re.search(r'builtinDefaultsDigest = "([0-9a-f]+)"', _GEN.read_text(encoding="utf-8")).group(1)
_KEYS = sorted(BUILTIN_DEFAULTS)


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _line(row: dict, key: str) -> str:
    """The row's one line for *key*."""
    hits = [d for d in row["details"] if f"`{key}`" in d]
    assert len(hits) == 1, (key, row)
    return hits[0]


def _cli(tree: str, env: "dict[str, str] | None" = None) -> "tuple[int, dict]":
    """(exit code, the row) as the whole tool reports it (`--json`): a tree with
    a file that does not read is judged through `main`'s reader, not a bare call."""
    r = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", tree, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300,
                       env={**os.environ, **(env or {})})
    rows = [c for c in json.loads(r.stdout) if c["check"] == "platform_defaults"]
    assert len(rows) == 1, r.stdout + r.stderr
    return r.returncode, rows[0]


@pytest.mark.parametrize("files", [
    {"t1.yaml": "tenants:\n  t1: {}\n", "t2.yaml": "tenants:\n  t2: {}\n"},
    {"_defaults.yaml": "defaults: {}\n", "t1.yaml": "tenants:\n  t1: {}\n", "t2.yaml": "tenants:\n  t2: {}\n"},
], ids=["no-root-file", "empty-defaults"])
def test_a_tree_that_writes_no_default_lists_every_key_as_builtin(tmp_path, files):
    """沒有根層檔、或根層 `defaults:` 是空的：每個預設啟用鍵都是 `builtin`，附平台值與
    「2 個租戶都由內建層供給」，第一行印出內建表摘要。PASS：這是正常狀態。"""
    assert BUILTIN_DEFAULTS, "the built-in table is empty — this test would pass vacuously"
    row = vc.check_platform_defaults(_tree(tmp_path, files))
    assert row["status"] == vc.PASS, row
    assert row["details"][0] == (f"built-in table {_DIGEST}: {len(_KEYS)} default-armed threshold(s), "
                                 "2 tenant(s)"), row
    for key in _KEYS:
        assert _line(row, key) == (f"builtin `{key}` = {_fmt(BUILTIN_DEFAULTS[key])} — the root "
                                   "platform file does not give it a value; the built-in layer "
                                   "serves 2 of 2 tenant(s)"), row
    assert len(row["details"]) == 1 + len(_KEYS), row


def test_a_number_under_the_root_defaults_is_overridden(tmp_path):
    """根層給了數字的鍵是 `overridden`，印出那個數與平台值；與平台值相同時註明。"""
    other, same = _KEYS[0], _KEYS[1]
    row = vc.check_platform_defaults(_tree(tmp_path, {
        "_defaults.yaml": f"defaults:\n  {other}: 7\n  {same}: {_fmt(BUILTIN_DEFAULTS[same])}\n",
        "t1.yaml": "tenants:\n  t1: {}\n",
    }))
    assert row["status"] == vc.PASS, row
    assert _line(row, other) == (f"overridden `{other}` = 7 — set under `defaults:` of the root "
                                 f"platform file; platform value {_fmt(BUILTIN_DEFAULTS[other])}"), row
    assert _line(row, same) == (f"overridden `{same}` = {_fmt(BUILTIN_DEFAULTS[same])} (the platform's "
                                "value) — set under `defaults:` of the root platform file; platform value "
                                f"{_fmt(BUILTIN_DEFAULTS[same])}"), row
    assert _line(row, _KEYS[2]).startswith(f"builtin `{_KEYS[2]}`"), row


@pytest.mark.parametrize("text", ["null", "abc"], ids=["null", "refused"])
def test_a_root_value_that_gives_nothing_falls_to_builtin(tmp_path, text):
    """根層寫 `null`（等於沒寫）或 exporter 讀不了的值：落到內建值，所以是 `builtin`，
    不是 `overridden`——與 `/metrics` 送的值一致。"""
    key = _KEYS[0]
    row = vc.check_platform_defaults(_tree(tmp_path, {
        "_defaults.yaml": f"defaults:\n  {key}: {text}\n",
        "t1.yaml": "tenants:\n  t1: {}\n",
    }))
    assert _line(row, key).startswith(f"builtin `{key}` = {_fmt(BUILTIN_DEFAULTS[key])} — "), row
    assert _line(row, key).endswith("serves 1 of 1 tenant(s)"), row


def test_the_tenant_count_is_the_tenants_metrics_serves_the_builtin_value(tmp_path):
    """受影響的租戶數是 `/metrics` 實際送平台值的租戶（da-guard 依 exporter 出列規則數）：
    沒寫、寫 `null`、寫了讀不懂而改用預設的（租戶檔或子目錄）都算；自己給了數字、在子目錄
    給了數字、或寫 `disable` 的不算。數字與 served-values 逐租戶對照。"""
    key = _KEYS[0]
    tree = _tree(tmp_path, {
        "plain.yaml": "tenants:\n  plain: {}\n",
        "tnull.yaml": f"tenants:\n  tnull:\n    {key}: null\n",
        "tabc.yaml": f"tenants:\n  tabc:\n    {key}: abc\n",
        "tinf.yaml": f"tenants:\n  tinf:\n    {key}: .inf\n",
        "own.yaml": f"tenants:\n  own:\n    {key}: \"5\"\n",
        "off.yaml": f"tenants:\n  off:\n    {key}: disable\n",
        "sub/_defaults.yaml": f"defaults:\n  {key}: 6\n",
        "sub/deep.yaml": "tenants:\n  deep: {}\n",
        "bad/_defaults.yaml": f"defaults:\n  {key}: abc\n",
        "bad/tb.yaml": "tenants:\n  tb: {}\n",
    })
    row = vc.check_platform_defaults(tree)
    assert _line(row, key).endswith("the built-in layer serves 5 of 8 tenant(s)"), row
    assert _line(row, _KEYS[1]).endswith("the built-in layer serves 8 of 8 tenant(s)"), row
    served = json.loads(subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir", tree],
                                       capture_output=True, text=True, encoding="utf-8",
                                       timeout=120).stdout)
    platform = sorted(t for t, v in served["tenants"].items() if v["values"].get(key) == BUILTIN_DEFAULTS[key])
    assert platform == ["plain", "tabc", "tb", "tinf", "tnull"], platform


@pytest.mark.parametrize("root", ["defaults: [oops\n", f"defaults:\n  {_KEYS[0]}: [1, 2]\n"],
                         ids=["syntax", "dropped-by-build"])
def test_a_root_file_that_does_not_read_withholds_the_builtin_layer(tmp_path, root):
    """根層讀不了：內建層已撤掉，沒有任何租戶拿到預設列——這一列說出來（WARN），不是
    「not checked」，也不是列出一堆 `builtin`。"""
    rc, row = _cli(_tree(tmp_path, {"_defaults.yaml": root, "t1.yaml": "tenants:\n  t1: {}\n"}))
    assert row["status"] == vc.WARN, row
    assert row["details"] == [
        f"withheld: the root platform file does not read, so none of the {len(_KEYS)} default-armed "
        f"threshold(s) of the built-in table {_DIGEST} is served to any tenant"], row
    assert "read again" in row["suggested_action"], row


def test_another_file_the_load_drops_is_not_checked_never_a_pass(tmp_path):
    """別的檔被 exporter 整份丟掉：它的租戶沒被算進去，PASS 會讀成「每個租戶都數過了」，
    所以是 WARN「not checked」並點名那個檔。"""
    rc, row = _cli(_tree(tmp_path, {"t1.yaml": "tenants:\n  t1: {}\n", "t2.yaml": "tenants:\n  t2: [1]\n"}))
    assert row["status"] == vc.WARN, row
    assert row["details"][0].startswith("not checked: "), row
    assert "t2.yaml" in row["details"][0], row


def test_the_answer_is_da_guards_not_this_tools(tmp_path, monkeypatch):
    """摘要、狀態與平台值都照 da-guard 寫的印：換一支改寫 `builtin_defaults` 的 da-guard，
    這一列跟著變。這支工具若自己讀登錄表推算，這裡就會印出生成表的摘要。"""
    real = os.environ["DA_GUARD_BINARY"]
    key = _KEYS[0]
    rewrite = tmp_path / "rewrite.py"
    rewrite.write_text(
        "import json, sys\n"
        "d = json.load(sys.stdin)\n"
        "if isinstance(d, dict) and isinstance(d.get('builtin_defaults'), dict):\n"
        "    b = d['builtin_defaults']\n"
        "    b['digest'] = 'feedfacecafe'\n"
        f"    b['keys'][{key!r}] = {{'value': 42, 'state': 'overridden', 'root_value': 41}}\n"
        "json.dump(d, sys.stdout)\n", encoding="utf-8")
    wrapper = tmp_path / "da-guard"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = effective ]; then '{real}' \"$@\" | '{sys.executable}' '{rewrite}'; "
        "exit 0; fi\n"
        f"exec '{real}' \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("DA_GUARD_BINARY", str(wrapper))
    row = vc.check_platform_defaults(_tree(tmp_path / "t", {"t1.yaml": "tenants:\n  t1: {}\n"}))
    assert row["details"][0].startswith("built-in table feedfacecafe: "), row
    assert _line(row, key) == (f"overridden `{key}` = 41 — set under `defaults:` of the root "
                               "platform file; platform value 42"), row


def test_a_da_guard_without_the_answer_is_not_checked(tmp_path, monkeypatch):
    """沒有 `builtin_defaults` 的 da-guard（比本工具舊）：WARN「not checked」並說它比較舊，
    不是 PASS。"""
    real = os.environ["DA_GUARD_BINARY"]
    strip = tmp_path / "strip.py"
    strip.write_text("import json, sys\nd = json.load(sys.stdin)\nd.pop('builtin_defaults', None)\n"
                     "json.dump(d, sys.stdout)\n", encoding="utf-8")
    wrapper = tmp_path / "da-guard"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = effective ]; then '{real}' \"$@\" | '{sys.executable}' '{strip}'; exit 0; fi\n"
        f"exec '{real}' \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("DA_GUARD_BINARY", str(wrapper))
    row = vc.check_platform_defaults(_tree(tmp_path / "t", {"t1.yaml": "tenants:\n  t1: {}\n"}))
    assert row["status"] == vc.WARN, row
    assert row["details"][0].startswith("not checked: "), row
    assert "older than this tool" in row["details"][0], row


def test_it_recommends_no_copy_and_no_optional_overrides_listing(tmp_path):
    """舊列（`unsupplied_defaults`）教人把鍵複製進 `defaults:`、或列進 `optional_overrides:`
    來關掉——前者是又一份複製品，後者現在被 da-guard 報 `root_optional_builtin`。新列不給
    這兩個建議，舊列也不再出現。"""
    tree = _tree(tmp_path, {"t1.yaml": "tenants:\n  t1: {}\n"})
    r = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", tree, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    rows = json.loads(r.stdout)
    assert not [c for c in rows if c["check"] == "unsupplied_defaults"], rows
    row = next(c for c in rows if c["check"] == "platform_defaults")
    text = json.dumps(row) + vc._CHECK_HINTS["platform_defaults"][0] + vc._PLATFORM_DEFAULTS_WITHHELD_HINT
    assert "optional_overrides" not in text, text
    assert "Add the key" not in text, text


def test_it_never_changes_the_exit_code(tmp_path):
    """說明列不影響結束碼：一棵只靠內建層的乾淨樹，整支工具 exit 0、這一列 PASS。"""
    rc, row = _cli(_tree(tmp_path, {"t1.yaml": "tenants:\n  t1: {}\n"}))
    assert row["status"] == vc.PASS, row
    assert rc == 0, row
