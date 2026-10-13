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
The top-level keys of `_`-prefixed files are also compared one by one, as
`da-guard parse-tree` reads them, and each changed one is listed with what
the renders can say about it (ADR-037 decision 3). Nothing here reads the
YAML itself; `_render_diff` renders and compares.

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
from pathlib import Path, PurePosixPath
from typing import NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    format_json_report,
    is_disabled as _is_disabled,
)
from _lib_redact import REDACTED, SECRET_FIELDS, mask_values, redact, redact_pair  # noqa: E402
from _lib_redact import is_secret_field as _lib_redact_is_secret  # noqa: E402
from _lib_confd import printable_name  # noqa: E402
import _lib_parse_tree  # noqa: E402
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
# How many tenant names a change lists before "(+N more)".
TENANT_NAME_LIMIT = 5

# What `--at` takes: RFC3339, as `da-guard served-values --at` parses it.
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


# ── platform settings: the top-level keys of `_` files (ADR-037 decision 3) ──
#
# A "carrier" is one top-level key of a `_`-prefixed file. Both sides are read
# as `da-guard parse-tree` reads them (ADR-036: Python never parses conf.d
# YAML itself), and each key whose value differs is listed with what the
# report can say about it. Values are compared as yaml.v3 reads them (tag and
# value), so a comment, a re-indent or another quoting of the same string is
# no change; the report names keys, never values.
#
# ⛔ Stage 1 of decision 3 (owner decision on #1516, 2026-10-13): a changed
# carrier that a render covers but whose renders show no difference exits 1,
# full coverage or not. Where a key is written decides whether a reader reads
# it at all (`defaults` outside a defaults carrier, `tenants:` in a nested
# `_` file, ...), and nothing here knows that per key yet — reading it from
# Go is #2962. Until then "no difference" cannot be told from "not read
# there", and "full coverage: a note, exit code unaffected" would hide the
# second.

VIEW_PLATFORM = "platform"
# The parse-tree reader behind each render.
_VIEW_READER = {rd.VIEW_SERVED: "exporter", rd.VIEW_ROUTES: "generator"}

# Carrier -> (views that render it, coverage). Coverage is what the
# self-test (tests/ops/test_config_diff_carriers.py) holds the renders to;
# in stage 1 it changes no exit code.
RENDERED_CARRIERS = {
    "defaults": ((rd.VIEW_SERVED,), "full"),
    "optional_overrides": ((rd.VIEW_SERVED,), "partial"),  # the list itself is not served
    "state_filters": ((rd.VIEW_SERVED,), "partial"),        # `reasons` is read by no renderer
    "tenants": ((rd.VIEW_SERVED, rd.VIEW_ROUTES), "full"),
    "profiles": ((rd.VIEW_SERVED,), "full"),
    "_routing_defaults": ((rd.VIEW_ROUTES,), "partial"),   # its `routes` is dropped
    "_routing_enforced": ((rd.VIEW_ROUTES,), "full"),
    "routing_profiles": ((rd.VIEW_ROUTES,), "full"),
}

# Read by no renderer, deferred with a trigger (ADR-037 decision 3's table).
DEFERRED_CARRIERS = {
    "_custom_alerts": "a platform recipe is read by the custom-alert compiler, not by either render",
    "instance_tenant_mapping": "read by the tenant-mapping generator, not by either render",
    "max_metrics_per_tenant": "served-values does not serve the cap itself",
}


def _is_root_defaults_carrier(rel, doc):
    return rel == (doc.get("defaults_carriers") or {}).get(".")


# Carriers a CI check reads and enforces, not a render: listed, exit code
# unaffected — but only where that check reads them; written anywhere else
# they are "not read here" (exit 1), so the report never implies a check
# enforces a rule nothing reads.
CHECKED_CARRIERS = {
    "domain_policies": (
        "the route generator's `--validate --strict` rejects violations",
        lambda rel, doc: PurePosixPath(rel).name in ("_domain_policy.yaml", "_domain_policy.yml")),
    "_policies": (
        "`validate-config` fails on a violated rule of severity `error` "
        "(a `warning` rule does not fail it)",
        _is_root_defaults_carrier),
}

