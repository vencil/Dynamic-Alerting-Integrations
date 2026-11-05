"""#1211 缺口 2：三種輸出模式共用一個合併，base 只當來源（owner 裁決 C′＋S）。

背景：fragment 模式（不帶模式旗標）的檔頭叫 BYO 客戶把輸出「接在自己的路由
後面」，但平台的 4 條路由（Watchdog、custom 隔離、synthetic probe、sentinel
sinkhole）只在 `--output-configmap`／`--apply` 裡注入，而且必須排在最前面。
客戶自有一條 `severity="critical" → noc-pager` 時，照檔頭接上去的結果是：
Watchdog 進客戶預設頻道（dead-man's switch 收不到心跳）、租戶的 critical
custom 告警與 sentinel 叫醒客戶 NOC（#741 的隔離失效）。
`--output-configmap`／`--apply` 則反過來整個取代 `route.routes`，把客戶自有的
路由丟掉。

本檔釘住 owner 拍板的設計：

* fragment 模式移除；只帶 `-o`、或什麼模式都不帶 ⇒ 結束碼 2，不寫檔。
* 輸出 = merge(來源 base, conf.d)。base 是客戶自己的 config，不是本工具上一次
  的輸出（同 Prometheus Operator 的 input／generated Secret）：base 裡出現
  `tenant-*`／`platform-enforced*` receiver ⇒ 結束碼 1、不寫檔；`-o` 指向
  `--base-config` 本身 ⇒ 結束碼 2。
* 合併後順序：[平台 4 條 ＋ enforced NOC] → [客戶路由，原順序] →
  [租戶路由 ＋ root fallthrough]；客戶的 receiver、inhibit 原樣保留。
* `--apply` 以叢集 ConfigMap 的 `alertmanager.base.yml` 為來源（首次沒有時
  採用 `alertmanager.yml`），寫回時兩個 key 一起寫。
* `watchdog-heartbeat` 沒接 integration：WARN；有真實 base（`--base-config`，
  或 `--apply` 的叢集 config）且 `--strict` 時 ERROR、結束碼 1、不寫檔。

路由一律交給 Alertmanager 自己的路由引擎判（`amtool config routes test
--verify.receivers`，完全相等比對：多送、漏送、順序錯都非 0）。amtool 取得：
PATH 上的 `amtool` 優先，否則用 docker 跑部署 manifest 的 alertmanager image
（同 test_alertmanager_routing_correctness.py）；兩者都沒有則 skip——CI 的
`VIBE_REQUIRE_DOCKER=1` 由該檔的 presence guard 擋住靜默 skip。
租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

#: 產生器以 judge 身分讀樹（#3206）；同 test_generate_routes_custom_firehose.py。
pytestmark = pytest.mark.usefixtures("judging_run")

import _grar_render as render  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_routes import generate_inhibit_rules, generate_routes  # noqa: E402
from test_alertmanager_amtool_validity import _AM_IMAGE, _DOCKER, ensure_am_image  # noqa: E402

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

# ── fixture tree ────────────────────────────────────────────────────

#: per-tenant enforced NOC（`{{tenant}}`）：每個租戶一條 continue:true 的
#: `platform-enforced-<t>`，沒有 tenant route 的租戶另有 root fallthrough。
_DEFAULTS = textwrap.dedent("""\
    defaults:
      cpu_usage_percent: 80
    _routing_enforced:
      enabled: true
      receiver:
        type: webhook
        url: "https://noc.example.com/{{tenant}}"
