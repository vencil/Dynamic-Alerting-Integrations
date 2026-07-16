#!/usr/bin/env python3
"""Tests for migrate_to_operator.py — ConfigMap → CRD migration tool.

Verifies:
  1. parse_configmap_rules() — Parse ConfigMap YAML with rule groups
  2. convert_rules_to_crd() — Convert to PrometheusRule CRD structure
  3. analyze_migration() — Analyze migration scope and identify issues
  4. build_migration_checklist() — Generate 6-phase migration checklist
  5. generate_migration() — Full E2E orchestration
  6. RFC 1123 validation for tenant names
  7. Dry-run mode (no file writes)
  8. JSON output mode
  9. ConfigMap → CRD end-to-end conversion
"""

import dataclasses
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts', 'tools', 'ops'))

# Import the module to test
import migrate_to_operator as mto  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_configmap_dir():
    """Create a temporary directory with sample ConfigMap YAML files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create a sample ConfigMap with rules
        cm_yaml = textwrap.dedent("""
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: prometheus-rules-cpu
            data:
              cpu-rules.yaml: |
                groups:
                  - name: cpu_alerts
                    rules:
                      - alert: HighCPU
                        expr: cpu_usage > 80
                        for: 5m
                        labels:
                          severity: warning
                      - alert: CriticalCPU
                        expr: cpu_usage > 95
                        for: 2m
                        labels:
                          severity: critical
        """).strip()

        (tmppath / "cpu-rules.yaml").write_text(cm_yaml)

        # Create another ConfigMap with different rules
        cm_yaml_2 = textwrap.dedent("""
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: prometheus-rules-memory
            data:
              memory-rules.yaml: |
                groups:
                  - name: memory_alerts
                    rules:
                      - alert: HighMemory
                        expr: memory_usage > 85
                        for: 5m
                        labels:
                          severity: warning
        """).strip()

        (tmppath / "memory-rules.yaml").write_text(cm_yaml_2)

        yield tmppath


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory with sample tenant config files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create tenant config files
        tenant_a = textwrap.dedent("""
            tenants:
              db-a:
                cpu_threshold_warning: 80
                cpu_threshold_critical: 95
                memory_threshold_warning: 85
        """).strip()

        tenant_b = textwrap.dedent("""
            tenants:
              db-b:
                cpu_threshold_warning: 75
                cpu_threshold_critical: 90
        """).strip()

        (tmppath / "db-a.yaml").write_text(tenant_a)
        (tmppath / "db-b.yaml").write_text(tenant_b)

        yield tmppath


@pytest.fixture
def temp_output_dir():
    """Create a temporary directory for output CRDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ────────────────────────────────────────────────────────────────────────────
# Test: parse_configmap_rules
# ────────────────────────────────────────────────────────────────────────────

class TestParseConfigMapRules:
    """Tests for parse_configmap_rules()."""

    def test_parse_configmap_rules_basic(self, temp_configmap_dir):
        """Parse a simple ConfigMap YAML with 1 rule group."""
        rules = mto.parse_configmap_rules(temp_configmap_dir)

        # Should have parsed 2 ConfigMap files
        assert len(rules) == 2

        # Verify structure of first result
        first = next((r for r in rules if "cpu" in r["file"]), None)
        assert first is not None
        assert first["name"] == "prometheus-rules-cpu"
        assert first["file"] == "cpu-rules.yaml"
        assert len(first["rule_groups"]) == 1
        assert first["rule_groups"][0]["name"] == "cpu_alerts"

    def test_parse_configmap_rules_multiple(self, temp_configmap_dir):
        """Parse multiple ConfigMap files."""
        rules = mto.parse_configmap_rules(temp_configmap_dir)

        # Verify we got both files
        files = {r["file"] for r in rules}
        assert "cpu-rules.yaml" in files
        assert "memory-rules.yaml" in files

        # Verify rule group counts
        total_groups = sum(len(r["rule_groups"]) for r in rules)
        assert total_groups == 2

    def test_parse_configmap_rules_invalid_yaml(self, temp_configmap_dir):
        """Graceful handling of invalid YAML."""
        # Add an invalid YAML file
        invalid_yaml = "invalid: yaml: content: ["
        (temp_configmap_dir / "invalid.yaml").write_text(invalid_yaml)

        # Should not raise, but skip invalid files
        rules = mto.parse_configmap_rules(temp_configmap_dir)
        files = {r["file"] for r in rules}
        assert "invalid.yaml" not in files
        assert len(rules) == 2  # Still got the 2 valid files

    def test_parse_configmap_rules_directory_not_found(self):
        """FileNotFoundError when directory does not exist."""
        with pytest.raises(FileNotFoundError):
            mto.parse_configmap_rules(Path("/nonexistent/path"))


