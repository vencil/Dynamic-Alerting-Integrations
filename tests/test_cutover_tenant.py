"""Tests for cutover_tenant.py — Shadow Monitoring one-command cutover."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock


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

class TestLoadCutoverReadiness(unittest.TestCase):
    """load_cutover_readiness() tests."""

    def test_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            data = ct.load_cutover_readiness(path)
            self.assertTrue(data["ready"])
            self.assertEqual(data["convergence_percentage"], 100.0)

    def test_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            data = ct.load_cutover_readiness(path)
            self.assertFalse(data["ready"])

    def test_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"ready": True}, fh)
            with self.assertRaises(ValueError) as ctx:
                ct.load_cutover_readiness(path)
            self.assertIn("Missing required fields", str(ctx.exception))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json")
            with self.assertRaises(json.JSONDecodeError):
                ct.load_cutover_readiness(path)

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            ct.load_cutover_readiness("/nonexistent/path.json")


# ---------------------------------------------------------------------------
# TestRunKubectl
# ---------------------------------------------------------------------------

class TestRunKubectl(unittest.TestCase):
    """_run_kubectl() tests."""

    def test_dry_run(self):
        ok, msg = ct._run_kubectl(["get", "pods"], dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(msg, "(dry-run)")

    @mock.patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=0, stdout="deleted\n", stderr="",
        )
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        self.assertTrue(ok)
        self.assertEqual(msg, "deleted")

    @mock.patch("subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="not found",
        )
        ok, msg = ct._run_kubectl(["delete", "job", "shadow-monitor"])
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    @mock.patch("subprocess.run", side_effect=FileNotFoundError)
    def test_kubectl_not_found(self, _mock):
        ok, msg = ct._run_kubectl(["get", "pods"])
        self.assertFalse(ok)
        self.assertIn("kubectl not found", msg)

    @mock.patch("subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30))
    def test_kubectl_timeout(self, _mock):
        ok, msg = ct._run_kubectl(["get", "pods"])
        self.assertFalse(ok)
        self.assertIn("timed out", msg)


# ---------------------------------------------------------------------------
# TestStepFunctions
# ---------------------------------------------------------------------------

class TestStepFunctions(unittest.TestCase):
    """Individual step function tests."""

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "deleted"))
    def test_stop_shadow_job(self, mock_kube):
        ok, msg = ct.stop_shadow_job(namespace="monitoring")
        self.assertTrue(ok)
        mock_kube.assert_called_once()
        args = mock_kube.call_args[0][0]
        self.assertIn("shadow-monitor", args)

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "deleted"))
    def test_remove_old_rules(self, mock_kube):
        ok, msg = ct.remove_old_rules(configmap="my-cm")
        self.assertTrue(ok)

    @mock.patch("cutover_tenant._run_kubectl",
                return_value=(False, "not labeled"))
    def test_remove_shadow_label_already_absent(self, _mock):
        ok, msg = ct.remove_shadow_label()
        self.assertTrue(ok)
        self.assertIn("already absent", msg)

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "labeled"))
    def test_remove_shadow_route(self, _mock):
        ok, msg = ct.remove_shadow_route()
        self.assertTrue(ok)

    def test_verify_health_dry_run(self):
        ok, msg = ct.verify_health("db-a", "http://localhost:9090",
                                   dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(msg, "(dry-run)")


# ---------------------------------------------------------------------------
# TestApplyCutover
# ---------------------------------------------------------------------------

class TestApplyCutover(unittest.TestCase):
    """apply_cutover() integration tests."""

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "healthy"))
    def test_all_steps_succeed(self, _vh, _kube):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            self.assertTrue(report["success"])
            self.assertEqual(len(report["steps_completed"]), 5)
            self.assertIsNone(report["failed_step"])

    def test_not_ready_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            self.assertFalse(report["success"])
            self.assertEqual(report["failed_step"], "readiness_check")
            self.assertIn("--force", report["message"])

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_force_overrides_not_ready(self, _vh, _kube):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp, ready=False, pct=60.0)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090",
                                      force=True)
            self.assertTrue(report["success"])

    @mock.patch("cutover_tenant._run_kubectl",
                side_effect=[(True, "ok"), (False, "permission denied")])
    def test_fails_at_second_step(self, _kube):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090")
            self.assertFalse(report["success"])
            self.assertEqual(len(report["steps_completed"]), 1)
            self.assertEqual(report["failed_step"],
                             "Remove old Recording Rules")

    @mock.patch("cutover_tenant._run_kubectl", return_value=(True, "ok"))
    @mock.patch("cutover_tenant.verify_health", return_value=(True, "ok"))
    def test_dry_run(self, _vh, _kube):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_readiness(tmp)
            report = ct.apply_cutover(path, "db-a", "http://prom:9090",
                                      dry_run=True)
            self.assertTrue(report["success"])

    def test_missing_readiness_file(self):
        report = ct.apply_cutover("/no/such/file.json", "db-a",
                                  "http://prom:9090")
        self.assertFalse(report["success"])
        self.assertEqual(report["failed_step"], "load_readiness")


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):
    """CLI argument parsing tests."""

    def test_parser_required_args(self):
        parser = ct.build_parser()
        args = parser.parse_args([
            "--readiness-json", "r.json",
            "--tenant", "db-a",
        ])
        self.assertEqual(args.readiness_json, "r.json")
        self.assertEqual(args.tenant, "db-a")
        self.assertEqual(args.prometheus, "http://localhost:9090")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.force)

    def test_parser_all_flags(self):
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
        self.assertTrue(args.dry_run)
        self.assertTrue(args.force)
        self.assertTrue(args.json_output)
        self.assertEqual(args.namespace, "custom-ns")

    def test_parser_missing_required(self):
        parser = ct.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])


# ---------------------------------------------------------------------------
# TestEntrypointIntegration
# ---------------------------------------------------------------------------

class TestEntrypointIntegration(unittest.TestCase):
    """Verify cutover is registered in da-tools entrypoint."""

    def test_command_map_has_cutover(self):
        ep_path = os.path.join(
            os.path.dirname(__file__), "..", "components",
            "da-tools", "app", "entrypoint.py",
        )
        if not os.path.isfile(ep_path):
            self.skipTest("entrypoint.py not found")
        with open(ep_path, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn('"cutover"', content)
        self.assertIn("cutover_tenant.py", content)

    def test_prometheus_commands_has_cutover(self):
        ep_path = os.path.join(
            os.path.dirname(__file__), "..", "components",
            "da-tools", "app", "entrypoint.py",
        )
        if not os.path.isfile(ep_path):
            self.skipTest("entrypoint.py not found")
        with open(ep_path, encoding="utf-8") as fh:
            content = fh.read()
        # PROMETHEUS_COMMANDS should contain "cutover"
        self.assertIn('"cutover"', content)


if __name__ == "__main__":
    unittest.main()
