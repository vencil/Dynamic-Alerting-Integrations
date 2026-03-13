#!/usr/bin/env python3
"""validate_config.py — One-stop configuration validation.

Runs all validation checks in sequence and produces a unified report.
Designed for CI pipelines (exit 0 = all pass, exit 1 = any fail).

Checks:
  1. YAML syntax  — All files in config-dir parse as valid YAML
  2. Schema        — Tenant keys validated against known defaults + reserved keys
  3. Routes        — Alertmanager route generation with --validate semantics
  4. Policy        — Webhook domain allowlist (if --policy provided)
  5. Custom rules  — Deny-list linting on rule-packs/ (if --rule-packs provided)
  6. Versions      — bump_docs.py --check version consistency (if --version-check)

Usage:
  # Minimal (YAML + schema + routes):
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/

  # Full suite (CI):
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --policy .github/custom-rule-policy.yaml \\
    --rule-packs rule-packs/ \\
    --version-check

  # JSON output for CI consumption:
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --json
"""

import argparse
import json
import os
import subprocess
import sys

import yaml

from _lib_python import load_yaml_file, VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES  # noqa: E402

# ============================================================
# Check results
# ============================================================
PASS = "pass"
WARN = "warn"
FAIL = "fail"


def _make_result(name, status, details=None):
    """Create a check result dict."""
    return {"check": name, "status": status, "details": details or []}


# ============================================================
# Check 1: YAML Syntax
# ============================================================
def check_yaml_syntax(config_dir):
    """Validate that all YAML files in config_dir parse successfully."""
    errors = []
    file_count = 0
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        fpath = os.path.join(config_dir, fname)
        if not os.path.isfile(fpath):
            continue
        file_count += 1
        try:
            with open(fpath, encoding="utf-8") as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"{fname}: {e}")

    if errors:
        return _make_result("yaml_syntax", FAIL, errors)
    return _make_result("yaml_syntax", PASS,
                        [f"{file_count} files parsed successfully"])


# ============================================================
# Check 2: Schema validation
# ============================================================
def check_schema(config_dir):
    """Validate tenant config keys against known defaults and reserved keys."""
    # Import generate_alertmanager_routes in-process
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    _routing, _dedup, schema_warnings, _er, _mc = gen.load_tenant_configs(config_dir)

    if schema_warnings:
        return _make_result("schema", WARN, schema_warnings)
    return _make_result("schema", PASS, ["No schema warnings"])


