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
from unittest.mock import patch

import pytest

# Add scripts/tools to path
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools"))

import operator_generate as og
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
        with open(chart_dir / "Chart.yaml", encoding='utf-8') as f:
            return yaml.safe_load(f)

    @pytest.fixture(scope="class")
    def values_yaml(self, chart_dir: Path) -> dict:
        import yaml
        with open(chart_dir / "values.yaml", encoding='utf-8') as f:
            return yaml.safe_load(f)

    def test_chart_version_290(self, chart_yaml: dict):
        """Chart version is 2.9.0."""
        assert chart_yaml["version"] == "2.9.0"

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
        content = sm_template.read_text(encoding="utf-8")
        assert 'eq .Values.rules.mode "operator"' in content

    def test_deployment_template_handles_both_modes(self, chart_dir: Path):
        """Deployment template handles configmap and operator mode."""
        deployment = chart_dir / "templates" / "deployment.yaml"
        # encoding= 顯式 UTF-8：template 含非 ASCII（em-dash 註解），
        # Windows host cp950 預設解碼會炸（host 假紅）。
        content = deployment.read_text(encoding="utf-8")
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


# ──────────────────────────────────────────────────────────────────────────────
# main() --json：單一文件形狀 (#1112)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def gen_dirs(tmp_path: Path):
    """最小可用的 (rule-packs, conf.d, output) 三件組。"""
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    (packs / "rule-pack-demo.yaml").write_text(
        "groups:\n"
        "  - name: demo_alerts\n"
        "    rules:\n"
        "      - alert: DemoHigh\n"
        "        expr: demo_metric > 80\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n",
        encoding="utf-8",
    )
    confd = tmp_path / "conf.d"
    confd.mkdir()
    (confd / "_defaults.yaml").write_text("defaults:\n  demo_metric: 80\n", encoding="utf-8")
    (confd / "tenant-one.yaml").write_text(
        "tenants:\n  tenant-one:\n    demo_metric: 90\n", encoding="utf-8")
    out = tmp_path / "out"
    return packs, confd, out


class TestMainJsonSingleDocument:
    """`--json` ⇒ stdout 恰好一個物件 `{crds, kustomization, summary}`。

    舊行為在 `--dry-run --json` 印 **CRD 陣列**，接著再印一份 **summary 物件**
    ——兩份背對背的文件，對整段 stdout 做 json.loads 會直接炸。`--kustomize`
    更會在中間插一行 `---`。本測試釘住「一份、且 top-level 鍵就是這三個」，
    而不只是「parse 得過」。
    """

    def _run_json(self, gen_dirs, capsys, *extra) -> dict:
        packs, confd, out = gen_dirs
        with patch("sys.argv", [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
            "--config-dir", str(confd),
            "--output-dir", str(out),
            "--json", *extra,
        ]):
            og.main()
        captured = capsys.readouterr()
        assert "---" not in captured.out        # 沒有 YAML 文件分隔線混進來
        return json.loads(captured.out)         # 全文 parse ⇒ 單一文件

    def test_dry_run_json_shape(self, gen_dirs, capsys):
        """`--dry-run --json` → 單一物件，top-level 鍵恰為 {crds, kustomization, summary}。"""
        doc = self._run_json(gen_dirs, capsys, "--dry-run")

        assert isinstance(doc, dict)                    # 不是 list（舊行為吐陣列）
        assert set(doc) == {"crds", "kustomization", "summary"}

        assert isinstance(doc["crds"], list)
        kinds = [c["kind"] for c in doc["crds"]]
        assert kinds == ["PrometheusRule", "AlertmanagerConfig", "ServiceMonitor"]

        # 沒有 --kustomize ⇒ 該欄確實是 null（不是 {} 也不是缺鍵）
        assert doc["kustomization"] is None

        # summary 保留原本那些計數鍵
        assert doc["summary"] == {
            "prometheus_rules": 1,
            "alertmanager_configs": 1,
            "service_monitor": 1,
            "kustomization": 0,
            "total": 3,
        }

    def test_kustomize_json_is_still_one_document(self, gen_dirs, capsys):
        """`--kustomize --json` → kustomization 進到**文件裡**，不是第二份 YAML/JSON。"""
        doc = self._run_json(gen_dirs, capsys, "--kustomize")

        assert set(doc) == {"crds", "kustomization", "summary"}
        assert doc["kustomization"]["kind"] == "Kustomization"
        assert doc["summary"]["kustomization"] == 1
        assert doc["summary"]["total"] == 4      # 3 CRD + 1 kustomization
        assert len(doc["crds"]) == 3

    def test_dry_run_kustomize_combo_keeps_kustomization(self, gen_dirs, capsys):
        """#1112 flag-matrix sweep: `--dry-run --kustomize` 是**獨立分支**，不能把
        kustomization 掉在地上。

        `--dry-run` 與 `--kustomize` 正交，先前只被分開測過（dry-run 那條的
        kustomization 本來就是 null，kustomize 那條走的是 write path）——組合起來
        走的是 dry-run 分支內的 kustomize 區塊，是一段誰都沒 in-process 行使過的碼。
        這正是 migrate_to_operator 那個 bug 的形狀：兩個正交 flag 只被分開測。
        """
        doc = self._run_json(gen_dirs, capsys, "--dry-run", "--kustomize")

        assert set(doc) == {"crds", "kustomization", "summary"}
        # dry-run 也必須把 kustomization 嵌進**同一份**文件（不是 None、不是第二份 doc）
        assert doc["kustomization"]["kind"] == "Kustomization"
        assert doc["kustomization"]["namespace"] == "monitoring"
        assert doc["summary"]["kustomization"] == 1
        assert doc["summary"]["total"] == 4
        assert len(doc["crds"]) == 3