# Files neither render reads, by name: what config-diff can say for sure is
# that neither the exporter nor the route generator reads them; the note says
# who else may, which depends on how tenant-api is deployed.
OTHER_SERVICE_FILES = {
    "_tenant_orgs": "tenant-api reads `_tenant_orgs.yaml` from conf.d",
    "_rbac": "tenant-api reads a _rbac file only when its --rbac path points to it "
             "(try-local does; the Helm chart reads a ConfigMap)",
    "_groups": "tenant-api has not read `_groups.yaml` from conf.d since #2856 (a one-time import)",
    "_views": "tenant-api has not read `_views.yaml` from conf.d since #2856 (a one-time import)",
    "_federation_policy": ("tenant-api has not read `_federation_policy.yaml` from conf.d "
                           "since #2856 (a one-time import)"),
    "_account_registry": ("tenant-api has not read `_account_registry.yaml` from conf.d "
                          "since #2856 (a one-time import)"),
}

# Row statuses, and which of them raise the exit code to 1.
SETTING_RENDERED = "rendered"            # a view that covers it shows changes
SETTING_NO_RENDER_CHANGE = "no_render_change"
SETTING_CHECKED = "checked"
SETTING_NOT_READ_HERE = "not_read_here"
SETTING_NOT_COMPARED = "not_compared"
SETTINGS_TO_REVIEW = frozenset({SETTING_NO_RENDER_CHANGE, SETTING_NOT_READ_HERE,
                                SETTING_NOT_COMPARED})

_EMPTY_TREE = {"files": {}, "unreadable": [], "not_read": [], "selected": {},
               "defaults_carriers": {}}


def _canonical(node, *, by_text=False):
    """`node` (a parse-tree content node) as one string that is equal for
    equal values: a scalar by tag and value (its source text only where it
    has no value, a timestamp), mappings by sorted key. Iterative: content
    nests thousands deep (aliases expanded).

    `by_text`: a scalar that is not a string by tag and source text. For a
    key no render reads: yaml.v3 reads `007` and `7` as the same int, but a
    reader that decodes the field as a string (tenant-api's org lists) or
    with YAML 1.1 rules does not, and no render shows the difference."""
    out, stack = [], [node]
    while stack:
        v = stack.pop()
        if isinstance(v, _Token):
            out.append(v.text)
        elif _lib_parse_tree.is_scalar(v):
            plain = "value" in v and (v["tag"] == "!!str" or not by_text)
            out.append(json.dumps([v["tag"], v["value"] if plain else v["text"]]))
        elif isinstance(v, list):
            stack.append(_Token("]"))
            for item in reversed(v):
                stack.extend((_Token(","), item))
            stack.append(_Token("["))
        else:  # a mapping
            stack.append(_Token("}"))
            for k in sorted(v, reverse=True):
                stack.extend((_Token(","), v[k], _Token(json.dumps(k) + ":")))
            stack.append(_Token("{"))
    return "".join(out)


class _Token(NamedTuple):
    text: str


def _read_tree(conf_d, timeout):
    """parse-tree's document for `conf_d`; an empty one for a side with no
    config (a first import). Raises `_lib_parse_tree.ParseTreeError`."""
    if not rd.has_config(conf_d):
        return _EMPTY_TREE
    return _lib_parse_tree.run(conf_d, None, timeout)


def _platform_files(doc):
    return {rel: content for rel, content in doc["files"].items()
            if PurePosixPath(rel).name.startswith("_")}


def _unreadable_platform_files(doc):
    return {u["file"]: u["kind"] for u in doc["unreadable"]
            if PurePosixPath(u["file"]).name.startswith("_")}


def _setting_row(rel, key, change, status, note, views=()):
    return {"file": rel, "key": key, "change": change, "status": status,
            "note": note, "views": list(views)}


