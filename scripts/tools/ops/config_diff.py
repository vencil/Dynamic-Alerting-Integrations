#!/usr/bin/env python3
"""
config_diff.py — Directory-level Config Diff for GitOps PR review.

Compares two conf.d/ directories and produces a per-tenant blast radius report
showing which tenants, metrics, and alert thresholds changed (tighter / looser /
added / removed / toggled).

Complements patch_config.py --diff (single-metric live ConfigMap preview).
config_diff compares entire directory snapshots for PR review.

Usage:
  python3 scripts/tools/ops/config_diff.py --old-dir conf.d.bak --new-dir conf.d/
  python3 scripts/tools/ops/config_diff.py --old-dir conf.d.bak --new-dir conf.d/ --format json
  python3 scripts/tools/ops/config_diff.py --old-dir conf.d.bak --new-dir conf.d/ --format markdown
"""
import argparse
import io
import json
import os
import sys
import textwrap
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    format_json_report,
    is_disabled as _is_disabled,
    iter_yaml_files,
    load_tenant_configs as _load_tenant_configs_raw,
    VALID_RESERVED_KEYS,
)
from _lib_io import load_yaml_file_strict  # noqa: E402  (#2231 duplicate key = YAML error)
from _lib_io import (  # noqa: E402  (#2297 `_profile` as source text)
    YamlFileError, load_yaml_file_strict_exporter_keys, strict_load_exporter_keys,
)
from _lib_confd import iter_config_files, printable_name  # noqa: E402  (#1420)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_tenant_values import (  # noqa: E402  (#2115 profile binding is Go's)
    DaGuardError, DaGuardNotFoundError, ParseFailedError, load_effective, print_load_error,
)
from _threshold_alerts import alerts_for_key  # noqa: E402

# GitHub silently rejects (422 Unprocessable Entity) issue/PR comments over
# 65,536 chars. The config-diff bot posts render_markdown() output verbatim, so
# an oversized diff (e.g. a 150-recipe bulk refactor) would make the comment
# vanish — the highest-risk PR then reaches the reviewer with NO diff hint at
# all. Cap with ~5KB headroom for the workflow's wrapper marker/footer (Reef 2).
GITHUB_COMMENT_HARD_LIMIT = 65_536
COMMENT_SAFETY_LIMIT = 60_000

# How many not-compared file names the Markdown report spells out; the rest
# is a count. The JSON report lists them all.
UNCOVERED_LIST_LIMIT = 50


def load_configs_from_dir(dir_path):
    """Load all tenant configs from a conf.d/ directory.

    Returns {tenant_name: {metric_key: value, ...}}.
    Skips files starting with '_' or '.'.

    Supports both YAML formats:
      - Wrapped: {tenants: {name: {metric: value}}}  (actual conf.d/ format)
      - Flat: {metric: value}  (simplified / legacy)
    """
    if not Path(dir_path).is_dir():
        print(f"WARN: directory not found: {dir_path}", file=sys.stderr)
        return {}

    raw_configs = _load_tenant_configs_raw(dir_path)
    return {t: flatten_tenant_config(d) for t, d in raw_configs.items()}


def load_profiles_from_dir(dir_path):
    """Load profiles from _profiles.yaml in a conf.d/ directory.

    Returns {profile_name: {key: value, ...}} or empty dict.
    """
    base = Path(dir_path)
    if not base.is_dir():
        return {}
    profiles_path = str(base / "_profiles.yaml")
    # Strict (#2231): the exporter drops a _profiles.yaml holding a key
    # twice, so a diff of whichever value PyYAML kept last is a diff of a
    # file nobody applies — refuse it like bad syntax (main() -> rc 2).
    # #2297: profile names are the keys' source text, as the exporter keys
    # them (`010:` is "010"), so they join `load_tenant_profile_refs`.
    raw = load_yaml_file_strict_exporter_keys(profiles_path, default={})
    return raw.get("profiles", {}) if isinstance(raw, dict) else {}


