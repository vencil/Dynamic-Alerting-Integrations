"""Per-tenant values from Go: /metrics via da-guard served-values, /effective via da-guard effective.

What /metrics serves (#2115) and what tenant-api's /effective resolves
(#2564), each read from da-guard rather than re-derived in Python.

Semantics: `da-guard served-values` = /metrics, `da-guard effective` =
/effective. This module runs those subcommands and parses their JSON; it
decides nothing about the values itself.

`load_served_tree(conf_d, at=None, binary=None, schedules=False)` returns a `ServedTree`:
`tenants` is `load_served_values`'s answer, `skipped` the files the exporter's
load read but serves no tenant from (a file outside the `_` files with no
`tenants:` mapping, #2115 R3), each a `SkippedFile(file, reason)` in the words
of the load; `stderr_lines` every non-empty line da-guard wrote to stderr on
a run that succeeded (a successful run would otherwise swallow them), split on
"\n" only. They are not sorted into kinds here. `aliases` is the exporter's
own alias table, retired base key -> canonical base key (#2115): the keys of
`values` / `severities` / `schedules` / `dropped` are canonical, and a key a
caller holds in a retired spelling is looked up through this table, not
through one kept here. The exporter canonicalizes the `<base>_critical` and
`<base>{...}` spellings of a retired base the same way. `print_load_warnings` prints
both: each of those lines behind `DA_GUARD_PREFIX` and escaped with
`safe_label`, then one named WARN line per skipped file. A file the load
cannot stat or read (da-guard's `unreadable`) is not a warning: it raises
`ParseFailedError`, as a file that does not decode does.

What is passed on is da-guard's stderr line by line, never a faithful copy of
what the exporter meant: a file name holding a real "\n" is printed by Go as
two lines, and nothing here can tell those two from two messages. The prefix
is what keeps either of them off column 0.

`load_served_values(conf_d, at=None, binary=None, schedules=False)` returns
`{tenant_id: TenantValues}`:

* `values` — every key /metrics serves for the tenant, canonical spelling; a
  threshold's value is a float, a reserved key's value is what the
  exporter's resolver for it reads at `at` (default: now).
* `severities` — the severity label of each threshold key in `values`.
* `unserved` — keys the tenant's merged config carries with no entry in
  `values` (switched off included), value as written; plus the threshold
  keys it inherits from a subtree `_defaults.yaml` that the exporter cannot
  deliver (not declared at the root or in `optional_overrides:`; reserved
  keys, keys the exporter never serves as a threshold row such as
  `_silent_*` / `_state_*`, and switched-off keys excluded; #1976) — for
  those the value is the exporter's
  normalised rendering of the deepest threshold-shaped one, not as written.
* `dropped` — keys whose row /metrics drops because the exporter cannot build
  its series; key → the reason for each dropped row. `dropped` is keyed by the
  canonical spelling, `unserved` by the spelling as written.
* `schedules` — None unless `schedules=True` (da-guard `--schedules`: it
  resolves the tree once per part of the day in which some schedule changes,
  so it is asked for only by a caller that reads it). Then: the whole UTC day
  of every threshold key /metrics serves at
  some minute of it (or whose `expires:` the exporter honours), canonical
  spelling → `KeySchedule` (#2115 (c)): `segments`, each a
  `ScheduleSegment(start, end, value, severity, error)` — `"HH:MM"` to
  `"HH:MM"` (the last ends `"24:00"`), in order with no gap; `value` a float
  as in `values`, or None when the key has no /metrics row in that segment
  (`severity` None too). `error` is None except in a segment (never the one
  holding `at`) in which the exporter's /metrics cannot be gathered at all:
  then it is da-guard's text for that failure, and `value` / `severity` are
  None — nothing is served then, for any key. The segments are the exporter's reading, so a
  caller checking "every part of the day" reads them as they are and never
  reads the `overrides:` of the config itself. `expires` (as written) and
  `expired` (the exporter's verdict at `at`) are None unless the exporter
  honours a time-box on the key (base keys only); the segments already
  follow that verdict.

Whether /metrics serves at all follows Gather over the same collectors
production /metrics serves. A tree whose output would carry any string that
is not valid UTF-8 is refused (JSON cannot carry it), even where the exporter
only drops that row and /metrics still serves.

`load_effective(conf_d, binary=None)` returns `{tenant_id: TenantEffective}`,
one entry per tenant of the tree, each field tenant-api's /effective answer
for that tenant (`effective_config`, `merged_hash`, `source_file`,
`source_hash`, `defaults_chain`, `platform_overlay`, `profile_overlay`,
`warnings`) plus:

* `profile` — the profile the tenant is bound to, None for none.
* `key_sources` — per top-level key of `effective_config`, a `KeySource`:
  `layer` (`defaults` / `platform` / `profile` / `tenant`), `file` (relative
  to `conf_d`) and `level` (the index in `defaults_chain`, 0 = root; None
  outside the defaults layer). A mapping merged across layers names the
  highest layer that wrote any part of it. A key whose layer is `tenant` is
  one the tenant's own file writes (`file` is then `source_file`): the
  readers that act on a tenant's file (threshold-govern, #2116) act on
  those keys only, in the spelling `effective_config` keys them by, which
  is the one written.
* `not_served` — per key of `effective_config` whose value /metrics does not
  serve as shown, a `NotServedKey`: `reason` (da-guard effective's
  `not_served` reasons, e.g. `value_unparsed`, `window_invalid`) and `file`
  (relative to `conf_d`; "" when no single file applies). Empty when /metrics
  serves every key as shown. A da-guard whose effective document lacks the
  field is named as older than this tool (#2065). The keys da-guard's
  `value_not_served` refuses are those `value_not_served_as_written`
  selects.

`effective_config` is the JSON as /effective sends it: a YAML `.inf` / `.nan`
arrives as the text `"Infinity"` / `"-Infinity"` / `"NaN"`.

`load_effective_tree(conf_d, binary=None)` returns an `EffectiveTree`:
`tenants` is `load_effective`'s answer, `skipped` the files the walk takes no
tenant from — the same `SkippedFile` list, in the same words, as
`load_served_tree`'s (da-guard effective's `skipped`, #2115 R3), for a
caller that needs only /effective and so does not run served-values at all.

`binary` is the da-guard path; without it, `$DA_GUARD_BINARY`, then
`da-guard` on `$PATH` (the resolution `da-tools guard` uses).

Raises (both loaders):

* `ParseFailedError` (a `YamlFileError`, `_lib_io`) — the exporter's load
  skips a file that does not decode (`parse_failed`) or a path it cannot
  stat or read (`unreadable`: a dangling symlink, a file it may not read, a
  sub-directory it may not list; a symlink to a directory is not one), both
  loaders (`load_effective` since #2588: an unreadable tenant file would
  drop its tenants, an unreadable `_defaults.yaml` the values every tenant
  inherits from it); `path` names the first, the one-line message names all
  (an unreadable one with its reason, `stat_error` / `read_error` /
  `walk_error`), `unreadable` lists the unreadable ones, `stderr_lines`
  carries da-guard's stderr whole (the exporter's reasons among it).
  A tree from which the load keeps no file at all because every config
  file is unreadable (or its only entries are sub-directories it cannot
  list) raises `ParseFailedError` from both loaders too (#2627; before,
  `da-guard served-values` refused it with exit 2, "no .yaml files
  found"), and so does a `conf_d` that cannot be read itself, named in
  `unreadable` as `.` (`walk_error` when it cannot be listed, `stat_error`
  when it cannot be stat'ed) (#2627; before, exit 2 from all three
  subcommands). A tree with no config file at all, or a `conf_d`
  that does not exist, is still exit 2: `ServedValuesError` /
  `EffectiveError`.
* `DaGuardNotFoundError` — no da-guard binary; the message says how to get one.
* `ServedValuesError` / `EffectiveError` (both `DaGuardError`) — da-guard
  failed, or its output is not the JSON it should be (for `effective`, a
  `schema` other than `da-guard.effective/v1` included); carries the exit
  code and stderr. `binary_fault` says whether the binary failed rather
  than the tree (#2725): it could not be run, exited with a code the
  subcommand never uses, wrote output that is not its JSON (`broken`), or
  is older than this tool (`stale`) — a missing field, or exit 2 from a
  da-guard that does not accept the subcommand and flags, which the same
  argv plus `-h` asks it (no file is written, its stderr is not read).
  Exit 2 from a da-guard that accepts them is the tree's (no config file,
  a tree the exporter's load rejects).

#2115: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115
#2564: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, TextIO, TypeVar

_HERE = Path(__file__).resolve().parent
# guard_dispatch lives in ops/ in the repo; the image ships it flat beside
# this module. Appended, so nothing there shadows a module already found.
if (_HERE / "ops").is_dir() and str(_HERE / "ops") not in sys.path:
    sys.path.append(str(_HERE / "ops"))

from _lib_io import YamlFileError, safe_label  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_constants import (  # noqa: E402
    TOP_LEVEL_READ_ELSEWHERE, VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES,
)
import guard_dispatch  # noqa: E402

__all__ = [
    "DaGuardError",
    "DaGuardNotFoundError",
    "EffectiveError",
    "KeySchedule",
    "KeySource",
    "NotServedKey",
    "ScheduleSegment",
    "ServedTree",
    "ServedValuesError",
    "SkippedFile",
    "TenantEffective",
    "TenantValues",
    "UnreadableFile",
    "YamlFileError",
    "EffectiveTree",
    "DA_GUARD_PREFIX",
    "MISSING_BINARY_MESSAGE",
    "VALUE_NOT_SERVED_REASONS",
    "ParseFailedError",
    "canonical_key",
    "exit_on_served_values_error",
    "is_tenant_reserved_key",
    "load_effective",
    "load_effective_tree",
    "load_served_tree",
    "load_served_values",
    "print_load_error",
    "print_load_warnings",
    "value_not_served_as_written",
    "written_config",
]

SUBCOMMAND = "served-values"
EFFECTIVE_SUBCOMMAND = "effective"
# The `schema` value load_effective reads; any other is refused.
EFFECTIVE_SCHEMA = "da-guard.effective/v1"
_KEY_LAYERS = frozenset({"defaults", "platform", "profile", "tenant"})
# Seconds: generous for a large tree, and a hang still ends.
DEFAULT_TIMEOUT = 600

_EXIT_OK = 0
_EXIT_CALLER_ERR = 2  # da-guard's own: caller error, e.g. a flag it does not know
_EXIT_PARSE_FAILED = 3
_NON_FINITE = {"+Inf": float("inf"), "-Inf": float("-inf"), "NaN": float("nan")}


class ScheduleSegment(NamedTuple):
    start: str              # "HH:MM", UTC
    end: str                # "HH:MM", UTC; the day's last segment ends "24:00"
    value: float | None     # None: no /metrics row for the key in [start, end)
    severity: str | None    # None exactly when value is None
    error: str | None = None  # da-guard's text when /metrics cannot be gathered in [start, end)


class KeySchedule(NamedTuple):
    segments: list[ScheduleSegment]
    expires: str | None     # the time-box as written, when the exporter honours one
    expired: bool | None    # the exporter's verdict at `at`; None without a time-box


class TenantValues(NamedTuple):
    tenant_id: str
    values: dict[str, Any]
    severities: dict[str, str]
    unserved: dict[str, Any]
    dropped: dict[str, list[str]]
    schedules: dict[str, KeySchedule] | None  # None unless asked for (schedules=True)


class SkippedFile(NamedTuple):
    file: str    # root-relative slash path, as the exporter's scan keys it
    reason: str  # the load's own words


class ServedTree(NamedTuple):
    tenants: dict[str, TenantValues]
    skipped: list[SkippedFile]
    stderr_lines: list[str]  # every non-empty line of da-guard's stderr, as written
    aliases: dict[str, str]  # the exporter's table: retired base key -> canonical base key


class UnreadableFile(NamedTuple):
    file: str    # root-relative slash path (a directory's, for walk_error)
    reason: str  # closed set: "stat_error" | "read_error" | "walk_error"


class ParseFailedError(YamlFileError):
    """The exporter's load skips a file that does not decode (parse_failed)
    or that it cannot stat or read (unreadable).

    `str()` keeps `YamlFileError`'s one-line contract; `stderr_lines` is
    da-guard's stderr, every non-empty line as written, for a caller to show
    below that line; `unreadable` the unreadable files ([] when none)."""

    def __init__(self, path: str, cause: Exception, stderr_lines: list[str],
                 unreadable: list[UnreadableFile] | None = None) -> None:
        super().__init__(path, cause)
        self.stderr_lines = stderr_lines
        self.unreadable = list(unreadable or [])


class KeySource(NamedTuple):
    layer: str
    file: str
    level: int | None


class NotServedKey(NamedTuple):
    reason: str
    file: str


# The not_served reasons whose cause is a written threshold value (#2065) —
# pkg/config ValueNotServedAsWritten's set (spelling_duplicate: #2031).
VALUE_NOT_SERVED_REASONS = ("value_unparsed", "value_unparsed_dropped", "window_invalid", "value_rejected",
                            "spelling_duplicate")


def value_not_served_as_written(key: str, reason: str) -> bool:
    """Whether a not_served verdict on `key` is one da-guard's
    `value_not_served` refuses (#2065): one of VALUE_NOT_SERVED_REASONS on a
    threshold key (not `_`-prefixed). The Python copy of pkg/config
    ValueNotServedAsWritten; tests/ops/test_validate_config_values_not_served.py
    compares the two through da-guard."""
    return not key.startswith("_") and reason in VALUE_NOT_SERVED_REASONS


class TenantEffective(NamedTuple):
    tenant_id: str
    effective_config: dict[str, Any]
    key_sources: dict[str, KeySource]
    profile: str | None
    merged_hash: str
    source_file: str
    source_hash: str
    defaults_chain: list[str]
    platform_overlay: list[dict[str, Any]]
    profile_overlay: list[dict[str, Any]]
    warnings: list[str]
    not_served: dict[str, NotServedKey]


class EffectiveTree(NamedTuple):
    tenants: dict[str, TenantEffective]
    skipped: list[SkippedFile]  # as ServedTree.skipped: files the walk takes no tenant from


class DaGuardNotFoundError(FileNotFoundError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class DaGuardError(RuntimeError):
    """A da-guard subcommand failed. `returncode` and `stderr` are its own.
    `stale` is True when the failure is named as a da-guard older than this
    tool (its output lacks a field this tool reads, or it does not accept
    the subcommand and flags this tool runs) — the binary's fault, not the
    config tree's. `broken` is True when the binary cannot be run or does
    not answer as da-guard does (an exit code the subcommand never uses,
    output that is not its JSON) — the binary's fault too (#2725)."""

    def __init__(self, message: str, returncode: int | None, stderr: str,
                 stale: bool = False, broken: bool = False) -> None:
        self.message = message
        self.returncode = returncode
        self.stderr = stderr
        self.stale = stale
        self.broken = broken
        detail = stderr.strip()
        super().__init__(f"{message}: {detail}" if detail else message)

    @property
    def binary_fault(self) -> bool:
        """The da-guard binary failed, not the config tree (#2725)."""
        return self.stale or self.broken


class ServedValuesError(DaGuardError):
    """da-guard served-values failed."""


class EffectiveError(DaGuardError):
    """da-guard effective failed."""


def _stderr_text(b: bytes | str | None) -> str:
    """da-guard's stderr as text; bytes that are not UTF-8 are shown escaped."""
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode("utf-8", errors="backslashreplace")


# Every line of da-guard's stderr a tool passes on is printed behind this, on
# both paths (a successful run, and the lines below an ERROR), so none of it
# ever starts at column 0 — not even text after a "\n" inside a file name.
DA_GUARD_PREFIX = "  da-guard| "


def _nonempty_lines(text: str) -> list[str]:
    """Every non-empty line of `text`, split on "\n" ONLY — `str.splitlines`
    also splits on NEL, \v, \f, \x1c-\x1e and U+2028/2029, which can sit in a
    file name, and would cut the line there. NEL, \v, \f and \x1c-\x1e then
    reach `safe_label`, which prints each as `?`. U+2028/2029 are not control
    characters to `safe_label` and pass through as they are; they stay inside
    the line, behind `DA_GUARD_PREFIX`.
    Trailing whitespace (a "\r" too) is dropped, the leading indent kept (a
    continuation line of a Go error is indented)."""
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]


