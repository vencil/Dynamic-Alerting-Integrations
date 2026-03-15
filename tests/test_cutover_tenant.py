"""pytest style tests for cutover_tenant.py — Shadow Monitoring one-command cutover."""

import json
import os
import subprocess
import tempfile
from unittest import mock

import pytest

import cutover_tenant as ct


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_readiness(tmp, ready=True, pct=100.0, converged=5, total=5):
    """Write a cutover-readiness.json and return its path."""
    data = {
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
    path = os.path.join(tmp, "cutover-readiness.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)
    return path


# ---------------------------------------------------------------------------
# TestLoadCutoverReadiness
# ---------------------------------------------------------------------------

class TestLoadCutoverReadiness:
    """load_cutover_readiness() tests。"""

    def test_valid_json(self):
        """有效 JSON 檔案正確載入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            data = ct.load_cutover_readiness(path)
            assert data["ready"]
            assert data["convergence_percentage"] == 100.0

    def test_not_ready(self):
        """未準備好的狀態正確解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            data = ct.load_cutover_readiness(path)
            assert not data["ready"]

    def test_missing_fields(self):
        """缺失必要欄位拋出 ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"ready": True}, fh)
            with pytest.raises(ValueError) as exc_info:
                ct.load_cutover_readiness(path)
            assert "Missing required fields" in str(exc_info.value)

    def test_invalid_json(self):
        """無效 JSON 拋出 JSONDecodeError。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json")
            with pytest.raises(json.JSONDecodeError):
                ct.load_cutover_readiness(path)

    def test_file_not_found(self):
        """檔案不存在拋出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            ct.load_cutover_readiness("/nonexistent/path.json")


# ---------------------------------------------------------------------------
# TestRunKubectl
# ---------------------------------------------------------------------------

class TestRunKubectl:
    """_run_kubectl() tests。"""

    def test_dry_run(self):
        """Dry-run 模式返回 (dry-run) 消息。"""
        ok, msg = ct._run_kubectl(["get", "pods"], dry_run=True)
        assert ok
        assert msg == "(dry-run)"

    @mock.patch("subprocess.run")
    def test_success(self, mock_run):
        """成功執行返回 True 和輸出。"""
        mock_run.return_value = mock.Mock(
            returncode=0, stdout="deleted\n", stderr="",
        )
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert ok
        assert msg == "deleted"

    @mock.patch("subprocess.run")
    def test_failure(self, mock_run):
        """失敗執行返回 False 和錯誤訊息。"""
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="not found",
        )
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        assert not ok
        assert "not found" in msg

    @mock.patch("subprocess.run", side_effect=FileNotFoundError)
    def test_kubectl_not_found(self, _mock):
        """kubectl 不存在時報告錯誤。"""
        ok, msg = ct._run_kubectl(["get", "pods"])
        assert not ok
        assert "kubectl not found" in msg

    @mock.patch("subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30))
    def test_kubectl_timeout(self, _mock):
        """kubectl 超時時報告錯誤。"""
        ok, msg = ct._run_kubectl(["get", "pods"])
        assert not ok
        assert "timed out" in msg


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
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            assert report["success"]
            assert len(report["steps_completed"]) == 5
            assert report["failed_step"] is None

    def test_not_ready_without_force(self):
        """未準備好時不強制執行。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            assert not report["success"]
            assert report["failed_step"] == "readiness_check"
            assert "--force" in report["message"]

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_force_overrides_not_ready(self, _vh, _kube):
        """--force 覆蓋未準備好的檢查。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090",
                                      force=True)
            assert report["success"]

    @mock.patch("cutover_tenant._run_kubectl",
                side_effect=[(True, "ok"), (False, "permission denied")])
    def test_fails_at_second_step(self, _kube):
        """在第二步失敗。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            assert not report["success"]
            assert len(report["steps_completed"]) == 1
            assert report["failed_step"] == "Remove old Recording Rules"

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_dry_run(self, _vh, _kube):
        """Dry-run 模式。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090",
                                      dry_run=True)
            assert report["success"]

    def test_missing_readiness_file(self):
        """缺失就緒檔案。"""
        report = ct.apply_cutover("/no/such/file.json", "db-a",
                                  "http://prom:9090")
        assert not report["success"]
        assert report["failed_step"] == "load_readiness"


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    """CLI argument parsing tests。"""

    def test_parser_required_args(self):
        """必要引數正確解析。"""
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-a",
        ])
        assert args.readiness_json == "r.json"
        assert args.tenant == "db-a"
        assert args.prometheus == "http://localhost:9090"
        assert not args.dry_run
        assert not args.force

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

    def test_prometheus_commands_has_cutover(self):
        """PROMETHEUS_COMMANDS 包含 cutover 命令。"""
        ep_path = os.path.join(
            os.path.dirname(__file__), "..", "components",
            "da-tools", "app", "entrypoint.py",
        )
        if not os.path.isfile(ep_path):
            pytest.skip("entrypoint.py not found")
        with open(ep_path, encoding="utf-8") as fh:
            content = fh.read()
        # PROMETHEUS_COMMANDS should contain "cutover"
        assert '"cutover"' in content