def _load_tenant_configs_profile_text(dir_path):
    """``_lib_python.load_tenant_configs``, except every `_profile:` value is
    its source TEXT, as the exporter reads it (#2297): `_profile: 010` names
    profile "010", where PyYAML's 8 named none — and `010` -> `8` diffed as
    no change.

    ⚠️ A copy of that loader's walk (same ``iter_yaml_files`` listing, same
    strict read with source-text keys, same wrapper / flat split), not a
    flag on it: ``_lib_io``'s loaders are being reworked under #2115, and
    their other callers must not change here. Only `load_settings_from_dir`
    uses it (which profile a `_profile` binds is da-guard's answer,
    `load_tenant_profile_refs`); tests/ops/test_profile_ref_as_text.py pins
    that it lists the same tenants as the shared loader.
    """
    configs = {}
    for fname, fpath in iter_yaml_files(dir_path):
        try:
            stream = io.StringIO(Path(fpath).read_bytes().decode("utf-8"))
            stream.name = str(fpath)
            raw = strict_load_exporter_keys(stream, raw_text_scalars=("_profile",))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise YamlFileError(str(fpath), exc) from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            continue
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            configs[fname.rsplit(".", 1)[0]] = raw
    return configs


def load_tenant_profile_refs(dir_path):
    """{profile_name: [tenant, ...]}: the tenants of `dir_path` whose
    `_profile` names each profile, as the exporter reads the name.

    The name is `da-guard effective`'s, not a Python reading of the files
    (#2115): each tenant's `effective_config._profile`, which the exporter
    has already turned into the name it elects (Go `withProfileText`) — a
    scalar's source text (`010` is "010"), the `default:` of the mapping
    form (`_profile: {default: std}`, #2741; its other keys elect nothing).
    A value that elects no name (a mapping without `default:`, a sequence)
    is not a string there and is left out. Stripped, as Go's `profileNameOf`
    (TrimSpace) names it: `'010 '` refers to profile 010. Only a `_profile`
    the tenant's own layer writes (its file, or its entry in a root platform
    file — `key_sources` layer `tenant` / `platform`): one inherited from a
    sub-directory `_defaults.yaml` shows in effective_config but elects
    nothing (measured: `profile` None for it).

    By NAME, not by the bound profile (`profile`, None for a name no profile
    defines): a tenant still naming a profile this change removes is in its
    blast radius. Raises what `load_effective` raises (da-guard missing or
    failing, a file the exporter cannot decode).
    """
    refs = {}
    for t_name, eff in sorted(load_effective(dir_path).items()):
        profile = eff.effective_config.get("_profile")
        source = eff.key_sources.get("_profile")
        if source is None or source.layer not in ("tenant", "platform"):
            continue
        if isinstance(profile, str) and profile.strip():
            refs.setdefault(profile.strip(), []).append(t_name)
    return refs


def compute_profile_key_diff(old_data, new_data):
    """Compute fine-grained key-level diff between two profile data dicts.

    Returns list of {"key", "old", "new", "change"} entries.
    """
    if old_data is None:
        old_data = {}
    if new_data is None:
        new_data = {}

    all_keys = set(old_data.keys()) | set(new_data.keys())
    changes = []
    for key in sorted(all_keys):
        old_val = old_data.get(key)
        new_val = new_data.get(key)
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
    return changes


def compute_profile_diff(old_dir, new_dir):
    """Compute profile-level diff and blast radius with fine-grained key diffs.

    Returns list of {profile, change, affected_tenants, key_diffs} entries.
    """
    old_profiles = load_profiles_from_dir(old_dir)
    new_profiles = load_profiles_from_dir(new_dir)
    # Asked of da-guard only when some profile changed (#2115): a run with
    # no profile change needs no tenant's binding.
    new_refs = None

    all_names = set(old_profiles.keys()) | set(new_profiles.keys())
    results = []

    for name in sorted(all_names):
        old_p = old_profiles.get(name)
        new_p = new_profiles.get(name)
        if old_p == new_p:
            continue

        if new_refs is None:
            new_refs = load_tenant_profile_refs(new_dir)
        affected = new_refs.get(name, [])
        if old_p is None:
            change = "added"
        elif new_p is None:
            change = "removed"
        else:
            change = "modified"

        key_diffs = compute_profile_key_diff(old_p, new_p)

        results.append({
            "profile": name,
            "change": change,
            "affected_tenants": affected,
            "affected_count": len(affected),
            "key_diffs": key_diffs,
        })

    return results


def _compared_names(dir_path):
    """Top-level names the tenant comparison reads: what the shared loader
    lists (`iter_yaml_files`, reserved and dot files skipped)."""
    return {fname for fname, _ in iter_yaml_files(dir_path)}


