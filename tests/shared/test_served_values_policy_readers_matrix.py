"""policy 讀取端語意矩陣：值寫在哪裡，答案都要與 Go 的 served-values 一致（#2115 0-B／B2）。

讀取端：`policy_engine`（`--config-dir`）、`validate_config.check_policy_dsl`（同一個引擎）、
`policy_opa_bridge`（`--dry-run` 印出的 OPA input）。

四個位置沿用 `test_served_values_readers_matrix.POSITIONS`：同一個值
（`custom_mysql_slow: 5`）只寫在根 `defaults:`、平台檔 `tenants:`、租戶檔、子樹
`_defaults.yaml` 其中之一；tenant-b 是對照組（7，永遠寫在自己的租戶檔）。舊讀取端
（`_lib_io.load_tenant_configs`）只讀根目錄租戶檔本身，所以 `custom_mysql_slow lte 3` 只在
「租戶檔」那格對 tenant-a FAIL（子樹那格整個租戶消失）。

裁決（#2115 (c)）：閾值看 exporter 實際發出的數字、排程閾值每個時段都要過；保留鍵看
「寫法＋繼承」（da-guard effective），Go 自動補的預設值不算有寫；`_routing` 看路由產生器
解析後的結果；`when` 同一套。OPA 的 `input.tenants` 保持原形狀，另加 `input.served`。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import policy_engine as pe
import policy_opa_bridge as pob
import validate_config as vc
from test_served_values_readers_matrix import POSITIONS, _tree

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_RULES = """policies:
  - name: slow-lte-3
    target: custom_mysql_slow
    operator: lte
    value: 3
  - name: slow-lte-6
    target: custom_mysql_slow
    operator: lte
    value: 6
  - name: slow-lte-3-when-conn
    target: custom_mysql_slow
    operator: lte
    value: 3
    when:
      target: mysql_connections
      operator: gte
      value: 80
  - name: slow-required
    target: custom_mysql_slow
    operator: required
