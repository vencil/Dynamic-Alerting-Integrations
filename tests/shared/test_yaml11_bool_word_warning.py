"""#2509（owner 裁決 ②）：允許布林的欄位寫了未加引號的 YAML 1.1 字眼要給 WARN，讀取端不動。

`send_resolved: on`、`_routing_enforced: {enabled: yes}`、`_state_maintenance: {enabled: off}`：
PyYAML（YAML 1.1）讀成布林，schema 因此放行；yaml.v3 讀到的是字串 "on" / "yes" / "off"。
各檔種由不同的 Go reader 讀（有的保留字串、有的讀成布林、有的根本不讀），所以訊息只陳述
可驗證的事實，不斷言 merged_hash／effective 的值（盲審第 2 輪 G／F）。本檔釘住：

- 字眼的集合：以 Go `receiverspec.YAML11BoolLiterals`（spec.go）為準——去掉 yaml.v3 自己也讀成
  布林的 true/false 三種拼法，剩下 yes/no/on/off 各三種拼法；schema 的 `yamlBool` enum 與它一致。
- 兩條 lint 路徑都看得到：`check_confd_schema`（pre-commit 的 confd-schema hook）印
  `WARN: <檔>:<行>: <欄位路徑>…`、rc 不變；`validate_config` 的 `yaml_quoting` 列轉 WARN、
  明細指名檔案與欄位。
- 前提（必響）：每個欄位位置，Go 讀到的確實是字串、PyYAML 讀到的確實是布林——WARN 講的是真的。
- 不響的對照組：`true` / `false`（各種大小寫）、字串欄位寫 `yes`（那是 #2164 的 ERROR，不是本 WARN）、
  加引號的 `"yes"`。
- 盲審第 1 輪（F3／F4／F5／T2）：根 `_defaults*` 的 `tenants:` 區塊依 tenant schema 檢查，WARN 與
  租戶檔一致（#2164 的 ERROR 同一修法一併補上；巢狀 `_defaults.yaml` 的 `tenants:` Go 不讀，兩條路徑
  都不報）；`yaml_quoting` 的 FAIL 列附上 WARN 明細。
- 盲審第 3 輪（換主體）：明確 `!!bool yes` 在任何位置都是同一則中性 WARN、rc 不變。「exporter
  讀不讀得了這份檔」不再由 Python 重建（三版位置規則都被盲審打穿），交給 da-guard 的 parse_failed：
  `validate_config` 的 da-guard 列會連同 exporter 的理由報 FAIL（本檔有一格釘住這條路徑）。
  PyYAML 建不出的值（`!!bool y`、`!!int x`）由 `check_confd_schema` 具名報 ERROR、不出 traceback，
  措辭只講 lint 自己的限制（這份檔沒做 schema 檢查）。
- #2740（owner 選 B）：lint 沒有 da-guard，不再斷言「會有對應的 `profiles` 列」，只叫人對這棵樹跑
  validate-config。validate-config 的 `yaml_quoting` 只為 da-guard effective 的 parse_failed 點名的檔
  保留這則 WARN（指向 `profiles` 列，與 `profiles` 共用同一次 da-guard 執行）；沒點名的檔（exporter 不載入、
  Go routing reader 照讀 `!!bool yes` 的 `_routing_profiles.yaml`）不報。沒有 da-guard 的判決（找不到、
  太舊、失敗）時照舊全部保留、措辭不變。

修正前（main 59e58c81）兩條 lint 對 yes/on/no/off 都完全安靜（schema 讓它過、quoting 只看字串欄位）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import _lib_tenant_values as tv
import validate_config as vc
# Module, not the names: on a tree without the finder (the counterfactual run on
# main) the lint-path tests below must fail by assertion, not at collection.
import _lib_io

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_confd_schema.py"
SPEC_GO = (REPO_ROOT / "components" / "threshold-exporter" / "app" / "pkg" / "receiverspec"
           / "spec.go")
TENANT_SCHEMA = REPO_ROOT / "docs" / "schemas" / "tenant-config.schema.json"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_YAML12_BOOLS = {"true", "True", "TRUE", "false", "False", "FALSE"}


def _go_yaml11_literals() -> set[str]:
    src = SPEC_GO.read_text(encoding="utf-8")
    body = re.search(r"var YAML11BoolLiterals = map\[string\]bool\{(.*?)\n\}", src, re.S)
    assert body, "YAML11BoolLiterals not found in spec.go"
    return set(re.findall(r'"([^"]+)":', body.group(1)))


WORDS = sorted(_go_yaml11_literals() - _YAML12_BOOLS)

# field position -> (file name, body template with {v}, JSON-pointer path, where Go puts it)
_WEBHOOK = ("    _routing:\n      receiver:\n        type: webhook\n"
            "        url: https://hooks.example.com/a\n        send_resolved: {v}\n")
FIELDS = {
    "send_resolved": ("t1.yaml", "tenants:\n  t1:\n" + _WEBHOOK,
                      "/tenants/t1/_routing/receiver/send_resolved",
                      ("_routing", "receiver", "send_resolved")),
    "routing_enforced.enabled": (
        "t1.yaml",
        "tenants:\n  t1:\n    _routing_enforced:\n      enabled: {v}\n"
        "      receiver:\n        type: webhook\n        url: https://noc.example.com/a\n",
        "/tenants/t1/_routing_enforced/enabled", ("_routing_enforced", "enabled")),
    "state_maintenance.enabled": (
        "t1.yaml", "tenants:\n  t1:\n    _state_maintenance:\n      enabled: {v}\n",
        "/tenants/t1/_state_maintenance/enabled", ("_state_maintenance", "enabled")),
}
_DEFAULTS = "defaults:\n  mysql_connections: 80\n"


def _tree(tmp_path: Path, field: str, value: str) -> tuple[Path, int]:
    fname, template, _path, _go = FIELDS[field]
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    body = template.format(v=value)
    (conf_d / fname).write_text(body, encoding="utf-8")
    line = next(i for i, l in enumerate(body.splitlines(), 1) if l.rstrip().endswith(value))
    return conf_d, line


def _lint(conf_d: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", str(LINT), "--config-dir", str(conf_d)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)


def _warn_lines(p: subprocess.CompletedProcess) -> list[str]:
    return [l for l in (p.stdout + p.stderr).splitlines() if l.startswith("WARN:")]


def test_word_set_is_go_yaml11_literals_minus_yaml12_bools():
    """Go 為準；schema 的 enum 與 Go 一致；PyYAML resolver 恰好把這 12 個讀成布林。"""
    assert len(WORDS) == 12 and {w.lower() for w in WORDS} == {"yes", "no", "on", "off"}
    schema = json.loads(TENANT_SCHEMA.read_text(encoding="utf-8"))
    assert set(schema["definitions"]["yamlBool"]["anyOf"][1]["enum"]) == _go_yaml11_literals()
    for w in WORDS:
        assert yaml.safe_load(f"k: {w}")["k"] in (True, False), w


def test_finder_flags_exactly_the_words(tmp_path):
    schemas = {"tenant-config.schema.json": json.loads(TENANT_SCHEMA.read_text(encoding="utf-8"))}
    candidates = WORDS + sorted(_YAML12_BOOLS) + ["y", "n", "Y", "N", "1", "0", "enable", "~"]
    flagged = set()
    for c in candidates:
        text = "tenants:\n  t1:\n" + _WEBHOOK.format(v=c)
        for root in _lib_io.compose_all_nodes(io.StringIO(text)):
            if _lib_io.find_yaml11_bool_words(root, schemas["tenant-config.schema.json"],
                                              schemas, "tenant-config.schema.json"):
                flagged.add(c)
    assert flagged == set(WORDS)


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_go_reads_the_word_as_a_string(tmp_path, field):
    """前提（必響）：同一份 bytes，Go 端出字串 "on"、PyYAML 讀成 True。"""
    conf_d, _ = _tree(tmp_path, field, "on")
    cfg = tv.load_effective(conf_d)["t1"].effective_config
    for k in FIELDS[field][3]:
        cfg = cfg[k]
    assert cfg == "on"
    doc = yaml.safe_load((conf_d / FIELDS[field][0]).read_text(encoding="utf-8"))
    node = doc["tenants"]["t1"]
    for k in FIELDS[field][3]:
        node = node[k]
    assert node is True


_CASES = [("send_resolved", w) for w in WORDS] + [
    (f, w) for f in ("routing_enforced.enabled", "state_maintenance.enabled")
    for w in ("yes", "On", "NO", "off")]


@pytest.mark.parametrize("field,word", _CASES, ids=[f"{f}-{w}" for f, w in _CASES])
def test_both_lint_paths_warn_naming_file_and_field(tmp_path, field, word):
    conf_d, line = _tree(tmp_path, field, word)
    fname, _t, path, _go = FIELDS[field]
    p = _lint(conf_d)
    assert p.returncode == 0, p.stderr                      # WARN 不改 rc
    warns = _warn_lines(p)
    assert len(warns) == 1, p.stderr
    assert warns[0].startswith(
        f"WARN: {fname}:{line}: {path}: unquoted YAML 1.1 boolean word '{word}'"), warns
    want = "true" if word.lower() in ("yes", "on") else "false"
    assert f"write it as true / false ({path.rsplit('/', 1)[-1]}: {want})" in warns[0], warns
    assert "1 warning(s)" in p.stdout, p.stdout

    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert row["details"] == warns, row
    # 盲審第 5 輪：row 的 hint 只指回各行，補救方式寫在每一行自己身上。
    assert row["hint"] is None, row
    assert "as its own line says" in vc._CHECK_HINTS["yaml_quoting"][0]


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("word", sorted(_YAML12_BOOLS) + ['"yes"', "'off'"])
def test_quiet_controls_in_a_boolean_field(tmp_path, field, word):
    """true/false 三種拼法（yaml.v3 也讀成布林）與加引號的字眼：不響。"""
    conf_d, _ = _tree(tmp_path, field, word)
    p = _lint(conf_d)
    assert _warn_lines(p) == [], p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] != vc.WARN


def test_a_string_field_with_yes_is_2164s_error_not_this_warn(tmp_path):
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
        "        api_url: \"https://hooks.slack.com/services/x\"\n        channel: yes\n",
        encoding="utf-8")
    p = _lint(conf_d)
    assert p.returncode == 1 and _warn_lines(p) == [], p.stderr
    assert "channel: unquoted 'yes'" in p.stderr      # #2164 照舊
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
    assert p.returncode == 0, p.stderr
    assert _warn_lines(p) and _warn_lines(p)[0].startswith(
        "WARN: _defaults.yaml:4: /_routing_enforced/enabled: unquoted YAML 1.1 boolean word "
        "'on'"), p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.WARN



# ── 盲審第 1 輪 ──────────────────────────────────────────────────────────

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
    """`WARN: <file>:<line>: <rest>` -> `<rest>`."""
    return line.split(": ", 2)[2]


def test_root_platform_tenants_block_warns_like_a_tenant_file(tmp_path):
    """F3：根 `_defaults.yaml` 的 `tenants:` 與同內容的租戶檔，WARN（去掉檔名與行號）完全相同；
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
    assert len(pw) == 2 and all(l.startswith("WARN: _defaults.yaml:") for l in pw), pw
    assert [_strip_loc(l) for l in pw] == [_strip_loc(l) for l in tw], (pw, tw)
    assert vc.check_yaml_quoting(str(plat))["details"] == pw


