"""#4277：migrate-rule 寫出的 migrated 告警（`da_migrated="true"`，#4355）在 Alertmanager 的隔離。

影子期新舊規則並存，migrated 告警不能通知任何人；切換（`_routing_migrated:
active`）之後，它與該租戶的其他告警走同一條路。切換只動 conf.d 與 Alertmanager，
規則本身不改，告警的 label set 前後相同。

實際路由（amtool 當判準）見 test_alertmanager_routing_correctness.py 的
TestEnforcedRoutingGuardrails；本檔釘住產生器的輸出與 conf.d 的讀法。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

#: 產生器以 judge 身分讀樹（#3206）；同 test_generate_routes_custom_firehose.py。
pytestmark = pytest.mark.usefixtures("judging_run")

import _grar_render as render  # noqa: E402
import _grar_routes as routes_mod  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

#: #4355: the one spelling, `_grar_routes.MIGRATED_LABEL` / `MIGRATED_VALUE`.
MIG = f'{routes_mod.MIGRATED_LABEL}="{routes_mod.MIGRATED_VALUE}"'

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

_ROUTING = textwrap.dedent("""\
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/{t}"
""")


def _tree(tmp_path: Path, states: dict[str, object], *, root_states: dict | None = None) -> Path:
    """conf.d with one routed tenant per key of *states*; a value that is not
    the sentinel ``...`` is written as its `_routing_migrated`. *root_states*
    go in `_defaults.yaml`'s `tenants:` (the platform_tenants layer)."""
    conf = tmp_path / "conf.d"
    conf.mkdir()
    defaults = "defaults: {}\n"
    if root_states:
        defaults += "tenants:\n" + "".join(
            f"  {t}:\n    _routing_migrated: {v}\n" for t, v in root_states.items())
    (conf / "_defaults.yaml").write_text(defaults, encoding="utf-8")
    for t, state in states.items():
        body = textwrap.indent(_ROUTING.format(t=t), "    ")
        if state is not ...:
            body += f"    _routing_migrated: {state}\n"
        (conf / f"{t}.yaml").write_text(f"tenants:\n  {t}:\n{body}", encoding="utf-8")
    return conf


def _render(conf: Path) -> tuple[subprocess.CompletedProcess, dict | None]:
    out = conf.parent / "alertmanager.yml"
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(conf),
         "--output-alertmanager", "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    am = yaml.safe_load(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc, am


def _migrated_routes(am: dict) -> list[tuple[int, dict]]:
    return [(i, r) for i, r in enumerate(am["route"]["routes"])
            if MIG in (r.get("matchers") or [])]


def test_every_tenant_is_in_shadow_by_default(tmp_path):
    proc, am = _render(_tree(tmp_path, {"t-a": ..., "t-b": ...}))
    assert proc.returncode == 0, proc.stderr
    (idx, route), = _migrated_routes(am)
    assert route["matchers"] == [MIG]
    assert route["receiver"] == "migrated-alerts-shadow" and route["continue"] is False
    assert idx == 4, [r.get("matchers") for r in am["route"]["routes"]]
    (recv,) = [r for r in am["receivers"] if r["name"] == "migrated-alerts-shadow"]
    assert render.receiver_integration_kinds(recv) == []  # notifies no one


def test_an_active_tenant_is_left_to_its_own_routing(tmp_path):
    proc, am = _render(_tree(tmp_path, {"t-a": "active", "t-b": "shadow", "t-c": ...}))
    assert proc.returncode == 0, proc.stderr
    (_, route), = _migrated_routes(am)
    assert route["matchers"] == [MIG, 'tenant!~"t-a"']


def test_the_platform_tenants_layer_counts_too(tmp_path):
    """A root `_` file's `tenants:` value is the tenant's (reserved_keys.json:
    read_by generator tenant, platform_tenants)."""
    proc, am = _render(_tree(tmp_path, {"t-a": ..., "t-b": ...}, root_states={"t-b": "active"}))
    assert proc.returncode == 0, proc.stderr
    (_, route), = _migrated_routes(am)
    assert route["matchers"] == [MIG, 'tenant!~"t-b"']


@pytest.mark.parametrize("value", ["yes", "Active", "true", "on", "{state: active}", "1"])
def test_a_value_that_is_no_state_refuses_the_tree(tmp_path, value):
    """Guessing either state would page someone, or no one, wrongly."""
    proc, am = _render(_tree(tmp_path, {"t-a": value}))
    assert proc.returncode == 1, proc.stderr
    assert "_routing_migrated must be one of shadow, active" in proc.stderr, proc.stderr
    assert am is None


def test_null_is_shadow(tmp_path):
    proc, am = _render(_tree(tmp_path, {"t-a": "null"}))
    assert proc.returncode == 0, proc.stderr
    (_, route), = _migrated_routes(am)
    assert route["matchers"] == [MIG]


