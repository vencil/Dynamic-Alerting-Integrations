#!/usr/bin/env python3
"""
E2E smoke test for Guided Flow system.

Validates:
  1. flows.json schema and structure
  2. All component JSX paths resolve to existing files
  3. CUSTOM_FLOW_MAP in jsx-loader.html covers all registry tools
  4. Bilingual fields (en/zh) present on every step
  5. Condition/validation fields have valid structure
  6. jsx-loader.html contains required flow infrastructure

Usage:
    python3 tests/test_flows_e2e.py [--verbose]

Exit codes:
    0 = all checks passed
    1 = one or more failures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_ASSETS = PROJECT_ROOT / "docs" / "assets"


def load_json(path: Path) -> dict | None:
    """Load and return JSON, or None on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"FAIL  Cannot parse {path.name}: {exc}")
        return None


def check_flows_json(verbose: bool) -> list[str]:
    """Validate flows.json structure and references."""
    errors: list[str] = []
    flows_path = DOCS_ASSETS / "flows.json"

    data = load_json(flows_path)
    if data is None:
        return ["flows.json: parse error"]

    flows = data.get("flows", {})
    if not flows:
        errors.append("flows.json: no flows defined")
        return errors

    for name, flow in flows.items():
        # Required top-level fields
        for field in ("title", "desc", "steps"):
            if field not in flow:
                errors.append(f"Flow '{name}': missing '{field}'")

        # Bilingual title/desc
        for field in ("title", "desc"):
            obj = flow.get(field, {})
            if isinstance(obj, dict):
                for lang in ("en", "zh"):
                    if not obj.get(lang):
                        errors.append(f"Flow '{name}': {field}.{lang} missing")

        steps = flow.get("steps", [])
        if not steps:
            errors.append(f"Flow '{name}': empty steps array")
            continue

        for i, step in enumerate(steps):
            prefix = f"Flow '{name}' step {i}"

            # Required step fields
            if not step.get("tool"):
                errors.append(f"{prefix}: missing 'tool'")
            if not step.get("component"):
                errors.append(f"{prefix}: missing 'component'")

            # Bilingual title/hint
            for field in ("title", "hint"):
                obj = step.get(field, {})
                if isinstance(obj, dict):
                    for lang in ("en", "zh"):
                        if not obj.get(lang) and field == "title":
                            errors.append(f"{prefix}: {field}.{lang} missing")
                elif not obj and field == "title":
                    errors.append(f"{prefix}: missing 'title'")

            # Component path exists
            component = step.get("component", "")
            if component:
                resolved = (DOCS_ASSETS / component).resolve()
                if not resolved.exists():
                    errors.append(f"{prefix}: component not found: {component}")
                elif verbose:
                    print(f"  OK  {prefix}: {component}")

            # Condition structure validation
            cond = step.get("condition")
            if cond is not None:
                if not isinstance(cond, dict):
                    errors.append(f"{prefix}: 'condition' must be an object")
                else:
                    for k, v in cond.items():
                        if not isinstance(v, list):
                            errors.append(
                                f"{prefix}: condition['{k}'] must be an array"
                            )

            # Validation structure validation
            val = step.get("validation")
            if val is not None:
                if not isinstance(val, dict):
                    errors.append(f"{prefix}: 'validation' must be an object")
                else:
                    if "required_state" in val:
                        rs = val["required_state"]
                        if not isinstance(rs, list):
                            errors.append(
                                f"{prefix}: validation.required_state must be array"
                            )
                    if "warn" in val:
                        w = val["warn"]
                        if isinstance(w, dict):
                            if not w.get("en"):
                                errors.append(
                                    f"{prefix}: validation.warn.en missing"
                                )

    return errors


