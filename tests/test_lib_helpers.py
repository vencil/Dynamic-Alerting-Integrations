"""
tests/test_lib_helpers.py — Unit tests for Python snippets used in scripts/_lib.sh
Tests the pure-Python logic embedded in shell functions:
  - url_encode (urllib.parse.quote via stdin)
  - prom_query_value response parsing
  - get_alert_status response parsing
  - get_cm_value YAML parsing logic
  - get_exporter_metric regex extraction
"""

import json
import os
import re
import subprocess
import sys
import unittest
import urllib.parse

import yaml


# ---------------------------------------------------------------------------
# Helper: run an inline Python snippet the same way _lib.sh does
# ---------------------------------------------------------------------------
def run_python_snippet(code: str, stdin_data: str = "") -> str:
    """Execute a Python3 snippet and return stripped stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


# ===================================================================
# 1. url_encode — urllib.parse.quote via stdin
# ===================================================================
class TestUrlEncode(unittest.TestCase):
    """Verify the stdin-based url_encode approach used in _lib.sh."""

    _SNIPPET = (
        "import sys, urllib.parse; "
        "print(urllib.parse.quote(sys.stdin.read().strip()))"
    )

    def _encode(self, text: str) -> str:
        return run_python_snippet(self._SNIPPET, text)

    def test_simple_metric(self):
        self.assertEqual(self._encode("mysql_up"), "mysql_up")

    def test_spaces(self):
        self.assertEqual(self._encode("a b"), "a%20b")

    def test_curly_braces(self):
        expr = 'mysql_up{job="foo"}'
        encoded = self._encode(expr)
        self.assertIn("%7B", encoded)
        self.assertIn("%7D", encoded)
        # round-trip
        self.assertEqual(urllib.parse.unquote(encoded), expr)

    def test_single_quote_safe(self):
        """Ensure single quotes don't break (was the A1 finding)."""
        expr = "rate(m{l='v'}[5m])"
        encoded = self._encode(expr)
        self.assertEqual(urllib.parse.unquote(encoded), expr)

    def test_empty_string(self):
        self.assertEqual(self._encode(""), "")

    def test_complex_promql(self):
        expr = 'sum(rate(http_requests_total{method="GET"}[5m])) by (status)'
        encoded = self._encode(expr)
        self.assertEqual(urllib.parse.unquote(encoded), expr)


# ===================================================================
# 2. prom_query_value — JSON response parsing
# ===================================================================
class TestPromQueryValueParsing(unittest.TestCase):
    """Verify the Python snippet used in prom_query_value()."""

    def _parse(self, json_data: str, default: str = "N/A") -> str:
        snippet = f"""
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
    print(r[0]['value'][1] if r else '{default}')
except:
    print('{default}')
"""
        return run_python_snippet(snippet, json_data)

    def test_normal_result(self):
        data = {"data": {"result": [{"value": [1234567890, "42"]}]}}
        self.assertEqual(self._parse(json.dumps(data)), "42")

    def test_empty_result(self):
        data = {"data": {"result": []}}
        self.assertEqual(self._parse(json.dumps(data)), "N/A")

    def test_malformed_json(self):
        self.assertEqual(self._parse("{bad json"), "N/A")

    def test_missing_data_key(self):
        self.assertEqual(self._parse(json.dumps({"status": "success"})), "N/A")

    def test_custom_default(self):
        data = {"data": {"result": []}}
        self.assertEqual(self._parse(json.dumps(data), default="0"), "0")

    def test_float_value(self):
        data = {"data": {"result": [{"value": [0, "3.14159"]}]}}
        self.assertEqual(self._parse(json.dumps(data)), "3.14159")