"""


def _policy(tmp_path: Path, body: str = _RULES) -> Path:
    pol = tmp_path / "policy.yaml"   # conf.d 之外：不被當成設定檔讀
    pol.write_text(body, encoding="utf-8")
    return pol


def _engine(conf_d: Path, pol: Path, capsys, *extra: str) -> tuple[int, dict]:
    rc = pe.main(["--config-dir", str(conf_d), "--policy", str(pol), "--json", *extra])
    return rc, json.loads(capsys.readouterr().out)


def _want_from_served(conf_d: Path) -> set[tuple[str, str]]:
    """(tenant, rule) pairs that must FAIL, derived from served-values alone."""
    served = tv.load_served_values(conf_d)
    want = set()
    for tenant, t in served.items():
        v = t.values.get("custom_mysql_slow")
        if v is None:
            want.add((tenant, "slow-required"))
            continue
        if v > 3:
            want.add((tenant, "slow-lte-3"))
            if t.values.get("mysql_connections", 0) >= 80:
                want.add((tenant, "slow-lte-3-when-conn"))
        if v > 6:
            want.add((tenant, "slow-lte-6"))
    return want


# ── 四位置：三個讀取端都與 served-values 一致 ───────────────────────────────

@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_policy_engine_matches_served_values(where, tmp_path, capsys):
    conf_d = _tree(tmp_path, POSITIONS[where])
    rc, doc = _engine(conf_d, _policy(tmp_path), capsys, "--ci")
    got = {(v["tenant"], v["rule_name"]) for v in doc["violations"]}
    assert got == _want_from_served(conf_d), where
    # 必響：tenant-a=5 在四格都違反 lte 3（when 的 mysql_connections=80 只來自根 defaults）。
    assert got == {("tenant-a", "slow-lte-3"), ("tenant-a", "slow-lte-3-when-conn"),
                   ("tenant-b", "slow-lte-3"), ("tenant-b", "slow-lte-3-when-conn"),
                   ("tenant-b", "slow-lte-6")}, where   # tenant-b 對照組四格不變
    assert doc["tenants_evaluated"] == 2 and rc == 1, (where, doc)
    msgs = {v["message"] for v in doc["violations"] if v["tenant"] == "tenant-a"}
    assert "'custom_mysql_slow' 期望 ≤ 3，實際為 '5'" in msgs, msgs


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_check_policy_dsl_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    row = vc.check_policy_dsl(str(conf_d), str(_policy(tmp_path)))
    assert row["status"] == vc.FAIL, row
    got = {tuple(d.split(" — ")[0].removeprefix("[ERROR] ").split(": ", 1))
           for d in row["details"] if d.startswith("[ERROR] ")}
    assert got == _want_from_served(conf_d), (where, row["details"])
    assert ("tenant-a", "slow-lte-3") in got, where


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_opa_input_served_matches_served_values(where, tmp_path, capsys):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    assert pob.main(["--config-dir", str(conf_d), "--dry-run"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["served"] == {t: {k: s.values[k] for k in s.severities} for t, s in served.items()}, where
    assert doc["served"]["tenant-a"]["custom_mysql_slow"] == 5.0, where
    assert doc["served"]["tenant-b"]["custom_mysql_slow"] == 7.0, where
    # input.tenants：原形狀（寫法），加上繼承——四格都看得到 tenant-a 的 5 與根的 80。
    assert doc["tenants"]["tenant-a"]["custom_mysql_slow"] == 5, where
    assert doc["tenants"]["tenant-a"]["mysql_connections"] == 80, where
    assert doc["tenants"]["tenant-b"]["custom_mysql_slow"] == 7, where


# ── 別名：租戶用舊拼法寫值、規則用任一拼法都比得到（10-06 P1）────────────────

_ALIAS_RULES = """policies:
  - name: threads-lte-100
    target: mysql_threads_running
    operator: lte
    value: 100
  - name: cpu-lte-100
    target: mysql_cpu
    operator: lte
    value: 100
  - name: cpu-crit-lte-100
    target: mysql_cpu_critical
    operator: lte
    value: 100
"""


@pytest.mark.parametrize("spelling", ["mysql_threads_running", "mysql_cpu"])
def test_retired_spelling_in_tenant_or_rule_is_compared(spelling, tmp_path, capsys):
    """P0（現行拼法）與 P1（舊拼法 `mysql_cpu`）送出同一個值，結論必須相同：兩條規則都 FAIL。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
        "tenant-a.yaml": f"tenants:\n  tenant-a:\n    {spelling}: \"500\"\n"
                         f"    {spelling}_critical: \"900\"\n",
    })
    assert tv.load_served_values(conf_d)["tenant-a"].values["mysql_threads_running"] == 500.0
    rc, doc = _engine(conf_d, _policy(tmp_path, _ALIAS_RULES), capsys, "--ci")
    assert {v["rule_name"] for v in doc["violations"]} == {
        "threads-lte-100", "cpu-lte-100", "cpu-crit-lte-100"}, doc
    assert rc == 1
    row = vc.check_policy_dsl(str(conf_d), str(_policy(tmp_path, _ALIAS_RULES)))
    assert row["status"] == vc.FAIL and sum(d.startswith("[ERROR]") for d in row["details"]) == 3, row
    assert pob.main(["--config-dir", str(conf_d), "--dry-run"]) == 0
    served = json.loads(capsys.readouterr().out)["served"]["tenant-a"]
    assert served == {"mysql_threads_running": 500.0, "mysql_threads_running_critical": 900.0}


def test_wildcard_matches_the_retired_spelling_too(tmp_path, capsys):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_cpu: \"500\"\n",
    })
    rc, doc = _engine(conf_d, _policy(tmp_path, "policies:\n  - name: w\n    target: \"*_cpu\"\n"
                                                "    operator: lte\n    value: 100\n"), capsys)
    assert [(v["tenant"], v["target"]) for v in doc["violations"]] == [
        ("tenant-a", "mysql_threads_running")], doc


