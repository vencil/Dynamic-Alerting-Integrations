"""test_operator_generate_v2.py — operator-generate v2.6.0 功能驗證

驗證 Phase .a 新功能：
- 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook）
- Secret 引用（secretKeyRef）— 禁止明文 credential
- 三態 CRD 抑制規則（severity dedup + silent + maintenance）
- --receiver-template / --secret-name / --secret-key CLI 參數

v2.6.0 Phase A 新增。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# Add scripts/tools to path
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools"))

from operator_generate import (
    _build_inhibit_rules_crd,
    _build_receiver_config,
    _DEFAULT_SECRET_KEYS,
    _RECEIVER_TEMPLATES,
    build_alertmanager_config,
    build_prometheus_rule,
    build_servicemonitor,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_tenant() -> str:
    return "db-a"


@pytest.fixture
def namespace() -> str:
    return "monitoring"


# ──────────────────────────────────────────────────────────────────────────────
# Receiver Template Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestReceiverTemplates:
    """Verify all 6 receiver templates produce correct CRD structure."""

    @pytest.mark.parametrize("receiver_type", list(_RECEIVER_TEMPLATES))
    def test_all_templates_produce_valid_receiver(
        self, sample_tenant: str, receiver_type: str
    ):
        """Each template type produces a receiver with correct name."""
        receiver = _build_receiver_config(sample_tenant, receiver_type)
        assert receiver["name"] == f"{sample_tenant}-{receiver_type}"

    def test_slack_has_slack_configs(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "slack")
        assert "slackConfigs" in receiver
        config = receiver["slackConfigs"][0]
        assert "apiURL" in config
        assert "secret" in config["apiURL"]
        assert config["channel"] == f"#alerts-{sample_tenant}"
        assert config["sendResolved"] is True

    def test_pagerduty_has_pagerduty_configs(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "pagerduty")
        assert "pagerdutyConfigs" in receiver
        config = receiver["pagerdutyConfigs"][0]
        assert "routingKey" in config
        assert "secret" in config["routingKey"]
        assert config["sendResolved"] is True

    def test_email_has_email_configs(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "email")
        assert "emailConfigs" in receiver
        config = receiver["emailConfigs"][0]
        assert "authPassword" in config
        assert "secret" in config["authPassword"]
        assert config["requireTLS"] is True

    def test_teams_has_webhook_with_auth(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "teams")
        assert "webhookConfigs" in receiver
        config = receiver["webhookConfigs"][0]
        assert "httpConfig" in config
        assert "authorization" in config["httpConfig"]
        assert "secret" in config["httpConfig"]["authorization"]["credentials"]

    def test_opsgenie_has_opsgenie_configs(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "opsgenie")
        assert "opsgenieConfigs" in receiver
        config = receiver["opsgenieConfigs"][0]
        assert "apiKey" in config
        assert "secret" in config["apiKey"]

    def test_webhook_has_webhook_configs(self, sample_tenant: str):
        receiver = _build_receiver_config(sample_tenant, "webhook")
        assert "webhookConfigs" in receiver
        config = receiver["webhookConfigs"][0]
        assert f"/webhook/{sample_tenant}" in config["url"]


# ──────────────────────────────────────────────────────────────────────────────
# Secret Integration Tests (Enterprise Audit Requirement)
# ──────────────────────────────────────────────────────────────────────────────


class TestSecretIntegration:
    """Verify NO plaintext credentials appear in generated CRDs."""

    @pytest.mark.parametrize("receiver_type", list(_RECEIVER_TEMPLATES))
    def test_no_plaintext_credentials(
        self, sample_tenant: str, receiver_type: str
    ):
        """Generated receiver must use secretKeyRef, never plaintext values."""
        receiver = _build_receiver_config(sample_tenant, receiver_type)
        receiver_json = json.dumps(receiver)
        # Must NOT contain actual credential values
        assert "your-secret" not in receiver_json.lower()
        assert "password" not in receiver_json.lower() or "authPassword" in receiver_json
        # Must contain secret references
        assert "secret" in receiver_json

    @pytest.mark.parametrize("receiver_type", list(_RECEIVER_TEMPLATES))
    def test_default_secret_name_convention(
        self, sample_tenant: str, receiver_type: str
    ):
        """Default secret name follows da-{tenant}-{type} pattern."""
        receiver = _build_receiver_config(sample_tenant, receiver_type)
        receiver_json = json.dumps(receiver)
        expected_name = f"da-{sample_tenant}-{receiver_type}"
        assert expected_name in receiver_json

    def test_custom_secret_name(self, sample_tenant: str):
        """Custom --secret-name is respected."""
        receiver = _build_receiver_config(
            sample_tenant, "slack",
            secret_name="my-custom-secret",
            secret_key="my-key",
        )
        config = receiver["slackConfigs"][0]
        assert config["apiURL"]["secret"]["name"] == "my-custom-secret"
        assert config["apiURL"]["secret"]["key"] == "my-key"

    @pytest.mark.parametrize("receiver_type", list(_RECEIVER_TEMPLATES))
    def test_default_secret_key_matches_type(
        self, sample_tenant: str, receiver_type: str
    ):
        """Default secret key is inferred from receiver type."""
        receiver = _build_receiver_config(sample_tenant, receiver_type)
        receiver_json = json.dumps(receiver)
        expected_key = _DEFAULT_SECRET_KEYS[receiver_type]
        assert expected_key in receiver_json


# ──────────────────────────────────────────────────────────────────────────────
# Inhibit Rules Tests (Tri-state: severity dedup + silent + maintenance)
# ──────────────────────────────────────────────────────────────────────────────


class TestInhibitRules:
    """Verify CRD-format inhibit rules for all three modes."""

    def test_produces_four_rules(self, sample_tenant: str):
        """Four rules: severity dedup + silent warning + silent critical + maintenance."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        assert len(rules) == 4

    def test_severity_dedup_rule(self, sample_tenant: str):
        """Critical suppresses Warning for same alertname+instance."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        dedup = rules[0]
        source_names = {m["name"]: m["value"] for m in dedup["sourceMatch"]}
        target_names = {m["name"]: m["value"] for m in dedup["targetMatch"]}
        assert source_names["severity"] == "critical"
        assert target_names["severity"] == "warning"
        assert "alertname" in dedup["equal"]
        assert "instance" in dedup["equal"]

    def test_silent_mode_warning_rule(self, sample_tenant: str):
        """TenantSilentWarning sentinel suppresses warnings."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        silent_warn = rules[1]
        source_names = {m["name"]: m["value"] for m in silent_warn["sourceMatch"]}
        assert source_names["alertname"] == "TenantSilentWarning"
        target_names = {m["name"]: m["value"] for m in silent_warn["targetMatch"]}
        assert target_names["severity"] == "warning"

    def test_silent_mode_critical_rule(self, sample_tenant: str):
        """TenantSilentCritical sentinel suppresses criticals."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        silent_crit = rules[2]
        source_names = {m["name"]: m["value"] for m in silent_crit["sourceMatch"]}
        assert source_names["alertname"] == "TenantSilentCritical"

    def test_maintenance_mode_rule(self, sample_tenant: str):
        """TenantMaintenanceMode suppresses ALL alerts for tenant."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        maint = rules[3]
        source_names = {m["name"]: m["value"] for m in maint["sourceMatch"]}
        assert source_names["alertname"] == "TenantMaintenanceMode"
        # Target should match all alerts for tenant (no severity filter)
        target_names = {m["name"] for m in maint["targetMatch"]}
        assert "severity" not in target_names

    def test_all_rules_scoped_to_tenant(self, sample_tenant: str):
        """All inhibit rules are tenant-scoped."""
        rules = _build_inhibit_rules_crd(sample_tenant)
        for rule in rules:
            source_tenants = [
                m["value"] for m in rule["sourceMatch"] if m["name"] == "tenant"
            ]
            assert sample_tenant in source_tenants


