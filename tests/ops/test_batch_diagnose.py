#!/usr/bin/env python3
"""test_batch_diagnose.py — batch_diagnose.py pytest 風格測試。

驗證:
  1. discover_tenants() — ConfigMap key 解析
  2. run_diagnose_for_tenant() — 單租戶診斷執行
  3. generate_report() — 報告產出 + health score
  4. print_text_report() — 文字報告格式
"""

import json
from pathlib import Path
import os
import subprocess
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

import _lib_kubectl  # noqa: E402
import batch_diagnose as bd  # noqa: E402


_REAL_RUN = subprocess.run


def _kubectl_only(fake):
    """A `subprocess.run` that hands every `kubectl` call to *fake* and runs
    everything else for real: `discover_tenants` classes the ConfigMap keys
    through `da-guard parse-tree --names` (#3206), a subprocess too, and a
    fake that answered it as well would be the ConfigMap's JSON read as
    da-guard's document."""
    def run(cmd, *args, **kwargs):
        if cmd and os.path.basename(str(cmd[0])) == "kubectl":
            return fake(cmd, *args, **kwargs)
        return _REAL_RUN(cmd, *args, **kwargs)
    return run


@pytest.fixture
def mock_run(monkeypatch, da_guard_env):
    """The `kubectl` that `discover_tenants` runs (a MagicMock: set its
    `return_value` / `side_effect`); da-guard is the real one. #3349: the
    read goes through `_lib_kubectl.kubectl_get`, so this is that runner's
    `subprocess.run` — the process boundary, with kubectl_get's own reading
    of rc / stdout / stderr under test too."""
    fake = MagicMock()
    monkeypatch.setattr(_lib_kubectl.subprocess, "run", _kubectl_only(fake))
    return fake