def platform_settings(old_doc, new_doc, changed_views, view_status, old_dir=None, new_dir=None):
    """One row per changed carrier, per unreadable `_` file and per changed
    `_` file no reader reads, sorted.

    `changed_views`: the views whose sections show a change; `view_status`:
    {view: status} — a carrier whose covering view was not compared cannot
    be said to be rendered. `old_dir` / `new_dir`: where a file parse-tree
    lists as read by no reader (`not_read`) is compared, byte for byte —
    its content is in no document."""
    rows = _not_read_rows(old_doc, new_doc, old_dir, new_dir)
    old_files, new_files = _platform_files(old_doc), _platform_files(new_doc)
    old_bad, new_bad = _unreadable_platform_files(old_doc), _unreadable_platform_files(new_doc)
    for rel in sorted(set(old_bad) | set(new_bad)):
        side = "PR" if rel in new_bad else "base"
        kind = new_bad.get(rel) or old_bad.get(rel)
        rows.append(_setting_row(rel, None, "unreadable", SETTING_NOT_COMPARED,
                                 f"cannot be read on the {side} side ({kind})"))
    for rel in sorted((set(old_files) | set(new_files)) - set(old_bad) - set(new_bad)):
        old, new = old_files.get(rel, {}), new_files.get(rel, {})
        stem = PurePosixPath(rel).stem.lower()
        for key in sorted(set(old) | set(new)):
            by_text = key not in RENDERED_CARRIERS
            if key in old and key in new and (_canonical(old[key], by_text=by_text)
                                              == _canonical(new[key], by_text=by_text)):
                continue
            change = "added" if key not in old else "removed" if key not in new else "modified"
            rows.append(_classify(rel, key, change, stem, new_doc if key in new else old_doc,
                                  changed_views, view_status))
    return sorted(rows, key=lambda r: (r["file"], r["key"] or ""))


def _not_read_rows(old_doc, new_doc, old_dir, new_dir):
    """A `_` file parse-tree lists as read by no reader (an unselected
    defaults carrier: `_defaults.yml` beside `_defaults.yaml`), when its
    bytes differ between the sides or it is on one side only."""
    reasons = {}
    for doc in (old_doc, new_doc):
        for e in doc.get("not_read") or []:
            if PurePosixPath(e["file"]).name.startswith("_"):
                reasons.setdefault(e["file"], e["reason"])
    rows = []
    for rel in sorted(reasons):
        old = _read_bytes(old_dir, rel)
        new = _read_bytes(new_dir, rel)
        if old == new:
            continue
        change = "added" if old is None else "removed" if new is None else "modified"
        rows.append(_setting_row(rel, None, change, SETTING_NOT_READ_HERE,
                                 f"no reader reads this file ({reasons[rel]})"))
    return rows


def _read_bytes(root, rel):
    """The file's bytes, or None when it is not there. Bytes only: nothing
    here parses it (ADR-036)."""
    if root is None:
        return None
    path = Path(root) / rel
    return path.read_bytes() if path.is_file() else None


def _classify(rel, key, change, stem, doc, changed_views, view_status):
    if key in CHECKED_CARRIERS:
        checker, read_here = CHECKED_CARRIERS[key]
        if read_here(rel, doc):
            return _setting_row(rel, key, change, SETTING_CHECKED, f"not compared; {checker}")
        return _setting_row(rel, key, change, SETTING_NOT_READ_HERE,
                            "the check that reads this key does not read it from this file")
    if key in DEFERRED_CARRIERS:
        return _setting_row(rel, key, change, SETTING_NOT_COMPARED, DEFERRED_CARRIERS[key])
    if key not in RENDERED_CARRIERS:
        note = "neither the exporter nor the route generator reads this key"
        if stem in OTHER_SERVICE_FILES:
            note += "; " + OTHER_SERVICE_FILES[stem]
        return _setting_row(rel, key, change, SETTING_NOT_COMPARED, note)
    # A view counts only where its reader reads the file at all (parse-tree's
    # `selected`); which keys of the file it reads is #2962.
    readers = (doc.get("selected") or {}).get(rel) or []
    views = tuple(v for v in RENDERED_CARRIERS[key][0]
                  if _VIEW_READER[v] in readers)
    if not views:
        return _setting_row(rel, key, change, SETTING_NOT_READ_HERE,
                            "no reader that renders this key reads this file")
    if any(view_status.get(v) != "computed" for v in views):
        return _setting_row(rel, key, change, SETTING_NOT_COMPARED,
                            "a view that renders it was not computed or not compared", views)
    if any(v in changed_views for v in views):
        return _setting_row(rel, key, change, SETTING_RENDERED,
                            "the renders that read it changed; which of those changes "
                            "come from this key is not told apart", views)
    return _setting_row(rel, key, change, SETTING_NO_RENDER_CHANGE,
                        "the renders show no difference: it may have no effect, or a reader "
                        "may not read it where it is written", views)


