#!/usr/bin/env python3
"""Threshold-registry gate — schema + scaffold equivalence + generated-surface freshness + header-prose membership (TRK-339 WS1a / #1200).

WHY: epic #1200 D2 (LOCKED) makes ``rule-packs/threshold-registry.yaml`` the
standalone SoT for the threshold contract (language-neutral,
schema-validatable). The PR-2 rewire promoted it from a gated parallel copy to
a REAL SoT: three previously hand-copied surfaces (rule-pack header threshold
sections, helm values ``thresholdConfig.defaults``, the dev template
``conf.d/_defaults.yaml`` defaults section) are now GENERATED from it inside
delimited blocks. ``scaffold_tenant.RULE_PACKS`` remains the operative copy
for the config-generation path until PR-3 demotes it, so the transition still
needs the anti-dual-SoT gate. Two live copies of a contract is exactly the
disease #1189 diagnosed (18 dead keys from four hand-copied surfaces
drifting), so every copy relationship is gated:

  1. SCHEMA (the "pre-generation hard gate"): the registry must validate
     against ``docs/schemas/threshold-registry.schema.json`` BEFORE anything
     consumes it — required keys, value:number, unit/desc:string, tier enum.
     A malformed entry fails at author/CI time, never downstream.
  2. EQUIVALENCE (anti dual-SoT drift, exit-lock shaped): the registry must be
     SEMANTICALLY EQUAL to a fresh mechanical extraction of RULE_PACKS (plus
     the registry-scope enrichment tables in _registry_lib). ANY divergence in
     EITHER direction is a hard error — no grandfather list, the registry is
     born equal. On drift: edit scaffold_tenant.RULE_PACKS, then ``--regen``.
  3. SURFACE FRESHNESS: each generated block in the three surfaces must be
     byte-equal to a fresh render from the committed registry. STALE (or
     missing/duplicated delimiters) is a hard error; ``--regen`` re-splices
     every surface in one shot.
  4. HEADER-PROSE MEMBERSHIP (design F6): hand-written pack-header prose may
     only name conf.d threshold keys that exist in the contract — registry
     keys, their derived ``_critical`` opt-ins, the #1196 KNOWN_UNWIRED
     pending line (imported from check_threshold_reachability so the two
     gates shrink together), or scaffold state-filter names. Dimensional
     tokens (``key{...}``) are exempt. This kills the "header-only claim"
     class where prose advertises keys that exist nowhere.

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
    "fix: edit scaffold_tenant.RULE_PACKS (the operative copy until PR-3) "
    "and/or the _registry_lib enrichment tables, then run `python3 "
    "scripts/tools/lint/check_threshold_registry.py --regen` to refresh the "
    "registry AND every generated surface."
)


def _known_unwired_keys() -> set[str]:
    """The #1196 pending line — reused verbatim so the gates agree.

    A KNOWN_UNWIRED key may legitimately appear in header prose (e.g. the
    db2_lock_wait_time calibration walkthrough) while its identity repair is
    pending; check_threshold_reachability's exit-lock forces that list to
    shrink as fixes land, so this exemption cannot rot.
    """
    import check_threshold_reachability  # noqa: E402 — sibling lint module

    return set(check_threshold_reachability.KNOWN_UNWIRED)


def _state_filter_names() -> set[str]:
    """Scaffold-owned state-filter names (legit `key:` prose in K8s header)."""
    import scaffold_tenant  # noqa: E402 — _OPS already on sys.path

    return {
        name
        for pack in scaffold_tenant.RULE_PACKS.values()
        for name in (pack.get("state_filters") or {})
    }


def run_check(
    registry_path: str | None = None,
    schema_path: str | None = None,
    rule_packs: dict | None = None,
    surface_paths: dict[str, str] | None = None,
    prose_files: list[str] | None = None,
    extra_allowed: set[str] | None = None,
) -> dict[str, list[str]]:
    """Return {errors}. Inputs default to the real artifacts; hermetic tests
    inject synthetic docs/packs/paths to exercise each branch.

    - ``surface_paths``: {surface_id: path} overrides for the freshness check
      (a missing id keeps the real path).
    - ``prose_files``: rule-pack files for the membership scan (defaults to
      every rule-packs/rule-pack-*.yaml).
    - ``extra_allowed``: membership-universe extras (defaults to KNOWN_UNWIRED
      + scaffold state-filter names).
    """
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

    # Surfaces + membership render FROM the doc; a schema-invalid or drifted
    # doc already failed above, but keep going so one run reports everything.

    # 3. generated-surface freshness (helm values / dev template / 13 packs).
    for spec in registry_lib.surface_specs(doc):
        if surface_paths and spec["id"] in surface_paths:
            spec = {**spec, "path": surface_paths[spec["id"]]}
        if not os.path.isfile(spec["path"]):
            errors.append(
                f"stale-surface: {spec['path']} (surface {spec['id']!r}) does "
                "not exist"
            )
            continue
        with open(spec["path"], encoding="utf-8") as fh:
            text = fh.read()
        msg = registry_lib.check_surface(text, spec)
        if msg:
            errors.append(f"stale-surface: {msg}")

    # 4. header-prose key membership (F6).
    if prose_files is None:
        import glob

        prose_files = sorted(
            p
            for p in glob.glob(
                os.path.join(PROJECT_ROOT, "rule-packs", "rule-pack-*.yaml")
            )
        )
    if extra_allowed is None:
        extra_allowed = _known_unwired_keys() | _state_filter_names()
    universe = registry_lib.membership_universe(doc, extra_allowed)
    for pf in prose_files:
        with open(pf, encoding="utf-8") as fh:
            text = fh.read()
        for lineno, token in registry_lib.prose_key_tokens(text):
            if token not in universe:
                errors.append(
                    f"membership: {os.path.relpath(pf, PROJECT_ROOT)}:{lineno}: "
                    f"header prose claims conf.d key {token!r} which exists "
                    "nowhere in the threshold contract (not a registry key / "
                    "derived _critical / KNOWN_UNWIRED pending key / state "
                    "filter) — fix the prose or wire the key"
                )

    return {"errors": errors}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "threshold registry gate：schema 驗證 + 與 scaffold RULE_PACKS 的"
            "過渡期等價斷言 + 三生成面新鮮度 + header 手寫 prose key membership"
            "（TRK-339 WS1a / #1200）",
            "Threshold-registry gate: schema validation + transition-period "
            "equivalence with scaffold RULE_PACKS + generated-surface "
            "freshness + header-prose key membership (TRK-339 WS1a / #1200)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("schema violation／雙 SoT 漂移／生成面 stale／prose 引用"
                       "不存在 key 即 exit 1",
                       "exit 1 on schema violation, dual-SoT drift, a stale "
                       "generated surface, or a prose membership breach"))
    parser.add_argument(
        "--regen", action="store_true",
        help=i18n_text(
            "從 scaffold_tenant.RULE_PACKS 重新機械萃取並覆寫 registry，"
            "並重產三個生成面（helm values / dev 範本 / pack headers）",
            "re-extract the registry from scaffold_tenant.RULE_PACKS AND "
            "re-splice every generated surface"))
    args = parser.parse_args(argv)

    if args.regen:
        try:
            summary = registry_lib.write_registry()
            touched = registry_lib.regen_surfaces()
        except Exception as exc:  # noqa: BLE001 — caller error, not a violation
            print(f"ERROR: regen failed: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        print(
            f"regenerated {summary['path']} — {summary['packs']} packs, "
            f"{summary['keys']} keys; surfaces touched: {len(touched)}.",
            file=sys.stderr)
        for p in touched:
            print(f"  spliced {p}", file=sys.stderr)
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
        "scaffold RULE_PACKS, all generated surfaces fresh, header prose "
        "membership clean.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
