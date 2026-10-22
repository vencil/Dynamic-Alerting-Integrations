"""Every "at most one of" pair in the http_config schema, expanded (#2984).

definitions.httpConfig and the definitions under it state Alertmanager's
"at most one of" rules as `not: {required: [a, b], properties: {a: <set>,
b: <set>}}` members of `allOf` — at the top level, inside an `if` / `then` /
`else` branch, or through a `$ref` to a shared rule set (httpProxyRules). A
member can be deleted or neutralised and nothing else notices unless some
receiver depends on exactly that member. This test reads every such pair out
of the schema and expands each into three receivers:

  - `both`: a and b set, everything else valid — must be refused;
  - `only-a` / `only-b`: the same receiver with one of the two — must be
    accepted, which proves the pair, and nothing else, refuses `both`.

The rows live in http_config_exclusive_pairs.json next to this file, so the
same table is judged twice: here by the schema, and in
tests/alertmanager-inhibit (TestExclusivePairs_AlertmanagerVerdict) by
Alertmanager's config.Load. A schema change that adds, drops or reshapes a
pair turns this test red until the table is regenerated:

    python3 tests/shared/test_http_config_exclusive_pairs.py
"""
from __future__ import annotations

import json
import os
import sys

import jsonschema

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")
_TABLE = os.path.join(_HERE, "http_config_exclusive_pairs.json")
_PREFIX = "#/definitions/"
_URL = "https://h.example/a"

# Where an object of each definition sits in a receiver, and the smallest
# valid object of it (oauth2 needs client_id and token_url).
_CONTEXTS = {
    "httpConfig": ([], {}),
    "httpBasicAuth": (["basic_auth"], {}),
    "httpAuthorization": (["authorization"], {}),
    "httpOAuth2": (["oauth2"], {"client_id": "c", "token_url": "https://t.example/x"}),
    "httpTLSConfig": (["tls_config"], {}),
}
# Definitions that only lend their rules to the object referencing them.
_MIXINS = {"httpProxyRules"}
# A mapping value that is valid on its own, per key (an empty one otherwise).
_OBJECT_SAMPLES = {"oauth2": {"client_id": "c", "token_url": "https://t.example/x"}}
# Sibling keys a pair needs so that only the pair can refuse `both`: no_proxy
# needs a proxy_url key. (#3257 dropped the cert / key families: their pairs
# were the three forms of a client cert or key, and tls_config now takes only
# the inline form — no pair is left there.)
_FIXUPS = [
    ({"no_proxy"}, {"proxy_url": ""}),
]


def _defs() -> dict:
    with open(_SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)["definitions"]


def _value(key: str, shape: dict):
    if shape == {"type": "object"}:
        return _OBJECT_SAMPLES.get(key, {})
    if shape == {"type": "string", "minLength": 1}:
        return "x"
    if shape == {"$ref": _PREFIX + "yamlTrue"}:
        return True
    raise AssertionError(f"pair member {key!r}: unmodelled 'set' shape {shape}")


def _satisfy(cond: dict) -> dict:
    """The keys that make an `if` hold: `required` keys with a `const`."""
    assert set(cond) <= {"required", "properties"}, f"unmodelled if: {cond}"
    out = {}
    for k in cond.get("required", []):
        prop = cond.get("properties", {}).get(k, {})
        assert set(prop) == {"const"}, f"unmodelled if property {k}: {prop}"
        out[k] = prop["const"]
    return out


def _pairs(defs: dict, node, where: str, extra: dict):
    """(where, extra, a, b, shapes) for every pair reachable from node."""
    if isinstance(node, list):
        for i, x in enumerate(node):
            yield from _pairs(defs, x, f"{where}[{i}]", extra)
        return
    if not isinstance(node, dict):
        return
    ref = node.get("$ref", "")
    if ref[len(_PREFIX):] in _MIXINS:
        name = ref[len(_PREFIX):]
        yield from _pairs(defs, defs[name], f"{where}->{name}", extra)
    neg = node.get("not")
    if isinstance(neg, dict) and len(neg.get("required", [])) == 2:
        assert set(neg) == {"required", "properties"}, f"{where}: unmodelled not: {neg}"
        a, b = neg["required"]
        yield where, extra, a, b, neg["properties"]
    for key in ("allOf",):
        if key in node:
            yield from _pairs(defs, node[key], f"{where}.{key}", extra)
    if "if" in node:
        inner = list(_pairs(defs, node.get("then"), f"{where}.then", {}))
        if inner:  # only a branch holding pairs needs its `if` made true
            held = {**extra, **_satisfy(node["if"])}
            for w, e, a, b, shapes in inner:
                yield w, {**held, **e}, a, b, shapes
        if "else" in node:
            yield from _pairs(defs, node["else"], f"{where}.else", extra)


