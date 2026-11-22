#!/usr/bin/env python3
"""pytest style tests for blind_spot_discovery.py — Blind Spot Discovery 測試套件。"""

import json
import os
import sys
import tempfile
from unittest import mock
from unittest.mock import patch

import pytest
import yaml


import blind_spot_discovery as bsd  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


# ── 1. DB Type Inference ────────────────────────────────────────────

class TestInferDbType:
    """Test _infer_kind_from_job。"""

    @pytest.mark.parametrize("job_name,expected", [
        ("mysql", "mariadb"),
        ("postgres", "postgresql"),
        ("redis", "redis"),
        ("mysql-exporter-prod", "mariadb"),
        ("my-kafka-cluster", "kafka"),
        ("kafka-exporter", "kafka"),
        ("pg-exporter", "postgresql"),
        ("MySQL", "mariadb"),
        ("PostgreSQL", "postgresql"),
        ("nginx", "unknown"),
        ("node-exporter", "unknown"),
        ("prometheus", "unknown"),
        ("process-exporter", "unknown"),
        ("thanos-sidecar", "unknown"),
    ], ids=["exact-mysql", "exact-postgres", "exact-redis",
            "segment-mysql", "segment-kafka", "segment-kafka2", "segment-pg",
            "case-MySQL", "case-PostgreSQL",
            "unknown-nginx", "unknown-node", "unknown-prometheus",
            "unknown-process", "unknown-thanos"])
    def test_infer_db_from_job(self, job_name, expected):
        """job 名稱正確推斷 DB 類型。"""
        assert bsd._infer_kind_from_job(job_name) == expected

    def test_job_vocabulary_is_within_the_declarable_kinds(self):
        """#4026、#4097：live 端 job 推得的 kind，必須是租戶 `_expected_exporters` 能宣告的
        kind（registry 帶 `presence:` 的 pack，生成進 `EXPECTED_EXPORTER_KINDS`，與 schema
        `definitions.exporterKind.enum` 同一組）。差一個字，那一類不是永遠 covered 就是
        永遠 blind_spot。不是每一種 kind 都能從 job 名稱看出來（jvm），所以是子集。"""
        import pathlib
        repo = pathlib.Path(bsd.__file__).resolve().parents[3]
        schema = json.loads((repo / "docs/schemas/tenant-config.schema.json")
                            .read_text(encoding="utf-8"))
        kinds = set(bsd.EXPECTED_EXPORTER_KINDS)
        assert kinds == set(schema["definitions"]["exporterKind"]["enum"])
        assert set(bsd.JOB_DB_MAP.values()) and set(bsd.JOB_DB_MAP.values()) <= kinds
        assert {"nginx", "jvm"} <= kinds


# ── 2. Extract DB Instances ─────────────────────────────────────────

