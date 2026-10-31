#!/usr/bin/env python3
"""test_batch_diagnose.py — batch_diagnose.py pytest 風格測試。

驗證:
  1. read_configmap_files() + resolve_tenants() — ConfigMap 落成樹、租戶取 Go 的答案（#1525）
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
from _kubectl_fake import kubectl_run  # noqa: E402  (#3349)
import batch_diagnose as bd  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)
from _lib_tenant_values import DaGuardError, DaGuardNotFoundError  # noqa: E402
from _go_refusal import refuses  # noqa: E402  (#3206: the shared refusal)


_REAL_RUN = subprocess.run


def _kubectl_only(fake):
    """A `subprocess.run` that hands every `kubectl` call to *fake* and runs
    everything else for real: `resolve_tenants` reads the ConfigMap's tree
    through `da-guard effective` / `served-values` (#1525), subprocesses too,
    and a fake that answered them as well would be the ConfigMap's JSON read
    as da-guard's document."""
    def run(cmd, *args, **kwargs):
        if cmd and os.path.basename(str(cmd[0])) == "kubectl":
            return fake(cmd, *args, **kwargs)
        return _REAL_RUN(cmd, *args, **kwargs)
    return run


@pytest.fixture
def mock_run(monkeypatch, da_guard_env):
    """The `kubectl` that `read_configmap_files` runs (a MagicMock: set its
    `return_value` / `side_effect`); da-guard is the real one. #3349: the
    read goes through `_lib_kubectl.kubectl_get`, so this is that runner's
    `subprocess.run` — the process boundary, with kubectl_get's own reading
    of rc / stdout / stderr under test too."""
    fake = MagicMock()
    monkeypatch.setattr(_lib_kubectl.subprocess, "run", kubectl_run(_kubectl_only(fake)))
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


def _discover():
    """The tenants batch-diagnose finds in the ConfigMap kubectl returns."""
    return bd.resolve_tenants(bd.read_configmap_files())[0]


def _cm(data, binary=None):
    """A `kubectl get configmap -o json` answer."""
    doc = {"data": data}
    if binary is not None:
        doc["binaryData"] = binary
    return MagicMock(returncode=0, stdout=json.dumps(doc), stderr="")


_DEFAULTS = "defaults:\n  cpu: 50\n"
#: The tenants the CLI tests below name; one file declares them all.
_CM_TENANTS = ("db-a", "db-b", "t0", "t1", "t2", "t3")


def _cm_files(tenants=_CM_TENANTS):
    return {"_defaults.yaml": _DEFAULTS.encode(),
            "tenants.yaml": ("tenants:\n" + "".join(
                f"  {t}:\n    cpu: 90\n" for t in tenants)).encode()}


@pytest.fixture
def configmap(monkeypatch, da_guard_env):
    """main() reads this ConfigMap (#1525: in both modes); da-guard is real."""
    monkeypatch.setattr(bd, "read_configmap_files", lambda **kw: _cm_files())


def _batch_cli(tmp_path, path, *args):
    return subprocess.run(
        [sys.executable, str(Path(bd.__file__)), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path),
        env={**os.environ, "PATH": path, "PROMETHEUS_URL": ""})


class TestDiscoverTenants:
    """read_configmap_files() + resolve_tenants()（#1525：租戶取 Go 的答案）。"""

    def test_normal_discovery(self, mock_run):
        """正常 ConfigMap 應返回排序的 tenant 列表。"""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS,
            "db-b.yaml": "tenants:\n  db-b: {}\n",
            "db-a.yaml": "tenants:\n  db-a: {}\n",
        })
        assert _discover() == ["db-a", "db-b"]
        cmd = mock_run.call_args.args[0]
        assert cmd == ["kubectl", "get", "configmap", "threshold-config", "-n", "monitoring",
                       "--ignore-not-found", "-o", "json"]

    def test_skip_underscore_keys(self, mock_run):
        """_ 前綴的 key 不是租戶檔。"""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS,
            "_profiles.yaml": "profiles:\n  p:\n    cpu: 1\n",
            "db-a.yaml": "tenants:\n  db-a: {}\n",
        })
        assert _discover() == ["db-a"]

    def test_the_tenant_ids_are_the_files_not_the_key_names(self, mock_run):
        """#1525: one key declaring two tenants. Discovery by key name reported
        the tenant `team-x` and missed `db-a` and `db-b`."""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS,
            "team-x.yaml": "tenants:\n  db-a:\n    cpu: 90\n  db-b:\n    cpu: 70\n",
        })
        assert _discover() == ["db-a", "db-b"]

    def test_every_tenant_gets_its_chain_from_the_configmap(self, mock_run):
        """#1525: the chain is resolved from the same read, per tenant — the
        report's conf.d side used to be empty in batch."""
        mock_run.return_value = _cm({
            "_defaults.yaml": "defaults:\n  cpu: 50\n  mem: 60\n",
            "team-x.yaml": "tenants:\n  db-a:\n    cpu: 90\n  db-b:\n    mem: 70\n",
        })
        tenants, chains = bd.resolve_tenants(bd.read_configmap_files())
        assert tenants == ["db-a", "db-b"]
        assert chains["db-a"]["resolved"] == {"cpu": 90, "mem": 60}
        assert chains["db-b"]["resolved"] == {"cpu": 50, "mem": 70}
        assert [c["source"] for c in chains["db-a"]["chain"]] == ["_defaults.yaml", "team-x.yaml"]

    def test_binary_data_is_part_of_the_tree(self, mock_run):
        """The kubelet mounts binaryData keys as files too."""
        import base64
        mock_run.return_value = _cm(
            {"_defaults.yaml": _DEFAULTS},
            {"db-c.yaml": base64.b64encode(b"tenants:\n  db-c: {}\n").decode()})
        assert _discover() == ["db-c"]

    @pytest.mark.parametrize("data, binary", [
        ({"../x.yaml": "tenants: {}"}, None),
        ({"..": "tenants: {}"}, None),
        ({"a.yaml": 1}, None),
        ({}, {"b.yaml": "not base64!"}),
    ], ids=["path-key", "dotdot", "not-a-string", "bad-base64"])
    def test_a_key_that_is_not_a_mountable_file_is_refused(self, mock_run, data, binary):
        """Written as it is, `../x.yaml` would land outside the tree."""
        mock_run.return_value = _cm({"_defaults.yaml": _DEFAULTS, **data}, binary)
        with pytest.raises(bd.CallerError) as exc:
            bd.read_configmap_files()
        assert exc.value.reason == "configmap_unreadable"

    def test_a_tree_the_exporter_cannot_load_is_refused(self, mock_run):
        """No tenant is checked against a partial read: rc 2 in main."""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS,
            "db-a.yaml": "tenants:\n  db-a: {}\n",
            "zzz.yaml": "tenants:\n  db-b: [\n",
        })
        with refuses("zzz.yaml"):
            _discover()

    def test_requested_tenants_must_be_in_the_tree(self, mock_run):
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a: {}\n"})
        with pytest.raises(bd.TenantNotFoundError, match="'nope', 'gone'") as exc:
            bd.resolve_tenants(bd.read_configmap_files(), ["db-a", "nope", "gone"])
        assert exc.value.reason == "tenant_not_found"
        # must-fire control: the same tree answers the tenant it has
        assert bd.resolve_tenants(bd.read_configmap_files(), ["db-a"])[0] == ["db-a"]

    @pytest.mark.parametrize("doc, why", [
        ({"data": ["a.yaml"]}, "data is not a mapping"),
        ({"data": {"a.yaml": "tenants: {}"}, "binaryData": {"a.yaml": "dGVuYW50czoge30K"}},
         "in both data and binaryData"),
        ({"data": {"a.yaml": "x\ud800"}}, "not encodable as UTF-8"),
    ], ids=["data-not-mapping", "key-in-both", "lone-surrogate"])
    def test_a_configmap_of_an_impossible_shape_is_refused_by_name(self, mock_run, doc, why):
        """Not something an API server returns, but never a traceback (the
        lone surrogate was `UnicodeEncodeError`), and never one key's body
        silently served as the other's."""
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(doc), stderr="")
        with pytest.raises(bd.CallerError, match=why) as exc:
            bd.read_configmap_files()
        assert exc.value.reason == "configmap_unreadable"

    def test_two_keys_one_name_on_this_filesystem_is_refused(self, monkeypatch):
        """A case-insensitive filesystem makes `a.yaml` and `A.yaml` one file."""
        real_open = open
        seen = set()

        def folding_open(path, mode="r", *a, **k):
            folded = str(path).lower()
            if "x" in mode and folded in seen:
                raise FileExistsError(path)
            seen.add(folded)
            return real_open(path, mode, *a, **k)

        monkeypatch.setattr(bd, "open", folding_open, raising=False)
        with pytest.raises(bd.CallerError, match="collides") as exc:
            with bd.configmap_tree({"a.yaml": b"", "A.yaml": b""}):
                pass
        assert exc.value.reason == "configmap_unreadable"

    def test_the_written_tree_is_removed_on_every_exit(self, monkeypatch):
        roots = []
        with bd.configmap_tree({"a.yaml": b"x"}) as root:
            roots.append(root)
            assert os.path.isfile(os.path.join(root, "a.yaml"))
        with pytest.raises(RuntimeError):
            with bd.configmap_tree({"b.yaml": b"x"}) as root:
                roots.append(root)
                raise RuntimeError("boom")
        assert not any(os.path.exists(os.path.dirname(r)) for r in roots), roots

    def test_a_refusal_says_which_configmap_the_paths_are_keys_of(self, mock_run):
        """da-guard names files of the written-out copy; the run says whose
        keys they are."""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS, "zzz.yaml": "tenants:\n  db-b: [\n"})
        with refuses("zzz.yaml") as err:
            bd.resolve_tenants(bd.read_configmap_files(), where="ConfigMap ns/threshold-config")
        assert "NOTE: the paths named are keys of ConfigMap ns/threshold-config" in err.getvalue()

    def test_kubectl_failure(self, mock_run):
        """kubectl 失敗是 KubectlError（rc 2，點名 kubectl 自己那行話）。
        #3349：以前返回空列表，main 再報成 `No tenants found`。"""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="",
            stderr='Error from server (Forbidden): configmaps "threshold-config" is forbidden')
        with pytest.raises(bd.KubectlError, match=r"\(exit 1\).*Forbidden"):
            bd.read_configmap_files()

    def test_kubectl_timeout(self, mock_run):
        """kubectl timeout 是 KubectlError（#3349：以前返回空列表）。"""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=15)
        with pytest.raises(bd.KubectlError, match="no answer within 15s"):
            bd.read_configmap_files()

    def test_missing_configmap_is_named(self, mock_run):
        """ConfigMap 不存在（`--ignore-not-found`：rc 0、stdout 空）→ 具名的呼叫端
        錯誤，點名那個 ConfigMap 與 namespace，不是 `No tenants found`（#3349）。
        kubectl 有回答，所以 reason 不是 kubectl_error。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(bd.CallerError, match="ConfigMap 'threshold-config' not found "
                                                 "in namespace 'monitoring'") as exc:
            bd.read_configmap_files()
        assert not isinstance(exc.value, bd.KubectlError)
        assert exc.value.reason == "configmap_not_found"

    def test_a_tree_without_tenants_has_none(self, mock_run):
        """只有 `_defaults.yaml` 的 ConfigMap：沒有租戶（main 報 No tenants found）。"""
        mock_run.return_value = _cm({"_defaults.yaml": _DEFAULTS})
        assert _discover() == []


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
        """正常診斷回傳 check() 交回的文件（#3216：不再經 stdout 往返）。"""
        def mock_check(tenant, prom_url, *, chain=None):
            return {"tenant": tenant, "status": "healthy"}
        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")
        assert result["tenant"] == "db-a"
        assert result["status"] == "healthy"
        assert "elapsed_seconds" in result

    def test_exception_caught(self, monkeypatch):
        """例外回傳 error 狀態。"""
        def mock_check(tenant, prom_url, *, chain=None):
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

        def mock_check(tenant, prom_url, *, chain=None):
            seen.append(sys.stdout)
            return {"tenant": tenant, "status": "healthy"}

        monkeypatch.setattr(bd, "diagnose_check", mock_check)
        before = sys.stdout
        result = bd.run_diagnose_for_tenant("db-a", "http://prom")

        assert result["status"] == "healthy"
        assert seen == [before], (
            "check() 執行期間 sys.stdout 被換掉了 —— 有人又把行程全域當成捕捉"
            "輸出的手段；平行 worker 下這會讓真 stdout 一去不回"
        )
        assert sys.stdout is before


