"""#2519 F1：per-tenant enforced route 不得把「沒有 tenant route 的租戶」的告警從 root 拿走。

Alertmanager `dispatch/route.go` 的 `Route.Match`：子 route 一個都沒命中才加入 root；
`continue: true` 的子 route 命中也算命中。所以只有閾值／只有 `_silent_mode`／
`_routing: disable`／`_routing` 被拒收的租戶，在 #2519 之前落到 root receiver，
展開 enforced route 之後若不補尾端 route，就只剩 `platform-enforced-<id>`
（amtool v0.28.1 實測 `default` → `platform-enforced-t-thr`）。

釘的不變量：**加上 `{{tenant}}` `_routing_enforced` 只多出 `platform-enforced-<id>`**——
同一棵樹拿掉 enforced 區塊時的投遞，前面加上該租戶的 enforced receiver，就是加上
之後的投遞。內建 base 與 `--base-config`（BYO catch-all root）各量一次；`--apply`
的合併路徑用叢集 config 的 root receiver。

投遞由下面的 `_deliver` 依 `Route.Match` 的語意計算（只支援 `=` matcher，其餘
raise——fail-closed，不猜）；PATH 上有 `amtool` 時同一組期望值再交給
`amtool config routes test --verify.receivers` 對一次，確認模擬與 Alertmanager 一致。
CI 的 docker amtool 版本在 test_alertmanager_routing_correctness.py。
租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
import validate_config as vc  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_render import (  # noqa: E402
    _merge_routes_receivers_inhibits, assemble_configmap, load_base_config)
from _grar_routes import (  # noqa: E402
    generate_inhibit_rules, generate_routes, is_root_fallthrough_route)

_PER_TENANT = ("_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
               "    url: \"https://noc.example.com/{{tenant}}\"\n")
_SINGLE = ("_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
           "    url: \"https://noc.example.com/hook\"\n")
_BYO_BASE = ("route:\n  receiver: byo-root\n  group_by: [alertname, tenant]\n"
             "receivers:\n  - name: byo-root\n    webhook_configs:\n"
             "      - url: https://byo.example.com/all\n")

# tenant id → (file body below `tenants.<id>:`, owns a tenant route)
_TENANTS = {
    "t-thr": ("    cpu_usage_percent: '85'\n", False),          # threshold only
    "t-silent": ("    _silent_mode: warning\n", False),         # _silent_mode only
    "t-off": ("    _routing: disable\n", False),               # routing opted out
    "t-refused": ("    _routing: [1, 2]\n", False),            # _routing refused
    # main receiver skipped (unsupported type): no tenant route is rendered
    "t-skipped": ("    _routing:\n      receiver: {type: carrier-pigeon}\n", False),
    "t-routed": ("    _routing:\n      receiver: {type: webhook, "
                 "url: \"https://r.example.com/hook\"}\n", True),
}
_UNROUTED = sorted(t for t, (_b, routed) in _TENANTS.items() if not routed)


def _tree(tmp_path: Path, enforced: str, name: str = "conf.d",
          exclude: tuple[str, ...] = ()) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "defaults:\n  cpu_usage_percent: 80\n" + enforced, encoding="utf-8")
    for tid, (body, _routed) in _TENANTS.items():
        if tid in exclude:
            continue
        (d / f"{tid}.yaml").write_text(f"tenants:\n  {tid}:\n{body}", encoding="utf-8")
    return d


def _generate(d: Path) -> tuple[list[dict], list[dict], list[str], list[dict]]:
    tree = load_tenant_tree(str(d))
    routes, receivers, warnings = generate_routes(
        tree.routing_configs, enforced_routing=tree.enforced_routing,
        tenants=tree.dedup_configs)
    inhibit, _w = generate_inhibit_rules(tree.dedup_configs)
    return routes, receivers, warnings, inhibit


def _assembled(d: Path, base: dict) -> dict:
    routes, receivers, _w, inhibit = _generate(d)
    cm = assemble_configmap(base, routes, receivers, inhibit)
    return yaml.safe_load(yaml.safe_load(cm)["data"]["alertmanager.yml"])


_EQ = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"$')


def _matches(route: dict, labels: dict[str, str]) -> bool:
    for m in route.get("matchers") or []:
        hit = _EQ.match(m)
        if hit is None:
            raise AssertionError(f"_deliver models `=` matchers only, got {m!r}")
        if labels.get(hit[1], "") != hit[2]:
            return False
    return True


def _deliver(route: dict, labels: dict[str, str], receiver: str | None = None) -> list[str]:
    """``Route.Match`` (alertmanager dispatch/route.go): the receivers of the
    matching leaves, in tree order. A route that matched no child is itself the
    leaf; a matched child without ``continue`` stops the scan. ``receiver`` is
    inherited from the parent when a route leaves it out."""
    receiver = route.get("receiver") or receiver
    out: list[str] = []
    for child in route.get("routes") or []:
        if not _matches(child, labels):
            continue
        out.extend(_deliver(child, labels, receiver))
        if not child.get("continue"):
            break
    return out or [receiver]


def _labels(tid: str) -> dict[str, str]:
    return {"alertname": "DemoAlert", "severity": "warning", "tenant": tid}


def _bases(tmp_path: Path) -> dict[str, dict]:
    byo = tmp_path / "byo.yaml"
    byo.write_text(_BYO_BASE, encoding="utf-8")
    return {"builtin": load_base_config(None), "byo": load_base_config(str(byo))}


@pytest.mark.parametrize("base_name", ["builtin", "byo"])
def test_enforced_only_adds_its_receiver(tmp_path, base_name):
    base = _bases(tmp_path)[base_name]
    without = _assembled(_tree(tmp_path, "", "plain"), base)
    with_enf = _assembled(_tree(tmp_path, _PER_TENANT), base)
    root = base["route"]["receiver"]
    for tid in _TENANTS:
        before = _deliver(without["route"], _labels(tid))
        after = _deliver(with_enf["route"], _labels(tid))
        assert after == [f"platform-enforced-{tid}"] + before, (tid, before, after)
    # The premise: the unrouted ones fell to the root before.
    for tid in _UNROUTED:
        assert _deliver(without["route"], _labels(tid)) == [root]
    # An alert with no tenant label is untouched.
    assert _deliver(with_enf["route"], {"alertname": "X"}) == [root]


def test_single_platform_route_gets_no_fallthrough(tmp_path):
    """The matcher-less enforced route took every alert off the root before
    #2519 too; adding fallthroughs would change that, so none is emitted."""
    routes, _r, _w, _i = _generate(_tree(tmp_path, _SINGLE))
    assert not [r for r in routes if is_root_fallthrough_route(r)]


