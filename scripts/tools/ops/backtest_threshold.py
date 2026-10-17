#!/usr/bin/env python3
"""backtest_threshold.py — Backtest threshold changes against historical Prometheus data.

Given a set of threshold changes (two conf.d trees, or one change given on
the command line), queries Prometheus range data to simulate how alert firing
counts would change under old vs new thresholds. Produces a risk assessment
report suitable for PR review.

Each change is replayed against the one series its alert compares, the way
the alert compares it (`>` / `<`), as metric_observed_map.yaml records it
(#3017 / #3002); a key the map does not record that way is listed as not
backtested.

Usage:
  # A PR's conf.d against its base's (both trees read by the exporter,
  # `da-guard served-values`). The base is a checkout the caller makes, e.g.
  #   git worktree add /tmp/base HEAD~1
  python3 backtest_threshold.py --config-dir conf.d/ --baseline /tmp/base/conf.d/ \
    --prometheus http://localhost:9090

  # Manual single metric
  python3 backtest_threshold.py --tenant db-a --metric mysql_connections \
    --old-value 70 --new-value 50 --prometheus http://localhost:9090

  # Skip if Prometheus unavailable (CI-friendly); JSON + Markdown for a PR comment
  python3 backtest_threshold.py --config-dir conf.d/ --baseline /tmp/base/conf.d/ \
    --prometheus http://localhost:9090 --skip-if-unavailable \
    --json --markdown-output /tmp/backtest-comment.md

`--git-diff` was removed (#3111): it exits 2 naming the commands above.

需求:
  - Prometheus Query API reachable (or --skip-if-unavailable)
  - da-guard (DA_GUARD_BINARY, or on PATH) for --config-dir / --baseline
"""
import argparse
import errno
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import is_disabled, http_get_json, query_prometheus_range, write_json_or_die, write_text_or_die, add_prometheus_arg  # noqa: E402
from _lib_python import YamlFileError  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import OutputWriteError  # noqa: E402  (#3089 output preflight)
from _lib_io import load_yaml_file_strict_exporter_keys  # noqa: E402  (#2216)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR,
    arm_crash_envelope, disarm_crash_envelope,
)
from _lib_validation import is_valid_tenant_id, tenant_id_rule  # noqa: E402  (#3066)
from _lib_validation import GO_SPACE, THRESHOLD_SEVERITIES, split_threshold_severity  # noqa: E402  (#3071)
# #2119: both trees (`--config-dir` / `--baseline`) are read by the exporter,
# not here.
from _lib_tenant_values import (  # noqa: E402
    DaGuardError,
    DaGuardNotFoundError,
    ServedValuesError,
    DA_GUARD_PREFIX,
    canonical_key,
    load_served_tree,
    now_at,
    print_load_error,
)
# #3017 / #3002: what each alert compares (series + direction) is read from the
# observed-map, never guessed; a `--tenant` key is as written, so a retired
# spelling goes through the alias table first.
import _observed_map_lib as observed_map_lib  # noqa: E402
from _registry_lib import DEPRECATED_KEY_ALIASES  # noqa: E402
from _lib_confd import (  # noqa: E402
    list_config_tree,
    is_hidden_name,
)

# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------
DEFAULT_LOOKBACK = "7d"
DEFAULT_STEP = "5m"
RISK_THRESHOLDS = {
    "HIGH": 50,    # >50% change in firing count
    "MEDIUM": 20,  # >20% change
    "LOW": 0,      # any change
}


def parse_lookback(lookback_str):
    """Convert lookback string (e.g., '7d', '24h') to seconds.

    Raises ``ValueError`` on anything that is not ``<number><d|h|m>``.

    ⛔ #1625: this used to ``return 7 * 86400`` on a non-match, so a bare
    ``7`` (the shape the docs taught), ``banana`` and the empty string all
    measured 604800 s and the run exited 0 — a supplied-but-unusable value
    silently replaced by the default (agent-rulebook D-06). The grammar is
    deliberately NOT widened: the fix is to fail loud, not to accept more.
    """
    m = re.match(r"^(\d+)([dhm])$", lookback_str)
    if not m:
        raise ValueError(
            f"invalid lookback {lookback_str!r}: expected <number><d|h|m>, "
            "e.g. 7d / 24h / 30m")
    val = int(m.group(1))
    unit = m.group(2)
    multipliers = {"d": 86400, "h": 3600, "m": 60}
    return val * multipliers[unit]


def lookback_arg(value: str) -> str:
    """argparse ``type`` for ``--lookback``: validate, return the ORIGINAL string.

    The report echoes the string the caller gave (``generate_report(results,
    args.lookback)`` → ``"lookback": "3d"``), so the parsed seconds are
    recomputed later via ``parse_lookback``; this only turns an unusable
    value into an argparse usage error (exit 2 = EXIT_CALLER_ERROR) that
    names the value and the accepted shape. argparse applies ``type`` to a
    string default too, so ``DEFAULT_LOOKBACK`` is validated on every run.
    """
    try:
        parse_lookback(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return value


def prometheus_available(prom_url, timeout=5):
    """Check if Prometheus is reachable."""
    url = f"{prom_url}/api/v1/status/buildinfo"
    data, err = http_get_json(url, timeout=timeout)
    return err is None


class PrometheusQueryError(Exception):
    """A range query Prometheus did not answer (HTTP error, timeout, an
    ``error`` status, a body that is not a JSON object); the message is the
    error text as received."""


def query_range(prom_url, query, lookback_seconds, step=DEFAULT_STEP):
    """Execute a Prometheus range_query and return result data.

    Thin wrapper over ``_lib_prometheus.query_prometheus_range`` (ROI r3 W1);
    tests monkeypatch this module attribute by name — keep it.

    Raises ``PrometheusQueryError`` when the query fails (#3059): a failed
    query used to return ``[]`` here, so it was reported as ``no_data`` —
    "no history" — with exit code 0. An empty result is still ``[]``.
    """
    import time
    end = time.time()
    start = end - lookback_seconds

    result, err = query_prometheus_range(
        prom_url, query, start, end, step, timeout=30)
    if err:
        raise PrometheusQueryError(str(err))
    return result


def _is_nan_sample(val_str):
    """Whether a Prometheus sample value is NaN (#2963).

    Only NaN: ``+Inf`` / ``-Inf`` are real samples (an alert on them does
    compare and can fire), so they are NOT missing data. An unparseable value
    is left to :func:`count_threshold_breaches`, which already skips it.
    """
    try:
        return math.isnan(float(val_str))
    except (ValueError, TypeError):
        return False


def count_threshold_breaches(values, threshold, direction="above"):
    """Count how many data points breach a threshold.

    direction: 'above' (value > threshold) or 'below' (value < threshold).
    """
    if threshold is None:
        return 0
    try:
        threshold = float(threshold)
    except (ValueError, TypeError):
        return 0

    count = 0
    for _, val_str in values:
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            continue
        if direction == "above" and val > threshold:
            count += 1
        elif direction == "below" and val < threshold:
            count += 1
    return count


def tenant_is_queryable(tenant):
    r"""Whether `tenant` can go into the query's tenant selector as written.

    Not when it holds `"`, `\` or a control character (a PromQL string would
    end or escape there: the query is invalid, or asks about another
    tenant), nor when it is not valid UTF-8 (`urlencode` raises on a lone
    surrogate, #1634). `--tenant` is held to the tenant id rule up front
    (#3066); a tree's tenant ids are the exporter's `tenants:` keys, which
    it does not hold to that rule (#3111 review: it serves `a|b"c`), so a
    change of such a tenant is listed as not backtested (`not_backtestable`)
    rather than queried or dropped.
    """
    try:
        tenant.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ch in '"\\' or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in tenant)


