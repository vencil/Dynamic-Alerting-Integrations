#!/usr/bin/env python3
"""Describe effective tenant config — resolve _defaults.yaml inheritance chain.

Usage:
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> [--conf-d PATH]
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --show-sources
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --diff <tenant-id-2>
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --what-if <outside/draft.yaml>
    python3 scripts/tools/dx/describe_tenant.py <tenant-id> --what-if <copy.yaml> --replaces <conf.d/team/_defaults.yaml>
    python3 scripts/tools/dx/describe_tenant.py --all --conf-d PATH --output effective.json

Resolves the full inheritance chain (L0→L1→L2→L3→tenant) using deep merge
with override semantics (ADR-017). Array fields are replaced, not concatenated.

Output: JSON or YAML of the effective (merged) config.
"""
import argparse
import binascii
import codecs
import copy
import datetime
import decimal
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout, PROJECT_ROOT_MARKERS  # noqa: E402
from _lib_confd import (  # noqa: E402  (#1588 shared name predicates)
    duplicate_declarations,
    has_yaml_extension,
    is_defaults_name,
    is_dir_symlink,
    is_reserved_name,
    list_config_tree,
    readable_carriers,
    select_defaults_carrier,
    warn_dir_symlink_once,
    warn_multi_carrier,
    unusable_reason,
)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)
# #2123: conf.d YAML is read strictly — a key written twice in one mapping
# raises (the exporter's yaml.v3 rejects that file) instead of last-wins.
from _lib_io import strict_safe_load_all  # noqa: E402
# #2114: tenant ids as the exporter keys them (raw text), on the same strict
# reading — `_lib_io` composes the two loaders. #2371: this tool reads every
# conf.d file through a subclass of it, `_GoKeyLoader`.
from _lib_io import StrictExporterKeyLoader  # noqa: E402
# #2117: the #1231 alias boundary (deprecated key spelling → canonical), for
# profile expansion's "does the tenant already set this key" check. The
# repo-layout path is `ops/`; the image is flat (build.sh ships both).
# Appended, not prepended, so no `ops/` module can shadow one already found.
sys.path.append(os.path.join(str(_THIS_DIR), "..", "ops"))
from _grar_validate import (  # noqa: E402
    _canonical_tenant_key,
    _legacy_tenant_key,
    is_null_schedule,
    overlay_across_spellings,
    writes_nothing,
)

try:
    import yaml
except ImportError:
    yaml = None  # fallback to simple parser

# #772: `_custom_alerts` does NOT follow generic array-REPLACE (ADR-017) — ADR-024
# defines it as UNION across the inheritance chain (a tenant's own list ADDS to
# inherited platform/domain policy recipes; a tenant must NOT silently wipe a
# platform policy). deep_merge alone (REPLACE) would drop the inherited policy
# from an override tenant's effective view, blinding blast_radius. So we delegate
# THIS one field to the compiler's own inheritance resolver (`collect_instances`)
# — the single source of truth — instead of forking the union logic here.
try:
    from custom_alerts import loader as _ca_loader  # noqa: E402
except Exception:  # pragma: no cover - compiler package unavailable → graceful fallback
    _ca_loader = None

# ---------------------------------------------------------------------------
# Deep merge logic (ADR-017 semantics)
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override wins for scalars and arrays; dicts recurse.

    ADR-017 rules:
    - Dict fields: deep merge (child adds new keys, overrides same keys)
    - Array fields: REPLACE (not concat)
    - Scalar fields: child overrides parent
    - Explicit None/null: deletes parent's key — RESERVED (`_`-prefixed) keys
      only. On a threshold key it is a no-op: the exporter's emitting path
      (collector.go -> ResolveAtWithStats) ignores the null and falls back to
      the platform default, so a diagnostic that deleted the key would
      contradict what /metrics is actually emitting (#1339 P0). Use "disable"
      to stop alerting on a threshold key.
    - `{default: null}` with no override window on a threshold key is that
      same null (#2708, Go `nullSchedule`).
    - _metadata: never inherited (skipped in merge)

    MUST stay in lockstep with pkg/config/hierarchy.go deepMerge — the golden
    fixtures under tests/golden/ pin both against the same expected output.
    """
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k == "_metadata":
            continue  # _metadata is never inherited
        if v is None:
            if k.startswith("_"):
                result.pop(k, None)  # explicit null = opt-out (reserved keys)
            # threshold key: null is NOT an opt-out — keep the inherited value
            continue
        if is_null_schedule(v) and not (isinstance(k, str) and k.startswith("_")):
            continue  # #2708: `{default: null}` is the null above
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Load a YAML file (a `_defaults.yaml`, or the `--what-if` file),
    returning its parsed dict. Keys are source text carrying the exporter's
    spelling (`_GoKeyLoader`, #2371) — the tenant files' key identity, so a
    key merges across the two exactly when its text matches.

    #2459: only the FIRST document is read, as yaml.v3 `Unmarshal` does —
    the exporter serves a multi-document `_defaults.yaml` from its first
    document, where this used to refuse the whole file.

    The document's shape is checked before the chain uses it. The two
    shapes below parse, but this tool refuses them (`_UnsupportedShape`,
    reported as `DefaultsShapeError`: the file named, rc 2) — both used to
    end the run with an AttributeError traceback. What the exporter does
    with them depends on the level (measured against LoadDir and
    ResolveEffective):

      * a document that is not a mapping (a list, a scalar, `false`): at
        the ROOT the flat load puts the file in parse_failed and the merge
        skips it; in a SUB-DIRECTORY the merge skips it and the flat load
        does not report it. Either way nothing is served from it, but the
        file is broken, and refusing says so (owner's ruling on #2459);
      * a `defaults:` that is neither a mapping nor null: at the ROOT the
        flat load drops the whole file while the merge takes the WHOLE
        document as the defaults block — no answer both give. In a
        SUB-DIRECTORY both take the whole document (`defaults: [1]` beside
        `mysql_connections: 81` serves 81), so an answer exists there; it is
        refused all the same, on one rule for every level, and the message
        says the shape is unsupported rather than that the file does not
        parse;
      * empty, or `defaults: ~`: no defaults, in both planes — not refused.
    """
    if not yaml:
        # Minimal fallback — only works for simple flat YAML
        raise RuntimeError(f"PyYAML is required for describe-tenant. Install: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    doc = next(strict_safe_load_all(content, loader=_GoKeyLoader), None)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise _UnsupportedShape(f"the document is a {type(doc).__name__}, not a mapping")
    inner = doc.get("defaults")
    if inner is not None and not isinstance(inner, dict):
        raise _UnsupportedShape(f"'defaults' is a {type(inner).__name__}, not a mapping")
    return doc


class _UnsupportedShape(ValueError):
    """A defaults document that parses but that this tool refuses to
    describe (`_load_yaml`, #2459)."""


def _defaults_block(ddata: dict) -> dict:
    """The part of a loaded defaults document the chain merges — Go's
    extractDefaultsBlock: its `defaults:` mapping, else the whole document
    (a legacy flat file; also `defaults: ~`, whose null key the merge then
    skips, as Go's does)."""
    inner = ddata.get("defaults")
    return inner if isinstance(inner, dict) else ddata


class DefaultsParseError(Exception):
    """A selected `_defaults.yaml` carrier that does not parse (#2413).

    Unlike a tenant file (`_load_tenant_file`, skipped with a warning), a
    defaults level cannot be dropped: every tenant below it would be
    described without it — a wrong answer with rc 0. So the scan stops, and
    the CLI names the file and the YAML error on stderr with
    EXIT_CALLER_ERROR instead of a traceback. `path` is the entry as listed.
    """

    def __init__(self, path: Path, cause: BaseException):
        self.path = path
        self.cause = cause
        super().__init__(self._text(path, cause))

    @staticmethod
    def _text(path: Path, cause: BaseException) -> str:
        return f"{path} does not parse: {cause}"


class DefaultsShapeError(DefaultsParseError):
    """A selected `_defaults.yaml` carrier that parses but has a shape this
    tool does not describe (#2459, `_load_yaml`). A subclass so every
    caller that stops on `DefaultsParseError` stops on it too, with its
    own message: the file parses, and at some levels the exporter serves
    it, so "does not parse" would be wrong."""

    @staticmethod
    def _text(path: Path, cause: BaseException) -> str:
        return (f"{path} has an unsupported shape: {cause} — refused, as "
                f"this tool cannot tell that its answer would match the exporter's")


def _file_hash(path: Path) -> str:
    """SHA-256 of file bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# JSON for the values PyYAML types but JSON does not (#2371)
# ---------------------------------------------------------------------------
#
# An unquoted `2026-12-31` is a `date` to PyYAML and a `!!binary` value is
# `bytes`; `json.dumps` raised TypeError on both, so one such value anywhere
# in the tree ended every mode — `--all` included — with a traceback, while
# the exporter served the same tree. The oracle for what they should become
# is Go: yaml.v3 reads a timestamp into time.Time and a `!!binary` into a
# string of its raw bytes, and pkg/config's canonical JSON (encoding/json)
# writes them as below. merged_hash must match the exporter's byte for byte,
# so this is the exporter's rendering, not a nicer one.
#
#   timestamp  -> decided at READ time, not here: `_GoKeyLoader` applies
#                 yaml.v3's four layouts to the source text and yields the
#                 text encoding/json writes for that time.Time (RFC 3339,
#                 nanoseconds, "Z" for UTC) — or the source text itself where
#                 yaml.v3 reads no time. PyYAML's own timestamp rules differ
#                 both ways (a naive `T` value, a space before the zone, a
#                 9-digit fraction, `2026-1-2`, `2026-12-31 23:59:59`), so
#                 its datetime objects cannot be rendered into Go's answer.
#   date / datetime objects from any other reader -> as time.Time JSON:
#                 "YYYY-MM-DDT00:00:00Z", or RFC 3339 with the offset
#   bytes      -> UTF-8 decoded; each byte that is not part of a valid
#                 sequence becomes encoding/json's six-character escape
#                 (backslash, then `ufffd`) in the JSON text, NOT a raw U+FFFD
#
# ⛔ Timestamp values the reader still cannot align (strict xfails in
# tests/dx/test_describe_tenant.py, TestYamlTypedScalarParity):
#   - an explicit `!!timestamp` yaml.v3 cannot parse, on a text PyYAML
#     would have tagged `!!timestamp` anyway (`!!timestamp
#     2026-12-31T10:20:30`, `!!timestamp 2026-13-01`): yaml.v3 refuses the
#     FILE; here the tag is invisible and it is the text. (`!!timestamp foo`
#     and other visibly explicit ones ARE refused — `_construct_timestamp`.)
#   - an explicit `!!str` on a text PyYAML itself reads as a string but
#     yaml.v3 as a time (`!!str 2026-1-2`): indistinguishable from the plain
#     scalar once composed, so it is rendered as the time.
#   - a tab between date and time: PyYAML's scanner refuses the file
#     (`found character '\t'`), yaml.v3 keeps the text. A parser difference
#     (#2123), not a typing one.
#   - a zone offset of 24h or more (`+24:00`, `-23:60`): yaml.v3 parses it,
#     but Go's CanonicalJSON fails once the value lands in a tenant's
#     effective config (no merged_hash); here it is rendered as a time.
#   - an explicit `!!timestamp` that is not a time on a KEY or tenant ID
#     (`!!timestamp foo: 1`): yaml.v3 refuses the file; here the key is
#     its text. The refusal above covers VALUES only.
#
# Mapping KEYS are a separate path (json's `default` is never called for a
# key): see `_GoKey` below.
_GO_INVALID_UTF8 = "\udfff"
"""Stand-in for one invalid byte while `json.dumps` runs; `_json_text`
turns it into encoding/json's `\\ufffd` escape afterwards. A lone surrogate
because no str this tool reads can carry one: `_GoKeyLoader` refuses the
`"\\udfff"` escape PyYAML would accept (`_reject_surrogates`). Belt and
braces, `_json_text` still replaces only when every occurrence is one it
inserted."""


def _go_invalid_utf8_handler(exc: UnicodeError):
    # Resume ONE byte later, as Go's utf8 decoding does: b"\xe4\xb8" (a
    # truncated 3-byte sequence) is two replacements there, one under
    # Python's built-in "replace" handler.
    return _GO_INVALID_UTF8, exc.start + 1


codecs.register_error("describe_tenant.go_invalid_utf8", _go_invalid_utf8_handler)


def _go_time_text(value: "datetime.datetime") -> str:
    """`value` as encoding/json writes a time.Time (RFC 3339 with nanosecond
    precision): trailing zeros of the fraction dropped, `Z` for UTC."""
    text = (f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
            f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}")
    if value.microsecond:
        text += f".{value.microsecond:06d}".rstrip("0")
    offset = value.utcoffset()
    if offset is None:
        return text
    if not offset:
        return text + "Z"
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    return text + f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _go_json_default(value: Any) -> Any:
    """`json.dumps(default=...)` for the YAML scalars JSON has no type for,
    rendered as the exporter's canonical JSON renders them (see above)."""
    if isinstance(value, datetime.datetime):
        return _go_time_text(value)
    if isinstance(value, datetime.date):
        return f"{value.isoformat()}T00:00:00Z"
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="describe_tenant.go_invalid_utf8")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_text(data: Any, **kwargs: Any) -> str:
    """`json.dumps(data, ensure_ascii=False, **kwargs)` through
    `_go_json_default` — the one JSON writer for the hash and the output."""
    inserted = 0
    floats: "list[str]" = []

    def default(value: Any) -> Any:
        nonlocal inserted
        if isinstance(value, _GoFloatSlot):
            return f"{_GO_FLOAT_MARK}{value.index}{_GO_FLOAT_MARK}"
        rendered = _go_json_default(value)
        if isinstance(value, (bytes, bytearray)):
            inserted += rendered.count(_GO_INVALID_UTF8)
        return rendered

    text = json.dumps(_slot_floats(data, floats), ensure_ascii=False, default=default, **kwargs)
    if floats:
        text, found = _GO_FLOAT_SLOT.subn(lambda m: floats[int(m.group(1))], text)
        if found != len(floats):  # pragma: no cover - a mark only this function writes
            raise ValueError(f"float placeholders: wrote {len(floats)}, found {found}")
    if inserted and text.count(_GO_INVALID_UTF8) == inserted:
        text = text.replace(_GO_INVALID_UTF8, "\\ufffd")
    return text


# #2415: `json.dumps` writes a float with repr() (`1.0`, `1e+20`, `1.5e-05`)
# and a float subclass cannot change that, so each finite float VALUE goes
# through `default` as a slot and its encoding/json text (`_go_float_json`)
# replaces the quoted mark afterwards. The mark is a lone surrogate, which no
# str this tool reads can hold (`_reject_surrogates`). NaN / ±Inf stay
# floats: json.dumps' bare NaN / Infinity / -Infinity is exactly what
# pkg/config's canonical JSON writes for them.
_GO_FLOAT_MARK = "\udffe"
_GO_FLOAT_SLOT = re.compile(f'"{_GO_FLOAT_MARK}([0-9]+){_GO_FLOAT_MARK}"')


class _GoFloatSlot:
    __slots__ = ("index",)

    def __init__(self, index: int):
        self.index = index


def _slot_floats(data: Any, floats: "list[str]") -> Any:
    """`data` with every finite float value (not key) a `_GoFloatSlot`
    whose text is appended to `floats`."""
    if isinstance(data, float):
        if not math.isfinite(data):
            return data
        floats.append(_go_float_json(data))
        return _GoFloatSlot(len(floats) - 1)
    if isinstance(data, dict):
        return {k: _slot_floats(v, floats) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_slot_floats(v, floats) for v in data]
    return data


def _canonical_json(data: Any) -> str:
    """The canonical JSON merged_hash is computed over — pkg/config's
    CanonicalJSON: sorted keys, no spaces, no HTML or non-ASCII escaping."""
    return _json_text(_go_keys(data), sort_keys=True, separators=(",", ":"))


def _canonical_hash(data: dict) -> str:
    """SHA-256 of canonical JSON representation."""
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Mapping keys as the exporter spells them (#2371)
# ---------------------------------------------------------------------------
#
# Below a tenant id, yaml.v3 decodes a mapping into `map[any]any` as soon as
# one key is not a string, and pkg/config then spells every key with
# `fmt.Sprintf("%v", k)`: an unquoted `2026-12-31:` is
# "2026-12-31 00:00:00 +0000 UTC" (time.Time.String()), `010:` is "8",
# `1.0:` is "1", `True:` is "true". merged_hash is computed over those
# spellings. This tool reads keys as their source TEXT (#2114 — tenant ids
# must stay text, and so the merge's key identity is text on both the tenant
# and the `_defaults.yaml` side), so `_GoKeyLoader` hands out keys that ARE
# that text but also carry the exporter's spelling, and `_go_keys` swaps it
# in wherever a structure leaves the tool (hash and output alike). A key
# written in a `_defaults.yaml` was a PyYAML-typed object before; a `date`
# there ended every mode with "keys must be str…".
#
# ⛔ What this does NOT align (tests/dx/test_describe_tenant.py
# TestYamlMappingKeyParity pins each as a strict xfail):
#   - a null key (`~:`, `null:`): yaml.v3 keeps it as "<nil>"; the #2114
#     key reader drops it, as the exporter does for a tenant id.
#   - two spellings of one Go key (`010:` in the defaults, `8:` in the
#     tenant file): one key to Go, so their values merge; two keys here, and
#     the later one wins where they meet in `_go_keys`.
#   - a `+00:00` offset: time.Parse puts it in time.Local when Local's
#     offset is zero, so its String() depends on the exporter's TZ. Rendered
#     for a UTC Local ("+0000 UTC"), what the shipped image runs with.
#   - an explicit tag PyYAML resolves the same way implicitly (`!!str 1e3`):
#     indistinguishable from the plain scalar once composed.


class _GoKey(str):
    """A mapping key: its source text (identity, hashing, equality — so the
    merge and every lookup behave exactly as the #2114 text keys do), plus
    `go`, the spelling pkg/config gives it."""

    __slots__ = ("go",)

    def __new__(cls, text: str, go: str):
        self = super().__new__(cls, text)
        self.go = go
        return self

    def __reduce__(self):  # deepcopy (deep_merge) keeps the spelling
        return (_GoKey, (str.__str__(self), self.go))


_GO_INT_DIGITS = {16: re.compile(r"[0-9a-fA-F]+"), 10: re.compile(r"[0-9]+"),
                  8: re.compile(r"[0-7]+"), 2: re.compile(r"[01]+")}
# yaml.v3 resolve.go `yamlStyleFloat`.
_GO_YAML_FLOAT = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
# yaml.v3 `allowedTimestampFormats`, as time.Parse reads them: 1-2 digit
# month / day / hour / minute / second, any number of fraction digits, a run
# of spaces where the layout has one.
# ⛔ Used with `fullmatch` only, and `[0-9]` not `\d`: `$` would accept a
# trailing "\n" (a `|` block scalar's `2026-12-31\n`, which yaml.v3 keeps as
# text) and `\d` would accept non-ASCII digits time.Parse does not.
_GO_TS_ZONED = re.compile(
    r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})[Tt]([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})"
    r"(?:[.,]([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})")
_GO_TS_SPACE = re.compile(
    r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2}) +([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})"
    r"(?:[.,]([0-9]+))?")
