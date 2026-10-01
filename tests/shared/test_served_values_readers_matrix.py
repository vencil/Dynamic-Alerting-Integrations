"""讀取端語意矩陣：值寫在哪裡，讀取端的答案都要與 Go 的 served-values 一致（#2115 0-B／B1）。

讀取端：`blind_spot_discovery.load_monitored_db_types`、
`analyze_rule_pack_gaps.load_tenant_configs(config_dir=...)`（目錄模式）。

同一個值（`custom_mysql_slow: 5`）只寫在四個位置之一：根 `defaults:`、平台檔
`tenants:`、租戶檔、子樹 `_defaults.yaml`。tenant-a 是受測租戶；tenant-b 是對照組，
值永遠寫在自己的租戶檔（7），四個位置下答案都不變。

`mysql_connections` 只有根 defaults 有（80），租戶都沒寫：已監控（mariadb）只能來自
繼承。舊讀取端（`_lib_io.load_tenant_configs`）只讀根目錄租戶檔本身，所以除了
「租戶檔」那一格，tenant-a 的答案都錯（子樹那格整個租戶消失）。

另有三格：平面格式檔（Go 列進 `skipped`）、Go 丟掉的檔（`parse_failed`，fail-closed
rc 2）、`disable`／沒有預設值的鍵（不算已監控）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import analyze_rule_pack_gaps as arg
import blind_spot_discovery as bsd

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_BASE = "defaults:\n  mysql_connections: 80\n  custom_mysql_slow: 1\n"
_A_PLAIN = "tenants:\n  tenant-a:\n    _silent_mode: disable\n"
_A_VALUE = "tenants:\n  tenant-a:\n    custom_mysql_slow: 5\n"
_B = "tenants:\n  tenant-b:\n    custom_mysql_slow: 7\n"

POSITIONS = {
    "defaults": {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  custom_mysql_slow: 5\n",
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "platform-tenants": {
        "_defaults.yaml": _BASE + _A_VALUE,
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "tenant-file": {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": _A_VALUE,
        "tenant-b.yaml": _B,
    },
    "subtree": {
        "_defaults.yaml": _BASE,
        "team/_defaults.yaml": "defaults:\n  custom_mysql_slow: 5\n",
        "team/tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_the_oracle_serves_the_value_in_every_position(where, tmp_path):
    """前提（非空洞）：Go 在四個位置都發出 tenant-a=5、tenant-b=7。"""
    served = tv.load_served_values(_tree(tmp_path, POSITIONS[where]))
    assert served["tenant-a"].values["custom_mysql_slow"] == 5.0
    assert served["tenant-a"].values["mysql_connections"] == 80.0
    assert served["tenant-b"].values["custom_mysql_slow"] == 7.0


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_analyze_gaps_dir_mode_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    got = arg.load_tenant_configs(config_dir=str(conf_d))
    for tenant in ("tenant-a", "tenant-b"):
        assert got[tenant]["custom_mysql_slow"] == served[tenant].values["custom_mysql_slow"], (where, tenant)
    assert got["tenant-a"]["custom_mysql_slow"] == 5.0, where
    assert got["tenant-b"]["custom_mysql_slow"] == 7.0, where  # 對照組不變
    metrics = {(m["tenant"], m["metric_key"]): m["value"] for m in arg.extract_custom_metrics(got)}
    assert metrics == {("tenant-a", "custom_mysql_slow"): 5.0, ("tenant-b", "custom_mysql_slow"): 7.0}, where


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_blind_spot_monitored_set_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    want: dict[str, set[str]] = {}
    for tenant, t in served.items():
        for key in t.severities:
            db = bsd._infer_db_type_from_metric(key)
            if db:
                want.setdefault(db, set()).add(tenant)
    got = bsd.load_monitored_db_types(str(conf_d))
    assert got == want, where
    # 繼承來的 mysql_connections：兩個租戶在四個位置都算 mariadb 已監控。
    assert got == {"mariadb": {"tenant-a", "tenant-b"}}, where


def test_disabled_and_unserved_keys_are_not_monitored(tmp_path):
    """行為變更（changelog.d）：`disable` 的鍵與沒有預設值、Go 不發的鍵不算已監控。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_memory: 1024\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    redis_memory: disable\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_slow_queries: 5\n    custom_pg_x: 3\n",
    })
    served = tv.load_served_values(conf_d)
    assert "redis_memory" not in served["tenant-a"].values
    assert "mysql_slow_queries" not in served["tenant-b"].values
    assert bsd.load_monitored_db_types(str(conf_d)) == {"redis": {"tenant-b"}}
    assert arg.load_tenant_configs(config_dir=str(conf_d)) == {
        "tenant-a": {}, "tenant-b": {"redis_memory": 1024.0}}


def _cli(script: str, conf_d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPS / script), "--config-dir", str(conf_d), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


_CLIS = [
    ("blind_spot_discovery.py", ("--prometheus", "http://127.0.0.1:1", "--json-output")),
    ("analyze_rule_pack_gaps.py", ("--json",)),
]


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_flat_file_is_named_from_skipped_and_is_not_a_tenant(script, extra, tmp_path):
    """R3：平面格式檔由 Go 判定（`skipped`），讀取端照欄位具名 WARN，不當租戶。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-b.yaml": _B,
        "flat-a.yaml": "custom_mysql_slow: 5\n",
    })
    assert [s.file for s in tv.load_served_tree(conf_d).skipped] == ["flat-a.yaml"]
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 0, p.stderr
    assert "WARN: flat-a.yaml: declares no tenant" in p.stderr, p.stderr
    assert "flat-a" not in p.stdout, p.stdout


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_file_the_exporter_drops_fails_closed(script, extra, tmp_path):
    """被 exporter 丟掉的檔（`parse_failed`）：rc 2、指名該檔、沒有 traceback（R5 推翻後的契約）。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-b.yaml": _B,
        "_platform.yaml": "tenants:\n  tenant-b: 3\n",
    })
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, (p.returncode, p.stderr)
    assert "_platform.yaml" in p.stderr and "Traceback" not in p.stderr, p.stderr


@pytest.mark.parametrize("script, extra", _CLIS, ids=[c[0] for c in _CLIS])
def test_tree_the_exporter_rejects_fails_closed(script, extra, tmp_path):
    """跨檔重複宣告：Go rc 2，讀取端也 rc 2、一行 ERROR、沒有 traceback。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "b1.yaml": _B,
        "b2.yaml": _B,
    })
    p = _cli(script, conf_d, *extra)
    assert p.returncode == 2, (p.returncode, p.stderr)
    assert "duplicate tenant" in p.stderr and "Traceback" not in p.stderr, p.stderr
