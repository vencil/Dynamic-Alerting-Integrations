"""scaffold_tenant 從 CLI 跑到產物檔的情境測試。

非互動模式對各種 --db 組合產出的 `<tenant>.yaml`、`_defaults.yaml`、
`scaffold-report.txt`，加上 --catalog 與無效 DB 類型的錯誤處理。

前身是 tests/scenarios/test-scaffold.sh。那支 shell 腳本沒有任何 runner
（Makefile、CI、pre-commit 都不引用），工具搬到 scripts/tools/ops/ 之後在第一次
呼叫就以 rc 2 結束也沒人發現；斷言逐條移到這裡，由 Python Tests 跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TOOL = _REPO / "scripts" / "tools" / "ops" / "scaffold_tenant.py"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(_TOOL), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


# tenant id → --db；每組只跑一次（module scope），各測試讀同一份產物。
_CASES = {
    "mariadb": ("t-maria", "mariadb"),
    "mariadb_redis": ("t-redis", "mariadb,redis"),
    "all_four": ("t-full", "mariadb,redis,mongodb,elasticsearch"),
    "oracle": ("t-ora", "oracle"),
    "db2": ("t-db2", "db2"),
    "oracle_db2": ("t-ent", "oracle,db2"),
    "clickhouse": ("t-ch", "clickhouse"),
}


@pytest.fixture(scope="module")
def scaffolded(tmp_path_factory):
    root = tmp_path_factory.mktemp("scaffold")
    out = {}
    for case, (tenant, dbs) in _CASES.items():
        d = root / case
        proc = _run("--tenant", tenant, "--db", dbs, "-o", d)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out[case] = (tenant, d)
    return out


def _read(scaffolded, case, name):
    return (scaffolded[case][1] / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("case", list(_CASES))
def test_writes_the_three_artifacts(scaffolded, case):
    tenant, d = scaffolded[case]
    for name in (f"{tenant}.yaml", "_defaults.yaml", "scaffold-report.txt"):
        assert (d / name).is_file(), name


def test_tenant_file_names_the_tenant(scaffolded):
    tenant, _ = scaffolded["mariadb"]
    assert tenant in _read(scaffolded, "mariadb", f"{tenant}.yaml")


@pytest.mark.parametrize("case,keys", [
    ("mariadb", ["mysql_connections", "container_cpu"]),
    ("mariadb_redis", ["redis_memory", "redis_connected_clients"]),
    # #1196 C：ES heap key 由 es_jvm_memory_used_percent 改名
    ("all_four", ["mongodb_connections", "es_heap_usage_percent"]),
    ("oracle", ["oracle_sessions_active", "oracle_tablespace_used_percent"]),
    ("db2", ["db2_connections_active", "db2_bufferpool_hit_ratio"]),
    ("oracle_db2", ["oracle_sessions_active", "db2_connections_active"]),
    ("clickhouse", ["clickhouse_queries_rate", "clickhouse_active_connections"]),
])
def test_defaults_declare_the_db_keys(scaffolded, case, keys):
    defaults = _read(scaffolded, case, "_defaults.yaml")
    for key in keys:
        assert key in defaults, key


@pytest.mark.parametrize("word", ["container_crashloop", "maintenance", "default_state"])
def test_defaults_carry_state_filters(scaffolded, word):
    assert word in _read(scaffolded, "mariadb", "_defaults.yaml")


@pytest.mark.parametrize("case,words", [
    ("mariadb", ["MariaDB"]),
    ("mariadb_redis", ["Redis"]),
    ("all_four", ["MongoDB", "Elasticsearch", "helm upgrade"]),
    ("oracle", ["Oracle"]),
    ("db2", ["DB2"]),
    ("clickhouse", ["ClickHouse"]),
])
def test_report_names_the_dbs(scaffolded, case, words):
    report = _read(scaffolded, case, "scaffold-report.txt")
    for word in words:
        assert word in report, word


def test_catalog_lists_the_db_types():
    proc = _run("--catalog")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = proc.stdout + proc.stderr
    for word in ("Kubernetes", "Redis", "Oracle", "DB2", "ClickHouse"):
        assert word in text, word


def test_unknown_db_type_is_rejected(tmp_path):
    proc = _run("--tenant", "t-err", "--db", "invalid_db", "-o", tmp_path / "out")
    assert proc.returncode != 0, proc.stdout