# ────────────────────────────────────────────────────────────────────────────
# Test: convert_rules_to_crd
# ────────────────────────────────────────────────────────────────────────────

class TestConvertRulesToCRD:
    """Tests for convert_rules_to_crd()."""

    def test_convert_rules_to_crd(self):
        """Verify CRD output structure (apiVersion, kind, metadata, spec.groups)."""
        rule_groups = [
            {
                "name": "cpu_alerts",
                "rules": [
                    {"alert": "HighCPU", "expr": "cpu > 80", "for": "5m"},
                ],
            },
        ]

        crd = mto.convert_rules_to_crd(rule_groups, "cpu-pack", "monitoring")

        # Verify structure
        assert crd["apiVersion"] == "monitoring.coreos.com/v1"
        assert crd["kind"] == "PrometheusRule"
        assert crd["metadata"]["name"] == "da-rule-pack-cpu-pack"
        assert crd["metadata"]["namespace"] == "monitoring"
        assert len(crd["spec"]["groups"]) == 1
        assert crd["spec"]["groups"][0]["name"] == "cpu_alerts"

    def test_convert_rules_preserves_labels(self):
        """Ensure 'migrated-from: configmap' label is present."""
        rule_groups = [{"name": "test", "rules": []}]

        crd = mto.convert_rules_to_crd(rule_groups, "test", "monitoring")

        labels = crd["metadata"]["labels"]
        assert labels["migrated-from"] == "configmap"
        assert labels["app.kubernetes.io/part-of"] == "dynamic-alerting"
        assert labels["prometheus"] == "kube-prometheus"


# ────────────────────────────────────────────────────────────────────────────
# Test: analyze_migration
# ────────────────────────────────────────────────────────────────────────────

class TestAnalyzeMigration:
    """Tests for analyze_migration()."""

    def test_analyze_migration(self, temp_configmap_dir, temp_config_dir):
        """Check analysis counts (ConfigMaps, rule groups, tenants, estimated CRDs)."""
        analysis = mto.analyze_migration(temp_configmap_dir, temp_config_dir)

        # Verify counts
        assert analysis["configmap_files"] == 2
        assert analysis["rule_groups"] == 2
        assert analysis["tenants"] == 2
        assert analysis["estimated_crds"] == 4  # 2 ConfigMaps + 2 tenants
        assert len(analysis["issues"]) == 0

    def test_analyze_migration_invalid_tenant(self, temp_configmap_dir):
        """RFC 1123 validation catches bad tenant names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create invalid tenant name (starts with hyphen)
            (tmppath / "-invalid.yaml").write_text("tenants: {}")

            # Create valid tenant name
            (tmppath / "valid-tenant.yaml").write_text("tenants: {}")

            analysis = mto.analyze_migration(temp_configmap_dir, tmppath)

            # Should report the invalid tenant name
            assert len(analysis["issues"]) > 0
            assert any("RFC 1123" in issue or "invalid" in issue.lower()
                      for issue in analysis["issues"])
            assert analysis["tenants"] == 1  # Only the valid one


# ────────────────────────────────────────────────────────────────────────────
# Test: validate_tenant_name
# ────────────────────────────────────────────────────────────────────────────

class TestValidateTenantName:
    """Tests for validate_tenant_name()."""

    @pytest.mark.parametrize("name,expected", [
        ("db-a", True),
        ("db-b", True),
        ("tenant-1", True),
        ("a", True),
        ("ab", True),
        ("-invalid", False),
        ("invalid-", False),
        ("UPPERCASE", False),
        ("under_score", False),
        ("db..a", False),
    ])
    def test_validate_tenant_name(self, name, expected):
        """RFC 1123 compliance checks."""
        assert mto.validate_tenant_name(name) == expected


# ────────────────────────────────────────────────────────────────────────────
# Test: build_migration_checklist
# ────────────────────────────────────────────────────────────────────────────

class TestBuildMigrationChecklist:
    """Tests for build_migration_checklist()."""

    def test_build_migration_checklist_contains_steps(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Checklist has all 6 phases."""
        result = {
            "configmap_files": 2,
            "rule_group_count": 2,
            "tenants": 2,
            "prometheus_rules": [{"crd": {}}],
            "alertmanager_configs": [{"crd": {}}],
        }

        checklist = mto.build_migration_checklist(
            temp_configmap_dir, temp_config_dir, temp_output_dir, result
        )

        # Check that all phases are present
        phases = [
            "Phase 1",
            "Phase 2",
            "Phase 3",
            "Phase 4",
            "Phase 5",
            "Phase 6",
        ]

        for phase in phases:
            assert phase in checklist

        # Check that key sections exist
        assert "Migration Checklist" in checklist or "遷移檢核清單" in checklist