def _platform_view(old_dir, new_dir, timeout):
    """(ViewResult, old_doc, new_doc) for the platform settings. A PR side
    parse-tree cannot read, or a tool failure on either side, is no result
    (ADR-037 decision 4)."""
    try:
        new_doc = _read_tree(new_dir, timeout)
    except _lib_parse_tree.ParseTreeError as e:
        return rd.ViewResult("not_computed", f"PR side: {e}"), None, None
    try:
        old_doc = _read_tree(old_dir, timeout)
    except _lib_parse_tree.ParseTreeError as e:
        return rd.ViewResult("not_computed", f"base side: {e}"), None, None
    return rd.ViewResult("computed", ""), old_doc, new_doc


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


# ── The report: two renders compared, in sections ─────────────────────────

# The served-values fields the sections below read (`tenant` is the added /
# removed list).
_SECTIONED_FIELDS = frozenset(rd.SERVED_FIELDS) | {"tenant"}

VIEW_TITLES = {rd.VIEW_SERVED: "Exporter values (da-guard served-values)",
               rd.VIEW_ROUTES: "Routing (Alertmanager configuration)",
               VIEW_PLATFORM: "Platform settings (da-guard parse-tree)"}


def served_sections(old_tenants, new_tenants):
    """The three served sections from the two sides' served tenants: what
    `_render_diff.compare` keeps of the served renders (masked)."""
    return {
        "metric": compute_diff(served_metric_configs(old_tenants),
                               served_metric_configs(new_tenants)),
        "setting": served_setting_diff(old_tenants, new_tenants),
        "custom_alert": served_custom_alert_diff(old_tenants, new_tenants),
    }


def build_report(diff, old_dir, new_dir, *, timeout=rd.DEFAULT_TIMEOUT):
    """Everything both output formats print, from a `_render_diff.RenderDiff`
    and the two sides' `_` files as parse-tree reads them."""
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
        # Either the field is one no section reads, or no section names the
        # tenant at all: a tenant shown elsewhere must not hide a new field.
        if c.field not in _SECTIONED_FIELDS or c.tenant not in shown:
            other.setdefault(c.tenant, []).append(
                {"field": c.field, "key": c.key, "old": c.base, "new": c.pr})

    routes = [c for c in diff.routes.changes if isinstance(c, rd.RouteChange)]
    receivers = [c for c in diff.routes.changes if isinstance(c, rd.ReceiverChange)]
    inhibits = [c for c in diff.routes.changes if isinstance(c, rd.InhibitChange)]
    report = {
        "old_dir": str(old_dir), "new_dir": str(new_dir), "at": diff.at,
        "views": {rd.VIEW_SERVED: diff.served, rd.VIEW_ROUTES: diff.routes},
        "metric": metric, "setting": setting, "custom_alert": custom,
        "tenants_added": sorted(tenants_added), "tenants_removed": sorted(tenants_removed),
        "dropped": dropped, "other_served": other,
        "routes": routes, "receivers": receivers, "inhibits": inhibits,
    }
    platform, old_doc, new_doc = _platform_view(old_dir, new_dir, timeout)
    report["views"][VIEW_PLATFORM] = platform
    rows = []
    if platform.status == "computed":
        changed_views = {rd.VIEW_SERVED} if _served_changed(report) else set()
        if report["routes"] or report["receivers"] or report["inhibits"]:
            changed_views.add(rd.VIEW_ROUTES)
        rows = platform_settings(old_doc, new_doc, changed_views,
                                 {rd.VIEW_SERVED: diff.served.status,
                                  rd.VIEW_ROUTES: diff.routes.status}, old_dir, new_dir)
    report["platform_settings"] = rows
    report["uncovered_files"] = sorted({r["file"] for r in rows if r["status"] in SETTINGS_TO_REVIEW})
    return report


