#!/usr/bin/env python3
"""Tests for migrate_to_operator.py — ConfigMap → CRD migration tool.

Verifies:
  1. parse_configmap_rules() — Parse ConfigMap YAML with rule groups
  2. convert_rules_to_crd() — Convert to PrometheusRule CRD structure
  3. analyze_migration() — Analyze migration scope and identify issues
  4. build_migration_checklist() — Generate 6-phase migration checklist
  5. generate_migration() — Full E2E orchestration
  6. Dry-run mode (no file writes)
  7. JSON output mode
  8. ConfigMap → CRD end-to-end conversion

#3572: the tool emits no AlertmanagerConfig and reads no conf.d (routing is
generate-routes → configSecret); the refusal of its routing flags is pinned in
test_operator_routing_single_renderer.py.
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
from _platform_fs import symlink_or_skip  # noqa: E402


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
        """#3426: a file no reader reads stops the run, named with the
        reader's reason — it used to be a WARNING and a skip."""
        (temp_configmap_dir / "invalid.yaml").write_text("invalid: yaml: content: [")
        with pytest.raises(mto.SourceFilesUnreadable) as exc_info:
            mto.parse_configmap_rules(temp_configmap_dir)
        (name, why), = exc_info.value.files
        assert name == "invalid.yaml" and "yaml:" in why, exc_info.value.files
        assert "invalid.yaml: " in str(exc_info.value)

    @pytest.mark.usefixtures("da_guard_env")
    def test_parse_configmap_rules_directory_not_found(self):
        """#3721: a directory that is not there is da-guard's refusal (the
        listing it is read from), not an `is_dir` of this module's own —
        that second look turned a directory removed after it was judged
        into an `issues` WARNING and a migration from no file, rc 0."""
        from _lib_parse_tree import ParseTreeError
        with pytest.raises(ParseTreeError):
            mto.parse_configmap_rules(Path("/nonexistent/path"))

    @pytest.mark.usefixtures("da_guard_env", "da_read_env")
    def test_a_source_dir_gone_after_it_was_judged_writes_nothing(
            self, temp_configmap_dir, tmp_path, monkeypatch, capsys):
        """#3721: --source-dir removed after `_lib_input` judged and selected
        it was a WARNING ("Source directory error"), a checklist and no CRD,
        rc 0. Now the reading that finds it gone stops the run: rc 2,
        nothing written."""
        import shutil
        out = tmp_path / "out"
        real = mto._lib_input.require_selection

        def racing(*a, **kw):
            chosen = real(*a, **kw)
            shutil.rmtree(temp_configmap_dir)
            return chosen

        monkeypatch.setattr(mto._lib_input, "require_selection", racing)
        monkeypatch.setattr(sys, "argv", ["mto", "--source-dir", str(temp_configmap_dir),
                                          "--output-dir", str(out)])
        with pytest.raises(SystemExit) as exc:
            mto.main()
        assert exc.value.code == 2
        assert not out.exists() or not any(out.iterdir()), sorted(out.iterdir())
        assert "Source directory error" not in capsys.readouterr().err


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
        # #3427: both selector labels operator-generate emits, not only one.
        assert labels["release"] == "kube-prometheus-stack"


# ────────────────────────────────────────────────────────────────────────────
# Test: analyze_migration
# ────────────────────────────────────────────────────────────────────────────

class TestAnalyzeMigration:
    """Tests for analyze_migration()."""

    def test_analyze_migration(self, temp_configmap_dir):
        """Check analysis counts (ConfigMaps, rule groups, estimated CRDs)."""
        analysis = mto.analyze_migration(temp_configmap_dir)

        # Verify counts
        assert analysis["configmap_files"] == 2
        assert analysis["rule_groups"] == 2
        assert "tenants" not in analysis       # #3572: no conf.d is read
        assert analysis["estimated_crds"] == 2  # 2 ConfigMaps → 2 PrometheusRules
        assert len(analysis["issues"]) == 0