def test_root_platform_tenants_block_gets_2164s_error_too(tmp_path):
    """F3 順手：同一修法讓 #2164（字串欄位的未加引號 yes）在平台 `tenants:` 也報 ERROR。"""
    conf_d = _platform_tree(tmp_path, (
        "tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
        "        api_url: \"https://hooks.slack.com/services/x\"\n        channel: yes\n"))
    p = _lint(conf_d)
    assert p.returncode == 1, p.stderr
    assert "ERROR: _defaults.yaml:" in p.stderr and "/tenants/t1/_routing/receiver/channel: " \
        "unquoted 'yes'" in p.stderr, p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.FAIL


def test_nested_defaults_tenants_block_is_not_held_to_the_tenant_schema(tmp_path):
    """對照：巢狀 `_defaults.yaml` 的 `tenants:` 區塊 Go 不讀（#1576），兩條路徑都不報
    （盲審第 2 輪 T1：validate_config 這半原本沒有斷言，對它套 tenant schema 的突變存活）。"""
    conf_d = _platform_tree(tmp_path, _BLOCK, nested=True)
    assert _warn_lines(_lint(conf_d)) == []
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.PASS, row


_EXPLICIT = ["!!bool yes", "!!bool on", "!!bool no", "!!bool off", "!!bool Yes", "!!bool OFF",
             "!!bool 'yes'"]


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("word", _EXPLICIT)
def test_explicit_bool_tag_yaml_v3_refuses_is_a_neutral_warn(tmp_path, field, word):
    """明確 `!!bool yes`：兩條 lint 路徑都給 WARN、rc 不變，指向 da-guard 與 validate-config，
    不斷言 exporter 會怎麼處理這份檔。不綁 `make` 入口（盲審第 4 輪 R4-1：Makefile 寫死
    conf.d 路徑，對其他樹不成立；這句也會出現在 validate-config 自己的輸出裡）。
    #2740：lint 那行不承諾 `profiles` 列（它沒有 da-guard）；租戶檔在 da-guard 的 parse_failed 裡，
    所以 validate-config 那行保留、且指向 `profiles` 列——兩行只差最後一句。"""
    conf_d, line = _tree(tmp_path, field, word)
    p = _lint(conf_d)
    assert p.returncode == 0, p.stderr
    (w,) = _warn_lines(p)
    assert w.startswith(f"WARN: {FIELDS[field][0]}:{line}: {FIELDS[field][2]}: explicit "
                        f"`!!bool {word.split(' ', 1)[1].strip(chr(39))}`"), w
    for point in ("da-guard", "validate-config"):
        assert point in w, (point, w)
    assert "make" not in w, w
    for claim in ("cannot decode", "drops", "parse_failed", "exits 3"):
        assert claim not in w, (claim, w)
    assert "`profiles` row" not in w and "run validate-config with `--config-dir`" in w, w
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN and row["details"] == [_as_validate_config(w)], row
    # 盲審第 5 輪 R5-1／R5-4：補救寫在這一行自己身上（拿掉 tag、不叫人只加引號），
    # 並指向 validate-config 的 `profiles` 列；row 沒有另一套依種類變化的 hint。
    assert "remove the tag" in w and "quote it" not in w, w
    assert "`profiles` row" in row["details"][0], row
    assert row["hint"] is None, row


