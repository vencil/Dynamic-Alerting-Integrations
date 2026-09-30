"""Tests for diagnose.py — tenant health check tool."""

import io
import json
import os
import sys
from unittest import mock

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops'))
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

# issue 1513（本輪決策 N2）：Pod／exporter 兩項只對 db_type=mariadb 的租戶執行，
# check() 先查 tenant_expected_exporter。這組測試描述 MariaDB 租戶，所以第一個
# 回應固定是它；非 MariaDB 與沒宣告的情境在 test_diagnose_db_type_and_exit.py。
_MARIADB = ([{"metric": {"tenant": "db-a", "db_type": "mariadb"}, "value": [0, "1"]}], None)


def _mariadb_then(results, err):
    def fake(_url, query):
        if query.startswith("tenant_expected_exporter"):
            return _MARIADB
        return results, err
    return fake


class TestCheck:
    """Tests for check() — the main health check function.

    query_prometheus now returns (results, error) tuples via query_prometheus_instant.
    """

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_healthy(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            _MARIADB,                                       # tenant_expected_exporter
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
    def test_check_writes_to_the_given_stream_not_sys_stdout(
        self, mock_qp, mock_cmd, capsys,
    ):
        """`out=` 讓呼叫端不必動到行程全域就能拿到文件。

        ⛔ 這個 seam 存在的理由在 check() 的 docstring 裡：唯一的替代方案
        `contextlib.redirect_stdout` 換的是行程全域的 `sys.stdout`，平行呼叫時
        會把真 stdout 弄丟。batch_diagnose.py 曾經就是那個形狀。

        兩個方向都斷言：文件**進了** buf，而且**沒有**同時漏到 stdout —— 少了
        後半，一個「寫 buf 也寫 stdout」的實作會照樣通過，而那正是把行程全域
        重新拖下水的寫法。
        """
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            _MARIADB,                                       # tenant_expected_exporter
            ([{"value": [1700000000, "1"]}], None),       # mysql_up
            ([], None),                                     # maintenance
            ([], None),                                     # silent
        ]

        buf = io.StringIO()
        before = sys.stdout
        diagnose.check("db-a", "http://prom:9090", out=buf)

        assert json.loads(buf.getvalue())["tenant"] == "db-a"
        assert capsys.readouterr().out == "", "帶了 out= 就不該再寫 stdout"
        assert sys.stdout is before, "check() 不得改動行程全域的 sys.stdout"

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_pod_not_found(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = [None, None]  # pod check fails, log fetch returns None
        mock_qp.side_effect = _mariadb_then(None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "Pod not found" in out["issues"]

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_pod_not_running(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = ["Pending", "ERROR log line\nERROR another"]
        mock_qp.side_effect = _mariadb_then(None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Pending" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_exporter_down(self, mock_qp, mock_cmd, capsys):
        mock_cmd.side_effect = ["Running", None]  # pod ok, log fetch
        mock_qp.side_effect = [
            _MARIADB,                                       # tenant_expected_exporter
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
        mock_qp.side_effect = _mariadb_then(None, "connection refused")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert any("Prometheus" in i for i in out["issues"])

    @mock.patch("diagnose.run_cmd")
    @mock.patch("diagnose.query_prometheus")
    def test_maintenance_mode(self, mock_qp, mock_cmd, capsys):
        mock_cmd.return_value = "Running"
        mock_qp.side_effect = [
            _MARIADB,                                       # tenant_expected_exporter
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
            _MARIADB,                                       # tenant_expected_exporter
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
            _MARIADB,                                       # tenant_expected_exporter
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
            _MARIADB,                                       # tenant_expected_exporter
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
            _MARIADB,                                       # tenant_expected_exporter
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
        mock_qp.side_effect = _mariadb_then(None, "query failed")

        diagnose.check("db-a", "http://prom:9090")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert len(out["recent_logs"]) <= 3
        assert all("ERROR" in log for log in out["recent_logs"])


# ============================================================
# #1982: a root platform file's `tenants:` block
# ============================================================

class TestPlatformTenantBlock:
    """The platform's per-tenant DEFAULT: the tenant file wins key by key,
    whatever either file is called, and a platform file cannot create a
    tenant. `tx.yaml` sorts AFTER `_defaults.yaml` and `TX.yaml` BEFORE it;
    the reader used to take the first file in name order and stop, so the
    two spellings gave opposite answers."""

    _PLATFORM = ("defaults:\n  mysql_connections: 80\n"
                 "tenants:\n  tx:\n    mysql_connections: '60'\n    _profile: p1\n")

    @pytest.mark.parametrize("fname", ["tx.yaml", "TX.yaml", "0tx.yaml"])
    def test_tenant_file_wins_whatever_its_name(self, tmp_path, fname):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        (tmp_path / fname).write_text(
            "tenants:\n  tx:\n    mysql_connections: '70'\n    _profile: p2\n",
            encoding="utf-8")
        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == "70"
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "p2"

    def test_platform_value_kept_when_tenant_file_is_silent(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        (tmp_path / "tx.yaml").write_text("tenants:\n  tx: {}\n", encoding="utf-8")
        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == "60"
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "p1"

    def test_platform_file_cannot_create_a_tenant(self, tmp_path, capsys):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        def _warns():
            return [ln for ln in capsys.readouterr().err.splitlines()
                    if "tenants.tx" in ln]

        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == 80
        warns = _warns()
        assert len(warns) == 1 and "_defaults.yaml" in warns[0], warns
        # #1522: lookup_tenant_profile is a view over the chain now, so
        # called on its own it says it too — once.
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) is None
        warns = _warns()
        assert len(warns) == 1 and "_defaults.yaml" in warns[0], warns

    def test_unselected_carrier_spelling_is_not_read(self, tmp_path):
        """`_defaults.yaml` + `_defaults.yml`: only the selected `.yaml` is
        read by any plane (#1674). Merging every root file let the unread
        `.yml` override it (blind review: 61/good before, 62/bad after)."""
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n"
            "tenants:\n  tx:\n    mysql_connections: '61'\n    _profile: good\n",
            encoding="utf-8")
        (tmp_path / "_defaults.yml").write_text(
            "defaults:\n  mysql_connections: 80\n"
            "tenants:\n  tx:\n    mysql_connections: '62'\n    _profile: bad\n",
            encoding="utf-8")
        (tmp_path / "tx.yaml").write_text("tenants:\n  tx: {}\n", encoding="utf-8")
        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == "61"
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "good"


# ---------------------------------------------------------------------------
# #1522: lookup_tenant_profile and check() read conf.d through the chain
# ---------------------------------------------------------------------------

class TestProfileLookupSharesTheChainRead:
    """`lookup_tenant_profile` used to be a second reader of the same
    directory that skipped a file it could not read in silence, while
    `resolve_inheritance_chain` WARNed about it. It is now a view over the
    chain, and `check()` reads the directory once."""

    @staticmethod
    def _confd(tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n"
            "tenants:\n  tx:\n    _profile: gold\n", encoding="utf-8")
        (tmp_path / "tx.yaml").write_text(
            "tenants:\n  tx:\n    mysql_connections: 70\n", encoding="utf-8")
        (tmp_path / "_profiles.yaml").write_text(
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n", encoding="utf-8")
        # Another tenant's file that does not parse.
        (tmp_path / "broken.yaml").write_text(
            "tenants:\n  other:\n    _profile: [unclosed\n", encoding="utf-8")
        return str(tmp_path)

    def test_called_on_its_own_it_names_the_file_it_could_not_read(
            self, tmp_path, capsys):
        d = self._confd(tmp_path)
        assert diagnose.lookup_tenant_profile("tx", d) == "gold"
        err = capsys.readouterr().err
        assert "WARN: skip broken.yaml" in err, err

    def test_answer_is_the_chains_profile_name(self, tmp_path, capsys):
        d = self._confd(tmp_path)
        chain = diagnose.resolve_inheritance_chain("tx", d)
        assert chain["skipped_unusable_files"] == ["broken.yaml"]
        assert diagnose.lookup_tenant_profile("tx", d) == chain["profile_name"]

    @staticmethod
    def _check(d):
        with mock.patch.object(diagnose, "tenant_db_type",
                               return_value=(None, None)), \
                mock.patch.object(diagnose, "query_prometheus",
                                  return_value=([], None)):
            return diagnose.check("tx", "http://prom:9090",
                                  config_dir=d, out=io.StringIO())

    def test_check_resolves_the_chain_once_and_warns_once(
            self, tmp_path, capsys):
        """Counts `resolve_inheritance_chain` CALLS from `check()` — the
        profile comes from that one call, not from a second
        `lookup_tenant_profile` call over the same files."""
        d = self._confd(tmp_path)
        real = diagnose.resolve_inheritance_chain
        with mock.patch.object(diagnose, "resolve_inheritance_chain",
                               wraps=real) as spy:
            result = self._check(d)
        assert spy.call_count == 1
        assert result["profile"] == "gold"
        assert result["inheritance_chain"]["skipped_unusable_files"] == [
            "broken.yaml"]
        skips = [ln for ln in capsys.readouterr().err.splitlines()
                 if "WARN: skip broken.yaml" in ln]
        assert len(skips) == 1, skips

    # A value of the wrong type that the exporter's typed decode refuses:
    # it drops the WHOLE file (parse_failed), measured against LoadDir.
    # These used to end both readers with AttributeError / TypeError.
    _WRONG_TYPE = {
        "defaults-not-a-mapping": (
            "_defaults.yaml", "defaults: [1, 2]\n",
            "'defaults' must be a mapping, got list", 70, 60),
        "optional-overrides-not-a-list": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\noptional_overrides: 5\n",
            "'optional_overrides' must be a list, got int", 70, 60),
        "profile-body-not-a-mapping": (
            "_profiles.yaml", "profiles:\n  gold: [1, 2]\n",
            "'profiles.gold' must be a mapping, got list", 70, None),
        # Another profile's body: the file still goes, gold with it.
        "sibling-profile-body-not-a-mapping": (
            "_profiles.yaml",
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n  bad: [1, 2]\n",
            "'profiles.bad' must be a mapping, got list", 70, None),
        "optional-overrides-entry-a-mapping": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\noptional_overrides: [{a: 1}]\n",
            "'optional_overrides' entries must be scalars, got dict", 70, 60),
        "optional-overrides-entry-a-list": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\noptional_overrides: [[a]]\n",
            "'optional_overrides' entries must be scalars, got list", 70, 60),
    }

    # The other side of the line: each of these LOADS on the exporter
    # (parse_failed empty; the values are what /metrics serves, measured
    # against LoadDir), so nothing may be skipped.
    _NULL_IS_FINE = {
        "profile-body-null": (
            "_profiles.yaml", "profiles:\n  gold: ~\n", {
                "mysql_connections": 70, "mysql_slow_queries": 90}),
        "sibling-profile-body-null": (
            "_profiles.yaml",
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n  bad: ~\n", {
                "mysql_connections": 70, "mysql_slow_queries": 60}),
        "optional-overrides-null": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 90\n"
            "optional_overrides: ~\n", {
                "mysql_connections": 70, "mysql_slow_queries": 60}),
        "optional-overrides-scalar-entries": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 90\n"
            "optional_overrides: [1, ~, true, x]\n", {
                "mysql_connections": 70, "mysql_slow_queries": 60}),
        # /metrics serves no row without a platform default, so there is no
        # value to compare here — only that the file is not dropped.
        "defaults-null": ("_defaults.yaml", "defaults: ~\n", None),
    }

    @pytest.mark.parametrize("case", sorted(_NULL_IS_FINE))
    def test_a_null_value_is_not_a_wrong_type(self, tmp_path, capsys, case):
        fname, body, resolved = self._NULL_IS_FINE[case]
        files = {
            "_defaults.yaml":
                "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 90\n",
            "tx.yaml": "tenants:\n  tx:\n    _profile: gold\n"
                       "    mysql_connections: 70\n",
            "_profiles.yaml": "profiles:\n  gold:\n    mysql_slow_queries: 60\n",
        }
        files[fname] = body
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        d = str(tmp_path)
        assert diagnose.lookup_tenant_profile("tx", d) == "gold"
        chain = diagnose.resolve_inheritance_chain("tx", d)
        assert "skipped_unusable_files" not in chain
        if resolved is not None:
            assert chain["resolved"] == resolved
        result = self._check(d)
        assert "skipped_unusable_files" not in result["inheritance_chain"]
        assert "WARN" not in capsys.readouterr().err

    @pytest.mark.parametrize("case", sorted(_WRONG_TYPE))
    def test_a_wrong_type_value_skips_its_file_instead_of_crashing(
            self, tmp_path, capsys, case):
        fname, body, reason, conn, slow = self._WRONG_TYPE[case]
        files = {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
            "tx.yaml": "tenants:\n  tx:\n    _profile: gold\n"
                       "    mysql_connections: 70\n",
            "_profiles.yaml": "profiles:\n  gold:\n    mysql_slow_queries: 60\n",
        }
        files[fname] = body
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        d = str(tmp_path)
        want = f"WARN: skip {fname}: {reason}"

        assert diagnose.lookup_tenant_profile("tx", d) == "gold"
        assert want in capsys.readouterr().err

        chain = diagnose.resolve_inheritance_chain("tx", d)
        capsys.readouterr()
        assert chain["skipped_unusable_files"] == [fname]
        assert chain["resolved"].get("mysql_connections") == conn
        assert chain["resolved"].get("mysql_slow_queries") == slow

        result = self._check(d)
        assert result["profile"] == "gold"
        assert result["inheritance_chain"]["skipped_unusable_files"] == [fname]
        lines = [ln for ln in capsys.readouterr().err.splitlines()
                 if want in ln]
        assert len(lines) == 1, lines

    def test_a_platform_file_dropped_for_its_type_takes_its_tenants_block(
            self, tmp_path, capsys):
        """The exporter drops the whole file, so its `tenants:` entry
        (here the profile assignment) is gone too."""
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n", encoding="utf-8")
        (tmp_path / "_platform.yaml").write_text(
            "optional_overrides: 5\ntenants:\n  tx:\n    _profile: gold\n",
            encoding="utf-8")
        (tmp_path / "tx.yaml").write_text(
            "tenants:\n  tx:\n    mysql_connections: 70\n", encoding="utf-8")
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) is None
        assert ("WARN: skip _platform.yaml: 'optional_overrides' must be a "
                "list, got int") in capsys.readouterr().err