# ── 排程：每個時段都要過（served `--schedules` 的逐段輸出）──────────────────

def _scheduled(overrides: str) -> dict[str, str]:
    return {
        "_defaults.yaml": "defaults:\n  custom_mysql_slow: 1\n",
        "tenant-a.yaml": ("tenants:\n  tenant-a:\n    custom_mysql_slow:\n      default: \"2\"\n"
                          "      overrides:\n" + overrides),
    }


def test_one_window_over_the_limit_fails(tmp_path, capsys):
    conf_d = _tree(tmp_path, _scheduled('        - window: "22:00-06:00"\n          value: "9"\n'))
    assert tv.load_served_values(conf_d, at="2026-07-01T12:00:00Z")["tenant-a"].values[
        "custom_mysql_slow"] == 2.0   # 前提：白天那一刻是合規的
    rc, doc = _engine(conf_d, _policy(tmp_path, "policies:\n  - name: lte3\n    target: custom_mysql_slow\n"
                                                "    operator: lte\n    value: 3\n"), capsys, "--ci")
    assert rc == 1
    assert sorted(v["message"] for v in doc["violations"]) == [
        "'custom_mysql_slow' 期望 ≤ 3，實際為 '9'（UTC 00:00-06:00）",
        "'custom_mysql_slow' 期望 ≤ 3，實際為 '9'（UTC 22:00-24:00）"], doc


def test_every_window_within_the_limit_passes(tmp_path, capsys):
    """對照：所有時段都合規 → PASS。`disable` 時段（served 該段沒有列）只有 `required` 會在意。"""
    conf_d = _tree(tmp_path, _scheduled('        - window: "22:00-06:00"\n          value: "3"\n'
                                        '        - window: "12:00-13:00"\n          value: disable\n'))
    rc, doc = _engine(conf_d, _policy(tmp_path, "policies:\n  - name: lte3\n    target: custom_mysql_slow\n"
                                                "    operator: lte\n    value: 3\n"), capsys, "--ci")
    assert (rc, doc["violations"]) == (0, []), doc
    rc, doc = _engine(conf_d, _policy(tmp_path, "policies:\n  - name: req\n    target: custom_mysql_slow\n"
                                                "    operator: required\n"), capsys, "--ci")
    assert [v["message"] for v in doc["violations"]] == [
        "'custom_mysql_slow' 為必填欄位但未配置或為空（UTC 12:00-13:00）"], doc


# ── 保留鍵：寫法＋繼承；Go 自動補的不算有寫（既有規則不翻轉）────────────────

_RESERVED_RULES = """policies:
  - name: no-silent
    target: _silent_mode
    operator: forbidden
  - name: dedup-equals-disable
    target: _severity_dedup
    operator: equals
    value: disable
  - name: dedup-forbidden
    target: _severity_dedup
    operator: forbidden
  - name: metadata-required
    target: _metadata
    operator: required
  - name: conn-equals-80
    target: mysql_connections
    operator: equals
    value: 80
"""