def _render_served(value):
    """A served threshold as the text this report carries (`None` stays None:
    no /metrics row). Go's text for a non-finite value, an integral value
    without a fraction."""
    if value is None:
        return None
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if float(value).is_integer() else repr(float(value))


_DAY_MINUTES = 24 * 60


def _minute(hhmm):
    hours, _, minutes = hhmm.partition(":")
    return int(hours) * 60 + int(minutes)


def _value_in(day, minute):
    for start, end, value, _key in day:
        if start <= minute < end:
            return value
    return None


def _hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _differing_runs(old_day, new_day):
    """`[(start, end, old, new)]`: every maximal run of the day over which
    the two sides serve one same (old, new) pair and differ, in day order.
    A run is not joined across midnight."""
    bounds = sorted({b for day in (old_day, new_day) for s, e, *_ in day for b in (s, e)})
    runs = []
    for start, end in zip(bounds, bounds[1:]):
        pair = (_value_in(old_day, start), _value_in(new_day, start))
        if runs and runs[-1][1] == start and runs[-1][2:] == pair:
            runs[-1] = (runs[-1][0], end) + pair
        else:
            runs.append((start, end) + pair)
    return [r for r in runs if r[2] != r[3]]


def _series_identity(series):
    """What makes two /metrics rows the same row: the family name and the
    full label set (#3111). The threshold key that serves it is not part of
    it — `90:critical` on a base key and `90` on its `_critical` key are one
    row, and an alert cannot tell them apart."""
    return series.name, tuple(sorted(series.labels.items()))


def _served_rows(tenant_values, side):
    """`{identity: (series, [(start_minute, end_minute, value, key)])}`: every
    /metrics row (`Series`, #2750) one tree serves for a tenant over the UTC
    day (`schedules`), with the rendered value and the threshold key serving
    it in each part of the day. A legacy twin (the same row again under a
    retired identity) is not a row of its own. A part of the day in which
    /metrics cannot be gathered at all is refused: a diff across it would be
    a guess."""
    out = {}
    if tenant_values is None:
        return out
    for key, schedule in sorted((tenant_values.schedules or {}).items()):
        for seg in schedule.segments:
            if seg.error is not None:
                raise ServedValuesError(
                    f"da-guard served-values: {side} /metrics cannot be gathered "
                    f"{seg.start}-{seg.end} UTC ({seg.error})", None, "")
            if seg.value is None:
                continue
            for series in seg.series:
                if series.legacy_twin:
                    continue
                out.setdefault(_series_identity(series), (series, []))[1].append(
                    (_minute(seg.start), _minute(seg.end), _render_served(seg.value), key))
    return out


def _row_day(parts):
    """One row's whole UTC day from its served parts: `[(start, end, value,
    key)]` in day order, a part it is not served in as `(…, None, None)`."""
    day, at = [], 0
    for start, end, value, key in sorted(parts):
        if start > at:
            day.append((at, start, None, None))
        day.append((start, end, value, key))
        at = end
    if at < _DAY_MINUTES:
        day.append((at, _DAY_MINUTES, None, None))
    return day


def _keys_in(days, spans):
    """The threshold keys serving a row on either side in any of `spans`, sorted."""
    return sorted({key for day in days for start, end in spans
                   for s, e, _v, key in day if key is not None and s < end and start < e})


def _alert_key(series):
    """The threshold key whose alert compares a row (#3017): a critical row
    is compared by the `<metric_key>_critical` alert, any other by
    `metric_key`'s (whether one exists is `alert_comparison`'s question)."""
    if series.labels.get("severity") == "critical":
        return series.metric_key + "_critical"
    return series.metric_key


def _print_tree_warnings(trees):
    """`print_load_warnings` for the two trees, each line named once: a line
    both trees carry is printed once and says so, any other names its tree
    by its flag. Every line escaped (`safe_label`); da-guard's own stderr
    behind `DA_GUARD_PREFIX`."""
    for kind in ("stderr", "skipped"):
        lines = {}
        for flag, tree in trees:
            got = (tree.stderr_lines if kind == "stderr"
                   else [f"{s.file}: {s.reason}" for s in tree.skipped])
            for line in got:
                lines.setdefault(line, []).append(flag)
        for line, flags in lines.items():
            where = "both trees" if len(set(flags)) > 1 else flags[0]
            if kind == "stderr":
                print(f"{DA_GUARD_PREFIX}[{where}] {safe_label(line)}", file=sys.stderr)
            else:
                print(f"WARN: [{where}] {safe_label(line)}", file=sys.stderr)


def _row_identity(series):
    """What a threshold recording rule groups a /metrics row by: its metric
    key and severity (`max by(tenant)` over every row of that pair)."""
    return series.metric_key, series.labels.get("severity")


def _dimensioned_rows(sides):
    """`{(metric_key, severity): {key, ...}}`: the keys serving a dimensioned
    row of the tenant, on either side at any time of the day."""
    out = {}
    for rows in sides:
        for series, parts in rows.values():
            if series.dimensions or series.dimensions_regex:
                out.setdefault(_row_identity(series), set()).update(k for *_x, k in parts)
    return out


def extract_changes_from_dirs(config_dir, baseline_dir):
    """Threshold changes between two conf.d trees, as /metrics serves them.

    Returns list of dicts: [{tenant, metric, old_value, new_value, keys,
    series}, ...], plus `window` (a list of `"HH:MM-HH:MM"`, UTC) on a
    change that holds in part of the day only.

    #2119: both trees are read by the exporter (`da-guard served-values`),
    never by re-reading YAML here, so a change written in `_platform.yaml`,
    a `_defaults.yaml`, a profile or a sub-directory is a change, and a
    `tenants:` wrapper is a tenant, not a metric called `tenants`.

    #3111: rows are per tenant and /metrics row (its family name and full
    label set, `_series_identity`) — what the alerts see — not per threshold
    key. The same row written under another key (`90:critical` on the base
    key, or `disable` plus `90` on its `_critical` key) is no change; moving
    a value to another severity (`70` -> `70:critical`) is two rows, one that
    stops being served and one that starts, because each is compared by
    another alert (#3060). A legacy twin is not a row of its own.

    * `metric`: the threshold key whose alert compares the row (`_alert_key`:
      `<metric_key>_critical` for a critical row, else `metric_key`); for a
      dimensioned row the key that serves it (the first, when several do).
      `keys`: every threshold key serving the row on either side in the
      parts of the day the change holds in, in canonical spelling.
    * `series`: `[the row's Series]` (#2750); `alert_comparison` reads which
      alert compares it from that.
    * `dimensioned_siblings` (only when non-empty): the keys serving a
      dimensioned row of the tenant, on either side at any time of the day,
      of the same metric key and severity as this row. The alert's
      threshold is `max by(tenant)` over all of those rows, so the changed
      value alone is not what the alert compares (#3017).
    * A row served on one side only (switched off, dropped, moved to another
      severity, or the tenant added / removed) has None on the other side —
      the shape `backtest_change` reads as "enabled" / "disabled".
    * Values are compared over the whole UTC day (`schedules`, both trees
      read at the same instant). One change per (old, new) pair a row takes:
      `window` lists every part of the day the pair holds in, and is absent
      when it holds all day. A pair is backtested once, over the whole
      lookback (the Prometheus query is not restricted to its windows), so
      one change split across parts of the day is not counted twice.

    Raises what `load_served_tree` raises (da-guard missing, stale or
    failing; a file it cannot decode or read), and `ServedValuesError` for a
    part of the day in which either tree's /metrics cannot be gathered: the
    caller exits 2 rather than reporting a partial diff.
    """
    at = now_at()
    trees = [(flag, load_served_tree(conf_d, at=at, schedules=True))
             for flag, conf_d in (("--baseline", baseline_dir), ("--config-dir", config_dir))]
    _print_tree_warnings(trees)
    old_tree, new_tree = (tree.tenants for _flag, tree in trees)

    changes = []
    for tenant in sorted(set(old_tree) | set(new_tree)):
        sides = (_served_rows(old_tree.get(tenant), "baseline"),
                 _served_rows(new_tree.get(tenant), "current"))
        dimensioned = _dimensioned_rows(sides)
        tenant_changes = []
        for identity in set(sides[0]) | set(sides[1]):
            series = (sides[1].get(identity) or sides[0][identity])[0]
            days = tuple(_row_day(side[identity][1] if identity in side else [])
                         for side in sides)
            pairs = {}
            for start, end, old, new in _differing_runs(*days):
                pairs.setdefault((old, new), []).append((start, end))
            dimensioned_row = bool(series.dimensions or series.dimensions_regex)
            for (old_v, new_v), spans in pairs.items():
                keys = _keys_in(days, spans)
                change = {"tenant": tenant,
                          "metric": _alert_key(series) + (
                              "{" + keys[0].partition("{")[2] if dimensioned_row else ""),
                          "old_value": old_v, "new_value": new_v, "keys": keys}
                if spans != [(0, _DAY_MINUTES)]:
                    change["window"] = [f"{_hhmm(s)}-{_hhmm(e)}" for s, e in spans]
                change["series"] = [series]
                if not dimensioned_row:
                    siblings = sorted(dimensioned.get(_row_identity(series), ()))
                    if siblings:
                        change["dimensioned_siblings"] = siblings
                tenant_changes.append(((change["metric"], identity), change))
        changes.extend(c for _order, c in sorted(tenant_changes, key=lambda oc: oc[0]))

    return changes


