"""Python RECEIVER_TYPES ↔ tenant-config.schema.json receiver parity (#2137).

The receiver presence contract — which fields are required, and which groups
need EXACTLY ONE field set — is declared three times:

  - JSON Schema: docs/schemas/tenant-config.schema.json (`required` + a `oneOf`
    whose branches each require one field)
  - Python:      scripts/tools/_lib_constants.py RECEIVER_TYPES
                 (`required` + `exactly_one_of`)
  - Go guard:    components/threshold-exporter/app/internal/guard/routing.go
                 receiverTypeSpecs (Required + ExactlyOneOf)

The schema is the hub. This test pins the Python copy to it; the Go copy is
pinned to it by TestReceiverTypeSpecs_MatchSchema in the same package as
receiverTypeSpecs. Each side reads the schema as JSON and its own copy as a
value, so no copy is parsed out of another language's source text.

It also pins the ACCEPTED field set (schema `properties` vs Python
required + optional + metadata): the schema is `additionalProperties: false`,
so a field Python forwards but the schema lacks is rejected by schema
validation even though the pipeline would use it.

The schema reader fails on any presence-shaping keyword it does not model, so
an unmodelled constraint cannot read as "no constraint".
"""
from __future__ import annotations

import json
import os

import pytest

from _lib_constants import RECEIVER_TYPES

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")

_UNMODELLED = ("anyOf", "allOf", "not", "if", "dependencies", "dependentRequired")


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
        groups = []
        if "oneOf" in d:
            group = []
            for i, br in enumerate(d["oneOf"]):
                assert set(br) == {"required"} and len(br["required"]) == 1, (
                    f"{name}.oneOf[{i}] is not {{'required': [<one field>]}}: {br}")
                group.append(br["required"][0])
            groups.append(sorted(group))
        out[rtype] = {
            "required": sorted(f for f in d.get("required", []) if f != "type"),
            "exactly_one_of": groups,
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


def test_sources_are_read_not_empty():
    assert len(SCHEMA) >= 6
    assert set(SCHEMA) == set(PYTHON)


@pytest.mark.parametrize("rtype", sorted(SCHEMA))
@pytest.mark.parametrize("aspect", ["required", "exactly_one_of", "fields"])
def test_python_matches_schema(rtype, aspect):
    assert PYTHON[rtype][aspect] == SCHEMA[rtype][aspect], (
        f"{rtype}.{aspect}: Python RECEIVER_TYPES {PYTHON[rtype][aspect]} "
        f"!= tenant-config.schema.json {SCHEMA[rtype][aspect]}")


def test_group_fields_are_listed_optional():
    """Group fields stay in `optional` so required+optional walkers see them."""
    for rtype, spec in RECEIVER_TYPES.items():
        for group in spec.get("exactly_one_of", []):
            missing = [f for f in group if f not in spec["optional"]]
            assert not missing, f"{rtype}: {missing} in exactly_one_of but not optional"
