"""Validation and parsing helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import threading
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional, Union

from _lib_compat import find_project_root
from _lib_constants import (
    _ALLOWED_SCHEMES,
    _DISABLED_VALUES,
    _DURATION_MULTIPLIERS,
    _DURATION_RE,
    GUARDRAILS,
    HTTP_CONFIG_AUTH_FIELDS,
    HTTP_CONFIG_AUTH_MAPPINGS,
    PLATFORM_DEFAULTS,
    RECEIVER_TYPES,
    THRESHOLD_NUMBER_PATTERN,
    THRESHOLD_TRIM_CODE_POINTS,
)


def detect_cli_lang() -> str:
    """Detect CLI language from environment variables.

    Checks in order: ``DA_LANG``, ``LC_ALL``, ``LANG``.
    Returns ``'zh'`` if any starts with ``zh``, ``'en'`` otherwise.
    """
    for var in ("DA_LANG", "LC_ALL", "LANG"):
        val: str = os.environ.get(var, "")
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"
    return "en"


def parse_duration_seconds(value: Union[str, int, float, None]) -> Optional[int]:
    """Parse a Prometheus-style duration string to seconds.

    Accepts: ``5s``, ``30s``, ``1m``, ``5m``, ``1h``, ``4h``, ``72h``,
    ``1d``, or numeric ``int``/``float``.

    Returns:
        Seconds as ``int``, or ``None`` if *value* is invalid.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not value or not isinstance(value, str):
        return None
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        return None
    return int(float(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2)])


def positive_seconds(value: str) -> float:
    """argparse ``type=`` for a timeout or interval in seconds (#2942).

    A finite number above 0 and at most ``threading.TIMEOUT_MAX`` (about 292
    years), fractions allowed; anything else is refused, so argparse exits 2
    (CALLER_ERROR) before any work. A ``urllib`` timeout of 0 makes the socket
    non-blocking (every call fails at once), a negative one raises ValueError,
    and one past TIMEOUT_MAX raises OverflowError in ``socket.settimeout`` /
    ``time.sleep``: none is a usable value.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}") from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a number of seconds above 0 and at most "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return seconds


def non_negative_seconds(value: str) -> float:
    """argparse ``type=`` for a pause in seconds where 0 means "do not wait" (#2966).

    The sibling of :func:`positive_seconds` for a rate limit, a throttle or a
    poll interval: a finite number from 0 up to ``threading.TIMEOUT_MAX``,
    fractions allowed; anything else is refused, so argparse exits 2
    (CALLER_ERROR) before any work. A negative pause used to be skipped
    silently by a ``> 0`` guard (or raise ValueError in ``time.sleep``), and
    ``inf`` raised OverflowError in ``time.sleep`` partway through a run.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}") from None
    if not math.isfinite(seconds) or seconds < 0 or seconds > threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a number of seconds from 0 to "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return seconds


