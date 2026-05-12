#!/usr/bin/env python3
"""test_silencer_drift_check.py — tests for da-tools silencer-drift-check (#405 Cat B).

Coverage:
  - load_silences: valid / missing / invalid JSON / non-array
  - load_alerts: file / dir / recursive / skip non-rule-pack YAML
  - extract_alerts_from_pack: alert+labels, recording rules skipped,
    implicit `alertname` label injection, malformed entries
  - matcher_applies: full 4-combination semantics matrix
    (isEqual × isRegex) plus absent-label handling
  - silence_matches_alert: all-matchers-must-match semantics
  - is_silence_active: status.state / startsAt-endsAt fallback
  - check_drift: orphan detection / inactive filtering
  - render_text + compute_exit_code
  - main() end-to-end via argv

Usage:
  pytest tests/ops/test_silencer_drift_check.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

import silencer_drift_check as sdc


# ─── Helpers + fixtures ───────────────────────────────────────────────


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")
    return path


_SENTINEL: list = []  # used as default sentinel to distinguish None vs empty


def _silence(
    *,
    id_: str = "abc123",
    matchers: list | None = _SENTINEL,
    state: str | None = "active",
    comment: str = "",
    created_by: str = "",
) -> dict:
    """Build a minimal silence dict matching amtool silence query -o json shape.

    Pass `matchers=[]` explicitly to test malformed-empty-matchers behavior;
    omit the kwarg entirely for the default Foo-alertname matcher.
    """
    if matchers is _SENTINEL:
        actual_matchers = [
            {"name": "alertname", "value": "FooAlert", "isEqual": True, "isRegex": False}
        ]
    else:
        actual_matchers = matchers
    s = {
        "id": id_,
        "matchers": actual_matchers,
        "comment": comment,
        "createdBy": created_by,
        "startsAt": "2026-05-01T00:00:00Z",
        "endsAt": "2026-12-31T23:59:59Z",
    }
    if state is not None:
        s["status"] = {"state": state}
    return s


@pytest.fixture
def silences_file(tmp_path: Path) -> Path:
    """A silences JSON file with one active alertname silence."""
    p = tmp_path / "silences.json"
    p.write_text(json.dumps([_silence()]), encoding="utf-8")
    return p


@pytest.fixture
def rule_pack_file(tmp_path: Path) -> Path:
    """A minimal rule pack with one matching alert."""
    return _write(tmp_path / "pack.yaml", """
        groups:
          - name: g
            rules:
              - alert: FooAlert
                expr: up
                labels:
                  severity: warning
    """)


# ─── load_silences ────────────────────────────────────────────────────


def test_load_silences_valid(silences_file):
    data = sdc.load_silences(silences_file)
    assert data is not None
    assert len(data) == 1
    assert data[0]["id"] == "abc123"


def test_load_silences_missing(tmp_path, capsys):
    data = sdc.load_silences(tmp_path / "nope.json")
    assert data is None
    assert "cannot read" in capsys.readouterr().err


def test_load_silences_invalid_json(tmp_path, capsys):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    data = sdc.load_silences(p)
    assert data is None
    assert "invalid JSON" in capsys.readouterr().err


def test_load_silences_top_level_not_array(tmp_path, capsys):
    """amtool silence query -o json produces a JSON array. If somebody
    pipes the wrong shape (e.g. {silences: [...]} or a single object),
    fail loudly rather than silently producing zero results."""
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({"silences": []}), encoding="utf-8")
    data = sdc.load_silences(p)
    assert data is None
    err = capsys.readouterr().err
    assert "top-level must be a JSON array" in err
    assert "dict" in err


# ─── extract_alerts_from_pack ─────────────────────────────────────────


def test_extract_alerts_injects_alertname_label():
    """The implicit `alertname=<name>` label must be in the effective
    set so a silence matcher on `alertname=FooAlert` works against this
    alert's labels."""
    pack = {
        "groups": [
            {
                "name": "g",
                "rules": [
                    {
                        "alert": "FooAlert",
                        "labels": {"severity": "warning"},
                    }
                ],
            }
        ]
    }
    alerts = sdc.extract_alerts_from_pack(pack, source_path="x.yaml")
    assert len(alerts) == 1
    assert alerts[0]["labels"]["alertname"] == "FooAlert"
    assert alerts[0]["labels"]["severity"] == "warning"


