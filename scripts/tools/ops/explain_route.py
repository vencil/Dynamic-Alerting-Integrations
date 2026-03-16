#!/usr/bin/env python3
"""explain_route.py — Routing merge pipeline debugger (ADR-007).

Shows the four-layer merge expansion for each tenant's routing config:
  1. _routing_defaults  → global defaults
  2. routing_profiles[ref] → team/domain shared config
  3. tenant _routing → per-tenant overrides
  4. _routing_enforced → NOC immutable override

Usage:
    explain_route.py --config-dir conf.d
    explain_route.py --config-dir conf.d --tenant db-a
    explain_route.py --config-dir conf.d --show-profile-expansion
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

# ---------------------------------------------------------------------------
# Internal imports (same package)
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from generate_alertmanager_routes import (  # noqa: E402
    _parse_config_files,
    merge_routing_with_defaults,
)
from _lib_python import detect_cli_lang  # noqa: E402


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def explain_tenant_routing(
    parsed: dict,
    tenant: str,
) -> dict:
    """Build a layer-by-layer explanation of a tenant's routing merge.

    Returns dict with keys:
        tenant, profile_ref, layers, final
    Each layer is {name, source, config}.
    """
    layers: list[dict] = []

    # Layer 1: routing defaults
    routing_defaults = parsed.get("routing_defaults", {})
    layers.append({
        "name": "Layer 1: _routing_defaults",
        "source": "_defaults.yaml / _routing_defaults key",
        "config": dict(routing_defaults) if routing_defaults else {},
    })

    # Layer 2: routing profile
    profile_refs = parsed.get("tenant_profile_refs", {})
    profiles = parsed.get("routing_profiles", {})
    profile_ref = profile_refs.get(tenant)
    profile_cfg = {}
    if profile_ref and profile_ref in profiles:
        profile_cfg = dict(profiles[profile_ref])
    layers.append({
        "name": "Layer 2: routing_profiles",
        "source": f"_routing_profiles.yaml → {profile_ref or '(none)'}",
        "config": profile_cfg,
    })

    # Build base after layers 1+2
    base = dict(routing_defaults) if routing_defaults else {}
    for k, v in profile_cfg.items():
        base[k] = v

    # Layer 3: tenant explicit _routing
    tenant_routing = parsed.get("explicit_routing", {}).get(tenant, {})
    layers.append({
        "name": "Layer 3: tenant _routing",
        "source": f"{tenant}.yaml → _routing",
        "config": dict(tenant_routing) if tenant_routing else {},
    })

    # Merge layers 1-3
    merged = merge_routing_with_defaults(base, tenant_routing, tenant)

    # Layer 4: _routing_enforced
    enforced = parsed.get("enforced_routing")
    enforced_cfg = {}
    if enforced and isinstance(enforced, dict):
        enforced_cfg = {k: v for k, v in enforced.items() if k != "enabled"}
    layers.append({
        "name": "Layer 4: _routing_enforced",
        "source": "_defaults.yaml → _routing_enforced",
        "config": enforced_cfg,
    })

    # Apply enforced on top
    final = dict(merged)
    for k, v in enforced_cfg.items():
        final[k] = v

    return {
        "tenant": tenant,
        "profile_ref": profile_ref,
        "layers": layers,
        "final": final,
    }


def explain_profile_expansion(parsed: dict) -> dict:
    """Show all routing profiles and which tenants reference them.

    Returns dict {profile_name: {config, referenced_by}}.
    """
    profiles = parsed.get("routing_profiles", {})
    refs = parsed.get("tenant_profile_refs", {})

    result = {}
    for name, cfg in sorted(profiles.items()):
        tenants_using = sorted(t for t, p in refs.items() if p == name)
        result[name] = {
            "config": cfg,
            "referenced_by": tenants_using,
        }

    # Detect orphan refs (pointing to non-existent profiles)
    for tenant, pname in sorted(refs.items()):
        if pname not in profiles:
            if pname not in result:
                result[pname] = {"config": None, "referenced_by": []}
            result[pname].setdefault("referenced_by", []).append(tenant)
            result[pname]["error"] = "profile not found"

    return result


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_yaml(data: dict | list, indent: int = 2) -> str:
    """Format data as YAML string."""
    if not data:
        return "  (empty)"
    return yaml.dump(data, default_flow_style=False, allow_unicode=True,
                     sort_keys=False).rstrip()


def format_explanation(explanation: dict, *, lang: str = "en") -> str:
    """Format a tenant routing explanation as human-readable text."""
    lines: list[str] = []
    t = explanation["tenant"]
    ref = explanation["profile_ref"]

    if lang == "zh":
        lines.append(f"╔══ 租戶: {t} ══╗")
        if ref:
            lines.append(f"  路由設定檔: {ref}")
    else:
        lines.append(f"╔══ Tenant: {t} ══╗")
        if ref:
            lines.append(f"  Routing Profile: {ref}")

    lines.append("")

    for layer in explanation["layers"]:
        lines.append(f"── {layer['name']} ──")
        lines.append(f"   Source: {layer['source']}")
        cfg = layer["config"]
        if cfg:
            for line in _fmt_yaml(cfg).splitlines():
                lines.append(f"   {line}")
        else:
            lines.append("   (empty)")
        lines.append("")

    header = "最終合併結果:" if lang == "zh" else "Final merged result:"
    lines.append(f"── {header} ──")
    for line in _fmt_yaml(explanation["final"]).splitlines():
        lines.append(f"   {line}")
    lines.append("")

    return "\n".join(lines)


def format_profile_expansion(expansion: dict, *, lang: str = "en") -> str:
    """Format profile expansion as human-readable text."""
    lines: list[str] = []
    header = "路由設定檔展開:" if lang == "zh" else "Routing Profile Expansion:"
    lines.append(f"╔══ {header} ══╗")
    lines.append("")

    for name, info in sorted(expansion.items()):
        lines.append(f"── Profile: {name} ──")
        if info.get("error"):
            lines.append(f"   ⚠ {info['error']}")
        if info["config"]:
            for line in _fmt_yaml(info["config"]).splitlines():
                lines.append(f"   {line}")
        else:
            lines.append("   (not defined)")

        refs = info.get("referenced_by", [])
        ref_label = "引用者:" if lang == "zh" else "Referenced by:"
        if refs:
            lines.append(f"   {ref_label} {', '.join(refs)}")
        else:
            orphan = "（無引用 — 孤立設定檔）" if lang == "zh" else "(no references — orphan profile)"
            lines.append(f"   {ref_label} {orphan}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert route tracing (v2.1.0)
# ---------------------------------------------------------------------------

def trace_alert_routing(
    parsed: dict,
    tenant: str,
    alertname: str,
    severity: str = "warning",
    extra_labels: dict | None = None,
) -> dict:
    """Simulate how a specific alert would be routed through the pipeline.

    Given a tenant + alert labels, traces the full decision path:
      1. Which route node matches
      2. Which receiver the alert lands on
      3. Whether any inhibit rules would suppress it
      4. Timing parameters applied

    Returns dict with keys:
        tenant, alertname, severity, steps, final_receiver,
        inhibited, inhibit_reason, timing
    """
    steps: list[dict] = []

    # Step 1: Resolve tenant routing config (4-layer merge)
    explanation = explain_tenant_routing(parsed, tenant)
    final_routing = explanation.get("final", {})

    # Build alert label set
    alert_labels = {
        "alertname": alertname,
        "tenant": tenant,
        "severity": severity,
    }
    if extra_labels:
        alert_labels.update(extra_labels)

    steps.append({
        "step": 1,
        "action": "resolve_routing_config",
        "detail": f"4-layer merge for tenant '{tenant}'",
        "profile_ref": explanation.get("profile_ref"),
        "result": final_routing,
    })

    # Step 2: Determine receiver
    receiver_type = final_routing.get("receiver_type", "webhook")
    receiver_url = final_routing.get("receiver_url", "")
    channel = final_routing.get("channel", "")
    receiver_desc = f"{receiver_type}"
    if channel:
        receiver_desc += f" → {channel}"
    elif receiver_url:
        receiver_desc += f" → {receiver_url}"

    steps.append({
        "step": 2,
        "action": "match_receiver",
        "detail": f"Alert labels: {alert_labels}",
        "receiver_type": receiver_type,
        "receiver_desc": receiver_desc,
    })

    # Step 3: Check enforced routing (NOC override)
    enforced = parsed.get("enforced_routing")
    has_enforced = bool(enforced and isinstance(enforced, dict)
                        and enforced.get("enabled") is not False)
    if has_enforced:
        enforced_type = enforced.get("receiver_type", "webhook")
        enforced_channel = enforced.get("channel", "")
        steps.append({
            "step": 3,
            "action": "enforced_routing",
            "detail": "NOC enforced route active — additional notification",
            "enforced_receiver": f"{enforced_type} → {enforced_channel or '(default)'}",
        })
    else:
        steps.append({
            "step": 3,
            "action": "enforced_routing",
            "detail": "No enforced routing active",
        })

    # Step 4: Check inhibition (severity dedup)
    inhibited = False
    inhibit_reason = ""
    dedup_config = parsed.get("dedup_tenants", {}).get(tenant)
    if dedup_config and severity == "warning":
        # If tenant has severity dedup enabled, warning alerts may be
        # inhibited when a critical alert is also firing
        inhibited = False  # can't know at config time; mark as "possible"
        inhibit_reason = (
            f"Severity dedup active for '{tenant}': warning may be "
            f"inhibited if critical alert is also firing"
        )
        steps.append({
            "step": 4,
            "action": "inhibit_check",
            "detail": inhibit_reason,
            "inhibited": "possible",
        })
    else:
        steps.append({
            "step": 4,
            "action": "inhibit_check",
            "detail": "No inhibition applies",
            "inhibited": False,
        })

    # Step 5: Domain policy check
    domain_policies = parsed.get("domain_policies", {})
    policy_issues: list[str] = []
    for domain_name, policy in domain_policies.items():
        constraints = policy.get("constraints", {})
        forbidden = constraints.get("forbidden_receiver_types", [])
        allowed = constraints.get("allowed_receiver_types", [])
        if forbidden and receiver_type in forbidden:
            policy_issues.append(
                f"Domain '{domain_name}' forbids receiver type '{receiver_type}'"
            )
        if allowed and receiver_type not in allowed:
            policy_issues.append(
                f"Domain '{domain_name}' only allows {allowed}, got '{receiver_type}'"
            )

    if policy_issues:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": "Policy violations detected",
            "violations": policy_issues,
            "passed": False,
        })
    else:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": "All domain policies passed",
            "passed": True,
        })

    # Timing
    timing = {
        "group_wait": final_routing.get("group_wait", "30s"),
        "group_interval": final_routing.get("group_interval", "5m"),
        "repeat_interval": final_routing.get("repeat_interval", "4h"),
    }

    return {
        "tenant": tenant,
        "alertname": alertname,
        "severity": severity,
        "labels": alert_labels,
        "steps": steps,
        "final_receiver": receiver_desc,
        "inhibited": inhibited,
        "inhibit_reason": inhibit_reason,
        "timing": timing,
    }


def format_trace(trace: dict, *, lang: str = "en") -> str:
    """Format a route trace as human-readable text."""
    lines: list[str] = []
    t = trace["tenant"]
    a = trace["alertname"]
    s = trace["severity"]

    if lang == "zh":
        lines.append(f"╔══ 路由追蹤: {a} (租戶={t}, 嚴重度={s}) ══╗")
    else:
        lines.append(f"╔══ Route Trace: {a} (tenant={t}, severity={s}) ══╗")
    lines.append("")

    step_icons = {
        "resolve_routing_config": "📋",
        "match_receiver": "📡",
        "enforced_routing": "🛡️",
        "inhibit_check": "🚫",
        "policy_check": "✅",
    }

    for step in trace["steps"]:
        icon = step_icons.get(step["action"], "▸")
        action_label = step["action"].replace("_", " ").title()
        lines.append(f"  {icon} Step {step['step']}: {action_label}")
        lines.append(f"     {step['detail']}")
        if "receiver_desc" in step:
            label = "接收者:" if lang == "zh" else "Receiver:"
            lines.append(f"     {label} {step['receiver_desc']}")
        if "enforced_receiver" in step:
            label = "強制接收者:" if lang == "zh" else "Enforced:"
            lines.append(f"     {label} {step['enforced_receiver']}")
        if step.get("violations"):
            for v in step["violations"]:
                lines.append(f"     ⚠ {v}")
        lines.append("")

    # Final summary
    if lang == "zh":
        lines.append(f"── 最終結果 ──")
        lines.append(f"  接收者: {trace['final_receiver']}")
        lines.append(f"  抑制: {'可能' if trace['inhibit_reason'] else '否'}")
    else:
        lines.append(f"── Final Result ──")
        lines.append(f"  Receiver: {trace['final_receiver']}")
        lines.append(f"  Inhibited: {'possible' if trace['inhibit_reason'] else 'no'}")

    t_info = trace["timing"]
    lines.append(f"  Timing: group_wait={t_info['group_wait']}, "
                 f"group_interval={t_info['group_interval']}, "
                 f"repeat_interval={t_info['repeat_interval']}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_HELP = {
    "config_dir": {
        "zh": "設定目錄路徑 (含 tenant YAML 與 _routing_profiles.yaml)",
        "en": "Config directory path (with tenant YAMLs and _routing_profiles.yaml)",
    },
    "tenant": {
        "zh": "只顯示指定 tenant（可多次指定）",
        "en": "Show only specified tenant(s) (repeatable)",
    },
    "show_profile_expansion": {
        "zh": "顯示所有路由設定檔的展開與引用關係",
        "en": "Show all routing profile expansions and references",
    },
    "json": {
        "zh": "以 JSON 格式輸出",
        "en": "Output in JSON format",
    },
    "trace": {
        "zh": "追蹤模式：模擬 alert 路由路徑（需搭配 --tenant）",
        "en": "Trace mode: simulate alert routing path (requires --tenant)",
    },
    "alertname": {
        "zh": "追蹤的 alert 名稱（搭配 --trace）",
        "en": "Alert name to trace (use with --trace)",
    },
    "severity": {
        "zh": "Alert 嚴重度（預設 warning）",
        "en": "Alert severity (default: warning)",
    },
}


def main(argv: list[str] | None = None) -> int:
    lang = detect_cli_lang()

    def _h(key: str) -> str:
        return _HELP[key].get(lang, _HELP[key]["en"])

    parser = argparse.ArgumentParser(
        description="Routing merge pipeline debugger (ADR-007)",
    )
    parser.add_argument("--config-dir", required=True, help=_h("config_dir"))
    parser.add_argument("--tenant", action="append", dest="tenants",
                        help=_h("tenant"))
    parser.add_argument("--show-profile-expansion", action="store_true",
                        help=_h("show_profile_expansion"))
    parser.add_argument("--trace", action="store_true", help=_h("trace"))
    parser.add_argument("--alertname", default="GenericAlert",
                        help=_h("alertname"))
    parser.add_argument("--severity", default="warning",
                        help=_h("severity"))
    parser.add_argument("--json", action="store_true", help=_h("json"))

    args = parser.parse_args(argv)

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config directory not found: {args.config_dir}",
              file=sys.stderr)
        return 1

    parsed = _parse_config_files(args.config_dir)

    # --trace mode: simulate alert routing path
    if args.trace:
        if not args.tenants:
            print("ERROR: --trace requires --tenant", file=sys.stderr)
            return 1
        all_tenants = sorted(set(parsed["all_tenants"]))
        traces = []
        for t in args.tenants:
            if t not in all_tenants:
                print(f"  WARN: tenant '{t}' not found", file=sys.stderr)
                continue
            trace = trace_alert_routing(
                parsed, t, args.alertname, args.severity)
            traces.append(trace)
        if args.json:
            print(json.dumps(traces, indent=2, ensure_ascii=False))
        else:
            for trace in traces:
                print(format_trace(trace, lang=lang))
        return 0

    # --show-profile-expansion mode
    if args.show_profile_expansion:
        expansion = explain_profile_expansion(parsed)
        if args.json:
            print(json.dumps(expansion, indent=2, ensure_ascii=False))
        else:
            print(format_profile_expansion(expansion, lang=lang))
        return 0

    # Default: explain tenant routing
    all_tenants = sorted(set(parsed["all_tenants"]))
    disabled = parsed.get("disabled_tenants", set())
    target_tenants = args.tenants or all_tenants

    results = []
    for t in target_tenants:
        if t not in all_tenants:
            print(f"  WARN: tenant '{t}' not found in config-dir",
                  file=sys.stderr)
            continue
        if t in disabled:
            print(f"  INFO: tenant '{t}' has routing disabled, skipping",
                  file=sys.stderr)
            continue
        explanation = explain_tenant_routing(parsed, t)
        results.append(explanation)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for explanation in results:
            print(format_explanation(explanation, lang=lang))

    return 0


if __name__ == "__main__":
    sys.exit(main())