# ---------------------------------------------------------------------------
# Custom-alert recipe awareness (#657)
#
# This tool is the *flat-threshold* eval home: it only understands scalar
# `metric: value` thresholds. Custom-alert recipes (ADR-024 `_custom_alerts`)
# are evaluated by a different authoritative engine (the compiler + promtool).
# Without these guards a recipe-only change passes silently (the flat tool
# finds nothing). We surface recipes loudly and keep them out of the flat
# report instead. Recipe would-fire preview: #657.
# ---------------------------------------------------------------------------

def load_conf_files(paths):
    """Parse tenant conf.d files into {tenant: parsed_dict}.

    Skips platform files (`_`-prefixed), hidden ones and anything that
    isn't a YAML mapping; a path that is not a file is skipped silently.
    """
    parsed = {}
    for p in paths:
        path = Path(p)
        # The hidden axis as well as the reserved one: the exporter's
        # scanner skips `.`-prefixed entries, so a hidden file declares no
        # tenant this report should name.
        if (path.name.startswith("_") or is_hidden_name(path.name)
                or not path.is_file()):
            continue
        # The caller's own spelling, not `str(path)` (#2558): an unreadable
        # file is named in the form the caller gave.
        # #2216: tenant ids are the keys' source text, as the exporter reads
        # them — `find_custom_alert_tenants` reports `010:` as "010", not 8.
        data = load_yaml_file_strict_exporter_keys(os.fspath(p), default={})
        if isinstance(data, dict):
            parsed[path.stem] = data
    return parsed


def find_custom_alert_tenants(parsed):
    """Tenant IDs that declare a non-empty `_custom_alerts` block.

    conf.d wraps tenants as `tenants: {<id>: {<metric>: <value>,
    _custom_alerts: [...]}}`, so recipes live at `tenants.<id>._custom_alerts`
    (NOT top-level) and the tenant id is the KEY (not the filename). These
    recipes are evaluated by the compiler+promtool eval home, NOT by this
    flat-threshold backtest. Surfacing them keeps a "no flat changes" result
    from being mistaken for "nothing to review". See issue #657.
    """
    found = []
    for file_data in parsed.values():
        tenants = file_data.get("tenants")
        if not isinstance(tenants, dict):
            continue
        for tid, tconf in tenants.items():
            if isinstance(tconf, dict) and tconf.get("_custom_alerts"):
                found.append(tid)
    return sorted(set(found))


def custom_alert_notice(tenants):
    """stderr notice listing tenants with custom-alert recipes (or '' if none)."""
    if not tenants:
        return ""
    listed = ", ".join(tenants)
    return (
        f"NOTE: {len(tenants)} tenant(s) declare custom-alert recipes "
        f"(_custom_alerts): {listed}\n"
        "      Recipes are evaluated by the compiler+promtool eval home, "
        "not by this flat-threshold backtest.\n"
        "      Recipe would-fire preview: see issue #657 (portal recipe builder)."
    )


def custom_alert_markdown(tenants):
    """Markdown block surfacing recipe changes in the PR comment."""
    listed = ", ".join(f"`{t}`" for t in tenants)
    return (
        "## Custom-Alert Recipes (not flat-backtested)\n"
        "\n"
        f"{len(tenants)} tenant(s) declare custom-alert recipes "
        f"(`_custom_alerts`): {listed}\n"
        "\n"
        "> Recipes are evaluated by the **compiler + promtool** eval home, "
        "not by this flat-threshold backtest. For a recipe would-fire preview "
        "see [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657)."
    )


_OBSERVED_MAP = None


def _observed_map():
    """The committed observed-map's `keys` (`load_observed_map`), loaded once
    per run. Raises `ObservedMapError` when it cannot be used: `main` turns
    that into rc 2 — a backtest with no record of what the alerts compare
    would be the #3002 guess again, not a smaller backtest."""
    global _OBSERVED_MAP
    if _OBSERVED_MAP is None:
        _OBSERVED_MAP = observed_map_lib.load_observed_map()
    return _OBSERVED_MAP


# The severities a threshold recording rule selects (`severity="warning"` for
# `tenant:alert_threshold:<key>`, `severity="critical"` for `<key>_critical`),
# which are the only ones the exporter serves (#3071): Go's
# `config.ThresholdSeverities`, carried by tests/shared/threshold_value_matrix.json.
_COMPARED_SEVERITIES = THRESHOLD_SEVERITIES


def parse_threshold_value(key, raw):
    """`(value, severity)` as the exporter reads one threshold value written
    for `key`, or None when it is switched off; raises ValueError for a value
    this backtest cannot read the way the exporter does (#3017).

    Mirrors `pkg/config/resolve.go`: a disabled value (`is_disabled`) serves
    nothing; a `<base>_critical` key's whole value is the number and its row
    is critical (`resolveCriticalRows`); any other key's value is
    `splitThresholdSeverity`'s grammar (#3071): split at the LAST `:` into
    number and severity, each trimmed, an ASCII severity lower-cased
    (`"70:Critical"` is critical; no `:` is `warning`), and a severity other
    than `_COMPARED_SEVERITIES` (`70:high`, the empty `70:`) makes the value
    unreadable (`_lib_validation.split_threshold_severity`). The number is read with Python's `float`
    only where it agrees with Go's `ParseFloat` (measured, go1.x):

    * refused, Go refuses too: a non-ASCII digit; a signed NaN (`+nan`);
      an overflow (`1e400` — Go returns ErrRange, so the exporter treats the
      value as unreadable, while `float` answers inf);
    * refused although Go accepts — conservative, it only costs a backtest:
      an underscore (`1_000`), a hex float (`0x1p4`);
    * accepted, both agree: decimals, `NaN`, `Inf` / `-inf` / `infinity`.

    A value the exporter itself cannot read is replaced with the default or
    dropped, which this tool cannot see from the value alone."""
    if raw is None or is_disabled(str(raw)):
        return None
    if key.endswith("_critical"):
        number, severity = str(raw).strip(GO_SPACE), "critical"
    else:
        # #3071: raises for a severity the exporter cannot read either.
        number, severity = split_threshold_severity(raw)
    if not number.isascii() or "_" in number:
        raise ValueError(raw)
    value = float(number)
    spelled = number.lower().lstrip("+-")
    if math.isnan(value) and number.lower() != "nan":
        raise ValueError(raw)  # `+nan` / `-nan`: Go refuses a signed NaN
    if math.isinf(value) and spelled not in ("inf", "infinity"):
        raise ValueError(raw)  # overflow: Go's ErrRange
    return value, severity


