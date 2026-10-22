#!/usr/bin/env python3
"""
sync_schema.py — Generate the reserved `_` key lists from the one registry.

The top-level `_` keys of conf.d are listed ONCE, in
components/threshold-exporter/app/pkg/config/reserved_keys.json (ADR-038
decision 2): which reader reads each key, in which layer, and the retired
keys with their hints. Every other copy is GENERATED from it:

  - Python : the block between the GENERATED markers in
             scripts/tools/_lib_constants.py (VALID_RESERVED_KEYS,
             VALID_RESERVED_PREFIXES, TENANT_ROUTING_KEYS,
             PLATFORM_ROUTING_KEYS, TOP_LEVEL_READ_ELSEWHERE,
             RETIRED_RESERVED_KEYS, RESERVED_KEY_READERS,
             RESERVED_PREFIX_READERS)
  - Go     : components/threshold-exporter/app/pkg/config/reserved_keys_gen.go
  - Schema : the `_` keys of docs/schemas/tenant-config.schema.json
             (tenantConfig.properties and the propertyNames pattern that
             closes the `_` namespace, #3016 R3) and of
             docs/schemas/platform-defaults.schema.json (top-level
             properties and patternProperties)

A schema property's BODY is hand-written and kept; only which `_` keys exist
is generated (a new key gets a stub carrying the registry's description, a
key gone from the registry is removed).

Whether the registry matches what the readers actually do is not this tool's
question: tests/shared/test_reserved_key_probes.py measures it.

Usage:
  sync_schema.py [--registry PATH] [--root DIR] [--check | --update]

Options:
  --registry PATH  The registry (default: <root>/components/threshold-exporter/
                   app/pkg/config/reserved_keys.json)
  --root DIR       Repository root the generated files live under (default:
                   the repository this script is in)
  --check          Exit 1 if any generated file is stale (for CI)
  --update         Rewrite the stale generated files

Exit codes: 0 in sync (or updated), 1 stale under --check, 2 the registry or a
target cannot be read or is malformed.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

REGISTRY_REL = "components/threshold-exporter/app/pkg/config/reserved_keys.json"
PY_REL = "scripts/tools/_lib_constants.py"
GO_REL = "components/threshold-exporter/app/pkg/config/reserved_keys_gen.go"
TENANT_SCHEMA_REL = "docs/schemas/tenant-config.schema.json"
PLATFORM_SCHEMA_REL = "docs/schemas/platform-defaults.schema.json"

LAYERS = ("tenant", "platform_tenants", "profile", "subtree_defaults", "platform_top")
READERS = ("exporter", "generator", "custom_alert_compiler", "policy_engine", "annotation")
#: The layers that end up in a tenant's own config: a key read in one of them
#: is a reserved TENANT key.
TENANT_LAYERS = frozenset({"tenant", "platform_tenants", "profile"})
#: A key's value shape, as its readers parse it (`value.shape`).
SHAPES = ("scalar", "mapping", "list", "scalar_or_mapping")
VALUE_FIELDS = frozenset({"shape", "check", "refers_to"})
BATCH_FIELDS = frozenset({"unset"})
#: The key whose content is the tenant's scope (environment/domain): a key
#: whose value names an entry in a layer the exporter reads it from decides
#: the tenant's scope too (``scope_metadata_keys``).
METADATA_KEY = "_metadata"

PY_BEGIN = "# BEGIN GENERATED reserved keys"
PY_END = "# END GENERATED reserved keys"


class RegistryError(ValueError):
    """The registry is malformed; the message names what and where."""


def _repo_root():
    return Path(_THIS_DIR).resolve().parents[2]


# ---------------------------------------------------------------- registry

def load_registry(path):
    """The registry at *path*, checked. Raises RegistryError on any shape it
    does not expect — a misspelt layer or reader would otherwise generate a
    list that silently leaves the key out."""
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise RegistryError(f"{path}: cannot read: {e}") from e
    return check_registry(reg, path)


def check_registry(reg, path="registry"):
    """*reg* (the registry's parsed JSON), checked as load_registry checks
    it; *path* names it in the messages."""
    if not isinstance(reg, dict):
        raise RegistryError(f"{path}: not a JSON object")
    want = {"$comment", "layers", "readers", "keys", "prefixes", "retired"}
    if set(reg) != want:
        raise RegistryError(f"{path}: top-level keys must be {sorted(want)}, got {sorted(reg)}")
    if tuple(reg["layers"]) != LAYERS:
        raise RegistryError(f"{path}: layers must be {list(LAYERS)} in that order")
    if tuple(reg["readers"]) != READERS:
        raise RegistryError(f"{path}: readers must be {list(READERS)} in that order")
    for section in ("keys", "prefixes"):
        for name, entry in reg[section].items():
            where = f"{section}.{name}"
            if not name.startswith("_") or name != name.strip():
                raise RegistryError(f"{where}: a name must start with '_'")
            allowed = ("read_by", "description", "note") + (("value", "batch") if section == "keys" else ())
            if not isinstance(entry, dict) or not set(entry) <= set(allowed):
                raise RegistryError(f"{where}: allowed fields are {', '.join(allowed)}")
            if not isinstance(entry.get("description"), str) or not entry["description"]:
                raise RegistryError(f"{where}: description must be a non-empty string")
            read_by = entry.get("read_by")
            if not isinstance(read_by, dict):
                raise RegistryError(f"{where}: read_by must be an object")
            if section == "keys" and not read_by:
                raise RegistryError(f"{where}: a key nothing reads belongs under retired")
            for reader, layers in read_by.items():
                if reader not in READERS:
                    raise RegistryError(f"{where}: unknown reader {reader!r}")
                if (not isinstance(layers, list) or not layers
                        or any(l not in LAYERS for l in layers)
                        or len(set(layers)) != len(layers)):
                    raise RegistryError(f"{where}.read_by.{reader}: a non-empty list of distinct layers")
    for name, entry in reg["keys"].items():
        _check_value(f"keys.{name}", entry)
        _check_batch(f"keys.{name}", entry)
    for name, hint in reg["retired"].items():
        if not name.startswith("_") or name in reg["keys"]:
            raise RegistryError(f"retired.{name}: must start with '_' and not be a key")
        if not isinstance(hint, str) or not hint:
            raise RegistryError(f"retired.{name}: the hint must be a non-empty string")
    # A batch op that removes a scope key would move the tenant's scope with
    # no post-state authorization (tenant-api touchesScopeMetadata judges only
    # what a patch sets); `_metadata` itself is the scope.
    for name in sorted(scope_metadata_keys(reg)) + [METADATA_KEY]:
        if reg["keys"].get(name, {}).get("batch", {}).get("unset"):
            raise RegistryError(f"keys.{name}.batch.unset: {name} decides the tenant's scope, "
                                "so a batch cannot remove it")
    return reg


def _check_value(where, entry):
    """`value`: required for a key read in a tenant's config (tenant-api
    decides from its shape whether a batch patch's string can set it); the
    platform-top-only keys may leave it out."""
    value = entry.get("value")
    if value is None:
        if _layers(entry) & TENANT_LAYERS:
            raise RegistryError(f"{where}: a key read in a tenant's config needs value.shape")
        return
    if not isinstance(value, dict) or not set(value) <= VALUE_FIELDS:
        raise RegistryError(f"{where}.value: allowed fields are {', '.join(sorted(VALUE_FIELDS))}")
    if value.get("shape") not in SHAPES:
        raise RegistryError(f"{where}.value.shape: must be one of {', '.join(SHAPES)}")
    if "check" in value:
        if not isinstance(value["check"], str) or not value["check"]:
            raise RegistryError(f"{where}.value.check: must be a non-empty string")
        # The check is bound to a function of the exporter's parser
        # (pkg/config/reserved_values.go): a key the exporter does not read
        # has no parser there to judge it.
        if "exporter" not in entry["read_by"]:
            raise RegistryError(f"{where}.value.check: only a key the exporter reads can name a check")
    if "refers_to" in value and value["refers_to"] not in LAYERS:
        raise RegistryError(f"{where}.value.refers_to: must be one of {', '.join(LAYERS)}")


def _check_batch(where, entry):
    """`batch`: what a tenant-api batch op may do to the key."""
    batch = entry.get("batch")
    if batch is None:
        return
    if not isinstance(batch, dict) or not set(batch) <= BATCH_FIELDS:
        raise RegistryError(f"{where}.batch: allowed fields are {', '.join(sorted(BATCH_FIELDS))}")
    if "unset" in batch and batch["unset"] is not True:
        raise RegistryError(f"{where}.batch.unset: must be true (or left out)")


def scope_metadata_keys(reg):
    """The keys whose value names an entry (`value.refers_to`) of a layer the
    exporter reads `_metadata` in: the tenant's scope then comes from that
    entry's `_metadata` when no layer writes its own (`_profile`)."""
    meta = reg["keys"].get(METADATA_KEY, {}).get("read_by", {}).get("exporter", [])
    return sorted(k for k, e in reg["keys"].items()
                  if e.get("value", {}).get("refers_to") in meta)


def batch_unsettable_keys(reg):
    """The keys a batch op's `unset` may remove: `batch.unset`, and every
    retired key. A retired key is unsettable on purpose: nothing reads it,
    so removing it changes no output, and while it is on disk the write gate
    refuses every other write to the tenant — a batch must be able to
    remove it."""
    return sorted({k for k, e in reg["keys"].items() if e.get("batch", {}).get("unset")}
                  | set(reg["retired"]))


def is_routing_key(k):
    """`_routing` or `_routing_<…>` (Go config.IsRoutingKey)."""
    return k == "_routing" or k.startswith("_routing_")


def _layers(entry):
    return {l for layers in entry["read_by"].values() for l in layers}


def derive(reg):
    """The lists every generated file holds, from the registry."""
    keys = reg["keys"]
    tenant = sorted(k for k, e in keys.items() if _layers(e) & TENANT_LAYERS)
    top = sorted(k for k, e in keys.items() if "platform_top" in _layers(e))
    return {
        "reserved_keys": tenant,
        "reserved_prefixes": list(reg["prefixes"]),
        "tenant_routing_keys": [k for k in tenant if is_routing_key(k)],
        "platform_routing_keys": [k for k in top if is_routing_key(k)],
        "top_level_read_elsewhere": top,
        # A prefix is OPEN in the schema when something reads keys under it
        # in a tenant's config (`_state_<filter>`: the suffix is a platform
        # `state_filters` name this file cannot know); `_routing` is closed:
        # its keys are listed.
        "open_prefixes": [p for p, e in reg["prefixes"].items() if _layers(e) & TENANT_LAYERS],
        "retired": dict(sorted(reg["retired"].items())),
        "descriptions": {k: e["description"] for k, e in keys.items()},
        # Every reader of every key and prefix, by layer, in registry order:
        # served-values hands the readers other than the exporter to its
        # caller (ADR-038 decision 5).
        "read_by": {k: e["read_by"] for k, e in keys.items()},
        "prefix_read_by": {p: e["read_by"] for p, e in reg["prefixes"].items()},
        "value_shapes": {k: e["value"]["shape"] for k, e in sorted(keys.items()) if "value" in e},
        "value_checks": {k: e["value"]["check"] for k, e in sorted(keys.items())
                         if "check" in e.get("value", {})},
        "batch_unsettable": batch_unsettable_keys(reg),
        "scope_metadata_keys": scope_metadata_keys(reg),
    }


# ---------------------------------------------------------------- Python

def render_python(d):
    def fset(name, items, kind="frozenset"):
        body = "".join(f"    {json.dumps(k)},\n" for k in items)
        if kind == "set":
            return f"{name}: Final[set[str]] = {{\n{body}}}\n"
        return f"{name}: Final[frozenset[str]] = frozenset({{\n{body}}})\n"

    prefixes = ", ".join(json.dumps(p) for p in d["reserved_prefixes"])
    retired = "".join(f"    {json.dumps(k)}:\n        {json.dumps(v, ensure_ascii=False)},\n"
                      for k, v in d["retired"].items())

    def layers(ls):
        body = ", ".join(json.dumps(l) for l in ls)
        return f"({body},)" if len(ls) == 1 else f"({body})"

    def readers(read_by, indent):
        if not read_by:
            return "{}"
        body = "".join(f"{indent}    {json.dumps(r)}: {layers(ls)},\n" for r, ls in read_by.items())
        return "{\n" + body + indent + "}"

    key_readers = "".join(f"    {json.dumps(k)}: {readers(rb, '    ')},\n"
                          for k, rb in sorted(d["read_by"].items()))
    prefix_readers = "".join(f"    ({json.dumps(p)}, {readers(rb, '    ')}),\n"
                             for p, rb in d["prefix_read_by"].items())
    return (
        f"{PY_BEGIN} — scripts/tools/dx/sync_schema.py --update writes this\n"
        f"# block from {REGISTRY_REL}. DO NOT EDIT.\n"
        + fset("VALID_RESERVED_KEYS", d["reserved_keys"], "set")
        + f"VALID_RESERVED_PREFIXES: Final[tuple[str, ...]] = ({prefixes})\n"
        + fset("TENANT_ROUTING_KEYS", d["tenant_routing_keys"])
        + fset("PLATFORM_ROUTING_KEYS", d["platform_routing_keys"])
        + fset("TOP_LEVEL_READ_ELSEWHERE", d["top_level_read_elsewhere"])
        + f"RETIRED_RESERVED_KEYS: Final[dict[str, str]] = {{\n{retired}}}\n"
        + "# Each key's and prefix's readers, reader -> the layers it reads it in.\n"
        + f"RESERVED_KEY_READERS: Final[dict[str, dict[str, tuple[str, ...]]]] = {{\n{key_readers}}}\n"
        + f"RESERVED_PREFIX_READERS: Final[tuple[tuple[str, dict[str, tuple[str, ...]]], ...]] = (\n{prefix_readers})\n"
        + PY_END + "\n"
    )


def splice_python(text, block):
    """*text* with the GENERATED block replaced by *block*."""
    if text.count(PY_BEGIN) != 1 or text.count(PY_END) != 1:
        raise RegistryError(f"{PY_REL}: expected one {PY_BEGIN!r} and one {PY_END!r} line")
    start = text.index(PY_BEGIN)
    end = text.index("\n", text.index(PY_END)) + 1
    return text[:start] + block + text[end:]


# ---------------------------------------------------------------- Go

def _go_str(s):
    return json.dumps(s, ensure_ascii=False)


def render_go(d):
    def lines(items):
        return "".join(f"\t{_go_str(k)},\n" for k in items)

    retired = "".join(f"\t{{{_go_str(k)}, {_go_str(v)}}},\n" for k, v in d["retired"].items())

    def readers(read_by):
        # Aligned as gofmt aligns a composite literal's values.
        width = max((len(_go_str(r)) for r in read_by), default=0)
        body = "".join(f"\t\t{_go_str(r)}:{' ' * (width - len(_go_str(r)) + 1)}"
                       f"{{{', '.join(_go_str(l) for l in layers)}}},\n"
                       for r, layers in read_by.items())
        return f"{{\n{body}\t}}" if body else "{}"

    key_readers = "".join(f"\t{_go_str(k)}: {readers(rb)},\n"
                          for k, rb in sorted(d["read_by"].items()))
    prefix_readers = "".join(f"\t{{{_go_str(p)}, map[string][]string{readers(rb)}}},\n"
                             for p, rb in d["prefix_read_by"].items())

    def str_map(m):
        width = max((len(_go_str(k)) for k in m), default=0)
        body = "".join(f"\t{_go_str(k)}:{' ' * (width - len(_go_str(k)) + 1)}{_go_str(v)},\n"
                       for k, v in m.items())
        return f"map[string]string{{\n{body}}}" if body else "map[string]string{}"

    def str_slice(items):
        return f"[]string{{\n{lines(items)}}}" if items else "[]string{}"

    return (
        "// Code generated by scripts/tools/dx/sync_schema.py; DO NOT EDIT.\n"
        f"// Source: {REGISTRY_REL} (ADR-038 decision 2).\n"
        "\n"
        "package config\n"
        "\n"
        "// validReservedKeys are the reserved tenant keys: read in a tenant's\n"
        "// own config (tenant file, root platform `tenants:` entry, profile).\n"
        f"var validReservedKeys = keySet(\n{lines(d['reserved_keys'])})\n"
        "\n"
        "// validReservedPrefixes reserve every key under them.\n"
        f"var validReservedPrefixes = []string{{\n{lines(d['reserved_prefixes'])}}}\n"
        "\n"
        "// tenantRoutingKeys / platformRoutingKeys are the `_routing` keys the\n"
        "// route generator reads in a tenant's config / at the top level of a\n"
        "// root conf.d `_` file (#2983).\n"
        f"var tenantRoutingKeys = keySet(\n{lines(d['tenant_routing_keys'])})\n"
        "\n"
        f"var platformRoutingKeys = keySet(\n{lines(d['platform_routing_keys'])})\n"
        "\n"
        "// topLevelReadElsewhere are the `_` keys another reader takes from the\n"
        "// top level of a root conf.d `_` file.\n"
        f"var topLevelReadElsewhere = []string{{\n{lines(d['top_level_read_elsewhere'])}}}\n"
        "\n"
        "// retiredReservedKeys are keys nothing reads any more, with the hint\n"
        "// printed when one is found.\n"
        f"var retiredReservedKeys = []retiredKey{{\n{retired}}}\n"
        "\n"
        "// reservedKeyReaders / reservedPrefixReaders are each key's and\n"
        "// prefix's readers, reader → the layers it reads it in.\n"
        f"var reservedKeyReaders = map[string]map[string][]string{{\n{key_readers}}}\n"
        "\n"
        f"var reservedPrefixReaders = []prefixReaders{{\n{prefix_readers}}}\n"
        "\n"
        "// reservedValueShapes is each key's value shape as its readers parse\n"
        "// it (`value.shape`): scalar, mapping, list or scalar_or_mapping.\n"
        f"var reservedValueShapes = {str_map(d['value_shapes'])}\n"
        "\n"
        "// reservedValueChecks names, per key, the exporter-side function that\n"
        "// judges its value (`value.check`; bound in valueChecks).\n"
        f"var reservedValueChecks = {str_map(d['value_checks'])}\n"
        "\n"
        "// batchUnsettableKeys are the keys a tenant-api batch op's `unset` may\n"
        "// remove: `batch.unset`, and every retired key — on purpose: nothing\n"
        "// reads a retired key, and while it is on disk the write gate refuses\n"
        "// every other write to the tenant.\n"
        f"var batchUnsettableKeys = {str_slice(d['batch_unsettable'])}\n"
        "\n"
        "// scopeMetadataKeys are the keys whose value names an entry\n"
        "// (`value.refers_to`) of a layer the exporter reads `_metadata` in, so\n"
        "// they decide the tenant's environment/domain.\n"
        f"var scopeMetadataKeys = {str_slice(d['scope_metadata_keys'])}\n"
    )


# ---------------------------------------------------------------- schemas

_DEC = json.JSONDecoder()
_WS = " \t\r\n"


def _skip_ws(text, i):
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def members(text, obj):
    """The direct members of the JSON object whose `{` is at *obj*:
    ``[(key, key_start, value_start, value_end)]`` in file order."""
    if text[obj] != "{":
        raise RegistryError(f"expected an object at offset {obj}")
    out = []
    i = _skip_ws(text, obj + 1)
    if text[i] == "}":
        return out
    while True:
        key, after_key = _DEC.raw_decode(text, i)
        j = _skip_ws(text, after_key)
        if text[j] != ":":
            raise RegistryError(f"expected ':' at offset {j}")
        vstart = _skip_ws(text, j + 1)
        _, vend = _DEC.raw_decode(text, vstart)
        out.append((key, i, vstart, vend))
        i = _skip_ws(text, vend)
        if text[i] == "}":
            return out
        if text[i] != ",":
            raise RegistryError(f"expected ',' or '}}' at offset {i}")
        i = _skip_ws(text, i + 1)


def locate(text, path):
    """Offset of the value at *path* (a list of member names) in *text*."""
    at = _skip_ws(text, 0)
    for name in path:
        found = [m for m in members(text, at) if m[0] == name]
        if len(found) != 1:
            raise RegistryError(f"expected exactly one {'.'.join(path)}")
        at = found[0][2]
    return at


def _indent_of(text, at):
    line = text.rindex("\n", 0, at) + 1
    return text[line:at]


def _dump(value, indent):
    return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)


