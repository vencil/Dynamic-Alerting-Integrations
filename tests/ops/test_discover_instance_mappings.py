"""Tests for discover_instance_mappings.py — auto-discover 1:N mappings."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import urllib.parse

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import discover_instance_mappings as dim  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402


# ---------------------------------------------------------------------------
# parse_prometheus_text
# ---------------------------------------------------------------------------
class TestParsePrometheusText:
    def test_extracts_schema_labels(self):
        raw = textwrap.dedent("""\
            # HELP pg_stat_user_tables_n_tup_ins Number of rows inserted
            # TYPE pg_stat_user_tables_n_tup_ins counter
            pg_stat_user_tables_n_tup_ins{datname="production",schemaname="public",relname="users"} 1234
            pg_stat_user_tables_n_tup_ins{datname="production",schemaname="analytics",relname="events"} 5678
            pg_stat_user_tables_n_tup_ins{datname="staging",schemaname="public",relname="users"} 42
        """)
        result = dim.parse_prometheus_text(raw)
        assert "schemaname" in result
        assert result["schemaname"] == {"public", "analytics"}
        assert "datname" in result
        assert result["datname"] == {"production", "staging"}

    def test_extracts_tablespace(self):
        raw = 'oracledb_tablespace_used{tablespace="USERS"} 100\n' \
              'oracledb_tablespace_used{tablespace="SYSTEM"} 200\n'
        result = dim.parse_prometheus_text(raw)
        assert "tablespace" in result
        assert result["tablespace"] == {"USERS", "SYSTEM"}

    def test_skips_comments_and_empty(self):
        raw = "# HELP metric desc\n# TYPE metric gauge\nmetric 42\n"
        result = dim.parse_prometheus_text(raw)
        assert len(result) == 0  # no partition labels

    def test_ignores_non_partition_labels(self):
        raw = 'metric{instance="localhost",job="test"} 1\n'
        result = dim.parse_prometheus_text(raw)
        assert "instance" not in result
        assert "job" not in result

    def test_empty_label_values_skipped(self):
        raw = 'metric{schema=""} 1\n'
        result = dim.parse_prometheus_text(raw)
        assert "schema" not in result or len(result.get("schema", set())) == 0

    def test_kafka_topic(self):
        raw = 'kafka_topic_partitions{topic="orders"} 12\n' \
              'kafka_topic_partitions{topic="events"} 6\n'
        result = dim.parse_prometheus_text(raw)
        assert "topic" in result
        assert result["topic"] == {"orders", "events"}


# ---------------------------------------------------------------------------
# detect_db_type
# ---------------------------------------------------------------------------
class TestDetectDbType:
    def test_postgres(self):
        raw = "pg_stat_user_tables_n_tup_ins 1234\n"
        assert dim.detect_db_type(raw) == "pg"

    def test_oracle(self):
        raw = "oracledb_tablespace_used 100\n"
        assert dim.detect_db_type(raw) == "oracledb"

    def test_mysql(self):
        raw = "mysql_global_status_threads_connected 5\n"
        assert dim.detect_db_type(raw) == "mysql"

    def test_unknown(self):
        raw = "custom_metric_value 42\n"
        assert dim.detect_db_type(raw) == "unknown"


# ---------------------------------------------------------------------------
# rank_partition_labels
# ---------------------------------------------------------------------------
class TestRankPartitionLabels:
    def test_schema_ranks_high(self):
        label_values = {
            "schema": {"a", "b", "c"},
            "topic": {"x", "y"},
        }
        ranked = dim.rank_partition_labels(label_values)
        assert ranked[0][0] == "schema"  # schema ranks higher

    def test_single_value_excluded(self):
        label_values = {"schema": {"only_one"}}
        ranked = dim.rank_partition_labels(label_values)
        assert len(ranked) == 0  # needs 2+ values

    def test_ideal_cardinality_bonus(self):
        label_values = {
            "schema": {f"s{i}" for i in range(10)},      # ideal range
            "database": {f"d{i}" for i in range(300)},    # too many
        }
        ranked = dim.rank_partition_labels(label_values)
        # schema should rank higher due to ideal cardinality
        schema_score = next(s for n, v, s in ranked if n == "schema")
        db_score = next(s for n, v, s in ranked if n == "database")
        assert schema_score > db_score

    def test_empty_input(self):
        assert dim.rank_partition_labels({}) == []


# ---------------------------------------------------------------------------
# generate_mapping_draft
# ---------------------------------------------------------------------------
class TestGenerateMappingDraft:
    def test_basic_draft(self):
        draft = dim.generate_mapping_draft(
            "oracle-prod:9161",
            "tablespace",
            {"USERS", "SYSTEM"},
            db_type="oracle",
        )
        mapping = draft["instance_tenant_mapping"]
        assert "oracle-prod:9161" in mapping
        entries = mapping["oracle-prod:9161"]
        assert len(entries) == 2
        filters = {e["filter"] for e in entries}
        assert 'tablespace="SYSTEM"' in filters
        assert 'tablespace="USERS"' in filters
        # Tenant should be placeholder
        assert all("<FILL_TENANT_FOR_" in e["tenant"] for e in entries)

    def test_sorted_values(self):
        draft = dim.generate_mapping_draft(
            "host:9104", "schema", {"z_schema", "a_schema"})
        entries = draft["instance_tenant_mapping"]["host:9104"]
        # Should be sorted alphabetically
        assert "a_schema" in entries[0]["filter"]


# ---------------------------------------------------------------------------
# query_prometheus_label_values — W1 兩個真 bug 的回歸鎖
# ---------------------------------------------------------------------------
class TestQueryPrometheusLabelValues:
    """ROI r3 W1 修正的兩個 bug：matcher URL 未編碼 + label/values 分支不可達。"""

    def test_label_values_string_list_path_collects(self, monkeypatch):
        """無 matcher → /api/v1/label/<label>/values（回字串列表）真的收到值。

        修正前兩個分支測**相同**條件（`isinstance(data["data"], list)` ×2），
        elif 永不可達 → 字串列表路徑永遠收不到值、回傳恆空。
        """
        urls: list[str] = []

        def mock_get(url, timeout=10):
            urls.append(url)
            if "/api/v1/label/schema/values" in url:
                return {"status": "success", "data": ["sales", "hr"]}, None
            return {"status": "success", "data": []}, None

        monkeypatch.setattr(dim, "http_get_json", mock_get)
        result = dim.query_prometheus_label_values("http://prom:9090")
        assert result.get("schema") == {"sales", "hr"}
        assert any("/api/v1/label/schema/values" in u for u in urls)

    def test_series_dict_path_still_collects(self, monkeypatch):
        """有 matcher → /api/v1/series（回 label-set dict 列表）路徑照舊收值。"""
        def mock_get(url, timeout=10):
            return {"status": "success",
                    "data": [{"__name__": "m", "schema": "sales"},
                             {"__name__": "m", "schema": "hr"}]}, None

        monkeypatch.setattr(dim, "http_get_json", mock_get)
        result = dim.query_prometheus_label_values(
            "http://prom:9090", instance="db:9104")
        assert result.get("schema") == {"sales", "hr"}

    def test_non_list_data_ignored(self, monkeypatch):
        """data 非 list（例如錯誤物件）→ 安靜跳過，不 crash。"""
        monkeypatch.setattr(dim, "http_get_json",
                            lambda url, timeout=10: ({"data": {"oops": 1}}, None))
        assert dim.query_prometheus_label_values("http://prom:9090") == {}

    def test_matcher_with_space_and_quotes_is_encoded(self, monkeypatch):
        """matcher 含空白/引號時 URL 必須合法（#1112 InvalidURL 同類）。

        修正前 f-string 直插 `{instance="oracle prod:9161"}` —— 空白會讓
        http.client 直接 raise InvalidURL，這支查詢對含空白的 instance
        從來就不能用。
        """
        urls: list[str] = []

        def mock_get(url, timeout=10):
            urls.append(url)
            return {"status": "success", "data": []}, None

        monkeypatch.setattr(dim, "http_get_json", mock_get)
        dim.query_prometheus_label_values(
            "http://prom:9090", instance="oracle prod:9161")

        assert urls
        for url in urls:
            assert " " not in url          # InvalidURL 觸發字元
            assert '"' not in url          # 未編碼的引號
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            # round-trip：解碼後 matcher 完整還原
            assert qs["match[]"] == ['{instance="oracle prod:9161"}']


# ---------------------------------------------------------------------------
# CLI main (unit-level)
# ---------------------------------------------------------------------------
class TestCliMain:
    def test_no_args_exits(self):
        with pytest.raises(SystemExit):
            dim.main([])

    def test_prometheus_without_instance_or_job(self, capsys):
        rc = dim.main(["--prometheus", "http://localhost:9090"])
        assert rc == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert "requires" in captured.err.lower() or "requires" in captured.out.lower()


# ---------------------------------------------------------------------------
# --json empty envelope 形狀 (#1112)
# ---------------------------------------------------------------------------
class TestJsonEmptyEnvelope:
    """沒發現可用分區標籤時，`--json` 仍須吐一份完整 envelope。

    subprocess gate 只斷言 json.loads(stdout) 成功——status 值改一個字、
    reason 鍵拿掉，它照樣全綠。這裡逐鍵釘住形狀，並區分兩種「空」的成因
    （完全沒有標籤值 vs 有標籤值但沒有一個適合分區），因為 reason 是
    consumer 唯一能拿來分辨的東西。
    """

    def test_no_label_values_envelope(self, monkeypatch, capsys):
        """Prometheus 回不出任何標籤值 → reason=no_label_values、exit 1。"""
        monkeypatch.setattr(dim, "query_prometheus_label_values",
                            lambda *a, **kw: {})
        rc = dim.main(["--prometheus", "http://prom:9090",
                       "--instance", "stub:9104", "--json"])

        assert rc == EXIT_VIOLATION              # 空結果 = violation，exit 語意不變
        captured = capsys.readouterr()
        doc = json.loads(captured.out)           # 全文 parse ⇒ stdout 只有 JSON

        assert doc["status"] == "no_mappings"
        assert doc["reason"] == "no_label_values"
        assert doc["instance_tenant_mapping"] == {}     # 設計上清空的欄位確實是 {}
        # envelope 只帶「與 happy path 共用的 payload 鍵」+ discriminator，
        # 不憑空發明 db_type / partition_label（happy path 也沒有這些機器契約鍵）
        assert set(doc) == {"status", "reason", "instance_tenant_mapping"}

        # 人類訊息在 stderr，stdout 不含散文
        assert "label values" in captured.err or "標籤值" in captured.err
        assert "Querying" not in captured.out

    def test_no_suitable_label_envelope(self, monkeypatch, capsys):
        """有標籤值但沒一個適合分區（單值標籤）→ reason=no_suitable_label。

        rank_partition_labels() 排除 count<2 的標籤，故此處 ranked 為空但
        label_values 非空——與上一條是**不同的分支**，reason 必須分得開。
        """
        monkeypatch.setattr(dim, "query_prometheus_label_values",
                            lambda *a, **kw: {"schemaname": {"only-one-value"}})
        rc = dim.main(["--prometheus", "http://prom:9090",
                       "--instance", "stub:9104", "--json"])

        assert rc == EXIT_VIOLATION
        captured = capsys.readouterr()
        doc = json.loads(captured.out)

        assert doc["status"] == "no_mappings"
        assert doc["reason"] == "no_suitable_label"     # ≠ no_label_values
        assert doc["instance_tenant_mapping"] == {}
        assert set(doc) == {"status", "reason", "instance_tenant_mapping"}
        assert "none suitable" in captured.err.lower()
