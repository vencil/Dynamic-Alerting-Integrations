"""Configuration loading + parsing for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _parse_platform_config(...)   → defaults / _routing_defaults / profiles / policies
  _parse_tenant_overrides(...)  → per-tenant _routing / _severity_dedup / _metadata
  _parse_config_files(dir)      → walk YAML files → raw parsed dict
  _merge_tenant_routing(...)    → 4-layer merge (defaults → profile → tenant)
  load_tenant_configs(dir)      → orchestrate the full pipeline (public entry)
"""
from __future__ import annotations

import os
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import is_disabled as _is_disabled  # noqa: E402

from _grar_merge import (  # noqa: E402
    _substitute_tenant,
    merge_routing_with_defaults,
)
from _grar_validate import (  # noqa: E402
    _validate_profile_refs,
    check_domain_policies,
    validate_tenant_keys,
)


def _parse_platform_config(data: dict, fname: str, result: dict) -> None:
    """Extract platform-level config from a _ prefixed YAML file.

    Handles: defaults keys, _routing_defaults, _routing_enforced,
    routing_profiles (ADR-007), domain_policies (ADR-007).
    Mutates *result* in place.
    """
    is_defaults_file = os.path.basename(fname).startswith("_")

    # Collect defaults keys for schema validation
    if isinstance(data.get("defaults"), dict):
        result["defaults_keys"].update(data["defaults"].keys())

    # Extract _routing_defaults (only from _ prefixed files)
    if "_routing_defaults" in data:
        if is_defaults_file:
            result["routing_defaults"] = data["_routing_defaults"]
        else:
            print(f"  WARN: _routing_defaults in {fname} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    # v1.7.0: Extract _routing_enforced (only from _ prefixed files)
    if "_routing_enforced" in data:
        if is_defaults_file:
            raw = data["_routing_enforced"]
            if isinstance(raw, dict) and raw.get("enabled", False):
                result["enforced_routing"] = raw
            elif isinstance(raw, dict) and not raw.get("enabled", False):
                pass  # explicitly disabled → None
            else:
                print(f"  WARN: _routing_enforced in {fname} must be a dict "
                      "with 'enabled: true', ignoring", file=sys.stderr)
        else:
            print(f"  WARN: _routing_enforced in {fname} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract routing_profiles (only from _routing_profiles.yaml)
    if "routing_profiles" in data:
        rp_fname = os.path.basename(fname)
        if rp_fname in ("_routing_profiles.yaml", "_routing_profiles.yml"):
            profiles = data["routing_profiles"]
            if isinstance(profiles, dict):
                result["routing_profiles"].update(profiles)
            else:
                print(f"  WARN: routing_profiles in {fname} must be a dict, ignoring",
                      file=sys.stderr)
        else:
            print(f"  WARN: routing_profiles in {fname} ignored "
                  "(only allowed in _routing_profiles.yaml)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract domain_policies (only from _domain_policy.yaml)
    if "domain_policies" in data:
        dp_fname = os.path.basename(fname)
        if dp_fname in ("_domain_policy.yaml", "_domain_policy.yml"):
            policies = data["domain_policies"]
            if isinstance(policies, dict):
                result["domain_policies"].update(policies)
            else:
                print(f"  WARN: domain_policies in {fname} must be a dict, ignoring",
                      file=sys.stderr)
        else:
            print(f"  WARN: domain_policies in {fname} ignored "
                  "(only allowed in _domain_policy.yaml)", file=sys.stderr)


def _parse_tenant_overrides(tenant: str, overrides: dict, result: dict) -> None:
    """Extract per-tenant config from a tenant overrides dict.

    Handles: tenant keys, _routing_profile (ADR-007), _severity_dedup,
    _metadata, _routing (explicit or disabled).
    Mutates *result* in place.
    """
    result["all_tenants"].append(tenant)

    # Collect tenant keys for schema validation
    if tenant not in result["tenant_keys"]:
        result["tenant_keys"][tenant] = set()
    result["tenant_keys"][tenant].update(overrides.keys())

    # v2.1.0 ADR-007: Extract _routing_profile reference
    rp_ref = overrides.get("_routing_profile")
    if rp_ref and isinstance(rp_ref, str):
        result["tenant_profile_refs"][tenant] = rp_ref.strip()

    # Severity dedup: default "enable", explicit "disable" to opt out
    raw_dedup = overrides.get("_severity_dedup", "enable")
    dedup_val = str(raw_dedup).strip().lower()
    if _is_disabled(dedup_val):
        result["dedup_configs"][tenant] = "disable"
    else:
        result["dedup_configs"][tenant] = "enable"

    # v1.11.0: Metadata extraction
    metadata = overrides.get("_metadata")
    if metadata and isinstance(metadata, dict):
        result["metadata_configs"][tenant] = _substitute_tenant(metadata, tenant)

    # Routing: "disable" string → skip routing
    routing = overrides.get("_routing")
    if isinstance(routing, str) and _is_disabled(routing):
        result["disabled_tenants"].add(tenant)
        return

    if routing and isinstance(routing, dict):
        result["explicit_routing"][tenant] = routing


def _parse_config_files(config_dir: str) -> dict:
    """Parse all YAML files in config_dir and extract raw data.

    Returns a dict with keys:
        all_tenants, defaults_keys, routing_defaults, enforced_routing,
        explicit_routing, disabled_tenants, dedup_configs, metadata_configs,
        tenant_keys, routing_profiles, domain_policies, tenant_profile_refs.

    Delegates to:
        _parse_platform_config() — platform-level keys from _ prefixed files
        _parse_tenant_overrides() — per-tenant overrides from tenant sections
    """
    result = {
        "all_tenants": [],
        "defaults_keys": set(),
        "routing_defaults": {},
        "enforced_routing": None,
        "explicit_routing": {},
        "disabled_tenants": set(),
        "dedup_configs": {},
        "metadata_configs": {},
        "tenant_keys": {},
        "routing_profiles": {},      # v2.1.0 ADR-007: named routing configs
        "domain_policies": {},       # v2.1.0 ADR-007: domain compliance constraints
        "tenant_profile_refs": {},   # v2.1.0 ADR-007: tenant → profile name
    }

    if not os.path.isdir(config_dir):
        print(f"ERROR: config directory not found: {config_dir}", file=sys.stderr)
        sys.exit(1)

    files = sorted(f for f in os.listdir(config_dir)
                   if (f.endswith(".yaml") or f.endswith(".yml"))
                   and not f.startswith("."))

    for fname in files:
        path = os.path.join(config_dir, fname)
        with open(path, encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                print(f"  WARN: skip unparseable {fname}: {e}", file=sys.stderr)
                continue

        if not data:
            continue

        _parse_platform_config(data, fname, result)

        if "tenants" not in data:
            continue

        for tenant, overrides in data.get("tenants", {}).items():
            if not isinstance(overrides, dict):
                continue
            _parse_tenant_overrides(tenant, overrides, result)

    return result


def _merge_tenant_routing(parsed: dict, routing_defaults: dict) -> dict[str, dict]:
    """Merge routing defaults with explicit tenant routing configs.

    v2.1.0 ADR-007: Four-layer merge pipeline:
      1. _routing_defaults → global defaults
      2. routing_profiles[ref] → team/domain shared config (if _routing_profile set)
      3. tenant _routing → per-tenant overrides
      4. _routing_enforced → NOC immutable override (applied later in generate_routes)

    Returns routing_configs dict {tenant: merged_routing}.
    """
    profiles = parsed.get("routing_profiles", {})
    profile_refs = parsed.get("tenant_profile_refs", {})

    routing_configs = {}
    seen_tenants = set()
    for tenant in sorted(set(parsed["all_tenants"])):
        if tenant in parsed["disabled_tenants"] or tenant in seen_tenants:
            continue
        seen_tenants.add(tenant)

        # Layer 1: Start with routing defaults
        base = dict(routing_defaults) if routing_defaults else {}

        # Layer 2: Merge routing profile (if referenced)
        if tenant in profile_refs:
            profile_name = profile_refs[tenant]
            if profile_name in profiles:
                profile_cfg = profiles[profile_name]
                if isinstance(profile_cfg, dict):
                    for k, v in profile_cfg.items():
                        base[k] = v
            # Warning for unknown profile is emitted by _validate_profile_refs()

        # Layer 3: Merge explicit tenant routing overrides
        tenant_routing = parsed["explicit_routing"].get(tenant)

        # Only produce a routing config if there's something to route
        if tenant_routing or base:
            routing_configs[tenant] = merge_routing_with_defaults(
                base, tenant_routing, tenant)

    return routing_configs


def load_tenant_configs(config_dir: str) -> tuple[dict[str, dict], dict[str, str], list[str], dict | None, dict[str, dict]]:
    """Load and parse all tenant YAML files from a config directory.

    Orchestrates the full configuration pipeline:
      1. Parse all .yaml/.yml files in config_dir (delegated to _parse_config_files())
      2. Merge _routing_defaults with tenant _routing overrides (delegated to _merge_tenant_routing())
      3. Validate tenant keys against defaults (checks for typos)
      4. Resolve routing profile references (ADR-007)
      5. Validate domain policies against resolved routes (ADR-007)

    Configuration Hierarchy (4 layers):
      - Layer 0: _routing_defaults from _ prefixed files
      - Layer 1: routing_profile reference (_routing_profile key)
      - Layer 2: profile config resolved from _routing_profiles.yaml
      - Layer 3: explicit tenant _routing overrides

    Returns:
        (routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs):
        - routing_configs: {tenant_name: routing_config_dict} for tenants with _routing
        - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")
        - schema_warnings: list of validation warning strings
        - enforced_routing: dict or None — platform enforced routing config (v1.7.0+)
        - metadata_configs: {tenant_name: {runbook_url, owner, tier, ...}} (v1.11.0+)

    Note:
        All tenants appear in dedup_configs even if they have no _routing config.
    """
    parsed = _parse_config_files(config_dir)

    routing_configs = _merge_tenant_routing(
        parsed, parsed["routing_defaults"])

    # Schema validation: check tenant keys against defaults
    schema_warnings = []
    for tenant, keys in sorted(parsed["tenant_keys"].items()):
        schema_warnings.extend(
            validate_tenant_keys(tenant, keys, parsed["defaults_keys"]))

    # v2.1.0 ADR-007: Validate profile references
    schema_warnings.extend(_validate_profile_refs(parsed))

    # v2.1.0 ADR-007: Validate domain policies against resolved routing
    if parsed["domain_policies"]:
        schema_warnings.extend(
            check_domain_policies(routing_configs, parsed["domain_policies"]))

    return (routing_configs, parsed["dedup_configs"], schema_warnings,
            parsed["enforced_routing"], parsed["metadata_configs"])