_GO_TS_DATE = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
# time.Parse's own zone bounds (`Z07:00`): an hour above 24 or a minute
# above 60 is a parse error, so yaml.v3 keeps the text; within them the
# offset is hours*60 + minutes, so `+08:60` IS +09:00.
_GO_ZONE_MAX_HOUR, _GO_ZONE_MAX_MINUTE = 24, 60


def _go_parse_int(text: str) -> "int | None":
    """strconv.ParseInt(text, 0, 64) — then ParseUint for an unsigned text —
    as yaml.v3 tries them, underscores already removed."""
    sign, body = "", text
    if body[:1] in "+-":
        sign, body = body[0], body[1:]
    base = 10
    if len(body) > 1 and body[0] == "0":
        prefix = body[1].lower()
        base, body = {"x": (16, body[2:]), "o": (8, body[2:]),
                      "b": (2, body[2:])}.get(prefix, (8, body[1:]))
    if not body or not _GO_INT_DIGITS[base].fullmatch(body):
        return None
    value = int(body, base) * (-1 if sign == "-" else 1)
    if -(1 << 63) <= value < (1 << 63):
        return value
    if not sign and value < (1 << 64):  # ParseUint takes no sign at all
        return value
    return None


def _go_underscore_ok(text: str) -> bool:
    """strconv's underscoreOK: an `_` only between digits (or between a base
    prefix and a digit)."""
    if text[:1] in ("+", "-"):
        text = text[1:]
    saw, start, hexa = "^", 0, False
    if len(text) >= 2 and text[0] == "0" and text[1].lower() in "box":
        saw, start, hexa = "0", 2, text[1].lower() == "x"
    for ch in text[start:]:
        if "0" <= ch <= "9" or (hexa and ch.lower() in "abcdef"):
            saw = "0"
        elif ch == "_":
            if saw != "0":
                return False
            saw = "_"
        elif saw == "_":
            return False
        else:
            saw = "!"
    return saw != "_"


def _go_float_v(value: float) -> str:
    """fmt's `%v` of a float64: strconv 'g' at the shortest precision, which
    switches to an exponent below 1e-4 and from 1e+06 up (`%e` with at least
    two exponent digits)."""
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign, digs, exp10 = _go_float_digits(value)
    if exp10 < -4 or exp10 >= 6:
        return sign + _go_float_e(digs, exp10)
    return sign + _go_float_f(digs, exp10)


def _go_float_json(value: float) -> str:
    """encoding/json's text for a finite float64 (#2415) — what pkg/config's
    canonical JSON writes for a float VALUE, and not `%v`: strconv 'f' at the
    shortest precision, 'e' only below 1e-6 or from 1e21 up, and a one-digit
    negative exponent without its leading zero (`1e-7`, not `1e-07`). So
    `1.0` is `1`, `1e20` is `100000000000000000000` and `1.5e-5` is
    `0.000015`, where `json.dumps` writes `1.0`, `1e+20` and `1.5e-05`."""
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign, digs, exp10 = _go_float_digits(value)
    if abs(value) < 1e-6 or abs(value) >= 1e21:
        text = _go_float_e(digs, exp10)
        if -10 < exp10 < 0:  # encoding/json's "clean up e-09 to e-9"
            text = text[:-2] + text[-1]
        return sign + text
    return sign + _go_float_f(digs, exp10)


def _go_float_digits(value: float) -> "tuple[str, str, int]":
    """(sign, shortest round-trip digits, decimal exponent of the first
    digit) of a finite, non-zero float — strconv's shortest digits, which
    repr() also produces."""
    parts = decimal.Decimal(repr(abs(value))).normalize().as_tuple()
    digs = "".join(str(d) for d in parts.digits)
    return ("-" if value < 0 else ""), digs, len(digs) - 1 + parts.exponent


def _go_float_e(digs: str, exp10: int) -> str:
    """strconv's 'e' format of `digs` (at least two exponent digits)."""
    body = digs[0] + ("." + digs[1:] if len(digs) > 1 else "")
    return f"{body}e{'-' if exp10 < 0 else '+'}{abs(exp10):02d}"


def _go_float_f(digs: str, exp10: int) -> str:
    """strconv's 'f' format of `digs` (no exponent, no trailing zeros)."""
    point = exp10 + 1  # digits before the decimal point
    if point <= 0:
        return f"0.{'0' * -point}{digs}"
    if point >= len(digs):
        return f"{digs}{'0' * (point - len(digs))}"
    return f"{digs[:point]}.{digs[point:]}"


def _go_parse_timestamp(text: str) -> "tuple | None":
    """yaml.v3's parseTimestamp: `(y, mo, d, h, mi, s, frac, zone)` for a
    text one of its four layouts accepts, else None — including a field
    time.Parse refuses as out of range (month 13, Feb 30, hour 24, a zone
    hour above 24 or minute above 60), which leaves the scalar a plain
    string there. `frac` is at most 9 digits with trailing zeros dropped;
    `zone` is "Z" for a zero offset (time.Local is UTC in the shipped image)
    or the offset re-spelled "±HH:MM" — `+08:60` is "+09:00", as Go
    computes it; HH may be 24 or more (see `_GO_ZONE_MAX_HOUR`)."""
    m = _GO_TS_ZONED.fullmatch(text) or _GO_TS_SPACE.fullmatch(text)
    if m:
        fields = [int(g) for g in m.groups()[:6]]
        frac = (m.group(7) or "")[:9].rstrip("0")
        zone = m.group(8) if m.re is _GO_TS_ZONED else "Z"
    else:
        m = _GO_TS_DATE.fullmatch(text)
        if not m:
            return None
        fields, frac, zone = [int(g) for g in m.groups()] + [0, 0, 0], "", "Z"
    if not _go_valid_clock(*fields):
        return None
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > _GO_ZONE_MAX_HOUR or minutes > _GO_ZONE_MAX_MINUTE:
            return None
        total = hours * 60 + minutes
        zone = "Z" if not total else f"{zone[0]}{total // 60:02d}:{total % 60:02d}"
    return (*fields, frac, zone)


def _go_valid_clock(year, month, day, hour, minute, second) -> bool:
    """time.Parse's range checks, on the proleptic Gregorian calendar Go
    uses — year 0 included (a leap year), which `datetime` cannot hold."""
    if not 1 <= month <= 12 or hour > 23 or minute > 59 or second > 59:
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    return 1 <= day <= days


def _go_time_string(year, month, day, hour=0, minute=0, second=0,
                    frac: str = "", zone: str = "Z") -> str:
    """time.Time.String() — how `%v` spells a time.Time map key."""
    text = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
    frac = frac[:9].rstrip("0")
    if frac:
        text += "." + frac
    if zone in ("Z", "+00:00", "-00:00"):
        return text + " +0000 UTC"
    offset = zone.replace(":", "")
    return f"{text} {offset} {offset}"


def _go_time_json(year, month, day, hour, minute, second, frac, zone) -> str:
    """time.Time.MarshalJSON's text (RFC 3339, nanosecond precision) — how
    pkg/config's canonical JSON writes a time.Time VALUE."""
    text = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
    return text + (f".{frac}" if frac else "") + zone


def _go_timestamp_key(text: str) -> "str | None":
    parsed = _go_parse_timestamp(text)
    return _go_time_string(*parsed) if parsed else None


