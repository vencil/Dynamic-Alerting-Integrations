#!/usr/bin/env python3
"""Metric-group upgrade-pair gate — every X/XCritical alert pair must carry a shared `metric_group` label (TRK-339 WS2a / #1200; bug #1199).

WHY: ADR-001 severity-dedup keys on `labels.metric_group` — when a warning
alert and its critical escalation share the group, the platform suppresses
the warning while the critical fires. A pair MISSING the label (or carrying
mismatched values) silently degrades to DOUBLE notification: the exact bug
shipped for the four kubernetes upgrade pairs (#1199 / TRK-338). Nothing
else catches this — promtool sees valid YAML, firing tests see both alerts
fire (which looks "extra correct"). This gate makes the pairing contract
mechanical.

WHAT THIS CHECKS: across the two rule trees (T1 `rule-packs/rule-pack-*.yaml`
incl. custom-alerts; T2 `k8s/03-monitoring/configmap-rules-*.yaml` with
ConfigMap `data` re-parsed — platform pack is T2-only), every alert pair
`X` / `XCritical` (exact full-name equality, `Critical` suffix; corroborated
by the severity convention: the Critical side labelled `critical`, the base
side `warning`/`info`) must have `labels.metric_group` present on BOTH sides
with EQUAL values. Verdicts: MISSING_BOTH / MISSING_ONE / VALUE_MISMATCH are
violations; PASS is clean.

KNOWN LIMITATIONS (deliberate scope, recorded here so nobody assumes more
coverage than exists):
  - Differently-named conceptual pairs (the ClusterYellow/ClusterRed or
    Down/ClusterDown shape) are NOT detected — there is no mechanical naming
    contract to key on. If severity-dedup matters for such a pair, that is a
    design review concern, not a lint concern. (Context in #1199.)
  - Single alerts with no Critical twin (~114 today) are OUT of scope and
    deliberately NOT ledgered: the check is "paired but unlabelled", not
    "every alert must have a metric_group".

KNOWN_UNGROUPED: pairs grandfathered as INFO. It held the 4 kubernetes
pairs that were violating when this gate landed (the #1199 bug itself);
#1199 gave each a static `metric_group` and emptied it. The list is
EXIT-LOCKED (same discipline as check_threshold_reachability.KNOWN_UNWIRED):
a grandfathered pair that gets fixed, or stops being a detected pair, is a
HARD error — the list can only shrink, so it stays empty.

GROUP NAMESPACE (#3692): severity dedup pairs alerts by `metric_group` within a
tenant only (`equal: ["metric_group"]`), so a group is one namespace across
every rule pack a tenant runs. Over T1 (the source packs, one file per pack)
three more checks, all computed — no list to keep:
  - GROUP-COLLISION: a value carried by alerts of two rule packs. A tenant
    running both has one pack's critical mute the other's warning (measured
    for `liveness` in mariadb + mongodb before #3692).
  - RESERVED-PREFIX: a literal value starting `slo_`, the prefix the
    custom-alert compiler gives its SLO alerts (`slo_{{ $labels.name }}`,
    custom_alert_compile.go). A value templated after that prefix (`slo_{{`)
    is that compiler's own and is left out of every group check: its value
    exists per alert instance. Any other templated value is checked as written.
  - NO-DEDUP-GROUP: a value with no critical member or no warning member in
    its pack (the dedup inhibit mutes `severity="warning"` only). It suppresses nothing, so it is only a second, unrelated use
    of the label (a routing handle) — and dedup is what the label is for.

SCOPE NOTE (deliberate non-gate): the WS2a S2 or-masking scan is NOT gated
here or anywhere — its same-label-set `or` heuristic needs human judgment
per finding, so it stays a periodic read-only script. Only the two
mechanical identity checks (check_orphan_recordings + this gate) were
promoted to pre-commit.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)

CRITICAL_SUFFIX = "Critical"

# Grandfathered violating pairs (INFO). The 4 kubernetes pairs that seeded it
# (the #1199 / TRK-338 double-notification bug) were fixed by #1199 and removed.
# EXIT-LOCKED: entries can only be removed — never add one; fix the pair.
KNOWN_UNGROUPED: dict[str, str] = {}

# #3471: the two trees are the rule-source declaration's entries — their
# directories and which files there are rule files (.yaml|yml; an undeclared
# YAML there is named, `SourcesUnlisted`) — not globs of this lint's own.
T1_SOURCE = _lib_consumer_read.RULE_PACKS
T2_SOURCE = _lib_consumer_read.CONFIGMAP_RULES

# The base side of an upgrade pair may be informational; the escalated side
# must be labelled critical for the name match to count as an upgrade pair.
_BASE_SEVERITIES = ("warning", "info")


def collect_alerts(root: Path = PROJECT_ROOT) -> dict[str, dict]:
    """Return {alert_name: labels} across both trees.

    The 3-copy gate keeps T1/T2 semantically synced; where both trees define
    an alert, T1 labels win (first-seen otherwise). T2-only platform alerts
    contribute their own labels.

    Every rule file is read the way Prometheus' rule loader reads it (#3206,
    ADR-036 §12: `da-read prometheus-rules`; a T2 ConfigMap's outer layer
    the way kubectl reads it) — one run per tree. A rule file the loader
    refuses raises `RuleFilesRefused`, which names it."""
    alerts: dict[str, dict] = {}

    def eat(doc, prefer: bool):
        if not doc or "groups" not in doc:
            return
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                if "alert" not in r:
                    continue
                name = r["alert"]
                labels = r.get("labels") or {}
                if name not in alerts or prefer:
                    alerts[name] = labels

    t1 = _lib_consumer_read.prometheus_rule_file_results(T1_SOURCE.files_under(root))
    t2 = [rf for rf in _lib_consumer_read.manifest_rule_file_results(
        T2_SOURCE.files_under(root)) if rf.kind == "ConfigMap"]
    _lib_consumer_read.require_read(t1 + t2)
    for rf in t1:
        eat(rf.documents[0], prefer=True)
    for rf in t2:
        eat(rf.documents[0], prefer=False)

    return alerts


def collect_groups_by_pack(root: Path = PROJECT_ROOT) -> dict[str, dict[str, list[str]]]:
    """Return {pack file name: {metric_group: [severity, …]}} over T1, read the
    way Prometheus' rule loader reads it (#3206). The compiler's templated
    `slo_{{…}}` is left out (see GROUP NAMESPACE)."""
    out: dict[str, dict[str, list[str]]] = {}
    files = T1_SOURCE.files_under(root)
    results = _lib_consumer_read.prometheus_rule_file_results(files)
    _lib_consumer_read.require_read(results)
    for path, rf in zip(files, results):
        groups = out.setdefault(Path(path).name, {})
        doc = rf.documents[0] or {}
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                if "alert" not in r:
                    continue
                labels = r.get("labels") or {}
                mg = labels.get("metric_group")
                if not isinstance(mg, str) or not mg or mg.startswith("slo_{{"):
                    continue
                groups.setdefault(mg, []).append(str(labels.get("severity", "")))
    return out


def group_namespace_errors(by_pack: dict[str, dict[str, list[str]]]) -> list[str]:
    """GROUP-COLLISION / RESERVED-PREFIX / NO-DEDUP-GROUP (see GROUP NAMESPACE)."""
    errors: list[str] = []
    owners: dict[str, list[str]] = {}
    for pack, groups in sorted(by_pack.items()):
        for mg, severities in sorted(groups.items()):
            owners.setdefault(mg, []).append(pack)
            if mg.startswith("slo_"):
                errors.append(
                    f"RESERVED-PREFIX: {pack}: metric_group {mg!r} starts with `slo_`, the "
                    "prefix the custom-alert compiler gives its SLO alerts — a tenant's SLO "
                    "alert of that name would share its severity-dedup group. Rename it. (#3692)")
            # The dedup inhibit's target is `severity="warning"` only
            # (_grar_routes._build_inhibit_rules): an `info` member is never
            # muted, so it does not make a group do anything.
            has_crit = "critical" in severities
            has_base = "warning" in severities
            if not (has_crit and has_base):
                errors.append(
                    f"NO-DEDUP-GROUP: {pack}: metric_group {mg!r} has "
                    f"{'no critical' if not has_crit else 'no warning'} member "
                    f"(severities {sorted(severities)}), so severity dedup suppresses "
                    "nothing through it. Remove the label, or add the alert it pairs with. (#3692)")
    for mg, packs in sorted(owners.items()):
        if len(packs) > 1:
            errors.append(
                f"GROUP-COLLISION: metric_group {mg!r} is used by {', '.join(packs)} — "
                "severity dedup pairs by metric_group within a tenant, so for a tenant "
                "running these packs one pack's critical mutes the other's warning. "
                "Give each pack its own value (e.g. prefix it with the pack). (#3692)")
    return errors


def detect_pairs(alerts: dict[str, dict]) -> dict[str, str]:
    """Return {base_name: verdict} for every detected upgrade pair.

    A pair is `X` + `XCritical` (exact full-name equality) whose severities
    corroborate the upgrade relationship. Verdicts: PASS / MISSING_BOTH /
    MISSING_ONE / VALUE_MISMATCH."""
    verdicts: dict[str, str] = {}
    for base in sorted(alerts):
        crit = base + CRITICAL_SUFFIX
        if crit not in alerts:
            continue
        labels_w = alerts[base] or {}
        labels_c = alerts[crit] or {}
        if labels_c.get("severity") != "critical":
            continue
        if labels_w.get("severity") not in _BASE_SEVERITIES:
            continue
        mg_w = labels_w.get("metric_group")
        mg_c = labels_c.get("metric_group")
        if mg_w is None and mg_c is None:
            verdicts[base] = "MISSING_BOTH"
        elif mg_w is None or mg_c is None:
            verdicts[base] = "MISSING_ONE"
        elif mg_w != mg_c:
            verdicts[base] = "VALUE_MISMATCH"
        else:
            verdicts[base] = "PASS"
    return verdicts


def run_check(
    alerts: dict[str, dict] | None = None,
    known_ungrouped: dict[str, str] | None = None,
    groups_by_pack: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, list[str]]:
    """Return {errors, infos}. errors fail --ci; infos are report-only.

    Inputs default to the real repo collector; hermetic tests inject a
    synthetic {alert: labels} map to exercise each branch."""
    if alerts is None:
        alerts = collect_alerts()
    if known_ungrouped is None:
        known_ungrouped = KNOWN_UNGROUPED
    if groups_by_pack is None:
        groups_by_pack = collect_groups_by_pack()

    verdicts = detect_pairs(alerts)
    violating = {b: v for b, v in verdicts.items() if v != "PASS"}

    errors: list[str] = group_namespace_errors(groups_by_pack)
    infos: list[str] = []

    # NEW violating pairs — a fresh severity-dedup hole.
    for base in sorted(set(violating) - set(known_ungrouped)):
        errors.append(
            f"UNGROUPED-PAIR [{violating[base]}]: {base!r} / "
            f"{base + CRITICAL_SUFFIX!r} lack a shared `metric_group` label — "
            "ADR-001 severity-dedup cannot suppress the warning when the "
            "critical fires, so the pair DOUBLE-notifies. Add the same "
            "`metric_group` to both sides. (TRK-339 / #1200; bug shape #1199)"
        )

    # Grandfathered pairs are report-only WHILE still violating...
    for base in sorted(set(violating) & set(known_ungrouped)):
        infos.append(
            f"known-ungrouped {base}/{base + CRITICAL_SUFFIX} "
            f"[{violating[base]}] — {known_ungrouped[base]}")

    # ...but the ledger is exit-locked.
    for base in sorted(set(known_ungrouped) - set(violating)):
        if base in verdicts:  # detected pair, now PASS
            errors.append(
                f"STALE-EXEMPTION: {base!r} is in KNOWN_UNGROUPED but its "
                "pair now carries a shared metric_group — the fix landed; "
                "remove it from KNOWN_UNGROUPED so the gate protects it."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {base!r} is in KNOWN_UNGROUPED but no "
                "X/XCritical pair with that base is detected anymore "
                "(renamed, deleted, or severity convention changed) — remove "
                "it from KNOWN_UNGROUPED."
            )

    return {"errors": errors, "infos": infos}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "metric_group 升級對 gate：每對 X/XCritical 告警須共享 metric_group 標籤（TRK-339 WS2a / #1200；bug #1199）",
            "Metric-group upgrade-pair gate: every X/XCritical alert pair "
            "must share a metric_group label (TRK-339 WS2a / #1200; "
            "bug #1199)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("新缺標對或 KNOWN_UNGROUPED exit-lock 破口即 exit 1（已列管者為 INFO）",
                       "exit 1 on NEW ungrouped pairs or a KNOWN_UNGROUPED "
                       "exit-lock breach (grandfathered pairs are INFO)"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3206: a rule file Prometheus would not load (each named, with the
        # loader's reason), or no da-read — malformed input, not a violation.
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: metric-group pair check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]

    for msg in infos:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} metric-group pair violation(s) — TRK-339/#1200.\n"
            "An ungrouped upgrade pair double-notifies (severity-dedup "
            "inert). See scripts/tools/lint/check_metric_group_pairs.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"✅ metric-group pairs OK — {len(infos)} known-ungrouped "
        "(grandfathered), 0 new drift.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
