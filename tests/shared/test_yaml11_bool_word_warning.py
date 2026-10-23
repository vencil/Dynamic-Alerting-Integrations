"""#2509 → ADR-036 步驟 4（#2906）：允許布林的欄位寫了 YAML 1.1 布林字眼（不論加不加引號）給 WARN。

`send_resolved: on`、`_routing_enforced: {enabled: yes}`、`_state_maintenance: {enabled: off}`：
yaml.v3 讀到的是字串 "on" / "yes" / "off"——#2906 起 conf.d 的每個讀取端都是這種讀法
（`da-guard parse-tree`），加不加引號都一樣。schema 的 `yamlBool` 只收布林與 null，所以
這個值本身是 schema 的 ERROR（rc 1）；WARN 是附帶的補救說明：寫成 true / false。本檔釘住：

- 字眼的集合：YAML 1.1 讀成布林的字眼（`_lib_validation.yaml_bool_literals`）去掉 yaml.v3
  自己也讀成布林的 true/false 三種拼法，剩下 yes/no/on/off 各三種拼法。
- 兩條 lint 路徑都看得到：`check_confd_schema` 印 `WARN: <檔>: <欄位路徑>: …`（parse-tree 不給
  行號），`validate_config` 的 `yaml_quoting` 列轉 WARN、明細與 lint 的 WARN 行相同。
- 前提（必響）：每個欄位位置，exporter 讀到的確實是字串。
- 加引號與不加引號的輸出完全相同（bytes 不同、讀到的值相同）。
- 不響的對照組：`true` / `false`（各種大小寫）；字串欄位寫 `yes`（yaml.v3 讀成字串，沒有任何
  finding）。字串欄位寫 `1e3`（yaml.v3 讀成浮點數）是 #2164 的 ERROR。
- 根 `_defaults*` 的 `tenants:` 區塊依 tenant schema 檢查，WARN 與租戶檔一致；巢狀
  `_defaults.yaml` 的 `tenants:`（Go 不讀）兩條路徑都不報。
- 明確的 `!!bool yes`（以及 `!!bool y`、`!!int x` 這類 yaml.v3 套不上的 tag）讓整份檔讀不了
  （parse-tree 的 `decode_error`）：lint 結束碼 2 並帶 kind、不出 traceback；validate-config 由
  `yaml_syntax` 列報出，`yaml_quoting` 跳過該檔。#2740 那條「依 da-guard parse_failed 決定保留
  WARN」的路徑隨之移除。

da-guard 由 conftest 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import _lib_parse_tree as lpt
import _lib_tenant_values as tv
import _lib_validation
import validate_config as vc
import _lib_io
import _lib_parse_cache

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_confd_schema.py"
TENANT_SCHEMA = REPO_ROOT / "docs" / "schemas" / "tenant-config.schema.json"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_YAML12_BOOLS = {"true", "True", "TRUE", "false", "False", "FALSE"}


def _yaml11_bool_words() -> set[str]:
    """The YAML 1.1 boolean words (`_lib_validation.yaml_bool_literals`).
    #2906: the schema's `yamlBool` holds booleans and null only."""
    schema = json.loads(TENANT_SCHEMA.read_text(encoding="utf-8"))
    assert schema["definitions"]["yamlBool"]["type"] == ["boolean", "null"]
    return set(_lib_validation.yaml_bool_literals())


WORDS = sorted(_yaml11_bool_words() - _YAML12_BOOLS)

# field position -> (file name, body template with {v}, JSON-pointer path, where Go puts it)
_WEBHOOK = ("    _routing:\n      receiver:\n        type: webhook\n"
            "        url: https://hooks.example.com/a\n        send_resolved: {v}\n")
FIELDS = {
    "send_resolved": ("t1.yaml", "tenants:\n  t1:\n" + _WEBHOOK,
                      "/tenants/t1/_routing/receiver/send_resolved",
                      ("_routing", "receiver", "send_resolved")),
    "state_maintenance.enabled": (
        "t1.yaml", "tenants:\n  t1:\n    _state_maintenance:\n      enabled: {v}\n",
        "/tenants/t1/_state_maintenance/enabled", ("_state_maintenance", "enabled")),
}
_DEFAULTS = "defaults:\n  mysql_connections: 80\n"