def _fake_kubectl(tmp_path, rc, stdout="", stderr=""):
    """A real `kubectl` executable that prints *stdout* / *stderr* and exits
    *rc*; returns a PATH with it first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "kubectl"
    exe.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({rc})\n", encoding="utf-8")
    exe.chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ['PATH']}"


def _batch_cli(tmp_path, path, *args):
    return subprocess.run(
        [sys.executable, str(Path(bd.__file__)), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path),
        env={**os.environ, "PATH": path, "PROMETHEUS_URL": ""})


class TestDiscoverTenants:
    """discover_tenants() 測試。"""

    def test_normal_discovery(self, mock_run):
        """正常 ConfigMap 應返回排序的 tenant 列表。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "db-b.yaml": "tenants: {}",
                "db-a.yaml": "tenants: {}",
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == ["db-a", "db-b"]
        cmd = mock_run.call_args.args[0]
        assert cmd == ["kubectl", "get", "configmap", "threshold-config", "-n", "monitoring",
                       "--ignore-not-found", "-o", "json"]

    def test_skip_underscore_keys(self, mock_run):
        """_ 前綴的 key 應被忽略。"""
        cm_data = {
            "data": {
                "_defaults.yaml": "defaults: {}",
                "_internal.yaml": "stuff",
                "db-a.yaml": "tenants: {}",
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == ["db-a"]

    def test_kubectl_failure(self, mock_run):
        """kubectl 失敗是 KubectlError（rc 2，點名 kubectl 自己那行話）。
        #3349：以前返回空列表，main 再報成 `No tenants found`。"""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="",
            stderr='Error from server (Forbidden): configmaps "threshold-config" is forbidden')
        with pytest.raises(bd.KubectlError, match=r"\(exit 1\).*Forbidden"):
            bd.discover_tenants()

    def test_kubectl_timeout(self, mock_run):
        """kubectl timeout 是 KubectlError（#3349：以前返回空列表）。"""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
        with pytest.raises(bd.KubectlError, match="no answer within 15s"):
            bd.discover_tenants()

    def test_missing_configmap_is_named(self, mock_run):
        """ConfigMap 不存在（`--ignore-not-found`：rc 0、stdout 空）→ KubectlError
        點名那個 ConfigMap 與 namespace，不是 `No tenants found`（#3349）。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(bd.KubectlError, match="ConfigMap 'threshold-config' not found "
                                                  "in namespace 'monitoring'"):
            bd.discover_tenants()

    def test_empty_configmap(self, mock_run):
        """空 ConfigMap 應返回空列表。"""
        cm_data = {"data": {}}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data),
        )
        result = bd.discover_tenants()
        assert result == []


class TestGenerateReport:
    """generate_report() 測試。"""

    def test_all_healthy(self):
        """全部健康時 health_score = 1.0。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "healthy"},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert report["health_score"] == 1.0
        assert report["healthy_count"] == 2
        assert report["issue_count"] == 0

    def test_partial_health(self):
        """部分健康時 health_score 正確計算。"""
        results = [
            {"tenant": "db-a", "status": "healthy"},
            {"tenant": "db-b", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert report["health_score"] == 0.5
        assert report["healthy_count"] == 1
        assert report["issue_count"] == 1

    def test_recommendations_pod_issue(self):
        """Pod 相關 issue 應產生 kubectl get pods 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Pod not found"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert any("kubectl get pods" in r for r in report["recommendations"])

    def test_recommendations_exporter_issue(self):
        """Exporter 相關 issue 應產生 kubectl logs 建議。"""
        results = [
            {"tenant": "db-a", "status": "error", "issues": ["Exporter DOWN"]},
        ]
        report = bd.generate_report(results, "http://localhost:9090")
        assert any("kubectl logs" in r for r in report["recommendations"])

    def test_empty_results(self):
        """空結果應返回 0 health_score。"""
        report = bd.generate_report([], "http://localhost:9090")
        assert report["health_score"] == 0.0
        assert report["total_tenants"] == 0

    def test_report_has_timestamp(self):
        """報告應包含 timestamp。"""
        results = [{"tenant": "db-a", "status": "healthy"}]
        report = bd.generate_report(results, "http://localhost:9090")
        assert "timestamp" in report


class TestPrintTextReport:
    """print_text_report() 格式驗證。"""

    def test_no_crash_on_healthy(self):
        """全部健康的報告不應崩潰。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy", "operational_mode": "normal",
              "elapsed_seconds": 1.2}],
            "http://localhost:9090",
        )
        # Should not raise
        bd.print_text_report(report)

    def test_healthy_section(self, capsys):
        """健康 tenant 區塊正確顯示。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy", "operational_mode": "normal",
              "elapsed_seconds": 1.2}],
            "http://localhost:9090",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Health Score" in out
        assert "db-a" in out
        assert "100%" in out

    def test_issues_section(self, capsys):
        """問題 tenant 區塊顯示 issue 清單。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "error", "issues": ["Pod missing"]}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Issues" in out
        assert "Pod missing" in out

    def test_recommendations_section(self, capsys):
        """補救步驟區塊正確顯示。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "error", "issues": ["Prometheus timeout"]}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "Remediation" in out

    def test_silent_mode_suffix(self, capsys):
        """非 normal 模式顯示 [mode] 後綴。"""
        report = bd.generate_report(
            [{"tenant": "db-a", "status": "healthy",
              "operational_mode": "maintenance", "elapsed_seconds": 0.5}],
            "http://prom",
        )
        bd.print_text_report(report)
        out = capsys.readouterr().out
        assert "[maintenance]" in out


# ── generate_report recommendations 分支覆蓋 ─────────────────────


class TestRecommendations:
    """generate_report() recommendations 分支完整覆蓋。"""

    def test_prometheus_issue(self):
        """Prometheus 相關 issue 建議連線檢查。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Prometheus query timeout"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("Prometheus" in r for r in report["recommendations"])

    def test_down_issue(self):
        """DOWN 狀態 issue 建議查看 exporter logs。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Service DOWN"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("kubectl logs" in r for r in report["recommendations"])

    def test_generic_issue(self):
        """未分類 issue 直接顯示原文。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["some random error"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert any("some random error" in r for r in report["recommendations"])

    def test_multiple_issues(self):
        """多個 issue 產生多條建議。"""
        results = [
            {"tenant": "db-a", "status": "error",
             "issues": ["Pod not ready", "Exporter DOWN"]},
        ]
        report = bd.generate_report(results, "http://prom")
        assert len(report["recommendations"]) == 2


# ── run_diagnose_for_tenant（mock diagnose_check）─────────────────


class TestRunDiagnoseForTenant:
    """run_diagnose_for_tenant() 單租戶診斷。"""

    def test_healthy_result(self, monkeypatch):
        """正常診斷回傳 JSON 結果。"""
        def mock_check(tenant, prom_url, *, out):
            out.write(json.dumps({"tenant": tenant, "status": "healthy"}))
        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["tenant"] == "db-a"
        assert result["status"] == "healthy"
        assert "elapsed_seconds" in result

    def test_empty_output(self, monkeypatch):
        """空輸出回傳 error 狀態。"""
        monkeypatch.setattr(bd, "diagnose_check", lambda t, p, *, out: None)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["status"] == "error"
        assert "empty output" in result["issues"][0]

    def test_exception_caught(self, monkeypatch):
        """例外回傳 error 狀態。"""
        def mock_check(tenant, prom_url, *, out):
            raise OSError("connection refused")
        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["status"] == "error"
        assert "connection refused" in result["issues"][0]

    def test_the_worker_never_rebinds_process_global_stdout(self, monkeypatch):
        """⛔ 這一格守的是機制，不是時序。

        舊寫法用 `contextlib.redirect_stdout(buf)` 抓 check() 的輸出。它換掉的是
        **行程全域**的 `sys.stdout`，而本函式跑在 ThreadPoolExecutor 的 worker
        裡：兩條執行緒交錯成「A 進 → B 進 → A 出 → B 出」時，B 存下的「舊值」
        已經是 A 的 buffer，B 離開時就把真 stdout 永久換成它 —— 之後 main() 印
        的 JSON 報告全進了那個 StringIO，行程 rc 0、stdout 全空、stderr 全空。

        ⚠️ 用「跑很多次看會不會空」來守這件事是錯的守法：那是在賭時序，紅綠都
        不可靠。這裡改成對著**唯一的成因**斷言 —— worker 執行期間 `sys.stdout`
        必須還是原本那一個物件。單執行緒、完全確定，舊寫法必紅。
        """
        seen = []

        def mock_check(tenant, prom_url, *, out):
            seen.append(sys.stdout)
            out.write(json.dumps({"tenant": tenant, "status": "healthy"}))

        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        before = sys.stdout
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")

        assert result["status"] == "healthy"
        assert seen == [before], (
            "check() 執行期間 sys.stdout 被換掉了 —— 有人又把行程全域當成捕捉"
            "輸出的手段；平行 worker 下這會讓真 stdout 一去不回"
        )
        assert sys.stdout is before


# ── discover_tenants JSON decode ──────────────────────────────────


class TestDiscoverTenantsEdge:
    """discover_tenants() 邊際案例。"""

    def test_invalid_json(self, mock_run):
        """無效 JSON 是 KubectlError（#3349：以前回傳空清單）。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        with pytest.raises(bd.KubectlError, match="not JSON"):
            bd.discover_tenants()

    def test_non_yaml_keys_ignored(self, mock_run):
        """非 .yaml 結尾的 key 被忽略。"""
        cm_data = {"data": {"readme.txt": "info", "db-a.yaml": "tenants: {}"}}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(cm_data))
        result = bd.discover_tenants()
        assert result == ["db-a"]


# ── main() CLI 測試 ─────────────────────────────────────────────


class TestMainCLI:
    """main() CLI 整合測試。"""

    def test_tenants_flag(self, monkeypatch, capsys, cli_argv):
        """--tenants 直接指定 tenant 清單。

        #1112: 人類可讀的 dry-run 清單走 stderr（stdout 保留給 --json 的
        單一 JSON 文件），故斷言 stderr。
        """
        cli_argv("batch_diagnose", "--tenants", "db-a,db-b",
            "--prometheus", "http://prom:9090", "--dry-run")
        bd.main()
        err = capsys.readouterr().err
        assert "db-a" in err
        assert "db-b" in err
        assert "2 tenants" in err

    def test_dry_run_single(self, monkeypatch, capsys, cli_argv):
        """--dry-run 列出 tenant 但不執行檢查（清單走 stderr，#1112）。"""
        cli_argv("batch_diagnose", "--tenants", "db-a", "--dry-run")
        bd.main()
        err = capsys.readouterr().err
        assert "db-a" in err
        assert "dry-run" in err.lower() or "without --dry-run" in err

    def test_dry_run_json_envelope(self, monkeypatch, capsys, cli_argv):
        """#1112: `--dry-run --json` 的 envelope 形狀（不只「能 parse」）。

        釘住三件事，任一被改壞就紅：
        1. discriminator 是 `status="dry_run"` / `reason="health_checks_skipped"`
           的**確切值**——subprocess gate 只做 json.loads，把 dry_run 改成
           dryrun 它照樣綠。
        2. **未測量 ≠ 0**：`healthy_count` / `issue_count` / `health_score`
           必須是 **None**，不是 0。若寫成 0，consumer 讀到 health_score=0
           會以為「全部不健康」，而真相是「根本沒檢查」。`total_tenants`
           反之是真實計數（租戶確實被探索到了），這個區別是刻意的。
        3. key set 與 happy path 的 generate_report() **完全一致**（+ 兩個
           discriminator）——同一個 consumer 兩條路徑都能吃。
        """
        cli_argv("batch_diagnose", "--tenants", "db-a,db-b",
            "--prometheus", "http://prom:9090", "--dry-run", "--json")
        bd.main()
        captured = capsys.readouterr()

        doc = json.loads(captured.out)          # 全文 parse ⇒ stdout 只有 JSON
        assert doc["status"] == "dry_run"
        assert doc["reason"] == "health_checks_skipped"

        # 未測量的欄位必須是 None（不是 0）
        assert doc["healthy_count"] is None
        assert doc["issue_count"] is None
        assert doc["health_score"] is None
        # 已探索到的才是真計數
        assert doc["total_tenants"] == 2
        assert doc["prometheus_url"] == "http://prom:9090"
        assert doc["recommendations"] == []
        assert doc["tenants"] == {
            "db-a": {"tenant": "db-a", "status": "not_checked"},
            "db-b": {"tenant": "db-b", "status": "not_checked"},
        }

        # envelope 保留 happy-path 的全部 schema 鍵（drift-proof：generate_report
        # 日後新增欄位而 dry-run envelope 沒跟上 → 這行紅）
        happy_keys = set(bd.generate_report([], "http://prom:9090"))
        assert set(doc) == happy_keys | {"status", "reason"}

        # 人類訊息在 stderr，stdout 不含散文
        assert "Discovered 2 tenants" in captured.err
        assert "Discovered" not in captured.out

    def test_no_tenants_exits(self, monkeypatch, cli_argv):
        """沒有 tenant 時 exit 2 (caller error: nothing to act on, #452)。"""
        cli_argv("batch_diagnose", "--tenants", "")
        # Empty tenants string → discover from ConfigMap → mock empty
        with patch.object(bd, "discover_tenants", return_value=[]):
            with pytest.raises(SystemExit) as exc_info:
                bd.main()
            assert exc_info.value.code == 2  # EXIT_CALLER_ERROR (#452: no tenants found)

    def test_json_output(self, monkeypatch, capsys, cli_argv):
        """--json 輸出 JSON 格式。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json")

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["health_score"] == 1.0
        assert data["total_tenants"] == 1

    def test_text_output(self, monkeypatch, capsys, cli_argv):
        """預設文字輸出。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090")

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        assert "Health Score" in out
        assert "db-a" in out

    def test_output_file(self, monkeypatch, tmp_path, capsys, cli_argv):
        """--output 寫入 JSON 檔案。"""
        out_file = tmp_path / "report.json"
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090",
            "--output", str(out_file))

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["total_tenants"] == 1

    def test_executor_exception(self, monkeypatch, capsys, cli_argv):
        """ThreadPoolExecutor 例外被捕獲。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json")

        def mock_run_for_tenant(tenant, prom_url, timeout=30):
            raise RuntimeError("boom")

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        bd.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["issue_count"] == 1

    def test_auto_discover(self, monkeypatch, capsys, cli_argv):
        """不指定 --tenants 時自動探索（清單走 stderr，#1112）。"""
        cli_argv("batch_diagnose", "--prometheus", "http://prom:9090",
            "--dry-run")
        monkeypatch.setattr(bd, "discover_tenants",
                            lambda **kw: ["db-a", "db-b"])
        bd.main()
        err = capsys.readouterr().err
        assert "db-a" in err
        assert "db-b" in err

    def test_prometheus_env_fallback(self, monkeypatch, cli_argv):
        """--prometheus 省略 + $PROMETHEUS_URL 設定 → 讀 env（README §6.1、add_prometheus_arg）。

        batch-diagnose 先前硬編 http://localhost:9090（standalone 直跑讀不到
        env）；add_prometheus_arg 現在於 argparse 層 resolve $PROMETHEUS_URL。
        """
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        cli_argv("batch_diagnose", "--tenants", "db-a", "--json")
        captured = {}

        def mock_run(tenant, prom_url, timeout=30):
            captured["url"] = prom_url
            return {"tenant": tenant, "status": "healthy", "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant", mock_run)
        bd.main()
        assert captured["url"] == "http://test:1234"

    def test_prometheus_env_unset_fallback_localhost(self, monkeypatch, cli_argv):
        """--prometheus 省略 + env 未設 → localhost（byte-identical 舊行為）。"""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        cli_argv("batch_diagnose", "--tenants", "db-a", "--json")
        captured = {}

        def mock_run(tenant, prom_url, timeout=30):
            captured["url"] = prom_url
            return {"tenant": tenant, "status": "healthy", "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant", mock_run)
        bd.main()
        assert captured["url"] == "http://localhost:9090"


