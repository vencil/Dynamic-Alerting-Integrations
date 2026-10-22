"""Python RECEIVER_TYPES ↔ tenant-config.schema.json receiver parity (#2137).

The receiver presence contract — which fields are required, and which groups
need EXACTLY ONE field set — is declared in:

  - JSON Schema: docs/schemas/tenant-config.schema.json (`required` + a `oneOf`
    whose branches each require one non-empty field)
  - Python:      scripts/tools/_lib_constants.py RECEIVER_TYPES
                 (`required` + `exactly_one_of`)

The schema is the hub. This test pins the Python copy to it, reading the
schema as JSON and the copy as a value. Until ADR-036 step 3 (#2905) a Go copy
(pkg/receiverspec Check, used by da-guard) was pinned to the same schema; it
was deleted with da-guard's routing checks, and pkg/receiverspec keeps only
the list of receiver types the exporter looks up (TestKnown_MatchesSchema).

Emptiness is part of the contract. Python treats "" and null as unset,
as Alertmanager does (its config is a Go struct; both decode to the zero
value — a YAML `service_key:` with no value is null), so the schema reader
demands a single pinned type plus `minLength >= 1` (`minItems` for arrays) on
every required field and every exactly-one branch — otherwise an empty or
null key would count as "given" in the schema only, and
{service_key: "" | null, routing_key: "r"} would match both branches.

Shared case table:
components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json
Its `am` column is Alertmanager's own verdict, asserted by
tests/alertmanager-inhibit/receiver_cases_test.go with config.Load (#2180).

Value shapes of required fields (#2180) follow the schema's type: a string
field must be a string and match the schema `pattern` when there is one (URL
and smarthost formats, written once as schema definitions and read by Python
at run time); email `to` given as a list needs non-empty string items.

Optional values (#2295) are refused only where Alertmanager refuses them: a
property referencing `definitions.yamlBool` must be a boolean or null for the
generator (#2906: a YAML 1.1 boolean word given as a STRING is refused there,
while the schema's enum still takes it — those rows carry `python_differs`), and http_config
follows Alertmanager's HTTPClientConfig / ProxyConfig rules — at most one auth
method (`HTTP_CONFIG_AUTH_FIELDS`), string-only tokens and proxy_url. Whether a
proxy_url parses as a URL is left to amtool (the generator's --validate gate),
so those rows carry `python_differs` (the Python verdict and why), as
tests/shared/routing_policy_parity_matrix.json does. What is nested inside
http_config (tls_config, oauth2, authorization, basic_auth, the header
mappings; #2984) only the schema and Alertmanager judge: those rows carry
`python_differs` too. No row carries a Go verdict: the Go copy that judged
receivers was deleted in ADR-036 step 3 (#2905), and `valid` / `am` /
`python_differs` are the only verdict columns. A row may carry its receiver as
YAML text (`yaml`) instead of JSON: it is read with PyYAML here (plain `on` a boolean, quoted
`"on"` a string; see the plain-scalar candidates below). `schema_valid`
records a row where the schema cannot judge like the pipeline (proxy_url parsing,
a yaml row's plain-string `to`); `strict` names each row
Alertmanager accepts but the platform refuses on purpose.

It also pins the ACCEPTED field set (schema `properties` vs Python
required + optional + metadata): the schema is `additionalProperties: false`,
so a field Python forwards but the schema lacks is rejected by schema
validation even though the pipeline would use it.

The schema reader fails on any presence-shaping keyword or branch shape it
does not model, so an unmodelled constraint cannot read as "no constraint".

It also pins the receiver-type enums of docs/schemas/domain-policy.schema.json
(`allowed_receiver_types` / `forbidden_receiver_types`, #3116) to the same
set, both ways: the generator refuses a policy entry that is not a
RECEIVER_TYPES key.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import os

import jsonschema
import pytest
import yaml

from _lib_constants import RECEIVER_TYPES
from _grar_merge import build_receiver_config

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")
_CASES = os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "pkg",
                      "receiverspec", "testdata", "receiver_presence_cases.json")

_UNMODELLED = ("anyOf", "allOf", "not", "if", "dependencies", "dependentRequired")


def _rejects_empty(prop: dict | None) -> bool:
    """True when the property rejects "" and null, which Python reads as unset.

    The type must be pinned to one type: minLength / minItems do not apply to
    null, so `type: ["string", "null"]` + minLength would still let null in.
    """
    prop = prop or {}
    bound = {"string": "minLength", "array": "minItems"}.get(prop.get("type"))
    return bound is not None and prop.get(bound, 0) >= 1


def _schema_receivers() -> dict[str, dict]:
    with open(_SCHEMA, encoding="utf-8") as f:
        defs = json.load(f)["definitions"]
    out: dict[str, dict] = {}
    for branch in defs["receiver"]["oneOf"]:
        ref = branch["$ref"]
        assert ref.startswith("#/definitions/"), ref
        name = ref[len("#/definitions/"):]
        d = defs[name]
        bad = [k for k in _UNMODELLED if k in d]
        assert not bad, f"{name} uses {bad}; this parity check does not model it"
        rtype = d["properties"]["type"]["const"]
        required = sorted(f for f in d.get("required", []) if f != "type")
        groups = []
        if "oneOf" in d:
            group = []
            for i, br in enumerate(d["oneOf"]):
                req = br.get("required", [])
                assert len(req) == 1 and set(br) <= {"required", "properties"}, (
                    f"{name}.oneOf[{i}] is not {{'required': [k], 'properties': {{k: ...}}}}: {br}")
                props = br.get("properties", {})
                assert set(props) <= {req[0]} and set(props.get(req[0], {})) <= {"type", "minLength"}, (
                    f"{name}.oneOf[{i}].properties constrains something unmodelled: {props}")
                group.append((req[0], _rejects_empty(props.get(req[0]))))
            groups.append(sorted(group))
        out[rtype] = {
            "required": required,
            # Python truthiness: an empty value never satisfies presence.
            "required_rejects_empty": {f: _rejects_empty(d["properties"].get(f)) for f in required},
            "exactly_one_of": [[f for f, _ in g] for g in groups],
            "group_rejects_empty": {f: e for g in groups for f, e in g},
            "fields": sorted(f for f in d["properties"] if f != "type"),
        }
    return out


def _python_receivers() -> dict[str, dict]:
    out = {}
    for rtype, spec in RECEIVER_TYPES.items():
        groups = spec.get("exactly_one_of", [])
        out[rtype] = {
            "required": sorted(spec["required"]),
            "exactly_one_of": sorted(sorted(g) for g in groups),
            "fields": sorted(set(spec["required"]) | set(spec["optional"])
                             | set(spec.get("metadata", []))
                             | {f for g in groups for f in g}),
        }
    return out


SCHEMA = _schema_receivers()
PYTHON = _python_receivers()
with open(_CASES, encoding="utf-8") as _fh:
    CASES = json.load(_fh)


def _receiver(case: dict) -> dict:
    """A row's receiver: JSON as is, or its `yaml` text read with PyYAML."""
    if "yaml" in case:
        return yaml.safe_load(case["yaml"])
    return case["receiver"]


