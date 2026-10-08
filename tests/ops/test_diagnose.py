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
from _lib_tenant_values import ParseFailedError  # noqa: E402

# #2526: the chain is `da-guard effective`'s answer.
pytestmark = pytest.mark.usefixtures("da_guard_env")


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
    """Tests for lookup_tenant_profile(): the profile the exporter BINDS
    (`da-guard effective`'s `profile`, #2526), None for none."""

    @staticmethod
    def _tree(tmp_path, **files):
        (tmp_path / "_defaults.yaml").write_text("defaults:\n  cpu: 50\n", encoding="utf-8")
        for name, body in files.items():
            (tmp_path / name).write_text(body, encoding="utf-8")
        return str(tmp_path)

    def test_no_config_dir(self):
        assert diagnose.lookup_tenant_profile("db-a", None) is None

    def test_nonexistent_dir(self):
        assert diagnose.lookup_tenant_profile("db-a", "/nonexistent/path") is None

    def test_finds_profile_in_tenants_block(self, tmp_path):
        d = self._tree(tmp_path, **{
            "_profiles.yaml": "profiles:\n  high-load:\n    cpu: 95\n",
            "multi.yaml": "tenants:\n  db-a:\n    _profile: high-load\n    cpu: 90\n"})
        assert diagnose.lookup_tenant_profile("db-a", d) == "high-load"

    def test_a_file_without_tenants_declares_no_tenant(self, tmp_path, capsys):
        """The exporter reads a non-`_` file only through its `tenants:`
        mapping (the old flat reader took the file name as the tenant id)."""
        d = self._tree(tmp_path, **{
            "_profiles.yaml": "profiles:\n  low-load:\n    cpu: 10\n",
            "db-b.yaml": "_profile: low-load\nmem: 80\n"})
        assert diagnose.lookup_tenant_profile("db-b", d) is None
        err = capsys.readouterr().err
        assert "WARN: db-b.yaml: declares no tenant" in err, err
        assert "no tenant 'db-b'" in err, err

    def test_an_unknown_profile_binds_nothing_and_says_so(self, tmp_path, capsys):
        d = self._tree(tmp_path, **{
            "db-a.yaml": "tenants:\n  db-a:\n    _profile: nonexistent\n    mem: 80\n"})
        assert diagnose.lookup_tenant_profile("db-a", d) is None
        assert "_profile 'nonexistent' binds no profile" in capsys.readouterr().err

    def test_skips_hidden_files(self, tmp_path):
        d = self._tree(tmp_path, **{".hidden.yaml": "tenants:\n  .hidden:\n    _profile: x\n"})
        assert diagnose.lookup_tenant_profile(".hidden", d) is None

    def test_skips_non_yaml_files(self, tmp_path):
        d = self._tree(tmp_path, **{"readme.txt": "not yaml"})
        assert diagnose.lookup_tenant_profile("readme", d) is None

    @pytest.mark.parametrize("body", [": : : invalid", "- item1\n- item2\n"],
                             ids=["bad-syntax", "not-a-mapping"])
    def test_a_file_the_exporter_drops_fails_closed(self, tmp_path, body):
        """#2526: no answer from a partial read — the exporter drops the
        file, so the lookup raises (the CLI exits 2) instead of skipping it."""
        d = self._tree(tmp_path, **{"bad.yaml": body})
        with pytest.raises(ParseFailedError, match="bad.yaml"):
            diagnose.lookup_tenant_profile("bad", d)

    def test_tenant_not_found(self, tmp_path):
        d = self._tree(tmp_path, **{"multi.yaml": "tenants:\n  db-a:\n    cpu: 90\n"})
        assert diagnose.lookup_tenant_profile("db-z", d) is None

    def test_no_profile_key(self, tmp_path):
        d = self._tree(tmp_path, **{"db-a.yaml": "tenants:\n  db-a:\n    cpu: 90\n"})
        assert diagnose.lookup_tenant_profile("db-a", d) is None

    def test_skips_directories(self, tmp_path):
        (tmp_path / "subdir.yaml").mkdir()
        d = self._tree(tmp_path, **{"a.yaml": "tenants:\n  db-a:\n    cpu: 1\n"})
        assert diagnose.lookup_tenant_profile("subdir", d) is None


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
        (tmp_path / "db-a.yaml").write_text("tenants:\n  db-a: {}\n", encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result is not None
        assert result["profile_name"] is None
        assert result["resolved"]["cpu"] == 80
        assert result["chain"] == [{"layer": "defaults", "source": "_defaults.yaml",
                                    "keys": {"cpu": 80, "mem": 70}}]

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
            yaml.safe_dump({"tenants": {"db-a": {"_profile": "high-load", "cpu": 99}}}),
            encoding="utf-8",
        )

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["profile_name"] == "high-load"
        # cpu: tenant override wins (99), not profile (95) or default (80)
        assert result["resolved"]["cpu"] == 99
        # net: from profile (fill-in)
        assert result["resolved"]["net"] == "60"  # not declared at the root: /metrics serves no row, the value as written
        # mem, disk: from defaults
        assert result["resolved"]["mem"] == 70
        assert result["resolved"]["disk"] == 90

        # Chain has 3 layers, each listing the keys it supplies
        assert result["chain"] == [
            {"layer": "defaults", "source": "_defaults.yaml", "keys": {"disk": 90, "mem": 70}},
            {"layer": "profile", "source": "_profiles.yaml → high-load", "keys": {"net": "60"}},
            {"layer": "tenant", "source": "db-a.yaml", "keys": {"cpu": 99}},
        ]

    def test_tenant_in_multi_tenant_file(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 50}}),
            encoding="utf-8",
        )
        multi = {"tenants": {"db-a": {"cpu": 75}, "db-b": {"cpu": 60}}}
        (tmp_path / "tenants.yaml").write_text(yaml.safe_dump(multi), encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["resolved"]["cpu"] == 75
        assert result["chain"][-1]["source"] == "tenants.yaml"

    def test_invalid_defaults_fails_closed(self, tmp_path):
        """#2526: was a WARN and a chain without the defaults layer; the
        exporter drops the file, so the chain is not answered at all."""
        (tmp_path / "_defaults.yaml").write_text(": : bad", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("tenants:\n  db-a:\n    cpu: 50\n",
                                            encoding="utf-8")
        with pytest.raises(ParseFailedError, match="_defaults.yaml"):
            diagnose.resolve_inheritance_chain("db-a", str(tmp_path))

    def test_no_profiles_file(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            yaml.safe_dump({"defaults": {"cpu": 50}}), encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(
            yaml.safe_dump({"tenants": {"db-a": {"_profile": "nonexistent", "mem": 80}}}),
            encoding="utf-8")

        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["profile_name"] is None   # bound to none
        assert result["resolved"]["mem"] == "80"  # not declared at the root: /metrics serves no row, the value as written
        assert [c["layer"] for c in result["chain"]] == ["defaults", "tenant"]

    def test_a_tenant_below_the_root_is_found(self, tmp_path):
        """The exporter's tree is hierarchical; the old reader was flat."""
        (tmp_path / "_defaults.yaml").write_text("defaults:\n  cpu: 50\n  mem: 1\n",
                                                 encoding="utf-8")
        (tmp_path / "eu").mkdir()
        (tmp_path / "eu" / "_defaults.yaml").write_text("defaults:\n  cpu: 60\n",
                                                        encoding="utf-8")
        (tmp_path / "eu" / "db-a.yaml").write_text("tenants:\n  db-a:\n    mem: 2\n",
                                                   encoding="utf-8")
        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["resolved"] == {"cpu": 60, "mem": 2}
        assert result["chain"] == [
            {"layer": "defaults", "source": "eu/_defaults.yaml", "keys": {"cpu": 60}},
            {"layer": "tenant", "source": "eu/db-a.yaml", "keys": {"mem": 2}},
        ]

    def test_declared_is_the_root_optional_overrides(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  cpu: 50\noptional_overrides:\n  - oracle_x\n  - 123\n  - ~\n",
            encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("tenants:\n  db-a: {}\n", encoding="utf-8")
        result = diagnose.resolve_inheritance_chain("db-a", str(tmp_path))
        assert result["declared"] == ["oracle_x", "123"]
        assert "oracle_x" not in result["resolved"]


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
            yaml.safe_dump({"tenants": {"db-a": {"_profile": "prod", "cpu": 95}}}),
            encoding="utf-8")
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

    @pytest.fixture(autouse=True)
    def _profiles(self, tmp_path):
        # #2526: `profile_name` is the profile the exporter BINDS.
        (tmp_path / "_profiles.yaml").write_text(
            "profiles:\n  p1: {k: 1}\n  p2: {k: 2}\n  good: {k: 3}\n  bad: {k: 4}\n",
            encoding="utf-8")

    @pytest.mark.parametrize("fname", ["tx.yaml", "TX.yaml", "0tx.yaml"])
    def test_tenant_file_wins_whatever_its_name(self, tmp_path, fname):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        (tmp_path / fname).write_text(
            "tenants:\n  tx:\n    mysql_connections: '70'\n    _profile: p2\n",
            encoding="utf-8")
        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == 70  # served (#2526)
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "p2"

    def test_platform_value_kept_when_tenant_file_is_silent(self, tmp_path):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        (tmp_path / "tx.yaml").write_text("tenants:\n  tx: {}\n", encoding="utf-8")
        chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
        assert chain["resolved"]["mysql_connections"] == 60
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "p1"

    def test_platform_file_cannot_create_a_tenant(self, tmp_path, capsys):
        (tmp_path / "_defaults.yaml").write_text(self._PLATFORM, encoding="utf-8")
        # #2526: the exporter has no tenant tx, so there is no chain (the
        # old reader answered with the defaults alone).
        def _not_found():
            return [ln for ln in capsys.readouterr().err.splitlines()
                    if "no tenant 'tx'" in ln]

        assert diagnose.resolve_inheritance_chain("tx", str(tmp_path)) is None
        assert len(_not_found()) == 1
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) is None
        assert len(_not_found()) == 1

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
        assert chain["resolved"]["mysql_connections"] == 61
        assert diagnose.lookup_tenant_profile("tx", str(tmp_path)) == "good"


# ---------------------------------------------------------------------------
# #1522: lookup_tenant_profile and check() read conf.d through the chain
# ---------------------------------------------------------------------------

class TestProfileLookupSharesTheChainRead:
    """`lookup_tenant_profile` is a view over the chain, and `check()` reads
    the directory once. #2526: a file the exporter's load drops is no longer
    skipped with a WARN — the read fails closed (ParseFailedError; the CLI
    exits 2), so no answer is given from a partial tree."""

    @staticmethod
    def _confd(tmp_path, broken=False):
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n"
            "tenants:\n  tx:\n    _profile: gold\n", encoding="utf-8")
        (tmp_path / "tx.yaml").write_text(
            "tenants:\n  tx:\n    mysql_connections: 70\n", encoding="utf-8")
        (tmp_path / "_profiles.yaml").write_text(
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n", encoding="utf-8")
        if broken:
            # Another tenant's file that does not parse.
            (tmp_path / "broken.yaml").write_text(
                "tenants:\n  other:\n    _profile: [unclosed\n", encoding="utf-8")
        return str(tmp_path)

    def test_another_tenants_broken_file_fails_closed(self, tmp_path):
        d = self._confd(tmp_path, broken=True)
        with pytest.raises(ParseFailedError, match="broken.yaml"):
            diagnose.lookup_tenant_profile("tx", d)
        with pytest.raises(ParseFailedError, match="broken.yaml"):
            self._check(d)

    def test_answer_is_the_chains_profile_name(self, tmp_path):
        d = self._confd(tmp_path)
        chain = diagnose.resolve_inheritance_chain("tx", d)
        assert "skipped_unusable_files" not in chain
        assert diagnose.lookup_tenant_profile("tx", d) == chain["profile_name"] == "gold"

    @staticmethod
    def _check(d):
        with mock.patch.object(diagnose, "tenant_db_type",
                               return_value=(None, None)), \
                mock.patch.object(diagnose, "query_prometheus",
                                  return_value=([], None)):
            return diagnose.check("tx", "http://prom:9090",
                                  config_dir=d, out=io.StringIO())

    def test_check_resolves_the_chain_once(self, tmp_path):
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
        assert "skipped_unusable_files" not in result["inheritance_chain"]

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
        # `!!set` is a mapping node to the exporter (and to diagnose's
        # loaders): where a string entry is wanted, the file fails like
        # `[{a: 1}]`.
        "optional-overrides-entry-a-set": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\noptional_overrides: [!!set {a}]\n",
            "'optional_overrides' entries must be scalars, got dict", 70, 60),
        "optional-overrides-entry-an-empty-set": (
            "_defaults.yaml",
            "defaults:\n  mysql_connections: 80\noptional_overrides: [!!set {}]\n",
            "'optional_overrides' entries must be scalars, got dict", 70, 60),
        "optional-overrides-entry-a-set-in-profiles-file": (
            "_profiles.yaml",
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n"
            "optional_overrides: [!!set {a}]\n",
            "'optional_overrides' entries must be scalars, got dict", 70, None),
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
        # `!!set` in a mapping position: to the exporter a mapping (here
        # with every value null), so it loads.
        "defaults-a-set": (
            "_defaults.yaml",
            "defaults: !!set {mysql_connections, mysql_slow_queries}\n", {
                # every root default null: no row is served, so the values
                # are the ones written (served-values' `unserved`)
                "mysql_connections": "70", "mysql_slow_queries": "60"}),
        "profiles-a-set": (
            "_profiles.yaml", "profiles: !!set {gold}\n", {
                "mysql_connections": 70, "mysql_slow_queries": 90}),
        "profile-body-a-set": (
            "_profiles.yaml", "profiles:\n  gold: !!set {mysql_slow_queries}\n", {
                "mysql_connections": 70, "mysql_slow_queries": 90}),
        "sibling-profile-body-a-set": (
            "_profiles.yaml",
            "profiles:\n  gold:\n    mysql_slow_queries: 60\n  bad: !!set {k}\n", {
                "mysql_connections": 70, "mysql_slow_queries": 60}),
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
    def test_a_wrong_type_value_fails_closed(self, tmp_path, case):
        """#2526: was a WARN and a chain without that file; the exporter
        drops the file, so neither reader answers."""
        fname, body, _reason, _conn, _slow = self._WRONG_TYPE[case]
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
        with pytest.raises(ParseFailedError, match=fname):
            diagnose.lookup_tenant_profile("tx", d)
        with pytest.raises(ParseFailedError, match=fname):
            self._check(d)

    def test_a_platform_file_dropped_for_its_type_fails_closed(self, tmp_path):
        """The exporter drops the whole file, `tenants:` entry and all."""
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 80\n", encoding="utf-8")
        (tmp_path / "_platform.yaml").write_text(
            "optional_overrides: 5\ntenants:\n  tx:\n    _profile: gold\n",
            encoding="utf-8")
        (tmp_path / "tx.yaml").write_text(
            "tenants:\n  tx:\n    mysql_connections: 70\n", encoding="utf-8")
        with pytest.raises(ParseFailedError, match="_platform.yaml"):
            diagnose.lookup_tenant_profile("tx", str(tmp_path))


class TestSetTagReadAsTheExporterReadsIt:
    """#1522: yaml.v3 decodes a `!!set`-tagged node by its kind, so in a
    mapping position it is that mapping, values kept. PyYAML's own `!!set`
    builds a Python set and drops the values; diagnose's loaders do not.

    Each case is written twice — with `!!set` and as the plain mapping
    (the control, which must already give the right answer) — and both
    must resolve to what /metrics serves (measured against LoadDir +
    ResolveAt on the same tree)."""

    _DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 90\n"
    _TX = "tenants:\n  tx:\n    _profile: gold\n    mysql_connections: 70\n"
    _PROF = "profiles:\n  gold:\n    mysql_slow_queries: 60\n"

    # case: (files with {TAG} where the set/plain tag goes, profile, /metrics)
    _CASES = {
        "profile-body-flow": (
            {"_profiles.yaml": "profiles:\n  gold: {TAG}{mysql_slow_queries: 55}\n"},
            "gold", {"mysql_connections": 70, "mysql_slow_queries": 55}),
        "profile-body-block": (
            {"_profiles.yaml": "profiles:\n  gold: {TAG}\n    mysql_slow_queries: 55\n"},
            "gold", {"mysql_connections": 70, "mysql_slow_queries": 55}),
        "profiles": (
            {"_profiles.yaml": "profiles: {TAG}{gold: {mysql_slow_queries: 55}}\n"},
            "gold", {"mysql_connections": 70, "mysql_slow_queries": 55}),
        "defaults": (
            {"_defaults.yaml":
                "defaults: {TAG}{mysql_connections: 5, mysql_slow_queries: 6}\n",
             "tx.yaml": "tenants:\n  tx:\n    mysql_connections: 70\n",
             "_profiles.yaml": None},
            None, {"mysql_connections": 70, "mysql_slow_queries": 6}),
        "tenant-block": (
            {"tx.yaml": "tenants: {tx: {TAG}{mysql_connections: 71, "
                        "_profile: gold}}\n"},
            "gold", {"mysql_connections": 71, "mysql_slow_queries": 60}),
    }

    def _tree(self, tmp_path, overrides, tag):
        files = {"_defaults.yaml": self._DEFAULTS, "tx.yaml": self._TX,
                 "_profiles.yaml": self._PROF}
        files.update(overrides)
        for name, text in files.items():
            if text is not None:
                (tmp_path / name).write_text(text.replace("{TAG}", tag),
                                             encoding="utf-8")
        return str(tmp_path)

    @pytest.mark.parametrize("tag", ["!!set ", ""], ids=["set", "plain"])
    @pytest.mark.parametrize("case", sorted(_CASES))
    def test_a_set_in_a_mapping_position_keeps_its_values(
            self, tmp_path, capsys, case, tag):
        overrides, profile, served = self._CASES[case]
        d = self._tree(tmp_path, overrides, tag)
        chain = diagnose.resolve_inheritance_chain("tx", d)
        assert "skipped_unusable_files" not in chain
        assert chain["profile_name"] == profile
        assert chain["resolved"] == served
        assert diagnose.lookup_tenant_profile("tx", d) == profile
        assert "WARN" not in capsys.readouterr().err

    def test_a_tenants_set_names_tenants_with_no_keys(self, tmp_path, capsys):
        """`tenants: !!set {tx}` is `tenants: {tx: ~}` on /metrics: the
        tenant exists with nothing of its own (80/90 = the defaults)."""
        d = self._tree(tmp_path, {"tx.yaml": "tenants: !!set {tx}\n"}, "")
        chain = diagnose.resolve_inheritance_chain("tx", d)
        assert "skipped_unusable_files" not in chain
        assert chain["resolved"] == {"mysql_connections": 80,
                                     "mysql_slow_queries": 90}

    def test_a_repeated_key_in_a_set_fails_closed(self, tmp_path):
        d = self._tree(tmp_path, {
            "_profiles.yaml":
                "profiles:\n  gold: !!set {mysql_slow_queries: 55, "
                "mysql_slow_queries: 56}\n"}, "")
        with pytest.raises(ParseFailedError, match="_profiles.yaml"):
            diagnose.resolve_inheritance_chain("tx", d)

    @pytest.mark.parametrize("body", [
        "defaults: !!set {1, mysql_connections}\n",
        "defaults: {1: 5, mysql_connections: 80}\n"], ids=["set", "plain"])
    def test_a_non_string_defaults_key_does_not_end_the_run(
            self, tmp_path, capsys, body):
        """/effective keys it by its text, `"1"` (#2547); must not raise."""
        d = self._tree(tmp_path, {
            "_defaults.yaml": body,
            "tx.yaml": "tenants:\n  tx:\n    mysql_connections: 70\n",
            "_profiles.yaml": None}, "")
        chain = diagnose.resolve_inheritance_chain("tx", d)
        # `!!set {1, mysql_connections}` gives the root default no value, so
        # /metrics serves no row and the value is the one written.
        assert chain["resolved"]["mysql_connections"] == ("70" if "!!set" in body else 70)
        assert 1 not in chain["resolved"]
        with mock.patch.object(diagnose, "tenant_db_type",
                               return_value=(None, None)), \
                mock.patch.object(diagnose, "query_prometheus",
                                  return_value=([], None)):
            result = diagnose.check("tx", "http://prom:9090", config_dir=d,
                                    out=io.StringIO())
        assert result["status"] == "unchecked"
