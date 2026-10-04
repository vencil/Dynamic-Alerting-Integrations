"""#2490: the route generator reads routing timing values the way
Alertmanager does (tenant-config.schema.json ``definitions.duration``, Go
``model.ParseDuration``; the value table is tests/shared/am_duration_matrix.json).

Before: ``1h30m`` (Alertmanager reads 90 minutes) was "invalid" and became the
platform default 4h; ``1.5h`` (Alertmanager refuses the whole config) was taken
as 90 minutes and handed to Alertmanager verbatim at ``--validate`` rc 0.

Now: a value Alertmanager reads is used (and clamped by the guardrails as
before); a value it refuses is rendered as the platform default with a
``ReplacedValueWarning`` line, which ``--validate`` and validate-config refuse
(rc 1) — and which explain-route does not list as a skipped sub-route.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"

_RECEIVER = ("      receiver:\n        type: webhook\n"
             "        url: https://hooks.example.com/alert\n")


def _tree(tmp_path: Path, routing_extra: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n" + _RECEIVER + routing_extra,
        encoding="utf-8")
    return d


def _generate(d: Path):
    gar = importlib.import_module("generate_alertmanager_routes")
    routing, _dedup, schema_warnings, enforced, _mc = gar.load_tenant_configs(str(d))
    routes, _receivers, warnings = gar.generate_routes(
        routing, enforced_routing=enforced)
    return gar, routes, schema_warnings + warnings


def _cli(tool: str, d: Path, tmp_path: Path, *flags: str):
    # No amtool on PATH: the verdict is the Python checks'.
    env = {**os.environ, "PATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(OPS_DIR / tool), "--config-dir", str(d), *flags],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)


def _tenant_route(routes: list[dict]) -> dict:
    [route] = [r for r in routes if r.get("receiver", "").startswith("tenant-t1")]
    return route


# ── values Alertmanager reads are used ────────────────────────────────────

@pytest.mark.parametrize("field,value,rendered", [
    ("repeat_interval", "1h30m", "1h30m"),   # was: invalid → 4h
    ("repeat_interval", "90m", "90m"),
    ("repeat_interval", "1d", "1d"),
    ("repeat_interval", "4d", "72h"),        # clamped, written as h
    ("group_wait", "1m30s", "1m30s"),
    ("group_wait", "500ms", "5s"),           # read, then clamped up
])
def test_alertmanager_durations_render(tmp_path, field, value, rendered):
    gar, routes, warnings = _generate(_tree(tmp_path, f"      {field}: \"{value}\"\n"))
    assert _tenant_route(routes)[field] == rendered, warnings
    assert gar.blocking_generation_errors(warnings) == []


# ── values Alertmanager refuses block ─────────────────────────────────────

_REFUSED = ["1.5h", "30m1h", "1h1h", "1ns", "1us", "-1h", "banana", "293y"]


@pytest.mark.parametrize("value", _REFUSED)
def test_refused_duration_renders_default_and_blocks(tmp_path, value):
    gar, routes, warnings = _generate(
        _tree(tmp_path, f"      repeat_interval: \"{value}\"\n"))
    assert _tenant_route(routes)["repeat_interval"] == "4h"
    blocking = gar.blocking_generation_errors(warnings)
    assert len(blocking) == 1, warnings
    line = blocking[0]
    assert f"invalid repeat_interval '{value}'" in line
    assert "platform default 4h" in line and "refuse" in line


@pytest.mark.parametrize("value", ["1.5h", "30m1h", "1ns"])
def test_refused_duration_fails_both_clis(tmp_path, value):
    d = _tree(tmp_path, f"      repeat_interval: \"{value}\"\n")
    gen = _cli("generate_alertmanager_routes.py", d, tmp_path, "--validate")
    vc = _cli("validate_config.py", d, tmp_path)
    assert gen.returncode == 1, gen.stdout + gen.stderr
    assert f"invalid repeat_interval '{value}'" in gen.stderr
    assert vc.returncode == 1, vc.stdout + vc.stderr
    # Render mode still renders (the default in its place), exit 0.
    out = tmp_path / "out.yaml"
    render = _cli("generate_alertmanager_routes.py", d, tmp_path, "-o", str(out))
    assert render.returncode == 0, render.stdout + render.stderr
    assert "repeat_interval: 4h" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("routing_extra,where", [
    ("      overrides:\n        - alertname: A\n          group_wait: \"1.5h\"\n"
     "          receiver:\n            type: webhook\n"
     "            url: https://hooks.example.com/b\n", "t1-override-0"),
    ("      routes:\n        - match: {severity: critical}\n"
     "          repeat_interval: \"30m1h\"\n"
     "          receiver:\n            type: webhook\n"
     "            url: https://hooks.example.com/c\n", "t1-route-0"),
], ids=["override", "routes"])
def test_refused_duration_in_a_sub_route_blocks(tmp_path, routing_extra, where):
    gar, _routes, warnings = _generate(_tree(tmp_path, routing_extra))
    blocking = gar.blocking_generation_errors(warnings)
    assert len(blocking) == 1 and where in blocking[0], warnings


def test_refused_duration_is_not_a_skipped_sub_route_in_explain(tmp_path):
    """The override IS rendered (with the default), so explain-route must not
    list it under "Not in effect" — why the line is not a SkippedEntryWarning."""
    explain_route = importlib.import_module("explain_route")
    gar = importlib.import_module("generate_alertmanager_routes")
    d = _tree(tmp_path,
              "      overrides:\n        - alertname: A\n"
              "          repeat_interval: \"1.5h\"\n"
              "          receiver:\n            type: webhook\n"
              "            url: https://hooks.example.com/b\n")
    exp = explain_route.explain_tenant_routing(gar._parse_config_files(str(d)), "t1")
    assert [s["source"] for s in exp["sub_routes"]] == ["overrides[0]"]
    assert exp["skipped_sub_routes"] == []


def test_refused_duration_in_routing_defaults_blocks(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(
        "_routing_defaults:\n  receiver:\n    type: webhook\n"
        "    url: https://hooks.example.com/d\n  group_interval: \"1.5m\"\n",
        encoding="utf-8")
    (d / "t1.yaml").write_text("tenants:\n  t1: {}\n", encoding="utf-8")
    gar, routes, warnings = _generate(d)
    assert _tenant_route(routes)["group_interval"] == "5m"
    assert len(gar.blocking_generation_errors(warnings)) == 1, warnings


# ── strict domain policy judges the value Alertmanager is given ───────────

def _policy(constraints: dict) -> dict:
    return {"pol": {"tenants": ["tenant-x"], "constraints": constraints}}


def test_strict_policy_refuses_a_tenant_value_alertmanager_refuses():
    from _grar_validate import check_domain_policies
    # 1.5h was read as 90 minutes and passed a 2h ceiling; Alertmanager
    # refuses it, so there is nothing to compare.
    msgs = check_domain_policies({"tenant-x": {"repeat_interval": "1.5h"}},
                                 _policy({"max_repeat_interval": "2h"}), strict=True)
    assert any("'1.5h'" in m and "not a valid duration" in m for m in msgs), msgs


def test_strict_policy_reads_alertmanager_units():
    from _grar_validate import check_domain_policies
    msgs = check_domain_policies({"tenant-x": {"repeat_interval": "1d"}},
                                 _policy({"max_repeat_interval": "12h"}), strict=True)
    assert any("exceeds max" in m for m in msgs), msgs


# ── explain-route says which values were replaced ─────────────────────────

def _explain_tree(tmp_path: Path) -> Path:
    return _tree(tmp_path,
                 "      group_wait: \"1.5m\"\n"
                 "      overrides:\n        - alertname: A\n"
                 "          repeat_interval: \"30m1h\"\n"
                 "          receiver:\n            type: webhook\n"
                 "            url: https://hooks.example.com/b\n"
                 "        - alertname: B\n")


def test_explain_lists_replaced_values_apart_from_skipped(tmp_path):
    """The main route's group_wait 1.5m renders as 30s (and --validate
    fails); explain-route used to show only the merged 1.5m."""
    explain_route = importlib.import_module("explain_route")
    gar = importlib.import_module("generate_alertmanager_routes")
    exp = explain_route.explain_tenant_routing(
        gar._parse_config_files(str(_explain_tree(tmp_path))), "t1")
    replaced = exp["replaced_values"]
    assert len(replaced) == 2, replaced
    assert any("invalid group_wait '1.5m'" in w and "30s" in w for w in replaced)
    assert any("t1-override-0" in w and "'30m1h'" in w for w in replaced)
    # Kept apart: the skipped list still holds only the dropped override.
    assert len(exp["skipped_sub_routes"]) == 1
    assert "override[1] missing 'receiver'" in exp["skipped_sub_routes"][0]
    text = explain_route.format_explanation(exp)
    assert "Replaced by the generator" in text
    block = text.split("Replaced by the generator")[1]
    assert "invalid group_wait '1.5m'" in block
    zh = explain_route.format_explanation(exp, lang="zh")
    assert "產生器以平台預設值頂替" in zh


def test_explain_json_carries_replaced_values(tmp_path):
    import json
    d = _explain_tree(tmp_path)
    r = subprocess.run(
        [sys.executable, str(OPS_DIR / "explain_route.py"), "--config-dir", str(d),
         "--tenant", "t1", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    [doc] = json.loads(r.stdout)
    assert len(doc["replaced_values"]) == 2, doc
    assert len(doc["skipped_sub_routes"]) == 1


def test_explain_clean_tenant_has_empty_replaced_values(tmp_path):
    explain_route = importlib.import_module("explain_route")
    gar = importlib.import_module("generate_alertmanager_routes")
    d = _tree(tmp_path, "      group_wait: \"30s\"\n")
    exp = explain_route.explain_tenant_routing(gar._parse_config_files(str(d)), "t1")
    assert exp["replaced_values"] == []
    assert "Replaced by the generator" not in explain_route.format_explanation(exp)
