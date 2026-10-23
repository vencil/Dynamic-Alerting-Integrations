"""_lib_confedit：Python 工具改既有 conf.d 檔時把意圖交給 `da-guard edit`（ADR-036 §2，#3206）。

這裡釘的是本模組自己的契約：值的型別規則（讀到的保留型別、使用者打的只有標準
十進位才是數字）、`da-guard.write/v2` 的回答（寫入／拒絕／呼叫錯誤／寫到一半）
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
    assert runs == ["edit"]
    assert [(d["path"], d["status"]) for d in docs] == [
        (f"t-{i}.yaml", ce.WRITTEN) for i in range(3)]
    assert ce.edit_all(root, []) == [] and runs == ["edit"]  # nothing to send, nothing run


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
        ("_defaults.yml", "not_read", "emit")]
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
    return {"schema": ce.SCHEMA, "da_guard_version": "test", "command": "edit",
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


def test_no_answer_to_a_write_may_have_landed_anywhere(tmp_path, monkeypatch):
    # da-guard killed or timed out while writing (no document): any of the
    # batch's renames may have happened, so every file is named as one that
    # MAY have been written. A dry run writes nothing: the error as it is.
    root = _tree(tmp_path / "conf.d", {"a.yaml": "tenants: {}\n", "b.yaml": "tenants: {}\n"})

    def run(*a, **kw):
        raise _lib_parse_tree.ParseTreeError("da-guard edit did not finish within 1s")
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
    with pytest.raises(_lib_parse_tree.ParseTreeError, match="older than this tool"):
        ce.edit(root, "a.yaml", [], dry_run=True)