# ────────────────────────────────────────────────────────────────────────────
# Test: build_migration_checklist
# ────────────────────────────────────────────────────────────────────────────

class TestBuildMigrationChecklist:
    """Tests for build_migration_checklist()."""

    def test_build_migration_checklist_contains_steps(self, temp_configmap_dir, temp_output_dir):
        """Checklist has all 6 phases."""
        result = {
            "configmap_files": 2,
            "rule_group_count": 2,
            "prometheus_rules": [{"crd": {}}],
        }

        checklist = mto.build_migration_checklist(
            temp_configmap_dir, temp_output_dir, result
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
        # #3572: the routing phase is generate-routes → configSecret, no CRD apply
        assert "da-tools generate-routes --config-dir conf.d --output-alertmanager" in checklist
        assert "alertmanager.alertmanagerSpec.configSecret" in checklist
        assert "operator-alertmanager-integration/" in checklist
        assert "*-tenant-*.yaml" not in checklist


# ────────────────────────────────────────────────────────────────────────────
# Test: generate_migration
# ────────────────────────────────────────────────────────────────────────────

class TestGenerateMigration:
    """Tests for generate_migration()."""

    def test_generate_migration_writes_crds(self, temp_configmap_dir, temp_output_dir):
        """Full run creates output directory with CRD files + checklist."""
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify result structure
        assert result["configmap_files"] == 2
        assert result["rule_group_count"] == 2
        assert "tenants" not in result and "alertmanager_configs" not in result   # #3572
        assert len(result["prometheus_rules"]) == 2
        assert len(result["errors"]) == 0

        # Verify PrometheusRule CRD fields
        for item in result["prometheus_rules"]:
            assert "name" in item
            assert "crd" in item
            crd = item["crd"]
            assert crd["kind"] == "PrometheusRule"
            assert crd["metadata"]["labels"]["migrated-from"] == "configmap"

    def test_generate_migration_json_output(self, temp_configmap_dir):
        """JSON mode returns valid JSON with all fields."""
        result = mto.generate_migration(
            temp_configmap_dir,
            Path("/tmp/dummy"),
            "monitoring",
        )

        # Verify that result can be serialized to JSON
        json_str = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        parsed = json.loads(json_str)

        # Verify essential fields
        assert "configmap_files" in parsed
        assert "rule_group_count" in parsed
        assert "prometheus_rules" in parsed
        assert "alertmanager_configs" not in parsed   # #3572


# ────────────────────────────────────────────────────────────────────────────
# Test: End-to-End
# ────────────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    """End-to-end migration tests."""

    def test_end_to_end_configmap_to_crd(self, temp_configmap_dir, temp_output_dir):
        """Full E2E: create temp files, run migration, verify CRD output."""
        # Run full migration
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got CRDs
        assert len(result["prometheus_rules"]) == 2

        # Verify PrometheusRule structure
        pr = result["prometheus_rules"][0]["crd"]
        assert pr["apiVersion"] == "monitoring.coreos.com/v1"
        assert pr["kind"] == "PrometheusRule"
        assert "spec" in pr
        assert "groups" in pr["spec"]

# ────────────────────────────────────────────────────────────────────────────
# Test: Dry-run Mode
# ────────────────────────────────────────────────────────────────────────────

class TestDryRunMode:
    """Tests for dry-run functionality."""

    def test_generate_migration_dry_run_produces_no_files(self, temp_configmap_dir, temp_output_dir):
        """Dry run produces result dict but no files are written."""
        # Note: generate_migration() itself doesn't write files
        # (that's done in main()), so we're testing the core behavior

        result = mto.generate_migration(
            temp_configmap_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got a result
        assert result is not None
        assert len(result["prometheus_rules"]) == 2


# ────────────────────────────────────────────────────────────────────────────
# Test: main() --json envelope 形狀 (#1112)
# ────────────────────────────────────────────────────────────────────────────

class TestMainJsonEnvelope:
    """`--checklist-only --json` 的 envelope 形狀 + checklist 必須真的送到 stdout。"""

    def _run(self, argv_tail, configmap_dir, output_dir):
        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(configmap_dir),
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
            "configmap_files", "rule_groups",
            "prometheus_rules", "total_crds",
            "already_crd", "status", "checklist",
        }
        # checklist-only ⇒ 沒有產生任何 CRD，這兩個計數確實是 0
        assert doc["prometheus_rules"] == 0
        assert doc["total_crds"] == 0
        # 但「掃到的東西」是真實計數（fixture: 2 個 ConfigMap）
        assert doc["configmap_files"] == 2

        # 人類訊息在 stderr，stdout 不含散文（checklist 是 JSON 字串值，不是裸 Markdown）
        assert "Analyzing migration scope" in captured.err
        assert not captured.out.lstrip().startswith("#")

    def test_checklist_only_json_envelope(
        self, temp_configmap_dir, temp_output_dir, capsys,
    ):
        """#1112: `--checklist-only --json` → checklist 在 `checklist` 欄裡。"""
        self._run(["--checklist-only", "--json"],
                  temp_configmap_dir, temp_output_dir)
        self._assert_checklist_envelope(capsys.readouterr())

    def test_checklist_only_with_dry_run_json_envelope(
        self, temp_configmap_dir, temp_output_dir, capsys,
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
                  temp_configmap_dir, temp_output_dir)
        captured = capsys.readouterr()

        doc = json.loads(captured.out)
        # bug 的簽名：dry-run 的 CRD-preview 鍵出現在 checklist-only 的輸出裡
        assert "metadata" not in doc, (
            "--checklist-only --dry-run 掉回 dry-run 的 CRD-preview 分支了"
        )
        self._assert_checklist_envelope(captured)

    def test_dry_run_json_is_crd_preview_not_summary(
        self, temp_configmap_dir, temp_output_dir, capsys,
    ):
        """對照組：沒有 --checklist-only 時，`--dry-run --json` 仍是單一 CRD preview。

        釘住修法的另一半——`checklist_for_json is not None` 這個 escape hatch
        不能讓純 dry-run 也追加一份 summary（那會變成 stdout 兩份文件、
        json.loads 直接炸）。
        """
        self._run(["--dry-run", "--json"],
                  temp_configmap_dir, temp_output_dir)
        captured = capsys.readouterr()

        doc = json.loads(captured.out)        # 兩份文件的話這裡就炸了
        assert set(doc) == {
            "metadata", "prometheus_rules", "already_crd", "errors",
        }
        assert "checklist" not in doc
        assert "status" not in doc
        assert len(doc["prometheus_rules"]) == 2      # 真的有預覽內容


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
    #                             already_crd, errors}
    #   text_checklist          — 裸 checklist markdown（無 CRD preview）
    #   json_checklist_envelope — 單一 JSON：6 鍵 summary + status + checklist
    payload: str
    writes_files: bool          # output_dir 是否寫出檔案（CRD ×2 + checklist）


# 8 列全矩陣。fixture 現況：2 個 ConfigMap（各 1 rule group）
# ⇒ 生成模式下 2 PrometheusRules = 2 CRDs（#3572：沒有 AlertmanagerConfig）。
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
    "configmap_files", "rule_groups",
    "prometheus_rules", "total_crds",
    "already_crd",   # #3426: PrometheusRules of --source-dir, named, not converted
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
    assert doc["prometheus_rules"] == 2
    assert doc["total_crds"] == 2


def _validate_text_checklist_preview(out):
    # dry-run 文字模式的 banner 是 hardcode 英文，語言無關
    assert out.startswith("# MIGRATION CHECKLIST\n")
    assert _checklist_header_in(out)
    assert "# CRD PREVIEW" in out
    # preview 真的載有 CRD 的 YAML（不是空殼），且沒有 AlertmanagerConfig（#3572）
    assert out.count("kind: PrometheusRule") == 2
    assert "kind: AlertmanagerConfig" not in out


def _validate_json_crd_preview(out):
    doc = json.loads(out)
    assert set(doc) == {
        "metadata", "prometheus_rules", "already_crd", "errors",
    }
    # 不是 checklist envelope（bug-1 的反向保證：純 dry-run 不長出 checklist）
    assert "checklist" not in doc
    assert "status" not in doc
    assert len(doc["prometheus_rules"]) == 2
    assert doc["errors"] == []
    assert doc["metadata"]["configmap_files"] == 2
    assert doc["metadata"]["rule_groups"] == 2
    assert "tenants" not in doc["metadata"] and "config_dir" not in doc["metadata"]
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
    assert doc["total_crds"] == 0
    # 掃描計數是真實的
    assert doc["configmap_files"] == 2
    # 鍵名錯配 bug 已修（owner 拍板）：analyze_migration() 的 "rule_groups"
    # 計數經 main() 橋接到消費端讀的 "rule_group_count"——fixture 有 2 個
    # rule group，envelope 必須回報真實值而非恆 0。
    assert doc["rule_groups"] == 2


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
        self, row, temp_configmap_dir, temp_output_dir, capsys,
    ):
        argv = [
            "migrate_to_operator.py",
            "--source-dir", str(temp_configmap_dir),
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
        # "Generated: <path>" 只在 write 模式：2 CRD + 1 checklist = 3 行
        assert err.count("Generated: ") == (3 if row.writes_files else 0)
        # 人類 ✓ summary 走 stderr，且只在非 JSON 模式
        assert ("✓" in err) == (not row.json_mode)

        # ── 檔案寫入副作用 ─────────────────────────────────────────────────
        written = sorted(p.name for p in temp_output_dir.glob("*"))
        if row.writes_files:
            assert "MIGRATION-CHECKLIST.md" in written
            assert len(written) == 3           # 2 PrometheusRule + checklist
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
            "configmap_files": 1,
            "rule_group_count": 1,
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
            assert "kind: AlertmanagerConfig" not in preview   # #3572
        elif row.payload == "json_crd_preview":
            assert len(docs) == 1
            doc = json.loads(docs[0])
            assert set(doc) == {
                "metadata", "prometheus_rules", "already_crd", "errors",
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


class TestWritingIsOptIn:
    """#1582：沒有 `--output-dir` 就一個檔也不寫，checklist 改走 stdout。

    反事實（`9f5fe89a`，本改動前）：不給 `--output-dir` 時，同一條指令在**呼叫
    目錄**建出 `migration-output/`。⚠️ 本改動把「沒有寫入目標」摺進既有的
    `dry_run` 布林（`plan_stdout()` 的 8 組合矩陣），所以第一版只擋掉寫入而
    stdout 是空的——那是「靜默什麼都不做」，比原本更糟。下面第二支測試就是
    釘住這一點：不寫檔的同時**必須**有東西送到 stdout。
    """

    @staticmethod
    def _tree(root: Path) -> set:
        """⛔ `root` 必須是 `tmp_path`，不能只給呼叫目錄——理由與
        `test_operator_generate_v2.TestWritingIsOptIn._tree` 同一條（盲審實測，
        只看 cwd 時「寫進被重導的輸入樹」這個變異會存活）。"""
        return {p.relative_to(root) for p in root.rglob("*")}

    def test_no_output_dir_writes_nothing(
        self, temp_configmap_dir, tmp_path, monkeypatch, capsys,
    ):
        cwd = tmp_path / "caller_cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        # ⚠️ 兩個輸入樹是 module-level fixture（不在 tmp_path 底下），所以三棵都要看。
        roots = [tmp_path, Path(temp_configmap_dir)]
        before = [self._tree(r) for r in roots]
        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(temp_configmap_dir),
        ]):
            mto.main()

        for root, snap in zip(roots, before):
            assert self._tree(root) == snap, (
                f"no --output-dir was given, so NOTHING may be written under {root} "
                f"(cwd, the source tree, or the redirected conf.d tree); "
                f"these appeared: {sorted(map(str, self._tree(root) - snap))}"
            )
        capsys.readouterr()

    def test_no_output_dir_still_produces_output_on_stdout(
        self, temp_configmap_dir, tmp_path, monkeypatch, capsys,
    ):
        cwd = tmp_path / "caller_cwd2"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(temp_configmap_dir),
        ]):
            mto.main()

        out = capsys.readouterr().out
        assert out.strip(), (
            "suppressing the write without redirecting the payload leaves the tool "
            "silently producing nothing — stdout must carry the checklist"
        )
        assert "<--output-dir>" in out, (
            "the checklist embeds the output directory in its kubectl commands. "
            "With no --output-dir there is no real path, so it must render a "
            "placeholder rather than a default the tool never created. "
            f"stdout was {out[:300]!r}"
        )

    def test_output_dir_still_writes_and_checklist_carries_the_real_path(
        self, temp_configmap_dir, temp_output_dir, tmp_path, monkeypatch, capsys,
    ):
        cwd = tmp_path / "caller_cwd3"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        # ⚠️ before/after 差集，不是「必須為空」——輸入 fixture 本來就有檔案在裡面。
        roots = [tmp_path, Path(temp_configmap_dir)]
        before = [self._tree(r) for r in roots]

        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(temp_configmap_dir),
            "--output-dir", str(temp_output_dir),
        ]):
            mto.main()
        capsys.readouterr()

        checklist = temp_output_dir / "MIGRATION-CHECKLIST.md"
        assert checklist.exists(), "--output-dir was given: the write path must still write"
        body = checklist.read_text(encoding="utf-8")
        assert "<--output-dir>" not in body, (
            "a real --output-dir was given, so the checklist must name it, not the placeholder"
        )
        for root, snap in zip(roots, before):
            leaked = self._tree(root) - snap
            assert not leaked, (
                f"an explicit --output-dir was given, so every new file must land "
                f"there; these appeared under {root}: {sorted(map(str, leaked))}"
            )