def _no_alert(key):
    """Whether no shipped alert compares threshold key `key` (#3111 (e)): the
    observed-map has no entry for it and it is not KNOWN_DEFERRED."""
    return observed_map_lib.lookup(_observed_map(), key)[0] is observed_map_lib.LookupKind.NO_ALERT


def _written_key(change):
    """A change's key as `--tenant` gives it, in canonical spelling, without
    a dimension part."""
    return canonical_key(change["metric"], DEPRECATED_KEY_ALIASES).partition("{")[0]


def _written_alert(key, parsed):
    """The threshold key whose alert compares one side of a change written as
    `--tenant` gives it, read as `parsed` (`parse_threshold_value`): `N:critical`
    on a base key is a critical row, compared by the `<key>_critical` alert."""
    return key + "_critical" if parsed[1] == "critical" and not key.endswith("_critical") else key


def written_changes(tenant, metric, old_value, new_value):
    """`--tenant`'s one edit as the /metrics rows it changes (#3111), by the
    rule `extract_changes_from_dirs` applies to two trees, without da-guard:
    each side's value is read as the exporter reads it
    (`parse_threshold_value`), and a side is the row of its severity — the
    base key's warning row, or the critical row its `<key>_critical` alert
    compares (`_written_alert`). So `70` -> `70:critical` is two rows, the
    warning one switched off and the critical one switched on, and `70` ->
    `70` is none. `metric` is the row's alert key (a dimension part kept),
    its values the numbers alone, `keys` the key as given.

    A value this backtest cannot read the way the exporter does is one row
    of the values as written; `not_backtestable` names why."""
    key = canonical_key(metric, DEPRECATED_KEY_ALIASES)
    base, brace, dims = key.partition("{")
    try:
        sides = [parse_threshold_value(base, raw) for raw in (old_value, new_value)]
    except ValueError:
        return [{"tenant": tenant, "metric": metric, "old_value": old_value,
                 "new_value": new_value}]
    rows = {}
    for side, parsed in zip(("old_value", "new_value"), sides):
        if parsed is not None:
            name = _written_alert(base, parsed) + brace + dims
            rows.setdefault(name, {"old_value": None, "new_value": None})[side] = (
                _render_served(parsed[0]))
    return [{"tenant": tenant, "metric": name, **values, "keys": [metric]}
            for name, values in sorted(rows.items())
            if values["old_value"] != values["new_value"]]


def _written_alert_keys(change):
    """`(keys, None)`: the threshold keys whose alerts compare a change
    written as `--tenant` gives it — or `(None, reason)`.

    The key as written: a retired spelling goes through the alias table
    (the exporter's own), and `N:critical` on a base key is a critical row,
    compared by the `<key>_critical` alert. #3060: each side is compared by
    the alert of its own severity, so `70` -> `50:critical` is compared by
    both. A value that is not read as written is a reason — since #3071 that
    includes a severity no threshold rule selects (`70:high`, `70:`), which
    makes the exporter's whole value unreadable (`parse_threshold_value`
    raises).

    #3111: `written_changes` makes every `--tenant` change one /metrics row,
    so both sides name one alert. (e): an alert key no shipped alert
    compares (`_no_alert`: mongodb's `_critical`, its pack has no such
    alert) compares nothing; with no key left the change is not
    backtested, the reason naming that key."""
    key = _written_key(change)
    keys, no_alert = [], []
    for raw in (change["old_value"], change["new_value"]):
        try:
            parsed = parse_threshold_value(key, raw)
        except ValueError:
            return None, f"value {str(raw)!r}: this backtest cannot read it the way the exporter does"
        if parsed is None:
            continue
        alert = _written_alert(key, parsed)
        if _no_alert(alert):
            no_alert.append(alert)
        elif alert not in keys:
            keys.append(alert)
    if not keys:
        if not no_alert:
            return None, "no threshold on either side"
        reason = observed_map_lib.lookup_reason(observed_map_lib.LookupKind.NO_ALERT, no_alert[0])
        return None, reason if no_alert[0] == key else f"{no_alert[0]}: {reason}"
    return keys, None


def _alert_keys(change):
    """`(keys, None)`: the threshold keys whose alerts compare this change,
    in canonical spelling, each once — or `(None, reason)`.

    A change read from two trees names its /metrics row (#2750, #3111):
    a critical row is compared by the `<metric_key>_critical` alert (a window
    overridden to `N:critical` too), a warning row by `metric_key`'s
    (`_alert_key`); a row of any other severity (the rules select
    `severity="warning"` / `"critical"` only) by none, and is not
    backtested. Since #3071 the exporter serves no such row; one comes only
    from an older da-guard named by `DA_GUARD_BINARY`, which served `60:high`
    as written. A legacy twin is the same row under a retired name and is
    never read.
    ⛔ A non-critical row whose `metric_key` itself ends in `_critical` is a
    `<base>_critical` key served on its own (its base is not declared in the
    root `defaults:`): its metric label is `<x>_critical`, which no threshold
    rule selects, so no alert compares it. A change from `--tenant` is
    `_written_alert_keys`."""
    if "series" not in change:
        return _written_alert_keys(change)
    out, uncompared = [], []
    for s in change["series"]:
        if s.legacy_twin:
            continue
        severity = s.labels.get("severity")
        if severity not in _COMPARED_SEVERITIES:
            uncompared.append(severity)
            continue
        if severity != "critical" and s.metric_key.endswith("_critical"):
            return None, (f"served as a non-critical row of {s.metric_key} (its base is not "
                          "declared in the root defaults:), which no alert compares")
        key = _alert_key(s)
        if key not in out:
            out.append(key)
    if not out and uncompared:
        return None, f"severity {uncompared[0]!r} is selected by no threshold rule"
    return out, None


def alert_comparison(change):
    """`(series, "above" | "below", None)`: the recording rule the change's
    alert compares against its threshold and the way it compares, from the
    observed-map (#3017 / #3002) — or `(None, None, reason)`.

    ⛔ Nothing is guessed. A key the map has no entry for (no shipped alert
    compares it, or KNOWN_DEFERRED: `observed_map_lib.lookup`'s kind), or
    whose entry a backtest cannot replay (`usable_for_backtest`: needs
    review, composite, unresolved direction, not per-tenant), has a reason
    and is not backtested; so is a change whose rows are compared by alerts
    that disagree on the series or the direction (one replay cannot stand
    for both). A reason about a key other than the change's own (a critical
    row's `_critical` alert) names it."""
    observed = _observed_map()
    keys, reason = _alert_keys(change)
    if reason is not None:
        return None, None, reason
    found = []
    for key in keys:
        kind, entry = observed_map_lib.lookup(observed, key)
        if entry is None:
            series, direction, reason = None, None, observed_map_lib.lookup_reason(kind, key)
        else:
            series, direction, reason = observed_map_lib.usable_for_backtest(entry)
        if reason is not None:
            return None, None, (reason if key == change["metric"] else f"{key}: {reason}")
        if (series, direction) not in found:
            found.append((series, direction))
    if not found:  # no non-twin row: `not_backtestable` says so first
        return None, None, "served-values gave no non-twin /metrics series"
    if len(found) > 1:
        return None, None, ("rows compared differently: "
                            + "; ".join(f"{s} ({d})" for s, d in found))
    return found[0][0], found[0][1], None