""")
_TENANTS = {
    # 有 _routing ⇒ main tenant route（與 custom 子樹下的子路由）。
    "t-routed": textwrap.dedent("""\
        tenants:
          t-routed:
            _routing:
              receiver:
                type: webhook
                url: "https://tenant.example.com/hook"
    """),
    # 只有閾值 ⇒ 沒有 tenant route，只有 root fallthrough。
    "t-thr": "tenants:\n  t-thr:\n    cpu_usage_percent: '85'\n",
}

#: 客戶自有的 Alertmanager：root 有預設頻道、一條 critical → NOC pager 的路由，
#: watchdog-heartbeat 接了 dead-man's switch，另有一條自己的 inhibit。
_CUSTOMER_BASE = textwrap.dedent("""\
    global:
      resolve_timeout: 5m
    route:
      receiver: cust-default
      group_by: [alertname, tenant]
      routes:
        - matchers: ['severity="critical"']
          receiver: noc-pager
        - matchers: ['team="db"']
          receiver: cust-db
    receivers:
      - name: cust-default
        webhook_configs:
          - url: https://cust.example.com/default
      - name: noc-pager
        webhook_configs:
          - url: https://cust.example.com/pager
      - name: cust-db
        webhook_configs:
          - url: https://cust.example.com/db
      - name: watchdog-heartbeat
        webhook_configs:
          - url: https://deadman.example.com/ping
    inhibit_rules:
      - source_matchers: ['alertname="CustDown"', 'cluster=~".+"']
        target_matchers: ['alertname="CustDegraded"', 'cluster=~".+"']
        equal: [cluster]
