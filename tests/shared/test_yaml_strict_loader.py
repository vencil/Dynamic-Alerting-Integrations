"""The shared strict YAML reader (`_lib_io`, #2123): a key written twice in
one mapping is an error, with yaml.v3's notion of "the same key".

YAML forbids a repeated key; PyYAML keeps the last value and says nothing;
the exporter's `gopkg.in/yaml.v3` rejects the file. So every Python tool
that judges a conf.d on `safe_load` passed a file the exporter never serves.

What is pinned here, and why each is pinned:

* **Key identity = yaml.v3's** (node kind + raw text; keys brought in by a
  `<<` merge are not compared). `_IDENTITY` is the matrix measured against
  yaml.v3 v3.0.1 (`Unmarshal` into `map[string]interface{}`) when this was
  written; the Go probe is in the #2123 PR description. ⛔ It is NOT a list
  of PyYAML-vs-yaml.v3 differences to extend (#2033 R′ rejected that shape):
  it pins the ONE comparison rule, with a row per branch of that rule.
* **The parser underneath does not change** — pure by default, libyaml on
  `fast=True`. Tools have made the pure parser's limits part of their
  contract (`test_yaml_loader_is_libyaml.py`), so strictness must not bring a
  different parser in with it.
* **Safety by behaviour**: these entry points drive a loader by hand, which
  is outside bandit B506's predicate (`test_sast.py` §4). A real
  `!!python/object/apply` payload must be refused. ⛔ Deleting that test
  leaves them unguarded.
* **Nothing the pre-#2123 private loaders refused is accepted now**
  (`check_admin_config_schema`, `check_image_pin_capability` compared
  CONSTRUCTED keys — stricter than yaml.v3 — and keep doing so).
"""
from __future__ import annotations

import io

import pytest
import yaml

import _lib_io
from _lib_io import (
    DuplicateKeyError,
    YamlFileError,
    find_duplicate_key,
    load_yaml_file_strict,
    strict_safe_load,
    strict_safe_load_all,
)

_LIBYAML = bool(getattr(yaml, "__with_libyaml__", False))
_PARSERS = [False] + ([True] if _LIBYAML else [])

# (name, document, rejected-by-yaml.v3)
_IDENTITY = [
    ("same plain key", "c:\n  a: 1\n  a: 2\n", True),
    ("quoted vs plain, same text", 'c:\n  "1": a\n  1: b\n', True),
    ("explicit !!str vs plain, same text", "c:\n  !!str a: 1\n  a: 2\n", True),
    ("two literal merge keys", "a: &x {p: 1}\nb: &y {q: 1}\nc:\n  <<: *x\n  <<: *y\n", True),
    ("repeat inside an inline merge value", "c:\n  <<: {p: 1, p: 2}\n", True),
    ("repeat inside a list item", "c:\n  - {p: 1, p: 2}\n", True),
    ("top-level tenants twice", "tenants: {}\ntenants: {}\n", True),
    ("different text, equal value (true/True)", "c:\n  true: a\n  True: b\n", False),
    ("different text, both null (~/null)", "c:\n  ~: 1\n  null: 2\n", False),
    ("explicit key overrides a merged one", "a: &x {p: 1}\nc:\n  <<: *x\n  p: 2\n", False),
]


@pytest.mark.parametrize("fast", _PARSERS, ids=lambda f: "libyaml" if f else "pure")
@pytest.mark.parametrize("name,doc,rejected", _IDENTITY, ids=[r[0] for r in _IDENTITY])
def test_key_identity_is_yaml_v3s(name, doc, rejected, fast):
    if rejected:
        with pytest.raises(DuplicateKeyError) as exc:
            strict_safe_load(doc, fast=fast)
        assert isinstance(exc.value, yaml.YAMLError)  # every existing handler sees it
        assert "found duplicate key" in str(exc.value)
    else:
        assert strict_safe_load(doc, fast=fast) == yaml.safe_load(doc)


def test_the_error_names_the_key_both_lines_and_the_file(tmp_path):
    path = tmp_path / "tenant-a.yaml"
    path.write_text("tenants:\n  tenant-a:\n    cpu: 1\n    mem: 2\n    cpu: 3\n",
                    encoding="utf-8")
    with pytest.raises(YamlFileError) as exc:
        load_yaml_file_strict(str(path))
    cause = exc.value.cause
    assert isinstance(cause, DuplicateKeyError)
    assert cause.key == "cpu" and cause.first_line == 3
    assert cause.problem_mark.line + 1 == 5
    text = str(exc.value)
    assert str(path) in text and "line 5" in text and "'cpu'" in text


def test_load_yaml_file_strict_keeps_load_yaml_files_contract(tmp_path):
    """Same default for missing / empty, same value for a clean file."""
    assert load_yaml_file_strict(None, default={}) == {}
    assert load_yaml_file_strict(str(tmp_path / "missing.yaml"), default=[]) == []
    empty = tmp_path / "e.yaml"
    empty.write_text("# nothing\n", encoding="utf-8")
    assert load_yaml_file_strict(str(empty), default={}) == {}
    clean = tmp_path / "c.yaml"
    clean.write_text("a: 1\nb: [x, y]\n", encoding="utf-8")
    assert load_yaml_file_strict(str(clean)) == _lib_io.load_yaml_file(str(clean))
    bad_utf8 = tmp_path / "b.yaml"
    bad_utf8.write_bytes(b"a: \xff\n")
    with pytest.raises(YamlFileError) as exc:
        load_yaml_file_strict(str(bad_utf8))
    assert isinstance(exc.value.cause, UnicodeDecodeError)