def _as_validate_config(lint_line: str) -> str:
    """lint 那行換成 validate-config 的最後一句（#2740）；其餘逐字相同。"""
    tail = _lib_io.GoRejectedBoolTag.LINT_POINTER
    assert lint_line.endswith(tail), lint_line
    return lint_line[:-len(tail)] + _lib_io.GoRejectedBoolTag.PROFILES_ROW_POINTER


_SLACK_TAGGED = ("    _routing:\n      receiver:\n        type: slack\n"
                 "        api_url: \"https://hooks.slack.com/services/x\"\n"
                 "        channel: !!bool yes\n")


def test_string_field_with_explicit_bool_tag_lines_agree(tmp_path):
    """盲審第 5 輪 R5-1：字串欄位寫 `channel: !!bool yes`，ERROR 與 WARN 兩行的建議不得互相矛盾——
    兩行都叫人拿掉 tag，沒有一行只叫人加引號，也沒有一行叫人改寫成 true / false。"""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / "t1.yaml").write_text("tenants:\n  t1:\n" + _SLACK_TAGGED, encoding="utf-8")
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.FAIL and len(row["details"]) == 2, row
    err, warn = row["details"]
    assert "explicit `!!bool yes`" in err and "unquoted" not in err, err
    assert 'remove the tag and quote it: channel: "yes"' in err, err
    assert warn.startswith("WARN: ") and "remove the tag" in warn, warn
    assert "a quoted string for a string" in warn, warn
    assert row["hint"] is None, row


