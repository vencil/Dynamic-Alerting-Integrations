#!/usr/bin/env python3
"""backtest_threshold.py — Backtest threshold changes against historical Prometheus data.

Given a set of threshold changes (from git diff or manual input), queries
Prometheus range data to simulate how alert firing counts would change under
old vs new thresholds. Produces a risk assessment report suitable for PR review.

Each change is replayed against the one series its alert compares, the way
the alert compares it (`>` / `<`), as metric_observed_map.yaml records it
(#3017 / #3002); a key the map does not record that way is listed as not
backtested.

Usage:
  # From git diff (CI mode)
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090

  # From config directory
  python3 backtest_threshold.py --config-dir conf.d/ --baseline conf.d.bak/ \
    --prometheus http://localhost:9090

  # Manual single metric
  python3 backtest_threshold.py --tenant db-a --metric mysql_connections \
    --old-value 70 --new-value 50 --prometheus http://localhost:9090

  # Skip if Prometheus unavailable (CI-friendly)
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090 \
    --skip-if-unavailable

  # JSON + Markdown output for PR comment
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090 \
    --json --markdown-output /tmp/backtest-comment.md

需求:
  - Prometheus Query API reachable (or --skip-if-unavailable)
  - git available (for --git-diff mode)
"""
import argparse
import errno
import math
import os
import re
import stat
import subprocess
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
# #2231: a conf.d file holding a key twice is one the exporter drops whole;
# strict reads raise YamlFileError for it, the path bad syntax already takes.
# ADR-036 step 4 (#2906): conf.d content is read through `da-guard
# parse-tree` (the working tree, and a `git show` of HEAD~1 in its file
# mode) — yaml.v3 is the only parser of conf.d; a file it cannot read raises
# YamlFileError (the working tree) or is named (HEAD~1).
from _lib_io import load_confd_file  # noqa: E402
import _lib_parse_cache  # noqa: E402
from _lib_parse_tree import FileUnreadable, ParseTreeError  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR,
    arm_crash_envelope, disarm_crash_envelope,
)
from _lib_validation import is_valid_tenant_id, tenant_id_rule  # noqa: E402  (#3066)
from _lib_validation import GO_SPACE, THRESHOLD_SEVERITIES, split_threshold_severity  # noqa: E402  (#3071)
# #2119: `--config-dir` / `--baseline` are read by the exporter, not here.
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
# observed-map, never guessed; `--git-diff` / `--tenant` keys are as written, so
# a retired spelling goes through the alias table first.
import _observed_map_lib as observed_map_lib  # noqa: E402
from _registry_lib import DEPRECATED_KEY_ALIASES  # noqa: E402
from _lib_confd import (  # noqa: E402
    config_stem,
    has_yaml_extension,
    is_hidden_name,
    is_reserved_name,
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


# Paths this tool could not parse, so each is named once per run rather than
# once per diff line (same reason as `_WARNED_UNLISTABLE` below: a warning
# repeated per line trains the operator to skip it).
_WARNED_DROPPED_CARRIER: set = set()


def _fsdecode(raw):
    r"""Decode a path git handed us the way `os.listdir` decodes one.

    `surrogateescape` is not decoration. A conf.d entry name is not required
    to be valid UTF-8, and `os.listdir` answers `'legacy-\udcff.yaml'` for one
    that is not (measured). Decoding git's answer the same way is what lets a
    name that came from the TREE compare equal to a name that came from the
    DIRECTORY -- and it round-trips back into git's argv, because `subprocess`
    encodes argv with `os.fsencode` (measured: `git show` on the surrogate
    form of that name returns rc=0, for all three of an ASCII, a non-ASCII
    and an invalid-UTF-8 carrier).
    """
    return raw.decode(sys.getfilesystemencoding(), "surrogateescape")


def _git_bytes(args, quotepath_off=False):
    r"""Run git and return raw stdout BYTES, or None if the call failed.

    ⛔ #1634. Every path-carrying git call in this file used `text=True`, and
    the fix has TWO halves that do not substitute for each other. Both
    directions measured on one fixture:

      * bytes alone does not undo quoting. `core.quotepath` defaults to TRUE,
        so `git diff --name-only` answers `b'"conf.d/legacy-\377.yaml"'` --
        the octal escape is in the bytes too, and the extension test fails on
        the trailing quote exactly as it did on the str.
      * turning quoting off alone does not survive `text=True`: git then emits
        the real bytes and strict UTF-8 decoding raises `UnicodeDecodeError`
        from INSIDE `subprocess.run`, which is not in this file's
        `except (TimeoutExpired, FileNotFoundError)`. That is #1634's dead
        end B in a different spelling -- a case that WORKED becomes a crash.

    ⚠️ `-z` and `quotepath_off` are not interchangeable either: `-z` suppresses
    quoting for `--name-only` and `ls-tree`, and has NO effect on the `+++`
    header of a unified diff (measured -- with and without `-z` that header is
    byte-identical). Each caller below says which one it needs and why.
    """
    cmd = ["git"]
    if quotepath_off:
        cmd += ["-c", "core.quotepath=false"]
    try:
        result = subprocess.run(cmd + list(args), capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _warn_dropped_carrier(shown, reason):
    r"""Say once that a carrier was dropped, and why.

    ⛔ The charge in #1634 is that a REMOVED threshold vanished silently. The
    shapes below still vanish -- they are not all fixable here -- but they no
    longer do it quietly, which is the whole difference the family turns on.

    ⚠️ `shown` may hold a lone surrogate (a name that is not valid UTF-8,
    decoded by `_fsdecode`). Printing it needs no escaping of our own: the
    first version of this function ran it through `backslashreplace` first,
    on the stated grounds that "printing a surrogate raises
    UnicodeEncodeError, so the warning would itself be the crash". ⛔ That
    was reasoned, not measured, and it is FALSE -- Python gives `sys.stderr`
    `errors='backslashreplace'`, so the surrogate prints as `\udcff` on its
    own. Measured, both defaults and `PYTHONIOENCODING=utf-8:strict`:

        print('legacy-\udcff', file=sys.stderr)   rc=0, prints legacy-\udcff

    The helper bought nothing, had no guard of its own, and its docstring
    asserted a crash that does not happen -- exactly the shape this whole
    change exists to remove. It was deleted rather than re-justified.
    """
    if shown in _WARNED_DROPPED_CARRIER:
        return
    _WARNED_DROPPED_CARRIER.add(shown)
    print(f"WARNING: dropped {shown} ({reason}); its threshold changes are NOT "
          f"in this report", file=sys.stderr)


def reset_dropped_carrier_warnings_for_test() -> None:
    """Clear the once-per-run dedup so a test can drive the branch twice."""
    _WARNED_DROPPED_CARRIER.clear()


def tenant_is_queryable(tenant):
    r"""Whether `tenant` survives the trip into a PromQL query string.

    ⛔ #1634, and this one is load-bearing for the fix above rather than
    optional. A conf.d carrier name is not required to be valid UTF-8, and
    every path in this file now decodes with `surrogateescape` so the name
    round-trips back into git -- which is exactly what makes such a carrier
    REACHABLE for the first time. A PromQL label value cannot make the same
    trip: it goes through `urllib.parse.urlencode`, which encodes strict
    UTF-8 and raises `UnicodeEncodeError` on a lone surrogate.

    Measured, one fixture, carrier `legacy-\xff.yaml`:

      --git-diff    on 05be065b : silent "No threshold changes found."
                    with only the sites above fixed : UnicodeEncodeError
      --config-dir  on 05be065b : UnicodeEncodeError  (pre-existing, its
                    tenant ids come straight from `os.listdir`)

    ⚠️ The crash is raised AFTER the Prometheus reachability check, so
    `--skip-if-unavailable` does not contain it, and rc=1 is indistinguishable
    from `EXIT_VIOLATION`.

    ⛔ Filtering at ONE point, for all three change sources, is deliberate.
    Guarding only the `--git-diff` path would leave the two enumerators
    answering differently about the same carrier -- which is the #1911 shape
    this whole chain exists to remove, and which this chain has already
    produced three times by fixing one path of a pair. The `--config-dir`
    half therefore changes behaviour too: crash becomes a named skip. That is
    a deliberate, named change, not a drive-by.
    """
    try:
        tenant.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def extract_changes_from_git_diff():
    """Parse git diff of conf.d/ to find threshold changes.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...],
    or ``None`` when git itself could not answer (not on PATH, not inside a
    work tree, no ``HEAD~1``, timeout).

    ⛔ #1556 item 4 (D-06): ``None`` from ``_git_bytes`` used to be folded into
    ``[]`` here, so "git is unusable" and "the diff is empty" were the same
    value and ``main()`` printed "No threshold changes found." + rc 0 for
    both. The da-tools image installs no git and its WORKDIR is not a repo,
    so `--git-diff` there ALWAYS looked like "your PR changed nothing".
    ``b""`` (git ran, diff empty) still parses to ``[]``; only ``None`` is
    passed through.
    """
    # ⚠️ `quotepath_off`, NOT `-z`: `-z` changes nothing in a unified diff's
    # `+++` header. Parsing then stays on bytes all the way down -- see
    # `_git_bytes` for why those two are inseparable.
    raw = _git_bytes(["diff", "HEAD~1", "--unified=0", "--", "conf.d/"],
                     quotepath_off=True)
    if raw is None:
        return None

    changes = []
    current_file = None

    for line in raw.splitlines():
        # Track current file
        if line.startswith(b'+++ "'):
            # ⛔ #1634 decision A1: with quoting off, git still quotes a path
            # holding a newline, a double quote or a backslash. Un-escaping
            # those is a parser of its own, and for a NEWLINE it cannot work
            # here at all -- the `+++` header is split across two lines, so a
            # line-oriented scan has already lost it. Left unfixed ON PURPOSE
            # and named in the ticket; what changes is that it is now audible.
            _warn_dropped_carrier(_fsdecode(line[4:]),
                                  "git quoted it: the name holds a newline, a "
                                  "quote or a backslash")
            current_file = None
            continue
        if line.startswith(b"+++ b/"):
            fname = _fsdecode(line[6:])
            if fname.endswith("\t"):
                # ⛔ Also #1634 and also NOT fixed: a diff header pads a path
                # containing a space with a trailing TAB, which this slice
                # takes to be part of the name. Stripping it would start
                # admitting space-named carriers -- a behaviour change on a
                # different axis, so it is named rather than smuggled in.
                _warn_dropped_carrier(fname.rstrip("\t"),
                                      "the diff header padded a space in the "
                                      "name with a tab")
                current_file = None
                continue
            # Extract tenant from filename (conf.d/db-a.yaml → db-a)
            basename = Path(fname).name
            # #1588 site 1 of 6 in this file (the first version counted 4 and the
            # two it missed were `_flat_keys_at_head1` — which was still
            # broken — and `load_conf_files`, which needed no change). The
            # extension set is now the shared default (#1603): omitting the
            # argument takes `CONFIG_SUFFIXES`, which is the exporter's own
            # set, so a `conf.d/db-b.yml` the exporter is serving stops being
            # invisible to this diff parser. Hand-slicing
            # the stem is what `config_stem` exists to stop: `str.lower()`
            # can shrink byte length, so an offset taken from the folded
            # copy cuts the original in the wrong place.
            # ⚠️ `config_stem` also declines `.`-prefixed names, so a diff
            # touching `conf.d/.foo.yaml` no longer yields a tenant called
            # `.foo`. The exporter's scanner skips hidden entries, so that
            # agrees with the oracle — but it is a second-order change and
            # is recorded here rather than left for someone to discover.
            if has_yaml_extension(basename) \
                    and not is_reserved_name(basename):
                # `config_stem` answers "" for a hidden name and the
                # `if not current_file` below already treats that as
                # "no tenant", so an `or None` here would be a third
                # spelling of the same decision. Mutation confirms the
                # two forms are equivalent.
                current_file = config_stem(basename)
            else:
                current_file = None
            continue

        if not current_file:
            continue

        # Parse YAML key: value changes
        # Lines starting with - (removed) or + (added) in diff
        # ⚠️ BYTES patterns, because the whole scan is on bytes now (#1634).
        # `\w` in bytes mode matches ASCII only -- which is what a metric key
        # already is, so the class does not narrow in practice (measured: the
        # same three hunk lines match, key=b'cpu_usage' val=b'80').
        old_match = re.match(rb"^-\s+(\w+):\s+(.+)$", line)
        new_match = re.match(rb"^\+\s+(\w+):\s+(.+)$", line)

        if old_match:
            metric = _fsdecode(old_match.group(1))
            old_val = _fsdecode(old_match.group(2)).strip().strip("'\"")
            # Look for corresponding + line
            changes.append({
                "tenant": current_file,
                "metric": metric,
                "old_value": old_val,
                "new_value": None,  # will be filled by + line
            })
        elif new_match:
            metric = _fsdecode(new_match.group(1))
            new_val = _fsdecode(new_match.group(2)).strip().strip("'\"")
            # Try to match with previous - entry
            matched = False
            for c in reversed(changes):
                if c["tenant"] == current_file and c["metric"] == metric and c["new_value"] is None:
                    c["new_value"] = new_val
                    matched = True
                    break
            if not matched:
                changes.append({
                    "tenant": current_file,
                    "metric": metric,
                    "old_value": None,
                    "new_value": new_val,
                })

    # Filter out entries where nothing actually changed
    return [c for c in changes if c["old_value"] != c["new_value"]
            and not c["metric"].startswith("_")]


# Roots already named as unlistable, so the warning is said once per run.
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


def _served_day(tenant_values, key, side):
    """`[(start_minute, end_minute, (rendered value, severity), series)]` for
    one key's whole UTC day as /metrics serves it (`schedules`; `series` the
    segment's /metrics series, #2750); a key or tenant /metrics never serves
    is one segment of None. The severity is part of what is compared
    (#3060): it decides which alert, if any, compares the row. A segment in
    which /metrics cannot be gathered at all is refused: a diff across it
    would be a guess."""
    if tenant_values is None or key not in (tenant_values.schedules or {}):
        return [(0, _DAY_MINUTES, None, None)]
    out = []
    for seg in tenant_values.schedules[key].segments:
        if seg.error is not None:
            raise ServedValuesError(
                f"da-guard served-values: {side} /metrics cannot be gathered "
                f"{seg.start}-{seg.end} UTC ({seg.error})", None, "")
        served = None if seg.value is None else (_render_served(seg.value), seg.severity)
        out.append((_minute(seg.start), _minute(seg.end), served, seg.series))
    return out


def _value_in(day, minute):
    for start, end, value, _series in day:
        if start <= minute < end:
            return value
    return None


def _series_in(days, spans):
    """Every /metrics series (`Series`, #2750) the key is served as on
    either side in any of `spans`, in day order, each once."""
    out = []
    for day in days:
        for start, end in spans:
            for s, e, _v, series in day:
                if s < end and start < e:
                    out.extend(x for x in series or () if x not in out)
    return out


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


def _change_pair(old, new):
    """What groups the differing runs of one key into changes (#3060): the
    served values and, per side, whether a threshold rule selects its
    severity — so a severity boundary between two compared severities does
    not split a value change in two. With equal values the severities
    themselves are what changed, and they are part of the pair."""
    (old_v, old_s), (new_v, new_s) = (side or (None, None) for side in (old, new))
    pair = (old_v, new_v, old_s in _COMPARED_SEVERITIES, new_s in _COMPARED_SEVERITIES)
    return pair + ((old_s, new_s) if old_v == new_v else ())


def _joined(spans):
    """`spans` (day order) with the ones that touch joined: a severity
    boundary inside one change's part of the day is not a new part of it.
    Not joined across midnight, as `_differing_runs`."""
    out = []
    for start, end in spans:
        if out and out[-1][1] == start:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _severity_list(severities):
    """One side's severities as `old_severity` / `new_severity` carry them:
    the one, or several "/"-joined with an empty one spelled `""`."""
    if len(severities) == 1:
        return severities[0]
    return "/".join(s or '""' for s in severities)


def _severities_in(day, spans):
    """The severities a side is served with in `spans`, in day order, each
    once ("" included; a part of the day with no row adds none)."""
    out = []
    for start, end in spans:
        for s, e, served, _series in day:
            if served is not None and s < end and start < e and served[1] not in out:
                out.append(served[1])
    return out


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


def _dimensioned_rows(days):
    """`{(metric_key, severity): {key, ...}}`: the dimensioned keys a tenant
    serves on either side at any time of the day (legacy twins aside)."""
    out = {}
    for key, pair in days.items():
        for day in pair:
            for _s, _e, _v, series in day:
                for s in series or ():
                    if (s.dimensions or s.dimensions_regex) and not s.legacy_twin:
                        out.setdefault(_row_identity(s), set()).add(key)
    return out


def extract_changes_from_dirs(config_dir, baseline_dir):
    """Threshold changes between two conf.d trees, as /metrics serves them.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...],
    plus `window` (a list of `"HH:MM-HH:MM"`, UTC) on a change that holds
    in part of the day only.

    #2119: both trees are read by the exporter (`da-guard served-values`),
    never by re-reading YAML here, so a change written in `_platform.yaml`,
    a `_defaults.yaml`, a profile or a sub-directory is a change, and a
    `tenants:` wrapper is a tenant, not a metric called `tenants`. Rows are
    per tenant and threshold key (the exporter's canonical spelling) whose
    served value or severity differs.

    * #3060: the severity decides which alert compares a row — a
      `severity="warning"` row the `metric_key` alert, a critical one the
      `<metric_key>_critical` alert, any other none — and the value alone
      decides when it fires. A side whose severity no rule selects is
      `old_compared` / `new_compared` False: `backtest_change` reads it as
      no threshold. Since #3071 the exporter serves no such row (`70:high`
      is an unreadable value, served as the platform default); only an
      older da-guard named by `DA_GUARD_BINARY` serves one. When
      the severities differ, `old_severity` / `new_severity` name them
      ("/"-joined when a side has several in the change's parts of the day);
      the same value under another compared severity (`70` -> `70:critical`)
      is a change of its own, which fires on the same points.

    * `series`: every /metrics series (`Series`, #2750) the key is served
      as on either side in any part of the day the pair holds in, as
      served-values reports it — never empty, since at least one side
      serves a value there and a served segment always carries its series.
      `alert_comparison` reads, from each one that is not a legacy twin,
      which alert compares it (`<metric_key>_critical` for a critical row,
      else `metric_key`; #3017).
    * `dimensioned_siblings` (only when non-empty): the dimensioned keys
      the tenant serves, on either side at any time of the day, as rows of
      the same metric key and severity as one of this change's rows. The
      alert's threshold is `max by(tenant)` over all of those rows, so the
      changed value alone is not what the alert compares (#3017).
    * A key /metrics serves on one side only (switched off, dropped, or the
      tenant added / removed) has None on the other side — the shape
      `backtest_change` already reads as "enabled" / "disabled".
    * Values are compared over the whole UTC day (`schedules`, both trees
      read at the same instant). One row per (old, new) pair a key takes:
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
    trees = []
    for flag, conf_d in (("--baseline", baseline_dir), ("--config-dir", config_dir)):
        trees.append((flag, load_served_tree(conf_d, at=at, schedules=True)))
    _print_tree_warnings(trees)
    old_tree, new_tree = trees[0][1].tenants, trees[1][1].tenants

    changes = []
    for tenant in sorted(set(old_tree) | set(new_tree)):
        old_tv, new_tv = old_tree.get(tenant), new_tree.get(tenant)
        keys = set()
        for tv_ in (old_tv, new_tv):
            if tv_ is not None:
                keys |= set(tv_.schedules or {})
        days = {key: (_served_day(old_tv, key, "baseline"), _served_day(new_tv, key, "current"))
                for key in sorted(keys)}
        dimensioned = _dimensioned_rows(days)
        for key, (old_day, new_day) in days.items():
            pairs = {}
            for start, end, old, new in _differing_runs(old_day, new_day):
                pairs.setdefault(_change_pair(old, new), []).append((start, end))
            for (old_v, new_v, old_c, new_c, *_), spans in pairs.items():
                spans = _joined(spans)
                change = {"tenant": tenant, "metric": key,
                          "old_value": old_v, "new_value": new_v}
                for side, compared in (("old", old_c), ("new", new_c)):
                    if not compared and change[f"{side}_value"] is not None:
                        change[f"{side}_compared"] = False
                old_s, new_s = (_severities_in(day, spans) for day in (old_day, new_day))
                if old_s and new_s and old_s != new_s:
                    change["old_severity"], change["new_severity"] = (
                        _severity_list(old_s), _severity_list(new_s))
                if spans != [(0, _DAY_MINUTES)]:
                    change["window"] = [f"{_hhmm(s)}-{_hhmm(e)}" for s, e in spans]
                change["series"] = _series_in((old_day, new_day), spans)
                siblings = sorted({k for s in change["series"]
                                   if not (s.legacy_twin or s.dimensions or s.dimensions_regex)
                                   for k in dimensioned.get(_row_identity(s), ())})
                if siblings:
                    change["dimensioned_siblings"] = siblings
                changes.append(change)

    return changes


# ---------------------------------------------------------------------------
# Custom-alert recipe awareness (#657)
#
# This tool is the *flat-threshold* eval home: it only understands scalar
# `metric: value` thresholds. Custom-alert recipes (ADR-024 `_custom_alerts`)
# are evaluated by a different authoritative engine (the compiler + promtool).
# Without these guards a recipe-only change passes silently (the flat tool
# finds nothing) and the line-based git-diff parser can even mis-capture a
# recipe's inner fields as bogus flat changes. We surface recipes loudly and
# keep them out of the flat report instead. Recipe would-fire preview: #657.
# ---------------------------------------------------------------------------

def changed_conf_files():
    """Tenant conf.d files changed in HEAD~1..HEAD, as `conf.d/<name>` paths.

    `git diff --name-only` emits REPO-ROOT-relative paths even when run from a
    subdirectory — and CI runs this tool from components/.../config, so the raw
    output (`components/.../conf.d/db-a.yaml`) is NOT loadable relative to cwd.
    Reduce each to `conf.d/<basename>`, the cwd-relative form the rest of the
    tool uses (matching the `git diff -- conf.d/` contract). Works whether
    conf.d sits at the repo root (customer convention) or under a subtree
    (this repo). (#657)
    """
    # ⚠️ `-z` here rather than `quotepath_off` (#1634): for `--name-only` the
    # NUL-delimited form is git's own contract for "never quote anything", and
    # unlike the unified-diff header it also carries a name holding a NEWLINE
    # intact. `-z` is not a substitute for `quotepath_off` in the other
    # direction -- see `extract_changes_from_git_diff`, where it does nothing.
    raw = _git_bytes(["diff", "-z", "--name-only", "HEAD~1", "--", "conf.d/"])
    if raw is None:
        return []
    # #1588 site 3 of 6. Same rule, third hand-written copy in one file.
    #
    # ⛔ `is_hidden_name` as well, and this is a fix for a split THIS CHAIN
    # INTRODUCED. Aligning `extract_changes_from_git_diff` on the
    # hidden axis (its `config_stem(basename) or None`) without aligning
    # this listing left the tool contradicting itself inside one run:
    # measured on a tree holding `.hidden.yaml` and `real.yaml`, both
    # changed —
    #
    #   05d3136   KEPT = [('.hidden','cpu_usage'), ('real','cpu_usage')]
    #   the split KEPT = [('real','cpu_usage')]
    #             while this function still answered
    #             ['conf.d/.hidden.yaml', 'conf.d/real.yaml']
    #
    # — i.e. "that carrier exists" and "its change does not", from the same
    # process, about the same file. That is the #1911 shape produced inside
    # the change that exists to remove it, and blind review had already
    # caught the identical split one code path over (`--config-dir`); this
    # is the sibling path that fix did not reach.
    #
    # ⚠️ `is_hidden_name`, NOT `config_stem`: the latter also declines
    # reserved names, and swapping to it silently dropped `_defaults.yaml`
    # from this list — a SECOND axis moving inside a hidden-axis fix.
    # `test_changed_conf_files_reduces_repo_root_to_cwd_relative` caught it.
    # ⚠️ No `.strip()` any more: with `-z` the delimiter is exact, and a name
    # is allowed to begin or end with a space. Stripping was load-bearing only
    # while the records were newline-delimited.
    #
    # ⛔ A KNOWN, NAMED DISAGREEMENT WITH `extract_changes_from_git_diff`.
    # This listing sees carriers that the diff parser above drops (it drops a
    # quoted or tab-padded header and says so). So the tool can answer "that
    # carrier changed" here while answering "it has no threshold change"
    # there — the #1911 shape, in one process, about one file.
    #
    # Measured on both trees, one carrier per arm:
    #
    #   has space.yaml   base ['conf.d/has space.yaml'] / diff [] )  identical
    #                    here ['conf.d/has space.yaml'] / diff [] )  to base
    #   has"quote.yaml   base []                        / diff []
    #                    here ['conf.d/has"quote.yaml'] / diff []  <- widened
    #
    # So the disagreement is PRE-EXISTING (the space row shows it byte for
    # byte on base) and `-z` widens it by one row. Closing it would mean
    # hand-writing git's quoting rules a second time, here in the listing, to
    # predict what the diff parser will refuse — a second copy of a rule,
    # which is the family's own charge. Deliberately left open and named.
    #
    # ⛔ And deliberately NOT pinned by a test: any assertion of the current
    # split would go red the day someone legitimately closes it, i.e. a guard
    # that says the opposite of what it means (#1448). The consequence is
    # bounded — the extra carriers only widen `find_custom_alert_tenants`
    # coverage; they cannot become a change, because the parser that builds
    # changes is the half that drops them.
    names = [_fsdecode(b) for b in raw.split(b"\0") if b]
    return [f"conf.d/{Path(n).name}" for n in names
            if has_yaml_extension(n)
            and not is_hidden_name(Path(n).name)]


def load_conf_files(paths, config_dir=None):
    """Read tenant conf.d files into {file stem: parsed dict}.

    ADR-036 step 4 (#2906): through `da-guard parse-tree` — the content as
    yaml.v3 reads it, and the file's selection as the exporter's: a path no
    reader reads (a hidden name, a name that is not a config file, an
    unselected carrier) is skipped, as is a `_`-prefixed platform file and a
    path that is not a file (e.g. deleted in the working tree).

    *config_dir* given (`--config-dir`): the paths are files of that tree,
    read from its one tree-mode document. None (`--git-diff`): each path is
    read alone, in parse-tree's file mode, as the file at its place in its
    own directory — so a sibling the document could not carry (a name that
    is not UTF-8) does not stop the read of the files that changed.

    A selected file parse-tree cannot read raises YamlFileError, naming it
    (rc 2); no usable da-guard raises ParseTreeError (rc 2).
    """
    parsed = {}
    for p in paths:
        path = Path(p)
        if path.name.startswith("_") or not path.is_file():
            continue
        # The caller's own spelling, not `str(path)`: on Windows that turns
        # the `conf.d/x.yaml` git reported into `conf.d\x.yaml`, and an
        # unreadable file is then named in a form nobody typed (#2558).
        # #2216: tenant ids are the keys' source text, as the exporter reads
        # them — `find_custom_alert_tenants` reports `010:` as "010", not 8.
        if config_dir is not None:
            reader = _lib_parse_cache.reader_for(config_dir)
            if reader.rel(path) not in reader.tree().selected:
                continue
            data = load_confd_file(config_dir, os.fspath(p), default={}, reader=reader)
        else:
            data = _read_alone(path, os.fspath(p))
            if data is None:
                continue
        if isinstance(data, dict):
            parsed[path.stem] = data
    return parsed


def _json_safe_name(rel):
    """*rel* with every byte that is not UTF-8 (a `surrogateescape`
    surrogate) replaced by U+FFFD: parse-tree's JSON cannot carry such a
    name, and in file mode the path only places the bytes being read — its
    prefix and suffix, which judge its readers, are kept. A tenant from such
    a name is dropped and named later (`tenant_is_queryable`)."""
    return os.fsencode(rel).decode("utf-8", "replace")


def _read_alone(path, shown):
    """*path* in parse-tree's file mode, as the file at its place in its own
    directory: its value, or None when no reader reads it there. Raises YamlFileError
    naming *shown* for a file parse-tree cannot read, ParseTreeError."""
    import _lib_parse_tree  # noqa: PLC0415
    rel = _json_safe_name(path.name)
    tree = _lib_parse_tree.load_file(path.parent, rel, path.read_bytes(), None,
                                     _lib_parse_cache.DEFAULT_TIMEOUT)
    if not tree.selected.get(rel):
        return None
    try:
        return _lib_parse_tree.to_python(tree.node(rel))
    except FileUnreadable as exc:
        raise YamlFileError(shown, exc) from exc


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


def _carrier_at_head1(tenant):
    """The HEAD~1 `conf.d/` path whose stem IS `tenant`, or None.

    ⚠️ Both spellings, matching every other site in this file (#1603): the
    tenant id reaching this function was itself parsed off a carrier this
    tool accepted, so a narrower set here would resolve to None for a
    tenant the very same run had just reported a change for.

    The comparison is `config_stem(name) == tenant` — the SAME predicate
    that produced the tenant id in the first place, so the two cannot
    disagree the way a re-spelled filename did.
    """
    # ⚠️ `-r` (#1634): without it `ls-tree` lists only the TOP level, so a
    # nested carrier shows up as a bare tree name with no extension and is
    # dropped by the extension test -- while `git diff -- conf.d/` upstream IS
    # recursive. That asymmetry meant a tenant parsed from a nested carrier
    # could never be resolved here, and the ADD direction accepted it anyway
    # (`parsed.get(tenant) is None` keeps it): one carrier, two answers.
    # `-z` for the same reason as in `changed_conf_files`.
    raw = _git_bytes(["ls-tree", "-r", "-z", "--name-only", "HEAD~1", "./conf.d/"])
    if raw is None:
        return None
    for entry in (_fsdecode(b) for b in raw.split(b"\0") if b):
        name = Path(entry).name
        if has_yaml_extension(name) and config_stem(name) == tenant:
            # ⚠️ The FULL relative path, not `./conf.d/` + basename: with `-r`
            # the entry can be nested, and rebuilding from the basename hands
            # `git show` a path that is not in the tree. The `./` prefix is
            # required, not decoration -- measured: `git show HEAD~1:./conf.d/x`
            # rc=0 while `HEAD~1:conf.d/x` rc=128, because `ls-tree` answers
            # CWD-relative while `git diff --name-only` answers
            # repo-root-relative. The two are not interchangeable.
            return f"./{entry}"
    return None


def _head1_text_as(rel, data):
    """*data* (a HEAD~1 file's bytes) read as the file at *rel* of the conf.d
    in the working directory — parse-tree's file mode; with no `conf.d/`
    there, in an empty directory (only the content is asked here). Raises
    FileUnreadable / ParseTreeError."""
    if Path("conf.d").is_dir():
        return _lib_parse_cache.read_text_as("conf.d", rel, data)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="da-backtest-") as empty:
        return _lib_parse_cache.read_text_as(empty, rel, data)


def _flat_keys_at_head1(tenant):
    """Top-level scalar threshold keys for `tenant` as of HEAD~1.

    Used to classify a REMOVED key, which is gone from the working tree so the
    current-file scan can't see it. `git show HEAD~1:./conf.d/<tenant>.yaml` —
    the leading `./` forces a cwd-relative path (git's `<rev>:<path>` is
    repo-root by default). Returns an empty set if the old file is unavailable
    (then the removal is conservatively dropped, the prior behaviour). (#657)
    """
    try:
        # ⛔ #1634 decision B2 -- bytes, and the `try` shape is kept ON PURPOSE
        # rather than moved onto `_git_bytes`: this call's stdout is file
        # CONTENT, not a path, so folding "rc != 0" and "git missing" into one
        # None would send the latter through the resolver for no reason.
        # What forces bytes here is the fix ABOVE, not this line: until now a
        # carrier whose NAME held invalid UTF-8 was dropped upstream and never
        # reached this call; with the sites above fixed it arrives for the
        # first time, and `text=True` raises `UnicodeDecodeError` on a body
        # holding one bad byte (measured -- and that exception is not in the
        # `except` below). Shipping the upstream fix without this one would
        # mean shipping a crash that only my own fix made reachable.
        # ⚠️ `yaml.safe_load` on bytes rejects an invalid-UTF-8 body with
        # `ReaderError`, a `YAMLError` subclass, so it lands in the existing
        # handler and the key set comes back empty -- same as for a body that
        # will not parse. Crash becomes the pre-existing silent skip; making
        # THAT audible is a different axis and is not smuggled in here.
        spec = f"./conf.d/{tenant}.yaml"
        result = subprocess.run(
            ["git", "show", f"HEAD~1:{spec}"],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            # ⛔ #1588 site 5 of 6, and the one the first version MISSED —
            # it counted "of 4" and this was not among them. Re-spelling
            # the carrier from the tenant id is the family's own defect in
            # miniature: `config_stem` preserves the carrier's case (it
            # must — folding it renames the tenant on the write plane), so
            # a `DB-A.YAML` carrier gives tenant `DB-A`, and this line then
            # asked git for `conf.d/DB-A.yaml`, which does not exist.
            # Measured end to end on two git repos with byte-identical
            # bodies, HEAD~1 holding a threshold that HEAD removes:
            #
            #   lower              raw=1 change   KEPT=1  (reported)
            #   UPPER, this file    raw=1 change   KEPT=0  (dropped here)
            #   UPPER, on 05d3136   raw=0 change   KEPT=0  (dropped earlier,
            #                                              by sites 1 and 3)
            #
            # ⚠️ Those three rows are the CORRECTED table. The first version
            # of this comment printed `05d3136` as `raw=1 KEPT=0`, which is
            # this file's own intermediate state, not the base — blind review
            # re-ran it and got `raw=0`. The operator-visible symptom really
            # is identical on both ("No threshold changes found." for a
            # removal that happened), which is what matters, but a number
            # labelled "measured" has to be the number that was measured.
            #
            # The name is resolved by ASKING GIT WHAT IS THERE rather than
            # by spelling it again. Kept as a fallback so the guessed path
            # stays the fast path and nothing changes when it is right.
            resolved = _carrier_at_head1(tenant)
            if resolved is None:
                return set()
            # `resolved` now carries the FULL cwd-relative path with its `./`
            # prefix (see `_carrier_at_head1`), so this spec stays correct for
            # a nested carrier as well. Bytes for the same reason as above.
            spec = resolved
            result = subprocess.run(
                ["git", "show", f"HEAD~1:{spec}"],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                return set()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    # ADR-036 step 4 (#2906): the HEAD~1 text is read by `da-guard
    # parse-tree`'s file mode, as the file it was at its path — yaml.v3, as
    # the exporter read it then. A file it cannot read (bad syntax, a key
    # written twice — the exporter dropped it whole) keeps no removal, and
    # is named. No usable da-guard raises ParseTreeError (rc 2 in `main`).
    rel = _json_safe_name(spec.removeprefix("./conf.d/"))
    try:
        data = _head1_text_as(rel, result.stdout)
    except FileUnreadable as exc:
        print(f"WARNING: HEAD~1:{safe_label(spec)}: removals not classified "
              f"— da-guard parse-tree cannot read it ({safe_label(exc.kind)}: "
              f"{safe_label(exc.message)})", file=sys.stderr)
        return set()
    tenants = data.get("tenants") if isinstance(data, dict) else None
    keys = set()
    for tconf in (tenants.values() if isinstance(tenants, dict) else []):
        if isinstance(tconf, dict):
            keys |= {k for k, v in tconf.items()
                     if not str(k).startswith("_") and not isinstance(v, (dict, list))}
    return keys


def keep_flat_threshold_changes(changes, parsed):
    """Drop changes whose key isn't a real flat threshold.

    The line-based git-diff parser captures any `key: value` line regardless
    of nesting, so it mis-captures `_custom_alerts` recipe inner fields
    (recipe / name / op / window / threshold / metric / mode) as flat changes.
    A real flat threshold is a SCALAR directly under `tenants.<id>`.

    Adds / modifies are classified against the CURRENT file. A REMOVAL (the key
    is gone from the working tree, so the current-file scan would always say
    "not flat") is classified against HEAD~1 instead — so a real flat-threshold
    removal (a disable transition the backtest reports) is kept, while a removed
    recipe inner-field is dropped. (#657)
    """
    kept = []
    head1 = {}
    for c in changes:
        if c["new_value"] is None:  # removal — the key is gone from the tree
            if c["tenant"] not in head1:
                head1[c["tenant"]] = _flat_keys_at_head1(c["tenant"])
            if c["metric"] in head1[c["tenant"]]:
                kept.append(c)
            continue
        file_data = parsed.get(c["tenant"])
        if file_data is None:
            kept.append(c)
            continue
        tenants = file_data.get("tenants")
        tconfs = list(tenants.values()) if isinstance(tenants, dict) else []
        is_flat = any(
            isinstance(tc, dict)
            and c["metric"] in tc
            and not isinstance(tc[c["metric"]], (dict, list))
            for tc in tconfs
        )
        if is_flat:
            kept.append(c)
    return kept


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


def _written_severities(change):
    """`[old severity, new severity]` of a change written as `--git-diff` /
    `--tenant` give it, as the exporter reads the values (None for a side
    without one, or one this backtest cannot read)."""
    key = canonical_key(change["metric"], DEPRECATED_KEY_ALIASES).partition("{")[0]
    out = []
    for raw in (change["old_value"], change["new_value"]):
        try:
            parsed = parse_threshold_value(key, raw)
        except ValueError:
            parsed = None
        out.append(parsed and parsed[1])
    return out


def _written_alert_keys(change):
    """`(keys, None)`: the threshold keys whose alerts compare a change
    written as `--git-diff` / `--tenant` give it — or `(None, reason)`.

    The key as written: a retired spelling goes through the alias table
    (the exporter's own), and `N:critical` on a base key is a critical row,
    compared by the `<key>_critical` alert. #3060: each side is compared by
    the alert of its own severity, so `70` -> `50:critical` is compared by
    both. A value that is not read as written is a reason — since #3071 that
    includes a severity no threshold rule selects (`70:high`, `70:`), which
    makes the exporter's whole value unreadable (`parse_threshold_value`
    raises), so the `uncompared` branch below is never taken for it."""
    key = canonical_key(change["metric"], DEPRECATED_KEY_ALIASES).partition("{")[0]
    keys, uncompared = [], []
    for raw in (change["old_value"], change["new_value"]):
        try:
            parsed = parse_threshold_value(key, raw)
        except ValueError:
            return None, f"value {str(raw)!r}: this backtest cannot read it the way the exporter does"
        if parsed is None:
            continue
        if parsed[1] not in _COMPARED_SEVERITIES:
            uncompared.append(parsed[1])
            continue
        alert = key + "_critical" if parsed[1] == "critical" and not key.endswith("_critical") else key
        if alert not in keys:
            keys.append(alert)
    if not keys:
        return None, f"severity {(uncompared or ['warning'])[0]!r} is selected by no threshold rule"
    return keys, None


def _alert_keys(change):
    """`(keys, None)`: the threshold keys whose alerts compare this change,
    in canonical spelling, each once — or `(None, reason)`.

    A change read from two trees names them by its /metrics series (#2750):
    a critical row is compared by the `<metric_key>_critical` alert (a window
    overridden to `N:critical` too), a warning row by `metric_key`'s; a row
    of any other severity (the rules select `severity="warning"` /
    `"critical"` only) by none: #3060, such a row is skipped
    (`backtest_change` reads its side as no threshold), and with no row any
    rule selects the change is not backtested. Since #3071 the exporter
    serves no such row; one comes only from an older da-guard named by
    `DA_GUARD_BINARY`, which served `60:high` as written. A legacy twin is
    the same row under a retired name and is never read.
    ⛔ A non-critical row whose `metric_key` itself ends in `_critical` is a
    `<base>_critical` key served on its own (its base is not declared in the
    root `defaults:`): its metric label is `<x>_critical`, which no threshold
    rule selects, so no alert compares it. A change from `--git-diff` /
    `--tenant` is `_written_alert_keys`."""
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
        critical = severity == "critical"
        if not critical and s.metric_key.endswith("_critical"):
            return None, (f"served as a non-critical row of {s.metric_key} (its base is not "
                          "declared in the root defaults:), which no alert compares")
        key = s.metric_key + "_critical" if critical else s.metric_key
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
    compares it, or KNOWN_DEFERRED), or whose entry a backtest cannot replay
    (`usable_for_backtest`: needs review, composite, unresolved direction,
    not per-tenant), has a reason and is not backtested; so is a change
    whose rows are compared by alerts that disagree on the series or the
    direction (one replay cannot stand for both). A reason about a key other
    than the change's own (a critical row's `_critical` alert) names it."""
    observed = _observed_map()
    keys, reason = _alert_keys(change)
    if reason is not None:
        return None, None, reason
    found = []
    for key in keys:
        entry, reason = observed_map_lib.lookup(observed, key)
        if entry is not None:
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
    `dimensions_regex`. A change from `--git-diff` or `--tenant` names a key
    as written, with no series; there the key's `{` is what marks it
    dimensioned.

    #3017: a served change with `dimensioned_siblings` (the alert's
    threshold is the max over them too) is not backtested. A change from
    `--git-diff` / `--tenant` cannot know its siblings — a known
    approximation, like `for:`.

    #3017 / #3002: then `alert_comparison`'s reason — a key whose alert the
    observed-map does not record as one per-tenant series compared one way
    is not backtested, rather than replayed against a guessed query."""
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


def _side_compared(change, side, parsed):
    """Whether a threshold rule selects this side's severity (#3060). A
    change read from two trees says so (`<side>_compared`); one written as
    `--git-diff` / `--tenant` give it carries the severity in its value."""
    if "series" in change:
        return change.get(f"{side}_compared", True)
    return parsed is None or parsed[1] in _COMPARED_SEVERITIES


def severity_change(change):
    """`(old, new)`: the severities of a change whose two sides differ in
    it, or None (#3060). A side without a threshold has none to compare."""
    if "series" in change:
        if "old_severity" in change:
            return change["old_severity"], change["new_severity"]
        return None
    old_s, new_s = _written_severities(change)
    return (old_s, new_s) if old_s is not None and new_s is not None and old_s != new_s else None


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
    key = canonical_key(metric, DEPRECATED_KEY_ALIASES).partition("{")[0]
    old_t, new_t = (parse_threshold_value(key, v) for v in (old_value, new_value))
    # #3060: a side whose severity no threshold rule selects is no threshold.
    old_t, new_t = (t and t[0] if _side_compared(change, side, t) else None
                    for side, t in (("old", old_t), ("new", new_t)))
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
    """A side's value as the reports show it: `none` without one, and its
    severity beside it when the two sides' severities differ (#3060) and
    the value does not already carry it (`--tenant 70:critical` does)."""
    value = change[f"{side}_value"]
    if value is None:
        return none
    severity = change.get(f"{side}_severity")
    if severity is None or ":" in str(value):
        return str(value)
    return f"{value} ({safe_label(severity) or '\"\"'})"


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
              f"{old_v} -> {new_v}{window}")
        print(f"           {safe_label(change['message'])}")
        if change.get("query"):  # #3002 B: the PromQL this verdict rests on
            print(f"           query: {safe_label(change['query'])}")

    print()


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
        query = f"`{c['query']}`" if c.get("query") else "—"
        # #3059: a `query_error` message carries Prometheus's own text, which
        # may hold `|` (a PromQL parse error quoting the query) — escaped so
        # it stays one cell.
        message = c["message"].replace("|", "\\|")
        lines.append(f"| {c['risk']} | {c['tenant']} | `{c['metric']}`{window} | "
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
    path (same shape as the ``git_diff_unavailable`` exit above).
    """
    print(f"ERROR: cannot read {safe_label(exc)}", file=sys.stderr)  # #1538
    _end_not_run(args, "caller_error", "conf_file_unreadable", EXIT_CALLER_ERROR)


def _exit_parse_tree_error(exc, args, recipe_tenants=()):
    """ADR-036 step 4 (#2906): no `da-guard parse-tree` document (missing,
    failing or older than this tool, or a tree it refuses whole — e.g. a
    file name JSON cannot carry) → rc 2, one `ERROR:` line naming why, the
    `--json` `caller_error` envelope — the conf.d was not read."""
    print(_lib_parse_cache.parse_tree_error_line(exc), file=sys.stderr)
    _end_not_run(args, "caller_error", "parse_tree_failed", EXIT_CALLER_ERROR,
                 recipe_tenants)


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


@observed_map_lib.exit_on_observed_map_error  # #3017: no usable observed-map → rc 2, named
def main():
    """CLI entry point: Backtest threshold changes against historical Prometheus data."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Backtest threshold changes against historical Prometheus data",
    )

    # Change source (mutually exclusive)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--git-diff", action="store_true",
        help="Extract changes from git diff HEAD~1 -- conf.d/",
    )
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
    elif not (args.git_diff or args.config_dir):
        print("ERROR: Specify --git-diff, --config-dir, or --tenant", file=sys.stderr)
        if args.json:
            print(format_json_report(empty_report(
                args.lookback, "caller_error", "no_change_source")))
        sys.exit(EXIT_CALLER_ERROR)

    # #3089 (b): after the argument checks, before any git, served-values,
    # observed-map or Prometheus work — every path below writes the outputs.
    _preflight_outputs(args)

    # Surface custom-alert recipes (parsed from disk; Prometheus-independent),
    # BEFORE the availability gate — a recipe-only change must not pass
    # silently just because the flat backtest has nothing to do or Prometheus
    # is unreachable. Recipes use the compiler+promtool eval home, not this
    # flat-threshold tool. (#657)
    # ADR-036 step 4 (#2906): the tenant files are the ones `da-guard
    # parse-tree` selects (`--config-dir`: every tenant file of the tree, at
    # any depth; `--git-diff`: the changed paths parse-tree selects), read by
    # yaml.v3. A file it cannot read is rc 2, named; no usable da-guard too.
    try:
        if args.git_diff:
            parsed_conf = load_conf_files(changed_conf_files())
        elif args.config_dir:
            tree = _lib_parse_cache.reader_for(args.config_dir).tree()
            parsed_conf = load_conf_files(
                [os.path.join(args.config_dir, rel)
                 for rel in _lib_parse_cache.tenant_files(tree)],
                config_dir=args.config_dir)
        else:
            parsed_conf = {}
    except YamlFileError as exc:
        _exit_unreadable_conf(exc, args)
    except ParseTreeError as exc:
        _exit_parse_tree_error(exc, args)
    recipe_tenants = find_custom_alert_tenants(parsed_conf)
    if recipe_tenants:
        print(custom_alert_notice(recipe_tenants), file=sys.stderr)

    # #1556 item 4 / D-06: `--git-diff` was supplied, so git must be able to
    # answer. This runs BEFORE the Prometheus check on purpose:
    # `--skip-if-unavailable` is about Prometheus, not git, and must not turn
    # "git is unusable" into a green skip. It also runs before the "no
    # changes" exit below, which is the silent path this replaces.
    git_changes = None
    if args.git_diff:
        git_changes = extract_changes_from_git_diff()
        if git_changes is None:
            print("--git-diff: `git diff HEAD~1 -- conf.d/` could not run "
                  "(git not on PATH, not inside a git work tree, or no parent "
                  "commit); run from a checkout with git installed.\n"
                  "  ⛔ Do not switch to --config-dir to clear this — that "
                  "compares two trees, not your PR.", file=sys.stderr)
            _end_not_run(args, "caller_error", "git_diff_unavailable", EXIT_CALLER_ERROR,
                         recipe_tenants)

    # #2119: the two trees are read by da-guard, and this runs BEFORE the
    # Prometheus check for the reason the git check above does:
    # `--skip-if-unavailable` is about Prometheus, and must not turn "a tree
    # the exporter refuses" into a green skip.
    dir_changes = None
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
    if args.git_diff:
        try:
            changes = keep_flat_threshold_changes(git_changes, parsed_conf)
        except ParseTreeError as exc:
            _exit_parse_tree_error(exc, args, recipe_tenants)
    elif args.config_dir:
        changes = dir_changes
    else:  # `--tenant`: the no-source case already exited above
        changes = [{
            "tenant": args.tenant,
            "metric": args.metric,
            "old_value": args.old_value,
            "new_value": args.new_value,
        }]

    # ⛔ #1634: one filter, all three sources — see `tenant_is_queryable` for
    # why this is not per-path. A carrier whose name is not valid UTF-8 cannot
    # be backtested (Prometheus cannot be asked about it), so it is dropped —
    # but NAMED, because "the tenant quietly disappeared from the report" is
    # the symptom this whole family is about.
    for change in changes:
        if not tenant_is_queryable(change["tenant"]):
            _warn_dropped_carrier(
                change["tenant"],
                "the carrier name is not valid UTF-8, so it cannot be a "
                "PromQL label value")
    changes = [c for c in changes if tenant_is_queryable(c["tenant"])]

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
        severities = severity_change(change)
        if severities:  # #3060: `70 -> 70` alone does not say what changed
            result["old_severity"], result["new_severity"] = severities
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
