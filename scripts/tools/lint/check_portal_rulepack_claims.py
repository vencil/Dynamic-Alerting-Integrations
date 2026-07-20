#!/usr/bin/env python3
"""Portal rule-pack claim guard — the portal must not advertise alerts we don't ship.

`tools/portal/src/interactive/tools/rule-pack-detail.jsx` is a HAND-MAINTAINED
customer-facing data file: for each rule pack it lists the alert rules a tenant
gets, with severity and PromQL. Nothing generated it and — until this guard —
nothing checked it. It had drifted to 28 alert names that exist in NO shipping
rule tree (fabricated capability claims: "MariaDBReplicationLag",
"KubernetesOOMKill", "PostgreSQLSlowQueries", ...), plus 2 severity claims that
contradicted the shipped rule.

That drift direction matters: the portal is the thing a prospect reads. An
invented alert name is a promise the platform cannot keep, and a wrong severity
mis-sets paging expectations.

What is checked
---------------
For every alert entry in the portal file:
  1. its `name` exists as an `alert:` in at least one shipping rule tree, and
  2. its `severity` matches that rule's `labels.severity`.
For every recording entry in the portal file:
  3. its `name` exists as a `record:` in at least one shipping rule tree.

The `recording:` arrays were checked in the same sweep and were WORSE than the
alert side — 27 of 30 names did not exist (90%), all of them name drift onto a
real rule (`tenant:kafka_lag:max` → `tenant:kafka_consumer_lag:max`,
`tenant:redis_evictions:rate5m` → `tenant:redis_evicted_keys:rate5m`, ...).
Gating only alert names would have let this file pass a green check while 90%
of its recording claims were still wrong — a false sense of coverage, which is
the disease this guard exists to cure.

Shipping rule trees (a name found in ANY of the three counts as shipped):
  1. rule-packs/rule-pack-<pack>.yaml               (canonical source)
  2. k8s/03-monitoring/configmap-rules-*.yaml       (ConfigMap deploy copy;
     includes configmap-rules-platform.yaml, which has NO rule-pack counterpart
     — platform-scope rules live only here)
  3. operator-manifests/da-rule-pack-*.yaml         (PrometheusRule CRD copy)

Trees 1-3 are kept in lockstep by check_rulepack_sync.py, so accepting a match
in any of them cannot silently bless a name that isn't deployed; scanning all
three just makes this guard independent of which copy a rule lands in first.

Deliberately NOT checked: the portal's `expr` strings. The shipped exprs carry a
large metadata-join wrapper (`* on(tenant) group_left(runbook_url, owner, tier)
tenant_metadata_info ...`) that would be unreadable in a customer-facing viewer,
so the portal shows the semantic CORE. Comparing those mechanically would need a
PromQL-subset matcher and would fail on a faithful simplification — out of scope.
Rule NAMES (+ alert severity) are the load-bearing, exactly-checkable claims.

An EMPTY `recording: []` is legitimate and must stay legal: the `operational`
and `platform` packs genuinely ship zero recording rules (their alerts read raw
injected/platform series). Only a NON-empty array with unparseable entries is an
error — see parse_portal_claims.

Matching is name-scoped, not pack-scoped: a portal pack may legitimately surface
a rule that ships in a neighbouring tree (the `platform` block lists rules from
configmap-rules-platform.yaml). Pack-level attribution is reported as INFO only.

Exit codes:
    0  Every portal alert claim resolves to a shipped rule
    1  Bogus claim(s) found (--ci) — details printed
    2  Error (missing file, YAML parse failure, portal file unparseable)

Usage:
    python scripts/tools/lint/check_portal_rulepack_claims.py          # report
    python scripts/tools/lint/check_portal_rulepack_claims.py --ci     # exit 1
    python scripts/tools/lint/check_portal_rulepack_claims.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

PORTAL_REL = "tools/portal/src/interactive/tools/rule-pack-detail.jsx"


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


# --------------------------------------------------------------------------
# Shipping rule trees → {alert_name: {severity, ...}}
# --------------------------------------------------------------------------
def _rules_from_groups(
    doc: Any,
    origin: str,
    alerts: Dict[str, Dict[str, Any]],
    records: Dict[str, Dict[str, Any]],
) -> None:
    if not isinstance(doc, dict):
        return
    for group in doc.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for rule in group.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("alert"):
                labels = rule.get("labels") or {}
                entry = alerts.setdefault(
                    rule["alert"], {"severity": labels.get("severity"), "origins": []}
                )
                entry["origins"].append(origin)
            elif rule.get("record"):
                entry = records.setdefault(rule["record"], {"origins": []})
                entry["origins"].append(origin)


def collect_shipped_rules(repo: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Index every alert AND recording rule across the three shipping trees."""
    alerts: Dict[str, Dict[str, Any]] = {}
    records: Dict[str, Dict[str, Any]] = {}

    for path in sorted((repo / "rule-packs").glob("rule-pack-*.yaml")):
        pack = path.stem.replace("rule-pack-", "")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _rules_from_groups(doc, pack, alerts, records)

    # ConfigMaps wrap one or more rule documents as strings under `data:`.
    for path in sorted((repo / "k8s" / "03-monitoring").glob("configmap-rules-*.yaml")):
        pack = path.stem.replace("configmap-rules-", "")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for _key, val in (doc.get("data") or {}).items():
            _rules_from_groups(yaml.safe_load(val) or {}, pack, alerts, records)

    # PrometheusRule CRD copies keep groups under `spec:`.
    op_dir = repo / "operator-manifests"
    if op_dir.is_dir():
        for path in sorted(op_dir.glob("da-rule-pack-*.yaml")):
            pack = path.stem.replace("da-rule-pack-", "")
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _rules_from_groups(doc.get("spec") or {}, pack, alerts, records)

    return alerts, records


