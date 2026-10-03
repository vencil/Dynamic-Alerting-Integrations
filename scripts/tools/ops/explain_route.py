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
import shutil
import sys
import tempfile
from collections.abc import Iterable
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
    generate_inhibit_rules,
    generate_routes,
    load_base_config,
    merge_routing_with_defaults,
    tree_refusal,
)
from _grar_render import (  # noqa: E402
    _run_binary, assemble_configmap, receiver_integration_kinds,
)
from _grar_validate import (  # noqa: E402
    ROUTING_TREE_ERROR_PREFIX,
    check_policy_scope,
    duplicate_tenant_errors,
    invalid_tenant_id_text,
    routing_defaults_not_mapping_text,
    routing_not_mapping_warning,
)
from _grar_parse import BLOCKING_TREE_KINDS, policy_level_source  # noqa: E402
from _grar_routes import enforced_route_tenants  # noqa: E402
# #2326: the layer chain across conf.d directory levels — the generator's own.
from _grar_merge import (  # noqa: E402
    ROOT_LEVEL,
    SkippedEntryWarning,
    domain_policy_levels,
    policy_reaches,
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
    ``skipped`` is every dropped-entry line (``SkippedEntryWarning``, #2489)
    the generator emitted for an override / ``routes`` entry. No domain allowlist is applied here
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
    # the tenant route's, and a timing clamp is not a skip. #2489: "skipped"
    # is the line's TYPE, not the word — a clamp WARN whose value is
    # `skipping` used to be listed here. `warnings` is the generator's own
    # list, unreformatted, so the mark is intact.
    markers = ("override[", "routes[", "'overrides'", "'routes'",
               f"{tenant}-override-", f"{tenant}-route-")
    skipped = [w for w in warnings
               if isinstance(w, SkippedEntryWarning)
               and any(m in w for m in markers)]
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
# Route trace through Alertmanager's own parser (#2293)
# ---------------------------------------------------------------------------
# The trace renders the WHOLE tree the generator would ship (platform top-level
# routes, enforced NOC routes, tenant routes and their children) and hands it
# to `amtool config routes test --tree`. Which routes match and which receivers
# get the alert are Alertmanager's verdict, not a re-implementation: two rounds
# of simulating RE2 with Python `re` still diverged (case folding, flags,
# repetition limits). The tool only reads amtool's answer back and looks the
# matched nodes up in the tree it rendered, for the timing they inherit.

# Alertmanager's built-in route defaults (config/config.go DefaultRouteOpts),
# used only when the root route itself leaves a value unset.
AM_DEFAULT_TIMING = {"group_wait": "30s", "group_interval": "5m",
                     "repeat_interval": "4h"}
_TIMING_KEYS = tuple(AM_DEFAULT_TIMING)
AMTOOL_TIMEOUT_S = 60  # same budget as _grar_render.amtool_gate
AMTOOL_MISSING = "Not validated by Alertmanager: amtool not found on PATH"
_TREE_BRANCHES = ("├── ", "└── ")
_TREE_PADS = ("│   ", "    ")
_RECEIVER_SEP = "  receiver: "
_CONTINUE_MARK = "  continue: true"  # `routes show` only


class AmtoolOutputError(ValueError):
    """amtool printed something this reader does not recognise."""


def amtool_label_arg(name: str, value: str) -> str:
    """One ``name="value"`` argument for ``amtool config routes test``.

    amtool reads each argument with Alertmanager's matcher parser, so the
    value is double-quoted with ``\\\\``, ``\\"`` and ``\\n`` escaped — which
    keeps ``,``, ``=``, spaces, quotes and newlines inside one label.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n"))
    return f'{name}="{escaped}"'


def parse_amtool_tree(text: str) -> dict:
    """Parse the tree amtool draws (``routes show`` or ``routes test --tree``).

    Returns the root node ``{"label", "receiver", "children"}``. ``label`` is
    amtool's rendering of the route's matchers (``default-route`` for a route
    without any), without the ``  continue: true`` mark ``routes show`` adds;
    ``receiver`` is None where amtool prints none. Raises AmtoolOutputError
    on a shape it does not recognise.
    """
    rows: list[tuple[int, str, str | None]] = []
    started = False
    for line in text.split("\n"):  # "\n" only: see run_amtool_trace
        if not started:
            started = line == "."
            continue
        if not line:
            break  # the tree ends at the first blank line
        pos = depth = 0
        while line[pos:pos + 4] in _TREE_PADS:
            pos += 4
            depth += 1
        if line[pos:pos + 4] not in _TREE_BRANCHES:
            raise AmtoolOutputError(f"unrecognised tree line: {line!r}")
        body = line[pos + 4:]
        label, sep, receiver = body.rpartition(_RECEIVER_SEP)
        if not sep:
            label, receiver = body, None
        label = label.removesuffix(_CONTINUE_MARK)
        rows.append((depth, label, receiver))
    if not rows or rows[0][0] != 0:
        raise AmtoolOutputError("no route tree in amtool output")
    root = {"label": rows[0][1], "receiver": rows[0][2], "children": []}
    stack = [root]
    for depth, label, receiver in rows[1:]:
        if depth < 1 or depth > len(stack):
            raise AmtoolOutputError(f"tree depth jumps at {label!r}")
        del stack[depth:]
        node = {"label": label, "receiver": receiver, "children": []}
        stack[-1]["children"].append(node)
        stack.append(node)
    return root


def _leaf_paths(node: dict, prefix: list[dict] | None = None) -> list[list[dict]]:
    """Root-to-leaf node lists of a ``routes test --tree`` tree, in order —
    each leaf is one delivery."""
    path = (prefix or []) + [node]
    if not node["children"]:
        return [path]
    return [p for child in node["children"] for p in _leaf_paths(child, path)]


def _align(shown: dict, rendered: dict, out: dict[int, dict]) -> bool:
    """Pair each node of ``routes show`` with the rendered route at the same
    position (amtool keeps the order). False when the shapes differ."""
    kids = rendered.get("routes") or []
    if len(shown["children"]) != len(kids):
        return False
    out[id(shown)] = rendered
    return all(_align(s, r, out) for s, r in zip(shown["children"], kids))


def _resolve(shown: dict, path: list[dict]) -> list[list[dict]]:
    """Every ``routes show`` node chain whose labels spell *path*, the last
    node carrying the delivered receiver. Pure lookup — no matching."""
    head, rest = path[0], path[1:]
    if shown["label"] != head["label"]:
        return []
    if not rest:
        return [[shown]] if shown["receiver"] == head["receiver"] else []
    return [[shown] + tail for child in shown["children"]
            for tail in _resolve(child, rest)]


def _inherited(nodes: list[dict]) -> dict:
    """receiver / group_by / timings after AM's parent-to-child inheritance."""
    opts: dict = {"receiver": None, "group_by": []}
    opts.update(AM_DEFAULT_TIMING)
    for node in nodes:
        if node.get("receiver"):
            opts["receiver"] = node["receiver"]
        if isinstance(node.get("group_by"), list):
            opts["group_by"] = list(node["group_by"])
        for key in _TIMING_KEYS:
            if node.get(key):
                opts[key] = str(node[key])
    return opts


def run_amtool_trace(am_yml: str, root: dict, labels: dict[str, str], *,
                     warn) -> tuple[list[dict] | None, str]:
    """Ask Alertmanager where an alert with *labels* goes.

    Writes *am_yml* (the assembled alertmanager.yml, *root* its parsed route)
    to a private temp file and runs ``amtool config routes show`` and
    ``amtool config routes test --tree`` on it. amtool's output is read as
    bytes, decoded as UTF-8 and split on ``\n`` only: a CR, FF or U+2028 in
    some tenant's matcher value is part of a line, not a line break. Returns
    one dict per delivery (a leaf of the ``--tree`` output), in Alertmanager's
    order: ``receiver``,
    ``labels`` (amtool's matcher rendering along the path, root excluded) and
    ``nodes`` (the rendered route dicts along it, root included, or None when
    the path could not be looked up — *warn* says why), plus ``""``. When
    amtool is absent or gave no verdict: ``(None, reason)``, *warn* called.

    Not handled, all fail-safe (a WARN or an unknown, never a wrong verdict):
    an ``~/.config/amtool`` user config changing amtool's defaults, a tenant
    name containing a comma, a long WARN, and the fake amtool on Windows.
    """
    amtool = shutil.which("amtool")
    if amtool is None:
        warn(AMTOOL_MISSING)
        return None, "amtool not found"
    with tempfile.TemporaryDirectory(prefix="explain-route-") as tmp:
        path = Path(tmp) / "alertmanager.yml"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(am_yml)
        os.chmod(path, 0o600)  # receiver URLs / keys live in here
        cfg = f"--config.file={path}"
        show = _run_binary([amtool, "config", "routes", "show", cfg],
                           timeout=AMTOOL_TIMEOUT_S, text=False)
        test = _run_binary(
            [amtool, "config", "routes", "test", "--tree", cfg]
            + [amtool_label_arg(k, v) for k, v in labels.items()],
            timeout=AMTOOL_TIMEOUT_S, text=False)
    out = {}
    for what, res in (("routes show", show), ("routes test --tree", test)):
        text = res.stdout.decode("utf-8", "replace")
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", "replace")
            warn(f"amtool config {what} failed (rc={res.returncode}), so "
                 f"Alertmanager did not route this alert: {(text + err).strip()}")
            return None, "amtool gave no verdict"
        out[what] = text
    try:
        shown = parse_amtool_tree(out["routes show"])
        tested = parse_amtool_tree(out["routes test --tree"])
    except AmtoolOutputError as exc:
        warn(f"cannot read amtool's route tree: {exc}")
        return None, "amtool output not understood"
    leaves = _leaf_paths(tested)
    aligned: dict[int, dict] = {}
    if not _align(shown, root, aligned):
        warn("amtool's route tree does not have the shape of the rendered "
             "tree; timings are not looked up")
        aligned = {}
    hits = []
    for leaf in leaves:
        nodes = None
        if aligned:
            chains = _resolve(shown, leaf)
            if len(chains) == 1:
                nodes = [aligned[id(n)] for n in chains[0]]
            else:
                warn(f"route path {[n['label'] for n in leaf]} maps to "
                     f"{len(chains)} rendered routes; timings not looked up")
        hits.append({"receiver": leaf[-1]["receiver"],
                     "labels": [n["label"] for n in leaf[1:]], "nodes": nodes})
    return hits, ""


def _conf_receiver_types(routing_configs: dict[str, dict],
                         enforced: dict | None,
                         tenants: Iterable[str]) -> dict[str, str]:
    """Receiver name → the ``receiver.type`` it was generated from (G1).

    *tenants* is the generator's tenant set (``dedup_configs`` keys): a
    ``{{tenant}}`` enforced routing renders a receiver for each (#2519).

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
            for tenant in enforced_route_tenants(routing_configs, tenants):
                sub = _substitute_tenant(enforced, tenant)
                types[f"platform-enforced-{tenant}"] = _t(sub.get("receiver"))
        else:
            types["platform-enforced"] = _t(enforced.get("receiver"))
    return {k: v for k, v in types.items() if v}


def _receiver_type(name: str, receivers: dict[str, dict],
                   conf_types: dict[str, str]) -> str:
    """conf.d ``receiver.type``; for a receiver not generated from conf.d (the
    base config's, the platform placeholders) the AM integration it carries,
    or ``none`` for a receiver that notifies no one — name-only, or with only
    empty ``*_configs`` lists (#2660: the same predicate as the generator's
    root-receiver WARN)."""
    if name in conf_types:
        return conf_types[name]
    kinds = receiver_integration_kinds(receivers.get(name, {}))
    return "+".join(kinds) if kinds else "none"


INHIBIT_NOTE = ("Inhibit rules of the assembled config, verbatim; whether one "
                "suppresses this alert depends on which alerts are firing at "
                "the same time, which the trace does not evaluate")
INHIBIT_NOTE_ZH = ("組好設定裡的 inhibit rules（原文）；是否抑制取決於執行時同時 "
                   "firing 的告警，trace 不評估")


class UnassemblableBase(ValueError):
    """``assemble_configmap`` crashed on the --base-config (not a refusal)."""


def build_trace_tree(parsed: dict, base: dict | None = None
                     ) -> tuple[str, dict, dict[str, dict], dict[str, str]]:
    """The alertmanager.yml ``--output-configmap`` would emit, and its tree.

    Same pipeline as the generator — the four-layer merge, ``generate_routes``
    with the enforced routing, the severity-dedup inhibit rules — assembled by
    the generator's own ``assemble_configmap`` on *base* (the built-in base
    when None, as ``--validate`` uses), so ``global`` / ``templates`` /
    ``inhibit_rules`` are the ones Alertmanager would load. Raises ValueError
    where the generator refuses to assemble.

    Returns (alertmanager.yml text, its root route, receivers by name,
    conf.d receiver types by name).
    """
    routing_configs = _merge_tenant_routing(
        parsed, parsed.get("routing_defaults") or {})
    enforced = parsed.get("enforced_routing")
    if not (isinstance(enforced, dict) and enforced.get("enabled") is not False):
        enforced = None
    tenants = parsed.get("dedup_configs") or {}
    routes, receivers, _warnings = generate_routes(
        routing_configs, None, enforced_routing=enforced, tenants=tenants)
    inhibit_rules, _warnings = generate_inhibit_rules(tenants)
    try:
        cm_yaml = assemble_configmap(
            base if base is not None else load_base_config(None),
            routes, receivers, inhibit_rules)
    except (KeyError, TypeError) as exc:
        # It crashes rather than refuses on a malformed --base-config (a
        # receiver without `name`, `receivers` not a list). Only this call:
        # the trace's own bugs must surface, not read as a bad base.
        raise UnassemblableBase(f"{type(exc).__name__}: {exc}") from exc
    am_yml = yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"]
    am = yaml.safe_load(am_yml)
    by_name = {r["name"]: r for r in am.get("receivers") or []}
    return am_yml, am["route"], by_name, _conf_receiver_types(
        routing_configs, enforced, tenants)


def summarize_tree(root: dict) -> list[str]:
    """One line per rendered route, indented by depth — what the trace can
    show when Alertmanager cannot be asked."""
    lines: list[str] = []

    def _walk(node: dict, depth: int) -> None:
        matchers = ", ".join(str(m) for m in node.get("matchers") or [])
        cont = " (continue)" if node.get("continue") else ""
        head = "root" if depth == 0 else f"{{{matchers}}}"
        lines.append(f"{'  ' * depth}{head} → {node.get('receiver')}{cont}")
        for child in node.get("routes") or []:
            _walk(child, depth + 1)

    _walk(root, 0)
    return lines


def _level_policies_for_tenant(domain_policies: dict, tenant: str
                               ) -> tuple[list[tuple[str, set, set]], list[str]]:
    """The policies of ONE level's ``domain_policies`` that list *tenant*,
    read the way the generator's ``check_domain_policies`` reads them
    (non-strict). Returns ``(applicable, inert)`` as ``_policies_for_tenant``.
    """
    applicable, inert = [], []
    for name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            inert.append(f"'{name}': 'tenants' is not a list, so the policy "
                         f"applies to no tenant")
            continue
        if tenant not in tenants:
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            inert.append(f"'{name}' lists '{tenant}' but its 'constraints' "
                         f"is not a mapping, so nothing is enforced")
            continue

        def _types(field: str) -> set:
            raw = constraints.get(field)
            return set(raw) if isinstance(raw, list) else set()

        applicable.append((name, _types("forbidden_receiver_types"),
                           _types("allowed_receiver_types")))
    return applicable, inert


def _policies_for_tenant(parsed: dict, tenant: str
                         ) -> tuple[list[tuple[str, set, set]], list[str]]:
    """Domain policies for *tenant*, at every conf.d level (#2435).

    The levels and their reach are the generator's own
    (``domain_policy_levels`` / ``policy_reaches``, #2326 (d)): the root's
    policies reach every tenant, a subtree's only the tenants of that
    subtree. A subtree policy that names *tenant* from outside its subtree
    is not enforced — the generator's ``check_policy_scope`` finding, in its
    own words, becomes a note.

    Returns ``(applicable, inert)``: ``applicable`` is ``(name, forbidden
    types, allowed types)`` of every reaching policy whose ``tenants`` list
    names the tenant; ``inert`` says, per policy, why one that may concern
    the tenant is not applied (``tenants`` not a list, ``constraints`` not a
    mapping, out of its subtree's scope).
    """
    tenant_dirs = parsed.get("tenant_dirs") or {}
    applicable, inert = [], []
    for level, policies in domain_policy_levels(parsed):
        if policy_reaches(level, tenant, tenant_dirs):
            got, why = _level_policies_for_tenant(policies, tenant)
            applicable.extend(got)
            inert.extend(why)
            continue
        msgs, rows = check_policy_scope(
            level, policies, tenant_dirs,
            source=policy_level_source(parsed, level))
        for msg, (_domain, named) in zip(msgs, rows):
            if named == tenant:
                inert.append(msg.strip().split(": ", 1)[-1])
    return applicable, inert


def _policy_step(parsed: dict, tenant: str, tenant_types: list[str] | None
                 ) -> dict:
    """Step 5: the receiver-type constraints of the policies that list
    *tenant*, against the tenant receiver(s) Alertmanager delivers to.
    ``tenant_types`` None means the delivery is unknown (no amtool)."""
    applicable, inert = _policies_for_tenant(parsed, tenant)
    step: dict = {"step": 5, "action": "policy_check"}
    if inert:
        step["notes"] = inert
    typed = [(n, f, a) for n, f, a in applicable if f or a]
    if not typed:
        step["detail"] = (
            f"No receiver-type constraint applies to tenant '{tenant}'"
            + (" (the policies listing it carry other constraints, which "
               "--trace does not check)" if applicable else
               " (no usable domain policy lists it)" if inert else
               " (no domain policy lists it)"))
        step["passed"] = True
        return step
    if tenant_types is None:
        step["detail"] = ("Receiver-type constraints not evaluated: the "
                          "delivered receiver is unknown")
        step["passed"] = None
        return step
    if not tenant_types:
        step["detail"] = ("No tenant receiver on the alert's path — "
                          "receiver-type constraints do not apply")
        step["passed"] = True
        return step
    issues = []
    for name, forbidden, allowed in typed:
        for rtype in tenant_types:
            if forbidden and rtype in forbidden:
                issues.append(f"Domain '{name}' forbids receiver type '{rtype}'")
            if allowed and rtype not in allowed:
                issues.append(f"Domain '{name}' only allows {sorted(allowed)}, "
                              f"got '{rtype}'")
    if issues:
        step.update(detail="Receiver-type constraint violated",
                    violations=issues, passed=False)
    else:
        step.update(detail=("Receiver-type constraints passed "
                            f"({', '.join(n for n, _f, _a in typed)}); other "
                            "policy constraints are not checked by --trace"),
                    passed=True)
    return step


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

    Renders the WHOLE route tree (``build_trace_tree``) and asks Alertmanager
    (``amtool config routes test --tree``) where the alert goes (#2293):
      1. The tenant's merged routing config
      2. The route path(s) and receiver(s) Alertmanager delivers to
      3. Whether the enforced NOC route takes a copy (only when it matches)
      4. The inhibit rules of the assembled config, verbatim (not evaluated:
         suppression depends on which alerts fire at runtime)
      5. Receiver-type constraints of the domain policies listing the tenant

    Without ``amtool`` on PATH the delivery is unknown: a WARN is printed,
    the receiver reads ``(unknown: amtool not found)`` and step 2 carries a
    summary of the rendered tree instead.

    ``extra_labels`` adds labels to the alert (e.g. ``metric_group`` for an
    ``overrides`` entry, or a ``routes`` match key). A key the trace sets
    itself (``alertname`` / ``severity`` / ``tenant``) raises ValueError
    rather than silently replacing the dedicated argument (#2264).
    ``base_config`` is the loaded ``--base-config`` (None → built-in base).

    Returns dict with keys:
        tenant, alertname, severity, labels, steps, final_receiver, timing
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

    # Step 2: Alertmanager's verdict on the assembled config (#2293)
    def _warn(msg: str) -> None:
        print(f"  WARN: {safe_label(msg)}", file=sys.stderr)

    hits, unknown = None, "the generator refuses this config"
    root, receivers, conf_types = {}, {}, {}
    assembled = False
    # The generator refuses these trees before building anything (unreadable
    # tenant file #1460, duplicate tenant #2315, routing-tree error #2326,
    # invalid tenant id ADR-035 D3): same judgment, same words, same order.
    refusal_rc, refusal = tree_refusal(
        parsed.get("files_read", 0),
        parsed.get("tenant_file_errors") or [],
        parsed.get("duplicate_tenants") or {},
        parsed.get("routing_tree_problems") or [],
        parsed.get("invalid_tenant_ids") or [])
    if refusal:
        _warn("the generator refuses this config "
              f"(generate_alertmanager_routes.py exits {refusal_rc}):")
        for msg in refusal:
            _warn(msg)
    else:
        try:
            am_yml, root, receivers, conf_types = build_trace_tree(
                parsed, base_config)
        except UnassemblableBase as exc:
            _warn("the generator cannot assemble this config (malformed "
                  f"--base-config?): {exc}")
        except ValueError as exc:
            _warn(f"the generator refuses to assemble this config: {exc}")
        else:
            assembled = True
            hits, unknown = run_amtool_trace(am_yml, root, alert_labels,
                                             warn=_warn)
    main_prefix = f"tenant-{tenant}"

    def _is_enforced(hit: dict) -> bool:
        return str(hit["receiver"]).startswith("platform-enforced")

    def _path(hit: dict) -> str:
        return " → ".join(hit["labels"]) or "root (no route matched)"

    if hits is None:
        step2 = {
            "step": 2, "action": "match_receiver",
            "detail": (f"Alert labels: {alert_labels} — not validated by "
                       f"Alertmanager ({unknown})"),
            "receiver_type": "", "route_path": "",
            "receiver_desc": f"(unknown: {unknown})",
            "matched_routes": [],
            "rendered_tree": summarize_tree(root),
        }
        steps.append(step2)
        steps.append({"step": 3, "action": "enforced_routing",
                      "detail": "Unknown: Alertmanager was not asked"})
        tenant_types = None
        timing = {key: None for key in _TIMING_KEYS}
    else:
        for hit in hits:
            hit["receiver_type"] = _receiver_type(hit["receiver"], receivers,
                                                  conf_types)
        enforced_hits = [h for h in hits if _is_enforced(h)]
        primary = [h for h in hits if not _is_enforced(h)]
        # Only continue:true (enforced) routes matched: they ARE the delivery
        # — Alertmanager does NOT fall back to the root receiver then.
        top = (primary or enforced_hits)[0]
        where = _path(top)
        matched_sub = None
        nodes = top["nodes"] or []
        if (len(nodes) == 3 and nodes[1].get("receiver") == main_prefix
                and f'tenant="{tenant}"' in (nodes[1].get("matchers") or [])):
            name = top["receiver"]
            for kind, field in (("override", "overrides"), ("route", "routes")):
                head = f"{main_prefix}-{kind}-"
                if name.startswith(head) and name[len(head):].isdigit():
                    matched_sub = f"{field}[{name[len(head):]}]"
            if matched_sub:
                where = f"sub-route {matched_sub}"
        if not primary:
            where += "; only the enforced route matched, no root fallback"
        step2 = {
            "step": 2, "action": "match_receiver",
            "detail": f"Alert labels: {alert_labels}",
            "receiver_type": top["receiver_type"],
            "receiver_desc": f"{top['receiver_type']} → {top['receiver']} ({where})",
            "route_path": _path(top),
            "matched_routes": [
                {"receiver": h["receiver"], "receiver_type": h["receiver_type"],
                 "route_path": _path(h)} for h in hits],
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
                "step": 3, "action": "enforced_routing",
                "detail": "NOC enforced route matches — additional notification",
                "enforced_receiver": ", ".join(
                    f"{h['receiver_type']} → {h['receiver']}"
                    for h in enforced_hits),
            })
        elif has_enforced:
            steps.append({
                "step": 3, "action": "enforced_routing",
                "detail": ("NOC enforced route configured but not reached by "
                           "this alert (its matchers exclude it, or an earlier "
                           "continue:false route took the alert)"),
            })
        else:
            steps.append({"step": 3, "action": "enforced_routing",
                          "detail": "No enforced routing active"})
        tenant_types = [h["receiver_type"] for h in hits
                        if h["receiver"] == main_prefix
                        or h["receiver"].startswith(main_prefix + "-")]
        if top["nodes"] is None:
            timing = {key: None for key in _TIMING_KEYS}
        else:
            opts = _inherited(top["nodes"])
            timing = {key: opts[key] for key in _TIMING_KEYS}

    # Step 4: the inhibit rules Alertmanager would load, verbatim (#2293).
    # Whether one suppresses this alert depends on which alerts are firing
    # at the same time — the trace does not evaluate it.
    if not assembled:
        steps.append({"step": 4, "action": "inhibit_rules",
                      "detail": ("Unknown: inhibit rules not available "
                                 f"({unknown})"),
                      "inhibit_rules_yaml": None})
    else:
        # As YAML text, dumped like the generator writes alertmanager.yml:
        # a base rule may hold values JSON cannot carry (dates, !!binary).
        rules = yaml.safe_load(am_yml).get("inhibit_rules") or []
        steps.append({
            "step": 4, "action": "inhibit_rules",
            "detail": INHIBIT_NOTE,
            "inhibit_rules_yaml": yaml.dump(rules, default_flow_style=False,
                                            allow_unicode=True,
                                            sort_keys=False),
        })

    # Step 5: receiver-type constraints, scoped like the generator's check
    steps.append(_policy_step(parsed, tenant, tenant_types))

    return {
        "tenant": tenant,
        "alertname": alertname,
        "severity": severity,
        "labels": alert_labels,
        "steps": steps,
        "final_receiver": steps[1]["receiver_desc"],
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
        "inhibit_rules": "🚫",
    }
    # #2435: the policy step's icon is its verdict, not its kind — a
    # violation under ✅ read as a pass. ⚠ rather than ❌: --trace reads the
    # policies the way the generator's non-strict run does (WARN, rc 0), and
    # the violation lines under it carry ⚠ already. ❔ = not evaluated (the
    # delivered receiver is unknown, e.g. no amtool).
    policy_icons = {True: "✅", False: "⚠", None: "❔"}

    for step in trace["steps"]:
        if step["action"] == "policy_check":
            icon = policy_icons.get(step.get("passed"), "▸")
        else:
            icon = step_icons.get(step["action"], "▸")
        action_label = step["action"].replace("_", " ").title()
        lines.append(f"  {icon} Step {step['step']}: {action_label}")
        detail = step["detail"]
        if detail == INHIBIT_NOTE and lang == "zh":
            detail = INHIBIT_NOTE_ZH
        lines.append(f"     {safe_label(detail)}")
        if step.get("route_path"):
            label = "路徑:" if lang == "zh" else "Path:"
            lines.append(f"     {label} {safe_label(step['route_path'])}")
        if "receiver_desc" in step:
            label = "接收者:" if lang == "zh" else "Receiver:"
            lines.append(f"     {label} {safe_label(step['receiver_desc'])}")
        if step.get("rendered_tree"):
            label = ("產出的路由樹（未經 Alertmanager 判定）:" if lang == "zh"
                     else "Rendered route tree (not evaluated by Alertmanager):")
            lines.append(f"     {label}")
            for row in step["rendered_tree"]:
                lines.append(f"       {safe_label(row)}")
        if step.get("inhibit_rules_yaml") is not None:
            label = ("生效的 inhibit rules:" if lang == "zh"
                     else "Effective inhibit rules:")
            lines.append(f"     {label}")
            rows = step["inhibit_rules_yaml"].splitlines()
            for row in ["(none)"] if rows == ["[]"] else rows:
                lines.append(f"       {safe_label(row)}")
        if "enforced_receiver" in step:
            label = "強制接收者:" if lang == "zh" else "Enforced:"
            lines.append(f"     {label} {safe_label(step['enforced_receiver'])}")
        for note in step.get("notes") or []:
            lines.append(f"     ℹ {safe_label(note)}")
        if step.get("violations"):
            for v in step["violations"]:
                lines.append(f"     ⚠ {safe_label(v)}")
        lines.append("")

    # Final summary
    receiver = safe_label(trace["final_receiver"])
    if lang == "zh":
        lines.append("── 最終結果 ──")
        lines.append(f"  接收者: {receiver}")
    else:
        lines.append("── Final Result ──")
        lines.append(f"  Receiver: {receiver}")

    t_info = {k: ("(unknown)" if v is None else v)
              for k, v in trace["timing"].items()}
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
        "zh": "追蹤模式：由 Alertmanager（amtool）判定 alert 的路由路徑"
              "（需搭配 --tenant；需 PATH 上有 amtool）",
        "en": "Trace mode: the alert's routing path as Alertmanager (amtool) "
              "decides it (requires --tenant; needs amtool on PATH)",
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
        "zh": "追蹤用的 base Alertmanager YAML（route.routes 會被產生的路由整份"
              "取代）。只在 --trace 下讀取；"
              "未給時用內建 base（與 generate_alertmanager_routes --validate 相同）",
        "en": "Base Alertmanager YAML for the trace (route.routes is replaced "
              "by the generated routes). Read only with --trace; omitted → the built-in "
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
    # #2326: a tree the generator refuses outright (rc 2) is still explained
    # here — this is a diagnostic — but never without saying so first.
    # ⛔ Not under --trace: the trace asks the generator itself
    # (`tree_refusal`) and prints the refusal it would print, verbatim, with
    # the verdict set to unknown (#2293). Warning here too would say the same
    # refusal twice, in two different spellings.
    if not args.trace:
        for kind, _f, _fld, msg in parsed.get("routing_tree_problems", []):
            if kind in BLOCKING_TREE_KINDS:
                print(f"  {ROUTING_TREE_ERROR_PREFIX} {safe_label(msg)} — "
                      f"generate-routes refuses this tree", file=sys.stderr)
        # #2315 owns the duplicate-tenant refusal (rc 1, not a routing-tree
        # kind), so it is named from its own record — the same lines the
        # generator prints.
        for line in duplicate_tenant_errors(
                parsed.get("duplicate_tenants", {})):
            print(f"{safe_label(line)} — generate-routes refuses this tree",
                  file=sys.stderr)
        # #2341 R5: a `_routing_defaults` that is not a mapping (blocking in
        # generate-routes --validate; the level contributes nothing here too).
        for fname, value in parsed.get("routing_defaults_not_mapping", []):
            print(f"  WARN: {safe_label(routing_defaults_not_mapping_text(fname, value))}",
                  file=sys.stderr)
        # #2341 R8: a tenant id generate-routes renders nothing for (it is
        # not in all_tenants, so it is not explained below either) — and,
        # ADR-035 D3, refuses the whole tree for, in every mode.
        for tenant in sorted(set(parsed.get("invalid_tenant_ids", [])), key=str):
            print(f"  WARN: {safe_label(invalid_tenant_id_text(tenant))} — "
                  f"generate-routes refuses this tree", file=sys.stderr)

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
    refused = parsed.get("routing_refused", {})
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
        if t in refused:
            # #2341 R5: generate-routes renders no route for this tenant.
            print(safe_label(routing_not_mapping_warning(t, refused[t])),
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
