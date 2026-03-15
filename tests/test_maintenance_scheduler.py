#!/usr/bin/env python3
"""test_maintenance_scheduler.py — Recurring Maintenance Scheduler 測試套件。

Wave 12 pytest 遷移。
"""

import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import maintenance_scheduler as ms  # noqa: E402
from factories import mock_http_response, write_yaml  # noqa: E402


# ── 1. parse_duration ─────────────────────────────────────────────

class TestParseDuration:
    """Test Go-style duration string parsing."""

    @pytest.mark.parametrize("duration_str,expected", [
        ("4h", timedelta(hours=4)),
        ("30m", timedelta(minutes=30)),
        ("90s", timedelta(seconds=90)),
        ("2h30m", timedelta(hours=2, minutes=30)),
        ("1h15m30s", timedelta(hours=1, minutes=15, seconds=30)),
        ("60", timedelta(minutes=60)),
        ("1d", timedelta(days=1)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("2d", timedelta(days=2)),
    ], ids=["hours", "minutes", "seconds", "composite-hm", "composite-hms",
            "integer-fallback", "days", "days-hours", "multi-days"])
    def test_valid_duration(self, duration_str, expected):
        """各種合法 duration 字串正確解析。"""
        assert ms.parse_duration(duration_str) == expected

    @pytest.mark.parametrize("duration_str", ["abc", "0h"],
                             ids=["invalid", "zero-units"])
    def test_invalid_returns_none(self, duration_str):
        """無效 duration 字串回傳 None。"""
        assert ms.parse_duration(duration_str) is None


# ── 2. is_in_window ──────────────────────────────────────────────

class TestIsInWindow:
    """Test cron-based maintenance window detection."""

    def test_inside_window(self):
        """now=02:30 UTC, cron triggers every hour, duration=4h → in window."""
        now = datetime(2025, 6, 15, 2, 30, tzinfo=timezone.utc)
        in_w, start, end = ms.is_in_window("0 * * * *", "4h", now=now)
        assert in_w
        assert start.hour == 2
        assert end.hour == 6

    def test_outside_window(self):
        """now=10:00 UTC, cron at 03:00 daily, duration=1h → not in window."""
        now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        in_w, start, end = ms.is_in_window("0 3 * * *", "1h", now=now)
        assert not in_w
        assert start is None
        assert end is None

    def test_at_boundary(self):
        """now exactly at window end → still in window (<=)."""
        now = datetime(2025, 6, 15, 4, 0, tzinfo=timezone.utc)
        in_w, start, end = ms.is_in_window("0 3 * * *", "1h", now=now)
        assert in_w

    def test_weekly_cron(self):
        """Weekly maintenance: every Sunday at 02:00, 4h duration."""
        # 2025-06-15 is a Sunday
        now = datetime(2025, 6, 15, 3, 0, tzinfo=timezone.utc)
        in_w, start, end = ms.is_in_window("0 2 * * 0", "4h", now=now)
        assert in_w

    def test_invalid_duration(self):
        now = datetime(2025, 6, 15, 3, 0, tzinfo=timezone.utc)
        in_w, start, end = ms.is_in_window("0 * * * *", "xyz", now=now)
        assert not in_w


# ── 3. load_recurring_schedules ───────────────────────────────────

class TestLoadRecurringSchedules:
    """Test loading tenant recurring schedules from YAML files."""

    def test_basic_load(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          duration: "4h"
          reason: "Weekly backup"
""")
            schedules = ms.load_recurring_schedules(d)
            assert "db-a" in schedules
            assert len(schedules["db-a"]) == 1
            assert schedules["db-a"][0]["cron"] == "0 2 * * 0"
            assert schedules["db-a"][0]["duration"] == "4h"
            assert schedules["db-a"][0]["reason"] == "Weekly backup"

    def test_multiple_schedules(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          duration: "4h"
          reason: "Weekly backup"
        - cron: "0 3 * * 1-5"
          duration: "30m"
          reason: "Weekday maintenance"
""")
            schedules = ms.load_recurring_schedules(d)
            assert len(schedules["db-a"]) == 2

    def test_skip_hidden_files(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          duration: "4h"
""")
            schedules = ms.load_recurring_schedules(d)
            assert schedules == {}

    def test_skip_missing_cron(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - duration: "4h"
          reason: "no cron"
""")
            schedules = ms.load_recurring_schedules(d)
            assert schedules == {}

    def test_skip_missing_duration(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          reason: "no duration"
""")
            schedules = ms.load_recurring_schedules(d)
            assert schedules == {}

    def test_default_reason(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          duration: "4h"
""")
            schedules = ms.load_recurring_schedules(d)
            assert schedules["db-a"][0]["reason"] == "Recurring maintenance"

    def test_no_recurring_key(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      target: all
""")
            schedules = ms.load_recurring_schedules(d)
            assert schedules == {}

    def test_nonexistent_dir(self):
        schedules = ms.load_recurring_schedules("/nonexistent/path")
        assert schedules == {}

    def test_multi_tenant(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 2 * * 0"
          duration: "4h"
""")
            write_yaml(d, "db-b.yaml", """\
tenants:
  db-b:
    _state_maintenance:
      recurring:
        - cron: "0 3 * * 6"
          duration: "2h"
""")
            schedules = ms.load_recurring_schedules(d)
            assert "db-a" in schedules
            assert "db-b" in schedules


# ── 3b. _parse_iso ────────────────────────────────────────────────

class TestParseIso:
    """Test ISO 8601 datetime parsing helper."""

    def test_basic_utc(self):
        dt = ms._parse_iso("2025-06-15T06:00:00+00:00")
        assert dt == datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)

    def test_z_suffix(self):
        dt = ms._parse_iso("2025-06-15T06:00:00Z")
        assert dt == datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)

    def test_empty_string(self):
        assert ms._parse_iso("") is None

    def test_none_input(self):
        assert ms._parse_iso(None) is None

    def test_invalid_string(self):
        assert ms._parse_iso("not-a-date") is None