def collect_shipped_alerts(repo: Path) -> Dict[str, Dict[str, Any]]:
    """Back-compat accessor for the alert index alone."""
    return collect_shipped_rules(repo)[0]


# --------------------------------------------------------------------------
# Portal claims
# --------------------------------------------------------------------------
_PACK_RE = re.compile(r"^  (\w+): \{$", re.M)
_ALERTS_RE = re.compile(r"alerts: \[(.*?)^    \]", re.S | re.M)
_RECORDING_RE = re.compile(r"recording: \[(.*?)^    \]", re.S | re.M)
_RECORDING_EMPTY_RE = re.compile(r"recording: \[\],")
_ALERT_ENTRY_RE = re.compile(r"\{\s*name: '([^']+)',\s*severity: '([^']+)'")
_RECORD_ENTRY_RE = re.compile(r"\{\s*name: '([^']+)',\s*expr:")


def parse_portal_claims(path: Path) -> Dict[str, Dict[str, List]]:
    """Extract {pack: {"alerts": [(name, severity)...], "recording": [name...]}}.

    The portal file is plain JS data, not importable from Python, so this is a
    text parse. Every step below FAILS LOUD rather than returning an empty set:
    a silently-empty parse would turn this guard into a no-op that reports
    success forever (fail-open), which is exactly the failure mode the guard
    exists to prevent.
    """
    src = path.read_text(encoding="utf-8")
    claims: Dict[str, Dict[str, List]] = {}

    pack_matches = list(_PACK_RE.finditer(src))
    if not pack_matches:
        raise ValueError(
            f"{PORTAL_REL}: no rule-pack blocks matched — the file's shape changed "
            "and this guard can no longer read it. Fix the parser, do not ignore."
        )

    for idx, match in enumerate(pack_matches):
        pack = match.group(1)
        end = pack_matches[idx + 1].start() if idx + 1 < len(pack_matches) else len(src)
        segment = src[match.end():end]

        alerts_block = _ALERTS_RE.search(segment)
        if not alerts_block:
            # A pack with no alerts array at all is itself suspicious.
            raise ValueError(
                f"{PORTAL_REL}: pack '{pack}' has no parseable `alerts: [...]` array."
            )
        alert_entries = _ALERT_ENTRY_RE.findall(alerts_block.group(1))
        if not alert_entries:
            raise ValueError(
                f"{PORTAL_REL}: pack '{pack}' has an `alerts` array but no parseable "
                "`{ name: '...', severity: '...' }` entries."
            )

        # `recording: []` is legal (operational / platform ship no recording
        # rules). A NON-empty array that yields no entries is a parser break.
        # ⚠ Check the EMPTY form FIRST: _RECORDING_RE's `.*?^    ]` lazily scans
        # forward for a 4-space-indented closing bracket, so on `recording: [],`
        # it over-matches past the empty array into the pack's `alerts: [ ... ]`
        # block and captures alert entries (which have `severity:`, not `expr:`),
        # spuriously raising "non-empty but no parseable entries". Ordering the
        # empty check first makes the legal empty array short-circuit cleanly.
        recording_names: List[str] = []
        if _RECORDING_EMPTY_RE.search(segment):
            pass  # legal empty array — operational / platform ship no recording rules
        else:
            recording_block = _RECORDING_RE.search(segment)
            if not recording_block:
                raise ValueError(
                    f"{PORTAL_REL}: pack '{pack}' has no parseable `recording: [...]` array "
                    "(and is not an explicit `recording: [],`)."
                )
            recording_names = _RECORD_ENTRY_RE.findall(recording_block.group(1))
            if not recording_names:
                raise ValueError(
                    f"{PORTAL_REL}: pack '{pack}' has a non-empty `recording` array but "
                    "no parseable `{ name: '...', expr: ... }` entries."
                )

        claims[pack] = {"alerts": alert_entries, "recording": recording_names}

    return claims