# ──────────────────────────────────────────────────────────────────────────────
# AlertmanagerConfig CRD Integration Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestAlertmanagerConfigCRD:
    """End-to-end tests for build_alertmanager_config."""

    def test_default_produces_webhook_receiver(
        self, sample_tenant: str, namespace: str
    ):
        """Without --receiver-template, fallback to generic webhook."""
        crd = build_alertmanager_config(sample_tenant, namespace)
        assert crd["kind"] == "AlertmanagerConfig"
        receivers = crd["spec"]["receivers"]
        assert len(receivers) == 1
        assert "webhookConfigs" in receivers[0]

    @pytest.mark.parametrize("template", list(_RECEIVER_TEMPLATES))
    def test_receiver_template_produces_correct_crd(
        self, sample_tenant: str, namespace: str, template: str
    ):
        """Each receiver template produces valid AlertmanagerConfig."""
        crd = build_alertmanager_config(
            sample_tenant, namespace,
            receiver_template=template,
        )
        assert crd["apiVersion"] == "monitoring.coreos.com/v1beta1"
        assert crd["kind"] == "AlertmanagerConfig"
        assert crd["metadata"]["labels"]["tenant"] == sample_tenant
        # Must have inhibitRules
        assert "inhibitRules" in crd["spec"]
        assert len(crd["spec"]["inhibitRules"]) == 4
        # Must have matchers in route
        assert "matchers" in crd["spec"]["route"]

    def test_api_version_v1alpha1(self, sample_tenant: str, namespace: str):
        """v1alpha1 API version is supported."""
        crd = build_alertmanager_config(
            sample_tenant, namespace, api_version="v1alpha1"
        )
        assert crd["apiVersion"] == "monitoring.coreos.com/v1alpha1"

    def test_custom_secret_passthrough(
        self, sample_tenant: str, namespace: str
    ):
        """Custom secret name/key passed to receiver."""
        crd = build_alertmanager_config(
            sample_tenant, namespace,
            receiver_template="slack",
            secret_name="org-slack-secret",
            secret_key="url",
        )
        receiver = crd["spec"]["receivers"][0]
        secret = receiver["slackConfigs"][0]["apiURL"]["secret"]
        assert secret["name"] == "org-slack-secret"
        assert secret["key"] == "url"


