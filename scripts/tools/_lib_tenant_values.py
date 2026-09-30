"""Per-tenant values as the exporter's /metrics serves them (#2115) and as
tenant-api's /effective resolves them (#2564).

Semantics: `da-guard served-values` = /metrics, `da-guard effective` =
/effective. This module runs those subcommands and parses their JSON; it
decides nothing about the values itself.

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
  highest layer that wrote any part of it.

`effective_config` is the JSON as /effective sends it: a YAML `.inf` / `.nan`
arrives as the text `"Infinity"` / `"-Infinity"` / `"NaN"`.

`binary` is the da-guard path; without it, `$DA_GUARD_BINARY`, then
`da-guard` on `$PATH` (the resolution `da-tools guard` uses).

Raises (both loaders):

* `YamlFileError` (`_lib_io`) — the exporter's load skips a file that does
  not decode; `path` names the first, the message names all.
* `DaGuardNotFoundError` — no da-guard binary; the message says how to get one.
* `ServedValuesError` / `EffectiveError` (both `DaGuardError`) — da-guard
  failed, or its output is not the JSON it should be (for `effective`, a
  `schema` other than `da-guard.effective/v1` included); carries the exit
  code and stderr.

#2115: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115
#2564: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564
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
    "DaGuardError",
    "DaGuardNotFoundError",
    "EffectiveError",
    "KeySource",
    "ServedValuesError",
    "TenantEffective",
    "TenantValues",
    "YamlFileError",
    "load_effective",
    "load_served_values",
]

SUBCOMMAND = "served-values"
EFFECTIVE_SUBCOMMAND = "effective"
# The `schema` value load_effective reads; any other is refused.
EFFECTIVE_SCHEMA = "da-guard.effective/v1"
_KEY_LAYERS = frozenset({"defaults", "platform", "profile", "tenant"})
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


class KeySource(NamedTuple):
    layer: str
    file: str
    level: int | None


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


class DaGuardNotFoundError(FileNotFoundError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class DaGuardError(RuntimeError):
    """A da-guard subcommand failed. `returncode` and `stderr` are its own."""

    def __init__(self, message: str, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip()
        super().__init__(f"{message}: {detail}" if detail else message)


class ServedValuesError(DaGuardError):
    """da-guard served-values failed."""


class EffectiveError(DaGuardError):
    """da-guard effective failed."""


def _stderr_text(b: bytes | str | None) -> str:
    """da-guard's stderr as text; bytes that are not UTF-8 are shown escaped."""
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode("utf-8", errors="backslashreplace")


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


def _run_da_guard(
    subcommand: str,
    extra: list[str],
    conf_d: str | Path,
    binary: str | None,
    timeout: float,
    error: type[DaGuardError],
    schema: str | None,
) -> tuple[Any, int, str]:
    """Run `da-guard <subcommand> --config-dir <conf_d> <extra>` and return
    (the document's `tenants`, the exit code, stderr) — the one error contract
    both loaders share. A missing binary, an exit code other than 0 / 3,
    output that is not the JSON it should be (with `schema` given, a `schema`
    field of any other value included) and a non-empty `parse_failed` raise."""
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
        raise error(f"da-guard {subcommand} could not be run ({exe}): {e}", None, "") from e

    stderr = _stderr_text(proc.stderr)
    if proc.returncode not in (_EXIT_OK, _EXIT_PARSE_FAILED):
        raise error(f"da-guard {subcommand} exited {proc.returncode}", proc.returncode, stderr)
    try:
        doc = json.loads(proc.stdout.decode("utf-8"))
        if schema is not None and doc["schema"] != schema:
            raise ValueError(f"schema {doc['schema']!r}, this reader reads {schema!r}")
        parse_failed = list(doc["parse_failed"])
        tenants = doc["tenants"]
    except (ValueError, KeyError, TypeError) as e:  # UnicodeDecodeError is a ValueError
        raise error(
            f"da-guard {subcommand} exited {proc.returncode} without the expected JSON ({e})",
            proc.returncode, stderr) from e

    if parse_failed:
        raise YamlFileError(
            str(Path(conf_d) / parse_failed[0]),
            ValueError(f"the exporter's load skips {len(parse_failed)} file(s) that do not decode: "
                       f"{', '.join(parse_failed)}"))
    if proc.returncode != _EXIT_OK:
        raise error(
            f"da-guard {subcommand} exited {proc.returncode} with no file in parse_failed",
            proc.returncode, stderr)
    return tenants, proc.returncode, stderr


def load_served_values(
    conf_d: str | Path,
    at: str | None = None,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, TenantValues]:
    """`{tenant_id: TenantValues}` for the conf.d tree at `conf_d`, as
    /metrics serves it at `at` (RFC3339; None = now). See the module
    docstring for the contract and the exceptions."""
    extra = ["--at", at] if at is not None else []
    tenants, returncode, stderr = _run_da_guard(
        SUBCOMMAND, extra, conf_d, binary, timeout, ServedValuesError, schema=None)

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
                returncode, stderr) from e
        out[tenant_id] = TenantValues(tenant_id, values, severities, dict(tv["unserved"]),
                                     {k: list(v) for k, v in tv["dropped"].items()})
    return out


def load_effective(
    conf_d: str | Path,
    binary: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, TenantEffective]:
    """`{tenant_id: TenantEffective}` for every tenant of the conf.d tree at
    `conf_d`, as tenant-api's /effective resolves it. See the module docstring
    for the contract and the exceptions."""
    tenants, returncode, stderr = _run_da_guard(
        EFFECTIVE_SUBCOMMAND, [], conf_d, binary, timeout, EffectiveError, schema=EFFECTIVE_SCHEMA)
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
            )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise EffectiveError(
            f"da-guard {EFFECTIVE_SUBCOMMAND}: an entry is not the shape this reader reads ({e})",
            returncode, stderr) from e
    return out
