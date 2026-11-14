"""validate-config 對退役閾值鍵（registry `retired_keys:`，#3829）的判決。

判決是 Go 的（`pkg/keyverdict`，經 `da-guard key-verdict`），Python 不留退役表。
三組量測：

* 前半建一支以 `-ldflags -X` 加入測試條目的 da-guard（`config.retiredThresholdKeysFixture`，
  只有測試建置會設），量的是機制本身，與提交的 registry 登記了什麼無關。
* 「有替代鍵」的真實登記（#3778）：用提交的 registry 建出的 da-guard，量 `mysql_connections`；
  拿掉 registry 的條目（`--regen` 後重建）這組就轉紅。
* 「沒有替代鍵」的真實登記（#3783 的 Kafka 鍵）：同樣直接量提交的 registry 條目。

租戶 id 與 fixture 退役鍵名都是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc
from _go_build import go_build_argv

_REPO = Path(__file__).resolve().parents[2]
_APP = _REPO / "components" / "threshold-exporter" / "app"
_OLD, _NEW = "fixture_old_conns", "mysql_connection_usage_percent"
_FIXTURE_X = (f"github.com/vencil/threshold-exporter/pkg/config.retiredThresholdKeysFixture="
              f"{_OLD}:{_NEW}:3829")


@pytest.fixture(scope="module")
def retired_da_guard(tmp_path_factory):
    """A da-guard whose retired table also holds `_OLD` → `_NEW`."""
    if shutil.which("go") is None:
        pytest.fail("`go` is not on PATH: the retired-key da-guard cannot be built")
    out = tmp_path_factory.mktemp("da-guard-retired") / "da-guard"
    proc = subprocess.run(go_build_argv(str(_APP), "./cmd/da-guard", str(out), ldflags_x=[_FIXTURE_X]),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False, timeout=600)
    if proc.returncode != 0:
        pytest.fail(f"go build da-guard failed (rc={proc.returncode}):\n{proc.stderr}")
    return str(out)


def _tree(root: Path, defaults: str) -> str:
    """The deployment's own root `_defaults.yaml` writes *defaults*; one tenant
    overrides the retired key, one writes nothing of it."""
    files = {
        "_defaults.yaml": defaults,
        "ta.yaml": f"tenants:\n  ta:\n    {_OLD}: \"70\"\n    {_NEW}: \"70\"\n",
        "tb.yaml": "tenants:\n  tb:\n    _silent_mode: warning\n",
    }
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _validate(conf_d: str, da_guard: str) -> tuple[int, dict]:
    env = {**os.environ, "DA_GUARD_BINARY": da_guard}
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", conf_d, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300, env=env)
    return p.returncode, {r["check"]: r for r in json.loads(p.stdout)}


def test_a_retired_key_in_the_deployments_root_defaults_fails_and_names_its_replacement(
        tmp_path, retired_da_guard):
    """部署方 root `defaults:` 寫了退役鍵（exporter 照樣送出一列）：rc 1，unread_keys FAIL，
    掛在 `_defaults.yaml` 那一行列出繼承它的租戶並點名替代鍵；租戶自己寫的那一筆也是 ERROR。"""
    conf_d = _tree(tmp_path, f"defaults:\n  {_OLD}: 80\n  {_NEW}: 80\n")
    rc, rows = _validate(conf_d, retired_da_guard)
    assert rc == 1, rows
    # The key verdict fails its own row and the routes row, which is
    # `generate-routes --validate` and reports the same verdict (#3788).
    assert [c for c, r in rows.items() if r["status"] == vc.FAIL] == ["routes", "unread_keys"], rows
    row = rows["unread_keys"]
    assert row["status"] == vc.FAIL, row
    lines = [d for d in row["details"] if f"`{_OLD}`" in d]
    assert any(d.startswith("_defaults.yaml: error: unread") and d.endswith("tenant(s) tb")
               for d in lines), lines
    assert any(d.startswith("ta.yaml: error: unread") for d in lines), lines
    for d in lines:
        assert "(retired: retired threshold key (#3829): write `mysql_connection_usage_percent` instead" in d, d
    assert not [d for d in row["details"] if f"unread `{_NEW}`" in d], row["details"]


def test_the_same_tree_without_the_retired_entry_is_only_advisory(tmp_path, da_guard_binary):
    """對照：同一棵樹、退役表沒有這個鍵的 da-guard（提交的 registry）——exporter 讀它，
    unread_keys 不列它，只剩 no_alert 的建議（#3829 之前的行為），rc 0。"""
    conf_d = _tree(tmp_path, f"defaults:\n  {_OLD}: 80\n  {_NEW}: 80\n")
    rc, rows = _validate(conf_d, da_guard_binary)
    assert not [d for d in rows["unread_keys"]["details"] if f"`{_OLD}`" in d], rows["unread_keys"]
    assert rows["no_alert"]["status"] == vc.WARN, rows["no_alert"]
    assert rc == 0, [r for r in rows.values() if r["status"] == vc.FAIL]