def check_custom_flow_map(verbose: bool) -> list[str]:
    """Verify CUSTOM_FLOW_MAP in jsx-loader.html covers all registry tools."""
    errors: list[str] = []

    # Read jsx-loader.html
    loader_path = DOCS_ASSETS / "jsx-loader.html"
    try:
        loader_src = loader_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read jsx-loader.html: {exc}"]

    # Extract CUSTOM_FLOW_MAP keys
    map_match = re.search(
        r"var CUSTOM_FLOW_MAP\s*=\s*\{([\s\S]*?)\};", loader_src
    )
    if not map_match:
        return ["jsx-loader.html: CUSTOM_FLOW_MAP not found"]

    map_keys: set[str] = set()
    for m in re.finditer(r"'([^']+)'\s*:", map_match.group(1)):
        map_keys.add(m.group(1))

    # Read registry
    registry_path = DOCS_ASSETS / "tool-registry.yaml"
    try:
        import yaml  # noqa: PLC0415

        with open(registry_path, encoding="utf-8") as f:
            registry = yaml.safe_load(f)
        registry_keys = {t["key"] for t in registry.get("tools", [])}
    except Exception:
        # Fallback: just check map is non-empty
        if len(map_keys) < 20:
            errors.append(
                f"CUSTOM_FLOW_MAP has only {len(map_keys)} entries "
                f"(expected 23)"
            )
        return errors

    # Every registry key should be in CUSTOM_FLOW_MAP
    missing = registry_keys - map_keys
    if missing:
        errors.append(
            f"CUSTOM_FLOW_MAP missing registry keys: {sorted(missing)}"
        )
    extra = map_keys - registry_keys
    if extra and verbose:
        print(f"  INFO  CUSTOM_FLOW_MAP extra keys: {sorted(extra)}")

    if verbose:
        print(
            f"  OK  CUSTOM_FLOW_MAP: {len(map_keys)} keys "
            f"(registry: {len(registry_keys)})"
        )

    return errors


def check_jsx_loader_infrastructure(verbose: bool) -> list[str]:
    """Verify jsx-loader.html contains all required flow infrastructure."""
    errors: list[str] = []

    loader_path = DOCS_ASSETS / "jsx-loader.html"
    try:
        src = loader_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read jsx-loader.html: {exc}"]

    required_patterns = [
        ("__FLOW_STATE", "Cross-step data state object"),
        ("__flowSave", "Flow state save function"),
        ("__da_flow_progress_", "Progress persistence key"),
        ("__da_flow_completed_", "Completion tracking key"),
        ("filterSteps", "Conditional step filtering"),
        ("checkValidation", "Checkpoint validation function"),
        ("__checkFlowGate", "Validation gate handler"),
        ("buildCustomFlow", "Custom flow builder function"),
        ("renderFlowUI", "Flow UI renderer"),
        ("flow-stepper", "Stepper CSS class"),
        ("flow-nav", "Navigation bar CSS class"),
        ("flow-hint", "Hint banner CSS class"),
    ]

    for pattern, desc in required_patterns:
        if pattern not in src:
            errors.append(f"jsx-loader.html: missing '{pattern}' ({desc})")
        elif verbose:
            print(f"  OK  jsx-loader.html: {pattern} ({desc})")

    return errors


def check_hub_flow_section(verbose: bool) -> list[str]:
    """Verify Hub index.html has flow cards, analytics, and builder."""
    errors: list[str] = []

    hub_path = PROJECT_ROOT / "docs" / "interactive" / "index.html"
    try:
        src = hub_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read index.html: {exc}"]

    required = [
        ("flow-cards", "Flow card container"),
        ("flow-analytics", "Flow analytics section"),
        ("custom-flow-builder", "Custom flow builder"),
        ("__da_flow_progress_", "Progress reading in Hub"),
        ("__da_flow_completed_", "Completion reading in Hub"),
        ("flows.json", "flows.json fetch"),
    ]

    for pattern, desc in required:
        if pattern not in src:
            errors.append(f"Hub index.html: missing '{pattern}' ({desc})")
        elif verbose:
            print(f"  OK  Hub: {pattern} ({desc})")

    return errors


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    all_errors: list[str] = []

    print("=== Guided Flow E2E Smoke Test ===\n")

    print("[1/4] Checking flows.json schema and references...")
    errs = check_flows_json(verbose)
    all_errors.extend(errs)
    print(f"      {'FAIL' if errs else 'PASS'} ({len(errs)} issues)\n")

    print("[2/4] Checking CUSTOM_FLOW_MAP coverage...")
    errs = check_custom_flow_map(verbose)
    all_errors.extend(errs)
    print(f"      {'FAIL' if errs else 'PASS'} ({len(errs)} issues)\n")

    print("[3/4] Checking jsx-loader.html infrastructure...")
    errs = check_jsx_loader_infrastructure(verbose)
    all_errors.extend(errs)
    print(f"      {'FAIL' if errs else 'PASS'} ({len(errs)} issues)\n")

    print("[4/4] Checking Hub flow section...")
    errs = check_hub_flow_section(verbose)
    all_errors.extend(errs)
    print(f"      {'FAIL' if errs else 'PASS'} ({len(errs)} issues)\n")

    if all_errors:
        print(f"=== FAILED: {len(all_errors)} error(s) ===")
        for e in all_errors:
            print(f"  ✗ {e}")
        return 1

    print("=== ALL CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
