"""`_lib_parse_tree`: the one reader of `da-guard parse-tree`'s document.

ADR-036 makes Go the only YAML parser of conf.d; a Python tool reads a file's
content from parse-tree's JSON. migration-audit had the only reader; #1516 P3
(config-diff's carrier table) needs the same one, so it moved here and both
use it. What the document must look like, and every way of refusing one, is
held through migration-audit's CLI in `test_migration_audit.py`
(`test_cli_refuses_a_da_guard_answer_it_cannot_read`,
`test_cli_refuses_a_parse_tree_document_of_the_wrong_shape`); this module
holds the library's own contract and that migration-audit goes through it
instead of keeping a copy.
"""
from __future__ import annotations

import dataclasses
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))

import _lib_parse_tree as lpt  # noqa: E402
import migration_audit as ma  # noqa: E402


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "conf.d"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_run_reads_each_file_as_tagged_nodes(tmp_path, da_guard_binary) -> None:
    root = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  cpu: 80\n_routing_defaults:\n  receiver:\n    type: email\n",
        "team/t1.yaml": "tenants:\n  t1:\n    cpu: \"90\"\n",
    })
    doc = lpt.run(root, da_guard_binary, 60)
    assert doc["schema"] == lpt.SCHEMA
    assert set(doc["files"]) == {"_defaults.yaml", "team/t1.yaml"}
    d = doc["files"]["_defaults.yaml"]
    assert set(d) == {"defaults", "_routing_defaults"}
    cpu = d["defaults"]["cpu"]
    assert lpt.is_scalar(cpu) and cpu["text"] == "80" and cpu["tag"] == "!!int", cpu
    quoted = doc["files"]["team/t1.yaml"]["tenants"]["t1"]["cpu"]
    assert quoted["tag"] == "!!str" and quoted["value"] == "90", quoted
    assert not lpt.is_scalar(d["_routing_defaults"])


def test_no_binary_is_its_own_error(tmp_path) -> None:
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    with pytest.raises(lpt.BinaryMissing) as e:
        lpt.run(root, str(tmp_path / "no-such-da-guard"), 60)
    assert isinstance(e.value, lpt.ParseTreeError)
    assert not str(e.value).startswith("Error: "), str(e.value)


def test_a_failing_binary_is_a_parse_tree_error(tmp_path) -> None:
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    fake = tmp_path / "fake-da-guard"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(lpt.ParseTreeError, match="exited 3: boom") as e:
        lpt.run(root, str(fake), 60)
    assert not isinstance(e.value, lpt.BinaryMissing)


