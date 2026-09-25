#!/usr/bin/env python3
"""patch_config.py — Patch threshold-config ConfigMap for a specific tenant.

Supports both legacy (single config.yaml key) and multi-file (per-tenant YAML keys)
ConfigMap structures: a '_defaults' key (.yaml/.yml, any casing) means
multi-file, else 'config.yaml' means legacy. The tenant's key is the one whose
`tenants:` declares it. This tool's reading of the ConfigMap is not the
exporter's; known divergences are tracked in #1950.

Usage: patch_config.py <tenant> <metric_key> <value>
       patch_config.py --diff [--json] <tenant> <metric_key> <value>

Three-state logic (for keys `_defaults.yaml` gives a value to, under `defaults:`):
  - Custom value:  patch_config.py db-a mysql_connections 50
  - Default (delete key): patch_config.py db-a mysql_connections default
  - Disable:       patch_config.py db-a mysql_connections disable

  ⚠️ For a key `_defaults.yaml` only DECLARES (its `optional_overrides:` list)
  there is no platform value to fall back to, so `default` does not restore one
  — deleting the key means no value and no series at all (#1321).

Diff preview (terraform plan analogy):
  - patch_config.py --diff db-a mysql_connections 50
  Shows before/after comparison without applying any changes.

Dimensional metrics (Phase 2B):
  - patch_config.py db-a 'redis_queue_length{queue="tasks"}' 500
  - patch_config.py db-a 'redis_db_keys{db="db0"}' disable
  Note: Shell quoting is important — wrap the metric key in single quotes.
"""
import argparse
import contextlib
import subprocess
import yaml
import sys
import json
import tempfile
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_confd import (  # noqa: E402
    has_yaml_extension,
    is_defaults_name,
    is_hidden_name,
    is_reserved_name,
)

LEGACY_KEY = "config.yaml"
_NULL = "tag:yaml.org,2002:null"
_MERGE = "tag:yaml.org,2002:merge"


class ConfigMapShapeError(Exception):
    """The ConfigMap cannot be read unambiguously; refused instead of guessed."""


class _Unparseable(ConfigMapShapeError):
    """Locating skips a key whose read raises this."""


class KubectlError(Exception):
    """kubectl could not be run, failed, or printed something that is not JSON."""


def run_cmd(cmd):
    """Execute a command safely using list arguments (no shell=True)."""
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KubectlError(f"Error executing: {' '.join(cmd)}\n{exc}") from exc
    if result.returncode != 0:
        raise KubectlError(f"Error executing: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def _cm_keys(cm_data):
    data = cm_data.get("data") if isinstance(cm_data, dict) else cm_data
    if not isinstance(data, (dict, type(None))):
        raise ConfigMapShapeError("threshold-config `data` is not a mapping.")
    return data or {}


def read_node(text, label, strict=True):
    """Root node of the FIRST document in `text` (None if there is none);
    later documents are not read. Refused: a syntax error in what is read,
    nesting too deep to walk, and — when `strict` — a duplicate key in any
    mapping."""
    try:
        root = next(yaml.compose_all(text or "", Loader=yaml.SafeLoader), None)
        if strict:
            _refuse_duplicate_keys(root, label, set())
    except yaml.YAMLError as exc:
        raise _Unparseable(f"{label} does not parse as YAML: {exc}") from exc
    except RecursionError as exc:
        raise _Unparseable(f"{label} is nested too deeply.") from exc
    return root


def _refuse_duplicate_keys(node, label, seen):
    if id(node) in seen:  # an alias is the same node object
        return
    seen.add(id(node))
    children = []
    if isinstance(node, yaml.MappingNode):
        names = set()
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode):
                if k.value in names:
                    raise ConfigMapShapeError(
                        f"{label} has the key {k.value!r} twice in one mapping.")
                names.add(k.value)
            children += [k, v]
    elif isinstance(node, yaml.SequenceNode):
        children = node.value
    for child in children:
        _refuse_duplicate_keys(child, label, seen)


