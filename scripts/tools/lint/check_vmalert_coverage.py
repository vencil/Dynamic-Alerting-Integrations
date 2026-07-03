#!/usr/bin/env python3
"""check_vmalert_coverage — rule-pack alert FIRING-decision coverage baseline guard.

Problem
-------
The per-PR VM parity gate A (tests/rulepacks/test_vm_alert_parity.py) and the promtool
gate only exercise alerts that have an ``alert_rule_test`` in some
tests/rulepacks/*_test.yaml. An alert declared in rule-packs/*.yaml with NO such test
escapes BOTH engines silently — nobody verifies it fires/no-fires correctly on
Prometheus OR on VictoriaMetrics. At gate introduction 90 of 113 rule-pack alerts were
in that state
(mostly OPTIONAL DB reference packs whose threshold VALUE contract IS tested via
rule-pack-<db>-threshold_test.yaml, but whose firing decision is not) — including the
Oracle / DB2 packs that are the active Splunk→VM migration targets (#947).

What this guards (NOT "every alert must be tested")
---------------------------------------------------
This does not retroactively demand a test for every reference-pack alert. It freezes the
CURRENT gap into an explicit baseline so it cannot silently GROW, mirroring the
bidirectional discipline of vm_deviation_catalog.yaml (catalog == reality):
  * a NEW uncovered alert (in a pack, no alert_rule_test, NOT in the baseline)
        -> FAIL: add an alert_rule_test, or list it in the baseline (a conscious decision).
  * a baseline alert that NOW has a test (or was renamed/removed)
        -> FAIL: remove the stale baseline entry so the baseline stays == reality.

"Covered" = the alert name appears in an ``alert_rule_test[].alertname`` in any
tests/rulepacks/*_test.{yaml,yml}. Threshold-only fixtures (``promql_expr_test`` on
``tenant:alert_threshold:*``) verify the value contract, not the firing decision, so they
do NOT count as firing coverage. ⚠️ It counts a *test case*, not necessarily a *firing*
case: a block asserting ``exp_alerts: []`` (no-fire) also marks the alert covered, so
"covered" alone does not guarantee the alert's ``for:`` state machine or its
annotation-template rendering were exercised — requiring a non-empty ``exp_alerts`` is a
deliberate future step, not taken here to avoid churning the baseline in this PR (Gemini #969).

Usage:
  check_vmalert_coverage.py            # check; exit 1 on drift (dev-rule #13: 0 ok / 1 violation / 2 caller-error)
  check_vmalert_coverage.py --ci       # same, for CI (explicit)
  check_vmalert_coverage.py --generate # rewrite the baseline from the current tree
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("check_vmalert_coverage: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[3]                         # <repo>/scripts/tools/lint/x.py -> <repo>
_RULE_PACKS = _REPO / "rule-packs"
_TESTS = _REPO / "tests" / "rulepacks"
_BASELINE = _TESTS / "vmalert_coverage_baseline.yaml"

_BASELINE_HEADER = """\
# Rule-pack alert FIRING-decision coverage baseline — SSOT for alerts that escape the
# promtool + vmalert-tool (gate A) firing tests. Managed by
# scripts/tools/lint/check_vmalert_coverage.py (bidirectional, like vm_deviation_catalog.yaml):
#   * a NEW uncovered alert not listed here          -> CI FAIL (add an alert_rule_test or list it)
#   * a listed alert that now HAS a test / was removed -> CI FAIL (remove the stale entry)
# "Covered" = the alert appears in an alert_rule_test[].alertname in tests/rulepacks/*_test.{yaml,yml}
# (a test CASE, not necessarily a FIRING case — exp_alerts:[] also counts; see the lint docstring).
# Listing an alert here is NOT an endorsement — it is TRACKED coverage debt. Most entries are
# firing-decision gaps in OPTIONAL DB reference packs (their threshold VALUE contract is tested
# via rule-pack-<db>-threshold_test.yaml).
# ⚠️ MIGRATION BURN-DOWN (Gemini #969): the Oracle/DB2 entries are the active Splunk→VM migration
# targets (#947) — without a firing test we cannot verify their for:/group_by/label rendering on
# vmalert before soak. Treat THIS list as the soak burn-down: clear Oracle/DB2 first.
# Regenerate after an intentional change: python scripts/tools/lint/check_vmalert_coverage.py --generate
"""


def _load_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _rule_pack_files() -> list[Path]:
    """Rule-pack YAML files — matches BOTH ``.yaml`` and ``.yml`` so a ``.yml``-suffixed pack
    cannot silently escape the gate. A bare ``glob("*.yaml")`` would ignore it, letting all its
    alerts bypass the coverage check (no test, not baselined) — a fail-OPEN hole (Gemini #969)."""
    return sorted(f for f in _RULE_PACKS.iterdir()
                  if f.is_file() and f.suffix in (".yaml", ".yml"))


def _fixture_files() -> list[Path]:
    """``*_test.{yaml,yml}`` fixtures — same ``.yml`` defence as rule packs."""
    return sorted(f for f in _TESTS.iterdir()
                  if f.is_file() and (f.name.endswith("_test.yaml") or f.name.endswith("_test.yml")))


def declared_alerts() -> dict[str, str]:
    """{alertname: pack_filename} for every alert declared in rule-packs/*.{yaml,yml}.

    Alertnames are globally unique in this repo by convention (component-prefixed, e.g.
    ``OracleDatabaseDown``). The "covered" check keys on the bare alertname, so a cross-pack
    collision would (a) silently drop one pack's alert from tracking via dict-overwrite and
    (b) let a test in one pack falsely "cover" a same-named untested alert in another. Rather
    than mask that, a collision is detected and FAILS loudly (CodeRabbit #969)."""
    out: dict[str, str] = {}
    dupes: dict[str, list[str]] = {}
    for f in _rule_pack_files():
        for grp in (_load_yaml(f).get("groups") or []):
            for rule in (grp.get("rules") or []):
                name = rule.get("alert")
                if not name:
                    continue
                if name in out and out[name] != f.name:
                    dupes.setdefault(name, [out[name]]).append(f.name)
                out[name] = f.name
    if dupes:
        raise ValueError(
            "duplicate alertname(s) across rule-packs — alertnames must be globally unique, "
            "else a pack's alert is silently dropped from coverage tracking: "
            + "; ".join(f"{a} in {sorted(set(ps))}" for a, ps in sorted(dupes.items())))
    return out


def tested_alertnames() -> set[str]:
    """Alert names exercised by an alert_rule_test in any tests/rulepacks/*_test.{yaml,yml}.

    ⚠️ "Covered" here means only that the alert appears in an ``alert_rule_test`` — NOT that a
    FIRING (true-positive) case exists. A block with ``exp_alerts: []`` (asserting no-fire)
    also counts, so this does not by itself guarantee the alert's ``for:`` state machine or its
    annotation-template rendering (``{{ $value }}`` / ``{{ $labels.* }}``) were exercised. That
    deeper guarantee (require a non-empty ``exp_alerts``) is a deliberate future step, not done
    here to avoid churning the baseline + coverage semantics in this PR (Gemini #969)."""
    out: set[str] = set()
    for f in _fixture_files():
        for t in (_load_yaml(f).get("tests") or []):
            for art in (t.get("alert_rule_test") or []):
                name = art.get("alertname")
                if name:
                    out.add(name)
    return out


def current_uncovered() -> dict[str, list[str]]:
    """{pack: [alert, ...]} for declared alerts with no firing test, grouped by pack."""
    declared = declared_alerts()
    tested = tested_alertnames()
    by_pack: dict[str, list[str]] = {}
    for alert, pack in declared.items():
        if alert not in tested:
            by_pack.setdefault(pack, []).append(alert)
    return {pack: sorted(alerts) for pack, alerts in sorted(by_pack.items())}


def load_baseline() -> dict[str, list[str]]:
    if not _BASELINE.exists():
        return {}
    data = _load_yaml(_BASELINE)
    return {pack: sorted(alerts or []) for pack, alerts in (data.get("uncovered") or {}).items()}


def _flatten(by_pack: dict[str, list[str]]) -> set[tuple[str, str]]:
    return {(pack, a) for pack, alerts in by_pack.items() for a in alerts}


def generate() -> None:
    uncovered = current_uncovered()
    body = yaml.safe_dump({"uncovered": uncovered}, allow_unicode=True, sort_keys=True,
                          default_flow_style=False)
    _BASELINE.write_text(_BASELINE_HEADER + body, encoding="utf-8")
    total = sum(len(v) for v in uncovered.values())
    print(f"check_vmalert_coverage: wrote baseline with {total} uncovered alert(s) "
          f"across {len(uncovered)} pack(s) -> {_BASELINE.relative_to(_REPO)}")


def check() -> int:
    now = _flatten(current_uncovered())
    base = _flatten(load_baseline())

    new_gaps = sorted(now - base)          # uncovered now, not grandfathered -> a NEW silent gap
    healed = sorted(base - now)            # listed but now covered / renamed / removed -> stale entry

    if not new_gaps and not healed:
        covered = len(declared_alerts()) - len(now)
        print(f"check_vmalert_coverage: OK — {covered} alert(s) firing-tested, "
              f"{len(now)} baselined (== reality).")
        return 0

    if new_gaps:
        print("check_vmalert_coverage: NEW untested alert(s) — each escapes BOTH the promtool "
              "and vmalert-tool (gate A) firing gates. Add an alert_rule_test in "
              "tests/rulepacks/<pack>_test.yaml, OR (if intentionally reference-only) list it in "
              f"{_BASELINE.relative_to(_REPO)} (then re-run --generate):")
        for pack, alert in new_gaps:
            print(f"    + {alert}   [{pack}]")
    if healed:
        print("check_vmalert_coverage: STALE baseline entry(ies) — now firing-tested, or the alert "
              f"was renamed/removed. Remove them from {_BASELINE.relative_to(_REPO)} so the baseline "
              "stays == reality (run --generate):")
        for pack, alert in healed:
            print(f"    - {alert}   [{pack}]")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--generate", action="store_true", help="rewrite the baseline from the tree")
    ap.add_argument("--ci", action="store_true", help="CI mode (exit 1 on drift)")
    ap.add_argument("files", nargs="*", help="ignored (pre-commit passes changed files)")
    args = ap.parse_args()
    if not _RULE_PACKS.is_dir() or not _TESTS.is_dir():
        print(f"check_vmalert_coverage: rule-packs/ or tests/rulepacks/ not found under {_REPO}",
              file=sys.stderr)
        return 2
    try:
        if args.generate:
            generate()
            return 0
        return check()
    except ValueError as e:   # duplicate-alertname collision (declared_alerts) — fail loud
        print(f"check_vmalert_coverage: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
