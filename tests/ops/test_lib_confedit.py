"""_lib_confedit：Python 工具改既有 conf.d 檔時把意圖交給 `da-guard edit`（ADR-036 §2，#3206）。

這裡釘的是本模組自己的契約：值的型別規則（讀到的保留型別、使用者打的只有標準
十進位才是數字）、`da-guard.write/v1` 的三種回答（寫入／拒絕／呼叫錯誤）各自
怎麼交給工具、以及多檔編輯「全部通過才寫」的兩段式。edit 本身的判定（§4 不變式、
讀回）是 Go 的，測在 pkg/confedit 與 cmd/da-guard。

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


def test_a_failure_after_a_write_names_the_files_written(tmp_path, monkeypatch):
    root = _tree(tmp_path / "conf.d", {
        "a.yaml": "tenants:\n  t-a:\n    cpu: '5'\n",
        "b.yaml": "tenants:\n  t-b:\n    cpu: '6'\n",
    })
    real = ce.edit

    def edit(r, rel, ops, *, dry_run=False, **kw):
        if rel == "b.yaml" and not dry_run:  # stands in for a failed write
            raise ce.EditCallerError({"command": "edit", "path": rel, "status": "caller_error",
                                      "reason": "write_failed", "message": "disk full"})
        return real(r, rel, ops, dry_run=dry_run, **kw)
    monkeypatch.setattr(ce, "edit", edit)
    with pytest.raises(ce.PartialWrite) as exc:
        ce.edit_all(root, [
            ("a.yaml", [ce.unset_op(["tenants", "t-a", "cpu"])]),
            ("b.yaml", [ce.unset_op(["tenants", "t-b", "cpu"])]),
        ])
    assert (exc.value.written, exc.value.failed) == (["a.yaml"], "b.yaml")
    assert "already written: a.yaml" in str(exc.value)
    assert (root / "b.yaml").read_text(encoding="utf-8") == "tenants:\n  t-b:\n    cpu: '6'\n"