# --------------------------------------------------------------------------
# Check
# --------------------------------------------------------------------------
def check(repo: Path) -> Tuple[List[dict], dict]:
    """Return (findings, stats). A finding names the pack AND the rule."""
    shipped_alerts, shipped_records = collect_shipped_rules(repo)
    if not shipped_alerts:
        raise ValueError("no alert rules found in any rule tree — wrong repo root?")
    if not shipped_records:
        raise ValueError("no recording rules found in any rule tree — wrong repo root?")

    claims = parse_portal_claims(repo / PORTAL_REL)

    findings: List[dict] = []
    n_alerts = 0
    n_records = 0
    for pack, entry in sorted(claims.items()):
        for name, severity in entry["alerts"]:
            n_alerts += 1
            rule = shipped_alerts.get(name)
            if rule is None:
                findings.append({
                    "pack": pack,
                    "rule": name,
                    "kind": "unknown-alert",
                    "detail": "no alert with this name in any shipping rule tree",
                })
                continue
            real = rule["severity"]
            if severity != real:
                findings.append({
                    "pack": pack,
                    "rule": name,
                    "kind": "severity-mismatch",
                    "detail": f"portal says '{severity}', shipped rule says '{real}'",
                })

        for name in entry["recording"]:
            n_records += 1
            if name not in shipped_records:
                findings.append({
                    "pack": pack,
                    "rule": name,
                    "kind": "unknown-recording-rule",
                    "detail": "no recording rule with this name in any shipping rule tree",
                })

    stats = {
        "portal_packs": len(claims),
        "portal_alerts": n_alerts,
        "portal_recording": n_records,
        "shipped_alerts": len(shipped_alerts),
        "shipped_recording": len(shipped_records),
    }
    return findings, stats


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Portal rule-pack claim guard (alert name + severity vs shipped rules)"
    )
    parser.add_argument("--ci", action="store_true", help="exit 1 on any bogus claim")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    repo = _repo_root()
    portal = repo / PORTAL_REL
    if not portal.exists():
        print(f"ERROR: portal data file not found: {portal}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        findings, stats = check(repo)
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.json:
        print(json.dumps({"findings": findings, "stats": stats},
                         ensure_ascii=False, indent=2))
    else:
        if findings:
            print("❌ Portal advertises rules that do not match what we ship:\n")
            for f in findings:
                print(f"  [{f['pack']}] {f['rule']}")
                print(f"       {f['kind']}: {f['detail']}")
        print(
            f"\nPortal rule-pack claims: {stats['portal_alerts']} alert + "
            f"{stats['portal_recording']} recording claims across {stats['portal_packs']} "
            f"packs, checked against {stats['shipped_alerts']} shipped alerts / "
            f"{stats['shipped_recording']} shipped recording rules"
            + (f" — {len(findings)} bogus" if findings else " — all resolve ✅")
        )

    if findings and args.ci:
        print(
            f"\n❌ {len(findings)} portal claim(s) do not match the shipped rules. "
            f"Rules are the SSOT — fix {PORTAL_REL}, not the rule packs.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