def test_extract_alerts_skips_recording_rules():
    """Only `alert:` rules contribute — record rules don't participate
    in AM matcher checks (silencers don't apply to recording rules)."""
    pack = {
        "groups": [
            {
                "name": "g",
                "rules": [
                    {"record": "r1", "expr": "rate(x[5m])"},
                    {"alert": "A1", "expr": "up"},
                ],
            }
        ]
    }
    alerts = sdc.extract_alerts_from_pack(pack, source_path="x.yaml")
    assert [a["name"] for a in alerts] == ["A1"]


def test_extract_alerts_handles_missing_labels():
    """Rule without a `labels:` block still yields an alert (with only
    the implicit alertname label)."""
    pack = {"groups": [{"name": "g", "rules": [{"alert": "Bare", "expr": "x"}]}]}
    alerts = sdc.extract_alerts_from_pack(pack, source_path="x.yaml")
    assert alerts[0]["labels"] == {"alertname": "Bare"}


def test_extract_alerts_robust_against_malformed():
    """Non-dict groups/rules and non-string alertnames must not crash."""
    pack = {
        "groups": [
            "not a dict",
            {
                "name": "g",
                "rules": [
                    "scalar",
                    {"alert": 123},  # non-string name
                    {"alert": "Real", "expr": "up"},
                ],
            },
        ]
    }
    alerts = sdc.extract_alerts_from_pack(pack, source_path="x.yaml")
    assert [a["name"] for a in alerts] == ["Real"]


def test_extract_alerts_label_values_coerced_to_strings():
    """YAML can parse label values as int / bool; matchers compare
    against strings. Coerce here so the match engine doesn't have to
    care about types."""
    pack = {
        "groups": [
            {
                "name": "g",
                "rules": [
                    {"alert": "A", "expr": "x", "labels": {"port": 8080, "tls": True}}
                ],
            }
        ]
    }
    alerts = sdc.extract_alerts_from_pack(pack, source_path="x.yaml")
    assert alerts[0]["labels"]["port"] == "8080"
    assert alerts[0]["labels"]["tls"] == "True"


# ─── load_alerts (file + dir) ─────────────────────────────────────────


def test_load_alerts_single_file(rule_pack_file):
    alerts, errors = sdc.load_alerts(rule_pack_file)
    assert errors == []
    assert len(alerts) == 1
    assert alerts[0]["name"] == "FooAlert"


def test_load_alerts_directory_recursive(tmp_path):
    _write(tmp_path / "a/pack1.yaml", """
        groups: [{name: g, rules: [{alert: A1, expr: up}]}]
    """)
    _write(tmp_path / "b/c/pack2.yaml", """
        groups: [{name: g, rules: [{alert: A2, expr: up}]}]
    """)
    alerts, errors = sdc.load_alerts(tmp_path)
    assert errors == []
    assert {a["name"] for a in alerts} == {"A1", "A2"}


def test_load_alerts_skips_non_rule_pack_yaml(tmp_path):
    """tenant _defaults.yaml has no `groups:` root — must be skipped
    silently, not error out the whole load."""
    _write(tmp_path / "pack.yaml", """
        groups: [{name: g, rules: [{alert: A, expr: up}]}]
    """)
    _write(tmp_path / "_defaults.yaml", """
        defaults:
          mysql_connections: 80
    """)
    alerts, errors = sdc.load_alerts(tmp_path)
    assert errors == []
    assert [a["name"] for a in alerts] == ["A"]


def test_load_alerts_reports_yaml_parse_error(tmp_path):
    _write(tmp_path / "good.yaml", """
        groups: [{name: g, rules: [{alert: A, expr: up}]}]
    """)
    (tmp_path / "bad.yaml").write_text("groups: [unclosed\n", encoding="utf-8")
    alerts, errors = sdc.load_alerts(tmp_path)
    # Good file's alert is still loaded
    assert [a["name"] for a in alerts] == ["A"]
    # Bad file is captured as an error (caller can decide to bail)
    assert any("invalid YAML" in e for e in errors)


def test_load_alerts_empty_dir(tmp_path):
    alerts, errors = sdc.load_alerts(tmp_path)
    assert alerts == []
    assert any("no YAML files found" in e for e in errors)


# ─── matcher_applies: full 4-combination matrix ───────────────────────


def test_matcher_equal_literal_match():
    m = {"name": "alertname", "value": "Foo", "isEqual": True, "isRegex": False}
    assert sdc.matcher_applies(m, {"alertname": "Foo"})


def test_matcher_equal_literal_miss():
    m = {"name": "alertname", "value": "Foo", "isEqual": True, "isRegex": False}
    assert not sdc.matcher_applies(m, {"alertname": "Bar"})