class TestReadsNoConfd:
    """#3572: migrate-to-operator emits no AlertmanagerConfig, so it reads no
    conf.d at all — no tenant set, no `da-guard parse-tree` of a conf.d; it
    lists --source-dir once (#3206)."""

    def test_a_run_lists_the_source_dir_once_and_parses_no_tree(self, tmp_path, monkeypatch, capsys):
        import _lib_parse_tree

        confd = tmp_path / "conf.d"      # in the cwd, where the old default looked
        confd.mkdir()
        (confd / "db-a.yaml").write_text("tenants:\n  db-a:\n    x: 2\n", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        # #3462: a --source-dir with no source file is refused — one plain
        # rule file, so the run reaches the read this test counts.
        (src / "rules.yaml").write_text(
            "groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n",
            encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        loads, lists = [], []
        real, real_list = _lib_parse_tree.load, _lib_parse_tree.list_tree

        def counting(*a, **kw):
            loads.append(a[0])
            return real(*a, **kw)

        def counting_list(*a, **kw):
            lists.append(a[0])
            return real_list(*a, **kw)

        monkeypatch.setattr(_lib_parse_tree, "load", counting)
        monkeypatch.setattr(_lib_parse_tree, "list_tree", counting_list)
        monkeypatch.setattr(sys, "argv", ["mto", "--source-dir", str(src), "--dry-run"])
        mto.main()
        assert loads == [], loads
        assert [os.path.realpath(d) for d in lists] == [os.path.realpath(src)], lists
        assert "tenants" not in capsys.readouterr().err


class TestSourceDirNamesWhatItDoesNotRead:
    """--source-dir is Go's list-only walk (#3206): a top-level entry the
    walk leaves out or cannot read is named on stderr, never dropped
    silently (`kubectl get cm -o yaml > .rules.yaml` is a real way to get
    one)."""

    _RULES = "groups:\n  - name: g\n    rules: []\n"

    def test_hidden_and_unreadable_entries_are_named(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rules.yaml").write_text(self._RULES, encoding="utf-8")
        (src / ".hidden-rules.yaml").write_text(self._RULES, encoding="utf-8")
        (src / "notes.txt").write_text("not a rule file\n", encoding="utf-8")
        symlink_or_skip("nowhere", src / "dangling.yaml")
        rules = mto.parse_configmap_rules(src)
        assert [r["file"] for r in rules] == ["rules.yaml"]
        err = capsys.readouterr().err
        line = next((ln for ln in err.splitlines() if "not read" in ln), "")
        assert ".hidden-rules.yaml (hidden)" in line, err
        assert "dangling.yaml (" in line, err
        assert "notes.txt" not in line, "a name that is no config file was never read"

    def test_nothing_is_named_when_everything_is_read(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rules.yaml").write_text(self._RULES, encoding="utf-8")
        mto.parse_configmap_rules(src)
        assert "not read" not in capsys.readouterr().err


class TestSourceFilesAreWhatTheReadersSay:
    """#3426: a --source-dir file is a manifest kubectl reads, or else a
    plain rule file (one object declaring `groups`, read with
    `da-crdecode --any-shape`); anything else stops the run at rc 2 before a
    single file is written, every such file named with the reader's reason.
    Before, a file kubectl's reading refused was a WARNING and a skip, and a
    document that was no mapping was skipped without a word."""

    _RULES = "groups:\n  - name: g\n    rules:\n      - alert: A\n        expr: up == 0\n"

    @pytest.mark.parametrize("name,text,reason", [
        ("top-level-list.yaml", "- groups: []\n", "the document is array, not a mapping"),
        ("scalar.yaml", "just text\n", "the document is string, not a mapping"),
        # A List is read as its items; one kubectl would not apply is named.
        ("list.yaml", "apiVersion: v1\nkind: List\nitems:\n- {kind: ConfigMap}\n",
         "items[0] (line 4): it is not a Kubernetes object: it has no `apiVersion`"),
        # #3426 round 3: shapes the API server refuses.
        ("pr.yaml", "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
                    "metadata: {name: p}\ngroups:\n- name: g\n  rules: []\n",
         'unknown field "groups"'),
        ("cm-data-list.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n"
                              "data:\n- groups: []\n",
         "its data is list, not a mapping"),
        # Neither YAML reading reads it, and the rule loader refuses it too.
        ("broken.yaml", "groups: [\n", "Prometheus' rule loader refuses it"),
        ("cm-then-junk.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n---\n- x\n",
         "the document starting at line 5"),
    ])
    def test_unreadable_file_is_named(self, tmp_path, name, text, reason):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rules.yaml").write_text(self._RULES, encoding="utf-8")
        (src / name).write_text(text, encoding="utf-8")
        with pytest.raises(mto.SourceFilesUnreadable) as exc_info:
            mto.parse_configmap_rules(src)
        (got_name, why), = exc_info.value.files
        assert got_name == name and reason in why, exc_info.value.files

    def test_a_rule_file_the_rule_loader_refuses_stops_the_run(self, tmp_path):
        """Two documents that each declare `groups`: a rule file, which
        Prometheus' rule loader refuses (it reads one document) — named with
        the loader's reason."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "two.yaml").write_text(self._RULES + "---\n" + self._RULES, encoding="utf-8")
        with pytest.raises(mto._lib_consumer_read.RuleFilesRefused) as exc_info:
            mto.parse_configmap_rules(src)
        assert [rf.path for rf in exc_info.value.refused] == [str(src / "two.yaml")]

    def _main(self, src, out):
        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(src),
            "--output-dir", str(out),
        ]):
            with pytest.raises(SystemExit) as exc_info:
                mto.main()
        return exc_info.value.code

    @pytest.mark.parametrize("json_mode", [False, True])
    def test_main_stops_on_a_rule_file_the_loader_refuses(
            self, tmp_path, capsys, json_mode):
        """#3426 F2: a rule file Prometheus' rule loader refuses used to be an
        `issues` WARNING beside a CRD set written without it (rc 0)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ok.yaml").write_text(self._RULES, encoding="utf-8")
        (src / "bad.yaml").write_text(
            "groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n", encoding="utf-8")
        out = tmp_path / "out"
        argv_json = ["--json"] if json_mode else []
        with patch("sys.argv", ["migrate_to_operator.py", "--source-dir", str(src),
                                "--output-dir", str(out), *argv_json]):
            with pytest.raises(SystemExit) as exc_info:
                mto.main()
        assert exc_info.value.code == 2
        assert not out.exists(), "nothing may be written when a rule file is refused"
        captured = capsys.readouterr()
        assert "bad.yaml" in captured.err + captured.out and "parse error" in captured.err + captured.out

    def test_checklist_only_stops_on_a_refused_rule_file(self, tmp_path, capsys):
        """--checklist-only runs the analysis alone: a refused rule file
        there is rc 2 too, not an `issues` line beside a plan."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "bad.yaml").write_text(
            "groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n", encoding="utf-8")
        with patch("sys.argv", ["migrate_to_operator.py", "--source-dir", str(src), "--checklist-only"]):
            with pytest.raises(SystemExit) as exc_info:
                mto.main()
        assert exc_info.value.code == 2
        assert "bad.yaml" in capsys.readouterr().err

    def test_main_stops_without_da_read(self, tmp_path, capsys, monkeypatch):
        """#3426 F2: no reader binary is no answer about the sources: rc 2,
        nothing written (it used to be an `errors` line and rc 0)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ok.yaml").write_text(self._RULES, encoding="utf-8")
        monkeypatch.setenv("DA_READ_BINARY", str(tmp_path / "no-such-da-read"))
        out = tmp_path / "out"
        assert self._main(src, out) == 2
        assert not out.exists()
        assert "da-read" in capsys.readouterr().err

    _PR = ("apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
           "metadata: {name: da-rule-pack-x}\nspec:\n  groups:\n  - name: g\n"
           "    rules:\n    - {alert: A, expr: up == 0}\n")

    @pytest.mark.parametrize("extra", [[], ["--checklist-only"]], ids=["write", "checklist"])
    def test_a_prometheusrule_is_named_already_a_crd_not_converted(
            self, tmp_path, capsys, extra):
        """#3426 F11: a PrometheusRule in --source-dir is already the CRD this
        tool writes. It is not converted and no error (rc 0), but named on
        stderr and in the checklist — it used to be passed over by the kind
        filter without a word."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ok.yaml").write_text(self._RULES, encoding="utf-8")
        (src / "pr.yaml").write_text(self._PR, encoding="utf-8")
        out = tmp_path / "out"
        with patch("sys.argv", ["migrate_to_operator.py", "--source-dir", str(src),
                                "--output-dir", str(out), *extra]):
            try:
                mto.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        assert code == 0
        captured = capsys.readouterr()
        notice = [ln for ln in captured.err.splitlines() if "pr.yaml" in ln]
        assert len(notice) == 1 and "da-rule-pack-x" in notice[0], captured.err
        assert "already a PrometheusRule CRD" in notice[0] or "已是 PrometheusRule CRD" in notice[0]
        checklist = (out / "MIGRATION-CHECKLIST.md").read_text(encoding="utf-8") \
            if not extra else captured.out
        assert "`pr.yaml`: PrometheusRule `da-rule-pack-x`" in checklist, checklist
        if not extra:
            written = sorted(p.name for p in out.iterdir())
            assert not any("da-rule-pack-x" in n for n in written), written
            assert any("rule-pack-ok" in n for n in written), written

    @pytest.mark.parametrize("extra", [["--dry-run"], []], ids=["dry-run", "no-output-dir"])
    def test_the_json_preview_names_a_prometheusrule_too(self, tmp_path,
                                                         capsys, extra):
        """#3426 F17: the preview envelope (`--dry-run --json`, or `--json`
        with no --output-dir) carries `already_crd` as the summary does."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ok.yaml").write_text(self._RULES, encoding="utf-8")
        (src / "pr.yaml").write_text(self._PR, encoding="utf-8")
        argv = ["migrate_to_operator.py", "--source-dir", str(src), "--json", *extra]
        if extra:   # --dry-run with an --output-dir: still nothing written
            argv += ["--output-dir", str(tmp_path / "out")]
        with patch("sys.argv", argv):
            try:
                mto.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        assert code == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["already_crd"] == [{"file": "pr.yaml", "name": "da-rule-pack-x"}], doc
        assert not (tmp_path / "out").exists()

    def test_a_list_of_configmaps_migrates(self, tmp_path):
        """`kubectl get cm -o yaml` writes a List: its ConfigMaps are read
        as kubectl applies them (they used to be passed over by the kind
        filter without a word)."""
        src = tmp_path / "src"
        src.mkdir()
        rules = self._RULES.replace("\n", "\n        ")
        (src / "dump.yaml").write_text(
            "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n"
            "  metadata: {name: listed}\n  data:\n    r.yml: |\n      " + rules.rstrip() + "\n",
            encoding="utf-8")
        (got,) = mto.parse_configmap_rules(src)
        assert (got["name"], got["cm_data_key"]) == ("listed", "r.yml")
        assert [g["name"] for g in got["rule_groups"]] == ["g"]

    def test_a_rule_file_only_the_rule_loader_reads_migrates(self, tmp_path):
        """An annotation value `.inf` has no JSON: neither da-crdecode
        reading reads the file. Prometheus' rule loader reads it (the string
        ".inf"), so it is a rule file (`rule_files_in`)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "inf.yaml").write_text(
            self._RULES + "        annotations: {x: .inf}\n", encoding="utf-8")
        (got,) = mto.parse_configmap_rules(src)
        assert got["file"] == "inf.yaml" and got["cm_data_key"] is None
        assert got["rule_groups"][0]["rules"][0]["annotations"] == {"x": ".inf"}

    def test_plain_rule_file_still_migrates(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rules.yaml").write_text(self._RULES, encoding="utf-8")
        (got,) = mto.parse_configmap_rules(src)
        assert got["file"] == "rules.yaml" and got["cm_data_key"] is None
        assert [g["name"] for g in got["rule_groups"]] == ["g"]

    def test_main_stops_and_writes_nothing(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rules.yaml").write_text(self._RULES, encoding="utf-8")
        (src / "bad.yaml").write_text("- groups: []\n", encoding="utf-8")
        out = tmp_path / "out"
        with patch("sys.argv", [
            "migrate_to_operator.py",
            "--source-dir", str(src),
            "--output-dir", str(out),
        ]):
            with pytest.raises(SystemExit) as exc_info:
                mto.main()
        assert exc_info.value.code == 2
        assert not out.exists(), "nothing may be written when a source file is unreadable"
        err = capsys.readouterr().err
        assert "bad.yaml: " in err and "the document is array" in err, err
        assert "WARNING: Failed to parse" not in err


class TestConfigMapScanAcceptsBothSpellings:
    """`parse_configmap_rules` reads `kubectl` output; `.yml` is ordinary."""

    def test_yml_configmap_is_read(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        body = (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: rules\n"
            "data:\n"
            "  x.yaml: |\n"
            "    groups:\n"
            "      - name: g\n"
            "        rules:\n"
            "          - alert: A\n"
            "            expr: up == 0\n"
        )
        (src / "cm.yml").write_text(body, encoding="utf-8")
        got = mto.parse_configmap_rules(src)
        assert got, "a .yml ConfigMap dump must be read (#1604)"

        # 必響對照組：同一份內容改回 .yaml 也要讀得到，證明不是 fixture 本身壞了
        (src / "cm.yml").unlink()
        (src / "cm.yaml").write_text(body, encoding="utf-8")
        assert mto.parse_configmap_rules(src), "control: .yaml must still be read"
