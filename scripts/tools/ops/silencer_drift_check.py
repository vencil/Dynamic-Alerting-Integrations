#!/usr/bin/env python3
"""silencer_drift_check.py — Alertmanager silence drift auditor.

For a given silences JSON dump (`amtool silence query -o json` output)
and a rule pack source (file or directory), reports silences whose
matchers no longer match any alert defined in the rule packs.

Use case (per docs/integration/troubleshooting-checklist.md §1.3.2):
    During staged adoption / Rule Pack upgrade, customers often have AM
    silences pointing at v1 alertnames or label values. When v2 ships
    with renamed alerts or changed label schemas, the silences silently
    fail to match — and customer code that was relying on the silence
    (silenced alert page = "we already know") gets a surprise alert
    storm.

Design choices (per issue #405 Category B):
  - Offline-first: NO `--am-url` flag. Tool eats a JSON file dumped from
    `amtool silence query -o json` separately, so it runs in CI / VPN /
    auth-proxy-walled environments without network access.
  - Same shape as `da-parser` / `da-guard`: file-in, structured-out.
  - Matches against a `--rule-source` path (file or directory of YAML).
    Recursively scans .yaml/.yml; ignores files without a `groups:` root
    (e.g. tenant `_defaults.yaml` is silently skipped).

Matcher semantics (Alertmanager / amtool):
    matcher = {name, value, isEqual, isRegex}
        isEqual=true,  isRegex=false → label == value
        isEqual=false, isRegex=false → label != value
        isEqual=true,  isRegex=true  → label =~ value (Go regex; fullmatch)
        isEqual=false, isRegex=true  → label !~ value
    A silence matches an alert iff ALL its matchers match the alert's
    label set. The label set of an alert includes the implicit
    `alertname=<alert>` label plus any `labels:` block on the rule.

Coverage reporting:
    The matcher engine has to compute each silence's full match set to
    decide orphan-hood. That set is reported (`coverage[]` in --json, a
    "Match coverage" section in text) instead of being collapsed to a
    boolean, because it answers the question that precedes drift: how
    much would this silence actually suppress?

    A silence fails in two opposite directions. An orphan matches
    nothing and suppresses nothing — the alert storm case above. An
    over-broad matcher (a dropped label, a `.*` left in) matches far
    more than intended and suppresses alerts nobody meant to silence.
    Orphan detection only sees the first. `counts.widest_match` and the
    per-silence `matched_rules` make the second visible before it is
    exercised in production.

Usage:
    amtool silence query -o json --alertmanager.url=... > silences.json
    da-tools silencer-drift-check --silences-file silences.json \\
        --rule-source rule-packs/

    # CI gate (exit 1 on orphans)
    da-tools silencer-drift-check --silences-file s.json \\
        --rule-source rule-packs/ --ci

    # Machine-readable
    da-tools silencer-drift-check --silences-file s.json \\
        --rule-source rule-packs/ --json

Exit codes:
    0  no orphaned silences (or orphans present but --ci not set)
    1  --ci mode detected orphaned silences
    2  caller error (file missing, parse failure, bad arg)

Malformed-silence handling:
    Silences with empty matchers or matchers missing the `name` field
    are partitioned into a separate `malformed_silences` report rather
    than silently classified as "not orphan" (the naive universal-match
    fallback would mask hand-edited / corrupted JSON). In --ci mode,
    malformed silences also fail the gate alongside orphans — that's a
    corruption signal automation should block on.

Known limitations (out of MVP scope, document for follow-up):
    - `_defaults.yaml` disable-list drift is NOT checked. Issue #405
      Category B included an `upgrade-check` framing for this; deferred
      to a sibling tool because the input artefact differs (silences
      JSON vs YAML `disable:` list) even though the check engine is
      shared.
    - Alert label values are read literally from the rule pack YAML.
      Templated label values (e.g. `{{ $labels.tenant }}`) compare
      literally, which means a silence using a specific tenant value
      won't match the templated rule — false positive orphan. Workaround:
      audit by group, not literal value, for templated alerts.
      ⚠️ Measured 2026-08-27 by loading `rule-packs/` through this
      tool's own `load_alerts()`: all 122 alerts carry a `tenant:`
      label and all 122 are templated (0 literal). Every silence
      `maintenance_scheduler.py` creates carries a literal
      `tenant=<id>` matcher. So this platform's own scheduler-created
      silences ALL land in this false-positive case — they report
      `matched_rules: 0` and are classified orphan. Do not wire `--ci`
      over a live silence dump until that is resolved; the gate would
      fail on every scheduled maintenance window.
      (Counting method matters here: a `grep` for the double-quoted
      form misses `rule-pack-custom-alerts.yaml`'s single-quoted
      `tenant: '{{ ... }}'` lines — and that pack, tenant self-service
      Custom Alerts, is the one most likely to grow a literal tenant
      value. Re-measure through `load_alerts()`, not a regex.)
    - Runtime semantics not modeled: a silence whose matchers DO match
      an alert in the rule source is classified non-orphan, even if the
      alert never actually fires (e.g. its `expr:` always returns
      empty). This tool is a static analyzer over the rule source.
    - `matched_rules` inherits that limitation and must be read as
      "rule definitions in the rule source", NOT "alerts currently
      firing": a non-zero count says those rules exist and the matchers
      select them, never that anything is firing right now. Answering
      the firing-set question needs live Prometheus/Alertmanager state,
      which this tool deliberately does not reach for (offline-first,
      see Design choices above).
    - ⚠️ Against a templated rule source the two limitations compound
      rather than offset: a silence naming a concrete tenant does not
      under-count, it collapses to zero and is reported as an orphan
      (see the measurement above). Read a `0` on a tenant-scoped
      silence as "this tool cannot see templated labels", not as
      "the rule source has nothing for that tenant".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# See _lib_compat.py module docstring for the rationale + sys.path layout.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import format_json_report  # noqa: E402

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML not installed. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(EXIT_CALLER_ERROR)


# ─── Loading ──────────────────────────────────────────────────────────


def load_silences(path: Path) -> list[dict] | None:
    """Parse an amtool silence dump. Returns the list of silence dicts."""
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print(
            f"ERROR: {path} top-level must be a JSON array (amtool silence "
            f"query -o json shape), got {type(data).__name__}",
            file=sys.stderr,
        )
        return None
    return data


def _yaml_files_in(source: Path) -> list[Path]:
    """Enumerate .yaml/.yml files at source (file or dir, recursive)."""
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(
            [
                *source.rglob("*.yaml"),
                *source.rglob("*.yml"),
            ]
        )
    return []


def extract_alerts_from_pack(pack: dict, source_path: str) -> list[dict]:
    """Pull all `alert:` rules out of a parsed rule pack.

    Returns list of `{name, labels, source}` dicts. The label set
    includes the implicit `alertname=<name>` plus any `labels:` block.
    Recording rules and malformed entries are skipped silently.
    """
    if not isinstance(pack, dict):
        return []
    groups = pack.get("groups")
    if not isinstance(groups, list):
        return []
    alerts = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for rule in group.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            name = rule.get("alert")
            if not name or not isinstance(name, str):
                continue
            rule_labels = rule.get("labels") or {}
            if not isinstance(rule_labels, dict):
                rule_labels = {}
            # AM silence matchers see `alertname` implicitly; build the
            # effective label set the same way Prom/AM does at runtime.
            effective_labels = {"alertname": name}
            for k, v in rule_labels.items():
                effective_labels[str(k)] = str(v)
            alerts.append(
                {
                    "name": name,
                    "labels": effective_labels,
                    "source": source_path,
                    "group": group.get("name", "<unnamed>"),
                }
            )
    return alerts


def load_alerts(rule_source: Path) -> tuple[list[dict], list[str]]:
    """Load all alerts from a rule-source path. Returns (alerts, errors)."""
    errors: list[str] = []
    files = _yaml_files_in(rule_source)
    if not files:
        return [], [f"no YAML files found at {rule_source}"]

    alerts: list[dict] = []
    for f in files:
        try:
            with f.open(encoding="utf-8") as h:
                data = yaml.safe_load(h)
        except OSError as exc:
            errors.append(f"cannot read {f}: {exc}")
            continue
        except yaml.YAMLError as exc:
            errors.append(f"invalid YAML in {f}: {exc}")
            continue
        if not isinstance(data, dict):
            # File is YAML but not a mapping at top — skip silently
            # (e.g. some inventory files use YAML lists).
            continue
        # Only files with `groups:` at root contribute alerts; skip
        # tenant `_defaults.yaml` etc. silently.
        if "groups" not in data:
            continue
        alerts.extend(extract_alerts_from_pack(data, source_path=str(f)))
    return alerts, errors


# ─── Matcher engine ───────────────────────────────────────────────────


def matcher_applies(matcher: dict, alert_labels: dict[str, str]) -> bool:
    """Decide whether an AM silence matcher applies to a given alert's labels.

    AM matcher semantics (mirrors prometheus/alertmanager's matcher.Match):
      - isEqual=true,  isRegex=false → label == value
      - isEqual=false, isRegex=false → label != value
      - isEqual=true,  isRegex=true  → label =~ value  (Go regex; fullmatch)
      - isEqual=false, isRegex=true  → label !~ value

    Absent labels: an equality matcher against an absent label is treated
    as "label value is empty string" per AM's semantics — so `foo="bar"`
    against an alert without `foo` does NOT match (empty != bar), and
    `foo!="bar"` against an absent label DOES match (empty != bar is
    true). Same logic for regex.
    """
    name = matcher.get("name")
    value = matcher.get("value", "")
    is_regex = bool(matcher.get("isRegex", False))
    is_equal = bool(matcher.get("isEqual", True))

    # Absent label is treated as empty string per AM conventions.
    label_value = str(alert_labels.get(name, ""))

    if is_regex:
        try:
            pattern = re.compile(str(value))
        except re.error:
            # Invalid regex — conservatively don't match. This means an
            # invalid-regex silence is reported as orphan, which surfaces
            # the bug to the operator.
            return False
        match = pattern.fullmatch(label_value) is not None
    else:
        match = label_value == str(value)

    return match if is_equal else not match


def detect_malformed(silence: dict) -> str | None:
    """Inspect a silence for structural issues. Returns the reason if
    malformed, else None.

    Why this matters: a silence with no matchers OR a matcher missing
    `name` cannot meaningfully participate in the orphan check. The naive
    "match everything if no matchers" behaviour would silently classify
    a malformed silence as not-orphan (false negative). Real AM silences
    always have well-formed matchers (the AM API rejects bad input on
    create); seeing one in a JSON dump means the JSON was hand-edited
    or corrupted — exactly the case we want to surface, not hide.
    """
    matchers = silence.get("matchers")
    if not isinstance(matchers, list):
        kind = type(matchers).__name__ if matchers is not None else "missing"
        return f"matchers field is {kind}, expected a non-empty JSON array"
    if not matchers:
        return "matchers list is empty (real AM silences always have matchers)"
    for i, m in enumerate(matchers):
        if not isinstance(m, dict):
            return f"matcher[{i}] is not a JSON object"
        name = m.get("name")
        if not name or not isinstance(name, str):
            return f"matcher[{i}] missing or non-string 'name' field"
    return None


def silence_matches_alert(silence: dict, alert: dict) -> bool:
    """A silence matches an alert iff ALL its matchers match.

    Caller must have already filtered out malformed silences via
    `detect_malformed`; this function assumes well-formed matchers.
    """
    matchers = silence.get("matchers") or []
    return all(matcher_applies(m, alert["labels"]) for m in matchers)


def is_silence_active(silence: dict, *, at: datetime | None = None) -> bool:
    """Decide whether a silence is currently active at `at` (default now).

    AM silence shape includes `status.state` (active/expired/pending)
    or `startsAt` / `endsAt` (ISO 8601 timestamps). Prefer status.state
    when present (newer amtool dumps); fall back to timestamp comparison.
    """
    state = (silence.get("status") or {}).get("state")
    if state == "active":
        return True
    if state in ("expired", "pending"):
        return False
    # No status.state — check timestamps.
    if at is None:
        at = datetime.now(tz=timezone.utc)
    try:
        starts = silence.get("startsAt")
        ends = silence.get("endsAt")
        if starts and ends:
            s = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
            return s <= at <= e
    except (TypeError, ValueError):
        # Malformed timestamps — treat as active (don't filter out
        # silences we can't classify).
        return True
    return True


# ─── Drift check ──────────────────────────────────────────────────────


def check_drift(
    silences: list[dict],
    alerts: list[dict],
    *,
    include_inactive: bool = False,
) -> dict:
    """Compute the orphaned-silence report.

    For each silence in scope (active by default), iterate every alert
    in the corpus; if NO alert matches all the silence's matchers, the
    silence is orphaned — it can no longer suppress anything.

    Malformed silences (empty matchers, missing matcher name, etc.) are
    partitioned into `malformed_silences` separately. Without this, the
    naive code-path would classify them as "not orphan" (because the
    universal-match fallback for empty matchers hides them), masking
    bad JSON input.
    """
    in_scope: list[dict] = []
    malformed: list[dict] = []
    skipped_inactive = 0
    for s in silences:
        reason = detect_malformed(s)
        if reason is not None:
            malformed.append(
                {
                    "silence_id": s.get("id", "<unknown>"),
                    "reason": reason,
                    "raw_matchers": s.get("matchers"),
                }
            )
            continue
        if include_inactive or is_silence_active(s):
            in_scope.append(s)
        else:
            skipped_inactive += 1

    orphans: list[dict] = []
    coverage: list[dict] = []
    for silence in in_scope:
        matching_alerts = [a for a in alerts if silence_matches_alert(silence, a)]
        # The match set is the answer to "what would this silence actually
        # suppress" — report it, don't collapse it to a boolean. See the
        # `coverage` note in the module docstring for why.
        coverage.append(
            {
                "silence_id": silence.get("id", "<unknown>"),
                "matchers": silence.get("matchers", []),
                "matched_rules": len(matching_alerts),
                "matched_alertnames": sorted({a["name"] for a in matching_alerts}),
            }
        )
        if not matching_alerts:
            orphans.append(
                {
                    "silence_id": silence.get("id", "<unknown>"),
                    "matchers": silence.get("matchers", []),
                    "comment": silence.get("comment", ""),
                    "created_by": silence.get("createdBy", ""),
                    "starts_at": silence.get("startsAt"),
                    "ends_at": silence.get("endsAt"),
                    "reason": "no alert in rule source matches all matchers",
                }
            )

    return {
        "silences_total": len(silences),
        "silences_in_scope": len(in_scope),
        "silences_inactive_skipped": skipped_inactive,
        "alerts_total": len(alerts),
        "orphans": orphans,
        "malformed_silences": malformed,
        "coverage": coverage,
        "counts": {
            "silences_total": len(silences),
            "in_scope": len(in_scope),
            "inactive_skipped": skipped_inactive,
            "malformed": len(malformed),
            "alerts": len(alerts),
            "orphans": len(orphans),
            "widest_match": max((c["matched_rules"] for c in coverage), default=0),
        },
    }


# ─── Rendering ────────────────────────────────────────────────────────


def _matcher_to_str(m: dict) -> str:
    """Pretty-print one matcher in PromQL-ish form for human output."""
    name = m.get("name", "<noname>")
    value = m.get("value", "")
    is_eq = bool(m.get("isEqual", True))
    is_re = bool(m.get("isRegex", False))
    op = {
        (True, False): "=",
        (False, False): "!=",
        (True, True): "=~",
        (False, True): "!~",
    }[(is_eq, is_re)]
    return f'{name}{op}"{value}"'


def render_text(
    report: dict, *, silences_path: str, rule_source: str, errors: list[str]
) -> None:
    """Human-readable rendering of the drift report."""
    c = report["counts"]
    print("Silencer Drift Check")
    print(f"  silences-file: {silences_path}")
    print(f"  rule-source:   {rule_source}")
    print()
    print(
        f"Summary: {c['silences_total']} silences total / "
        f"{c['in_scope']} in scope (active) / "
        f"{c['inactive_skipped']} inactive skipped / "
        f"{c.get('malformed', 0)} malformed"
    )
    print(
        f"         {c['alerts']} alerts loaded / "
        f"{c['orphans']} orphaned (no alert matches all matchers) / "
        f"widest silence matches {c.get('widest_match', 0)} rule(s)"
    )
    print()

    if errors:
        print("⚠️  Errors loading rule source:")
        for e in errors:
            print(f"    {e}")
        print()

    if report.get("malformed_silences"):
        print(
            "⚠️  Malformed silences (cannot participate in orphan check — "
            "JSON likely hand-edited or corrupted):"
        )
        for m in report["malformed_silences"]:
            print(f"    silence {m['silence_id']}: {m['reason']}")
        print()

    if report["orphans"]:
        print("⚠️  Orphaned silences (will silently fail to suppress in v2):")
        for o in report["orphans"]:
            matchers_str = ", ".join(_matcher_to_str(m) for m in o["matchers"])
            print(f"    silence {o['silence_id']}")
            print(f"        matchers: {{{matchers_str}}}")
            if o["comment"]:
                print(f"        comment:  {o['comment']}")
            if o["created_by"]:
                print(f"        author:   {o['created_by']}")
            if o["ends_at"]:
                print(f"        endsAt:   {o['ends_at']}")
        print()
    else:
        print("✓ No orphaned silences detected.")

    render_coverage(report)


_COVERAGE_NAMES_SHOWN = 3


def _alertnames_preview(names: list[str]) -> str:
    """Render up to _COVERAGE_NAMES_SHOWN names, stating what was elided.

    The overflow is spelled out ("+N more") rather than trimmed silently:
    a truncated list that reads as complete is the same failure this
    section exists to expose. The full list is always in --json.
    """
    if not names:
        return "(none — orphan, see above)"
    shown = ", ".join(names[:_COVERAGE_NAMES_SHOWN])
    extra = len(names) - _COVERAGE_NAMES_SHOWN
    return f"{shown} +{extra} more" if extra > 0 else shown


def render_coverage(report: dict) -> None:
    """Print what each in-scope silence actually matches, widest first.

    Orphan detection answers "does this silence still hit anything?".
    This answers the question that comes before it: "how much does it
    hit?" — the signal that catches an over-broad matcher, which fails
    the opposite way from an orphan and is otherwise invisible.
    """
    coverage = report.get("coverage") or []
    if not coverage:
        return
    print()
    print("Match coverage (rule-pack alerts each in-scope silence matches):")
    for c in sorted(
        coverage, key=lambda x: (-x["matched_rules"], str(x["silence_id"]))
    ):
        names = _alertnames_preview(c["matched_alertnames"])
        print(f"    silence {c['silence_id']}: {c['matched_rules']} rule(s) — {names}")


def compute_exit_code(report: dict, *, ci: bool) -> int:
    """0 unless --ci AND (orphans OR malformed silences) present.

    Malformed silences in --ci mode also fail the gate — they're a
    signal that the input JSON is corrupted or hand-mangled, which
    should block any automated workflow downstream of this check.
    """
    if ci and (
        report["counts"]["orphans"] > 0
        or report["counts"].get("malformed", 0) > 0
    ):
        return EXIT_VIOLATION
    return EXIT_OK


# ─── Main ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=(
            "Audit Alertmanager silences against rule pack alerts; "
            "report silences whose matchers no longer match anything."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Capture silences (separate step, requires AM access)\n"
            "  amtool silence query -o json --alertmanager.url=http://... > silences.json\n"
            "\n"
            "  # Offline drift check\n"
            "  da-tools silencer-drift-check --silences-file silences.json \\\n"
            "      --rule-source rule-packs/\n"
            "\n"
            "  # CI gate (exit 1 on orphans)\n"
            "  da-tools silencer-drift-check --silences-file silences.json \\\n"
            "      --rule-source rule-packs/ --ci\n"
        ),
    )
    ap.add_argument(
        "--silences-file",
        required=True,
        help="Path to amtool silence dump (amtool silence query -o json output).",
    )
    ap.add_argument(
        "--rule-source",
        required=True,
        help=(
            "Path to a rule pack YAML file, or a directory containing "
            "rule pack YAML files (scanned recursively for .yaml/.yml)."
        ),
    )
    ap.add_argument(
        "--include-inactive",
        action="store_true",
        help=(
            "Include expired / pending silences in the check. Default: "
            "only active silences are checked (most common: 'is my "
            "current AM silence set still hitting v2 alerts?')."
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    ap.add_argument(
        "--ci",
        action="store_true",
        help=(
            "Exit 1 when orphaned silences are detected. Without --ci, "
            "the report prints and the tool exits 0 regardless."
        ),
    )
    args = ap.parse_args(argv)

    silences_path = Path(args.silences_file)
    rule_source = Path(args.rule_source)

    silences = load_silences(silences_path)
    if silences is None:
        return EXIT_CALLER_ERROR

    if not rule_source.exists():
        print(f"ERROR: --rule-source path does not exist: {rule_source}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    alerts, errors = load_alerts(rule_source)
    if not alerts and errors:
        # No alerts AND errors → caller probably typo'd or pointed at wrong dir.
        # Empty alerts with no errors is legitimate (rule source is empty).
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    report = check_drift(silences, alerts, include_inactive=args.include_inactive)
    report["silences_file"] = str(silences_path)
    report["rule_source"] = str(rule_source)
    report["errors"] = errors

    if args.json:
        print(format_json_report(report, default=str))
    else:
        render_text(
            report,
            silences_path=str(silences_path),
            rule_source=str(rule_source),
            errors=errors,
        )

    return compute_exit_code(report, ci=args.ci)


if __name__ == "__main__":
    sys.exit(main())