def test_matcher_not_equal_literal():
    m = {"name": "alertname", "value": "Foo", "isEqual": False, "isRegex": False}
    assert sdc.matcher_applies(m, {"alertname": "Bar"})
    assert not sdc.matcher_applies(m, {"alertname": "Foo"})


def test_matcher_regex_match_fullmatch():
    """AM regex semantics require fullmatch (the matcher anchors at both
    ends); `prefix.*` does NOT match `foo_prefix` unless it's exactly
    `prefix<anything>`."""
    m = {"name": "alertname", "value": "Foo.*", "isEqual": True, "isRegex": True}
    assert sdc.matcher_applies(m, {"alertname": "FooBar"})
    assert sdc.matcher_applies(m, {"alertname": "Foo"})
    assert not sdc.matcher_applies(m, {"alertname": "BarFoo"})


def test_matcher_not_regex():
    m = {"name": "alertname", "value": "Foo.*", "isEqual": False, "isRegex": True}
    assert sdc.matcher_applies(m, {"alertname": "BarFoo"})
    assert not sdc.matcher_applies(m, {"alertname": "FooBar"})


def test_matcher_absent_label_equality_fails():
    """AM treats absent label as empty string — `foo="bar"` against an
    alert without `foo` does NOT match."""
    m = {"name": "team", "value": "sre", "isEqual": True, "isRegex": False}
    assert not sdc.matcher_applies(m, {"alertname": "Foo"})


def test_matcher_absent_label_inequality_succeeds():
    """Conversely, `foo!="bar"` against an absent `foo` DOES match
    (empty != bar). This is AM convention."""
    m = {"name": "team", "value": "sre", "isEqual": False, "isRegex": False}
    assert sdc.matcher_applies(m, {"alertname": "Foo"})


def test_matcher_invalid_regex_does_not_crash():
    """A silence with a malformed regex pattern must not crash the tool.
    Conservatively: report as no-match (which will mark the silence as
    orphan, surfacing the operator's bug)."""
    m = {"name": "alertname", "value": "[unclosed", "isEqual": True, "isRegex": True}
    # Doesn't raise
    result = sdc.matcher_applies(m, {"alertname": "Foo"})
    assert result is False


# ─── silence_matches_alert ────────────────────────────────────────────


def test_silence_all_matchers_must_match():
    silence = {
        "matchers": [
            {"name": "alertname", "value": "FooAlert", "isEqual": True, "isRegex": False},
            {"name": "severity", "value": "critical", "isEqual": True, "isRegex": False},
        ]
    }
    alert = {"labels": {"alertname": "FooAlert", "severity": "warning"}}
    # Matches alertname but not severity → silence doesn't apply
    assert not sdc.silence_matches_alert(silence, alert)


# ─── detect_malformed ─────────────────────────────────────────────────


def test_detect_malformed_well_formed():
    s = _silence()
    assert sdc.detect_malformed(s) is None


def test_detect_malformed_empty_matchers():
    """Real AM silences always have matchers; empty list is a signal that
    the JSON was hand-edited or corrupted. Must be flagged, not silently
    treated as 'universal match' (which would hide the bad input)."""
    s = _silence(matchers=[])
    reason = sdc.detect_malformed(s)
    assert reason is not None
    assert "empty" in reason


def test_detect_malformed_matchers_missing_or_wrong_type():
    s = {"id": "x"}  # no matchers field at all
    assert sdc.detect_malformed(s) is not None

    s = {"id": "x", "matchers": "not a list"}
    reason = sdc.detect_malformed(s)
    assert reason is not None
    assert "expected a non-empty JSON array" in reason


def test_detect_malformed_matcher_missing_name():
    s = _silence(matchers=[{"value": "foo", "isEqual": True, "isRegex": False}])
    reason = sdc.detect_malformed(s)
    assert reason is not None
    assert "missing or non-string 'name'" in reason


def test_detect_malformed_matcher_non_dict():
    s = _silence(matchers=["not a dict"])
    reason = sdc.detect_malformed(s)
    assert reason is not None
    assert "not a JSON object" in reason


# ─── is_silence_active ────────────────────────────────────────────────


def test_active_via_status_state():
    s = _silence(state="active")
    assert sdc.is_silence_active(s)


def test_expired_via_status_state():
    s = _silence(state="expired")
    assert not sdc.is_silence_active(s)