def test_explicit_bool_tags_in_two_files_each_listed(tmp_path):
    """盲審第 5 輪 R5-2：兩份檔各一個明確 `!!bool`，兩行都列出（不只最後一份）。"""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    for t in ("t1", "t2"):
        (conf_d / f"{t}.yaml").write_text(
            f"tenants:\n  {t}:\n    _state_maintenance:\n      enabled: !!bool yes\n",
            encoding="utf-8")
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert [d.split(":", 2)[1].strip() for d in row["details"]] == ["t1.yaml", "t2.yaml"], row


@pytest.mark.parametrize("word", ["!!bool true", "!!bool True", "!!bool FALSE"])
def test_explicit_bool_tag_yaml_v3_takes_is_quiet(tmp_path, word):
    conf_d, _ = _tree(tmp_path, "send_resolved", word)
    tv.load_effective(conf_d)                              # 前提：Go 讀得了
    p = _lint(conf_d)
    assert p.returncode == 0 and "explicit `!!bool" not in p.stderr, p.stderr


def test_warn_message_states_only_verifiable_facts(tmp_path):
    """盲審第 2 輪 G：`_routing_profiles.yaml` 的 `send_resolved: on`，da-guard 的 hash 與寫 true
    時相同，舊訊息「effective／merged_hash 帶字串」不成立。訊息只說 PyYAML 讀成布林、exporter 的
    部分 reader 保留字串，不再斷言 merged_hash 或 effective 的值。"""
    conf_d = tmp_path / "conf.d"
    (conf_d / "sub").mkdir(parents=True)
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    profile = ("routing_profiles:\n  rp1:\n    receiver: {{type: webhook, "
               "url: 'https://h.example.com/x', send_resolved: {v}}}\n")
    (conf_d / "sub" / "t1.yaml").write_text("tenants:\n  t1:\n    _routing_profile: rp1\n",
                                            encoding="utf-8")
    (conf_d / "_routing_profiles.yaml").write_text(profile.format(v="true"), encoding="utf-8")
    hash_true = tv.load_effective(conf_d)["t1"].merged_hash
    (conf_d / "_routing_profiles.yaml").write_text(profile.format(v="on"), encoding="utf-8")
    assert tv.load_effective(conf_d)["t1"].merged_hash == hash_true     # 前提
    (w,) = _warn_lines(_lint(conf_d))
    assert w.startswith("WARN: _routing_profiles.yaml:3: "
                        "/routing_profiles/rp1/receiver/send_resolved: unquoted"), w
    assert w.endswith("PyYAML reads it as a boolean; some of the exporter's readers keep it "
                      "as a string"), w
    for claim in ("merged_hash", "effective", "da-guard", "Go readers"):
        assert claim not in w, (claim, w)