def not_backtestable(change):
    """Why a threshold change cannot be backtested, or None.

    #2119: a dimensioned key (`mysql_connections{db="a"}`) narrows its
    alert to some label values, which the per-tenant query asked does not,
    so it is listed in the report as not backtested. Building selectors for
    it is not done here.

    #2750: a change read from two trees (`extract_changes_from_dirs`)
    carries the /metrics series served-values reports for it, and is judged
    by those fields alone: dimensioned when a series has `dimensions` or
    `dimensions_regex`. A change from `--tenant` names a key
    as written, with no series; there the key's `{` is what marks it
    dimensioned.

    #3017: a served change with `dimensioned_siblings` (the alert's
    threshold is the max over them too) is not backtested. A change from
    `--tenant` cannot know its siblings — a known
    approximation, like `for:`.

    #3017 / #3002: then `alert_comparison`'s reason — a key whose alert the
    observed-map does not record as one per-tenant series compared one way
    is not backtested, rather than replayed against a guessed query.

    #1634 / #3111: a tenant id that cannot go into the PromQL selector as
    written (`tenant_is_queryable`) is not backtested either."""
    if not tenant_is_queryable(change["tenant"]):
        return ("tenant id holds a character a PromQL selector cannot take as written "
                '(`"`, `\\`, a control character, or bytes that are not UTF-8)')
    if "series" not in change:
        if "{" in change["metric"]:
            return "dimensioned key"
    else:
        # Fail closed: with no series to name the data (none at all, or only
        # a legacy twin, which is never read) nothing would be queried, and
        # `backtest_change` would report that as "no data".
        # `extract_changes_from_dirs` never yields such a change today.
        if all(s.legacy_twin for s in change["series"]):
            return "served-values gave no non-twin /metrics series"
        if any(s.dimensions or s.dimensions_regex for s in change["series"]):
            return "dimensioned key"
        # #3017: the alert's threshold is `max by(tenant)` over this row AND
        # the dimensioned rows of the same metric and severity, so replaying
        # the changed value alone would compare against the wrong number.
        if change.get("dimensioned_siblings"):
            return ("dimensioned rows share this alert's threshold (max by tenant): "
                    + ", ".join(change["dimensioned_siblings"]))
    return alert_comparison(change)[2]


def _not_backtested(change, reason):
    return {
        "tenant": change["tenant"],
        "metric": change["metric"],
        "old_value": change["old_value"],
        "new_value": change["new_value"],
        "status": "not_backtested",
        "risk": "UNKNOWN",
        "backtest": f"skipped: {reason}",
        "message": f"Not backtested: {reason}",
    }


# How much of a query error's text a `query_error` row carries (#3059).
_QUERY_ERROR_MAX = 300


def backtest_change(prom_url, change, lookback_seconds):
    """Backtest a single threshold change against historical data.

    Returns analysis dict with breach counts and risk assessment; `query` is
    the PromQL asked (#3002 B). A change `not_backtestable` refuses is
    returned as `not_backtested` without asking Prometheus anything; one
    whose query fails is `query_error`, its message the error text (#3059).
    """
    metric = change["metric"]
    tenant = change["tenant"]
    old_value = change["old_value"]
    new_value = change["new_value"]

    skip = not_backtestable(change)
    if skip:
        return _not_backtested(change, skip)
    # #3017 / #3002: ONE query — the series the alert compares, per tenant —
    # and the alert's own direction. It used to try three guessed names
    # (`<key>`, `tenant:<key>:max`, `<key>{namespace=}`), none of which is
    # what 31 of 64 alerts compare, and count every breach as `>`.
    series, direction, _ = alert_comparison(change)
    query = f'{series}{{tenant="{tenant}"}}'

    # #3059: a query Prometheus did not answer is `query_error`, not
    # `no_data` — the change was not measured, which `main` turns into rc 2.
    try:
        rows = query_range(prom_url, query, lookback_seconds)
    except PrometheusQueryError as exc:
        text = safe_label(str(exc))
        if len(text) > _QUERY_ERROR_MAX:
            text = text[:_QUERY_ERROR_MAX] + "..."
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "query_error",
            "risk": "UNKNOWN",
            "query": query,
            "message": f"Prometheus query failed: {text}",
        }
    values = []
    for row in rows:
        values.extend(row.get("values", []))

    if not values:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "no_data",
            "risk": "UNKNOWN",
            "query": query,
            "message": "No historical data found in Prometheus",
        }

    # #2963: a series that returned only NaN samples is not "analyzed" — every
    # comparison against it is false, so it would read as analyzed/LOW (or
    # MEDIUM for a disable) on no evidence at all. Same `no_data` verdict as an
    # empty result, with its own message so the two stay distinguishable.
    # There is no other query to fall back to: the alert compares this one.
    if all(_is_nan_sample(v) for _, v in values):
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "no_data",
            "risk": "UNKNOWN",
            "query": query,
            "message": "Only NaN samples in lookback window",
        }

    # The denominator deliberately KEEPS NaN samples (owner ruling on #2963,
    # option P): the percentages below mean "share of the whole lookback the
    # alert would fire", and at a NaN step `NaN > t` is false in PromQL, so the
    # alert really is silent there. Dropping NaN here would turn it into
    # "share of the time with data" and raise newly-enabled risk levels (and
    # with HIGH, the exit code) — a different metric, not a bug fix.
    # ±Inf stays too: it is a real sample that does fire.
    total_points = len(values)

    # #3017: the numbers as the exporter reads them (`70:critical` is 70 on
    # the critical row); `not_backtestable` already refused a value it cannot
    # read. None is a switched-off side: the disable / enable transitions.
    key = _written_key(change)
    old_t, new_t = (parse_threshold_value(key, v) for v in (old_value, new_value))
    old_t, new_t = (t and t[0] for t in (old_t, new_t))
    old_disabled, new_disabled = old_t is None, new_t is None

    if new_disabled and not old_disabled:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "query": query,
            "risk": "MEDIUM",
            "data_points": total_points,
            "old_breach_count": count_threshold_breaches(values, old_t, direction),
            "new_breach_count": 0,
            "impact_pct": -100.0,
            "message": "Metric disabled — all alerts silenced",
        }

    if old_disabled and not new_disabled:
        new_breaches = count_threshold_breaches(values, new_t, direction)
        pct = (new_breaches / total_points * 100) if total_points > 0 else 0
        risk = "HIGH" if pct > 10 else "MEDIUM" if pct > 0 else "LOW"
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "query": query,
            "risk": risk,
            "data_points": total_points,
            "old_breach_count": 0,
            "new_breach_count": new_breaches,
            "impact_pct": float("inf") if new_breaches > 0 else 0,
            "message": f"Metric newly enabled — {new_breaches}/{total_points} points would fire",
        }

    # Normal threshold change
    old_breaches = count_threshold_breaches(values, old_t, direction)
    new_breaches = count_threshold_breaches(values, new_t, direction)

    if old_breaches == 0 and new_breaches == 0:
        impact_pct = 0.0
        risk = "LOW"
        message = "No firing in lookback window under either threshold"
    elif old_breaches == 0:
        impact_pct = float("inf")
        risk = "HIGH"
        message = f"New threshold would START firing ({new_breaches} points)"
    else:
        impact_pct = ((new_breaches - old_breaches) / old_breaches) * 100
        abs_pct = abs(impact_pct)
        if abs_pct > RISK_THRESHOLDS["HIGH"]:
            risk = "HIGH"
        elif abs_pct > RISK_THRESHOLDS["MEDIUM"]:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        more = "more" if new_breaches > old_breaches else "fewer"
        message = f"{abs(new_breaches - old_breaches)} {more} firing points ({impact_pct:+.1f}%)"

    return {
        "tenant": tenant,
        "metric": metric,
        "old_value": old_value,
        "new_value": new_value,
        "status": "analyzed",
        "query": query,
        "risk": risk,
        "data_points": total_points,
        "old_breach_count": old_breaches,
        "new_breach_count": new_breaches,
        "impact_pct": round(impact_pct, 1) if impact_pct != float("inf") else "Inf",
        "message": message,
    }


