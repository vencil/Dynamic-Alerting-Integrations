"""#2509（owner 裁決 ②）：允許布林的欄位寫了未加引號的 YAML 1.1 字眼要給 WARN，讀取端不動。

`send_resolved: on`、`_routing_enforced: {enabled: yes}`、`_state_maintenance: {enabled: off}`：
PyYAML（YAML 1.1）讀成布林，schema 因此放行；Go（yaml.v3）讀到的是字串 "on" / "yes" / "off"——
`da-guard effective` 端出字串、merged_hash 與 describe_tenant 不同。本檔釘住：

- 字眼的集合：以 Go `receiverspec.YAML11BoolLiterals`（spec.go）為準——去掉 yaml.v3 自己也讀成
  布林的 true/false 三種拼法，剩下 yes/no/on/off 各三種拼法；schema 的 `yamlBool` enum 與它一致。
- 兩條 lint 路徑都看得到：`check_confd_schema`（pre-commit 的 confd-schema hook）印
  `WARN: <檔>:<行>: <欄位路徑>…`、rc 不變；`validate_config` 的 `yaml_quoting` 列轉 WARN、
  明細指名檔案與欄位。
- 前提（必響）：每個欄位位置，Go 讀到的確實是字串、PyYAML 讀到的確實是布林——WARN 講的是真的。
- 不響的對照組：`true` / `false`（各種大小寫）、字串欄位寫 `yes`（那是 #2164 的 ERROR，不是本 WARN）、
  加引號的 `"yes"`。

修正前（main 59e58c81）兩條 lint 對 yes/on/no/off 都完全安靜（schema 讓它過、quoting 只看字串欄位）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import io
import json
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
    assert warns[0].startswith(f"WARN: {fname}:{line}: {path}: unquoted '{word}'"), warns
    want = "true" if word.lower() in ("yes", "on") else "false"
    assert warns[0].endswith(f"write it as: {path.rsplit('/', 1)[-1]}: {want}"), warns
    assert "1 warning(s)" in p.stdout, p.stdout

    row = vc.check_yaml_quoting(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert row["details"] == warns, row


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
        "WARN: _defaults.yaml:4: /_routing_enforced/enabled: unquoted 'on'"), p.stderr
    assert vc.check_yaml_quoting(str(conf_d))["status"] == vc.WARN