def _self_killing_da_guard(tmp_path: Path) -> tuple[Path, Path]:
    """A da-guard that records its GOTRACEBACK and then ends by SIGABRT —
    what the Go runtime does under ``GOTRACEBACK=crash`` when it fails
    (out of memory, a fault inside the runtime)."""
    seen = tmp_path / "gotraceback"
    fake = tmp_path / "fake-da-guard"
    fake.write_text("#!/bin/sh\nprintf '%s' \"${GOTRACEBACK-unset}\" > "
                    f"'{seen}'\necho 'fatal error: whatever' >&2\nkill -ABRT $$\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    return fake, seen


def test_a_signal_without_a_memory_limit_is_a_parse_tree_error(tmp_path, monkeypatch) -> None:
    """#2906: no memory limit declared — a da-guard ended by a signal is a
    failing da-guard (rc 2 for the caller), and nothing changes its env."""
    monkeypatch.setattr(lpt, "_MEMORY_LIMITED", False)
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    fake, seen = _self_killing_da_guard(tmp_path)
    with pytest.raises(lpt.ParseTreeError, match="exited -6") as e:
        lpt.run(root, str(fake), 60)
    assert not isinstance(e.value, MemoryError)
    assert seen.read_text(encoding="utf-8") == "unset"


def test_a_signal_under_the_memory_limit_is_memory(tmp_path, monkeypatch) -> None:
    """#2906: under `run_under_memory_limit` (the generator container's
    judgement) da-guard runs with GOTRACEBACK=crash, and ended by a signal it
    is the judgement's memory limit — judged by the process status, the
    stderr text plays no part — named with the signal. Both file mode and
    tree mode."""
    monkeypatch.setattr(lpt, "_MEMORY_LIMITED", False)
    lpt.run_under_memory_limit()
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    fake, seen = _self_killing_da_guard(tmp_path)
    for call in (lambda: lpt.run(root, str(fake), 60),
                 lambda: lpt.run_file(root, "t.yaml", b"a: 1\n", str(fake), 60)):
        with pytest.raises(MemoryError, match="SIGABRT"):
            call()
        assert seen.read_text(encoding="utf-8") == "crash"
    # An ordinary failure under the limit is still a failing da-guard.
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    with pytest.raises(lpt.ParseTreeError, match="exited 3: boom"):
        lpt.run(root, str(fake), 60)


@pytest.mark.parametrize("node, scalar", [
    ({"text": "80", "tag": "!!int", "value": "80"}, True),
    ({"text": "x", "tag": "!!str", "value": None}, True),
    ({"text": {"text": "x", "tag": "!!str", "value": "x"}}, False),  # a mapping whose key is "text"
    ({"text": "x"}, False),
    ({"a": 1}, False),
    ([], False),
    ("80", False),
])
def test_is_scalar(node, scalar) -> None:
    assert lpt.is_scalar(node) is scalar


# ── migration-audit goes through this module, it does not keep a copy ───

def test_migration_audit_reads_through_the_shared_reader(tmp_path, monkeypatch) -> None:
    calls = []

    sentinel = lpt.Tree(selected={}, unreadable={}, not_read={}, rendered_by={}, _files={})

    def fake(root, binary, timeout):
        calls.append((root, binary, timeout))
        return sentinel

    monkeypatch.setattr(lpt, "load", fake)
    assert ma.run_parse_tree(tmp_path, "bin", 5) is sentinel
    assert calls == [(tmp_path, "bin", 5)]
    assert ma._is_v3_scalar is lpt.is_scalar
    assert (ma.PARSE_TREE_SUBCOMMAND, ma.PARSE_TREE_SCHEMA) == (lpt.SUBCOMMAND, lpt.SCHEMA)


@pytest.mark.parametrize("err, audit_note", [
    (lpt.BinaryMissing("da-guard not found"), True),
    (lpt.ParseTreeError("da-guard parse-tree exited 3"), False),
])
def test_migration_audit_turns_the_shared_errors_into_its_own(tmp_path, monkeypatch, err,
                                                              audit_note) -> None:
    def fail(*_a):
        raise err

    monkeypatch.setattr(lpt, "load", fail)
    with pytest.raises(ma.AuditError) as e:
        ma.run_parse_tree(tmp_path, None, 5)
    assert str(e.value).startswith(str(err))
    assert ("does not parse the YAML another way" in str(e.value)) is audit_note


def test_the_image_ships_the_module() -> None:
    build = (ROOT / "components/da-tools/app/build.sh").read_text(encoding="utf-8")
    assert any(line.strip() == "_lib_parse_tree.py" for line in build.splitlines()), (
        "build.sh does not copy _lib_parse_tree.py: migration-audit would ImportError in the image")



# ── loader API: Tree / to_python (ADR-036 step 4, #2906) ─────────────

def _s(text, tag, *value):
    """A scalar node; `value` omitted means the document has no "value" field."""
    node = {"text": text, "tag": tag}
    if value:
        node["value"] = value[0]
    return node


@pytest.mark.parametrize("node, want", [
    (_s("yes", "!!str", "yes"), "yes"),            # a string, never True (ADR-036 §2)
    (_s("off", "!!str", "off"), "off"),
    (_s("true", "!!bool", True), True),
    (_s("False", "!!bool", False), False),
    (_s("~", "!!null", None), None),
    (_s("", "!!null", None), None),
    (_s("0777", "!!int", "511"), 511),
    (_s("0x1F", "!!int", "31"), 31),
    (_s("-12", "!!int", "-12"), -12),
    (_s("9223372036854775808", "!!int", "9223372036854775808"), 2**63),
    (_s("18446744073709551615", "!!int", "18446744073709551615"), 2**64 - 1),
    (_s("1e3", "!!float", "1000"), 1000.0),
    (_s("1e-7", "!!float", "0.0000001"), 1e-7),
    (_s("-0.0", "!!float", "-0"), -0.0),
])
def test_to_python_reads_a_scalar_by_its_tag(node, want) -> None:
    got = lpt.to_python(node)
    assert got == want and type(got) is type(want), (got, want)


def test_a_big_int_is_exact() -> None:
    got = lpt.to_python(_s("123456789012345678901", "!!int", "123456789012345678901"))
    assert got == 123456789012345678901 and isinstance(got, int)


# Every spelling yaml.v3 resolves to an infinite or NaN float
# (third_party/yaml.v3/resolve.go); parse-tree gives them no "value".
@pytest.mark.parametrize("text", [".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"])
def test_positive_infinity(text) -> None:
    assert lpt.to_python(_s(text, "!!float")) == math.inf


@pytest.mark.parametrize("text", ["-.inf", "-.Inf", "-.INF"])
def test_negative_infinity(text) -> None:
    assert lpt.to_python(_s(text, "!!float")) == -math.inf


@pytest.mark.parametrize("text", [".nan", ".NaN", ".NAN"])
def test_nan(text) -> None:
    assert math.isnan(lpt.to_python(_s(text, "!!float")))


def test_a_timestamp_is_its_text_and_not_a_string() -> None:
    got = lpt.to_python(_s("2001-12-14", "!!timestamp"))
    assert got == lpt.Timestamp("2001-12-14")
    assert not isinstance(got, str)
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.text = "x"  # type: ignore[misc]


@pytest.mark.parametrize("node", [
    _s("x", "!!binary", "x"),                  # a tag outside the allowed set
    _s("x", "!foo", "x"),
    _s("1", "!!int"),                          # no value
    _s("1", "!!int", 1),                       # a JSON number, not the decimal text
    _s("1", "!!int", " 1"),                    # int() would take these
    _s("1", "!!int", "+1"),
    _s("1000", "!!int", "1_000"),
    _s("nan", "!!float", "nan"),               # float() would take it
    _s("inf", "!!float"),                      # not a yaml.v3 spelling
    _s(".inf", "!!float", None),               # null is not "no value"
    _s("yes", "!!bool", "yes"),                # no lenient conversion (ADR-036 §2)
    _s("yes", "!!str", True),
    _s("~", "!!null"),
    _s("2001-12-14", "!!timestamp", "2001-12-14"),
    {"text": "x", "tag": "!!str", "value": "x", "extra": 1},
    "x",
    3,
])
def test_to_python_refuses_any_other_node(node) -> None:
    with pytest.raises(ValueError):
        lpt.to_python({"k": [node]})


def test_to_python_keeps_keys_and_nesting() -> None:
    node = {"010": {"_routing": {"send_resolved": _s("yes", "!!str", "yes")}},
            "list": [_s("1", "!!int", "1"), [], {}, [_s("~", "!!null", None)]]}
    assert lpt.to_python(node) == {"010": {"_routing": {"send_resolved": "yes"}},
                                   "list": [1, [], {}, [None]]}


def test_to_python_is_not_limited_by_the_recursion_limit() -> None:
    depth = sys.getrecursionlimit() * 3
    node: object = _s("x", "!!str", "x")
    for _ in range(depth):
        node = {"a": node}
    got = lpt.to_python(node)
    for _ in range(depth):
        got = got["a"]
    assert got == "x"


def test_scalar_text() -> None:
    assert lpt.scalar_text(_s("0777", "!!int", "511")) == "0777"
    with pytest.raises(ValueError):
        lpt.scalar_text({"a": _s("1", "!!int", "1")})


def _entry(path, readers=("exporter", "generator"), *, cls="tenant", tenant="t", not_read=None,
           role="config", problem=None, document=False, verdict=None) -> dict:
    return {"path": path, "depth": path.count("/"), "class": cls,
            "tenant_named": tenant if cls == "tenant" else None, "defaults_document": document,
            "readers": list(readers), "not_read": not_read, "role": role,
            "problem": None if problem is None else {"kind": problem[0], "message": problem[1]},
            "verdict": verdict}


def _doc(**over) -> dict:
    doc = {
        "schema": lpt.SCHEMA, "da_guard_version": "test", "mode": "tree",
        "defaults_carriers": {},
        "entries": [
            _entry("a/_defaults.yml", (), cls="defaults", not_read="unselected_defaults_carrier",
                   document=True),
            _entry("bad.yaml", tenant="bad", problem=("duplicate_key", "line 2: twice")),
            _entry("gone.yaml", tenant="gone", role="not_a_file",
                   problem=("read_failure", "the path cannot be statted (stat_error)")),
            _entry("t.yaml"),
        ],
        "files": {"t.yaml": {"cpu": _s("80", "!!int", "80")}},
        "rendered_by": {},
    }
    doc.update(over)
    lpt.check_shape(doc)
    return doc


def test_tree_reads_a_selected_file() -> None:
    tree = lpt.tree_of(_doc())
    assert tree.value("t.yaml") == {"cpu": 80}
    assert lpt.is_scalar(tree.node("t.yaml")["cpu"])
    # Computed from the entries: every path a reader tries, with its readers.
    assert dict(tree.selected) == {"t.yaml": ("exporter", "generator"),
                                   "bad.yaml": ("exporter", "generator"),
                                   "gone.yaml": ("exporter", "generator")}
    assert dict(tree.unreadable) == {"bad.yaml": ("duplicate_key", "line 2: twice"),
                                     "gone.yaml": ("read_failure", "the path cannot be statted (stat_error)")}
    assert dict(tree.not_read) == {"a/_defaults.yml": "unselected_defaults_carrier"}
    assert tree.value("t.yaml") is not tree.value("t.yaml")   # a new object each call


def test_tree_entries_carry_go_s_role() -> None:
    """The role is read, never composed: `gone.yaml` has the same problem kind
    a permission refusal would have, and only its role says it is no file."""
    tree = lpt.tree_of(_doc())
    by = {e.path: e for e in tree.entries}
    assert [e.path for e in tree.entries] == ["a/_defaults.yml", "bad.yaml", "gone.yaml", "t.yaml"]
    assert by["gone.yaml"].not_a_file and not by["gone.yaml"].config_file
    assert by["bad.yaml"].config_file and by["bad.yaml"].problem == ("duplicate_key", "line 2: twice")
    assert by["a/_defaults.yml"].config_file and by["a/_defaults.yml"].readers == ()
    doc = _doc()
    doc["entries"][2]["role"] = "config"   # the same facts, Go's other verdict
    assert lpt.tree_of(doc).entries[2].config_file


def test_an_unreadable_file_raises_never_reads_empty() -> None:
    tree = lpt.tree_of(_doc())
    for read in (tree.value, tree.node):
        with pytest.raises(lpt.FileUnreadable) as e:
            read("bad.yaml")
        assert (e.value.file, e.value.kind, e.value.message) == ("bad.yaml", "duplicate_key", "line 2: twice")
        assert isinstance(e.value, lpt.ParseTreeError)


@pytest.mark.parametrize("rel", ["a/_defaults.yml", "missing.yaml"])
def test_a_file_no_reader_selects_is_a_key_error(rel) -> None:
    tree = lpt.tree_of(_doc())
    for read in (tree.value, tree.node):
        with pytest.raises(KeyError):
            read(rel)


def test_tree_is_read_only() -> None:
    tree = lpt.tree_of(_doc())
    with pytest.raises(dataclasses.FrozenInstanceError):
        tree.selected = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        tree.selected["x"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        tree.unreadable["x"] = ("k", "m")  # type: ignore[index]


def test_rendered_by_is_read() -> None:
    tree = lpt.tree_of(_doc(rendered_by={"_defaults.yaml": {"defaults": ["exporter"]}}))
    assert tree.rendered_by is not None
    assert tree.rendered_by["_defaults.yaml"]["defaults"] == ("exporter",)


def _without(field):
    def drop(doc):
        del doc[field]
    return drop


def _entry_without(field):
    def drop(doc):
        del doc["entries"][3][field]
    return drop


def _set(**kv):
    def put(doc):
        doc.update(kv)
    return put


def _entry_set(i, **kv):
    def put(doc):
        doc["entries"][i].update(kv)
    return put


@pytest.mark.parametrize("change", [
    _set(rendered_by={"_defaults.yaml": {"defaults": "exporter"}}),
    _set(rendered_by=[]),
    _without("rendered_by"),
    _without("entries"),
    _without("defaults_carriers"),
    _without("da_guard_version"),
    _set(da_guard_version=1),
    _set(mode="list"),
    _set(entries=[_entry("t.yaml"), _entry("t.yaml")]),          # a path twice
    _entry_without("role"),                                       # a da-guard before roles
    _entry_without("defaults_document"),
    _entry_set(3, role="maybe"),                                  # a role nobody knows
    _entry_set(3, extra=1),
    _entry_set(3, readers=[]),                                    # no reader, no reason
    _entry_set(1, problem={"kind": "duplicate_key"}),
    _entry_set(1, problem=None),                                  # a config file with no content
    _set(files={"t.yaml": {}, "bad.yaml": {}}),                   # content beside a problem
    _set(files={"t.yaml": {}, "elsewhere.yaml": {}}),             # content of no entry
], ids=lambda f: f.__qualname__)
def test_a_malformed_document_is_unusable_whole(change) -> None:
    doc = {**_doc(), "entries": [dict(e) for e in _doc()["entries"]]}
    change(doc)
    with pytest.raises(lpt.ParseTreeError):
        lpt.tree_of(doc)


@pytest.mark.parametrize("schema", ["da-guard.parse-tree/v1", "da-guard.parse-tree-list/v1"])
def test_an_older_da_guard_s_document_is_named_as_such(schema) -> None:
    raw = json.dumps({**_doc(), "schema": schema}).encode()
    with pytest.raises(lpt.ParseTreeError, match="older than this tool"):
        lpt.read_document(raw)


def test_a_document_without_a_mode_names_an_older_da_guard() -> None:
    doc = _doc()
    del doc["mode"]
    with pytest.raises(lpt.ParseTreeError, match="no 'mode' .*older than this tool"):
        lpt.read_document(json.dumps(doc).encode())


def test_content_on_an_entry_that_is_no_config_file_is_refused() -> None:
    """Tree mode reads only config files: content beside a `dir` entry is a
    document wrong somewhere, refused whole. (A `not_a_file` entry always
    carries a problem, so content beside one is "content and a problem".)"""
    doc = _doc()
    doc["entries"][2].update(role="dir", problem=None, readers=[], not_read=None,
                             tenant_named=None, **{"class": "directory"})
    doc["files"]["gone.yaml"] = {}
    with pytest.raises(lpt.ParseTreeError, match="content but role 'dir'"):
        lpt.tree_of(doc)
    doc = _doc()
    doc["files"]["gone.yaml"] = {}
    with pytest.raises(lpt.ParseTreeError, match="content and a problem"):
        lpt.tree_of(doc)


def test_an_entry_without_a_field_names_an_older_da_guard() -> None:
    doc = _doc()
    del doc["entries"][0]["role"]
    with pytest.raises(lpt.ParseTreeError, match="no role .*older than this tool"):
        lpt.read_document(json.dumps(doc).encode())


def test_load_reads_the_real_document(tmp_path, da_guard_binary) -> None:
    root = _tree(tmp_path, {
        "t.yaml": "tenants:\n  010:\n    send_resolved: yes\n    cpu: 0777\n    at: 2001-12-14\n",
        "dup.yaml": "a: 1\na: 2\n",
    })
    tree = lpt.load(root, da_guard_binary, 60)
    assert tree.value("t.yaml") == {"tenants": {"010": {
        "send_resolved": "yes", "cpu": 511, "at": lpt.Timestamp("2001-12-14")}}}
    assert lpt.scalar_text(tree.node("t.yaml")["tenants"]["010"]["cpu"]) == "0777"
    assert tree.rendered_by is not None   # today's da-guard writes it
    with pytest.raises(lpt.FileUnreadable) as e:
        tree.value("dup.yaml")
    assert e.value.kind == "duplicate_key"


def test_mapping_keys_come_sorted_not_in_file_order(tmp_path, da_guard_binary) -> None:
    """The fact the module docstring states: parse-tree writes Go maps, whose
    JSON keys encoding/json sorts. A reader needing the author's order cannot
    get it from the document."""
    root = _tree(tmp_path, {"t.yaml": 'zeta: 1\nalpha:\n  mike: a\n  bravo: b\n  "10": x\n  "9": y\nMid: 2\n_b: 3\n'})
    v = lpt.load(root, da_guard_binary, 60).value("t.yaml")
    assert list(v) == ["Mid", "_b", "alpha", "zeta"]
    assert list(v["alpha"]) == ["10", "9", "bravo", "mike"]


def test_every_corpus_row_through_the_real_binary(tmp_path, da_guard_binary) -> None:
    """tests/shared/parse_tree_corpus.json, every row and every merge-key row,
    each file alone in its own directory of one tree: a row parse-tree reads
    converts without raising; a row it refuses raises FileUnreadable with the
    row's kind. Directories, not file mode: one run of the binary for all
    1,600-odd rows, and every row keeps the file name it is written for."""
    sys.path.insert(0, str(ROOT / "tests" / "shared"))
    from test_merge_key_policy_corpus import _encode  # noqa: E402

    shared = ROOT / "tests" / "shared"
    corpus = json.loads((shared / "parse_tree_corpus.json").read_text(encoding="utf-8"))
    mk_docs = {r["id"]: r for r in json.loads(
        (shared / "merge_key_policy_corpus.json").read_text(encoding="utf-8"))["rows"]}
    mk_cats = json.loads((shared / "parse_tree_merge_key_categories.json")
                         .read_text(encoding="utf-8"))["categories"]
    rows = [(r["id"], r["file"], r["doc"], r.get("encoding"), r["category"]) for r in corpus["rows"]]
    rows += [(r["id"], "_domain_policy.yaml", mk_docs[r["id"]]["doc"], mk_docs[r["id"]].get("encoding"),
              mk_cats[r["id"]]) for r in corpus["merge_key_rows"]]
    assert len(rows) > 1000
    root = tmp_path / "conf.d"
    rels = {}
    for rid, name, text, enc, cat in rows:
        rel = f"r{rid}/{name}"
        if rel in rels:   # the same file twice (one row id = one file's bytes)
            assert rels[rel] == cat, rel
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_encode(text, enc))
        rels[rel] = cat
    tree = lpt.load(root, da_guard_binary, 300)
    assert set(tree.selected) == set(rels), "every row's file must be selected"
    read = refused = 0
    for rel, cat in sorted(rels.items()):
        if cat.startswith("unreadable:"):
            with pytest.raises(lpt.FileUnreadable) as e:
                tree.value(rel)
            assert e.value.kind == cat.removeprefix("unreadable:"), rel
            refused += 1
        else:
            assert cat in ("both_read", "pyyaml_only"), (rel, cat)
            assert isinstance(tree.value(rel), dict), rel
            read += 1
    assert read and refused, (read, refused)


def test_a_relative_da_guard_binary_is_the_current_directory_s(tmp_path, monkeypatch,
                                                               da_guard_binary) -> None:
    """#2906 review: a relative `$DA_GUARD_BINARY` means what it means in the
    shell — relative to the working directory at resolution — and a process
    about to change directory makes it absolute first."""
    import shutil
    root = _tree(tmp_path, {"t.yaml": "a: 1\n"})
    (tmp_path / "bin").mkdir()
    shutil.copy2(da_guard_binary, tmp_path / "bin" / "da-guard")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DA_GUARD_BINARY", "bin/da-guard")
    assert lpt.load(root, None, 60).value("t.yaml") == {"a": 1}
    env = {"DA_GUARD_BINARY": "bin/da-guard"}
    lpt.absolute_da_guard_binary(env)
    assert env["DA_GUARD_BINARY"] == str(tmp_path / "bin" / "da-guard")
    for unchanged in ({}, {"DA_GUARD_BINARY": " "}, {"DA_GUARD_BINARY": "/abs/da-guard"}):
        before = dict(unchanged)
        lpt.absolute_da_guard_binary(unchanged)
        assert unchanged == before
