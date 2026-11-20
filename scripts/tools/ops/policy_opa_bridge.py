#!/usr/bin/env python3
"""
policy_opa_bridge.py — OPA (Open Policy Agent) bridge for tenant config policy evaluation.

Converts tenant YAML configs to OPA input JSON format, evaluates via OPA (REST API or local binary),
and converts OPA responses back to PolicyResult / Violation format compatible with policy_engine.py.

Modes:
  --opa-url: Call OPA REST API (POST /v1/data/<package, dots as slashes>/violations)
  --policy-path: Path to .rego file(s) for local eval via `opa eval`
      (`--opa-binary`, default `opa`; query `data.<package>.violations`,
      input on stdin)
  --dry-run: Show input JSON without calling OPA

When OPA does not evaluate (#2724) — the server cannot be reached, answers
an HTTP error or a redirect (redirects are not followed: point `--opa-url`
at OPA's final address), or breaks the HTTP exchange; `opa` is missing,
fails or times out; the answer is not JSON; `<package>.violations` is
undefined (no policy under that package) — or when an item of `violations`
is not a violation object (a set of strings, `"severity": null`), the tool
exits 2 (EXIT_CALLER_ERROR) with one `ERROR:` line on stderr and nothing on
stdout, `--json` included. Each used to report `All policies passed.` rc 0.

When OPA did evaluate, every item is listed in the report; the run passes
when none is error-level. `--ci` exits 1 only on an error-level item, so a
`violations` holding only warnings is still rc 0 — a pass with warnings, by
design. An empty `violations` set is a pass.

OPA Input JSON format:
  {
    "tenants": {
      "tenant-a": { "mysql_connection_usage_percent": "70", "_routing": {...} },
      ...
    },
    "served": {
      "tenant-a": {
        "mysql_connection_usage_percent": [
          { "from": "00:00", "to": "24:00", "value": 70, "severity": "warning" } ],
        "mysql_connections": [
          { "from": "00:00", "to": "01:00", "value": 70, "severity": "warning" },
          { "from": "01:00", "to": "03:00", "value": null, "reason": "disabled" },
          { "from": "03:00", "to": "24:00", "value": 70, "severity": "warning" } ]
      },
      ...
    },
    "unserved": {
      "tenant-a": { "redis_memory": "disable" },
      ...
    },
    "defaults": { "defaults": { "mysql_connection_usage_percent": 80 } },
    "rule_packs": ["mariadb", "kubernetes"],
    "platform_version": "v2.3.0"
  }

`tenants` (#2115 0-B): every tenant of the tree, subdirectories included,
each the config as written PLUS what it inherits (`da-guard effective`'s
`effective_config`: `defaults:`, platform `tenants:`, profile, subtree
`_defaults.yaml`), in the same shape as before: values and keys as written
(a retired spelling stays retired). Only the reserved keys a tenant may
carry (a root-only key such as `_policies` / `_routing_defaults` is not the
tenant's), and `_metadata` as /metrics inherits it (/effective drops it)
minus the fields the exporter fills in empty. A default the exporter fills
in by itself is not there (`_lib_tenant_values.written_config`).

`served` (#2115 0-B, #4025): per tenant, every threshold /metrics serves
over the whole UTC day, keyed by the exporter's canonical spelling (aliases
resolved; `<key>_critical` for the critical row), each the day as a list of
segments (`KeySchedule.as_json`: `{from, to, value, severity}`; a segment
with no row has `value: null` and da-guard's `reason`) — one segment
`00:00`-`24:00` when the value does not change, never a bare number, so a
policy reads one shape. The same tree gives the same `served` at any hour:
only an `expires:` is judged at `--at` (default now; ADR-038), and a key
time-boxed so is named on stderr with the instant its answer holds until.
Which keys are there is da-guard's answer (served in some part of the day,
or time-boxed). A part of the day /metrics cannot be gathered in is exit 2,
naming it: nothing is known of any key there. A policy on a threshold's VALUE
reads it here, every segment: it is the number that alerts. A rego guards
`value != null` before comparing (OPA orders null below every number). A file the exporter drops or cannot
read is exit 2, as is a tree da-guard refuses; a file it reads but serves no
tenant from is a WARN line.

`unserved` (#4108): per tenant, the threshold keys its config carries
(written or inherited, root `defaults:` dimensional defaults, keys the
build cannot deliver) that /metrics serves at no minute of the day —
da-guard's `unserved_thresholds`, keyed and valued as written, like
`tenants` (a retired spelling stays retired), `_` keys not included. It
tells "configured but never served" (in `unserved`) from "not configured"
(in neither): a key served in some part of the day is in `served`, not
here. Only an `expires:` is judged at `--at`, as for `served`.

`_routing` in `tenants` is as written plus inherited, NOT the route
generator's resolution (#2724): `_routing_defaults` is not applied and
`{{tenant}}` is not substituted, so a tenant that relies on the defaults
alone has no `_routing` here. `evaluate-policy` reads the resolved routing.

`defaults` (#2724): the root defaults carrier's top-level keys as written,
minus the `_`-prefixed ones — NOT unwrapped. The usual carrier writes its
thresholds under `defaults:`, so a rego reads
`input.defaults.defaults.mysql_connection_usage_percent`; `state_filters:` and any other
top-level key come along beside it. The thresholds a tenant actually gets
are in `served`.

OPA Response format (REST; `opa eval` nests the same list under
`result[0].expressions[0].value`):
  { "result": [{"msg": "...", "severity": "error|warning", "tenant": "...", "field": "..."}] }

Violation conversion:
  OPA "severity": "error|warning" → Violation "level": "ERROR|WARNING"
  OPA "msg" → Violation "message"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Optional
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_validation import url_type  # noqa: E402  (#3528)
# ADR-036 step 4 (#2906): the root defaults carrier is parse-tree's choice
# (`defaults_carriers`) and its content parse-tree's yaml.v3 reading.
import _lib_input  # noqa: E402  (#3902)
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402

# ---------------------------------------------------------------------------
# Repo-layout import compatibility
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from _lib_python import (
        detect_cli_lang,
        exit_on_yaml_file_error,
    )
except ImportError:
    from scripts.tools._lib_python import (  # type: ignore[no-redef]
        detect_cli_lang,
        exit_on_yaml_file_error,
    )
from _lib_tenant_values import (  # noqa: E402  (#2115 0-B)
    exit_on_served_values_error,
    load_whole_day,
)
# A file parse-tree cannot read raises YamlFileError (rc 2 via
# exit_on_yaml_file_error, naming it); no usable parse-tree is rc 2 too.
from _lib_io import load_confd_file  # noqa: E402

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    """OPA policy violation."""
    tenant: str
    level: str  # ERROR or WARNING
    message: str
    field: str


@dataclass
class PolicyResult:
    """OPA policy evaluation result."""
    violations: list[Violation] = field(default_factory=list)
    tenants_evaluated: int = 0
    policy_package: str = ""

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.level == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.level == "WARNING")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
class TenantInputs(NamedTuple):
    """`load_tenant_inputs`' answer: the input's `tenants`, `served` and
    `unserved` (see the module docstring)."""
    tenants: dict[str, dict]
    served: dict[str, dict]
    unserved: dict[str, dict]


def load_tenant_inputs(config_dir: str, at: Optional[str] = None) -> TenantInputs:
    """`tenants`, `served` and `unserved` for the OPA input (#2115 0-B,
    #4025, #4108; see the module docstring): `tenants` from `da-guard
    effective` with `_metadata` from served-values' facts, `served` and
    `unserved` the whole day from `da-guard served-values --schedules-only`
    (*at*: the instant an `expires:` is judged at, None = now) — nothing
    read at one instant of the day. Prints
    da-guard's stderr, one WARN per file the load serves no tenant from, and
    one NOTE per time-boxed key not yet expired. Raises what the loads raise,
    and `ServedValuesError` when a part of the day cannot be gathered
    (`ungathered_refusal`) or the readings disagree on the tenants; `main`
    turns each into exit 2 (`exit_on_served_values_error`)."""
    whole = load_whole_day(config_dir, at=at)
    days, tenants = whole.days, whole.tenants
    served = {t: {k: day.as_json() for k, day in sorted(d.items())}
              for t, d in sorted(days.tenants.items())}
    for t, d in sorted(days.tenants.items()):
        for k, day in sorted(d.items()):
            if day.expires is not None and day.expired is False:
                print(f"NOTE: {safe_label(t)}: {safe_label(k)} is time-boxed by "
                      f"`expires: {safe_label(day.expires)}`; its served day holds until then",
                      file=sys.stderr)
    # load_whole_day refused a day with an ungathered part, the only one
    # whose lists are null.
    unserved = {t: dict(sorted(u.items())) for t, u in sorted(days.unserved_thresholds.items())}
    return TenantInputs(tenants, served, unserved)


def load_defaults(config_dir: str) -> dict[str, Any]:
    """Load the root defaults carrier.

    #1674: the carrier the exporter reads (any casing, `.yaml` over `.yml`),
    not a hard-coded `_defaults.yaml`. ADR-036 step 4 (#2906): which file
    that is, and what it holds, is `da-guard parse-tree`'s answer
    (`defaults_carriers`, yaml.v3) — an unquoted `yes` is the string "yes"
    here as it is for the exporter, and a `!!timestamp` is its text (OPA's
    input is JSON). A file parse-tree cannot read raises YamlFileError; no
    usable parse-tree raises ParseTreeError (rc 2 in `main`).
    """
    reader = _lib_parse_cache.reader_for(config_dir)
    tree = reader.tree()
    # #2115 0-B: the tenants come from the whole tree (`load_tenant_inputs`),
    # so only the nested carriers `input.defaults` leaves out are named.
    _lib_parse_cache.warn_skipped_nested_carriers(
        tree, tool="policy_opa_bridge",
        note=("`input.defaults` comes from the root defaults carrier only "
              "(tenants in subdirectories are in `input.tenants` / "
              "`input.served`), so it leaves out these"))
    rel = _lib_parse_cache.root_defaults_carrier(tree)
    if rel is None:
        return {}
    data = load_confd_file(config_dir, os.path.join(config_dir, rel), default={},
                           reader=reader)
    return _jsonable(data) if isinstance(data, dict) else {}


def _jsonable(value: Any) -> Any:
    """*value* with every `!!timestamp` (`_lib_parse_tree.Timestamp`) as its
    source text: OPA's input is JSON."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, _lib_parse_tree.Timestamp):
        return value.text
    return value


# ---------------------------------------------------------------------------
# OPA input builder
# ---------------------------------------------------------------------------
def build_opa_input(
    config_dir: str,
    tenant_configs: dict[str, dict],
    defaults: dict,
    rule_packs: Optional[list[str]] = None,
    platform_version: str = "v2.3.0",
    served: Optional[dict[str, dict]] = None,
    unserved: Optional[dict[str, dict]] = None,
) -> dict[str, Any]:
    """Build OPA input JSON from tenant configs.

    Args:
        config_dir: config directory path
        tenant_configs: {tenant_name: config_dict}
        defaults: defaults dict (usually from _defaults.yaml)
        rule_packs: list of rule pack names
        platform_version: platform version string
        served: {tenant_name: {threshold_key: [segment, ...]}}, what /metrics
            serves over the whole day (`load_tenant_inputs`, #4025); `{}` when
            omitted
        unserved: {tenant_name: {threshold_key: value as written}}, the
            keys served at no minute of the day (`load_tenant_inputs`,
            #4108); `{}` when omitted

    Returns:
        OPA input dict ready for JSON serialization
    """
    # Extract threshold defaults (non-underscore keys)
    defaults_thresholds = {
        k: v for k, v in defaults.items()
        if not k.startswith("_")
    }

    # Detect rule packs from config filenames
    if rule_packs is None:
        rule_packs = []

    return {
        "tenants": tenant_configs,
        "served": served if served is not None else {},
        "unserved": unserved if unserved is not None else {},
        "defaults": defaults_thresholds,
        "rule_packs": rule_packs,
        "platform_version": platform_version,
    }


# ---------------------------------------------------------------------------
# OPA evaluation
# ---------------------------------------------------------------------------
OPA_TIMEOUT_SECONDS = 10


class OpaEvalError(Exception):
    """OPA did not evaluate the policy (#2724): unreachable, failed, timed
    out, answered something that is not the `violations` list, or has no
    `violations` rule under the package. `main` prints the message as one
    `ERROR:` line and exits 2 — before #2724 each of these was swallowed into
    `[]` and reported as `All policies passed.` rc 0, `--ci` included."""


def package_url_path(package: str) -> str:
    """`--policy-package` as the REST path under `/v1/data/`: OPA's data API
    separates the package's segments with `/`, so the default
    `dynamic_alerting.policy` must become `dynamic_alerting/policy` — sent as
    written, OPA looks up one key named `dynamic_alerting.policy`, finds
    nothing and answers `{}` (#2724). A package already written with `/`
    passes unchanged."""
    return package.strip("./").replace(".", "/")


def package_query(package: str) -> str:
    """`--policy-package` as the `opa eval` query for its `violations` rule:
    `data.<package>.violations`, either separator accepted."""
    return f"data.{package.strip('./').replace('/', '.')}.violations"


def _violations_list(value: Any, where: str) -> list:
    """`value` is what OPA answered for `<package>.violations`; a rule that is
    not a set or array of violations cannot be reported on."""
    if not isinstance(value, list):
        raise OpaEvalError(
            f"{where}: `violations` is a {type(value).__name__}, not a set or array of "
            "violations")
    return value


def _undefined(package: str) -> OpaEvalError:
    return OpaEvalError(
        f"`{package_query(package)}` is undefined in OPA (no `violations` rule under "
        f"package {package!r}, or it produced no value): no policy was evaluated — "
        "check --policy-package and that the policy is loaded")


def _first_lines(text: str, limit: int = 5) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    more = f" (+{len(lines) - limit} more lines)" if len(lines) > limit else ""
    return " / ".join(lines[:limit]) + more


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect (#2724). urllib re-sends a 301/302/303'd POST as
    a GET without its body, so OPA would evaluate an empty input and answer
    `[]` — a pass; it does not follow a POST's 307/308 at all. Refusing all
    of them gives one behaviour: the 3xx is raised as HTTPError (returning
    None makes urllib do that), which `call_opa_rest` reports as exit 2."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = build_opener(_NoRedirect)


def urlopen(req: Request, timeout: float):
    """`urllib.request.urlopen` without following redirects (`_NoRedirect`)."""
    return _OPENER.open(req, timeout=timeout)  # nosec B310  #see call_opa_rest


def _eval_errors(stdout: str, limit: int = 3) -> str:
    """`opa eval --format json`'s `{"errors": [...]}` as one line
    (`code: message (file:row)`); `""` when stdout is not that shape."""
    try:
        errors = json.loads(stdout).get("errors")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(errors, list):
        return ""
    parts = []
    for err in errors[:limit]:
        if not isinstance(err, dict):
            continue
        loc = err.get("location") if isinstance(err.get("location"), dict) else {}
        where = f" ({loc.get('file', '')}:{loc.get('row', '')})" if loc else ""
        parts.append(f"{err.get('code', 'error')}: {err.get('message', '')}{where}")
    more = f" (+{len(errors) - limit} more)" if len(errors) > limit else ""
    return "; ".join(parts) + more


def call_opa_rest(
    opa_url: str,
    package: str,
    input_data: dict,
) -> list[dict]:
    """Call OPA REST API.

    Args:
        opa_url: Base OPA URL (e.g., http://localhost:8181)
        package: OPA package path (e.g., dynamic_alerting.policy)
        input_data: OPA input dict

    Returns:
        List of violation dicts from OPA result

    Raises:
        OpaEvalError: OPA did not evaluate (see the class)
    """
    endpoint = f"{opa_url.rstrip('/')}/v1/data/{package_url_path(package)}/violations"

    request_body = json.dumps({"input": input_data}).encode("utf-8")
    try:
        req = Request(endpoint, data=request_body, method="POST")  # nosec B310  #operator-supplied internal OPA URL
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=OPA_TIMEOUT_SECONDS) as response:  # nosec B310  #see Request line above
            raw = response.read()
    except HTTPError as e:
        if 300 <= e.code < 400:
            # `_NoRedirect` refuses every redirect (see its docstring).
            location = e.headers.get("Location", "") if e.headers else ""
            raise OpaEvalError(
                f"OPA API call failed: POST {endpoint}: HTTP {e.code} redirect"
                + (f" to {location}" if location else "")
                + " (redirects are not followed; point --opa-url at OPA's final address)"
            ) from e
        detail = ""
        try:
            detail = _first_lines(e.read().decode("utf-8", "replace"), 3)
        except (OSError, HTTPException):
            pass
        raise OpaEvalError(
            f"OPA API call failed: POST {endpoint}: HTTP {e.code}"
            + (f": {detail}" if detail else "")) from e
    except (OSError, ValueError, HTTPException) as e:
        # URLError, refused, timeout, bad URL; HTTPException: a malformed
        # status line (BadStatusLine), a body cut short (IncompleteRead), ...
        raise OpaEvalError(
            f"OPA API call failed: POST {endpoint}: {type(e).__name__}: {e}") from e

    where = f"OPA response from {endpoint}"
    try:
        response_data = json.loads(raw.decode("utf-8"))
    except ValueError as e:  # JSONDecodeError, UnicodeDecodeError
        raise OpaEvalError(f"{where} is not JSON: {e}") from e
    if not isinstance(response_data, dict):
        raise OpaEvalError(f"{where} is not a JSON object")
    if "result" not in response_data:
        raise _undefined(package)
    return _violations_list(response_data["result"], where)


def call_opa_binary(
    opa_binary: str,
    policy_path: str,
    package: str,
    input_data: dict,
) -> list[dict]:
    """Call local OPA binary via subprocess.

    `opa eval --format json -d <policy_path> --stdin-input
    data.<package>.violations`, the input JSON on stdin (#2724: the old
    command passed it after `-I`, which takes no value, so OPA saw a second
    query and refused every run).

    Args:
        opa_binary: Path to opa binary (default: 'opa')
        policy_path: Path to .rego file(s)
        package: OPA package path
        input_data: OPA input dict

    Returns:
        List of violation dicts from OPA result

    Raises:
        OpaEvalError: OPA did not evaluate (see the class)
    """
    cmd = [
        opa_binary,
        "eval",
        "--format", "json",
        "-d", policy_path,
        "--stdin-input",
        package_query(package),
    ]

    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(input_data),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=OPA_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as e:
        raise OpaEvalError(f"OPA binary not found: {opa_binary}") from e
    except subprocess.TimeoutExpired as e:
        raise OpaEvalError(
            f"OPA eval timed out after {OPA_TIMEOUT_SECONDS}s: {opa_binary}") from e
    except OSError as e:  # not executable, ...
        raise OpaEvalError(f"OPA binary could not run: {opa_binary}: {e}") from e

    if result.returncode != 0:
        # `--format json` puts evaluation errors on stdout, usage errors on stderr.
        detail = _eval_errors(result.stdout) or _first_lines(result.stderr) \
            or _first_lines(result.stdout)
        raise OpaEvalError(f"OPA eval failed (rc {result.returncode}): {detail}")

    where = f"`{opa_binary} eval` output"
    try:
        response_data = json.loads(result.stdout)
    except ValueError as e:
        raise OpaEvalError(f"{where} is not JSON: {e}") from e
    if not isinstance(response_data, dict):
        raise OpaEvalError(f"{where} is not a JSON object")
    if "result" not in response_data:
        raise _undefined(package)
    try:
        value = response_data["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError, TypeError) as e:
        raise OpaEvalError(
            f"{where} has no result[0].expressions[0].value: {_first_lines(result.stdout, 2)}"
        ) from e
    return _violations_list(value, where)


# ---------------------------------------------------------------------------
# OPA response conversion
# ---------------------------------------------------------------------------
def convert_opa_violations(
    opa_violations: list[dict],
    tenants_count: int,
) -> PolicyResult:
    """Convert OPA response to PolicyResult.

    Args:
        opa_violations: List of violation dicts from OPA
        tenants_count: Number of tenants evaluated

    Returns:
        PolicyResult with converted violations

    Raises:
        OpaEvalError: an item is not a violation object, or its `severity`
            is not a string (#2724: such items used to be skipped, so a
            `violations contains msg if {...}` set of strings, or
            `"severity": null`, reported a pass).

    A string `severity` is compared case-insensitively, ASCII only:
    `warning` in any ASCII case is warning-level; every other string
    (`warnıng` with a dotless ı included), and a missing `severity`, is
    error-level. Missing `tenant` / `msg` / `field` get their defaults.
    """
    result = PolicyResult(
        tenants_evaluated=tenants_count,
        policy_package="dynamic_alerting.policy",
    )

    expected = ('an object {"msg": ..., "severity": "error"|"warning", '
                '"tenant": ..., "field": ...}')
    for i, v in enumerate(opa_violations):
        if not isinstance(v, dict):
            raise OpaEvalError(
                f"violations item {i} is not {expected}: got JSON {_json_type(v)} "
                f"{_preview(v)}")
        severity = v.get("severity", "error")
        if not isinstance(severity, str):
            raise OpaEvalError(
                f"violations item {i} has a severity that is not a string: got JSON "
                f"{_json_type(severity)} {_preview(severity)}; expected {expected}")
        # ASCII-only case folding: `str.upper()` maps "warnıng" (U+0131) to
        # "WARNING", which would turn an error into a warning (#2724).
        level = ("WARNING" if severity.isascii() and severity.lower() == "warning"
                 else "ERROR")
        result.violations.append(Violation(
            tenant=str(v.get("tenant", "unknown")),
            level=level,
            message=str(v.get("msg", "Policy violation")),
            field=str(v.get("field", "")),
        ))

    return result


def _json_type(value: Any) -> str:
    """The JSON name of `value`'s type, for an error message."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return {str: "string", list: "array", dict: "object"}.get(type(value), type(value).__name__)


def _preview(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_text_report(result: PolicyResult, lang: str = "en") -> str:
    """Generate text report."""
    lines: list[str] = []

    if lang == "zh":
        lines.append("═══ OPA 策略評估報告 ═══")
        lines.append(f"租戶數: {result.tenants_evaluated}")
        lines.append(f"錯誤: {result.error_count} | 警告: {result.warning_count}")
    else:
        lines.append("═══ OPA Policy Evaluation Report ═══")
        lines.append(f"Tenants: {result.tenants_evaluated}")
        lines.append(f"Errors: {result.error_count} | Warnings: {result.warning_count}")

    if not result.violations:
        lines.append("")
        lines.append("✓ All policies passed." if lang == "en"
                     else "✓ 所有策略均通過。")
        return "\n".join(lines)

    lines.append("")

    # Group by tenant
    by_tenant: dict[str, list[Violation]] = {}
    for v in result.violations:
        by_tenant.setdefault(v.tenant, []).append(v)

    for tenant in sorted(by_tenant):
        lines.append(f"[{safe_label(tenant)}]")
        for v in by_tenant[tenant]:
            icon = "✗" if v.level == "ERROR" else "⚠"
            lines.append(f"  {icon} [{safe_label(v.level)}] "
                         f"{safe_label(v.field)}: {safe_label(v.message)}")
        lines.append("")

    status = "FAIL" if not result.passed else "PASS"
    lines.append(f"Result: {status}" if lang == "en"
                 else f"結果: {status}")

    return "\n".join(lines)


def generate_json_report(result: PolicyResult) -> dict:
    """Generate JSON report."""
    return {
        "tenants_evaluated": result.tenants_evaluated,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "passed": result.passed,
        "violations": [
            {
                "tenant": v.tenant,
                "level": v.level,
                "message": v.message,
                "field": v.field,
            }
            for v in result.violations
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#: The parser's text per language (#3902): ONE parser, so each argument —
#: its type, default and declaration — exists once and the two languages
#: cannot drift apart.
_HELP = {
    "en": {
        "description": "OPA (Open Policy Agent) bridge — convert tenant configs to OPA input "
                       "and evaluate.",
        "config_dir": "Path to conf.d/ directory containing tenant YAML files",
        "opa_url": "OPA REST API URL (e.g., http://localhost:8181)",
        "opa_binary": "Path to opa binary (default: opa)",
        "policy_package": "OPA package path (default: dynamic_alerting.policy)",
        "policy_path": "Path to .rego file(s) for local evaluation",
        "json": "Output in JSON format",
        "ci": "CI mode: exit 1 if any error-level violations found",
        "dry_run": "Show OPA input JSON only, do not call OPA",
        "at": "The instant an `expires:` is judged at (default: now); `served` is "
              "read over the whole UTC day. da-guard judges the value",
    },
    "zh": {
        "description": "OPA (Open Policy Agent) 策略評估橋接 — 將 tenant 配置轉換為 OPA 輸入並評估。",
        "config_dir": "conf.d/ 目錄路徑（含 tenant YAML）",
        "opa_url": "OPA REST API URL（例：http://localhost:8181）",
        "opa_binary": "OPA 二進檔路徑（預設：opa）",
        "policy_package": "OPA 套件路徑（預設：dynamic_alerting.policy）",
        "policy_path": ".rego 檔案路徑（用於本地評估）",
        "json": "JSON 格式輸出",
        "ci": "CI 模式：有 error 級違規時 exit 1",
        "dry_run": "僅顯示 OPA 輸入 JSON，不呼叫 OPA",
        "at": "判斷 `expires:` 是否過期的時刻（預設：現在）；`served` 一律讀整個 UTC 日。"
              "值由 da-guard 判",
    },
}


def build_parser(lang: str = "en") -> argparse.ArgumentParser:
    """Build CLI argument parser (its text in *lang*, `_HELP`)."""
    h = _HELP["zh" if lang == "zh" else "en"]
    parser = argparse.ArgumentParser(description=h["description"])
    # #3902: a declared input, judged once in `main` (`declared_trees`); a
    # tree with no config file is refused (da-guard served-values refuses it).
    _lib_input.add_input(parser, "--config-dir", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED, required=True,
                         help=h["config_dir"])
    parser.add_argument("--opa-url", type=url_type(empty="refuse"),  # #3528
                        help=h["opa_url"])
    parser.add_argument("--opa-binary", default="opa", help=h["opa_binary"])
    parser.add_argument("--policy-package", default="dynamic_alerting.policy",
                        help=h["policy_package"])
    parser.add_argument("--policy-path", help=h["policy_path"])
    parser.add_argument("--json", action="store_true", dest="json_output", help=h["json"])
    parser.add_argument("--ci", action="store_true", help=h["ci"])
    parser.add_argument("--dry-run", action="store_true", help=h["dry_run"])
    parser.add_argument("--at", default=None, metavar="RFC3339", help=h["at"])
    return parser


@exit_on_yaml_file_error  # #1654: unreadable tenant / _defaults file → rc 2, named
@exit_on_served_values_error  # #2115: da-guard missing or failing, a dropped file → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # #2906: no usable parse-tree → rc 2
def main(argv: Optional[list[str]] = None) -> "int | Report":
    """CLI entry point. A JSON document (the OPA input under --dry-run, the
    report under --json) is handed back for run() to encode (#3216)."""
    try_utf8_stdout()
    lang = detect_cli_lang()
    parser = build_parser(lang)
    args = parser.parse_args(argv)

    # Load configs
    # #3902: judged once, before anything is read — a refusal names the flag.
    _lib_parse_cache.declared_trees(args, "config_dir", lang=lang)
    if args.at == "":  # '' is given (#1616), not passed on as "now"
        print("ERROR: --at takes an RFC3339 instant (e.g. 2026-10-10T03:00:00Z)",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    inputs = load_tenant_inputs(args.config_dir, at=args.at)
    tenant_configs = inputs.tenants
    defaults = load_defaults(args.config_dir)

    if not tenant_configs:
        # #1112: prose → stderr; stdout still owes exactly one JSON document.
        if lang == "zh":
            print(f"未找到 tenant 配置於 {args.config_dir}", file=sys.stderr)
        else:
            print(f"No tenant configs found in {args.config_dir}", file=sys.stderr)
        if args.dry_run:
            # --dry-run's stdout IS the OPA input document (it is piped into
            # `opa eval`, not read as a report) — so the zero-tenant answer is
            # an OPA input with no tenants, NOT a report envelope. Same schema
            # as the populated dry-run, so the same consumer keeps working.
            return Report(build_opa_input(
                args.config_dir, {}, defaults,
                rule_packs=None, platform_version="v2.3.0",
            ))
        if args.json_output:
            # Report schema, zeroed, with a status/reason discriminator.
            return Report({
                "status": "no_tenant_configs",
                "reason": "no_tenant_configs_found",
                "tenants_evaluated": 0,
                "error_count": 0,
                "warning_count": 0,
                "passed": True,
                "violations": [],
            })
        return EXIT_OK

    # Build OPA input
    opa_input = build_opa_input(
        args.config_dir,
        tenant_configs,
        defaults,
        rule_packs=None,
        platform_version="v2.3.0",
        served=inputs.served,
        unserved=inputs.unserved,
    )

    # Dry-run mode
    if args.dry_run:
        return Report(opa_input)

    # Evaluate via OPA
    if not args.opa_url and not args.policy_path:
        if lang == "zh":
            print("錯誤：必須指定 --opa-url 或 --policy-path", file=sys.stderr)
        else:
            print("ERROR: Must specify --opa-url or --policy-path", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        if args.opa_url:
            opa_violations = call_opa_rest(
                args.opa_url,
                args.policy_package,
                opa_input,
            )
        else:
            opa_violations = call_opa_binary(
                args.opa_binary,
                args.policy_path,
                args.policy_package,
                opa_input,
            )
        result = convert_opa_violations(opa_violations, len(tenant_configs))
    except OpaEvalError as e:
        # #2724: no evaluation (or an answer that is not violations) is not a
        # pass. One stderr line, rc 2, stdout empty under --json too (#3558),
        # as for the da-guard and no-OPA-selected exits above.
        print(f"ERROR: {safe_label(str(e))}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Exit code
    status = EXIT_VIOLATION if args.ci and not result.passed else EXIT_OK

    # Output
    if args.json_output:
        return Report(generate_json_report(result), status)
    print(generate_text_report(result, lang))
    return status


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