def set_member(text, obj_path, name, value):
    """*text* with member *name* of the object at *obj_path* set to *value*
    (added as the object's first member when absent)."""
    obj = locate(text, obj_path)
    ms = members(text, obj)
    for key, kstart, vstart, vend in ms:
        if key == name:
            return text[:vstart] + _dump(value, _indent_of(text, kstart)) + text[vend:]
    if not ms:
        raise RegistryError(f"{'.'.join(obj_path)} is empty")
    indent = _indent_of(text, ms[0][1])
    new = f"{json.dumps(name)}: {_dump(value, indent)},\n{indent}"
    return text[:ms[0][1]] + new + text[ms[0][1]:]


def _drop_member(text, ms, i):
    """*text* without member *i* of *ms* (and the comma that joined it)."""
    if i + 1 < len(ms):
        return text[:ms[i][1]] + text[ms[i + 1][1]:]
    if i == 0:
        raise RegistryError(f"refusing to empty an object (member {ms[i][0]!r})")
    return text[:ms[i - 1][3]] + text[ms[i][3]:]


def sync_underscore_members(text, obj_path, keep, required, stub):
    """*text* with the `_`-prefixed members of the object at *obj_path*
    synced: each name in *required* that is missing is added as
    ``stub(name)``, placed before the first `_` member that sorts after it
    (else after the last `_` member); each `_` member ``keep(name)`` refuses
    is removed. The bodies of the members kept are not touched, and non-`_`
    members are left alone."""
    for name in sorted(required):
        ms = members(text, locate(text, obj_path))
        if any(m[0] == name for m in ms):
            continue
        under = [m for m in ms if m[0].startswith("_")]
        after = [m for m in under if m[0] > name]
        if after:
            indent = _indent_of(text, after[0][1])
            member = f"{json.dumps(name)}: {_dump(stub(name), indent)}"
            text = text[:after[0][1]] + member + ",\n" + indent + text[after[0][1]:]
        else:
            last = under[-1] if under else ms[-1]
            indent = _indent_of(text, last[1])
            member = f"{json.dumps(name)}: {_dump(stub(name), indent)}"
            text = text[:last[3]] + ",\n" + indent + member + text[last[3]:]
    while True:
        ms = members(text, locate(text, obj_path))
        extra = [i for i, m in enumerate(ms) if m[0].startswith("_") and not keep(m[0])]
        if not extra:
            return text
        text = _drop_member(text, ms, extra[0])