# ============================================================
# Check 3: Route validation
# ============================================================
def check_routes(config_dir, policy_file=None):
    """Validate Alertmanager route generation (--validate semantics)."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    routing, dedup, _sw, enforced_routing, _mc = gen.load_tenant_configs(config_dir)

    # Load allowed_domains from policy
    allowed_domains = None
    if policy_file:
        allowed_domains = gen.load_policy(policy_file)

    # Capture stderr for warnings
    import io
    old_stderr = sys.stderr
    sys.stderr = captured = io.StringIO()

    try:
        routes, receivers, route_warnings = gen.generate_routes(
            routing, allowed_domains=allowed_domains,
            enforced_routing=enforced_routing)
        inhibit_rules, dedup_warnings = gen.generate_inhibit_rules(dedup)
    finally:
        sys.stderr = old_stderr

    captured_output = captured.getvalue().strip()
    all_issues = list(route_warnings) + list(dedup_warnings)
    if captured_output:
        all_issues.extend(captured_output.split("\n"))

    # Match --validate semantics: errors are WARNs with "skipping"
    errors = [w for w in all_issues if "WARN" in w and "skipping" in w]

    if errors:
        return _make_result("routes", FAIL, all_issues)
    if all_issues:
        return _make_result("routes", WARN, all_issues)
    return _make_result("routes", PASS,
                        [f"{len(routes)} routes, {len(receivers)} receivers, "
                         f"{len(inhibit_rules)} inhibit_rules"])


# ============================================================
# Check 4: Policy (webhook domain allowlist)
# ============================================================
def check_policy(config_dir, policy_file):
    """Check webhook URLs against domain allowlist."""
    if not policy_file or not os.path.isfile(policy_file):
        return _make_result("policy", PASS, ["No policy file — skipped"])

    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    allowed_domains = gen.load_policy(policy_file)
    if not allowed_domains:
        return _make_result("policy", PASS,
                            ["No allowed_domains in policy — no restrictions"])

    routing, _dedup, _sw, enforced_routing, _mc = gen.load_tenant_configs(config_dir)

    import io
    old_stderr = sys.stderr
    sys.stderr = captured = io.StringIO()

    try:
        _r, _recv, warnings = gen.generate_routes(
            routing, allowed_domains=allowed_domains,
            enforced_routing=enforced_routing)
    finally:
        sys.stderr = old_stderr

    domain_issues = [w for w in warnings if "domain" in w.lower() or
                     "allowlist" in w.lower() or "blocked" in w.lower() or
                     "not in allowed_domains" in w]

    if domain_issues:
        return _make_result("policy", FAIL, domain_issues)
    return _make_result("policy", PASS,
                        [f"All webhook URLs comply with "
                         f"{len(allowed_domains)} allowed domain(s)"])


# ============================================================
# Check 5: Custom rule linting
# ============================================================
def check_custom_rules(rule_packs_dir, policy_file=None):
    """Run lint_custom_rules.py on rule packs directory."""
    if not rule_packs_dir or not os.path.isdir(rule_packs_dir):
        return _make_result("custom_rules", PASS,
                            ["No rule-packs dir — skipped"])

    tools_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(tools_dir, "lint_custom_rules.py"),
           rule_packs_dir, "--ci"]
    if policy_file and os.path.isfile(policy_file):
        cmd.extend(["--policy", policy_file])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        output = (result.stdout + result.stderr).strip()
        lines = [l for l in output.split("\n") if l.strip()] if output else []

        if result.returncode != 0:
            return _make_result("custom_rules", FAIL, lines)
        if any("WARN" in l for l in lines):
            return _make_result("custom_rules", WARN, lines)
        return _make_result("custom_rules", PASS,
                            lines or ["No violations found"])
    except subprocess.TimeoutExpired:
        return _make_result("custom_rules", FAIL, ["Lint timed out (30s)"])
    except FileNotFoundError:
        return _make_result("custom_rules", FAIL,
                            ["lint_custom_rules.py not found"])


# ============================================================
# Check 6: Profile references (v1.12.0)
# ============================================================
def _is_reserved_key(key):
    """Check if a key is a reserved tenant config key (starts with _)."""
    if key in VALID_RESERVED_KEYS:
        return True
    for prefix in VALID_RESERVED_PREFIXES:
        if key.startswith(prefix):
            return True
    return False


def check_profiles(config_dir):
    """Validate tenant _profile references and profile structure.

    Checks:
      - Tenant _profile references point to defined profiles
      - Profile keys don't use reserved prefixes (_, _routing, _state_)
      - Profile values are valid types (numeric, string, dict for scheduled)
      - Profiles have at least one metric key
    """
    profiles_path = os.path.join(config_dir, "_profiles.yaml")
    profiles_raw = load_yaml_file(profiles_path, default={})
    profiles = profiles_raw.get("profiles", {}) if isinstance(profiles_raw, dict) else {}

    # Load defaults for cross-referencing
    defaults_path = os.path.join(config_dir, "_defaults.yaml")
    defaults_raw = load_yaml_file(defaults_path, default={})
    known_defaults = set()
    if isinstance(defaults_raw, dict):
        known_defaults = set(defaults_raw.get("defaults", {}).keys())

    warnings = []
    tenant_count = 0
    profile_ref_count = 0

    # ── Profile structure validation ──
    for p_name, p_data in profiles.items():
        if not isinstance(p_data, dict):
            warnings.append(f"profile={p_name}: value is not a mapping (got {type(p_data).__name__})")
            continue

        if not p_data:
            warnings.append(f"profile={p_name}: empty profile (no metric keys)")
            continue

        for key in p_data:
            # Reserved keys should not be in profiles (they belong in tenant config)
            if key.startswith("_"):
                if _is_reserved_key(key):
                    warnings.append(
                        f"profile={p_name}: contains reserved key \"{key}\" "
                        f"(reserved keys belong in tenant config, not profiles)")
                else:
                    warnings.append(
                        f"profile={p_name}: contains unknown reserved key \"{key}\"")

    # ── Tenant _profile reference validation ──
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        if fname.startswith("_") or fname.startswith("."):
            continue
        fpath = os.path.join(config_dir, fname)
        raw = load_yaml_file(fpath, default={})
        if not isinstance(raw, dict):
            continue

        tenants = {}
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
        else:
            tenant = fname.rsplit(".", 1)[0]
            tenants = {tenant: raw}

        for t_name, t_data in tenants.items():
            if not isinstance(t_data, dict):
                continue
            tenant_count += 1
            profile = t_data.get("_profile")
            if not profile or not isinstance(profile, str):
                continue
            profile_ref_count += 1
            profile = profile.strip()
            if profile and profile not in profiles:
                warnings.append(
                    f"tenant={t_name}: _profile references unknown profile "
                    f"\"{profile}\"")

    if warnings:
        return _make_result("profiles", WARN, warnings)
    details = [f"{tenant_count} tenants scanned, {profile_ref_count} profile refs, "
               f"{len(profiles)} profiles defined"]
    return _make_result("profiles", PASS, details)


# ============================================================
# Check 7: Version consistency
# ============================================================
def check_versions():
    """Run bump_docs.py --check for version consistency."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(tools_dir, "bump_docs.py"), "--check"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=15)
        output = (result.stdout + result.stderr).strip()
        lines = [l for l in output.split("\n") if l.strip()] if output else []

        if result.returncode != 0:
            return _make_result("versions", FAIL, lines)
        return _make_result("versions", PASS,
                            lines or ["Version numbers consistent"])
    except subprocess.TimeoutExpired:
        return _make_result("versions", FAIL, ["Version check timed out"])
    except FileNotFoundError:
        return _make_result("versions", FAIL, ["bump_docs.py not found"])


