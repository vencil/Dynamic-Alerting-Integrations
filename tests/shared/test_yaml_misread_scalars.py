"""#2164: an UNQUOTED scalar in a string-typed field that PyYAML reads as
something other than a string (`_lib_io.find_misread_scalars`).

The two things the rule must NOT carry are pinned here too:

* no word list — what a scalar "is" comes from PyYAML's resolver, so `y` / `n`
  (strings to PyYAML, unlike YAML 1.1's spec) are NOT reported;
* no field list — which fields are strings comes from the schema, so the same
  bytes flip verdict when only the schema changes.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from _lib_io import compose_all_nodes, find_misread_scalars  # noqa: E402
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


def _hits(text: str, schemas: dict, name: str = _TENANT):
    out = []
    for root in compose_all_nodes(io.StringIO(text)):
        out.extend(find_misread_scalars(root, schemas[name], schemas, name))
    return out


def _slack(channel: str) -> str:
    return ("tenants:\n  t1:\n    _routing:\n      receiver:\n"
            "        type: slack\n"
            "        api_url: \"https://hooks.slack.com/services/x\"\n"
            f"        channel: {channel}\n")


# token -> the tag PyYAML's resolver gives it when written plain
_MISREAD = {"yes": "bool", "no": "bool", "on": "bool", "off": "bool",
            "~": "null", "null": "null", "123": "int", "1.0": "float"}


@pytest.mark.parametrize("token", sorted(_MISREAD))
def test_plain_non_string_in_a_string_field_is_reported(token, schemas):
    hits = _hits(_slack(token), schemas)
    assert len(hits) == 1, hits
    hit = hits[0]
    assert (hit.line, hit.path, hit.text, hit.resolved) == (
        7, "/tenants/t1/_routing/receiver/channel", token, _MISREAD[token])
    if _MISREAD[token] != "null":
        assert f'quote it: channel: "{token}"' in hit.message()


@pytest.mark.parametrize("token", sorted(_MISREAD) + ["y", "n"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_the_quoted_control_passes(token, quote, schemas):
    assert _hits(_slack(f"{quote}{token}{quote}"), schemas) == []


@pytest.mark.parametrize("token", ["y", "n", "Y", "N"])
def test_y_and_n_are_strings_to_pyyaml_so_nothing_is_reported(token, schemas):
    """The resolver decides, not a YAML 1.1 word list: PyYAML's bool regexp
    has no `y`/`n`, so a list-based rule would report these and this one does
    not."""
    assert _hits(_slack(token), schemas) == []


@pytest.mark.parametrize("token", ["yes", "no", "on", "off", "y", "n"])
def test_a_boolean_field_is_not_a_string_field(token, schemas):
    text = ("tenants:\n  t1:\n    _routing:\n      receiver:\n"
            "        type: webhook\n        url: \"https://a.example.com/h\"\n"
            f"        send_resolved: {token}\n")
    assert _hits(text, schemas) == []


def test_null_is_not_reported_where_the_schema_allows_it(schemas):
    """`group_wait: ~` is an explicit opt-out (#1339: type [string, null])."""
    text = "tenants:\n  t1:\n    _routing:\n      group_wait: ~\n"
    assert _hits(text, schemas) == []


def test_enum_and_sequence_items_are_string_fields(schemas):
    text = ("tenants:\n  t1:\n    _severity_dedup: no\n"
            "    _routing:\n      receiver:\n        type: webhook\n"
            "        url: \"https://a.example.com/h\"\n"
            "      group_by: [alertname, on]\n")
    got = {(h.path, h.resolved) for h in _hits(text, schemas)}
    assert got == {("/tenants/t1/_severity_dedup", "bool"),
                   ("/tenants/t1/_routing/group_by/1", "bool")}
    seq = [h for h in _hits(text, schemas) if h.path.endswith("/1")][0]
    assert 'quote it: - "on"' in seq.message()



def test_explicit_tag_names_the_tag_not_quoting_alone(schemas):
    """#2509 盲審第 5 輪：`channel: !!bool yes` 只加引號仍是布林，建議要先拿掉 tag。"""
    text = ("tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
            "        channel: !!bool yes\n")
    (hit,) = _hits(text, schemas)
    assert hit.explicit, hit
    msg = hit.message()
    assert "explicit `!!bool yes`" in msg and "unquoted" not in msg, msg
    assert 'remove the tag and quote it: channel: "yes"' in msg, msg


@pytest.mark.parametrize("written, shown", [
    ("!foo yes", "!foo yes"),
    ("!foo:bar yes", "!foo:bar yes"),
    ("!<tag:example.com,2000:x> yes", "!<tag:example.com,2000:x> yes"),
    ("!<tag:yaml.org,2002:int> 5", "!!int 5"),
    ("!!python/str yes", "!!python/str yes"),
])
def test_explicit_tag_is_named_as_written(schemas, written, shown):
    """盲審續輪 1／2：tag 照作者寫法印出（本地 tag 不加 `!!`，含 `:` 的 tag 不被截斷）。"""
    text = ("tenants:\n  t1:\n    _routing:\n      receiver:\n        type: slack\n"
            f"        channel: {written}\n")
    (hit,) = _hits(text, schemas)
    assert f"explicit `{shown}`" in hit.message(), hit.message()

def test_defaults_routing_blocks_follow_the_cross_file_ref(schemas):
    """`_routing_defaults` / `_routing_enforced` in `_defaults.yaml` are typed
    by `$ref` into tenant-config.schema.json (#2164)."""
    text = ("_routing_defaults:\n  receiver:\n    type: pagerduty\n"
            "    service_key: yes\n"
            "_routing_enforced:\n  enabled: yes\n  receiver:\n"
            "    type: webhook\n    url: \"https://noc.example.com/h\"\n"
            "  match: [on]\n")
    got = {(h.line, h.path) for h in _hits(text, schemas, _PLATFORM)}
    assert got == {(4, "/_routing_defaults/receiver/service_key"),
                   (10, "/_routing_enforced/match/0")}


def test_which_fields_are_strings_comes_from_the_schema_alone():
    """Same bytes, two schemas: the verdict follows the schema's `type`."""
    text = "a: yes\n"
    as_string = {"type": "object", "properties": {"a": {"type": "string"}}}
    as_bool = {"type": "object", "properties": {"a": {"type": "boolean"}}}
    mixed = {"type": "object", "properties": {"a": {"type": ["string", "boolean"]}}}
    root = next(compose_all_nodes(io.StringIO(text)))
    assert [h.path for h in find_misread_scalars(root, as_string)] == ["/a"]
    assert find_misread_scalars(root, as_bool) == []
    # A mixed scalar type accepts the non-string reading; left alone (#1017
    # tightened the schema to string instead of having a lint guess).
    assert find_misread_scalars(root, mixed) == []


def test_an_unloaded_cross_file_ref_is_an_error_not_a_silent_skip():
    schema = {"type": "object",
              "properties": {"a": {"$ref": "other.schema.json#/definitions/x"}}}
    root = next(compose_all_nodes(io.StringIO("a: yes\n")))
    with pytest.raises(KeyError):
        find_misread_scalars(root, schema, {}, "self.json")


def test_an_alias_bomb_is_walked_once_per_schema_position(schemas):
    text = ("tenants:\n  t1:\n    _routing:\n      group_by: &g [on, off]\n"
            "      overrides:\n" + "".join(
                f"        - alertname: A{i}\n          group_by: *g\n"
                "          receiver: {type: webhook, url: \"https://a.example.com/h\"}\n"
                for i in range(3)))
    hits = _hits(text, schemas)
    # Each hit points at the line where the list is WRITTEN. It is judged once
    # per schema position (routing.group_by, routingOverride.group_by): the
    # 2nd and 3rd alias uses reach the same node under the same schema and
    # are not walked again.
    assert {h.line for h in hits} == {4}
    assert sorted(h.path for h in hits) == [
        "/tenants/t1/_routing/group_by/0", "/tenants/t1/_routing/group_by/1",
        "/tenants/t1/_routing/overrides/0/group_by/0",
        "/tenants/t1/_routing/overrides/0/group_by/1"]


def test_the_nodes_are_the_reported_scalars_each_once(schemas):
    """#2695: `misread_scalar_nodes` is the same walk as
    `find_misread_scalars`, handing back the scalar NODES — each once, an
    aliased one reported at two schema positions included (the caller
    re-tags it, and one node re-tagged covers every use)."""
    text = ("tenants:\n  t1:\n    _routing:\n      group_by: &g [on, off]\n"
            "      overrides:\n"
            "        - alertname: A0\n          group_by: *g\n"
            "          receiver: {type: webhook, url: \"https://a.example.com/h\"}\n")
    root = next(compose_all_nodes(io.StringIO(text)))
    hits = find_misread_scalars(root, schemas[_TENANT], schemas, _TENANT)
    nodes = misread_scalar_nodes(root, schemas[_TENANT], schemas, _TENANT)
    assert len(hits) == 4
    assert [(n.start_mark.line + 1, n.start_mark.column + 1) for n in nodes] \
        == sorted({(h.line, h.column) for h in hits})
    assert [n.value for n in nodes] == ["on", "off"]
