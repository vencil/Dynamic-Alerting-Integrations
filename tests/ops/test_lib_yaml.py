"""test_lib_yaml.py — characterization golden for the extracted CRD YAML lib.

Wave 2 of da-tools ROI round 5 hoisted the byte-identical
``write_yaml_crd`` + ``_dict_to_yaml`` pair out of
``ops/operator_generate.py`` and ``ops/migrate_to_operator.py`` into the
shared ``_lib_yaml`` module. This test is the byte-identity lock: the
golden literals below were captured from the PRE-extraction
``operator_generate._dict_to_yaml`` output, so they pin the exact
(quirks-and-all) serialization the two tools shipped — not a tautological
compare against the function under test.

Coverage:
  * ``_dict_to_yaml`` golden bytes over representative CRD shapes: nested
    dicts, lists (scalar + dict items + nested), bool true/false,
    quote-needing vs plain strings, unicode, deep indent, empty
    dict/list, ints/floats.
  * ``write_yaml_crd`` fallback path (``yaml`` unavailable — forced via
    monkeypatch on the lib's module global, NOT a real global swap).
  * ``write_yaml_crd`` PyYAML path (default) incl. gitops sort_keys.
  * Cross-module identity: both ops tools resolve the shared lib objects
    (guards against a future local re-definition silently diverging).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml as _real_yaml

import _lib_yaml
from _lib_yaml import _dict_to_yaml, write_yaml_crd


# ── _dict_to_yaml golden bytes (captured pre-extraction) ────────────────
#
# Each tuple is (input_obj, expected_output). The expected strings are the
# literal bytes the fallback emitter produced BEFORE the refactor. Do NOT
# "fix" these to look like valid YAML — the minimal fallback emitter has
# known quirks (single-key nested dicts don't newline, etc.); preserving
# them verbatim is the whole point of this characterization lock.
GOLDEN_CASES = [
    pytest.param(
        {"apiVersion": "v1", "metadata": {"name": "db-a", "labels": {"tier": "gold"}}},
        "apiVersion: v1\nmetadata:\n  name: db-a\n  labels:     tier: gold",
        id="nested_dict",
    ),
    pytest.param(
        {"items": ["a", "b", "c"]},
        "items:\n  - a\n  - b\n  - c",
        id="list_scalar",
    ),
    pytest.param(
        {"rules": [{"alert": "HighCPU", "for": "5m"}, {"alert": "LowMem"}]},
        "rules:\n  -\n    alert: HighCPU\n    for: 5m\n  -     alert: LowMem",
        id="list_dict",
    ),
    pytest.param(
        {"enabled": True, "disabled": False},
        "enabled: true\ndisabled: false",
        id="bools",
    ),
    pytest.param(
        {
            "expr": 'up{job="x"} > 0',
            "note": "a,b",
            "colon": "k: v",
            "brace": "{x}",
            "brack": "[y]",
            "quote": "it's",
        },
        'expr: "up{job="x"} > 0"\nnote: "a,b"\ncolon: "k: v"\n'
        'brace: "{x}"\nbrack: "[y]"\nquote: "it\'s"',
        id="quote_needed",
    ),
    pytest.param(
        {"plain": "hello", "num_like": "abc123"},
        "plain: hello\nnum_like: abc123",
        id="no_quote",
    ),
    pytest.param(
        {"名稱": "資料庫", "msg": "中文告警"},
        "名稱: 資料庫\nmsg: 中文告警",
        id="unicode",
    ),
    pytest.param(
        {"a": {"b": {"c": {"d": "deep"}}}},
        "a:   b:     c:       d: deep",
        id="multi_indent",
    ),
    pytest.param({"meta": {}}, "meta: ", id="empty_dict"),
    pytest.param({"items": []}, "items: ", id="empty_list"),
    pytest.param({"count": 3, "ratio": 1.5}, "count: 3\nratio: 1.5", id="int_float"),
    pytest.param(
        {"groups": [{"name": "g1", "rules": [{"alert": "A"}]}, "raw-string-item"]},
        "groups:\n  -\n    name: g1\n    rules:       -         alert: A\n  - raw-string-item",
        id="mixed_list_nested",
    ),
]


@pytest.mark.parametrize("obj, expected", GOLDEN_CASES)
def test_dict_to_yaml_golden_bytes(obj, expected):
    """Fallback emitter output is byte-identical to the pre-extraction golden."""
    assert _dict_to_yaml(obj) == expected


# ── write_yaml_crd: fallback path (yaml unavailable) ────────────────────


def test_write_yaml_crd_fallback_when_yaml_none(tmp_path, monkeypatch):
    """With the lib's ``yaml`` forced to None, write_yaml_crd emits the
    minimal fallback serialization (== _dict_to_yaml)."""
    # Force the fallback branch via the lib's module global (monkeypatch =
    # auto-restored; NO real global swap of the yaml package).
    monkeypatch.setattr(_lib_yaml, "yaml", None)

    crd = {"apiVersion": "v1", "kind": "PrometheusRule", "spec": {"groups": ["g1"]}}
    out = tmp_path / "crd.yaml"
    write_yaml_crd(out, crd)

    written = out.read_text(encoding="utf-8")
    assert written == _dict_to_yaml(crd)
    # Explicit golden so the fallback wiring can't silently change shape.
    # NB the single-key-dict collapse ("spec:   groups:     - g1" on one
    # line, no newlines) is the emitter's known quirk — pinned verbatim.
    assert written == "apiVersion: v1\nkind: PrometheusRule\nspec:   groups:     - g1"


def test_write_yaml_crd_fallback_uses_lib_dict_to_yaml(tmp_path, monkeypatch):
    """The fallback delegates to the SAME _dict_to_yaml in the lib (not a
    stale copy) — patch it and observe the write pick up the change."""
    monkeypatch.setattr(_lib_yaml, "yaml", None)
    monkeypatch.setattr(_lib_yaml, "_dict_to_yaml", lambda crd: "SENTINEL")
    out = tmp_path / "crd.yaml"
    write_yaml_crd(out, {"any": "thing"})
    assert out.read_text(encoding="utf-8") == "SENTINEL"


# ── write_yaml_crd: PyYAML path (default) ───────────────────────────────


def test_write_yaml_crd_pyyaml_path(tmp_path):
    """When PyYAML is present (default), output == yaml.dump(sort_keys=False)."""
    crd = {"kind": "PrometheusRule", "apiVersion": "v1", "spec": {"z": 1, "a": 2}}
    out = tmp_path / "crd.yaml"
    write_yaml_crd(out, crd)
    expected = _real_yaml.dump(
        crd, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    assert out.read_text(encoding="utf-8") == expected


def test_write_yaml_crd_gitops_sorts_keys(tmp_path):
    """gitops=True flips sort_keys=True (deterministic GitOps output)."""
    crd = {"kind": "PrometheusRule", "apiVersion": "v1", "spec": {"z": 1, "a": 2}}
    out = tmp_path / "crd.yaml"
    write_yaml_crd(out, crd, gitops=True)
    expected = _real_yaml.dump(
        crd, default_flow_style=False, sort_keys=True, allow_unicode=True
    )
    assert out.read_text(encoding="utf-8") == expected


def test_write_yaml_crd_allows_unicode(tmp_path):
    """allow_unicode=True keeps CJK characters unescaped in the PyYAML path."""
    out = tmp_path / "crd.yaml"
    write_yaml_crd(out, {"名稱": "資料庫"})
    assert "資料庫" in out.read_text(encoding="utf-8")


# ── Cross-module identity: both ops tools use the shared lib ────────────


def test_ops_tools_resolve_shared_lib_objects():
    """operator_generate + migrate_to_operator import the lib's functions —
    no local shadow copy that could silently diverge again."""
    import operator_generate
    import migrate_to_operator

    assert operator_generate.write_yaml_crd is _lib_yaml.write_yaml_crd
    assert operator_generate._dict_to_yaml is _lib_yaml._dict_to_yaml
    assert migrate_to_operator.write_yaml_crd is _lib_yaml.write_yaml_crd
    assert migrate_to_operator._dict_to_yaml is _lib_yaml._dict_to_yaml