""")
#: 同一份，但 watchdog-heartbeat 只有名字（沒有 integration）。
_BASE_WATCHDOG_UNWIRED = _CUSTOMER_BASE.replace(
    "  - name: watchdog-heartbeat\n    webhook_configs:\n"
    "      - url: https://deadman.example.com/ping\n",
    "  - name: watchdog-heartbeat\n")
assert _BASE_WATCHDOG_UNWIRED != _CUSTOMER_BASE

_PLATFORM_RECEIVERS = ("watchdog-heartbeat", "custom-alerts-firehose",
                       "synthetic-receiver", "sentinel-sinkhole")


def _tree(tmp_path: Path) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    for tid, body in _TENANTS.items():
        (d / f"{tid}.yaml").write_text(body, encoding="utf-8")
    return d


def _base(tmp_path: Path, text: str = _CUSTOMER_BASE, name: str = "base.yml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=300)


def _emit_am(tmp_path: Path, conf_d: Path, base: Path, out_name: str = "am.yml",
             *extra: str) -> str:
    out = tmp_path / out_name
    proc = _run("--config-dir", str(conf_d), "--output-alertmanager",
                "--base-config", str(base), *extra, "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return out.read_text(encoding="utf-8")


def _generated(conf_d: Path) -> tuple[list[dict], list[dict], list[dict]]:
    tree = load_tenant_tree(str(conf_d))
    routes, receivers, _w = generate_routes(
        tree.routing_configs, enforced_routing=tree.enforced_routing,
        tenants=tree.dedup_configs)
    inhibit_rules, _w2 = generate_inhibit_rules(tree.dedup_configs)
    return routes, receivers, inhibit_rules


# ── amtool：Alertmanager 自己的路由引擎 ─────────────────────────────

_AMTOOL = shutil.which("amtool")


def _routes_test(am_yml: Path, labels: dict, expected: str) -> subprocess.CompletedProcess:
    tail = ["config", "routes", "test", f"--verify.receivers={expected}",
            *[f"{k}={v}" for k, v in sorted(labels.items())]]
    if _AMTOOL:
        return subprocess.run([_AMTOOL, *tail[:3], f"--config.file={am_yml}", *tail[3:]],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=120)
    if not _DOCKER:
        pytest.skip("neither amtool on PATH nor docker — routing needs Alertmanager's own engine")
    ensure_am_image()
    am_yml.parent.chmod(0o755)
    am_yml.chmod(0o644)
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "amtool",
         "-v", f"{am_yml.parent.as_posix()}:/etc/alertmanager", _AM_IMAGE,
         *tail[:3], f"--config.file=/etc/alertmanager/{am_yml.name}", *tail[3:]],
        capture_output=True, text=True, encoding="utf-8", timeout=300)


#: (labels, 預期 receiver 序列)。每列都對應規格裡的一條主張。
_DELIVERIES = [
    # Watchdog 進 dead-man's switch，不進客戶預設頻道或 NOC。
    ({"alertname": "Watchdog", "severity": "none"}, "watchdog-heartbeat"),
    ({"alertname": "Watchdog", "severity": "critical"}, "watchdog-heartbeat"),
    # 租戶的 critical custom 告警留在 custom 隔離子樹，不叫醒客戶 NOC（#741）。
    ({"alertname": "QHigh", "component": "custom", "tenant": "t-routed",
      "severity": "critical"}, "tenant-t-routed"),
    ({"alertname": "QHigh", "component": "custom", "tenant": "t-thr",
      "severity": "critical"}, "custom-alerts-firehose"),
    # sentinel 進 sinkhole，不 page 人。
    ({"alertname": "TenantSilentWarning", "component": "sentinel",
      "tenant": "t-routed", "severity": "critical"}, "sentinel-sinkhole"),
    # enforced NOC 在客戶路由之前（continue:true）；客戶路由在租戶路由之前。
    ({"alertname": "CpuHigh", "tenant": "t-routed", "severity": "critical"},
     "platform-enforced-t-routed,noc-pager"),
    ({"alertname": "CpuHigh", "tenant": "t-routed", "severity": "warning"},
     "platform-enforced-t-routed,tenant-t-routed"),
    # 沒有 tenant route 的租戶：fallthrough 回客戶的 root receiver（#2519）。
    ({"alertname": "CpuHigh", "tenant": "t-thr", "severity": "warning"},
     "platform-enforced-t-thr,cust-default"),
    # 客戶自己的告警照客戶路由走。
    ({"alertname": "DbDown", "team": "db", "severity": "warning"}, "cust-db"),
    ({"alertname": "Other", "severity": "info"}, "cust-default"),
]


@pytest.mark.parametrize("labels,expected", _DELIVERIES,
                         ids=[f"{d[0]['alertname']}-{d[0].get('severity')}-"
                              f"{d[0].get('tenant', d[0].get('team', '-'))}"
                              for d in _DELIVERIES])
def test_alertmanager_routes_each_alert_where_the_spec_says(tmp_path, labels, expected):
    am = tmp_path / "out"
    am.mkdir()
    (am / "alertmanager.yml").write_text(
        _emit_am(tmp_path, _tree(tmp_path), _base(tmp_path)), encoding="utf-8")
    r = _routes_test(am / "alertmanager.yml", labels, expected)
    assert r.returncode == 0, (f"{labels} did not resolve to [{expected}]\n"
                               f"{r.stdout}\n{r.stderr}")


def test_control_amtool_rejects_a_wrong_expectation(tmp_path):
    """反例控制組：預期錯時 amtool 必須非 0，否則上面全綠沒有意義。"""
    am = tmp_path / "out"
    am.mkdir()
    (am / "alertmanager.yml").write_text(
        _emit_am(tmp_path, _tree(tmp_path), _base(tmp_path)), encoding="utf-8")
    r = _routes_test(am / "alertmanager.yml", {"alertname": "Watchdog"}, "cust-default")
    assert r.returncode != 0, r.stdout


# ── 結構：保留、順序、冪等 ──────────────────────────────────────────

def _routes_of(am_text: str) -> list[dict]:
    return render.alertmanager_reading(am_text)["route"]["routes"]


def test_customer_routes_are_kept_in_order_between_enforced_and_tenant_routes(tmp_path):
    routes = _routes_of(_emit_am(tmp_path, _tree(tmp_path), _base(tmp_path)))
    receivers = [r.get("receiver") for r in routes]
    assert receivers[:4] == list(_PLATFORM_RECEIVERS), receivers
    enforced = [i for i, r in enumerate(receivers)
                if str(r).startswith("platform-enforced")]
    cust = [receivers.index("noc-pager"), receivers.index("cust-db")]
    tenant = [i for i, r in enumerate(receivers) if str(r).startswith("tenant-")]
    fallthrough = [i for i, r in enumerate(routes)
                   if r.get("receiver") == "cust-default"]
    assert enforced and tenant and fallthrough, receivers
    assert cust[0] < cust[1], "customer routes reordered"
    assert cust == list(range(cust[0], cust[0] + 2)), "customer routes split up"
    assert max(enforced) < cust[0] < cust[1] < min(tenant + fallthrough), receivers
    # the customer's routes come back exactly as written (as Alertmanager reads them)
    assert routes[cust[0]] == {"matchers": ['severity="critical"'], "receiver": "noc-pager"}
    assert routes[cust[1]] == {"matchers": ['team="db"'], "receiver": "cust-db"}


def test_the_customer_receivers_and_inhibit_rules_are_kept(tmp_path):
    am = render.alertmanager_reading(_emit_am(tmp_path, _tree(tmp_path), _base(tmp_path)))
    by_name = {r["name"]: r for r in am["receivers"]}
    for name in ("cust-default", "noc-pager", "cust-db"):
        assert name in by_name, sorted(by_name)
    # the base's richer platform receiver wins over the name-only placeholder
    assert by_name["watchdog-heartbeat"]["webhook_configs"][0]["url"] == \
        "https://deadman.example.com/ping"
    cust_inhibits = [r for r in am["inhibit_rules"]
                     if 'alertname="CustDown"' in r.get("source_matchers", [])]
    assert len(cust_inhibits) == 1, am["inhibit_rules"]
    assert am["global"]["resolve_timeout"] == "5m"


def test_the_same_source_renders_the_same_output(tmp_path):
    conf_d = _tree(tmp_path)
    first = _emit_am(tmp_path, conf_d, _base(tmp_path), "first.yml")
    second = _emit_am(tmp_path, conf_d, _base(tmp_path), "second.yml")
    assert second == first


def test_feeding_the_output_back_as_the_base_is_refused(tmp_path):
    """The base is a source: an earlier output (it holds `tenant-*` /
    `platform-enforced*` receivers) is refused, rc 1, nothing written."""
    conf_d = _tree(tmp_path)
    first = _emit_am(tmp_path, conf_d, _base(tmp_path), "first.yml")
    out = tmp_path / "second.yml"
    proc = _run("--config-dir", str(conf_d), "--output-alertmanager",
                "--base-config", str(_base(tmp_path, first, "base2.yml")), "-o", str(out))
    assert proc.returncode == 1, proc.stderr[-3000:]
    assert "not an earlier output of this tool" in proc.stderr, proc.stderr[-3000:]
    assert "'tenant-t-routed'" in proc.stderr and "'platform-enforced-t-thr'" in proc.stderr
    assert not out.exists()


def test_writing_the_output_over_the_source_is_a_caller_error(tmp_path):
    base = _base(tmp_path)
    before = base.read_bytes()
    proc = _run("--config-dir", str(_tree(tmp_path)), "--output-alertmanager",
                "--base-config", str(base), "-o", str(base))
    assert proc.returncode == 2, proc.stderr[-3000:]
    assert "names the --base-config file itself" in proc.stderr
    assert base.read_bytes() == before, "the source was overwritten"


def test_a_removed_tenant_leaves_nothing_behind(tmp_path):
    """Rendered again from the same source after a tenant is removed: none of
    its routes, receivers or inhibit rules remain."""
    conf_d = _tree(tmp_path)
    first = _emit_am(tmp_path, conf_d, _base(tmp_path), "first.yml")
    assert "tenant-t-routed" in first
    (conf_d / "t-routed.yaml").unlink()
    second = _emit_am(tmp_path, conf_d, _base(tmp_path), "second.yml")
    assert "t-routed" not in second, "a removed tenant survived"
    # ...and the customer's own content is still there
    assert "noc-pager" in second and "CustDown" in second


def test_output_configmap_carries_the_same_alertmanager_yml(tmp_path):
    conf_d = _tree(tmp_path)
    am_text = _emit_am(tmp_path, conf_d, _base(tmp_path))
    cm = tmp_path / "cm.yaml"
    proc = _run("--config-dir", str(conf_d), "--output-configmap",
                "--base-config", str(_base(tmp_path)), "-o", str(cm))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert render.emitted_alertmanager_yml(cm.read_text(encoding="utf-8")) == am_text


def test_the_apply_merge_keeps_the_cluster_routes_too(tmp_path):
    """`--apply` 從「整個取代 route.routes」改為同一個合併：來源的客戶路由保留，
    且結果與 --output-alertmanager 對同一份 base 的輸出相同。"""
    conf_d = _tree(tmp_path)
    routes, receivers, inhibit_rules = _generated(conf_d)
    existing = render.alertmanager_reading(_CUSTOMER_BASE)
    merged = render.merge_generated(existing, routes, receivers, inhibit_rules)
    emitted = render.alertmanager_reading(_emit_am(tmp_path, conf_d, _base(tmp_path)))
    assert render.alertmanager_reading(
        yaml.dump(merged, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)) == emitted


def test_a_customer_tenant_route_is_kept_whatever_its_spelling(tmp_path):
    """客戶寫 `tenant = "t-routed"` 送到自己的 receiver：來源裡的路由一律是
    客戶的，原樣保留（da-read 正規化 matcher 寫法）。"""
    base = _CUSTOMER_BASE.replace(
        "    - matchers: ['team=\"db\"']\n",
        "    - matchers: ['tenant = \"t-routed\"']\n"
        "      receiver: cust-db\n"
        "    - matchers: ['team=\"db\"']\n")
    assert base != _CUSTOMER_BASE
    routes = _routes_of(_emit_am(tmp_path, _tree(tmp_path), _base(tmp_path, base)))
    assert {"matchers": ['tenant="t-routed"'], "receiver": "cust-db"} in routes


# ── CLI：沒有模式就沒有輸出 ─────────────────────────────────────────

@pytest.mark.parametrize("argv", [["-o", "OUT"], [], ["--dry-run"]],
                         ids=["only-o", "bare", "dry-run-only"])
def test_no_mode_flag_is_a_caller_error_and_writes_nothing(tmp_path, argv):
    out = tmp_path / "routes.yaml"
    proc = _run("--config-dir", str(_tree(tmp_path)),
                *[str(out) if a == "OUT" else a for a in argv])
    assert proc.returncode == 2, (proc.returncode, proc.stderr[-2000:])
    assert "--output-alertmanager" in proc.stderr, proc.stderr[-2000:]
    assert not out.exists()
    assert "route:" not in proc.stdout, "a fragment still reached stdout"


def test_render_output_is_gone():
    assert not hasattr(render, "render_output")


# ── watchdog-heartbeat 沒接 integration ────────────────────────────

def _watchdog_lines(proc: subprocess.CompletedProcess) -> list[str]:
    return [ln for ln in proc.stderr.splitlines()
            if "watchdog-heartbeat" in ln and "integration" in ln]


def test_unwired_watchdog_with_a_base_config_is_an_error_under_strict(tmp_path):
    out = tmp_path / "am.yml"
    proc = _run("--config-dir", str(_tree(tmp_path)), "--output-alertmanager",
                "--base-config", str(_base(tmp_path, _BASE_WATCHDOG_UNWIRED)),
                "--strict", "-o", str(out))
    assert proc.returncode == 1, proc.stderr[-3000:]
    [line] = _watchdog_lines(proc)
    assert line.startswith("ERROR:"), line
    assert not out.exists(), "a refused config was written"


def test_unwired_watchdog_without_strict_is_a_warning_and_is_written(tmp_path):
    out = tmp_path / "am.yml"
    proc = _run("--config-dir", str(_tree(tmp_path)), "--output-alertmanager",
                "--base-config", str(_base(tmp_path, _BASE_WATCHDOG_UNWIRED)),
                "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-3000:]
    [line] = _watchdog_lines(proc)
    assert line.startswith("WARN:"), line
    assert out.is_file()


def test_the_built_in_base_is_unwired_by_design_so_strict_only_warns(tmp_path):
    """內建 base 本來就沒接；CI 的 --strict 不能因此壞掉。"""
    out = tmp_path / "cm.yaml"
    proc = _run("--config-dir", str(_tree(tmp_path)), "--output-configmap",
                "--strict", "-o", str(out))
    assert proc.returncode == 0, proc.stderr[-3000:]
    [line] = _watchdog_lines(proc)
    assert line.startswith("WARN:"), line


def test_control_a_wired_watchdog_reports_nothing(tmp_path):
    proc = _run("--config-dir", str(_tree(tmp_path)), "--output-alertmanager",
                "--base-config", str(_base(tmp_path)), "--strict",
                "-o", str(tmp_path / "am.yml"))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _watchdog_lines(proc) == []


# ── --apply：叢集 ConfigMap 的 alertmanager.base.yml 是來源 ────────────

def _fake_cluster(monkeypatch, am_yml: str, base_yml: str | None = None) -> list[list[str]]:
    calls: list[list[str]] = []
    data = {"alertmanager.yml": am_yml}
    if base_yml is not None:
        data["alertmanager.base.yml"] = base_yml

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        out = json.dumps({"data": data}) if argv[:2] == ["kubectl", "get"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    return calls


def _apply(tmp_path, *extra) -> int:
    import generate_alertmanager_routes as gar
    conf_d = tmp_path / "conf.d"
    if not conf_d.is_dir():
        _tree(tmp_path)
    try:
        gar.main(["--config-dir", str(conf_d), "--apply", "--yes", *extra])
    except SystemExit as exc:
        return exc.code
    return 0


def _created(calls) -> dict[str, str]:
    """The keys `kubectl create configmap --from-literal=K=V` was given."""
    [create] = [c for c in calls if c[:3] == ["kubectl", "create", "configmap"]]
    lits = [a[len("--from-literal="):] for a in create if a.startswith("--from-literal=")]
    return dict(lit.split("=", 1) for lit in lits)


def test_apply_adopts_the_cluster_config_as_the_source_then_keeps_it(tmp_path, monkeypatch, capsys):
    """First --apply (no base key): the running alertmanager.yml is the
    source; the ConfigMap then holds the output AND that source."""
    calls = _fake_cluster(monkeypatch, _CUSTOMER_BASE)
    assert _apply(tmp_path) == 0, capsys.readouterr().err[-3000:]
    keys = _created(calls)
    assert keys["alertmanager.base.yml"] == _CUSTOMER_BASE
    out = render.alertmanager_reading(keys["alertmanager.yml"])
    assert "tenant-t-routed" in {r["name"] for r in out["receivers"]}


def test_apply_renders_from_the_base_key_not_from_its_own_output(tmp_path, monkeypatch, capsys):
    """Second --apply: alertmanager.yml is the previous output; the source is
    the base key, and the new output equals a render from that source."""
    calls = _fake_cluster(monkeypatch, _CUSTOMER_BASE)
    assert _apply(tmp_path) == 0
    first = _created(calls)
    capsys.readouterr()
    calls2 = _fake_cluster(monkeypatch, first["alertmanager.yml"], first["alertmanager.base.yml"])
    assert _apply(tmp_path) == 0, capsys.readouterr().err[-3000:]
    second = _created(calls2)
    assert second == first


def test_apply_refuses_a_cluster_config_that_is_already_an_output(tmp_path, monkeypatch, capsys):
    """No base key, but alertmanager.yml already holds generated routing (an
    older --apply): there is no source to render from — refused, nothing
    applied."""
    conf_d = _tree(tmp_path)
    generated = _emit_am(tmp_path, conf_d, _base(tmp_path))
    calls = _fake_cluster(monkeypatch, generated)
    assert _apply(tmp_path) == 1
    assert "not an earlier output of this tool" in capsys.readouterr().err
    assert not [c for c in calls if c[:2] == ["kubectl", "apply"]], calls


@pytest.mark.parametrize("base,strict,rc,level", [
    (_BASE_WATCHDOG_UNWIRED, True, 1, "ERROR:"),
    (_BASE_WATCHDOG_UNWIRED, False, 0, "WARN:"),
    (_CUSTOMER_BASE, True, 0, None),
], ids=["unwired-strict", "unwired", "wired-strict"])
def test_apply_judges_the_clusters_watchdog_receiver(tmp_path, monkeypatch, capsys,
                                                     base, strict, rc, level):
    calls = _fake_cluster(monkeypatch, base)
    code = _apply(tmp_path, *(["--strict"] if strict else []))
    err = capsys.readouterr().err
    assert code == rc, err[-3000:]
    applied = [c for c in calls if c[:2] == ["kubectl", "apply"]]
    assert bool(applied) is (rc == 0), calls
    lines = [ln for ln in err.splitlines()
             if "watchdog-heartbeat" in ln and "integration" in ln]
    if level is None:
        assert lines == [], lines
    else:
        [line] = lines
        assert line.startswith(level), line


# ── fallthrough 與來源：客戶自己的路由、root receiver 改名、空樹 ────────

def _fallthroughs(am: dict, root: str) -> list[dict]:
    return [r for r in am["route"]["routes"]
            if r.get("receiver") == root and set(r) == {"matchers", "receiver"}
            and r["matchers"][0].startswith('tenant="')]


def test_a_customer_tenant_route_to_the_root_receiver_is_kept(tmp_path):
    """An operator's own `tenant="x"` → root route has the fallthrough's
    shape; in the source it is the operator's, kept where it was — for a
    tenant in conf.d and for one that is not."""
    base = _CUSTOMER_BASE.replace(
        "  routes:\n",
        "  routes:\n"
        "    - matchers: ['tenant=\"t-thr\"']\n"
        "      receiver: cust-default\n"
        "    - matchers: ['tenant = \"t-gone\"']\n"
        "      receiver: cust-default\n", 1)
    assert base != _CUSTOMER_BASE
    am_text = _emit_am(tmp_path, _tree(tmp_path), _base(tmp_path, base))
    routes = render.alertmanager_reading(am_text)["route"]["routes"]
    cust_at = [i for i, r in enumerate(routes) if r.get("receiver") == "noc-pager"][0]
    assert routes[cust_at - 2:cust_at] == [
        {"matchers": ['tenant="t-thr"'], "receiver": "cust-default"},
        {"matchers": ['tenant="t-gone"'], "receiver": "cust-default"}], routes
    am = tmp_path / "out"
    am.mkdir()
    (am / "alertmanager.yml").write_text(am_text, encoding="utf-8")
    # the customer's route takes t-thr's critical alert before its noc-pager route
    r = _routes_test(am / "alertmanager.yml",
                     {"alertname": "CpuHigh", "tenant": "t-thr", "severity": "critical"},
                     "platform-enforced-t-thr,cust-default")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_renamed_root_receiver_moves_the_fallthrough_with_it(tmp_path):
    """The source's root receiver renamed: the fallthrough is rendered from
    the source, so it names the new root and nothing names the old one."""
    renamed = _CUSTOMER_BASE.replace("  receiver: cust-default\n  group_by",
                                     "  receiver: noc-pager\n  group_by", 1)
    assert renamed != _CUSTOMER_BASE
    am = render.alertmanager_reading(
        _emit_am(tmp_path, _tree(tmp_path), _base(tmp_path, renamed)))
    assert [r["matchers"] for r in _fallthroughs(am, "noc-pager")] == [['tenant="t-thr"']]
    assert _fallthroughs(am, "cust-default") == []


@pytest.mark.parametrize("keep_enforced", [True, False],
                         ids=["enforced-left:no-valid-routes", "nothing-left:no-tenants"])
def test_an_output_mode_on_an_empty_tree_writes_the_source_and_platform_routes(
        tmp_path, keep_enforced):
    """With no tenant left an output mode still writes (the source plus the
    platform routes) — the output is always a render of (source, conf.d).
    Both early exits: `_routing_enforced` still declared (no route generated)
    and nothing routable declared at all (no tenants found)."""
    conf_d = _tree(tmp_path)
    for t in _TENANTS:
        (conf_d / f"{t}.yaml").unlink()
    if not keep_enforced:
        (conf_d / "_defaults.yaml").write_text(
            "defaults:\n  cpu_usage_percent: 80\n", encoding="utf-8")
    out = _emit_am(tmp_path, conf_d, _base(tmp_path), "empty.yml")
    assert "tenant-" not in out and "platform-enforced" not in out, out
    assert "watchdog-heartbeat" in out and "noc-pager" in out and "CustDown" in out


def test_control_apply_keeps_refusing_an_empty_tree(tmp_path, monkeypatch, capsys):
    """--apply keeps the early exit on an empty tree (a wrong --config-dir
    must not strip the cluster's routing): nothing is applied."""
    import generate_alertmanager_routes as gar
    calls = _fake_cluster(monkeypatch, _CUSTOMER_BASE)
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        gar.main(["--config-dir", str(d), "--apply", "--yes"])
    assert exc.value.code == 0
    assert not [c for c in calls if c[:2] == ["kubectl", "apply"]], calls


def test_a_base_that_carries_the_platform_routes_gets_each_once(tmp_path):
    """The platform's own base (k8s/03-monitoring) writes the four platform
    routes itself; they are the platform's, emitted canonically once, first."""
    base = _CUSTOMER_BASE.replace(
        "  routes:\n",
        "  routes:\n"
        "    - matchers: ['alertname=\"Watchdog\"']\n"
        "      receiver: watchdog-heartbeat\n"
        "    - matchers: ['component=\"sentinel\"']\n"
        "      receiver: sentinel-sinkhole\n", 1)
    base = base.replace("receivers:\n", "receivers:\n  - name: sentinel-sinkhole\n", 1)
    routes = _routes_of(_emit_am(tmp_path, _tree(tmp_path), _base(tmp_path, base)))
    receivers = [r.get("receiver") for r in routes]
    for name in _PLATFORM_RECEIVERS:
        assert receivers.count(name) == 1, receivers
    assert receivers[:4] == list(_PLATFORM_RECEIVERS), receivers


def test_a_gitops_configmap_carries_its_source_so_apply_can_take_over(
        tmp_path, monkeypatch, capsys):
    """--output-configmap writes the source beside the output (the pair
    --apply keeps); --apply on that ConfigMap then renders from the source
    and produces the same output — not a refusal of its own output."""
    conf_d = _tree(tmp_path)
    cm = tmp_path / "cm.yaml"
    proc = _run("--config-dir", str(conf_d), "--output-configmap",
                "--base-config", str(_base(tmp_path)), "-o", str(cm))
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = yaml.safe_load(cm.read_text(encoding="utf-8"))["data"]
    assert render.alertmanager_reading(data["alertmanager.base.yml"]) == \
        render.alertmanager_reading(_CUSTOMER_BASE)
    calls = _fake_cluster(monkeypatch, data["alertmanager.yml"], data["alertmanager.base.yml"])
    assert _apply(tmp_path) == 0, capsys.readouterr().err[-3000:]
    applied = _created(calls)
    assert render.alertmanager_reading(applied["alertmanager.yml"]) == \
        render.alertmanager_reading(data["alertmanager.yml"])