# ===================================================================
# 3. get_alert_status — alert state extraction
# ===================================================================
class TestGetAlertStatusParsing(unittest.TestCase):
    """Verify the Python snippet used in get_alert_status()."""

    def _parse(self, json_data: str, alertname: str, tenant: str) -> str:
        snippet = f"""
import sys, json
try:
    data = json.load(sys.stdin)
    alerts = [a for a in data['data']['alerts']
              if a.get('labels',{{}}).get('alertname') == '{alertname}'
              and '{tenant}' in str(a)]
    if any(a['state'] == 'firing' for a in alerts):
        print('firing')
    elif any(a['state'] == 'pending' for a in alerts):
        print('pending')
    else:
        print('inactive')
except:
    print('unknown')
"""
        return run_python_snippet(snippet, json_data)

    def _make_alerts(self, *alert_defs):
        alerts = []
        for name, tenant, state in alert_defs:
            alerts.append({
                "labels": {"alertname": name, "tenant": tenant},
                "state": state,
            })
        return json.dumps({"data": {"alerts": alerts}})

    def test_firing(self):
        data = self._make_alerts(("MariaDBDown", "db-a", "firing"))
        self.assertEqual(self._parse(data, "MariaDBDown", "db-a"), "firing")

    def test_pending(self):
        data = self._make_alerts(("MariaDBDown", "db-a", "pending"))
        self.assertEqual(self._parse(data, "MariaDBDown", "db-a"), "pending")

    def test_inactive_no_match(self):
        data = self._make_alerts(("OtherAlert", "db-b", "firing"))
        self.assertEqual(self._parse(data, "MariaDBDown", "db-a"), "inactive")

    def test_firing_takes_precedence(self):
        data = self._make_alerts(
            ("MariaDBDown", "db-a", "pending"),
            ("MariaDBDown", "db-a", "firing"),
        )
        self.assertEqual(self._parse(data, "MariaDBDown", "db-a"), "firing")

    def test_empty_alerts(self):
        data = json.dumps({"data": {"alerts": []}})
        self.assertEqual(self._parse(data, "MariaDBDown", "db-a"), "inactive")

    def test_malformed_json(self):
        self.assertEqual(self._parse("{bad", "X", "Y"), "unknown")


# ===================================================================
# 4. get_cm_value — ConfigMap YAML parsing logic
# ===================================================================
class TestGetCmValueParsing(unittest.TestCase):
    """Verify the inline Python used in get_cm_value()."""

    def _parse(self, cm_json: str, tenant: str, key: str) -> str:
        snippet = f"""
import sys, json, yaml
cm = json.load(sys.stdin)
data = cm.get('data', {{}})
tenant_key = '{tenant}.yaml'
if '_defaults.yaml' in data and tenant_key in data:
    tc = yaml.safe_load(data[tenant_key]) or {{}}
    val = tc.get('tenants', {{}}).get('{tenant}', {{}}).get('{key}', 'default')
elif 'config.yaml' in data:
    c = yaml.safe_load(data['config.yaml']) or {{}}
    val = c.get('tenants', {{}}).get('{tenant}', {{}}).get('{key}', 'default')
else:
    val = 'default'
print(val)
"""
        return run_python_snippet(snippet, cm_json)

    def test_per_tenant_yaml(self):
        tenant_cfg = yaml.dump({"tenants": {"db-a": {"mysql_connections": 70}}})
        cm = {"data": {"_defaults.yaml": "defaults: {}", "db-a.yaml": tenant_cfg}}
        result = self._parse(json.dumps(cm), "db-a", "mysql_connections")
        self.assertEqual(result, "70")

    def test_config_yaml_fallback(self):
        config = yaml.dump({"tenants": {"db-a": {"container_cpu": 80}}})
        cm = {"data": {"config.yaml": config}}
        result = self._parse(json.dumps(cm), "db-a", "container_cpu")
        self.assertEqual(result, "80")

    def test_missing_key_returns_default(self):
        tenant_cfg = yaml.dump({"tenants": {"db-a": {}}})
        cm = {"data": {"_defaults.yaml": "x: 1", "db-a.yaml": tenant_cfg}}
        result = self._parse(json.dumps(cm), "db-a", "nonexistent")
        self.assertEqual(result, "default")

    def test_empty_data(self):
        cm = {"data": {}}
        result = self._parse(json.dumps(cm), "db-a", "mysql_connections")
        self.assertEqual(result, "default")


