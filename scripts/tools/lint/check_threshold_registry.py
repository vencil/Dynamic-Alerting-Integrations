#!/usr/bin/env python3
"""Threshold-registry gate — schema + self-consistency + generated-surface freshness + header-prose membership (TRK-339 WS1a / #1200).

WHY: epic #1200 D2 (LOCKED) makes ``rule-packs/threshold-registry.yaml`` the
standalone SoT for the threshold contract (language-neutral,
schema-validatable). The PR-2 rewire promoted it from a gated parallel copy to
a REAL SoT: previously hand-copied surfaces (rule-pack header threshold
sections, helm values ``thresholdConfig.defaults``, the dev template
``conf.d/_defaults.yaml`` defaults section, and — since #1310 — the
``optional_overrides:`` declared-key LIST on both runtime surfaces) are now
GENERATED from it inside delimited blocks. Since D-A PR-3 the registry is
authored by hand and ``scaffold_tenant.RULE_PACKS`` is generated from it too
(``scripts/tools/ops/_rule_packs_generated.py``, merged with scaffold's own
state filters and dimensional examples), so no second copy of a threshold is
written anywhere. Two live copies of a contract is exactly the disease #1189
diagnosed (18 dead keys from four hand-copied surfaces drifting):

  1. SCHEMA (the "pre-generation hard gate"): the registry must validate
     against ``docs/schemas/threshold-registry.schema.json`` BEFORE anything
     consumes it — required keys, value:number, unit/desc:string, tier enum.
     A malformed entry fails at author/CI time, never downstream.
  2. SELF-CONSISTENCY: the registry must be a fixed point of projecting it to
     the RULE_PACKS shape and extracting it again (``_registry_lib.
     fixed_point_problems``) — so the authored file still obeys every rule
     the extraction enforces: ``critical_of`` exactly where it derives,
     every ``_registry_lib.arming_problems`` rule (D-A A1), a ``metric_class`` on every
     key, no key in two tiers or two packs, aliases onto live keys only.
  3. SURFACE FRESHNESS: each generated block in every surface, and the
     generated RULE_PACKS module as a whole, must be byte-equal to a fresh
     render from the committed registry. STALE (or
     missing/duplicated delimiters) is a hard error; ``--regen`` re-splices
     every surface in one shot.
  4. HEADER-PROSE MEMBERSHIP (design F6): hand-written pack-header prose may
     only name conf.d threshold keys that exist in the contract — registry
     keys, their derived ``_critical`` opt-ins, the #1196 KNOWN_UNWIRED
     pending line (imported from check_threshold_reachability so the two
     gates shrink together), or scaffold state-filter names. Dimensional
     tokens (``key{...}``) are exempt. This kills the "header-only claim"
     class where prose advertises keys that exist nowhere.

``--regen`` changes two conf.d files (the dev template and the try-local
seed ``_defaults.yaml``), so it follows ADR-036 §2: only Go writes conf.d.
Each changed one is a ``replace`` (its whole spliced text) in one ``da-guard
write`` batch per conf.d root (``_registry_lib.plan_surfaces``); every batch
is first asked as a dry run, before anything is written, and a refusal is
named file by file. On a refusal nothing is written — no surface and not the
RULE_PACKS module (the registry itself is never written) — and the run exits 2, as it does when a conf.d file would
change and there is no usable da-guard. A generated block whose
enclosing key is not the carrier its surface names (read back by
parse-tree) is refused the same way. A write that stops part-way — a
conf.d batch, a helm values / rule pack file, the RULE_PACKS module — names the file
that failed and the files already written (not rolled back), exit 2. The
check mode reads
every surface as bytes (a text compare of the generated blocks) and runs no
da-guard.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error
(including a refused or unconfirmable ``--regen``).
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
from _lib_io import safe_label  # noqa: E402
from _lib_parse_tree import ParseTreeError as _ParseTreeError  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3471: which files are rule packs)

_REGEN_HINT = (
    "fix: edit rule-packs/threshold-registry.yaml (the SoT since D-A PR-3), "
    "then run `python3 scripts/tools/lint/check_threshold_registry.py "
    "--regen` to refresh every generated surface and the RULE_PACKS module."
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

    # 2. self-consistency: a fixed point of project -> extract (D-A PR-3).
    for msg in registry_lib.fixed_point_problems(doc):
        errors.append(f"inconsistent: {msg}")

    # Surfaces + membership render FROM the doc; a schema-invalid or
    # inconsistent doc already failed above, but keep going so one run
    # reports everything.

    # 3. generated-surface freshness (helm values + dev template — `defaults`
    #    mapping AND `optional_overrides` list on each — plus 13 pack headers).
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

    # 3b. the generated RULE_PACKS module (scaffold_tenant's threshold half).
    module_path = (surface_paths or {}).get(registry_lib.RULE_PACKS_MODULE_ID)
    msg = registry_lib.check_rule_packs_module(doc, module_path)
    if msg:
        errors.append(f"stale-surface: {msg}")

    # 3c. the generated Go table of `retired_keys:` (#3829), which the key
    #     verdict refuses every retired key from.
    go_path = (surface_paths or {}).get(registry_lib.RETIRED_KEYS_GO_ID)
    msg = registry_lib.check_retired_keys_go(doc, go_path)
    if msg:
        errors.append(f"stale-surface: {msg}")

    # 4. header-prose key membership (F6).
    if prose_files is None:
        # #3471: the rule packs are the one declaration's (rule-pack-*.yaml|yml;
        # an undeclared YAML beside them raises `SourcesUnlisted`, named).
        # #1595 D1a: TENANT packs only — a declared platform pack
        # (`PLATFORM_PACKS`) has no tenant threshold, so its prose (which
        # names alert labels such as `alert_source`) is no conf.d claim.
        prose_files = [str(p) for p in _lib_consumer_read.tenant_rule_pack_files(
            os.path.join(PROJECT_ROOT, _lib_consumer_read.RULE_PACKS.directory))]
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
            "threshold registry gate：schema 驗證 + 自身一致性 + 生成面（含 "
            "RULE_PACKS 生成模組）新鮮度 + header 手寫 prose key membership"
            "（TRK-339 WS1a / #1200）",
            "Threshold-registry gate: schema validation + self-consistency + "
            "generated-surface freshness (the RULE_PACKS module included) + "
            "header-prose key membership (TRK-339 WS1a / #1200)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("schema violation／registry 自身不一致／生成面 stale／"
                       "prose 引用不存在 key 即 exit 1",
                       "exit 1 on schema violation, an inconsistent registry, a "
                       "stale generated surface, or a prose membership breach"))
    parser.add_argument(
        "--regen", action="store_true",
        help=i18n_text(
            "從 registry 重產所有生成面（helm values / dev 範本各含 defaults 與 "
            "optional_overrides 兩段 / pack headers / RULE_PACKS 生成模組）；"
            "registry 本身是手寫 SoT，不會被寫入",
            "re-generate every surface from the registry (helm values / dev "
            "templates / pack headers / the RULE_PACKS module); the registry "
            "itself is authored and never written"))
    args = parser.parse_args(argv)

    if args.regen:
        try:
            summary, touched = registry_lib.regen()
        except registry_lib.ConfdWriteRefused as exc:
            print(f"ERROR: regen refused: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        except registry_lib.SurfaceWriteStopped as exc:  # ConfdWriteStopped too
            print(f"ERROR: regen stopped: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        except _ParseTreeError as exc:
            print("ERROR: cannot write the generated conf.d surfaces through "
                  f"`da-guard write` (ADR-036 §2): {safe_label(' '.join(str(exc).split()))}"
                  "; nothing was written", file=sys.stderr)
            return EXIT_CALLER_ERROR
        except Exception as exc:  # noqa: BLE001 — caller error, not a violation
            print(f"ERROR: regen failed: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        print(
            f"regenerated from {summary['path']} — {summary['packs']} packs, "
            f"{summary['keys']} keys; surfaces touched: {len(touched)}.",
            file=sys.stderr)
        for p in touched:
            print(f"  spliced {p}", file=sys.stderr)
        return EXIT_OK

    try:
        result = run_check()
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3471: an undeclared YAML beside the rule packs, named.
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
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
        "✅ threshold registry OK — schema valid, self-consistent, all "
        "generated surfaces (RULE_PACKS module included) fresh, header prose "
        "membership clean.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
