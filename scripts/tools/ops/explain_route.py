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
    explain_route.py --config-dir conf.d --tenant <tenant> --trace \\
        --alertname <name> --label metric_group=<group>
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

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
    _build_tenant_routes,
    _parse_config_files,
    merge_routing_with_defaults,
)
from _grar_validate import (  # noqa: E402
    ROUTING_TREE_ERROR_PREFIX,
    _matcher_matches_labels,
    duplicate_tenant_errors,
)
from _grar_parse import BLOCKING_TREE_KINDS  # noqa: E402
# #2326: the layer chain across conf.d directory levels — the generator's own.
from _grar_merge import (  # noqa: E402
    ROOT_LEVEL,
    resolve_routing_defaults,
    visible_routing_profiles,
)
from _lib_python import detect_cli_lang, format_json_report  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

# Keys of the merged routing config that the generator turns into CHILD
# routes rather than settings of the tenant's main route (#2245). The text
# report shows them as rendered sub-routes, not as merged config, so an
# entry the generator skips is never presented as if it were in effect.
SUB_ROUTE_SOURCE_KEYS = ("overrides", "routes")


def effective_sub_routes(tenant: str, merged: dict) -> tuple[list[dict], list[str]]:
    """The child routes the generator renders under the tenant's main route.

    Computed by the generator itself (``_build_tenant_routes``) from the
    merged config, so this is what Alertmanager receives, in match order:
    overrides first, then the ADR-007 label-match ``routes`` (#2245). Each
    entry is the rendered child route plus ``source`` (``overrides[i]`` /
    ``routes[i]``) and ``receiver_type``. Returns (sub_routes, skipped) —
    ``skipped`` is every ``… skipping`` line the generator emitted for an
    override / ``routes`` entry. No domain allowlist is applied here
    (explain has no ``--policy``).
    """
    if not isinstance(merged, dict) or not merged.get("receiver"):
        return [], []
    routes, receivers, warnings = _build_tenant_routes({tenant: merged})
    types: dict[str, str] = {}
    for recv in receivers:
        for key in recv:
            if key.endswith("_configs"):
                types[recv["name"]] = key[:-len("_configs")]
    prefix = f"tenant-{tenant}-"
    out: list[dict] = []
    for parent in routes:
        for child in parent.get("routes", []):
            name = child.get("receiver", "")
            kind, _, idx = name[len(prefix):].rpartition("-")
            source = {"override": "overrides", "route": "routes"}.get(kind, kind)
            entry = {"source": f"{source}[{idx}]"}
            entry.update(child)
            entry["receiver_type"] = types.get(name, "")
            out.append(entry)
    # Skipped sub-route entries only: the main receiver's own problems are
    # the tenant route's, and a timing clamp is not a skip.
    markers = ("override[", "routes[", "'overrides'", "'routes'",
               f"{tenant}-override-", f"{tenant}-route-")
    skipped = [w for w in warnings
               if "skipping" in w and any(m in w for m in markers)]
    return out, skipped

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
    # #2326: a tenant below the conf.d root inherits `_routing_defaults` from
    # every directory level on its way down and sees the profiles of those
    # levels — the generator's own resolution (_grar_merge).
    level = parsed.get("tenant_dirs", {}).get(tenant, ROOT_LEVEL)

    # Layer 1: routing defaults
    routing_defaults = resolve_routing_defaults(parsed, level)
    layers.append({
        "name": "Layer 1: _routing_defaults",
        "source": ("_defaults.yaml / _routing_defaults key"
                   if level == ROOT_LEVEL else
                   f"_routing_defaults of the conf.d root, then each "
                   f"_defaults.yaml down to {level}/"),
        "config": dict(routing_defaults) if routing_defaults else {},
    })

    # Layer 2: routing profile
    profile_refs = parsed.get("tenant_profile_refs", {})
    profiles = visible_routing_profiles(parsed, level)
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

    # #2245: what the generator actually renders from `overrides` / `routes`.
    sub_routes, skipped = effective_sub_routes(tenant, merged)

    return {
        "tenant": tenant,
        "profile_ref": profile_ref,
        "layers": layers,
        "final": final,
        "sub_routes": sub_routes,
        "skipped_sub_routes": skipped,
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
    # #1538: tenant names and layer sources are tenant-controlled. Escaped in the
    # TEXT renderer only — format_json_report() gets the same dict unescaped.
    t = safe_label(explanation["tenant"])
    ref = safe_label(explanation["profile_ref"]) if explanation["profile_ref"] else \
        explanation["profile_ref"]

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
        lines.append(f"── {safe_label(layer['name'])} ──")
        lines.append(f"   Source: {safe_label(layer['source'])}")
        cfg = layer["config"]
        if cfg:
            for line in _fmt_yaml(cfg).splitlines():
                lines.append(f"   {line}")
        else:
            lines.append("   (empty)")
        lines.append("")

    header = "最終合併結果:" if lang == "zh" else "Final merged result:"
    lines.append(f"── {header} ──")
    # #2245: `overrides` / `routes` are listed below as the sub-routes the
    # generator renders — an entry it skips must not read as in effect.
    final = {k: v for k, v in explanation["final"].items()
             if k not in SUB_ROUTE_SOURCE_KEYS}
    for line in _fmt_yaml(final).splitlines():
        lines.append(f"   {line}")
    lines.append("")

    sub_routes = explanation.get("sub_routes", [])
    skipped = explanation.get("skipped_sub_routes", [])
    if sub_routes or skipped:
        header = ("生效的子路由（依比對順序，都沒命中則用主 receiver）:"
                  if lang == "zh" else
                  "Effective sub-routes (match order; no match → main receiver):")
        lines.append(f"── {header} ──")
        for i, sub in enumerate(sub_routes, 1):
            matchers = ", ".join(sub.get("matchers", []))
            lines.append(f"   {i}. {safe_label(sub['source'])}: "
                         f"{safe_label(matchers)} → {safe_label(sub['receiver'])}"
                         f" ({safe_label(sub.get('receiver_type') or '?')})")
            own = {k: sub[k] for k in ("group_by", "group_wait",
                                       "group_interval", "repeat_interval")
                   if k in sub}
            if own:
                lines.append(f"      {safe_label(own)}")
        if not sub_routes:
            lines.append("   (none)")
        if skipped:
            lines.append("   未生效（產生器略過）:" if lang == "zh"
                         else "   Not in effect (skipped by the generator):")
            for w in skipped:
                lines.append(f"     {safe_label(w.strip())}")
        lines.append("")

    return "\n".join(lines)


def format_profile_expansion(expansion: dict, *, lang: str = "en") -> str:
    """Format profile expansion as human-readable text."""
    lines: list[str] = []
    header = "路由設定檔展開:" if lang == "zh" else "Routing Profile Expansion:"
    lines.append(f"╔══ {header} ══╗")
    lines.append("")

    for name, info in sorted(expansion.items()):
        lines.append(f"── Profile: {safe_label(name)} ──")
        if info.get("error"):
            lines.append(f"   ⚠ {safe_label(info['error'])}")
        if info["config"]:
            for line in _fmt_yaml(info["config"]).splitlines():
                lines.append(f"   {line}")
        else:
            lines.append("   (not defined)")

        refs = info.get("referenced_by", [])
        ref_label = "引用者:" if lang == "zh" else "Referenced by:"
        if refs:
            lines.append(f"   {ref_label} {safe_label(', '.join(refs))}")
        else:
            orphan = "（無引用 — 孤立設定檔）" if lang == "zh" else "(no references — orphan profile)"
            lines.append(f"   {ref_label} {orphan}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert route tracing (v2.1.0)
# ---------------------------------------------------------------------------

# Labels the trace itself sets, each from its own dedicated input (#2264).
# An extra label with one of these keys would silently replace that input,
# so it is refused instead — the value maps to the flag that sets it.
RESERVED_TRACE_LABELS = {
    "alertname": "--alertname",
    "severity": "--severity",
    "tenant": "--tenant",
}

# Prometheus label-name syntax.
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def parse_label_args(raw: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--label KEY=VALUE`` into a dict (#2264).

    Split on the FIRST ``=`` — the value may contain ``=`` and may be empty.
    Raises ValueError (message for the caller, values already escaped) on a
    missing ``=``, a key that is not a valid label name, a key the trace sets
    through its own flag, or a key given twice.
    """
    labels: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(
                f"--label {safe_label(item)}: expected KEY=VALUE")
        # fullmatch: `$` with .match() would accept a trailing "\n".
        if not _LABEL_NAME_RE.fullmatch(key):
            raise ValueError(
                f"--label {safe_label(item)}: invalid label name "
                f"'{safe_label(key)}' (must match [a-zA-Z_][a-zA-Z0-9_]*)")
        if key in RESERVED_TRACE_LABELS:
            raise ValueError(
                f"--label {safe_label(item)}: '{key}' is set by "
                f"{RESERVED_TRACE_LABELS[key]}; use that flag instead")
        if key in labels:
            raise ValueError(f"--label: '{safe_label(key)}' given more than once")
        labels[key] = value
    return labels


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

    ``extra_labels`` adds labels to the alert (e.g. ``metric_group`` for an
    ``overrides`` entry, or a ``routes`` match key). A key the trace sets
    itself (``alertname`` / ``severity`` / ``tenant``) raises ValueError
    rather than silently replacing the dedicated argument (#2264).

    Returns dict with keys:
        tenant, alertname, severity, labels, steps, final_receiver,
        inhibited, inhibit_reason, timing
    """
    reserved = sorted(set(extra_labels or {}) & set(RESERVED_TRACE_LABELS))
    if reserved:
        raise ValueError(
            f"extra_labels must not set {reserved}: pass them as the "
            f"dedicated arguments instead")
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

    # #2245: the first rendered sub-route (overrides, then `routes`) whose
    # matchers all hold takes the alert — Alertmanager's first-match rule
    # among the children of the tenant's main route.
    matched_sub = next(
        (sub for sub in explanation.get("sub_routes", [])
         if all(_matcher_matches_labels(m, alert_labels)
                for m in sub.get("matchers", []))), None)
    step2 = {
        "step": 2,
        "action": "match_receiver",
        "detail": f"Alert labels: {alert_labels}",
        "receiver_type": receiver_type,
        "receiver_desc": receiver_desc,
    }
    if matched_sub is not None:
        receiver_type = matched_sub.get("receiver_type") or receiver_type
        receiver_desc = (f"{receiver_type} → {matched_sub['receiver']} "
                         f"(sub-route {matched_sub['source']})")
        step2.update(receiver_type=receiver_type, receiver_desc=receiver_desc,
                     matched_sub_route=matched_sub["source"])
    steps.append(step2)

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

    # Timing — a matched sub-route's own values win; the rest are inherited
    # from the tenant's main route (#2252 / #2245).
    timing = {
        "group_wait": final_routing.get("group_wait", "30s"),
        "group_interval": final_routing.get("group_interval", "5m"),
        "repeat_interval": final_routing.get("repeat_interval", "4h"),
    }
    if matched_sub is not None:
        for key in timing:
            if key in matched_sub:
                timing[key] = matched_sub[key]

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
    t = safe_label(trace["tenant"])
    a = safe_label(trace["alertname"])
    s = safe_label(trace["severity"])

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
    "label": {
        "zh": "追蹤用的額外 alert label，格式 KEY=VALUE（可多次指定；"
              "只在 --trace 下讀取；alertname/severity/tenant 請用各自的旗標）",
        "en": "Extra alert label for the trace, as KEY=VALUE (repeatable; "
              "read only with --trace; set alertname/severity/tenant with "
              "their own flags)",
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
    parser.add_argument("--label", action="append", dest="labels",
                        metavar="KEY=VALUE", help=_h("label"))
    parser.add_argument("--json", action="store_true", help=_h("json"))

    args = parser.parse_args(argv)

    # #2264: --label only feeds the trace; outside it the flag would be
    # silently ignored, so refuse it (argparse caller error → rc 2).
    if args.labels and not args.trace:
        parser.error("--label is read only with --trace")
    try:
        extra_labels = parse_label_args(args.labels)
    except ValueError as exc:
        parser.error(str(exc))

    if not Path(args.config_dir).is_dir():
        print(f"ERROR: config directory not found: {safe_label(args.config_dir)}",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    parsed = _parse_config_files(args.config_dir)
    # #2326: a tree the generator refuses outright (rc 2) is still explained
    # here — this is a diagnostic — but never without saying so first.
    for kind, _f, _fld, msg in parsed.get("routing_tree_problems", []):
        if kind in BLOCKING_TREE_KINDS:
            print(f"  {ROUTING_TREE_ERROR_PREFIX} {safe_label(msg)} — "
                  f"generate-routes refuses this tree", file=sys.stderr)
    # #2315 owns the duplicate-tenant refusal (rc 1, not a routing-tree kind),
    # so it is named from its own record — the same lines the generator prints.
    for line in duplicate_tenant_errors(parsed.get("duplicate_tenants", {})):
        print(f"{safe_label(line)} — generate-routes refuses this tree",
              file=sys.stderr)

    # --trace mode: simulate alert routing path
    if args.trace:
        if not args.tenants:
            print("ERROR: --trace requires --tenant", file=sys.stderr)
            return EXIT_CALLER_ERROR
        all_tenants = sorted(set(parsed["all_tenants"]))
        traces = []
        for t in args.tenants:
            if t not in all_tenants:
                print(f"  WARN: tenant '{safe_label(t)}' not found", file=sys.stderr)
                continue
            trace = trace_alert_routing(
                parsed, t, args.alertname, args.severity,
                extra_labels=extra_labels)
            traces.append(trace)
        if args.json:
            print(format_json_report(traces))
        else:
            for trace in traces:
                print(format_trace(trace, lang=lang))
        return EXIT_OK

    # --show-profile-expansion mode
    if args.show_profile_expansion:
        expansion = explain_profile_expansion(parsed)
        if args.json:
            print(format_json_report(expansion))
        else:
            print(format_profile_expansion(expansion, lang=lang))
        return EXIT_OK

    # Default: explain tenant routing
    all_tenants = sorted(set(parsed["all_tenants"]))
    disabled = parsed.get("disabled_tenants", set())
    target_tenants = args.tenants or all_tenants

    results = []
    for t in target_tenants:
        if t not in all_tenants:
            print(f"  WARN: tenant '{safe_label(t)}' not found in config-dir",
                  file=sys.stderr)
            continue
        if t in disabled:
            print(f"  INFO: tenant '{safe_label(t)}' has routing disabled, skipping",
                  file=sys.stderr)
            continue
        explanation = explain_tenant_routing(parsed, t)
        results.append(explanation)

    if args.json:
        print(format_json_report(results))
    else:
        for explanation in results:
            print(format_explanation(explanation, lang=lang))

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