def test_fail_row_carries_the_warnings_too(tmp_path):
    """T2：同一列同時有 #2164 ERROR 與本 WARN 時，FAIL 列的明細是 errors + warnings。"""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (conf_d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _state_maintenance: {enabled: on}\n    _routing:\n"
        "      receiver:\n        type: slack\n"
        "        api_url: \"https://hooks.slack.com/services/x\"\n        channel: yes\n",
        encoding="utf-8")
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.FAIL, row
    errs = [d for d in row["details"] if not d.startswith("WARN:")]
    warns = [d for d in row["details"] if d.startswith("WARN:")]
    assert len(errs) == 1 and "channel" in errs[0], row
    assert len(warns) == 1 and "/_state_maintenance/enabled" in warns[0], row
    assert row["details"] == errs + warns, row


# ── 盲審第 3 輪：位置不影響判斷；exporter 讀不讀得了交給 da-guard ─────────────

def _explicit_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    conf_d = tmp_path / "conf.d"
    (conf_d / "sub").mkdir(parents=True)
    files = {"_defaults.yaml": _DEFAULTS, "sub/t1.yaml": "tenants:\n  t1:\n    mysql_connections: '5'\n",
             **files}
    for rel, body in files.items():
        (conf_d / rel).write_text(body, encoding="utf-8")
    return conf_d


_SM = "    _state_maintenance: {enabled: !!bool yes}\n"
_ROUTING_PROFILE_TAGGED = ("routing_profiles:\n  rp1:\n    receiver: {type: webhook, "
                           "url: 'https://h.example.com/x', send_resolved: !!bool yes}\n")
# (id, files, file the finding is in, in da-guard effective's parse_failed) — file kinds
# both lints read; the lint gives one WARN each, rc 0, in every one. validate-config keeps
# it only where da-guard names the file (#2740). A tenant file's second document is one
# the exporter's load does not decode a tag in (measured: parse_failed is empty), so it
# is not named either.
_PLACES = [
    ("tenant-doc2", {"sub/t1.yaml": "tenants:\n  t1:\n    mysql_connections: '5'\n---\n"
                                    "tenants:\n  t1:\n" + _SM}, "sub/t1.yaml", False),
    ("root-defaults", {"_defaults.yaml": _DEFAULTS + "  container_cpu: !!bool yes\n"},
     "_defaults.yaml", True),
    ("root-multidb-tenants", {"_defaults-multidb.yaml": "tenants:\n  t1:\n" + _SM},
     "_defaults-multidb.yaml", True),
    ("routing-profiles", {"_routing_profiles.yaml": _ROUTING_PROFILE_TAGGED},
     "_routing_profiles.yaml", False),
    ("routing-profiles-subdir", {"sub/_routing_profiles.yaml": _ROUTING_PROFILE_TAGGED},
     "sub/_routing_profiles.yaml", False),
]