@pytest.mark.skipif(not _LIBYAML, reason="no libyaml on this machine")
def test_the_parser_underneath_is_unchanged():
    """A trailing tab: libyaml accepts it, the pure parser rejects it. The
    strict default must reject (pure, like `load_yaml_file`); `fast=True`
    must accept (libyaml, like `_lib_io.safe_load`)."""
    trailing_tab = "defaults:\n  cpu_usage: 80\t\n"
    with pytest.raises(yaml.scanner.ScannerError):
        strict_safe_load(trailing_tab)
    assert strict_safe_load(trailing_tab, fast=True) == {"defaults": {"cpu_usage": 80}}


@pytest.mark.parametrize("fast", _PARSERS, ids=lambda f: "libyaml" if f else "pure")
def test_the_loader_cannot_construct_python_objects(fast):
    """Behavioural safety pin (see module docstring) — with a must-still-work
    control, so a loader that refuses everything cannot pass."""
    payload = '!!python/object/apply:os.system ["echo strict-loader-pwned"]\n'
    with pytest.raises(yaml.constructor.ConstructorError):
        strict_safe_load(payload, fast=fast)
    with pytest.raises(yaml.constructor.ConstructorError):
        list(strict_safe_load_all(payload, fast=fast))
    assert strict_safe_load("a: [1, 2]\n", fast=fast) == {"a": [1, 2]}


def test_a_caller_supplied_loader_must_be_a_strict_one():
    with pytest.raises(TypeError):
        strict_safe_load("a: 1\n", loader=yaml.UnsafeLoader)


def test_load_all_is_lazy_like_safe_load_all():
    """A caller taking only the first document never parses the second —
    `describe_tenant` relies on it (a broken later document must not drop
    the first, as with yaml.v3's single-document Unmarshal)."""
    stream = io.StringIO("tenants: {tenant-a: {}}\n---\na: 1\na: 2\n")
    assert next(strict_safe_load_all(stream)) == {"tenants": {"tenant-a": {}}}
    with pytest.raises(DuplicateKeyError):
        list(strict_safe_load_all("a: 1\n---\nb: 1\nb: 2\n"))


def test_single_document_load_still_refuses_a_second_document():
    with pytest.raises(yaml.YAMLError):
        strict_safe_load("a: 1\n---\nb: 2\n")


def test_the_walk_visits_each_node_once_under_alias_fan_out():
    """Billion-laughs shaped input: 10**6 paths, a handful of nodes."""
    levels = ["l0: &l0 {a: x, b: x}"] + [
        f"l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(1, 7)]
    root = yaml.compose("\n".join(levels) + "\n")
    calls = []
    real = _lib_io.duplicate_in_mapping
    try:
        _lib_io.duplicate_in_mapping = lambda n: calls.append(id(n)) or real(n)
        assert find_duplicate_key(root) is None
    finally:
        _lib_io.duplicate_in_mapping = real
    assert len(calls) == len(set(calls)) == 2  # the root mapping + l0


# ── The five hand-written checks folded in: nothing refused before passes ──

_LEGACY_VALUE_IDENTITY = {
    "true/True": "c:\n  true: a\n  True: b\n",
    "1/01": "c:\n  1: a\n  01: b\n",
}


@pytest.mark.parametrize("doc", sorted(_LEGACY_VALUE_IDENTITY.values()),
                         ids=sorted(_LEGACY_VALUE_IDENTITY))
def test_admin_config_and_image_pin_still_refuse_equal_constructed_keys(doc):
    import check_admin_config_schema as adm
    import check_image_pin_capability as pin
    with pytest.raises(DuplicateKeyError):
        adm._load_all_strict(doc)
    with pytest.raises(yaml.YAMLError):
        pin.load_all_strict(doc)


def test_admin_config_still_refuses_an_override_of_a_merged_key():
    import check_admin_config_schema as adm
    with pytest.raises(DuplicateKeyError):
        adm._load_all_strict("a: &x {p: 1}\nc:\n  <<: *x\n  p: 2\n")


def test_image_pin_still_refuses_a_merge_key():
    import check_image_pin_capability as pin
    with pytest.raises(yaml.YAMLError):
        pin.load_all_strict("a: &x {p: 1}\nc:\n  <<: *x\n")


@pytest.mark.parametrize("name,doc,rejected",
                         [r for r in _IDENTITY if r[2]], ids=[r[0] for r in _IDENTITY if r[2]])
def test_every_folded_in_check_refuses_what_yaml_v3_refuses(name, doc, rejected):
    """The raw-text half now reaches the lints that compared values only
    (`"1"` / `1` used to pass both), and the three node walkers keep it."""
    import check_admin_config_schema as adm
    import check_image_pin_capability as pin
    import patch_config as pc
    with pytest.raises(yaml.YAMLError):
        adm._load_all_strict(doc)
    with pytest.raises(yaml.YAMLError):
        pin.load_all_strict(doc)
    with pytest.raises(pc.ConfigMapShapeError, match="twice"):
        pc.read_node(doc, "t.yaml")
    assert find_duplicate_key(yaml.compose(doc)) is not None  # deprecate_rule's walker