# ── read_configmap_files / resolve_tenants 邊際 ──────────────────────────────────


class TestDiscoverTenantsEdge:
    """read_configmap_files() / resolve_tenants() 邊際案例。"""

    def test_invalid_json(self, mock_run):
        """無效 JSON 是 KubectlError（#3349：以前回傳空清單）。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        with pytest.raises(bd.KubectlError, match="not JSON"):
            bd.read_configmap_files()

    def test_non_yaml_keys_are_not_read(self, mock_run):
        """非設定檔副檔名的 key，exporter 不讀，裡面的 `tenants:` 不算。"""
        mock_run.return_value = _cm({
            "_defaults.yaml": _DEFAULTS, "readme.txt": "tenants:\n  db-r: {}\n",
            "db-a.yaml": "tenants:\n  db-a: {}\n"})
        assert _discover() == ["db-a"]


# ── main() CLI 測試 ─────────────────────────────────────────────


@pytest.mark.usefixtures("configmap")
class TestMainCLI:
    """main() CLI 整合測試。"""

    def test_tenants_flag(self, monkeypatch, capsys, cli_argv):
        """--tenants 直接指定 tenant 清單。

        #1112: 人類可讀的 dry-run 清單走 stderr（stdout 保留給 --json 的
        單一 JSON 文件），故斷言 stderr。
        """
        cli_argv("batch_diagnose", "--tenants", "db-a,db-b",
            "--prometheus", "http://prom:9090", "--dry-run")
        emit_result(bd.main())
        err = capsys.readouterr().err
        assert "db-a" in err
        assert "db-b" in err
        assert "2 tenants" in err

    def test_dry_run_single(self, monkeypatch, capsys, cli_argv):
        """--dry-run 列出 tenant 但不執行檢查（清單走 stderr，#1112）。"""
        cli_argv("batch_diagnose", "--tenants", "db-a", "--dry-run")
        emit_result(bd.main())
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
        emit_result(bd.main())
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
        # Empty tenants string → every tenant of the ConfigMap → it has none
        monkeypatch.setattr(bd, "read_configmap_files",
                            lambda **kw: {"_defaults.yaml": _DEFAULTS.encode()})
        with pytest.raises(SystemExit) as exc_info:
            emit_result(bd.main())
        assert exc_info.value.code == 2  # EXIT_CALLER_ERROR (#452: no tenants found)

    def test_each_tenant_carries_its_chain_from_the_configmap(self, monkeypatch, capsys,
                                                              cli_argv):
        """#1525 end to end through the real check(): the report's conf.d
        side, empty in batch before, is each tenant's chain."""
        import diagnose
        monkeypatch.setattr(diagnose, "tenant_db_type", lambda *a: (None, None))
        monkeypatch.setattr(diagnose, "query_prometheus", lambda *a: ([], None))
        cli_argv("batch_diagnose", "--tenants", "db-a,db-b",
                 "--prometheus", "http://prom:9090", "--json")
        assert emit_result(bd.main()) == 0
        doc = json.loads(capsys.readouterr().out)
        for t in ("db-a", "db-b"):
            chain = doc["tenants"][t]["inheritance_chain"]
            assert [c["layer"] for c in chain["layers"]] == ["tenant"], chain
            assert chain["layers"][0]["source"] == "tenants.yaml", chain

    def test_a_tree_the_exporter_cannot_load_checks_no_tenant(self, monkeypatch, cli_argv):
        """A broken ConfigMap stops the whole batch (rc 2) before any check —
        not one `status: error` per tenant (rc 1)."""
        monkeypatch.setattr(bd, "read_configmap_files", lambda **kw: {
            **_cm_files(), "zzz.yaml": b"tenants:\n  db-z: [\n"})
        ran = []
        monkeypatch.setattr(bd, "run_diagnose_for_tenant", lambda *a, **k: ran.append(a))
        cli_argv("batch_diagnose", "--prometheus", "http://prom:9090", "--json")
        with refuses("zzz.yaml"):
            emit_result(bd.main())
        assert ran == []

    def test_a_named_tenant_the_configmap_lacks_exits_2(self, monkeypatch, cli_argv, capsys):
        ran = []
        monkeypatch.setattr(bd, "run_diagnose_for_tenant", lambda *a, **k: ran.append(a))
        cli_argv("batch_diagnose", "--tenants", "db-a,nope", "--namespace", "ops", "--json")
        with pytest.raises(bd.TenantNotFoundError,
                           match="'nope' in ConfigMap ops/threshold-config"):
            emit_result(bd.main())
        assert ran == []

    def test_a_tenant_named_twice_is_checked_once(self, monkeypatch, cli_argv, capsys):
        ran = []
        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            lambda t, *a, **k: ran.append(t) or {"tenant": t, "status": "healthy"})
        cli_argv("batch_diagnose", "--tenants", "db-a,db-a,db-b", "--json")
        assert emit_result(bd.main()) == 0
        assert sorted(ran) == ["db-a", "db-b"]

    def test_a_tenants_flag_naming_none_exits(self, cli_argv):
        """`--tenants ,` names no tenant: not read as "every tenant"."""
        cli_argv("batch_diagnose", "--tenants", ",", "--dry-run")
        with pytest.raises(SystemExit) as exc_info:
            emit_result(bd.main())
        assert exc_info.value.code == 2

    def test_json_output(self, monkeypatch, capsys, cli_argv):
        """--json 輸出 JSON 格式。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json")

        def mock_run_for_tenant(tenant, prom_url, timeout=30, chain=None):
            return {"tenant": tenant, "status": "healthy",
                    "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        emit_result(bd.main())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["health_score"] == 1.0
        assert data["total_tenants"] == 1

    def test_text_output(self, monkeypatch, capsys, cli_argv):
        """預設文字輸出。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090")

        def mock_run_for_tenant(tenant, prom_url, timeout=30, chain=None):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        emit_result(bd.main())
        out = capsys.readouterr().out
        assert "Health Score" in out
        assert "db-a" in out

    def test_output_file(self, monkeypatch, tmp_path, capsys, cli_argv):
        """--output 寫入 JSON 檔案。"""
        out_file = tmp_path / "report.json"
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090",
            "--output", str(out_file))

        def mock_run_for_tenant(tenant, prom_url, timeout=30, chain=None):
            return {"tenant": tenant, "status": "healthy",
                    "operational_mode": "normal", "elapsed_seconds": 0.5}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        emit_result(bd.main())
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["total_tenants"] == 1

    def test_executor_exception(self, monkeypatch, capsys, cli_argv):
        """ThreadPoolExecutor 例外被捕獲。"""
        cli_argv("batch_diagnose", "--tenants", "db-a",
            "--prometheus", "http://prom:9090", "--json")

        def mock_run_for_tenant(tenant, prom_url, timeout=30, chain=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            mock_run_for_tenant)
        emit_result(bd.main())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["issue_count"] == 1

    def test_auto_discover(self, monkeypatch, capsys, cli_argv):
        """不指定 --tenants 時自動探索（清單走 stderr，#1112）。"""
        cli_argv("batch_diagnose", "--prometheus", "http://prom:9090",
            "--dry-run")
        emit_result(bd.main())
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

        def mock_run(tenant, prom_url, timeout=30, chain=None):
            captured["url"] = prom_url
            return {"tenant": tenant, "status": "healthy", "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant", mock_run)
        emit_result(bd.main())
        assert captured["url"] == "http://test:1234"

    def test_prometheus_env_unset_fallback_localhost(self, monkeypatch, cli_argv):
        """--prometheus 省略 + env 未設 → localhost（byte-identical 舊行為）。"""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        cli_argv("batch_diagnose", "--tenants", "db-a", "--json")
        captured = {}

        def mock_run(tenant, prom_url, timeout=30, chain=None):
            captured["url"] = prom_url
            return {"tenant": tenant, "status": "healthy", "elapsed_seconds": 0.1}

        monkeypatch.setattr(bd, "run_diagnose_for_tenant", mock_run)
        emit_result(bd.main())
        assert captured["url"] == "http://localhost:9090"