def _served_changed(report):
    return any(report[k] for k in ("metric", "setting", "custom_alert", "tenants_added",
                                   "tenants_removed", "dropped", "other_served"))


def mask_report(report, secrets):
    """`report` with every string a credential field holds masked wherever
    else it occurs (a YAML alias or a copy puts it in a field not named like
    a credential, #2865), and the count as `masked_by_value`. Run on the data
    both formats print, not on their text: escaping in either could turn a
    secret into a string that no longer matches.

    Of `views`, only status and reason are printed; the changes and served
    report they keep were read into the sections already, and are dropped
    here so the count is of what the report prints."""
    report = dict(report, views={name: v._replace(changes=(), report=None)
                                 for name, v in report["views"].items()})
    masked, count = mask_values(report, secrets)
    masked["masked_by_value"] = count
    return masked


def report_has_changes(report):
    return any(report[k] for k in (
        "metric", "setting", "custom_alert", "tenants_added", "tenants_removed",
        "dropped", "other_served", "routes", "receivers", "inhibits"))


def report_exit_code(diff, report):
    """`RenderDiff.exit_code`, raised to 2 when the platform settings were not
    computed and to 1 for a platform setting to review or anything the
    sections show; never lowered from 2."""
    code = diff.exit_code
    platform = report["views"].get(VIEW_PLATFORM)
    if code == 2 or (platform is not None and platform.status == "not_computed"):
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
        "masked_by_value": report.get("masked_by_value", 0),
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
        "platform_settings": report["platform_settings"],
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


_SETTING_HEADINGS = (
    (SETTING_NO_RENDER_CHANGE, ":warning: Changed, but the renders show no difference"),
    (SETTING_NOT_READ_HERE, ":warning: Not read where it is written"),
    (SETTING_NOT_COMPARED, ":warning: Not compared"),
    (SETTING_CHECKED, "Checked by CI, not compared"),
    (SETTING_RENDERED, "Rendered (see the sections below)"),
)


# How much of one file or key name the Markdown prints: a name comes from the
# PR's tree, and one long name must not fill the comment (the JSON keeps it).
SETTING_NAME_LIMIT = 200


def _shorten(name):
    return name if len(name) <= SETTING_NAME_LIMIT else name[:SETTING_NAME_LIMIT] + "…"


