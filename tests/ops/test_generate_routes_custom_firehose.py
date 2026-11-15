"""#1211 gap 1：page-mode custom alert 落到沒有 integration 的 firehose。

custom alert 的 `mode` 預設 `page`。產生器只替有效 `_routing` 的租戶在
`component="custom"` 隔離 route 下掛子路由；其餘租戶的 custom alert 落到
`custom-alerts-firehose`，而出貨的 base 裡那個 receiver 沒有 integration——
告警永遠不會送出，產生器卻 rc 0、一聲不吭。

本檔釘住核可的設計（D5）：

* 租戶有 da-guard served-values 判為 **served** 且 page mode 的 custom alert、
  在 custom route 下沒有投遞子路由 ⇒ 每租戶一個 `custom_alert_undelivered`
  finding（stderr 一行＋`--findings-json` 一筆）。
* 看得到 firehose 的模式（`--output-configmap`、`--apply`）才判它有沒有
  integration：有 ⇒ 沒有 finding；沒有 ⇒ WARN，`--strict` 升為 ERROR、rc 1、
  不寫檔。看不到的模式（`--validate`；#1211 缺口 2 移除了 render）照樣 WARN，但說明沒檢查到，
  `--strict` 不升級。
* 租戶自己寫的 `_routing: disable` 是刻意退出：照報 WARN，`--strict` 不升級。

每個主張都配一個反例控制組，避免「什麼都報」或「什麼都不報」的實作也能過。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: 產生器以 judge 身分讀樹（#3206）；同 test_generate_routes_orchestration.py。
pytestmark = pytest.mark.usefixtures("judging_run")

import _grar_render as render  # noqa: E402
import _lib_tenant_values  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402
from _kubectl_fake import run_from_argv as kubectl_from_argv  # noqa: E402  (#3539)

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")
KIND = "custom_alert_undelivered"


def _alert(name: str, mode: str | None = None) -> str:
    """One threshold recipe; the metric is named after the alert, so two
    alerts never share a shape (a duplicate is not served)."""
    extra = f"\n        mode: {mode}" if mode else ""
    return textwrap.dedent(f"""\
      - recipe: threshold
        name: {name}
        metric: {name}_depth
        op: ">"
        window: 5m
        threshold: "1000:warning"{extra}
    """)


def _tenant(tenant: str, alerts: list[str], routing: str | None = None) -> str:
    body = "".join(textwrap.indent(a, "      ") for a in alerts)
    out = f"tenants:\n  {tenant}:\n    _custom_alerts:\n{body}"
    if routing is not None:
        out += textwrap.indent(routing, "    ")
    return out


_WEBHOOK_ROUTING = textwrap.dedent("""\
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/alerts"
""")

#: 名稱 → (租戶檔內容)。每個 tree 一個租戶 `t1`。
TREES = {
    # 有 served page alert（一個 mode 未寫、一個明寫 page）、沒有 _routing。
    "page_no_routing": _tenant("t1", [_alert("q_high"), _alert("q_page", "page")]),
    # exporter 不 serve 的 page alert（同 shape 重複的第二筆 = duplicate）：
    # 它根本不會成為規則，算它就是替不存在的告警報警。
    "unserved_only": _tenant("t1", [
        _alert("q_high", "silent"),
        _alert("q_high", "page").replace("name: q_high", "name: q_dup")]),
    # 只有 silent 的 custom alert：不在 page mode，不送出是設計。
    "silent_only": _tenant("t1", [_alert("q_quiet", "silent")]),
    # page alert，但租戶有有效 _routing ⇒ custom route 下有它的子路由。
    "page_with_routing": _tenant("t1", [_alert("q_high")], _WEBHOOK_ROUTING),
    # page alert，租戶自己寫 `_routing: disable`（顯式退出）。
    "page_routing_disabled": _tenant("t1", [_alert("q_high")], "_routing: disable\n"),
}

#: Both bases wire watchdog-heartbeat, so the only receiver judged here is the
#: firehose (an unwired one is its own finding under --strict, #1211 gap 2).
_WATCHDOG_WIRED = textwrap.indent(textwrap.dedent("""\
    - name: watchdog-heartbeat
      webhook_configs:
        - url: "https://deadman.example.com/ping"
"""), "  ")
_BASE_NO_FIREHOSE_INTEGRATION = textwrap.dedent("""\
    global:
      resolve_timeout: 5m
    route:
      receiver: default
    receivers:
      - name: default
      - name: custom-alerts-firehose
""") + _WATCHDOG_WIRED
_BASE_FIREHOSE_WITH_WEBHOOK = textwrap.dedent("""\
    global:
      resolve_timeout: 5m
    route:
      receiver: default
    receivers:
      - name: default
      - name: custom-alerts-firehose
        webhook_configs:
          - url: "https://logs.example.com/am"