def test_served_values_carries_the_hint_and_keeps_the_measurement(tmp_path, retired_da_guard):
    """served-values 照實記錄 exporter 讀它（exporter: true），另帶 registry 的 hint。"""
    conf_d = _tree(tmp_path, f"defaults:\n  {_OLD}: 80\n")
    p = subprocess.run([retired_da_guard, "served-values", "--config-dir", conf_d],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert p.returncode == 0, p.stderr
    key = json.loads(p.stdout)["tenants"]["ta"]["keys"][_OLD]
    assert key["exporter"] is True, key
    assert "`mysql_connection_usage_percent`" in key["hint"], key


def _write(da_guard: str, conf_d: str, key: str) -> subprocess.CompletedProcess:
    """`da-guard write --json` setting tenant tb's *key* to "70"."""
    batch = {"files": [{"path": "tb.yaml", "ops": [
        {"op": "set", "path": ["tenants", "tb", key], "value": {"str": "70"}}]}]}
    return subprocess.run([da_guard, "write", "--config-dir", conf_d, "--input", "-", "--json"],
                          input=json.dumps(batch), capture_output=True, text=True, encoding="utf-8",
                          timeout=120, check=False)


def test_da_guard_write_refuses_a_newly_written_retired_key_naming_its_replacement(
        tmp_path, retired_da_guard):
    """`da-guard write` 新寫入退役鍵：拒絕、點名替代鍵、不說「沒有讀取端讀它」（exporter 其實讀），
    檔案不變；同一筆寫成替代鍵則通過。"""
    conf_d = _tree(tmp_path, f"defaults:\n  {_NEW}: 80\n")
    before = (tmp_path / "tb.yaml").read_text(encoding="utf-8")
    p = _write(retired_da_guard, conf_d, _OLD)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    out = p.stdout + p.stderr
    assert "write `mysql_connection_usage_percent` instead" in out and _OLD in out, out
    assert "no reader reads it" not in out, out
    assert (tmp_path / "tb.yaml").read_text(encoding="utf-8") == before
    p = _write(retired_da_guard, conf_d, _NEW)
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)


# ── 提交的 registry：`mysql_connections` 退役（#3778）─────────────────────────────

_REAL_OLD, _REAL_NEW = "mysql_connections", "mysql_connection_usage_percent"


def _real_tree(root: Path, key: str) -> str:
    """#3778 之前的部署形狀：root `_defaults.yaml` 寫舊鍵（沿用舊版出貨 defaults 的
    `mysql_connections: 80`），一個租戶覆寫它，一個租戶什麼都不寫。*key* 是舊鍵或其
    `_critical`。"""
    files = {
        "_defaults.yaml": f"defaults:\n  {_REAL_NEW}: 80\n  {key}: 80\n",
        "ta.yaml": f"tenants:\n  ta:\n    {key}: \"70\"\n",
        "tb.yaml": "tenants:\n  tb:\n    _silent_mode: warning\n",
    }
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


@pytest.mark.parametrize("key,replacement", [
    (_REAL_OLD, _REAL_NEW),
    (f"{_REAL_OLD}_critical", f"{_REAL_NEW}_critical"),
], ids=["base", "critical"])
def test_the_registered_mysql_connections_fails_in_defaults_and_tenant_naming_the_percent_key(
        tmp_path, da_guard_binary, key, replacement):
    """提交的 registry 登記了 `mysql_connections`：root defaults 與租戶檔各寫一筆，
    validate-config rc 1，unread_keys 兩處都是 ERROR 並點名 `%` 新鍵；`_critical`
    隨 base 推導，點名 `mysql_connection_usage_percent_critical`。"""
    rc, rows = _validate(_real_tree(tmp_path, key), da_guard_binary)
    assert rc == 1, rows
    row = rows["unread_keys"]
    assert row["status"] == vc.FAIL, row
    lines = [d for d in row["details"] if f"`{key}`" in d]
    assert any(d.startswith("_defaults.yaml: error: unread") for d in lines), row["details"]
    assert any(d.startswith("ta.yaml: error: unread") for d in lines), row["details"]
    for d in lines:
        assert f"(retired: retired threshold key (#3778): write `{replacement}` instead" in d, d
        assert "max_connections" in d, d  # the registry's hint rides along
# #3783: the committed registry retires these with `replaced_by: null`; the
# `_critical` twin is retired by derivation, not by an entry of its own.
_KAFKA_RETIRED = ("kafka_request_rate", "kafka_active_controllers", "kafka_request_rate_critical")


def test_the_committed_kafka_retirements_fail_wherever_written_and_name_no_replacement(
        tmp_path, da_guard_binary):
    """提交的 registry（#3783）：部署方 root `_defaults.yaml` 寫 `kafka_request_rate`、租戶各寫
    `kafka_active_controllers`／`kafka_request_rate_critical`——rc 1，三筆都是 unread／error／
    retired，說明沒有替代鍵、要刪掉；`_critical` 沒有自己的條目，靠 base 推導。"""
    files = {
        "_defaults.yaml": "defaults:\n  kafka_request_rate: 10000\n",
        "ta.yaml": "tenants:\n  ta:\n    kafka_active_controllers: \"1\"\n",
        "tb.yaml": "tenants:\n  tb:\n    kafka_request_rate_critical: \"20000\"\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    rc, rows = _validate(str(tmp_path), da_guard_binary)
    assert rc == 1, rows
    row = rows["unread_keys"]
    assert row["status"] == vc.FAIL, row
    where = {"kafka_request_rate": "_defaults.yaml", "kafka_active_controllers": "ta.yaml",
             "kafka_request_rate_critical": "tb.yaml"}
    for key in _KAFKA_RETIRED:
        lines = [d for d in row["details"] if f"unread `{key}`" in d]
        assert len(lines) == 1, (key, row["details"])
        d = lines[0]
        assert d.startswith(f"{where[key]}: error: unread"), d
        assert "(retired: retired threshold key (#3783) with no replacement: remove it;" in d, d
        assert "instead" not in d, d
