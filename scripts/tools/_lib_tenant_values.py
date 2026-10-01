"""Per-tenant values as the exporter's /metrics serves them (#2115).

Semantics: `da-guard served-values` = /metrics. This module runs that
subcommand and parses its JSON; it decides nothing about the values itself.

`load_served_tree(conf_d, at=None, binary=None)` returns a `ServedTree`:
`tenants` is `load_served_values`'s answer, `skipped` the files the exporter's
load read but serves no tenant from (a file outside the `_` files with no
`tenants:` mapping, #2115 R3), each a `SkippedFile(file, reason)` in the words
of the load; `stderr_lines` every non-empty line da-guard wrote to stderr on
a run that succeeded (a successful run would otherwise swallow them), split on
"\n" only. They are not sorted into kinds here. `print_load_warnings` prints
both: each of those lines behind `DA_GUARD_PREFIX` and escaped with
`safe_label`, then one named WARN line per skipped file. A file the load
cannot stat or read (da-guard's `unreadable`) is not a warning: it raises
`ParseFailedError`, as a file that does not decode does.

What is passed on is da-guard's stderr line by line, never a faithful copy of
what the exporter meant: a file name holding a real "\n" is printed by Go as
two lines, and nothing here can tell those two from two messages. The prefix
is what keeps either of them off column 0.

`load_served_values(conf_d, at=None, binary=None)` returns
`{tenant_id: TenantValues}`:

* `values` — every key /metrics serves for the tenant, canonical spelling; a
  threshold's value is a float, a reserved key's value is what the
  exporter's resolver for it reads at `at` (default: now).
* `severities` — the severity label of each threshold key in `values`.
* `unserved` — keys the tenant's merged config carries with no entry in
  `values` (switched off included), value as written.
* `dropped` — keys whose row /metrics drops because the exporter cannot build
  its series; key → the reason for each dropped row. `dropped` is keyed by the
  canonical spelling, `unserved` by the spelling as written.

Whether /metrics serves at all follows Gather over the same collectors
production /metrics serves. A tree whose output would carry any string that
is not valid UTF-8 is refused (JSON cannot carry it), even where the exporter
only drops that row and /metrics still serves.

`binary` is the da-guard path; without it, `$DA_GUARD_BINARY`, then
`da-guard` on `$PATH` (the resolution `da-tools guard` uses).

Raises:

* `ParseFailedError` (a `YamlFileError`, `_lib_io`) — the exporter's load
  skips a file that does not decode (`parse_failed`) or that it cannot stat
  or read (`unreadable`: a dangling symlink, a file it may not read; a
  symlink to a directory is not one); `path` names the first, the one-line
  message names all (an unreadable one with its reason, `stat_error` /
  `read_error`), `stderr_lines` carries da-guard's stderr whole (the
  exporter's reasons among it).
* `DaGuardNotFoundError` — no da-guard binary; the message says how to get one.
* `ServedValuesError` — da-guard failed, or its output is not the JSON it
  should be; carries the exit code and stderr.

#2115: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115
"""
from __future__ import annotations

import functools
import json
import os
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
import guard_dispatch  # noqa: E402

__all__ = [
    "DaGuardNotFoundError",
    "ServedTree",
    "ServedValuesError",
    "SkippedFile",
    "TenantValues",
    "UnreadableFile",
    "YamlFileError",
    "DA_GUARD_PREFIX",
    "MISSING_BINARY_MESSAGE",
    "ParseFailedError",
    "exit_on_served_values_error",
    "load_served_tree",
    "load_served_values",
    "print_load_warnings",
]

SUBCOMMAND = "served-values"
# Seconds: generous for a large tree, and a hang still ends.
DEFAULT_TIMEOUT = 600

_EXIT_OK = 0
_EXIT_PARSE_FAILED = 3
_NON_FINITE = {"+Inf": float("inf"), "-Inf": float("-inf"), "NaN": float("nan")}


class TenantValues(NamedTuple):
    tenant_id: str
    values: dict[str, Any]
    severities: dict[str, str]
    unserved: dict[str, Any]
    dropped: dict[str, list[str]]


class SkippedFile(NamedTuple):
    file: str    # root-relative slash path, as the exporter's scan keys it
    reason: str  # the load's own words


class ServedTree(NamedTuple):
    tenants: dict[str, TenantValues]
    skipped: list[SkippedFile]
    stderr_lines: list[str]  # every non-empty line of da-guard's stderr, as written


class UnreadableFile(NamedTuple):
    file: str    # root-relative slash path, as the exporter's scan keys it
    reason: str  # closed set: "stat_error" | "read_error"


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