def _parse_failed(conf_d: Path) -> list[str]:
    try:
        tv.load_effective(conf_d)
    except tv.ParseFailedError as exc:
        return exc.parse_failed
    return []


@pytest.mark.parametrize("name,files,rel,dropped", _PLACES, ids=[c[0] for c in _PLACES])
def test_explicit_bool_tag_is_the_same_warn_in_every_file_kind(tmp_path, name, files, rel,
                                                               dropped):
    conf_d = _explicit_tree(tmp_path, files)
    p = _lint(conf_d)
    hits = [l for l in (p.stdout + p.stderr).splitlines() if "explicit `!!bool yes`" in l]
    assert p.returncode == 0 and len(hits) == 1 and hits[0].startswith(f"WARN: {rel}:"), p.stderr
    assert (rel in _parse_failed(conf_d)) is dropped                 # 前提：da-guard 的判決
    row = vc.check_yaml_quoting(str(conf_d))
    if dropped:
        assert row["status"] == vc.WARN and row["details"] == [_as_validate_config(hits[0])], row
    else:
        # #2740：da-guard 不點名這份檔（exporter 不載入它、或不解碼那份文件）——不報。
        assert row["status"] == vc.PASS, row
        assert not any("explicit `!!bool" in d for d in row["details"]), row


def test_a_file_the_exporter_drops_fails_validate_config_through_da_guard(tmp_path):
    """換主體的前提：lint 只給 WARN，「exporter 丟掉整份檔」由 da-guard 判斷——`validate_config`
    的 profiles 列（da-guard effective）FAIL，指名該檔與 exporter 自己的理由（served-values）。"""
    conf_d = _explicit_tree(tmp_path, {"sub/t1.yaml": "tenants:\n  t1:\n" + _SM})
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL, row
    text = "\n".join(row["details"])
    assert "sub/t1.yaml" in text and "!!bool" in text, row


@pytest.mark.parametrize("value,exc", [("!!bool y", "KeyError"), ("!!bool 1", "KeyError"),
                                       ("!!bool foo", "KeyError"), ("!!int x", "ValueError")])
def test_value_pyyaml_cannot_construct_is_named_not_a_traceback(tmp_path, value, exc):
    """C／B3-7：PyYAML 的 constructor 建不出（`!!bool y` → KeyError、`!!int x` → ValueError）時，
    check_confd_schema 原本丟 traceback。現在具名報 ERROR（rc 1），措辭只講 lint 自己的限制；
    `!!bool` 那幾格另有中性 WARN。validate_config 的 quoting 列只做 compose，給 WARN 或 PASS。"""
    conf_d = _explicit_tree(tmp_path, {
        "sub/t1.yaml": f"tenants:\n  t1:\n    _state_maintenance: {{enabled: {value}}}\n"})
    p = _lint(conf_d)
    assert p.returncode == 1, p.stderr
    assert "Traceback" not in p.stderr, p.stderr
    (err,) = [l for l in p.stderr.splitlines() if l.startswith("ERROR: sub/t1.yaml")]
    assert err.startswith(f"ERROR: sub/t1.yaml: PyYAML cannot construct a value in this file "
                          f"({exc}: "), err
    assert err.endswith("this file was not schema-checked"), err
    assert "exporter" not in err, err
    tag_warns = [l for l in _warn_lines(p) if "explicit `!!bool" in l]
    assert len(tag_warns) == (1 if value.startswith("!!bool") else 0), p.stderr


# ── #2740（owner 選 B）：validate-config 以 da-guard 的 parse_failed 為準 ──────────

VALIDATE_CONFIG = REPO_ROOT / "scripts" / "tools" / "ops" / "validate_config.py"