# ---------------------------------------------------------------------------
# #1607 — unusable-entry reporting, called IN-PROCESS
# ---------------------------------------------------------------------------
#
# ⛔ The cross-tool harness in `tests/shared/test_confd_case_parity_across_tools.py`
# drives these tools through `subprocess`, which is right for asserting what an
# operator sees on their terminal but leaves the branches uncovered: this repo's
# `[tool.coverage.run]` sets no `concurrency`/`COVERAGE_PROCESS_START`, so a
# child process is not measured. Measured on the PR that added the harness:
# `operator_generate` -0.3%, `deprecate_rule` -2.6%, `offboard_tenant` -2.2%,
# while `custom_alerts/loader` — the one exercised in-process — went UP 2.0%.
# These tests call the functions directly, so the same branches are both
# asserted and measured.


def _unusable_confd(root: Path) -> Path:
    """conf.d with one real tenant and three entries no reader can read."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_defaults.yaml").write_text(
        "defaults:\n  pg_connections: 80\n", encoding="utf-8")
    (root / "alpha.yaml").write_text(
        "tenants:\n  alpha:\n    pg_connections: 90\n", encoding="utf-8")
    (root / "notes.yaml").mkdir()                       # directory-shaped
    (root / "broken.yaml").symlink_to(root / "gone.yaml")   # dangling
    return root


def test_discover_tenant_configs_names_unusable_entries(tmp_path, capsys):
    """The discovery site never opens a file — it takes the stem as a tenant
    name — so an unusable entry cannot fall into any `except`. Dropping it is
    the correctness half; naming it is the half that keeps the signal."""
    tree = _unusable_confd(tmp_path / "conf.d")

    tenants = og.discover_tenant_configs(tree)

    assert tenants == ["alpha"], (
        f"a directory and a dangling symlink must not become tenants; "
        f"got {tenants}")
    err = capsys.readouterr().err
    assert "notes.yaml" in err and "broken.yaml" in err, (
        f"both unusable entries must be named on stderr; got {err!r}")
    assert "is a directory, not a config file" in err
    assert "is a broken symlink" in err


def test_discover_tenant_configs_is_silent_about_reserved_entries(
        tmp_path, capsys):
    """`_`-prefixed entries are dropped whatever their shape, so naming an
    unusable one would report a loss that did not happen in this tool."""
    tree = _unusable_confd(tmp_path / "conf.d")
    (tree / "_defaults.yaml").unlink()
    (tree / "_defaults.yaml").mkdir()

    og.discover_tenant_configs(tree)

    err = capsys.readouterr().err
    assert "_defaults.yaml" not in err, (
        f"reported a reserved entry this function never reads; got {err!r}")
    assert "notes.yaml" in err, "non-reserved entries must still be named"


def test_discover_tenant_configs_is_quiet_on_a_clean_tree(tmp_path, capsys):
    """⛔ Blast radius: a healthy conf.d must gain no new output."""
    tree = tmp_path / "conf.d"
    tree.mkdir()
    (tree / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
    (tree / "alpha.yaml").write_text(
        "tenants:\n  alpha: {}\n", encoding="utf-8")

    assert og.discover_tenant_configs(tree) == ["alpha"]
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


# ── the extension-SPELLING axis (#1603) ───────────────────────────────
#
# The last of the four readers #1603 names. Both selection sites here used
# to pass `suffixes=(".yaml",)` while the exporter's scanner
# (`config_hierarchy.go:195`) lowercases the entry name and accepts BOTH
# spellings — and this is the reader where that costs a KUBERNETES OBJECT
# rather than a report line. Measured on two trees whose contents are
# byte-identical and differ only in the extension:
#
#     db-a.yaml -> 18 CRDs, including `da-tenant-db-a.yaml`   <- control
#     db-a.yml  -> 17 CRDs, no tenant CRD at all              <- before
#     db-a.yml  -> 18 CRDs, including `da-tenant-db-a.yaml`   <- after
#
# rc=0 in every row, and in the `.yml` row the string `db-a` appears in
# NEITHER stdout NOR stderr.
#
# ⚠️ SCOPE — the other axes are NOT covered here. Open tickets DO cover
# parts of them (#1630, #1604), but neither is about THIS reader, so read
# these notes rather than the ticket numbers:
#   * Hidden names: dot-prefixed carriers DO reach the loop (this module
#     imports `is_reserved_name` but not `is_hidden_name`) while the
#     exporter skips them (`config_hierarchy.go:181,190`). ⚠️ But they
#     cannot become tenants here, and the reason is structural rather than
#     intentional: the id is the file STEM, `Path(".hidden.yaml").stem` is
#     `".hidden"`, and `_TENANT_NAME_RE` requires `^[a-z0-9]`. The two ends
#     therefore agree on the OUTCOME by way of different mechanisms, and
#     the whole observable divergence is one extra `Skipping invalid tenant
#     name` line on stderr. That makes `validate_tenant_name` load-bearing
#     for this note, so the ceiling test below pins the dot case.
#     ⚠️ Ticket-wise: #1630 IS the open Python-reader hidden-axis ticket,
#     but its named subjects are `run_chaos_soak.trigger_reload` and
#     `check_threshold_unit_sanity._iter_yaml_files` — the two places where
#     the axis BITES; here it is inert for the reason above. #1589 is also
#     open on the hidden axis, with the exporter's `pkg/config` enumerator
#     as its subject rather than any Python reader.
#   * Recursion: this reader is flat (`config_dir.iterdir()`).
#   * ⛔ RAW GLOBS are structurally invisible to a census keyed on literal
#     suffix tuples, so the ones that read a conf.d are named rather than
#     counted: the SIBLING `migrate_to_operator.discover_tenant_configs` —
#     same name, same job — narrows via `config_dir.glob("*.yaml")`, and so
#     do `config_history.py:66` (`_scan_config_dir`, whose parameter is
#     literally `config_dir`) and `drift_detect.py:151`. On Linux a raw
#     glob is blind to `.yml` AND to `upper.YAML`. All out of scope here.
#     ⚠️ Ticket-wise: #1604 is open on the `migrate_to_operator` twin and
#     that twin is its whole subject; no ticket covers the others.


class TestDiscoverTenantConfigsExtensionSpelling:
    """`.yml` carriers must yield exactly what `.yaml` ones do — no more."""

    @staticmethod
    def _seed(root: Path, ext: str) -> Path:
        """One conf.d whose every carrier uses `ext`. Bodies never change."""
        root.mkdir(parents=True, exist_ok=True)
        (root / f"_defaults{ext}").write_text(
            "defaults:\n  pg_connections: 80\n", encoding="utf-8")
        for tid in ("db-a", "db-b"):
            (root / f"{tid}{ext}").write_text(
                f"tenants:\n  {tid}:\n    pg_connections: 90\n",
                encoding="utf-8")
        return root

    def test_discovery_agrees_across_extension_spellings(self, tmp_path):
        """FLOOR. Two trees, same bytes, different extension → same answer.

        ⛔ An EQUALITY between the two runs, not "`.yml` is accepted": the
        latter is satisfied by a reader that takes `.yml` and drops `.yaml`.

        ⚠️ Attribution, measured rather than assumed: the detection lives
        in the pinned-list guard below, on BOTH sides. Once both guards
        pass, `assert a == b` holds by construction — blind review swapped
        it for `a == a` and no verdict in this class changed. Keep it as
        the statement of the property, but do NOT weaken the guard on the
        theory that the equality still holds the line. A THIRD spelling is
        covered by `test_every_spelling_the_shared_set_names_is_discovered`,
        not here.
        """
        a = og.discover_tenant_configs(self._seed(tmp_path / "yaml", ".yaml"))
        b = og.discover_tenant_configs(self._seed(tmp_path / "yml", ".yml"))

        # ⛔ Vacuity guard FIRST, on BOTH sides: two trees this cannot read
        # at all also compare equal. Pinned as the concrete list so that
        # gutting the comparison cannot silently disarm the equality.
        for label, got in (("`.yaml`", a), ("`.yml`", b)):
            assert got == ["db-a", "db-b"], (
                f"the {label} tree discovered {got}, not ['db-a', 'db-b'] — "
                f"the equality below would prove nothing"
            )
        assert a == b, (
            f"operator-generate discovers a different tenant set for a "
            f"conf.d whose only difference is `.yaml` vs `.yml`, both of "
            f"which the exporter serves:\n  .yaml: {a}\n  .yml : {b}"
        )

    def test_an_unusable_yml_carrier_is_named_and_a_json_one_is_not(
            self, tmp_path, capsys):
        """FLOOR **and** CEILING for the second site (`unusable_config_...`).

        The equality above only exercises the `has_yaml_extension` site.
        Reverting this one alone leaves that equality green — the same gap
        that survived the first round of the sibling fix (#1663).

        ⛔ The ceiling half is the mirror of the fix and had no witness
        until blind review measured it: widening THIS call site to include
        `.json` survived every test in the module. That direction is a real
        defect, not a hypothetical — `unusable_config_entries`'s `suffixes`
        argument exists precisely so a tool does not report what it would
        never read, i.e. a finding the operator cannot act on.

        ⛔ A DIRECTORY named like a config file, not a broken symlink: the
        existing `_unusable_confd` helper above uses `symlink_to`, which
        needs administrator rights on Windows, so a symlink fixture would be
        skipped on the host most of this repo's maintainers use.

        ⛔ The assertion names the REASON, not just the filename. This
        module has other stderr stations (the RFC-1123 rejection below, and
        `warn_nested`), and a filename-only assertion cannot tell which one
        spoke.
        """
        for ext in (".yaml", ".yml"):
            root = self._seed(tmp_path / ext.lstrip("."), ext)
            (root / f"db-broken{ext}").mkdir()
            # Same shape, but an extension this tool never reads.
            (root / "db-payload.json").mkdir()

            tenants = og.discover_tenant_configs(root)
            err = capsys.readouterr().err

            assert tenants == ["db-a", "db-b"], (
                f"a directory named `db-broken{ext}` became a tenant: "
                f"{tenants}"
            )
            named = [ln for ln in err.splitlines()
                     if f"db-broken{ext}" in ln
                     and "is a directory, not a config file" in ln]
            assert len(named) == 1, (
                f"the unusable entry must be named exactly once, with the "
                f"reason it could not be used; stderr was {err!r}"
            )
            assert "db-payload.json" not in err, (
                f"a carrier this tool never reads was reported as unusable "
                f"— that is a finding the operator cannot act on from this "
                f"tool; stderr was {err!r}"
            )

    def test_carriers_the_exporter_would_not_serve_are_not_tenants(
            self, tmp_path, capsys):
        """CEILING, by counterexample — that is all a ceiling can be.

        ⛔ You cannot enumerate the complement of an accept-set, so this
        pins THREE carriers, each stopped by a DIFFERENT mechanism:

          * `db-json.json` — over-widening a call site to
            `(".yaml", ".yml", ".json")` is a single token, and
            `has_yaml_extension`'s own docstring says this argument gets
            touched. Measured on this change's base: with that widening
            applied and this class absent, the CI-exact suite is green.
          * `_profiles.yaml` — reserved. ⛔ The stderr half is what makes
            this half of the name provable: a `_`-prefixed stem fails RFC
            1123 anyway, so dropping the `is_reserved_name` filter does not
            change the RETURNED list at all. Blind review measured exactly
            that — before the silence assertion, this carrier could not
            testify under any single-point mutation.
          * `.hidden.yaml` — hidden. It reaches the loop (see SCOPE above)
            and is stopped only by `validate_tenant_name`, which nothing
            else in this repo pins: blind review made that gate `return
            True` and 429 tests stayed green. This is what keeps the SCOPE
            note above true.

        ⚠️ Here the tenant id comes from the file STEM, so a `.json` carrier
        needs no `tenants:` key to become a tenant — the opposite of the
        oracle trap that made the sibling fix's first ceiling test vacuous
        (#1663: there the id came from file CONTENT, so asserting on the
        stem could never fail).

        ⚠️ Three samples are still three samples. Over-widening the call
        site to an extension NONE of them use (measured: `.txt`) has no
        witness here, and adding one more sample would not change that —
        the honest boundary is the sentence at the top of this docstring.
        """
        root = self._seed(tmp_path / "confd", ".yaml")
        (root / "db-json.json").write_text(
            '{"tenants": {"db-json": {"pg_connections": 90}}}\n',
            encoding="utf-8")
        (root / "_profiles.yaml").write_text(
            "profiles:\n  x: 1\n", encoding="utf-8")
        (root / ".hidden.yaml").write_text(
            "tenants:\n  hidden:\n    pg_connections: 90\n",
            encoding="utf-8")

        tenants = og.discover_tenant_configs(root)
        err = capsys.readouterr().err

        assert tenants == ["db-a", "db-b"], (
            f"a carrier the exporter does not serve became a tenant, so a "
            f"CRD would be emitted for it: {tenants}"
        )
        assert "_profiles" not in err, (
            f"a reserved carrier was mentioned on stderr; the exporter "
            f"drops these silently and so must this reader — this is the "
            f"only observable signal the `is_reserved_name` filter has. "
            f"stderr was {err!r}"
        )

    def test_every_spelling_the_shared_set_names_is_discovered(self, tmp_path):
        """FLOOR, derived: one carrier per member of `CONFIG_SUFFIXES`.

        The tests above hard-code `.yaml` and `.yml`, so they stop covering
        the floor the day the exporter grows a third spelling and
        `_lib_confd.CONFIG_SUFFIXES` follows it. This reads the set instead
        of restating it.

        ⛔ It does NOT re-implement `has_yaml_extension`; it uses the shared
        CONSTANT, whose agreement with the exporter is pinned by
        `tests/shared/confd_name_classification_matrix.json` (asserted from
        the Go side and the Python side). ⚠️ Floor only — a too-WIDE reader
        satisfies it, which is what the counterexample test is for.
        """
        from _lib_confd import CONFIG_SUFFIXES  # noqa: PLC0415

        # ⛔ Anti-vacuity: an empty set makes the assertion `[] == []`.
        # Guarding that set is not this test's job, so name who does.
        assert len(CONFIG_SUFFIXES) >= 2, (
            f"CONFIG_SUFFIXES collapsed to {CONFIG_SUFFIXES!r}; the shared "
            f"classification matrix should have gone red first"
        )

        root = tmp_path / "confd"
        root.mkdir()
        (root / "_defaults.yaml").write_text(
            "defaults:\n  pg_connections: 80\n", encoding="utf-8")
        for i, suffix in enumerate(CONFIG_SUFFIXES):
            (root / f"t{i}{suffix}").write_text(
                f"tenants:\n  t{i}:\n    pg_connections: 90\n",
                encoding="utf-8")

        tenants = og.discover_tenant_configs(root)
        expected = sorted(f"t{i}" for i in range(len(CONFIG_SUFFIXES)))

        assert tenants == expected, (
            f"one carrier was written per member of CONFIG_SUFFIXES "
            f"({CONFIG_SUFFIXES!r}) and discovery returned {tenants}, "
            f"expected {expected}"
        )
