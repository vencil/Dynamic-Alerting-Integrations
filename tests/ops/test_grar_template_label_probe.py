"""#2817: a templated rule-level label value is a fire-time unknown, not a literal.

``platform_alert_identities`` builds each platform alert's probe from its
``labels:`` block. ``TenantMetricsOverLimit`` carries
``tenant: "{{ $labels.tenant }}"`` there, and taking that string literally made
the FULL probe set miss a tenant-triggered inhibit whose target regex-matches
``tenant`` — one the 6-entry fallback (``tenant: any-tenant``) catches. The full
set must never be looser than the fallback.
"""
from __future__ import annotations

import importlib
import sys

import pytest
import yaml

from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION

_VALIDATE = importlib.import_module("_grar_validate")

# The rule from the issue: triggered by a tenant alert, silences
# TenantMetricsOverLimit for any slug-shaped tenant.
_ISSUE_RULE = {"source_matchers": ['tenant=~".+"', 'severity="critical"'],
               "target_matchers": ['alertname="TenantMetricsOverLimit"',
                                   'tenant=~"[a-z0-9-]+"']}


def _pack(tmp_path, labels_yaml: str, expr: str = "some_metric > 0"):
    body = (
        "groups:\n- name: g\n  rules:\n"
        "  - alert: Templated\n"
        f"    expr: {expr}\n"
        "    labels:\n"
        "      alert_source: platform\n"
        f"{labels_yaml}"
    )
    p = tmp_path / "pack.yaml"
    p.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n"
        "  platform.yml: |\n" + "".join("    " + ln + "\n"
                                         for ln in body.splitlines()),
        encoding="utf-8")
    return p


@pytest.fixture
def full(monkeypatch):
    for name, value in (("_PLATFORM_IDENTITY_CACHE", None),
                        ("_PLATFORM_DEGRADED_REASON", None),
                        ("_PLATFORM_DEGRADED_WARNED", False)):
        monkeypatch.setattr(_VALIDATE, name, value)
    assert not _VALIDATE.probe_set_is_degraded(), (
        "the repo pack must be reachable for this test")


def test_issue_rule_is_caught_by_the_full_set(full):
    hits = _VALIDATE.find_tenant_silenceable_platform_inhibits([_ISSUE_RULE])
    assert [h[0] for h in hits] == [0], (
        "the full probe set let through an inhibit the fallback catches")
    assert hits[0][2]["alertname"] == "TenantMetricsOverLimit"


def test_control_the_fallback_catches_the_same_rule():
    hits = _VALIDATE.find_tenant_silenceable_platform_inhibits(
        [_ISSUE_RULE], _VALIDATE.PLATFORM_ALERT_IDENTITY_LABELS)
    assert [h[0] for h in hits] == [0]


def test_no_full_set_probe_carries_a_template_string(full):
    # Structural: any rule growing a templated `labels:` value later must not
    # reopen the hole.
    templated = [(d["alertname"], k) for d in _VALIDATE.platform_alert_identities()
                 for k, v in d.items() if isinstance(v, str) and "{{" in v]
    assert not templated, templated


def test_a_templated_non_tenant_label_is_probed_as_a_placeholder(tmp_path):
    p = _pack(tmp_path, "      severity: warning\n"
                        "      component: \"{{ $labels.job }}\"\n")
    (ident,) = _VALIDATE.platform_alert_identities(p)
    rule = {"source_matchers": ['tenant=~".+"'],
            "target_matchers": ['alertname="Templated"', 'component=~"[a-z0-9-]+"']}
    assert _VALIDATE.find_tenant_silenceable_platform_inhibits([rule], (ident,))


def test_control_a_literal_label_value_is_kept_as_is(tmp_path):
    p = _pack(tmp_path, "      severity: warning\n"
                        "      component: exporter\n")
    (ident,) = _VALIDATE.platform_alert_identities(p)
    assert ident["component"] == "exporter"
    rule = {"source_matchers": ['tenant=~".+"'],
            "target_matchers": ['alertname="Templated"', 'component="other"']}
    assert not _VALIDATE.find_tenant_silenceable_platform_inhibits(
        [rule], (ident,))


def test_a_templated_tenant_is_re_probed_with_a_pinned_literal(tmp_path):
    # Guards the fix's shape, not the bug: the template must stay a
    # tenant-bearing probe (not be dropped), so a target pinning `tenant="t1"`
    # is still re-probed with t1 — same as an expr-derived tenant.
    p = _pack(tmp_path, "      tenant: \"{{ $labels.tenant }}\"\n")
    rule = {"source_matchers": ['tenant=~".+"'],
            "target_matchers": ['alertname="Templated"', 'tenant="t1"']}
    assert _VALIDATE.find_tenant_silenceable_platform_inhibits(
        [rule], _VALIDATE.platform_alert_identities(p))


def test_output_configmap_refuses_the_issue_rule(full, monkeypatch, capsys,
                                                  tmp_path):
    """End to end: the render path the issue names rejects it (rc 1)."""
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n"
        "      receiver:\n        type: webhook\n"
        "        url: https://hooks.example.com/alert\n", encoding="utf-8")
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({
        "route": {"receiver": "default"}, "receivers": [{"name": "default"}],
        "inhibit_rules": [_ISSUE_RULE]}), encoding="utf-8")
    out = tmp_path / "out.yaml"
    gar = importlib.import_module("generate_alertmanager_routes")
    monkeypatch.setenv("PATH", str(tmp_path))          # no amtool on PATH
    monkeypatch.setattr(sys, "argv", [
        "generate_alertmanager_routes.py", "--config-dir", str(conf),
        "--output-configmap", "--base-config", str(base), "-o", str(out)])
    try:
        gar.main()
        rc = EXIT_OK
    except SystemExit as exc:
        rc = exc.code if exc.code is not None else EXIT_OK
    err = capsys.readouterr().err
    assert rc == EXIT_VIOLATION, err
    assert not out.exists()
    assert "TenantMetricsOverLimit" in err


def test_a_partial_template_keeps_its_literal_part(tmp_path):
    p = _pack(tmp_path, "      component: \"exp-{{ $labels.job }}\"\n")
    (ident,) = _VALIDATE.platform_alert_identities(p)
    assert ident["component"] == "exp-any-tenant"
    rule = {"source_matchers": ['tenant=~".+"'],
            "target_matchers": ['alertname="Templated"', 'component=~"exp-.+"']}
    assert _VALIDATE.find_tenant_silenceable_platform_inhibits([rule], (ident,))
