#!/usr/bin/env python3
"""
check_routing_profiles.py — Lint routing profiles and domain policies (ADR-007).

Validates:
  1. All _routing_profile references in tenant YAML point to existing profiles
  2. All tenant IDs in domain_policies exist in config-dir
  3. Domain policy constraints are well-formed
  4. No orphan profiles (defined but never referenced) — warning only

Usage:
  python3 check_routing_profiles.py --config-dir conf.d/
  python3 check_routing_profiles.py --config-dir conf.d/ --strict
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import detect_cli_lang, load_yaml_file  # noqa: E402

_LANG = detect_cli_lang()


def _collect_data(config_dir: str) -> dict:
    """Scan config-dir and collect profiles, policies, tenant IDs, and refs."""
    profiles: dict = {}
    policies: dict = {}
    tenant_ids: set[str] = set()
    profile_refs: dict[str, str] = {}  # tenant → profile name

    files = sorted(
        f for f in os.listdir(config_dir)
        if (f.endswith(".yaml") or f.endswith(".yml")) and not f.startswith(".")
    )

    for fname in files:
        path = os.path.join(config_dir, fname)
        data = load_yaml_file(path)
        if not data or not isinstance(data, dict):
            continue

        # Routing profiles
        if fname in ("_routing_profiles.yaml", "_routing_profiles.yml"):
            rp = data.get("routing_profiles", {})
            if isinstance(rp, dict):
                profiles.update(rp)

        # Domain policies
        if fname in ("_domain_policy.yaml", "_domain_policy.yml"):
            dp = data.get("domain_policies", {})
            if isinstance(dp, dict):
                policies.update(dp)

        # Tenant IDs + profile refs
        tenants_block = data.get("tenants", {})
        if isinstance(tenants_block, dict):
            for tenant, overrides in tenants_block.items():
                tenant_ids.add(tenant)
                if isinstance(overrides, dict):
                    ref = overrides.get("_routing_profile")
                    if ref and isinstance(ref, str):
                        profile_refs[tenant] = ref.strip()

    return {
        "profiles": profiles,
        "policies": policies,
        "tenant_ids": tenant_ids,
        "profile_refs": profile_refs,
    }


def validate(data: dict, *, strict: bool = False) -> list[str]:
    """Run all validation checks. Returns list of messages."""
    messages: list[str] = []
    severity = "ERROR" if strict else "WARN"
    profiles = data["profiles"]
    policies = data["policies"]
    tenant_ids = data["tenant_ids"]
    profile_refs = data["profile_refs"]

    # Check 1: Profile references point to existing profiles
    for tenant, ref in sorted(profile_refs.items()):
        if ref not in profiles:
            messages.append(
                f"{severity}: tenant '{tenant}': _routing_profile "
                f"references unknown profile '{ref}'")

    # Check 2: Domain policy tenant lists reference existing tenants
    for policy_name, policy in sorted(policies.items()):
        if not isinstance(policy, dict):
            messages.append(f"WARN: domain_policy '{policy_name}': not a dict")
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            messages.append(
                f"WARN: domain_policy '{policy_name}': 'tenants' must be a list")
            continue
        for t in tenants:
            if t not in tenant_ids:
                messages.append(
                    f"{severity}: domain_policy '{policy_name}': "
                    f"tenant '{t}' not found in config-dir")

    # Check 3: Domain policy constraints are well-formed
    valid_constraint_keys = {
        "allowed_receiver_types", "forbidden_receiver_types",
        "enforce_group_by", "max_repeat_interval", "min_group_wait",
        "require_critical_escalation",
    }
    for policy_name, policy in sorted(policies.items()):
        if not isinstance(policy, dict):
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            messages.append(
                f"WARN: domain_policy '{policy_name}': 'constraints' must be a dict")
            continue
        for key in constraints:
            if key not in valid_constraint_keys:
                messages.append(
                    f"WARN: domain_policy '{policy_name}': "
                    f"unknown constraint '{key}'")

    # Check 4: Orphan profiles (defined but never referenced) — info only
    referenced = set(profile_refs.values())
    for pname in sorted(profiles):
        if pname not in referenced:
            messages.append(
                f"INFO: routing_profile '{pname}' is defined but "
                f"not referenced by any tenant")

    return messages


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lint routing profiles and domain policies (ADR-007)")
    parser.add_argument("--config-dir", required=True,
                        help="Config directory path")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors (exit 1)")
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {args.config_dir}", file=sys.stderr)
        sys.exit(1)

    data = _collect_data(args.config_dir)
    messages = validate(data, strict=args.strict)

    if not messages:
        profiles_count = len(data["profiles"])
        policies_count = len(data["policies"])
        refs_count = len(data["profile_refs"])
        print(f"OK: {profiles_count} profile(s), {policies_count} policy(ies), "
              f"{refs_count} profile ref(s) — all valid")
        sys.exit(0)

    for msg in messages:
        print(msg, file=sys.stderr)

    has_errors = any(m.startswith("ERROR") for m in messages)
    has_warns = any(m.startswith("WARN") for m in messages)

    if has_errors or (args.strict and has_warns):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
