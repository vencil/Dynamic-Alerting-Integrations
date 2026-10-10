"""#1533 §1: a degraded platform-alert probe set takes part in the verdict.

When ``configmap-rules-platform.yaml`` cannot be found / read / yields no
platform alert, the identity probe set falls back to a 6-entry constant, and
``assert_platform_alerts_not_tenant_silenceable`` can no longer see a
tenant-triggered inhibit rule that silences a platform alert outside it
(fail-OPEN). Owner decision D4 (d):

* render paths (``--output-configmap``, ``--apply``) refuse with rc 2 when the
  OPERATOR-supplied inhibit rules (base config / cluster config) hold a
  tenant-triggered rule — "could not verify", nothing written or applied;
* without such a rule the run keeps today's behaviour (WARN only);
* ``--validate`` keeps its exit code and prints one summary line.

Degradation is simulated by making ``_find_platform_rules_configmap`` return
None and clearing the cache; ``monkeypatch.setattr`` on the module globals
restores the real cache for the next test in the worker.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys

import pytest
import yaml

from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION

_VALIDATE = importlib.import_module("_grar_validate")

# Tenant-triggered, targets a platform alert that is in the FULL pack but NOT
# in the 6-entry fallback — the measured fail-open shape from the issue.
_TENANT_RULE = {"source_matchers": ['tenant=~".+"', 'severity="critical"'],
                "target_matchers": ['alertname="CronJobLastRunFailed"']}
# Not tenant-triggered: out of scope for the invariant either way.
_BENIGN_RULE = {"source_matchers": ['severity="critical"'],
                "target_matchers": ['severity="info"']}


def _fresh_cache(monkeypatch):
    for name, value in (("_PLATFORM_IDENTITY_CACHE", None),
                        ("_PLATFORM_DEGRADED_REASON", None),
                        ("_PLATFORM_DEGRADED_WARNED", False)):
        monkeypatch.setattr(_VALIDATE, name, value)


@pytest.fixture
def degraded(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(_VALIDATE, "_find_platform_rules_configmap",
                        lambda: None)
    assert _VALIDATE.probe_set_is_degraded()
    # the check above filled the cache and spent the warn-once flag; the run
    # under test must do its own lookup, as a fresh process would
    _fresh_cache(monkeypatch)


@pytest.fixture
def full(monkeypatch):
    _fresh_cache(monkeypatch)
    assert not _VALIDATE.probe_set_is_degraded(), (
        "the repo pack must be reachable for the control cases")


@pytest.fixture
def tenant_dir(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n"
        "      receiver:\n        type: webhook\n"
        "        url: https://hooks.example.com/alert\n",
        encoding="utf-8")
    return d


def _main(monkeypatch, tmp_path, *argv) -> int:
    gar = importlib.import_module("generate_alertmanager_routes")
    monkeypatch.setenv("PATH", str(tmp_path))          # no amtool on PATH
    monkeypatch.setattr(sys, "argv", ["generate_alertmanager_routes.py", *argv])
    # Only SystemExit (or a normal return) may leave main(): any other
    # exception IS the traceback this change must not produce.
    try:
        gar.main()
    except SystemExit as exc:
        return exc.code if exc.code is not None else EXIT_OK
    return EXIT_OK


def _render(monkeypatch, tmp_path, config_dir, rule):
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({
        "route": {"receiver": "default"}, "receivers": [{"name": "default"}],
        "inhibit_rules": [rule]}), encoding="utf-8")
    out = tmp_path / "out.yaml"
    rc = _main(monkeypatch, tmp_path, "--config-dir", str(config_dir),
               "--output-configmap", "--base-config", str(base), "-o", str(out))
    return rc, out


# ── --output-configmap ──────────────────────────────────────────────────

def test_degraded_base_tenant_rule_is_refused_rc2_nothing_written(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    rc, out = _render(monkeypatch, tmp_path, tenant_dir, _TENANT_RULE)
    err = capsys.readouterr().err
    assert rc == EXIT_CALLER_ERROR, err
    assert not out.exists()
    assert "could NOT be verified" in err
    assert "configmap-rules-platform.yaml not found" in err   # names the cause
    assert "inhibit_rules[0]" in err                          # names the rule
    assert "nothing was written to" in err
    assert "Traceback" not in err


def test_control_same_input_with_the_pack_is_a_real_violation(
        full, monkeypatch, capsys, tmp_path, tenant_dir):
    """Must-fire: the fixture IS a violation the full set catches (rc 1)."""
    rc, out = _render(monkeypatch, tmp_path, tenant_dir, _TENANT_RULE)
    err = capsys.readouterr().err
    assert rc == EXIT_VIOLATION, err
    assert not out.exists()
    assert "CronJobLastRunFailed" in err
    assert "could NOT be verified" not in err


def test_control_degraded_without_tenant_rule_still_renders(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    rc, out = _render(monkeypatch, tmp_path, tenant_dir, _BENIGN_RULE)
    err = capsys.readouterr().err
    assert rc == EXIT_OK, err
    assert out.exists()
    assert "WARN: platform alert identity probe set degraded" in err
    assert "could NOT be verified" not in err


def test_degraded_rule_that_excludes_platform_alerts_still_renders(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    """The fix the violation message itself recommends — `alert_source=""` on
    the target — cannot reach a platform alert whatever the probe set holds,
    so degradation must not refuse it."""
    rule = {**_TENANT_RULE, "target_matchers": [
        *_TENANT_RULE["target_matchers"], 'alert_source=""']}
    rc, out = _render(monkeypatch, tmp_path, tenant_dir, rule)
    err = capsys.readouterr().err
    assert rc == EXIT_OK, err
    assert out.exists()


@pytest.mark.parametrize("matcher, unverifiable", [
    ('alert_source=""', False),
    ('alert_source!="platform"', False),
    ('alert_source="platform"', True),      # pins platform alerts IN
    ('alert_source=~"plat.*"', True),       # still matches platform
    ('severity="critical"', True),          # says nothing about alert_source
])
def test_only_targets_that_reject_platform_are_exempt(
        degraded, matcher, unverifiable):
    rule = {**_TENANT_RULE, "target_matchers": [
        *_TENANT_RULE["target_matchers"], matcher]}
    got = _VALIDATE.find_unverifiable_tenant_triggered_inhibits([rule])
    assert got == ([0] if unverifiable else [])


# ── --apply ─────────────────────────────────────────────────────────────

def _apply(monkeypatch, tmp_path, config_dir, rules):
    render = importlib.import_module("_grar_render")
    existing = {"route": {"receiver": "default"},
                "receivers": [{"name": "default"}], "inhibit_rules": rules}
    calls: list[list[str]] = []

    def fake(argv, *, timeout, stdin_text=None, text=True):
        calls.append(list(argv))
        stdout = (json.dumps({"data": {"alertmanager.yml":
                                       yaml.safe_dump(existing)}})
                  if argv[:2] == ["kubectl", "get"] else "")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    rc = _main(monkeypatch, tmp_path, "--config-dir", str(config_dir),
               "--apply", "--yes")
    return rc, calls


def test_apply_degraded_cluster_tenant_rule_is_refused_rc2(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    rc, calls = _apply(monkeypatch, tmp_path, tenant_dir,
                       [_BENIGN_RULE, _TENANT_RULE])
    err = capsys.readouterr().err
    assert rc == EXIT_CALLER_ERROR, err
    assert "could NOT be verified" in err and "inhibit_rules[1]" in err
    assert "nothing was applied to the cluster" in err
    assert [c[:2] for c in calls] == [["kubectl", "get"]], calls


def test_apply_control_with_the_pack_is_a_real_violation(
        full, monkeypatch, capsys, tmp_path, tenant_dir):
    rc, calls = _apply(monkeypatch, tmp_path, tenant_dir, [_TENANT_RULE])
    err = capsys.readouterr().err
    assert rc == EXIT_VIOLATION, err
    assert "CronJobLastRunFailed" in err
    assert [c[:2] for c in calls] == [["kubectl", "get"]], calls


def test_apply_control_degraded_without_tenant_rule_applies(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    rc, calls = _apply(monkeypatch, tmp_path, tenant_dir, [_BENIGN_RULE])
    err = capsys.readouterr().err
    assert rc == EXIT_OK, err
    assert ["kubectl", "apply"] in [c[:2] for c in calls], calls


def test_merge_exempts_generated_rules_but_not_lookalikes(degraded):
    """Unit level: this run's generated rules and an earlier run's (source
    AND target require `metric_group`) are exempt; a hand-written rule that
    only borrows the source-side shape is not."""
    render = importlib.import_module("_grar_render")
    generated = {"source_matchers": ['tenant="t1"', 'severity="critical"',
                                     'metric_group=~".+"'],
                 "target_matchers": ['tenant="t1"', 'severity="warning"',
                                     'metric_group=~".+"'],
                 "equal": ["tenant", "metric_group"]}
    # this run generated nothing, so an earlier run's rule is kept
    render._merge_routes_receivers_inhibits(
        {"inhibit_rules": [dict(generated)]}, [], [], [])
    # this run's own generated rule
    render._merge_routes_receivers_inhibits(
        {"inhibit_rules": []}, [], [], [dict(generated)])
    lookalike = {"source_matchers": ['tenant=~".+"', 'metric_group=~".+"'],
                 "target_matchers": ['alertname="CronJobLastRunFailed"']}
    with pytest.raises(_VALIDATE.PlatformProbeSetUnverifiable):
        render._merge_routes_receivers_inhibits(
            {"inhibit_rules": [lookalike]}, [], [], [])


# ── --validate ──────────────────────────────────────────────────────────

_SUMMARY = "Probe set: DEGRADED to the 6-entry built-in fallback"


def test_validate_degraded_keeps_rc_and_prints_one_summary_line(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    rc = _main(monkeypatch, tmp_path, "--config-dir", str(tenant_dir),
               "--validate")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert out.count(_SUMMARY) == 1, out
    assert "OK: all configs valid" in out


def test_validate_control_full_set_has_no_summary_line(
        full, monkeypatch, capsys, tmp_path, tenant_dir):
    rc = _main(monkeypatch, tmp_path, "--config-dir", str(tenant_dir),
               "--validate")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "Probe set:" not in out, out


# ── the predicate itself ────────────────────────────────────────────────

def test_every_degradation_cause_counts(monkeypatch, tmp_path):
    """Not found, unreadable, no platform alert — and a readable pack not."""
    _fresh_cache(monkeypatch)
    assert not _VALIDATE.probe_set_is_degraded()
    for body in (None, "kind: ConfigMap\ndata: {r: ': : :'}\n",
                 "kind: ConfigMap\ndata: {r: 'groups: []'}\n"):
        _fresh_cache(monkeypatch)
        if body is None:
            monkeypatch.setattr(_VALIDATE, "_find_platform_rules_configmap",
                                lambda: None)
        else:
            p = tmp_path / "pack.yaml"
            p.write_text(body, encoding="utf-8")
            monkeypatch.setattr(_VALIDATE, "_find_platform_rules_configmap",
                                lambda p=p: p)
        assert _VALIDATE.probe_set_is_degraded(), body
        assert _VALIDATE.probe_set_degraded_reason(), body


# ── review follow-ups ───────────────────────────────────────────────────

def test_degraded_base_copy_of_a_generated_rule_still_renders(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    """A base that carries the generator's own shape (the repo's k8s base
    does) must not be refused: its target requires `metric_group`, which no
    platform alert carries — the same reason the generated rules are exempt."""
    copy = {"source_matchers": ['severity="critical"', 'metric_group=~".+"',
                                'tenant="t1"'],
            "target_matchers": ['severity="warning"', 'metric_group=~".+"',
                                'tenant="t1"'],
            "equal": ["metric_group"]}
    rc, out = _render(monkeypatch, tmp_path, tenant_dir, copy)
    err = capsys.readouterr().err
    assert rc == EXIT_OK, err
    assert out.exists()


def test_a_verdict_that_needs_no_probe_set_outranks_could_not_verify(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    """Ungated `equal:` under --strict is rc 1 whatever the probe set holds,
    so it must be reported instead of the rc 2 "could not verify"."""
    rule = {"source_matchers": ['tenant=~".+"', 'severity="critical"'],
            "target_matchers": ['alertname="NoSuchAlert"'],
            "equal": ["cluster"]}
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({
        "route": {"receiver": "default"}, "receivers": [{"name": "default"}],
        "inhibit_rules": [rule]}), encoding="utf-8")
    rc = _main(monkeypatch, tmp_path, "--config-dir", str(tenant_dir),
               "--output-configmap", "--base-config", str(base),
               "-o", str(tmp_path / "out.yaml"), "--strict")
    err = capsys.readouterr().err
    assert rc == EXIT_VIOLATION, err
    assert "could NOT be verified" not in err


def test_explain_route_trace_does_not_traceback_when_unverifiable(
        degraded, monkeypatch, capsys, tmp_path, tenant_dir):
    """explain_route assembles with the operator's --base-config too; the
    rc-2 refusal must reach it as a WARN, like the generator's other
    refusals, not as an uncaught exception."""
    explain = importlib.import_module("explain_route")
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({
        "route": {"receiver": "default"}, "receivers": [{"name": "default"}],
        "inhibit_rules": [_TENANT_RULE]}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        explain.main(["--config-dir", str(tenant_dir), "--trace",
                      "--tenant", "t1", "--alertname", "X",
                      "--base-config", str(base)])
    except SystemExit:
        pass
    err = capsys.readouterr().err
    assert "cannot verify this config" in err