def test_sources_are_read_not_empty():
    assert len(SCHEMA) >= 6
    assert set(SCHEMA) == set(PYTHON)
    assert len(CASES) >= 7


_DOMAIN_POLICY_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas",
                                     "domain-policy.schema.json")
_POLICY_TYPE_FIELDS = ("allowed_receiver_types", "forbidden_receiver_types")


@pytest.mark.parametrize("field", _POLICY_TYPE_FIELDS)
def test_domain_policy_receiver_type_enum_is_the_receiver_set(field):
    """#3116: a domain policy's receiver-type lists name the receiver types
    the generator renders, no more and no fewer. The generator refuses the
    tree on any other entry (`unknown_receiver_type_entries`, keyed on
    RECEIVER_TYPES), so an enum value it lacks (`opsgenie` was one) passes
    schema validation yet refuses every run, and a receiver type the enum
    lacks cannot be constrained under schema validation."""
    with open(_DOMAIN_POLICY_SCHEMA, encoding="utf-8") as f:
        prop = json.load(f)["definitions"]["policyConstraints"]["properties"][field]
    enum = prop["items"]["enum"]
    assert len(enum) == len(set(enum)), f"{field}: duplicate enum value in {enum}"
    assert set(enum) == set(RECEIVER_TYPES) == set(SCHEMA), (
        f"domain-policy.schema.json {field} enum {sorted(enum)} != generator "
        f"RECEIVER_TYPES {sorted(RECEIVER_TYPES)} / tenant-config.schema.json "
        f"receiver types {sorted(SCHEMA)}")
    for example in prop.get("examples", []):
        assert set(example) <= set(enum), f"{field}: example {example} outside the enum"


