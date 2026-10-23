"""寫入 conf.d 前的確認（ADR-036 §2，#2906 E2）：`_lib_write_check`。

釘住的契約：

1. 產生的文字以 `parse-tree` 單檔模式、在它要寫入的路徑讀回，鍵、結構、每個純量的
   原文、型別與值都與預期相同才放行；第一個不同的位置被指名（鍵路徑、預期與讀到
   的原文／型別）。
2. `text` 是 yaml.v3 的 `Node.Value`：只換引號（`'a'` → `a`）不算改變；`~`／空值
   改寫成 `null`、`!!float 1` 改寫成 `1.0`、字串 `1e3` 未加引號寫出，都是不同的
   原文或型別，整份拒寫。
3. 原檔讀不了、寫入路徑沒有讀取端（隱藏檔、目錄沒選上的 `_defaults` 拼法、不是
   設定檔的副檔名）一律拒寫。
4. 新檔（原檔不存在）的預期是工具要寫的內容；根目錄還不存在時以空樹判。
5. 沒有 da-guard 是 `ParseTreeError`（呼叫端給 exit 2），不是拒寫。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import yaml

import _lib_parse_tree as pt
import _lib_write_check as wc


def _orig(root, rel, text):
    return wc.original(root, rel, text.encode("utf-8"))


def test_a_rewrite_that_reads_back_as_intended_passes(tmp_path):
    text = "tenants:\n  tenant-a:\n    cpu: '70'\n    mem: x\n"
    nodes = _orig(tmp_path, "tenant-a.yaml", text)
    expected = wc.with_change(nodes, lambda t: t["tenants"]["tenant-a"].__setitem__(
        "cpu", wc.scalar("80")))
    new = "tenants:\n  tenant-a:\n    cpu: '80'\n    mem: x\n"
    wc.confirm(tmp_path, "tenant-a.yaml", new, expected)


def test_quoting_style_alone_is_not_a_change(tmp_path):
    nodes = _orig(tmp_path, "t.yaml", "tenants:\n  tenant-a:\n    a: 'x'\n    b: \"y\"\n")
    wc.confirm(tmp_path, "t.yaml", "tenants:\n  tenant-a:\n    a: x\n    b: y\n", nodes)


@pytest.mark.parametrize("old, new, needle", [
    # a null spelled `~` / empty is a different text once PyYAML writes `null`
    ("x: ~\n", "x: null\n", "!!null '~'"),
    ("x:\n", "x: null\n", "!!null ''"),
    # an explicit `!!float 1` is dumped as `1.0`
    ("x: !!float 1\n", "x: 1.0\n", "!!float '1'"),
    # quoted "010" (a string) written unquoted reads as an int
    ("x: \"010\"\n", "x: 010\n", "!!str '010'"),
])
def test_a_scalar_whose_text_or_tag_changes_is_refused(tmp_path, old, new, needle):
    nodes = _orig(tmp_path, "t.yaml", "tenants:\n  tenant-a:\n    " + old.replace("\n", "\n    ").rstrip() + "\n")
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, "t.yaml",
                   "tenants:\n  tenant-a:\n    " + new, nodes)
    assert exc.value.reason == wc.CONTENT_DIFFERS
    assert "tenants.tenant-a.x" in exc.value.detail
    assert needle in exc.value.detail


def test_a_value_that_differs_only_in_value_is_refused():
    # same text and tag, another value: compared too
    e = {"a": {"text": "1", "tag": "!!int", "value": "1"}}
    g = {"a": {"text": "1", "tag": "!!int", "value": "2"}}
    assert "value" in wc.first_difference(e, g)


@pytest.mark.parametrize("expected, got, needle", [
    ({"a": wc.scalar("x")}, {"a": wc.scalar("x"), "b": wc.scalar("y")}, "unexpected ['b']"),
    ({"a": wc.scalar("x"), "b": wc.scalar("y")}, {"a": wc.scalar("x")}, "missing ['b']"),
    ({"a": [wc.scalar("x")]}, {"a": [wc.scalar("x"), wc.scalar("y")]}, "expected 1 items"),
    ({"a": {"b": wc.scalar("x")}}, {"a": wc.scalar("x")}, "expected a mapping"),
    ({"a": [wc.scalar("x")]}, {"a": {"b": wc.scalar("x")}}, "expected a sequence"),
    ({"a": wc.scalar("x")}, {"a": {}}, "expected !!str 'x', read a mapping"),
    ({"a": [wc.scalar("x")]}, {"a": [wc.scalar("z")]}, "at a[0]"),
])
def test_each_kind_of_structural_difference_is_named(expected, got, needle):
    diff = wc.first_difference(expected, got)
    assert diff is not None and needle in diff, diff


def test_key_order_is_not_compared():
    a = {"b": wc.scalar("1"), "a": wc.scalar("2")}
    b = {"a": wc.scalar("2"), "b": wc.scalar("1")}
    assert wc.first_difference(a, b) is None


def test_an_unreadable_original_is_refused(tmp_path):
    with pytest.raises(wc.WriteRefused) as exc:
        _orig(tmp_path, "t.yaml", "tenants:\n  a: 1\n  a: 2\n")
    assert exc.value.reason == wc.ORIGINAL_UNREADABLE
    assert "duplicate_key" in exc.value.detail


def test_generated_text_parse_tree_cannot_read_is_refused(tmp_path):
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, "t.yaml", "a: !!set {x: null}\n", {"a": {}})
    assert exc.value.reason == wc.TEXT_UNREADABLE


@pytest.mark.parametrize("rel, setup", [
    (".hidden.yaml", {}),
    ("t.json", {}),
    ("sub/.x/t.yaml", {}),
    # `.yaml` wins over `.yml`: a `_defaults.yml` beside it is read by no one
    ("_defaults.yml", {"_defaults.yaml": "defaults: {}\n"}),
])
def test_a_path_no_reader_reads_is_refused(tmp_path, rel, setup):
    for name, body in setup.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, rel, "defaults: {}\n", {"defaults": {}})
    assert exc.value.reason == wc.NOT_READ
    with pytest.raises(wc.WriteRefused) as exc:
        wc.original(tmp_path, rel, b"defaults: {}\n")
    assert exc.value.reason == wc.NOT_READ


@pytest.mark.parametrize("rel", ["team", "team/", "./team", "a/../team"])
def test_a_directory_target_is_named_whatever_its_spelling(tmp_path, rel):
    """#3206: parse-tree holds the entry under the cleaned name, so `team/`,
    `./team` and `a/../team` get the directory's own reason, not the
    generic one."""
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, rel, "tenants: {}\n", {"tenants": {}})
    assert exc.value.reason == wc.NOT_READ
    assert "it is a directory (parse-tree role dir)" in exc.value.detail


@pytest.mark.parametrize("rel", ["./t.yaml", "sub//t.yaml", "sub/../t.yaml"])
def test_an_uncleaned_file_target_is_judged_under_its_clean_name(tmp_path, rel):
    """A spelling Go's `--path` cleans is the same file: it is read, not
    refused as a path no reader reads."""
    (tmp_path / "sub").mkdir()
    wc.confirm(tmp_path, rel, "tenants: {}\n", {"tenants": {}})
    assert wc.original(tmp_path, rel, b"tenants: {}\n") == {"tenants": {}}


@pytest.mark.parametrize("rel", ["", ".", "./", "..", "../t.yaml", "a/../../t.yaml",
                                 "/abs/t.yaml", "a\x00b.yaml", "bad\udcff.yaml"])
def test_a_target_naming_no_file_inside_the_root_is_refused_before_da_guard(
        tmp_path, monkeypatch, rel):
    """Empty, `.`, absolute or leaving the root: a clean WriteRefused, decided
    before any da-guard run — never parse-tree's caller error read back as
    a ParseTreeError ("no usable da-guard")."""
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "nope"))
    monkeypatch.setenv("PATH", str(tmp_path))
    for call in (lambda: wc.confirm(tmp_path, rel, "tenants: {}\n", {"tenants": {}}),
                 lambda: wc.original(tmp_path, rel, b"tenants: {}\n")):
        with pytest.raises(wc.WriteRefused) as exc:
            call()
        assert exc.value.reason == wc.NOT_READ
        assert exc.value.detail.startswith(f"the write target {rel!r} "), exc.value.detail
    with pytest.raises(wc.WriteRefused):
        with wc.confirm_tree(tmp_path / "out", [(rel, "tenants: {}\n", {"tenants": {}})]):
            pass


def test_confirm_tree_stages_nothing_outside_itself_for_an_absolute_target(tmp_path):
    """An absolute target joined to the staging directory would be written
    where it names; it is refused before anything is staged."""
    outside = tmp_path / "outside.yaml"
    with pytest.raises(wc.WriteRefused):
        with wc.confirm_tree(tmp_path / "out", [(str(outside), "tenants: {}\n", {"tenants": {}})]):
            pass
    assert not outside.exists()


# Spellings both sides judge: Go's `cleanRelPath` (through `parse-tree
# --path`) and `_clean_target`. A NUL byte is not here: no argv carries it.
_TARGET_SPELLINGS = [
    "t.yaml", "./t.yaml", "sub/t.yaml", "sub//t.yaml", "sub/./t.yaml", "sub/../t.yaml",
    "sub/", "a/b/../../t.yaml", ".", "./", "..", "../t.yaml", "a/../../t.yaml",
    "/t.yaml", "//t.yaml", "/abs/../t.yaml", "bad\udcff.yaml", "sub/bad\udcff/t.yaml",
    "é.yaml",
]
_GO_REFUSALS = {"must be a path relative": wc.TARGET_NOT_RELATIVE,
                "must name a file inside": wc.TARGET_NOT_INSIDE,
                "is not valid UTF-8": wc.TARGET_NOT_UTF8}


def _da_guard() -> str:
    binary = os.environ.get("DA_GUARD_BINARY") or shutil.which("da-guard")
    assert binary, "no da-guard ($DA_GUARD_BINARY / PATH): this file's checks need one"
    return binary


@pytest.mark.parametrize("rel", _TARGET_SPELLINGS)
def test_python_and_go_refuse_the_same_write_targets(tmp_path, rel):
    """`_clean_target` is Go's `cleanRelPath` in Python: for each spelling
    both refuse for the same reason, or both accept it under one cleaned
    name — so the two rule sets cannot drift apart. (The empty target is
    no `--path` at all to Go; the NUL byte reaches it through no argv:
    both are pinned on the Python side alone.)"""
    (tmp_path / "sub").mkdir()
    r = subprocess.run([_da_guard(), "parse-tree", "--config-dir", str(tmp_path),
                        "--path", os.fsencode(rel), "--input", "-"],
                       input=b"tenants: {}\n", capture_output=True, timeout=60, check=False)
    go_why = next((why for needle, why in _GO_REFUSALS.items()
                   if needle in r.stderr.decode("utf-8", "replace")), None)
    try:
        py_clean, py_why = wc._clean_target(rel), None
    except wc.WriteRefused as exc:
        py_clean = None
        py_why = next(w for w in _GO_REFUSALS.values() if w in exc.detail)
    assert go_why == py_why, (r.returncode, r.stderr, py_why)
    if py_why is None:
        assert r.returncode == 0, r.stderr
        assert [e["path"] for e in json.loads(r.stdout)["entries"]] == [py_clean]
    else:
        assert r.returncode == 2, r.stderr


def test_a_nul_byte_target_is_refused_not_a_value_error(tmp_path):
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, "a\x00b.yaml", "tenants: {}\n", {"tenants": {}})
    assert wc.TARGET_NUL in exc.value.detail


def test_place_takes_any_spelling_of_a_staged_name(tmp_path):
    """`confirm_tree` stages the cleaned name; `place` finds it whatever
    spelling the caller passes."""
    out = tmp_path / "out"
    items = [("sub/../a.yaml", "tenants: {}\n", {"tenants": {}})]
    with wc.confirm_tree(out, items) as staged:
        out.mkdir()
        staged.place("sub/../a.yaml", out / "a.yaml")
    assert (out / "a.yaml").read_text(encoding="utf-8") == "tenants: {}\n"


def test_two_spellings_of_one_target_are_refused_naming_both(tmp_path):
    """Staged one over the other, only the last text would be judged and
    placed: refused, naming both spellings."""
    items = [("a.yaml", "tenants: {}\n", {"tenants": {}}),
             ("./a.yaml", "tenants:\n  x: {}\n", {"tenants": {"x": {}}})]
    with pytest.raises(wc.WriteRefused) as exc:
        with wc.confirm_tree(tmp_path / "out", items):
            pass
    assert "'a.yaml' and './a.yaml' are both 'a.yaml'" in exc.value.detail


def test_a_new_file_is_judged_against_what_the_tool_means(tmp_path):
    assert wc.original(tmp_path, "tenant-a.yaml", None) is None
    doc = {"tenants": {"tenant-a": {"cpu": "70", "on": True, "n": 3, "z": None}}}
    text = yaml.safe_dump(doc, sort_keys=False)
    wc.confirm(tmp_path, "tenant-a.yaml", text, wc.from_python(doc))
    # a str PyYAML writes plain and yaml.v3 reads as a number is refused
    doc = {"tenants": {"tenant-a": {"cpu": "1e3"}}}
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm(tmp_path, "tenant-a.yaml", yaml.safe_dump(doc), wc.from_python(doc))
    assert "expected !!str '1e3', read !!float '1e3'" in exc.value.detail


def test_a_missing_root_is_an_empty_tree(tmp_path):
    root = tmp_path / "not-yet"
    wc.confirm(root, "_defaults.yaml", "defaults:\n  cpu: 80\n",
               {"defaults": {"cpu": wc.AnyNumber("80")}})
    assert not root.exists()


def test_any_number_takes_int_and_float_and_nothing_else():
    num = wc.AnyNumber("1e3")
    assert wc.first_difference(num, {"text": "1e3", "tag": "!!float", "value": "1000"}) is None
    assert wc.first_difference(wc.AnyNumber("8"), {"text": "8", "tag": "!!int", "value": "8"}) is None
    assert "a number" in wc.first_difference(num, wc.scalar("1e3"))
    assert wc.first_difference(num, {"text": "1000", "tag": "!!float", "value": "1000"})


def test_confirm_all_stops_at_the_first_refusal(tmp_path):
    good = ("a.yaml", "tenants: {}\n", {"tenants": {}})
    bad = ("b.yaml", "tenants: 1\n", {"tenants": {}})
    with pytest.raises(wc.WriteRefused) as exc:
        wc.confirm_all(tmp_path, [good, bad, good])
    assert exc.value.rel == "b.yaml"


@pytest.mark.parametrize("value", [80.0, 70.5, -0.25, 1e20, 1e-7, float("inf")])
def test_a_float_is_expected_as_pyyaml_writes_it_and_yaml_v3_reads_it(tmp_path, value):
    doc = {"tenants": {"tenant-a": {"cpu": value}}}
    wc.confirm(tmp_path, "t.yaml", yaml.safe_dump(doc), wc.from_python(doc))


def test_scalar_refuses_another_type():
    with pytest.raises(TypeError):
        wc.scalar(b"x")


def test_no_da_guard_is_a_parse_tree_error_not_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "nope"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(pt.ParseTreeError) as exc:
        wc.confirm(tmp_path, "t.yaml", "a: 1\n", {"a": wc.scalar(1)})
    assert not isinstance(exc.value, wc.WriteRefused)


def test_keep_null_spelling_writes_an_untouched_null_as_it_was_read(tmp_path):
    """Text production only: a null the rewrite does not touch keeps its
    spelling (`~`, empty, `Null`), so the confirmation reads the same text."""
    from _lib_io import strict_load_for_rewrite
    from _lib_yaml_keys import dump_for_rewrite
    text = "tenants:\n  tenant-a:\n    a: ~\n    b:\n    c: Null\n    d: [~, 1]\n    e: '1'\n"
    nodes = _orig(tmp_path, "t.yaml", text)
    doc = strict_load_for_rewrite(text)
    plain = dump_for_rewrite(doc, sort_keys=False)
    with pytest.raises(wc.WriteRefused):
        wc.confirm(tmp_path, "t.yaml", plain, nodes)
    kept = dump_for_rewrite(wc.keep_null_spelling(doc, nodes), sort_keys=False)
    wc.confirm(tmp_path, "t.yaml", kept, nodes)