def _uncovered_contents(dir_path):
    """{relative POSIX path: bytes or None} for every config file of
    `dir_path` the exporter reads and the tenant comparison does not.

    The walk is `iter_config_files`, the exporter's rule (recursive, hidden
    entries skipped); minus the top-level files `_compared_names` lists,
    what is left is the `_`-prefixed files (`_defaults.yaml`, `_profiles.yaml`,
    `_platform.yaml`, ...) and everything in a sub-directory. None = the file
    could not be read, which counts as a difference.
    """
    root = Path(dir_path)
    compared = _compared_names(dir_path)
    out = {}
    for path in iter_config_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in compared:
            continue
        try:
            out[rel] = path.read_bytes()
        except OSError:
            out[rel] = None
    return out


def compute_uncovered_files(old_dir, new_dir):
    """Changed files this report does not compare (#1420), sorted.

    The metric, setting and custom-alert sections read each top-level tenant
    file on its own; nothing here resolves `_defaults.yaml` inheritance or
    reads a sub-directory, and the Profile section reads only the `profiles:`
    of a top-level `_profiles.yaml`. A change confined to those files used to
    print "No changes detected." with exit 0 — while a platform default is
    the widest-reaching edit this report is ever handed. So such a file is
    listed when its bytes differ (or it exists on one side only): byte
    equality, not a parse, because a file this tool does not interpret has
    no semantic comparison here to lean on. A comment-only edit is listed
    too; "not compared" is the claim, not "changed meaning".
    """
    old = _uncovered_contents(old_dir)
    new = _uncovered_contents(new_dir)
    return sorted(
        rel for rel in set(old) | set(new)
        if rel not in old or rel not in new
        or old[rel] is None or new[rel] is None or old[rel] != new[rel]
    )


def flatten_tenant_config(raw):
    """Flatten a tenant YAML into {metric_key: value}, skipping reserved keys.

    Values can be: numeric (threshold), "disable", dict (scheduled), etc.
    Reserved keys (starting with '_') are excluded.
    """
    flat = {}
    for key, value in (raw or {}).items():
        if key.startswith("_"):
            continue
        flat[key] = value
    return flat


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


def load_custom_alerts_from_dir(dir_path):
    """Load tenant-OWN `_custom_alerts` recipes from a conf.d/ directory.

    Returns {tenant_name: {recipe_name: recipe_dict}}. Only tenants declaring
    at least one named recipe are included.

    Scope note: like the metric diff above, this reads each tenant file's own
    declarations and does NOT resolve `_defaults.yaml` inheritance — `_defaults`
    files are skipped by the loader. Inherited platform/domain policy recipes
    are out of scope here (consistent with how metric diffs ignore `_defaults`).
    The ADR-024 (#741) compiler in scripts/tools/dx/custom_alerts/ owns the full
    inheritance-resolved view.
    """
    if not Path(dir_path).is_dir():
        return {}

    raw_configs = _load_tenant_configs_raw(dir_path)
    out = {}
    for tenant, cfg in raw_configs.items():
        recipes = (cfg or {}).get("_custom_alerts")
        # Strict type guard (Reef 5): a mistyped `_custom_alerts: "to be added"`
        # is iterable, so a naive `for r in recipes` would iterate CHARS and
        # `r.get(...)` would AttributeError → CI bot crash → PR blocked. Skip
        # anything that isn't a list (None / dict / str). The compiler/schema
        # rejects the malformed shape separately with a clear message.
        if not isinstance(recipes, list):
            continue
        named = {}
        for r in recipes:
            if isinstance(r, dict) and r.get("name"):
                named[str(r["name"])] = r
        if named:
            out[tenant] = named
    return out


# Reserved keys this report already covers in its own section.
_SETTING_KEYS_REPORTED_ELSEWHERE = frozenset({"_custom_alerts"})

# Receiver fields in docs/schemas/tenant-config.schema.json that hold a
# credential: a Slack / Teams / Rocket.Chat webhook URL *is* the secret, and
# so are the PagerDuty keys, bearer tokens and passwords. The schema lets a
# tenant file carry them inline, and this report is posted as a PR comment —
# whose body is also mailed to every watcher and survives a history rewrite
# that purges the file. So a change to one is reported as a change, with
# both values replaced by _REDACTED. `url` and `proxy_url` are included
# because either can embed a token or `user:pass@`. The schema parity test
# fails when a new credential-shaped field is neither listed here nor named
# as not a credential.
_CREDENTIAL_KEYS = frozenset({
    "api_url", "webhook_url", "url", "proxy_url",
    "routing_key", "service_key", "bearer_token",
    "auth_password", "password",
})
_REDACTED = "<redacted>"