def _go_plain_key(text: str, timestamps: bool = True) -> str:
    """yaml.v3's resolution of an untagged plain scalar, spelled with `%v`."""
    if text in ("", "~", "null", "Null", "NULL"):
        return "<nil>"
    if text in ("true", "True", "TRUE"):
        return "true"
    if text in ("false", "False", "FALSE"):
        return "false"
    if text in (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"):
        return "+Inf"
    if text in ("-.inf", "-.Inf", "-.INF"):
        return "-Inf"
    if text in (".nan", ".NaN", ".NAN"):
        return "NaN"
    if text[0] not in "0123456789+-.":
        return text
    if timestamps and text[0].isdigit():
        stamp = _go_timestamp_key(text)
        if stamp is not None:
            return stamp
    number = _go_plain_number(text)
    if number is None:
        return text
    return str(number) if isinstance(number, int) else _go_float_v(number)


def _go_plain_number(text: str) -> "int | float | None":
    """yaml.v3's number resolution of an untagged plain scalar that starts
    with a digit, `+`, `-` or `.` and is not a timestamp: an int (ParseInt,
    then ParseUint, base prefixes `0x` / `0o` / `0b` / a leading `0`, any
    case, underscores dropped), else a finite float (`yamlStyleFloat`, then
    ParseFloat), else None — the text stays a string."""
    plain = text.replace("_", "")
    if text[0] == ".":
        # yaml.v3's `.` hint: strconv.ParseFloat on the text AS WRITTEN —
        # underscores are not stripped first, so they must pass Go's
        # underscoreOK (between digits only): `.5_0` is 0.5, `._5`, `.5_`,
        # `.5__0` and `.5e_1` stay strings.
        if not (_go_underscore_ok(text) and _GO_YAML_FLOAT.match(plain)):
            return None
    else:
        number = _go_parse_int(plain)
        if number is not None:
            return number
    if _GO_YAML_FLOAT.match(plain) and not math.isinf(float(plain)):
        # ParseFloat's range error (`1e400`) leaves the text a string.
        return float(plain)
    return None


_GO_NONFINITE = {**dict.fromkeys((".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"), math.inf),
                 **dict.fromkeys(("-.inf", "-.Inf", "-.INF"), -math.inf),
                 **dict.fromkeys((".nan", ".NaN", ".NAN"), math.nan)}


def _go_plain_value(text: str) -> "int | float | str":
    """yaml.v3's int / float resolution of an untagged plain scalar VALUE
    (#2415): the number it decodes, or the text itself where it decodes a
    string. Only for a text PyYAML types `!!int` / `!!float` / `!!str` —
    null, bool and timestamp are decided elsewhere (`yes` / `on` stay
    PyYAML's bools on purpose: #2164)."""
    if text in _GO_NONFINITE:
        return _GO_NONFINITE[text]
    if text[:1] and text[0] in "0123456789+-.":
        number = _go_plain_number(text)
        if number is not None:
            return number
    return text


_YAML_STR_TAG = "tag:yaml.org,2002:str"
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_YAML_INT_TAG = "tag:yaml.org,2002:int"
_YAML_FLOAT_TAG = "tag:yaml.org,2002:float"
_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_BINARY_TAG = "tag:yaml.org,2002:binary"
# yaml.v3's resolve table: the only texts an explicit `!!bool` accepts.
_GO_BOOL_TEXTS = {"true": True, "True": True, "TRUE": True,
                  "false": False, "False": False, "FALSE": False}


def _go_key_spelling(loader: Any, node: Any) -> str:
    """How pkg/config spells the key `node` (a scalar key node)."""
    text = node.value
    if node.style is not None:  # quoted / block scalar: always a string
        return text
    if node.tag == _YAML_TIMESTAMP_TAG:
        # `!!timestamp` (explicit or implicit) — yaml.v3 honours the tag.
        return _go_timestamp_key(text) or text
    implicit = loader.resolve(yaml.ScalarNode, text, (True, False))
    if node.tag != implicit:
        # An explicit tag PyYAML would not have inferred: `!!str 010`.
        return text if node.tag == _YAML_STR_TAG else _go_plain_key(text)
    return _go_plain_key(text)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _reject_surrogates(text: Any, node: Any) -> Any:
    """A `"\\udfff"` escape (a lone or paired UTF-16 surrogate) is a str
    PyYAML builds and yaml.v3 refuses ("found invalid Unicode character
    escape code") — the exporter does not load that file, and here it ended
    the hash in a UnicodeEncodeError traceback. Refused the same way, it
    takes this tool's existing "does not parse" path (#2371)."""
    if isinstance(text, str) and _SURROGATE.search(text):
        raise yaml.constructor.ConstructorError(
            None, None, "found invalid Unicode character escape code (a UTF-16 surrogate)",
            node.start_mark)
    return text


class _GoKeyLoader(StrictExporterKeyLoader):
    """The #2114 / #2123 reader (text keys, strict), with every scalar key a
    `_GoKey` carrying the exporter's spelling, and every timestamp VALUE
    decided by yaml.v3's rules instead of PyYAML's (`_construct_timestamp`)."""

    def construct_scalar(self, node):  # noqa: D102 — see _reject_surrogates
        return _reject_surrogates(super().construct_scalar(node), node)

    def compose_scalar_node(self, anchor):
        """Remember whether the scalar carried a tag of its own (#2415): the
        composed node's tag is the same for `!!str 1e3` and a plain `1e3`,
        and yaml.v3 keeps the first a string. Read by the number path only
        (`_construct_str`, `_construct_number`)."""
        tag = self.peek_event().tag
        node = super().compose_scalar_node(anchor)
        node.go_explicit_tag = tag not in (None, "!")
        return node

    def _construct_timestamp(self, node):
        """A scalar PyYAML tags `!!timestamp`. yaml.v3 decides on its own
        four layouts, which are not PyYAML's: where it reads a time, the
        value is the text pkg/config's canonical JSON writes for that
        time.Time (so `2026-12-31 23:59:59` is "2026-12-31T23:59:59Z", a
        9-digit fraction keeps all 9); where it does not (`…T10:20:30` with
        no zone, a tab or a space before the zone, `+08`, month 13 — the last
        of which PyYAML raised ValueError on and dropped the whole file), the
        value is the source text, as yaml.v3 keeps it.

        An EXPLICIT `!!timestamp` VALUE yaml.v3 cannot parse (`!!timestamp
        foo`, an empty one, a quoted or block one) is refused, taking the
        file down this tool's existing "does not parse" path, as yaml.v3
        refuses the file. Explicit is knowable only where PyYAML would not
        have inferred the tag from the plain text; `!!timestamp 2026-13-01`
        looks implicit and stays text (a known divergence).

        ⛔ A zone offset of 24h or more (`+24:00`) is rendered like any other
        time. time.Parse accepts it; only time.Time.MarshalJSON refuses it,
        and only once the value is IN a tenant's effective config — another
        tenant in the same file, a root-level sibling key, or an overridden
        default does not stop the exporter. Refusing the file here was wider
        than that (a known divergence where the value does reach it)."""
        text = self.construct_scalar(node)
        parsed = _go_parse_timestamp(text)
        if parsed is None:
            explicit = (node.style is not None or self.resolve(
                yaml.ScalarNode, text, (True, False)) != _YAML_TIMESTAMP_TAG)
            if explicit:
                raise yaml.constructor.ConstructorError(
                    None, None, f"cannot decode {text!r} as a !!timestamp", node.start_mark)
            return text
        return _go_time_json(*parsed)

    def _construct_str(self, node):
        """A scalar PyYAML reads as a string that yaml.v3 reads as a time —
        `2026-1-2`, `…T10:20:30,5Z`, two spaces before the time. Only for a
        plain scalar PyYAML would itself have tagged `!!str`: a quoted one
        stays text in both."""
        text = self.construct_scalar(node)
        if (isinstance(node, yaml.ScalarNode) and node.style is None
                and self.resolve(yaml.ScalarNode, text, (True, False)) == _YAML_STR_TAG):
            parsed = _go_parse_timestamp(text)
            if parsed:
                return _go_time_json(*parsed)
            # #2415: a number to yaml.v3 only — `1e3`, `0o17`, `+.5`, `08`;
            # `!!str 1e3` stays the text there.
            if not getattr(node, "go_explicit_tag", False):
                return _go_plain_value(text)
        return text

    def _construct_number(self, node):
        """A plain scalar PyYAML types `!!int` / `!!float` by YAML 1.1's
        rules, decided by yaml.v3's instead (#2415): `1:30` / `12:30:45`
        (sexagesimal) and `0x10000000000000000` (past uint64) stay the text,
        `1e400` too; `0b_`, which PyYAML raised ValueError on (dropping the
        whole file), is the text as well. An explicit `!!int` / `!!float`
        (`!!float 1`, `!!int 017`) keeps PyYAML's constructor."""
        if (isinstance(node, yaml.ScalarNode) and node.style is None
                and not getattr(node, "go_explicit_tag", False)):
            return _go_plain_value(self.construct_scalar(node))
        if node.tag == _YAML_INT_TAG:
            return self.construct_yaml_int(node)
        return self.construct_yaml_float(node)

    def _construct_bool(self, node):
        """#2459: an EXPLICIT `!!bool` reads only yaml.v3's six texts
        (`true` / `True` / `TRUE` and the three `false`), quoted or not;
        `!!bool yes` is "cannot decode !!str `yes` as a !!bool" there, and
        the exporter does not load the file. PyYAML's YAML 1.1 table read it
        as True. Refused the same way, it takes this tool's existing "does
        not parse" path. A PLAIN `yes` is left as it was."""
        if getattr(node, "go_explicit_tag", False):
            text = self.construct_scalar(node)
            if text not in _GO_BOOL_TEXTS:
                raise yaml.constructor.ConstructorError(
                    None, None, f"cannot decode {text!r} as a !!bool", node.start_mark)
            return _GO_BOOL_TEXTS[text]
        return self.construct_yaml_bool(node)

    def _construct_binary(self, node):
        """#2459: `!!binary` decoded as Go's base64.StdEncoding does —
        padding required, only CR / LF skipped — where PyYAML's decoder
        dropped any character outside the alphabet (`!!binary "%%%"` loaded
        as b""). yaml.v3 refuses such a value ("invalid base64 data") and
        the exporter does not load the file; refused the same way here."""
        text = self.construct_scalar(node)
        try:
            return binascii.a2b_base64(text.replace("\r", "").replace("\n", ""),
                                       strict_mode=True)
        except (binascii.Error, ValueError) as exc:
            raise yaml.constructor.ConstructorError(
                None, None, f"!!binary value contains invalid base64 data ({exc})",
                node.start_mark) from exc

    def construct_mapping(self, node, deep=False):  # noqa: D102 — see class
        mapping = super().construct_mapping(node, deep=deep)
        nodes = {kn.value: kn for kn, _ in node.value if isinstance(kn, yaml.ScalarNode)}
        for key_node in nodes.values():
            _reject_surrogates(key_node.value, key_node)
        # A `raw_text_scalars` value skipped construct_scalar (#2297): the
        # same surrogate refusal, or it would reach the hash as text.
        for key_node, value_node in node.value:
            if (isinstance(key_node, yaml.ScalarNode) and isinstance(value_node, yaml.ScalarNode)
                    and key_node.value in self.raw_text_scalars):
                _reject_surrogates(value_node.value, value_node)
        return {(_GoKey(k, _go_key_spelling(self, nodes[k])) if k in nodes else k): v
                for k, v in mapping.items()}


_GoKeyLoader.add_constructor(_YAML_TIMESTAMP_TAG, _GoKeyLoader._construct_timestamp)
_GoKeyLoader.add_constructor(_YAML_STR_TAG, _GoKeyLoader._construct_str)
_GoKeyLoader.add_constructor(_YAML_INT_TAG, _GoKeyLoader._construct_number)
_GoKeyLoader.add_constructor(_YAML_FLOAT_TAG, _GoKeyLoader._construct_number)
_GoKeyLoader.add_constructor(_YAML_BOOL_TAG, _GoKeyLoader._construct_bool)
_GoKeyLoader.add_constructor(_YAML_BINARY_TAG, _GoKeyLoader._construct_binary)


def _go_typed_key(key: Any) -> Any:
    """A key some other reader typed (not a `_GoKey`), spelled as `%v` would."""
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "<nil>"
    if isinstance(key, int):
        return str(key)
    if isinstance(key, float):
        return _go_float_v(key)
    if isinstance(key, datetime.datetime):
        offset = key.utcoffset()
        zone = "Z"
        if offset:
            minutes = int(offset.total_seconds()) // 60
            zone = f"{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
        return _go_time_string(key.year, key.month, key.day, key.hour, key.minute,
                               key.second, f"{key.microsecond:06d}", zone)
    if isinstance(key, datetime.date):
        return _go_time_string(key.year, key.month, key.day)
    return key


def _go_keys(data: Any) -> Any:
    """`data` with every mapping key in the exporter's spelling and every
    `_GoKey` (also one used as a value, e.g. an attribution's key list)
    back to a plain `str` — the one shape that leaves this tool."""
    if isinstance(data, dict):
        return {(k.go if isinstance(k, _GoKey) else _go_typed_key(k)): _go_keys(v)
                for k, v in data.items()}
    if isinstance(data, list):
        return [_go_keys(v) for v in data]
    if isinstance(data, _GoKey):
        return str.__str__(data)
    return data


def _iter_confd_yaml(entries, suffixes):
    """The entries of an ALREADY-LISTED conf.d carrying one of `suffixes`,
    CASE-INSENSITIVELY (#1588), sorted.

    ⛔ `entries` is required and comes from `_scan`'s one
    `list_config_tree` walk (#2054). The `rglob("*")` fallback that
    used to live here walked hidden files and hidden directories, which
    the exporter's `ScanDirTree` never reads — so this tool described
    tenants the exporter does not have. With the fallback gone there is
    no second way to list a conf.d in this module.

    `_scan` calls this twice (`.yaml`, then `.yml`) beside its own defaults
    pass; all three share one listing so they describe ONE tree even if
    anything changes under `conf.d` mid-run.
    """
    return sorted(p for p in entries if has_yaml_extension(p.name, suffixes))


# #2297: a `_profile:` value names a profile by its source text, as the
# exporter reads it (the #2216 precedent: `deprecate_rule._read_yaml`).
_PROFILE_AS_TEXT = ("_profile",)


class _GoKeyProfileTextLoader(_GoKeyLoader):
    """`_GoKeyLoader` with a `_profile:` VALUE read as its source text
    (#2297), for the tenant files, the root platform files (their
    `tenants:` and `profiles:` blocks, read by `_load_first_document`) and
    the `--what-if` file — the reads #2297 changed. `ExporterKeyLoader.construct_mapping` takes that value
    before any constructor runs, so neither the timestamp nor the `!!binary`
    rendering (#2371) reaches it: `_profile: 2026-12-31` names profile
    "2026-12-31", as the exporter's `ScheduledValue` keeps `value.Value`.

    A `_profile:` MAPPING is read as Go `withProfileText` reads it (#2515),
    through `ScheduledValue.UnmarshalYAML` (pkg/config/parse.go):
      - a WRITTEN `default:` key (the scheduled-value form, e.g.
        `{default: '010'}`): the default's text — as yaml.v3 decodes a scalar
        into a Go string: its source text (`010` is "010", `1.50` is "1.50",
        `2026-12-31` is "2026-12-31"), a null is "", `!!binary` is the decoded
        bytes. The mapping's other keys elect nothing.
      - any other mapping (no `default` at all) stays the generic mapping;
        it elects no profile (`_profile_name`).
    ⚠️ Not mirrored, named on stderr (`_warn_unmirrored_profiles`): no
    written `default:` but one reached through a merge key
    (`{<<: {default: x}}`) or an alias key (`{*k : x}` with `&k default`). ScheduledValue sees `<<`, takes its
    arbitrary-mapping branch and elects the yaml.v3 `Marshal` text of the
    merged mapping (`default: x\\n`) as the profile NAME — normally an
    unknown profile. Here it stays the generic mapping and elects nothing:
    the served values agree unless a profile is named by that text, but
    `_profile` and merged_hash differ from the exporter's.
    ⚠️ Not mirrored: a `default:` that is a sequence or a mapping. yaml.v3
    cannot decode it into a string, so the exporter rejects the WHOLE file
    (the `_read_profiles` precedent); here `_profile` stays that mapping and
    elects nothing."""

    raw_text_scalars = frozenset(_PROFILE_AS_TEXT)

    def compose_node(self, parent, index):
        """Remember which keys of a mapping are ALIASES (#2515 review F2).
        PyYAML hands back the anchored node itself, so `{*k : '010'}` with
        `&k default` looks like a written `default` key; yaml.v3's
        ScheduledValue compares the alias node's own Value (the alias
        name) and does not. Recorded by pair position: an alias KEY is
        composed while its mapping holds `len(parent.value)` pairs."""
        if (index is None and isinstance(parent, yaml.MappingNode)
                and self.check_event(yaml.AliasEvent)):
            positions = getattr(parent, "go_alias_key_positions", None)
            if positions is None:
                positions = parent.go_alias_key_positions = set()
            positions.add(len(parent.value))
        return super().compose_node(parent, index)

    def flatten_mapping(self, node):
        """Remember the keys a mapping WRITES before `<<` is merged into it
        (#2515): `ScheduledValue` tests the written keys for `default`, and
        PyYAML's flattening rewrites `node.value` in place. Recorded once, so
        a node an alias reaches again keeps its written keys."""
        if not hasattr(node, "go_written_keys"):
            node.go_written_keys = _written_keys(node)
        super().flatten_mapping(node)

    def construct_mapping(self, node, deep=False):  # noqa: D102 — see class
        mapping = super().construct_mapping(node, deep=deep)
        for key_node, value_node in node.value:
            if (isinstance(key_node, yaml.ScalarNode) and isinstance(value_node, yaml.MappingNode)
                    and key_node.value in self.raw_text_scalars):
                text = self._scheduled_value_text(value_node)
                if text is not None:
                    mapping[key_node.value] = text  # the dict keeps its `_GoKey`
                elif ("default" not in value_node.go_written_keys
                      and any(isinstance(k, yaml.ScalarNode) and k.value == "default"
                              for k, _ in value_node.value)):
                    # Merge-key-only `default` (flattened above): not mirrored
                    # — kept generic, tagged for `_warn_unmirrored_profiles`.
                    # Built deep here: the lazily filled dict `super()` handed
                    # out may still be empty.
                    mapping[key_node.value] = _UnmirroredMergeProfile(
                        self.construct_mapping(value_node, deep=True))
        return mapping

    def _scheduled_value_text(self, node) -> "str | None":
        """`ScheduledValue.Default` of a mapping `node`, as `withProfileText`
        takes it; None where it leaves the generic value (see the class)."""
        written = getattr(node, "go_written_keys", None)
        if written is None:
            written = _written_keys(node)
        self.flatten_mapping(node)
        if "default" in written:
            default = None
            for key_node, value_node in node.value:  # merged first, so the written one wins
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == "default":
                    default = value_node
            if not isinstance(default, yaml.ScalarNode):
                return None  # yaml.v3 rejects the file; not mirrored (see the class)
            if default.tag == _YAML_NULL_TAG and default.style is None:
                return ""
            if default.tag == _YAML_BINARY_TAG:
                return self._construct_binary(default).decode(
                    "utf-8", "describe_tenant.go_invalid_utf8")
            return _reject_surrogates(default.value, default)
        return None


_YAML_NULL_TAG = "tag:yaml.org,2002:null"


def _written_keys(node: Any) -> frozenset:
    """The scalar keys `node` (a mapping, not yet flattened) WRITES: neither
    merged in by `<<` nor an alias (`compose_node`'s positions) — the keys
    yaml.v3's ScheduledValue sees by their Value."""
    aliases = getattr(node, "go_alias_key_positions", ())
    return frozenset(k.value for i, (k, _) in enumerate(node.value)
                     if isinstance(k, yaml.ScalarNode) and i not in aliases)


class _UnmirroredMergeProfile(dict):
    """A `_profile:` mapping whose `default` comes only through a merge key
    or an alias key (#2515): kept as the generic mapping, which the exporter does not do
    (see `_GoKeyProfileTextLoader`). A plain dict otherwise."""


# (resolved file, tenant id) already named — one line per file read, not per
# construct (the platform files and the --what-if file are read again).
_UNMIRRORED_PROFILE_WARNED: set = set()


def _warn_unmirrored_profiles(path: Path, doc: Any) -> None:
    """Name each tenant in `doc`'s `tenants:` whose `_profile` is an
    `_UnmirroredMergeProfile`, once per file and tenant."""
    block = doc.get("tenants") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return
    for tid, body in block.items():
        if not (isinstance(body, dict) and isinstance(body.get("_profile"), _UnmirroredMergeProfile)):
            continue
        key = (str(Path(path).resolve()), str(tid))
        if key in _UNMIRRORED_PROFILE_WARNED:
            continue
        _UNMIRRORED_PROFILE_WARNED.add(key)
        print(f"WARNING: {path}: tenant '{tid}': `_profile` takes `default` only through a "
              f"merge key (`<<`) or an alias key — not mirrored here. The exporter elects the YAML text of "
              f"the merged mapping as the profile name; this tool keeps the mapping and "
              f"elects no profile, so `_profile` and merged_hash differ from the "
              f"exporter's (#2515).", file=sys.stderr)


def _load_first_document(path: Path) -> Any:
    """The FIRST YAML document of `path`, as the exporter's walker reads a
    config file (yaml.v3 `Unmarshal` decodes one document).

    Raises on a file whose first document does not parse — the caller
    decides what that costs. A failure in a LATER document does not matter:
    the generator is never advanced past the first one. Before #2019 a
    multi-document tenant file (`tenants: …` then `---`) killed the whole
    run with ComposerError while the exporter served its tenants.

    Mapping keys are the scalar's source TEXT (#2114), as yaml.v3 decodes
    them into the exporter's `map[string]…`: `010:` is tenant "010" and
    `yes:` is "yes", not PyYAML's 8 / True. Values keep PyYAML's types.
    Strict (#2123): a key written twice in one mapping raises — by the
    exporter's identity, so `123:` and `"123":` are that duplicate.
    Same pure-Python parser as before.

    A `_profile:` VALUE is its source text too (#2297): the exporter binds
    `_profile: 010` to profile `010`, where PyYAML's 8 bound none.
    """
    if not yaml:
        raise RuntimeError("PyYAML is required for describe-tenant. Install: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        # #2123 strict + #2114 exporter keys, composed in `_lib_io`; each key
        # also carries the exporter's spelling (#2371), and `_profile:` is
        # source text (#2297).
        doc = next(strict_safe_load_all(f, loader=_GoKeyProfileTextLoader), None)
    _warn_unmirrored_profiles(path, doc)
    return doc


def _load_platform_doc(path: Path) -> Any:
    """`path`'s first document with source-text keys (#2114) — the read
    `_load_first_document` gives the root platform files it may stand in
    for (first document, strict, pure parser; #2459 — it used to refuse a
    multi-document file), for the `--what-if` file's `tenants:` block, so
    its ids match the tenant files' — and its `_profile:` values (#2297)."""
    return _load_first_document(path) or {}


def _overlay_tenant(tenant_raw: Any, blocks: "list[tuple[str, dict]]",
                    chain: "dict | None" = None) -> "tuple[Any, list[dict]]":
    """The override merged over the defaults chain, and its attribution.

    `blocks` is the tenant's entries in the ROOT platform files' `tenants:`
    blocks, in merge order (#2019). Every top-level key the tenant file does
    not write is taken from them — a later file over an earlier one — so the
    tenant file wins KEY BY KEY, replacing a platform value wholesale (its
    `_routing` map, its scheduled value) exactly as the exporter's /metrics
    plane does. The attribution lists, per file, the keys it supplied: never
    `_metadata` (not inherited) and never a null on a threshold key
    (deep_merge ignores it); a null on a RESERVED (`_`-prefixed) key deletes
    the inherited value, so it is listed exactly when `chain` (the merged
    defaults chain) has that key. MUST stay in lockstep with pkg/config/platform_overlay.go
    `overlayTenant` — tests/shared/platform_tenant_overlay_matrix.json's
    `walker` column pins both.
    """
    if not blocks or not isinstance(tenant_raw, dict):
        return tenant_raw, []
    combined: dict = {}
    owner: dict = {}
    # Per THRESHOLD, not per spelling (#2368): a later layer writing either
    # #1231 spelling drops the other spelling an earlier layer wrote.
    for i, (_fname, block) in enumerate(blocks):
        _overlay_across_spellings(combined, block)
        for k in block:
            owner[k] = i
    _overlay_across_spellings(combined, tenant_raw)
    owner = {k: i for k, i in owner.items() if k not in tenant_raw and k in combined}
    by_file: dict[int, set[str]] = {}
    for k, i in owner.items():
        if k == "_metadata":
            continue
        if combined[k] is None and not (k.startswith("_") and k in (chain or {})):
            continue
        # Attribution names the CANONICAL spelling (#2368), once.
        by_file.setdefault(i, set()).add(_canonical_tenant_key(k)[0] if isinstance(k, str) else k)
    sources = [{"file": blocks[i][0], "keys": sorted(by_file[i])}
               for i in range(len(blocks)) if i in by_file]
    return combined, sources


def _without_null_thresholds(body: Any) -> Any:
    """Go `withoutNullThresholds` (#2518): a threshold key (no `_` prefix)
    written as null — or as `{default: null}` with no window (#2708,
    `writes_nothing`) — is no write, so a layer carrying one does not own
    the key and the layer below shows through. A reserved key's null stays
    (it deletes the inherited value, ADR-017). Non-dict bodies pass
    through."""
    if not isinstance(body, dict):
        return body
    if not any(writes_nothing(k, v) for k, v in body.items()):
        return body
    return {k: v for k, v in body.items() if not writes_nothing(k, v)}


def _platform_tenant_blocks(doc: Any) -> dict:
    """`{tenant: body}` of a root platform file's `tenants:` block, keeping
    only bodies that are non-empty mappings (anything else supplies no key),
    each without its null thresholds (#2518). Ids go through `_tenant_id`
    like the tenant files' do."""
    tenants = doc.get("tenants") if isinstance(doc, dict) else None
    if not isinstance(tenants, dict):
        return {}
    out = {}
    for tid, body in tenants.items():
        body = _without_null_thresholds(body)
        if isinstance(body, dict) and body:
            out[_tenant_id(tid)] = body
    return out


def _tenant_id(tid: Any) -> str:
    """A tenant id as the exporter reads it: the key's source text.

    Every document this module takes tenant ids from is read through
    `_load_first_document` / `_load_platform_doc`, whose keys already ARE
    the source text (#2114) — `0123:`, `1.50:`, `true:` come out verbatim,
    as yaml.v3 keys them. The `str()` fallback is only for a document a
    caller built itself; it is lossy (`str()` of PyYAML's 83 is not "0123").
    """
    return tid if isinstance(tid, str) else str(tid)


def _tenant_body(tconfig: Any) -> Any:
    """Normalise one tenant's body at ingest.

    `tenants:\\n  t1:\\n` (a tenant declared with an empty / null body) parses
    to None. The exporter serves such a tenant with its inherited defaults,
    and pkg/config extractTenantRaw maps the null body to an empty override
    (#1677 F2) — so this oracle does too. Before, deep_merge(merged, None)
    crashed the whole run with AttributeError. Any other non-dict body is
    passed through unchanged (the Go side rejects it; out of scope here).
    A threshold written as null is dropped (#2518, Go TenantDoc.tenantRaw).
    """
    return {} if tconfig is None else _without_null_thresholds(tconfig)


def _undecodable_tenant_body(doc: Any) -> "str | None":
    """Why the exporter cannot decode `doc` for its `tenants:` block, or None.

    A tenant body that is neither a mapping nor null (`tenants: {tx: [a]}`,
    `{tx: 5}`) fails the exporter's typed decode of the WHOLE file (#2115,
    measured: served-values lists the file in parse_failed and serves none
    of its tenants, `ty: {}` beside it included; effective exits 3). Named
    by the first such tenant, in the file's order."""
    block = doc.get("tenants") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return None
    for tid, body in block.items():
        if body is not None and not isinstance(body, dict):
            return f"tenant '{_tenant_id(tid)}' is a {type(body).__name__}, not a mapping"
    return None


# ---------------------------------------------------------------------------
# Profile expansion (#2117) — MUST stay in lockstep with
# pkg/config/profile_overlay.go; tests/shared/platform_tenant_overlay_matrix.json
# `walker` column pins both against /metrics.
# ---------------------------------------------------------------------------

# Go `legacySpellingFor` / `overlayAcrossSpellings` (#2368): one Python copy,
# in `_grar_validate` beside `_canonical_tenant_key`, shared with diagnose.
_legacy_spelling = _legacy_tenant_key
_overlay_across_spellings = overlay_across_spellings


def _has_alias_equivalent(own: dict, key: str) -> bool:
    """Go `hasAliasEquivalent`: `own` sets `key` under ANY spelling."""
    if key in own:
        return True
    canon, _ = _canonical_tenant_key(key)
    if canon != key and canon in own:
        return True
    legacy = _legacy_spelling(canon)
    return legacy is not None and legacy in own


def _canonical_view(m: dict) -> dict:
    """Go `canonicalView`: deprecated spellings moved onto the canonical key,
    the canonical spelling winning when both are present."""
    out: dict = {}
    for k, v in m.items():
        canon, is_alias = _canonical_tenant_key(k)
        if is_alias and canon in m:
            continue  # canonical wins; deprecated duplicate ignored
        out[canon] = v
    return out


def _profile_fill(canon_profile: dict, own: dict, declared: set) -> dict:
    """Go `profileFill`: the entries of `canon_profile` that fill in for a
    tenant whose own layer is `own` — not a key `own` sets under any
    spelling, not a key declared in `optional_overrides` (by its base before
    `{`), except the `_critical` shape."""
    fill: dict = {}
    for key, value in canon_profile.items():
        brace = key.find("{")
        declared_key = key[:brace] if brace > 0 else key
        is_declared = declared_key in declared and not key.endswith("_critical")
        if _has_alias_equivalent(own, key) or is_declared:
            continue
        fill[key] = value
    return fill


def _read_profiles(files: "list[tuple[str, Any]]") -> dict:
    """Go `newPlatformProfiles`: `files` is `(name, first document)` of the
    root platform files in merge order. Returns `{"by_name": {profile:
    {key: (file, value)}}, "files": [...], "declared": set}` — a profile
    merged per name, per key, a later file over an earlier one; `declared`
    the canonical `optional_overrides` of a defaults carrier.

    ⚠️ Not mirrored (as `_read_platform_files`): Go also drops a file its
    typed decode rejects — WHOLE, every profile in it. Concretely,
    `profiles: {std: 5, alt: {mysql_connections: 1}}` with a tenant on
    `_profile: alt`: the exporter (/metrics, /effective) rejects the file
    (`std` is not a mapping), serves the chain's value and emits no
    `profile_overlay`; this reader skips only `std` and expands `alt` (1)."""
    by_name: dict = {}
    order: list = []
    declared: set = set()
    for fname, doc in files:
        if not isinstance(doc, dict):
            continue
        oo = doc.get("optional_overrides")
        if is_defaults_name(fname) and isinstance(oo, list):
            declared.update(_canonical_tenant_key(str(k))[0] for k in oo)
        profiles = doc.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            continue
        order.append(fname)
        for pname, body in profiles.items():
            entries = by_name.setdefault(_tenant_id(pname), {})
            for k, v in (body if isinstance(body, dict) else {}).items():
                if writes_nothing(k, v):
                    continue  # #2518 / #2708: a null threshold is no write
                entries[k] = (fname, v)
    return {"by_name": by_name, "files": order, "declared": declared}


def _profile_name(value: Any) -> str:
    """Go `profileNameOf`: a string `_profile`, stripped; "" otherwise
    (the schema types it as a string)."""
    return value.strip() if isinstance(value, str) else ""


def _expand_profile(own: Any, profiles: dict,
                    chain: "dict | None" = None) -> "tuple[Any, list[dict]]":
    """Go `PlatformProfiles.expand`: `own` (the tenant block with the
    platform overlay applied) with the elected profile's keys filled in,
    and the attribution `[{profile, file, keys}]` in merge order —
    `_overlay_tenant`'s rules (never `_metadata`, never a null on a
    threshold key, a null on a reserved key only when `chain` has it)."""
    if not isinstance(own, dict):
        return own, []
    name = _profile_name(own.get("_profile"))
    profile = profiles["by_name"].get(name) if name else None
    if not profile:
        return own, []
    fill = _profile_fill(_canonical_view(profile), own, profiles["declared"])
    if not fill:
        return own, []
    out = dict(own)
    by_file: dict[str, list[str]] = {}
    for k, (fname, v) in fill.items():
        out[k] = v
        if k == "_metadata":
            continue
        if v is None and not (k.startswith("_") and k in (chain or {})):
            continue
        by_file.setdefault(fname, []).append(k)
    sources = [{"profile": name, "file": f, "keys": sorted(by_file[f])}
               for f in profiles["files"] if f in by_file]
    return out, sources


class ConfDScanner:
    """Scan a conf.d/ directory and build the inheritance graph."""

    def __init__(self, conf_d: Path):
        self.conf_d = conf_d.resolve()
        self.tenants: dict[str, dict] = {}           # tenant_id → raw config
        self.tenant_files: dict[str, Path] = {}       # tenant_id → file path
        self.defaults_chain: dict[str, list[Path]] = {}  # tenant_id → [L0, L1, ...] defaults paths
        # #1967: the conf.d ENTRIES (as listed, not resolved) behind
        # `tenant_files` / `defaults_chain`, index-aligned with the latter.
        # Reporting falls back to them when a link points outside conf.d
        # (`_report_path`), and --what-if takes a level's depth from them.
        self._tenant_entries: dict[str, Path] = {}
        self._defaults_chain_entries: dict[str, list[Path]] = {}
        self.defaults_data: dict[str, dict] = {}      # defaults path str → parsed defaults dict
        # #2049: tenant_id → sorted conf.d-relative entries, for every tenant
        # MORE THAN ONE carrier declares. `tenants` / `tenant_files` still
        # hold one of those declarations; the CLI must not describe it.
        self.duplicates: dict[Any, list[str]] = {}
        # #772: tenant_id → [(recipe, origin, is_own), ...] — the ADR-024 UNION
        # resolution of `_custom_alerts` from the compiler's own walker. Computed
        # ONCE (collect_instances scans the whole tree) so effective_config() is a
        # cheap dict lookup, not an O(N²) re-walk under --all.
        self._custom_alerts_resolved: dict[str, list] = {}
        # Set when the compiler resolver raised — output is degraded to the
        # deep_merge (REPLACE) fallback for `_custom_alerts`; callers can detect it.
        self.custom_alerts_resolution_error: str | None = None
        self._profiles: dict | None = None  # #2117: `profiles()` cache
        self._scan()
        self._resolve_custom_alerts()

    def _resolve_custom_alerts(self) -> None:
        """Build the per-tenant `_custom_alerts` UNION resolution via the compiler's
        SSOT walker (#772). No-op if the compiler package is unavailable — then
        effective_config() falls back to the (REPLACE) deep_merge result."""
        if _ca_loader is None:
            return
        try:
            # collect_instances returns (triples, file_errors) (#1008 Part B fail-soft);
            # describe_tenant only needs the triples — unloadable files surface in the
            # compiler's own skip report, not here.
            triples, _file_errors = _ca_loader.collect_instances(self.conf_d)
            for tenant, recipe, origin, is_own in triples:
                self._custom_alerts_resolved.setdefault(tenant, []).append(
                    (recipe, origin, is_own))
        except Exception as e:  # pragma: no cover
            # Do NOT silently swallow: the `_custom_alerts` view degrades to the
            # deep_merge REPLACE fallback (own-only for override tenants), which is
            # the exact #772 blind spot — so warn loudly + record a marker.
            self._custom_alerts_resolved = {}
            self.custom_alerts_resolution_error = str(e)
            print(f"WARN: _custom_alerts inheritance resolution failed "
                  f"({_ca_loader.__name__}.collect_instances: {e}); "
                  f"falling back to deep_merge REPLACE — override tenants may show "
                  f"only own recipes (#772).", file=sys.stderr)

    def _scan(self) -> None:
        """Recursively scan conf.d/ and build tenant + defaults maps."""
        # #2054: the listing is `list_config_tree` — the shared mirror of
        # the exporter's walker (`pkg/config.ScanDirTree`): recursive, prunes
        # `.`-prefixed directories, drops `.`-prefixed files, never descends
        # a directory symlink, lists regular files only. The `rglob("*")`
        # this replaced read `.hidden.yaml`, `.snap/…` and a ConfigMap
        # mount's `..2026_…/` payload, so it described tenants the exporter
        # does not have (and read a ConfigMap tenant twice, via the root
        # link and via the payload).
        #
        # ⛔ ONE walk for everything below — the warnings and all three
        # passes (defaults, `.yaml`, `.yml`): separate walks could describe
        # DIFFERENT trees if anything changes under `conf.d` mid-run, the
        # exact reason `collect_instances` lists once and says so
        # (`test_reader_walks_the_tree_once`).
        listing = list_config_tree(self.conf_d)
        entries = listing.files
        # #1607: what the listing DROPS must not be silent — this tool's job
        # is "what config does the exporter see", so an entry skipped
        # without a word is a wrong answer. `listing.unusable` is the
        # disjoint complement of `listing.files` (same hidden pruning).
        # ⛔ `_`-prefixed entries other than the defaults carriers are NOT
        # read by this scanner at all, so naming an unusable one would
        # report a loss that did not happen. Defaults carriers stay in: this
        # scanner really does read them.
        # ⛔ That name filter is for ENTRIES, never for a directory the walk
        # could not read: the exporter descends `_`-prefixed directories
        # (`_arch/arch.yaml` is a tenant to it), so a chmod-000 `_locked/`
        # hides real tenants and must be named like `locked/` is (#2054).
        # A directory SYMLINK likewise (#1972): `_shared -> .payload/_shared`
        # hides a subtree the exporter would otherwise have descended.
        # Printed through the once-per-process helper: the custom-alerts
        # loader this tool also runs names the same link.
        for bad in listing.unusable:
            if is_dir_symlink(bad):
                warn_dir_symlink_once(bad)
            elif (bad in listing.unscannable or is_defaults_name(bad.name)
                    or not is_reserved_name(bad.name)):
                print(f"WARNING: skipped {bad} — {unusable_reason(bad)}",
                      file=sys.stderr)
        # Collect all _defaults.yaml files
        defaults_files: dict[str, dict] = {}
        # #1588: matched by the shared predicate, not by two literal names.
        # `_DEFAULTS.YAML` measured as invisible here while the exporter
        # merged it into every downstream tenant.
        # (entry as listed, its resolved path), grouped by the directory that
        # HOLDS THE ENTRY. ⛔ Both halves are what Go's walker does (WalkDir
        # never follows a link): the carrier is selected by the entry's name
        # and belongs to the entry's directory, while the chain and the JSON
        # keep reporting the resolved path as they always did.
        #   - selecting on the resolved name made
        #     `_defaults.yaml -> sub/platform-base.yaml` select nothing and
        #     crash the run;
        #   - grouping by the RESOLVED parent moved that link's level into
        #     `sub/`, so a root tenant lost it and a `sub/` tenant saw only one
        #     of its two levels — a different merged_hash from the exporter
        #     (both from blind review of #1674; the root-tenant half predates
        #     it). Pinned by tests/shared/defaults_symlink_parity_matrix.json.
        listed: dict[Path, list[tuple[Path, Path]]] = {}
        for dp in entries:
            if is_defaults_name(dp.name):
                listed.setdefault(dp.parent.resolve(), []).append((dp, dp.resolve()))
        # #1674 (B8): ONE carrier per directory, chosen by the rule every
        # plane shares (`select_defaults_carrier`). Before, a directory with
        # `_defaults.yaml` + `_defaults.yml` put BOTH into the chain and
        # merged them, while the exporter's chain read one and its flat root
        # `Defaults` let the `.yml` overwrite — three answers for one tree.
        # A second spelling is a misconfiguration, so it is named, not merged.
        #
        # Candidates are the carriers the exporter's walker KEEPS: an entry
        # it cannot read is logged and dropped there, so it is dropped here
        # before selecting (`readable_carriers`), and named on stderr.
        by_dir: dict[Path, list[tuple[Path, Path]]] = {}
        for d in sorted(listed):
            readable, unreadable = readable_carriers(dp for dp, _ in listed[d])
            for bad, exc in unreadable:
                print(f"WARNING: skipped {bad} — cannot read: {exc}", file=sys.stderr)
            chosen = select_defaults_carrier(readable)
            if chosen is None:
                continue
            warn_multi_carrier(d, readable)
            resolved = dict(listed[d])[chosen]
            try:
                defaults_files[str(resolved)] = _load_yaml(chosen)
            except _UnsupportedShape as exc:  # #2459: parses, refused
                raise DefaultsShapeError(chosen, exc) from exc
            except Exception as exc:  # noqa: BLE001 — named, then refused
                # As broad as `_load_tenant_file`: PyYAML's constructors
                # raise more than YAMLError for an explicit tag whose text
                # does not fit it — ValueError (`!!int foo`), KeyError
                # (`!!bool foo`) — and the exporter rejects those files too.
                raise DefaultsParseError(chosen, exc) from exc
            by_dir[d] = [(chosen, resolved)]
        self._defaults_by_dir = by_dir
        self.defaults_data = defaults_files
        # #2097: the resolved files of this listing — the files `--replaces`
        # may name, and the ones `--what-if` alone refuses (F1).
        self.listed_files = frozenset(p.resolve() for p in entries)
        self._platform_files = self._read_platform_files(entries)

        # Collect all tenant files.
        # #2049: every carrier that declares a tenant is recorded in
        # `declared`, keyed by the conf.d ENTRY as listed (never resolved),
        # in BOTH passes — `acme.yaml` + `acme.yml` is two carriers to the
        # exporter as much as `a.yaml` + `b.yaml` is. `self.tenants` /
        # `tenant_files` still pick one declaration (last `.yaml` wins, `.yaml` over `.yml`) so
        # existing library callers keep their shape; the CLI refuses to
        # describe a tenant listed in `self.duplicates` (see
        # `duplicate_error`), which is what the exporter does with it.
        declared: dict[Any, list[str]] = {}
        for fp in _iter_confd_yaml(entries, (".yaml",)):
            if is_reserved_name(fp.name):
                continue
            data = self._load_tenant_file(fp)
            if not isinstance(data, dict):
                continue
            tenants_block = data.get("tenants", {})
            if not isinstance(tenants_block, dict):
                continue
            for tid, tconfig in tenants_block.items():
                tid = _tenant_id(tid)
                declared.setdefault(tid, []).append(self._entry_label(fp))
                self.tenants[tid] = _tenant_body(tconfig)
                self._record_tenant(tid, fp)

        for fp in _iter_confd_yaml(entries, (".yml",)):
            if is_reserved_name(fp.name):
                continue
            data = self._load_tenant_file(fp)
            if not isinstance(data, dict):
                continue
            tenants_block = data.get("tenants", {})
            if not isinstance(tenants_block, dict):
                continue
            for tid, tconfig in tenants_block.items():
                tid = _tenant_id(tid)
                declared.setdefault(tid, []).append(self._entry_label(fp))
                if tid not in self.tenants:
                    self.tenants[tid] = _tenant_body(tconfig)
                    self._record_tenant(tid, fp)

        # ⛔ The shared predicate (`_lib_confd.duplicate_declarations`, also
        # behind validate_config's `tenant_uniqueness`), not a local one.
        self.duplicates = duplicate_declarations(declared)

    def _entry_label(self, entry: Path) -> str:
        """How a duplicate report names carrier `entry`: its conf.d-relative
        path AS LISTED (#2049).

        ⛔ Not `_report_path`, which reports a link by its target: for
        `acme.yaml -> real.yaml` beside `real.yaml` that names `real.yaml`
        twice — one carrier to this reader, two to the exporter (its walker
        counts directory entries and raises DuplicateTenantError on them).
        """
        return entry.relative_to(self.conf_d).as_posix()

    def duplicate_error(self, tenant_id: Any) -> str | None:
        """Why `tenant_id` cannot be described when more than one carrier
        declares it; None when it is declared at most once (#2049).

        When every carrier parses, the exporter's walker raises
        DuplicateTenantError for such a tenant (and a full load rejects the
        whole conf.d), so it serves NO effective config for it. Describing
        one of the declarations — whichever the scan kept, as this tool did
        before — answered a question the exporter refuses, with rc 0 and no
        warning. Every carrier is named, sorted, so the operator can decide
        which one owns the tenant.

        ⚠️ The verdict is CONDITIONAL on purpose. A carrier here is a file
        whose `tenants:` key names the tenant (the #1942 rule, see
        `_lib_confd.declared_tenant_ids`); a file the exporter's full decode
        rejects declares nothing there, so Go can resolve this tenant from
        the other file. Refusing is still right — it matches validate_config
        and beats picking a value — but claiming the exporter rejects it
        would be false for that tree. One such shape never gets here: a file
        with a non-mapping tenant body is skipped by the scan
        (`_load_tenant_file`, #2115), so it declares nothing here either.
        """
        files = self.duplicates.get(tenant_id)
        if not files:
            return None
        return (f"duplicate tenant ID '{tenant_id}': declared in {len(files)} "
                f"files under {self.conf_d}: {', '.join(files)}. If every one "
                f"of these files parses, the exporter rejects this tenant (a "
                f"full load rejects the whole conf.d); a file its full decode "
                f"rejects declares nothing there. Not describing any of "
                f"them — run validate-config, and keep the tenant in "
                f"exactly one file.")

    @staticmethod
    def _load_tenant_file(fp: Path) -> Any:
        """A tenant file's first document, or None when it does not parse.

        The exporter's walker skips such a file (it declares no tenant, and
        the exporter logs it) rather than refusing the whole tree, so this
        tool names it on stderr and describes the rest — before #2019 one
        broken tenant file ended the run with a traceback.

        A file that parses but holds a tenant body the exporter cannot
        decode (`_undecodable_tenant_body`) is skipped the same way (#2115):
        the exporter serves none of its tenants. Before, describing one of
        them ended in an AttributeError traceback (rc 1), and describing
        another tenant of the same file answered rc 0.
        """
        try:
            doc = _load_first_document(fp)
        except Exception as exc:  # noqa: BLE001 — named, then skipped
            print(f"WARNING: skipped {fp} — does not parse: {exc}", file=sys.stderr)
            return None
        why = _undecodable_tenant_body(doc)
        if why is not None:
            print(f"WARNING: skipped {fp} — the exporter cannot decode it: {why}; "
                  f"none of its tenants is served", file=sys.stderr)
            return None
        return doc

    def _read_platform_files(self, entries) -> "list[tuple[str, Path, dict]]":
        """Every ROOT platform file's `tenants:` block, in merge order (#2019).

        `(name, resolved path, {tenant: body})` for each `_`-prefixed config
        file directly in conf.d, sorted by name — the files the exporter's
        flat merge reads per-tenant platform values from
        (`rootPlatformKeys`): a NESTED platform file's block is read by no
        plane (#1576), and a root defaults carrier the chain did NOT select
        contributes nothing (#1674). A tenant body that is not a non-empty
        mapping supplies nothing.

        ⚠️ Not mirrored: the exporter also drops a platform file its typed
        decode rejects (a `defaults:` value of the wrong type, say). This
        reader keeps its syntactically valid `tenants:` block — the same
        stated difference as `_lib_confd.declared_tenant_ids`.
        """
        chosen_root = {entry for entry, _resolved in self._defaults_by_dir.get(self.conf_d, [])}
        out: list[tuple[str, Path, dict]] = []
        # #2117: the same files' first documents, for their `profiles:`
        # (and the carrier's `optional_overrides:`) — `_read_profiles`.
        self._platform_docs: list[tuple[str, Path, Any]] = []
        for fp in sorted((p for p in entries if p.parent == self.conf_d
                          and is_reserved_name(p.name)), key=lambda p: p.name):
            if is_defaults_name(fp.name) and fp not in chosen_root:
                continue
            try:
                doc = _load_first_document(fp)
            except Exception:  # noqa: BLE001 — contributes nothing, like Go
                doc = None
            # Kept even when it supplies nothing: `--what-if` may stand a
            # new version of it in (platform_blocks' `replace`).
            out.append((fp.name, fp.resolve(), _platform_tenant_blocks(doc)))
            self._platform_docs.append((fp.name, fp.resolve(), doc))
        return out

    def profiles(self, replace: "dict[str, Any] | None" = None) -> dict:
        """The root platform files' merged `profiles:` set (#2117,
        `_read_profiles`). `replace` as for `platform_blocks`: a resolved
        path → a document standing in for that file (`--what-if`)."""
        if not replace and self._profiles is not None:
            return self._profiles
        docs = [(name, replace[str(resolved)] if replace and str(resolved) in replace else doc)
                for name, resolved, doc in self._platform_docs]
        result = _read_profiles(docs)
        if not replace:
            self._profiles = result
        return result

    def platform_blocks(self, tenant_id: str,
                        replace: "dict[str, dict] | None" = None
                        ) -> "list[tuple[str, dict]]":
        """`tenant_id`'s entries in the root platform files, merge order.

        `replace` maps a resolved path to a document that stands in for that
        file's `tenants:` block (`--what-if` on a root platform file — the
        defaults carrier, or one off the chain such as `_profiles.yaml`,
        which `what_if_result` substitutes for `--replaces` since #2097).
        """
        out: list[tuple[str, dict]] = []
        for name, resolved, blocks in self._platform_files:
            if replace and str(resolved) in replace:
                blocks = _platform_tenant_blocks(replace[str(resolved)])
            block = blocks.get(tenant_id)
            if block is not None:
                out.append((name, block))
        return out

    def _record_tenant(self, tid: str, fp: Path) -> None:
        """Record where tenant `tid` came from: conf.d entry `fp`."""
        self.tenant_files[tid] = fp.resolve()
        self._tenant_entries[tid] = fp
        chain = self._resolve_defaults_chain(fp)
        self.defaults_chain[tid] = [resolved for _entry, resolved in chain]
        self._defaults_chain_entries[tid] = [entry for entry, _resolved in chain]

    def _report_path(self, entry: Path, resolved: Path) -> str:
        """The conf.d-relative path this tool REPORTS for one entry (#1967).

        ⛔ The ONE rule every reported path goes through. A target inside
        conf.d is reported by its resolved path — the long-standing design
        (see tests/shared/defaults_symlink_parity_matrix.json `_comment`).
        A target OUTSIDE conf.d has no conf.d-relative resolved path, so
        `resolved.relative_to(conf_d)` raised ValueError and the whole run
        died with a traceback; such an entry is reported by the entry (the
        link) itself, which always lives under conf.d because `_scan` lists
        entries from `self.conf_d`.

        ⛔ Always `/`-joined (#1550): the Go side and golden.json both use `/`,
        so a host-separator path made the same tree report differently on
        Windows. Same rule as `_entry_label`.
        """
        try:
            return resolved.relative_to(self.conf_d).as_posix()
        except ValueError:
            return entry.relative_to(self.conf_d).as_posix()

    def entry_level(self, entry: Path) -> int | None:
        """The chain level of conf.d entry `entry`, or None outside conf.d.

        ⛔ #1967: ONE rule for the levels this tool computes — the chain's
        carriers, and the `--what-if` file WHEN IT IS NOT a file of the
        scanned tree (#2097: such a file is refused, or names a `--replaces`
        target). The level is that of the directory HOLDING the entry,
        resolved (the same key `_scan` groups carriers by,
        `dp.parent.resolve()`), so a linked conf.d or a `..` in the spelling
        lands on the real directory; the entry's own name is never followed.
        Resolving the whole path instead put a link's level at its target's
        directory: `_whatif.yaml -> ../o/w.yaml` came out as append-external
        while an identical regular file was inserted, and
        `_x.yaml -> sub/deep/y.yaml` counted as level 2 instead of 0.

        ⚠️ Not covered by this rule: whether the `--what-if` file IS a file
        of the tree is decided on the RESOLVED path against the scanned
        files (`listed_files`, resolved), so a what-if link pointing at a
        file of the tree is refused (#2097 F1: it would compare that file
        with itself) rather than being placed at its own level.
        """
        holder = Path(os.path.realpath(entry.absolute().parent))
        try:
            return len(holder.relative_to(self.conf_d).parts)
        except ValueError:
            return None

    def _resolve_defaults_chain(self, tenant_file: Path) -> list[tuple[Path, Path]]:
        """Walk from tenant file up to conf.d/ root, collecting _defaults.yaml at each level.

        Returns (entry, resolved) per level, L0 first.

        ⛔ #1967: the walk starts at the directory that HOLDS THE ENTRY
        `tenant_file`, not at its resolved parent — the same rule `_scan`
        applies to defaults carriers (#1674), and what the exporter's walker
        does (WalkDir never follows a link). `tg.yaml -> sub/_real.txt` is a
        root tenant: starting from `sub/` gave it `sub/_defaults.yaml` too, a
        different merged_hash from the exporter; a target outside conf.d
        never reached `root` at all. `tenant_file` is an entry listed from
        `self.conf_d` (already resolved), so its unresolved parents reach
        `root` by `==`.
        """
        chain: list[tuple[Path, Path]] = []
        current = tenant_file.parent
        root = self.conf_d

        while True:
            # #1588: looked up in the map `_scan` already built by
            # walking the tree, rather than re-listing this directory.
            # ⛔ Re-listing would have made this function a FLAT read of a
            # config dir — which `test_confd_enumeration_contract.py`
            # correctly rejects — even though the chain walk around it is
            # what makes the reader hierarchical. Reusing the recursive
            # scan's own result is both cheaper and honest.
            chain.extend(self._defaults_by_dir.get(current.resolve(), []))
            if current == root or current == current.parent:
                break
            current = current.parent

        chain.reverse()  # L0 (root) first, L3 (nearest) last
        return chain

    def effective_config(self, tenant_id: str, resolve_custom_alerts: bool = True) -> dict:
        """Compute effective config by merging defaults chain + tenant config.

        `resolve_custom_alerts` (#772): when True (default, the normal/blast_radius
        view) `_custom_alerts` is the ADR-024 compiler UNION. Set False for the
        `--what-if` simulation, whose simulated side is built from an in-memory
        modified chain that the disk-based compiler walker cannot resolve — both
        sides then use the same raw deep_merge so the diff is apples-to-apples
        (the union view stays the job of normal mode / blast_radius)."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found in {self.conf_d}")

        merged = self._chain_merged(tenant_id)

        # Apply tenant config (highest priority), with every key it does not
        # write taken from the root platform files' entries for it (#2019),
        # then every key neither writes from the profile it elects (#2117).
        merged = deep_merge(merged, self._tenant_layer(tenant_id, merged)[0])

        # #772: when the compiler resolved any `_custom_alerts` for this tenant,
        # OVERWRITE deep_merge's array-REPLACE result with the ADR-024 UNION (own +
        # inherited) — REPLACE would have dropped inherited platform/domain policy
        # recipes from an override tenant, blinding blast_radius to the highest-
        # blast-radius change class. This field FOLLOWS UNION INHERITANCE, unlike
        # every other array (which stays REPLACE); `_custom_alerts_resolution` makes
        # that explicit per-entry so the effective config is honest, not silently
        # inconsistent.
        #
        # We deliberately do NOT remove a deep_merge-derived `_custom_alerts` when
        # the resolver returned nothing: the resolver covers only `*.yaml` and could
        # be unavailable / have raised (→ empty map), so "no resolution" must fall
        # back to the deep_merge value rather than silently wiping a real declaration
        # (a missing/`.yml` tenant or a single parse error would otherwise drop the
        # field from EVERY tenant). deepcopy isolates the returned snapshot from the
        # shared resolver map (matching deep_merge's copy semantics).
        resolved = self._custom_alerts_resolved.get(tenant_id) if resolve_custom_alerts else None
        if resolved:
            merged["_custom_alerts"] = [copy.deepcopy(recipe) for recipe, _o, _own in resolved]
            merged["_custom_alerts_resolution"] = [
                {"name": (recipe.get("name") if isinstance(recipe, dict) else None),
                 "origin": origin, "is_own": is_own}
                for recipe, origin, is_own in resolved
            ]

        return merged

    def platform_overlay(self, tenant_id: str) -> list[dict]:
        """`[{file, keys}]`: the root platform files whose `tenants:` entry
        supplied values to `tenant_id`'s effective config, merge order, each
        with the keys it supplied (#2019). Empty when that layer supplies
        nothing. Same name and shape as the Go EffectiveConfig field."""
        return self._tenant_layer(tenant_id, self._chain_merged(tenant_id))[1]

    def profile_overlay(self, tenant_id: str) -> list[dict]:
        """`[{profile, file, keys}]`: the profile `tenant_id` elects and, per
        root platform file its filled-in values came from (merge order), the
        keys it supplied to the effective config (#2117). Empty when the
        profile supplies nothing. Same name and shape as the Go
        EffectiveConfig field."""
        return self._tenant_layer(tenant_id, self._chain_merged(tenant_id))[2]

    def _tenant_layer(self, tenant_id: str, chain: dict,
                      replace: "dict[str, Any] | None" = None,
                      tenant_raw: Any = None
                      ) -> "tuple[Any, list[dict], list[dict]]":
        """The override merged over `chain`: the tenant block, the platform
        overlay (#2019) under it, the elected profile (#2117) under both —
        and the two attributions. `replace` as for `platform_blocks`;
        `tenant_raw`, when given, stands in for the tenant's own block
        (`--what-if` on its tenant file)."""
        own, platform_sources = _overlay_tenant(
            self.tenants[tenant_id] if tenant_raw is None else tenant_raw,
            self.platform_blocks(tenant_id, replace=replace), chain)
        layer, profile_sources = _expand_profile(own, self.profiles(replace=replace), chain)
        return layer, platform_sources, profile_sources

    def _chain_merged(self, tenant_id: str) -> dict:
        """The tenant's defaults chain merged L0 → Ln (no tenant layer)."""
        merged: dict = {}
        for dp in self.defaults_chain[tenant_id]:
            ddata = self.defaults_data.get(str(dp), {})
            merged = deep_merge(merged, _defaults_block(ddata))
        return merged

    def source_info(self, tenant_id: str) -> dict:
        """Return source traceability for a tenant."""
        if tenant_id not in self.tenants:
            raise KeyError(f"Tenant '{tenant_id}' not found")

        chain = self.defaults_chain[tenant_id]
        effective = self.effective_config(tenant_id)
        source_h = _file_hash(self.tenant_files[tenant_id])
        merged_h = _canonical_hash(effective)

        info = {
            "tenant_id": tenant_id,
            "source_file": self._report_path(self._tenant_entries[tenant_id],
                                             self.tenant_files[tenant_id]),
            "source_hash": source_h,
            "merged_hash": merged_h,
            "defaults_chain": [
                self._report_path(entry, p)
                for entry, p in zip(self._defaults_chain_entries[tenant_id], chain)
            ],
        }
        # Omitted when empty, as Go's `platform_overlay,omitempty`.
        overlay = self.platform_overlay(tenant_id)
        if overlay:
            info["platform_overlay"] = overlay
        # Omitted when empty, as Go's `profile_overlay,omitempty` (#2117).
        profile = self.profile_overlay(tenant_id)
        if profile:
            info["profile_overlay"] = profile
        info["effective_config"] = effective
        return info

    def diff_tenants(self, id_a: str, id_b: str) -> dict:
        """Compare effective configs of two tenants."""
        eff_a = self.effective_config(id_a)
        eff_b = self.effective_config(id_b)

        only_a, only_b, different = {}, {}, {}
        all_keys = set(eff_a.keys()) | set(eff_b.keys())
        for k in sorted(all_keys):
            if k not in eff_b:
                only_a[k] = eff_a[k]
            elif k not in eff_a:
                only_b[k] = eff_b[k]
            elif eff_a[k] != eff_b[k]:
                different[k] = {"a": eff_a[k], "b": eff_b[k]}

        return {
            "tenant_a": id_a,
            "tenant_b": id_b,
            f"only_in_{id_a}": only_a,
            f"only_in_{id_b}": only_b,
            "different": different,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class WhatIfError(Exception):
    """A `--what-if` the simulation cannot answer (a caller error)."""


def _same_file(a: Path, b: Path) -> bool:
    """One file on disk — a hard link is one too (#2097 blind review 2, D:
    comparing resolved paths let a hard link to a scanned file through)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a == b


def _listed_file_of(scanner: "ConfDScanner", path: Path) -> "Path | None":
    """The scanned file *path* is (by path, else by inode), or None."""
    if path in scanner.listed_files:
        return path
    return next((f for f in sorted(scanner.listed_files) if _same_file(f, path)), None)


def what_if_result(scanner: "ConfDScanner", tid: str, what_if_path: Path,
                   what_if_entry: Path, what_if_data: Any,
                   what_if_platform_doc: Any,
                   replaces: "Path | None" = None) -> dict:
    """`--what-if`: `tid`'s effective config with `what_if_path`'s content
    (`what_if_data` as a defaults carrier reads it, `what_if_platform_doc`
    as a platform / tenant file reads it) in the place it names, diffed
    against the tree `scanner` read.

    `substitution_type`:
      - "substitute": `replaces` (resolved) is given — a file of the scanned
        tree. `what_if_path`'s content stands in for THAT file and the tree
        is evaluated again (#2097): as a carrier on `tid`'s chain, as a root
        platform file (its `tenants:` / `profiles:` blocks), as `tid`'s own
        tenant file — and as nothing at all where no plane reads it for
        `tid` (`_profiles.yaml` is not a chain level; a carrier on another
        branch is not on this chain). The baseline is the tree as scanned,
        so it holds `replaces`' current bytes; the simulation holds
        `what_if_path`'s.
      - "insert": no `replaces`, and `what_if_path` is inside conf.d but
        not a scanned file (a `.`-prefixed draft) — placed in the chain at
        its entry's level (`entry_level`).
      - "append-external": no `replaces`, and `what_if_path` is outside
        conf.d — the highest chain level.

    ⛔ #2097 F1: `what_if_path` that IS a scanned file, given without
    `replaces`, is refused. The scanner read that file's bytes for the
    baseline and the simulation would read the same bytes again — an
    in-place edit compared with itself, so every edit reported "no reload"
    (fail-open; chain carriers had it before #2097 too). For the same
    reason `replaces` resolving to `what_if_path` is refused, and so is a
    `replaces` that is not a scanned file (nothing there to stand in for).

    ⚠️ Not simulated in "substitute": a tenant file whose new content
    declares `tid` when `tid` lives in another file (the exporter would see
    a duplicate), and any OTHER tenant the new content adds or drops.

    Raises WhatIfError for the refusals above, and when `replaces` is
    `tid`'s own tenant file and the new content no longer declares `tid`."""
    if replaces is None:
        listed = _listed_file_of(scanner, what_if_path)
        if listed is not None:
            raise WhatIfError(
                f"--what-if file {what_if_path} is a file of the scanned tree"
                f"{'' if listed == what_if_path else f' ({listed}, through a hard link)'}: "
                f"the baseline already reads its current bytes, so the simulation would "
                f"compare the file with itself and never report a reload. Save the modified "
                f"content as a copy and run: --what-if <copy> --replaces {listed}")
        target = what_if_path
    else:
        if replaces not in scanner.listed_files:
            raise WhatIfError(
                f"--replaces {replaces} is not a file of the scanned tree {scanner.conf_d} "
                f"(it must name an existing config file the scan lists)")
        if _same_file(replaces, what_if_path):
            raise WhatIfError(
                f"--what-if {what_if_path} and --replaces {replaces} are the same file: "
                f"that compares the file with itself. Save the modified content as a "
                f"separate copy and pass the copy to --what-if")
        target = replaces
    # Baseline: current effective config. #772: use the RAW (deep_merge)
    # `_custom_alerts` here so it matches the simulated side below (which is
    # built from an in-memory modified chain the compiler walker cannot
    # resolve) — otherwise an unrelated what-if edit would falsely diff the
    # UNION baseline against the REPLACE simulation. The union view is the
    # normal-mode / blast_radius contract, not what-if's.
    baseline_effective = scanner.effective_config(tid, resolve_custom_alerts=False)
    baseline_merged_hash = _canonical_hash(baseline_effective)

    chain = scanner.defaults_chain[tid]
    chain_entries = scanner._defaults_chain_entries[tid]
    simulated_defaults_data = dict(scanner.defaults_data)
    simulated_defaults_data[str(target)] = what_if_data
    tenant_raw = None

    if replaces is not None:
        # ⛔ Substitution of `replaces`, on or off the chain (#2097): the
        # chain is unchanged, so a file off it changes the chain merge in
        # nothing (`simulated_defaults_data` is read only for chain levels).
        simulated_chain = list(chain)
        substitution_type = "substitute"
        if replaces == scanner.tenant_files.get(tid):
            block = what_if_platform_doc.get("tenants") if isinstance(what_if_platform_doc, dict) else None
            bodies = {_tenant_id(k): v for k, v in block.items()} if isinstance(block, dict) else {}
            if tid not in bodies:
                raise WhatIfError(f"--replaces {replaces} is tenant '{tid}''s own file, and "
                                  f"the --what-if content ({what_if_path}) does not declare '{tid}'")
            # #2739: `_tenant_body` passes a non-mapping body through, and
            # deep_merge then died on it (AttributeError traceback, rc 1).
            # Refused by name like a non-mapping document (`_UnsupportedShape`).
            # #2115: any tenant's, not only `tid`'s — the exporter cannot
            # decode the whole file, so it serves `tid` from it neither.
            why = _undecodable_tenant_body(what_if_platform_doc)
            if why is not None:
                raise WhatIfError(f"--what-if file {what_if_path} has an unsupported shape: {why}")
            tenant_raw = _tenant_body(bodies[tid])
        elif any(str(resolved) == str(target) for _n, resolved, _b in scanner._platform_files):
            # #2115: standing in for a root platform file (`_defaults.yaml`,
            # `_profiles.yaml`, …), whose `tenants:` block `platform_blocks`
            # reads with a non-mapping body silently dropped — rc 0 with the
            # tenant's platform values simply gone, where the exporter
            # cannot decode that file at all (effective exits 3).
            why = _undecodable_tenant_body(what_if_platform_doc)
            if why is not None:
                raise WhatIfError(f"--what-if file {what_if_path} has an unsupported shape: {why} "
                                  f"(it stands in for the root platform file {replaces}, which "
                                  f"the exporter could not decode)")
    else:
        # Insert according to directory depth if the ENTRY is inside
        # conf.d/, else append. #1967: both depths come from
        # `entry_level` — the directory holding the entry, never the
        # resolved target (a carrier or what-if link may point elsewhere
        # in conf.d, or outside it). Before, the chain side raised into a
        # `except ValueError` here and silently misfiled the what-if as
        # append-external, and the what-if side took its link's target's
        # level.
        what_if_depth = scanner.entry_level(what_if_entry)
        if what_if_depth is not None:
            # Insert sorted by depth so that outer (L0) precedes inner (L3)
            inserted = False
            simulated_chain = []
            for dp, entry in zip(chain, chain_entries):
                if not inserted and what_if_depth < scanner.entry_level(entry):
                    simulated_chain.append(what_if_path)
                    inserted = True
                simulated_chain.append(dp)
            if not inserted:
                simulated_chain.append(what_if_path)
            substitution_type = "insert"
        else:
            # what-if entry outside conf.d/ → append at end (highest override)
            simulated_chain = list(chain) + [what_if_path]
            substitution_type = "append-external"

    # Recompute effective config with simulated chain
    simulated: dict = {}
    for dp in simulated_chain:
        ddata = simulated_defaults_data.get(str(dp), {})
        simulated = deep_merge(simulated, _defaults_block(ddata))
    # #2019 / #2117: the same platform per-tenant layer and profile
    # expansion as the baseline — taken from the what-if document when
    # it stands in for a root platform file, so an edit to its
    # `tenants:` or `profiles:` block is simulated too.
    sim_tenant = scanner._tenant_layer(
        tid, simulated, replace={str(target): what_if_platform_doc},
        tenant_raw=tenant_raw)[0]
    simulated = deep_merge(simulated, sim_tenant)
    what_if_merged_hash = _canonical_hash(simulated)

    # Compute per-key diff
    only_baseline: dict = {}
    only_what_if: dict = {}
    changed: dict = {}
    for k in sorted(set(baseline_effective) | set(simulated)):
        if k not in simulated:
            only_baseline[k] = baseline_effective[k]
        elif k not in baseline_effective:
            only_what_if[k] = simulated[k]
        elif baseline_effective[k] != simulated[k]:
            changed[k] = {"baseline": baseline_effective[k], "what_if": simulated[k]}

    hash_changed = baseline_merged_hash != what_if_merged_hash
    return {
        "tenant_id": tid,
        "what_if_file": str(what_if_path),
        "substitution_type": substitution_type,
        # #2097 F1: the scanned file the what-if content stood in for
        # (`substitute`); None for `insert` / `append-external`.
        "replaces": None if replaces is None else str(replaces),
        "baseline_merged_hash": baseline_merged_hash,
        "what_if_merged_hash": what_if_merged_hash,
        "merged_hash_changed": hash_changed,
        "would_trigger_reload": hash_changed,  # per ADR-017 dual-hash logic
        "removed_keys": only_baseline,
        "added_keys": only_what_if,
        "changed_keys": changed,
    }


@exit_on_output_write_error
def main() -> None:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Describe effective tenant config with _defaults.yaml inheritance (ADR-017).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "tenant_id", nargs="?",
        help="Tenant ID to describe (omit with --all)",
    )
    parser.add_argument(
        "--conf-d", "-c", type=str, default=None,
        help="Path to conf.d/ directory (default: auto-detect from repo root)",
    )
    parser.add_argument(
        "--show-sources", "-s", action="store_true",
        help="Show inheritance chain sources for each field",
    )
    parser.add_argument(
        "--diff", "-d", type=str, default=None, metavar="TENANT_ID_2",
        help="Diff effective config against another tenant",
    )
    parser.add_argument(
        "--what-if", "-w", type=str, default=None, metavar="PATH",
        help="Simulate the effect of a modified conf.d file: diff baseline vs what-if "
             "effective config + merged_hash change. Without --replaces, PATH must not be "
             "a file of the scanned tree (that would compare the file with itself; rc 2): "
             "a PATH outside conf.d is merged as the highest defaults level "
             "(append-external), an unlisted path inside it (e.g. a '.'-prefixed draft) "
             "is inserted at its directory's level",
    )
    parser.add_argument(
        "--replaces", type=str, default=None, metavar="PATH",
        help="With --what-if: the conf.d file the --what-if content stands in for "
             "(substitute). The baseline reads PATH as it is now; PATH must be a file the "
             "scan lists, and must not be the --what-if file itself (rc 2)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Dump all tenants' effective configs",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file (default: stdout). Used with --all.",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "yaml"], default="json",
        help="Output format (default: json)",
    )
    args = parser.parse_args()
    # #2097 blind review 2 (E): before any mode branch — `--all` / `--diff`
    # returned rc 0 with `--replaces` silently ignored. `is not None`, not
    # truthiness: `--replaces ''` was taken as "not given" (#2509 blind review 3),
    # and so was `--what-if ''` (blind review 4).
    if args.what_if == "":
        parser.error("--what-if needs a path (got an empty string)")
    if args.replaces is not None and args.what_if is None:
        parser.error("--replaces requires --what-if")
    if args.replaces == "":
        parser.error("--replaces needs a path (got an empty string)")
    # #2739: the same shape as (E) above, one flag over — the `--all` /
    # `--diff` branches run before the what-if one, so `--what-if` (and its
    # `--replaces`) returned rc 0 with output identical to a run without it.
    if args.what_if is not None and (args.all or args.diff is not None):
        flags = "--what-if" + (" / --replaces" if args.replaces is not None else "")
        mode = "--all" if args.all else "--diff"
        parser.error(f"{flags} cannot be used together with {mode}: the simulation "
                     f"describes one tenant; drop {mode}, or run the two separately")

    # Resolve conf.d path
    if args.conf_d:
        conf_d = Path(args.conf_d)
    else:
        # ⛔ Was `.parent` four times (#1494). This file ships flattened into
        # the image (`/opt/da-tools/describe_tenant.py`, three ancestors), and
        # a `.parent` chain does not raise past the top — it SATURATES at the
        # filesystem root, so the walk silently produced `/` and then looked
        # for `/conf.d`. Search for the directory instead of counting levels;
        # the repo-layout answer is unchanged, and outside a checkout the
        # search simply comes up empty and the existing error path below says
        # so with the `--conf-d` remedy.
        here = Path(__file__).resolve().parent
        # ⛔ BOUNDED. The search stops at the repository root and never looks
        # above it. An unbounded `for base in (here, *here.parents)` walk —
        # which is what the first version of this fix did — keeps climbing
        # past the checkout and will happily bind to a `conf.d` that belongs
        # to somebody else: `C:\Users\<user>\conf.d` on a dev box, `/opt` or
        # `/` inside the image. That direction fails by silently describing
        # the WRONG tenants, which is worse than the level-counting bug it
        # replaced (that one at least stayed inside the repo).
        #
        # "Project root" = the nearest ancestor carrying one of
        # `PROJECT_ROOT_MARKERS`. ⚠️ `.git` ALONE is not enough: a source
        # tarball (`git archive`, a GitHub release zip, a vendored or rsync'd
        # copy) has no `.git`, and keying only on it turned a tree the OLD
        # code handled correctly into an error — a regression introduced while
        # fixing the unbounded walk, measured on two of ten layouts. `.git` is
        # a directory in a clone and a FILE in a git worktree, hence
        # `exists()`. The image carries none of these markers, so there the
        # search ends empty and the error path below prints the `--conf-d`
        # remedy — the same outcome the old code produced.
        repo_root = next(
            (base for base in (here, *here.parents)
             if any((base / m).exists() for m in PROJECT_ROOT_MARKERS)),
            None,
        )
        conf_d = None
        if repo_root is not None:
            if (repo_root / "conf.d").is_dir():
                conf_d = repo_root / "conf.d"
            else:
                fixtures = repo_root / "tests" / "fixtures"
                for fixture_dir in sorted(fixtures.glob("synthetic-*")):
                    if (fixture_dir / "conf.d").is_dir():
                        conf_d = fixture_dir / "conf.d"
                        break
        if conf_d is None:
            # Non-existent on purpose: the check below reports it with the
            # `--conf-d` remedy. Named under the repo root when we found one
            # so the message points somewhere the reader recognises.
            conf_d = (repo_root or here) / "conf.d"

    if not conf_d.exists():
        print(f"❌ conf.d/ not found at {conf_d}", file=sys.stderr)
        print(f"   Use --conf-d to specify the path.", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    try:
        scanner = ConfDScanner(conf_d)
    except DefaultsParseError as exc:  # #2413
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    print(f"📂 Scanned {conf_d}: {len(scanner.tenants)} tenants, {len(scanner.defaults_data)} defaults files", file=sys.stderr)

    def _output(data: Any) -> str:
        # #2371: keys in the exporter's spelling, and no `_GoKey` left for
        # yaml.dump to tag as a Python object.
        data = _go_keys(data)
        if args.format == "yaml":
            if yaml:
                return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
            print("⚠️  PyYAML not installed — falling back to JSON output "
                  "(pip install pyyaml)", file=sys.stderr)
            return _json_text(data, indent=2)
        return _json_text(data, indent=2)

    # #2049: THE checkpoint for "this tenant is declared by more than one
    # carrier". Every mode that names a tenant (default, --show-sources,
    # --diff for BOTH sides, --what-if) passes through it before anything
    # is computed; --all asks `duplicate_error` per tenant. Exit code is
    # EXIT_VIOLATION, not the not-found EXIT_CALLER_ERROR: the invocation
    # and the tree are readable, and the finding is one the user must fix
    # in conf.d — the code validate_config's `tenant_uniqueness` gives for
    # the same state. It also keeps "ambiguous" apart from "absent".
    def _refuse_duplicate(t: Any) -> None:
        msg = scanner.duplicate_error(t)
        if msg is not None:
            print(f"❌ {msg}", file=sys.stderr)
            sys.exit(EXIT_VIOLATION)

    # --all mode
    if args.all:
        result = {}
        duplicated = 0
        for tid in sorted(scanner.tenants.keys()):
            msg = scanner.duplicate_error(tid)
            if msg is not None:
                # Reported, not described; the other tenants still are.
                print(f"❌ {msg}", file=sys.stderr)
                duplicated += 1
                continue
            info = scanner.source_info(tid)
            # ⛔ A plain str, not the `_GoKey`: this key is a TENANT ID, which
            # the exporter keys by source text (#2114) — `_go_keys` must not
            # respell it, or tenants `010` and `8` both become "8" and one
            # silently overwrites the other (#2371 review F1).
            result[str.__str__(tid)] = info
        out = _output(result)
        if args.output:
            with output_write(args.output, flag="-o/--output"):
                Path(args.output).write_text(out, encoding="utf-8", newline="\n")
            print(f"✅ Written {len(result)} tenants to {args.output}", file=sys.stderr)
        else:
            print(out)
        if duplicated:
            print(f"❌ {duplicated} tenant(s) not described: declared in more "
                  f"than one file (see above).", file=sys.stderr)
            sys.exit(EXIT_VIOLATION)
        return

    if not args.tenant_id:
        parser.error("tenant_id is required (or use --all)")

    tid = args.tenant_id
    _refuse_duplicate(tid)
    if tid not in scanner.tenants:
        print(f"❌ Tenant '{tid}' not found. Available: {', '.join(sorted(scanner.tenants.keys())[:10])}...", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # --diff mode
    if args.diff:
        _refuse_duplicate(args.diff)
        if args.diff not in scanner.tenants:
            print(f"❌ Tenant '{args.diff}' not found.", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        result = scanner.diff_tenants(tid, args.diff)
        print(_output(result))
        return

    # --what-if mode: simulate a modified conf.d file
    if args.what_if is not None:
        what_if_path = Path(args.what_if).resolve()
        if not what_if_path.exists():
            print(f"❌ --what-if file not found: {what_if_path}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        # Load the simulated defaults content
        try:
            what_if_data = _load_yaml(what_if_path)
            # #2114: its `tenants:` block, keyed by source text like the
            # platform files it may stand in for — `_load_yaml` keeps
            # PyYAML's typing for the defaults chain it has always fed.
            what_if_platform_doc = _load_platform_doc(what_if_path)
        except _UnsupportedShape as e:  # #2459: parses, refused
            print(f"❌ --what-if file {what_if_path} has an unsupported shape: {e}",
                  file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        except Exception as e:  # noqa: BLE001 — named, then refused (#2413)
            print(f"❌ Failed to parse --what-if file {what_if_path}: {e}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

        try:
            result = what_if_result(
                scanner, tid, what_if_path, Path(args.what_if), what_if_data,
                what_if_platform_doc,
                replaces=Path(args.replaces).resolve() if args.replaces is not None else None)
        except WhatIfError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        print(_output(result))
        return

    # Default: show effective config
    if args.show_sources:
        result = scanner.source_info(tid)
    else:
        result = {"tenant_id": tid}
        overlay = scanner.platform_overlay(tid)
        if overlay:
            result["platform_overlay"] = overlay
        profile = scanner.profile_overlay(tid)
        if profile:
            result["profile_overlay"] = profile
        result["effective_config"] = scanner.effective_config(tid)
    print(_output(result))


if __name__ == "__main__":
    main()
