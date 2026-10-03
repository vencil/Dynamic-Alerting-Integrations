"""#2519：`_routing_enforced` 的 receiver 含 `{{tenant}}` 時，per-tenant enforced route
對產生器認得的**每一個**租戶展開，不只帶路由設定的那些。

「認得的租戶」是 `load_tenant_configs` 的 `dedup_configs` 鍵——inhibit rule 用的同一個集合：
只有閾值、只有 `_silent_mode`、`_routing: disable` 的租戶都在裡面；id 不合法而被丟棄的
（`skipped_entry_warning`，產生器 CLI 會整棵樹拒收）不在。Silent mode 不由產生器處理：
它是 base config 的 inhibit rule（`tenant=~".+"`、`equal: [tenant]`），與 route 無關，
所以新加入的租戶不需要、也不得有額外處理——這裡釘的是「enforced route 存在與否」。

單一平台 route（receiver 不含 `{{tenant}}`）不受影響。所有讀這個展開的讀者——產生器
CLI、validate-config 的 routes 列、explain-route 的 trace 樹與 receiver 型別表、`--strict`
的 enforced group_by 判定——用同一個集合。租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
import explain_route as er  # noqa: E402
import validate_config as vc  # noqa: E402
from _grar_parse import _parse_config_files, load_tenant_tree  # noqa: E402
from _grar_routes import _build_enforced_routes, generate_routes  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

_PER_TENANT = ("_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
               "    url: \"https://noc.example.com/{{tenant}}\"\n")
_SINGLE = ("_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
           "    url: \"https://noc.example.com/hook\"\n")

# tenant id → its file body below `tenants.<id>:`, and whether a tenant
# route (`tenant-<id>`) is rendered for it.
_TENANTS = {
    "t-thr": ("    cpu_usage_percent: '85'\n", False),          # threshold only
    "t-silent": ("    _silent_mode: warning\n", False),         # _silent_mode only
    "t-routed": ("    _routing:\n      receiver: {type: webhook, "
                 "url: \"https://r.example.com/hook\"}\n"
                 "    _silent_mode: warning\n", True),          # own _routing
    "t-off": ("    _routing: disable\n", False),               # routing opted out
}
# `_routing_defaults` of its own subtree (#2326) supplies this one's routing.
_DEFAULTED = "t-dflt"
_EVERY = sorted([*_TENANTS, _DEFAULTED])


def _tree(tmp_path: Path, enforced: str, invalid: bool = False) -> Path:
    d = tmp_path / "conf.d"
    (d / "team").mkdir(parents=True)
    (d / "_defaults.yaml").write_text(
        "defaults:\n  cpu_usage_percent: 80\n" + enforced, encoding="utf-8")
    for tid, (body, _routed) in _TENANTS.items():
        (d / f"{tid}.yaml").write_text(f"tenants:\n  {tid}:\n{body}", encoding="utf-8")
    (d / "team" / "_defaults.yaml").write_text(
        "_routing_defaults:\n  receiver:\n    type: webhook\n"
        "    url: \"https://team.example.com/{{tenant}}\"\n", encoding="utf-8")
    (d / "team" / f"{_DEFAULTED}.yaml").write_text(
        f"tenants:\n  {_DEFAULTED}:\n    cpu_usage_percent: '85'\n", encoding="utf-8")
    if invalid:
        (d / "bad.yaml").write_text(
            "tenants:\n  Bad_Id:\n    cpu_usage_percent: '85'\n", encoding="utf-8")
    return d


def _enforced_names(receivers: list[dict]) -> list[str]:
    return [r["name"] for r in receivers if r["name"].startswith("platform-enforced")]


def _render(d: Path) -> dict:
    out = d.parent / "routes.yaml"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), "-o", str(out)],
                       capture_output=True, text=True, encoding="utf-8",
                       check=False, timeout=120)
    assert p.returncode == 0, p.stderr
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def test_every_recognised_tenant_gets_its_enforced_route(tmp_path):
    frag = _render(_tree(tmp_path, _PER_TENANT))
    names = _enforced_names(frag["receivers"])
    assert names == [f"platform-enforced-{t}" for t in _EVERY]
    by_receiver = {r["receiver"]: r for r in frag["route"]["routes"]}
    for tid in _EVERY:
        route = by_receiver[f"platform-enforced-{tid}"]
        assert route["matchers"] == [f'tenant="{tid}"']
        assert route["continue"] is True
    recv = {r["name"]: r for r in frag["receivers"]}
    assert (recv["platform-enforced-t-thr"]["webhook_configs"][0]["url"]
            == "https://noc.example.com/t-thr")
    # Only the enforced set grew: tenant routes stay those with a routing.
    tenant_routes = sorted(n for n in recv if n.startswith("tenant-"))
    assert tenant_routes == sorted(
        [f"tenant-{t}" for t, (_b, routed) in _TENANTS.items() if routed]
        + [f"tenant-{_DEFAULTED}"])


def test_the_enforced_set_is_the_inhibit_set(tmp_path):
    """The same tenants as the severity-dedup inhibit rules (dedup_configs)."""
    frag = _render(_tree(tmp_path, _PER_TENANT))
    inhibited = sorted(
        m[len('tenant="'):-1] for r in frag["inhibit_rules"]
        for m in r["source_matchers"] if m.startswith('tenant="'))
    assert inhibited == _EVERY
    assert _enforced_names(frag["receivers"]) == [f"platform-enforced-{t}" for t in inhibited]


def test_a_dropped_invalid_id_gets_none(tmp_path):
    """`Bad_Id` is dropped by the reader (the CLI refuses the tree on it);
    the library path validate-config uses renders the others, and not it."""
    tree = load_tenant_tree(str(_tree(tmp_path, _PER_TENANT, invalid=True)))
    assert tree.invalid_tenant_ids == ["Bad_Id"]
    _routes, receivers, _w = generate_routes(
        tree.routing_configs, enforced_routing=tree.enforced_routing,
        tenants=tree.dedup_configs)
    assert _enforced_names(receivers) == [f"platform-enforced-{t}" for t in _EVERY]


def test_single_platform_route_is_unchanged(tmp_path):
    frag = _render(_tree(tmp_path, _SINGLE))
    assert _enforced_names(frag["receivers"]) == ["platform-enforced"]
    enforced = [r for r in frag["route"]["routes"] if r["receiver"] == "platform-enforced"]
    assert len(enforced) == 1 and "matchers" not in enforced[0]


def test_tenants_is_required_and_keyword_only(tmp_path):
    """Fail-closed: no default falls back to the routed set (how #2519
    shipped) — leaving `tenants` out, or passing it by position, is a
    TypeError on both entry points."""
    tree = load_tenant_tree(str(_tree(tmp_path, _PER_TENANT)))
    with pytest.raises(TypeError, match="tenants"):
        generate_routes(tree.routing_configs, enforced_routing=tree.enforced_routing)
    with pytest.raises(TypeError):
        generate_routes(tree.routing_configs, None, tree.enforced_routing,
                        tree.dedup_configs)
    with pytest.raises(TypeError, match="tenants"):
        _build_enforced_routes(tree.enforced_routing, tree.routing_configs)
    with pytest.raises(TypeError):
        _build_enforced_routes(tree.enforced_routing, tree.routing_configs, None,
                               tree.dedup_configs)


def test_validate_config_routes_row_counts_them(tmp_path):
    row = vc.check_routes(str(_tree(tmp_path, _PER_TENANT)))
    assert row["status"] in (vc.PASS, vc.WARN), row
    # One enforced route + receiver per tenant, one tenant route + receiver
    # per routed tenant (t-routed, t-dflt); one dedup inhibit rule each.
    n = len(_EVERY) + 2
    assert row["details"][0] == f"{n} routes, {n} receivers, {len(_EVERY)} inhibit_rules", row


def test_explain_route_trace_tree_and_types_follow(tmp_path):
    parsed = _parse_config_files(str(_tree(tmp_path, _PER_TENANT)))
    _am, _root, by_name, types = er.build_trace_tree(parsed)
    for tid in _EVERY:
        assert f"platform-enforced-{tid}" in by_name
        assert types[f"platform-enforced-{tid}"] == "webhook"


@pytest.mark.parametrize("tid", ["t-thr", "t-silent"])
def test_strict_judges_enforced_group_by_for_unrouted_tenants(tmp_path, tid):
    """`--strict`'s enforced group_by verdict runs over the same set: a
    `{{tenant}}` group_by entry repeating a listed label is judged for a
    tenant with no routing too (the tenant id is the repeated label)."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "defaults:\n  cpu_usage_percent: 80\n" + _PER_TENANT
        + f"  group_by: [\"{tid}\", \"{{{{tenant}}}}\"]\n", encoding="utf-8")
    (d / f"{tid}.yaml").write_text(
        f"tenants:\n  {tid}:\n{_TENANTS[tid][0]}", encoding="utf-8")
    tree = load_tenant_tree(str(d), strict_policies=True)
    assert any(f"_routing_enforced ({tid}): group_by[1]" in w
               for w in tree.schema_warnings), tree.schema_warnings