# ── 4. get_existing_silences ──────────────────────────────────────

class TestGetExistingSilences:
    """Test Alertmanager silence listing and filtering."""

    def _mock_silences(self):
        return [
            {
                "id": "abc-123",
                "status": {"state": "active"},
                "createdBy": ms.SILENCE_CREATOR,
                "comment": "Weekly backup",
                "endsAt": "2025-06-15T06:00:00Z",
                "matchers": [{"name": "tenant", "value": "db-a"}],
            },
            {
                "id": "def-456",
                "status": {"state": "expired"},
                "createdBy": ms.SILENCE_CREATOR,
                "comment": "Old",
                "endsAt": "2025-06-14T06:00:00Z",
                "matchers": [{"name": "tenant", "value": "db-b"}],
            },
            {
                "id": "ghi-789",
                "status": {"state": "active"},
                "createdBy": "manual",
                "comment": "Manual silence",
                "endsAt": "2025-06-15T08:00:00Z",
                "matchers": [{"name": "tenant", "value": "db-c"}],
            },
        ]

    @mock.patch.object(ms, "_api_request")
    def test_filters_active_and_creator(self, mock_api):
        mock_api.return_value = self._mock_silences()
        result = ms.get_existing_silences("http://alertmanager:9093")
        assert len(result) == 1
        assert ("db-a", "Weekly backup") in result
        info = result[("db-a", "Weekly backup")]
        assert info["id"] == "abc-123"
        assert info["endsAt"] == datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)

    @mock.patch.object(ms, "_api_request")
    def test_api_error_returns_empty(self, mock_api):
        mock_api.side_effect = Exception("connection refused")
        result = ms.get_existing_silences("http://alertmanager:9093")
        assert result == {}


# ── 5. create_silence ─────────────────────────────────────────────

class TestCreateSilence:
    """Test silence creation and dry-run mode."""

    @mock.patch.object(ms, "_api_request")
    def test_creates_silence(self, mock_api):
        mock_api.return_value = {"silenceID": "new-001"}
        ends = datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)
        sid = ms.create_silence("http://am:9093", "db-a", "backup", ends)
        assert sid == "new-001"
        mock_api.assert_called_once()
        call_args = mock_api.call_args
        assert "/api/v2/silences" in call_args[0][0]

    def test_dry_run_returns_none(self):
        ends = datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)
        sid = ms.create_silence("http://am:9093", "db-a", "backup", ends,
                                dry_run=True)
        assert sid is None

    @mock.patch.object(ms, "_api_request")
    def test_api_error_returns_none(self, mock_api):
        mock_api.side_effect = urllib.error.URLError("500 Internal Server Error")
        ends = datetime(2025, 6, 15, 6, 0, tzinfo=timezone.utc)
        sid = ms.create_silence("http://am:9093", "db-a", "backup", ends)
        assert sid is None


# ── 5b. extend_silence ───────────────────────────────────────────