def empty_report(lookback, status, reason):
    """Report envelope for a terminal path that ran no backtest (#1112).

    Same schema as :func:`generate_report` with every count zeroed, plus a
    ``status`` / ``reason`` discriminator.  A `--json` consumer reading
    ``.risk_summary.HIGH`` therefore works unchanged on the skip / no-change
    paths (it sees 0), and can branch on ``.status`` when it cares.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback": lookback,
        "status": status,
        "reason": reason,
        "total_changes": 0,
        "analyzed": 0,
        "no_data": 0,
        "query_error": 0,
        "risk_summary": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
        "changes": [],
    }


def generate_report(results, lookback):
    """Generate aggregate backtest report.

    #3059: when any change is `query_error` (not measured), the report also
    carries ``status: "partial"`` / ``reason: "query_error"``, so a consumer
    reading ``.status // "ok"`` does not take it for a complete run; a
    complete run carries no ``status``, as before.
    """
    analyzed = [r for r in results if r["status"] == "analyzed"]
    no_data = [r for r in results if r["status"] == "no_data"]
    query_error = [r for r in results if r["status"] == "query_error"]

    high_risk = [r for r in analyzed if r["risk"] == "HIGH"]
    medium_risk = [r for r in analyzed if r["risk"] == "MEDIUM"]
    low_risk = [r for r in analyzed if r["risk"] == "LOW"]

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback": lookback,
        "total_changes": len(results),
        "analyzed": len(analyzed),
        "no_data": len(no_data),
        "query_error": len(query_error),
        "risk_summary": {
            "HIGH": len(high_risk),
            "MEDIUM": len(medium_risk),
            "LOW": len(low_risk),
        },
        "changes": results,
    }
    if query_error:
        report["status"] = "partial"
        report["reason"] = "query_error"
    return report


def _not_backtested_counts(changes):
    """`"N (reason), ..."` for the changes listed but not backtested, or ""."""
    reasons = {}
    for c in changes:
        if c.get("status") == "not_backtested":
            reason = c["backtest"].removeprefix("skipped: ")
            reasons[reason] = reasons.get(reason, 0) + 1
    return ", ".join(f"{n} ({r})" for r, n in reasons.items())


def _shown_value(change, side, none):
    """A side's value as the reports show it: `none` without one."""
    value = change[f"{side}_value"]
    return none if value is None else str(value)


def _served_by(change, shown):
    """` (served by: a, b)` when the keys serving a row (#3111) are not just
    the one its `metric` names; else ""."""
    keys = change.get("keys")
    if not keys or keys == [change["metric"]]:
        return ""
    return " (served by: " + ", ".join(shown(k) for k in keys) + ")"


def print_text_report(report):
    """Print human-readable backtest report."""
    print()
    print("=" * 60)
    print("  Threshold Backtest Report")
    print(f"  Lookback: {report['lookback']}")
    print("=" * 60)
    print()

    rs = report["risk_summary"]
    print(f"  Changes analyzed: {report['analyzed']}/{report['total_changes']}")
    print(f"  Risk: {rs['HIGH']} HIGH, {rs['MEDIUM']} MEDIUM, {rs['LOW']} LOW")
    if report["no_data"] > 0:
        print(f"  No data: {report['no_data']} (no usable data in Prometheus)")
    if report["query_error"] > 0:
        print(f"  Query error: {report['query_error']} (the Prometheus query failed; "
              "not measured)")
    skipped = _not_backtested_counts(report["changes"])
    if skipped:
        print(f"  Not backtested: {skipped}")
    print()

    for change in report["changes"]:
        risk = change["risk"]
        marker = "!!!" if risk == "HIGH" else " ! " if risk == "MEDIUM" else "   "
        old_v, new_v = (_shown_value(change, side, "(none)") for side in ("old", "new"))
        window = f" ({', '.join(change['window'])} UTC)" if change.get("window") else ""
        print(f"  {marker} [{risk:6s}] {safe_label(change['tenant'])}/"
              f"{safe_label(change['metric'])}: "
              f"{old_v} -> {new_v}{window}{_served_by(change, safe_label)}")
        print(f"           {safe_label(change['message'])}")
        if change.get("query"):  # #3002 B: the PromQL this verdict rests on
            print(f"           query: {safe_label(change['query'])}")

    print()


def _markdown_cell(text):
    """A string from a tree (a tenant id, a key) inside one Markdown table
    cell: escaped (`safe_label`), its `|` too, so the row keeps its columns."""
    return safe_label(text).replace("|", "\\|")


def _code_cell(text):
    """A string from a tree or built from one (a key, a query holding a
    tenant id) as inline code in one Markdown table cell: `_markdown_cell`'s
    escaping, inside a backtick fence longer than any backtick run in it
    (padded when it starts or ends with one), so neither the code span nor
    the row can be closed early."""
    text = _markdown_cell(text)
    fence = "`" * (max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def generate_markdown(report):
    """Generate Markdown suitable for a PR comment."""
    lines = []
    lines.append("## Threshold Backtest Results")
    lines.append("")
    lines.append(f"**Lookback:** {report['lookback']} | "
                 f"**Analyzed:** {report['analyzed']}/{report['total_changes']}")
    skipped = _not_backtested_counts(report["changes"])
    if skipped:
        lines.append(f"\n**Not backtested:** {skipped}")

    if report["query_error"] > 0:
        lines.append(f"\n> **{report['query_error']} change(s) not measured: "
                     "the Prometheus query failed.**")

    rs = report["risk_summary"]
    if rs["HIGH"] > 0:
        lines.append(f"\n> **{rs['HIGH']} HIGH risk change(s) detected.**")

    lines.append("")
    lines.append("| Risk | Tenant | Metric | Old | New | Impact | Query |")
    lines.append("|------|--------|--------|-----|-----|--------|-------|")

    for c in sorted(report["changes"], key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}.get(x["risk"], 9)):
        old_v, new_v = (_shown_value(c, side, "—").replace("|", "\\|") for side in ("old", "new"))
        window = f" ({', '.join(c['window'])} UTC)" if c.get("window") else ""
        # #3002 B: the PromQL the verdict rests on; "—" when none was asked.
        query = _code_cell(c["query"]) if c.get("query") else "—"
        # #3059: a `query_error` message carries Prometheus's own text, which
        # may hold `|` (a PromQL parse error quoting the query) — escaped so
        # it stays one cell.
        message = c["message"].replace("|", "\\|")
        served_by = _served_by(c, _code_cell)
        lines.append(f"| {c['risk']} | {_markdown_cell(c['tenant'])} | "
                     f"{_code_cell(c['metric'])}{window}{served_by} | "
                     f"{old_v} | {new_v} | {message} | {query} |")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by `backtest_threshold.py`*")

    return "\n".join(lines)