class TestExtractDbInstances:
    """Test extract_db_instances()。"""

    def test_basic_extraction(self):
        """基本提取 DB 實例。"""
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "redis", "instance": "10.0.0.2:9121", "namespace": "db-a", "labels": {}},
            {"job": "mysql", "instance": "10.0.0.3:9104", "namespace": "db-b", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        assert len(result["mariadb"]) == 2
        assert len(result["redis"]) == 1

    def test_exclude_jobs(self):
        """排除特定 job。"""
        targets = [
            {"job": "mysql", "instance": "10.0.0.1:9104", "namespace": "db-a", "labels": {}},
            {"job": "node-exporter", "instance": "10.0.0.2:9100", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets, exclude_jobs=["node-exporter"])
        assert "mariadb" in result
        assert "unknown" not in result

    def test_empty_targets(self):
        """空 targets 列表返回空字典。"""
        result = bsd.extract_db_instances([])
        assert result == {}

    def test_no_namespace_uses_instance_only(self):
        """無 namespace 時僅用 instance。"""
        targets = [
            {"job": "redis", "instance": "redis:6379", "namespace": "", "labels": {}},
        ]
        result = bsd.extract_db_instances(targets)
        assert "redis:6379" in result["redis"]


# ── 3. Load Declared Kinds ──────────────────────────────────────

@pytest.mark.usefixtures("da_guard_env")
class TestLoadDeclaredKinds:
    """Test load_declared_kinds()。

    #4026、#4097：已監控＝租戶 `_expected_exporters` 宣告的 exporter kind，照 exporter 的讀法
    （da-guard served-values --config-facts）——也就是 exporter 送出
    `tenant_expected_exporter{tenant,pack}` 的那份宣告（#869、#2737）。
    /metrics 送了哪些閾值鍵不算：內建層的鍵每個租戶都有列（#3832）。
    """

    @staticmethod
    def _write(d, files):
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)

    _DEFAULTS = "defaults:\n  mysql_connections: 80\n  redis_memory: 1024\n"

    def test_flat_file_is_not_a_tenant_and_is_named(self, capsys):
        """平面格式（無 `tenants:` 包裝）：exporter 不當租戶，不算已監控，並具名 WARN（R3）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "mysql_connections: 50\n_expected_exporters: [mariadb]\n"})
            result = bsd.load_declared_kinds(d)
            assert result == {}
            err = capsys.readouterr().err
            assert "WARN: db-a.yaml: declares no tenant" in err

    def test_declared_kinds_are_monitored(self):
        """宣告一個或一串 kind：每一類都算這個租戶已監控。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    _expected_exporters: [mariadb]\n",
                            "db-b.yaml": "tenants:\n  db-b:\n    _expected_exporters: [postgresql, redis]\n"})
            assert bsd.load_declared_kinds(d) == {
                "mariadb": {"db-a"}, "postgresql": {"db-b"}, "redis": {"db-b"}}

    def test_a_kind_declared_per_cluster_is_monitored_by_kind(self):
        """#4097：條目可以是 `kind@cluster`。這支工具比的是 kind：`@` 前面那一段，
        同一個 kind 宣告兩個叢集仍是一個 kind、一個租戶。混寫（同一個 kind 既單獨寫又寫
        `@cluster`）exporter 不讀，等於什麼都沒宣告。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n"
                                         "    _expected_exporters: [mariadb@east, mariadb@west, redis]\n",
                            "db-b.yaml": "tenants:\n  db-b:\n    _expected_exporters: [kafka@east]\n",
                            "db-c.yaml": "tenants:\n  db-c:\n"
                                         "    _expected_exporters: [nginx, nginx@east]\n"})
            declared = bsd.load_declared_kinds(d)
            assert declared == {"mariadb": {"db-a"}, "redis": {"db-a"}, "kafka": {"db-b"}}
            # … and a kind declared only per cluster is covered, not an unknown declaration.
            spots = bsd.find_blind_spots({"mariadb": {"10.0.0.1:9104"}}, declared)
            assert {s["kind"]: s["status"] for s in spots} == {
                "mariadb": "covered", "redis": "covered", "kafka": "covered"}

    def test_served_keys_without_a_declaration_are_not_monitored(self):
        """#4026 行為變更：租戶送出 mysql_／redis_ 的閾值鍵（自己寫的、繼承的、內建層的），
        沒宣告 `_expected_exporters` 就不算已監控。改寫前 db-a 算 mariadb＋redis，以及內建層推得出的
        每一類（樹裡只要有一個租戶，那幾類永遠不會是盲點）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\n"
                                         "    redis_memory: 512\n"})
            assert bsd.load_declared_kinds(d) == {}

    def test_a_disabled_key_does_not_withdraw_the_declaration(self):
        """宣告不看閾值鍵：db-a 把 redis 的鍵全關了，仍宣告 redis，仍算已監控
        （exporter 照樣送 tenant_expected_exporter）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    redis_memory: disable\n"
                                         "    _expected_exporters: [redis]\n"})
            assert bsd.load_declared_kinds(d) == {"redis": {"db-a"}}

    def test_the_declaration_is_read_through_the_platform_layer(self):
        """根平台檔的 `tenants.<id>` 是一層（#2370）：db-a 只在那裡宣告也算；
        db-b 的租戶檔整值換掉平台層的宣告。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS
                            + "tenants:\n  db-a:\n    _expected_exporters: [oracle]\n"
                            + "  db-b:\n    _expected_exporters: [oracle, db2]\n",
                            "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 50\n",
                            "db-b.yaml": "tenants:\n  db-b:\n    _expected_exporters: [kafka]\n"})
            assert bsd.load_declared_kinds(d) == {"oracle": {"db-a"}, "kafka": {"db-b"}}

    @pytest.mark.parametrize("at", ["2026-11-13T01:15:00Z", "2026-11-13T12:00:00Z"])
    def test_no_instant_enters_the_answer(self, tmp_path, monkeypatch, at):
        """#4026 的重現：mariadb 的鍵在 01:00-03:00 全部排程 `disable`，另一個鍵在
        02:00-03:00 與 `_critical` 撞成同一條 series（那段 /metrics 收集不了）。
        改寫前 01:15 判成沒監控 mariadb、12:00 判成有；收集不了的時段 rc 2。
        現在任何時刻都是同一個答案，也不用收集 /metrics。"""
        from factories import da_guard_at
        window = ('{default: "70", overrides: [{window: "01:00-03:00", value: disable}]}')
        d = tmp_path / "conf.d"
        d.mkdir()
        self._write(str(d), {
            "_defaults.yaml": self._DEFAULTS,
            "ta.yaml": "tenants:\n  ta:\n    _expected_exporters: [mariadb]\n"
                       + "".join(f"    {k}: {window}\n" for k in (
                           "mysql_connections", "mysql_connection_usage_percent",
                           "mysql_replication_lag"))
                       + '    redis_memory: {default: "70", overrides: '
                         '[{window: "02:00-03:00", value: "95:critical"}]}\n'
                       + '    redis_memory_critical: "95"\n'})
        monkeypatch.setenv("DA_GUARD_BINARY", da_guard_at(tmp_path, at))
        assert bsd.load_declared_kinds(str(d)) == {"mariadb": {"ta"}}

    def test_the_removed_metadata_db_type_declares_nothing(self):
        """#4097：`_metadata.db_type` 已移除、沒有別名——還這樣寫的租戶不算宣告了任何 kind
        （exporter 不送 tenant_expected_exporter，TenantExporterAbsent 也不管它）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    _metadata: {db_type: mariadb}\n"})
            assert bsd.load_declared_kinds(d) == {}

    def test_nginx_and_jvm_can_be_declared(self):
        """#4097：nginx、jvm 是可宣告的 kind（v2.9 的 db_type 沒有這兩個值）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    _expected_exporters: [nginx, jvm]\n"})
            assert bsd.load_declared_kinds(d) == {"nginx": {"db-a"}, "jvm": {"db-a"}}

    def test_kinds_that_are_not_a_list_of_strings_are_exit_2_named(self, monkeypatch):
        """da-guard 送的 expected_exporters 一律是字串清單；不是時不猜（字串會被逐字拆開），
        rc 2 並點名租戶。"""
        import _lib_tenant_values as tv
        real = tv.load_served_facts

        def fake(conf_d, *a, **kw):
            facts = real(conf_d, *a, **kw)
            t = facts.tenants["db-a"]
            facts.tenants["db-a"] = t._replace(expected_exporters="mariadb")
            return facts
        monkeypatch.setattr(bsd, "load_served_facts", fake)
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"_defaults.yaml": self._DEFAULTS,
                            "db-a.yaml": "tenants:\n  db-a:\n    _expected_exporters: [mariadb]\n"})
            with pytest.raises(tv.ServedValuesError, match="tenant 'db-a' carries expected_exporters "
                                                           "that are not a list of strings"):
                bsd.load_declared_kinds(d)

    @pytest.mark.parametrize("name, reason", [("nope", "dir_not_found"), ("f", "dir_not_found")])
    def test_a_config_dir_that_is_not_a_directory_is_a_caller_error(self, tmp_path, name,
                                                                    reason, cli_argv):
        """#3392：不存在或不是目錄的 --config-dir 是呼叫端錯誤（rc 2，具名），
        不再是 WARN 加空字典（每個活著的 DB 都被報成 blind spot、rc 0）。
        #3751：由 `main` 的宣告輸入判定一次（`declared_trees`），不再由
        `load_declared_kinds` 自己判，所以經 CLI 量。"""
        from _lib_exitcodes import CallerError
        (tmp_path / "f").write_text("x", encoding="utf-8")
        cli_argv("blind_spot_discovery", "--config-dir", str(tmp_path / name))
        with pytest.raises(CallerError) as exc:
            bsd.main()
        assert exc.value.reason == reason
        assert str(exc.value).startswith(f"--config-dir {tmp_path / name}: ")