def positive_int(value: str) -> int:
    """argparse ``type=`` for a count that must be at least 1 (#2966).

    ``int()`` parsing (so ``"1.5"`` is refused), then ``>= 1``; anything else
    raises ArgumentTypeError, so argparse exits 2 (CALLER_ERROR) with a
    message that names the problem instead of the function.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def positive_int_seconds(value: str) -> int:
    """argparse ``type=`` for a whole number of seconds above 0 (#2966).

    For a value that is both slept on and used in integer arithmetic
    (``duration // interval`` feeding ``range()``), where a float would turn a
    sample count into ``40.0`` and break ``range``. ``int()`` parsing, then 1
    up to the ``threading.TIMEOUT_MAX`` ceiling of :func:`positive_seconds`
    (past it ``time.sleep`` raises OverflowError). ⚠️ Not a ceiling for a
    ``subprocess`` timeout: that waits in poll(), which overflows past
    2147483 s on Linux.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= number <= threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a whole number of seconds from 1 to "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return number


# ============================================================
# URLs a caller hands a tool (#3528)
# ============================================================

def url_problem(value: str) -> Optional[str]:
    """The ONE judgement of a URL a tool will send a request to (#3528).

    ``None`` when *value* is usable: it parses, its scheme is one of
    ``_ALLOWED_SCHEMES`` (the SSRF allowlist), it names a host, and its port
    (if any) is a number in range. Otherwise the reason, one line.

    Every URL flag is judged here, through :func:`url_type` at parse time,
    and so is every request ``_lib_prometheus`` sends
    (``_validate_url_scheme``): a value the parser accepts is one a request
    will not refuse for its shape. ⚠️ A scheme message starts with
    ``Unsupported URL scheme:`` and names the scheme — callers and tests
    read that wording.
    """
    # ⛔ Before urlsplit: it silently drops \t \r \n and keeps a space in the
    # host, so "http://h :80" or "http://h\n" would pass here and then make
    # http.client raise InvalidURL at request time. No whitespace, no control
    # character anywhere — refused by name, never stripped.
    bad = next((c for c in value if c.isspace() or unicodedata.category(c) in ("Cc", "Cf")),
               None)
    if bad is not None:
        return f"contains whitespace or a control character ({bad!r})"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as exc:  # "http://[::1": Invalid IPv6 URL
        return f"not a URL: {exc}"
    if parts.scheme not in _ALLOWED_SCHEMES:
        return f"Unsupported URL scheme: {parts.scheme or 'none given'}"
    if not parts.hostname:
        return "no host"
    try:
        parts.port
    except ValueError as exc:  # "http://h:abc", "http://h:99999"
        return f"port is not usable: {exc}"
    # http.client sends the request target as ASCII: "/métrics" would pass
    # here and raise UnicodeEncodeError at request time. The host may be
    # non-ASCII (IDNA-encoded on connect); path and query may not (the
    # fragment is never sent).
    bad = next((c for c in parts.path + parts.query if not c.isascii()),
               None)
    if bad is not None:
        return f"non-ASCII character {bad!r} in the path or query (percent-encode it)"
    return None


#: What :func:`url_type` takes for ``empty=``.
URL_EMPTY_MODES = ("refuse", "disabled")


class UrlArg:
    """An argparse ``type=`` made by :func:`url_type` / :func:`url_list_type`.

    The class is the mark: a parser action whose ``type`` is an instance of
    it is a URL flag judged by :func:`url_problem`, and the da-tools gate
    (``tests/shared/test_caller_path_manifest.py``) reads it off the parser
    by identity — not by the flag's name or help text.
    """

    def __init__(self, empty: str, separator: Optional[str] = None) -> None:
        if empty not in URL_EMPTY_MODES:
            raise ValueError(f"empty= must be one of {URL_EMPTY_MODES}, got {empty!r}")
        if separator == "":  # would parse as one URL, not the list it promises
            raise ValueError("separator= must not be empty")
        self.empty = empty
        self.separator = separator
        # argparse names a type by __name__ in a ValueError message; this
        # one raises ArgumentTypeError, whose message is used as it is.
        self.__name__ = "url_list" if separator else "url"

    def __repr__(self) -> str:
        sep = f", separator={self.separator!r}" if self.separator else ""
        return f"UrlArg(empty={self.empty!r}{sep})"

    def _one(self, value: str) -> str:
        problem = url_problem(value)
        if problem is not None:
            raise argparse.ArgumentTypeError(
                f"not a usable URL {value!r} (wanted http:// or https://, a host, "
                f"a numeric port): {problem}")
        return value

    def __call__(self, value: str) -> "str | list[str]":
        if value == "":
            if self.empty == "disabled":
                return [] if self.separator else ""
            raise argparse.ArgumentTypeError("empty: a URL is required")
        if not self.separator:
            return self._one(value)
        # Not stripped: a space around an item is refused by url_problem, the
        # same as in a single URL — the two types judge one way.
        items = value.split(self.separator)
        if "" in items:
            raise argparse.ArgumentTypeError(
                f"an empty item in {value!r} ({self.separator!r}-separated URLs)")
        return [self._one(item) for item in items]


def url_type(*, empty: str) -> UrlArg:
    """argparse ``type=`` for a URL flag (#3528): :func:`url_problem`, so a
    value that is not a usable http(s) URL exits 2 (CALLER_ERROR) at parse
    time with argparse's ``argument --flag: ...`` line.

    *empty*: ``"refuse"`` — ``""`` is refused like any other unusable value;
    ``"disabled"`` — ``""`` parses as ``""``, the flag's off switch (for a
    flag whose absence already means "do not send").
    """
    return UrlArg(empty)


def url_list_type(*, empty: str, separator: str = ",") -> UrlArg:
    """:func:`url_type` for a *separator*-separated list of URLs; parses to a
    ``list[str]``, each item judged on its own (not stripped), an empty
    item refused."""
    return UrlArg(empty, separator)


def format_duration(seconds: int) -> str:
    """Format seconds back to a Prometheus-compatible duration string.

    Uses the largest whole unit that divides evenly (``h`` → ``m`` → ``s``).

    Note:
        Prometheus/Alertmanager only supports ``s``/``m``/``h`` (not ``d``).
        This function intentionally never produces day units.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def routing_disabled(value: Any) -> bool:
    """Whether a tenant's `_routing` string switches its routing off, as the
    route generator — routing's one judge (ADR-036 section 9) — reads it:
    Python ``str.strip()`` (U+001C–U+001F are white space here) then
    ``lower()``, so ``"\x1coff"`` disables and ``"d\u0130sable"`` does not.
    Pinned by tests/shared/routing_policy_parity_matrix.json. ⛔ Not a
    threshold row's reading, which is the exporter's alone (#3141; Python
    tools read it from served-values' `keys[].reason`, #3140 PR2); kept
    apart so that ruling does not move a routing verdict.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


# The severities a threshold value's `:severity` suffix may name: Go's
# config.ThresholdSeverities (#3071), the ones the rule packs' threshold
# recording rules select. tests/shared/threshold_value_matrix.json pins it.
# ⛔ Python has no reader of a threshold VALUE (#3267) — its `:severity`
# half, its trim, what a disable is: a tool that needs one asks the exporter
# (`_lib_parse_tree.threshold_values`, served-values).
THRESHOLD_SEVERITIES = ("warning", "critical")

# The grammar of a threshold NUMBER is the recipe contract's
# patterns.thresholdNumber (#3457), generated into _lib_constants: decimal
# only, at most 20 mantissa digits. `float()` reads more than it — `1_000`,
# ` 5`, `infinity`, Unicode digits — so nothing here calls it on a text the
# pattern has not matched. re.ASCII and fullmatch: `$` alone also matches
# before a trailing newline.
_THRESHOLD_NUMBER = re.compile(THRESHOLD_NUMBER_PATTERN, re.ASCII)


def threshold_trim(text: str) -> str:
    """*text* without the characters the exporter trims around a threshold
    value (Go's strings.TrimSpace, generated into THRESHOLD_TRIM_CODE_POINTS
    from the threshold value matrix's header).

    ⛔ Not `str.strip()`: that one also takes U+001C to U+001F, which the
    exporter keeps — `"70\\x1f"` is not a number to it."""
    start, end = 0, len(text)
    while start < end and ord(text[start]) in THRESHOLD_TRIM_CODE_POINTS:
        start += 1
    while end > start and ord(text[end - 1]) in THRESHOLD_TRIM_CODE_POINTS:
        end -= 1
    return text[start:end]


def written_threshold_number(value: Any) -> Optional[float]:
    """The number of a threshold a tool holds — as a file wrote it, or as a
    reader already decoded it — or None when it is not one.

    * a `str` is the text a file wrote: trimmed as the exporter trims
      (`threshold_trim`), it must be a number the exporter reads
      (`threshold_number`). `1_000`, `0x1F`, a value with a `:severity`,
      `disable`, `inf` are None — never what `float()` would make of them;
    * an `int` / `float` is a number a reader already decoded (a served
      value, a YAML number the exporter's decode kept): itself as a float,
      NaN and ±Inf included (the caller decides what those mean); an int past
      float64 is None;
    * a `bool` and anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return None
    if isinstance(value, str):
        return threshold_number(threshold_trim(value))
    return None


def is_threshold_number_text(text: Any) -> bool:
    """Whether *text* is in the grammar of a threshold number, as written:
    no surrounding space, no `:severity`. `1e999` is in it and is not a
    number the exporter serves — `threshold_number` says that."""
    return isinstance(text, str) and _THRESHOLD_NUMBER.fullmatch(text) is not None


def threshold_number(text: Any) -> Optional[float]:
    """The number the exporter reads *text* as (its parseThresholdNumber):
    in the grammar and finite, else None.

    For a tool that judges a number it is given or about to write (a CLI
    argument, a generated `defaults:` value). A tenant's value — with its
    severity, its spaces, its disable words — is read by the exporter."""
    if not is_threshold_number_text(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def threshold_number_text(value: float) -> str:
    """*value* as a text `threshold_number` reads back as the same number,
    or as `repr` writes it when *value* is NaN or ±Inf (no text of one is in
    the grammar).

    `repr` where that is in the grammar; it is not for every number —
    `repr(1.2345678901234567e-4)` is `0.00012345678901234567`, 21 digits —
    and then the exponent form, whose mantissa is 17 digits."""
    value = float(value)
    text = str(value)  # a float's str is its repr: the shortest text that reads back
    if not math.isfinite(value) or is_threshold_number_text(text):
        return text
    return format(value, ".16e")


def is_valid_tenant_id(tenant: Any) -> bool:
    """Whether *tenant* is a valid tenant id (ADR-035; #2341 R8).

    The rule is ``tenant_id_rule()`` — tenant-config.schema.json
    ``definitions.tenantId``, a DNS-1123 label — matched with ``fullmatch``.
    Go twin: pkg/tenantid.Valid. Pinned by the
    ``tenant_ids`` table of tests/shared/routing_policy_parity_matrix.json.
    Raises RuntimeError when the schema cannot be read (fail-closed).
    """
    return isinstance(tenant, str) and tenant_id_rule()[0].fullmatch(tenant) is not None


def validate_and_clamp(
    param: str,
    value: Union[str, int, float],
    tenant: str,
) -> tuple[Union[str, int, float], list[str]]:
    """Validate a timing parameter against guardrails and clamp if needed.

    #2490: *value* is read as Alertmanager reads it (``am_duration_seconds``):
    ``1h30m`` and ``1d`` are durations, ``1.5h`` / ``30m1h`` / ``1ns`` are
    not. A clamped value is written by ``format_duration`` (h / m / s only),
    which Alertmanager reads; a value within bounds is returned unchanged.

    Args:
        param: Parameter name (``group_wait``, ``group_interval``,
               ``repeat_interval``).
        value: Duration string (e.g. ``"30s"``), or numeric seconds (kept
               for direct callers; the route generator always passes the
               YAML value's ``str()``, where a bare number has no unit).
        tenant: Tenant identifier (used in warning messages).

    Returns:
        A 2-tuple ``(clamped_value, warnings)`` where *warnings* is a
        list of human-readable strings (empty if within bounds).
    """
    warnings: list[str] = []

    if param not in GUARDRAILS:
        return value, warnings

    min_sec, max_sec, desc = GUARDRAILS[param]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds: Optional[float] = float(value)
    else:
        seconds = am_duration_seconds(value if isinstance(value, str) else str(value))

    if seconds is None:
        # #1560 ⑤-b: a value goes into a message through shown_value only.
        warnings.append(f"  WARN: {tenant}: invalid {param} {shown_value(value)} "
                        f"({INVALID_DURATION_HINT}), using platform default "
                        f"{shown_value(PLATFORM_DEFAULTS.get(param, value))}")
        return PLATFORM_DEFAULTS.get(param, value), warnings

    if seconds < min_sec:
        clamped = format_duration(min_sec)
        warnings.append(f"  WARN: {tenant}: {param} {shown_value(value)} below minimum ({desc}), "
                        f"clamped to {clamped}")
        return clamped, warnings

    if seconds > max_sec:
        clamped = format_duration(max_sec)
        warnings.append(f"  WARN: {tenant}: {param} {shown_value(value)} above maximum ({desc}), "
                        f"clamped to {clamped}")
        return clamped, warnings

    return value, warnings


FIELD_UNSET = "unset"
FIELD_SET = "set"
FIELD_NOT_STRING = "not_string"


def receiver_field_state(receiver: dict[str, Any], field: str) -> str:
    """State of an exactly-one group field, by the schema's type rule (#2137).

    Cases:
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json
    ``FIELD_NOT_STRING`` is an error,
    stricter than Alertmanager on purpose: it renders ``0`` as ``"0"`` but
    fails to load ``[]`` (``cannot unmarshal !!seq``), so no "counts as given"
    rule for non-strings is right for all of them.

    #1560: a Secret reference (``is_secret_ref``) is ``FIELD_SET`` — it gives
    the value, through a file. Whether its shape is right, and whether this
    field may take one at all, is ``receiver_required_problem`` /
    ``receiver_optional_problem``'s to judge.
    """
    value = receiver.get(field)
    if value is None or value == "":
        return FIELD_UNSET
    if isinstance(value, str) or is_secret_ref(value):
        return FIELD_SET
    return FIELD_NOT_STRING


def receiver_exactly_one_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """Check the ``exactly_one_of`` groups of ``RECEIVER_TYPES[rtype]``.

    Each field's state comes from ``receiver_field_state``; a non-string value
    is reported first, and only when every field's type is valid is "exactly
    one" checked. Returns the first problem (callers prefix tenant / receiver
    context), or ``None`` when every group has exactly one field set. Unknown
    types return ``None``; callers reject those separately.
    """
    spec = RECEIVER_TYPES.get(rtype, {})
    for group in spec.get("exactly_one_of", []):
        states = {f: receiver_field_state(receiver, f) for f in group}
        for f, state in states.items():
            if state == FIELD_NOT_STRING:
                return (f"field '{f}' must be a string, "
                        f"got {type(receiver[f]).__name__}")
        set_fields = [f for f, state in states.items() if state == FIELD_SET]
        if len(set_fields) == 1:
            continue
        names = " or ".join(f"'{f}'" for f in group)
        if not set_fields:
            return f"requires exactly one of {names}, none is set"
        problem = f"requires exactly one of {names}, not both"
        if rtype == "pagerduty":
            problem += (" (Alertmanager would use the Events API v1 via "
                        "service_key and silently ignore routing_key)")
        return problem
    return None


_TENANT_SCHEMA_BASENAME = "tenant-config.schema.json"
_RECEIVER_FIELD_SCHEMAS: Optional[dict[str, dict[str, dict[str, Any]]]] = None


def _find_tenant_schema() -> Optional[Path]:
    """Locate tenant-config.schema.json in either shipping layout.

    Flat first: the da-tools image copies it beside this module (build.sh
    ``REPO_DATA_FILES``, paired by ``REQUIRED_DATA_FILES`` in
    check_build_completeness.py) and has no repo tree. Otherwise
    ``docs/schemas/`` under THIS tree's root (``find_project_root``; #1494 no
    counting of levels, #2845 never an enclosing project's).
    """
    here = Path(__file__).resolve().parent
    flat = here / _TENANT_SCHEMA_BASENAME
    if flat.is_file():
        return flat
    repo_root = find_project_root(here)
    if repo_root is not None:
        candidate = repo_root / "docs" / "schemas" / _TENANT_SCHEMA_BASENAME
        if candidate.is_file():
            return candidate
    return None


def _receiver_field_schemas() -> dict[str, dict[str, dict[str, Any]]]:
    """``{rtype: {field: {"type": ..., "pattern": ...}}}`` read from the schema.

    The schema is the hub of the receiver field contract (#2180): the
    ``pattern`` of a URL / smarthost field is written once there (a
    ``definitions`` entry the property references via ``allOf``) and read
    here, never copied. Fails closed — a missing schema raises instead of
    silently skipping the format check.
    """
    global _RECEIVER_FIELD_SCHEMAS
    if _RECEIVER_FIELD_SCHEMAS is not None:
        return _RECEIVER_FIELD_SCHEMAS
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; receiver fields cannot be checked")
    with open(path, encoding="utf-8") as f:
        defs = json.load(f)["definitions"]

    def resolve(prop: dict[str, Any]) -> dict[str, Any]:
        out = {"type": prop.get("type"), "pattern": prop.get("pattern"),
               "ref": prop.get("$ref"), "secret_ref": False}
        subs = list(prop.get("allOf", []))
        # #1560: a credential field is "a value, or a Secret reference" —
        # exactly `anyOf: [<value schema>, {$ref: secretRef}]`. Its type and
        # pattern are the value branch's. Any other anyOf on a receiver field
        # is a shape this reader does not model: fail closed.
        alts = prop.get("anyOf")
        if alts is not None:
            refs = [a for a in alts if a == {"$ref": _SECRET_REF_REF}]
            values = [a for a in alts if "$ref" not in a]
            if len(refs) != 1 or len(values) != 1 or len(alts) != 2:
                raise RuntimeError(
                    f"unsupported anyOf on a receiver field in {path}: {alts!r} "
                    "(only [<value>, {$ref: #/definitions/secretRef}] is read)")
            out["secret_ref"] = True
            out["type"] = values[0].get("type")
            out["pattern"] = values[0].get("pattern")
            subs.extend(values[0].get("allOf", []))
        for sub in subs:
            ref = sub["$ref"]
            if not ref.startswith("#/definitions/"):
                raise RuntimeError(f"unsupported $ref {ref!r} in {path}")
            target = defs[ref[len("#/definitions/"):]]
            if target.get("pattern"):
                out["pattern"] = target["pattern"]
        return out

    table: dict[str, dict[str, dict[str, Any]]] = {}
    for branch in defs["receiver"]["oneOf"]:
        d = defs[branch["$ref"][len("#/definitions/"):]]
        table[d["properties"]["type"]["const"]] = {
            f: resolve(p) for f, p in d["properties"].items() if f != "type"}
    _RECEIVER_FIELD_SCHEMAS = table
    return table


# ── #1560: Secret references on receiver credential fields ───────────────
# A credential field may hold `{secret: {key: <k>}}` (the tenant's own Secret,
# named `da-<tenant>`) or `{secret: {shared: <alias>, key: <k>}}` (a Secret
# the deployment policy's `shared_secrets` lists) instead of the value. The
# shape is tenant-config.schema.json `definitions.secretRef`; which fields
# take it is the schema too (`_receiver_field_schemas`' `secret_ref`). Which
# Secret a reference may name — the binding — is the route generator's
# (`_grar_merge.secret_binding_findings`), and so is the rendering.
_SECRET_REF_REF = "#/definitions/secretRef"
_SECRET_REF_RULE: Optional[dict[str, tuple[re.Pattern[str], int]]] = None
SECRET_REF_SHAPES = ("{secret: {key: <key>}} (the tenant's own Secret) or "
                     "{secret: {shared: <alias>, key: <key>}}")


def is_secret_ref(value: Any) -> bool:
    """Whether *value* is written as a Secret reference: a mapping whose
    ``secret`` key holds a mapping. Its shape is ``secret_ref_problem``'s to
    judge — a malformed reference (``{secret: {name: …, key: …}}``, the shape
    before the owner decision of 2026-11-04) is still a reference, refused
    for its shape, never taken for a value. A ``secret`` key holding a
    scalar is not one: an HTTP header, an oauth2 endpoint parameter or a
    claim may be named ``secret`` (#1560 review)."""
    return isinstance(value, dict) and isinstance(value.get("secret"), dict)


def secret_ref_rule() -> dict[str, tuple[re.Pattern[str], int]]:
    """``{"key" | "shared": (pattern, maxLength)}`` read from the schema's
    ``definitions.secretRef`` (fails closed like ``tenant_id_rule``)."""
    global _SECRET_REF_RULE
    if _SECRET_REF_RULE is not None:
        return _SECRET_REF_RULE
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; Secret references cannot be checked")
    with open(path, encoding="utf-8") as f:
        definition = json.load(f).get("definitions", {}).get("secretRef")
    try:
        props = definition["properties"]["secret"]["properties"]
        rule = {name: (re.compile(props[name]["pattern"]), int(props[name]["maxLength"]))
                for name in ("key", "shared")}
    except (KeyError, TypeError, ValueError, re.error) as exc:
        raise RuntimeError(
            f"{path}: definitions.secretRef with key / shared patterns and "
            f"maxLength is missing or malformed ({exc!r}); Secret references "
            "cannot be checked") from exc
    _SECRET_REF_RULE = rule
    return rule


def secret_ref_problem(value: Any) -> Optional[str]:
    """What is wrong with the shape of Secret reference *value*, or None.

    The schema's ``definitions.secretRef``: ``secret`` and nothing else;
    under it ``key`` (required) and ``shared`` (optional) and nothing else;
    each a string of at most maxLength that ``fullmatch``es its pattern (so a
    trailing newline is refused, as the schema's ``not`` does), and ``key``
    neither ``.`` nor starting with ``..`` (the schema's ``not`` on key,
    Kubernetes IsConfigMapKey). tests/shared/test_secret_ref_schema.py gives
    this and jsonschema the same cases.
    """
    if not isinstance(value, dict) or set(value) != {"secret"}:
        keys = (sorted(shown_key(k) for k in value) if isinstance(value, dict) else [])
        has = f": it has {keys}" if keys else ""
        return f"is not a Secret reference ({SECRET_REF_SHAPES}){has}"
    inner = value["secret"]
    if not isinstance(inner, dict):
        return (f"is not a Secret reference ({SECRET_REF_SHAPES}): 'secret' is "
                f"{_type_name(inner, shown=MASKED_VALUE)}")
    unknown = sorted(shown_key(k) for k in inner if k not in ("key", "shared"))
    if unknown:
        hint = (" — a reference never names its Secret: the tenant's own is "
                "da-<tenant>, a shared one is an alias the deployment policy's "
                "shared_secrets lists" if "name" in unknown else "")
        return (f"is not a Secret reference ({SECRET_REF_SHAPES}): "
                f"'secret' has {unknown}{hint}")
    if "key" not in inner:
        return f"is not a Secret reference ({SECRET_REF_SHAPES}): 'secret.key' is missing"
    rule = secret_ref_rule()
    for name in ("key", "shared"):
        if name not in inner:
            continue
        item = inner[name]
        pattern, max_len = rule[name]
        if (not isinstance(item, str) or len(item) > max_len
                or pattern.fullmatch(item) is None
                or (name == "key" and (item == "." or item.startswith("..")))):
            what = ("a Secret data key (letters, digits, '-', '_', '.'; at most "
                    f"{max_len}; not '.' and not starting with '..')"
                    if name == "key" else
                    f"a shared Secret alias (a DNS-1123 label, at most {max_len})")
            return (f"has 'secret.{name}' {_type_name(item, shown=MASKED_VALUE)}, "
                    f"which is not {what}")
    return None


#: #1560 plaintext policy of a credential site (``CredentialRefSite.plaintext``,
#: ``CredentialPlaintextSite.policy``). The route generator refuses by it
#: (``_grar_parse.credential_findings``, #1560 ⑤-b) and
#: ``da-tools migrate-credentials`` rewrites by it.
PLAINTEXT_REFUSED = "refused"
PLAINTEXT_UNLESS_USERINFO = "unless_userinfo"
PLAINTEXT_ALLOWED = "allowed"
PLAINTEXT_FORBIDDEN = "forbidden"
PLAINTEXT_POLICIES = frozenset({PLAINTEXT_REFUSED, PLAINTEXT_UNLESS_USERINFO,
                                PLAINTEXT_ALLOWED, PLAINTEXT_FORBIDDEN})


class CredentialRefSite(NamedTuple):
    """One place in a receiver where a Secret reference may stand (#1560).

    ``path`` is read from the receiver: a key, ``"*"`` for any key of a
    mapping, ``"[]"`` for any item of a list. ``file_key`` is the
    Alertmanager key the reference renders as — the leaf's sibling
    (``password`` → ``password_file``), or, for a list item, the sibling list
    the item moves to (a header's ``secrets[i]`` → that header's ``files``).

    ``destinations`` are the receiver fields naming where the credential is
    sent, when that is not the credential itself: the tenant writes them, so
    a reference here takes only the tenant's own Secret, and only under a
    policy whose non-empty ``allowed_domains`` admits every one of their
    hosts (written out, not a reference). Empty: the credential IS where it
    goes (a webhook URL, a routing key). An oauth2 credential has two: it
    goes to token_url, and the access token it is exchanged for — a
    credential the tenant's Secret yields — goes with every request to url.
    ``proxies`` are the proxy keys the requests carrying it go through (the
    token request through oauth2.proxy_url, the request to url through
    http_config.proxy_url). ``follows_redirects``: Alertmanager sends it
    again on a redirect (http_config.follow_redirects defaults to true) to a
    host the allowlist did not judge. prometheus/common v0.70.1 re-adds
    Authorization (basic_auth, authorization, bearer_token, the oauth2 access
    token) on every hop isCrossHostRedirect does not flag, and that check
    passes a SUBDOMAIN of the original host (isDomainOrSubdomain) — outside
    an allowlist that names the host exactly; a custom http_headers value is
    re-added on every hop, any host (sensitiveHeadersStripRT strips only
    Authorization / Cookie-like names). A TLS client key is never sent.
    The oauth2 rows carry the flag for the access token they yield, which
    rides the webhook client's redirects: http_config.follow_redirects
    governs only that client. The token request itself goes through a
    separate client that does not read FollowRedirects, so the flag says
    nothing about it.

    ``plaintext``: what a value written out (not a reference) at this path
    is under the owner's #1560 policy (``plaintext_credential_findings``):
    ``PLAINTEXT_REFUSED`` — only a reference is taken; or
    ``PLAINTEXT_UNLESS_USERINFO`` — a written URL is taken too, unless it
    carries userinfo (``user:pass@`` or ``user@``).
    """

    rtype: str
    path: tuple[str, ...]
    file_key: str
    destinations: tuple[tuple[str, ...], ...]
    proxies: tuple[tuple[str, ...], ...]
    follows_redirects: bool = False
    plaintext: str = PLAINTEXT_REFUSED



_HTTP_PROXY = ("http_config", "proxy_url")
_OAUTH2_PROXY = ("http_config", "oauth2", "proxy_url")
_TOKEN_URL = ("http_config", "oauth2", "token_url")
_URL = ("url",)
_TO_URL = ((_URL,), (_HTTP_PROXY,))
_TO_TOKEN_URL = ((_TOKEN_URL, _URL), (_OAUTH2_PROXY, _HTTP_PROXY))
_IS_URL = ((), (_HTTP_PROXY,))

#: ⛔ THE one table of where a Secret reference may stand (#1560). The schema
#: (`anyOf: [<value>, secretRef]` at exactly these paths, pinned by
#: tests/shared/test_secret_ref_schema.py), the shape check, the rendering,
#: the binding rules and every listing of a receiver's references read it.
#: Each `*_file` is the key prometheus/common v0.70.1 (Alertmanager v0.34.2)
#: reads from a file: config/http_config.go BasicAuth.PasswordFile,
#: Authorization.CredentialsFile, HTTPClientConfig.BearerTokenFile,
#: TLSConfig.KeyFile, OAuth2.ClientSecretFile / ClientCertificateKeyFile,
#: config/headers.go Header.Files; the receivers' own `*_file` for the rest.
#: The oauth2 three go to token_url, through oauth2's own proxy_url (its
#: token client is built from the OAuth2 block alone), and the access token
#: they yield goes to url, through http_config.proxy_url. Not here:
#: proxy_connect_header (no file form upstream).
CREDENTIAL_REF_SITES: tuple[CredentialRefSite, ...] = (
    CredentialRefSite("webhook", ("url",), "url_file", *_IS_URL,
                      plaintext=PLAINTEXT_UNLESS_USERINFO),
    CredentialRefSite("rocketchat", ("url",), "url_file", *_IS_URL),
    CredentialRefSite("slack", ("api_url",), "api_url_file", *_IS_URL),
    CredentialRefSite("teams", ("webhook_url",), "webhook_url_file", *_IS_URL),
    CredentialRefSite("pagerduty", ("service_key",), "service_key_file", *_IS_URL),
    CredentialRefSite("pagerduty", ("routing_key",), "routing_key_file", *_IS_URL),
    CredentialRefSite("email", ("auth_password",), "auth_password_file",
                      (("smarthost",),), ()),
    CredentialRefSite("webhook", ("http_config", "basic_auth", "password"),
                      "password_file", *_TO_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "authorization", "credentials"),
                      "credentials_file", *_TO_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "bearer_token"),
                      "bearer_token_file", *_TO_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "tls_config", "key"),
                      "key_file", *_TO_URL),
    CredentialRefSite("webhook", ("http_config", "http_headers", "*", "secrets", "[]"),
                      "files", *_TO_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "oauth2", "client_secret"),
                      "client_secret_file", *_TO_TOKEN_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "oauth2", "client_certificate_key"),
                      "client_certificate_key_file", *_TO_TOKEN_URL, follows_redirects=True),
    CredentialRefSite("webhook", ("http_config", "oauth2", "tls_config", "key"),
                      "key_file", *_TO_TOKEN_URL, follows_redirects=True),
)


#: the destinations of CREDENTIAL_REF_SITES written as `host:port` (email
#: smarthost); every other destination is a URL and must be written with an
#: http:// or https:// scheme (#1560: without one, the host read is the text
#: before the first ':' — `hook.example.com:80@evil.example` would read as
#: hook.example.com).
HOST_PORT_DESTINATIONS: frozenset[tuple[str, ...]] = frozenset({("smarthost",)})


def credential_ref_sites(rtype: str) -> tuple[CredentialRefSite, ...]:
    """The ``CREDENTIAL_REF_SITES`` of receiver type *rtype*."""
    return tuple(s for s in CREDENTIAL_REF_SITES if s.rtype == rtype)


RefPath = tuple[Union[str, int], ...]


#: The pattern steps of a ``CREDENTIAL_REF_SITES`` path: any key, any item.
_PATTERN_STEPS = frozenset({"*", "[]"})


def ref_path_text(path: RefPath) -> str:
    """``("http_config", "http_headers", "X", "secrets", 0)`` →
    ``http_config.http_headers.X.secrets[0]``. A key is the author's text
    and may be a URL, so it is shown as ``key_segment`` shows it (#1560
    ⑤-b)."""
    out = ""
    for part in path:
        if isinstance(part, int) and not isinstance(part, bool):
            out += f"[{part}]"
            continue
        seg = part if part in _PATTERN_STEPS else key_segment(part)
        out += f".{seg}" if out else seg
    return out


def _walk_site(value: Any, pattern: tuple[str, ...], here: RefPath) -> Iterator[RefPath]:
    if not pattern:
        if is_secret_ref(value):
            yield here
        return
    step, rest = pattern[0], pattern[1:]
    if step == "[]":
        if isinstance(value, list):
            for i, item in enumerate(value):
                yield from _walk_site(item, rest, here + (i,))
    elif isinstance(value, dict):
        keys = ([k for k in value if isinstance(k, str)] if step == "*"
                else [step] if step in value else [])
        for k in keys:
            yield from _walk_site(value[k], rest, here + (k,))


def find_credential_refs(receiver: Any, rtype: Optional[str] = None,
                         ) -> list[tuple[CredentialRefSite, RefPath]]:
    """``(site, concrete path)`` of every Secret reference (``is_secret_ref``)
    standing on a ``CREDENTIAL_REF_SITES`` path of *receiver*, in table
    order. *rtype* defaults to the receiver's own ``type`` (an Alertmanager
    entry has none). A receiver that is not a mapping, or of no known type,
    has none."""
    if rtype is None:
        rtype = receiver.get("type") if isinstance(receiver, dict) else None
    if not isinstance(receiver, dict) or not isinstance(rtype, str):
        return []
    return [(site, path) for site in credential_ref_sites(rtype)
            for path in _walk_site(receiver, site.path, ())]


def ref_value_at(receiver: Any, path: RefPath) -> Any:
    """The value at concrete *path* of *receiver* (None when absent)."""
    value = receiver
    for part in path:
        if isinstance(part, int):
            if not isinstance(value, list) or part >= len(value):
                return None
        elif not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class CredentialPlaintextSite(NamedTuple):
    """One place in a receiver that holds a credential but takes no Secret
    reference (#1560 ⑤-a): Alertmanager has no ``*_file`` form for it.
    ``path`` is spelled as ``CredentialRefSite.path`` (``"*"`` any key,
    ``"[]"`` any item) and names the place itself or the subtree it heads.
    ``policy``: ``PLAINTEXT_ALLOWED`` (written out is fine: every reading
    tool masks it), ``PLAINTEXT_FORBIDDEN`` (the tenant may not write it at
    all, whatever its value) or ``PLAINTEXT_UNLESS_USERINFO`` (refused when
    the URL carries userinfo)."""

    rtype: str
    path: tuple[str, ...]
    policy: str


#: ⛔ The credential places CREDENTIAL_REF_SITES does not hold (#1560 ⑤-a,
#: owner decision): together with that table and the test's list of
#: non-credential leaves they classify every leaf under an `x-credential`
#: node of the tenant schema exactly once
#: (tests/shared/test_credential_plaintext_policy.py). Kept apart from
#: CREDENTIAL_REF_SITES because every row there is read as "a reference may
#: stand here". The four proxy rows have no file form upstream: the platform
#: gives Alertmanager HTTPS_PROXY from a Secret and the tenant writes
#: `proxy_from_environment: true` instead.
CREDENTIAL_PLAINTEXT_SITES: tuple[CredentialPlaintextSite, ...] = (
    CredentialPlaintextSite("email", ("headers", "*"), PLAINTEXT_ALLOWED),
    CredentialPlaintextSite("webhook", ("http_config", "http_headers", "*", "values", "[]"),
                            PLAINTEXT_ALLOWED),
    CredentialPlaintextSite("webhook", ("http_config", "oauth2", "endpoint_params", "*"),
                            PLAINTEXT_ALLOWED),
    CredentialPlaintextSite("webhook", ("http_config", "oauth2", "claims"), PLAINTEXT_ALLOWED),
    CredentialPlaintextSite("webhook", ("http_config", "proxy_connect_header"),
                            PLAINTEXT_FORBIDDEN),
    CredentialPlaintextSite("webhook", ("http_config", "oauth2", "proxy_connect_header"),
                            PLAINTEXT_FORBIDDEN),
    CredentialPlaintextSite("webhook", ("http_config", "proxy_url"), PLAINTEXT_UNLESS_USERINFO),
    CredentialPlaintextSite("webhook", ("http_config", "oauth2", "proxy_url"),
                            PLAINTEXT_UNLESS_USERINFO),
)

#: What a plaintext finding asks for (``PlaintextFinding.category``).
NEEDS_REFERENCE = "needs_reference"        # write a Secret reference instead
URL_USERINFO = "userinfo"                  # a URL carrying user[:password]@
PROXY_CREDENTIAL = "proxy_credential"      # a proxy credential the tenant may not write
PLAINTEXT_CATEGORIES = (NEEDS_REFERENCE, URL_USERINFO, PROXY_CREDENTIAL)


class PlaintextFinding(NamedTuple):
    """One credential written out where the #1560 policy refuses it. Holds
    where (``path``, concrete; ``site``, the table row's path) and why
    (``category``) — never the value."""

    path: RefPath
    site: tuple[str, ...]
    category: str


def url_has_userinfo(text: str) -> bool:
    """Whether URL *text* carries userinfo (``user:pass@host`` or
    ``user@host``), as ``urllib.parse.urlsplit`` reads its authority. A URL
    the parser refuses (a malformed IPv6 host) counts when it holds an ``@``:
    it cannot be shown to be free of one."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(text).username is not None
    except ValueError:
        return "@" in text


def _walk_present(value: Any, pattern: tuple[str, ...], here: RefPath
                  ) -> Iterator[tuple[RefPath, Any]]:
    """``(concrete path, value)`` of every value standing on *pattern* of
    *value* (``_walk_site``'s steps), whatever it is."""
    if not pattern:
        yield here, value
        return
    step, rest = pattern[0], pattern[1:]
    if step == "[]":
        if isinstance(value, list):
            for i, item in enumerate(value):
                yield from _walk_present(item, rest, here + (i,))
    elif isinstance(value, dict):
        keys = ([k for k in value if isinstance(k, str)] if step == "*"
                else [step] if step in value else [])
        for k in keys:
            yield from _walk_present(value[k], rest, here + (k,))


def _written_out(value: Any) -> bool:
    """A credential value written out: a non-empty scalar (null and "" are
    not set, as in Alertmanager; a mapping or list is a shape problem the
    schema refuses, and a reference is not written out)."""
    return (value is not None and value != "" and not isinstance(value, (dict, list)))


def plaintext_credential_findings(receiver: Any, rtype: Optional[str] = None
                                  ) -> list[PlaintextFinding]:
    """#1560: every credential *receiver* writes out where the owner's
    policy refuses it — THE one judgement: the route generator refuses by it
    (``_grar_parse.credential_findings``, ⑤-b) and ``da-tools
    migrate-credentials`` rewrites by it (⑤-a).

    *receiver* is the receiver as one file writes it (before any routing
    layer is merged), *rtype* its type (default: its own ``type``). Per row
    of ``CREDENTIAL_REF_SITES`` (``plaintext``) and
    ``CREDENTIAL_PLAINTEXT_SITES`` (``policy``):

    * ``PLAINTEXT_REFUSED``: a written-out value → ``NEEDS_REFERENCE``;
    * ``PLAINTEXT_UNLESS_USERINFO``: a written-out string whose URL carries
      userinfo (``url_has_userinfo``) → ``URL_USERINFO`` on a reference
      site (webhook url), ``PROXY_CREDENTIAL`` on a proxy;
    * ``PLAINTEXT_FORBIDDEN``: the key present, whatever its value →
      ``PROXY_CREDENTIAL``;
    * ``PLAINTEXT_ALLOWED``: nothing.

    A Secret reference is never a finding (its shape is judged elsewhere).
    In table order; no value is returned, only where and why."""
    if rtype is None:
        rtype = receiver.get("type") if isinstance(receiver, dict) else None
    if not isinstance(receiver, dict) or not isinstance(rtype, str):
        return []
    out: list[PlaintextFinding] = []
    rows = ([(s.path, s.plaintext, URL_USERINFO) for s in CREDENTIAL_REF_SITES
             if s.rtype == rtype]
            + [(s.path, s.policy, PROXY_CREDENTIAL) for s in CREDENTIAL_PLAINTEXT_SITES
               if s.rtype == rtype])
    for site, policy, userinfo_category in rows:
        for path, value in _walk_present(receiver, site, ()):
            if policy == PLAINTEXT_FORBIDDEN:
                out.append(PlaintextFinding(path, site, PROXY_CREDENTIAL))
            elif is_secret_ref(value) or not _written_out(value):
                continue
            elif policy == PLAINTEXT_REFUSED:
                out.append(PlaintextFinding(path, site, NEEDS_REFERENCE))
            elif (policy == PLAINTEXT_UNLESS_USERINFO and isinstance(value, str)
                  and url_has_userinfo(value)):
                out.append(PlaintextFinding(path, site, userinfo_category))
    return out


def credential_shape_problems(receiver: Any, rtype: Optional[str] = None) -> list[RefPath]:
    """#1560 ⑤-b: the places of *receiver* where a credential site that
    ``plaintext_credential_findings`` judges holds a value of a shape it
    cannot read (at the site, or on the way to it). Such a value is neither
    a finding nor a reference, and a receiver no render reads gets no schema
    check either, so the caller reports each place as unjudged."""
    if rtype is None:
        rtype = receiver.get("type") if isinstance(receiver, dict) else None
    if not isinstance(receiver, dict) or not isinstance(rtype, str):
        return []
    judged = (PLAINTEXT_REFUSED, PLAINTEXT_UNLESS_USERINFO)
    sites = ([s.path for s in CREDENTIAL_REF_SITES if s.rtype == rtype and s.plaintext in judged]
             + [s.path for s in CREDENTIAL_PLAINTEXT_SITES
                if s.rtype == rtype and s.policy in judged])
    found: list[RefPath] = []

    def walk(value: Any, pattern: tuple[str, ...], here: RefPath) -> None:
        if value is None or value == "":
            return
        if not pattern:
            # A reference of the wrong shape is no reference: what it holds
            # is not judged either (one shape rule: secret_ref_problem).
            if isinstance(value, (dict, list)) and (
                    not is_secret_ref(value) or secret_ref_problem(value) is not None):
                found.append(here)
            return
        step, rest = pattern[0], pattern[1:]
        if step == "[]":
            if not isinstance(value, list):
                found.append(here)
                return
            for i, item in enumerate(value):
                walk(item, rest, here + (i,))
            return
        if not isinstance(value, dict):
            found.append(here)
            return
        for k in ([k for k in value if isinstance(k, str)] if step == "*" else [step]):
            if k in value:
                walk(value[k], rest, here + (k,))

    for site in sites:
        walk(receiver, site, ())
    return list(dict.fromkeys(found))


def value_paths_to_mask(receiver: Any, rtype: Optional[str] = None) -> list[RefPath]:
    """The concrete path of every written-out scalar *receiver* holds on or
    below a credential site of either table (reference or not, allowed or
    not) — what a tool masks before it shows a receiver's text. A reference
    is not written out; its key names nothing secret, so it is not listed."""
    if rtype is None:
        rtype = receiver.get("type") if isinstance(receiver, dict) else None
    if not isinstance(receiver, dict) or not isinstance(rtype, str):
        return []
    found: list[RefPath] = []

    def scalars(value: Any, here: RefPath) -> None:
        if is_secret_ref(value):
            return
        if isinstance(value, dict):
            for k, v in value.items():
                scalars(v, here + (k,))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                scalars(v, here + (i,))
        elif _written_out(value):
            found.append(here)

    sites = ([s.path for s in CREDENTIAL_REF_SITES if s.rtype == rtype]
             + [s.path for s in CREDENTIAL_PLAINTEXT_SITES if s.rtype == rtype])
    for site in sites:
        for path, value in _walk_present(receiver, site, ()):
            scalars(value, path)
    return list(dict.fromkeys(found))


def site_path_text(path: tuple[str, ...]) -> str:
    """A ``CREDENTIAL_REF_SITES`` row's path as the policy's
    ``shared_secrets.<alias>.fields`` spells it (#1560 ⑤-b): the keys joined
    with ``.``, a ``"*"`` step (any key of a mapping) written ``*``, a
    ``"[]"`` step (any item of a list) written ``[]`` after the step before
    it — ``("http_config", "http_headers", "*", "secrets", "[]")`` →
    ``http_config.http_headers.*.secrets[]``. One spelling per row: the text
    names the row, never a concrete header name or index."""
    out = ""
    for step in path:
        out += "[]" if step == "[]" else (f".{step}" if out else step)
    return out


def shared_secret_field_texts() -> frozenset[str]:
    """Every value a ``shared_secrets.<alias>.fields`` list may hold: the
    ``site_path_text`` of each ``CREDENTIAL_REF_SITES`` row (#1560 ⑤-b). A
    row whose credential goes to a destination the receiver names
    (``destinations``) is listable, but the generator takes a shared
    Secret there only on the receiver no tenant owns (the platform-wide
    ``_routing_enforced``)."""
    return frozenset(site_path_text(s.path) for s in CREDENTIAL_REF_SITES)


def receiver_secret_ref_fields(rtype: str) -> list[str]:
    """The top-level fields of *rtype* that take a Secret reference
    (``CREDENTIAL_REF_SITES`` of one key)."""
    return [s.path[0] for s in credential_ref_sites(rtype) if len(s.path) == 1]


def _receiver_secret_ref_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """#1560: the first problem with a Secret reference in *receiver*:

    - a credential field that takes one holds a malformed one
      (``secret_ref_problem``), or a mapping / list that is no reference
      (Alertmanager cannot load it as the string it reads there);
    - a string field that does not take one holds one (``channel:
      {secret: …}`` would be copied to Alertmanager as written);
    - a nested ``CREDENTIAL_REF_SITES`` path (inside ``http_config``) holds
      a malformed one. A reference anywhere else inside ``http_config`` is
      the generator's output check's (``secret_ref_unsupported_field``).
    """
    fields = _receiver_field_schemas().get(rtype, {})
    takes_ref = set(receiver_secret_ref_fields(rtype))
    for field, prop in fields.items():
        if field not in receiver:
            continue
        value = receiver[field]
        if field in takes_ref:
            if is_secret_ref(value):
                problem = secret_ref_problem(value)
                if problem:
                    return f"field '{field}' {problem}"
            elif isinstance(value, (dict, list)):
                return (f"field '{field}' must be a string or a Secret reference "
                        f"({SECRET_REF_SHAPES}), got {receiver_type_name(rtype, (field,), value)}")
        elif is_secret_ref(value) and prop.get("type") == "string":
            takes = ", ".join(receiver_secret_ref_fields(rtype)) or "none"
            return (f"field '{field}' does not take a Secret reference (the "
                    f"fields of a {rtype} receiver that do: {takes})")
    for _site, path in find_credential_refs(receiver, rtype):
        if len(path) > 1:
            problem = secret_ref_problem(ref_value_at(receiver, path))
            if problem:
                return f"field '{ref_path_text(path)}' {problem}"
    return None


_TENANT_ID_RULE: Optional[tuple[re.Pattern[str], str]] = None


def tenant_id_rule() -> tuple[re.Pattern[str], str]:
    """``(compiled pattern, description)`` of the tenant-id rule (ADR-035).

    Read from tenant-config.schema.json ``definitions.tenantId``, the one
    authored copy; Go and the portal read copies generated from it by
    scripts/tools/dx/gen_tenant_id_json.py. Match with ``fullmatch``: Python's
    ``$`` also matches before a trailing newline. Fails closed like
    ``_receiver_field_schemas`` (#2180): a missing schema or definition raises
    instead of letting every id through.
    """
    global _TENANT_ID_RULE
    if _TENANT_ID_RULE is not None:
        return _TENANT_ID_RULE
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; tenant ids cannot be checked")
    with open(path, encoding="utf-8") as f:
        definition = json.load(f).get("definitions", {}).get("tenantId")
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("pattern"), str)
            or not isinstance(definition.get("description"), str)
            or not definition["pattern"] or not definition["description"]):
        raise RuntimeError(
            f"{path}: definitions.tenantId with a string pattern and description "
            "is missing; tenant ids cannot be checked")
    _TENANT_ID_RULE = (re.compile(definition["pattern"]), definition["description"])
    return _TENANT_ID_RULE