@pytest.mark.usefixtures("da_guard_env")
class TestDiscoverTenantsSpellingMatchesTheProducer:
    """#1603 — the ConfigMap keys ARE conf.d filenames, so match the producer.

    `scripts/ops/configmap_assemble.py` (what `make configmap-assemble` runs)
    turns a conf.d into those keys by the exporter's own case-insensitive
    rule, so both spellings ship; a narrower set here would report a smaller
    tenant set than what is deployed. Bounded on both sides below.

    #1525: the tenants are now the ones the exporter reads from those files
    (`da-guard effective` on the ConfigMap written out as a tree), so which
    key is read and what id a file declares are both Go's answer; the cases
    below pin that answer, each file declaring a tenant named after its stem
    unless said otherwise.
    """

    @staticmethod
    def _discover(monkeypatch, keys, bodies=None):
        bodies = bodies or {}
        data = {}
        for k in keys:
            stem = k.rsplit(".", 1)[0]
            data[k] = bodies.get(k, _DEFAULTS if k == "_defaults.yaml"
                                 else "profiles:\n  p:\n    cpu: 1\n" if k.startswith("_")
                                 else f"tenants:\n  {stem}: {{}}\n")
        monkeypatch.setattr(bd, "kubectl_get", lambda *a, **k: {"data": data})
        return _discover()

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

    def test_a_tenant_declared_under_both_spellings_refuses_the_tree(self, monkeypatch):
        """`db-a.yaml` + `db-a.yml` both declaring `db-a`: the exporter rejects
        the ENTIRE config dir (duplicate tenant ID). It used to be counted once
        here and the batch ran on; now the run is refused (rc 2 in main)."""
        with pytest.raises(DaGuardError, match="duplicate tenant ID"):
            self._discover(monkeypatch, ["_defaults.yaml", "db-a.yaml", "db-a.yml"])

    def test_a_same_stem_pair_declaring_two_ids_is_two_tenants(self, monkeypatch):
        """Must-fire for the case above: same stem, different ids — both are
        served, and the old stem rule reported only `db-a`."""
        got = self._discover(monkeypatch, ["_defaults.yaml", "db-a.yaml", "db-a.yml"],
                             {"db-a.yml": "tenants:\n  db-x: {}\n"})
        assert got == ["db-a", "db-x"]

    def test_reserved_keys_stay_out_in_both_spellings(self, monkeypatch):
        """The `_` filter is orthogonal to the spelling and must survive it."""
        got = self._discover(
            monkeypatch,
            ["_defaults.yaml", "_profiles.yml", "db-a.yaml", "db-b.yml"])
        assert got == ["db-a", "db-b"]

    def test_upper_case_key_keeps_its_case_as_go_names_it(self, monkeypatch):
        """The tenant id is the one the file declares (case kept), not a fold."""
        got = self._discover(monkeypatch, ["_defaults.yaml", "Tenant-Q.YAML"])
        assert got == ["Tenant-Q"]

    def test_no_da_guard_refuses_the_run(self, monkeypatch, tmp_path):
        """No usable da-guard: no tenant list at all (rc 2 in main, named) —
        never a Python guess, and no longer `No tenants found`."""
        monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
        with pytest.raises(DaGuardNotFoundError):
            self._discover(monkeypatch, ["_defaults.yaml", "db-a.yaml"])


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