@pytest.mark.parametrize("rtype", sorted(SCHEMA))
@pytest.mark.parametrize("aspect", ["required", "exactly_one_of", "fields"])
def test_python_matches_schema(rtype, aspect):
    assert PYTHON[rtype][aspect] == SCHEMA[rtype][aspect], (
        f"{rtype}.{aspect}: Python RECEIVER_TYPES {PYTHON[rtype][aspect]} "
        f"!= tenant-config.schema.json {SCHEMA[rtype][aspect]}")


@pytest.mark.parametrize("rtype", sorted(SCHEMA))
def test_schema_rejects_empty_like_python(rtype):
    """Python reads "" and null as unset; the schema must too."""
    accepts_empty = [f for f, ok in {**SCHEMA[rtype]["required_rejects_empty"],
                                     **SCHEMA[rtype]["group_rejects_empty"]}.items() if not ok]
    assert not accepts_empty, (
        f"{rtype}: schema lets {accepts_empty} be empty, but Python "
        "treat empty and null as unset — pin `type` and add minLength: 1 (minItems for arrays)")


def test_group_fields_are_listed_optional():
    """Group fields stay in `optional` so required+optional walkers see them."""
    for rtype, spec in RECEIVER_TYPES.items():
        for group in spec.get("exactly_one_of", []):
            missing = [f for f in group if f not in spec["optional"]]
            assert not missing, f"{rtype}: {missing} in exactly_one_of but not optional"


with open(_SCHEMA, encoding="utf-8") as _fh:
    _VALIDATOR = jsonschema.Draft7Validator(json.load(_fh))


def test_url_fields_carry_a_schema_pattern():
    """#2180: every URL / smarthost field has a `pattern` in the schema.

    Python reads the pattern from the schema at run time
    (_lib_validation.receiver_required_problem) instead of holding a copy, so
    dropping it from the schema would silently switch the format check off.
    """
    from _lib_constants import RECEIVER_URL_FIELDS
    from _lib_validation import _receiver_field_schemas

    fields = _receiver_field_schemas()
    missing = [f"{rtype}.{f}" for rtype, fs in RECEIVER_URL_FIELDS.items() for f in fs
               if not fields[rtype][f].get("pattern")]
    assert not missing, f"no schema pattern for {missing}"


def test_shared_case_table_am_column():
    """Each row's `am` is Alertmanager's verdict on the receiver the pipeline
    emits; tests/alertmanager-inhibit asserts it against config.Load. A row AM
    rejects must be invalid: one receiver AM cannot load fails the whole reload."""
    bad = [c["name"] for c in CASES if c.get("am") not in ("accept", "reject", "n/a")]
    assert not bad, f"rows without a valid `am`: {bad}"
    loose = [c["name"] for c in CASES if c["am"] == "reject" and c["valid"]]
    assert not loose, f"Alertmanager rejects these but the table says valid: {loose}"
    # #2295: the platform refuses only what Alertmanager refuses, except for
    # deliberate items, each named by its row's `strict`.
    unnamed = [c["name"] for c in CASES
               if (not c["valid"] and c["am"] == "accept") != bool(c.get("strict"))]
    assert not unnamed, f"strict refusals without a reason, or a reason on a non-strict row: {unnamed}"
    assert any("yaml" in c for c in CASES), "no `yaml` rows: the YAML 1.1 words go untested"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_case_table_schema_and_python_agree(case):
    """The schema and the generator's receiver build, row by row."""
    receiver = _receiver(case)
    doc = {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}
    schema_errors = [e.message for e in _VALIDATOR.iter_errors(doc)]
    cfg, warnings = build_receiver_config(dict(receiver), "t1")
    assert (not schema_errors) == case.get("schema_valid", case["valid"]), f"schema: {schema_errors}"
    differs = case.get("python_differs")
    python_valid = differs["valid"] if differs else case["valid"]
    assert (cfg is not None) == python_valid, f"python: {warnings}"