_DURATION_RULE: Optional[tuple[re.Pattern[str], int, str]] = None

# Operator-facing summary of definitions.duration, for invalid-value lines.
INVALID_DURATION_HINT = ("not an Alertmanager duration: whole numbers with units "
                         "largest first, e.g. 30s, 5m, 1h30m, 1d")

# prometheus/common model.ParseDuration's units (time.go unitMap), in the
# order a value must give them. Nanoseconds per unit.
_AM_DURATION_UNIT_NS: dict[str, int] = {
    "y": 365 * 86400 * 10**9, "w": 7 * 86400 * 10**9, "d": 86400 * 10**9,
    "h": 3600 * 10**9, "m": 60 * 10**9, "s": 10**9, "ms": 10**6,
}
_AM_DURATION_TOKEN_RE = re.compile(r"([0-9]+)(ms|y|w|d|h|m|s)")
_INT64_MAX = (1 << 63) - 1


def duration_rule() -> tuple[re.Pattern[str], int, str]:
    """``(compiled pattern, minLength, description)`` of the routing timing
    syntax (#2490) — tenant-config.schema.json ``definitions.duration``, the
    one authored copy, which states Alertmanager's ``model.ParseDuration``.

    Match with ``fullmatch`` (Python's ``$`` also matches before a trailing
    newline). Fails closed like ``tenant_id_rule``: a missing schema or
    definition raises instead of letting every value through.
    """
    global _DURATION_RULE
    if _DURATION_RULE is not None:
        return _DURATION_RULE
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; durations cannot be checked")
    with open(path, encoding="utf-8") as f:
        definition = json.load(f).get("definitions", {}).get("duration")
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("pattern"), str)
            or not definition["pattern"]
            or not isinstance(definition.get("minLength"), int)
            or not isinstance(definition.get("description"), str)):
        raise RuntimeError(
            f"{path}: definitions.duration with a string pattern, an integer "
            "minLength and a description is missing; durations cannot be checked")
    _DURATION_RULE = (re.compile(definition["pattern"]), definition["minLength"],
                      definition["description"])
    return _DURATION_RULE