def test_active_via_timestamps_when_no_status():
    """Older amtool dumps don't include status.state; fall back to
    startsAt/endsAt comparison."""
    s = _silence(state=None)
    s["startsAt"] = "2026-05-01T00:00:00Z"
    s["endsAt"] = "2026-12-31T23:59:59Z"
    # Pick an `at` known to be in range
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert sdc.is_silence_active(s, at=at)


def test_inactive_via_timestamps_in_past():
    s = _silence(state=None)
    s["startsAt"] = "2020-01-01T00:00:00Z"
    s["endsAt"] = "2020-01-02T00:00:00Z"
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert not sdc.is_silence_active(s, at=at)


def test_active_when_timestamps_malformed():
    """Malformed timestamps + no status.state → treat as active so we
    don't silently filter out unparseable silences from the check."""
    s = _silence(state=None)
    s["startsAt"] = "not a date"
    assert sdc.is_silence_active(s)


# ─── check_drift end-to-end ───────────────────────────────────────────


def test_drift_orphan_detected():
    alerts = [
        {"name": "Other", "labels": {"alertname": "Other"}, "source": "x", "group": "g"},
    ]
    silences = [
        _silence(
            id_="s1",
            matchers=[
                {"name": "alertname", "value": "Foo", "isEqual": True, "isRegex": False}
            ],
        )
    ]
    r = sdc.check_drift(silences, alerts)
    assert r["counts"]["orphans"] == 1
    assert r["orphans"][0]["silence_id"] == "s1"


def test_drift_no_orphans_when_alert_matches():
    alerts = [
        {"name": "Foo", "labels": {"alertname": "Foo"}, "source": "x", "group": "g"},
    ]
    silences = [
        _silence(
            id_="s1",
            matchers=[
                {"name": "alertname", "value": "Foo", "isEqual": True, "isRegex": False}
            ],
        )
    ]
    r = sdc.check_drift(silences, alerts)
    assert r["counts"]["orphans"] == 0


def test_drift_inactive_filtered_by_default():
    """Inactive silences shouldn't pollute the orphan report. They're
    not actively suppressing anything; if they happen to be orphan
    too, it doesn't operationally matter."""
    alerts = [{"name": "Bar", "labels": {"alertname": "Bar"}, "source": "x", "group": "g"}]
    silences = [_silence(id_="dead", state="expired")]  # default Foo matcher; alerts has Bar
    r = sdc.check_drift(silences, alerts)
    assert r["counts"]["orphans"] == 0
    assert r["counts"]["inactive_skipped"] == 1


def test_drift_inactive_included_when_flagged():
    alerts = [{"name": "Bar", "labels": {"alertname": "Bar"}, "source": "x", "group": "g"}]
    silences = [_silence(id_="dead", state="expired")]
    r = sdc.check_drift(silences, alerts, include_inactive=True)
    assert r["counts"]["orphans"] == 1


def test_drift_partitions_malformed_separately():
    """Malformed silences must go into malformed_silences, NOT be
    silently classified as 'not orphan' just because the empty-matcher
    fallback used to match everything. This is the round-1 self-review
    regression guard: round 0 had `silence with empty matchers → match
    everything → never orphan` which masked bad JSON input."""
    alerts = [{"name": "A", "labels": {"alertname": "A"}, "source": "x", "group": "g"}]
    silences = [
        _silence(id_="good"),
        _silence(id_="bad-empty", matchers=[]),
        _silence(id_="bad-noname", matchers=[{"value": "v", "isEqual": True, "isRegex": False}]),
    ]
    r = sdc.check_drift(silences, alerts)
    # Two malformed surfaced separately
    assert r["counts"]["malformed"] == 2
    malformed_ids = {m["silence_id"] for m in r["malformed_silences"]}
    assert malformed_ids == {"bad-empty", "bad-noname"}
    # `good` silence's default Foo matcher doesn't match `A` → orphan
    assert r["counts"]["orphans"] == 1
    assert r["orphans"][0]["silence_id"] == "good"


def test_drift_ci_exits_1_on_malformed():
    """--ci mode must fail not just on orphans but also on malformed
    silences — that's a corruption signal automation should not pass."""
    alerts = [{"name": "A", "labels": {"alertname": "A"}, "source": "x", "group": "g"}]
    silences = [_silence(id_="mal", matchers=[])]
    r = sdc.check_drift(silences, alerts)
    assert sdc.compute_exit_code(r, ci=True) == 1