def test_the_route_is_pinned_ahead_of_the_enforced_routes(tmp_path):
    """A matcher-less enforced route (continue: true) would otherwise take
    every shadow alert to the NOC."""
    conf = _tree(tmp_path, {"t-a": ...})
    (conf / "_defaults.yaml").write_text(textwrap.dedent("""\
        defaults: {}
        _routing_enforced:
          enabled: true
          receiver:
            type: webhook
            url: "https://noc.example.com/alerts"
    """), encoding="utf-8")
    proc, am = _render(conf)
    assert proc.returncode == 0, proc.stderr
    names = [r.get("receiver") for r in am["route"]["routes"]]
    assert names.index("migrated-alerts-shadow") < names.index("platform-enforced"), names


def test_a_merge_given_no_set_puts_every_tenant_in_shadow():
    """A caller with no tenant tree: the safe default."""
    out_routes, out_receivers = render._inject_custom_alert_isolation([], [])
    assert out_routes[4]["matchers"] == [MIG]
    assert "migrated-alerts-shadow" in {r["name"] for r in out_receivers}


def test_a_stale_migrated_route_is_replaced_by_this_runs():
    """Re-merging an output (or a base that held one) keeps one: this run's."""
    stale = routes_mod._build_migrated_alert_routes(["old"])[0]
    out_routes, _ = render._inject_custom_alert_isolation(stale, [], migrated_active=["new"])
    found = [r for r in out_routes if MIG in r["matchers"]]
    assert found == routes_mod._build_migrated_alert_routes(["new"])[0]


def test_the_route_count_and_empty_check_count_tenant_routes_only(tmp_path):
    """The platform route is the merge's: `Validation: N route(s)` is not one
    more for it, as for the other four."""
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(_tree(tmp_path, {"t-a": "active"})),
         "--validate"], capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert "Validation: 1 route(s)" in proc.stdout, proc.stdout


def test_a_tenant_id_is_escaped_for_the_regex():
    (route,), _ = routes_mod._build_migrated_alert_routes(["a.b", "c-d"])
    assert route["matchers"][1] == 'tenant!~"a\\\\.b|c-d"'


def test_a_tenant_with_routing_disabled_still_cuts_over(tmp_path):
    """`_routing: disable` routes the tenant's alerts to the root: after
    cutover its migrated alerts must go there too, not stay swallowed."""
    conf = _tree(tmp_path, {"t-a": ...})
    (conf / "t-off.yaml").write_text(
        "tenants:\n  t-off:\n    _routing: disable\n    _routing_migrated: active\n",
        encoding="utf-8")
    proc, am = _render(conf)
    assert proc.returncode == 0, proc.stderr
    (_, route), = _migrated_routes(am)
    assert route["matchers"] == [MIG, 'tenant!~"t-off"']


def test_a_notifier_on_the_shadow_receiver_is_refused(tmp_path):
    """A base must not give migrated-alerts-shadow an integration: each event
    would page twice in the shadow period (review: the base's fuller
    placeholder definition used to win silently)."""
    conf = _tree(tmp_path, {"t-a": ...})
    base = tmp_path / "base.yml"
    base.write_text(textwrap.dedent("""\
        route:
          receiver: default
        receivers:
          - name: default
          - name: migrated-alerts-shadow
            webhook_configs:
              - url: https://pager.example.com/x
    """), encoding="utf-8")
    out = tmp_path / "am.yml"
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(conf), "--output-alertmanager",
         "--base-config", str(base), "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 1, proc.stderr
    assert "migrated-alerts-shadow" in proc.stderr and "Leave it name-only" in proc.stderr, proc.stderr
    assert not out.exists()


def test_a_name_only_shadow_receiver_in_the_base_is_kept(tmp_path):
    conf = _tree(tmp_path, {"t-a": ...})
    base = tmp_path / "base.yml"
    base.write_text("route:\n  receiver: default\nreceivers:\n  - name: default\n"
                    "  - name: migrated-alerts-shadow\n", encoding="utf-8")
    out = tmp_path / "am.yml"
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(conf), "--output-alertmanager",
         "--base-config", str(base), "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 0, proc.stderr
    am = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [r["name"] for r in am["receivers"]].count("migrated-alerts-shadow") == 1


def test_the_validate_assembly_check_sees_the_cut_over_tenants(tmp_path, monkeypatch):
    """--validate hands the generated config to Alertmanager's loader: the
    tree it judges is the output's, the tenant!~ matcher included (review)."""
    seen = []
    real = gar.evaluate_generated_config

    def spy(*a, **kw):
        seen.append(set(kw.get("migrated_active") or ()))
        return real(*a, **kw)

    monkeypatch.setattr(gar, "evaluate_generated_config", spy)
    conf = _tree(tmp_path, {"t-a": "active", "t-b": ...})
    with pytest.raises(SystemExit):
        gar.main(["--config-dir", str(conf), "--validate"])
    assert seen == [{"t-a"}]


def test_the_verdict_assembles_with_the_set_it_was_given(monkeypatch):
    seen = []
    real = render.assemble_configmap

    def spy(*a, **kw):
        seen.append(set(kw.get("migrated_active") or ()))
        return real(*a, **kw)

    monkeypatch.setattr(render, "assemble_configmap", spy)
    render.evaluate_generated_config([], [], [], [], migrated_active=["t-a"])
    assert seen == [{"t-a"}]