# ── 4. Find Blind Spots ─────────────────────────────────────────────

class TestFindBlindSpots:
    """Test find_blind_spots()。"""

    def test_all_covered(self):
        """所有 DB 都有監控。"""
        live = {"mariadb": {"db-a/10.0.0.1:9104"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        assert len(results) == 1
        assert results[0]["status"] == "covered"

    def test_blind_spot_detected(self):
        """偵測到盲點。"""
        live = {"mariadb": {"db-a/10.0.0.1:9104"}, "postgresql": {"db-b/10.0.0.2:5432"}}
        monitored = {"mariadb": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        statuses = {r["kind"]: r["status"] for r in results}
        assert statuses["mariadb"] == "covered"
        assert statuses["postgresql"] == "blind_spot"

    def test_monitored_but_not_live(self):
        """Tenant 有設定但無實況 target — 仍標記為 covered。"""
        live = {}
        monitored = {"redis": {"db-a"}}
        results = bsd.find_blind_spots(live, monitored)
        assert results[0]["kind"] == "redis"
        assert results[0]["live_count"] == 0
        assert results[0]["status"] == "covered"

    @pytest.mark.parametrize("declared", ["MySQL", "mysql", "unknown", "123"])
    def test_a_declaration_outside_the_kinds_is_never_covered(self, declared):
        """#4026 審查 F2：宣告不在 kind 詞彙表（`EXPECTED_EXPORTER_KINDS`）裡時，它沒有存在指標，
        TenantExporterAbsent 永遠解不掉——不算 covered，另列 unknown_declaration。
        `unknown` 也不能被 live 端的 unknown 吃掉。（#4097 起 exporter 自己拒收這種值，
        所以真實的 da-guard 不會送出來；這裡釘的是工具與 da-guard 版本不同時的行為。）"""
        live = {"mariadb": {"db/m:9104"}, "unknown": {"ns/x:80"}}
        results = bsd.find_blind_spots(live, {declared: {"t1"}})
        by = {(r["kind"], r["status"]): r for r in results}
        assert by[(declared, "unknown_declaration")]["monitored_tenants"] == ["t1"]
        assert (declared, "covered") not in by
        assert by[("mariadb", "blind_spot")]["live_count"] == 1
        assert by[("unknown", "unrecognized")]["live_count"] == 1

    @pytest.mark.parametrize("declared", ["nginx", "jvm"])
    def test_a_kind_no_job_name_maps_to_is_still_covered(self, declared):
        """#4097：nginx、jvm 是 kind，但 `JOB_DB_MAP` 沒有對應的 job 名稱——宣告了就是
        covered（Absent 靠存在指標判斷，不靠 job 名稱），不是 unknown_declaration。"""
        results = bsd.find_blind_spots({}, {declared: {"t1"}})
        assert [(r["kind"], r["status"]) for r in results] == [(declared, "covered")]

    def test_the_report_lists_unknown_declarations_escaped(self):
        report = bsd.render_report(bsd.find_blind_spots(
            {"unknown": {"ns/\n[PASS] inst:80"}}, {"redis\n[PASS] x": {"t\n[PASS] y"}}))
        assert "UNKNOWN DECLARATIONS" in report
        assert "1 unknown declaration(s)" in report
        assert not [ln for ln in report.splitlines() if ln.startswith("[PASS]")], report

    def test_unknown_targets_separate(self):
        """未知 target 分開標記。"""
        live = {"unknown": {"ns/nginx:80"}}
        monitored = {}
        results = bsd.find_blind_spots(live, monitored)
        assert len(results) == 1
        assert results[0]["status"] == "unrecognized"

    def test_empty_inputs(self):
        """空輸入返回空列表。"""
        results = bsd.find_blind_spots({}, {})
        assert results == []


# ── 5. Report Rendering ──────────────────────────────────────────────

class TestRenderReport:
    """Test render_report()。"""

    def test_report_contains_blind_spot(self):
        """報告包含盲點標記。"""
        results = [{"kind": "postgresql", "live_count": 3,
                     "live_instances": ["a", "b", "c"],
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "blind_spot"}]
        report = bsd.render_report(results)
        assert "BLIND SPOTS" in report
        assert "postgresql" in report
        assert "3 instance(s)" in report

    def test_report_contains_covered(self):
        """報告包含已涵蓋標記。"""
        results = [{"kind": "mariadb", "live_count": 2,
                     "live_instances": ["x", "y"],
                     "monitored_tenants": ["db-a"], "monitored_count": 1,
                     "status": "covered"}]
        report = bsd.render_report(results)
        assert "COVERED" in report
        assert "db-a" in report

    def test_report_summary(self):
        """報告摘要統計正確。"""
        results = [
            {"kind": "mariadb", "live_count": 2, "live_instances": [],
             "monitored_tenants": ["t1"], "monitored_count": 1, "status": "covered"},
            {"kind": "redis", "live_count": 1, "live_instances": [],
             "monitored_tenants": [], "monitored_count": 0, "status": "blind_spot"},
        ]
        report = bsd.render_report(results)
        assert "1 kind(s) covered" in report
        assert "1 blind spot(s)" in report


# ── 6. CLI ───────────────────────────────────────────────────────────

class TestCLI:
    """Test CLI argument parsing。"""

    def test_required_args(self):
        """必要引數正確解析。"""
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp/conf.d"])
        assert args.config_dir == "/tmp/conf.d"

    def test_exclude_jobs(self):
        """--exclude-jobs 參數正確解析。"""
        parser = bsd.build_parser()
        args = parser.parse_args(["--config-dir", "/tmp", "--exclude-jobs", "a,b,c"])
        assert args.exclude_jobs == "a,b,c"

    def test_missing_config_dir(self):
        """缺失必要引數時退出。"""
        parser = bsd.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ── 7. query_prometheus_targets ───────────────────────────────────

class TestQueryPrometheusTargets:
    """query_prometheus_targets() 測試：經 lib 的 prometheus_targets 讀（#3133）。"""

    @patch("_lib_prometheus.http_get_json")
    def test_success(self, mock_http):
        """正常回傳 targets。"""
        mock_http.return_value = (
            {
                "status": "success",
                "data": {
                    "activeTargets": [
                        {"labels": {"job": "mysqld", "instance": "10.0.0.1:9104",
                                    "namespace": "db-a"}},
                        {"labels": {"job": "redis", "instance": "10.0.0.2:9121",
                                    "namespace": "db-b"}},
                    ]
                }
            },
            None,
        )
        result = bsd.query_prometheus_targets("http://prom:9090")
        assert len(result) == 2
        assert result[0]["job"] == "mysqld"
        assert result[1]["namespace"] == "db-b"

    @pytest.mark.parametrize("answer,message", [
        ((None, "connection refused"), "connection refused"),
        (({"status": "error", "error": "bad query"}, None), "bad query"),
        (({"status": "success", "data": {}}, None), "'data.activeTargets' is missing"),
    ], ids=["http-error", "error-status", "no-targets-key"])
    def test_not_answered_raises(self, answer, message):
        """#3133：沒得到回答不再回空列表（以前報告會把所有 DB 列為盲點、rc 0）。"""
        from _lib_prometheus import QueryError
        with patch("_lib_prometheus.http_get_json", return_value=answer):
            with pytest.raises(QueryError, match=message.replace("'", ".")):
                bsd.query_prometheus_targets("http://prom:9090")

    @patch("_lib_prometheus.http_get_json")
    def test_empty_targets(self, mock_http):
        """有回答但沒有 target 才是空列表。"""
        mock_http.return_value = (
            {"status": "success", "data": {"activeTargets": []}},
            None,
        )
        assert bsd.query_prometheus_targets("http://prom:9090") == []


# ── 8. render_report 進階分支 ─────────────────────────────────────

class TestRenderReportAdvanced:
    """render_report() 進階分支覆蓋。"""

    def test_blind_spot_more_than_5(self):
        """Blind spot 超過 5 個 instance 顯示 '... and N more'。"""
        instances = [f"inst-{i}" for i in range(8)]
        results = [{"kind": "redis", "live_count": 8,
                     "live_instances": instances,
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "blind_spot"}]
        report = bsd.render_report(results)
        assert "and 3 more" in report

    def test_unknown_status(self):
        """Unrecognized 狀態正確渲染。"""
        results = [{"kind": "unknown", "live_count": 2,
                     "live_instances": ["u1", "u2"],
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "unrecognized"}]
        report = bsd.render_report(results)
        assert "UNRECOGNIZED" in report

    def test_unknown_more_than_5(self):
        """Unrecognized 超過 5 個 instance 顯示 '... and N more'。"""
        instances = [f"unknown-{i}" for i in range(7)]
        results = [{"kind": "unknown", "live_count": 7,
                     "live_instances": instances,
                     "monitored_tenants": [], "monitored_count": 0,
                     "status": "unrecognized"}]
        report = bsd.render_report(results)
        assert "and 2 more" in report


# ── 9. main() 整合測試 ───────────────────────────────────────────

@pytest.mark.usefixtures("da_guard_env")
class TestMainIntegration:
    """main() CLI 整合測試。"""

    def test_text_output(self, monkeypatch, capsys, tmp_path, cli_argv):
        """main() 預設文字輸出。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        cli_argv("blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090")
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [
            {"job": "mysqld_exporter", "instance": "10.0.0.1:9104",
             "namespace": "db-a", "labels": {}},
        ])
        emit_result(bsd.main())
        out = capsys.readouterr().out
        assert "Summary" in out

    def test_json_output(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--json-output 輸出 JSON。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        cli_argv("blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090",
            "--json-output")
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [])
        # #3216: the --json status rides on the Report; nothing found is 0
        assert emit_result(bsd.main()) == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_exclude_jobs(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--exclude-jobs 正確排除 job。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "db-a.yaml").write_text(
            "mariadb:\n  connection_count:\n    warning: 100\n",
            encoding="utf-8",
        )
        cli_argv("blind_spot_discovery",
            "--config-dir", str(conf_dir),
            "--prometheus", "http://prom:9090",
            "--exclude-jobs", "prometheus,node-exporter")
        monkeypatch.setattr(bsd, "query_prometheus_targets", lambda url: [
            {"job": "prometheus", "instance": "localhost:9090",
             "namespace": "monitoring", "labels": {}},
        ])
        emit_result(bsd.main())
        out = capsys.readouterr().out
        assert "Summary" in out

    def test_prom_env_fallback(self, monkeypatch, capsys, tmp_path, cli_argv):
        """PROMETHEUS_URL env var 作為 fallback。"""
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "t1.yaml").write_text("redis:\n  x: {}\n",
                                          encoding="utf-8")
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        cli_argv("blind_spot_discovery", "--config-dir", str(conf_dir))
        captured_url = []
        def mock_targets(url):
            captured_url.append(url)
            return []
        monkeypatch.setattr(bsd, "query_prometheus_targets", mock_targets)
        emit_result(bsd.main())
        assert captured_url[0] == "http://env-prom:9090"

    def test_prom_empty_env_falls_back_to_localhost(self, monkeypatch, capsys,
                                                    tmp_path, cli_argv):
        """PROMETHEUS_URL 設定但為空字串（如 ConfigMap 缺鍵）→ 回退 localhost。

        回歸鎖：os.environ.get(key, default) 對 present-but-empty 會回空字串
        （key 存在），把空 URL 送進請求。
        """
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "t1.yaml").write_text("redis:\n  x: {}\n", encoding="utf-8")
        monkeypatch.setenv("PROMETHEUS_URL", "")
        cli_argv("blind_spot_discovery", "--config-dir", str(conf_dir))
        captured_url = []

        def mock_targets(url):
            captured_url.append(url)
            return []
        monkeypatch.setattr(bsd, "query_prometheus_targets", mock_targets)
        emit_result(bsd.main())
        assert captured_url[0] == "http://localhost:9090"
