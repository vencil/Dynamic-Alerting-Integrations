"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

import yaml

from _lib_constants import ONBOARD_HINTS_FILENAME
import _lib_input  # noqa: E402  (#3445: the one decoding of an input file)
# No import cycle: _lib_exitcodes imports only stdlib + _lib_compat (#1641).
import _lib_exitcodes  # noqa: E402
from _lib_label import safe_label  # noqa: E402,F401  (re-export, #3276)
# EXIT_CALLER_ERROR: kept as a module attribute (it was one before #4223).
from _lib_exitcodes import EXIT_CALLER_ERROR, die_caller_error  # noqa: F401

_F = TypeVar("_F", bound=Callable[..., Any])


class YamlFileError(yaml.YAMLError):
    """A YAML file that exists but cannot be read — and WHICH file (#1654).

    ``yaml.YAMLError`` subclass on purpose: every ``except yaml.YAMLError``
    (and ``except Exception``) a caller already has catches this, so a
    decode failure now takes the same message path as a syntax error
    instead of escaping as a traceback. Attributes:

    * ``path``  — the file as the caller named it.
    * ``cause`` — the original error (also ``__cause__``): the
      ``UnicodeDecodeError`` for content that is not valid UTF-8, the
      PyYAML error for bad syntax.

    ``str()`` is ONE line, ``<path>: <original message> (<CauseClass>)``,
    with the original's line breaks collapsed so the line/column PyYAML
    reports survive but a caller can interpolate it into a report line.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        detail = " ".join(str(cause).split()) or cause.__class__.__name__
        super().__init__(f"{path}: {detail} ({cause.__class__.__name__})")


# ⛔ The pure-Python parser, never libyaml's C one. The two differ on
# MALFORMED input — libyaml accepts a trailing tab the pure parser rejects,
# and parses nesting the pure parser dies on with RecursionError — and tools
# that read operator files have made the pure parser's limits part of their
# contract (`validate_config` / `_grar_parse` route RecursionError as
# "unreadable file, exit 1"). The libyaml opt-in (`SAFE_LOADER` /
# `safe_load`, `strict_safe_load(fast=True)`) lost its last caller in
# ADR-036 §12 and was deleted (#3206); `tests/shared/test_yaml_loader_is_libyaml.py`
# pins that `load_yaml_file` stays on the pure parser.


def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.

    Raises:
        YamlFileError: the file exists but cannot be read — content that is
            not valid UTF-8 (``cause`` is the ``UnicodeDecodeError``) OR bad
            YAML syntax (``cause`` is the PyYAML error). Before #1654 the
            first escaped as a bare ``UnicodeDecodeError`` — a ``ValueError``
            no ``except yaml.YAMLError`` in this repo saw, and one that never
            carried the path. ⚠️ Never swallowed into *default*: a file that
            is present but unreadable is the loudest input, not an empty one.
        OSError: as before; not wrapped.

    Decoding is STRICT UTF-8, exactly what the text-mode ``open`` did, so
    the accepted encodings are unchanged: this helper must not start
    serving (say) UTF-16 while ``validate_config``'s own reads and the
    routes generator still refuse it — that would be #1911's "one input,
    two answers" inside the Python tool family (blind review). Whether the
    family should follow the exporter's parser on other encodings is a
    separate decision. The decoded text is parsed from a named stream so
    PyYAML's own marks (``in "<path>", line N, column M``) keep naming the
    real file instead of ``"<unicode string>"``.
    """
    if not path or not Path(path).is_file():
        return default
    raw = Path(path).read_bytes()
    try:
        # #3445: the one decoding of an input file (`_lib_input.decode_bytes`):
        # strict UTF-8, the pure parser (see above), the stream named.
        data = _lib_input.decode_bytes(raw, fmt="yaml", name=str(path))
    except _lib_input.DocumentError as exc:
        raise YamlFileError(str(path), exc.cause) from exc.cause
    return data if data is not None else default


