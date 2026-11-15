"""pytest style tests for cutover_tenant.py — Shadow Monitoring one-command cutover."""

import errno
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

import cutover_tenant as ct
from _kubectl_fake import install as install_kubectl  # (#3539)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _readiness(ready=True, pct=100.0, converged=5, total=5):
    """A parsed cutover-readiness.json."""
    return {
        "ready": ready,
        "timestamp": "2026-03-07T12:00:00Z",
        "convergence_percentage": pct,
        "converged_count": converged,
        "total_pairs": total,
        "converged_pairs": [f"pair_{i}" for i in range(converged)],
        "unconverged_pairs": [],
        "round_count": 10,
        "stability_window": 5,
        "recommendation": "Safe to cutover" if ready else "Not ready",
    }


# ---------------------------------------------------------------------------
# TestLoadCutoverReadiness
# ---------------------------------------------------------------------------

class TestLoadCutoverReadiness:
    """load_cutover_readiness() tests。"""

    def test_valid_json(self):
        """有效內容正確載入。"""
        data = ct.load_cutover_readiness(_readiness())
        assert data["ready"]
        assert data["convergence_percentage"] == 100.0

    def test_not_ready(self):
        """未準備好的狀態正確解析。"""
        data = ct.load_cutover_readiness(_readiness(ready=False, pct=60.0))
        assert not data["ready"]

    def test_missing_fields(self):
        """缺失必要欄位拋出 ValueError。"""
        with pytest.raises(ValueError) as exc_info:
            ct.load_cutover_readiness({"ready": True})
        assert "Missing required fields" in str(exc_info.value)


class TestReadinessFileIsReadByTheLib:
    """#3445: --readiness-json 的路徑與內容由 `_lib_input.document` 判，
    任何步驟執行前就以 caller error（點名旗標與路徑）結束——不是 traceback，
    也不是 report 裡的一個失敗步驟。三種壞內容的 CLI 列在
    tests/shared/test_caller_paths.py（由 decode 宣告自動生成）。"""

    @pytest.mark.parametrize("content, detail", [
        (b"not json", "not valid JSON: Expecting value: line 1 column 1 (char 0)"),
        (b"[]", "the top level is not a mapping (got a list)"),
    ])
    def test_content_it_cannot_read_is_named(self, tmp_path, monkeypatch, content, detail):
        from _lib_exitcodes import CallerError
        path = tmp_path / "r.json"
        path.write_bytes(content)
        monkeypatch.setattr("sys.argv", ["cutover", "--readiness-json", str(path),
                                         "--tenant", "t", "--dry-run"])
        with mock.patch("cutover_tenant._run_kubectl") as kube:
            with pytest.raises(CallerError) as exc:
                ct.main()
        assert str(exc.value) == f"--readiness-json {path}: {detail}"
        kube.assert_not_called()

    def test_file_not_found(self, monkeypatch):
        from _lib_exitcodes import CallerError
        monkeypatch.setattr("sys.argv", ["cutover", "--readiness-json", "/no/such/file.json",
                                         "--tenant", "t"])
        with pytest.raises(CallerError) as exc:
            ct.main()
        assert exc.value.reason == "file_not_found"


class TestDryRunDoesNotJudgeTheEnv:
    """#3528 review: a dry run sends nothing, so an unusable $PROMETHEUS_URL
    may not stop it (it was rc 2); a real run is stopped, named."""

    def _run(self, tmp_path, monkeypatch, *extra):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(_readiness()), encoding="utf-8")
        monkeypatch.setenv("PROMETHEUS_URL", "ftp://x")
        monkeypatch.setattr("sys.argv", ["cutover", "--readiness-json", str(path),
                                         "--tenant", "t1", *extra])
        seen = {}

        def fake_apply(**kwargs):
            seen.update(kwargs)
            return {"success": True, "steps": [], "failed_step": None}
        monkeypatch.setattr(ct, "apply_cutover", fake_apply)
        try:
            ct.main()
        except SystemExit:
            pass
        return seen

    def test_dry_run_with_an_unusable_env_runs(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, "--dry-run")
        assert seen["prometheus_url"] == "ftp://x" and seen["dry_run"] is True

    def test_a_real_run_with_an_unusable_env_is_refused_by_name(self, tmp_path, monkeypatch):
        from _lib_exitcodes import CallerError
        with pytest.raises(CallerError) as exc:
            self._run(tmp_path, monkeypatch)
        assert "$PROMETHEUS_URL" in str(exc.value) and exc.value.reason == \
            "prometheus_url_unusable"