# ──────────────────────────────────────────────────────────────────────────────
# Helm Chart Template Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestHelmChartThresholdExporter:
    """Verify threshold-exporter Helm chart v2.6.0 changes."""

    @pytest.fixture(scope="class")
    def chart_dir(self) -> Path:
        # P2b consolidation: Helm chart moved from components/threshold-exporter/
        # to helm/threshold-exporter/ (see commit cd357a3).
        return _REPO_ROOT / "helm" / "threshold-exporter"

    @pytest.fixture(scope="class")
    def chart_yaml(self, chart_dir: Path) -> dict:
        import yaml
        with open(chart_dir / "Chart.yaml") as f:
            return yaml.safe_load(f)

    @pytest.fixture(scope="class")
    def values_yaml(self, chart_dir: Path) -> dict:
        import yaml
        with open(chart_dir / "values.yaml") as f:
            return yaml.safe_load(f)

    def test_chart_version_260(self, chart_yaml: dict):
        """Chart version is 2.6.0."""
        assert chart_yaml["version"] == "2.6.0"

    def test_values_has_rules_mode(self, values_yaml: dict):
        """values.yaml has rules.mode field."""
        assert "rules" in values_yaml
        assert "mode" in values_yaml["rules"]
        assert values_yaml["rules"]["mode"] in ("configmap", "operator")

    def test_values_default_configmap_mode(self, values_yaml: dict):
        """Default mode is configmap (backward compatible)."""
        assert values_yaml["rules"]["mode"] == "configmap"

    def test_values_operator_section(self, values_yaml: dict):
        """Operator section has expected fields."""
        operator = values_yaml["rules"]["operator"]
        assert "ruleLabels" in operator
        assert "serviceMonitor" in operator
        assert "receiverTemplate" in operator
        assert "secretRef" in operator

    def test_values_secret_ref_empty_by_default(self, values_yaml: dict):
        """Secret ref is empty by default (must be user-provided)."""
        secret_ref = values_yaml["rules"]["operator"]["secretRef"]
        assert secret_ref["name"] == ""
        assert secret_ref["key"] == ""

    def test_servicemonitor_template_exists(self, chart_dir: Path):
        """ServiceMonitor template exists for operator mode."""
        sm_template = chart_dir / "templates" / "servicemonitor.yaml"
        assert sm_template.exists()

    def test_servicemonitor_template_conditional(self, chart_dir: Path):
        """ServiceMonitor template is conditional on operator mode."""
        sm_template = chart_dir / "templates" / "servicemonitor.yaml"
        content = sm_template.read_text()
        assert 'eq .Values.rules.mode "operator"' in content

    def test_deployment_template_handles_both_modes(self, chart_dir: Path):
        """Deployment template handles configmap and operator mode."""
        deployment = chart_dir / "templates" / "deployment.yaml"
        content = deployment.read_text()
        assert 'eq .Values.rules.mode "configmap"' in content


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot: AlertmanagerConfig CRD structure validation
# ──────────────────────────────────────────────────────────────────────────────


class TestAlertmanagerConfigSnapshot:
    """Snapshot-style tests for CRD structure stability."""

    def test_crd_structure_keys(self, sample_tenant: str, namespace: str):
        """CRD has exactly the expected top-level structure."""
        crd = build_alertmanager_config(
            sample_tenant, namespace, receiver_template="slack"
        )
        assert set(crd.keys()) == {"apiVersion", "kind", "metadata", "spec"}
        assert set(crd["metadata"].keys()) == {"name", "namespace", "labels"}
        assert set(crd["spec"].keys()) == {"route", "receivers", "inhibitRules"}

    def test_route_has_matchers(self, sample_tenant: str, namespace: str):
        """Route includes tenant matchers (v2.6.0 addition)."""
        crd = build_alertmanager_config(
            sample_tenant, namespace, receiver_template="slack"
        )
        matchers = crd["spec"]["route"]["matchers"]
        assert len(matchers) == 1
        assert matchers[0]["name"] == "tenant"
        assert matchers[0]["value"] == sample_tenant

    def test_receiver_template_count(self):
        """Exactly 6 receiver templates are supported."""
        assert len(_RECEIVER_TEMPLATES) == 6
        assert set(_RECEIVER_TEMPLATES) == {
            "slack", "pagerduty", "email", "teams", "opsgenie", "webhook"
        }
