"""test_operator_generate_v2.py — operator-generate 的 CLI 與輸出通道

#3572 起 operator-generate 不再產 AlertmanagerConfig（receiver 模板、secret 引用、
抑制規則、租戶探索都隨之移除）；拒收路由相關旗標的契約釘在
test_operator_routing_single_renderer.py。這裡留下 PrometheusRule / ServiceMonitor
的輸出、--json 單一文件、寫檔 opt-in 與 selector label。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/tools to path
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools"))

import operator_generate as og
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


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
        """Operator section declares only what the chart's templates read (#2073).

        ruleLabels / receiverTemplate / secretRef used to be declared here
        but no template ever read them — setting them was a silent no-op.
        Per ADR-008 the chart stays path-agnostic: PrometheusRule and
        AlertmanagerConfig come from `da-tools operator-generate`
        (--receiver-template / --secret-name / --secret-key).
        """
        operator = values_yaml["rules"]["operator"]
        assert set(operator) == {"serviceMonitor"}, sorted(operator)

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
            "--output-dir", str(out),
            "--json", *extra,
        ]):
            emit_result(og.main())
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
        assert kinds == ["PrometheusRule", "ServiceMonitor"]   # #3572: no AlertmanagerConfig

        # 沒有 --kustomize ⇒ 該欄確實是 null（不是 {} 也不是缺鍵）
        assert doc["kustomization"] is None

        # summary 保留原本那些計數鍵
        assert doc["summary"] == {
            "prometheus_rules": 1,
            "service_monitor": 1,
            "kustomization": 0,
            "total": 2,
        }

    def test_kustomize_json_is_still_one_document(self, gen_dirs, capsys):
        """`--kustomize --json` → kustomization 進到**文件裡**，不是第二份 YAML/JSON。"""
        doc = self._run_json(gen_dirs, capsys, "--kustomize")

        assert set(doc) == {"crds", "kustomization", "summary"}
        assert doc["kustomization"]["kind"] == "Kustomization"
        assert doc["summary"]["kustomization"] == 1
        assert doc["summary"]["total"] == 3      # 2 CRD + 1 kustomization
        assert len(doc["crds"]) == 2

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
        assert doc["summary"]["total"] == 3
        assert len(doc["crds"]) == 2


class TestWritingIsOptIn:
    """#1582：沒有 `--output-dir` 就一個檔也不寫，CRD 改走 stdout。

    ⛔ 缺陷形狀是「輸入被重導、輸出沒被重導」：把 `--config-dir` 指向一棵拋棄式
    fixture 樹，工具仍把產物寫進**呼叫目錄**的 `operator-manifests/`。
    反事實（`9f5fe89a`，本改動前）：同一條指令在呼叫目錄建出 17 個檔、stdout 空。

    三支測試各釘一個方向，任何一支單獨都不夠：
      * 下界——不給輸出就不准寫（拿掉 `output_dir is None` 分派即紅）；
      * 上界——給了輸出仍然要寫（把寫入整段拿掉即紅，光有下界抓不到）；
      * `--gitops` 不得在 stdout 這條路上被靜默丟掉（本改動把 stdout 從
        `--dry-run` 專屬變成**預設**路徑，而 `gitops` 原本只傳給 write path）。
    """

    @staticmethod
    def _tree(root: Path) -> set:
        """`root` 底下的完整相對路徑集合。

        ⛔ 用集合而不是「有沒有某個具名目錄」，否則預設輸出路徑一改名，這支測試
        就對它盲了。⛔ 而且 `root` 必須是 **`tmp_path`**（涵蓋 rule-packs / conf.d /
        呼叫目錄三棵），不能只給呼叫目錄：盲審實測，只看 cwd 時「改成把 19 個檔
        寫進被重導的 `--config-dir` 樹」這個變異**存活**——而那正是本票 docstring
        描述的情境。⚠️ 誠實邊界：寫到 `tmp_path` 之外的絕對路徑仍不在覆蓋內。
        """
        return {p.relative_to(root) for p in root.rglob("*")}

    def test_no_output_dir_writes_nothing_and_streams_to_stdout(
        self, gen_dirs, tmp_path, monkeypatch, capsys,
    ):
        packs, confd, _unused = gen_dirs
        cwd = tmp_path / "caller_cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        before = self._tree(tmp_path)
        with patch("sys.argv", [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
        ]):
            emit_result(og.main())
        after = self._tree(tmp_path)

        assert after == before, (
            f"no --output-dir was given, so NOTHING may be written anywhere under "
            f"tmp_path (cwd, the rule-packs tree, or the redirected conf.d tree); "
            f"these appeared: {sorted(map(str, after - before))}"
        )
        out = capsys.readouterr().out
        assert "kind: PrometheusRule" in out, (
            "writing is opt-in, so the CRDs have to go somewhere — stdout. "
            f"stdout was {out[:200]!r}"
        )

    def test_output_dir_still_writes_and_keeps_stdout_clean(
        self, gen_dirs, tmp_path, monkeypatch, capsys,
    ):
        packs, confd, out_dir = gen_dirs
        cwd = tmp_path / "caller_cwd2"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        before_files = {p for p in self._tree(tmp_path) if (tmp_path / p).is_file()}

        with patch("sys.argv", [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
            "--output-dir", str(out_dir),
        ]):
            emit_result(og.main())

        written = sorted(p.name for p in out_dir.glob("*.yaml"))
        assert written, "--output-dir was given: the write path must still write"
        captured = capsys.readouterr()
        assert captured.out == "", (
            f"write mode keeps stdout empty (progress goes to stderr); got {captured.out[:200]!r}"
        )
        leaked = {
            p for p in self._tree(tmp_path)
            if not str(p).startswith(out_dir.name) and (tmp_path / p).is_file()
        } - before_files
        assert not leaked, (
            f"an explicit --output-dir was given, so every new file must land there; "
            f"these landed elsewhere under tmp_path: {sorted(map(str, leaked))}"
        )

    def test_stdout_is_forced_to_utf8_lf_on_a_legacy_console(
        self, tmp_path, monkeypatch,
    ):
        """stdout 現在是預設的產物通道 ⇒ 它必須與寫檔通道等價。

        ⛔ 不能用 `capsys` 測：那是記憶體裡的 text stream，沒有目標編碼可言，
        **沒有修法也會綠**。這裡把 `sys.stdout` 換成真的 cp950 `TextIOWrapper`
        （並帶 Windows 的 CRLF 轉譯）來重現機制：PyYAML 對目標編碼放不下的字元
        會退成字面 escape，而 `write_text_secure` 寫出的檔是真正的字元。
        盲審在 cp950 主控台上實測 15 份相同、**1 份語意不同**、rc=0 無聲。
        """
        import io as _io

        # 變異測試會把預設值還原成 cwd-relative，所以這支也要隔離 cwd，
        # 否則變異跑動時會把產物漏進工作樹（實測漏過一次）。
        cwd = tmp_path / "caller_cwd5"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        packs = tmp_path / "rule-packs"
        packs.mkdir()
        (packs / "rule-pack-uni.yaml").write_text(
            "groups:\n"
            "  - name: uni_alerts\n"
            "    rules:\n"
            "      - alert: UniHigh\n"
            "        expr: uni_metric > 80\n"
            "        annotations:\n"
            "          summary: needs an HA set (≥ 2) to page\n",
            encoding="utf-8",
        )
        confd = tmp_path / "conf.d"
        confd.mkdir()
        (confd / "_defaults.yaml").write_text(
            "defaults:\n  uni_metric: 80\n", encoding="utf-8")

        buf = _io.BytesIO()
        legacy = _io.TextIOWrapper(buf, encoding="cp950", newline="\r\n")
        monkeypatch.setattr(sys, "stdout", legacy)
        with patch("sys.argv", [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
            "--components", "rules",
        ]):
            emit_result(og.main())
        legacy.flush()
        raw = buf.getvalue()

        assert raw, "the stdout path must have emitted something"
        assert b"\\u2265" not in raw, (
            "the character survives into the file channel but was escaped on stdout — "
            "the two channels are not equivalent, and stdout is now the default one"
        )
        assert "≥".encode("utf-8") in raw, (
            f"stdout must carry the real character as UTF-8; got {raw[:200]!r}"
        )
        assert b"\r\n" not in raw, (
            "write_text_secure forces LF and calls it load-bearing; the stdout "
            "channel must not hand back CRLF for the same document"
        )

    def test_empty_output_dir_is_rejected_not_silently_the_cwd(
        self, gen_dirs, tmp_path, monkeypatch,
    ):
        """`--output-dir "$OUT"` 在 OUT 未設時傳空字串，而 `Path("").resolve()` 是 cwd。

        ⛔ 這一格在本改動之前命中面是零（沒人需要打這個參數），之後是**全部**。
        ⛔ 判定必須發生在 argparse 的 type 層：`type=Path` 之後再判就晚了，
        `Path("")` 與 `Path(".")` 已經無法區分（實測補判版本回 rc=0 並照樣寫檔）。
        `.` 仍須合法——那是明確要求寫進目前目錄。
        """
        packs, confd, _unused = gen_dirs
        cwd = tmp_path / "caller_cwd4"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        for bad in ("", " "):
            with pytest.raises(SystemExit) as exc:
                with patch("sys.argv", [
                    "operator_generate.py",
                    "--rule-packs-dir", str(packs),
                            "--output-dir", bad,
                ]):
                    emit_result(og.main())
            assert exc.value.code == 2, f"argparse caller error expected for {bad!r}"
            assert self._tree(cwd) == set(), (
                f"{bad!r} must be rejected, not resolved to the cwd; "
                f"these appeared: {sorted(map(str, self._tree(cwd)))}"
            )

        # 必響對照組：`.` 是合法的，仍然要寫。
        with patch("sys.argv", [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
            "--output-dir", ".",
        ]):
            emit_result(og.main())
        assert list(cwd.glob("*.yaml")), "--output-dir . must still write into the cwd"

    def test_gitops_is_not_silently_dropped_on_the_stdout_path(
        self, gen_dirs, tmp_path, monkeypatch, capsys,
    ):
        packs, confd, _unused = gen_dirs
        cwd = tmp_path / "caller_cwd3"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        argv = [
            "operator_generate.py",
            "--rule-packs-dir", str(packs),
        ]
        with patch("sys.argv", list(argv)):
            emit_result(og.main())
        plain = capsys.readouterr().out
        with patch("sys.argv", argv + ["--gitops"]):
            emit_result(og.main())
        gitops = capsys.readouterr().out

        assert plain and gitops, "both runs must emit YAML to stdout"
        assert plain != gitops, (
            "--gitops is a rendering property (sorted keys). If the stdout path "
            "ignores it, the two runs are byte-identical and the flag is silently "
            "dropped — which is what this test exists to catch."
        )


class TestSelectorLabels:
    """#2075：Prometheus 只載入 label 對得上 selector 的 PrometheusRule／ServiceMonitor。

    文件宣稱產出的 PrometheusRule 帶 `prometheus` 與 `release` 兩個 label，程式
    卻只帶 `prometheus`；Helm release 不叫 kube-prometheus-stack 時更沒有旗標可改。
    """

    def _labels_of(self, gen_dirs, capsys, *extra) -> dict:
        doc = TestMainJsonSingleDocument._run_json(
            TestMainJsonSingleDocument(), gen_dirs, capsys, "--dry-run", *extra,
        )
        return {c["kind"]: c["metadata"]["labels"] for c in doc["crds"]}

    def test_defaults_carry_both_documented_selector_labels(self, gen_dirs, capsys):
        labels = self._labels_of(gen_dirs, capsys)
        assert labels["PrometheusRule"] == {
            "prometheus": "kube-prometheus",
            "release": "kube-prometheus-stack",
            "app.kubernetes.io/part-of": "dynamic-alerting",
        }
        assert labels["ServiceMonitor"] == {
            "release": "kube-prometheus-stack",
            "app.kubernetes.io/part-of": "dynamic-alerting",
        }

    def test_selector_label_overrides_and_extends_both_crds(self, gen_dirs, capsys):
        labels = self._labels_of(
            gen_dirs, capsys,
            "--selector-label", "release=my-prom",
            "--selector-label", "team=db",
        )
        for kind in ("PrometheusRule", "ServiceMonitor"):
            assert labels[kind]["release"] == "my-prom", kind
            assert labels[kind]["team"] == "db", kind
            assert labels[kind]["app.kubernetes.io/part-of"] == "dynamic-alerting", kind
        assert labels["PrometheusRule"]["prometheus"] == "kube-prometheus"
        assert "team" not in labels.get("AlertmanagerConfig", {}), (
            "selector labels are for Prometheus selection, not AlertmanagerConfig"
        )

    @pytest.mark.parametrize("good", [
        "team=",                                # empty value is allowed
        "example.com/team=db",
        "a-b.c1.example.com/Team_1.x=v-1",
    ])
    def test_valid_selector_label_is_accepted(self, good):
        args = og.build_arg_parser().parse_args(["--selector-label", good])
        assert args.selector_label == [tuple(good.split("=", 1))]

    @pytest.mark.parametrize("bad", [
        "no-equals-sign",
        "bad key=x",
        "k=" + "a" * 64,                        # value over 63 chars
        "team..example.com/name=x",             # empty DNS label in the prefix
        "a-.example.com/name=x",                # DNS label ending in '-'
        "a/b/c=x",                              # '/' inside the prefix
        "example.com/=x",                       # prefix with an empty name
        "team=db\n",                            # trailing newline
        "app.kubernetes.io/part-of=other",      # identifies this tool's CRDs
    ])
    def test_invalid_selector_label_is_a_caller_error(self, bad, capsys):
        with pytest.raises(SystemExit) as exc:
            og.build_arg_parser().parse_args(["--selector-label", bad])
        assert exc.value.code == 2
        assert "--selector-label" in capsys.readouterr().err