# ---------------------------------------------------------------------------
# TestRunKubectl
# ---------------------------------------------------------------------------

def _kubectl(monkeypatch, answer):
    """Every kubectl `_lib_kubectl.kubectl_run` starts is *answer*: a callable
    taking the argv (it starts with `kubectl`) that returns `(rc, stdout,
    stderr)` or raises what starting it raised. Returns the recorded argvs."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, *answer(cmd, **kw))
    install_kubectl(monkeypatch, fake)
    return calls


class TestRunKubectl:
    """_run_kubectl() tests。"""

    def test_dry_run(self):
        """Dry-run 模式返回 (dry-run) 消息。"""
        ok, msg = ct._run_kubectl(["get", "pods"], dry_run=True)
        assert ok
        assert msg == "(dry-run)"

    def test_success(self, monkeypatch):
        """成功執行返回 True 和輸出。"""
        calls = _kubectl(monkeypatch, lambda cmd, **kw: (0, "deleted\n", ""))
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert ok
        assert msg == "deleted"
        assert calls == [["kubectl", "delete", "job", "shadow-monitor"]]

    def test_failure(self, monkeypatch):
        """失敗執行返回 False 和錯誤訊息。"""
        _kubectl(monkeypatch, lambda cmd, **kw: (1, "", "not found"))
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert not ok
        assert "not found" in msg
        assert not msg.startswith(ct.CALLER_ERROR_PREFIX)

    @pytest.mark.parametrize("raised, why", [
        (PermissionError(errno.EACCES, "Permission denied", "kubectl"), "cannot run kubectl"),
        (OSError(errno.ENOEXEC, "Exec format error", "kubectl"), "cannot run kubectl"),
        (FileNotFoundError(errno.ENOENT, "No such file or directory", "kubectl"),
         "cannot run kubectl"),
        (subprocess.TimeoutExpired(cmd="kubectl", timeout=30), "did not finish within 30s"),
    ], ids=["eacces", "enoexec", "enoent", "timeout"])
    def test_no_exit_status_is_a_caller_error(self, monkeypatch, raised, why):
        """#3539: kubectl 起不來（沒有執行權限、ENOEXEC、`#!` 直譯器不存在）或逾時
        ＝環境問題（caller-error，exit 2），訊息指名哪個指令。EACCES／ENOEXEC
        原本是未接的 OSError（traceback、`unexpected internal error`）。"""
        def boom(cmd, **kw):
            raise raised
        _kubectl(monkeypatch, boom)
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert not ok
        assert msg.startswith(ct.CALLER_ERROR_PREFIX)
        assert why in msg and "kubectl delete job shadow-monitor" in msg

    def test_a_kubectl_killed_by_a_signal_is_a_caller_error(self, monkeypatch):
        """Review of #3539: it gave no exit status of its own — exit 2, not a
        cutover finding (it was exit 1, "exit code -9") — and the words do
        not say it did not run: the step may have been sent."""
        _kubectl(monkeypatch, lambda cmd, **kw: (-9, "", ""))
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert not ok and msg.startswith(ct.CALLER_ERROR_PREFIX)
        assert msg.endswith("kubectl delete was killed by signal 9") and "did not run" not in msg

    def test_kubectl_not_on_path_is_a_caller_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        ok, msg = ct._run_kubectl(["get", "pods"])
        assert not ok
        assert msg.startswith(ct.CALLER_ERROR_PREFIX) and "kubectl is not available" in msg


class TestPartWayCutover:
    """#3539: 第一步成功、之後 kubectl 起不來 —— 不可報成功，結束碼 2，
    報告指出完成了哪些步驟、停在哪一步。"""

    @pytest.mark.parametrize("raised", [
        PermissionError(errno.EACCES, "Permission denied", "kubectl"),
        OSError(errno.ENOEXEC, "Exec format error", "kubectl"),
        subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
    ], ids=["eacces", "enoexec", "timeout"])
    def test_a_later_step_that_cannot_start_is_exit_2_and_names_what_ran(
            self, monkeypatch, tmp_path, capsys, raised):
        import _lib_exitcodes
        seen = []

        def kubectl(cmd, **kw):
            seen.append(cmd[1:3])
            if len(seen) > 1:
                raise raised
            return (0, "job deleted\n", "")
        _kubectl(monkeypatch, kubectl)
        readiness = tmp_path / "ready.json"
        readiness.write_text(json.dumps({
            "ready": True, "timestamp": "2026-01-01T00:00:00Z",
            "convergence_percentage": 100, "converged_count": 1, "total_pairs": 1,
        }), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "cutover_tenant.py", "--readiness-json", str(readiness), "--tenant", "t1",
            "--prometheus", "http://127.0.0.1:9", "--json-output"])
        rc = _lib_exitcodes.emit_result(ct.main())
        out, err = capsys.readouterr()
        assert rc == 2
        assert seen == [["delete", "job"], ["delete", "configmap"]]   # stopped there
        doc = json.loads(out)
        assert doc["success"] is False and doc["caller_error"] is True
        assert doc["steps_completed"] == ["Stop Shadow Monitor Job"]
        assert doc["failed_step"] == "Remove old Recording Rules"
        assert "Cutover completed successfully" not in err


# ---------------------------------------------------------------------------
# TestStepFunctions
# ---------------------------------------------------------------------------

class TestStepFunctions:
    """Individual step function tests。"""

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "deleted"))
    def test_stop_shadow_job(self, mock_kube):
        """停止 shadow job 的步驟。"""
        ok, msg = ct.stop_shadow_job(namespace="monitoring")
        assert ok
        mock_kube.assert_called_once()
        args = mock_kube.call_args[0][0]
        assert "shadow-monitor" in args

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "deleted"))
    def test_remove_old_rules(self, mock_kube):
        """移除舊規則的步驟。"""
        ok, msg = ct.remove_old_rules(configmap="my-cm")
        assert ok

    @mock.patch("cutover_tenant._run_kubectl",
                return_value=(False, "not labeled"))
    def test_remove_shadow_label_already_absent(self, _mock):
        """移除 shadow label (已缺失的情況)。"""
        ok, msg = ct.remove_shadow_label()
        assert ok
        assert "already absent" in msg

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "labeled"))
    def test_remove_shadow_route(self, _mock):
        """移除 shadow route 的步驟。"""
        ok, msg = ct.remove_shadow_route()
        assert ok

    def test_verify_health_dry_run(self):
        """驗證健康狀態的 dry-run 模式。"""
        ok, msg = ct.verify_health("db-a", "http://localhost:9090",
                                   dry_run=True)
        assert ok
        assert msg == "(dry-run)"

    @staticmethod
    def _answer(monkeypatch, body, err=None):
        import _lib_prometheus
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda url, timeout=10: (body, err))

    def test_verify_health_counts(self, monkeypatch):
        self._answer(monkeypatch, {"status": "success", "data": {
            "resultType": "vector", "result": [{"metric": {}, "value": [1, "7"]}]}})
        assert ct.verify_health("t1", "http://prom:9090") == (
            True, "tenant=t1: 7 threshold metrics active")

    def test_verify_health_empty_vector_is_a_finding(self, monkeypatch):
        self._answer(monkeypatch, {"status": "success",
                                   "data": {"resultType": "vector", "result": []}})
        ok, msg = ct.verify_health("t1", "http://prom:9090")
        assert not ok and not msg.startswith(ct.CALLER_ERROR_PREFIX)

    @pytest.mark.parametrize("body, err", [
        (None, "connection refused"),
        ({"status": "error", "error": "bad_data"}, None),
        ({"status": "success", "data": None}, None),
        ({"status": "success", "data": {"resultType": "vector",
                                        "result": [{"metric": {}, "value": [1, "NaN"]}]}}, None),
    ], ids=["transport", "status-error", "data-null", "count-not-finite"])
    def test_verify_health_unanswered_is_caller_error(self, monkeypatch, body, err):
        """#3133: an answer that could not be read used to read as "no
        threshold metrics" — a cutover finding (exit 1) instead of exit 2."""
        self._answer(monkeypatch, body, err)
        ok, msg = ct.verify_health("t1", "http://prom:9090")
        assert not ok and msg.startswith(ct.CALLER_ERROR_PREFIX)