class TestExtendSilence:
    """Test silence extension (self-healing)."""

    @mock.patch.object(ms, "_api_request")
    def test_extends_silence(self, mock_api):
        mock_api.return_value = {"silenceID": "abc-123"}
        ends = datetime(2025, 6, 15, 8, 0, tzinfo=timezone.utc)
        sid = ms.extend_silence("http://am:9093", "abc-123", "db-a", "backup", ends)
        assert sid == "abc-123"
        mock_api.assert_called_once()
        payload = mock_api.call_args[1]["payload"]
        assert payload["id"] == "abc-123"

    def test_dry_run_returns_id(self):
        ends = datetime(2025, 6, 15, 8, 0, tzinfo=timezone.utc)
        sid = ms.extend_silence("http://am:9093", "abc-123", "db-a", "backup",
                                ends, dry_run=True)
        assert sid == "abc-123"

    @mock.patch.object(ms, "_api_request")
    def test_api_error_returns_none(self, mock_api):
        mock_api.side_effect = urllib.error.URLError("503 Service Unavailable")
        ends = datetime(2025, 6, 15, 8, 0, tzinfo=timezone.utc)
        sid = ms.extend_silence("http://am:9093", "abc-123", "db-a", "backup", ends)
        assert sid is None


# ── 5c. push_metrics ─────────────────────────────────────────────

class TestPushMetrics:
    """Test Pushgateway metric push (observability)."""

    @mock.patch("maintenance_scheduler.urllib.request.urlopen")
    def test_push_success(self, mock_urlopen):
        mock_resp = mock_http_response(body=b"")
        mock_urlopen.return_value = mock_resp
        # Should not raise
        ms.push_metrics("http://pushgateway:9091", 2, 1, 0, 0.5)
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        data = call_args[1].get("data") or call_args[0][1]
        body = data.decode("utf-8")
        assert "# TYPE maintenance_scheduler_last_run_timestamp_seconds gauge" in body
        assert "maintenance_scheduler_silences_created 2" in body
        assert "maintenance_scheduler_errors 0" in body

    @mock.patch("maintenance_scheduler.urllib.request.urlopen")
    def test_push_failure_nonfatal(self, mock_urlopen):
        """Pushgateway failure should not raise."""
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        # Should NOT raise — just print warning
        ms.push_metrics("http://pushgateway:9091", 0, 0, 0, 0.1)


# ── 6. evaluate_and_apply (integration) ──────────────────────────

class TestEvaluateAndApply:
    """Test the main orchestration logic."""

    def test_no_schedules(self):
        with tempfile.TemporaryDirectory() as d:
            created, skipped, errors = ms.evaluate_and_apply(d, None)
            assert (created, skipped, errors) == (0, 0, 0)

    def test_active_window_report_only(self):
        """Without --alertmanager, just report active windows."""
        with tempfile.TemporaryDirectory() as d:
            # Use "every minute" cron so it's always in window
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, None, now=now)
            assert created == 1
            assert errors == 0

    def test_not_in_window_skips(self):
        """Window not active → nothing created."""
        with tempfile.TemporaryDirectory() as d:
            # Cron at 03:00 daily, duration 1h. now=10:00 → outside.
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "0 3 * * *"
          duration: "1h"
          reason: "Nightly"
