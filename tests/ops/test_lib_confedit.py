"""_lib_confedit：Python 工具改 conf.d（改寫、新建、整份取代、搬移、刪除）時把意圖交給 `da-guard write`（ADR-036 §2，#3206）。

這裡釘的是本模組自己的契約：值的型別規則（讀到的保留型別、使用者打的只有標準
十進位才是數字）、`da-guard.write/v3` 的回答（寫入／拒絕／呼叫錯誤／寫到一半）
各自怎麼交給工具、以及多檔一次送出（#3206 步驟 10c：一次 da-guard、整批一次判定，
全部通過才寫）。edit 本身的判定（§4 不變式、讀回）是 Go 的，測在 pkg/confedit 與
cmd/da-guard。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import pytest

import _lib_confedit as ce
import _lib_parse_tree
from _platform_fs import symlink_or_skip

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tree(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        (root / rel).write_text(body, encoding="utf-8")
    return root


def _bytes(root):
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


# ── values ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("80", {"int": "80"}), ("-1", {"int": "-1"}), ("0", {"int": "0"}),
    ("0.5", {"float": "0.5"}), ("-12.25", {"float": "-12.25"}),
    ("70:critical", {"str": "70:critical"}), ("010", {"str": "010"}),
    ("1e3", {"str": "1e3"}), ("+1", {"str": "+1"}), ("-0", {"str": "-0"}),
    ("0x1F", {"str": "0x1F"}), ("1_000", {"str": "1_000"}), (".5", {"str": ".5"}),
    ("disable", {"str": "disable"}), ("true", {"str": "true"}), ("", {"str": ""}),
    ("1" * 18, {"int": "1" * 18}), ("1" * 19, {"str": "1" * 19}),
])
def test_a_typed_value_is_a_number_only_when_canonical_decimal(text, want):
    assert ce.cli_value(text) == want


@pytest.mark.parametrize("node,want", [
    ({"text": "80", "tag": "!!str", "value": "80"}, {"str": "80"}),
    ({"text": "80", "tag": "!!int", "value": "80"}, {"int": "80"}),
    ({"text": "0.5", "tag": "!!float", "value": "0.5"}, {"float": "0.5"}),
    ({"text": "yes", "tag": "!!str", "value": "yes"}, {"str": "yes"}),
    ({"text": "true", "tag": "!!bool", "value": True}, {"bool": True}),
    ({"text": "~", "tag": "!!null", "value": None}, {"null": None}),
    ([{"text": "a", "tag": "!!str", "value": "a"}], {"seq": [{"str": "a"}]}),
    ({"b": {"text": "1", "tag": "!!int", "value": "1"},
      "a": {"text": "x", "tag": "!!str", "value": "x"}},
     {"map": [["a", {"str": "x"}], ["b", {"int": "1"}]]}),
])
def test_a_read_value_keeps_the_type_it_was_read_with(node, want):
    assert ce.node_value(node) == want


@pytest.mark.parametrize("node", [
    {"text": "010", "tag": "!!int", "value": "8"},
    {"text": "1e3", "tag": "!!float", "value": "1000"},
    {"text": "2024-01-01", "tag": "!!timestamp", "value": "2024-01-01"},
])
def test_a_read_value_the_edit_cannot_carry_as_written_is_refused(node):
    with pytest.raises(ValueError, match="cannot be written back as it is written"):
        ce.node_value(node)


@pytest.mark.parametrize("obj,want", [
    ("80", {"str": "80"}), ("disable", {"str": "disable"}), (80, {"int": "80"}),
    (-1, {"int": "-1"}), (1.5, {"float": "1.5"}), (2.0, {"float": "2.0"}),
    (1e-05, {"float": "0.00001"}), (1e20, {"float": "100000000000000000000.0"}),
    (True, {"bool": True}), (None, {"null": None}),
    (["a", 1], {"seq": [{"str": "a"}, {"int": "1"}]}),
    ({"b": 1, "a": "x"}, {"map": [["b", {"int": "1"}], ["a", {"str": "x"}]]}),
])
def test_a_generated_value_has_its_python_type_and_key_order(obj, want):
    # `data_value` (#3206 10c-2): what a tool generates keeps its Python type;
    # a mapping keeps the order the tool built it in (no sorting).
    assert ce.data_value(obj) == want


@pytest.mark.parametrize("obj,where", [
    ({"defaults": {"k": float("inf")}}, "defaults.k"),
    ({"defaults": {"k": float("nan")}}, "defaults.k"),
    ({"x": [10 ** 18]}, r"x\.\[0\]"),
    ({"x": {1, 2}}, "x"),
    ({"x": {1: "a"}}, "x"),
    ({"x": {None: 1}}, "x"),
])
def test_a_generated_value_with_no_contract_type_is_refused_by_its_key(obj, where):
    with pytest.raises(ValueError, match=f"^{where}: "):
        ce.data_value(obj)


def test_a_negative_int_at_the_limit_is_named_by_its_length_not_its_digits():
    # 18 digits plus a sign is past the contract's 18 characters; the message
    # must not claim more digits than there are.
    with pytest.raises(ValueError, match="longer than 18 characters"):
        ce.data_value(-(10 ** 18 - 1))


def test_a_generated_float_reads_back_as_the_number_meant(tmp_path):
    root = _tree(tmp_path / "conf.d", {})
    content = ce.data_value({"defaults": {"a": 1e-05, "b": 2.0, "c": 7}})
    ce.emit_all(root, [("_defaults.yaml", content)])
    node = _lib_parse_tree.load(root, None, 120).node("_defaults.yaml")["defaults"]
    assert {k: (v["tag"], v["text"]) for k, v in node.items()} == {
        "a": ("!!float", "0.00001"), "b": ("!!float", "2.0"), "c": ("!!int", "7")}


# ── one file ────────────────────────────────────────────────────────

def test_a_typed_severity_round_trips_as_the_string(tmp_path):
    root = _tree(tmp_path / "conf.d", {"t-a.yaml": "tenants:\n  t-a:\n    cpu: '5'\n"})
    ops = [ce.set_op(["tenants", "t-a", "cpu"], ce.cli_value("70:critical"))]
    doc = ce.edit(root, "t-a.yaml", ops)
    assert doc["status"] == ce.WRITTEN, doc
    assert (root / "t-a.yaml").read_text(encoding="utf-8") == (
        'tenants:\n  t-a:\n    cpu: "70:critical"\n')
    node = _lib_parse_tree.load(root).node("t-a.yaml")["tenants"]["t-a"]["cpu"]
    assert (node["tag"], node["value"]) == ("!!str", "70:critical")
    assert ce.edit(root, "t-a.yaml", ops)["status"] == ce.UNCHANGED


def test_comments_outside_the_edit_survive(tmp_path):
    # Before #3206 the PyYAML rewrite kept only a head block of `#` lines
    # (`_header_of`); every other comment was lost.
    text = ("# head\n# second head line\ntenants:\n  t-a:\n"
            "    # about cpu\n    cpu: '5' # trailing\n"
            "    mem: '6' # mem note\n# foot\n")
    root = _tree(tmp_path / "conf.d", {"t-a.yaml": text})
    doc = ce.edit(root, "t-a.yaml", [ce.unset_op(["tenants", "t-a", "mem"])], dry_run=True)
    assert doc["status"] == ce.DRY_RUN
    out = doc["content"]
    for kept in ("# head\n# second head line\n", "    # about cpu\n",
                 "    cpu: '5' # trailing\n"):
        assert kept in out, out
    assert "mem" not in out, out
    assert (root / "t-a.yaml").read_text(encoding="utf-8") == text  # dry run


def test_a_refusal_is_edit_refused_with_dagards_reason(tmp_path):
    root = _tree(tmp_path / "conf.d", {"t-a.yaml": "tenants:\n  t-a:\n"})
    with pytest.raises(ce.EditRefused) as exc:
        ce.edit(root, "t-a.yaml", [ce.ensure_map_op(["tenants", "t-a"])])
    assert (exc.value.status, exc.value.reason) == ("refused", "edit_not_applicable")
    assert "t-a.yaml" in str(exc.value) and "edit_not_applicable" in str(exc.value)


def test_a_caller_error_is_edit_caller_error(tmp_path):
    root = _tree(tmp_path / "conf.d", {"t-a.yaml": "tenants: {}\n"})
    with pytest.raises(ce.EditCallerError) as exc:
        ce.edit(root, "t-a.yaml", [ce.set_op(["tenants", "x"], {"int": "010"})])
    assert (exc.value.status, exc.value.reason) == ("caller_error", "bad_input")
    with pytest.raises(ce.EditCallerError) as exc:
        ce.edit(root, "missing.yaml", [])
    assert exc.value.reason == "path_missing"


def test_no_da_guard_is_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    root = _tree(tmp_path / "conf.d", {"t-a.yaml": "tenants: {}\n"})
    with pytest.raises(_lib_parse_tree.BinaryMissing):
        ce.edit(root, "t-a.yaml", [])


# ── several files: all or nothing ───────────────────────────────────

def test_one_refusal_writes_no_file_and_names_every_refusal(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "a.yaml": "tenants:\n  t-a:\n    cpu: '5'\n",
        "b.yaml": "tenants:\n  t-b:\n",
        "real.txt": "tenants:\n  t-c:\n    cpu: '5'\n",
    })
    symlink_or_skip("real.txt", root / "c.yaml")
    before = _bytes(root)
    with pytest.raises(ce.EditsRefused) as exc:
        ce.edit_all(root, [
            ("a.yaml", [ce.unset_op(["tenants", "t-a", "cpu"])]),
            ("b.yaml", [ce.ensure_map_op(["tenants", "t-b"])]),
            ("c.yaml", [ce.unset_op(["tenants", "t-c", "cpu"])]),
        ])
    assert [(r.path, r.reason) for r in exc.value.refusals] == [
        ("b.yaml", "edit_not_applicable"), ("c.yaml", "symlink")]
    assert _bytes(root) == before


def test_every_file_passes_then_each_is_written(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "a.yaml": "tenants:\n  t-a:\n    cpu: '5'\n",
        "b.yaml": "tenants:\n  t-b:\n    cpu: '6'\n",
    })
    docs = ce.edit_all(root, [
        ("a.yaml", [ce.unset_op(["tenants", "t-a", "cpu"])]),
        ("b.yaml", [ce.unset_op(["tenants", "t-b", "nothing"])]),
    ])
    assert [d["status"] for d in docs] == [ce.WRITTEN, ce.UNCHANGED]
    assert (root / "a.yaml").read_text(encoding="utf-8") == "tenants:\n  t-a: {}\n"
    assert (root / "b.yaml").read_text(encoding="utf-8") == "tenants:\n  t-b:\n    cpu: '6'\n"


def test_several_files_are_one_da_guard_run(tmp_path, monkeypatch):
    # #3206 step 10c: one batch, one subprocess, one judgement — not a dry
    # run per file and a write per file.
    root = _tree(tmp_path / "conf.d", {
        f"t-{i}.yaml": f"tenants:\n  t-{i}:\n    cpu: '5'\n" for i in range(3)})
    runs = []
    real = _lib_parse_tree._run

    def run(args, *a, **kw):
        runs.append(args[0])
        return real(args, *a, **kw)
    monkeypatch.setattr(_lib_parse_tree, "_run", run)
    docs = ce.edit_all(root, [(f"t-{i}.yaml", [ce.unset_op(["tenants", f"t-{i}", "cpu"])])
                              for i in range(3)])
    assert runs == ["write"]
    assert [(d["path"], d["status"]) for d in docs] == [
        (f"t-{i}.yaml", ce.WRITTEN) for i in range(3)]
    assert ce.edit_all(root, []) == [] and runs == ["write"]  # nothing to send, nothing run


def test_the_batch_is_judged_as_one_tree(tmp_path):
    # Two carrier spellings of one directory pass each alone; written
    # together, the directory reads one, the other has no reader: refused,
    # and neither is written.
    root = _tree(tmp_path / "conf.d", {"x.yaml": "tenants:\n  t-a: {}\n"})
    content = ce.map_value([("defaults", ce.map_value([("cpu", {"int": "5"})]))])
    for rel in ("_defaults.yaml", "_defaults.yml"):
        assert ce.emit(root, rel, content, dry_run=True)["status"] == ce.DRY_RUN
    before = _bytes(root)
    with pytest.raises(ce.EditsRefused) as exc:
        ce.emit_all(root, [("_defaults.yaml", content), ("_defaults.yml", content)])
    assert [(r.path, r.reason, r.command) for r in exc.value.refusals] == [
        ("_defaults.yml", "not_read", "write")]
    assert _bytes(root) == before


def test_emit_all_writes_every_new_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {"x.yaml": "tenants:\n  t-x: {}\n"})
    files = [(f"team/t-{i}.yaml", ce.map_value([("tenants", ce.map_value(
        [(f"t-{i}", ce.map_value([("cpu", ce.cli_value("70"))]))]))])) for i in range(3)]
    docs = ce.emit_all(root, files)
    assert [d["status"] for d in docs] == [ce.WRITTEN] * 3
    assert (root / "team" / "t-1.yaml").read_text(encoding="utf-8") == (
        "tenants:\n  t-1:\n    cpu: 70\n")
    with pytest.raises(ce.EditCallerError) as exc:  # they exist now
        ce.emit_all(root, files)
    assert (exc.value.path, exc.value.reason) == ("team/t-0.yaml", "path_exists")


def test_a_path_given_twice_is_a_caller_error(tmp_path):
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n"})
    with pytest.raises(ce.EditCallerError) as exc:
        ce.edit_all(root, [("a.yaml", []), ("a.yaml", [])])
    assert (exc.value.path, exc.value.reason) == ("", "bad_input")
    assert "given once" in exc.value.message


def test_set_the_whole_top_level(tmp_path):
    root = _tree(tmp_path / "conf.d", {"gen.yaml": "# generated\nold: '1'\n"})
    top = ce.map_value([("tenants", ce.map_value([("t-a", ce.map_value([]))]))])
    assert ce.edit(root, "gen.yaml", [ce.set_op([], top)])["status"] == ce.WRITTEN
    assert (root / "gen.yaml").read_text(encoding="utf-8") == "tenants:\n  t-a: {}\n"


def _answer(status, reason, files):
    return {"schema": ce.SCHEMA, "da_guard_version": "test", "command": "write",
            "status": status, "reason": reason, "message": "stopped",
            "files": [{"path": p, "status": s, "reason": r, "message": m, "entry": None,
                       "problem": None, "difference": None, "content": None, "warnings": []}
                      for p, s, r, m in files]}


def test_a_failure_after_a_write_names_the_files_written(tmp_path, monkeypatch):
    # da-guard's answer when a rename fails after another file was renamed
    # (cmd/da-guard tests make it happen; here the library reads it).
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n", "b.yaml": "tenants: {}\n"})
    answer = _answer("caller_error", "partial_write", [
        ("a.yaml", "written", None, None),
        ("b.yaml", "caller_error", "write_failed", "disk full")])
    monkeypatch.setattr(_lib_parse_tree, "_run", lambda *a, **kw: answer)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.edit_all(root, [("a.yaml", []), ("b.yaml", [])])
    assert (exc.value.written, exc.value.failed, exc.value.maybe_written) == (["a.yaml"], "b.yaml", [])
    assert isinstance(exc.value.cause, ce.EditCallerError)
    assert exc.value.cause.reason == "write_failed"
    assert "already written: a.yaml" in str(exc.value)


def test_an_emitted_file_left_in_place_may_have_been_written(tmp_path, monkeypatch):
    root = _tree(tmp_path / "conf.d", {})
    answer = _answer("caller_error", "partial_write", [
        ("a.yaml", "maybe_written", None, "could not be removed again"),
        ("b.yaml", "refused", "changed_during_write", "created meanwhile")])
    monkeypatch.setattr(_lib_parse_tree, "_run", lambda *a, **kw: answer)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.emit_all(root, [("a.yaml", ce.map_value([])), ("b.yaml", ce.map_value([]))])
    assert (exc.value.written, exc.value.maybe_written, exc.value.failed) == ([], ["a.yaml"], "b.yaml")
    assert isinstance(exc.value.cause, ce.EditRefused)


def test_a_move_left_unknown_names_its_source_too(tmp_path, monkeypatch):
    # #3206 review: a move da-guard could not undo may have changed both
    # names: PartialWrite lists the target and the source.
    root = _tree(tmp_path / "conf.d", {"m.yaml": "tenants: {}\n"})
    answer = _answer("caller_error", "partial_write", [
        ("n.yaml", "maybe_written", None, "not undone"),
        ("b.yaml", "caller_error", "write_failed", "disk full")])
    answer["files"][0].update(kind="move", **{"from": "m.yaml"})
    monkeypatch.setattr(_lib_parse_tree, "_run", lambda *a, **kw: answer)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.write_all(root, [ce.move_entry("n.yaml", "m.yaml"), ce.edit_entry("b.yaml", [])])
    assert exc.value.maybe_written == ["n.yaml", "m.yaml"]


def test_no_answer_to_a_write_may_have_landed_anywhere(tmp_path, monkeypatch):
    # da-guard killed or timed out while writing (no document): any of the
    # batch's renames may have happened, so every file is named as one that
    # MAY have been written. A dry run writes nothing: the error as it is.
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n", "b.yaml": "tenants: {}\n"})

    def run(*a, **kw):
        raise _lib_parse_tree.ParseTreeError("da-guard write did not finish within 1s")
    monkeypatch.setattr(_lib_parse_tree, "_run", run)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.edit_all(root, [("a.yaml", []), ("b.yaml", [])])
    assert (exc.value.written, exc.value.maybe_written) == ([], ["a.yaml", "b.yaml"])
    assert "may have been written: a.yaml, b.yaml" in str(exc.value)
    with pytest.raises(_lib_parse_tree.ParseTreeError):
        ce.edit_all(root, [("a.yaml", [])], dry_run=True)
    with pytest.raises(_lib_parse_tree.ParseTreeError):  # one file: as every other run
        ce.edit(root, "a.yaml", [])


def test_an_older_da_guard_is_named_as_such(tmp_path, monkeypatch):
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n"})
    v1 = {"schema": "da-guard.write/v1", "status": "written"}
    monkeypatch.setattr(_lib_parse_tree.subprocess, "run", lambda *a, **kw: type(
        "P", (), {"returncode": 0, "stdout": __import__("json").dumps(v1).encode(), "stderr": b""})())
    # #3206 review: it answered as another da-guard and took nothing — not a
    # maybe-written PartialWrite, on a dry run or a real write alike.
    for dry in (True, False):
        with pytest.raises(ce.EditCallerError, match="older than this tool") as exc:
            ce.edit_all(root, [("a.yaml", [])], dry_run=dry)
        assert exc.value.reason == "da_guard_mismatch"
        assert exc.value.__cause__.older is True


def test_a_da_guard_that_does_not_know_the_command_took_nothing(tmp_path, monkeypatch):
    # exit 2 and the binary rejects the command as unknown: older, by the
    # exception's metadata, not its message.
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n"})
    monkeypatch.setattr(_lib_parse_tree.subprocess, "run", lambda *a, **kw: type(
        "P", (), {"returncode": 2, "stdout": b"", "stderr": b"unknown command"})())
    monkeypatch.setattr(_lib_parse_tree, "_accepts", lambda *a, **kw: False)
    with pytest.raises(ce.EditCallerError) as exc:
        ce.edit_all(root, [("a.yaml", [])])
    assert exc.value.reason == "da_guard_mismatch" and exc.value.__cause__.older is True


def test_a_newer_da_guard_may_have_written(tmp_path, monkeypatch):
    # #3206 review: a schema not known to be older (a newer da-guard) is no
    # proof that nothing was written: a real write is PartialWrite, every
    # file maybe written; a dry run wrote nothing.
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n", "b.yaml": "tenants: {}\n"})
    v3 = {"schema": "da-guard.write/v99", "status": "written"}  # newer than this tool
    monkeypatch.setattr(_lib_parse_tree.subprocess, "run", lambda *a, **kw: type(
        "P", (), {"returncode": 0, "stdout": __import__("json").dumps(v3).encode(), "stderr": b""})())
    with pytest.raises(ce.PartialWrite) as exc:
        ce.edit_all(root, [("a.yaml", []), ("b.yaml", [])])
    assert (exc.value.written, exc.value.maybe_written) == ([], ["a.yaml", "b.yaml"])
    assert isinstance(exc.value.cause, _lib_parse_tree.NotThisDocument) and exc.value.cause.older is False
    with pytest.raises(ce.EditCallerError) as dry:
        ce.edit_all(root, [("a.yaml", [])], dry_run=True)
    assert dry.value.reason == "da_guard_mismatch"


def test_a_batch_that_worsens_the_tree_is_tree_worsened(tmp_path):
    # Each file passes alone; together with the tree a tenant is declared
    # twice: refused whole, every place named, nothing written.
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants:\n  t-a: {}\n"})
    content = ce.map_value([("tenants", ce.map_value([("t-a", ce.map_value([]))]))])
    before = _bytes(root)
    with pytest.raises(ce.TreeWorsened) as exc:
        ce.emit_all(root, [("b.yaml", content)])
    assert [(w["kind"], w["path"], w["tenant"]) for w in exc.value.worsened] == [
        ("duplicate_tenant", "b.yaml", "t-a")]
    assert [(r.path, r.reason) for r in exc.value.refusals] == [("b.yaml", "worsens_tree")]
    assert isinstance(exc.value, ce.EditsRefused)
    assert _bytes(root) == before


# ── write: edit, create and move in one batch ───────────────────────

_KEPT = "# kept as written\ntenants:\n  t-m:\n    x: 010 # an int as written\n"


def test_write_all_edits_creates_and_moves_in_one_run(tmp_path):
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants:\n  t-a:\n    cpu: '5'\n",
                                       "m.yaml": _KEPT})
    docs = ce.write_all(root, [
        ce.create_entry("c.yaml", ce.map_value([("tenants", ce.map_value([("t-c", ce.map_value([]))]))]),
                        head_comment="generated"),
        ce.move_entry("team/m.yaml", "m.yaml"),
        ce.edit_entry("a.yaml", [ce.unset_op(["tenants", "t-a", "cpu"])]),
    ])
    assert [(d["kind"], d["status"]) for d in docs] == [
        ("create", ce.WRITTEN), ("move", ce.WRITTEN), ("edit", ce.WRITTEN)]
    assert docs[1]["from"] == "m.yaml"
    assert not (root / "m.yaml").exists()
    assert (root / "team" / "m.yaml").read_bytes() == _KEPT.encode()
    assert (root / "c.yaml").read_text(encoding="utf-8") == "# generated\n\ntenants:\n  t-c: {}\n"


def test_a_refused_entry_writes_none_of_the_batch(tmp_path):
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants:\n  t-a:\n", "m.yaml": _KEPT})
    before = _bytes(root)
    with pytest.raises(ce.EditsRefused) as exc:
        ce.write_all(root, [ce.move_entry("n.yaml", "m.yaml"),
                            ce.edit_entry("a.yaml", [ce.ensure_map_op(["tenants", "t-a"])])])
    assert [(r.path, r.reason) for r in exc.value.refusals] == [("a.yaml", "edit_not_applicable")]
    assert _bytes(root) == before


def test_move_is_byte_for_byte_and_refuses_an_existing_target(tmp_path):
    root = _tree(tmp_path / "conf.d", {"m.yaml": _KEPT, "n.yaml": "tenants: {}\n"})
    with pytest.raises(ce.EditCallerError) as exc:
        ce.move(root, "n.yaml", "m.yaml")
    assert (exc.value.path, exc.value.reason) == ("n.yaml", "path_exists")
    assert ce.move(root, "o.yaml", "m.yaml")["status"] == ce.WRITTEN
    assert (root / "o.yaml").read_bytes() == _KEPT.encode() and not (root / "m.yaml").exists()


def test_comments_are_written_as_comments(tmp_path):
    root = _tree(tmp_path / "conf.d", {})
    content = ce.map_value([("tenants", ce.map_value([
        ("t-a", ce.map_value([("cpu", {"int": "80"}, ce.comments(head="⚠ counterexample", line="default"))])),
    ]))])
    doc = ce.emit(root, "t-a.yaml", content, head_comment="a header", dry_run=True)
    assert doc["content"] == ("# a header\n\ntenants:\n  t-a:\n    # ⚠ counterexample\n"
                              "    cpu: 80 # default\n")
    with pytest.raises(ce.EditCallerError) as exc:  # a line comment is one line
        ce.emit(root, "t-a.yaml", ce.map_value([("tenants", ce.map_value([]),
                                                  ce.comments(line="x\ntenants: {}"))]))
    assert exc.value.reason == "bad_input"
    assert list(root.iterdir()) == []
    with pytest.raises(ValueError):
        ce.map_value([("a", {"int": "1"}, {}, {})])


def test_a_foot_comment_ends_the_file_and_a_seq_item_carries_its_comments(tmp_path):
    root = _tree(tmp_path / "conf.d", {})
    content = ce.map_value([
        ("optional_overrides", ce.seq_value([
            {"str": "a"}, ({"str": "b"}, ce.comments(head="⚠ counterexample", line="x")),
            ({"str": "c"}, None)])),
        ("tenants", ce.map_value([("t-a", ce.map_value([]))])),
    ])
    assert content["map"][0][1] == {"seq": [{"str": "a"}, [{"str": "b"}, {"head": "⚠ counterexample", "line": "x"}],
                                            {"str": "c"}]}
    entry = ce.create_entry("t-a.yaml", content, foot_comment="declared:\n\n  cpu: 80")
    assert entry["foot_comment"] == "declared:\n\n  cpu: 80" and "head_comment" not in entry
    doc = ce.emit(root, "t-a.yaml", content, head_comment="h", foot_comment="declared:\n\n  cpu: 80")
    assert doc["status"] == ce.WRITTEN
    assert (root / "t-a.yaml").read_text(encoding="utf-8") == (
        "# h\n\noptional_overrides:\n  - \"a\"\n  # ⚠ counterexample\n  - \"b\" # x\n  - \"c\"\n"
        "tenants:\n  t-a: {}\n\n# declared:\n#\n#   cpu: 80\n")


def test_a_bad_foot_or_seq_item_comment_writes_nothing(tmp_path):
    root = _tree(tmp_path / "conf.d", {})
    for kwargs, content in [
        ({"foot_comment": "x\ry"}, ce.map_value([])),
        ({}, ce.map_value([("x", ce.seq_value([({"map": [["a", {"int": "1"}]]}, ce.comments(line="l"))]))])),
    ]:
        with pytest.raises(ce.EditCallerError) as exc:
            ce.emit(root, "a.yaml", content, **kwargs)
        assert exc.value.reason == "bad_input"
    assert list(root.iterdir()) == []
    with pytest.raises(ValueError):
        ce.seq_value([({"str": "a"},)])


# ── write: replace and delete ───────────────────────────────────────

def test_replace_and_delete_entries_are_the_contracts_shape():
    content = ce.map_value([])
    assert ce.create_entry("a.yaml", content) == {"path": "a.yaml", "create": content}
    assert ce.create_entry("a.yaml", content, head_comment="h", replace=True) == {
        "path": "a.yaml", "create": content, "replace": True, "head_comment": "h"}
    assert ce.delete_entry("a.yaml") == {"path": "a.yaml", "delete": True}


def test_write_all_replaces_and_deletes_in_one_run(tmp_path):
    # The replaced file's old text (a CRLF one that parse-tree cannot read)
    # is not kept: the new one is exactly the value, LF.
    root = _tree(tmp_path / "conf.d", {"r.yaml": "# old\r\ntenants: [\r\n",
                                       "d.yaml": "tenants:\n  t-d: {}\n",
                                       "x.yaml": "tenants:\n  t-x: {}\n"})
    content = ce.map_value([("tenants", ce.map_value([("t-r", ce.map_value([]))]))])
    entries = [ce.create_entry("r.yaml", content, replace=True), ce.delete_entry("d.yaml"),
               ce.delete_entry("gone.yaml")]
    dry = ce.write_all(root, entries, dry_run=True)
    assert [(d["kind"], d["status"], d["content"]) for d in dry] == [
        ("replace", ce.DRY_RUN, "tenants:\n  t-r: {}\n"), ("delete", ce.DRY_RUN, None),
        ("delete", ce.UNCHANGED, None)]
    assert (root / "d.yaml").exists()
    docs = ce.write_all(root, entries)
    assert [(d["kind"], d["status"]) for d in docs] == [
        ("replace", ce.WRITTEN), ("delete", ce.WRITTEN), ("delete", ce.UNCHANGED)]
    assert (root / "r.yaml").read_bytes() == b"tenants:\n  t-r: {}\n"
    assert sorted(p.name for p in root.iterdir()) == ["r.yaml", "x.yaml"]  # no hidden link left
    assert ce.write_all(root, entries[:1])[0]["status"] == ce.UNCHANGED


def test_deleting_the_selected_carrier_is_tree_worsened(tmp_path):
    root = _tree(tmp_path / "conf.d", {"_defaults.yaml": "defaults:\n  cpu: 5\n",
                                       "_defaults.yml": "defaults:\n  - 1\n",
                                       "t.yaml": "tenants:\n  t-a: {}\n"})
    before = _bytes(root)
    with pytest.raises(ce.TreeWorsened):
        ce.write_all(root, [ce.delete_entry("_defaults.yaml")])
    assert _bytes(root) == before


def test_a_delete_not_linked_back_may_have_been_written(tmp_path, monkeypatch):
    # da-guard's answer when an edit's rename fails after a replace was
    # renamed and a delete could not be linked back (cmd/da-guard tests make
    # it happen; here the library reads it).
    root = _tree(tmp_path / "conf.d", {"r.yaml": "tenants: {}\n", "d.yaml": "tenants: {}\n",
                                       "b.yaml": "tenants: {}\n"})
    answer = _answer("caller_error", "partial_write", [
        ("r.yaml", "written", None, None),
        ("d.yaml", "maybe_written", None, "deleted, and not put back"),
        ("b.yaml", "caller_error", "write_failed", "disk full")])
    for f, kind in zip(answer["files"], ["replace", "delete", "edit"]):
        f["kind"] = kind
    monkeypatch.setattr(_lib_parse_tree, "_run", lambda *a, **kw: answer)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.write_all(root, [ce.create_entry("r.yaml", ce.map_value([]), replace=True),
                            ce.delete_entry("d.yaml"), ce.edit_entry("b.yaml", [])])
    assert (exc.value.written, exc.value.maybe_written, exc.value.failed) == (
        ["r.yaml"], ["d.yaml"], "b.yaml")


def test_number_value_sends_an_int_past_18_characters_as_a_float():
    # #3206 10c: a generated `defaults:` number only has to be a number.
    assert ce.number_value(80) == {"int": "80"}
    assert ce.number_value(0.5) == {"float": "0.5"}
    assert ce.number_value(10 ** 20) == {"float": "100000000000000000000.0"}
    # 18 characters, a sign included: the last an int, one more a float
    assert ce.number_value(-(10 ** 16)) == {"int": str(-(10 ** 16))}
    assert ce.number_value(-(10 ** 17)) == {"float": "-100000000000000000.0"}
    for bad in (True, "80", None, float("inf")):
        with pytest.raises(ValueError):
            ce.number_value(bad)


@pytest.mark.parametrize("ch", ["\r", "\x00", "\x1b", "\x7f", "\x85", " ", " ", "﻿"])
def test_comment_text_problem_names_what_da_guard_refuses(ch, tmp_path):
    problem = ce.comment_text_problem(f"a{ch}b")
    assert problem and f"U+{ord(ch):04X}" in problem
    # …and da-guard does refuse it: the two say the same.
    with pytest.raises(ce.EditCallerError):
        ce.write_all(tmp_path, [ce.create_entry("a.yaml", ce.map_value([]),
                                                head_comment=f"a{ch}b")], dry_run=True)


def test_comment_text_problem_lets_tabs_lines_and_text_through():
    assert ce.comment_text_problem("tab\there\nnext — 中文 ⚠️") is None


# ── write_all_or_exit: the one CLI exit every conf.d writer shares (#3206 10c) ──

def test_write_all_or_exit_returns_the_results_when_the_batch_passes(tmp_path, capsys):
    out = ce.write_all_or_exit(tmp_path, [
        ce.create_entry("a.yaml", ce.data_value({"tenants": {"tenant-a": {"k": "1"}}}))],
        tool="t")
    assert [r["status"] for r in out] == [ce.WRITTEN]
    assert (tmp_path / "a.yaml").read_text(encoding="utf-8") == \
        'tenants:\n  tenant-a:\n    k: "1"\n'
    assert capsys.readouterr().err == ""


def test_write_all_or_exit_names_each_refusal_and_writes_none(tmp_path, capsys):
    # One refused (a hidden path no reader reads) ⇒ the other is not written.
    with pytest.raises(SystemExit) as exc:
        ce.write_all_or_exit(tmp_path, [
            ce.create_entry("a.yaml", ce.map_value([("tenants", ce.map_value([]))])),
            ce.create_entry(".hidden.yaml", ce.map_value([]))], tool="my_tool")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert all(ln.startswith("my_tool: ERROR: ") for ln in lines), err
    assert any(".hidden.yaml: not_read:" in ln for ln in lines), err
    assert "nothing was written" in lines[-1], err
    assert list(tmp_path.iterdir()) == []


def test_write_all_or_exit_names_the_files_a_partial_write_left(tmp_path, capsys, monkeypatch):
    def stopped(root, entries, **_kw):
        raise ce.PartialWrite(["a.yaml"], "b.yaml", OSError("disk full"),
                              maybe_written=["b.yaml"])
    monkeypatch.setattr(ce, "write_all", stopped)
    with pytest.raises(SystemExit) as exc:
        ce.write_all_or_exit(tmp_path, [], tool="t")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("t: ERROR: ") and "stopped part-way: disk full" in err, err
    assert "1 file(s) written, 1 may have been written, not rolled back: a.yaml, b.yaml" in err


def test_write_all_or_exit_without_da_guard_is_exit_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        ce.write_all_or_exit(tmp_path, [ce.create_entry("a.yaml", ce.map_value([]))],
                             tool="t", binary=str(tmp_path / "no-such-da-guard"))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "t: ERROR: cannot write the conf.d files through `da-guard write`" in err, err
    assert not (tmp_path / "a.yaml").exists()