# ── Strict reading: a duplicate mapping key is an error (#2123) ─────────────
# The one definition lives in `_lib_yaml_strict` (#4064: `_lib_input` reads
# every declared input with it, and must not import this module); every name
# is re-exported here for the callers that import it from `_lib_io`.
from _lib_yaml_strict import (  # noqa: E402,F401  (re-export)
    DuplicateKeyError,
    RejectDuplicateKeys,
    StrictSafeLoader,
    _key_identity,
    _strict_loader_class,
    duplicate_in_mapping,
    find_duplicate_key,
    raise_on_duplicate_key,
    reject_equal_constructed_keys,
    strict_safe_load,
    strict_safe_load_all,
)



def load_confd_file(config_dir: "str | os.PathLike[str]", path: Optional[str],
                    default: Any = None, *, mode: Any = None, reader: Any = None) -> Any:
    """A conf.d file's content as `da-guard parse-tree` reads it (ADR-036
    step 4, #2906) — the by-path read of the routing tools, which do not
    parse conf.d with PyYAML. *config_dir* is the conf.d root *path* belongs
    to (the session's reader of it, `_lib_parse_cache.reader_for`); *mode* a
    `_lib_parse_cache.ParseMode` (default VALUES); *reader* a
    `_lib_parse_cache.ConfdReader` of *config_dir* to read through instead
    (one built with an explicit da-guard binary).

    *default* for a path that is no config file of the tree — one the
    reader's listing does not hold (an absent `_routing_profiles.yaml`), or
    one Go's `role` says is no file (a directory, a dangling link). ⛔ Not
    `is_file()` (#3206): it raised for a path under a directory this uid may
    not search, and answered False for a file it may not stat; a config file
    whose read fails raises. A file parse-tree cannot read raises
    :class:`YamlFileError` (``cause`` is the `_lib_parse_tree.FileUnreadable`,
    whose ``kind`` names why), so a caller's "cannot read this file" path
    reports it with no new branch. An empty file is ``{}``. No usable
    da-guard raises `_lib_parse_tree.ParseTreeError`, never a default."""
    if not path:
        return default
    import _lib_parse_cache  # lazy: it runs da-guard through modules importing this one
    from _lib_parse_tree import FileUnreadable
    reader = reader or _lib_parse_cache.reader_for(config_dir)
    entry = reader.entry(path)
    if entry is None or not entry.config_file:
        return default
    try:
        return reader.read(
            path, mode if mode is not None else _lib_parse_cache.VALUES)
    except FileUnreadable as exc:
        raise YamlFileError(str(path), exc) from exc


# ── #2164 → ADR-036 step 4 (#2906): a scalar in a STRING field that yaml.v3
# reads as something else
#
# Every reader of conf.d reads it the way yaml.v3 does (`da-guard
# parse-tree`), so the value a field holds is the one parse-tree's tag says:
# `channel: 1e3` is the float 1000 and `name: 2024-01-02` a timestamp, while
# `channel: yes` is the string "yes" (YAML 1.1's boolean words are strings to
# yaml.v3). The author is told to quote such a value — the #1017
# "STRING-ONLY, always quote it" rule applied to every string-typed field.
#
# The judgement reads a file's parse-tree nodes (`{text, tag, value}`
# scalars, aliases and `<<` merges already expanded). ⛔ Two things are
# deliberately NOT written down here:
#
# * WHICH texts are not strings. That is yaml.v3's verdict, parse-tree's
#   `tag`; no word list here.
# * WHICH fields are strings. That is read from the JSON Schema passed in,
#   so a field that becomes a string (or stops being one) there changes what
#   this reports with no edit here.
#
# parse-tree does not say whether a scalar was quoted, nor where it sits in
# the file: a finding names the value's path (`/tenants/t1/_routing/…`), and
# it is made the same for `yes` and `"yes"` — both are the string "yes".
#
# A field counts as a string field when, among the schema branches that can
# describe it, every one that constrains a SCALAR allows only `string` (plus
# `null`). A mixed field such as `string | number` is left alone: a bare
# number is a legal value there, and #1017's answer to that ambiguity was to
# tighten the schema to `type: string`, not to have a lint guess the intent.
_JSON_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})
_SCHEMA_COMBINATORS = ("allOf", "anyOf", "oneOf")


def _is_tree_scalar(node: Any) -> bool:
    """A parse-tree scalar (`_lib_parse_tree.is_scalar`; repeated here so this
    module needs no import of that one)."""
    return isinstance(node, dict) and isinstance(node.get("text"), str) and "tag" in node


