"""唯讀工具的 conf.d 讀取改走 `da-guard parse-tree`（ADR-036 步驟 4，#2906 E1）。

每支工具四件事，各一列：

1. **yaml.v3 的讀法**：一個 PyYAML（YAML 1.1）與 yaml.v3 讀法不同的值
   （未加引號的 `yes`、`0o17`、`0x1F4`、日期、`on:` 鍵），工具給的是 yaml.v3
   的答案。
2. **工具封閉**：同一次執行裡 conf.d 不經 PyYAML——把 PyYAML 的兩個 parser
   （純 Python 的 `yaml.reader.Reader`、libyaml 的 `CParser`）改成一建構就
   失敗，工具照樣跑完。輸出用的 `yaml.dump` 是 emitter，不受影響。
3. **讀不了的檔被指名**：parse-tree 讀不了的檔（語法錯、重複鍵、非 UTF-8、
   頂層不是 mapping）出現在輸出或錯誤裡，不是悄悄略過。
4. **沒有 da-guard ⇒ exit 2**：parse-tree 那一條路拿不到 binary 時是呼叫端
   錯誤，不是「沒有發現」。只換掉 parse-tree 的 binary 解析，served-values /
   effective 照常，所以量到的是 parse-tree 那條路的對應。

⛔ 第 2 項的 patch 範圍是「整個 PyYAML 的解析」，比「conf.d 的解析」寬：凡是
工具本來就會讀非 conf.d YAML 的（metric dictionary、rule pack、chart values、
文件裡的 fenced YAML），那一列只驅動它讀 conf.d 的那一段，並在該列寫明。
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

for _sub in ("", "ops", "lint"):
    _p = str(TOOLS / _sub) if _sub else str(TOOLS)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _lib_parse_tree  # noqa: E402
from _lib_io import YamlFileError  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tool(name: str):
    return importlib.import_module(name)


def _write(root: pathlib.Path, files: dict[str, "str | bytes"]) -> pathlib.Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def no_yaml_parse(monkeypatch):
    """Every PyYAML parse raises (both parsers) — and is recorded, so a tool
    that swallows the AssertionError (`except Exception: continue`) still
    fails the test at teardown instead of passing on its fallback."""
    calls: "list[str]" = []

    def refuse(self, *a, **kw):
        calls.append(f"{a[:1]!r:.120}")
        raise AssertionError(f"PyYAML parse during the run: {calls[-1]}")

    monkeypatch.setattr(yaml.reader.Reader, "__init__", refuse)
    if hasattr(yaml, "CParser"):
        monkeypatch.setattr(yaml.CParser, "__init__", refuse)
    yield calls
    assert not calls, f"PyYAML parsed during the run: {calls}"


@pytest.fixture
def no_parse_tree_binary(monkeypatch):
    """`da-guard parse-tree` finds no binary; every other da-guard run is real."""
    def missing(args, binary, timeout, stdin=None):
        raise _lib_parse_tree.BinaryMissing("da-guard binary not found (test)")

    monkeypatch.setattr(_lib_parse_tree, "_run", missing)


def test_the_closure_guard_fires_on_a_yaml_parse(no_yaml_parse):
    """Must-fire control for `no_yaml_parse`: the parse raises and is
    recorded (cleared here, or this control's own teardown would fail)."""
    with pytest.raises(AssertionError, match="PyYAML parse"):
        yaml.safe_load("a: 1\n")
    assert len(no_yaml_parse) == 1
    no_yaml_parse.clear()


def _exit_code(fn, *args) -> int:
    try:
        rc = fn(*args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return rc if isinstance(rc, int) else 0


# ── gitops_check ─────────────────────────────────────────────────────────

_GITOPS = {"_defaults.yaml": "defaults:\n  cpu: 1\n",
           "team/t.yaml": "tenants:\n  t1: {cpu: 2}\nyes: 1\n"}


def test_gitops_check_reads_the_yaml_v3_way(tmp_path):
    """`yes:` is the key "yes" (PyYAML: True, on which `.startswith` raised);
    a nested tenant file is counted (the exporter's walk, not `iterdir`)."""
    gc = _tool("gitops_check")
    result = gc.check_local(str(_write(tmp_path, _GITOPS)))
    assert result.status == "pass", result
    assert result.details["tenant_files"] == 1
    assert result.details["total_metrics"] == 2  # `tenants` and `yes`


def test_gitops_check_parses_no_yaml(tmp_path, no_yaml_parse):
    gc = _tool("gitops_check")
    assert gc.check_local(str(_write(tmp_path, _GITOPS))).status == "pass"


def test_gitops_check_names_an_unreadable_defaults_carrier(tmp_path):
    gc = _tool("gitops_check")
    result = gc.check_local(str(_write(tmp_path, {"_defaults.yaml": "a: 1\na: 2\n"})))
    assert result.status == "fail"
    assert "_defaults.yaml is invalid: duplicate_key" in result.message, result.message


def test_gitops_check_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    gc = _tool("gitops_check")
    monkeypatch.setattr(sys, "argv", ["gitops_check", "local", "--dir",
                                      str(_write(tmp_path, _GITOPS))])
    assert _exit_code(gc.main) == 2


# ── diagnose (`optional_overrides`) ──────────────────────────────────────

_DIAG = {"_defaults.yaml": "defaults:\n  cpu: 50\noptional_overrides: [yes, 010, ~, x]\n",
         "t.yaml": "tenants:\n  tx:\n    cpu: 70\n"}


def test_diagnose_declared_keys_are_the_exporter_text(tmp_path):
    """Each entry's source text, as the exporter's `[]string` reads it."""
    dg = _tool("diagnose")
    chain = dg.resolve_inheritance_chain("tx", str(_write(tmp_path, _DIAG)))
    assert chain["declared"] == ["yes", "010", "x"]


def test_diagnose_parses_no_yaml(tmp_path, no_yaml_parse):
    dg = _tool("diagnose")
    assert dg.resolve_inheritance_chain("tx", str(_write(tmp_path, _DIAG)))["declared"]


def test_diagnose_names_a_carrier_parse_tree_refuses(tmp_path, capsys):
    """The exporter reads a `!!set` mapping, parse-tree refuses the tag: the
    declared list is left empty and the reason named."""
    dg = _tool("diagnose")
    d = _write(tmp_path, {**_DIAG, "_defaults.yaml":
                          "defaults: !!set {cpu}\noptional_overrides: [x]\n"})
    assert dg.resolve_inheritance_chain("tx", str(d))["declared"] == []
    assert "optional_overrides not read (declared left empty): tag_not_allowed" in \
        capsys.readouterr().err


def test_diagnose_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    dg = _tool("diagnose")
    monkeypatch.setattr(sys, "argv", ["diagnose", "tx", "--show-inheritance",
                                      "--config-dir", str(_write(tmp_path, _DIAG))])
    assert _exit_code(dg.main) == 2


# ── generate_tenant_mapping_rules ───────────────────────────────────────

_MAP = {"_instance_mapping.yaml": ("instance_tenant_mapping:\n  yes:\n"
                                   "    - {tenant: 010, filter: 'schema=\"a\"'}\n"),
        "team/t.yaml": "tenants:\n  '010': {}\n"}


def _mapping_main(monkeypatch, d, *extra):
    m = _tool("generate_tenant_mapping_rules")
    monkeypatch.setattr(sys, "argv", ["gtmr", "--config-dir", str(d), "--metrics", "cpu",
                                      "--dry-run", *extra])
    return _exit_code(m.main)


def test_mapping_rules_read_the_yaml_v3_way(tmp_path, monkeypatch, capsys):
    """Instance `yes` is "yes" (PyYAML: True); tenant `010` is "010", and the
    nested tenant file declares it (the exporter's walk)."""
    assert _mapping_main(monkeypatch, _write(tmp_path, _MAP), "--validate") == 0
    out = capsys.readouterr()
    assert "  yes:\n    → 010 (schema=\"a\")" in out.out, out
    assert "ERROR" not in out.err, out.err


def test_mapping_rules_parse_no_yaml(tmp_path, monkeypatch, no_yaml_parse):
    assert _mapping_main(monkeypatch, _write(tmp_path, _MAP), "--validate") == 0


def test_mapping_rules_name_an_unreadable_tenant_file(tmp_path, monkeypatch, capsys):
    d = _write(tmp_path, {**_MAP, "team/bad.yaml": "tenants:\n  a: {}\n  a: {}\n"})
    assert _mapping_main(monkeypatch, d, "--validate") == 2
    err = capsys.readouterr().err
    assert "bad.yaml" in err and "duplicate_key" in err, err


def test_mapping_rules_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    assert _mapping_main(monkeypatch, _write(tmp_path, _MAP)) == 2


# ── backtest_threshold (`--config-dir` / `--baseline`) ──────────────────
# #3111 removed `--git-diff` and the recipe scan that read tenant files: both
# trees, recipes included, are read by `da-guard served-values` (the
# exporter's own load), so the backtest has no `parse-tree` reader. The four
# rows below pin that on the served-values path. The observed-map the
# backtest itself needs is not conf.d and is read with PyYAML.

_BT = {"db-a.yaml": ("tenants:\n  db-a:\n    cpu: '70'\n    _custom_alerts:\n"
                     "      - {recipe: threshold, name: q, metric: m, op: '>', window: 5m,"
                     " threshold: '1:warning'}\n"),
       "team/db-b.yaml": "tenants:\n  db-b:\n    on: yes\n"}
_BT_BASE = {"db-a.yaml": "tenants:\n  db-a:\n    cpu: '70'\n",
            "team/db-b.yaml": "tenants:\n  db-b:\n    on: yes\n"}


def _bt_trees(tmp_path, new=_BT):
    bt = _tool("backtest_threshold")
    return bt, bt.load_trees(str(_write(tmp_path / "new", new)),
                             str(_write(tmp_path / "base", _BT_BASE)))


def test_backtest_reads_the_yaml_v3_way(tmp_path):
    bt, trees = _bt_trees(tmp_path)
    db_b = trees[1][1].tenants["db-b"]
    assert "on" in db_b.keys and db_b.unserved == {"on": "yes"}  # PyYAML: {True: True}
    assert bt.changed_recipe_tenants(trees) == ["db-a"]


def test_backtest_parses_no_yaml(tmp_path, no_yaml_parse):
    bt, trees = _bt_trees(tmp_path)
    assert bt.changed_recipe_tenants(trees) == ["db-a"]


def test_backtest_names_an_unreadable_conf_file(tmp_path):
    with pytest.raises(YamlFileError, match="db-a.yaml"):
        _bt_trees(tmp_path, {"db-a.yaml": b"tenants:\n  a\xff: {}\n"})


def test_backtest_without_da_guard_is_exit_2(tmp_path, monkeypatch, capsys, no_parse_tree_binary):
    """No `parse-tree` binary changes nothing (the backtest has no such
    reader); no da-guard at all is rc 2, `served_values_unavailable`."""
    _bt_trees(tmp_path)
    bt = _tool("backtest_threshold")
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    monkeypatch.setattr(sys, "argv", ["bt", "--config-dir", str(tmp_path / "new"),
                                      "--baseline", str(tmp_path / "base"),
                                      "--prometheus", "http://127.0.0.1:9", "--json"])
    assert _exit_code(bt.main) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "served_values_unavailable"


# ── policy_opa_bridge (`input.defaults`) ────────────────────────────────

_OPA = {"_defaults.yaml": "defaults:\n  cpu: 80\nflag: yes\n",
        "t.yaml": "tenants:\n  t1: {cpu: '70'}\n"}


def _opa_main(monkeypatch, d):
    pob = _tool("policy_opa_bridge")
    monkeypatch.setattr(sys, "argv", ["pob", "--config-dir", str(d), "--dry-run"])
    return _exit_code(pob.main)


def test_opa_bridge_reads_the_yaml_v3_way(tmp_path, monkeypatch, capsys):
    assert _opa_main(monkeypatch, _write(tmp_path, _OPA)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["defaults"]["flag"] == "yes"  # PyYAML: true


def test_opa_bridge_parses_no_yaml(tmp_path, monkeypatch, no_yaml_parse):
    assert _opa_main(monkeypatch, _write(tmp_path, _OPA)) == 0


def test_opa_bridge_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    assert _opa_main(monkeypatch, _write(tmp_path, _OPA)) == 2


# ── policy_engine (`--policy`, a standalone file) ───────────────────────

_PE = {"_defaults.yaml": "defaults:\n  cpu: 80\n",
       "t.yaml": "tenants:\n  t1:\n    cpu: '70'\n    _metadata:\n      owner: \"yes\"\n"}
_PE_POLICY = ("policies:\n  - name: owner-is\n    target: _metadata.owner\n"
              "    operator: equals\n    value: yes\n    severity: error\n")


def _pe_main(tmp_path, policy_text):
    pe = _tool("policy_engine")
    d = _write(tmp_path / "conf.d", _PE)
    policy = tmp_path / "policy.yaml"
    policy.write_text(policy_text, encoding="utf-8")
    return _exit_code(pe.main, ["--config-dir", str(d), "--policy", str(policy), "--ci"])


def test_policy_file_reads_the_yaml_v3_way(tmp_path):
    """`value: yes` is the string "yes" (PyYAML: True, which the tenant's
    `"yes"` does not equal — the rule failed)."""
    assert _pe_main(tmp_path, _PE_POLICY) == 0


def test_policy_engine_parses_no_yaml(tmp_path, no_yaml_parse):
    assert _pe_main(tmp_path, _PE_POLICY) == 0


def test_policy_file_parse_tree_cannot_read_is_named(tmp_path, capsys):
    assert _pe_main(tmp_path, _PE_POLICY + "policies: []\n") == 2
    assert "duplicate_key" in capsys.readouterr().err


def test_policy_engine_without_da_guard_is_exit_2(tmp_path, no_parse_tree_binary):
    assert _pe_main(tmp_path, _PE_POLICY) == 2


def test_a_policy_file_whose_name_is_not_utf8_is_read(tmp_path):
    """A standalone file is judged under a fixed name: its own name (here
    not UTF-8, which parse-tree's JSON cannot carry as --path) is no part
    of how its content reads."""
    pe = _tool("policy_engine")
    d = _write(tmp_path / "conf.d", _PE)
    policy = os.path.join(os.fsencode(tmp_path), b"pol\xff.yaml")
    with open(policy, "wb") as f:
        f.write(_PE_POLICY.encode())
    assert _exit_code(pe.main, ["--config-dir", str(d), "--policy", os.fsdecode(policy), "--ci"]) == 0


# ── check_retire_drift ───────────────────────────────────────────────────

_RD = {"_defaults.yaml": "defaults:\n  cpu: 80\n",
       "team/t.yaml": "tenants:\n  db-x:\n    _metadata: {db_type: mariadb}\n"}


def test_retire_drift_reads_the_yaml_v3_way(tmp_path):
    """A nested tenant file is judged (parse-tree's selection)."""
    rd = _tool("check_retire_drift")
    d = _write(tmp_path / "conf.d", _RD)
    assert rd.conf_d_declared_db_type_tenants(d) == {"db-x": "mariadb"}


def test_retire_drift_parses_no_yaml(tmp_path, no_yaml_parse):
    rd = _tool("check_retire_drift")
    d = _write(tmp_path / "conf.d", _RD)
    assert rd.evaluate(d, tmp_path / "helm", tmp_path / "ns.yaml")["declared"] == {
        "db-x": "mariadb"}


def test_retire_drift_names_an_unreadable_tenant_file(tmp_path):
    rd = _tool("check_retire_drift")
    d = _write(tmp_path / "conf.d", {**_RD, "u.yaml": "tenants:\n  a: [\n"})
    with pytest.raises(YamlFileError, match="syntax_error"):
        rd.conf_d_declared_db_type_tenants(d)


def test_retire_drift_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    rd = _tool("check_retire_drift")
    d = _write(tmp_path / "conf.d", _RD)
    monkeypatch.setattr(sys, "argv", ["rd", "--config-dir", str(d), "--helm-dir",
                                      str(tmp_path / "helm"), "--namespaces-file",
                                      str(tmp_path / "ns.yaml")])
    assert _exit_code(rd.main) == 2


# ── check_path_metadata_consistency ─────────────────────────────────────

_PM = {"prod/t.yaml": "tenants:\n  t:\n    _metadata:\n      environment: yes\n"}


def _pm_main(monkeypatch, d):
    pm = _tool("check_path_metadata_consistency")
    monkeypatch.setattr(sys, "argv", ["pm", "--config-dir", str(d), "--ci"])
    return _exit_code(pm.main)


def test_path_metadata_reads_the_yaml_v3_way(tmp_path, monkeypatch, capsys):
    """`environment: yes` is "yes" and disagrees with `prod/` (PyYAML: True,
    not a string, so no finding)."""
    assert _pm_main(monkeypatch, _write(tmp_path, _PM)) == 0
    assert "field=environment path=prod metadata=yes" in capsys.readouterr().out


def test_path_metadata_parses_no_yaml(tmp_path, monkeypatch, no_yaml_parse):
    assert _pm_main(monkeypatch, _write(tmp_path, _PM)) == 0


def test_path_metadata_names_an_unreadable_file(tmp_path, monkeypatch, capsys):
    assert _pm_main(monkeypatch, _write(tmp_path, {**_PM, "prod/u.yaml": "a: [\n"})) == 0
    assert "u.yaml:0: warning: not checked — " in capsys.readouterr().err


def test_path_metadata_without_da_guard_is_exit_2(tmp_path, monkeypatch, no_parse_tree_binary):
    assert _pm_main(monkeypatch, _write(tmp_path, _PM)) == 2


# ── check_threshold_unit_sanity (its fixed conf.d dirs) ─────────────────

_US_REGISTRY = {"version": 1, "packs": {"t": {"k": {"value": 50, "unit": "%",
                                                     "tier": "defaults"}}}}
_US_CONFD = "components/threshold-exporter/config/conf.d"


def _us_errors(root):
    return _tool("check_threshold_unit_sanity").run_check(
        registry=_US_REGISTRY, root=str(root))["errors"]


def test_unit_sanity_reads_a_threshold_as_the_exporter(tmp_path):
    """`0x1F4` is the text the exporter's float parse refuses — no value is
    served from it — not PyYAML's int 500 (OUT-OF-DOMAIN for a `%`); `300`
    is still out of domain."""
    _write(tmp_path, {f"{_US_CONFD}/_defaults.yaml": "defaults:\n  k: 0x1F4\n",
                      f"{_US_CONFD}/t.yaml": "tenants:\n  t:\n    k: 300\n"})
    errors = _us_errors(tmp_path)
    assert len(errors) == 1 and errors[0].startswith(
        f"OUT-OF-DOMAIN: {_US_CONFD}/t.yaml: k = 300"), errors


def test_unit_sanity_parses_no_yaml(tmp_path, no_yaml_parse):
    _write(tmp_path, {f"{_US_CONFD}/_defaults.yaml": "defaults:\n  k: 50\n"})
    assert _us_errors(tmp_path) == []


def test_unit_sanity_names_an_unreadable_file(tmp_path):
    _write(tmp_path, {f"{_US_CONFD}/_defaults.yaml": "defaults:\n  k: 50\n  k: 60\n"})
    errors = _us_errors(tmp_path)
    assert len(errors) == 1 and errors[0].startswith(
        f"NOT-READ: {_US_CONFD}/_defaults.yaml: da-guard parse-tree cannot read it "
        "(duplicate_key"), errors


def test_unit_sanity_without_da_guard_is_exit_2(no_parse_tree_binary):
    assert _tool("check_threshold_unit_sanity").main(["--ci"]) == 2


# ── check_threshold_reachability (the tracked defaults artifacts) ───────

def test_reachability_reads_an_artifact_the_yaml_v3_way():
    gate = _tool("check_threshold_reachability")
    assert gate._parse_artifact("x/conf.d/_defaults.yaml",
                                b"defaults:\n  yes: 1\n  on: 0o17\n") == {
        "defaults": {"yes": 1, "on": 15}}  # PyYAML: {True: "0o17"} (one key)


def test_reachability_reads_the_real_artifacts_with_no_yaml(no_yaml_parse):
    gate = _tool("check_threshold_reachability")
    paths = gate._defaults_artifacts()
    assert paths
    for p in paths:
        gate._read_artifact_keys(p)


def test_reachability_names_an_unreadable_artifact():
    gate = _tool("check_threshold_reachability")
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_defaults_artifacts_match_schema(
            ["x/conf.d/_defaults.yaml"], read=lambda rel: "defaults: [\n")
    assert "x/conf.d/_defaults.yaml: cannot read/parse YAML: syntax_error" in str(exc.value)


def test_reachability_without_da_guard_is_exit_2(no_parse_tree_binary):
    assert _tool("check_threshold_reachability").main(["--ci"]) == 2