@pytest.mark.usefixtures("da_guard_env")
class TestDiscoverTenantsSpellingMatchesTheProducer:
    """#1603 — the ConfigMap keys ARE conf.d filenames, so match the producer.

    `scripts/ops/configmap_assemble.py` (what `make configmap-assemble` runs)
    turns a conf.d into those keys by the exporter's own case-insensitive
    rule, so both spellings ship; a narrower set here would report a smaller
    tenant set than what is deployed. Bounded on both sides below.

    ⚠️ The coupling is to that RULE, not to how either side is wired: both
    now take it from Go (`pkg/confdname`; here `da-guard parse-tree --names`,
    #3206). This docstring once said "globs `conf.d/` straight into
    `--from-file`" long after the glob was gone (#1792/#1796), which is a
    reader of this file being told the wrong thing about why the two sides
    have to agree.
    """

    def _discover(self, monkeypatch, keys):
        cm = {"data": {k: "tenants: {}" for k in keys}}
        monkeypatch.setattr(bd, "kubectl_get", lambda *a, **k: cm)
        return bd.discover_tenants()

    def test_yml_key_is_a_tenant(self, monkeypatch):
        """LOWER + CONTROL."""
        got = self._discover(
            monkeypatch, ["_defaults.yaml", "db-a.yaml", "db-b.yml"])
        assert got == ["db-a", "db-b"]

    def test_non_config_extensions_are_still_not_tenants(self, monkeypatch):
        """UPPER — widened to the config suffixes, not to 'any key'."""
        got = self._discover(
            monkeypatch,
            ["_defaults.yaml", "db-a.yaml", "README.md", "db-c.json",
             "x.yang", "plain.y"])
        assert got == ["db-a"]

    def test_a_same_stem_pair_counts_once_and_says_so(self, monkeypatch, capsys):
        """Accepting both spellings made one stem reachable through two keys.
        A ConfigMap in that state makes the exporter reject the ENTIRE config
        dir, so counting `db-a` twice — or de-duplicating it quietly — would
        both be misleading. Counted once, and named on stderr."""
        got = self._discover(
            monkeypatch, ["_defaults.yaml", "db-a.yaml", "db-a.yml"])
        assert got == ["db-a"]
        assert "db-a" in capsys.readouterr().err

    def test_reserved_keys_stay_out_in_both_spellings(self, monkeypatch):
        """The `_` filter is orthogonal to the spelling and must survive it."""
        got = self._discover(
            monkeypatch,
            ["_defaults.yaml", "_profiles.yml", "db-a.yaml", "db-b.yml"])
        assert got == ["db-a", "db-b"]

    def test_upper_case_key_keeps_its_case_as_go_names_it(self, monkeypatch):
        """The tenant id is Go's `tenant_named` (case kept), not a fold."""
        got = self._discover(monkeypatch, ["_defaults.yaml", "Tenant-Q.YAML"])
        assert got == ["Tenant-Q"]

    def test_no_da_guard_finds_no_tenant_and_says_why(self, monkeypatch, tmp_path, capsys):
        """No usable da-guard: the keys are not classed, so no tenant is
        reported (main: `No tenants found`, exit 2) — never a Python guess."""
        monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
        got = self._discover(monkeypatch, ["_defaults.yaml", "db-a.yaml"])
        assert got == []
        assert "da-guard parse-tree" in capsys.readouterr().err


