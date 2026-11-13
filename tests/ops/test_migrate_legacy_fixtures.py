"""migrate_rule 對兩份舊式規則 fixture 的端到端輸出。

從 CLI 跑到產物檔：`legacy-dummy.yml`（MySQL，含 rate、critical 配對、
absent）與 `legacy-multidb.yml`（Redis / Elasticsearch / MongoDB，含維度
標籤）。驗的是轉換引擎：閾值抽取、聚合猜測（rate → sum、connections →
max）、維度提示、dry-run 計數、報告內容。

前身是 tests/scenarios/test-migrate-tool.sh 與 test-migrate-multidb.sh。那兩支
shell 腳本沒有任何 runner（Makefile、CI、pre-commit 都不引用），工具搬到
scripts/tools/ops/ 之後壞了也沒人發現；斷言逐條移到這裡，由 Python Tests 跑。

一律帶 --no-dictionary：fixture 的指標在 metric-dictionary.yaml 裡有黃金標準，
開著字典會被分流成「建議使用黃金標準」、不進轉換產物，就驗不到引擎。
字典分流由 test_migrate_rule_outputs.py 的 use_golden 測試管。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TOOL = _REPO / "scripts" / "tools" / "ops" / "migrate_rule.py"
_DUMMY = _REPO / "tests" / "fixtures" / "legacy-dummy.yml"
_MULTIDB = _REPO / "tests" / "fixtures" / "legacy-multidb.yml"


def _run(*args):
    proc = subprocess.run(
        [sys.executable, str(_TOOL), *map(str, args), "--no-dictionary"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout + proc.stderr


def _migrate(tmp_path_factory, fixture):
    out = tmp_path_factory.mktemp(fixture.stem) / "out"
    stdout = _run(fixture, "--tenant", "t1", "-o", out)
    return {
        "stdout": stdout,
        "out": out,
        "tenant": (out / ".conf.d" / "t1.yaml").read_text(encoding="utf-8"),
        "recording": (out / "platform-recording-rules.yaml").read_text(encoding="utf-8"),
        "alert": (out / "platform-alert-rules.yaml").read_text(encoding="utf-8"),
        "report": (out / "migration-report.txt").read_text(encoding="utf-8"),
    }


@pytest.fixture(scope="module")
def dummy(tmp_path_factory):
    return _migrate(tmp_path_factory, _DUMMY)


@pytest.fixture(scope="module")
def multidb(tmp_path_factory):
    return _migrate(tmp_path_factory, _MULTIDB)


def _dry_run(tmp_path_factory, fixture):
    # 帶一個不存在的 -o：dry-run 若寫了任何檔，這個目錄就會出現。只驗輸出
    # 訊息不夠——悄悄寫檔又沒印「檔案已輸出」的退化會漏掉。
    out = tmp_path_factory.mktemp(f"{fixture.stem}-dry") / "out"
    return _run(fixture, "--dry-run", "-o", out), out


@pytest.fixture(scope="module")
def dummy_dry_run(tmp_path_factory):
    return _dry_run(tmp_path_factory, _DUMMY)


@pytest.fixture(scope="module")
def multidb_dry_run(tmp_path_factory):
    return _dry_run(tmp_path_factory, _MULTIDB)


def _has(pattern, text):
    return re.search(pattern, text) is not None


# ---------------------------------------------------------------------------
# legacy-dummy.yml（MySQL）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    r"總規則數.*4",
    r"完美解析.*2",
    r"複雜.*自動猜測.*1",
    r"無法解析.*1",
    r"單點",  # 聚合猜測說明（單點上限）
])
def test_dummy_dry_run_reports_counts_and_guess(dummy_dry_run, pattern):
    stdout, _ = dummy_dry_run
    assert _has(pattern, stdout), stdout


def test_dummy_dry_run_writes_nothing(dummy_dry_run):
    stdout, out = dummy_dry_run
    assert "檔案已輸出" not in stdout
    assert not out.exists(), sorted(p.name for p in out.rglob("*"))


@pytest.mark.parametrize("pattern", [r"成功轉換.*3.*條", r"需人工處理", r"檔案已輸出"])
def test_dummy_stdout(dummy, pattern):
    assert _has(pattern, dummy["stdout"]), dummy["stdout"]


@pytest.mark.parametrize("name", [
    ".conf.d/t1.yaml",
    "platform-recording-rules.yaml",
    "platform-alert-rules.yaml",
    "migration-report.txt",
])
def test_dummy_writes_the_artifacts(dummy, name):
    assert (dummy["out"] / name).is_file()


@pytest.mark.parametrize("pattern", [
    r"mysql_global_status_threads_connected.*150",
    r"mysql_global_status_threads_connected_critical.*200",
])
def test_dummy_tenant_thresholds(dummy, pattern):
    assert _has(pattern, dummy["tenant"]), dummy["tenant"]


def test_dummy_recording_rules(dummy):
    rec = dummy["recording"]
    # 預設 custom_ 前綴隔離：record 名是 tenant:custom_<metric>:<agg>
    assert "tenant:custom_mysql_global_status_threads_connected:" in rec
    # base_key 取指標名，不是外層函式名
    assert "tenant:rate:" not in rec
    # 聚合猜測：rate() → sum；connections → max
    assert _has(r"sum by.tenant. .rate.", rec), rec
    assert _has(r"max by.tenant..*mysql_global_status_threads_connected", rec), rec


@pytest.mark.parametrize("word", ["maintenance", "group_left"])
def test_dummy_alert_rules(dummy, word):
    assert word in dummy["alert"]


@pytest.mark.parametrize("word", ["完美解析", "LLM", "absent"])
def test_dummy_report(dummy, word):
    assert word in dummy["report"]


# ---------------------------------------------------------------------------
# legacy-multidb.yml（Redis / Elasticsearch / MongoDB）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [r"成功轉換.*7", r"1.*需人工處理"])
def test_multidb_stdout(multidb, pattern):
    assert _has(pattern, multidb["stdout"]), multidb["stdout"]


@pytest.mark.parametrize("pattern", [
    r"redis_memory_used_bytes.*8589934592",
    r"redis_queue_length.*500",
    r"elasticsearch_indices_store_size_bytes_total.*107374182400",
    r"elasticsearch_indices_docs_total.*500000000",
    r"mongodb_connections_current.*500",
    r"mongodb_dbstats_storage_size.*53687091200",
])
def test_multidb_tenant_thresholds(multidb, pattern):
    assert _has(pattern, multidb["tenant"]), multidb["tenant"]


def test_multidb_dimensional_hint(multidb):
    # redis queue、ES index、MongoDB database 三條帶維度標籤
    assert "維度" in multidb["tenant"]


def test_multidb_recording_rules(multidb):
    rec = multidb["recording"]
    assert "tenant:custom_redis_memory_used_bytes:" in rec
    assert "tenant:custom_mongodb_connections_current:" in rec
    # RedisHighKeyEvictions：base_key 是 redis 指標，不是 rate
    assert "tenant:rate:" not in rec
    assert "tenant:custom_redis_evicted_keys_total" in rec
    assert _has(r"sum by.tenant. .rate.", rec), rec
    assert _has(r"max by.tenant..*mongodb_connections_current", rec), rec


@pytest.mark.parametrize("word", ["MongoDBDown", "absent", "維度標籤"])
def test_multidb_report_carries_the_absent_rule(multidb, word):
    assert word in multidb["report"]


@pytest.mark.parametrize("pattern", [r"總規則數.*8", r"Dry-Run"])
def test_multidb_dry_run(multidb_dry_run, pattern):
    stdout, _ = multidb_dry_run
    assert _has(pattern, stdout), stdout


def test_multidb_dry_run_writes_nothing(multidb_dry_run):
    _, out = multidb_dry_run
    assert not out.exists(), sorted(p.name for p in out.rglob("*"))
