"""#2164 → ADR-036 step 4 (#2906): a value in a string-typed field that yaml.v3
reads as something other than a string (`_lib_io.find_misread_scalars`).

The verdict is yaml.v3's — the tag `da-guard parse-tree` gives the scalar —
because every reader of conf.d reads it that way: `channel: 1e3` is a float,
`channel: yes` the string "yes" (no finding). The two things the rule must NOT
carry are pinned here too:

* no word list — what a scalar "is" comes from parse-tree's tag;
* no field list — which fields are strings comes from the schema, so the same
  nodes flip verdict when only the schema changes.

parse-tree does not say whether a scalar was quoted, nor its line: a finding
names the value's path.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

import _lib_parse_tree as lpt  # noqa: E402
from _lib_io import find_misread_scalars  # noqa: E402
from _lib_io import misread_scalar_nodes  # noqa: E402

_TENANT = "tenant-config.schema.json"
_PLATFORM = "platform-defaults.schema.json"


@pytest.fixture(scope="module")
def schemas() -> dict:
    out = {}
    for name in (_TENANT, _PLATFORM):
        with open(os.path.join(_REPO, "docs", "schemas", name), encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


@pytest.fixture(scope="module")
def nodes_of(tmp_path_factory):
    """*text* as the file `t.yaml` reads through `da-guard parse-tree`
    (file mode): its nodes."""
    root = tmp_path_factory.mktemp("conf.d")

    def read(text: str):
        return lpt.load_file(root, "t.yaml", text.encode("utf-8"), None, 60).node("t.yaml")
    return read


@pytest.fixture
def hits(schemas, nodes_of):
    def run(text: str, name: str = _TENANT):
        return find_misread_scalars(nodes_of(text), schemas[name], schemas, name)
    return run


def _slack(channel: str) -> str:
    return ("tenants:\n  t1:\n    _routing:\n      receiver:\n"
            "        type: slack\n"
            "        api_url: \"https://hooks.slack.com/services/x\"\n"
            f"        channel: {channel}\n")


# token -> the tag yaml.v3 gives it when written plain
_MISREAD = {"123": "int", "0x1F": "int", "1.0": "float", "1e3": "float",
            "true": "bool", "~": "null", "null": "null", "2024-01-02": "timestamp"}


@pytest.mark.parametrize("token", sorted(_MISREAD))
def test_a_non_string_in_a_string_field_is_reported(token, hits):
    (hit,) = hits(_slack(token))
    assert (hit.path, hit.text, hit.resolved) == (
        "/tenants/t1/_routing/receiver/channel", token, _MISREAD[token])
    if _MISREAD[token] != "null":
        assert f'quote it, without a tag: channel: "{token}"' in hit.message()
        assert "yaml.v3" in hit.message()


@pytest.mark.parametrize("token", sorted(_MISREAD) + ["yes", "no", "on", "off", "y", "n"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_the_quoted_control_passes(token, quote, hits):
    assert hits(_slack(f"{quote}{token}{quote}")) == []


@pytest.mark.parametrize("token", ["yes", "no", "on", "off", "Yes", "OFF", "y", "n"])
def test_yaml11_words_are_strings_to_yaml_v3_so_nothing_is_reported(token, hits):
    """#2906: `channel: yes` was PyYAML's True (#2164's original finding). To
    yaml.v3 — every reader of conf.d now — it is the string "yes": no
    finding, the same as `"yes"`."""
    assert hits(_slack(token)) == []


@pytest.mark.parametrize("token", ["true", "false", "yes", "no"])
def test_a_boolean_field_is_not_a_string_field(token, hits):
    text = ("tenants:\n  t1:\n    _routing:\n      receiver:\n"
            "        type: webhook\n        url: \"https://a.example.com/h\"\n"
            f"        send_resolved: {token}\n")
    assert hits(text) == []


def test_null_is_not_reported_where_the_schema_allows_it(hits):
    """`group_wait: ~` is an explicit opt-out (#1339: type [string, null])."""
    text = "tenants:\n  t1:\n    _routing:\n      group_wait: ~\n"
    assert hits(text) == []


def test_enum_and_sequence_items_are_string_fields(hits):
    text = ("tenants:\n  t1:\n    _severity_dedup: no\n"
            "    _routing:\n      receiver:\n        type: webhook\n"
            "        url: \"https://a.example.com/h\"\n"
            "      group_by: [alertname, true]\n")
    got = [(h.path, h.resolved) for h in hits(text)]
    # `_severity_dedup` takes a boolean too (definitions.severityDedup), so it
    # is no string-only field.
    assert got == [("/tenants/t1/_routing/group_by/1", "bool")]
    assert 'quote it, without a tag: - "true"' in hits(text)[0].message()


def test_an_explicit_tag_is_the_tag_yaml_v3_reads(hits, nodes_of):
    """`channel: !!int 5` is the int 5 to yaml.v3: reported as an int, the
    remedy drops the tag. A tag yaml.v3 cannot apply (`!!bool yes`) makes the
    whole file unreadable to parse-tree — never a value here."""
    (hit,) = hits(_slack("!!int 5"))
    assert (hit.text, hit.resolved) == ("5", "int")
    assert "without a tag" in hit.message()
    with pytest.raises(lpt.FileUnreadable) as e:
        nodes_of(_slack("!!bool yes"))
    assert e.value.kind == "decode_error"


def test_defaults_routing_blocks_follow_the_cross_file_ref(hits):
    """`_routing_defaults` / `_routing_enforced` in `_defaults.yaml` are typed
    by `$ref` into tenant-config.schema.json (#2164)."""
    text = ("_routing_defaults:\n  receiver:\n    type: pagerduty\n"
            "    service_key: 123\n"
            "_routing_enforced:\n  enabled: true\n  receiver:\n"
            "    type: webhook\n    url: \"https://noc.example.com/h\"\n"
            "  match: [1.5]\n")
    got = {h.path for h in hits(text, _PLATFORM)}
    assert got == {"/_routing_defaults/receiver/service_key",
                   "/_routing_enforced/match/0"}


def test_which_fields_are_strings_comes_from_the_schema_alone():
    """Same nodes, three schemas: the verdict follows the schema's `type`."""
    root = {"a": {"text": "5", "tag": "!!int", "value": "5"}}
    as_string = {"type": "object", "properties": {"a": {"type": "string"}}}
    as_int = {"type": "object", "properties": {"a": {"type": "integer"}}}
    mixed = {"type": "object", "properties": {"a": {"type": ["string", "integer"]}}}
    assert [h.path for h in find_misread_scalars(root, as_string)] == ["/a"]
    assert find_misread_scalars(root, as_int) == []
    # A mixed scalar type accepts the non-string reading; left alone (#1017
    # tightened the schema to string instead of having a lint guess).
    assert find_misread_scalars(root, mixed) == []


def test_an_unloaded_cross_file_ref_is_an_error_not_a_silent_skip():
    schema = {"type": "object",
              "properties": {"a": {"$ref": "other.schema.json#/definitions/x"}}}
    root = {"a": {"text": "5", "tag": "!!int", "value": "5"}}
    with pytest.raises(KeyError):
        find_misread_scalars(root, schema, {}, "self.json")


def test_an_alias_is_judged_at_every_place_it_is_used(hits, schemas, nodes_of):
    """parse-tree expands an alias where it is used, so each use is judged at
    its own schema position (routing.group_by, routingOverride.group_by), and
    `misread_scalar_nodes` hands back the nodes of the same walk (#2695)."""
    text = ("tenants:\n  t1:\n    _routing:\n      group_by: &g [true, 1]\n"
            "      overrides:\n"
            "        - alertname: A0\n          group_by: *g\n"
            "          receiver: {type: webhook, url: \"https://a.example.com/h\"}\n")
    got = hits(text)
    assert [h.path for h in got] == [
        "/tenants/t1/_routing/group_by/0", "/tenants/t1/_routing/group_by/1",
        "/tenants/t1/_routing/overrides/0/group_by/0",
        "/tenants/t1/_routing/overrides/0/group_by/1"]
    root = nodes_of(text)
    nodes = misread_scalar_nodes(root, schemas[_TENANT], schemas, _TENANT)
    assert [n["text"] for n in nodes] == ["true", "1", "true", "1"]
    assert [h.text for h in find_misread_scalars(root, schemas[_TENANT], schemas, _TENANT)] \
        == [n["text"] for n in nodes]
