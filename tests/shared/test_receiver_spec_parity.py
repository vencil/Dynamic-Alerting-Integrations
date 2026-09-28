"""Python RECEIVER_TYPES ↔ tenant-config.schema.json receiver parity (#2137).

The receiver presence contract — which fields are required, and which groups
need EXACTLY ONE field set — is declared in:

  - JSON Schema: docs/schemas/tenant-config.schema.json (`required` + a `oneOf`
    whose branches each require one non-empty field)
  - Python:      scripts/tools/_lib_constants.py RECEIVER_TYPES
                 (`required` + `exactly_one_of`)
  - Go guard:    components/threshold-exporter/app/internal/guard/routing.go
                 receiverTypeSpecs (Required + ExactlyOneOf)

The schema is the hub. This test pins the Python copy to it; the Go copy is
pinned to it by TestReceiverTypeSpecs_MatchSchema in the same package as
receiverTypeSpecs. Each side reads the schema as JSON and its own copy as a
value, so no copy is parsed out of another language's source text.

Emptiness is part of the contract. Python and Go treat "" and null as unset,
as Alertmanager does (its config is a Go struct; both decode to the zero
value — a YAML `service_key:` with no value is null), so the schema reader
demands a single pinned type plus `minLength >= 1` (`minItems` for arrays) on
every required field and every exactly-one branch — otherwise an empty or
null key would count as "given" in the schema only, and
{service_key: "" | null, routing_key: "r"} would match both branches.

Shared case table (also read by the Go guard's TestReceiverPresenceCases):
components/threshold-exporter/app/internal/guard/testdata/receiver_presence_cases.json
Its `am` column is Alertmanager's own verdict, asserted by
tests/alertmanager-inhibit/receiver_cases_test.go with config.Load (#2180).

Value shapes of required fields (#2180) follow the schema's type: a string
field must be a string and match the schema `pattern` when there is one (URL
and smarthost formats, written once as schema definitions and read by Python
at run time); email `to` given as a list needs non-empty string items.

It also pins the ACCEPTED field set (schema `properties` vs Python
required + optional + metadata): the schema is `additionalProperties: false`,
so a field Python forwards but the schema lacks is rejected by schema
validation even though the pipeline would use it.

The schema reader fails on any presence-shaping keyword or branch shape it
does not model, so an unmodelled constraint cannot read as "no constraint".
"""
from __future__ import annotations

import json
import os

import jsonschema
import pytest

from _lib_constants import RECEIVER_TYPES
from _grar_merge import build_receiver_config

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")
_CASES = os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "internal",
                      "guard", "testdata", "receiver_presence_cases.json")

_UNMODELLED = ("anyOf", "allOf", "not", "if", "dependencies", "dependentRequired")


def _rejects_empty(prop: dict | None) -> bool:
    """True when the property rejects "" and null, which Python/Go read as unset.

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
            # Python/Go truthiness: an empty value never satisfies presence.
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


def test_sources_are_read_not_empty():
    assert len(SCHEMA) >= 6
    assert set(SCHEMA) == set(PYTHON)
    assert len(CASES) >= 7


@pytest.mark.parametrize("rtype", sorted(SCHEMA))
@pytest.mark.parametrize("aspect", ["required", "exactly_one_of", "fields"])
def test_python_matches_schema(rtype, aspect):
    assert PYTHON[rtype][aspect] == SCHEMA[rtype][aspect], (
        f"{rtype}.{aspect}: Python RECEIVER_TYPES {PYTHON[rtype][aspect]} "
        f"!= tenant-config.schema.json {SCHEMA[rtype][aspect]}")


@pytest.mark.parametrize("rtype", sorted(SCHEMA))
def test_schema_rejects_empty_like_python_and_go(rtype):
    """Python and Go read "" and null as unset; the schema must too."""
    accepts_empty = [f for f, ok in {**SCHEMA[rtype]["required_rejects_empty"],
                                     **SCHEMA[rtype]["group_rejects_empty"]}.items() if not ok]
    assert not accepts_empty, (
        f"{rtype}: schema lets {accepts_empty} be empty, but Python and the Go guard "
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


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_case_table_schema_and_python_agree(case):
    """Same rows as the Go guard's TestReceiverPresenceCases."""
    doc = {"tenants": {"t1": {"_routing": {"receiver": case["receiver"]}}}}
    schema_errors = [e.message for e in _VALIDATOR.iter_errors(doc)]
    cfg, warnings = build_receiver_config(dict(case["receiver"]), "t1")
    assert (not schema_errors) == case["valid"], f"schema: {schema_errors}"
    assert (cfg is not None) == case["valid"], f"python: {warnings}"
