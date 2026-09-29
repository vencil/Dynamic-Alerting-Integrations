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
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import YamlFileError, detect_cli_lang  # noqa: E402
# #2123: a key written twice in one mapping is a YamlFileError like bad syntax.
# #2114: tenant ids as the exporter keys them (raw text), on that same strict read.
from _lib_io import load_yaml_file_strict_exporter_keys  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import (  # noqa: E402
    declared_tenant_ids,
    is_reserved_name,
    list_config_tree,
    overlay_platform_tenants,
    unselected_carriers,
    unusable_reason,
)
sys.path.insert(0, os.path.join(_THIS_DIR, '..', 'ops'))
# #2326: the directory-scope rules of the route generator, not a copy of them.
from _grar_merge import ROOT_LEVEL, chain_levels, level_contains  # noqa: E402

_LANG = detect_cli_lang()

_PROFILE_FILES = ("_routing_profiles.yaml", "_routing_profiles.yml")
_POLICY_FILES = ("_domain_policy.yaml", "_domain_policy.yml")


def _collect_data(config_dir: str) -> dict:
    """Scan config-dir and collect profiles, policies, tenant IDs, and refs.

    #2326: the WHOLE tree, as the route generator reads it since the
    hierarchical routing plane (ADR-007 / ADR-017 "Amendment 2026-09-28"):
    profiles and policies may sit in a subdirectory and are scoped to it, a
    tenant may live at any depth. File names are relative to the root (a root
    file keeps its bare name).
    """
    profiles: dict = {}
    policies: dict = {}
    tenant_ids: set[str] = set()
    profile_refs: dict[str, str] = {}  # tenant → profile name
    profiles_by_dir: dict[str, dict] = {}
    profile_origin: dict = {}
    profile_duplicates: list[tuple[str, str, str]] = []
    subtree_policies: dict[str, dict] = {}   # level → {domain: policy}
    subtree_policy_origin: dict = {}         # (level, domain) → file

    root = Path(config_dir)
    # #1679: the extension test is the shared, case-insensitive predicate —
    # the walker's own (`list_config_tree`: hidden entries skipped, the
    # extension folded). The `_`-prefixed control files matched by exact name
    # below are what this tool exists to READ.
    listing = list_config_tree(root)
    files = [p.relative_to(root).as_posix() for p in listing.files]

    unreadable: list[str] = []
    unreadable_files: list[str] = []
    # #1469 shape: a config-named entry that is not a readable file (a
    # directory, a dangling link, a directory symlink the walk does not
    # follow) is one "cannot read" finding, as the flat listing made it.
    for bad in listing.unusable:
        rel = bad.relative_to(root).as_posix() if bad != root else "."
        unreadable.append(f"{rel}: {unusable_reason(bad)}")
        unreadable_files.append(rel)
    entries: list[tuple[str, object, dict]] = []
    # The unselected carrier spelling is read by no plane (#1674); its
    # `tenants:` block used to reach tenant_ids / profile_refs here.
    by_dir: dict[Path, list[Path]] = {}
    for p in listing.files:
        by_dir.setdefault(p.parent, []).append(p)
    skip = {(d / n).relative_to(root).as_posix()
            for d, ps in by_dir.items() for n in unselected_carriers(ps)}
    for fname in files:
        if fname in skip:
            continue
        path = os.path.join(config_dir, fname)
        base = os.path.basename(fname)
        level = Path(fname).parent.as_posix()
        # #1654 blind review: a lint isolates per file — one unreadable
        # file is one ERROR finding, the other files are still checked
        # (#1008 convention), not an abort that hides their findings.
        # The file NAME is kept as well as the message: `validate` needs
        # it to skip the checks whose input this file was (re-review).
        # #2114: tenant ids are the keys' source TEXT, as the exporter keys
        # them — `123:` in `_defaults.yaml` and `"123":` in a tenant file
        # are one tenant — and a domain policy's `tenants:` list is read
        # the same way so it compares text to text. Strict (#2123) and the
        # same pure parser / YamlFileError as `load_yaml_file_strict`.
        try:
            data = load_yaml_file_strict_exporter_keys(
                path, raw_text_sequences=("tenants",))
        except YamlFileError as exc:
            unreadable.append(str(exc))
            unreadable_files.append(fname)
            continue
        if not data or not isinstance(data, dict):
            continue

        # Routing profiles — #2326 (c): one name, one file, across the tree.
        if base in _PROFILE_FILES:
            rp = data.get("routing_profiles", {})
            if isinstance(rp, dict):
                for name, cfg in rp.items():
                    if name in profile_origin:
                        profile_duplicates.append(
                            (name, profile_origin[name], fname))
                        continue
                    profile_origin[name] = fname
                    profiles[name] = cfg
                    profiles_by_dir.setdefault(level, {})[name] = cfg

        # Domain policies — #2326 (d): below the root, scoped to the subtree.
        if base in _POLICY_FILES:
            dp = data.get("domain_policies", {})
            if isinstance(dp, dict):
                if level == ROOT_LEVEL:
                    policies.update(dp)
                else:
                    subtree_policies.setdefault(level, {}).update(dp)
                    for name in dp:
                        subtree_policy_origin[(level, name)] = fname

        # Tenant IDs + profile refs — collected here, resolved after the
        # loop (#1982): platform file first, tenant file wins, whatever
        # either is called; a platform file cannot create a tenant. The
        # `tenants:` block of a platform file BELOW the root is read by no
        # plane (ADR-017 item 1), so it is not collected either.
        if level != ROOT_LEVEL and is_reserved_name(base):
            continue
        tenants_block = data.get("tenants", {})
        if isinstance(tenants_block, dict):
            for tenant, overrides in tenants_block.items():
                entries.append((fname, tenant,
                                overrides if isinstance(overrides, dict) else {}))

    tenant_dirs: dict = {}
    for fname, tenant, _o in entries:
        if not is_reserved_name(os.path.basename(fname)):
            tenant_dirs.setdefault(tenant, Path(fname).parent.as_posix())
    merged, platform_orphans = overlay_platform_tenants(
        entries, lambda: declared_tenant_ids(config_dir))
    for tenant, overrides in merged.items():
        tenant_ids.add(tenant)
        ref = overrides.get("_routing_profile")
        if ref and isinstance(ref, str):
            profile_refs[tenant] = ref.strip()

    return {
        "profiles": profiles,
        "policies": policies,
        "platform_orphans": platform_orphans,
        "tenant_ids": tenant_ids,
        "profile_refs": profile_refs,
        "unreadable": unreadable,
        "unreadable_files": unreadable_files,
        # #2326: directory scope of profiles / policies / tenants.
        "profiles_by_dir": profiles_by_dir,
        "profile_origin": profile_origin,
        "profile_duplicates": profile_duplicates,
        "subtree_policies": subtree_policies,
        "subtree_policy_origin": subtree_policy_origin,
        "tenant_dirs": tenant_dirs,
    }