""")
            now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, None, now=now)
            assert created == 0

    @mock.patch.object(ms, "get_existing_silences")
    @mock.patch.object(ms, "create_silence")
    def test_idempotency_skip(self, mock_create, mock_existing):
        """Existing silence with sufficient endsAt → skipped, not re-created."""
        # endsAt far in the future → no need to extend
        mock_existing.return_value = {
            ("db-a", "Always active"): {
                "id": "abc-123",
                "endsAt": datetime(2025, 6, 15, 23, 0, tzinfo=timezone.utc),
            },
        }
        mock_create.return_value = "new-001"

        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", now=now)
            assert skipped == 1
            assert created == 0
            mock_create.assert_not_called()

    @mock.patch.object(ms, "get_existing_silences")
    @mock.patch.object(ms, "create_silence")
    def test_creates_new_silence(self, mock_create, mock_existing):
        """No existing silence → creates new one."""
        mock_existing.return_value = {}
        mock_create.return_value = "new-001"

        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", now=now)
            assert created == 1
            assert errors == 0
            mock_create.assert_called_once()

    @mock.patch.object(ms, "get_existing_silences")
    @mock.patch.object(ms, "extend_silence")
    @mock.patch.object(ms, "create_silence")
    def test_extend_when_silence_expires_early(self, mock_create, mock_extend,
                                               mock_existing):
        """Existing silence endsAt < window end → extends instead of skip."""
        # Silence expires at 12:30, but window ends at 13:05 → extend
        mock_existing.return_value = {
            ("db-a", "Always active"): {
                "id": "abc-123",
                "endsAt": datetime(2025, 6, 15, 12, 30, tzinfo=timezone.utc),
            },
        }
        mock_extend.return_value = "abc-123"

        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", now=now)
            assert created == 1  # extended counts as created
            assert skipped == 0
            mock_extend.assert_called_once()
            mock_create.assert_not_called()

    @mock.patch.object(ms, "get_existing_silences")
    @mock.patch.object(ms, "extend_silence")
    @mock.patch.object(ms, "create_silence")
    def test_extend_failure_counts_error(self, mock_create, mock_extend,
                                         mock_existing):
        """extend_silence returns None → counts as error, not created."""
        mock_existing.return_value = {
            ("db-a", "Always active"): {
                "id": "abc-123",
                "endsAt": datetime(2025, 6, 15, 12, 30, tzinfo=timezone.utc),
            },
        }
        mock_extend.return_value = None  # extend failed

        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", now=now)
            assert created == 0
            assert errors == 1

    @mock.patch.object(ms, "get_existing_silences")
    @mock.patch.object(ms, "create_silence")
    def test_unparseable_ends_at_skips(self, mock_create, mock_existing):
        """endsAt=None (parse failure) → skip, don't extend."""
        mock_existing.return_value = {
            ("db-a", "Always active"): {
                "id": "abc-123",
                "endsAt": None,  # _parse_iso failed
            },
        }

        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", now=now)
            assert skipped == 1
            assert created == 0
            mock_create.assert_not_called()

    def test_dry_run_mode(self):
        """Dry run reports but doesn't create silences."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """\
tenants:
  db-a:
    _state_maintenance:
      recurring:
        - cron: "* * * * *"
          duration: "1h"
          reason: "Always active"
""")
            now = datetime(2025, 6, 15, 12, 5, tzinfo=timezone.utc)
            created, skipped, errors = ms.evaluate_and_apply(
                d, "http://am:9093", dry_run=True, now=now)
            assert created == 1
            assert errors == 0


# ── 7. _api_request retry ────────────────────────────────────────

class TestApiRequest:
    """Test HTTP retry logic."""

    @mock.patch("maintenance_scheduler.time.sleep")
    @mock.patch("maintenance_scheduler.urllib.request.urlopen")
    def test_4xx_not_retried(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = ms.urllib.error.HTTPError(
            "http://x", 400, "Bad Request", {}, None)
        with pytest.raises(ms.urllib.error.HTTPError) as exc_info:
            ms._api_request("http://x", max_retries=3)
        assert exc_info.value.code == 400
        mock_sleep.assert_not_called()

    @mock.patch("maintenance_scheduler.time.sleep")
    @mock.patch("maintenance_scheduler.urllib.request.urlopen")
    def test_5xx_retried(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = ms.urllib.error.HTTPError(
            "http://x", 503, "Service Unavailable", {}, None)
        with pytest.raises(ms.urllib.error.HTTPError):
            ms._api_request("http://x", max_retries=3)
        assert mock_sleep.call_count == 2  # retries: 0→1→2, sleeps between

    @mock.patch("maintenance_scheduler.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = mock_http_response(body=b'{"silenceID":"abc"}')
        mock_urlopen.return_value = mock_resp
        result = ms._api_request("http://x")
        assert result == {"silenceID": "abc"}


# ── 8. build_parser ──────────────────────────────────────────────

class TestBuildParser:
    """Test CLI argument parsing."""

    def test_required_config_dir(self):
        parser = ms.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_all_flags(self):
        parser = ms.build_parser()
        args = parser.parse_args([
            "--config-dir", "conf.d/",
            "--alertmanager", "http://am:9093",
            "--pushgateway", "http://pushgateway:9091",
            "--dry-run",
            "--json-output",
        ])
        assert args.config_dir == "conf.d/"
        assert args.alertmanager == "http://am:9093"
        assert args.pushgateway == "http://pushgateway:9091"
        assert args.dry_run
        assert args.json_output

    def test_defaults(self):
        parser = ms.build_parser()
        args = parser.parse_args(["--config-dir", "conf.d/"])
        assert args.alertmanager is None
        assert args.pushgateway is None
        assert not args.dry_run
        assert not args.json_output
