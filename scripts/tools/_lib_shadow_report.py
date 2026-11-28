"""The shadow-monitoring documents: the migration mapping migrate-rule writes,
the report `validate` writes and `shadow-verify` reads, and the readiness
`validate --auto-detect-convergence` writes and `cutover` reads (#3662, #4227).

⛔ The one definition of each:

* the mapping (``.migration/mapping/<tenant>.yaml``): its field names, written
  by ``migrate_rule.render_mapping`` and read by :func:`read_mapping` — the one
  reading `validate --mapping` and `shadow-verify preflight --mapping` share.
  The prefix-mapping of the numeric comparison (:data:`PREFIX_MAPPING_FIELDS`)
  is refused by name: :func:`is_prefix_mapping` (#4227, owner 2026-11-28).
* ``validation-report.csv``: its columns (:data:`REPORT_COLUMNS`), its
  statuses and its timestamp format. ``validate_migration.write_csv_report``
  writes the header from :data:`REPORT_COLUMNS`; ``shadow-verify
  --report-csv`` declares the same tuple as the header the file must have
  (``_lib_input.Document("csv", ..., columns=REPORT_COLUMNS)``), so a report
  of the numeric comparison (its old header) is refused, rc 2.
* ``cutover-readiness.json``: its fields (:data:`READINESS_FIELDS`), required
  by ``cutover_tenant``.

A report row is a selector count (``Kind`` ``selector``) or one alert series
of one side (``Kind`` ``alert``) (#4227). ``Tenant`` holds the tenant, and for
a row whose series carried ``da_cluster`` (ADR-006, #1200) it holds
:func:`tenant_cell` — ``<tenant> (da_cluster=<cluster>)``, the cluster escaped —
which :func:`split_tenant_cell` reads back.

Standard library and `_lib_cluster_identity` (itself standard library only).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import NamedTuple

from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL

# ── The migration mapping (#4227) ───────────────────────────────────────────

#: Top-level fields of the mapping migrate-rule writes.
MAPPING_TENANT = "tenant"
MAPPING_RULES_FILE = "rules_file"
MAPPING_RULES = "rules"
#: Fields of one rule entry.
RULE_GROUP = "group"
RULE_ALERT = "alert"
RULE_RECORD = "record"
RULE_MIGRATED_RECORD = "migrated_record"
RULE_SELECTORS = "selectors"
#: An alert migrate-rule adds, with no legacy twin: left out of the
#: alert-set comparison.
RULE_ADDED = "added"
#: The one alert migrate-rule adds (its expiry companion): the only name an
#: ``added`` entry may hold — any other would leave a compared alert out.
ADDED_ALERT = "MigratedRulesExpiring"
#: Fields of one selector pair: the selector before and after the pin.
SELECTOR_LEGACY = "legacy"
SELECTOR_PINNED = "pinned"

#: The fields of the prefix-mapping the numeric comparison read (removed,
#: #4227): a document holding any of them is refused, named.
PREFIX_MAPPING_FIELDS = ("old_query", "new_query", "original_metric")


class SelectorPair(NamedTuple):
    """One selector of one migrated rule, before (*legacy*) and after
    (*pinned*) the pin; *rule* is ``<group>/<alert or record>``."""

    rule: str
    legacy: str
    pinned: str


class MigrationMapping(NamedTuple):
    """A mapping as :func:`read_mapping` reads it."""

    tenant: str
    #: The migrated alerts that have a legacy twin, in file order.
    alerts: tuple
    #: The alerts migrate-rule added (``added: true``).
    added: tuple
    selectors: tuple


def is_prefix_mapping(doc: dict) -> bool:
    """Whether *doc* is the prefix-mapping of the numeric comparison: no
    mapping fields at the top, an entry holding one of
    :data:`PREFIX_MAPPING_FIELDS`."""
    if MAPPING_RULES in doc or MAPPING_TENANT in doc:
        return False
    return any(isinstance(v, dict) and any(f in v for f in PREFIX_MAPPING_FIELDS)
               for v in doc.values())


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def read_mapping(doc: dict, tenant_valid) -> "tuple[MigrationMapping | None, list[tuple[str, str]]]":
    """``(mapping, [])``, or ``(None, problems)``: every place of *doc* that is
    not what migrate-rule writes, as ``(where, expected)`` — *where* a field
    path (``rules[3].selectors[0].pinned``), *expected* one of ``str``
    (a non-empty string), ``list``, ``dict``, ``bool``, ``alert_or_record``
    (exactly one of the two), ``tenant_id`` (*tenant_valid* says no: the
    caller's ADR-035 rule, ``_lib_validation.is_valid_tenant_id``),
    ``unique`` (an alert named twice), ``added_alert`` (``added`` on an alert
    other than :data:`ADDED_ALERT`). All of them are named, none skipped
    (#4210 / #4221: an entry that cannot be read is not compared in silence)."""
    problems = []
    tenant = doc.get(MAPPING_TENANT)
    if not _nonempty_str(tenant):
        problems.append((MAPPING_TENANT, "str"))
    elif not tenant_valid(tenant):
        problems.append((MAPPING_TENANT, "tenant_id"))
    rules = doc.get(MAPPING_RULES)
    if not isinstance(rules, list):
        problems.append((MAPPING_RULES, "list"))
        rules = []
    alerts, added, selectors = [], [], []
    for i, entry in enumerate(rules):
        where = f"{MAPPING_RULES}[{i}]"
        if not isinstance(entry, dict):
            problems.append((where, "dict"))
            continue
        bad = len(problems)
        if not _nonempty_str(entry.get(RULE_GROUP)):
            problems.append((f"{where}.{RULE_GROUP}", "str"))
        has_alert, has_record = RULE_ALERT in entry, RULE_RECORD in entry
        if has_alert == has_record:
            problems.append((where, "alert_or_record"))
        name_field = RULE_ALERT if has_alert else RULE_RECORD
        name = entry.get(name_field)
        if has_alert != has_record and not _nonempty_str(name):
            problems.append((f"{where}.{name_field}", "str"))
        is_added = entry.get(RULE_ADDED, False)
        if not isinstance(is_added, bool):
            problems.append((f"{where}.{RULE_ADDED}", "bool"))
        pairs = entry.get(RULE_SELECTORS)
        if not isinstance(pairs, list):
            problems.append((f"{where}.{RULE_SELECTORS}", "list"))
            pairs = []
        rule = f"{entry.get(RULE_GROUP)}/{name}"
        entry_pairs = []
        for j, pair in enumerate(pairs):
            at = f"{where}.{RULE_SELECTORS}[{j}]"
            if not isinstance(pair, dict):
                problems.append((at, "dict"))
                continue
            for field in (SELECTOR_LEGACY, SELECTOR_PINNED):
                if not _nonempty_str(pair.get(field)):
                    problems.append((f"{at}.{field}", "str"))
            entry_pairs.append(SelectorPair(rule, pair.get(SELECTOR_LEGACY),
                                            pair.get(SELECTOR_PINNED)))
        if is_added is True and name != ADDED_ALERT:
            problems.append((f"{where}.{RULE_ADDED}", "added_alert"))
        if has_alert and name in alerts + added:
            problems.append((f"{where}.{RULE_ALERT}", "unique"))
        if len(problems) > bad:
            continue
        selectors.extend(entry_pairs)
        if has_alert:
            (added if is_added else alerts).append(name)
    if problems:
        return None, problems
    return MigrationMapping(tenant, tuple(alerts), tuple(added), tuple(selectors)), []


# ── The report (#3662, #4227) ───────────────────────────────────────────────

#: The header of validation-report.csv, in order.
REPORT_COLUMNS = (
    "Kind", "Rule", "Tenant", "State", "Labels",
    "Legacy", "Migrated", "Status", "Timestamp",
)

#: ``Kind`` of a row.
KIND_RULE = "rule"
KIND_SELECTOR = "selector"
KIND_ALERT = "alert"

#: ``Status`` of a selector row: both sides select series (or only the
#: pinned one); the legacy selector selects series and the pinned one none
#: (the pin's tenant assumption is wrong — the migrated rule never fires);
#: neither selects any (nothing to verify it by).
STATUS_OK = "ok"
STATUS_PIN_SELECTS_NOTHING = "pin_selects_nothing"
#: ``Status`` of an alert row: on both sides; on one side only; a legacy
#: alert of another tenant (the pin narrows it away by design).
STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_OTHER_TENANT = "other_tenant"
#: ``Status`` of a rule row: an alert of the mapping not loaded on that side
#: (on the migrated side as a migrated rule of the tenant); loaded in a group
#: evaluated less often than the window (a round between two evaluations
#: sees no ALERTS sample and would read as clean).
STATUS_RULE_NOT_LOADED = "rule_not_loaded"
STATUS_WINDOW_TOO_SHORT = "window_too_short"
#: Either kind: cannot be verified — a selector neither side selects, or a
#: legacy alert that carries neither ``tenant`` nor ``namespace``. Never
#: passes (owner 2026-11-28).
STATUS_UNVERIFIABLE = "unverifiable"

#: The statuses no waiting clears — the setup is wrong (rc 2): a pin that
#: selects nothing, a rule not loaded, a window no evaluation falls in.
SETUP_STATUSES = (STATUS_PIN_SELECTS_NOTHING, STATUS_RULE_NOT_LOADED, STATUS_WINDOW_TOO_SHORT)
#: The statuses that are a finding: the migrated rules do not behave as the
#: legacy ones (an alert set that differs) or cannot (:data:`SETUP_STATUSES`).
FINDING_STATUSES = (STATUS_MISMATCH,) + SETUP_STATUSES
#: The statuses that never pass: a finding, or nothing to verify by.
BLOCKING_STATUSES = FINDING_STATUSES + (STATUS_UNVERIFIABLE,)


def loaded_alert_rules(groups, alerts, *, migrated, tenant, label, value):
    """``{alert: [interval, ...]}`` — for each of *alerts*, the evaluation
    interval (seconds, None when the answer has none) of every group of
    *groups* (``_lib_prometheus.rules_groups``) that loads it: on the
    *migrated* side as a migrated rule of *tenant* (*label* = *value* and
    ``tenant``), otherwise as a rule without that label. An alert loaded
    nowhere maps to ``[]``."""
    out = {alert: [] for alert in alerts}
    for group in groups:
        interval = group.get("interval")
        interval = float(interval) if isinstance(interval, (int, float)) else None
        for rule in group["rules"]:
            labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}
            is_migrated = labels.get(label) == value
            if rule.get("name") not in out or is_migrated != migrated:
                continue
            if migrated and labels.get("tenant") != tenant:
                continue
            out[rule["name"]].append(interval)
    return out

#: How a row's ``Timestamp`` is written (UTC is not stated in the value:
#: the reader takes it as UTC, as it always has).
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The columns whose every value has a set form, as
#: ``_lib_input.Document(formats=...)`` declares it.
REPORT_FORMATS = (("Timestamp", TIMESTAMP_FORMAT),)


def parse_timestamp(value: str) -> datetime:
    """The ``Timestamp`` *value* of a row as an aware UTC datetime. Raises
    ``ValueError`` for a value not written in :data:`TIMESTAMP_FORMAT`."""
    return datetime.strptime(value.strip(), TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


# ── The readiness (#4227) ───────────────────────────────────────────────────

#: The fields of cutover-readiness.json, every one written by `validate
#: --auto-detect-convergence` and required by `cutover`.
READINESS_FIELDS = (
    "ready", "timestamp", "tenant", "round_count", "stability_window",
    "clean_rounds", "last_round", "recommendation",
)


# ── The Tenant cell (ADR-006, #1200) ────────────────────────────────────────

#: The cluster as the cell holds it: `\`, `(`, `)` and line ends escaped, so
#: the suffix is the one place of the cell an unescaped `(`…`)` pair closes at
#: the end — whatever the cluster's name or the tenant's holds.
_ESCAPES = {"\\": "\\\\", "(": "\\(", ")": "\\)", "\n": "\\n", "\r": "\\r"}
_UNESCAPES = {"\\": "\\", "(": "(", ")": ")", "n": "\n", "r": "\r"}
_CLUSTER_SUFFIX = re.compile(
    r"^(?P<tenant>.*) \(" + re.escape(CLUSTER_IDENTITY_LABEL)
    + r"=(?P<cluster>(?:[^()\\\n\r]|\\[\\()nr])+)\)$", re.DOTALL)
def tenant_cell(tenant: str, cluster: "str | None" = None) -> str:
    """What the ``Tenant`` column holds for the group (*tenant*, *cluster*):
    the tenant alone when the row has no cluster."""
    if not cluster:
        return tenant
    escaped = "".join(_ESCAPES.get(c, c) for c in cluster)
    return f"{tenant} ({CLUSTER_IDENTITY_LABEL}={escaped})"


def split_tenant_cell(cell: str) -> "tuple[str, str | None]":
    """``(tenant, cluster)`` of a ``Tenant`` cell; cluster None for a row
    written without one. Every cell :func:`tenant_cell` writes for a cluster
    reads back as written. ⚠️ A row WITHOUT a cluster holds the tenant as it
    is (the file such a run writes is what it always was), so a tenant whose
    own name ends in `` (da_cluster=…)`` reads back as a cluster row; no
    tenant id is spelled that way (ADR-035)."""
    m = _CLUSTER_SUFFIX.match(cell)
    if not m:
        return cell, None
    cluster = re.sub(r"\\(.)", lambda e: _UNESCAPES[e.group(1)], m["cluster"])
    return m["tenant"], cluster
