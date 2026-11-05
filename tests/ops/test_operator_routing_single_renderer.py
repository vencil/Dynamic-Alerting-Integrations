"""test_operator_routing_single_renderer.py — operator 路徑不再產生路由（#3572 決策 D）

`generate-routes` 是唯一的 Alertmanager 路由渲染器：輸出 = merge(來源 base, conf.d)，
平台的四條路由（Watchdog heartbeat、component="custom" 隔離子樹、synthetic-probe、
sentinel）排第一。operator 路徑原本每個租戶產一份 AlertmanagerConfig CRD——
Prometheus Operator 會把 CR 的路由排在最前面、強制 `continue: true`，預設策略
（OnNamespace）還加一個 `namespace="<CR ns>"` matcher：平台路由永遠排不到第一、
租戶路由可能永遠對不上、custom 告警漏到全域路由。

本檔釘住決策 D 的契約：
  * `operator-generate --components alertmanager` 是 caller error（rc 2），stderr
    帶共用的處置文字、stdout 什麼都沒有、什麼都沒寫；
  * 只餵 AlertmanagerConfig 的旗標（兩支工具各自那幾個）一給就 rc 2，訊息點名
    該旗標＋處置文字——明確傳一個「看起來像預設值」的值也一樣被拒；
  * 兩支工具的預設輸出都沒有 `kind: AlertmanagerConfig`，也不讀 conf.d（cwd 裡
    放一棵讀不了的 conf.d 不影響結果）；
  * `operator-check` 第 4 項：帶本工具 part-of label 的 AlertmanagerConfig 是舊版
    留下的殘骸 → warn；沒有 → pass；CRD 沒裝 → pass。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_OPS = _REPO / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
sys.path.insert(0, str(_REPO / "scripts" / "tools"))

import operator_check as oc  # noqa: E402
from _lib_constants import OPERATOR_ROUTING_REMEDY_EN, OPERATOR_ROUTING_REMEDY_ZH  # noqa: E402

_OPERATOR_GENERATE = _OPS / "operator_generate.py"
_MIGRATE = _OPS / "migrate_to_operator.py"

_PACK = textwrap.dedent("""\
    groups:
      - name: demo_alerts
        rules:
          - alert: DemoHigh
            expr: demo_metric > 80
            for: 5m
            labels:
              severity: warning