def _all_pair_holders(defs: dict) -> set[str]:
    """Every definition holding a two-key `not.required`, wherever it sits."""
    found = set()

    def scan(name, node):
        if isinstance(node, dict):
            neg = node.get("not")
            if isinstance(neg, dict) and len(neg.get("required", [])) == 2:
                found.add(name)
            for v in node.values():
                scan(name, v)
        elif isinstance(node, list):
            for v in node:
                scan(name, v)

    for name, d in defs.items():
        scan(name, d)
    return found


def _receiver(path: list[str], obj: dict) -> dict:
    hc = obj
    for key in reversed(path):
        hc = {key: hc}
    return {"type": "webhook", "url": _URL, "http_config": hc}


def build_rows(defs: dict) -> list[dict]:
    rows = []
    for ctx, (path, base) in _CONTEXTS.items():
        for where, extra, a, b, shapes in _pairs(defs, defs[ctx], ctx, {}):
            fix = {}
            for family, add in _FIXUPS:
                if {a, b} <= family or ({a, b} & family and family == {"no_proxy"}):
                    fix.update(add)
            obj = {**base, **extra, **fix}
            va, vb = _value(a, shapes[a]), _value(b, shapes[b])
            name = f"{where}:{a}+{b}"
            rows.append({"name": f"{name}:both", "receiver": _receiver(path, {**obj, a: va, b: vb}),
                         "expect": "reject"})
            rows.append({"name": f"{name}:only-{a}", "receiver": _receiver(path, {**obj, a: va}),
                         "expect": "accept"})
            rows.append({"name": f"{name}:only-{b}", "receiver": _receiver(path, {**obj, b: vb}),
                         "expect": "accept"})
    return rows


def _render(rows: list[dict]) -> str:
    return "[\n" + ",\n".join("  " + json.dumps(r, sort_keys=False) for r in rows) + "\n]\n"


def test_every_pair_holder_is_expanded():
    """A pair in a definition this test has no context for fails, rather than
    going unexpanded."""
    defs = _defs()
    holders = _all_pair_holders(defs) - {"tenantConfig"}
    reached = set(_CONTEXTS) | _MIXINS
    http = {h for h in holders if h.startswith("http")}
    assert http <= reached, f"pairs in definitions with no context here: {sorted(http - reached)}"
    assert http, "no pair found: the reader went blind"


def test_table_is_the_schema_expanded():
    rows = build_rows(_defs())
    with open(_TABLE, encoding="utf-8") as fh:
        committed = fh.read()
    assert committed == _render(rows), (
        "http_config_exclusive_pairs.json is not the schema's pairs expanded; "
        "run: python3 tests/shared/test_http_config_exclusive_pairs.py")
    # Every pair of every context is in it (38 when written; 10 since #3257
    # removed the file-reference keys and the 28 pairs that only they formed —
    # the four auth keys' 6 and the proxy rules' 2 in http_config and in
    # oauth2. This pins the floor, the equality above pins the rest.)
    assert len(rows) >= 3 * 10
    # No pair may name a key a conf.d receiver cannot carry: such a row would
    # judge a receiver the generator refuses whole.
    from _lib_validation import is_file_reference_key
    named = sorted({k for r in rows for k in _keys(r["receiver"]) if is_file_reference_key(k)})
    assert not named, f"pair rows name file-reference keys: {named}"


def _keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_schema_judges_each_row_as_the_table_says():
    with open(_SCHEMA, encoding="utf-8") as fh:
        validator = jsonschema.Draft7Validator(json.load(fh))
    with open(_TABLE, encoding="utf-8") as fh:
        rows = json.load(fh)
    wrong = []
    for row in rows:
        doc = {"tenants": {"t1": {"_routing": {"receiver": row["receiver"]}}}}
        got = "accept" if not list(validator.iter_errors(doc)) else "reject"
        if got != row["expect"]:
            wrong.append(f"{row['name']}: schema {got}, table {row['expect']}")
    assert not wrong, "\n".join(wrong)


if __name__ == "__main__":
    with open(_TABLE, "w", encoding="utf-8") as fh:
        fh.write(_render(build_rows(_defs())))
    print(f"wrote {_TABLE}", file=sys.stderr)
