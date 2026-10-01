"""Per-tenant values as the exporter's /metrics serves them (#2115).

Semantics: `da-guard served-values` = /metrics. This module runs that
subcommand and parses its JSON; it decides nothing about the values itself.

`load_served_tree(conf_d, at=None, binary=None)` returns a `ServedTree`:
`tenants` is `load_served_values`'s answer, `skipped` the files the exporter's
load read but serves no tenant from (a file outside the `_` files with no
`tenants:` mapping, #2115 R3), each a `SkippedFile(file, reason)` in the words
of the load; `warnings` the `WARN:` lines da-guard wrote to stderr on a run
that succeeded (a file the load cannot read, a dangling symlink, `defaults:`
in a tenant file, ...), which a successful run would otherwise swallow.
`print_load_warnings` prints both: da-guard's WARN lines, then one named WARN line
per skipped file.

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

* `YamlFileError` (`_lib_io`) — the exporter's load skips a file that does
  not decode; `path` names the first, the message names all.
* `DaGuardNotFoundError` — no da-guard binary; the message says how to get one.
* `ServedValuesError` — da-guard failed, or its output is not the JSON it
  should be; carries the exit code and stderr.

#2115: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115
"""
from __future__ import annotations

import functools
import json
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
import guard_dispatch  # noqa: E402

__all__ = [
    "DaGuardNotFoundError",
    "ServedTree",
    "ServedValuesError",
    "SkippedFile",
    "TenantValues",
    "YamlFileError",
    "MISSING_BINARY_MESSAGE",
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
    warnings: list[str]  # da-guard's own `WARN:` lines, as written


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


# A `WARN:` line of da-guard's stderr: the load's logger writes it bare, the
# process-wide `log` (the resolvers) with its date/time prefix.
_WARN_LINE = re.compile(r"^(?:\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} )?WARN: ")
# da-guard's own closing line on exit 3; it repeats parse_failed.
_EXIT3_SUMMARY = re.compile(r"^da-guard: \d+ file\(s\) cannot be decoded: ")


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
        tenants = doc["tenants"]
    except (ValueError, KeyError, TypeError) as e:  # UnicodeDecodeError is a ValueError
        stale = (" — this da-guard is older than this tool: upgrade or rebuild it"
                 if isinstance(e, KeyError) and e.args == ("skipped",) else "")
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} without the expected JSON ({e}){stale}",
            proc.returncode, stderr) from e

    if parse_failed:
        # The exporter's own reasons (line, YAML error), minus da-guard's
        # closing line, which only repeats parse_failed.
        reasons = [ln.strip() for ln in stderr.splitlines()
                   if ln.strip() and not _EXIT3_SUMMARY.match(ln)]
        raise YamlFileError(
            str(Path(conf_d) / parse_failed[0]),
            ValueError(f"the exporter's load skips {len(parse_failed)} file(s) that do not decode: "
                       f"{', '.join(parse_failed)}"
                       + (f"; {'; '.join(reasons)}" if reasons else "")))
    if proc.returncode != _EXIT_OK:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} with no file in parse_failed",
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
    warnings = [ln for ln in stderr.splitlines() if _WARN_LINE.match(ln)]
    return ServedTree(out, skipped, warnings)


def print_load_warnings(tree: ServedTree, stream: TextIO | None = None) -> None:
    """da-guard's own `WARN:` lines (as written), then one
    `WARN: <file>: <reason>` line per file the load serves no tenant from, in
    the load's order (#2115 R3). Every line is escaped for the terminal
    (`safe_label`): the file names in it come from the tree."""
    stream = sys.stderr if stream is None else stream
    for line in tree.warnings:
        print(safe_label(line), file=stream)
    for s in tree.skipped:
        print(f"WARN: {safe_label(s.file)}: {s.reason}", file=stream)


_F = TypeVar("_F", bound=Callable[..., Any])


def exit_on_served_values_error(fn: _F) -> _F:
    """Decorate a CLI `main` so that da-guard missing or failing exits 2
    (`EXIT_CALLER_ERROR`) with an `ERROR:` line, as `exit_on_yaml_file_error`
    does for an unreadable file; da-guard's stderr follows, one escaped line
    per line. `YamlFileError` is left to that decorator (or the tool's own
    handler).

    A missing binary gets this module's own text, not the dispatcher's: that
    one names `da-tools guard`'s `--da-guard-binary` flag, which the tools
    using this decorator do not have."""
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except DaGuardNotFoundError:
            print(f"ERROR: {MISSING_BINARY_MESSAGE}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        except ServedValuesError as exc:
            print(f"ERROR: {safe_label(exc.message)}", file=sys.stderr)
            for line in exc.stderr.splitlines():
                if line.strip():
                    print(f"  {safe_label(line)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped  # type: ignore[return-value]


MISSING_BINARY_MESSAGE = (
    "da-guard binary not found: this tool reads the values through "
    "`da-guard served-values`. Set $DA_GUARD_BINARY to its path, or put "
    "da-guard on $PATH (the da-tools image ships it as /usr/local/bin/da-guard).")