def _render_platform_settings(rows, lines):
    """The changed top-level keys of `_` files, grouped by what the report can
    say about them. File and key names come from the PR's tree: each goes
    through printable_name (terminal escapes) and _code_span (F5)."""
    lines.append("## Platform Settings")
    lines.append("")
    lines.append(
        "> Top-level keys of `_`-prefixed files whose value changed (comments and "
        "formatting are not changes), and `_` files no reader reads whose bytes changed. "
        "The renders compare what a setting does; a setting they do not show is listed "
        "here to review directly."
    )
    lines.append("")
    shown = 0
    for status, heading in _SETTING_HEADINGS:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        lines.append(f"**{heading}** ({len(group)})")
        lines.append("")
        for first, members in _group(group, lambda r: r["note"]):
            listed = members[:max(0, UNCOVERED_LIST_LIMIT - shown)]
            if not listed:
                break
            for r in listed:
                what = _code_span(_shorten(printable_name(r["file"])))
                if r["key"] is not None:
                    what += " " + _code_span(_shorten(printable_name(r["key"])))
                what += f" ({r['change']})"
                lines.append(f"- {what}")
            shown += len(listed)
            lines.append(f"  - {printable_name(first['note'])}")
        lines.append("")
    if len(rows) > shown:
        lines.append(f"- ... (+{len(rows) - shown} more; the JSON report lists them all)")
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
        # One entry per distinct change: a platform default reaches one
        # route node per tenant, each with its own matcher path. Paths that
        # differ only in the tenant they name are the same change; any other
        # matcher (an alertname, a component) is what the route catches, so
        # routes that differ there are listed apart.
        for c, group in _group(report["routes"], lambda c: (c.kind, c.changes, _path_shape(c.path))):
            if len(group) == 1:
                lines.append(f"- {_tenant_label(c.tenant)} — route {_code_span(_path_text(c.path))} ({c.kind})")
            else:
                lines.append(f"- {_who(g.tenant for g in group)} — {len(group)} routes ({c.kind}), "
                             f"e.g. {_code_span(_path_text(c.path))}")
            for attr, (b, p) in c.changes.items():
                lines.append(f"  - {_code_span(attr)}: {_code_span(b)} → {_code_span(p)}")
        lines.append("")
    if report["receivers"]:
        lines.append("### Receivers")
        lines.append("")
        for c, group in _group(report["receivers"], lambda c: (c.kind, c.changes)):
            who = _who(t for g in group for t in g.tenants)
            if len(group) == 1:
                lines.append(f"- {_code_span(c.name)} ({c.kind}) — {who}")
            else:
                lines.append(f"- {len(group)} receivers ({c.kind}): "
                             f"{_names([g.name for g in group])} — {who}")
            for field, (b, p) in c.changes.items():
                lines.append(f"  - {_code_span(field)}: {_code_span(b)} → {_code_span(p)}")
        lines.append("")
    if report["inhibits"]:
        lines.append("### Inhibit Rules")
        lines.append("")
        for c, group in _group(report["inhibits"], lambda c: (c.kind, c.rule)):
            if len(group) == 1:
                lines.append(f"- {_tenant_label(c.tenant)} ({c.kind}): {_code_span(c.rule)}")
            else:
                lines.append(f"- {_who(g.tenant for g in group)} — {len(group)} rules "
                             f"({c.kind}): {_code_span(c.rule)}")
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
    for c, tenants in _group_per_tenant(custom_alert_diffs):
        who = _who(tenants)
        name_cs = _code_span(c["name"])
        if c["change"] == "modified":
            lines.append(f"- {name_cs} (modified) — {who}")
            for fc in c["field_changes"]:
                annotation = _field_change_annotation(fc["field"], fc["old"], fc["new"])
                lines.append(
                    f"  - {_code_span(fc['field'])}: "
                    f"{_code_span(fc['old'])} → {_code_span(fc['new'])}{annotation}")
        elif c["change"] == "added":
            r = c["new"] or {}
            lines.append(
                f"- {name_cs} (added) — {who} — metric={_code_span(r.get('metric', '?'))} "
                f"threshold={_code_span(r.get('threshold', '?'))} "
                f"mode={_code_span(r.get('mode', '?'))}")
        else:
            r = c["old"] or {}
            lines.append(
                f"- {name_cs} (removed) — {who} — was metric={_code_span(r.get('metric', '?'))} "
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
    for c, tenants in _group_per_tenant(setting_diffs):
        who = _who(tenants)
        key_cs = _code_span(c["key"])
        if c["change"] == "modified":
            lines.append(f"- {key_cs}: {_code_span(c['old'])} → {_code_span(c['new'])} — {who}")
        elif c["change"] == "added":
            lines.append(f"- {key_cs} (added) — {_code_span(c['new'])} — {who}")
        else:
            lines.append(f"- {key_cs} (removed) — was {_code_span(c['old'])} — {who}")
    lines.append("")


def _render_keyed(title, note, per_tenant, lines):
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"> {note}")
    lines.append("")
    for c, tenants in _group_per_tenant(per_tenant):
        field = f"{_code_span(c['field'])} " if "field" in c else ""
        lines.append(f"- {field}{_code_span(c['key'])}: {_code_span(c['old'])} → "
                     f"{_code_span(c['new'])} — {_who(tenants)}")
    lines.append("")