def _exit_unreadable_conf(exc, args):
    """#1654: a conf.d file whose CONTENT cannot be read → rc 2, file named.

    The content axis of #1911 (the name axis is #1634): a pure-ASCII
    ``db-a.yaml`` with one ``\\xff`` byte used to escape ``load_conf_files``
    as a ``UnicodeDecodeError`` traceback — rc 1, and 0 bytes of stdout under
    ``--json``. Not the shared ``exit_on_yaml_file_error`` wrapper, because
    this tool owes ``--json`` its ``caller_error`` envelope on every terminal
    path.
    """
    print(f"ERROR: cannot read {safe_label(exc)}", file=sys.stderr)  # #1538
    _end_not_run(args, "caller_error", "conf_file_unreadable", EXIT_CALLER_ERROR)


def _exit_served_values_error(exc, args, recipe_tenants=()):
    """#2119: da-guard missing, stale or failing, or a file of either tree
    the exporter cannot decode or read → rc 2, named (`print_load_error`, the
    lines `exit_on_served_values_error` prints), never a partial diff. Not
    that decorator, because this tool owes ``--json`` its ``caller_error``
    envelope on every terminal path.
    """
    print_load_error(exc)
    _end_not_run(args, "caller_error", "served_values_unavailable", EXIT_CALLER_ERROR,
                 recipe_tenants)


def _exit_observed_map_error(exc, args, recipe_tenants=()):
    """#3017: the observed-map is missing, unreadable or empty → rc 2, the
    line `exit_on_observed_map_error` prints, plus the `--json`
    ``caller_error`` envelope this tool owes on every terminal path (that
    decorator stays on `main` as the backstop for any other load)."""
    print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)  # #1538
    _end_not_run(args, "caller_error", "observed_map_unavailable", EXIT_CALLER_ERROR,
                 recipe_tenants)


# #3089: why the PR comment has no table, for a path that ran no backtest.
# A `caller_error` names its reason; the ERROR line is in the job log.
_NOT_RUN_WHY = {
    ("skipped", "prometheus_unavailable"):
        "Prometheus is unavailable (`--skip-if-unavailable`).",
    ("no_changes", "no_threshold_changes_detected"): "no threshold changes found.",
}


def not_run_markdown(report):
    """#3089: the `--markdown-output` of a path that ran no backtest — never
    empty, so a PR comment built from it says that nothing was measured and
    why, instead of an older run's comment staying up."""
    why = _NOT_RUN_WHY.get((report["status"], report["reason"]))
    if why is None:
        why = f"caller error `{report['reason']}` (see the job log)."
    return ("## Threshold Backtest Results\n"
            "\n"
            f"> **Backtest not run:** {why}\n"
            "\n"
            "---\n"
            "*Generated by `backtest_threshold.py`*")


def _unwritable_output(path, flag):
    """#3089: the `OutputWriteError` writing *path* would meet — its parent
    is missing or not a writable directory, or it is a directory or a file
    that cannot be written — else None.

    Asked before any work, so a run that could not keep its result stops
    before printing one (`--json` would otherwise show a full report next to
    rc 2). Passing is not a promise: the write can still fail (a race, or
    uid 0, for whom the permission bits decide nothing; a read-only file
    system reads here as EACCES, where the write meets EROFS), and
    `write_*_or_die` still answers that. An existing path that is not a
    directory is not required to be a regular file (`-o /dev/null`).

    The same judgement the secure writer makes (#3105): a symlink is
    followed to where it ends (a dangling one is judged by its target's
    parent), and an existing regular file someone else owns is unwritable —
    the writer's `chmod 0600` would fail on it (EPERM) after writing."""
    p = Path(os.path.realpath(path))
    if p.is_dir():
        code = errno.EISDIR
    elif p.exists():
        st = p.stat()
        if not os.access(p, os.W_OK):
            code = errno.EACCES
        elif (stat.S_ISREG(st.st_mode) and hasattr(os, "geteuid")
              and os.geteuid() != 0 and st.st_uid != os.geteuid()):
            code = errno.EPERM
        else:
            code = None
    elif not p.parent.exists():
        code = errno.ENOENT
    elif not p.parent.is_dir():
        code = errno.ENOTDIR
    else:
        code = None if os.access(p.parent, os.W_OK | os.X_OK) else errno.EACCES
    if code is None:
        return None
    return OutputWriteError(path, OSError(code, os.strerror(code), path), flag=flag)


def _preflight_outputs(args):
    """#3089 (b): `-o` / `--markdown-output` that cannot be written → the
    line `write_*_or_die` prints, the `output_unwritable` envelope, rc 2."""
    for value, flag in ((args.output, "-o/--output"),
                        (args.markdown_output, "--markdown-output")):
        exc = _unwritable_output(value, flag) if value else None
        if exc is not None:
            print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)  # #1538
            if args.json:
                print(format_json_report(empty_report(
                    args.lookback, "caller_error", "output_unwritable")))
            sys.exit(EXIT_CALLER_ERROR)


def _write_outputs(args, report, markdown, recipe_tenants):
    """#3089 (a): the files the caller asked for, on every path after the
    preflight — `-o` is the document `--json` prints, `--markdown-output`
    is *markdown* plus the recipe block when a tenant declares recipes.
    #1641: an unwritable path is rc 2 + one line naming the flag, not a
    traceback at rc 1 (which here would read as "HIGH risk found")."""
    if args.output:
        write_json_or_die(args.output, report, flag="-o/--output")
        if not args.json:
            print(f"  JSON report: {args.output}")
    if args.markdown_output:
        if recipe_tenants:
            markdown += "\n\n" + custom_alert_markdown(recipe_tenants)
        write_text_or_die(args.markdown_output, markdown, flag="--markdown-output")
        if not args.json:
            print(f"  Markdown report: {args.markdown_output}")


def _end_not_run(args, status, reason, rc, recipe_tenants=()):
    """A path that ran no backtest: its envelope on stdout under `--json`,
    the same envelope in `-o`, why in `--markdown-output`, then exit *rc*."""
    report = empty_report(args.lookback, status, reason)
    if args.json:
        print(format_json_report(report))
        disarm_crash_envelope()  # #3094: the document is out; the writes below must not add one
    _write_outputs(args, report, not_run_markdown(report), recipe_tenants)
    sys.exit(rc)


# #3111: what an old `--git-diff` invocation is told. The tool no longer
# checks HEAD~1 out itself — every way it tried re-derived part of git's
# checkout and got some of it wrong — so the caller makes the base checkout.
GIT_DIFF_REMOVED = (
    "ERROR: --git-diff was removed (#3111): check the base out yourself and compare "
    "the two trees, e.g. `git worktree add /tmp/base HEAD~1` then `--config-dir conf.d "
    "--baseline /tmp/base/<path from the repo root>/conf.d` (in CI, check the PR base "
    "out into a separate path)")


