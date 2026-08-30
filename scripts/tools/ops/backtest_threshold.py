#!/usr/bin/env python3
"""backtest_threshold.py — Backtest threshold changes against historical Prometheus data.

Given a set of threshold changes (from git diff or manual input), queries
Prometheus range data to simulate how alert firing counts would change under
old vs new thresholds. Produces a risk assessment report suitable for PR review.

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
import os
import re
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
from _lib_python import load_yaml_file, is_disabled, http_get_json, query_prometheus_range, write_json_secure, write_text_secure, add_prometheus_arg  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import (  # noqa: E402
    config_stem,
    has_yaml_extension,
    is_hidden_name,
    is_reserved_name,
    warn_nested,
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
    """Convert lookback string (e.g., '7d', '24h') to seconds."""
    m = re.match(r"^(\d+)([dhm])$", lookback_str)
    if not m:
        return 7 * 86400  # default 7d
    val = int(m.group(1))
    unit = m.group(2)
    multipliers = {"d": 86400, "h": 3600, "m": 60}
    return val * multipliers[unit]


def prometheus_available(prom_url, timeout=5):
    """Check if Prometheus is reachable."""
    url = f"{prom_url}/api/v1/status/buildinfo"
    data, err = http_get_json(url, timeout=timeout)
    return err is None


def query_range(prom_url, query, lookback_seconds, step=DEFAULT_STEP):
    """Execute a Prometheus range_query and return result data.

    Thin wrapper over ``_lib_prometheus.query_prometheus_range`` (ROI r3 W1):
    the fetch core lives in the lib; the "any error → []" collapse stays
    here (tests monkeypatch this module attribute by name — keep it).
    """
    import time
    end = time.time()
    start = end - lookback_seconds

    result, err = query_prometheus_range(
        prom_url, query, start, end, step, timeout=30)
    if err:
        return []
    return result


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


def extract_changes_from_git_diff():
    """Parse git diff of conf.d/ to find threshold changes.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...]
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--unified=0", "--", "conf.d/"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    changes = []
    current_file = None

    for line in result.stdout.splitlines():
        # Track current file
        if line.startswith("+++ b/"):
            fname = line[6:]
            # Extract tenant from filename (conf.d/db-a.yaml → db-a)
            basename = Path(fname).name
            # #1588 site 1 of 6 in this file (the first version counted 4 and the
            # two it missed were `_flat_keys_at_head1` — which was still
            # broken — and `load_conf_files`, which needed no change). `.yaml` ONLY is preserved on
            # purpose — the spelling axis is #1603 — but the case folding
            # and the stem both move to the shared predicates. Hand-slicing
            # the stem is what `config_stem` exists to stop: `str.lower()`
            # can shrink byte length, so an offset taken from the folded
            # copy cuts the original in the wrong place.
            # ⚠️ `config_stem` also declines `.`-prefixed names, so a diff
            # touching `conf.d/.foo.yaml` no longer yields a tenant called
            # `.foo`. The exporter's scanner skips hidden entries, so that
            # agrees with the oracle — but it is a second-order change and
            # is recorded here rather than left for someone to discover.
            if has_yaml_extension(basename, (".yaml",)) \
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
        old_match = re.match(r"^-\s+(\w+):\s+(.+)$", line)
        new_match = re.match(r"^\+\s+(\w+):\s+(.+)$", line)

        if old_match:
            metric = old_match.group(1)
            old_val = old_match.group(2).strip().strip("'\"")
            # Look for corresponding + line
            changes.append({
                "tenant": current_file,
                "metric": metric,
                "old_value": old_val,
                "new_value": None,  # will be filled by + line
            })
        elif new_match:
            metric = new_match.group(1)
            new_val = new_match.group(2).strip().strip("'\"")
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
_WARNED_UNLISTABLE: set = set()


def reset_unlistable_warnings_for_test() -> None:
    """Clear the once-per-run dedup so a test can drive the branch twice."""
    _WARNED_UNLISTABLE.clear()