""")

_CONFIGMAP = textwrap.dedent("""\
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
""")


def _run(script: Path, cwd: Path, *argv: str, lang: str = "en") -> subprocess.CompletedProcess:
    env = {**os.environ, "DA_LANG": lang}
    return subprocess.run([sys.executable, str(script), *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def _unreadable_confd(root: Path) -> None:
    """A conf.d in *root* no reader can read: a tool that still reads conf.d
    by default stops on it (rc 2); one that does not never notices."""
    confd = root / "conf.d"
    confd.mkdir()
    (confd / "tenant-one.yaml").write_text("tenants:\n  tenant-one: [unclosed\n", encoding="utf-8")


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    d = tmp_path / "rule-packs"
    d.mkdir()
    (d / "rule-pack-demo.yaml").write_text(_PACK, encoding="utf-8")
    return d


@pytest.fixture
def source(tmp_path: Path) -> Path:
    d = tmp_path / "configmaps"
    d.mkdir()
    (d / "cpu-rules.yaml").write_text(_CONFIGMAP, encoding="utf-8")
    return d


def _assert_refused(proc: subprocess.CompletedProcess, named: str, lang: str = "en") -> None:
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "", proc.stdout
    assert named in proc.stderr, proc.stderr
    remedy = OPERATOR_ROUTING_REMEDY_EN if lang == "en" else OPERATOR_ROUTING_REMEDY_ZH
    assert remedy in proc.stderr, proc.stderr


class TestTheRemedyIsOneText:
    def test_both_languages_name_the_route_generator_and_the_config_secret(self):
        for text in (OPERATOR_ROUTING_REMEDY_EN, OPERATOR_ROUTING_REMEDY_ZH):
            assert "#3572" in text
            assert "da-tools generate-routes --config-dir conf.d --output-alertmanager" in text
            assert "--from-file=alertmanager.yaml" in text
            assert "alertmanager.alertmanagerSpec.configSecret" in text


class TestOperatorGenerateRefusesRouting:
    @pytest.mark.parametrize("lang", ["en", "zh"])
    def test_components_alertmanager_is_a_caller_error(self, tmp_path, packs, lang):
        out = tmp_path / "out"
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--rule-packs-dir", str(packs),
                    "--components", "alertmanager", "--output-dir", str(out), lang=lang)
        _assert_refused(proc, "--components alertmanager", lang)
        assert not out.exists()

    def test_components_alertmanager_json_is_refused_with_no_document(self, tmp_path, packs):
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--rule-packs-dir", str(packs),
                    "--components", "alertmanager", "--json")
        _assert_refused(proc, "--components alertmanager")

    @pytest.mark.parametrize("argv", [
        ["--config-dir", "conf.d"],          # the old default, given explicitly
        ["--config-dir=conf.d"],
        ["--api-version", "v1beta1"],        # the old default, given explicitly
        ["--receiver-template", "slack"],
        ["--secret-name", "da-slack"],
        ["--secret-key", "webhook-url"],
    ])
    def test_each_alertmanagerconfig_flag_is_refused_naming_it(self, tmp_path, packs, argv):
        (tmp_path / "conf.d").mkdir()
        out = tmp_path / "out"
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--rule-packs-dir", str(packs),
                    "--output-dir", str(out), *argv)
        _assert_refused(proc, argv[0].split("=")[0])
        assert not out.exists()

    def test_the_removed_flags_are_not_in_help(self, tmp_path):
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--help")
        assert proc.returncode == 0, proc.stderr
        for flag in ("--config-dir", "--api-version", "--receiver-template",
                     "--secret-name", "--secret-key"):
            assert flag not in proc.stdout, flag
        assert "--components {all,rules,servicemonitor}" in proc.stdout
        assert ",alertmanager" not in proc.stdout and "| alertmanager" not in proc.stdout

    def test_default_output_has_no_alertmanagerconfig_and_reads_no_confd(self, tmp_path, packs):
        _unreadable_confd(tmp_path)
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--rule-packs-dir", str(packs))
        assert proc.returncode == 0, proc.stderr
        assert "kind: PrometheusRule" in proc.stdout
        assert "kind: ServiceMonitor" in proc.stdout
        assert "AlertmanagerConfig" not in proc.stdout
        assert "AlertmanagerConfig" not in proc.stderr

    def test_json_summary_has_no_alertmanagerconfig_count(self, tmp_path, packs):
        import json
        proc = _run(_OPERATOR_GENERATE, tmp_path, "--rule-packs-dir", str(packs), "--json")
        assert proc.returncode == 0, proc.stderr
        doc = json.loads(proc.stdout)
        assert "alertmanager_configs" not in doc["summary"]
        assert [c["kind"] for c in doc["crds"]] == ["PrometheusRule", "ServiceMonitor"]


class TestMigrateToOperatorRefusesRouting:
    @pytest.mark.parametrize("argv", [
        ["--config-dir", "conf.d"],          # the old default, given explicitly
        ["--receiver-template", "slack"],
        ["--secret-name", "da-slack"],
        ["--secret-key", "webhook-url"],
    ])
    @pytest.mark.parametrize("lang", ["en", "zh"])
    def test_each_alertmanagerconfig_flag_is_refused_naming_it(self, tmp_path, source, argv, lang):
        (tmp_path / "conf.d").mkdir()
        out = tmp_path / "out"
        proc = _run(_MIGRATE, tmp_path, "--source-dir", str(source),
                    "--output-dir", str(out), *argv, lang=lang)
        _assert_refused(proc, argv[0], lang)
        assert not out.exists()

    def test_the_removed_flags_are_not_in_help(self, tmp_path):
        proc = _run(_MIGRATE, tmp_path, "--help")
        assert proc.returncode == 0, proc.stderr
        for flag in ("--config-dir", "--receiver-template", "--secret-name", "--secret-key"):
            assert flag not in proc.stdout, flag

    def test_output_has_no_alertmanagerconfig_and_reads_no_confd(self, tmp_path, source):
        _unreadable_confd(tmp_path)
        out = tmp_path / "out"
        proc = _run(_MIGRATE, tmp_path, "--source-dir", str(source), "--output-dir", str(out))
        assert proc.returncode == 0, proc.stderr
        written = sorted(p.name for p in out.iterdir())
        assert "MIGRATION-CHECKLIST.md" in written
        for p in out.iterdir():
            text = p.read_text(encoding="utf-8")
            assert "kind: AlertmanagerConfig" not in text, p.name
            if p.name != "MIGRATION-CHECKLIST.md":
                assert "AlertmanagerConfig" not in text, p.name
        assert not [n for n in written if "tenant" in n], written

    def test_dry_run_preview_has_no_alertmanagerconfig(self, tmp_path, source):
        # A readable conf.d with a tenant in the cwd: the old tool made one
        # AlertmanagerConfig of it.
        (tmp_path / "conf.d").mkdir()
        (tmp_path / "conf.d" / "tenant-one.yaml").write_text(
            "tenants:\n  tenant-one:\n    demo_metric: 90\n", encoding="utf-8")
        proc =_run(_MIGRATE, tmp_path, "--source-dir", str(source), "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "kind: PrometheusRule" in proc.stdout
        assert "kind: AlertmanagerConfig" not in proc.stdout

    def test_json_preview_and_summary_have_no_alertmanagerconfig_key(self, tmp_path, source):
        import json
        preview = _run(_MIGRATE, tmp_path, "--source-dir", str(source), "--dry-run", "--json")
        assert preview.returncode == 0, preview.stderr
        doc = json.loads(preview.stdout)
        assert "alertmanager_configs" not in doc
        assert "config_dir" not in doc["metadata"] and "tenants" not in doc["metadata"]
        out = tmp_path / "out"
        summary = _run(_MIGRATE, tmp_path, "--source-dir", str(source),
                       "--output-dir", str(out), "--json")
        assert summary.returncode == 0, summary.stderr
        doc = json.loads(summary.stdout)
        assert "alertmanager_configs" not in doc and "tenants" not in doc

    @pytest.mark.parametrize("lang", ["en", "zh"])
    def test_checklist_points_at_generate_routes_and_config_secret(self, tmp_path, source, lang):
        proc = _run(_MIGRATE, tmp_path, "--source-dir", str(source), "--checklist-only", lang=lang)
        assert proc.returncode == 0, proc.stderr
        assert "da-tools generate-routes" in proc.stdout
        assert "configSecret" in proc.stdout
        assert "operator-alertmanager-integration" in proc.stdout
        assert "*-tenant-*.yaml" not in proc.stdout


# ── operator-check 第 4 項 ─────────────────────────────────────────────────

def _checker(monkeypatch, *, installed: bool, leftovers: int):
    crds = {oc._CRD_RULES, oc._CRD_MONITORS} | ({oc._CRD_AM_CONFIGS} if installed else set())
    asked: list = []

    def fake_list(kind, **kw):
        asked.append((kind, kw))
        return [{"metadata": {"name": f"da-tenant-{i}"}} for i in range(leftovers)] \
            if kind == "alertmanagerconfig" else []

    monkeypatch.setattr(oc, "kubectl_list", fake_list)
    monkeypatch.setattr(oc, "kubectl_get",
                        lambda kind, name, **kw: {"metadata": {"name": name}} if name in crds else None)
    args = SimpleNamespace(namespace="monitoring", rule_packs_dir="rule-packs",
                           prometheus=None, kubeconfig=None, json=False, ci=False)
    return oc.OperatorChecker(args), asked


class TestOperatorCheckLeftoverAlertmanagerConfigs:
    def test_a_leftover_warns_and_says_to_delete_it(self, monkeypatch):
        checker, asked = _checker(monkeypatch, installed=True, leftovers=2)
        result = checker.check_alertmanager_config()
        assert result.status == "warn", result.detail
        assert "2" in result.detail
        assert "continue: true" in result.detail
        assert "delete" in result.detail
        assert "generate-routes" in result.detail
        assert ("alertmanagerconfig", {"kubeconfig": None, "namespace": "monitoring",
                                       "selector": oc._PART_OF}) in asked

    def test_a_leftover_warns_in_zh_too(self, monkeypatch):
        monkeypatch.setenv("DA_LANG", "zh")
        checker, _ = _checker(monkeypatch, installed=True, leftovers=1)
        result = checker.check_alertmanager_config()
        assert result.status == "warn"
        assert "continue: true" in result.detail and "generate-routes" in result.detail

    def test_none_passes(self, monkeypatch):
        checker, _ = _checker(monkeypatch, installed=True, leftovers=0)
        result = checker.check_alertmanager_config()
        assert result.status == "pass", result.detail

    def test_crd_not_installed_passes(self, monkeypatch):
        checker, asked = _checker(monkeypatch, installed=False, leftovers=0)
        result = checker.check_alertmanager_config()
        assert result.status == "pass", result.detail
        assert oc._CRD_AM_CONFIGS in result.detail
        assert not [k for k, _ in asked if k == "alertmanagerconfig"]