def test_python_differs_rows_say_why():
    """#2295: a row where the generator departs from `valid` names the reason."""
    bad = [c["name"] for c in CASES if "python_differs" in c
           and (set(c["python_differs"]) != {"valid", "reason"} or not c["python_differs"]["reason"]
                or c["python_differs"]["valid"] == c["valid"])]
    assert not bad, f"python_differs must be {{valid, reason}} and differ from `valid`: {bad}"


def test_no_row_carries_a_go_verdict():
    """#3093: the Go receiver check is deleted (#2905), so no column records
    its verdict; a `go_differs` here would be a column no reader asserts."""
    bad = [c["name"] for c in CASES if "go_differs" in c]
    assert not bad, f"go_differs has no reader since #2905: {bad}"


@pytest.mark.parametrize("receiver", [
    {"send_resolved": "Off"},
    {"send_resolved": "true"},
    {"http_config": {"proxy_from_environment": "yes"}},
])
def test_pipeline_refuses_yaml_bool_words_given_as_strings(receiver):
    """#2906 (was #2295's rewrite): a boolean word given as a STRING is not
    turned into the boolean it spells — the receiver is refused
    (invalid_receiver_field), the generator judges by type (ADR-036 §2)."""
    cfg, warnings = build_receiver_config(
        {"type": "webhook", "url": "https://h.example/a", **receiver}, "t1")
    assert cfg is None
    assert [w.kind for w in warnings] == ["invalid_receiver_field"], warnings


def test_pipeline_passes_booleans_through_unchanged():
    """A real boolean (an unquoted word PyYAML builds as one included) is
    written as given."""
    cfg, _ = build_receiver_config(
        {"type": "webhook", "url": "https://h.example/a", "send_resolved": False,
         "http_config": {"proxy_from_environment": True}}, "t1")
    entry = cfg["webhook_configs"][0]
    assert entry["send_resolved"] is False
    assert entry["http_config"]["proxy_from_environment"] is True


# --- PyYAML's implicit typing of plain scalars (#2295) ----------------------
#
# bearer_token / bearer_token_file / proxy_url / no_proxy must be strings.
# PyYAML (the route generator) reads plain `on` or `1:30` as a boolean or an
# integer, which the generator refuses there. Which plain text PyYAML retypes
# comes from PyYAML itself, not from memory: _plain_scalar_table is PyYAML's
# SafeLoader verdict on each candidate, computed at run time. (Until #2905 it
# was also committed as pkg/pyyamlcompat/testdata/pyyaml_plain_scalars.json
# for the Go decoder that copied PyYAML's typing; both are deleted.)


def _plain_scalar_candidates() -> list[str]:
    import itertools

    def casings(word):
        return {"".join(p) for p in itertools.product(*[(c.lower(), c.upper()) for c in word])}

    words = set()
    for w in ("yes", "no", "true", "false", "on", "off", "y", "n", "null", "nan", "inf"):
        words |= casings(w)
    bodies = ("0", "7", "8", "017", "0123", "089", "0_7", "1_000", "1__0", "_1", "1_",
              "0b101", "0b2", "0b_1", "0B101", "0x1F", "0x_1f", "0xg", "0X1F", "0o17", "0O17",
              "1:30", "190:20:30", "1:60", "1:5:9", "0:30", "01:30", "1:3a", "1:30:", "1_0:30",
              "1:30.5", "1:30.", "0:30.5", "1.5", "1.", "1._5", "1_0.5", ".5", "._5", "1e3",
              "1.0e+3", "1.0e3", "1.e+3", ".5e-1", "1.0E+3", "1:30e+3", ".inf", ".Inf", ".INF",
              ".iNf", ".nan", ".NaN", ".NAN", ".nAn")
    numbers = {sign + b for sign in ("", "+", "-") for b in bodies}
    dates = {"2024-01-01", "2024-1-1", "2024-13-01", "99-01-01", "2002-12-14", "2024-01-01T",
             "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "2001-12-15T02:59:43.1Z",
             "2001-12-15 2:59:43.10", "2001-12-15T02:59:43", "2024-01-01 10:00"}
    other = {"<<", "=", "~", "!", "&", "*", "abc", "http://p.example:3128", "localhost,127.0.0.1",
             "/var/run/token", "1.2.3.4", "token-1:30", "a:b", "NULL_x", "yes!", "0x", "0b",
             "+", "-", ".", ":30"}
    return sorted(words | numbers | dates | other)