def am_duration_seconds(value: Any) -> Optional[float]:
    """Seconds Alertmanager reads from a routing timing *value*, or ``None``
    when Alertmanager refuses it (#2490).

    The syntax is ``duration_rule()`` (the schema's ``definitions.duration``);
    on top of it, the one check a pattern cannot state — prometheus/common
    ``ParseDuration``'s int64-nanosecond overflow (``"duration out of
    range"``), computed the way it does: per unit ``v > 2**63 // unit``, then
    the running sum ``> 2**63 - 1``. Only a ``str`` is a duration: Alertmanager
    reads the YAML text, and an unquoted ``30`` has no unit.

    ⛔ Not ``parse_duration_seconds``: that one serves the CLI arguments of
    other tools (``7d``, ``1.5h``, bare numbers) and stays as it is.
    tests/shared/am_duration_matrix.json (written by Go) pins this function.
    """
    if not isinstance(value, str):
        return None
    pattern, min_len, _desc = duration_rule()
    if len(value) < min_len or pattern.fullmatch(value) is None:
        return None
    if value == "0":
        return 0.0
    total = 0
    for digits, unit in _AM_DURATION_TOKEN_RE.findall(value):
        v, mult = int(digits), _AM_DURATION_UNIT_NS[unit]
        if v > (1 << 63) // mult:
            return None
        total += v * mult
        if total > _INT64_MAX:
            return None
    return total / 1e9