PROPERTY_NAMES_COMMENT = (
    "GENERATED by scripts/tools/dx/sync_schema.py --update from "
    "components/threshold-exporter/app/pkg/config/reserved_keys.json — "
    "DO NOT EDIT. Closes the `_` namespace (#3016 R3): a `_`-prefixed key is "
    "either one the platform reads, or a typo nothing reads.")


def expected_property_names(d):
    """The `tenantConfig.propertyNames` subschema: a `_` key must be a
    reserved tenant key or start with an open prefix."""
    keys = d["reserved_keys"]
    open_prefixes = d["open_prefixes"]
    alts = ["^(?!_)", "^(?:" + "|".join(re.escape(k) for k in keys) + ")$"]
    if open_prefixes:
        alts.append("^(?:" + "|".join(re.escape(p) for p in open_prefixes) + ")")
    allowed = ", ".join(keys + [f"{p}<name>" for p in open_prefixes])
    return {
        "$comment": PROPERTY_NAMES_COMMENT,
        "pattern": "|".join(alts),
        "patternErrorMessage": (
            "A key starting with '_' must be one the platform reads: "
            f"{allowed}. Any other '_' key is read by nothing."),
    }


def render_tenant_schema(text, d):
    pn = expected_property_names(d)
    text = set_member(text, ["definitions", "tenantConfig"], "propertyNames", pn)
    # A `_state_<filter>` example property (`_state_maintenance`) is kept: the
    # pattern admits it. One the pattern refuses no tenant file could carry.
    pat = re.compile(pn["pattern"])
    return sync_underscore_members(
        text, ["definitions", "tenantConfig", "properties"],
        keep=lambda k: pat.search(k) is not None,
        required=d["reserved_keys"],
        stub=lambda k: {"description": d["descriptions"][k]})