def _written(path: str, value: str) -> str:
    """How the suggested line reads: `field: value`, or `- value` for a
    sequence item (no key to repeat)."""
    field = path.rsplit("/", 1)[-1]
    return f"- {value}" if field.isdigit() or not field else f"{field}: {value}"


class MisreadScalar:
    """One scalar in a string field that yaml.v3 reads as non-string.

    ``path`` is a JSON-pointer-like ``/tenants/t1/_routing/receiver/channel``;
    ``text`` the scalar's source text; ``resolved`` the short name of its
    yaml.v3 tag (``int``, ``float``, ``bool``, ``null``, ``timestamp``)."""

    __slots__ = ("path", "text", "resolved")

    def __init__(self, path: str, text: str, resolved: str) -> None:
        self.path, self.text, self.resolved = path, text, resolved

    def message(self) -> str:
        if self.resolved == "null":
            # ⛔ No "quote it" here: `"~"` would be a literal tilde, which is
            # almost never what an empty value meant.
            return (f"{self.path}: {self.text or '(empty)'!s} is YAML null — "
                    f"no value — but the schema requires a string for this "
                    f"field: write the value, quoted, or remove the key")
        return (f"{self.path}: {self.text!r} is read by yaml.v3 (every reader of "
                f"conf.d) as YAML {self.resolved}, not as the string the schema "
                f"requires for this field — quote it, without a tag: "
                f"{_written(self.path, chr(34) + self.text + chr(34))}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MisreadScalar({self.path} {self.text!r})"


def _schema_pointer(doc: Any, fragment: str) -> Any:
    node = doc
    for part in [p for p in fragment.split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _schema_expand(schema: Any, doc: str, schemas: dict[str, Any],
                   depth: int = 0) -> list[tuple[dict, str]]:
    """*schema* plus every branch it can stand for ($ref, allOf/anyOf/oneOf),
    each paired with the name of the schema document it lives in (so a $ref
    inside a referenced document resolves against THAT document).

    A cross-document ``$ref`` (``tenant-config.schema.json#/…``) is looked up
    by basename in *schemas*; an unknown one raises ``KeyError`` rather than
    silently checking less."""
    if not isinstance(schema, dict) or depth > 32:
        return []
    out = [(schema, doc)]
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target, _, fragment = ref.partition("#")
        tdoc = os.path.basename(target) if target else doc
        if tdoc not in schemas:
            raise KeyError(f"$ref {ref!r}: schema document {tdoc!r} not loaded")
        out.extend(_schema_expand(_schema_pointer(schemas[tdoc], fragment),
                                  tdoc, schemas, depth + 1))
    for kw in _SCHEMA_COMBINATORS:
        for sub in schema.get(kw) or ():
            out.extend(_schema_expand(sub, doc, schemas, depth + 1))
    return out


def _schema_key_children(cands: list[tuple[dict, str]], key: str,
                         schemas: dict[str, Any]) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    for schema, doc in cands:
        props = schema.get("properties")
        if isinstance(props, dict) and key in props:
            out.extend(_schema_expand(props[key], doc, schemas))
            continue
        matched = False
        for pattern, sub in (schema.get("patternProperties") or {}).items():
            if re.search(pattern, key):
                matched = True
                out.extend(_schema_expand(sub, doc, schemas))
        extra = schema.get("additionalProperties")
        if not matched and isinstance(extra, dict):
            out.extend(_schema_expand(extra, doc, schemas))
    return out


def _schema_item_children(cands: list[tuple[dict, str]], index: int,
                          schemas: dict[str, Any]) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    for schema, doc in cands:
        items = schema.get("items")
        if isinstance(items, dict):
            out.extend(_schema_expand(items, doc, schemas))
        elif isinstance(items, list):
            if index < len(items):
                out.extend(_schema_expand(items[index], doc, schemas))
            elif isinstance(schema.get("additionalItems"), dict):
                out.extend(_schema_expand(schema["additionalItems"], doc, schemas))
    return out


def _json_type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else "object"


def schema_scalar_types(cands: list[tuple[dict, str]]) -> Optional[frozenset]:
    """The JSON scalar types the branches in *cands* allow, or ``None`` when
    no branch constrains the value at all. Non-scalar types are dropped: a
    scalar can only ever be judged by the scalar branches."""
    types: set[str] = set()
    constrained = False
    for schema, _doc in cands:
        if "type" in schema:
            raw = schema["type"]
            types.update([raw] if isinstance(raw, str) else raw)
        elif "const" in schema:
            types.add(_json_type_of(schema["const"]))
        elif isinstance(schema.get("enum"), list):
            types.update(_json_type_of(v) for v in schema["enum"])
        else:
            continue
        constrained = True
    return frozenset(types & _JSON_SCALAR_TYPES) if constrained else None


def _tree_scalars_with_schema(root: Any, schema: dict,
                              schemas: Optional[dict[str, Any]],
                              schema_name: str) -> Iterator[tuple[dict, list, str]]:
    """Each parse-tree scalar under *root* (one file's nodes), with the schema
    branches that can describe its position and its JSON-pointer-like path,
    in document order (parse-tree's: keys byte-sorted)."""
    if root is None:
        return
    schemas = dict(schemas or {})
    schemas.setdefault(schema_name, schema)
    stack: list[tuple[Any, list[tuple[dict, str]], str]] = [
        (root, _schema_expand(schema, schema_name, schemas), "")]
    while stack:
        node, cands, path = stack.pop()
        if not cands:
            continue
        if _is_tree_scalar(node):
            yield node, cands, path
        elif isinstance(node, dict):
            for key in reversed(list(node)):
                stack.append((node[key], _schema_key_children(cands, key, schemas),
                              f"{path}/{key}"))
        elif isinstance(node, list):
            for idx in reversed(range(len(node))):
                stack.append((node[idx], _schema_item_children(cands, idx, schemas),
                              f"{path}/{idx}"))


def _walk_misread_scalars(root: Any, schema: dict, schemas: Optional[dict[str, Any]],
                          schema_name: str) -> list[tuple[dict, MisreadScalar]]:
    found: list[tuple[dict, MisreadScalar]] = []
    for node, cands, path in _tree_scalars_with_schema(root, schema, schemas, schema_name):
        if node["tag"] == "!!str":
            continue
        types = schema_scalar_types(cands)
        if types is None or types - {"null"} != {"string"}:
            continue
        if node["tag"] == "!!null" and "null" in types:
            continue
        found.append((node, MisreadScalar(path or "/", node["text"],
                                          node["tag"].lstrip("!"))))
    return found


def find_misread_scalars(root: Any, schema: dict,
                         schemas: Optional[dict[str, Any]] = None,
                         schema_name: str = "") -> list[MisreadScalar]:
    """Every scalar of *root* (a file's `da-guard parse-tree` nodes) that sits
    in a string-typed field of *schema* and that yaml.v3 read as something
    else (its tag is not `!!str`).

    *schemas* maps a schema document's basename to its parsed JSON, for
    cross-document ``$ref``; *schema_name* is *schema*'s own basename in it.
    See the block comment above for what is (and is not) a string field.
    """
    return [hit for _node, hit in _walk_misread_scalars(root, schema, schemas, schema_name)]


def misread_scalar_nodes(root: Any, schema: dict,
                         schemas: Optional[dict[str, Any]] = None,
                         schema_name: str = "") -> list[dict]:
    """The scalar NODES :func:`find_misread_scalars` reports — same walk, same
    verdict (#2695): a caller that reads such a value as the quoted string it
    was meant to be acts on the node, not on a path (a key holding ``/``
    makes two paths look nested)."""
    return [node for node, _hit in _walk_misread_scalars(root, schema, schemas, schema_name)]


# ── #3764: a number in a NUMBER field written in a spelling yaml.v3 rewrites
#
# A structured reserved value (`_custom_alerts`, `_metadata`, …) reaches the
# exporter's readers re-serialised: yaml.v3 decodes it and writes it back, so
# `min_events: 0x10` arrives as `16`, `010` as `8`, `1e3` as `1000`. The
# readers that judge such a field by its text (min_events enters the
# recipe_id slug as written, and its validator takes plain decimal digits
# only) then judge the rewritten text. The owner ruling on #3764 keeps the
# runtime as it reads (no second judgement there) and refuses the spelling
# at the entry instead — here, beside the string-field rule above, with the
# same inputs: parse-tree's tag says the scalar is a number, the schema says
# the field takes only numbers, and the text must then be the plain decimal
# spelling of that number — `0` or `-?[1-9][0-9]*`, plus `.digits` ending in
# a non-zero digit where the field takes a non-integer number. No spelling
# list: a text either is that, or not.
#
# Only a value the field can hold is this rule's: a non-integer in an
# integer field (`1.5`), or a non-finite one (`.inf`, which parse-tree gives
# no `value`), is a TYPE error the schema's own check (json_schema) reports;
# so is a number whose value has no plain spelling (an integer beyond
# yaml.v3's range, read as a float). Reporting those here would name a fix
# that does not exist and report the value twice.
#
# ⚠️ The entry is CI's validate-config: a tree changed in direct mode without
# it (a commit straight to git) is not judged until #3353's whole-tree
# verdict runs this row too.
_PLAIN_INTEGER = re.compile(r"0|-?[1-9][0-9]*")
_PLAIN_DECIMAL = re.compile(r"(0|-?[1-9][0-9]*)(\.[0-9]*[1-9])?|-0\.[0-9]*[1-9]")


class NondecimalNumber:
    """One number in a number-typed field written other than in plain
    decimal. ``path`` as for :class:`MisreadScalar`; ``text`` the scalar's
    source text; ``value`` yaml.v3's reading of it (parse-tree's ``value``);
    ``integer`` whether the field takes integers only."""

    __slots__ = ("path", "text", "value", "integer")

    def __init__(self, path: str, text: str, value: str, integer: bool) -> None:
        self.path, self.text, self.value, self.integer = path, text, value, integer

    def message(self) -> str:
        kind = "an integer" if self.integer else "a number"
        return (f"{self.path}: {self.text!r} is read by yaml.v3 (every reader of "
                f"conf.d) as {self.value}, and on the way to the exporter's readers "
                f"it is rewritten to that — this field takes {kind} written in "
                f"plain decimal (#3764): {_written(self.path, self.value)}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NondecimalNumber({self.path} {self.text!r})"


def find_nondecimal_numbers(root: Any, schema: dict,
                            schemas: Optional[dict[str, Any]] = None,
                            schema_name: str = "") -> list[NondecimalNumber]:
    """Every scalar of *root* (a file's `da-guard parse-tree` nodes) that sits
    in a field of *schema* taking only numbers (``integer`` / ``number``,
    plus ``null``), that yaml.v3 read as a number the field can hold, and
    whose text is not that number's plain decimal spelling (#3764). A field
    that also takes a string is left alone, as :func:`find_misread_scalars`
    leaves a mixed field; a value the field cannot hold (``1.5`` in an
    integer field, ``.inf``) is the schema's type check's to report."""
    found: list[NondecimalNumber] = []
    for node, cands, path in _tree_scalars_with_schema(root, schema, schemas, schema_name):
        if node["tag"] not in ("!!int", "!!float"):
            continue
        types = schema_scalar_types(cands)
        if types is None:
            continue
        numeric = types - {"null"}
        if not numeric or not numeric <= {"integer", "number"}:
            continue
        integer = numeric == {"integer"}
        plain = _PLAIN_INTEGER if integer else _PLAIN_DECIMAL
        if plain.fullmatch(node["text"]):
            continue
        # Its value, in the plain spelling the fix names — or, when it has
        # none the field can hold, the schema's type check's to report.
        value = node.get("value")
        if not isinstance(value, str) or not plain.fullmatch(value):
            continue
        found.append(NondecimalNumber(path or "/", node["text"], value, integer))
    return found


# ── which schema(s) the #2164 quoting walk applies to one conf.d file ────
#
# ⛔ ONE selection, for `check_confd_schema.validate_dir` (json_schema) and
# `validate-config`'s `yaml_quoting` row: were they to differ, a value would
# be reported twice, or read as quoted by json_schema in a file yaml_quoting
# never walked, so nobody reports it (#2695). It lives here, not in
# check_confd_schema, because yaml_quoting runs without that lint and
# without jsonschema.
CONFD_TENANT_SCHEMA = "tenant-config.schema.json"
CONFD_PLATFORM_SCHEMA = "platform-defaults.schema.json"
CONFD_ROUTING_PROFILES_SCHEMA = "routing-profiles.schema.json"
ROUTING_PROFILES_FILE_NAMES = ("_routing_profiles.yaml", "_routing_profiles.yml")
ROOT_PROFILES_DEFINITION = "rootPlatformProfiles"


def root_profiles_schema(platform_schema: dict) -> dict:
    """The platform schema's `definitions.rootPlatformProfiles` (#3647): what
    a ROOT platform file's `profiles:` block is held to — each profile's
    `_custom_alerts`, by `$ref` to the tenant schema's own definition.
    KeyError when the platform schema has none (one older than this tool)."""
    return platform_schema["definitions"][ROOT_PROFILES_DEFINITION]


def confd_quoting_schemas(entry: Any, schemas: "dict[str, Any]") -> "list[tuple[dict, str]]":
    """`(schema, schema name)` pairs the quoting walk applies to *entry* (a
    `_lib_parse_tree.Entry` of a config file), from *schemas* (basename →
    parsed schema; a name absent is a schema not in play).

    The file's own schema — `_defaults*` (da-guard's `defaults_document`)
    the platform schema, `_routing_profiles` the routing-profiles one, any
    other `_` file none, a tenant file the tenant schema — and, for a ROOT
    platform file (da-guard's `root_platform_file`), its `tenants:` block
    against the tenant schema (#2743) and its `profiles:` block against
    `rootPlatformProfiles` (#3647). Below the root no plane reads either
    block (#1576)."""
    platform = schemas.get(CONFD_PLATFORM_SCHEMA)
    routing = schemas.get(CONFD_ROUTING_PROFILES_SCHEMA)
    tenant = schemas.get(CONFD_TENANT_SCHEMA)
    pairs: list[tuple[dict, str]] = []
    if entry.defaults_document and platform is not None:
        pairs.append((platform, CONFD_PLATFORM_SCHEMA))
    elif entry.name in ROUTING_PROFILES_FILE_NAMES and routing is not None:
        pairs.append((routing, CONFD_ROUTING_PROFILES_SCHEMA))
    elif not entry.reserved and tenant is not None:
        pairs.append((tenant, CONFD_TENANT_SCHEMA))
    if entry.root_platform_file:
        if tenant is not None:
            pairs.append((tenant, CONFD_TENANT_SCHEMA))
        if platform is not None:
            pairs.append((root_profiles_schema(platform), CONFD_PLATFORM_SCHEMA))
    return pairs


# ── #2509 → #2906: a YAML 1.1 boolean word in a field that takes a boolean
#
# `send_resolved: on` / `_routing_enforced: {enabled: yes}` is the STRING
# "on" / "yes" to yaml.v3, quoted or not, and so to every reader of conf.d:
# a boolean field refuses it (the schema's `yamlBool` takes booleans only).
# The WARN says what to write instead. The words are YAML 1.1's (PyYAML's
# resolver) minus yaml.v3's six — `_lib_validation._YAML_BOOL_LITERALS`.
_YAML12_BOOL_TEXTS = frozenset(("true", "True", "TRUE", "false", "False", "FALSE"))


class YamlBoolWord:
    """One YAML 1.1 boolean word (a string to yaml.v3) in a boolean field.
    ``path`` as for :class:`MisreadScalar`; ``text`` the word as written."""

    __slots__ = ("path", "text")

    def __init__(self, path: str, text: str) -> None:
        self.path, self.text = path, text

    def message(self) -> str:
        value = "true" if self.text.lower() in ("yes", "on") else "false"
        return (f"{self.path}: {self.text!r} is the YAML 1.1 boolean word, which "
                f"yaml.v3 (every reader of conf.d) reads as the string "
                f"{self.text!r}, quoted or not — this field takes a boolean: "
                f"write {_written(self.path, value)}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"YamlBoolWord({self.path} {self.text!r})"


def find_yaml11_bool_words(root: Any, schema: dict,
                           schemas: Optional[dict[str, Any]] = None,
                           schema_name: str = "") -> list[YamlBoolWord]:
    """Every `!!str` scalar of *root* (parse-tree nodes) in a field of *schema*
    that takes a boolean, whose text is a YAML 1.1 boolean word yaml.v3 does
    not read as a boolean (`yes` / `no` / `on` / `off`, three spellings
    each) — #2509. Arguments as for :func:`find_misread_scalars`."""
    from _lib_validation import _YAML_BOOL_LITERALS
    words = frozenset(_YAML_BOOL_LITERALS) - _YAML12_BOOL_TEXTS
    found: list[YamlBoolWord] = []
    for node, cands, path in _tree_scalars_with_schema(root, schema, schemas, schema_name):
        if node["tag"] != "!!str" or node["text"] not in words:
            continue
        types = schema_scalar_types(cands)
        if types is None or "boolean" not in types:
            continue
        found.append(YamlBoolWord(path or "/", node["text"]))
    return found


def exit_on_yaml_file_error(fn: _F) -> _F:
    """Decorate a CLI ``main`` so an unreadable YAML input exits 2, named.

    The shared answer for every tool whose ``load_yaml_file`` call sites
    have no handler of their own (#1654): instead of 21 hand-written
    ``try/except`` blocks, the entry point is wrapped once, and
    :class:`YamlFileError` becomes ``ERROR: cannot read <path>: <reason>``
    on stderr plus ``sys.exit(EXIT_CALLER_ERROR)`` — an IO/decode failure
    is a caller error in ``_lib_exitcodes``, not the rc 1 an uncaught
    traceback produced.

    ⚠️ Only :class:`YamlFileError`. A bare ``yaml.YAMLError`` has no path to
    name and did not come from the helper; it keeps propagating so the
    layer that raised it stays visible.

    The exit goes through ``die_caller_error`` with
    ``reason="yaml_file_unreadable"``: one stderr line and the caller-error
    code; under ``--json`` stdout stays empty (#3558 — a caller error
    carries no document unless the tool returns one as a ``Report``).

    Lives here rather than in ``_lib_exitcodes`` because that module is
    kept stdlib-only for the tools that import nothing else; this one
    already owns ``yaml`` and the error class.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except YamlFileError as exc:
            # ``safe_label``: the path is an untrusted filename (#1538) —
            # measured, a ``\x1b[31m`` in the name reached the terminal raw.
            die_caller_error(f"ERROR: cannot read {safe_label(exc)}",
                             reason="yaml_file_unreadable")
    return _wrapped  # type: ignore[return-value]


def find_metric_dictionary(tool_dir: str) -> "str | None":
    """Locate ``metric-dictionary.yaml`` for a tool living in *tool_dir*.

    The image flattens every tool next to the dictionary (build.sh); the
    repository keeps the tools under ``scripts/tools/ops/`` and the dictionary
    one level up. Looking only beside the tool — what analyze-gaps and migrate
    did — silently yielded an empty dictionary for every reader who ran
    ``python3 scripts/tools/ops/<tool>.py`` as the docs teach (issue 1513).
    Returns the first file that exists, else ``None`` so the caller can say so.
    """
    for candidate in (os.path.join(tool_dir, "metric-dictionary.yaml"),
                      os.path.join(os.path.dirname(os.path.abspath(tool_dir)),
                                   "metric-dictionary.yaml")):
        if os.path.isfile(candidate):
            return candidate
    return None


# #4223: the output entry points live in _lib_output (no PyYAML, no
# _lib_constants). Re-exported so `from _lib_io import …` keeps working;
# a spy or monkeypatch must target _lib_output, where they call each other.
from _lib_output import (  # noqa: E402,F401  (re-export)
    OutputWriteError,
    _die_on_write_error,
    STDOUT_PATH,
    OUTPUT_ENTRY_POINTS,
    claim_stdout,
    _is_our_stdout,
    targets_stdout,
    _StdoutSink,
    _O_OPERATOR,
    _kernel_open_operator_output,
    open_operator_output,
    write_operator_output,
    write_operator_output_or_die,
    is_special_file,
    same_file,
    check_repo_artifact,
    _repo_artifact_mode,
    write_repo_artifact,
    ensure_dir,
    ensure_dir_or_die,
    _output_write_names_target,
    output_write,
    exit_on_output_write_error,
)


def write_onboard_hints(
    output_dir: str,
    hints: dict[str, Any],
    *,
    flag: Optional[str] = None,
) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).
        flag: The CLI flag *output_dir* came from, named in the error message;
              ``None`` for an internally derived path.

    Returns:
        Absolute path to the written file.

    Raises:
        OutputWriteError: see :func:`open_operator_output` (#1641, #3107).
    """
    path = str(Path(output_dir) / ONBOARD_HINTS_FILENAME)
    with open_operator_output(path, flag=flag) as fh:
        json.dump(hints, fh, indent=2, ensure_ascii=False)
    return path


# ── Common argparse helpers ─────────────────────────────────────────
# Extracted in v2.4.0 Phase B to eliminate argparse boilerplate across 20+ tools.


def add_config_dir_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    default: str | None = None,
    help_text: str = "Path to tenant config directory (conf.d/)",
) -> None:
    """Add ``--config-dir`` argument with standard defaults."""
    parser.add_argument(
        "--config-dir",
        required=required and default is None,
        default=default,
        help=help_text,
    )


def add_json_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Output as JSON (for CI integration)",
) -> None:
    """Add ``--json`` boolean flag for machine-readable output."""
    parser.add_argument("--json", action="store_true", dest="json_output", help=help_text)


def add_ci_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "CI mode: exit 1 on any issue",
) -> None:
    """Add ``--ci`` boolean flag for CI exit-code behaviour."""
    parser.add_argument("--ci", action="store_true", help=help_text)


#: Where ``--prometheus`` points when neither it nor ``$PROMETHEUS_URL`` is given.
PROMETHEUS_DEFAULT_URL = "http://localhost:9090"
#: The env var a flag made by :func:`add_prometheus_arg` falls back to; also
#: the value of that action's ``env_fallback`` attribute (#3528).
PROMETHEUS_ENV = "PROMETHEUS_URL"


def add_prometheus_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
    help_text: str = "Prometheus URL (default: $PROMETHEUS_URL or http://localhost:9090)",
) -> argparse.Action:
    """Add ``--prometheus``, judged at parse time (``url_type``, #3528).

    ⛔ The default is NOT ``$PROMETHEUS_URL`` (#3528): argparse runs ``type=``
    on a string default, so an unusable env value would stop every run of
    the tool at parse time — paths that never query Prometheus included.
    The flag defaults to *default* (``None`` unless given); the tool resolves
    the env fallback where it queries, through :func:`prometheus_url`, which
    judges the env value with the same ``url_problem``.
    """
    from _lib_validation import url_type
    action = parser.add_argument(
        "--prometheus",
        type=url_type(empty="refuse"),
        default=default,
        help=help_text,
    )
    # The declaration "--prometheus may be left out: $PROMETHEUS_URL stands
    # in" lives on the parser (#3528); the CLI-contract lint and the tests
    # read it here — there is no dispatcher-side list any more.
    action.env_fallback = PROMETHEUS_ENV
    return action


def prometheus_url(value: "str | None", default: str = PROMETHEUS_DEFAULT_URL, *,
                   judge: bool = True) -> str:
    """The Prometheus URL a tool sends to (#3528): *value* (``--prometheus``,
    already judged by ``url_type``) when given, else ``$PROMETHEUS_URL``, else
    *default*.

    The env value is judged HERE, with the same ``url_problem`` the flag is
    parsed with — at the point of use, so an unusable env stops only a run
    that would query with it, as a named caller error (rc 2). An empty
    ``$PROMETHEUS_URL`` counts as unset (`` or ``, not the ``get()`` default),
    so a ConfigMap key that resolves empty falls back to *default*.

    *judge*: ``False`` only where nothing is sent — a dry run that reports
    the URL it would query.
    """
    if value:
        return value
    env = os.environ.get(PROMETHEUS_ENV)
    if not env:
        return default
    if not judge:
        return env
    from _lib_validation import url_problem
    problem = url_problem(env)
    if problem is not None:
        raise _lib_exitcodes.CallerError(
            f"$PROMETHEUS_URL (the fallback for --prometheus) is not a usable URL "
            f"{safe_label(env)!r}: {problem}", reason="prometheus_url_unusable")
    return env