def _confd_entries(root: Path) -> list:
    """List `root` once — naming it, never raising, never silently empty.

    ⛔ Written because the #1588 fix below broke this and blind review
    measured it. Swapping `glob("*.yaml")` for `iterdir()` looks like a
    pure case fix, but the two disagree on an UNREADABLE directory:
    `glob` swallows the scandir failure and yields nothing, `iterdir()`
    raises straight through this module's callers. Measured on a
    `chmod 000` conf.d as a non-root uid, same fixture both sides:

        05d3136 (glob)     rc=2, "ERROR: Prometheus not reachable ..."
        the fix (iterdir)  rc=1, PermissionError traceback out of main()

    ⚠️ `is_dir()` does NOT guard this: it returns True for a directory
    that cannot be read (and raises on its own when the PARENT is not
    traversable), so the guard it replaced covered only the missing case.

    `_lib_confd.unusable_config_paths` already settled what the right
    answer is, in the same words: raising kills callers that iterate
    outside a `try`, and returning `[]` is "a green light for a directory
    nothing ever read" — so name the root instead. This is that answer at
    the one call site that cannot use that function (it needs the ENTRIES,
    not the unusable ones).
    """
    # ⛔ The #1339 guard lives HERE, not at the two call sites, and
    # `test_confd_enumeration_contract` is what moved it: a flat scan and
    # the warning that a hierarchical conf.d is not empty must be in the
    # same scope, or a refactor can carry the scan away from its guard.
    # This function is now the only place this tool lists a conf.d, so
    # both sites are covered by construction rather than by remembering.
    warn_nested(root, tool="backtest_threshold")
    try:
        return sorted(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        # Both were empty results under `glob`, and a missing --config-dir
        # is already reported by the caller's own emptiness. Unchanged.
        return []
    except OSError as exc:
        # ⛔ The caught errno, NOT `_lib_confd.unusable_reason`. That
        # function answers "why is `p` unusable as a config FILE", and the
        # conf.d root is not one — asked about the root it re-probes with
        # `os.walk` and can answer "is a directory, not a config file",
        # which is both a non-sequitur (of course it is a directory) and a
        # SECOND probe that may disagree with the failure actually caught
        # here (a FUSE mount going away, a transient EIO). Reporting what
        # was caught cannot drift from what happened.
        # ⛔ Once per RUN, not once per scan. `--config-dir` calls this
        # helper twice (`main` for the recipe scan, `extract_changes_from_dirs`
        # for the comparison) and the first version printed the same line
        # twice — measured. This file's own
        # `test_each_reader_names_an_unusable_entry_exactly_once` states the
        # rule for every other reader: "A repeated warning trains the
        # operator to skim past it, which costs the signal the report exists
        # to give." `warn_nested` already dedupes; this now does too.
        key = os.path.abspath(str(root))
        if key not in _WARNED_UNLISTABLE:
            _WARNED_UNLISTABLE.add(key)
            print(f"WARNING: {safe_label(str(root))}: could not be listed — "
                  f"{type(exc).__name__}: {exc.strerror or exc} — the config "
                  f"files inside it were NOT scanned", file=sys.stderr)
        return []


def extract_changes_from_dirs(config_dir, baseline_dir):
    """Compare two config directories to find threshold changes.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...]
    """
    changes = []

    config_base = Path(config_dir)
    baseline_base = Path(baseline_dir)
    # #1588 site 2 of 6. `glob("*.yaml")` is case-SENSITIVE on Linux, so a
    # `DB-A.YAML` carrier produced 0 changes where the identical body under
    # `db-a.yaml` produced 1 — a backtest that reports "no threshold changes"
    # for a change that is really there. `iterdir()` + the shared predicate
    # yields the SAME set as the glob did (directories included, exactly as
    # `glob` returned them), only case-folded: adding an `is_file()` filter
    # here would be the #1607 axis, which is not this commit's subject.
    # ⚠️ The listing goes through `_confd_entries`, not a bare `iterdir()`:
    # see its docstring for the unreadable-directory regression that a
    # plain `is_dir()` guard does NOT cover.
    _entries = _confd_entries(config_base)
    for path in (p for p in _entries
                 if has_yaml_extension(p.name, (".yaml",))):
        basename = path.name
        if is_reserved_name(basename):
            continue

        # ⛔ `removesuffix(".yaml")` is case-sensitive too, and letting the
        # scan above see `DB-A.YAML` while this line failed to strip it
        # produced a report naming a tenant called `DB-A.YAML` — the fix
        # for a silent miss turned into a loud wrong answer. Measured.
        tenant = config_stem(basename)
        if not tenant:
            # ⛔ `config_stem` answers "" for a `.`-prefixed name, and the
            # first version of this fix used the answer WITHOUT checking
            # it: blind review measured `.foo.yaml` producing a change
            # whose tenant was the empty string, where `05d3136` at least
            # said `.foo`. A silent miss turned into a loud wrong answer —
            # and an empty id flows on into the report and into
            # `_flat_keys_at_head1("")`.
            #
            # Skipped, not warned: hidden entries are skipped by every
            # reader in this repo and by the exporter's own scanner, so
            # naming one here would report a loss that did not happen
            # (the #1607 round settled that wording).
            continue
        new_data = load_yaml_file(str(path), default={})
        baseline_path = str(baseline_base / basename)
        old_data = load_yaml_file(baseline_path, default={})

        # Compare all metric keys
        all_keys = set(list(new_data.keys()) + list(old_data.keys()))
        for key in sorted(all_keys):
            if key.startswith("_"):
                continue
            old_val = old_data.get(key)
            new_val = new_data.get(key)
            if str(old_val) != str(new_val):
                changes.append({
                    "tenant": tenant,
                    "metric": key,
                    "old_value": str(old_val) if old_val is not None else None,
                    "new_value": str(new_val) if new_val is not None else None,
                })

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
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "--", "conf.d/"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
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
    # process, about the same file. That is the #1339 shape produced inside
    # the change that exists to remove it, and blind review had already
    # caught the identical split one code path over (`--config-dir`); this
    # is the sibling path that fix did not reach.
    #
    # ⚠️ `is_hidden_name`, NOT `config_stem`: the latter also declines
    # reserved names, and swapping to it silently dropped `_defaults.yaml`
    # from this list — a SECOND axis moving inside a hidden-axis fix.
    # `test_changed_conf_files_reduces_repo_root_to_cwd_relative` caught it.
    return [f"conf.d/{Path(ln.strip()).name}" for ln in result.stdout.splitlines()
            if has_yaml_extension(ln.strip(), (".yaml",))
            and not is_hidden_name(Path(ln.strip()).name)]


def load_conf_files(paths):
    """Parse tenant conf.d files into {tenant: parsed_dict}.

    Skips platform files (`_`-prefixed) and anything that isn't a YAML
    mapping; missing files are skipped silently (e.g. a path deleted in the
    working tree).
    """
    parsed = {}
    for p in paths:
        path = Path(p)
        # ⛔ `is_hidden_name` added, and ONLY that. `startswith("_")`
        # answered the reserved axis but nothing answered the hidden one,
        # so this function kept saying `.hidden` exists while the diff
        # parser beside it had stopped producing changes for that carrier —
        # one process, one file, two answers.
        # ⚠️ Deliberately NOT `config_stem` here: that predicate also
        # declines reserved names, and swapping to it silently dropped
        # `_defaults.yaml` from `changed_conf_files` — a SECOND axis moving
        # inside a hidden-axis fix. `test_changed_conf_files_reduces_repo_
        # root_to_cwd_relative` caught it; the narrow predicate is the fix.
        if (path.name.startswith("_") or is_hidden_name(path.name)
                or not path.is_file()):
            continue
        data = load_yaml_file(str(path), default={})
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


def _carrier_at_head1(tenant):
    """The HEAD~1 `conf.d/` path whose stem IS `tenant`, or None.

    ⚠️ `.yaml` ONLY, matching every other site in this file: the spelling
    axis is #1603 and widening it here would let a `.yml` carrier answer
    for a tenant this tool otherwise cannot see.

    The comparison is `config_stem(name) == tenant` — the SAME predicate
    that produced the tenant id in the first place, so the two cannot
    disagree the way a re-spelled filename did.
    """
    try:
        result = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD~1", "./conf.d/"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in result.stdout.splitlines():
        entry = line.strip()
        if not entry:
            continue
        name = Path(entry).name
        if has_yaml_extension(name, (".yaml",)) and config_stem(name) == tenant:
            return f"./conf.d/{name}"
    return None


def _flat_keys_at_head1(tenant):
    """Top-level scalar threshold keys for `tenant` as of HEAD~1.

    Used to classify a REMOVED key, which is gone from the working tree so the
    current-file scan can't see it. `git show HEAD~1:./conf.d/<tenant>.yaml` —
    the leading `./` forces a cwd-relative path (git's `<rev>:<path>` is
    repo-root by default). Returns an empty set if the old file is unavailable
    (then the removal is conservatively dropped, the prior behaviour). (#657)
    """
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD~1:./conf.d/{tenant}.yaml"],
            capture_output=True, text=True, timeout=15,
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
            result = subprocess.run(
                ["git", "show", f"HEAD~1:{resolved}"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return set()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    import yaml
    try:
        data = yaml.safe_load(result.stdout) or {}
    except yaml.YAMLError:
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


def backtest_change(prom_url, change, lookback_seconds):
    """Backtest a single threshold change against historical data.

    Returns analysis dict with breach counts and risk assessment.
    """
    metric = change["metric"]
    tenant = change["tenant"]
    old_value = change["old_value"]
    new_value = change["new_value"]

    # Build PromQL query for this metric + tenant
    # Try common recording rule patterns
    queries = [
        f'{metric}{{tenant="{tenant}"}}',
        f'tenant:{metric}:max{{tenant="{tenant}"}}',
        f'{metric}{{namespace="{tenant}"}}',
    ]

    values = []
    used_query = None
    for q in queries:
        result = query_range(prom_url, q, lookback_seconds)
        if result:
            # Collect all values from all series
            for series in result:
                values.extend(series.get("values", []))
            used_query = q
            break

    if not values:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "no_data",
            "risk": "UNKNOWN",
            "message": "No historical data found in Prometheus",
        }

    total_points = len(values)

    # Handle disable transitions
    old_disabled = old_value is None or is_disabled(str(old_value))
    new_disabled = new_value is None or is_disabled(str(new_value))

    if new_disabled and not old_disabled:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "risk": "MEDIUM",
            "data_points": total_points,
            "old_breach_count": count_threshold_breaches(values, old_value),
            "new_breach_count": 0,
            "impact_pct": -100.0,
            "message": "Metric disabled — all alerts silenced",
        }

    if old_disabled and not new_disabled:
        new_breaches = count_threshold_breaches(values, new_value)
        pct = (new_breaches / total_points * 100) if total_points > 0 else 0
        risk = "HIGH" if pct > 10 else "MEDIUM" if pct > 0 else "LOW"
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "risk": risk,
            "data_points": total_points,
            "old_breach_count": 0,
            "new_breach_count": new_breaches,
            "impact_pct": float("inf") if new_breaches > 0 else 0,
            "message": f"Metric newly enabled — {new_breaches}/{total_points} points would fire",
        }

    # Normal threshold change
    old_breaches = count_threshold_breaches(values, old_value)
    new_breaches = count_threshold_breaches(values, new_value)

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

        direction = "more" if new_breaches > old_breaches else "fewer"
        message = f"{abs(new_breaches - old_breaches)} {direction} firing points ({impact_pct:+.1f}%)"

    return {
        "tenant": tenant,
        "metric": metric,
        "old_value": old_value,
        "new_value": new_value,
        "status": "analyzed",
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
        "risk_summary": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
        "changes": [],
    }