def render_platform_schema(text, d):
    """The defaults file's top level: the reserved tenant keys (a defaults
    document read whole merges them into its tenants) and the keys another
    reader takes from the top level; one patternProperty per prefix."""
    names = sorted(set(d["reserved_keys"]) | set(d["top_level_read_elsewhere"]))
    text = sync_underscore_members(
        text, ["properties"], keep=lambda k: k in names, required=names,
        stub=lambda k: {"description": d["descriptions"][k]})
    patterns = ["^" + p for p in d["reserved_prefixes"]]
    for p in patterns:
        if p not in [m[0] for m in members(text, locate(text, ["patternProperties"]))]:
            text = set_member(text, ["patternProperties"], p, {})
    while True:
        ms = members(text, locate(text, ["patternProperties"]))
        extra = [i for i, m in enumerate(ms) if m[0].startswith("^_") and m[0] not in patterns]
        if not extra:
            return text
        text = _drop_member(text, ms, extra[0])


# ---------------------------------------------------------------- main

def targets(root, d):
    """``[(relpath, render(current_text) -> wanted_text)]``."""
    return [
        (PY_REL, lambda t: splice_python(t, render_python(d))),
        (GO_REL, lambda _t: render_go(d)),
        (TENANT_SCHEMA_REL, lambda t: render_tenant_schema(t, d)),
        (PLATFORM_SCHEMA_REL, lambda t: render_platform_schema(t, d)),
    ]