def _entries(node, label, strict=True):
    """{key text: value node} of a mapping node, {} for any other node. A null
    key is skipped; so is a non-scalar key unless `strict`. Refused: a merge
    (`<<`) key, a duplicate key, and — when `strict` — a non-scalar key."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    out, dup = {}, None
    for k, v in node.value:
        if not isinstance(k, yaml.ScalarNode):
            if strict:
                raise ConfigMapShapeError(f"{label} has a non-scalar mapping key.")
            continue
        if k.tag == _MERGE:
            raise ConfigMapShapeError(
                f"{label} has a merge key `<<` on the path being read.")
        dup = k.value if k.value in out else dup
        if k.tag != _NULL:
            out[k.value] = v
    if dup is not None:
        raise _Unparseable(f"{label} has the key {dup!r} twice in one mapping.")
    return out


def _is_null(node):
    return isinstance(node, yaml.ScalarNode) and node.tag == _NULL


def _mapping_or_null(node):
    return node is None or _is_null(node) or isinstance(node, yaml.MappingNode)


def _text(node):
    """Source text of a scalar (None for null); YAML text of a collection."""
    if isinstance(node, yaml.ScalarNode):
        return None if _is_null(node) else node.value
    return yaml.serialize(node)


def find_defaults_key(data):
    """The `_defaults` key in any casing / either spelling (`is_defaults_name`),
    or None; two such keys → refused."""
    hits = sorted(k for k in data if is_defaults_name(k))
    if len(hits) > 1:
        raise ConfigMapShapeError(
            f"threshold-config has {len(hits)} keys that are all the platform "
            f"defaults file ({', '.join(hits)}); keep exactly one.")
    return hits[0] if hits else None


def detect_mode(cm_data):
    """'multi-file' if a `_defaults` key exists, 'legacy' if only `config.yaml`
    does. Refused: neither."""
    data = _cm_keys(cm_data)
    if find_defaults_key(data) is not None:
        return "multi-file"
    if LEGACY_KEY in data:
        return "legacy"
    raise ConfigMapShapeError(
        "threshold-config has neither a _defaults.yaml/.yml key (multi-file "
        f"layout) nor a {LEGACY_KEY} key (legacy layout); refusing to guess "
        f"its schema. Keys present: {sorted(data) or '(none)'}")


def is_carrier_candidate(key):
    return (has_yaml_extension(key) and not is_hidden_name(key)
            and not is_reserved_name(key))


def tenant_declarations(cm_data, skipped=None):
    """tenant id → set of keys whose `tenants:` declares it. Only the root and
    `tenants:` mappings are checked here; a target key is read strictly. A key
    that does not parse, or repeats a key there, is refused, or — given a
    `skipped` list — declares nothing and is added to it; a `<<` there is
    refused."""
    data = _cm_keys(cm_data)
    declared = {}
    for key in sorted(k for k in data if is_carrier_candidate(k)):
        try:
            root = read_node(data[key], key, strict=False)
            tenants = _entries(root, key, strict=False).get("tenants")
            ids = _entries(tenants, key, strict=False)
        except _Unparseable as exc:
            if skipped is None:
                raise
            skipped.append(str(exc))
            continue
        for tenant in ids:
            declared.setdefault(tenant, set()).add(key)
    return declared


def locate_tenant_key(cm_data, tenant):
    """The ONE key declaring `tenant`, or None; refused if two keys declare it,
    or if none does and a key could not be read."""
    skipped = []
    keys = sorted(tenant_declarations(cm_data, skipped).get(tenant, ()))
    if len(keys) > 1:
        raise ConfigMapShapeError(
            f"tenant '{tenant}' is declared by {len(keys)} threshold-config "
            f"keys: {', '.join(keys)}. Decide which key owns the tenant and "
            f"remove it from the others before patching.")
    if not keys and skipped:
        raise ConfigMapShapeError(
            f"no readable threshold-config key declares tenant '{tenant}', "
            f"and it cannot be ruled out that one of these declares it: "
            f"{'; '.join(skipped)}")
    return keys[0] if keys else None


def tenant_block(cm_data, tenant):
    """(key, {metric: value node}) of the key declaring `tenant`, (None, {})
    if no key does. A null block is an empty one: still declared."""
    key = locate_tenant_key(cm_data, tenant)
    if key is None:
        return None, {}
    root = read_node(_cm_keys(cm_data)[key], key)
    block = _entries(_entries(root, key)["tenants"], key)[tenant]
    if not _mapping_or_null(block):
        raise ConfigMapShapeError(
            f"tenant '{tenant}' in {key} is not a mapping of metric keys; "
            f"refusing to read or overwrite it.")
    return key, _entries(block, key)


def _is_default(value):
    return str(value).lower() == "default"


def _patch_carrier(cm_data, tenant, metric_key, value, create_key,
                   shared_key=None):
    """Patch the key declaring `tenant`. None = no-op: `default` with no value
    to remove. An emptied block is kept: it still declares the tenant. A key
    declaring more than one tenant is refused, except `shared_key`."""
    key, block = tenant_block(cm_data, tenant)
    if _is_default(value) and metric_key not in block:
        return None
    if key is None:
        key = create_key
        if not is_carrier_candidate(key):
            raise ConfigMapShapeError(
                f"patch-config does not create {key}: it only uses YAML keys "
                f"that are not dot-hidden or `_`-prefixed as tenant keys.")
    old = _cm_keys(cm_data).get(key) or ""
    declared = set(_entries(_entries(read_node(old, key), key).get("tenants"), key))
    if key != shared_key and len(declared | {tenant}) > 1:
        raise ConfigMapShapeError(
            f"{key} would declare tenants {sorted(declared | {tenant})}; "
            f"patch-config only writes a key that declares one. Edit {key} "
            f"by hand.")
    # ⚠️ The rewrite re-serialises the key's other values with YAML 1.1 types
    # and drops its comments.
    doc = yaml.safe_load(old) or {}
    tenants = doc.get("tenants") if isinstance(doc, dict) else []
    if tenants is None:
        tenants = doc["tenants"] = {}
    if not isinstance(tenants, dict):
        raise ConfigMapShapeError(
            f"{key} or its `tenants:` is not a YAML mapping; refusing to "
            f"overwrite it.")
    own = tenants[tenant] = tenants.get(tenant) or {}
    if _is_default(value):
        own.pop(metric_key, None)
    else:
        own[metric_key] = str(value)
    new = yaml.dump(doc, sort_keys=False)
    after = set(_entries(_entries(read_node(new, key), key).get("tenants"), key))
    if after != declared | {tenant}:
        raise ConfigMapShapeError(
            f"rewriting {key} would change the tenants it declares from "
            f"{sorted(declared | {tenant})} to {sorted(after)}; refusing.")
    return {"data": {key: new}}


def patch_legacy(cm_data, tenant, metric_key, value):
    """Legacy mode: a tenant no key declares is added to 'config.yaml'."""
    return _patch_carrier(cm_data, tenant, metric_key, value, LEGACY_KEY,
                          shared_key=LEGACY_KEY)


def patch_multifile(cm_data, tenant, metric_key, value):
    """Multi-file mode: a tenant no key declares gets a new `<tenant>.yaml`."""
    return _patch_carrier(cm_data, tenant, metric_key, value,
                          create_key=f"{tenant}.yaml")


def read_platform_tiers(cm_data, mode):
    """(defaults_mapping, declared_key_list) — the platform side of the ConfigMap.

    ONE read path for both platform tiers, so nothing downstream has to re-parse
    the same document with its own idea of where the tiers live (#1321):

      * ``defaults``          — key → platform value text. The platform ASSERTS these.
      * ``optional_overrides`` — key NAMES only. The platform RECOGNISES these
        and asserts NO value; a tenant that does not set one gets no value at
        all, which is silence, not a fallback.

    Legacy mode keeps both tiers in the single ``config.yaml``; multi-file mode
    in the ``_defaults`` key.
    """
    data = _cm_keys(cm_data)
    key = LEGACY_KEY if mode == "legacy" else find_defaults_key(data)
    root = read_node(data.get(key), key)
    if not _mapping_or_null(root):
        raise ConfigMapShapeError(f"{key} is not a YAML mapping.")
    top = _entries(root, key)
    defaults = {k: _text(v) for k, v in _entries(top.get("defaults"), key).items()}
    listed = top.get("optional_overrides")
    declared = ([n.value for n in listed.value if isinstance(n, yaml.ScalarNode)]
                if isinstance(listed, yaml.SequenceNode) else [])
    return defaults, declared


def get_current_value(cm_data, mode, tenant, metric_key):
    """Get the current value of a metric key from the ConfigMap.

    Returns (current_value, source) where source is 'tenant', 'defaults',
    'declared', or 'none'. The value is source text (`_text`); null is None.

    ``'declared'`` (#1321) is the tier the platform recognises but assigns no
    value to. It is deliberately NOT folded into ``'none'``: for a declared key
    the tenant may set a value and it takes effect, while ``'none'`` means the
    platform does not know the key at all — the only two states that look alike
    from the outside (both currently valueless) but differ in what the tenant
    can do about it.
    """
    _, own = tenant_block(cm_data, tenant)
    if metric_key in own:
        return _text(own[metric_key]), "tenant"

    defaults_section, declared = read_platform_tiers(cm_data, mode)
    if metric_key in defaults_section:
        return defaults_section[metric_key], "defaults"
    if metric_key in declared:
        return None, "declared"

    return None, "none"


def find_affected_alerts(metric_key):
    """Identify alert rules that reference this metric.

    Returns list of alert rule names (best-effort, based on naming convention).
    """
    # Strip dimensional suffix for matching
    base_metric = metric_key.split("{")[0] if "{" in metric_key else metric_key

    # Common alert naming patterns based on metric names
    alerts = []
    parts = base_metric.split("_")

    # Build likely alert name patterns
    # e.g., mysql_connections → MariaDBHighConnections
    # e.g., container_cpu → PodContainerHighCPU
    if len(parts) >= 2:
        # CamelCase conversion
        camel = "".join(p.capitalize() for p in parts)
        alerts.append(f"*{camel}*")

    return alerts


# ---------------------------------------------------------------------------
# Valueless-state wording, per platform tier (#1321)
# ---------------------------------------------------------------------------
_AFTER_KEY_REMOVED = {
    "declared": ("no value (key removed — the platform DECLARES this key but "
                 "asserts no value, so nothing falls back: the metric goes "
                 "silent)"),
    "platform-default": "default (key removed)",
    "unknown": ("no value (key removed — no platform default for this key to "
                "fall back to)"),
}
_BEFORE_NOT_SET = {
    "declared": ("no value (declared key, not set — the platform asserts no "
                 "value here, so it is silent)"),
    "platform-default": "default (not set)",
    "unknown": "no value (not set — no platform default for this key)",
}
_NO_VALUE_DISPLAY = {
    "declared": "(no value — declared, unset)",
    "platform-default": "(platform default)",
    "unknown": "(no value)",
}


def diff_preview(cm_data, mode, tenant, metric_key, value):
    """Show before/after preview of a config change without applying it.

    Returns dict with diff details.
    """
    current_value, source = get_current_value(cm_data, mode, tenant, metric_key)
    affected_alerts = find_affected_alerts(metric_key)

    # Which platform tier the key sits in decides every valueless-state string
    # below (see the tables above). Read once, from the same path
    # `get_current_value` used.
    platform_defaults, declared_keys = read_platform_tiers(cm_data, mode)
    if metric_key in declared_keys:
        tier = "declared"
    elif source == "defaults" or metric_key in platform_defaults:
        # Sourced from `defaults:`, or merely landing there once the tenant key
        # goes away — either way deletion has something to fall back to.
        tier = "platform-default"
    else:
        tier = "unknown"

    # Determine new state description
    new_value = value
    if str(value).lower() == "default":
        new_state = _AFTER_KEY_REMOVED[tier]
        new_value = _NO_VALUE_DISPLAY[tier]
    elif str(value).lower() in ("disable", "disabled", "off", "false"):
        new_state = "disabled"
    else:
        new_state = f"custom: {value}"

    # Determine old state description
    if current_value is None:
        old_state = _BEFORE_NOT_SET[tier]
        old_display = _NO_VALUE_DISPLAY[tier]
    elif str(current_value).lower() in ("disable", "disabled", "off", "false"):
        old_state = "disabled"
        old_display = str(current_value)
    elif source == "defaults":
        old_state = f"platform default: {current_value}"
        old_display = str(current_value)
    else:
        old_state = f"custom: {current_value}"
        old_display = str(current_value)

    # Build diff result
    diff = {
        "tenant": tenant,
        "metric_key": metric_key,
        "configmap_mode": mode,
        "before": {"value": current_value, "source": source, "state": old_state},
        "after": {"value": new_value if str(value).lower() != "default" else None,
                  "state": new_state},
        # The same patch apply would send: unchanged iff none.
        "changed": build_patch(cm_data, mode, tenant, metric_key,
                               value) is not None,
        "affected_alerts": affected_alerts,
    }

    return diff


def print_diff(diff):
    """Print human-readable diff preview."""
    print()
    print("=" * 55)
    print("  Config Change Preview (--diff)")
    print("=" * 55)
    print()
    print(f"  Tenant:   {diff['tenant']}")
    print(f"  Metric:   {diff['metric_key']}")
    print(f"  Mode:     {diff['configmap_mode']}")
    print()

    before = diff["before"]
    after = diff["after"]

    if diff["changed"]:
        print(f"  - Before: {before['state']}  (source: {before['source']})")
        print(f"  + After:  {after['state']}")
    else:
        print(f"    No change (already: {before['state']})")

    if diff["affected_alerts"]:
        print()
        print(f"  Affected alerts (pattern): {', '.join(diff['affected_alerts'])}")

    print()
    if diff["changed"]:
        print("  To apply: remove --diff flag and re-run.")
    print()


def build_patch(cm_data, mode, tenant, metric_key, value):
    """The merge patch apply sends, or None for a no-op."""
    own = tenant_block(cm_data, tenant)[1]
    if (metric_key in own and _text(own[metric_key]) == str(value)
            and not _is_default(value)):
        return None
    patch = patch_legacy if mode == "legacy" else patch_multifile
    return patch(cm_data, tenant, metric_key, value)


def apply_patch(cm_data, mode, tenant, metric_key, value):
    """Build and apply the ConfigMap patch."""
    patch_data = build_patch(cm_data, mode, tenant, metric_key, value)

    if patch_data is None:
        print(f"No-op: nothing to change for tenant '{tenant}' key "
              f"'{metric_key}'; nothing was written.", file=sys.stderr)
        return

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json',
                                     encoding="utf-8", newline='\n') as temp:
        json.dump(patch_data, temp)
        temp_path = temp.name

    print(f"Patching ConfigMap ({mode}) for {tenant}: {metric_key} = {value}...")
    run_cmd(["kubectl", "patch", "configmap", "threshold-config",
             "-n", "monitoring", "--type", "merge", "--patch-file", temp_path])
    os.remove(temp_path)
    print("Success! Exporter will reload within its interval.")


def build_parser():
    """The CLI's argument parser."""
    parser = argparse.ArgumentParser(
        description="Patch threshold-config ConfigMap for a specific tenant",
    )
    parser.add_argument("tenant", help="Tenant name (e.g., db-a)")
    parser.add_argument("metric_key", help="Metric key to patch")
    parser.add_argument("value", help="New value, 'default', or 'disable'")
    parser.add_argument(
        "--diff", action="store_true",
        help="Preview change without applying (like terraform plan)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output diff as JSON (requires --diff)",
    )
    return parser


def _envelope(status, reason):
    """The diff keys emptied, plus `status`/`reason` (dev-rules §13)."""
    return format_json_report({
        "tenant": None, "metric_key": None, "configmap_mode": None,
        "before": None, "after": None, "changed": False,
        "affected_alerts": [], "status": status, "reason": reason})


def _refuse(as_json, reason, message):
    """Caller error: message on stderr and, under --json, one envelope."""
    print(f"ERROR: {message}", file=sys.stderr)
    if as_json:
        print(_envelope("caller_error", reason))
    sys.exit(EXIT_CALLER_ERROR)


def main():
    """CLI entry point: Patch threshold-config ConfigMap for a specific tenant."""
    as_json = any(len(a) > 2 and "--json".startswith(a.split("=", 1)[0])
                  for a in sys.argv[1:])
    try:
        with contextlib.redirect_stdout(sys.stderr if as_json else sys.stdout):
            args = build_parser().parse_args()
    except SystemExit as exc:
        if as_json and not exc.code:  # --help: its text went to stderr
            print(_envelope("help", None))
        elif as_json:
            _refuse(True, "bad_arguments", "arguments rejected (usage above)")
        raise

    if args.json and not args.diff:
        _refuse(True, "json_requires_diff",
                "--json requires --diff (it prints the diff preview as JSON). "
                "Without --diff this command APPLIES the change; refusing to "
                "apply while --json was requested.")

    try:
        try:
            cm_data = json.loads(run_cmd([
                "kubectl", "get", "configmap", "threshold-config",
                "-n", "monitoring", "-o", "json"]))
            if not isinstance(cm_data, dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            raise KubectlError(f"kubectl did not print JSON: {exc}") from exc
        mode = detect_mode(cm_data)

        if args.diff:
            # Preview mode: show diff without applying
            diff = diff_preview(cm_data, mode, args.tenant, args.metric_key,
                                args.value)
            if args.json:
                print(format_json_report(diff))
            else:
                print_diff(diff)
        else:
            print(f"Detected ConfigMap mode: {mode}")
            apply_patch(cm_data, mode, args.tenant, args.metric_key, args.value)
    except ConfigMapShapeError as exc:
        _refuse(args.json, "configmap_shape", exc)
    except KubectlError as exc:
        _refuse(args.json, "kubectl_failed", exc)
    except Exception as exc:  # noqa: BLE001 — every terminal path answers
        _refuse(args.json, "unexpected_error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