def generate_report(results, lookback):
    """Generate aggregate backtest report."""
    analyzed = [r for r in results if r["status"] == "analyzed"]
    no_data = [r for r in results if r["status"] == "no_data"]

    high_risk = [r for r in analyzed if r["risk"] == "HIGH"]
    medium_risk = [r for r in analyzed if r["risk"] == "MEDIUM"]
    low_risk = [r for r in analyzed if r["risk"] == "LOW"]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback": lookback,
        "total_changes": len(results),
        "analyzed": len(analyzed),
        "no_data": len(no_data),
        "risk_summary": {
            "HIGH": len(high_risk),
            "MEDIUM": len(medium_risk),
            "LOW": len(low_risk),
        },
        "changes": results,
    }


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
        print(f"  No data: {report['no_data']} (metric not found in Prometheus)")
    print()

    for change in report["changes"]:
        risk = change["risk"]
        marker = "!!!" if risk == "HIGH" else " ! " if risk == "MEDIUM" else "   "
        old_v = change["old_value"] or "(none)"
        new_v = change["new_value"] or "(none)"
        print(f"  {marker} [{risk:6s}] {safe_label(change['tenant'])}/"
              f"{safe_label(change['metric'])}: "
              f"{old_v} -> {new_v}")
        print(f"           {safe_label(change['message'])}")

    print()


