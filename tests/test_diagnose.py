"""Tests for diagnose.py — tenant health check tool."""

import json
import os
import sys
from unittest import mock

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops'))
import diagnose  # noqa: E402


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------

class TestRunCmd:
    """Tests for run_cmd()."""

    def test_success(self):
        result = diagnose.run_cmd(["echo", "hello"])
        assert result == "hello"

    def test_failure_returns_none(self):
        result = diagnose.run_cmd(["false"])
        assert result is None

    def test_rejects_string_input(self):
        with pytest.raises(TypeError, match="requires list"):
            diagnose.run_cmd("echo hello")


# ---------------------------------------------------------------------------
# query_prometheus
# ---------------------------------------------------------------------------

class TestQueryPrometheus:
    """Tests for query_prometheus() — now uses query_prometheus_instant from _lib_python."""

    def test_success(self, monkeypatch):
        fake = lambda prom_url, promql: ([{"value": [0, "42"]}], None)
        monkeypatch.setattr(diagnose, "query_prometheus", fake)
        results, err = diagnose.query_prometheus("http://prom:9090", "up")
        assert err is None
        assert results[0]["value"][1] == "42"

    def test_error_returns_none(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "connection refused")
        monkeypatch.setattr(diagnose, "query_prometheus", fake)
        results, err = diagnose.query_prometheus("http://prom:9090", "up")
        assert results is None
        assert "connection refused" in err


# ---------------------------------------------------------------------------
# _h (bilingual help)
# ---------------------------------------------------------------------------

class TestHelp:
    """Tests for _h() helper."""

    def test_returns_string(self):
        result = diagnose._h("description")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_all_keys_accessible(self):
        for key in diagnose._HELP:
            assert isinstance(diagnose._h(key), str)


# ---------------------------------------------------------------------------
# lookup_tenant_profile
# ---------------------------------------------------------------------------