_YAML_BOOL_REF = "#/definitions/yamlBool"
_HTTP_CONFIG_REF = "#/definitions/httpConfigOrNull"
#: The YAML 1.1 boolean words PyYAML's resolver reads as booleans
#: (true / false / yes / no / on / off, three spellings each).
_YAML_BOOL_LITERALS: dict[str, bool] = {
    spelling: word in ("true", "yes", "on")
    for word in ("true", "false", "yes", "no", "on", "off")
    for spelling in (word, word.capitalize(), word.upper())}


def yaml_bool_literals() -> dict[str, bool]:
    """``{literal: value}`` of the YAML 1.1 boolean words (#2295): the
    plain scalars PyYAML reads as booleans. ⛔ #2906: no reader of the
    routing plane takes any of them as a STRING — the route generator reads
    yaml.v3 (an unquoted ``yes`` is the string "yes") and judges a boolean
    field by type alone (ADR-036 §2, ``is_bool_or_unset``), and the schema's
    ``definitions.yamlBool`` is boolean or null only, so this is no longer a
    schema list. What still uses the table: the YAML 1.1 boolean-word
    warning — ``_lib_io.find_yaml11_bool_words`` takes its words from
    ``_YAML_BOOL_LITERALS`` minus yaml.v3's six (``true`` / ``false`` in
    three spellings) — and that warning's tests, through this function
    (tests/shared/test_yaml11_bool_word_warning.py). ``y`` / ``n`` are not in
    it: PyYAML reads them as strings."""
    return dict(_YAML_BOOL_LITERALS)