def _threshold(v: Any) -> float:
    """A threshold value from the JSON as a float: JSON's number (an int when
    it has no fraction), or the text da-guard writes for a value JSON has no
    number for."""
    if isinstance(v, str):
        if v not in _NON_FINITE:
            raise ValueError(f"not a threshold value: {v!r}")
        return _NON_FINITE[v]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"not a threshold value: {v!r}")
    return float(v)


_T = TypeVar("_T")


def _run_da_guard(
    subcommand: str,
    extra: list[str],
    conf_d: str | Path,
    binary: str | None,
    timeout: float,
    error: type[DaGuardError],
    schema: str | None,
    read: Callable[[dict[str, Any]], _T],
    reads_unreadable: bool = False,
) -> tuple[_T, int, str]:
    """Run `da-guard <subcommand> --config-dir <conf_d> <extra>` and return
    (`read(document)`, the exit code, stderr) — the one error contract both
    loaders share. `read` takes from the document what its caller reads
    (`tenants`, and for served-values `skipped`); a field it needs that is
    missing or not of its shape is output that is not the JSON it should be.

    Raises: a missing binary; an exit code other than 0 / 3; output that is
    not the JSON it should be (with `schema` given, a `schema` field of any
    other value included; a missing `skipped` / `unreadable` named as a
    da-guard older than this tool); `ParseFailedError` for a non-empty
    `parse_failed` or — with `reads_unreadable`, which makes `unreadable` a
    field the document must carry — `unreadable`; exit 3 with neither."""
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is None:
        raise DaGuardNotFoundError(dispatcher.binary_missing_message(binary).rstrip())

    cmd = [exe, subcommand, "--config-dir", str(conf_d), *extra]
    try:
        # Bytes, not text: stderr may carry a file name that is not UTF-8
        # (the exporter's load logs it as is).
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise error(f"da-guard {subcommand} did not finish within {timeout}s", None,
                    _stderr_text(e.stderr)) from e
    except OSError as e:
        # #2725: not executable, not a program this system runs, … — the binary.
        raise error(f"da-guard {subcommand} could not be run ({exe}): {e}", None, "",
                    broken=True) from e

    stderr = _stderr_text(proc.stderr)
    if proc.returncode == _EXIT_CALLER_ERR:
        # Exit 2 is da-guard's caller error AND a tree the exporter's load
        # rejects (no config file, a tenant declared twice, …). Which one is
        # asked of the binary, not of its stderr (#2725): the same argv plus
        # `-h` exits 0 only when it knows the subcommand and every flag.
        if not _accepts(cmd, timeout):
            raise error(
                f"da-guard {subcommand} exited {proc.returncode}, and this da-guard does not accept "
                f"`{subcommand}` with {' '.join(a for a in cmd[2:] if a.startswith('--'))} — it is "
                "older than this tool: upgrade or rebuild it", proc.returncode, stderr, stale=True)
        raise error(f"da-guard {subcommand} exited {proc.returncode}", proc.returncode, stderr)
    if proc.returncode not in (_EXIT_OK, _EXIT_PARSE_FAILED):
        raise error(f"da-guard {subcommand} exited {proc.returncode}, an exit code it never uses "
                    "(0, 2 or 3): this binary does not run as da-guard does",
                    proc.returncode, stderr, broken=True)
    try:
        doc = json.loads(proc.stdout.decode("utf-8"))
        if schema is not None and doc["schema"] != schema:
            raise ValueError(f"schema {doc['schema']!r}, this reader reads {schema!r}")
        parse_failed = list(doc["parse_failed"])
        got = read(doc)
        unreadable = ([UnreadableFile(str(e["file"]), str(e["reason"])) for e in doc["unreadable"]]
                      if reads_unreadable else [])
    except (ValueError, KeyError, TypeError) as e:  # UnicodeDecodeError is a ValueError
        stale = (" — this da-guard is older than this tool: upgrade or rebuild it"
                 if isinstance(e, KeyError) and e.args in (("skipped",), ("unreadable",)) else "")
        # Not the tree's either way: da-guard writes this JSON whatever the
        # tree holds (#2725).
        raise error(
            f"da-guard {subcommand} exited {proc.returncode} without the expected JSON ({e}){stale}",
            proc.returncode, stderr, stale=bool(stale), broken=not stale) from e

    if parse_failed or unreadable:
        clauses = []
        if parse_failed:
            clauses.append(f"the exporter's load skips {len(parse_failed)} file(s) that do not decode: "
                           f"{', '.join(parse_failed)}")
        if unreadable:
            clauses.append(f"the exporter's load cannot read {len(unreadable)} path(s): "
                           f"{', '.join(f'{u.file} ({u.reason})' for u in unreadable)}")
        first = parse_failed[0] if parse_failed else unreadable[0].file
        raise ParseFailedError(str(Path(conf_d) / first), ValueError("; ".join(clauses)),
                               _nonempty_lines(stderr), unreadable)
    if proc.returncode != _EXIT_OK:
        fields = "parse_failed or unreadable" if reads_unreadable else "parse_failed"
        raise error(
            f"da-guard {subcommand} exited {proc.returncode} with no file in {fields}",
            proc.returncode, stderr, broken=True)
    return got, proc.returncode, stderr