# ===================================================================
# 5. get_exporter_metric — regex value extraction
# ===================================================================
class TestGetExporterMetricRegex(unittest.TestCase):
    """Verify the grep -oP pattern used in get_exporter_metric()."""

    PATTERN = r'\d+\.?\d*$'

    def _extract(self, line: str) -> str:
        m = re.search(self.PATTERN, line)
        return m.group(0) if m else ""

    def test_integer_value(self):
        line = 'user_threshold{tenant="db-a",metric="connections",severity="warning"} 70'
        self.assertEqual(self._extract(line), "70")

    def test_float_value(self):
        line = 'user_threshold{tenant="db-a",metric="cpu",severity="warning"} 70.5'
        self.assertEqual(self._extract(line), "70.5")

    def test_no_value(self):
        line = 'user_threshold{tenant="db-a"}'
        self.assertEqual(self._extract(line), "")

    def test_zero_value(self):
        line = 'user_state_filter{tenant="db-a",filter="maintenance"} 0'
        self.assertEqual(self._extract(line), "0")

    def test_large_number(self):
        line = 'go_memstats_alloc_bytes{instance="exporter:8080"} 1234567890'
        self.assertEqual(self._extract(line), "1234567890")


# ===================================================================
# 6. Structural checks
# ===================================================================
class TestLibShStructure(unittest.TestCase):
    """Verify _lib.sh structural properties."""

    @classmethod
    def setUpClass(cls):
        repo_root = os.path.join(os.path.dirname(__file__), os.pardir)
        cls.lib_path = os.path.join(repo_root, "scripts", "_lib.sh")

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(self.lib_path))

    def test_shebang(self):
        with open(self.lib_path, encoding="utf-8") as f:
            first_line = f.readline().strip()
        self.assertEqual(first_line, "#!/bin/bash")

    def test_exported_functions_present(self):
        """All documented functions exist in _lib.sh."""
        with open(self.lib_path, encoding="utf-8") as f:
            content = f.read()
        expected_functions = [
            "log", "warn", "err", "info",
            "setup_port_forwards", "cleanup_port_forwards",
            "prom_query_value", "get_alert_status", "wait_for_alert",
            "get_exporter_metric", "wait_exporter",
            "require_services",
            "url_encode", "kill_port", "get_cm_value",
            "ensure_kubeconfig", "preflight_check",
        ]
        for fn in expected_functions:
            self.assertIn(
                f"{fn}()",
                content,
                f"Function {fn}() not found in _lib.sh",
            )

    def test_url_encode_uses_stdin(self):
        """Verify url_encode passes input via stdin (A1 fix)."""
        with open(self.lib_path, encoding="utf-8") as f:
            content = f.read()
        # Should use stdin pattern, not inline $1
        self.assertIn("sys.stdin.read()", content)


class TestScenarioScriptsSourceLib(unittest.TestCase):
    """Verify all scenario scripts source _lib.sh."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.join(os.path.dirname(__file__), os.pardir)

    def test_all_scenarios_source_lib(self):
        scenarios = ["scenario-a.sh", "scenario-b.sh", "scenario-c.sh", "scenario-d.sh"]
        for name in scenarios:
            path = os.path.join(self.repo_root, "tests", name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn(
                "source", content,
                f"{name} does not source any library",
            )
            self.assertIn(
                "_lib.sh", content,
                f"{name} does not source _lib.sh",
            )

    def test_all_scenarios_set_pipefail(self):
        scenarios = ["scenario-a.sh", "scenario-b.sh", "scenario-c.sh", "scenario-d.sh"]
        for name in scenarios:
            path = os.path.join(self.repo_root, "tests", name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn(
                "set -euo pipefail", content,
                f"{name} missing set -euo pipefail",
            )

    def test_all_scenarios_have_trap(self):
        scenarios = ["scenario-a.sh", "scenario-b.sh", "scenario-c.sh", "scenario-d.sh"]
        for name in scenarios:
            path = os.path.join(self.repo_root, "tests", name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn(
                "trap cleanup EXIT", content,
                f"{name} missing trap cleanup EXIT",
            )


if __name__ == "__main__":
    unittest.main()
