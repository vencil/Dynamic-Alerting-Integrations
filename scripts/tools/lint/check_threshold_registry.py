#!/usr/bin/env python3
"""Threshold-registry gate — schema validation + scaffold equivalence (TRK-339 WS1a / #1200).

WHY: epic #1200 D2 (LOCKED) makes ``rule-packs/threshold-registry.yaml`` the
standalone SoT for the threshold contract (language-neutral, schema-validatable).
This skeleton PR lands the registry WITHOUT rewiring any generator, so for the
whole transition period two copies of the contract exist: the registry and the
still-operative ``scaffold_tenant.RULE_PACKS``. Two live copies of a contract
is exactly the disease #1189 diagnosed (18 dead keys from four hand-copied
surfaces drifting), so the transition itself must be gated:

  1. SCHEMA (the "pre-generation hard gate"): the registry must validate
     against ``docs/schemas/threshold-registry.schema.json`` BEFORE anything
     consumes it — required keys, value:number, unit/desc:string, tier enum.
     A malformed entry fails at author/CI time, never downstream.
  2. EQUIVALENCE (anti dual-SoT drift, exit-lock shaped): the registry must be
     SEMANTICALLY EQUAL to a fresh mechanical extraction of RULE_PACKS. ANY
     divergence in EITHER direction is a hard error — there is no grandfather
     list because the registry is born equal (generated, not hand-copied), so
     the allowed drift set is empty and stays empty. On drift the fix is: edit
     scaffold_tenant.RULE_PACKS (still operative until the rewire PR), then
     ``--regen`` this registry. When the rewire PR flips the SoT direction,
     this assertion flips with it (registry authored, scaffold generated).

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

_OPS = os.path.join(PROJECT_ROOT, "scripts", "tools", "ops")
sys.path.insert(0, _OPS)
import _registry_lib as registry_lib  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

_REGEN_HINT = (
    "fix: edit scaffold_tenant.RULE_PACKS (the operative copy until the rewire "
    "PR), then run `python3 scripts/tools/lint/check_threshold_registry.py "
    "--regen` to refresh the registry."
)


def run_check(
    registry_path: str | None = None,
    schema_path: str | None = None,
    rule_packs: dict | None = None,
) -> dict[str, list[str]]:
    """Return {errors}. Inputs default to the real artifacts; hermetic tests
    inject synthetic docs/packs to exercise each branch."""
    errors: list[str] = []

    path = registry_path or registry_lib.REGISTRY_PATH
    if not os.path.isfile(path):
        return {
            "errors": [
                f"registry file missing: {path} — run --regen to create it."
            ]
        }
    try:
        doc = registry_lib.load_registry(path)
    except Exception as exc:  # unparseable YAML is a violation, not a crash
        return {"errors": [f"registry unparseable: {exc}"]}

    # 1. schema — the pre-generation hard gate.
    for msg in registry_lib.validate_registry(doc, schema_path):
        errors.append(msg)

    # 2. transition-period equivalence (both directions, no grandfather).
    for msg in registry_lib.diff_vs_scaffold(doc, rule_packs):
        errors.append(f"drift: {msg}")

    return {"errors": errors}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "threshold registry gate：schema 驗證 + 與 scaffold RULE_PACKS 的"
            "過渡期等價斷言（TRK-339 WS1a / #1200）",
            "Threshold-registry gate: schema validation + transition-period "
            "equivalence with scaffold RULE_PACKS (TRK-339 WS1a / #1200)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("schema violation 或雙 SoT 漂移即 exit 1",
                       "exit 1 on schema violation or dual-SoT drift"))
    parser.add_argument(
        "--regen", action="store_true",
        help=i18n_text(
            "從 scaffold_tenant.RULE_PACKS 重新機械萃取並覆寫 registry",
            "re-extract the registry mechanically from scaffold_tenant.RULE_PACKS"))
    args = parser.parse_args(argv)

    if args.regen:
        try:
            summary = registry_lib.write_registry()
        except Exception as exc:  # noqa: BLE001 — caller error, not a violation
            print(f"ERROR: regen failed: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        print(
            f"regenerated {summary['path']} — {summary['packs']} packs, "
            f"{summary['keys']} keys.",
            file=sys.stderr)
        return EXIT_OK

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: registry check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} threshold-registry violation(s) — TRK-339 WS1a.\n"
            f"{_REGEN_HINT}\n"
            "See scripts/tools/lint/check_threshold_registry.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        "✅ threshold registry OK — schema valid, semantically equal to "
        "scaffold RULE_PACKS.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
