"""Tests for discover_instance_mappings.py — auto-discover 1:N mappings."""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import discover_instance_mappings as dim  # noqa: E402


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
# CLI main (unit-level)
# ---------------------------------------------------------------------------
class TestCliMain:
    def test_no_args_exits(self):
        with pytest.raises(SystemExit):
            dim.main([])

    def test_prometheus_without_instance_or_job(self, capsys):
        rc = dim.main(["--prometheus", "http://localhost:9090"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "requires" in captured.err.lower() or "requires" in captured.out.lower()