def test_fallthrough_routes_shape_and_place(tmp_path):
    routes, receivers, warnings, _i = _generate(_tree(tmp_path, _PER_TENANT))
    tails = [r for r in routes if is_root_fallthrough_route(r)]
    assert tails == [{"matchers": [f'tenant="{t}"']} for t in _UNROUTED]
    # Last, after every enforced and tenant route.
    assert routes[-len(tails):] == tails
    # They add no receiver, and so no receiver-name collision either.
    assert len(receivers) == len(_TENANTS) + 1
    assert not [w for w in warnings if "duplicate receiver" in w.lower()], warnings


@pytest.mark.parametrize("base_name", ["builtin", "byo"])
def test_assembly_pins_the_bases_root_receiver(tmp_path, base_name):
    base = _bases(tmp_path)[base_name]
    am = _assembled(_tree(tmp_path, _PER_TENANT), base)
    root = base["route"]["receiver"]
    tails = [r for r in am["route"]["routes"]
             if r.get("receiver") == root and "continue" not in r]
    assert tails == [{"matchers": [f'tenant="{t}"'], "receiver": root}
                     for t in _UNROUTED]


def test_apply_merge_pins_the_cluster_root(tmp_path):
    routes, receivers, _w, inhibit = _generate(_tree(tmp_path, _PER_TENANT))
    existing = {"route": {"receiver": "cluster-root"},
                "receivers": [{"name": "cluster-root"}], "inhibit_rules": []}
    merged = _merge_routes_receivers_inhibits(existing, routes, receivers, inhibit)
    for tid in _TENANTS:
        want = [f"platform-enforced-{tid}"] + (
            [f"tenant-{tid}"] if _TENANTS[tid][1] else ["cluster-root"])
        assert _deliver(merged["route"], _labels(tid)) == want, tid
    assert [r for r in merged["route"]["routes"] if r.get("receiver") == "cluster-root"]


def test_validate_config_routes_row_counts_the_fallthroughs(tmp_path):
    # t-skipped is a skipped entry, which fails the row before it counts.
    row = vc.check_routes(str(_tree(tmp_path, _PER_TENANT, exclude=("t-skipped",))))
    n_recv = len(_TENANTS) - 1 + 1
    n_routes = n_recv + len(_UNROUTED) - 1
    assert row["details"][0].startswith(
        f"{n_routes} routes, {n_recv} receivers, "), row


_AMTOOL = shutil.which("amtool")


@pytest.mark.skipif(_AMTOOL is None, reason="amtool not on PATH — the _deliver "
                    "model above is the always-on half; CI's docker amtool runs "
                    "test_alertmanager_routing_correctness.py")
@pytest.mark.parametrize("base_name", ["builtin", "byo"])
def test_amtool_agrees_with_the_model(tmp_path, base_name):
    base = _bases(tmp_path)[base_name]
    am = _assembled(_tree(tmp_path, _PER_TENANT), base)
    cfg = tmp_path / "alertmanager.yml"
    cfg.write_text(yaml.safe_dump(am, sort_keys=False), encoding="utf-8")
    for tid in _TENANTS:
        want = ",".join(_deliver(am["route"], _labels(tid)))
        p = subprocess.run(
            [_AMTOOL, "config", "routes", "test", f"--config.file={cfg}",
             f"--verify.receivers={want}"]
            + [f"{k}={v}" for k, v in sorted(_labels(tid).items())],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
        assert p.returncode == 0, (tid, want, p.stdout, p.stderr)


def test_skipped_main_receiver_gets_root_back(tmp_path):
    """A tenant whose `_routing` main receiver the generator skips (an
    unsupported type here; a domain policy refusal is the same branch) renders
    no tenant route, so it is an unrouted tenant: the base delivery (the root)
    plus its enforced route."""
    base = load_base_config(None)
    without = _assembled(_tree(tmp_path, "", "plain"), base)
    with_enf = _assembled(_tree(tmp_path, _PER_TENANT), base)
    assert _deliver(without["route"], _labels("t-skipped")) == ["default"]
    assert _deliver(with_enf["route"], _labels("t-skipped")) == [
        "platform-enforced-t-skipped", "default"]
    routes, _r, warnings, _i = _generate(_tree(tmp_path, _PER_TENANT, "again"))
    assert any("t-skipped" in w for w in warnings), warnings