def _pyyaml_verdict(text: str) -> str:
    """The tag PyYAML's SafeLoader resolves the plain scalar `text` to, or
    "error" when it cannot even compose it."""
    try:
        node = yaml.compose("v: " + text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return "error"
    return node.value[0][1].tag.rsplit(":", 1)[-1]


def _plain_scalar_table() -> list[dict]:
    return [{"text": t, "pyyaml": _pyyaml_verdict(t)} for t in _plain_scalar_candidates()]


def test_plain_scalar_table_covers_every_implicit_resolver():
    """Each tag SafeLoader resolves plain scalars to appears in the table.

    `yaml` (`!`, `&`, `*`) cannot stand as a plain value — those rows compose
    as null or fail — so it is the one tag the table cannot show."""
    implicit = {tag.rsplit(":", 1)[-1] for resolvers in yaml.SafeLoader.yaml_implicit_resolvers.values()
                for tag, _ in resolvers}
    seen = {row["pyyaml"] for row in _plain_scalar_table()}
    assert implicit - {"yaml"} <= seen, f"no candidate resolves to {sorted(implicit - {'yaml'} - seen)}"
    assert {"!", "&", "*"} <= set(_plain_scalar_candidates())
    assert "yaml" not in seen


@pytest.mark.parametrize("key", ["bearer_token", "bearer_token_file", "proxy_url", "no_proxy"])
def test_generator_refuses_what_pyyaml_retypes(key):
    """The Python half of the contract: a candidate PyYAML does not read as a
    string is refused by the generator in each of the four string fields
    (null is unset; text PyYAML cannot construct fails the whole file).
    #3257: bearer_token_file is refused as a key, whatever its value
    (``receiver_file_reference_problem``), so for it the receiver-level
    verdict is what is asserted — still refused, by the stronger rule."""
    from _lib_validation import _http_config_problem, receiver_optional_problem

    def refused(hc: dict):
        if key == "bearer_token_file":
            return receiver_optional_problem(
                "webhook", {"type": "webhook", "url": "https://h.example/a", "http_config": hc})
        return _http_config_problem(hc)

    checked = 0
    for row in _plain_scalar_table():
        if row["pyyaml"] in ("str", "null", "error"):
            continue
        try:
            value = yaml.safe_load("v: " + row["text"])["v"]
        except (yaml.YAMLError, ValueError):
            continue
        checked += 1
        hc = {key: value}
        if key == "no_proxy":
            hc["proxy_url"] = "http://p.example:1"
        assert refused(hc), f"{key}: {row['text']!r} ({row['pyyaml']}) is not refused"
    assert checked > 50


# --- The http_config key set (#2984) ----------------------------------------
#
# definitions.httpConfig is additionalProperties: false; its `properties` is
# Alertmanager's http_config key set (tests/alertmanager-inhibit
# TestSchemaHTTPConfigKeys_MatchAlertmanager pins it by reflection). The route
# generator passes http_config through whole, so a key it handles but the
# schema lacks makes validate-config refuse a config the generator emits and
# Alertmanager loads — the #2984 bug (proxy_from_environment, oauth2, ...).

def _http_config_schema() -> dict:
    with open(_SCHEMA, encoding="utf-8") as f:
        return json.load(f)["definitions"]["httpConfig"]


_HTTP_CONFIG_KEYS = sorted(_http_config_schema()["properties"])


def _keys_read_from(module: str, function: str, var: str) -> set[str]:
    """String keys *function* in *module* reads from the mapping named *var*:
    ``var.get("k")``, ``var["k"]`` and ``"k" in var``."""
    tree = ast.parse(inspect.getsource(importlib.import_module(module)))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == function)

    def is_var(node):
        return isinstance(node, ast.Name) and node.id == var

    def const(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    keys = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and is_var(node.func.value) and node.args):
            keys.add(const(node.args[0]))
        elif isinstance(node, ast.Subscript) and is_var(node.value):
            keys.add(const(node.slice))
        elif (isinstance(node, ast.Compare) and len(node.ops) == 1
              and isinstance(node.ops[0], (ast.In, ast.NotIn)) and is_var(node.comparators[0])):
            keys.add(const(node.left))
    keys.discard(None)
    return keys