def load_settings_from_dir(dir_path):
    """Load each tenant's own `_`-prefixed settings from a conf.d/ directory.

    Returns {tenant_name: {"_key": value}}. `flatten_tenant_config` drops these
    keys from the metric diff, and until this section existed nothing else
    reported them: removing `_state_maintenance` (every alert of the tenant
    resumes), switching `_silent_mode`, `_routing` or `_profile` all printed
    "No changes detected" with exit 0. Every `_` key is read — not a list of
    known ones — so a reserved key added later is reported without touching
    this tool. `_custom_alerts` is left out: it has its own section.

    Same scope as the metric diff: the tenant file's own declarations, not
    `_defaults.yaml` inheritance.
    """
    if not Path(dir_path).is_dir():
        return {}
    # #2297: `_profile` as its source text — `010` -> `8` is a switch.
    raw_configs = _load_tenant_configs_profile_text(dir_path)
    return {
        tenant: {
            key: value for key, value in (cfg or {}).items()
            if str(key).startswith("_")
            and key not in _SETTING_KEYS_REPORTED_ELSEWHERE
        }
        for tenant, cfg in raw_configs.items()
    }


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
            if isinstance(old[key], dict) and isinstance(new[key], dict):
                changes.extend(_mapping_changes(old[key], new[key], path))
                continue
            change = "modified"
        else:
            change = "added" if key not in old else "removed"
        old_v, new_v = _redact_leaf(key, old.get(key), new.get(key))
        changes.append({"key": path, "old": old_v,
                        "new": new_v, "change": change})
    return changes