def _render_thresholds(metric, lines):
    lines.append("## Threshold Changes")
    lines.append("")
    lines.append("| Metric | Before | After | Change | Affected Alerts | Tenants |")
    lines.append("|--------|--------|-------|--------|-----------------|---------|")
    for c, tenants in _group_per_tenant(metric):
        lines.append(
            f"| {_cell(c['key'])} | {_cell(_format_value(c['old']))} "
            f"| {_cell(_format_value(c['new']))} | {c['change']} "
            f"| {estimate_affected_alerts(c['key'])} | {_who_cell(tenants)} |")
    lines.append("")


# ── grouping: one entry per distinct change, with the tenants it reaches ──


def _group(items, key):
    """[(first item, [items with the same key]), ...] in first-seen order."""
    groups = {}
    for it in items:
        k = json.dumps(key(it), sort_keys=True, default=str)
        groups.setdefault(k, (it, []))[1].append(it)
    return list(groups.values())


def _group_per_tenant(per_tenant):
    """{tenant: [change, ...]} -> [(change, [tenant, ...]), ...]: a platform
    change that reaches 1000 tenants is one entry, not 1000 (as `_grouped`
    for the JSON report)."""
    pairs = [(t, c) for t in sorted(per_tenant, key=str) for c in per_tenant[t]]
    return [(c, [t for t, _ in group]) for (_, c), group in _group(pairs, lambda tc: tc[1])]


def _path_shape(path):
    """A route's matcher path with the tenant it names left out; a `#n`
    repeat suffix (a sibling with the same matchers) is kept."""
    def shape(m):
        matcher, sep, repeat = m.partition(" #")
        return f"tenant=*{sep}{repeat}" if rd._tenant_of_matcher(matcher) is not None else m
    return [", ".join(shape(m) for m in _split_matchers(step)) for step in path]


def _split_matchers(step):
    """A path step's matchers: split on the `, ` that `_render_diff` joins
    them with, but not inside a quoted value (`component="api, tenant=x"`
    is one matcher)."""
    out, cur, quoted, escaped = [], [], False, False
    for i, ch in enumerate(step):
        if escaped:
            escaped = False
        elif ch == "\\" and quoted:
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted and step[i + 1:i + 2] == " ":
            out.append("".join(cur))
            cur = []
            continue
        if cur or ch != " " or not out:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _names(names):
    """Code spans of the first TENANT_NAME_LIMIT names, and how many more."""
    names = list(names)
    shown = ", ".join(_code_span(n) for n in names[:TENANT_NAME_LIMIT])
    rest = len(names) - TENANT_NAME_LIMIT
    return shown + (f" (+{rest} more)" if rest > 0 else "")


def _who(tenants):
    """The tenants a change reaches: one as `_tenant_label` prints it, more
    as a count and the first few names (platform first)."""
    ts = sorted(set(tenants), key=lambda t: (t != rd.PLATFORM, str(t)))
    if len(ts) == 1:
        return _tenant_label(ts[0])
    platform = ts[0] == rd.PLATFORM
    names = [t for t in ts if t != rd.PLATFORM]
    text = f"{len(names)} tenant{'' if len(names) == 1 else 's'}: {_names(names)}"
    return f"platform and {text}" if platform else text


def _who_cell(tenants):
    """`_who` in a table cell: its names are code spans already, where a
    backslash is literal, so only `|` and line breaks are escaped (`_cell`
    would double every backslash)."""
    return (_who(tenants).replace("|", "\\|")
            .replace("\r\n", " ").replace("\n", " ").replace("\r", " "))


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
    if report.get("masked_by_value"):
        lines += [f"Masked by value: {report['masked_by_value']} place(s) outside a credential "
                  f"field held a credential's value; they print as {_code_span(REDACTED)}.", ""]
    _render_views(report, lines)
    if report["platform_settings"]:
        _render_platform_settings(report["platform_settings"], lines)
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
                         f"{len(report['uncovered_files'])} platform file(s) with settings "
                         "to review (above).")
        else:
            lines.append("No changes detected.")
        return _cap_for_comment("\n".join(lines))

    if report["metric"]:
        _render_thresholds(report["metric"], lines)
    total_changes = sum(len(v) for v in report["metric"].values())

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
        summary += f", {len(report['uncovered_files'])} platform file(s) with settings to review"
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
    report = mask_report(report, diff.secrets)

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