def _validate_config(conf_d: Path, da_guard: str) -> dict[str, dict]:
    p = subprocess.run([sys.executable, "-X", "utf8", str(VALIDATE_CONFIG), "--config-dir",
                        str(conf_d), "--json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, env={**os.environ, "DA_GUARD_BINARY": da_guard})
    return {r["check"]: r for r in json.loads(p.stdout)}


def _explicit_warns(row: dict) -> list[str]:
    return [d for d in row["details"] if "explicit `!!bool" in d]


def _counting_da_guard(tmp_path: Path, real: str) -> tuple[str, Path]:
    """da-guard behind a wrapper that logs each subcommand it is run with."""
    log = tmp_path / "da-guard.log"
    wrapper = tmp_path / "da-guard-counting"
    wrapper.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nexec "{real}" "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper), log


def test_validate_config_keeps_the_warn_for_a_file_da_guard_drops(tmp_path, da_guard_binary):
    """租戶檔寫 `!!bool yes`：yaml_quoting 保留 WARN（指向 profiles 列），profiles FAIL 並指名該檔；
    兩列讀的是同一次 `da-guard effective`（validate-config 一次執行只跑一次）。"""
    conf_d = _explicit_tree(tmp_path, {"sub/t1.yaml": "tenants:\n  t1:\n" + _SM})
    da_guard, log = _counting_da_guard(tmp_path, da_guard_binary)
    rows = _validate_config(conf_d, da_guard)
    (w,) = _explicit_warns(rows["yaml_quoting"])
    assert w.startswith("WARN: sub/t1.yaml:3: ") and "`profiles` row" in w, w
    assert rows["yaml_quoting"]["status"] == vc.WARN, rows["yaml_quoting"]
    assert rows["profiles"]["status"] == vc.FAIL, rows["profiles"]
    assert "sub/t1.yaml" in "\n".join(rows["profiles"]["details"]), rows["profiles"]
    assert log.read_text(encoding="utf-8").split().count("effective") == 1, log.read_text()


def test_validate_config_two_dropped_files_each_keep_their_warn(tmp_path, da_guard_binary):
    conf_d = _explicit_tree(tmp_path, {
        f"sub/{t}.yaml": f"tenants:\n  {t}:\n" + _SM for t in ("t1", "t2")})
    rows = _validate_config(conf_d, da_guard_binary)
    assert [w.split(":", 2)[1].strip() for w in _explicit_warns(rows["yaml_quoting"])] == [
        "sub/t1.yaml", "sub/t2.yaml"], rows["yaml_quoting"]
    text = "\n".join(rows["profiles"]["details"])
    assert rows["profiles"]["status"] == vc.FAIL and "sub/t1.yaml" in text and "sub/t2.yaml" in text


@pytest.mark.parametrize("rel", ["_routing_profiles.yaml", "sub/_routing_profiles.yaml"])
def test_validate_config_drops_the_warn_for_a_routing_profiles_file(tmp_path, da_guard_binary,
                                                                    rel):
    """根目錄或子目錄的 `_routing_profiles.yaml` 寫 `!!bool yes`：exporter 不載入它，da-guard 的
    parse_failed 不點名、profiles PASS——yaml_quoting 不再報這則 WARN（lint 照報）。"""
    conf_d = _explicit_tree(tmp_path, {rel: _ROUTING_PROFILE_TAGGED})
    assert len([l for l in _warn_lines(_lint(conf_d)) if "explicit `!!bool" in l]) == 1
    rows = _validate_config(conf_d, da_guard_binary)
    assert _explicit_warns(rows["yaml_quoting"]) == [], rows["yaml_quoting"]
    assert rows["yaml_quoting"]["status"] == vc.PASS, rows["yaml_quoting"]
    assert rows["profiles"]["status"] == vc.PASS, rows["profiles"]


@pytest.mark.parametrize("no_verdict", ["missing", "exits-2"])
def test_without_a_da_guard_verdict_every_warn_is_kept(tmp_path, monkeypatch, no_verdict):
    """沒有 da-guard 的判決（找不到、或像沒有 `effective` 的舊版那樣 exit 2）：照舊全部保留，
    措辭不變（指向 profiles 列，那一列說明為何沒有判決）。"""
    if no_verdict == "missing":
        binary = tmp_path / "no-such-da-guard"
    else:
        binary = tmp_path / "old-da-guard"
        binary.write_text("#!/bin/sh\necho 'unknown subcommand' >&2\nexit 2\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setenv("DA_GUARD_BINARY", str(binary))
    conf_d = _explicit_tree(tmp_path, {"_routing_profiles.yaml": _ROUTING_PROFILE_TAGGED,
                                       "sub/t2.yaml": "tenants:\n  t2:\n" + _SM})
    lint_hits = [l for l in _warn_lines(_lint(conf_d)) if "explicit `!!bool" in l]
    assert len(lint_hits) == 2, lint_hits
    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert sorted(row["details"]) == sorted(_as_validate_config(l) for l in lint_hits), row
