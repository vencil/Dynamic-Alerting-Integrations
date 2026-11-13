"""`_severity_dedup` 的詞彙：schema 收下的值 ⇔ exporter 讀得懂的值（#2906、#3621）。

路由產生器自 #3621 起不再有自己的詞表：每個租戶的模式、以及哪些值 exporter 讀不懂，
都取自 `da-guard severity-dedup`（exporter 自己的載入與 `ResolveSeverityDedupLogf`）。
剩下唯一的第二份詞表是 `docs/schemas/tenant-config.schema.json` 的
`definitions.severityDedup`（給作者看、`check_confd_schema` 用）。

做法：把一份語料庫（各種拼法：大小寫、空白、布林、null、`"no"`、`1.00`、`0x1F`、
list…）各寫成一個租戶的 `_severity_dedup`，整棵樹交給 Go 子命令判讀；每個值再以
schema lint 讀 conf.d 的同一個 reader（`da-guard parse-tree` 的 yaml.v3 文件）取出，
用 jsonschema 對 `definitions.severityDedup` 驗。斷言：

* schema 接受 ⇔ Go 讀得懂（該租戶沒有 problem）——對每個純量值；
* Go 給的模式與值的語意一致（啟用詞 → enable，停用詞 → disable，null → enable）；
* mapping 寫法（`{default: disable}`）exporter 讀其 `default`，schema 不收任何
  mapping——schema 較嚴是安全方向，這裡只釘「schema 收的 Go 一定讀得懂」那一半。

讀不到（子命令失敗、某值讓整個檔被丟掉）一律失敗，不當成一致。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import _lib_parse_cache  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

SCHEMA = REPO / "docs" / "schemas" / "tenant-config.schema.json"

# (YAML text as written, the mode it means, or None for a value nothing reads).
SCALARS: list[tuple[str, str | None]] = [
    *((w, "enable") for w in ("enable", "enabled", "on", "true")),
    *((w, "disable") for w in ("disable", "disabled", "off", "false")),
    ('"ENABLE"', "enable"), ('" Enabled "', "enable"), ('"On"', "enable"), ('"TRUE"', "enable"),
    ('"Disable"', "disable"), ('"  DISABLED"', "disable"), ('" off "', "disable"), ('"False"', "disable"),
    ("True", "enable"), ("FALSE", "disable"), ("~", "enable"), ("null", "enable"),
    ('"no"', None), ("no", None), ('"yes"', None), ("yes", None), ('""', None),
    ("y", None), ("n", None), ("1", None), ("0", None), ("1.00", None), ('"1.00"', None),
    ("0x1F", None), ("maybe", None), ("enabl", None), ("disables", None),
    ('"enable disable"', None), ("[disable]", None), ("[]", None),
]
# Mapping forms: (YAML text, the mode the exporter reads) — the schema takes none.
MAPPINGS: list[tuple[str, str]] = [
    ("{default: disable}", "disable"),
    ("{default: enable}", "enable"),
    ("{a: 1}", "enable"),
]


def _write_tree(tmp_path: Path, values: list[str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n", encoding="utf-8")
    body = "".join(f"  t-{i}:\n    _severity_dedup: {v}\n" for i, v in enumerate(values))
    (d / "corpus.yaml").write_text("tenants:\n" + body, encoding="utf-8")
    return d


def _schema_validator() -> jsonschema.Draft7Validator:
    d = json.loads(SCHEMA.read_text(encoding="utf-8"))["definitions"]["severityDedup"]
    return jsonschema.Draft7Validator(d)


def _read(tmp_path: Path, values: list[str]) -> tuple[tv.SeverityDedupTree, dict]:
    d = _write_tree(tmp_path, values)
    tree = tv.load_severity_dedup(d)
    assert tree.parse_failed == [] and tree.unreadable == [], (tree.parse_failed, tree.unreadable)
    doc = _lib_parse_cache.ConfdReader(d).read(d / "corpus.yaml")
    return tree, doc["tenants"]


def test_schema_takes_exactly_what_the_exporter_reads(tmp_path):
    values = [v for v, _ in SCALARS]
    tree, written = _read(tmp_path, values)
    unread = {p.owner for p in tree.problems}
    assert {p.layer for p in tree.problems} <= {"tenant"}, tree.problems
    validator = _schema_validator()
    mismatched = []
    for i, (text, mode) in enumerate(SCALARS):
        t = f"t-{i}"
        assert t in tree.modes, (t, text)
        value = (written[t] or {}).get("_severity_dedup")
        schema_ok = validator.is_valid(value)
        go_ok = t not in unread
        if schema_ok != go_ok:
            mismatched.append((text, value, f"schema={schema_ok}", f"exporter={go_ok}"))
        if mode is None:
            assert not go_ok, f"{text}: the exporter reads it, the corpus says nothing does"
            # A value the exporter does not read is served as enable (its WARN).
            assert tree.modes[t] == "enable", (text, tree.modes[t])
        else:
            assert go_ok, f"{text}: the exporter does not read it: {[p for p in tree.problems if p.owner == t]}"
            assert tree.modes[t] == mode, (text, tree.modes[t], mode)
    assert not mismatched, mismatched
    # Both sides of the corpus are exercised: read and refused values.
    assert unread and len(unread) < len(SCALARS)


@pytest.mark.parametrize("text,mode", MAPPINGS)
def test_mapping_forms_schema_is_the_stricter_side(tmp_path, text, mode):
    tree, written = _read(tmp_path, [text])
    value = written["t-0"]["_severity_dedup"]
    assert not _schema_validator().is_valid(value), (text, value)
    assert tree.modes["t-0"] == mode, (text, tree.modes)
    assert tree.schedules == [], tree.schedules