# ── unchecked 不算健康（issue 1513，本輪決策 N2 的後續）─────────────
def test_unchecked_tenants_are_counted_apart_from_healthy():
    results = [
        {"tenant": "a", "status": "healthy"},
        {"tenant": "b", "status": "unchecked", "skipped": [{"check": "pod", "reason": "r"}]},
        {"tenant": "c", "status": "error", "issues": ["Pod not found"]},
    ]
    report = bd.generate_report(results, "http://p")
    assert report["healthy_count"] == 1
    assert report["unchecked_count"] == 1
    assert report["issue_count"] == 1
    # 分母只算有檢查的租戶：1 健康 / 2 有檢查
    assert report["health_score"] == 0.5
    assert not any(r.startswith("b:") for r in report["recommendations"])


def test_all_unchecked_has_no_score():
    report = bd.generate_report([{"tenant": "b", "status": "unchecked"}], "http://p")
    assert report["health_score"] is None and report["healthy_count"] == 0


def test_text_report_lists_unchecked_tenants(capsys):
    report = bd.generate_report([
        {"tenant": "b", "status": "unchecked",
         "skipped": [{"check": "pod", "reason": "db_type=postgresql"}]}], "http://p")
    bd.print_text_report(report)
    out = capsys.readouterr().out
    assert "Unchecked" in out and "b" in out and "db_type=postgresql" in out