def _accepts(cmd: list[str], timeout: float) -> bool:
    """Whether the da-guard in `cmd[0]` knows the subcommand and every flag
    of `cmd`: Go's flag package stops at the first flag it does not know
    (exit 2), and `-h` after all of them is help (exit 0, in every
    da-guard that has the subcommand). A da-guard without the subcommand never parses
    `-h` (it reads the subcommand's name as a stray argument). Nothing is
    written; stderr is not read (#2725)."""
    try:
        proc = subprocess.run([*cmd, "-h"], capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == _EXIT_OK


GENERATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# strptime alone takes "2019-1-1T0:0:0Z" and a lower-case "z"; Go's RFC3339 does not.
_GENERATED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def now_at() -> str:
    """The current instant as `served-values --at` takes it (RFC3339, UTC)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime(GENERATED_AT_FORMAT)


def recorded_at(artifact: object) -> str | None:
    """The `generated` instant a generated JSON artifact recorded, or None.

    A `--check` re-reads served values at this instant rather than now: the
    exporter's answer depends on the time (maintenance expiry, scheduled
    thresholds), and a gate evaluated at "now" would turn red with no file
    changed (#2115 B4). The file is a snapshot as of that instant, so a
    conf.d edit that changes nothing at that instant does not turn it red.

    None (the caller falls back to now) when absent, not exactly
    GENERATED_AT_FORMAT as `served-values --at` parses it, or later than now:
    a `generated` moved into the future would otherwise pick the instant a
    stale file is checked at."""
    from datetime import datetime, timezone
    if not isinstance(artifact, dict):
        return None
    at = artifact.get("generated")
    if not isinstance(at, str) or not _GENERATED_AT_RE.fullmatch(at):
        return None
    try:
        when = datetime.strptime(at, GENERATED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if when > datetime.now(timezone.utc):
        return None
    return at


def load_served_values(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    schedules: bool = False,
) -> dict[str, TenantValues]:
    """`{tenant_id: TenantValues}` for the conf.d tree at `conf_d`, as
    /metrics serves it at `at` (RFC3339; None = now); with `schedules`, each
    tenant's whole day too. See the module docstring for the contract and
    the exceptions."""
    return load_served_tree(conf_d, at=at, binary=binary, timeout=timeout, schedules=schedules).tenants


def load_served_tree(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    schedules: bool = False,
) -> ServedTree:
    """`load_served_values`'s tenants plus the files the load serves no
    tenant from. Same arguments and exceptions."""
    extra = ["--at", at] if at is not None else []
    if schedules:
        extra.append("--schedules")
    # A da-guard from before --schedules refuses the flag with exit 2; that
    # is named as older than this tool by `_run_da_guard`'s `-h` check, not
    # by a phrase of its stderr (#2725).
    (tenants, skipped, aliases), returncode, stderr = _run_da_guard(
        SUBCOMMAND, extra, conf_d, binary, timeout, ServedValuesError, schema=None,
        read=lambda doc: (doc["tenants"],
                          [SkippedFile(str(e["file"]), str(e["reason"])) for e in doc["skipped"]],
                          doc.get("aliases")),
        reads_unreadable=True)
    # Read after the fields _run_da_guard checks, so an older da-guard is
    # named by the first field it lacks.
    if aliases is None:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {returncode} without the expected JSON ('aliases') — this "
            "da-guard is older than this tool: upgrade or rebuild it", returncode, stderr, stale=True)
    try:
        aliases = {str(k): str(v) for k, v in aliases.items()}
    except AttributeError as e:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {returncode} without the expected JSON (aliases: {e})",
            returncode, stderr, broken=True) from e

    out: dict[str, TenantValues] = {}
    for tenant_id, tv in tenants.items():
        severities = dict(tv["severities"])
        values = dict(tv["values"])
        try:
            for key in severities:
                values[key] = _threshold(values[key])
            for row in values.get("_custom_alerts") or []:
                row["value"] = _threshold(row["value"])
        except (ValueError, KeyError, TypeError) as e:
            raise ServedValuesError(
                f"da-guard {SUBCOMMAND}: tenant {tenant_id!r} carries a value that is not a threshold ({e})",
                returncode, stderr, broken=True) from e
        days: dict[str, KeySchedule] | None = None
        if schedules:
            if "schedules" not in tv:
                raise ServedValuesError(
                    f"da-guard {SUBCOMMAND}: tenant {tenant_id!r} has no schedules — this da-guard is "
                    "older than this tool: upgrade or rebuild it", returncode, stderr, stale=True)
            try:
                days = {k: _key_schedule(s) for k, s in tv["schedules"].items()}
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise ServedValuesError(
                    f"da-guard {SUBCOMMAND}: tenant {tenant_id!r} carries a schedule that is not of its shape ({e})",
                    returncode, stderr, broken=True) from e
        out[tenant_id] = TenantValues(tenant_id, values, severities, dict(tv["unserved"]),
                                     {k: list(v) for k, v in tv["dropped"].items()}, days)
    return ServedTree(out, skipped, _nonempty_lines(stderr), aliases)


def _key_schedule(s: dict[str, Any]) -> KeySchedule:
    """One key's `schedules` entry, as da-guard wrote it; only the value text
    of a non-finite number becomes a float, as in `values`."""
    segments = []
    for seg in s["segments"]:
        if "error" in seg:
            segments.append(ScheduleSegment(str(seg["from"]), str(seg["to"]), None, None,
                                            str(seg["error"])))
            continue
        value = seg["value"]
        segments.append(ScheduleSegment(
            str(seg["from"]), str(seg["to"]),
            None if value is None else _threshold(value),
            None if value is None else str(seg["severity"])))
    expired = s.get("expired")
    if expired is not None and not isinstance(expired, bool):
        raise ValueError(f"expired is not a boolean: {expired!r}")
    expires = s.get("expires")
    return KeySchedule(segments, None if expires is None else str(expires), expired)


def print_load_warnings(tree: ServedTree, stream: TextIO | None = None) -> None:
    """Every non-empty line da-guard wrote to stderr, each behind
    `DA_GUARD_PREFIX`, then one `WARN: <file>: <reason>` line per file the
    load serves no tenant from, in the load's order (#2115 R3). Every line is
    escaped for the terminal (`safe_label`): the file names in it come from
    the tree."""
    stream = sys.stderr if stream is None else stream
    for line in tree.stderr_lines:
        print(f"{DA_GUARD_PREFIX}{safe_label(line)}", file=stream)
    for s in tree.skipped:
        print(f"WARN: {safe_label(s.file)}: {s.reason}", file=stream)


_F = TypeVar("_F", bound=Callable[..., Any])


def exit_on_served_values_error(fn: _F) -> _F:
    """Decorate a CLI `main` so that da-guard missing or failing exits 2
    (`EXIT_CALLER_ERROR`) with one `ERROR:` line; da-guard's stderr follows,
    every non-empty line escaped and behind `DA_GUARD_PREFIX`.

    `ParseFailedError` gets the line `exit_on_yaml_file_error` prints
    (`ERROR: cannot read <path>: <message>`) with da-guard's stderr below it:
    that decorator prints `str()` only, which is one line by contract, so the
    exporter's reasons would be lost there. Any other `YamlFileError` is left
    to that decorator (or the tool's own handler). `EffectiveError` (a tool
    that also calls `load_effective`) is handled as `ServedValuesError` is.

    A missing binary gets this module's own text, not the dispatcher's: that
    one names `da-tools guard`'s `--da-guard-binary` flag, which the tools
    using this decorator do not have."""
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (DaGuardNotFoundError, ParseFailedError, DaGuardError) as exc:
            print_load_error(exc)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped  # type: ignore[return-value]


def print_load_error(exc: DaGuardNotFoundError | ParseFailedError | DaGuardError,
                     stream: TextIO | None = None) -> None:
    """The lines `exit_on_served_values_error` prints for `exc`, without the
    exit — for a tool that owes something more on that path (a `--json`
    envelope): one `ERROR:` line, then da-guard's stderr, every non-empty
    line escaped and behind `DA_GUARD_PREFIX`."""
    stream = sys.stderr if stream is None else stream
    if isinstance(exc, DaGuardNotFoundError):
        head, lines = _missing_binary_message(), []
    elif isinstance(exc, ParseFailedError):
        head, lines = f"cannot read {exc}", exc.stderr_lines
    else:
        head, lines = exc.message, _nonempty_lines(exc.stderr)
    # stderr unless the caller collects the lines itself (validate-config
    # puts them in its report row).
    error_line = f"ERROR: {safe_label(head)}"
    print(error_line, file=stream)
    for line in lines:
        print(f"{DA_GUARD_PREFIX}{safe_label(line)}", file=stream)


def _missing_binary_message() -> str:
    """`MISSING_BINARY_MESSAGE`, or — when `$DA_GUARD_BINARY` is set but names
    no file, which stops the resolution there — that path and that fact."""
    env_var = guard_dispatch.DISPATCHER.env_var
    set_to = os.environ.get(env_var, "").strip()  # as the dispatcher reads it
    if set_to and not os.path.isfile(set_to):
        return (f"da-guard binary not found: ${env_var} is set to '{safe_label(set_to)}', "
                f"and no file exists at that path. Point it at the da-guard binary, or unset "
                f"it to use da-guard on $PATH (the da-tools image ships it as "
                f"/usr/local/bin/da-guard; in a checkout of this repo, `make da-guard-build` "
                f"builds it to .build/da-guard).")
    return MISSING_BINARY_MESSAGE


MISSING_BINARY_MESSAGE = (
    "da-guard binary not found: this tool reads the tenants through da-guard "
    "(`da-guard effective` / `da-guard served-values`). Set $DA_GUARD_BINARY to its path, or put "
    "da-guard on $PATH (the da-tools image ships it as /usr/local/bin/da-guard; "
    "in a checkout of this repo, `make da-guard-build` builds it to .build/da-guard).")


def load_effective(
    conf_d: str | Path,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, TenantEffective]:
    """`{tenant_id: TenantEffective}` for every tenant of the conf.d tree at
    `conf_d`, as tenant-api's /effective resolves it. See the module docstring
    for the contract and the exceptions."""
    return _load_effective(conf_d, binary, timeout, reads_skipped=False).tenants


def load_effective_tree(
    conf_d: str | Path,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> EffectiveTree:
    """`load_effective`'s tenants plus the files the walk takes no tenant
    from. Same arguments and exceptions; a da-guard without `skipped` in its
    effective document is named as older than this tool (`load_effective`
    does not read the field, so it does not require it)."""
    return _load_effective(conf_d, binary, timeout, reads_skipped=True)


def _load_effective(conf_d: str | Path, binary: str | None, timeout: float,
                    reads_skipped: bool) -> EffectiveTree:
    (tenants, skipped), returncode, stderr = _run_da_guard(
        EFFECTIVE_SUBCOMMAND, [], conf_d, binary, timeout, EffectiveError, schema=EFFECTIVE_SCHEMA,
        read=lambda doc: (doc["tenants"],
                          [SkippedFile(str(e["file"]), str(e["reason"])) for e in doc["skipped"]]
                          if reads_skipped else []),
        reads_unreadable=True)
    out: dict[str, TenantEffective] = {}
    try:
        for tenant_id, t in tenants.items():
            if t["tenant_id"] != tenant_id:
                raise ValueError(f"entry {tenant_id!r} carries tenant_id {t['tenant_id']!r}")
            sources: dict[str, KeySource] = {}
            for key, ks in t["key_sources"].items():
                layer, level = ks["layer"], ks.get("level")
                if layer not in _KEY_LAYERS:
                    raise ValueError(f"tenant {tenant_id!r} key {key!r}: unknown layer {layer!r}")
                if (level is None) == (layer == "defaults"):
                    raise ValueError(f"tenant {tenant_id!r} key {key!r}: layer {layer!r} with level {level!r}")
                sources[key] = KeySource(layer, ks["file"], level)
            effective = dict(t["effective_config"])
            if "not_served" not in t:
                raise EffectiveError(
                    f"da-guard {EFFECTIVE_SUBCOMMAND}: tenant {tenant_id!r} has no `not_served` — "
                    "this da-guard is older than this tool: upgrade or rebuild it",
                    returncode, stderr, stale=True)
            not_served = {str(k): NotServedKey(str(v["reason"]), str(v.get("file") or ""))
                          for k, v in t["not_served"].items()}
            if set(sources) != set(effective):
                raise ValueError(f"tenant {tenant_id!r}: key_sources and effective_config differ on "
                                 f"{sorted(set(sources) ^ set(effective))!r}")
            out[tenant_id] = TenantEffective(
                tenant_id=tenant_id,
                effective_config=effective,
                key_sources=sources,
                profile=t["profile"],
                merged_hash=t["merged_hash"],
                source_file=t["source_file"],
                source_hash=t["source_hash"],
                defaults_chain=list(t["defaults_chain"]),
                platform_overlay=list(t.get("platform_overlay") or []),
                profile_overlay=list(t.get("profile_overlay") or []),
                warnings=list(t.get("warnings") or []),
                not_served=not_served,
            )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise EffectiveError(
            f"da-guard {EFFECTIVE_SUBCOMMAND}: an entry is not the shape this reader reads ({e})",
            returncode, stderr, broken=True) from e
    return EffectiveTree(out, skipped)


def is_tenant_reserved_key(key: str) -> bool:
    """A reserved key a tenant may carry: `VALID_RESERVED_KEYS` or a
    `VALID_RESERVED_PREFIXES` key (the Python copy of the exporter's list,
    pinned to Go by tests/shared/test_reserved_key_py_go_parity.py), minus
    the platform-level keys another reader takes from the top of a defaults
    file (`TOP_LEVEL_READ_ELSEWHERE`: `_routing_defaults`,
    `_routing_enforced`) that are not themselves tenant keys. `_policies`
    and any other root-only `_` key is not one."""
    if key in VALID_RESERVED_KEYS:
        return True
    return key.startswith(VALID_RESERVED_PREFIXES) and key not in TOP_LEVEL_READ_ELSEWHERE


_CRITICAL = "_critical"


def canonical_key(key: str, aliases: dict[str, str] | None) -> str:
    """A threshold key in the exporter's canonical spelling: a retired base
    key (also as `<base>_critical` / `<base>{...}`) through `aliases`
    (`ServedTree.aliases`, the exporter's own table). Reserved keys and keys
    not in the table are returned as they are."""
    if not aliases or key.startswith("_"):
        return key
    base, brace, dims = key.partition("{")
    if base in aliases:
        return aliases[base] + brace + dims
    if base.endswith(_CRITICAL) and base[:-len(_CRITICAL)] in aliases:
        return aliases[base[:-len(_CRITICAL)]] + _CRITICAL + brace + dims
    return key


_EMPTY_FILL = ("", [], {}, None)


def written_config(effective: TenantEffective, served: TenantValues) -> dict[str, Any]:
    """A tenant's config as written plus inherited, for a reader that judges
    what was WRITTEN (#2115 (c), the policy readers): `effective_config`
    (`da-guard effective`) with

    * only the reserved keys a tenant may carry (`is_tenant_reserved_key`):
      a root `_defaults.yaml` without a `defaults:` mapping is merged whole,
      so its `_policies` / `_routing_defaults` would otherwise read as the
      tenant's own;
    * `_metadata` from /metrics (`served.values["_metadata"]`), not from
      /effective, which drops `_metadata` at every level: /metrics inherits
      it shallowly (#2115 R4). The fields the exporter fills in empty (`""`,
      `[]`) are not written, and with none left there is no `_metadata`.

    Threshold keys are as written (a retired spelling stays retired)."""
    out: dict[str, Any] = {}
    for key, value in effective.effective_config.items():
        if not key.startswith("_") or (key != "_metadata" and is_tenant_reserved_key(key)):
            out[key] = value
    meta = served.values.get("_metadata")
    if isinstance(meta, dict):
        written = {k: v for k, v in meta.items()
                   if not any(type(v) is type(e) and v == e for e in _EMPTY_FILL)}
        if written:
            out["_metadata"] = written
    return out