class DaGuardNotFoundError(FileNotFoundError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class ServedValuesError(RuntimeError):
    """da-guard served-values failed. `returncode` and `stderr` are its own."""

    def __init__(self, message: str, returncode: int | None, stderr: str) -> None:
        self.message = message
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip()
        super().__init__(f"{message}: {detail}" if detail else message)


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


def load_served_values(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, TenantValues]:
    """`{tenant_id: TenantValues}` for the conf.d tree at `conf_d`, as
    /metrics serves it at `at` (RFC3339; None = now). See the module
    docstring for the contract and the exceptions."""
    return load_served_tree(conf_d, at=at, binary=binary, timeout=timeout).tenants


def load_served_tree(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ServedTree:
    """`load_served_values`'s tenants plus the files the load serves no
    tenant from. Same arguments and exceptions."""
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is None:
        raise DaGuardNotFoundError(dispatcher.binary_missing_message(binary).rstrip())

    cmd = [exe, SUBCOMMAND, "--config-dir", str(conf_d)]
    if at is not None:
        cmd += ["--at", at]
    try:
        # Bytes, not text: stderr may carry a file name that is not UTF-8
        # (the exporter's load logs it as is).
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ServedValuesError(f"da-guard {SUBCOMMAND} did not finish within {timeout}s", None,
                                _stderr_text(e.stderr)) from e
    except OSError as e:
        raise ServedValuesError(f"da-guard {SUBCOMMAND} could not be run ({exe}): {e}", None, "") from e

    stderr = _stderr_text(proc.stderr)
    if proc.returncode not in (_EXIT_OK, _EXIT_PARSE_FAILED):
        raise ServedValuesError(f"da-guard {SUBCOMMAND} exited {proc.returncode}", proc.returncode, stderr)
    try:
        doc = json.loads(proc.stdout.decode("utf-8"))
        parse_failed = list(doc["parse_failed"])
        skipped = [SkippedFile(str(e["file"]), str(e["reason"])) for e in doc["skipped"]]
        unreadable = [UnreadableFile(str(e["file"]), str(e["reason"])) for e in doc["unreadable"]]
        tenants = doc["tenants"]
    except (ValueError, KeyError, TypeError) as e:  # UnicodeDecodeError is a ValueError
        stale = (" — this da-guard is older than this tool: upgrade or rebuild it"
                 if isinstance(e, KeyError) and e.args in (("skipped",), ("unreadable",)) else "")
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} without the expected JSON ({e}){stale}",
            proc.returncode, stderr) from e

    if parse_failed or unreadable:
        clauses = []
        if parse_failed:
            clauses.append(f"the exporter's load skips {len(parse_failed)} file(s) that do not decode: "
                           f"{', '.join(parse_failed)}")
        if unreadable:
            clauses.append(f"the exporter's load cannot read {len(unreadable)} file(s): "
                           f"{', '.join(f'{u.file} ({u.reason})' for u in unreadable)}")
        first = parse_failed[0] if parse_failed else unreadable[0].file
        raise ParseFailedError(str(Path(conf_d) / first), ValueError("; ".join(clauses)),
                               _nonempty_lines(stderr), unreadable)
    if proc.returncode != _EXIT_OK:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} with no file in parse_failed or unreadable",
            proc.returncode, stderr)

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
                proc.returncode, stderr) from e
        out[tenant_id] = TenantValues(tenant_id, values, severities, dict(tv["unserved"]),
                                     {k: list(v) for k, v in tv["dropped"].items()})
    return ServedTree(out, skipped, _nonempty_lines(stderr))


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
    to that decorator (or the tool's own handler).

    A missing binary gets this module's own text, not the dispatcher's: that
    one names `da-tools guard`'s `--da-guard-binary` flag, which the tools
    using this decorator do not have."""
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except DaGuardNotFoundError:
            print(f"ERROR: {_missing_binary_message()}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        except ParseFailedError as exc:
            _print_error_with_stderr(f"cannot read {exc}", exc.stderr_lines)
        except ServedValuesError as exc:
            _print_error_with_stderr(exc.message, _nonempty_lines(exc.stderr))
    return _wrapped  # type: ignore[return-value]


def _print_error_with_stderr(head: str, stderr_lines: list[str]) -> None:
    print(f"ERROR: {safe_label(head)}", file=sys.stderr)
    for line in stderr_lines:
        print(f"{DA_GUARD_PREFIX}{safe_label(line)}", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def _missing_binary_message() -> str:
    """`MISSING_BINARY_MESSAGE`, or — when `$DA_GUARD_BINARY` is set but names
    no file, which stops the resolution there — that path and that fact."""
    env_var = guard_dispatch.DISPATCHER.env_var
    set_to = os.environ.get(env_var, "").strip()  # as the dispatcher reads it
    if set_to and not os.path.isfile(set_to):
        return (f"da-guard binary not found: ${env_var} is set to '{safe_label(set_to)}', "
                f"and no file exists at that path. Point it at the da-guard binary, or unset "
                f"it to use da-guard on $PATH (the da-tools image ships it as "
                f"/usr/local/bin/da-guard).")
    return MISSING_BINARY_MESSAGE


MISSING_BINARY_MESSAGE = (
    "da-guard binary not found: this tool reads the values through "
    "`da-guard served-values`. Set $DA_GUARD_BINARY to its path, or put "
    "da-guard on $PATH (the da-tools image ships it as /usr/local/bin/da-guard).")