def is_bool_or_unset(value: Any) -> bool:
    """#2906: what a boolean field of the route generator accepts — a
    boolean, or null (unset). Judged by type alone (ADR-036 §2): a string is
    refused whatever it says, ``"true"`` / ``"yes"`` included. An unquoted
    ``yes`` is a boolean only because PyYAML (YAML 1.1) built one."""
    return value is None or isinstance(value, bool)


def receiver_bool_fields(rtype: str) -> list[str]:
    """The optional fields of ``rtype`` the schema types as a YAML boolean."""
    return [f for f, prop in _receiver_field_schemas().get(rtype, {}).items()
            if prop.get("ref") == _YAML_BOOL_REF]


def receiver_bool_problems(rtype: str, receiver: Any) -> list[tuple[str, Any]]:
    """#2906: ``(field, value)`` of each boolean field of a *rtype* receiver
    that is not ``is_bool_or_unset`` — the schema's ``yamlBool`` properties
    (``send_resolved``, email ``require_tls``) and
    ``http_config.proxy_from_environment``. *field* is relative to the
    receiver (``http_config.proxy_from_environment``). An unknown type has no
    boolean field here (``unknown_receiver_type`` names it)."""
    if not isinstance(receiver, dict):
        return []
    out = [(f, receiver[f]) for f in receiver_bool_fields(rtype)
           if f in receiver and not is_bool_or_unset(receiver[f])]
    for field, prop in _receiver_field_schemas().get(rtype, {}).items():
        if prop.get("ref") != _HTTP_CONFIG_REF:
            continue
        http_config = receiver.get(field)
        if (isinstance(http_config, dict) and "proxy_from_environment" in http_config
                and not is_bool_or_unset(http_config["proxy_from_environment"])):
            out.append((f"{field}.proxy_from_environment",
                        http_config["proxy_from_environment"]))
    return out


#: What a message shows in place of a receiver value that may be a
#: credential (``receiver_value_repr``).
MASKED_VALUE = "<masked>"


def _step_matches(part: Any, step: str) -> bool:
    if step == "*":
        return isinstance(part, str)
    if step == "[]":
        return isinstance(part, int)
    return part == step