def test_drift_multi_matcher_label_drift():
    """Realistic case: v2 changed severity from warning → critical.
    Customer's silence has alertname=FooAlert + severity=warning; v2
    FooAlert exists but its severity is critical now. Silence is
    orphan."""
    alerts = [
        {
            "name": "FooAlert",
            "labels": {"alertname": "FooAlert", "severity": "critical"},
            "source": "x",
            "group": "g",
        }
    ]
    silences = [
        _silence(
            matchers=[
                {"name": "alertname", "value": "FooAlert", "isEqual": True, "isRegex": False},
                {"name": "severity", "value": "warning", "isEqual": True, "isRegex": False},
            ]
        )
    ]
    r = sdc.check_drift(silences, alerts)
    assert r["counts"]["orphans"] == 1


# ─── compute_exit_code ────────────────────────────────────────────────


def test_exit_code_default_zero_with_orphans():
    report = {"counts": {"orphans": 3}}
    assert sdc.compute_exit_code(report, ci=False) == 0


def test_exit_code_ci_orphans_exits_1():
    report = {"counts": {"orphans": 1}}
    assert sdc.compute_exit_code(report, ci=True) == 1


def test_exit_code_ci_clean_exits_0():
    report = {"counts": {"orphans": 0}}
    assert sdc.compute_exit_code(report, ci=True) == 0


# ─── main() end-to-end ────────────────────────────────────────────────


def test_main_clean_run(silences_file, rule_pack_file, capsys):
    rc = sdc.main(
        ["--silences-file", str(silences_file), "--rule-source", str(rule_pack_file)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No orphaned silences detected" in out


def test_main_orphan_text_output(tmp_path, capsys):
    silences = tmp_path / "s.json"
    silences.write_text(
        json.dumps(
            [
                _silence(
                    id_="orph",
                    matchers=[
                        {
                            "name": "alertname",
                            "value": "DeadAlert",
                            "isEqual": True,
                            "isRegex": False,
                        }
                    ],
                )
            ]
        ),
        encoding="utf-8",
    )
    rules = _write(tmp_path / "p.yaml", """
        groups: [{name: g, rules: [{alert: OtherAlert, expr: up}]}]
    """)
    rc = sdc.main(["--silences-file", str(silences), "--rule-source", str(rules)])
    out = capsys.readouterr().out
    assert "Orphaned silences" in out
    assert "orph" in out
    assert 'alertname="DeadAlert"' in out
    assert rc == 0  # no --ci


def test_main_ci_exits_1_on_orphans(tmp_path):
    silences = tmp_path / "s.json"
    silences.write_text(
        json.dumps(
            [
                _silence(
                    matchers=[
                        {
                            "name": "alertname",
                            "value": "DeadAlert",
                            "isEqual": True,
                            "isRegex": False,
                        }
                    ]
                )
            ]
        ),
        encoding="utf-8",
    )
    rules = _write(tmp_path / "p.yaml", """
        groups: [{name: g, rules: [{alert: OtherAlert, expr: up}]}]
    """)
    rc = sdc.main(
        ["--silences-file", str(silences), "--rule-source", str(rules), "--ci"]
    )
    assert rc == 1


def test_main_json_output(silences_file, rule_pack_file, capsys):
    rc = sdc.main(
        [
            "--silences-file",
            str(silences_file),
            "--rule-source",
            str(rule_pack_file),
            "--json",
        ]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["orphans"] == 0
    assert report["silences_file"] == str(silences_file)
    assert report["rule_source"] == str(rule_pack_file)


def test_main_missing_silences_file(rule_pack_file, tmp_path):
    rc = sdc.main(
        [
            "--silences-file",
            str(tmp_path / "nope.json"),
            "--rule-source",
            str(rule_pack_file),
        ]
    )
    assert rc == 2


def test_main_missing_rule_source(silences_file, tmp_path, capsys):
    rc = sdc.main(
        [
            "--silences-file",
            str(silences_file),
            "--rule-source",
            str(tmp_path / "nope"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_main_empty_rule_source(silences_file, tmp_path, capsys):
    """Empty (no YAML files) rule source must error explicitly so
    a typo'd --rule-source isn't silently treated as 'all silences
    orphaned'."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    rc = sdc.main(
        ["--silences-file", str(silences_file), "--rule-source", str(empty_dir)]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "no YAML files found" in err


def test_main_empty_silences_clean(rule_pack_file, tmp_path, capsys):
    """Empty silences list (no silences) → 0 orphans, clean exit."""
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    rc = sdc.main(
        ["--silences-file", str(empty), "--rule-source", str(rule_pack_file)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No orphaned silences" in out
