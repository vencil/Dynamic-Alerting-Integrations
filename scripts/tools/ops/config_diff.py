#!/usr/bin/env python3
"""
config_diff.py — What a conf.d change does, for GitOps PR review (ADR-037).

Renders both conf.d trees — the base and the PR — with the two programs that
turn conf.d into what runs, and compares what they render to:

* `da-guard served-values`: what the exporter serves for each tenant
  (thresholds, severities, settings, state filters, custom alerts);
* the route generator's `--output-configmap`: the whole Alertmanager
  configuration (route nodes, receivers, inhibit rules).

So a platform change (`_defaults.yaml`, `_profiles.yaml`, a root
`_platform.yaml`, routing defaults) is reported for the tenants it reaches.
Nothing here reads the YAML itself; `_render_diff` renders and compares.

The time-dependent settings (schedules, silences, maintenance windows) are
read at one instant, `--at`, the same on both sides and printed on the report.
Without `--at` it is the time of the run.

Exit codes: 0 no change; 1 a change, or something not compared (a changed
`_`-prefixed file, a base side that does not render); 2 no result (the PR side
or a tool failed, or bad arguments). See ADR-037 decision 4.

Usage:
  python3 scripts/tools/ops/config_diff.py --old-dir conf.d.base --new-dir conf.d/
  python3 scripts/tools/ops/config_diff.py --old-dir conf.d.base --new-dir conf.d/ \\
      --at 2026-10-10T03:00:00Z --format json
"""
import argparse
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    format_json_report,
    is_disabled as _is_disabled,
)
from _lib_redact import REDACTED, SECRET_FIELDS, redact, redact_pair  # noqa: E402
from _lib_redact import is_secret_field as _lib_redact_is_secret  # noqa: E402
from _lib_confd import iter_config_files, printable_name  # noqa: E402  (#1420)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _threshold_alerts import alerts_for_key  # noqa: E402
import _render_diff as rd  # noqa: E402

# The JSON report's version. A reader refuses one it does not know.
JSON_SCHEMA = "da-tools.config-diff/v1"

# GitHub silently rejects (422 Unprocessable Entity) issue/PR comments over
# 65,536 chars. The config-diff bot posts render_markdown() output verbatim, so
# an oversized diff (e.g. a 150-recipe bulk refactor) would make the comment
# vanish — the highest-risk PR then reaches the reviewer with NO diff hint at
# all. Cap with ~5KB headroom for the workflow's wrapper marker/footer (Reef 2).
GITHUB_COMMENT_HARD_LIMIT = 65_536
COMMENT_SAFETY_LIMIT = 60_000

# How many changed platform file names the Markdown report spells out; the
# rest is a count. The JSON report lists them all.
UNCOVERED_LIST_LIMIT = 50

# What `--at` takes: RFC3339, as `da-guard served-values --at` parses it.
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _platform_contents(dir_path):
    """{relative POSIX path: bytes} for every `_`-prefixed config file of
    `dir_path`, at any depth — the exporter's walk (`iter_config_files`).

    A file that cannot be read raises: it is an IO failure, exit 2 through
    `main`'s catch-all, not a change."""
    root = Path(dir_path)
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in iter_config_files(root) if path.name.startswith("_")}


def compute_uncovered_files(old_dir, new_dir):
    """Changed `_`-prefixed files (#1420), sorted: they exit 1 whatever the
    renders show.

    The renders compare what these files do to the exporter's values and to
    the Alertmanager configuration, but not every setting in them reaches
    either (a platform `_custom_alerts` recipe, `instance_tenant_mapping`,
    ...), and which ones do is not yet checked file by file (ADR-037
    decision 3). Until it is, such a file is listed when its bytes differ,
    or it exists on one side only — a comment-only edit included: "review
    this file" is the claim, not "this changed what runs".
    """
    old = _platform_contents(old_dir)
    new = _platform_contents(new_dir)
    return sorted(
        rel for rel in set(old) | set(new)
        if rel not in old or rel not in new or old[rel] != new[rel]
    )


def classify_change(old_val, new_val):
    """Classify a single metric change.

    Returns one of: 'tighter', 'looser', 'added', 'removed', 'toggled', 'modified'.
    """
    if old_val is None:
        return "added"
    if new_val is None:
        return "removed"

    old_disabled = _is_disabled(old_val)
    new_disabled = _is_disabled(new_val)

    if old_disabled and not new_disabled:
        return "toggled"  # re-enabled
    if not old_disabled and new_disabled:
        return "toggled"  # disabled

    # Both are numeric — compare thresholds
    try:
        old_num = float(old_val) if not isinstance(old_val, dict) else None
        new_num = float(new_val) if not isinstance(new_val, dict) else None
    except (TypeError, ValueError):
        old_num = None
        new_num = None

    if old_num is not None and new_num is not None:
        if new_num < old_num:
            return "tighter"
        if new_num > old_num:
            return "looser"
        return "unchanged"  # same numeric value

    return "modified"