def _tree(tmp_path: Path, field: str, value: str) -> Path:
    fname, template, _path, _go = FIELDS[field]
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir(parents=True)
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / fname).write_text(template.format(v=value), encoding="utf-8")
    return conf_d


def _lint(conf_d: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", str(LINT), "--config-dir", str(conf_d)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)


def _warn_lines(p: subprocess.CompletedProcess) -> list[str]:
    return [l for l in (p.stdout + p.stderr).splitlines() if l.startswith("WARN:")]


def test_word_set_is_yaml11_bools_minus_yaml12_bools():
    """The 12 words: YAML 1.1 (PyYAML's resolver) reads each as a boolean,
    yaml.v3 does not."""
    assert len(WORDS) == 12 and {w.lower() for w in WORDS} == {"yes", "no", "on", "off"}
    for w in WORDS:
        assert yaml.safe_load(f"k: {w}")["k"] in (True, False), w


def test_finder_flags_exactly_the_words_quoted_or_not(tmp_path):
    schemas = {"tenant-config.schema.json": json.loads(TENANT_SCHEMA.read_text(encoding="utf-8"))}
    candidates = WORDS + [f'"{w}"' for w in WORDS] + sorted(_YAML12_BOOLS) + [
        "y", "n", "Y", "N", "1", "0", "enable", "~"]
    flagged = set()
    for c in candidates:
        text = "tenants:\n  t1:\n" + _WEBHOOK.format(v=c)
        root = lpt.load_file(tmp_path, "t.yaml", text.encode(), None, 60).node("t.yaml")
        if _lib_io.find_yaml11_bool_words(root, schemas["tenant-config.schema.json"],
                                          schemas, "tenant-config.schema.json"):
            flagged.add(c)
    assert flagged == set(WORDS) | {f'"{w}"' for w in WORDS}


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_go_reads_the_word_as_a_string(tmp_path, field):
    """前提（必響）：同一份 bytes，exporter 出的是字串 "on"，parse-tree 的 tag 是 `!!str`。"""
    conf_d = _tree(tmp_path, field, "on")
    cfg = tv.load_effective(conf_d)["t1"].effective_config
    for k in FIELDS[field][3]:
        cfg = cfg[k]
    assert cfg == "on"
    node = lpt.load(conf_d, None, 60).node(FIELDS[field][0])["tenants"]["t1"]
    for k in FIELDS[field][3]:
        node = node[k]
    assert node["tag"] == "!!str" and node["text"] == "on"


_CASES = [("send_resolved", w) for w in WORDS] + [
    # `_routing_enforced.enabled` is a platform field: the tenant schema rejects
    # `_routing_enforced` under a tenant (#3016 R3), and the platform-file case
    # is `test_platform_defaults_routing_enforced_is_covered`.
    (f, w) for f in ("state_maintenance.enabled",)
    for w in ("yes", "On", "NO", "off")]


@pytest.mark.parametrize("field,word", _CASES, ids=[f"{f}-{w}" for f, w in _CASES])
def test_both_lint_paths_warn_naming_file_and_field(tmp_path, field, word):
    conf_d = _tree(tmp_path, field, word)
    fname, _t, path, _go = FIELDS[field]
    p = _lint(conf_d)
    # The value is a string where the schema takes a boolean: a violation.
    assert p.returncode == 1, p.stderr
    assert f"ERROR: {fname}: '{word}' is not of type 'boolean'" in p.stderr, p.stderr
    warns = _warn_lines(p)
    assert len(warns) == 1, p.stderr
    assert warns[0].startswith(
        f"WARN: {fname}: {path}: '{word}' is the YAML 1.1 boolean word"), warns
    want = "true" if word.lower() in ("yes", "on") else "false"
    assert f"write {path.rsplit('/', 1)[-1]}: {want}" in warns[0], warns

    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert row["details"] == warns, row
    # 盲審第 5 輪：row 的 hint 只指回各行，補救方式寫在每一行自己身上。
    assert row["hint"] is None, row
    assert "as its own line says" in vc._CHECK_HINTS["yaml_quoting"][0]


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("word", ["yes", "Off", "on"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_and_unquoted_give_the_same_output(tmp_path, field, word, quote):
    """#2906：`yes` 與 `"yes"` 是同一個字串值，兩條路徑的輸出一字不差。"""
    plain = _tree(tmp_path / "plain", field, word)
    quoted = _tree(tmp_path / "quoted", field, f"{quote}{word}{quote}")
    pp, pq = _lint(plain), _lint(quoted)
    assert (pp.returncode, pp.stdout, pp.stderr) == (pq.returncode, pq.stdout, pq.stderr)
    assert vc.check_yaml_quoting(str(plain)) == vc.check_yaml_quoting(str(quoted))


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("word", sorted(_YAML12_BOOLS))
def test_quiet_controls_in_a_boolean_field(tmp_path, field, word):
    """true/false 三種拼法（yaml.v3 讀成布林）：不響、schema 也收。"""
    conf_d = _tree(tmp_path, field, word)
    p = _lint(conf_d)
    assert p.returncode == 0 and _warn_lines(p) == [], p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.PASS


def _slack_tree(tmp_path: Path, channel: str) -> Path:
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
        "        api_url: \"https://hooks.slack.com/services/x\"\n"
        f"        channel: {channel}\n", encoding="utf-8")
    return conf_d


def test_a_string_field_with_yes_is_no_finding(tmp_path):
    """#2906：yaml.v3 把 `channel: yes` 讀成字串 "yes"——字串欄位要的正是字串。"""
    conf_d = _slack_tree(tmp_path, "yes")
    p = _lint(conf_d)
    assert p.returncode == 0 and _warn_lines(p) == [], p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.PASS


def test_a_string_field_with_a_float_is_2164s_error(tmp_path):
    """#2906：`channel: 1e3` 是 yaml.v3 的浮點數 1000——#2164 的 ERROR。"""
    conf_d = _slack_tree(tmp_path, "1e3")
    p = _lint(conf_d)
    assert p.returncode == 1 and _warn_lines(p) == [], p.stderr
    assert ("ERROR: t1.yaml: /tenants/t1/_routing/receiver/channel: '1e3' is read by "
            "yaml.v3") in p.stderr, p.stderr
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.FAIL and not any(d.startswith("WARN:") for d in row["details"]), row


def test_platform_defaults_routing_enforced_is_covered(tmp_path):
    """`_defaults.yaml` 依 platform-defaults schema 檢查：同樣的欄位、同樣的 WARN。"""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(
        _DEFAULTS + "_routing_enforced:\n  enabled: on\n  receiver:\n    type: webhook\n"
        "    url: https://noc.example.com/a\n", encoding="utf-8")
    (conf_d / "t1.yaml").write_text("tenants:\n  t1: {}\n", encoding="utf-8")
    p = _lint(conf_d)
    assert _warn_lines(p) and _warn_lines(p)[0].startswith(
        "WARN: _defaults.yaml: /_routing_enforced/enabled: 'on' is the YAML 1.1 boolean "
        "word"), p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.WARN


# ── 根與巢狀 `_defaults*` 的 `tenants:` ──────────────────────────────────────

def _platform_tree(tmp_path: Path, block: str, nested: bool = False) -> Path:
    conf_d = tmp_path / "conf.d"
    (conf_d / "sub").mkdir(parents=True)
    carrier = conf_d / ("sub/_defaults.yaml" if nested else "_defaults.yaml")
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS + ("" if nested else block),
                                           encoding="utf-8")
    if nested:
        carrier.write_text("defaults:\n  mysql_connections: 70\n" + block, encoding="utf-8")
    (conf_d / "sub" / "t1.yaml").write_text("tenants:\n  t1: {}\n", encoding="utf-8")
    return conf_d


_BLOCK = ("tenants:\n  t1:\n    _state_maintenance: {enabled: on}\n"
          "    _routing:\n      receiver: {type: webhook, url: 'https://h.example.com/x', "
          "send_resolved: No}\n")


def _strip_loc(line: str) -> str:
    """`WARN: <file>: <rest>` -> `<rest>`."""
    return line.split(": ", 2)[2]


def test_root_platform_tenants_block_warns_like_a_tenant_file(tmp_path):
    """F3：根 `_defaults.yaml` 的 `tenants:` 與同內容的租戶檔，WARN（去掉檔名）完全相同；
    Go 端出的確實是字串（前提）。"""
    (tmp_path / "p").mkdir()
    (tmp_path / "t").mkdir()
    plat = _platform_tree(tmp_path / "p", _BLOCK)
    eff = tv.load_effective(plat)["t1"].effective_config
    assert eff["_state_maintenance"]["enabled"] == "on"
    assert eff["_routing"]["receiver"]["send_resolved"] == "No"
    tenant = tmp_path / "t" / "conf.d"
    tenant.mkdir()
    (tenant / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (tenant / "t1.yaml").write_text(_BLOCK, encoding="utf-8")
    pw, tw = _warn_lines(_lint(plat)), _warn_lines(_lint(tenant))
    assert len(pw) == 2 and all(l.startswith("WARN: _defaults.yaml: ") for l in pw), pw
    assert [_strip_loc(l) for l in pw] == [_strip_loc(l) for l in tw], (pw, tw)
    assert vc.check_yaml_quoting(str(plat))["details"] == pw


def test_root_platform_tenants_block_gets_2164s_error_too(tmp_path):
    """F3 順手：#2164（字串欄位的非字串值）在平台 `tenants:` 也報 ERROR。"""
    conf_d = _platform_tree(tmp_path, (
        "tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
        "        api_url: \"https://hooks.slack.com/services/x\"\n        channel: 1e3\n"))
    p = _lint(conf_d)
    assert p.returncode == 1, p.stderr
    assert ("ERROR: _defaults.yaml: /tenants/t1/_routing/receiver/channel: '1e3' is read "
            "by yaml.v3") in p.stderr, p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.FAIL


def test_nested_defaults_tenants_block_is_not_held_to_the_tenant_schema(tmp_path):
    """對照：巢狀 `_defaults.yaml` 的 `tenants:` 區塊 Go 不讀（#1576），兩條路徑都不報。"""
    conf_d = _platform_tree(tmp_path, _BLOCK, nested=True)
    assert _warn_lines(_lint(conf_d)) == []
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.PASS, row


# ── 明確的 tag：yaml.v3 套不上就整份讀不了 ───────────────────────────────────

@pytest.mark.parametrize("value", ["!!bool yes", "!!bool on", "!!bool 'yes'", "!!bool y",
                                   "!!int x"])
def test_a_tag_yaml_v3_cannot_apply_makes_the_file_unreadable(tmp_path, value):
    """#2906：parse-tree 的 `decode_error`。lint 結束碼 2、帶 kind、沒有 traceback；
    validate-config 的 `yaml_syntax` 列報出這份檔，`yaml_quoting` 跳過它。兩者
    都是 judge（#3206），validate-config 的列函式在 process 內以 judge 身分執行。"""
    conf_d = tmp_path / "conf.d"
    (conf_d / "sub").mkdir(parents=True)
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / "sub" / "t1.yaml").write_text(
        f"tenants:\n  t1:\n    _state_maintenance: {{enabled: {value}}}\n", encoding="utf-8")
    p = _lint(conf_d)
    assert p.returncode == 2, p.stderr
    assert "Traceback" not in p.stderr, p.stderr
    assert "ERROR: sub/t1.yaml: cannot read/parse YAML (decode_error): " in p.stderr, p.stderr
    assert _warn_lines(p) == [], p.stderr
    with _lib_parse_cache.judging():
        syntax = vc.check_yaml_syntax(str(conf_d))
        quoting = vc.check_yaml_quoting(str(conf_d))
    assert syntax["status"] == vc.FAIL and any(
        "sub/t1.yaml" in d and "decode_error" in d for d in syntax["details"]), syntax
    assert quoting["status"] == vc.PASS


@pytest.mark.parametrize("word", sorted(_YAML12_BOOLS))
def test_an_explicit_bool_tag_yaml_v3_takes_is_quiet(tmp_path, word):
    conf_d = _tree(tmp_path, "send_resolved", f"!!bool {word}")
    p = _lint(conf_d)
    assert p.returncode == 0 and _warn_lines(p) == [], p.stderr