def test_reserved_keys_read_as_written_plus_inherited(tmp_path, capsys):
    """served-values 對每個租戶都補 `_silent_mode: []`、`_severity_dedup: enable`、九欄
    `_metadata`；照它比會讓 `forbidden _silent_mode`／`equals`／`required _metadata` 翻轉。
    tenant-a 自己寫了 `_silent_mode`，`_severity_dedup: disable` 來自平台 `tenants:`（繼承）；
    tenant-b 都沒寫。閾值 `equals: 80` 以數字比（served 是 80.0）。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": ("defaults:\n  mysql_connections: 80\ntenants:\n  tenant-a:\n"
                           "    _severity_dedup: disable\n"),
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _silent_mode: disable\n",
        "tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
    })
    served = tv.load_served_values(conf_d)
    assert served["tenant-b"].values["_severity_dedup"] == "enable"   # 前提：Go 有自動補
    assert "_metadata" in served["tenant-b"].values
    rc, doc = _engine(conf_d, _policy(tmp_path, _RESERVED_RULES), capsys)
    assert sorted((v["tenant"], v["rule_name"]) for v in doc["violations"]) == [
        ("tenant-a", "dedup-forbidden"), ("tenant-a", "metadata-required"), ("tenant-a", "no-silent"),
        ("tenant-b", "metadata-required")], doc


# ── _routing：路由產生器解析後的結果 ────────────────────────────────────────

def test_routing_rule_reads_the_resolved_routing(tmp_path, capsys):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": ("defaults:\n  mysql_connections: 80\n_routing_defaults:\n  receiver:\n"
                           "    type: webhook\n    url: http://hooks.example.com/{{tenant}}\n"),
        "tenant-a.yaml": ("tenants:\n  tenant-a:\n    _routing:\n      receiver:\n        type: webhook\n"
                          "        url: https://hooks.example.com/a\n"),
        "team/tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
    })
    rc, doc = _engine(conf_d, _policy(tmp_path, "policies:\n  - name: https\n"
                                                "    target: _routing.receiver.url\n"
                                                "    operator: matches\n    value: \"^https://\"\n"), capsys)
    assert [(v["tenant"], v["message"]) for v in doc["violations"]] == [
        ("tenant-b", "'_routing.receiver.url' 期望匹配 /^https:///，實際為 'http://hooks.example.com/tenant-b'")], doc


# ── fail-closed 與 WARN ─────────────────────────────────────────────────────

def _cli(script: str, conf_d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPS / script), "--config-dir", str(conf_d), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def _clis(tmp_path: Path) -> list[tuple[str, tuple[str, ...]]]:
    return [("policy_engine.py", ("--policy", str(_policy(tmp_path)), "--json")),
            ("policy_opa_bridge.py", ("--dry-run",))]


def test_root_and_tenant_critical_fails_closed(tmp_path, capsys):
    """已知限制（#2115 10-03 F0，延後處理）：根 `defaults:` 與租戶同時寫 `X_critical` 時
    served-values rc 2、整棵樹讀不到；三個讀取端都 fail-closed，不評估、不繞過。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_connections_critical: 95\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections_critical: 95\n",
    })
    for script, extra in _clis(tmp_path):
        p = _cli(script, conf_d, *extra)
        assert p.returncode == 2, (script, p.returncode, p.stderr)
        assert "owns rows with different values" in p.stderr and "Traceback" not in p.stderr, p.stderr
    row = vc.check_policy_dsl(str(conf_d), str(_policy(tmp_path)))
    assert row["status"] == vc.FAIL and any("owns rows with different values" in d
                                            for d in row["details"]), row


def test_file_the_exporter_drops_fails_closed(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  custom_mysql_slow: 1\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    custom_mysql_slow: 7\n",
        "_platform.yaml": "tenants:\n  tenant-b: 3\n",
    })
    for script, extra in _clis(tmp_path):
        p = _cli(script, conf_d, *extra)
        assert p.returncode == 2, (script, p.returncode, p.stderr)
        assert "_platform.yaml" in p.stderr and "cannot unmarshal" in p.stderr, p.stderr
        assert "Traceback" not in p.stderr, p.stderr
    row = vc.check_policy_dsl(str(conf_d), str(_policy(tmp_path)))
    assert row["status"] == vc.FAIL and any("_platform.yaml" in d for d in row["details"]), row


def test_flat_file_is_named_from_skipped_and_is_not_a_tenant(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  custom_mysql_slow: 1\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    custom_mysql_slow: 2\n",
        "flat-a.yaml": "custom_mysql_slow: 5\n",
    })
    for script, extra in _clis(tmp_path):
        p = _cli(script, conf_d, *extra)
        assert p.returncode == 0, (script, p.stderr)
        assert "WARN: flat-a.yaml: declares no tenant" in p.stderr, p.stderr
        assert "flat-a" not in p.stdout, p.stdout