def compute_diff(old_configs, new_configs):
    """Compare two sets of tenant configs.

    Returns {tenant: [{"key", "old", "new", "change"}]}.
    Only tenants with actual changes are included.
    """
    all_tenants = set(old_configs.keys()) | set(new_configs.keys())
    diffs = {}

    for tenant in sorted(all_tenants):
        old_metrics = old_configs.get(tenant, {})
        new_metrics = new_configs.get(tenant, {})
        all_keys = set(old_metrics.keys()) | set(new_metrics.keys())

        changes = []
        for key in sorted(all_keys):
            old_val = old_metrics.get(key)
            new_val = new_metrics.get(key)
            if old_val == new_val:
                continue

            change_type = classify_change(old_val, new_val)
            if change_type == "unchanged":
                continue

            changes.append({
                "key": key,
                "old": old_val,
                "new": new_val,
                "change": change_type,
            })

        if changes:
            diffs[tenant] = changes

    return diffs


# Credential fields and how they print: one rule for every report
# (`_lib_redact`, the fields Alertmanager types as secret).
_CREDENTIAL_KEYS = SECRET_FIELDS
_REDACTED = REDACTED


def _mapping_changes(old, new, prefix):
    """Leaf-level changes between two mappings, keys joined with dots.

    Where both sides hold a mapping the walk goes down, so a changed
    group_wait reads `_routing.group_wait: 30s → 1m` instead of the whole
    `_routing` printed twice. Anything else (scalars, lists) is compared whole.
    Presence decides added / removed, so a key set to null is still a key that
    exists. Values are compared first and redacted after (_redact_leaf), so a
    rotated credential is still reported — only its values are not.
    """
    changes = []
    for key in sorted(set(old) | set(new), key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key in old and key in new:
            if old[key] == new[key]:
                continue
            # A credential container (`http_config`, a header map, …) is
            # compared and redacted whole: a leaf inside it need not be
            # named like a credential to hold one.
            if (isinstance(old[key], dict) and isinstance(new[key], dict)
                    and not _lib_redact_is_secret(key)):
                changes.extend(_mapping_changes(old[key], new[key], path))
                continue
            change = "modified"
        else:
            change = "added" if key not in old else "removed"
        old_v, new_v = _redact_leaf(key, old.get(key), new.get(key))
        changes.append({"key": path, "old": old_v,
                        "new": new_v, "change": change})
    return changes


_redact = redact
_redact_leaf = redact_pair


def compute_setting_diff(old_settings, new_settings):
    """{tenant: [{"key", "old", "new", "change"}]} for `_` settings.

    change is 'added' / 'removed' / 'modified'; `key` is a dotted path when
    the change sits inside a mapping (see _mapping_changes).
    """
    result = {}
    for tenant in sorted(set(old_settings) | set(new_settings)):
        changes = _mapping_changes(old_settings.get(tenant, {}),
                                   new_settings.get(tenant, {}), "")
        if changes:
            result[tenant] = changes
    return result


def _format_recipe_value(val):
    """Recipe field value as code-span-safe text (dicts/lists → compact JSON).

    Neutralizes backticks so the value cannot break out of the markdown code
    span the caller wraps it in (F5: PR bot-comment injection defence — these
    values are rendered PRE-compile, so they are untrusted). Inside a code span
    every other metachar (`<`, `>`, `|`, `</details>`) renders literally, so a
    backtick is the only break-out we must close.
    """
    if val is None:
        return "—"
    if isinstance(val, (dict, list)):
        text = json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(val)
    # Neutralize backticks (code-span break-out, F5) AND newlines: a raw \n
    # breaks a markdown code span / list item and shatters the rendered comment
    # (Reef 4). Collapse CR/LF to a space — this section uses code spans, where
    # an <br> would render as literal text rather than a break.
    return (
        text.replace("`", "'")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _code_span(val):
    """Wrap a (possibly attacker-controlled) value in a backtick code span,
    after neutralizing internal backticks (F5 injection defence)."""
    return f"`{_format_recipe_value(val)}`"


def _threshold_disabled(val):
    """Is a custom-alert threshold a three-state opt-out? Mirrors the exporter
    (custom_alert.go): split the optional `:severity`, then test the value part
    against the disabled set — so `off`/`disabled`/`false` and `:severity`-
    suffixed forms (e.g. ``disable:warning``) are caught, not just ``disable``
    (R2 self-review: keep parity with blast_radius._threshold_disabled)."""
    raw = str(val).strip()
    value = raw.rsplit(":", 1)[0] if ":" in raw else raw
    return _is_disabled(value)


def _field_change_annotation(field, old, new):
    """Flag field changes that SILENCE an alert — high-regret and easy to miss
    behind a plain ``[modified]`` line (Reef 1: ``disable``/``silent`` semantic
    camouflage). A `threshold` flipped to a disabled value or `mode` flipped to
    ``silent`` is effectively a delete/silence wearing a 'modified' disguise.
    """
    if field == "threshold" and _threshold_disabled(new) and not _threshold_disabled(old):
        return " — :warning: **DISABLED (alert silenced)**"
    if field == "mode" and str(new) == "silent" and str(old) != "silent":
        return " — :warning: **mode→silent (paging suppressed)**"
    return ""


def compute_recipe_field_changes(old_r, new_r):
    """Per-field diff between two recipe dicts.

    Returns list of {"field", "old", "new"} for every differing key.
    """
    all_keys = set(old_r or {}) | set(new_r or {})
    changes = []
    for key in sorted(all_keys):
        old_val = (old_r or {}).get(key)
        new_val = (new_r or {}).get(key)
        if old_val != new_val:
            changes.append({"field": key, "old": old_val, "new": new_val})
    return changes


def compute_custom_alert_diff(old_alerts, new_alerts):
    """Diff tenant-own `_custom_alerts` recipes between two snapshots.

    Args:
        old_alerts / new_alerts: {tenant: {recipe_name: recipe_dict}} as
            returned by :func:`load_custom_alerts_from_dir`.

    Returns {tenant: [{"name", "change", "old", "new", "field_changes"}]}.
    ``change`` is one of: 'added', 'removed', 'modified'. Recipes are matched by
    their per-tenant-unique ``name``. Only tenants with at least one change are
    included.

    Note (Reef 3): identity is name-based, so a pure RENAME (same params, new
    `name`) surfaces as a Remove + Add pair, not a 'renamed' change. Stateless
    rename heuristics aren't worth the false-match risk here; the reviewer reads
    two entries. Documented so a future maintainer doesn't 'fix' it as a bug.
    """
    all_tenants = set(old_alerts.keys()) | set(new_alerts.keys())
    result = {}

    for tenant in sorted(all_tenants):
        old_recipes = old_alerts.get(tenant, {})
        new_recipes = new_alerts.get(tenant, {})
        all_names = set(old_recipes.keys()) | set(new_recipes.keys())

        changes = []
        for name in sorted(all_names):
            old_r = old_recipes.get(name)
            new_r = new_recipes.get(name)
            if old_r == new_r:
                continue

            if old_r is None:
                changes.append({
                    "name": name, "change": "added",
                    "old": None, "new": new_r, "field_changes": [],
                })
            elif new_r is None:
                changes.append({
                    "name": name, "change": "removed",
                    "old": old_r, "new": None, "field_changes": [],
                })
            else:
                changes.append({
                    "name": name, "change": "modified",
                    "old": old_r, "new": new_r,
                    "field_changes": compute_recipe_field_changes(old_r, new_r),
                })

        if changes:
            result[tenant] = changes

    return result


# ── The same three sections, read from served-values (ADR-037) ─────────────
#
# config-diff compares what the exporter serves on each side (`da-guard
# served-values`), not each tenant file's own YAML. These readers turn one
# side's served tenants into the shapes compute_diff / compute_setting_diff /
# compute_custom_alert_diff compare (`served_sections`).
#
# What changed against the YAML readers they replaced, by decision (#1516):
# * thresholds are the values the exporter serves, so a change a tenant
#   inherits (a `defaults:` value it does not override) is reported for it;
# * a tenant added or removed is not listed in the settings section — the
#   threshold section already names it, and every reserved key the exporter
#   reads would otherwise be listed as added;
# * a custom alert is the rows the exporter serves for it: `threshold` is
#   "<value>:<severity>" per row, `mode` and `recipe_id` are its labels, so an
#   `op` / `window` / `for` change shows as a `recipe_id` change; a recipe
#   whose rows are gone is reported removed with SERVED_REMOVED_NOTE, as the
#   rows cannot tell a deleted recipe from a disabled threshold or a recipe
#   the exporter rejects.

SERVED_STATE_FILTERS_KEY = "state_filters"
SERVED_TIME_BOXES_KEY = "time_boxes"
SERVED_REMOVED_NOTE = ("no longer served: the recipe was deleted, its threshold "
                       "disabled, or the exporter rejected it (see da-guard's stderr)")


def _served_number(value):
    """A served threshold as written in YAML: 85.0 -> 85."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _served_threshold(key, value, severity, schedule):
    """One threshold key's reading, comparable across both sides.

    The number alone when the severity is the key's own (`_critical` keys are
    critical, every other key warning), `"<number>:<severity>"` otherwise —
    the spelling a tenant file uses for it. A key whose served value changes
    during the day, or that carries a time-box the exporter honours
    (`expires:`), is a mapping of both, shown as "(scheduled)".
    """
    if schedule is not None and (len(schedule.segments) > 1 or schedule.expires is not None):
        return {"schedule": [
            (seg.start, seg.end, _served_number(seg.value), seg.severity)
            for seg in schedule.segments],
            "expires": schedule.expires, "expired": schedule.expired}
    natural = "critical" if key.endswith("_critical") else "warning"
    num = _served_number(value)
    return num if severity == natural else f"{num}:{severity}"


def served_metric_configs(tenants):
    """{tenant: {key: reading}} for compute_diff, from served-values tenants.

    Threshold keys are what /metrics serves (`_served_threshold`); a key the
    tenant switched off, or that the exporter cannot serve, is its value as
    written (`unserved`, e.g. "disable"). Reserved (`_`) keys are not here.
    """
    out = {}
    for tenant, tv in tenants.items():
        flat = {}
        for key, raw in (tv.unserved or {}).items():
            if not str(key).startswith("_"):
                flat[str(key)] = raw
        for key, severity in tv.severities.items():
            schedule = (tv.schedules or {}).get(key)
            flat[key] = _served_threshold(key, tv.values.get(key), severity, schedule)
        out[tenant] = flat
    return out


def served_setting_configs(tenants):
    """{tenant: {"_key": value}} for compute_setting_diff: the reserved keys
    the exporter reads for each tenant (`_custom_alerts` aside, in its own
    section), the reserved keys it does not serve as written, and the state
    filters on for the tenant with their severity (SERVED_STATE_FILTERS_KEY),
    and the time-boxes the exporter honours on `_silent_mode` /
    `_state_maintenance` (SERVED_TIME_BOXES_KEY: extending a silence or making
    it open-ended changes nothing the mode's own reading shows). Neither is
    `_`-prefixed, so no key a tenant writes can collide with them.

    A routing key the exporter does not serve arrives as the text written
    (a receiver it rejects, a `type` left out): the text may hold a webhook
    URL or token that _redact cannot see inside a string. It is kept here so
    a change to it is still a change; served_setting_diff prints it as
    _REDACTED."""
    out = {}
    for tenant, tv in tenants.items():
        settings = {}
        for key, value in (tv.unserved or {}).items():
            if str(key).startswith("_"):
                settings[str(key)] = value
        for key, value in tv.values.items():
            if str(key).startswith("_"):
                settings[key] = value
        settings.pop("_custom_alerts", None)
        settings[SERVED_STATE_FILTERS_KEY] = dict(tv.state_filters or {})
        settings[SERVED_TIME_BOXES_KEY] = {
            key: [{"target": b.target, "expires": b.expires, "expired": b.expired} for b in boxes]
            for key, boxes in (tv.time_boxes or {}).items()}
        out[tenant] = settings
    return out


def served_setting_diff(old_tenants, new_tenants):
    """compute_setting_diff over the tenants both sides serve; a reserved key
    served as the text written (`unserved`) is printed as _REDACTED whatever
    its name — `_routng` or `_Routing` with a receiver under it is text no
    key-name rule can see into (see served_setting_configs)."""
    both = set(old_tenants) & set(new_tenants)
    old = served_setting_configs({t: v for t, v in old_tenants.items() if t in both})
    new = served_setting_configs({t: v for t, v in new_tenants.items() if t in both})
    as_text = {t: {str(k) for side in (old_tenants[t], new_tenants[t])
                   for k, v in (side.unserved or {}).items()
                   if str(k).startswith("_") and isinstance(v, str)}
               for t in both}
    diff = compute_setting_diff(old, new)
    for tenant, changes in diff.items():
        for c in changes:
            if "." not in str(c["key"]) and str(c["key"]) in as_text[tenant]:
                c["old"], c["new"] = (_REDACTED if isinstance(v, str) else v
                                      for v in (c["old"], c["new"]))
    return diff


def served_custom_alerts(tenants):
    """{tenant: {name: view}} for compute_custom_alert_diff: each recipe the
    exporter serves rows for, as `metric`, `threshold` ("<value>:<severity>"),
    `mode` and `recipe_id` — each the sorted distinct values of the recipe's
    rows joined by ", ", so a change to any row (two recipes sharing a name
    included) is a change to the view."""
    fields = ("metric", "threshold", "mode", "recipe_id")
    out = {}
    for tenant, tv in tenants.items():
        recipes = {}
        for row in tv.values.get("_custom_alerts") or []:
            labels = row.get("labels") or {}
            name = labels.get("name")
            if not name:
                continue
            view = recipes.setdefault(str(name), {f: set() for f in fields})
            view["metric"].add(str(row.get("metric")))
            view["threshold"].add(f"{_served_number(row.get('value'))}:{row.get('severity')}")
            view["mode"].add(str(labels.get("mode")))
            view["recipe_id"].add(str(labels.get("recipe_id")))
        for name, view in recipes.items():
            recipes[name] = {f: ", ".join(sorted(view[f])) for f in fields}
        if recipes:
            out[tenant] = recipes
    return out


def served_custom_alert_diff(old_tenants, new_tenants):
    """compute_custom_alert_diff over served rows; a removed recipe carries
    SERVED_REMOVED_NOTE as its `note`."""
    diff = compute_custom_alert_diff(served_custom_alerts(old_tenants),
                                     served_custom_alerts(new_tenants))
    for changes in diff.values():
        for c in changes:
            if c["change"] == "removed":
                c["note"] = SERVED_REMOVED_NOTE
    return diff


def estimate_affected_alerts(metric_key):
    """The alerts a change to ``metric_key`` reaches, as the report cell.

    Read from the rule packs (``_threshold_alerts``), not derived from the
    key's spelling: ``mysql_connections`` reaches ``MariaDBHighConnections``
    and ``MariaDBSystemBottleneck``, which no CamelCase of the key names.
    ``—`` means no rule-pack alert reads the key (the change reaches no
    alert); ``unknown`` means the rule packs were not found, and stderr says so.
    """
    alerts = alerts_for_key(metric_key)
    if alerts is None:
        return "unknown"
    if not alerts:
        return "—"
    return ", ".join(alerts)


def _format_value(val):
    """Format a config value for display."""
    if val is None:
        return "—"
    if _is_disabled(val):
        return "disabled"
    if isinstance(val, dict):
        return "(scheduled)"
    return str(val)


def _cap_for_comment(result):
    """Truncation safeguard (Reef 2): never let the bot comment exceed GitHub's
    ceiling — a silently-dropped comment on a huge, high-risk PR is worse than
    a truncated one. The exit code + changed files remain authoritative."""
    if len(result) > COMMENT_SAFETY_LIMIT:
        notice = (
            "\n\n---\n"
            "... (Diff too large, truncated to fit GitHub's comment limit. "
            "Please review the changed files directly.)"
        )
        result = result[: COMMENT_SAFETY_LIMIT - len(notice)] + notice
    return result


def _summarize_changes(changes):
    """Summarize change types for a tenant (e.g., '1 tighter, 2 added')."""
    counts = {}
    for c in changes:
        counts[c["change"]] = counts.get(c["change"], 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))


# ── The report: two renders compared, in sections ─────────────────────────

VIEW_TITLES = {rd.VIEW_SERVED: "Exporter values (da-guard served-values)",
               rd.VIEW_ROUTES: "Routing (Alertmanager configuration)"}


def served_sections(old_tenants, new_tenants):
    """The three served sections from the two sides' served tenants: what
    `_render_diff.compare` keeps of the served renders (masked)."""
    return {
        "metric": compute_diff(served_metric_configs(old_tenants),
                               served_metric_configs(new_tenants)),
        "setting": served_setting_diff(old_tenants, new_tenants),
        "custom_alert": served_custom_alert_diff(old_tenants, new_tenants),
    }


def build_report(diff, old_dir, new_dir):
    """Everything both output formats print, from a `_render_diff.RenderDiff`."""
    served = diff.served.report or {}
    metric = served.get("metric") or {}
    setting = served.get("setting") or {}
    custom = served.get("custom_alert") or {}

    tenants_added, tenants_removed, dropped = [], [], {}
    for c in diff.served.changes:
        if c.field == "tenant":
            (tenants_added if c.pr else tenants_removed).append(c.tenant)
        elif c.field == "dropped":
            dropped.setdefault(c.tenant, []).append(
                {"key": c.key, "old": c.base, "new": c.pr,
                 "change": "added" if c.base is None else "removed" if c.pr is None else "modified"})
    # A served change no section above shows (a field added to served-values
    # later, say) is still printed, so the exit code never reads 1 above a
    # report that says nothing changed. diff_served has masked it already.
    shown = set(metric) | set(setting) | set(custom) | set(dropped)
    shown |= set(tenants_added) | set(tenants_removed)
    other = {}
    for c in diff.served.changes:
        if c.tenant not in shown:
            other.setdefault(c.tenant, []).append(
                {"field": c.field, "key": c.key, "old": c.base, "new": c.pr})

    routes = [c for c in diff.routes.changes if isinstance(c, rd.RouteChange)]
    receivers = [c for c in diff.routes.changes if isinstance(c, rd.ReceiverChange)]
    inhibits = [c for c in diff.routes.changes if isinstance(c, rd.InhibitChange)]
    return {
        "old_dir": str(old_dir), "new_dir": str(new_dir), "at": diff.at,
        "views": {rd.VIEW_SERVED: diff.served, rd.VIEW_ROUTES: diff.routes},
        "metric": metric, "setting": setting, "custom_alert": custom,
        "tenants_added": sorted(tenants_added), "tenants_removed": sorted(tenants_removed),
        "dropped": dropped, "other_served": other,
        "routes": routes, "receivers": receivers, "inhibits": inhibits,
        "uncovered_files": compute_uncovered_files(old_dir, new_dir),
    }


def report_has_changes(report):
    return any(report[k] for k in (
        "metric", "setting", "custom_alert", "tenants_added", "tenants_removed",
        "dropped", "other_served", "routes", "receivers", "inhibits"))


def report_exit_code(diff, report):
    """`RenderDiff.exit_code`, raised to 1 for a changed `_` file or anything
    the sections show; never lowered from 2."""
    code = diff.exit_code
    if code == 2:
        return EXIT_CALLER_ERROR
    if code == 1 or report["uncovered_files"] or report_has_changes(report):
        return EXIT_VIOLATION
    return EXIT_OK


def _changed_tenants(report):
    tenants = set(report["metric"]) | set(report["setting"]) | set(report["custom_alert"])
    tenants |= set(report["dropped"]) | set(report["other_served"])
    tenants |= set(report["tenants_added"]) | set(report["tenants_removed"])
    tenants |= {c.tenant for c in report["routes"]} | {c.tenant for c in report["inhibits"]}
    for c in report["receivers"]:
        tenants |= set(c.tenants)
    tenants.discard(rd.PLATFORM)
    return tenants


# ── JSON: one entry per distinct change, with the tenants it reaches ──────


def _pairs(changes):
    """{field: (old, new)} -> {field: {"old", "new"}}."""
    return {k: {"old": b, "new": p} for k, (b, p) in changes.items()}


def _grouped(per_tenant):
    """{tenant: [change, ...]} -> [change + {"tenants": [...]}, ...], one
    entry per distinct change: a platform change that reaches 1000 tenants is
    one entry, not 1000."""
    groups = {}
    for tenant in sorted(per_tenant):
        for c in per_tenant[tenant]:
            key = json.dumps(c, sort_keys=True, default=str)
            groups.setdefault(key, (c, []))[1].append(tenant)
    return [dict(c, tenants=ts) for c, ts in groups.values()]


def _group_by(items, key, merge):
    groups = {}
    for it in items:
        k = json.dumps(key(it), sort_keys=True, default=str)
        if k in groups:
            merge(groups[k], it)
        else:
            groups[k] = dict(key(it))
            merge(groups[k], it)
    return list(groups.values())


def _add(group, field, values):
    group[field] = sorted(set(group.get(field, [])) | set(values), key=str)


def report_json(report, exit_code):
    """The JSON report (`JSON_SCHEMA`)."""
    def route_merge(g, c):
        _add(g, "tenants", [c.tenant])
        g.setdefault("paths", []).append(list(c.path))

    def receiver_merge(g, c):
        _add(g, "receivers", [c.name])
        _add(g, "tenants", c.tenants)

    return {
        "schema": JSON_SCHEMA,
        "at": report["at"],
        "old_dir": report["old_dir"],
        "new_dir": report["new_dir"],
        "exit_code": exit_code,
        "views": {name: {"status": v.status, "reason": v.reason}
                  for name, v in report["views"].items()},
        "tenants_added": report["tenants_added"],
        "tenants_removed": report["tenants_removed"],
        "threshold_changes": _grouped(report["metric"]),
        "setting_changes": _grouped(report["setting"]),
        "custom_alert_changes": _grouped(report["custom_alert"]),
        "dropped_key_changes": _grouped(report["dropped"]),
        "other_served_changes": _grouped(report["other_served"]),
        "route_changes": _group_by(
            report["routes"], lambda c: {"change": c.kind, "fields": _pairs(c.changes)}, route_merge),
        "receiver_changes": _group_by(
            report["receivers"], lambda c: {"change": c.kind, "fields": _pairs(c.changes)},
            receiver_merge),
        "inhibit_changes": _group_by(
            report["inhibits"], lambda c: {"change": c.kind, "rule": c.rule},
            lambda g, c: _add(g, "tenants", [c.tenant])),
        "uncovered_files": report["uncovered_files"],
    }


# ── Markdown ──────────────────────────────────────────────────────────────


def _tenant_label(tenant):
    return "platform" if tenant == rd.PLATFORM else f"tenant {_code_span(tenant)}"


def _path_text(path):
    return " / ".join(path) if path else "(root)"


def _render_views(report, lines):
    bad = [(name, v) for name, v in report["views"].items() if v.status != "computed"]
    if not bad:
        return
    lines.append("## Not Computed / Not Compared")
    lines.append("")
    for name, v in bad:
        # The reason names files from the PR's tree: escaped (printable_name)
        # and inside a code span (F5).
        reason = _code_span(printable_name(v.reason))
        if v.status == "not_computed":
            lines.append(f"- :x: **{VIEW_TITLES[name]}: not computed.** No result for this view, "
                         f"which is not the same as no change: {reason}")
        else:
            lines.append(f"- :warning: **{VIEW_TITLES[name]}: not compared.** Nothing sound to "
                         f"compare against: {reason}")
    lines.append("")


def _render_uncovered(uncovered, lines):
    # File names come from the PR's tree, so each one goes through
    # printable_name (terminal escapes) and _code_span (code-span break-out, F5).
    lines.append("## Changed Platform Files")
    lines.append("")
    lines.append(
        f"> :warning: **{len(uncovered)} `_`-prefixed file(s) changed.** The sections "
        "below show what the change does to the exporter's values and to the "
        "Alertmanager configuration. A setting neither of them reads (for example a "
        "platform `_custom_alerts` recipe or `instance_tenant_mapping`) is not "
        "compared, so review these files directly."
    )
    lines.append("")
    for rel in uncovered[:UNCOVERED_LIST_LIMIT]:
        lines.append(f"- {_code_span(printable_name(rel))}")
    if len(uncovered) > UNCOVERED_LIST_LIMIT:
        lines.append(f"- ... (+{len(uncovered) - UNCOVERED_LIST_LIMIT} more)")
    lines.append("")


def _render_routing(report, lines):
    if not (report["routes"] or report["receivers"] or report["inhibits"]):
        return
    lines.append("## Routing Changes")
    lines.append("")
    lines.append(
        "> :warning: How alerts are delivered: route nodes (identified by their "
        "matchers), receivers and inhibit rules, as the route generator renders them. "
        "Credential fields are reported as changed with both values masked."
    )
    lines.append("")
    if report["routes"]:
        lines.append("### Routes")
        lines.append("")
        for c in report["routes"]:
            lines.append(f"- {_tenant_label(c.tenant)} — route {_code_span(_path_text(c.path))} ({c.kind})")
            for attr, (b, p) in c.changes.items():
                lines.append(f"  - {_code_span(attr)}: {_code_span(b)} → {_code_span(p)}")
        lines.append("")
    if report["receivers"]:
        lines.append("### Receivers")
        lines.append("")
        for c in report["receivers"]:
            who = ", ".join(_tenant_label(t) for t in c.tenants)
            lines.append(f"- {_code_span(c.name)} ({c.kind}) — {who}")
            for field, (b, p) in c.changes.items():
                lines.append(f"  - {_code_span(field)}: {_code_span(b)} → {_code_span(p)}")
        lines.append("")
    if report["inhibits"]:
        lines.append("### Inhibit Rules")
        lines.append("")
        for c in report["inhibits"]:
            lines.append(f"- {_tenant_label(c.tenant)} ({c.kind}): {_code_span(c.rule)}")
        lines.append("")


def _render_custom_alerts(custom_alert_diffs, lines):
    # Custom alert changes (ADR-024 Capability B, #741): real alerting
    # changes (add/remove/retune a paging rule), not format-only.
    lines.append("## Custom Alert Changes")
    lines.append("")
    lines.append(
        "> :warning: Custom alert recipes are real alerting changes "
        "(new / removed / retuned alert rules), not format-only."
    )
    # #1517: state what this section can and cannot tell.
    lines.append(">")
    lines.append(
        "> Read from the rows the exporter serves: `threshold` is "
        "`<value>:<severity>`, and an `op` / `window` / `for` change shows as a "
        "`recipe_id` change. A recipe no longer served may have been deleted, "
        "disabled or rejected; which of them is not told apart here."
    )
    lines.append("")
    for tenant, changes in custom_alert_diffs.items():
        summary = _summarize_changes(changes)
        lines.append(f"### {tenant} — {len(changes)} custom alert change(s) ({summary})")
        lines.append("")
        for c in changes:
            name_cs = _code_span(c["name"])
            if c["change"] == "modified":
                lines.append(f"- {name_cs} (modified)")
                for fc in c["field_changes"]:
                    annotation = _field_change_annotation(fc["field"], fc["old"], fc["new"])
                    lines.append(
                        f"  - {_code_span(fc['field'])}: "
                        f"{_code_span(fc['old'])} → {_code_span(fc['new'])}{annotation}")
            elif c["change"] == "added":
                r = c["new"] or {}
                lines.append(
                    f"- {name_cs} (added) — metric={_code_span(r.get('metric', '?'))} "
                    f"threshold={_code_span(r.get('threshold', '?'))} "
                    f"mode={_code_span(r.get('mode', '?'))}")
            else:
                r = c["old"] or {}
                lines.append(
                    f"- {name_cs} (removed) — was metric={_code_span(r.get('metric', '?'))} "
                    f"threshold={_code_span(r.get('threshold', '?'))}")
                if c.get("note"):
                    lines.append(f"  - {c['note']}")
        lines.append("")


def _render_settings(setting_diffs, lines):
    # They change what alerts do without touching a threshold. Values go
    # through _code_span (F5): they come from the PR's files.
    lines.append("## Tenant Setting Changes")
    lines.append("")
    lines.append(
        "> :warning: `_`-prefixed tenant settings change how alerts "
        "behave (maintenance, silent mode, state filters, severity dedup, "
        "routing, metadata) without touching a threshold."
    )
    lines.append("")
    for tenant, changes in setting_diffs.items():
        summary = _summarize_changes(changes)
        lines.append(f"### {tenant} — {len(changes)} setting change(s) ({summary})")
        lines.append("")
        for c in changes:
            key_cs = _code_span(c["key"])
            if c["change"] == "modified":
                lines.append(f"- {key_cs}: {_code_span(c['old'])} → {_code_span(c['new'])}")
            elif c["change"] == "added":
                lines.append(f"- {key_cs} (added) — {_code_span(c['new'])}")
            else:
                lines.append(f"- {key_cs} (removed) — was {_code_span(c['old'])}")
        lines.append("")


def _render_keyed(title, note, per_tenant, lines):
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"> {note}")
    lines.append("")
    for tenant, changes in per_tenant.items():
        lines.append(f"### {tenant}")
        lines.append("")
        for c in changes:
            field = f"{_code_span(c['field'])} " if "field" in c else ""
            lines.append(f"- {field}{_code_span(c['key'])}: {_code_span(c['old'])} → {_code_span(c['new'])}")
        lines.append("")


def _cell(value):
    """A table cell from the PR's files (a key, a value as written): a `|`
    would end the cell and a newline the table, letting the rest of the value
    render as Markdown of its own (a heading, a link) in the PR comment."""
    return (str(value).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r\n", " ").replace("\n", " ").replace("\r", " "))


def render_markdown(report):
    """Render the Markdown report."""
    lines = ["# Config Diff Report", "",
             f"Comparing: {_code_span(report['old_dir'])} → {_code_span(report['new_dir'])}",
             "",
             (f"Rendered at: {_code_span(report['at'])} (schedules, silences and maintenance "
              "windows are read at this instant, on both sides)"),
             ""]
    _render_views(report, lines)
    if report["uncovered_files"]:
        _render_uncovered(report["uncovered_files"], lines)
    if report["tenants_added"] or report["tenants_removed"]:
        lines.append("## Tenants Added / Removed")
        lines.append("")
        for t in report["tenants_added"]:
            lines.append(f"- {_code_span(t)} (added)")
        for t in report["tenants_removed"]:
            lines.append(f"- {_code_span(t)} (removed)")
        lines.append("")
    _render_routing(report, lines)
    if report["custom_alert"]:
        _render_custom_alerts(report["custom_alert"], lines)
    if report["setting"]:
        _render_settings(report["setting"], lines)
    if report["dropped"]:
        _render_keyed("Keys the Exporter Drops",
                      "Keys the exporter reads and refuses to serve, with its reasons.",
                      report["dropped"], lines)
    if report["other_served"]:
        _render_keyed("Other Served Changes",
                      "Changes in what the exporter serves that no section above shows.",
                      report["other_served"], lines)

    computed = all(v.status == "computed" for v in report["views"].values())
    if not report_has_changes(report):
        if not computed:
            lines.append("No change among the views computed; see Not Computed / Not Compared above.")
        elif report["uncovered_files"]:
            lines.append("No change in what the two renders show; "
                         f"{len(report['uncovered_files'])} changed platform file(s) to review (above).")
        else:
            lines.append("No changes detected.")
        return _cap_for_comment("\n".join(lines))

    total_changes = 0
    for tenant, changes in report["metric"].items():
        lines.append(f"## {tenant} — {len(changes)} change(s) ({_summarize_changes(changes)})")
        lines.append("")
        lines.append("| Metric | Before | After | Change | Affected Alerts |")
        lines.append("|--------|--------|-------|--------|-----------------|")
        for c in changes:
            lines.append(
                f"| {_cell(c['key'])} | {_cell(_format_value(c['old']))} "
                f"| {_cell(_format_value(c['new']))} | {c['change']} "
                f"| {estimate_affected_alerts(c['key'])} |")
        lines.append("")
        total_changes += len(changes)

    lines.append("---")
    summary = (f"Summary: {len(_changed_tenants(report))} tenant(s) changed, "
               f"{total_changes} metric change(s)")
    if report["custom_alert"]:
        summary += (f", {len(report['custom_alert'])} tenant(s) with "
                    f"{sum(len(v) for v in report['custom_alert'].values())} custom alert change(s)")
    if report["setting"]:
        summary += (f", {len(report['setting'])} tenant(s) with "
                    f"{sum(len(v) for v in report['setting'].values())} setting change(s)")
    routing = len(report["routes"]) + len(report["receivers"]) + len(report["inhibits"])
    if routing:
        summary += f", {routing} routing change(s)"
    if any(c.tenant == rd.PLATFORM for c in report["routes"]):
        summary += " (some platform-wide)"
    if report["uncovered_files"]:
        summary += f", {len(report['uncovered_files'])} changed platform file(s)"
    lines.append(summary)
    return _cap_for_comment("\n".join(lines))


# ── CLI ───────────────────────────────────────────────────────────────────


def _at(value):
    try:
        ok = bool(_RFC3339.match(value)) and bool(
            datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        ok = False  # the shape is right, the date is not (month 13, hour 25, ...)
    if not ok:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not RFC3339 (e.g. 2026-10-10T03:00:00Z)")
    return value


def now_at():
    """This instant, RFC3339 in UTC to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser():
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Compare what two conf.d/ directories render to — "
                    "per-tenant blast radius report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Exit codes: 0 no change; 1 a change, or something not compared;
            2 no result (the PR side or a tool failed, or bad arguments).

            Examples:
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/ --at 2026-10-10T03:00:00Z
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/ --format json
        """),
    )
    parser.add_argument("--old-dir", required=True,
                        help="Baseline config directory (missing or empty: a first import)")
    parser.add_argument("--new-dir", required=True,
                        help="New/staged config directory")
    parser.add_argument("--at", type=_at, default=None,
                        help="Instant to read schedules, silences and maintenance windows at, "
                             "RFC3339 (e.g. the base commit's time). Default: now")
    parser.add_argument("--json-output", action="store_true",
                        help="Output JSON instead of Markdown")
    parser.add_argument("--format", choices=["markdown", "json"],
                        default="markdown",
                        help="Output format (default: markdown). "
                             "Alias: --json-output is equivalent to --format json")
    return parser


def main():
    """Entry point.

    Exit codes (SSOT: ``scripts/tools/_lib_exitcodes.py``): 0 no change,
    1 a change or something not compared, 2 no result.

    ⛔ The handler below catches ``Exception``, not a list of types: an
    uncaught exception exits **1**, this tool's "changes detected", with an
    empty stdout — every consumer that follows the documented contract
    (``docs/integration/gitops-deployment.md``: 0 skip / 1 post the report /
    2 fail the pipeline) would read a crash as a finding. A render failing is
    not an exception here (``_render_diff.compare`` reports it per view); what
    reaches this handler is the tool's own fault, an unreadable file among the
    platform files, or the like.
    """
    args = build_parser().parse_args()
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 — deliberate; see docstring
        # ``sys.exit`` inside ``_run`` raises SystemExit, which derives from
        # BaseException, so the tool's own 0/1/2 exits pass through untouched.
        # ``str(exc)``, not ``repr(exc)``: it carries the file name where the
        # exception has one; the type name because str() alone can be terse.
        print(
            f"ERROR: cannot compare configs "
            f"(--old-dir {args.old_dir} --new-dir {args.new_dir}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CALLER_ERROR)


def _run(args):
    """Render, compare, print; exits 0/1/2 itself."""
    for flag, path in (("--old-dir", args.old_dir), ("--new-dir", args.new_dir)):
        if Path(path).exists() and not Path(path).is_dir():
            print(f"ERROR: {flag} is not a directory: {path}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    at = args.at or now_at()
    diff = rd.compare(args.old_dir, args.new_dir, at, served_report=served_sections)
    report = build_report(diff, args.old_dir, args.new_dir)
    code = report_exit_code(diff, report)

    # The reasons go to stderr too: a CI log shows them even where the report
    # is only posted, and an exit 2 never comes with an empty stderr (#1405).
    for name, v in report["views"].items():
        reason = printable_name(v.reason)
        if v.status == "not_computed":
            print(f"ERROR: cannot compare configs (--old-dir {args.old_dir} --new-dir "
                  f"{args.new_dir}): {VIEW_TITLES[name]}: not computed: {reason}", file=sys.stderr)
        elif v.status == "not_compared":
            print(f"WARNING: {VIEW_TITLES[name]}: not compared: {reason}", file=sys.stderr)

    if args.json_output or args.format == "json":
        print(format_json_report(report_json(report, code), default=str))
    else:
        print(render_markdown(report))
    # Spelled out, not `sys.exit(code)`: the CLI-contract lint reads which
    # codes this tool can exit with from the literal arguments.
    if code == EXIT_CALLER_ERROR:
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_VIOLATION if code == EXIT_VIOLATION else EXIT_OK)


if __name__ == "__main__":
    main()