def is_credential_path(rtype: Any, path: tuple) -> bool:
    """#1560 ⑤-b: whether a value at receiver-relative *path* (keys and list
    indices) is on, below or above a credential place of receiver type
    *rtype* — a row of ``CREDENTIAL_REF_SITES`` or of
    ``CREDENTIAL_PLAINTEXT_SITES``, the ones that allow a written value
    included. Above: a value written where a mapping holding a credential
    is expected (``http_config: "user:pass"``) may be that credential. A
    type that is no receiver type has no table to read: every path is."""
    rows = ([s.path for s in CREDENTIAL_REF_SITES if s.rtype == rtype]
            + [s.path for s in CREDENTIAL_PLAINTEXT_SITES if s.rtype == rtype])
    if not isinstance(rtype, str) or not rows:
        return True
    return any(all(_step_matches(p, st) for p, st in zip(path, site)) for site in rows)


#: A string ``shown_value`` prints as it is (owner decision, #1560 ⑤-b
#: round 4): at most 64 characters of ``[A-Za-z0-9_.-]`` — a label, a
#: tenant or profile name, a duration (``1h30m``). Stricter than the
#: decision's example on purpose: no ``/`` (a webhook path without its
#: scheme, ``hooks.slack.com/services/T/B/X``, is a credential), no ``:``
#: (``user:pass``), no ``=`` / ``+`` (base64 tokens), so no ``@`` / ``://``.
_NAME_LIKE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NO_RTYPE = object()


def shown_value(value: Any, *, rtype: Any = _NO_RTYPE, path: tuple = (),
                bare: bool = False) -> str:
    """⛔ THE one exit of a config value into a message (#1560 ⑤-b, owner
    decision round 4). Messages reach stderr, ``--findings-json`` and
    tenant-api's log; the dynamic canary matrix
    (tests/ops/test_routing_value_canary_matrix.py) is what guards it.

    * a receiver value (*rtype* given) on, below or above a credential place
      (``is_credential_path``): ``MASKED_VALUE``;
    * a list or mapping: its type and length, never its content;
    * a string: its repr when it is name-like (``_NAME_LIKE_RE``), else
      ``a string of N chars``;
    * a number, boolean, null, date: its repr.

    *bare*: a name-like string without the quotes (a label, a receiver type
    inside a message that quotes it itself)."""
    if rtype is not _NO_RTYPE and is_credential_path(rtype, path):
        return MASKED_VALUE
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"a list of {len(value)} item(s)"
    if isinstance(value, dict):
        return f"a mapping of {len(value)} key(s)"
    if isinstance(value, str):
        if value == "" or _NAME_LIKE_RE.fullmatch(value):  # "" holds nothing
            return value if bare else repr(value)
        return f"a string of {len(value)} chars"
    return repr(value)


def shown_key(key: Any) -> str:
    """⛔ THE one exit of a name the config author wrote that LOCATES
    something (a mapping key, a tenant id, a profile or policy name) into a
    message or a structured path — keys are content too (#1560 ⑤-b round 5).

    A name-like string (``_NAME_LIKE_RE``) is itself. Anything else — the
    empty string and a key that is not a string included — is
    ``<key:xxxxxx>``: the first 6 hex chars of SHA-256 of its UTF-8 text
    (``str()`` of a non-string). Not the text, and not only its length
    (#3703): the same name is the same stand-in wherever it stands, two
    names almost never share one, so two findings about two such names
    stay two findings (tenant-api's ``findingKey``)."""
    if isinstance(key, str) and _NAME_LIKE_RE.fullmatch(key):
        return key
    text = key if isinstance(key, str) else str(key)
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"<key:{digest[:6]}>"


#: A segment ``shown_key`` already replaced: ``shown_file`` keeps it, so
#: showing a shown path again is the same path (a ``Finding`` rebuilt from
#: another one's fields), not a stand-in of a stand-in.
_STAND_IN_RE = re.compile(r"<key:[0-9a-f]{6}>")


def shown_file(path: Any) -> "str | None":
    """⛔ THE one exit of a conf.d path into a message or a structured field
    (``Finding.file``) — the config author names the files and directories
    of the tree, so a name is content too (#3703, owner decision).

    The path is relative to the conf.d root (``team-a/db-x.yaml``); each
    ``/``-separated segment is ``shown_key`` — the same name rule, the same
    ``<key:xxxxxx>`` stand-in — and the segments are joined again with
    ``/``. A segment that is already a stand-in stays as it is, so the exit
    is idempotent. ``None`` is ``None``. ``--config-dir`` is the operator's
    argument, not the author's text: it does not come here."""
    if path is None:
        return None
    return "/".join(seg if _STAND_IN_RE.fullmatch(seg) else shown_key(seg)
                    for seg in str(path).split("/"))


def key_segment(key: Any) -> str:
    """One segment of a structured path (``Finding.field``, ``policy``) that
    the config author wrote: ``shown_key`` — a path never carries a URL a
    key may be (#1560 ⑤-b round 5)."""
    return shown_key(key)


def receiver_value_repr(rtype: Any, path: tuple, value: Any) -> str:
    """``shown_value`` of a receiver's value at *path*."""
    return shown_value(value, rtype=rtype, path=path)


def scalar_text(value: Any) -> str:
    """``shown_value`` of a config value that is not a receiver's."""
    return shown_value(value)


def value_text(value: Any) -> str:
    """``<type> <shown_value>`` of a number, boolean or name-like string
    (``int 8``, ``str 'x'``); just ``shown_value`` otherwise."""
    shown = shown_value(value)
    if isinstance(value, (list, tuple, set, frozenset, dict)) or shown.startswith("a string of"):
        return shown
    return f"{type(value).__name__} {shown}"


def receiver_type_name(rtype: Any, path: tuple, value: Any) -> str:
    """``_type_name`` of a receiver value at *path*, its text through
    ``receiver_value_repr``."""
    return _type_name(value, shown=receiver_value_repr(rtype, path, value))