def _redact(value):
    """`value` with every credential field inside it replaced by _REDACTED.

    Walks mappings and lists: adding a whole `_routing`, or a list of
    receivers, carries its credentials inside a value that is not itself
    named like one.
    """
    if isinstance(value, dict):
        return {k: (_REDACTED if str(k) in _CREDENTIAL_KEYS and v is not None
                    else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _redact_leaf(key, old, new):
    if str(key) in _CREDENTIAL_KEYS:
        return (None if old is None else _REDACTED,
                None if new is None else _REDACTED)
    return _redact(old), _redact(new)


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


def render_markdown(diffs, old_dir, new_dir, profile_diffs=None,
                    custom_alert_diffs=None, setting_diffs=None,
                    uncovered_files=None):
    """Render a Markdown blast radius report."""
    lines = []
    lines.append("# Config Diff Report")
    lines.append("")
    lines.append(f"Comparing: `{old_dir}` → `{new_dir}`")
    lines.append("")

    # Coverage first (#1420): what follows is a comparison of top-level
    # tenant files, and a reviewer has to know what it left out before
    # reading it — above all when it then finds nothing. File names come
    # from the PR's tree, so each one goes through printable_name (terminal
    # escapes) and _code_span (code-span break-out, F5).
    if uncovered_files:
        lines.append("## Changed Files Not Compared")
        lines.append("")
        lines.append(
            f"> :warning: **This report does not compare "
            f"{len(uncovered_files)} changed file(s).** It reads each "
            "top-level tenant file on its own: `_`-prefixed files (platform "
            "defaults such as `_defaults.yaml`, inherited by every tenant "
            "that does not override them) and files in sub-directories are "
            "outside it, and of `_profiles.yaml` only `profiles:` is compared "
            "(Profile Changes). Review these files directly."
        )
        lines.append("")
        for rel in uncovered_files[:UNCOVERED_LIST_LIMIT]:
            lines.append(f"- {_code_span(printable_name(rel))}")
        if len(uncovered_files) > UNCOVERED_LIST_LIMIT:
            lines.append(
                f"- ... (+{len(uncovered_files) - UNCOVERED_LIST_LIMIT} more)")
        lines.append("")

    # Custom alert changes section (v2.9.0 ADR-024 Capability B, #741).
    # These are real alerting changes (add/remove/retune a paging rule), NOT
    # format-only — surfaced prominently so ops review never misses them.
    if custom_alert_diffs:
        lines.append("## Custom Alert Changes")
        lines.append("")
        lines.append(
            "> :warning: Custom alert recipes are real alerting changes "
            "(new / removed / retuned alert rules), not format-only."
        )
        lines.append("")
        for tenant, changes in custom_alert_diffs.items():
            summary = _summarize_changes(changes)
            lines.append(
                f"### {tenant} — {len(changes)} custom alert change(s) ({summary})"
            )
            lines.append("")
            for c in changes:
                # All dynamic values go inside code spans via _code_span (F5).
                name_cs = _code_span(c["name"])
                if c["change"] == "modified":
                    lines.append(f"- {name_cs} (modified)")
                    for fc in c["field_changes"]:
                        annotation = _field_change_annotation(
                            fc["field"], fc["old"], fc["new"]
                        )
                        lines.append(
                            f"  - {_code_span(fc['field'])}: "
                            f"{_code_span(fc['old'])} → {_code_span(fc['new'])}"
                            f"{annotation}"
                        )
                elif c["change"] == "added":
                    r = c["new"] or {}
                    lines.append(
                        f"- {name_cs} (added) — "
                        f"recipe={_code_span(r.get('recipe', '?'))} "
                        f"metric={_code_span(r.get('metric', '?'))} "
                        f"threshold={_code_span(r.get('threshold', '?'))} "
                        f"mode={_code_span(r.get('mode', 'page'))}"
                    )
                else:  # removed
                    r = c["old"] or {}
                    lines.append(
                        f"- {name_cs} (removed) — "
                        f"was recipe={_code_span(r.get('recipe', '?'))} "
                        f"metric={_code_span(r.get('metric', '?'))} "
                        f"threshold={_code_span(r.get('threshold', '?'))}"
                    )
            lines.append("")

    # Tenant `_` settings (maintenance, silent mode, routing, profile, ...).
    # They change what alerts do without touching a threshold, so they get
    # the same prominence as custom alerts. Values go through _code_span (F5):
    # they are rendered from the PR's files, i.e. untrusted.
    if setting_diffs:
        lines.append("## Tenant Setting Changes")
        lines.append("")
        lines.append(
            "> :warning: `_`-prefixed tenant settings change how alerts "
            "behave (maintenance, silent mode, state filters, severity dedup, "
            "routing, profile, metadata) without touching a threshold."
        )
        lines.append("")
        for tenant, changes in setting_diffs.items():
            summary = _summarize_changes(changes)
            lines.append(
                f"### {tenant} — {len(changes)} setting change(s) ({summary})"
            )
            lines.append("")
            for c in changes:
                key_cs = _code_span(c["key"])
                if c["change"] == "modified":
                    lines.append(
                        f"- {key_cs}: {_code_span(c['old'])} → "
                        f"{_code_span(c['new'])}"
                    )
                elif c["change"] == "added":
                    lines.append(f"- {key_cs} (added) — {_code_span(c['new'])}")
                else:
                    lines.append(
                        f"- {key_cs} (removed) — was {_code_span(c['old'])}"
                    )
            lines.append("")

    # Profile changes section (v1.12.0)
    if profile_diffs:
        lines.append("## Profile Changes")
        lines.append("")
        for pd in profile_diffs:
            affected = pd["affected_count"]
            tenants_str = ", ".join(pd["affected_tenants"][:10])
            if affected > 10:
                tenants_str += f" ... (+{affected - 10} more)"
            key_diffs = pd.get("key_diffs", [])
            lines.append(
                f"### Profile: {pd['profile']} ({pd['change']}) — "
                f"{affected} tenant(s) affected"
            )
            if pd["affected_tenants"]:
                lines.append(f"  Tenants: {tenants_str}")
            lines.append("")
            if key_diffs:
                lines.append("| Key | Before | After | Change |")
                lines.append("|-----|--------|-------|--------|")
                for kd in key_diffs:
                    lines.append(
                        f"| {kd['key']} | {_format_value(kd['old'])} "
                        f"| {_format_value(kd['new'])} | {kd['change']} |"
                    )
                lines.append("")
            elif pd["change"] == "added":
                lines.append(f"  New profile with {len(pd.get('key_diffs', []))} keys")
                lines.append("")
            else:
                lines.append("")

    if (not diffs and not profile_diffs and not custom_alert_diffs
            and not setting_diffs):
        if uncovered_files:
            # Not "No changes detected.": something changed, it is just
            # outside what this report compares (#1420).
            lines.append(
                "No changes among the files compared; "
                f"{len(uncovered_files)} changed file(s) not compared (above).")
        else:
            lines.append("No changes detected.")
        return "\n".join(lines)

    total_changes = 0
    for tenant, changes in diffs.items():
        change_summary = _summarize_changes(changes)
        lines.append(f"## {tenant} — {len(changes)} change(s) ({change_summary})")
        lines.append("")
        lines.append("| Metric | Before | After | Change | Affected Alerts |")
        lines.append("|--------|--------|-------|--------|-----------------|")

        for c in changes:
            alert = estimate_affected_alerts(c["key"])
            lines.append(
                f"| {c['key']} | {_format_value(c['old'])} "
                f"| {_format_value(c['new'])} | {c['change']} | {alert} |"
            )
        lines.append("")
        total_changes += len(changes)

    lines.append("---")
    # "tenant(s) changed" must count tenants changed via metric OR custom-alert
    # diffs (CodeRabbit #773): a custom-alert-only run otherwise prints
    # "0 tenant(s) changed" while the custom-alert clause below says otherwise.
    changed_tenants = set(diffs)
    changed_tenants.update(custom_alert_diffs or {})
    changed_tenants.update(setting_diffs or {})
    changed = len(changed_tenants)
    profile_affected = sum(pd["affected_count"] for pd in (profile_diffs or []))
    summary = f"Summary: {changed} tenant(s) changed, {total_changes} metric change(s)"
    if profile_affected:
        summary += f", {profile_affected} tenant(s) affected by profile changes"
    if custom_alert_diffs:
        ca_tenants = len(custom_alert_diffs)
        ca_changes = sum(len(v) for v in custom_alert_diffs.values())
        summary += (
            f", {ca_tenants} tenant(s) with {ca_changes} custom alert change(s)"
        )
    if setting_diffs:
        st_changes = sum(len(v) for v in setting_diffs.values())
        summary += (
            f", {len(setting_diffs)} tenant(s) with {st_changes} "
            f"setting change(s)"
        )
    if uncovered_files:
        summary += f", {len(uncovered_files)} changed file(s) not compared"
    lines.append(summary)

    # Truncation safeguard (Reef 2): never let the bot comment exceed GitHub's
    # ceiling — a silently-dropped comment on a huge, high-risk PR is worse than
    # a truncated one. The exit code + changed files remain authoritative.
    result = "\n".join(lines)
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


def build_parser():
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Compare two conf.d/ directories — per-tenant blast radius report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/ --json-output
        """),
    )
    parser.add_argument("--old-dir", required=True,
                        help="Baseline config directory")
    parser.add_argument("--new-dir", required=True,
                        help="New/staged config directory")
    parser.add_argument("--json-output", action="store_true",
                        help="Output JSON instead of Markdown")
    parser.add_argument("--format", choices=["markdown", "json"],
                        default="markdown",
                        help="Output format (default: markdown). "
                             "Alias: --json-output is equivalent to --format json")
    return parser


def main():
    """Entry point.

    Exit codes (SSOT: ``scripts/tools/_lib_exitcodes.py``):
      0 — no changes detected
      1 — changes detected (useful for CI: non-zero = PR has config diff)
      2 — caller error: bad args, missing dirs, malformed input, or any
          unexpected crash

    ⛔ Why the handler below catches ``Exception`` instead of naming the
    exception types: measured on this tool, the inputs that MUST land on 2
    span three different layers, and the third is unreachable from any
    try/except placed around the loaders.

      * ``yaml.YAMLError`` family (parser / scanner / composer / reader) —
        a malformed file under either ``--old-dir`` or ``--new-dir``.
      * ``UnicodeDecodeError`` — a conf.d file that is not UTF-8. Not a
        ``YAMLError``; ``open()`` raises before the parser ever runs.
      * ``ValueError: Circular reference detected`` — a self-referencing YAML
        anchor (``tenants: &a {t: *a}``). ``yaml.safe_load`` accepts it
        happily; the raise comes from ``json.dumps`` under ``--format json``,
        i.e. AFTER loading and rendering.

    An enumerated tuple was tried and provably misses the last two, so this is
    anchored on "the run did not complete" rather than on a list of spellings.

    What the collision costs, and why 2 is the correct code rather than a
    tidier 1: an uncaught exception exits **1**, which is this tool's
    "changes detected" signal, while stdout stays empty. Every consumer that
    follows the documented contract (``docs/integration/gitops-deployment.md``
    — 0 skip / 1 post the report / 2 fail the pipeline) therefore reads a
    crash as a finding and posts, or silently skips, an empty report. The
    exit-code SSOT already assigns "malformed input YAML/JSON ... or an
    unexpected crash" to ``EXIT_CALLER_ERROR``; this makes the code obey it.
    """
    args = build_parser().parse_args()
    try:
        return _run(args)
    except (DaGuardNotFoundError, ParseFailedError, DaGuardError) as exc:
        # The profile bindings (`load_tenant_profile_refs`, #2115): da-guard
        # missing or failing, or a --new-dir file the exporter cannot
        # decode. Same exit 2, with da-guard's own lines (its reason for a
        # dropped file) below one ERROR line — `str()` alone loses them.
        print(f"ERROR: cannot compare configs (--old-dir {args.old_dir} "
              f"--new-dir {args.new_dir}): reading the --new-dir profile bindings "
              f"through da-guard failed", file=sys.stderr)
        print_load_error(exc)
        sys.exit(EXIT_CALLER_ERROR)
    except Exception as exc:  # noqa: BLE001 — deliberate; see docstring
        # ``sys.exit`` inside ``_run`` raises SystemExit, which derives from
        # BaseException, so the tool's own 0/1/2 exits pass through untouched.
        #
        # ``str(exc)``, not ``repr(exc)``: a YAMLError's str() carries the file
        # name, line and column, while its repr() replaces those Mark objects
        # with memory addresses. The reader is someone in a CI log deciding
        # between "the image did not pull", "the mount is wrong" and "one of my
        # config files is broken".
        #
        # ⚠️ Measured, so that the next person does not over-trust this: only
        # the YAMLError family actually names the offending file — and since
        # #1654 that family includes non-UTF-8 content, because the shared
        # loader now hands ``yaml.safe_load`` the bytes and wraps the result
        # in ``YamlFileError`` with the path attached (measured: a ``\xff``
        # byte in ``alpha.yaml`` used to print this line with no filename;
        # it now names the file). ``ValueError('Circular reference
        # detected')`` still says nothing at all. Hence the type name (str()
        # alone can be that terse) and both directory paths (the exception
        # never says which side it was reading).
        print(
            f"ERROR: cannot compare configs "
            f"(--old-dir {args.old_dir} --new-dir {args.new_dir}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CALLER_ERROR)


def _run(args):
    """Do the comparison. Exits 0/1 itself; raises on anything unexpected."""
    if not Path(args.old_dir).is_dir():
        print(f"ERROR: old-dir not found: {args.old_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if not Path(args.new_dir).is_dir():
        print(f"ERROR: new-dir not found: {args.new_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    old_configs = load_configs_from_dir(args.old_dir)
    new_configs = load_configs_from_dir(args.new_dir)

    diffs = compute_diff(old_configs, new_configs)
    profile_diffs = compute_profile_diff(args.old_dir, args.new_dir)
    custom_alert_diffs = compute_custom_alert_diff(
        load_custom_alerts_from_dir(args.old_dir),
        load_custom_alerts_from_dir(args.new_dir),
    )
    setting_diffs = compute_setting_diff(
        load_settings_from_dir(args.old_dir),
        load_settings_from_dir(args.new_dir),
    )
    uncovered_files = compute_uncovered_files(args.old_dir, args.new_dir)

    use_json = args.json_output or args.format == "json"
    if use_json:
        output = {
            "metric_diffs": diffs,
            "profile_diffs": profile_diffs,
            "custom_alert_diffs": custom_alert_diffs,
            "setting_diffs": setting_diffs,
            "uncovered_files": uncovered_files,
        }
        print(format_json_report(output, default=str))
    else:
        print(render_markdown(
            diffs, args.old_dir, args.new_dir,
            profile_diffs=profile_diffs,
            custom_alert_diffs=custom_alert_diffs,
            setting_diffs=setting_diffs,
            uncovered_files=uncovered_files,
        ))

    # Exit 1 if changes detected (CI signal), 0 if clean. A changed file
    # this report does not compare is a change (#1420): exit 0 is read as
    # "nothing to review", and a platform default edit is not that.
    has_changes = (bool(diffs) or bool(profile_diffs)
                   or bool(custom_alert_diffs) or bool(setting_diffs)
                   or bool(uncovered_files))
    sys.exit(EXIT_VIOLATION if has_changes else EXIT_OK)


if __name__ == "__main__":
    main()