@pytest.mark.usefixtures("configmap")
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
            lambda tenant, prom_url, timeout=30, chain=None: {"tenant": tenant,
                                                  **by_tenant[tenant]})
        assert emit_result(bd.main()) == want
        json.loads(capsys.readouterr().out)   # the report is still printed

    def test_dry_run_still_exits_zero(self, cli_argv, capsys):
        cli_argv("batch_diagnose", "--tenants", "t1", "--dry-run")
        assert emit_result(bd.main()) == 0

    def test_the_process_exit_code_carries_it(self, tmp_path):
        """端到端：票上的重現指令，Prometheus 連不上必須是 rc 2，不是 0。"""
        cm = {"data": {k: v.decode() for k, v in _cm_files().items()}}
        path = _fake_kubectl(tmp_path, 0, stdout=json.dumps(cm))
        script = Path(bd.__file__)
        r = subprocess.run(
            [sys.executable, str(script), "--tenants", "t1",
             "--prometheus", "http://127.0.0.1:1", "--json"],
            capture_output=True, timeout=120, cwd=str(tmp_path),
            env={**os.environ, "PROMETHEUS_URL": "", "PATH": path})
        assert r.returncode == 2, r.stderr.decode("utf-8", "replace")[-600:]
        doc = json.loads(r.stdout.decode("utf-8"))
        assert doc["tenants"]["t1"]["status"] == "error"

    def test_missing_kubectl_is_2_as_in_diagnose(self, monkeypatch, capsys,
                                                 cli_argv):
        """`da-tools diagnose` alone exits 2 without kubectl; batch must too."""
        def no_kubectl(tenant, prom_url, chain=None):
            raise bd.KubectlError("`kubectl get pods` in namespace 't1': kubectl is not on PATH")
        monkeypatch.setattr(bd, "diagnose_check", no_kubectl)
        cli_argv("batch_diagnose", "--tenants", "t1",
                 "--prometheus", "http://prom:9090", "--json")
        assert emit_result(bd.main()) == 2
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"]["t1"]["status"] == "error"
        assert "_environment_error" not in doc["tenants"]["t1"]

    def test_workers_zero_is_a_caller_error(self, cli_argv):
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "0",
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            emit_result(bd.main())
        assert exc.value.code == 2

    def test_workers_must_be_an_integer(self, cli_argv, capsys):
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "abc",
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            emit_result(bd.main())
        assert exc.value.code == 2
        # argparse alone would say "invalid positive_int value"; name the problem.
        assert "not an integer: 'abc'" in capsys.readouterr().err

    def test_an_explicit_worker_count_is_used(self, monkeypatch, capsys, cli_argv):
        """Must-fire for the two refusals above: a valid count still runs."""
        cli_argv("batch_diagnose", "--tenants", "t1", "--workers", "3",
                 "--prometheus", "http://prom:9090", "--json")
        monkeypatch.setattr(bd, "run_diagnose_for_tenant",
                            lambda tenant, prom_url, timeout=30, chain=None:
                            {"tenant": tenant, "status": "healthy"})
        assert emit_result(bd.main()) == 0
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

    def test_a_configmap_the_exporter_rejects_exits_2(self, tmp_path, da_guard_env):
        """#1525: one tenant in two keys — the exporter rejects the whole tree,
        so the batch stops (rc 2, da-guard's reason), it does not run on."""
        cm = {"data": {"_defaults.yaml": _DEFAULTS,
                       "db-a.yaml": "tenants:\n  db-a: {}\n",
                       "db-a.yml": "tenants:\n  db-a: {}\n"}}
        path = _fake_kubectl(tmp_path, 0, stdout=json.dumps(cm))
        r = _batch_cli(tmp_path, path, "--prometheus", "http://127.0.0.1:1", "--json")
        assert r.returncode == 2, r.stdout + r.stderr
        assert "Traceback" not in r.stderr
        assert "duplicate tenant ID" in r.stderr
        assert json.loads(r.stdout)["reason"] == "served_values_unavailable"

    def test_a_missing_configmap_exits_2_naming_it(self, tmp_path):
        path = _fake_kubectl(tmp_path, 0)   # --ignore-not-found: rc 0, nothing on stdout
        r = _batch_cli(tmp_path, path, "--prometheus", "http://127.0.0.1:1", "--json")
        assert r.returncode == 2, r.stdout + r.stderr
        assert "No tenants found" not in r.stderr
        assert "ConfigMap 'threshold-config' not found in namespace 'monitoring'" in r.stderr
        assert json.loads(r.stdout)["reason"] == "configmap_not_found"