def _type_name(value: Any, *, shown: str) -> str:
    """YAML-ish name of a decoded value's type, for messages. A string shows
    *shown*, never its own text: a receiver value goes through
    ``receiver_type_name``, a Secret reference's parts are always masked."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"string {shown}"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def _http_config_problem(value: Any, rtype: str = "webhook") -> Optional[str]:
    """First problem of an ``http_config`` value, or ``None`` (#2295).

    Alertmanager's own rule (prometheus/common HTTPClientConfig / ProxyConfig
    as Alertmanager 0.34.1 loads them), stricter only where the shared case table
    says ``strict``:

    - null is unset; otherwise a mapping;
    - ``HTTP_CONFIG_AUTH_MAPPINGS`` keys: null is unset, else a mapping (set
      even when empty); the other ``HTTP_CONFIG_AUTH_FIELDS``: null and ``""``
      are unset, else a string (a number or date is refused: PyYAML has
      already rewritten its text, '0123' → 83) or a Secret reference
      (#1560, set); at most one set;
    - ``proxy_url`` / ``no_proxy``: null and ``""`` are unset; else a string
      (strict for the same reason). Whether a proxy_url
      parses as a URL is NOT checked here: that is Go's net/url.Parse, and the
      generator's ``--validate`` hands the rendered config to Alertmanager's
      loader (da-read), which runs it;
    - ``proxy_from_environment`` is a boolean or null (#2906: never a
      string, whatever it says — ``is_bool_or_unset``); true together with a
      non-empty proxy_url or a no_proxy is refused; no_proxy needs a
      proxy_url key; proxy_connect_header needs a non-empty proxy_url or
      proxy_from_environment.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return (f"field 'http_config' must be a mapping, got "
                f"{receiver_type_name(rtype, ('http_config',), value)}")
    set_keys = []
    for key in HTTP_CONFIG_AUTH_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if key in HTTP_CONFIG_AUTH_MAPPINGS:
            if not isinstance(item, dict):
                return (f"field 'http_config.{key}' must be a mapping, "
                        f"got {receiver_type_name(rtype, ('http_config', key), item)}")
            set_keys.append(key)
        elif is_secret_ref(item):
            # #1560: bearer_token as a Secret reference renders as
            # bearer_token_file, which conflicts the same way; its shape is
            # _receiver_secret_ref_problem's.
            set_keys.append(key)
        elif not isinstance(item, str):
            return (f"field 'http_config.{key}' must be a string, "
                    f"got {receiver_type_name(rtype, ('http_config', key), item)}")
        elif item != "":
            set_keys.append(key)
    if len(set_keys) > 1:
        return (f"http_config sets {', '.join(set_keys)}; Alertmanager accepts at "
                f"most one of {', '.join(HTTP_CONFIG_AUTH_FIELDS)}")
    proxy = value.get("proxy_url")
    proxy_key = proxy is not None
    if proxy is not None and not isinstance(proxy, str):
        return ("field 'http_config.proxy_url' must be a string, got "
                f"{receiver_type_name(rtype, ('http_config', 'proxy_url'), proxy)}")
    proxy_text = proxy is not None and proxy != ""
    from_env = False
    if "proxy_from_environment" in value:
        pfe = value["proxy_from_environment"]
        if not is_bool_or_unset(pfe):
            return (f"field 'http_config.proxy_from_environment' must be true or "
                    f"false, got "
                    f"{receiver_type_name(rtype, ('http_config', 'proxy_from_environment'), pfe)}")
        from_env = pfe is True
    no_proxy_value = value.get("no_proxy")
    if no_proxy_value is not None and not isinstance(no_proxy_value, str):
        return ("field 'http_config.no_proxy' must be a string, got "
                f"{receiver_type_name(rtype, ('http_config', 'no_proxy'), no_proxy_value)}")
    no_proxy = no_proxy_value is not None and no_proxy_value != ""
    header = value.get("proxy_connect_header")
    if header is not None and not isinstance(header, dict):
        return (f"field 'http_config.proxy_connect_header' must be a mapping, "
                f"got {receiver_type_name(rtype, ('http_config', 'proxy_connect_header'), header)}")
    if header and not from_env and not proxy_text:
        return "http_config: proxy_connect_header needs a non-empty proxy_url or proxy_from_environment: true"
    if from_env and proxy_text:
        return "http_config: proxy_url must not be set together with proxy_from_environment: true"
    if from_env and no_proxy:
        return "http_config: no_proxy must not be set together with proxy_from_environment: true"
    if no_proxy and not proxy_key:
        return "http_config: no_proxy needs a proxy_url"
    return None


# #3257: the receiver keys that make Alertmanager load something other than
# the value written — a file inside its own container (`*_file`, and the
# `files` list of an `http_headers` entry), or a secret through a secret
# manager Alertmanager does not have (`*_ref`: the config loads, then building
# the receiver fails and the whole reload with it). A receiver written in the
# tenant configs (tenant, defaults, overrides, enforced, profiles) must not
# carry any of them, at any depth. A RULE, not a list of today's keys
# (cf. Grafana Mimir's validateFileLoad, which refuses `_file` the same way):
# a key Alertmanager adds upstream is covered without a change here. Pinned
# by tests/shared/test_receiver_file_reference_rule.py (the schema has no
# property the rule matches anywhere a receiver reaches) and by
# tests/alertmanager-inhibit (the schema's http_config key sets are
# Alertmanager's minus exactly these keys). A secret the platform itself
# needs belongs in the base config (--base-config), which this rule never
# reads.
# ONE pattern, the very string tenant-config.schema.json carries as
# definitions.fileReferenceKey.pattern (the test asserts the two are equal),
# applied with `re.search` as jsonschema applies it — so both sides give one
# verdict on every key, `"password_file\n"` included (Python's `$` also
# matches before a final newline: refused on both sides, not on one).
FILE_REFERENCE_KEY_PATTERN = r"(?:_file|_ref)$|^files$"
_FILE_REFERENCE_KEY_RE = re.compile(FILE_REFERENCE_KEY_PATTERN)


def is_file_reference_key(key: Any) -> bool:
    """#3257: whether *key* is a receiver key the rule above refuses."""
    return isinstance(key, str) and _FILE_REFERENCE_KEY_RE.search(key) is not None


def file_reference_paths(value: Any, path: str = "", *,
                         label_maps: frozenset[str] = frozenset(),
                         receiver_keys: frozenset[str] = frozenset()) -> list[str]:
    """#3257: the path of every key of *value* (a receiver, walked through
    every nested mapping and list) that ``is_file_reference_key``: dotted,
    list items as ``[i]`` — ``http_config.basic_auth.password_file``,
    ``http_config.http_headers.X-Token.files``. The value under such a key
    does not matter (null and ``""`` included): the key itself is refused,
    as the schema refuses it (no such property, additionalProperties: false).
    A key that is not a string (a ``!!binary`` key reads as bytes, and
    Alertmanager decodes ``!!binary password_file`` to ``password_file``) is
    refused too: the rule cannot read its name, so it does not lean on the
    file reader having refused the tag first.

    *label_maps*: keys whose mapping's own keys are NAMES, not config keys
    (a routing block's ``match``: label names, which may legitimately end in
    ``_file``). Under such a key the names are not judged at all (Alertmanager
    reads no file through a matcher; a name of the wrong type is the routing
    checks' to name); every value is still walked with the full rule.
    *receiver_keys*: keys whose value is a receiver (a routing block's
    ``receiver``). Below one, *label_maps* no longer applies: inside a
    receiver ``match`` is an ordinary key (a header named ``match`` under
    ``http_headers``), so ``http_headers.match.files`` is refused."""
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = key_segment(key)
            here = f"{path}.{name}" if path else name
            if not isinstance(key, str) or is_file_reference_key(key):
                out.append(here)
            elif key in label_maps and isinstance(item, dict):
                for label, v in item.items():
                    lname = key_segment(label)
                    out.extend(file_reference_paths(v, f"{here}.{lname}", label_maps=label_maps,
                                                    receiver_keys=receiver_keys))
            elif key in receiver_keys:
                out.extend(file_reference_paths(item, here))
            else:
                out.extend(file_reference_paths(item, here, label_maps=label_maps,
                                                receiver_keys=receiver_keys))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(file_reference_paths(item, f"{path}[{i}]", label_maps=label_maps,
                                            receiver_keys=receiver_keys))
    return out


def file_reference_problem(paths: list[str]) -> Optional[str]:
    """#3257: the message for the ``file_reference_paths`` of one receiver,
    or ``None`` when there are none."""
    if not paths:
        return None
    names = ", ".join(f"'{p}'" for p in paths)
    return (f"sets {names}: a key ending in _file or _ref, or a 'files' list, "
            "makes Alertmanager load a file or secret from inside its own "
            "container, so a tenant-config receiver may not carry one; write the value "
            "itself, or have the platform set it in the base config")


def receiver_file_reference_problem(receiver: Any) -> Optional[str]:
    """#3257: the one judgment of the rule above for a receiver — the
    problem (callers prefix tenant / receiver context), or ``None``."""
    return file_reference_problem(file_reference_paths(receiver))


def receiver_optional_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """Check the optional values Alertmanager cannot load (#2295).

    Read from tenant-config.schema.json like the required-field formats:

    - every property of the type referencing ``yamlBool`` (``send_resolved``,
      email ``require_tls``) must be a boolean or null (unset) —
      ``send_resolved: maybe`` makes Alertmanager refuse the whole config,
      and since #2906 a string is refused whatever it says (``"yes"``,
      ``"true"``): the generator judges by type (ADR-036 §2). The route
      generator refuses the whole tree on these before any receiver is
      built (``receiver_bool_problems``); this check stays for the callers
      that build a receiver directly;
    - the property referencing ``httpConfigOrNull`` (webhook ``http_config``)
      follows ``_http_config_problem``;
    - before both, #3257: no key at any depth may be a file reference
      (``receiver_file_reference_problem``).

    Returns the first problem (callers prefix tenant / receiver context), or
    ``None``. Cases in
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json.
    """
    # #3257: first — a file-reference key is never written out, whatever
    # else the receiver gets wrong. The route generator refuses the whole
    # tree on it before any receiver is built
    # (`_grar_validate.routing_block_file_reference_errors`); this check stays for
    # the callers that build a receiver directly.
    problem = receiver_file_reference_problem(receiver)
    if problem:
        return problem
    # #1560: a Secret reference of the wrong shape, or on a field that does
    # not take one. Which Secret it may name is the generator's binding check.
    problem = _receiver_secret_ref_problem(rtype, receiver)
    if problem:
        return problem
    fields = _receiver_field_schemas().get(rtype, {})
    for field in receiver_bool_fields(rtype):
        if field in receiver and not is_bool_or_unset(receiver[field]):
            return (f"field '{field}' must be true or false, "
                    f"got {receiver_type_name(rtype, (field,), receiver[field])}")
    for field, prop in fields.items():
        if prop.get("ref") == _HTTP_CONFIG_REF and field in receiver:
            problem = _http_config_problem(receiver[field], rtype)
            if problem:
                return problem
    return None


def receiver_required_problem(rtype: str, receiver: dict[str, Any], field: str) -> Optional[str]:
    """Check one ``required`` field of ``RECEIVER_TYPES[rtype]`` (#2180).

    Presence is ``receiver_field_state`` (unset = missing, null or ``""``);
    everything else follows the field's type in tenant-config.schema.json:

    - a string field must be a string, and match the schema's ``pattern``
      when it has one (``re.fullmatch``: Python's ``$`` in ``re.search``
      also matches before a final newline, which ECMA and Go RE2 do not);
    - an array field (email ``to``) given as a list needs at least one
      item, each a non-empty string — the list is joined into
      Alertmanager's ``to`` string, where ``[""]`` reads as no address.
      A plain string is still taken for it: Alertmanager's own ``to`` is a
      string (the schema alone rejects that form).

    Returns the problem (callers prefix tenant / receiver context), or
    ``None``. Cases in
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json.
    """
    state = receiver_field_state(receiver, field)
    if state == FIELD_UNSET:
        return f"requires '{field}'"
    value = receiver[field]
    prop = _receiver_field_schemas().get(rtype, {}).get(field, {})
    if is_secret_ref(value):
        # #1560: the value is in a Secret; its shape is all that is judged
        # here (the URL format applies to the value, which is not visible).
        if field not in receiver_secret_ref_fields(rtype):
            return (f"field '{field}' must be a string; it does not take a "
                    "Secret reference")
        problem = secret_ref_problem(value)
        return f"field '{field}' {problem}" if problem else None
    if state == FIELD_NOT_STRING:
        if not (prop.get("type") == "array" and isinstance(value, list)):
            return f"field '{field}' must be a string, got {type(value).__name__}"
        if not value:
            return f"requires '{field}'"
        for i, item in enumerate(value):
            if not isinstance(item, str) or item == "":
                return (f"field '{field}' item {i} must be a non-empty string, got "
                        f"{receiver_value_repr(rtype, (field, i), item)}")
        return None
    pattern = prop.get("pattern")
    if pattern and re.fullmatch(pattern, value) is None:
        return (f"field '{field}' value {receiver_value_repr(rtype, (field,), value)} "
                "is not in the format "
                "tenant-config.schema.json requires")
    return None


def i18n_text(zh: str, en: str) -> str:
    """Return *zh* or *en* based on ``detect_cli_lang()`` result.

    Intended as a lightweight ``window.__t`` equivalent for Python CLI tools,
    replacing ad-hoc ``msg = zh if _LANG == 'zh' else en`` patterns.
    """
    return zh if detect_cli_lang() == "zh" else en
