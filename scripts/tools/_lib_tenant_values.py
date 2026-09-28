"""Per-tenant values as the exporter's /metrics serves them (#2115).

Semantics: `da-guard served-values` = /metrics. This module runs that
subcommand and parses its JSON; it decides nothing about the values itself.

`load_served_values(conf_d, at=None, binary=None)` returns
`{tenant_id: TenantValues}`:

* `values` — every key /metrics serves for the tenant, canonical spelling; a
  threshold's value is a float, a reserved key's value is what the
  exporter's resolver for it reads at `at` (default: now).
* `severities` — the severity label of each threshold key in `values`.
* `unserved` — keys the tenant's merged config carries with no entry in
  `values` (switched off included), value as written.

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

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

_HERE = Path(__file__).resolve().parent
# guard_dispatch lives in ops/ in the repo; the image ships it flat beside
# this module. Appended, so nothing there shadows a module already found.
if (_HERE / "ops").is_dir() and str(_HERE / "ops") not in sys.path:
    sys.path.append(str(_HERE / "ops"))

from _lib_io import YamlFileError  # noqa: E402
import guard_dispatch  # noqa: E402

__all__ = [
    "DaGuardNotFoundError",
    "ServedValuesError",
    "TenantValues",
    "YamlFileError",
    "load_served_values",
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


class DaGuardNotFoundError(FileNotFoundError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class ServedValuesError(RuntimeError):
    """da-guard served-values failed. `returncode` and `stderr` are its own."""

    def __init__(self, message: str, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip()
        super().__init__(f"{message}: {detail}" if detail else message)


def _finite_or_text(v: Any) -> Any:
    return _NON_FINITE.get(v, v) if isinstance(v, str) else v


def load_served_values(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, TenantValues]:
    """`{tenant_id: TenantValues}` for the conf.d tree at `conf_d`, as
    /metrics serves it at `at` (RFC3339; None = now). See the module
    docstring for the contract and the exceptions."""
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is None:
        raise DaGuardNotFoundError(dispatcher.binary_missing_message(binary).rstrip())

    cmd = [exe, SUBCOMMAND, "--config-dir", str(conf_d)]
    if at is not None:
        cmd += ["--at", at]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise ServedValuesError(f"da-guard {SUBCOMMAND} did not finish within {timeout}s", None, stderr) from e
    except OSError as e:
        raise ServedValuesError(f"da-guard {SUBCOMMAND} could not be run ({exe}): {e}", None, "") from e

    if proc.returncode not in (_EXIT_OK, _EXIT_PARSE_FAILED):
        raise ServedValuesError(f"da-guard {SUBCOMMAND} exited {proc.returncode}", proc.returncode, proc.stderr)
    try:
        doc = json.loads(proc.stdout)
        parse_failed = list(doc["parse_failed"])
        tenants = doc["tenants"]
    except (ValueError, KeyError, TypeError) as e:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} without the expected JSON ({e})",
            proc.returncode, proc.stderr) from e

    if parse_failed:
        raise YamlFileError(
            str(Path(conf_d) / parse_failed[0]),
            ValueError(f"the exporter's load skips {len(parse_failed)} file(s) that do not decode: "
                       f"{', '.join(parse_failed)}"))
    if proc.returncode != _EXIT_OK:
        raise ServedValuesError(
            f"da-guard {SUBCOMMAND} exited {proc.returncode} with no file in parse_failed",
            proc.returncode, proc.stderr)

    out: dict[str, TenantValues] = {}
    for tenant_id, tv in tenants.items():
        severities = dict(tv["severities"])
        values = dict(tv["values"])
        for key in severities:
            values[key] = _finite_or_text(values[key])
        for row in values.get("_custom_alerts") or []:
            row["value"] = _finite_or_text(row["value"])
        out[tenant_id] = TenantValues(tenant_id, values, severities, dict(tv["unserved"]))
    return out