def generate_markdown(report):
    """Generate Markdown suitable for a PR comment."""
    lines = []
    lines.append("## Threshold Backtest Results")
    lines.append("")
    lines.append(f"**Lookback:** {report['lookback']} | "
                 f"**Analyzed:** {report['analyzed']}/{report['total_changes']}")

    rs = report["risk_summary"]
    if rs["HIGH"] > 0:
        lines.append(f"\n> **{rs['HIGH']} HIGH risk change(s) detected.**")

    lines.append("")
    lines.append("| Risk | Tenant | Metric | Old | New | Impact |")
    lines.append("|------|--------|--------|-----|-----|--------|")

    for c in sorted(report["changes"], key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}.get(x["risk"], 9)):
        old_v = c["old_value"] or "—"
        new_v = c["new_value"] or "—"
        lines.append(f"| {c['risk']} | {c['tenant']} | `{c['metric']}` | "
                     f"{old_v} | {new_v} | {c['message']} |")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by `backtest_threshold.py`*")

    return "\n".join(lines)


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
        help="Single tenant (requires --metric, --old-value, --new-value)",
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
    parser.add_argument(
        "--lookback", default=DEFAULT_LOOKBACK,
        help=f"Historical lookback window (default: {DEFAULT_LOOKBACK})",
    )
    parser.add_argument(
        "--skip-if-unavailable", action="store_true",
        help="Exit 0 gracefully if Prometheus is unreachable",
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

    # Surface custom-alert recipes (parsed from disk; Prometheus-independent),
    # BEFORE the availability gate — a recipe-only change must not pass
    # silently just because the flat backtest has nothing to do or Prometheus
    # is unreachable. Recipes use the compiler+promtool eval home, not this
    # flat-threshold tool. (#657)
    if args.git_diff:
        parsed_conf = load_conf_files(changed_conf_files())
    elif args.config_dir:
        # #1588 site 4 of 6. Same listing helper as the scan above — the
        # unreadable-directory case reached main() FIRST, before the
        # Prometheus availability check, so `--skip-if-unavailable` could
        # not contain it and the `--json` contract lost its one document.
        #
        # ⛔ `config_stem(...)` truthiness, not just the extension, and that
        # is not tidiness. Blind review measured this tool answering the
        # HIDDEN axis two different ways after the first fix: the scan above
        # skipped `.hidden.yaml` (its `config_stem` is "") while this site
        # still loaded it, so ONE TOOL disagreed with itself about which
        # carriers exist. `05d3136` was at least consistent (both said
        # `.hidden`). Producing that split inside the change that exists to
        # remove exactly that split is the sharpest way to get this wrong;
        # both sites now defer to the same predicate, which is also what the
        # exporter's scanner does.
        # ⛔ No filter here any more. `load_conf_files` applies the reserved
        # and hidden rules itself (it must — the `--git-diff` path feeds it
        # too), so a copy of them at this call site was a SECOND place the
        # answer could drift. Mutation measured that copy as equivalent,
        # which is exactly what a redundant predicate looks like right up
        # until someone edits one of the two.
        # ⚠️ The extension rule is NOT redundant and stays here. Removing it
        # was tried and measured: `load_conf_files` checks reserved, hidden
        # and `is_file` but NOT the extension (it is also fed
        # `conf.d/<basename>` strings by the `--git-diff` path, where the
        # extension was already decided upstream), so a `notes.txt` and a
        # `db-c.yml` went straight through and the operator-facing recipe
        # notice grew two tenants that do not exist:
        #   NOTE: 3 tenant(s) ... acme, ghost_txt, ghost_yml
        # `_confd_entries` lists, this line decides the extension,
        # `load_conf_files` decides reserved/hidden. One rule, one place.
        parsed_conf = load_conf_files(
            [str(p) for p in _confd_entries(Path(args.config_dir))
             if has_yaml_extension(p.name, (".yaml",))]
        )
    else:
        parsed_conf = {}
    recipe_tenants = find_custom_alert_tenants(parsed_conf)
    if recipe_tenants:
        print(custom_alert_notice(recipe_tenants), file=sys.stderr)

    # Check Prometheus availability
    if not prometheus_available(args.prometheus):
        if args.skip_if_unavailable:
            print("Prometheus unavailable — skipping backtest (--skip-if-unavailable)",
                  file=sys.stderr)
            if args.json:
                print(format_json_report(empty_report(
                    args.lookback, "skipped", "prometheus_unavailable")))
            if recipe_tenants and args.markdown_output:
                write_text_secure(args.markdown_output, custom_alert_markdown(recipe_tenants))
            sys.exit(EXIT_OK)
        else:
            print(f"ERROR: Prometheus not reachable at {args.prometheus}", file=sys.stderr)
            print("Use --skip-if-unavailable to exit gracefully", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    # Extract changes
    if args.git_diff:
        changes = keep_flat_threshold_changes(extract_changes_from_git_diff(), parsed_conf)
    elif args.config_dir:
        if not args.baseline:
            print("ERROR: --config-dir requires --baseline", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        changes = extract_changes_from_dirs(args.config_dir, args.baseline)
    elif args.tenant:
        if not args.metric or (args.old_value is None and args.new_value is None):
            print("ERROR: --tenant requires --metric and at least one of --old-value/--new-value",
                  file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        changes = [{
            "tenant": args.tenant,
            "metric": args.metric,
            "old_value": args.old_value,
            "new_value": args.new_value,
        }]
    else:
        print("ERROR: Specify --git-diff, --config-dir, or --tenant", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not changes:
        print("No threshold changes found.", file=sys.stderr)
        if args.json:
            print(format_json_report(empty_report(
                args.lookback, "no_changes", "no_threshold_changes_detected")))
        if recipe_tenants and args.markdown_output:
            write_text_secure(args.markdown_output, custom_alert_markdown(recipe_tenants))
        sys.exit(EXIT_OK)

    # Run backtests
    lookback_seconds = parse_lookback(args.lookback)
    results = []
    for change in changes:
        result = backtest_change(args.prometheus, change, lookback_seconds)
        results.append(result)

    # Generate report
    report = generate_report(results, args.lookback)

    # Output
    if args.json:
        print(format_json_report(report))
    else:
        print_text_report(report)

    if args.output:
        write_json_secure(args.output, report)
        if not args.json:
            print(f"  JSON report: {args.output}")

    if args.markdown_output:
        md = generate_markdown(report)
        if recipe_tenants:
            md += "\n\n" + custom_alert_markdown(recipe_tenants)
        write_text_secure(args.markdown_output, md)
        if not args.json:
            print(f"  Markdown report: {args.markdown_output}")

    # Exit with non-zero if HIGH risk changes found
    high_count = report["risk_summary"]["HIGH"]
    if high_count > 0 and not args.json:
        print(f"\n  WARNING: {high_count} HIGH risk change(s) — review before merging.")
    sys.exit(EXIT_VIOLATION if high_count > 0 else EXIT_OK)


if __name__ == "__main__":
    main()