# ── #2493：結束碼反映租戶檢查結果 ────────────────────────────────


_PROM_FAIL = {"status": "error",
              "issues": ["Prometheus query failed (http://prom:9090)"]}
_OTHER_FAIL = {"status": "error", "issues": ["exporter down"]}


class TestExitCode:
    """#2493：租戶檢查失敗原本也回 0，拿它當 CI 閘門永遠放行。

    與 diagnose 的單租戶分類（`diagnose.exit_code`）一致，再依 _lib_exitcodes
    的慣例彙總：任一租戶的 Prometheus 查詢失敗 → 2（環境問題，同
    validate_migration 的 has_query_error 優先）；否則任一租戶有其他失敗 → 1；
    全部 healthy／unchecked → 0。
    """

    @pytest.mark.parametrize("statuses, want", [
        ([{"status": "healthy"}, {"status": "unchecked"}], 0),
        ([{"status": "healthy"}, _OTHER_FAIL], 1),
        ([{"status": "healthy"}, _PROM_FAIL], 2),
        ([_OTHER_FAIL, _PROM_FAIL], 2),
    ])
    def test_main_returns_the_aggregate(self, monkeypatch, capsys, cli_argv,
                                        statuses, want):
        by_tenant = {f"t{i}": s for i, s in enumerate(statuses)}
        cli_argv("batch_diagnose", "--tenants", ",".join(by_tenant),
                 "--prometheus", "http://prom:9090", "--json")
        monkeypatch.setattr(
            bd, "run_diagnose_for_tenant",
            lambda tenant, prom_url, timeout=30: {"tenant": tenant,
                                                  **by_tenant[tenant]})
        assert bd.main() == want
        json.loads(capsys.readouterr().out)   # the report is still printed

    def test_dry_run_still_exits_zero(self, cli_argv, capsys):
        cli_argv("batch_diagnose", "--tenants", "t1", "--dry-run")
        assert bd.main() == 0

    def test_the_process_exit_code_carries_it(self, tmp_path):
        """端到端：票上的重現指令，Prometheus 連不上必須是 rc 2，不是 0。"""
        script = Path(bd.__file__)
        r = subprocess.run(
            [sys.executable, str(script), "--tenants", "t1",
             "--prometheus", "http://127.0.0.1:1", "--json"],
            capture_output=True, timeout=120, cwd=str(tmp_path),
            env={**os.environ, "PROMETHEUS_URL": ""})
        assert r.returncode == 2, r.stderr.decode("utf-8", "replace")[-600:]
        doc = json.loads(r.stdout.decode("utf-8"))
        assert doc["tenants"]["t1"]["status"] == "error"

    def test_missing_kubectl_is_2_as_in_diagnose(self, monkeypatch, capsys,
                                                 cli_argv):
        """`da-tools diagnose` alone exits 2 without kubectl; batch must too."""
        def no_kubectl(tenant, prom_url, out=None):
            raise bd.KubectlError("`kubectl get pods` in namespace 't1': kubectl is not on PATH")
        monkeypatch.setattr(bd, "diagnose_check", no_kubectl)
        cli_argv("batch_diagnose", "--tenants", "t1",
                 "--prometheus", "http://prom:9090", "--json")
        assert bd.main() == 2
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"]["t1"]["status"] == "error"
        assert "_environment_error" not in doc["tenants"]["t1"]

    def test_workers_zero_is_a_caller_error(self, cli_argv):
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "0",
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            bd.main()
        assert exc.value.code == 2

    def test_workers_must_be_an_integer(self, cli_argv, capsys):
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "abc",
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            bd.main()
        assert exc.value.code == 2
        # argparse alone would say "invalid positive_int value"; name the problem.
        assert "not an integer: 'abc'" in capsys.readouterr().err

    def test_an_explicit_worker_count_is_used(self, monkeypatch, capsys, cli_argv):
        """Must-fire for the two refusals above: a valid count still runs."""
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "3",
                 "--prometheus", "http://prom:9090", "--json")
        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            lambda tenant, prom_url, timeout=30:
                            {"tenant": tenant, "status": "healthy"})
        assert bd.main() == 0
        json.loads(capsys.readouterr().out)