# ── #2954：--timeout 真的會中斷單一租戶的檢查 ─────────────────────


class TestPerTenantTimeout:
    """#2954: timeout_sec was never read and future.result(timeout=…) ran on
    already-finished futures, so a stuck check held the whole batch."""

    @staticmethod
    def _slow(seconds):
        def check(tenant, prom_url, *, chain=None):
            time.sleep(seconds)
            return {"tenant": tenant, "status": "healthy"}
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
        def boom(tenant, prom_url, *, chain=None):
            raise RuntimeError("boom")
        monkeypatch.setattr(bd, "diagnose_check", boom)
        with pytest.raises(RuntimeError, match="boom"):
            bd.run_diagnose_for_tenant("db-a", "http://prom", timeout_sec=5)

    @pytest.mark.usefixtures("configmap")
    def test_cli_timeout_ends_the_run_with_2(self, monkeypatch, capsys, cli_argv):
        """End to end: --timeout reaches the check, and a tenant that could not
        be checked in time is an environment failure (rc 2, as a missing
        kubectl), not a healthy result."""
        monkeypatch.setattr(bd, "diagnose_check", self._slow(3))
        cli_argv("batch_diagnose", "--tenants", "t1", "--timeout", "0.3",
                 "--prometheus", "http://prom:9090", "--json")
        start = time.monotonic()
        assert emit_result(bd.main()) == 2
        assert time.monotonic() - start < 2
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"]["t1"]["status"] == "error"
        assert "_environment_error" not in doc["tenants"]["t1"]

    @pytest.mark.parametrize("value", ["0", "-1", "nan"])
    def test_timeout_must_be_above_zero(self, cli_argv, capsys, value):
        cli_argv("batch_diagnose", "--tenants", "t1", "--timeout", value,
                 "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            emit_result(bd.main())
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


# ── #3349：某租戶的 Pod 讀取沒得到回答 → 整批 rc 2 ─────────────────


def test_a_forbidden_pod_read_makes_the_batch_an_environment_error(monkeypatch):
    """整條鏈：pod_phase 的讀取被拒（rc 1、stdout 仍有空 List）→ check() 往上丟
    KubectlError → 該租戶標成環境錯誤 → 批次 rc 2。之前是「Pod not found」、
    批次 rc 1——一個讀不到的叢集被報成租戶有問題。"""
    import diagnose

    def prom(_url, query):
        if query.startswith("tenant_expected_exporter"):
            return [{"metric": {"db_type": "mariadb"}, "value": [0, "1"]}], None
        if query.startswith("mysql_up"):
            return [{"metric": {}, "value": [0, "1"]}], None
        return [], None
    monkeypatch.setattr(diagnose, "query_prometheus", prom)
    forbidden = MagicMock(return_value=MagicMock(
        returncode=1, stdout='{"apiVersion": "v1", "items": [], "kind": "List"}',
        stderr='Error from server (Forbidden): pods is forbidden\n'))
    monkeypatch.setattr(_lib_kubectl.subprocess, "run", kubectl_run(forbidden))

    result = bd.run_diagnose_for_tenant("t1", "http://prom:9090", timeout_sec=30)
    assert result.get("_environment_error") is True, result
    assert "Forbidden" in result["issues"][0] and "Pod not found" not in result["issues"], result
    env = {result["tenant"]} if result.pop("_environment_error") else set()
    assert bd.batch_exit_code([result], env) == 2