def test_http_config_keys_python_handles_are_schema_properties():
    """Every http_config key the Python side handles by name — the auth keys
    and the keys _http_config_problem / build_receiver_config read — is a
    schema property, so the schema never refuses a key the generator
    validates and emits."""
    from _lib_constants import HTTP_CONFIG_AUTH_FIELDS

    validated = _keys_read_from("_lib_validation", "_http_config_problem", "value")
    # #2906: build_receiver_config no longer rewrites http_config (it used to
    # turn a quoted proxy_from_environment word into a boolean); the key the
    # Python side reads by name besides _http_config_problem is the boolean
    # check that refuses the whole tree.
    emitted = _keys_read_from("_lib_validation", "receiver_bool_problems", "http_config")
    # The extractor must see the reads it is there to see, or an empty set
    # would pass the subset check below vacuously.
    assert {"proxy_url", "no_proxy", "proxy_from_environment", "proxy_connect_header"} <= validated
    assert "proxy_from_environment" in emitted
    handled = set(HTTP_CONFIG_AUTH_FIELDS) | validated | emitted
    missing = sorted(handled - set(_HTTP_CONFIG_KEYS))
    assert not missing, f"Python handles http_config keys the schema refuses: {missing}"


_NESTED_SAMPLES = {"#/definitions/httpOAuth2": {"client_id": "c", "token_url": "https://t.example/x"}}


def _sample(prop: dict):
    """A value of the property's own type that sets nothing another key needs."""
    if prop.get("$ref") == "#/definitions/yamlBool":
        return False
    if "anyOf" in prop:  # a nested definition or null: its smallest valid mapping
        ref = next(b["$ref"] for b in prop["anyOf"] if "$ref" in b)
        return _NESTED_SAMPLES.get(ref, {})
    types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
    return {"object": {}, "string": "", "boolean": False}[next(t for t in types if t != "null")]


@pytest.mark.parametrize("key", _HTTP_CONFIG_KEYS)
def test_http_config_schema_key_passes_schema_and_generator(key):
    """Each schema key, alone, is accepted by the schema and the generator,
    and the generator emits it unchanged — over every key, the set the
    generator passes through is the schema's whole key set."""
    value = _sample(_http_config_schema()["properties"][key])
    receiver = {"type": "webhook", "url": "https://h.example/a", "http_config": {key: value}}
    doc = {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}
    assert not [e.message for e in _VALIDATOR.iter_errors(doc)]
    cfg, warnings = build_receiver_config(json.loads(json.dumps(receiver)), "t1")
    assert cfg is not None, warnings
    assert cfg["webhook_configs"][0]["http_config"] == {key: value}


def test_http_config_schema_refuses_an_unknown_key():
    """additionalProperties: false stays: Alertmanager refuses an unknown
    http_config key ("field ... not found") and the whole config with it."""
    assert _http_config_schema().get("additionalProperties") is False
    receiver = {"type": "webhook", "url": "https://h.example/a",
                "http_config": {"proxy_urls": "http://p.example:1"}}
    doc = {"tenants": {"t1": {"_routing": {"receiver": receiver}}}}
    assert [e.message for e in _VALIDATOR.iter_errors(doc)]


def test_yaml_true_is_the_true_half_of_yaml_bool():
    """definitions.yamlTrue (the proxy rules' "proxy_from_environment: true")
    is exactly the yamlBool value that means true. #2906: yamlBool is
    boolean or null only — no YAML 1.1 word, quoted or not, as the route
    generator reads conf.d through yaml.v3 and judges by type — so the true
    half is the boolean True alone."""
    with open(_SCHEMA, encoding="utf-8") as f:
        defs = json.load(f)["definitions"]
    assert defs["yamlBool"]["type"] == ["boolean", "null"] and "anyOf" not in defs["yamlBool"]
    assert defs["yamlTrue"]["enum"] == [True]