# ── #3349：探索讀不到 ConfigMap 是環境錯誤，不是「沒有租戶」──────────


class TestDiscoveryWithoutAnAnswer:
    """真的跑 CLI、PATH 上放假 kubectl：探索讀取沒得到回答時，以前印一行
    `ERROR: kubectl failed` 再以 `No tenants found` 結束——讀起來像叢集裡沒有
    租戶。現在是 KubectlError：rc 2，點名 kubectl，--json 的 envelope reason
    是 `kubectl_error`。"""

    def test_forbidden_discovery_exits_2_naming_kubectl(self, tmp_path):
        path = _fake_kubectl(
            tmp_path, 1, stderr='Error from server (Forbidden): configmaps "threshold-config" '
                                'is forbidden: User "nobody" cannot get resource "configmaps"\n')
        r = _batch_cli(tmp_path, path, "--prometheus", "http://127.0.0.1:1", "--json")
        assert r.returncode == 2, r.stdout + r.stderr
        assert "No tenants found" not in r.stderr
        assert "Traceback" not in r.stderr
        assert "kubectl get configmap threshold-config" in r.stderr and "Forbidden" in r.stderr
        assert json.loads(r.stdout)["reason"] == "kubectl_error"

    def test_a_missing_configmap_exits_2_naming_it(self, tmp_path):
        path = _fake_kubectl(tmp_path, 0)   # --ignore-not-found: rc 0, nothing on stdout
        r = _batch_cli(tmp_path, path, "--prometheus", "http://127.0.0.1:1", "--json")
        assert r.returncode == 2, r.stdout + r.stderr
        assert "No tenants found" not in r.stderr
        assert "ConfigMap 'threshold-config' not found in namespace 'monitoring'" in r.stderr
        assert json.loads(r.stdout)["reason"] == "kubectl_error"


