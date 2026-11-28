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

GROUP NAMESPACE (#3692): severity dedup pairs alerts by tenant and
`metric_group` — and by `da_cluster` when both alerts carry it (the two rules
of _grar_routes.build_dedup_inhibit_rules) — never by rule pack, so a group is
one namespace across every rule pack a tenant runs. Over T1 (the source packs, one file per pack)
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

GROUP CLUSTER SCOPE (#1200): the two dedup rules are "both alerts carry
`da_cluster`, equal on it" and "neither carries it". An alert with the label
and one without never pair. So within one `metric_group` of one pack the
warning and the critical members must have the same cluster scope:
  - SCOPE-MISMATCH: the members' results differ — one is per cluster
    (`cluster`: its series carry `da_cluster`), another is not (`flat`) — or
    one alert's own `or` sides differ. On a Prometheus whose series carry the
    label the per-cluster warning is then not suppressed by the tenant-wide
    critical (and the other way round a tenant-wide warning is muted only by
    the cluster whose id happens to equal the evaluating Prometheus').
  - SCOPE-NOT-JUDGED: a member's scope could not be determined: its
    expression has a side that is a scalar, the scope rows do not line up
    with the rules read here, or the alert's own `labels:` name `da_cluster`
    (any value — blank, static or templated). A rule's labels are applied
    after the expression is evaluated and overwrite what it computed (a blank
    value removes the label), so they rewrite the very identity Alertmanager
    pairs on, and no template is taken to be harmless. A scope the lint
    cannot DETERMINE is this failure, never a pass; shapes it determines
    WRONGLY are listed under "Not judged" below.
All `cluster` passes; all `flat` passes too — when one Prometheus evaluates
both alerts they get the same external label (or none) and one of the two
rules pairs them. What a member's result carries is check_cluster_identity's
judgement of the expression as Prometheus parses it (`alert_result_scopes`),
never the PromQL text and never the `# da_cluster: tenant-wide` declarations:
a rule that declares an inner tenant-wide read can still output the cluster
(MariaDBNoPrimary). `info` members are not held to it — the inhibit neither
reads them as a source nor mutes them. Over T1 only, like the namespace
checks (T2 is the generated copy); the platform pack is in T1 and is read,
it simply carries no `metric_group` today. Not over the compiler's
`slo_{{…}}` groups nor anything migrate writes for a customer — those
outputs are not in this tree and are pinned by their own tests
(custom_alert_compile_test.go, tests/ops/test_migrate_rule.py; migrate keeps
the customer's rules as written and writes no `metric_group` of its own).

Not judged (the scope check passes these, and Alertmanager sees otherwise):
the scope is check_cluster_identity's, so what that lint does not read
(its docstring, "Not judged") is not read here either. Three shapes are
known to be mis-determined, each pinned as such by
test_documented_scope_limits_are_still_limits:
  * `label_replace(v, "da_cluster", "", "", "")` — the lint says cluster;
    the label is erased, the alert reaches Alertmanager without it.
  * `absent(m{da_cluster="east"})` — the lint says flat; `absent` hands on
    its equality matchers, the alert carries `da_cluster="east"`.
  * an `or` below the top of the expression with sides of different scope,
    `(per-cluster or max by (tenant) (other)) > 5` — the lint says cluster;
    the rule also yields a series without the label. (Only the `or` sides
    at the TOP of an expression are judged apart.)

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
import check_cluster_identity  # noqa: E402  (#1200: what an alert's result carries)

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
            # (_grar_routes.build_dedup_inhibit_rules): an `info` member is never
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


# The inhibit's source is `severity="critical"`, its target `severity="warning"`
# (_grar_routes.build_dedup_inhibit_rules): only these members pair.
_DEDUP_SEVERITIES = ("warning", "critical")
NOT_JUDGED = "not judged"
#: In a row's scopes when the alert's own `labels:` name the cluster label:
#: whatever the expression computed, the rule overwrites it.
LABELS_SET_CLUSTER = "labels-set-cluster"
_LABELS_SET_TEXT = f"{NOT_JUDGED} (its `labels:` set `{check_cluster_identity.CLUSTER_LABEL}`)"


def collect_group_scopes(
        root: Path = PROJECT_ROOT) -> list[tuple[str, str, str, str, frozenset]]:
    """[(pack file name, metric_group, alert, severity, result scopes)] for
    every T1 alert that carries a group (the compiler's `slo_{{…}}` left out,
    as in `collect_groups_by_pack`). The scopes are check_cluster_identity's
    (`alert_result_scopes`), attached by the rule's place in its file; a rule
    with no row there, or a row of another name, gets `{None}` — not judged.
    An alert whose own `labels:` name `da_cluster` gets `{LABELS_SET_CLUSTER}`:
    the rule overwrites what its expression computed."""
    files = T1_SOURCE.files_under(root)
    results = _lib_consumer_read.prometheus_rule_file_results(files)
    _lib_consumer_read.require_read(results)
    judged = {(file, nth): (name, scopes) for file, nth, name, scopes
              in check_cluster_identity.alert_result_scopes(root)}
    rows: list[tuple[str, str, str, str, frozenset]] = []
    for path, rf in zip(files, results):
        pack, nth = Path(path).name, -1
        doc = rf.documents[0] or {}
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                nth += 1
                if "alert" not in r:
                    continue
                labels = r.get("labels") or {}
                mg = labels.get("metric_group")
                if not isinstance(mg, str) or not mg or mg.startswith("slo_{{"):
                    continue
                name, scopes = judged.get((pack, nth), (None, frozenset({None})))
                if name != str(r["alert"]):
                    scopes = frozenset({None})
                if check_cluster_identity.CLUSTER_LABEL in labels:
                    scopes = frozenset({LABELS_SET_CLUSTER})
                rows.append((pack, mg, str(r["alert"]), str(labels.get("severity", "")), scopes))
    return rows


def _scope_text(scopes: frozenset) -> str:
    known = {check_cluster_identity.CLUSTER, check_cluster_identity.FLAT}
    if LABELS_SET_CLUSTER in scopes:
        return _LABELS_SET_TEXT
    if not scopes or not scopes <= known:
        return NOT_JUDGED
    return "+".join(sorted(scopes))


def dedup_members(rows: list[tuple[str, str, str, str, frozenset]],
                  ) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """{(pack, metric_group): [(alert, severity, scope text)]} — the warning
    and critical members the scope check judges, in file order."""
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for pack, mg, alert, severity, scopes in rows:
        if severity in _DEDUP_SEVERITIES:
            groups.setdefault((pack, mg), []).append((alert, severity, _scope_text(scopes)))
    return groups


def group_scope_errors(rows: list[tuple[str, str, str, str, frozenset]]) -> list[str]:
    """SCOPE-MISMATCH / SCOPE-NOT-JUDGED (see GROUP CLUSTER SCOPE)."""
    errors: list[str] = []
    for (pack, mg), members in sorted(dedup_members(rows).items()):
        texts = {text for _a, _s, text in members}
        listed = ", ".join(f"{a} [{sev}] = {text}" for a, sev, text in members)
        if _LABELS_SET_TEXT in texts:
            errors.append(
                f"SCOPE-NOT-JUDGED: {pack}: metric_group {mg!r}: {listed} — an alert's own "
                f"`labels:` set `{check_cluster_identity.CLUSTER_LABEL}`. A rule's labels "
                "overwrite what its expression computed (a blank value removes the label), so "
                "they rewrite the identity Alertmanager pairs warning and critical on, and "
                "whether severity dedup pairs these alerts cannot be judged (this is not a "
                "pass). Drop the label from `labels:` and let the expression carry the "
                "cluster, or take the alert out of the group. (#1200)")
        elif NOT_JUDGED in texts:
            errors.append(
                f"SCOPE-NOT-JUDGED: {pack}: metric_group {mg!r}: {listed} — the cluster scope "
                "of a member could not be determined, so whether severity dedup pairs these "
                "alerts is unknown (this is not a pass). Write the alert so its result is a "
                "vector check_cluster_identity can judge, or take it out of the group. (#1200)")
        elif len(texts) > 1 or any("+" in t for t in texts):
            errors.append(
                f"SCOPE-MISMATCH: {pack}: metric_group {mg!r}: {listed} — severity dedup "
                "pairs two alerts only when both results carry `da_cluster` or neither does, "
                "so here a critical does not suppress the warning in a tenant's clusters "
                "(ADR-001, ADR-006). Give every warning/critical member the same grouping — "
                "keep `da_cluster` in each result (`by (tenant, da_cluster, …)`), every `or` "
                "side included — or move the tenant-wide alert to a group of its own. (#1200)")
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
    group_scopes: list[tuple[str, str, str, str, frozenset]] | None = None,
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
    if group_scopes is None:
        group_scopes = collect_group_scopes()

    verdicts = detect_pairs(alerts)
    violating = {b: v for b, v in verdicts.items() if v != "PASS"}

    errors: list[str] = group_namespace_errors(groups_by_pack)
    errors += group_scope_errors(group_scopes)
    judged = dedup_members(group_scopes)
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

    return {"errors": errors, "infos": infos,
            "scope_judged": (len(judged), sum(len(m) for m in judged.values()))}


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
    n_groups, n_members = result.get("scope_judged", (0, 0))

    for msg in infos:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} metric-group pair violation(s) — TRK-339/#1200.\n"
            "An ungrouped upgrade pair, or a group whose members differ in "
            "cluster scope, double-notifies (severity-dedup inert). See "
            "scripts/tools/lint/check_metric_group_pairs.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"✅ metric-group pairs OK — {len(infos)} known-ungrouped "
        "(grandfathered), 0 new drift; cluster scope judged for "
        f"{n_groups} group(s) / {n_members} warning+critical member(s).",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
