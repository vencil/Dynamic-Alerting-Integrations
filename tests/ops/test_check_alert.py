"""Tests for check_alert.py — Prometheus alert state query.

Audit flagged this as a 0% covered tool. Tests stub http_get_json so
no real Prometheus is contacted.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import check_alert as ca  # noqa: E402


def _stub_http(monkeypatch, payload, err=None):
    """Replace _lib_python.http_get_json behind ca's import."""
    import _lib_python as lib

    def fake(*args, **kwargs):
        return payload, err
    monkeypatch.setattr(lib, "http_get_json", fake)
    monkeypatch.setattr(ca, "http_get_json", fake)


class TestCheckAlert:
    def test_inactive_when_no_matching_alerts(self, monkeypatch, capsys):
        _stub_http(monkeypatch, {"data": {"alerts": []}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["state"] == "inactive"
        assert payload["alert"] == "HighCPU"
        assert payload["tenant"] == "db-a"

    def test_inactive_when_alertname_does_not_match(self, monkeypatch, capsys):
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "OtherAlert", "tenant": "db-a"}, "state": "firing"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        out = capsys.readouterr().out
        assert json.loads(out)["state"] == "inactive"

    def test_firing_when_tenant_label_matches(self, monkeypatch, capsys):
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "HighCPU", "tenant": "db-a"},
             "state": "firing", "activeAt": "2026-05-07T10:00:00Z"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "firing"
        assert payload["details"][0]["state"] == "firing"

    def test_firing_via_instance_label_fallback(self, monkeypatch, capsys):
        # tenant label absent but instance matches.
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "HighCPU", "instance": "db-a"},
             "state": "firing", "activeAt": "2026-05-07T10:00:00Z"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        assert json.loads(capsys.readouterr().out)["state"] == "firing"

    def test_pending_when_only_pending_matches(self, monkeypatch, capsys):
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "HighCPU", "tenant": "db-a"}, "state": "pending"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        assert json.loads(capsys.readouterr().out)["state"] == "pending"

    def test_firing_takes_priority_over_pending(self, monkeypatch, capsys):
        # firing > pending — even if pending appears first.
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "HighCPU", "tenant": "db-a"}, "state": "pending"},
            {"labels": {"alertname": "HighCPU", "tenant": "db-a"}, "state": "firing"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        assert json.loads(capsys.readouterr().out)["state"] == "firing"

    def test_unknown_when_no_firing_or_pending(self, monkeypatch, capsys):
        # Match exists but state is neither firing nor pending — falls
        # through the elif/else ladder to "unknown".
        _stub_http(monkeypatch, {"data": {"alerts": [
            {"labels": {"alertname": "HighCPU", "tenant": "db-a"}, "state": "resolved"},
        ]}})
        ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        assert json.loads(capsys.readouterr().out)["state"] == "unknown"

    def test_http_error_exits_caller_error(self, monkeypatch, capsys):
        _stub_http(monkeypatch, None, err="connection refused")
        with pytest.raises(SystemExit) as exc:
            ca.check_alert("HighCPU", "db-a", "http://localhost:9090")
        assert exc.value.code == 2  # EXIT_CALLER_ERROR (#452: cannot reach Prometheus)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "error" in payload
        assert "connection refused" in payload["error"]
