#!/usr/bin/env python3
"""explain_route.py — Routing merge pipeline debugger (ADR-007).

Shows the four-layer merge expansion for each tenant's routing config:
  1. _routing_defaults  → global defaults
  2. routing_profiles[ref] → team/domain shared config
  3. tenant _routing → per-tenant overrides
  4. _routing_enforced → NOC route rendered BESIDE the tenant's
     (continue: true); shown as its own layer, never merged into the result

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
import warnings
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
    BaseConfigInputError,
    _build_tenant_routes,
    _contains_tenant_placeholder,
    _merge_tenant_routing,
    _parse_config_files,
    _substitute_tenant,
    generate_routes,
    load_base_config,
    merge_routing_with_defaults,
)
from _grar_render import _inject_custom_alert_isolation  # noqa: E402
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

    # G2 (#2293): the enforced route is an ADDITIONAL route (continue: true)
    # the generator renders beside the tenant's, not an override of it — so
    # its keys never replace the tenant's receiver / timing here.
    final = dict(merged)

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


# ---------------------------------------------------------------------------
# Alertmanager route-tree walk (#2293)
# ---------------------------------------------------------------------------
# The trace walks the tree the generator actually renders — platform top-level
# routes, enforced NOC routes, tenant routes and their children — with
# Alertmanager's own semantics, instead of first-matching the tenant's
# children only. The matcher parser below is deliberately NOT
# `_grar_validate._matcher_matches_labels`: that one serves the inhibit guards,
# reads one matcher per string and answers True when it cannot parse, which is
# the safe direction there and the wrong one for a routing verdict.

# Alertmanager's built-in route defaults (config/config.go DefaultRouteOpts),
# used only when the root route itself leaves a value unset.
AM_DEFAULT_TIMING = {"group_wait": "30s", "group_interval": "5m",
                     "repeat_interval": "4h"}
_TIMING_KEYS = tuple(AM_DEFAULT_TIMING)
_MATCHER_OPS = ("=~", "!~", "!=", "=")  # longest first: "=~" before "="
_MATCHER_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_REGEX_CACHE: dict[str, re.Pattern] = {}


class MatcherParseError(ValueError):
    """A matcher string Alertmanager's syntax does not allow."""


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read the double-quoted string at ``text[start]``.

    Escapes as Alertmanager's matcher parser reads them: ``\\"`` → ``"``,
    ``\\\\`` → ``\\``, ``\\n`` → newline; any other backslash stays literal.
    Returns (value, index just past the closing quote).
    """
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise MatcherParseError(f"unterminated quoted string in {text!r}")


def _split_matcher_list(text: str) -> list[str]:
    """Split ``a="x", b=~"y"`` / ``{a="x", b=~"y"}`` on the commas outside quotes."""
    body = text.strip()
    if body.startswith("{") or body.endswith("}"):
        if not (body.startswith("{") and body.endswith("}")):
            raise MatcherParseError(f"unbalanced braces in {text!r}")
        body = body[1:-1]
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(body):
        ch = body[i]
        if quoted and ch == "\\" and i + 1 < len(body):
            buf.append(body[i:i + 2])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quoted:
        raise MatcherParseError(f"unterminated quoted string in {text!r}")
    parts.append("".join(buf).strip())
    if parts and parts[-1] == "":
        parts.pop()  # a trailing comma is allowed
    if any(p == "" for p in parts):
        raise MatcherParseError(f"empty matcher in {text!r}")
    return parts


def _parse_one_matcher(text: str) -> tuple[str, str, str]:
    """``name<op>value`` → (name, op, value), value unescaped."""
    if text.startswith('"'):
        name, i = _read_quoted(text, 0)
    else:
        m = _MATCHER_NAME_RE.match(text)
        if not m:
            raise MatcherParseError(f"invalid label name in {text!r}")
        name, i = m.group(0), m.end()
    rest = text[i:].lstrip()
    op = next((o for o in _MATCHER_OPS if rest.startswith(o)), None)
    if op is None:
        raise MatcherParseError(
            f"expected one of = != =~ !~ after the label name in {text!r}")
    raw = rest[len(op):].strip()
    if raw.startswith('"'):
        value, end = _read_quoted(raw, 0)
        if raw[end:].strip():
            raise MatcherParseError(f"text after the quoted value in {text!r}")
    elif '"' in raw:
        raise MatcherParseError(f"unescaped double quote in {text!r}")
    else:
        value = raw
    return name, op, value


def _compile_am_regex(value: str) -> re.Pattern:
    """Compile a matcher regex the way the trace evaluates it.

    Alertmanager anchors the value (``^(?:value)$``) and runs Go RE2, whose
    ``\\d`` / ``\\w`` / ``\\s`` are ASCII-only — hence ``re.ASCII`` and
    ``fullmatch`` on the value itself. It is NOT wrapped in ``(?:…)``: Python
    rejects a leading inline flag such as ``(?i)`` inside a group, which RE2
    accepts. A POSIX class (``[[:digit:]]``) means something different in
    Python (a nested set, with a FutureWarning), so it is refused rather than
    evaluated differently; any Python warning while compiling is refused the
    same way instead of leaking to stderr. Raises MatcherParseError.
    """
    if value in _REGEX_CACHE:
        return _REGEX_CACHE[value]
    if "[[:" in value:
        raise MatcherParseError(
            f"POSIX character class in regex {value!r} is not evaluated by "
            f"the trace (Python re reads it differently from Alertmanager)")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            pattern = re.compile(value, re.ASCII)
        except (re.error, Warning) as exc:
            raise MatcherParseError(
                f"regex {value!r} cannot be evaluated: {exc}") from exc
    _REGEX_CACHE[value] = pattern
    return pattern


def parse_matchers(text: str) -> list[tuple[str, str, str]]:
    """Parse one Alertmanager ``matchers:`` entry — possibly several matchers.

    Raises MatcherParseError.
    """
    out = []
    for part in _split_matcher_list(text):
        name, op, value = _parse_one_matcher(part)
        if op in ("=~", "!~"):
            _compile_am_regex(value)  # fail here, not at match time
        out.append((name, op, value))
    return out


def _matcher_holds(name: str, op: str, value: str, labels: dict) -> bool:
    """AM semantics: an absent label reads as ""; regexes are anchored."""
    have = labels.get(name, "")
    if op == "=":
        return have == value
    if op == "!=":
        return have != value
    hit = _compile_am_regex(value).fullmatch(have) is not None
    return hit if op == "=~" else not hit


def _route_matchers(node: dict) -> list[tuple[str, str, str]]:
    """Every matcher of a route node: ``matchers`` plus legacy ``match`` /
    ``match_re``. Raises MatcherParseError when one cannot be read."""
    out: list[tuple[str, str, str]] = []
    for entry in node.get("matchers") or []:
        if not isinstance(entry, str):
            raise MatcherParseError(f"matcher is not a string: {entry!r}")
        out.extend(parse_matchers(entry))
    for key, op in (("match", "="), ("match_re", "=~")):
        for name, value in (node.get(key) or {}).items():
            if op == "=~":
                _compile_am_regex(str(value))
            out.append((str(name), op, str(value)))
    return out


def walk_route_tree(root: dict, labels: dict, *,
                    warn=None) -> list[dict]:
    """Every route an alert with *labels* is delivered through, in AM order.

    Alertmanager's dispatch semantics (dispatch/route.go ``Route.Match``): a
    node that matches tries its children in order; a matching child ends the
    scan unless it sets ``continue: true``; a node none of whose children
    match delivers the alert itself. The root always matches. ``receiver``,
    ``group_by`` and the three timings are inherited from the parent unless a
    node sets its own.

    A matcher that cannot be parsed makes its route NOT match, and *warn* is
    called once with a message (fail closed: the trace must not claim a
    delivery the real Alertmanager would refuse to load).

    Returns one dict per delivery: ``receiver``, ``group_by``, the timings,
    ``path`` (child indices from the root) and ``nodes`` (the route dicts
    along that path, root excluded).
    """
    seen_warn: set[str] = set()

    def _matches(node: dict) -> bool:
        try:
            matchers = _route_matchers(node)
        except (MatcherParseError, re.error) as exc:
            shown = {k: node[k] for k in ("matchers", "match", "match_re")
                     if node.get(k) is not None}
            msg = f"cannot parse a matcher of route {shown!r}: {exc}"
            if warn and msg not in seen_warn:
                seen_warn.add(msg)
                warn(msg)
            return False
        return all(_matcher_holds(n, o, v, labels) for n, o, v in matchers)

    def _opts(node: dict, parent: dict) -> dict:
        opts = dict(parent)
        if node.get("receiver"):
            opts["receiver"] = node["receiver"]
        if isinstance(node.get("group_by"), list):
            opts["group_by"] = list(node["group_by"])
        for key in _TIMING_KEYS:
            if node.get(key):
                opts[key] = str(node[key])
        return opts

    def _visit(node: dict, parent: dict, path: list[int],
               nodes: list[dict]) -> list[dict]:
        if nodes and not _matches(node):
            return []
        opts = _opts(node, parent)
        hits: list[dict] = []
        for idx, child in enumerate(node.get("routes") or []):
            sub = _visit(child, opts, path + [idx], nodes + [child])
            hits.extend(sub)
            if sub and not child.get("continue", False):
                break
        if not hits:
            hits = [dict(opts, path=path, nodes=nodes)]
        return hits

    start = {"receiver": None, "group_by": []}
    start.update(AM_DEFAULT_TIMING)
    return _visit(root, start, [], [])


def _conf_receiver_types(routing_configs: dict[str, dict],
                         enforced: dict | None) -> dict[str, str]:
    """Receiver name → the ``receiver.type`` it was generated from (G1).

    The rendered ``*_configs`` key cannot stand in for it: ``rocketchat``
    renders to ``webhook_configs``, ``teams`` to ``msteams_configs``.
    """
    def _t(obj) -> str | None:
        rtype = obj.get("type") if isinstance(obj, dict) else None
        return rtype if isinstance(rtype, str) else None

    types: dict[str, str | None] = {}
    for tenant, cfg in routing_configs.items():
        types[f"tenant-{tenant}"] = _t(cfg.get("receiver"))
        for field, kind in (("overrides", "override"), ("routes", "route")):
            entries = cfg.get(field)
            for idx, entry in enumerate(entries if isinstance(entries, list) else []):
                if isinstance(entry, dict):
                    types[f"tenant-{tenant}-{kind}-{idx}"] = _t(entry.get("receiver"))
    if enforced:
        if _contains_tenant_placeholder(enforced):
            for tenant in routing_configs:
                sub = _substitute_tenant(enforced, tenant)
                types[f"platform-enforced-{tenant}"] = _t(sub.get("receiver"))
        else:
            types["platform-enforced"] = _t(enforced.get("receiver"))
    return {k: v for k, v in types.items() if v}


def _receiver_type(name: str, receivers: dict[str, dict],
                   conf_types: dict[str, str]) -> str:
    """conf.d ``receiver.type``; for a receiver not generated from conf.d (the
    base config's, the platform placeholders) the AM integration it carries,
    or ``none`` for a name-only receiver."""
    if name in conf_types:
        return conf_types[name]
    kinds = [k[:-len("_configs")] for k in receivers.get(name, {})
             if k.endswith("_configs")]
    return "+".join(kinds) if kinds else "none"


def build_trace_tree(parsed: dict, base: dict | None = None
                     ) -> tuple[dict, dict[str, dict], dict[str, str]]:
    """The routing tree ``--output-configmap`` would render, minus the wrapping.

    Same pipeline as the generator — the four-layer merge, ``generate_routes``
    with the enforced routing, then ``_inject_custom_alert_isolation`` — hung
    under the root of *base* (the built-in base when None, as ``--validate``
    uses). Like ``assemble_configmap``, the base's own ``route.routes`` is
    replaced; only its root receiver / group_by / timings are kept.

    Returns (root route, receivers by name, conf.d receiver types by name).
    """
    routing_configs = _merge_tenant_routing(
        parsed, parsed.get("routing_defaults") or {})
    enforced = parsed.get("enforced_routing")
    if not (isinstance(enforced, dict) and enforced.get("enabled") is not False):
        enforced = None
    routes, receivers, _warnings = generate_routes(
        routing_configs, None, enforced_routing=enforced)
    routes, receivers = _inject_custom_alert_isolation(routes, receivers)
    base = base if base is not None else load_base_config(None)
    root = {k: v for k, v in (base.get("route") or {}).items() if k != "routes"}
    root["routes"] = routes
    by_name: dict[str, dict] = {}
    for recv in list(base.get("receivers") or []) + receivers:
        if isinstance(recv, dict) and recv.get("name"):
            by_name.setdefault(recv["name"], recv)  # base wins, as in assembly
    return root, by_name, _conf_receiver_types(routing_configs, enforced)


def _policies_for_tenant(domain_policies: dict, tenant: str
                         ) -> list[tuple[str, set, set]]:
    """``(name, forbidden types, allowed types)`` of every domain policy
    that applies to *tenant* — read the way the generator's
    ``check_domain_policies`` reads them (non-strict): a policy applies only
    when its ``tenants`` list names the tenant; a non-mapping policy or
    ``constraints``, a non-list ``tenants``, and a non-list type constraint
    are inert.
    """
    out = []
    for name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list) or tenant not in tenants:
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            continue

        def _types(field: str) -> set:
            raw = constraints.get(field)
            return set(raw) if isinstance(raw, list) else set()

        out.append((name, _types("forbidden_receiver_types"),
                    _types("allowed_receiver_types")))
    return out


def _describe_path(hit: dict) -> str:
    """``route.routes[4] {tenant="<t>"} → routes[0] {team="x"}``; the root
    when no route matched."""
    if not hit["nodes"]:
        return "root (no route matched)"
    parts = []
    for depth, (idx, node) in enumerate(zip(hit["path"], hit["nodes"])):
        head = "route.routes" if depth == 0 else "routes"
        matchers = ", ".join(str(m) for m in node.get("matchers") or [])
        parts.append(f"{head}[{idx}] {{{matchers}}}")
    return " → ".join(parts)


def trace_alert_routing(
    parsed: dict,
    tenant: str,
    alertname: str,
    severity: str = "warning",
    extra_labels: dict | None = None,
    *,
    base_config: dict | None = None,
) -> dict:
    """Simulate how a specific alert would be routed through the pipeline.

    Given a tenant + alert labels, walks the WHOLE rendered route tree
    (``build_trace_tree``) with Alertmanager's semantics (#2293):
      1. The tenant's merged routing config
      2. The route(s) the alert is delivered through, and their receiver
      3. Whether the enforced NOC route takes a copy (only when it matches)
      4. Whether any inhibit rules would suppress it
      5. Domain policy against the receiver type actually reached

    ``extra_labels`` adds labels to the alert (e.g. ``metric_group`` for an
    ``overrides`` entry, or a ``routes`` match key). A key the trace sets
    itself (``alertname`` / ``severity`` / ``tenant``) raises ValueError
    rather than silently replacing the dedicated argument (#2264).
    ``base_config`` is the loaded ``--base-config`` (None → built-in base).

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

    # Step 2: walk the rendered tree (#2293)
    root, receivers, conf_types = build_trace_tree(parsed, base_config)

    def _warn(msg: str) -> None:
        print(f"  WARN: {safe_label(msg)} — treated as not matching",
              file=sys.stderr)

    hits = walk_route_tree(root, alert_labels, warn=_warn)
    for hit in hits:
        hit["receiver_type"] = _receiver_type(hit["receiver"] or "", receivers,
                                              conf_types)
        hit["route_path"] = _describe_path(hit)

    def _is_enforced(hit: dict) -> bool:
        return str(hit["receiver"] or "").startswith("platform-enforced")

    enforced_hits = [h for h in hits if _is_enforced(h)]
    primary = [h for h in hits if not _is_enforced(h)]

    main_prefix = f"tenant-{tenant}"
    matched_sub = None
    if primary:
        top = primary[0]
        where = top["route_path"]
        # A child of the tenant's main route: name it by its conf.d source.
        nodes = top["nodes"]
        if (len(nodes) == 2 and nodes[0].get("receiver") == main_prefix
                and f'tenant="{tenant}"' in (nodes[0].get("matchers") or [])):
            name = top["receiver"] or ""
            for kind, field in (("override", "overrides"), ("route", "routes")):
                head = f"{main_prefix}-{kind}-"
                if name.startswith(head) and name[len(head):].isdigit():
                    matched_sub = f"{field}[{name[len(head):]}]"
            if matched_sub:
                where = f"sub-route {matched_sub}"
        receiver_type = top["receiver_type"]
        receiver_desc = f"{receiver_type} → {top['receiver']} ({where})"
        timing_src = top
    else:
        # Only continue:true (enforced) routes matched: they ARE the
        # delivery — Alertmanager does NOT fall back to the root receiver.
        top = enforced_hits[0]
        receiver_type = top["receiver_type"]
        receiver_desc = (f"{receiver_type} → {top['receiver']} ({top['route_path']}"
                         f"; only the enforced route matched, no root fallback)")
        timing_src = top

    step2 = {
        "step": 2,
        "action": "match_receiver",
        "detail": f"Alert labels: {alert_labels}",
        "receiver_type": receiver_type,
        "receiver_desc": receiver_desc,
        "route_path": timing_src["route_path"],
        "matched_routes": [
            {"receiver": h["receiver"], "receiver_type": h["receiver_type"],
             "route_path": h["route_path"]} for h in hits],
    }
    if matched_sub is not None:
        step2["matched_sub_route"] = matched_sub
    steps.append(step2)

    # Step 3: enforced routing (NOC) — shown only when it takes the alert
    enforced = parsed.get("enforced_routing")
    has_enforced = bool(enforced and isinstance(enforced, dict)
                        and enforced.get("enabled") is not False)
    if enforced_hits:
        steps.append({
            "step": 3,
            "action": "enforced_routing",
            "detail": "NOC enforced route matches — additional notification",
            "enforced_receiver": ", ".join(
                f"{h['receiver_type']} → {h['receiver']}" for h in enforced_hits),
        })
    elif has_enforced:
        steps.append({
            "step": 3,
            "action": "enforced_routing",
            "detail": ("NOC enforced route configured but not reached by this "
                       "alert (its matchers exclude it, or an earlier "
                       "continue:false route took the alert)"),
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

    # Step 5: Domain policy check — against the type of the tenant receiver
    # the alert actually reaches (G1: `receiver.type`, not a legacy key).
    tenant_types = [h["receiver_type"] for h in primary
                    if str(h["receiver"] or "") == main_prefix
                    or str(h["receiver"] or "").startswith(main_prefix + "-")]
    applicable = _policies_for_tenant(parsed.get("domain_policies") or {},
                                      tenant)
    policy_issues: list[str] = []
    for domain_name, forbidden, allowed in applicable:
        for rtype in tenant_types:
            if forbidden and rtype in forbidden:
                policy_issues.append(
                    f"Domain '{domain_name}' forbids receiver type '{rtype}'")
            if allowed and rtype not in allowed:
                policy_issues.append(
                    f"Domain '{domain_name}' only allows {sorted(allowed)}, got '{rtype}'")

    if policy_issues:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": "Policy violations detected",
            "violations": policy_issues,
            "passed": False,
        })
    elif not applicable:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": f"No domain policy lists tenant '{tenant}'",
            "passed": True,
        })
    elif not tenant_types:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": ("No tenant receiver on the alert's path — domain "
                       "policies do not apply"),
            "passed": True,
        })
    else:
        steps.append({
            "step": 5,
            "action": "policy_check",
            "detail": "All domain policies passed",
            "passed": True,
        })

    # Timing — what the delivering route ends up with after inheritance.
    timing = {key: timing_src[key] for key in _TIMING_KEYS}

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
        lines.append(f"     {safe_label(step['detail'])}")
        if step.get("route_path"):
            label = "路徑:" if lang == "zh" else "Path:"
            lines.append(f"     {label} {safe_label(step['route_path'])}")
        if "receiver_desc" in step:
            label = "接收者:" if lang == "zh" else "Receiver:"
            lines.append(f"     {label} {safe_label(step['receiver_desc'])}")
        if "enforced_receiver" in step:
            label = "強制接收者:" if lang == "zh" else "Enforced:"
            lines.append(f"     {label} {safe_label(step['enforced_receiver'])}")
        if step.get("violations"):
            for v in step["violations"]:
                lines.append(f"     ⚠ {safe_label(v)}")
        lines.append("")

    # Final summary
    receiver = safe_label(trace["final_receiver"])
    if lang == "zh":
        lines.append("── 最終結果 ──")
        lines.append(f"  接收者: {receiver}")
        lines.append(f"  抑制: {'可能' if trace['inhibit_reason'] else '否'}")
    else:
        lines.append("── Final Result ──")
        lines.append(f"  Receiver: {receiver}")
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
    "base_config": {
        "zh": "追蹤用的 base Alertmanager YAML（只取 root 的 receiver／group_by／"
              "timing；route.routes 會被產生的路由整份取代）。只在 --trace 下讀取；"
              "未給時用內建 base（與 generate_alertmanager_routes --validate 相同）",
        "en": "Base Alertmanager YAML for the trace (only the root receiver / "
              "group_by / timings are used; route.routes is replaced by the "
              "generated routes). Read only with --trace; omitted → the built-in "
              "base (same as generate_alertmanager_routes --validate)",
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
    parser.add_argument("--base-config", default=None, metavar="PATH",
                        help=_h("base_config"))
    parser.add_argument("--json", action="store_true", help=_h("json"))

    args = parser.parse_args(argv)

    # #2264: --label only feeds the trace; outside it the flag would be
    # silently ignored, so refuse it (argparse caller error → rc 2).
    if args.labels and not args.trace:
        parser.error("--label is read only with --trace")
    if args.base_config is not None and not args.trace:
        parser.error("--base-config is read only with --trace")
    try:
        extra_labels = parse_label_args(args.labels)
    except ValueError as exc:
        parser.error(str(exc))

    if not Path(args.config_dir).is_dir():
        print(f"ERROR: config directory not found: {safe_label(args.config_dir)}",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    parsed = _parse_config_files(args.config_dir)

    # --trace mode: simulate alert routing path
    if args.trace:
        if not args.tenants:
            print("ERROR: --trace requires --tenant", file=sys.stderr)
            return EXIT_CALLER_ERROR
        try:
            base_config = load_base_config(args.base_config)
        except BaseConfigInputError as exc:
            print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        all_tenants = sorted(set(parsed["all_tenants"]))
        traces = []
        for t in args.tenants:
            if t not in all_tenants:
                print(f"  WARN: tenant '{safe_label(t)}' not found", file=sys.stderr)
                continue
            trace = trace_alert_routing(
                parsed, t, args.alertname, args.severity,
                extra_labels=extra_labels, base_config=base_config)
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