@observed_map_lib.exit_on_observed_map_error  # #3017: no usable observed-map → rc 2, named
def main():
    """CLI entry point: Backtest threshold changes against historical Prometheus data."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Backtest threshold changes against historical Prometheus data",
    )

    # Change source (mutually exclusive)
    source = parser.add_mutually_exclusive_group()
    # #3111: removed. Still PARSED (hidden from --help, as init_project's
    # retired GitOps flags are) only so that an old invocation gets the
    # refusal naming its replacement instead of argparse's bare
    # "unrecognized arguments".
    source.add_argument("--git-diff", action="store_true", help=argparse.SUPPRESS)
    source.add_argument(
        "--config-dir",
        help="Current config directory (requires --baseline)",
    )
    source.add_argument(
        "--tenant",
        help="Single tenant (requires --metric and at least one of --old-value/--new-value)",
    )

    parser.add_argument("--baseline", help="Baseline config directory (with --config-dir)")
    parser.add_argument("--metric", help="Metric key (with --tenant)")
    parser.add_argument("--old-value", help="Old threshold value (with --tenant)")
    parser.add_argument("--new-value", help="New threshold value (with --tenant)")

    add_prometheus_arg(
        parser,
        help_text="Prometheus Query API URL "
                  "(default: $PROMETHEUS_URL, else http://localhost:9090)",
    )
    # #1625: `type=lookback_arg` — a value that is supplied but does not match
    # `<number><d|h|m>` is a caller error (exit 2 with the value named), not a
    # silent 7-day fallback. Measured before the fix: `--lookback 7`,
    # `--lookback banana` and `--lookback ''` all ran a 604800 s window, rc 0.
    parser.add_argument(
        "--lookback", default=DEFAULT_LOOKBACK, type=lookback_arg,
        help="Historical lookback window as <number><d|h|m>, e.g. 7d / 24h / 30m "
             f"(default: {DEFAULT_LOOKBACK}); any other shape exits 2",
    )
    parser.add_argument(
        "--skip-if-unavailable", action="store_true",
        help="Exit 0 if Prometheus is unreachable (argument and output-path errors still exit 2)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON only",
    )
    parser.add_argument(
        "--markdown-output",
        help="Write Markdown report to file (for PR comments)",
    )
    args = parser.parse_args()
    if args.json:
        # #3094: a crash in --json mode still prints one envelope
        arm_crash_envelope(build=lambda: empty_report(
            args.lookback, "caller_error", "internal_error"))

    # #3059: argument errors first — before the Prometheus check, so
    # `--skip-if-unavailable` cannot turn them into a green skip — and with
    # the `--json` envelope (argparse's own usage errors still print none).
    if args.git_diff:
        print(GIT_DIFF_REMOVED, file=sys.stderr)
        if args.json:
            print(format_json_report(empty_report(
                args.lookback, "caller_error", "git_diff_removed")))
        sys.exit(EXIT_CALLER_ERROR)
    if args.tenant:
        if not args.metric or (args.old_value is None and args.new_value is None):
            print("ERROR: --tenant requires --metric and at least one of --old-value/--new-value",
                  file=sys.stderr)
            if args.json:
                print(format_json_report(empty_report(
                    args.lookback, "caller_error", "tenant_args_missing")))
            sys.exit(EXIT_CALLER_ERROR)
        # #3066: the id goes into `{tenant="..."}` as written; one the tenant
        # id rule refuses (`a"b`, `a\b`) would make the query invalid PromQL.
        if not is_valid_tenant_id(args.tenant):
            print(f"ERROR: --tenant '{safe_label(args.tenant)}' is not a valid tenant id. "
                  f"{tenant_id_rule()[1]}", file=sys.stderr)
            if args.json:
                print(format_json_report(empty_report(
                    args.lookback, "caller_error", "invalid_tenant_id")))
            sys.exit(EXIT_CALLER_ERROR)
    elif not args.config_dir:
        print("ERROR: Specify --config-dir (with --baseline) or --tenant", file=sys.stderr)
        if args.json:
            print(format_json_report(empty_report(
                args.lookback, "caller_error", "no_change_source")))
        sys.exit(EXIT_CALLER_ERROR)

    # #3089 (b): after the argument checks, before any served-values,
    # observed-map or Prometheus work — every path below writes the outputs.
    _preflight_outputs(args)

    # Surface custom-alert recipes (parsed from disk; Prometheus-independent),
    # BEFORE the availability gate — a recipe-only change must not pass
    # silently just because the flat backtest has nothing to do or Prometheus
    # is unreachable. Recipes use the compiler+promtool eval home, not this
    # flat-threshold tool. (#657)
    if args.config_dir:
        # #3111: the whole tree, as the exporter walks it (`list_config_tree`:
        # sub-directories included, hidden entries and other extensions
        # left out), so a recipe tenant declared in a nested file is named
        # too (#3155). One file at a time: two nested files may share a stem.
        try:
            parsed = [load_conf_files([str(p)])
                      for p in list_config_tree(args.config_dir).files]
        except YamlFileError as exc:
            _exit_unreadable_conf(exc, args)
        recipe_tenants = sorted({t for one in parsed for t in find_custom_alert_tenants(one)})
    else:
        recipe_tenants = []
    if recipe_tenants:
        print(custom_alert_notice(recipe_tenants), file=sys.stderr)

    dir_changes = None
    # #2119: the two trees are read by da-guard, and this runs BEFORE the
    # Prometheus check: `--skip-if-unavailable` is about Prometheus, and must
    # not turn "a tree the exporter refuses" into a green skip.
    if args.config_dir:
        if not args.baseline:
            print("ERROR: --config-dir requires --baseline", file=sys.stderr)
            _end_not_run(args, "caller_error", "baseline_missing", EXIT_CALLER_ERROR,
                         recipe_tenants)
        try:
            dir_changes = extract_changes_from_dirs(args.config_dir, args.baseline)
        except (DaGuardNotFoundError, DaGuardError, YamlFileError) as exc:
            _exit_served_values_error(exc, args, recipe_tenants)

    # #3017: the observed-map says what every alert compares; without it no
    # change can be backtested, so an unusable map is rc 2 — and BEFORE the
    # Prometheus check, for the reason the two checks above are: a
    # `--skip-if-unavailable` green must not hide it.
    try:
        _observed_map()
    except observed_map_lib.ObservedMapError as exc:
        _exit_observed_map_error(exc, args, recipe_tenants)

    # Check Prometheus availability
    if not prometheus_available(args.prometheus):
        if args.skip_if_unavailable:
            print("Prometheus unavailable — skipping backtest (--skip-if-unavailable)",
                  file=sys.stderr)
            _end_not_run(args, "skipped", "prometheus_unavailable", EXIT_OK, recipe_tenants)
        else:
            print(f"ERROR: Prometheus not reachable at {args.prometheus}", file=sys.stderr)
            print("Use --skip-if-unavailable to exit gracefully", file=sys.stderr)
            _end_not_run(args, "caller_error", "prometheus_unavailable", EXIT_CALLER_ERROR,
                         recipe_tenants)

    # Extract changes
    if args.config_dir:
        changes = dir_changes
    else:  # `--tenant`: the no-source case already exited above
        changes = written_changes(args.tenant, args.metric, args.old_value, args.new_value)

    if not changes:
        print("No threshold changes found.", file=sys.stderr)
        _end_not_run(args, "no_changes", "no_threshold_changes_detected", EXIT_OK,
                     recipe_tenants)

    # Run backtests
    lookback_seconds = parse_lookback(args.lookback)
    results = []
    for change in changes:
        # `not_backtestable` is asked inside: a change it refuses is listed
        # as not backtested and Prometheus is not asked about it.
        result = backtest_change(args.prometheus, change, lookback_seconds)
        if change.get("window"):  # #2119: the part of the day the values hold in
            result["window"] = change["window"]
        if "keys" in change:  # #3111: the keys serving the /metrics row
            result["keys"] = change["keys"]
        results.append(result)

    # Generate report
    report = generate_report(results, args.lookback)

    # Output
    if args.json:
        print(format_json_report(report))
        disarm_crash_envelope()  # #3094: the document is out; the writes below must not add one
    else:
        print_text_report(report)

    _write_outputs(args, report, generate_markdown(report), recipe_tenants)

    # Exit with non-zero if HIGH risk changes found
    high_count = report["risk_summary"]["HIGH"]
    if high_count > 0 and not args.json:
        print(f"\n  WARNING: {high_count} HIGH risk change(s) — review before merging.")
    # #3059: a change not measured outranks HIGH — with one left unmeasured
    # the run cannot say "only these are high risk". Prometheus answered, so
    # this is not `--skip-if-unavailable`'s case either.
    if report["query_error"] > 0:
        print(f"ERROR: {report['query_error']} change(s) not measured: the Prometheus "
              "query failed (see each change's message)", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_VIOLATION if high_count > 0 else EXIT_OK)


if __name__ == "__main__":
    main()