# ---------------------------------------------------------------------------
# TestApplyCutover
# ---------------------------------------------------------------------------

class TestApplyCutover:
    """apply_cutover() integration tests。"""

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "healthy"))
    def test_all_steps_succeed(self, _vh, _kube):
        """所有步驟成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            report = ct.apply_cutover(_readiness(), "db-a", "http://prom:9090")
            assert report["success"]
            assert len(report["steps_completed"]) == 5
            assert report["failed_step"] is None

    def test_not_ready_without_force(self):
        """未準備好時不強制執行。"""
        with tempfile.TemporaryDirectory() as tmp:
            report = ct.apply_cutover(_readiness(ready=False, pct=60.0), "db-a",
                                      "http://prom:9090")
            assert not report["success"]
            assert report["failed_step"] == "readiness_check"
            assert "--force" in report["message"]

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_force_overrides_not_ready(self, _vh, _kube):
        """--force 覆蓋未準備好的檢查。"""
        with tempfile.TemporaryDirectory() as tmp:
            report = ct.apply_cutover(_readiness(ready=False, pct=60.0), "db-a",
                                      "http://prom:9090", force=True)
            assert report["success"]

    @mock.patch("cutover_tenant._run_kubectl",
                side_effect=[(True, "ok"), (False, "permission denied")])
    def test_fails_at_second_step(self, _kube):
        """在第二步失敗。"""
        with tempfile.TemporaryDirectory() as tmp:
            report = ct.apply_cutover(_readiness(), "db-a", "http://prom:9090")
            assert not report["success"]
            assert len(report["steps_completed"]) == 1
            assert report["failed_step"] == "Remove old Recording Rules"

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_dry_run(self, _vh, _kube):
        """Dry-run 模式。"""
        with tempfile.TemporaryDirectory() as tmp:
            report = ct.apply_cutover(_readiness(), "db-a", "http://prom:9090",
                                      dry_run=True)
            assert report["success"]

    def test_readiness_without_its_fields(self):
        """就緒檔缺欄位：caller error 類的 load_readiness 失敗（缺檔改由
        `TestReadinessFileIsReadByTheLib` 在步驟前判）。"""
        report = ct.apply_cutover({"ready": True}, "db-a", "http://prom:9090")
        assert not report["success"]
        assert report["failed_step"] == "load_readiness"
        assert report["caller_error"]


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    """CLI argument parsing tests。"""

    def test_parser_required_args(self, monkeypatch):
        """必要引數正確解析；--prometheus 未設 env 時 fallback localhost（byte-identical）。"""
        # #3528: $PROMETHEUS_URL is no longer the argparse default; main()
        # resolves it with `_lib_io.prometheus_url` where it is used, so the
        # URL cutover sends to is prometheus_url(args.prometheus).
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-a",
        ])
        assert args.readiness_json == "r.json"
        assert args.tenant == "db-a"
        assert args.prometheus is None
        assert ct.prometheus_url(args.prometheus) == "http://localhost:9090"
        assert not args.dry_run
        assert not args.force

    def test_parser_prometheus_env_fallback(self, monkeypatch):
        """--prometheus 省略時讀 $PROMETHEUS_URL（README §6.1 全域 fallback）。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-a",
        ])
        assert ct.prometheus_url(args.prometheus) == "http://test:1234"

    def test_parser_prometheus_flag_overrides_env(self, monkeypatch):
        """顯式 --prometheus 蓋過 $PROMETHEUS_URL。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-a",
            "--prometheus", "http://cli:9099",
        ])
        assert args.prometheus == "http://cli:9099"

    def test_parser_all_flags(self):
        """所有旗標正確解析。"""
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-b",
            "--prometheus", "http://prom:9090",
            "--namespace", "custom-ns",
            "--dry-run",
            "--force",
            "--json-output",
        ])
        assert args.dry_run
        assert args.force
        assert args.json_output
        assert args.namespace == "custom-ns"

    def test_parser_missing_required(self):
        """缺失必要引數時退出。"""
        parser = ct.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ---------------------------------------------------------------------------
# TestEntrypointIntegration
# ---------------------------------------------------------------------------

class TestEntrypointIntegration:
    """Verify cutover is registered in da-tools entrypoint。"""

    def test_command_map_has_cutover(self):
        """entrypoint.py 中包含 cutover 命令。"""
        ep_path = os.path.join(
            os.path.dirname(__file__), "..", "components",
            "da-tools", "app", "entrypoint.py",
        )
        if not os.path.isfile(ep_path):
            pytest.skip("entrypoint.py not found")
        with open(ep_path, encoding="utf-8") as fh:
            content = fh.read()
        assert '"cutover"' in content
        assert "cutover_tenant.py" in content

    def test_prometheus_flag_declares_its_env_fallback(self):
        """#3528：--prometheus 可省略（$PROMETHEUS_URL 後備）由 parser 自己宣告
        （add_prometheus_arg 標在 action 上），不再靠 entrypoint 的 PROMETHEUS_COMMANDS。"""
        action = next(a for a in ct.build_parser()._actions
                      if "--prometheus" in a.option_strings)
        assert action.env_fallback == "PROMETHEUS_URL" and not action.required