def run(root, registry, update):
    """``(stale relpaths, errors)``; writes the stale files when *update*."""
    reg = load_registry(registry)
    d = derive(reg)
    stale = []
    for rel, render in targets(root, d):
        path = Path(root) / rel
        try:
            current = path.read_text(encoding="utf-8") if path.exists() or rel != GO_REL else ""
        except (OSError, UnicodeDecodeError) as e:
            raise RegistryError(f"{rel}: cannot read: {e}") from e
        wanted = render(current)
        if wanted != current:
            stale.append(rel)
            if update:
                path.write_text(wanted, encoding="utf-8", newline="\n")
    return stale


def main(argv=None):
    """CLI entry point: generate (--update) or check (--check) the lists."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", default=None,
                        help=f"the registry (default: <root>/{REGISTRY_REL})")
    parser.add_argument("--root", default=None,
                        help="repository root (default: the repository this script is in)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="exit 1 if any generated file is stale (for CI)")
    mode.add_argument("--update", action="store_true",
                      help="rewrite the stale generated files")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _repo_root()
    registry = Path(args.registry) if args.registry else root / REGISTRY_REL
    try:
        stale = run(root, registry, args.update)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not stale:
        print(f"OK: generated reserved-key lists match {REGISTRY_REL}")
        return EXIT_OK
    verb = "UPDATED" if args.update else "STALE"
    for rel in stale:
        print(f"{verb}: {rel}")
    if args.update:
        return EXIT_OK
    print(f"Run: python3 scripts/tools/dx/sync_schema.py --update", file=sys.stderr)
    return EXIT_VIOLATION if args.check else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