# ── #2954：--timeout 真的會中斷單一租戶的檢查 ─────────────────────


class TestPerTenantTimeout:
    """#2954: timeout_sec was never read and future.result(timeout=…) ran on
    already-finished futures, so a stuck check held the whole batch."""

    @staticmethod
    def _slow(seconds):
        def check(tenant, prom_url, *, out):
            time.sleep(seconds)
            out.write(json.dumps({"tenant": tenant, "status": "healthy"}))
        return check

    def test_a_check_past_its_deadline_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(bd, "diagnose_check", self._slow(3))
        start = time.monotonic()
        result = bd.run_diagnose_for_tenant("db-a", "http://prom", timeout_sec=0.3)
        assert time.monotonic() - start < 2
        assert result["status"] == "error"
        assert result["issues"] == ["timed out after 0.3s"]
        assert result["_environment_error"] is True

    def test_a_check_within_its_deadline_is_untouched(self, monkeypatch):
        """Control for the test above: same deadline, a fast check."""
        monkeypatch.setattr(bd, "diagnose_check", self._slow(0))
        result = bd.run_diagnose_for_tenant("db-a", "http://prom", timeout_sec=0.3)
        assert result["status"] == "healthy"

    def test_an_unexpected_exception_still_reaches_main(self, monkeypatch):
        """Moving the check into a thread must not swallow what main() used to
        catch from future.result() ("executor error")."""
        def boom(tenant, prom_url, *, out):
            raise RuntimeError("boom")
        monkeypatch.setattr(bd, "diagnose_check", boom)
        with pytest.raises(RuntimeError, match="boom"):
            bd.run_diagnose_for_tenant("db-a", "http://prom", timeout_sec=5)

    def test_cli_timeout_ends_the_run_with_2(self, monkeypatch, capsys, cli_argv):
        """End to end: --timeout reaches the check, and a tenant that could not
        be checked in time is an environment failure (rc 2, as a missing
        kubectl), not a healthy result."""
        monkeypatch.setattr(bd, "diagnose_check", self._slow(3))
        cli_argv("batch_diagnose", "--tenants", "t1", "--timeout", "0.3",
                 "--prometheus", "http://prom:9090", "--json")
        start = time.monotonic()
        assert bd.main() == 2
        assert time.monotonic() - start < 2
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"]["t1"]["status"] == "error"
        assert "_environment_error" not in doc["tenants"]["t1"]

    @pytest.mark.parametrize("value", ["0", "-1", "nan"])
    def test_timeout_must_be_above_zero(self, cli_argv, capsys, value):
        cli_argv("batch_diagnose", "--tenants", "t1", "--timeout", value,
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            bd.main()
        assert exc.value.code == 2
        assert "--timeout" in capsys.readouterr().err

    def test_default_timeout_outlasts_the_prometheus_queries(self):
        """A stuck Prometheus must still come back as "Prometheus query failed"
        (with its recommendation) under the default deadline, not as a bare
        timeout: the default outlasts every query diagnose can make."""
        import ast
        import inspect
        import diagnose
        tree = ast.parse(Path(diagnose.__file__).read_text(encoding="utf-8"))
        calls = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "query_prometheus")
        per_query = inspect.signature(
            diagnose.prometheus_instant).parameters["timeout"].default
        assert calls >= 4
        assert bd.DEFAULT_TIMEOUT_S > calls * per_query, (calls, per_query)