class TestLookupTenantProfile:
    """Tests for lookup_tenant_profile()."""

    def test_no_config_dir(self):
        assert diagnose.lookup_tenant_profile("db-a", None) is None

    def test_nonexistent_dir(self):
        assert diagnose.lookup_tenant_profile("db-a", "/nonexistent/path") is None

    def test_finds_profile_in_tenants_block(self, tmp_path):
        cfg = {"tenants": {"db-a": {"_profile": "high-load", "cpu": 90}}}
        (tmp_path / "multi.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        result = diagnose.lookup_tenant_profile("db-a", str(tmp_path))
        assert result == "high-load"

    def test_finds_profile_in_single_tenant_file(self, tmp_path):
        cfg = {"_profile": "low-load", "mem": 80}
        (tmp_path / "db-b.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        result = diagnose.lookup_tenant_profile("db-b", str(tmp_path))
        assert result == "low-load"

    def test_skips_hidden_files(self, tmp_path):
        cfg = {"_profile": "hidden", "cpu": 50}
        (tmp_path / ".hidden.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        result = diagnose.lookup_tenant_profile(".hidden", str(tmp_path))
        assert result is None

    def test_skips_non_yaml_files(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not yaml", encoding="utf-8")
        result = diagnose.lookup_tenant_profile("readme", str(tmp_path))
        assert result is None

    def test_skips_invalid_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(": : : invalid", encoding="utf-8")
        result = diagnose.lookup_tenant_profile("bad", str(tmp_path))
        assert result is None

    def test_skips_non_dict_yaml(self, tmp_path):
        (tmp_path / "list.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        result = diagnose.lookup_tenant_profile("list", str(tmp_path))
        assert result is None

    def test_tenant_not_found(self, tmp_path):
        cfg = {"tenants": {"db-a": {"cpu": 90}}}
        (tmp_path / "multi.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        result = diagnose.lookup_tenant_profile("db-z", str(tmp_path))
        assert result is None

    def test_no_profile_key(self, tmp_path):
        cfg = {"cpu": 90, "mem": 80}
        (tmp_path / "db-a.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        result = diagnose.lookup_tenant_profile("db-a", str(tmp_path))
        assert result is None

    def test_skips_directories(self, tmp_path):
        (tmp_path / "subdir.yaml").mkdir()
        result = diagnose.lookup_tenant_profile("subdir", str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# resolve_inheritance_chain
# ---------------------------------------------------------------------------

class TestResolveInheritanceChain:
    """Tests for resolve_inheritance_chain()."""

    def test_returns_none_for_no_config_dir(self):
        assert diagnose.resolve_inheritance_chain("db-a", None) is None

    def test_returns_none_for_nonexistent_dir(self):
        assert diagnose.resolve_inheritance_chain("db-a", "/nonexistent") is None

    def test_basic_defaults_only(self, tmp_path):
        defaults = {"defaults": {"cpu": 80, "mem": 70}}
        (tmp_path / "_defaults.yaml").write_text(yaml.safe_dump(defaults), encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result is not None
        assert result["profile_name"] is None
        assert result["resolved"]["cpu"] == 80

    def test_full_three_layers(self, tmp_path):
        # Layer 1: defaults
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 80, "mem": 70, "disk": 90}}),
            encoding="utf-8",
        )
        # Layer 2: profiles
        (tmp_path / "_profiles.yaml").write_text(
            yaml.safe_dump({"profiles": {"high-load": {"cpu": 95, "net": 60}}}),
            encoding="utf-8",
        )
        # Layer 3: tenant with profile ref
        (tmp_path / "db-a.yaml").write_text(
            yaml.safe_dump({"_profile": "high-load", "cpu": 99}),
            encoding="utf-8",
        )

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["profile_name"] == "high-load"
        # cpu: tenant override wins (99), not profile (95) or default (80)
        assert result["resolved"]["cpu"] == 99
        # net: from profile (fill-in)
        assert result["resolved"]["net"] == 60
        # mem, disk: from defaults
        assert result["resolved"]["mem"] == 70
        assert result["resolved"]["disk"] == 90

        # Chain has 3 layers
        assert len(result["chain"]) == 3
        assert result["chain"][0]["layer"] == "defaults"
        assert result["chain"][1]["layer"] == "profile"
        assert result["chain"][2]["layer"] == "tenant"

    def test_tenant_in_multi_tenant_file(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 50}}),
            encoding="utf-8",
        )
        multi = {"tenants": {"db-a": {"cpu": 75}, "db-b": {"cpu": 60}}}
        (tmp_path / "tenants.yaml").write_text(yaml.safe_dump(multi), encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["resolved"]["cpu"] == 75

    def test_skips_invalid_yaml(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(": : bad", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(yaml.safe_dump({"cpu": 50}), encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result is not None
        assert result["resolved"]["cpu"] == 50

    def test_no_profiles_file(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 50}}), encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(
            yaml.safe_dump({"_profile": "nonexistent", "mem": 80}), encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["profile_name"] == "nonexistent"
        assert result["resolved"]["mem"] == 80


# ---------------------------------------------------------------------------
# _format_chain_summary
# ---------------------------------------------------------------------------

class TestFormatChainSummary:
    """Tests for _format_chain_summary()."""

    def test_basic(self):
        inheritance = {
            "chain": [
                {"layer": "defaults", "source": "_defaults.yaml",
                 "keys": {"cpu": 80, "mem": 70}},
                {"layer": "tenant", "source": "db-a.yaml",
                 "keys": {"cpu": 99}},
            ],
            "resolved": {"cpu": 99, "mem": 70},
            "profile_name": None,
        }
        summary = diagnose._format_chain_summary(inheritance)
        assert summary["resolved_count"] == 2
        assert len(summary["layers"]) == 2
        assert summary["layers"][0]["key_count"] == 2
        assert summary["layers"][1]["key_count"] == 1
        assert summary["profile"] is None

    def test_empty(self):
        summary = diagnose._format_chain_summary({})
        assert summary["resolved_count"] == 0
        assert summary["layers"] == []


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------

class TestCheck:
    """Tests for check() — the main health check function.

    query_prometheus now returns (results, error) tuples via query_prometheus_instant.
    """

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_healthy(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            ([{"value": [1700000000, "1"]}], None),       # mysql_up
            ([], None),                                     # maintenance
            ([], None),                                     # silent
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "healthy"
        assert out["tenant"] == "db-a"

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_pod_not_found(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = [None, None]  # pod check fails, log fetch returns None
        mock_qp.return_value = (None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "Pod not found" in out["issues"]

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_pod_not_running(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = ["Pending", "ERROR log line\nERROR another"]
        mock_qp.return_value = (None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Pending" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_exporter_down(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = ["Running", None]  # pod ok, log fetch
        mock_qp.side_effect = [
            ([{"value": [0, "0"]}], None),  # mysql_up value "0" = DOWN
            ([], None),                       # maintenance
            ([], None),                       # silent
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Exporter" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_prometheus_query_fails(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.return_value = (None, "connection refused")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Prometheus" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_maintenance_mode(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            ([{"value": [1700000000, "1"]}], None),        # mysql_up
            ([{"value": [1700000000, "1"]}], None),        # maintenance active
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "healthy"
        assert out["operational_mode"] == "maintenance"

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_silent_mode_all(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            ([{"value": [1700000000, "1"]}], None),        # mysql_up
            ([], None),                                     # maintenance (empty)
            ([                                              # silent mode: both severities
                {"metric": {"target_severity": "warning"}, "value": [1700000000, "1"]},
                {"metric": {"target_severity": "critical"}, "value": [1700000000, "1"]},
            ], None),
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["operational_mode"] == "silent:all"

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_silent_mode_single_severity(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            ([{"value": [1700000000, "1"]}], None),        # mysql_up
            ([], None),                                     # no maintenance
            ([{"metric": {"target_severity": "warning"}, "value": [1700000000, "1"]}], None),
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["operational_mode"] == "silent:warning"

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_with_config_dir(self, mock_qp, mock_cmd, capsys, tmp_path):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            ([{"value": [1700000000, "1"]}], None),        # mysql_up
            ([], None),                                     # maintenance
            ([], None),                                     # silent
        ]
        # Create config files
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 80}}), encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(
            yaml.safe_dump({"_profile": "prod", "cpu": 95}), encoding="utf-8")
        (tmp_path / "_profiles.yaml").write_text(
            yaml.safe_dump({"profiles": {"prod": {"mem": 90}}}), encoding="utf-8")

        diagnose.check("db-a", "http://prom:9090", config_dir=str(tmp_path))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "healthy"
        assert out["profile"] == "prod"
        assert "inheritance_chain" in out

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_metrics_check_exception(self, mock_qp, mock_cmd, capsys):
        """Exception during Prometheus query is caught gracefully."""
        mock_cmd.side_effect = ["Running", None]
        # First call (mysql_up) raises, caught by except Exception
        mock_qp.side_effect = [
            Exception("connection error"),
            ([], None),  # maintenance
            ([], None),  # silent
        ]

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Metrics check failed" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_error_with_logs(self, mock_qp, mock_cmd, capsys):
        """Error result includes recent ERROR logs."""
        mock_cmd.side_effect = [
            None,  # pod check fails
            "2024-01-01 ERROR crash\n2024-01-01 INFO ok\n2024-01-01 ERROR oom",
        ]
        mock_qp.return_value = (None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert len(out["recent_logs"]) <= 3
        assert all("ERROR" in log for log in out["recent_logs"])