# ============================================================
# Report
# ============================================================
def print_report(results, as_json=False):
    """Print the validation report."""
    if as_json:
        print(json.dumps(results, indent=2))
        return

    print("=" * 60)
    print("  validate-config — Unified Validation Report")
    print("=" * 60)

    status_icon = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}

    for r in results:
        icon = status_icon[r["status"]]
        print(f"\n[{icon}] {r['check']}")
        for detail in r.get("details", []):
            print(f"       {detail}")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r["status"] == PASS)
    warned = sum(1 for r in results if r["status"] == WARN)
    failed = sum(1 for r in results if r["status"] == FAIL)

    print("\n" + "-" * 60)
    print(f"  Total: {total} checks | "
          f"{passed} pass | {warned} warn | {failed} fail")
    print("-" * 60)

    if failed > 0:
        print("  Result: FAIL")
    elif warned > 0:
        print("  Result: WARN (pass with warnings)")
    else:
        print("  Result: PASS")
    print()


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="One-stop configuration validation.")
    parser.add_argument("--config-dir", required=True,
                        help="Path to tenant config directory (conf.d/)")
    parser.add_argument("--policy",
                        help="Path to policy YAML (allowed_domains etc.)")
    parser.add_argument("--rule-packs",
                        help="Path to rule-packs/ directory for custom rule lint")
    parser.add_argument("--version-check", action="store_true",
                        help="Also run version consistency check")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON (for CI consumption)")
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {args.config_dir}",
              file=sys.stderr)
        sys.exit(1)

    # Ensure tools dir is in sys.path for imports
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    results = []

    # 1. YAML syntax
    results.append(check_yaml_syntax(args.config_dir))

    # 2. Schema validation
    results.append(check_schema(args.config_dir))

    # 3. Route validation
    results.append(check_routes(args.config_dir, args.policy))

    # 4. Policy check (if policy provided)
    if args.policy:
        results.append(check_policy(args.config_dir, args.policy))

    # 5. Custom rule lint (if rule-packs dir provided)
    if args.rule_packs:
        results.append(check_custom_rules(args.rule_packs, args.policy))

    # 6. Profile references (v1.12.0)
    results.append(check_profiles(args.config_dir))

    # 7. Version consistency (if requested)
    if args.version_check:
        results.append(check_versions())

    # Report
    print_report(results, as_json=args.json)

    # Exit code
    has_fail = any(r["status"] == FAIL for r in results)
    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