def _visible_profiles(data: dict, tenant: str) -> dict:
    """The profiles *tenant* can reference: root, its level, and between."""
    by_dir = data.get("profiles_by_dir")
    if by_dir is None:   # a hand-built data dict (tests): the flat view
        return data["profiles"]
    level = data.get("tenant_dirs", {}).get(tenant, ROOT_LEVEL)
    out: dict = {}
    for d in [ROOT_LEVEL, *chain_levels(level)]:
        out.update(by_dir.get(d, {}))
    return out


def validate(data: dict, *, strict: bool = False) -> list[str]:
    """Run all validation checks. Returns list of messages.

    #1654 re-review: a check whose INPUT file could not be read is skipped,
    and says so in one line, instead of running on the empty set. Measured
    before this: an unreadable ``_routing_profiles.yaml`` made every tenant's
    reference an "unknown profile" finding (ERROR under ``--strict``), and an
    unreadable tenant file made every profile "defined but not referenced" —
    one bad file, N findings about files that are fine. The ``ERROR: cannot
    read`` line for the bad file is still emitted by ``main`` and still sets
    rc 1; only the dependent checks go quiet.
    """
    messages: list[str] = []
    severity = "ERROR" if strict else "WARN"
    profiles = data["profiles"]
    policies = data["policies"]
    tenant_ids = data["tenant_ids"]
    profile_refs = data["profile_refs"]
    unreadable_files = list(data.get("unreadable_files", []))
    profiles_unreadable = [f for f in unreadable_files
                           if os.path.basename(f) in _PROFILE_FILES]
    tenants_unreadable = [f for f in unreadable_files
                          if not os.path.basename(f).startswith("_")]
    if profiles_unreadable:
        messages.append("INFO: profile checks skipped: "
                        f"{', '.join(profiles_unreadable)} unreadable")
    if tenants_unreadable:
        messages.append("INFO: tenant checks skipped: "
                        f"{', '.join(tenants_unreadable)} unreadable")

    # Check 0 (#1982): a platform file naming a tenant no tenant file
    # declares. The entry is already excluded from every check below.
    for fname, tenant in data.get("platform_orphans", []):
        messages.append(
            f"WARN: {fname}: tenants.{tenant} ignored — no tenant file "
            f"declares tenant '{tenant}'; a platform file can only provide "
            f"defaults for a tenant that already exists")

    # Check 1: Profile references point to existing profiles
    # (needs the profile set — skipped when _routing_profiles.yaml is unreadable)
    # #2326 (c): a profile name defined in two files is an error whatever
    # --strict says — the route generator refuses the tree (rc 2).
    for pname, first, second in data.get("profile_duplicates", []):
        messages.append(
            f"ERROR: routing_profile '{pname}' is defined in both {first} "
            f"and {second} — a profile name must be unique across the whole "
            f"conf.d tree")
    origin = data.get("profile_origin", {})
    if not profiles_unreadable:
        for tenant, ref in sorted(profile_refs.items()):
            if ref not in _visible_profiles(data, tenant):
                where = origin.get(ref)
                extra = (f" (a profile of that name is defined in {where}, "
                         f"which is not on this tenant's directory chain)"
                         if where else "")
                messages.append(
                    f"{severity}: tenant '{tenant}': _routing_profile "
                    f"references unknown profile '{ref}'{extra}")

    # The root's policies, then each subtree's (#2326 (d)): same checks, and
    # a subtree policy may only name tenants of its own subtree.
    groups: list[tuple[str, str, dict]] = [(ROOT_LEVEL, "", policies)]
    sub_origin = data.get("subtree_policy_origin", {})
    for level, pols in sorted(data.get("subtree_policies", {}).items()):
        groups.append((level, f" ({level}/)", pols))
    tenant_dirs = data.get("tenant_dirs", {})

    # Check 2: Domain policy tenant lists reference existing tenants
    # (the tenant-existence half needs every tenant file readable)
    for level, where, pols in groups:
        for policy_name, policy in sorted(pols.items()):
            if not isinstance(policy, dict):
                messages.append(
                    f"WARN: domain_policy '{policy_name}'{where}: not a dict")
                continue
            tenants = policy.get("tenants", [])
            if not isinstance(tenants, list):
                messages.append(
                    f"WARN: domain_policy '{policy_name}'{where}: 'tenants' "
                    f"must be a list")
                continue
            if tenants_unreadable:
                continue
            for t in tenants:
                if t not in tenant_ids:
                    messages.append(
                        f"{severity}: domain_policy '{policy_name}'{where}: "
                        f"tenant '{t}' not found in config-dir")
                elif not level_contains(level, tenant_dirs.get(t, ROOT_LEVEL)):
                    home = tenant_dirs.get(t, ROOT_LEVEL)
                    messages.append(
                        f"{severity}: domain_policy '{policy_name}' in "
                        f"{sub_origin.get((level, policy_name), level + '/')}: "
                        f"tenant '{t}' lives outside this policy's subtree "
                        f"{level}/ (in "
                        f"{'the conf.d root' if home == ROOT_LEVEL else home + '/'}"
                        f") — a policy below the root applies only to its own "
                        f"subtree, so this entry is not enforced")

    # Check 3: Domain policy constraints are well-formed
    valid_constraint_keys = {
        "allowed_receiver_types", "forbidden_receiver_types",
        "enforce_group_by", "max_repeat_interval", "min_group_wait",
        "require_critical_escalation",
    }
    for _level, where, pols in groups:
        for policy_name, policy in sorted(pols.items()):
            if not isinstance(policy, dict):
                continue
            constraints = policy.get("constraints", {})
            if not isinstance(constraints, dict):
                messages.append(
                    f"WARN: domain_policy '{policy_name}'{where}: "
                    f"'constraints' must be a dict")
                continue
            for key in constraints:
                if key not in valid_constraint_keys:
                    messages.append(
                        f"WARN: domain_policy '{policy_name}'{where}: "
                        f"unknown constraint '{key}'")

    # Check 4: Orphan profiles (defined but never referenced) — info only
    # (needs both sides readable: the profile set AND every tenant's ref)
    if not profiles_unreadable and not tenants_unreadable:
        referenced = set(profile_refs.values())
        for pname in sorted(profiles):
            if pname not in referenced:
                messages.append(
                    f"INFO: routing_profile '{pname}' is defined but "
                    f"not referenced by any tenant")

    return messages


# #1654: no entry-level rc-2 wrapper here on purpose — the one lib load site
# is caught per file in `_collect_data` (a lint reports an unreadable file as
# an ERROR finding, rc 1, and keeps checking the rest), so a decorator would
# be unreachable and its "rc 2" promise false.
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lint routing profiles and domain policies (ADR-007)")
    parser.add_argument("--config-dir", required=True,
                        help="Config directory path")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors (exit 1)")
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {safe_label(args.config_dir)}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    data = _collect_data(args.config_dir)
    messages = [f"ERROR: cannot read {u}" for u in data.get("unreadable", [])]
    messages += validate(data, strict=args.strict)

    if not messages:
        profiles_count = len(data["profiles"])
        policies_count = len(data["policies"])
        refs_count = len(data["profile_refs"])
        print(f"OK: {profiles_count} profile(s), {policies_count} policy(ies), "
              f"{refs_count} profile ref(s) — all valid")
        sys.exit(EXIT_OK)

    for msg in messages:
        print(safe_label(msg), file=sys.stderr)

    has_errors = any(m.startswith("ERROR") for m in messages)
    has_warns = any(m.startswith("WARN") for m in messages)

    if has_errors or (args.strict and has_warns):
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