# ────────────────────────────────────────────────────────────────────────────
# Test: generate_migration
# ────────────────────────────────────────────────────────────────────────────

class TestGenerateMigration:
    """Tests for generate_migration()."""

    def test_generate_migration_writes_crds(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Full run creates output directory with CRD files + checklist."""
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify result structure
        assert result["configmap_files"] == 2
        assert result["rule_group_count"] == 2
        assert result["tenants"] == 2
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2
        assert len(result["errors"]) == 0

        # Verify PrometheusRule CRD fields
        for item in result["prometheus_rules"]:
            assert "name" in item
            assert "crd" in item
            crd = item["crd"]
            assert crd["kind"] == "PrometheusRule"
            assert crd["metadata"]["labels"]["migrated-from"] == "configmap"

        # Verify AlertmanagerConfig CRD fields
        for item in result["alertmanager_configs"]:
            assert "name" in item
            assert "tenant" in item
            assert "crd" in item
            crd = item["crd"]
            assert crd["kind"] == "AlertmanagerConfig"
            assert crd["metadata"]["labels"]["migrated-from"] == "configmap"

    def test_generate_migration_json_output(self, temp_configmap_dir, temp_config_dir):
        """JSON mode returns valid JSON with all fields."""
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            Path("/tmp/dummy"),
            "monitoring",
        )

        # Verify that result can be serialized to JSON
        json_str = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        parsed = json.loads(json_str)

        # Verify essential fields
        assert "configmap_files" in parsed
        assert "rule_group_count" in parsed
        assert "tenants" in parsed
        assert "prometheus_rules" in parsed
        assert "alertmanager_configs" in parsed


# ────────────────────────────────────────────────────────────────────────────
# Test: End-to-End
# ────────────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    """End-to-end migration tests."""

    def test_end_to_end_configmap_to_crd(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Full E2E: create temp files, run migration, verify CRD output."""
        # Run full migration
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got CRDs
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2

        # Verify PrometheusRule structure
        pr = result["prometheus_rules"][0]["crd"]
        assert pr["apiVersion"] == "monitoring.coreos.com/v1"
        assert pr["kind"] == "PrometheusRule"
        assert "spec" in pr
        assert "groups" in pr["spec"]

        # Verify AlertmanagerConfig structure
        ac = result["alertmanager_configs"][0]["crd"]
        assert ac["apiVersion"] == "monitoring.coreos.com/v1beta1"
        assert ac["kind"] == "AlertmanagerConfig"
        assert "spec" in ac
        assert "route" in ac["spec"]
        assert "receivers" in ac["spec"]
        assert "inhibitRules" in ac["spec"]


# ────────────────────────────────────────────────────────────────────────────
# Test: Dry-run Mode
# ────────────────────────────────────────────────────────────────────────────

class TestDryRunMode:
    """Tests for dry-run functionality."""

    def test_generate_migration_dry_run_produces_no_files(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Dry run produces result dict but no files are written."""
        # Note: generate_migration() itself doesn't write files
        # (that's done in main()), so we're testing the core behavior

        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got a result
        assert result is not None
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2


# ────────────────────────────────────────────────────────────────────────────
# Test: main() --json envelope 形狀 (#1112)
# ────────────────────────────────────────────────────────────────────────────

class TestMainJsonEnvelope:
    """`--checklist-only --json` 的 envelope 形狀 + checklist 必須真的送到 stdout。"""

    def _run(self, argv_tail, configmap_dir, config_dir, output_dir):
        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(configmap_dir),
            "--config-dir", str(config_dir),
            "--output-dir", str(output_dir),
            *argv_tail,
        ]):
            mto.main()

    def _assert_checklist_envelope(self, captured):
        """checklist-only envelope 的共同形狀斷言。"""
        doc = json.loads(captured.out)        # 全文 parse ⇒ stdout 只有 JSON

        assert doc["status"] == "checklist_only"
        # checklist 是 --checklist-only 的**全部意義**——它必須在文件裡且非空。
        # 只做 json.loads 的 gate 擋不住「checklist 欄整個不見」，這正是
        # CodeRabbit 抓到的 bug 溜過去的原因。
        assert isinstance(doc["checklist"], str)
        assert doc["checklist"].strip()
        assert "Migration Checklist" in doc["checklist"] or "遷移檢核清單" in doc["checklist"]

        # summary 的既有 schema 鍵都還在
        assert set(doc) == {
            "configmap_files", "rule_groups", "tenants",
            "prometheus_rules", "alertmanager_configs", "total_crds",
            "status", "checklist",
        }
        # checklist-only ⇒ 沒有產生任何 CRD，這三個計數確實是 0
        assert doc["prometheus_rules"] == 0
        assert doc["alertmanager_configs"] == 0
        assert doc["total_crds"] == 0
        # 但「掃到的東西」是真實計數（fixture: 2 個 ConfigMap、2 個租戶）
        assert doc["configmap_files"] == 2
        assert doc["tenants"] == 2

        # 人類訊息在 stderr，stdout 不含散文（checklist 是 JSON 字串值，不是裸 Markdown）
        assert "Analyzing migration scope" in captured.err
        assert not captured.out.lstrip().startswith("#")

    def test_checklist_only_json_envelope(
        self, temp_configmap_dir, temp_config_dir, temp_output_dir, capsys,
    ):
        """#1112: `--checklist-only --json` → checklist 在 `checklist` 欄裡。"""
        self._run(["--checklist-only", "--json"],
                  temp_configmap_dir, temp_config_dir, temp_output_dir)
        self._assert_checklist_envelope(capsys.readouterr())

    def test_checklist_only_with_dry_run_json_envelope(
        self, temp_configmap_dir, temp_config_dir, temp_output_dir, capsys,
    ):
        """#1112 (CodeRabbit): `--checklist-only --dry-run --json` 也必須給 checklist。

        REGRESSION。舊碼的 `if args.checklist_only and not args.dry_run:` 讓這個
        組合掉進 dry-run 分支，吐出一份**空的 CRD preview**（keys 是
        metadata/prometheus_rules/alertmanager_configs/errors，兩個 list 都空），
        `checklist` 欄根本不存在——但它是合法 JSON，所以 subprocess gate 全綠。
        caller 要 checklist，拿到空預覽。

        `--checklist-only` 本來就隱含「不寫檔」，`--dry-run` 疊上去不該改變
        payload 的選擇。這條測試釘住「窄的 no-op flag 不得覆蓋選 payload 的 flag」。
        """
        self._run(["--checklist-only", "--dry-run", "--json"],
                  temp_configmap_dir, temp_config_dir, temp_output_dir)
        captured = capsys.readouterr()

        doc = json.loads(captured.out)
        # bug 的簽名：dry-run 的 CRD-preview 鍵出現在 checklist-only 的輸出裡
        assert "metadata" not in doc, (
            "--checklist-only --dry-run 掉回 dry-run 的 CRD-preview 分支了"
        )
        self._assert_checklist_envelope(captured)

    def test_dry_run_json_is_crd_preview_not_summary(
        self, temp_configmap_dir, temp_config_dir, temp_output_dir, capsys,
    ):
        """對照組：沒有 --checklist-only 時，`--dry-run --json` 仍是單一 CRD preview。

        釘住修法的另一半——`checklist_for_json is not None` 這個 escape hatch
        不能讓純 dry-run 也追加一份 summary（那會變成 stdout 兩份文件、
        json.loads 直接炸）。
        """
        self._run(["--dry-run", "--json"],
                  temp_configmap_dir, temp_config_dir, temp_output_dir)
        captured = capsys.readouterr()

        doc = json.loads(captured.out)        # 兩份文件的話這裡就炸了
        assert set(doc) == {
            "metadata", "prometheus_rules", "alertmanager_configs", "errors",
        }
        assert "checklist" not in doc
        assert "status" not in doc
        assert len(doc["prometheus_rules"]) == 2      # 真的有預覽內容
        assert len(doc["alertmanager_configs"]) == 2


# ────────────────────────────────────────────────────────────────────────────
# Test: 輸出模式矩陣 characterization — (checklist_only × dry_run × json) 全 8 組
# ────────────────────────────────────────────────────────────────────────────
#
# 同一個 PR 週期內這張矩陣咬過兩次（#1112 / CodeRabbit）：
#   bug 1: `--checklist-only --dry-run` 掉進 dry-run 分支 → 合法 JSON 但沒
#          checklist 欄（stdout 契約 gate 全綠——合法 JSON ≠ 對的 payload）
#   bug 2: `--checklist-only --dry-run --json` 下 summary 被抑制 → stdout 空白
# 兩個修補都是往 main() 加條件。這張表把 8 組合的**現行為**全部 pin 死：
# 每列斷言 stdout 的 payload 種類（不只 json.loads 成功）、stderr 訊息面、
# exit code、檔案寫入副作用。之後任何矩陣決策的改動都會在這裡現形。


@dataclasses.dataclass(frozen=True)
class ComboRow:
    """一列 = 一個 flag 組合的預期現況。"""
    checklist_only: bool
    dry_run: bool
    json_mode: bool
    # stdout payload 種類 discriminator（詳 _PAYLOAD_VALIDATORS）：
    #   empty                   — stdout 完全空白（write 文字模式）
    #   json_summary            — 單一 JSON：6 鍵 summary（無 status/checklist）
    #   text_checklist_preview  — "# MIGRATION CHECKLIST" + "# CRD PREVIEW" + YAML
    #   json_crd_preview        — 單一 JSON：{metadata, prometheus_rules,
    #                             alertmanager_configs, errors}
    #   text_checklist          — 裸 checklist markdown（無 CRD preview）
    #   json_checklist_envelope — 單一 JSON：6 鍵 summary + status + checklist
    payload: str
    writes_files: bool          # output_dir 是否寫出檔案（CRD ×4 + checklist）


# 8 列全矩陣。fixture 現況：2 個 ConfigMap（各 1 rule group）、2 個租戶
# ⇒ 生成模式下 2 PrometheusRules + 2 AlertmanagerConfigs = 4 CRDs。
OUTPUT_MODE_MATRIX = [
    ComboRow(False, False, False, "empty", True),
    ComboRow(False, False, True, "json_summary", True),
    ComboRow(False, True, False, "text_checklist_preview", False),
    ComboRow(False, True, True, "json_crd_preview", False),
    ComboRow(True, False, False, "text_checklist", False),
    ComboRow(True, False, True, "json_checklist_envelope", False),
    # bug-1 pin：--checklist-only 勝過 --dry-run（窄的 no-op flag 不得覆蓋
    # 選 payload 的 flag）⇒ 與 (True, False, *) 兩列同 payload。
    ComboRow(True, True, False, "text_checklist", False),
    # bug-2 pin：combo + --json 時 summary envelope 不得被 dry-run 抑制
    # ⇒ stdout 是非空的單一 checklist envelope，不是空白。
    ComboRow(True, True, True, "json_checklist_envelope", False),
]

_MATRIX_IDS = [
    f"C{int(r.checklist_only)}-D{int(r.dry_run)}-J{int(r.json_mode)}-{r.payload}"
    for r in OUTPUT_MODE_MATRIX
]

_SUMMARY_KEYS = {
    "configmap_files", "rule_groups", "tenants",
    "prometheus_rules", "alertmanager_configs", "total_crds",
}


def _checklist_header_in(text):
    return "# Migration Checklist" in text or "# 遷移檢核清單" in text


def _validate_empty(out):
    assert out == ""


def _validate_json_summary(out):
    doc = json.loads(out)                      # 全文 parse ⇒ stdout 恰一份文件
    assert set(doc) == _SUMMARY_KEYS
    assert doc["configmap_files"] == 2
    assert doc["rule_groups"] == 2
    assert doc["tenants"] == 2
    assert doc["prometheus_rules"] == 2
    assert doc["alertmanager_configs"] == 2
    assert doc["total_crds"] == 4


def _validate_text_checklist_preview(out):
    # dry-run 文字模式的 banner 是 hardcode 英文，語言無關
    assert out.startswith("# MIGRATION CHECKLIST\n")
    assert _checklist_header_in(out)
    assert "# CRD PREVIEW" in out
    # preview 真的載有兩種 CRD 的 YAML（不是空殼）
    assert out.count("kind: PrometheusRule") == 2
    assert out.count("kind: AlertmanagerConfig") == 2


def _validate_json_crd_preview(out):
    doc = json.loads(out)
    assert set(doc) == {
        "metadata", "prometheus_rules", "alertmanager_configs", "errors",
    }
    # 不是 checklist envelope（bug-1 的反向保證：純 dry-run 不長出 checklist）
    assert "checklist" not in doc
    assert "status" not in doc
    assert len(doc["prometheus_rules"]) == 2
    assert len(doc["alertmanager_configs"]) == 2
    assert doc["errors"] == []
    assert doc["metadata"]["configmap_files"] == 2
    assert doc["metadata"]["rule_groups"] == 2
    assert doc["metadata"]["tenants"] == 2
    assert doc["metadata"]["namespace"] == "monitoring"


def _validate_text_checklist(out):
    # 裸 checklist markdown：以 checklist 標題開頭（zh/en 依環境）
    assert out.startswith("# Migration Checklist") or out.startswith("# 遷移檢核清單")
    # 不得帶 dry-run 的 CRD preview 尾巴（bug-1 的行為面）
    assert "# MIGRATION CHECKLIST" not in out      # dry-run banner（全大寫）
    assert "# CRD PREVIEW" not in out
    assert "kind: PrometheusRule" not in out
    assert "kind: AlertmanagerConfig" not in out
    # 6 個 phase 都在
    for phase in range(1, 7):
        assert f"Phase {phase}" in out


def _validate_json_checklist_envelope(out):
    doc = json.loads(out)
    assert set(doc) == _SUMMARY_KEYS | {"status", "checklist"}
    assert doc["status"] == "checklist_only"
    # checklist 是 --checklist-only 的全部意義：必須在、必須非空、必須是 checklist
    assert isinstance(doc["checklist"], str)
    assert doc["checklist"].strip()
    assert _checklist_header_in(doc["checklist"])
    # checklist-only ⇒ 未生成任何 CRD
    assert doc["prometheus_rules"] == 0
    assert doc["alertmanager_configs"] == 0
    assert doc["total_crds"] == 0
    # 掃描計數是真實的
    assert doc["configmap_files"] == 2
    assert doc["tenants"] == 2
    # ⚠ SUSPECTED BUG（pin 現況，未修）：checklist-only 模式下 result 是
    # analyze_migration() 的 analysis dict，rule group 計數放在鍵
    # "rule_groups"；但 summary/checklist 讀的是 generate_migration() 的鍵
    # "rule_group_count" ⇒ 永遠 fallback 到 0，即使 analysis 實際數到 2。
    # 修法屬行為變更，不在本 refactor 範圍——這行只 pin 現況。
    assert doc["rule_groups"] == 0


_PAYLOAD_VALIDATORS = {
    "empty": _validate_empty,
    "json_summary": _validate_json_summary,
    "text_checklist_preview": _validate_text_checklist_preview,
    "json_crd_preview": _validate_json_crd_preview,
    "text_checklist": _validate_text_checklist,
    "json_checklist_envelope": _validate_json_checklist_envelope,
}


class TestOutputModeMatrix:
    """(checklist_only × dry_run × json) 8 組合的 table-driven characterization。"""

    @pytest.mark.parametrize("row", OUTPUT_MODE_MATRIX, ids=_MATRIX_IDS)
    def test_output_mode_matrix(
        self, row, temp_configmap_dir, temp_config_dir, temp_output_dir, capsys,
    ):
        argv = [
            "migrate_to_operator.py",
            "--source-dir", str(temp_configmap_dir),
            "--config-dir", str(temp_config_dir),
            "--output-dir", str(temp_output_dir),
        ]
        if row.checklist_only:
            argv.append("--checklist-only")
        if row.dry_run:
            argv.append("--dry-run")
        if row.json_mode:
            argv.append("--json")

        # exit code：main() 正常 return（無 sys.exit）⇒ process exit 0
        with patch("sys.argv", argv):
            try:
                ret = mto.main()
            except SystemExit as exc:  # pragma: no cover - 只為診斷訊息
                pytest.fail(f"main() raised SystemExit({exc.code}); expected exit 0")
        assert ret is None

        captured = capsys.readouterr()

        # ── stdout payload 種類 ────────────────────────────────────────────
        _PAYLOAD_VALIDATORS[row.payload](captured.out)

        # ── stderr 訊息面（進度走 stderr 的既有慣例；zh/en 依環境擇一）──────
        err = captured.err
        assert "Analyzing migration scope" in err or "正在分析遷移範圍" in err
        # "Generating CRDs" 只在非 checklist-only（checklist-only 跳過生成）
        has_generating = "Generating CRDs" in err or "正在生成 CRD" in err
        assert has_generating == (not row.checklist_only)
        # "Generated: <path>" 只在 write 模式：4 CRD + 1 checklist = 5 行
        assert err.count("Generated: ") == (5 if row.writes_files else 0)
        # 人類 ✓ summary 走 stderr，且只在非 JSON 模式
        assert ("✓" in err) == (not row.json_mode)

        # ── 檔案寫入副作用 ─────────────────────────────────────────────────
        written = sorted(p.name for p in temp_output_dir.glob("*"))
        if row.writes_files:
            assert "MIGRATION-CHECKLIST.md" in written
            assert len(written) == 5           # 2 PrometheusRule + 2 AMConfig + checklist
        else:
            assert written == []


# ────────────────────────────────────────────────────────────────────────────
# Test: plan_stdout() — 矩陣決策的純函式單元層
# ────────────────────────────────────────────────────────────────────────────
#
# TestOutputModeMatrix 從 process 面 pin 8 組合；這裡直接打純函式，讓未來的
# 矩陣回歸（改 precedence、動 summary 條件）在單元層現形、不用等 subprocess
# gate。表格重用 OUTPUT_MODE_MATRIX——同一張表、兩個觀測層。


class TestPlanStdout:
    """plan_stdout() 的文件序列選擇（io=0 純函式）。"""

    _CHK = "# Migration Checklist\n\nfake checklist body"

    def _fake_result(self):
        return {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "namespace": "monitoring",
            "prometheus_rules": [
                {"name": "da-rule-pack-x", "file": "x.yaml",
                 "crd": {"kind": "PrometheusRule", "metadata": {"name": "da-rule-pack-x"}}},
            ],
            "alertmanager_configs": [
                {"name": "da-tenant-t", "tenant": "t",
                 "crd": {"kind": "AlertmanagerConfig", "metadata": {"name": "da-tenant-t"}}},
            ],
            "configmap_files": 1,
            "rule_group_count": 1,
            "tenants": 1,
            "errors": [],
        }

    def _plan(self, row, result=None):
        return mto.plan_stdout(
            checklist_only=row.checklist_only,
            dry_run=row.dry_run,
            json_mode=row.json_mode,
            result=result if result is not None else self._fake_result(),
            checklist=self._CHK,
            namespace="monitoring",
            source_dir=Path("/src"),
            config_dir=Path("/conf"),
        )

    @pytest.mark.parametrize("row", OUTPUT_MODE_MATRIX, ids=_MATRIX_IDS)
    def test_plan_shape_matches_matrix(self, row):
        """8 組合各自的文件序列形狀（與 process 層同一張表）。"""
        docs = self._plan(row)

        if row.payload == "empty":
            assert docs == []
        elif row.payload == "json_summary":
            assert len(docs) == 1
            doc = json.loads(docs[0])
            assert set(doc) == _SUMMARY_KEYS
        elif row.payload == "text_checklist_preview":
            assert docs[0] == "# MIGRATION CHECKLIST\n"
            assert docs[1] == self._CHK + "\n"
            assert docs[2] == "\n# CRD PREVIEW\n\n"
            preview = "".join(docs[3:])
            assert "kind: PrometheusRule" in preview
            assert "kind: AlertmanagerConfig" in preview
        elif row.payload == "json_crd_preview":
            assert len(docs) == 1
            doc = json.loads(docs[0])
            assert set(doc) == {
                "metadata", "prometheus_rules", "alertmanager_configs", "errors",
            }
        elif row.payload == "text_checklist":
            assert docs == [self._CHK + "\n"]
        elif row.payload == "json_checklist_envelope":
            assert len(docs) == 1
            doc = json.loads(docs[0])
            assert set(doc) == _SUMMARY_KEYS | {"status", "checklist"}
            assert doc["status"] == "checklist_only"
            assert doc["checklist"] == self._CHK
        else:  # pragma: no cover - 表格新增 payload 種類時強制補分支
            pytest.fail(f"unknown payload kind: {row.payload}")

        # 每個 JSON 模式 payload 都是「恰一份文件」——stdout 契約的單元層鏡像
        if row.json_mode:
            json.loads("".join(docs))

    def test_bug1_checklist_only_outranks_dry_run(self):
        """bug-1 pin（單元層）：C+D+J 選 checklist envelope，不是 CRD preview。"""
        row = ComboRow(True, True, True, "json_checklist_envelope", False)
        docs = self._plan(row)
        doc = json.loads("".join(docs))
        assert "metadata" not in doc          # CRD preview 的 discriminator
        assert doc.get("status") == "checklist_only"

    def test_bug2_combo_summary_not_suppressed(self):
        """bug-2 pin（單元層）：C+D+J 的文件序列非空（stdout 不得空白）。"""
        row = ComboRow(True, True, True, "json_checklist_envelope", False)
        assert self._plan(row) != []

    def test_pure_no_result_mutation_and_deterministic(self):
        """io=0 純函式：不改 result、同輸入同輸出。"""
        import copy
        for row in OUTPUT_MODE_MATRIX:
            result = self._fake_result()
            snapshot = copy.deepcopy(result)
            first = self._plan(row, result=result)
            second = self._plan(row, result=result)
            assert result == snapshot, f"plan_stdout mutated result for {row}"
            assert first == second


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