""") + _WATCHDOG_WIRED


def _tree(tmp_path: Path, name: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(TREES[name], encoding="utf-8")
    return d


def _base(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "base.yml"
    p.write_text(text, encoding="utf-8")
    return p


def _run(conf_d: Path, *argv: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """跑一次產生器，回傳 (process, 本模組那個 kind 的 findings 紀錄)。"""
    fj = conf_d.parent / "findings.json"
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(conf_d), *argv,
         "--findings-json", str(fj)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    doc = json.loads(fj.read_text(encoding="utf-8"))
    assert doc["exit_code"] == proc.returncode, doc
    return proc, [r for r in doc["findings"] if r["kind"] == KIND]


def _main(argv: list[str]) -> int:
    """``gar.main`` in-process; its exit code (a mode that writes returns)."""
    try:
        gar.main(argv)
    except SystemExit as exc:
        return exc.code
    return 0


def _lines(proc: subprocess.CompletedProcess) -> list[str]:
    return [ln for ln in proc.stderr.splitlines() if "custom-alerts-firehose" in ln
            and "page-mode custom alert" in ln]


# ── 看不到 firehose 的模式：照報，但說沒檢查到 ─────────────────────────

@pytest.mark.parametrize("mode", [["--validate"], ["--validate", "--strict"]],
                         ids=["validate", "validate-strict"])
def test_page_alert_without_routing_warns_when_the_firehose_is_unknown(tmp_path, mode):
    proc, recs = _run(_tree(tmp_path, "page_no_routing"), *mode)
    assert proc.returncode == 0, proc.stderr[-2000:]
    [line] = _lines(proc)
    assert line.startswith("WARN: tenant 't1': 2 served page-mode custom alert(s) "
                           "(q_high, q_page)"), line
    assert "cannot check" in line, line
    [rec] = recs
    assert (rec["tenant"], rec["severity"], rec["blocks"]) == ("t1", "warning", "never")


# ── 看得到 firehose 的模式（--output-configmap）────────────────────────

def test_strict_refuses_when_the_firehose_has_no_integration(tmp_path):
    out = tmp_path / "cm.yaml"
    proc, recs = _run(_tree(tmp_path, "page_no_routing"), "--output-configmap",
                      "--base-config", str(_base(tmp_path, _BASE_NO_FIREHOSE_INTEGRATION)),
                      "--strict", "-o", str(out))
    assert proc.returncode == 1, proc.stderr[-2000:]
    [line] = _lines(proc)
    assert line.startswith("ERROR: tenant 't1'") and "has no integration" in line, line
    assert "FAIL: page-mode custom alerts would notify no one" in proc.stderr
    assert not out.exists(), "a refused ConfigMap was written"
    [rec] = recs
    assert (rec["severity"], rec["blocks"]) == ("error", "strict")


def test_without_strict_the_same_tree_is_a_warning_and_is_written(tmp_path):
    out = tmp_path / "cm.yaml"
    proc, recs = _run(_tree(tmp_path, "page_no_routing"), "--output-configmap",
                      "--base-config", str(_base(tmp_path, _BASE_NO_FIREHOSE_INTEGRATION)),
                      "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-2000:]
    [line] = _lines(proc)
    assert line.startswith("WARN: tenant 't1'") and "has no integration" in line, line
    assert out.is_file()
    [rec] = recs
    assert (rec["severity"], rec["blocks"]) == ("warning", "strict")


def test_the_built_in_base_firehose_has_no_integration_either(tmp_path):
    """沒給 --base-config 時組出的是內建 base：firehose 只有名字，一樣判得出來。"""
    proc, recs = _run(_tree(tmp_path, "page_no_routing"), "--output-configmap",
                      "--strict", "-o", str(tmp_path / "cm.yaml"))
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert [r["blocks"] for r in recs] == ["strict"]


def test_a_firehose_with_an_integration_is_no_finding(tmp_path):
    out = tmp_path / "cm.yaml"
    proc, recs = _run(_tree(tmp_path, "page_no_routing"), "--output-configmap",
                      "--base-config", str(_base(tmp_path, _BASE_FIREHOSE_WITH_WEBHOOK)),
                      "--strict", "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _lines(proc) == [] and recs == []
    assert out.is_file()


@pytest.mark.parametrize("tree", ["silent_only", "unserved_only", "page_with_routing"])
def test_no_page_alert_to_lose_is_no_finding(tmp_path, tree):
    out = tmp_path / "cm.yaml"
    proc, recs = _run(_tree(tmp_path, tree), "--output-configmap",
                      "--base-config", str(_base(tmp_path, _BASE_NO_FIREHOSE_INTEGRATION)),
                      "--strict", "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _lines(proc) == [] and recs == []
    assert out.is_file()


def test_silent_only_is_no_finding_in_validate_mode_either(tmp_path):
    proc, recs = _run(_tree(tmp_path, "silent_only"), "--validate")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _lines(proc) == [] and recs == []


def test_explicit_routing_disable_warns_but_strict_does_not_escalate(tmp_path):
    out = tmp_path / "cm.yaml"
    proc, recs = _run(_tree(tmp_path, "page_routing_disabled"), "--output-configmap",
                      "--base-config", str(_base(tmp_path, _BASE_NO_FIREHOSE_INTEGRATION)),
                      "--strict", "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-2000:]
    [line] = _lines(proc)
    assert line.startswith("WARN: tenant 't1'") and "_routing is 'disable'" in line, line
    assert "--strict does not escalate it" in line, line
    assert out.is_file()
    [rec] = recs
    assert (rec["severity"], rec["blocks"]) == ("warning", "never")


# ── --apply：判的是叢集上的 firehose ────────────────────────────────

def _fake_cluster(monkeypatch, am_yml: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        out = json.dumps({"data": {"alertmanager.yml": am_yml}}) \
            if argv[:2] == ["kubectl", "get"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setattr(render, "kubectl_run", kubectl_from_argv(fake))
    return calls


@pytest.mark.parametrize("base,strict,rc", [
    (_BASE_NO_FIREHOSE_INTEGRATION, True, 1),
    (_BASE_NO_FIREHOSE_INTEGRATION, False, 0),
    (_BASE_FIREHOSE_WITH_WEBHOOK, True, 0),
], ids=["no-integration-strict", "no-integration", "webhook-strict"])
def test_apply_judges_the_clusters_firehose(tmp_path, monkeypatch, capsys,
                                            base, strict, rc):
    calls = _fake_cluster(monkeypatch, base)
    conf_d = _tree(tmp_path, "page_no_routing")
    code = _main(["--config-dir", str(conf_d), "--apply", "--yes",
                  *(["--strict"] if strict else [])])
    err = capsys.readouterr().err
    assert code == rc, err[-2000:]
    applied = any(c[:2] == ["kubectl", "apply"] for c in calls)
    assert applied is (rc == 0), calls
    found = [ln for ln in err.splitlines() if ln.split(": ", 1)[-1].startswith("tenant 't1'")]
    if base is _BASE_FIREHOSE_WITH_WEBHOOK:
        assert found == []
    else:
        [line] = found
        assert line.startswith("ERROR:" if strict else "WARN:"), line


# ── da-guard：一次讀取、讀不到只是 NOTICE ───────────────────────────

def test_one_served_values_run_per_invocation(tmp_path, monkeypatch, capsys):
    """--validate 下 key verdict 與本檢查共用同一次 served-values。"""
    real = _lib_tenant_values.load_served_tree
    calls = []

    def counting(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(_lib_tenant_values, "load_served_tree", counting)
    code = _main(["--config-dir", str(_tree(tmp_path, "page_no_routing")), "--validate"])
    err = capsys.readouterr().err
    assert code == 0, err[-2000:]
    assert len(calls) == 1, calls
    assert any("page-mode custom alert" in ln for ln in err.splitlines()), err[-2000:]


def test_a_failing_served_values_read_is_a_notice_not_a_refusal(tmp_path, monkeypatch,
                                                                 capsys):
    def boom(*_a, **_kw):
        raise _lib_tenant_values.ServedValuesError("da-guard broke", 1, "")

    monkeypatch.setattr(_lib_tenant_values, "load_served_tree", boom)
    code = _main(["--config-dir", str(_tree(tmp_path, "page_no_routing")),
                  "--output-configmap", "--strict", "-o", str(tmp_path / "cm.yaml")])
    err = capsys.readouterr().err
    assert code == 0, err[-2000:]
    assert "NOTICE: custom-alert delivery not checked" in err
    assert "da-guard broke" in err


# ── 純函式：判定式本身 ─────────────────────────────────────────────

def test_the_custom_route_child_predicate_is_the_injection_one():
    """有主 tenant route 的租戶才有 custom 子路由；platform-enforced 的不算。"""
    routes = [
        {"matchers": ['tenant="a"'], "receiver": "tenant-a"},
        {"matchers": ['tenant="b"'], "receiver": "platform-enforced-b"},
    ]
    got = render.undelivered_custom_alert_tenants(
        {"a": ("x",), "b": ("y",), "c": ("z",)}, routes, frozenset({"c"}))
    assert [(u.tenant, u.routing_disabled) for u in got] == [("b", False), ("c", True)]
    # 判定式與注入用同一個：注入後 custom route 的子路由正好是 a。
    injected, _ = render._inject_custom_alert_isolation(routes, [])
    [custom] = [r for r in injected if 'component="custom"' in r["matchers"]]
    assert [c["receiver"] for c in custom["routes"]] == ["tenant-a"]


def test_the_firehose_reading_uses_non_empty_configs_only():
    def am(entry):
        return {"receivers": [entry]}
    assert render.custom_firehose_has_integration(
        am({"name": "custom-alerts-firehose", "webhook_configs": [{"url": "u"}]}))
    assert not render.custom_firehose_has_integration(
        am({"name": "custom-alerts-firehose", "webhook_configs": []}))
    assert not render.custom_firehose_has_integration(
        am({"name": "other", "webhook_configs": [{"url": "u"}]}))
