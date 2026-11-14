"""#3446: `_lib_input` — what a caller-given value selects, and its bytes.

The CLI-level rows (an empty / unlistable directory, an absent default, for
every declared flag) are made from the declarations in
``test_caller_paths.py``; the listing audit is ``test_input_listing_audit.py``.
These pin the module's own answers.
"""
from __future__ import annotations

import argparse
import errno
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
import _lib_input  # noqa: E402
from _lib_exitcodes import CallerError  # noqa: E402

from _platform_fs import has_posix_modes, symlink_or_skip  # noqa: E402


def _parser(**kw):
    p = argparse.ArgumentParser()
    action = _lib_input.add_input(p, "--d", **{"select": _lib_input.TopLevel("*.json"), **kw})
    return p, action


def _select(argv, **kw):
    p, _ = _parser(**kw)
    return _lib_input.selected(p.parse_args(argv), "d", lang="en")


def test_the_declaration_is_on_the_action():
    _, action = _parser(default="x", absent_default=_lib_input.ABSENT_EMPTY)
    assert action.input_spec == _lib_input.InputSpec(
        "--d", "d", _lib_input.TopLevel("*.json"), "allowed", "x", "empty")


def test_top_level_selects_matching_names_sorted(tmp_path):
    for name in ("b.json", "a.json", ".h.json", "c.txt", "D.JSON"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.json").write_text("{}", encoding="utf-8")
    got = _select(["--d", str(tmp_path)])
    assert got == [tmp_path / ".h.json", tmp_path / "a.json", tmp_path / "b.json"]


def test_an_empty_directory_is_as_declared(tmp_path):
    assert _select(["--d", str(tmp_path)]) == []
    with pytest.raises(CallerError) as exc:
        _select(["--d", str(tmp_path)], empty=_lib_input.EMPTY_REFUSED)
    assert exc.value.reason == "input_empty"
    assert str(exc.value) == f"--d {tmp_path}: selects nothing (*.json)"


@pytest.mark.parametrize("make, reason", [("missing", "dir_not_found"),
                                          ("file", "dir_not_found")])
def test_a_given_value_that_is_no_directory_is_refused(tmp_path, make, reason):
    path = tmp_path / "x"
    if make == "file":
        path.write_text("", encoding="utf-8")
    with pytest.raises(CallerError) as exc:
        _select(["--d", str(path)])
    assert exc.value.reason == reason


@pytest.mark.parametrize("absent, selects", [(_lib_input.ABSENT_EMPTY, []),
                                             (_lib_input.ABSENT_REFUSED, None)])
def test_an_absent_default_is_as_declared(tmp_path, absent, selects):
    kw = {"default": str(tmp_path / "nope"), "absent_default": absent}
    if selects is None:
        with pytest.raises(CallerError) as exc:
            _select([], **kw)
        assert exc.value.reason == "dir_not_found"
    else:
        assert _select([], **kw) == selects


def test_a_given_value_equal_to_the_default_is_still_given(tmp_path):
    """Given-ness is carried by the value, not by identity with the default."""
    missing = str(tmp_path / "nope")
    with pytest.raises(CallerError):
        _select(["--d", missing], default=missing, absent_default=_lib_input.ABSENT_EMPTY)


@pytest.mark.skipif(os.name == "nt" or not has_posix_modes() or os.geteuid() == 0,
                    reason="a mode bit must deny the listing (root lists anything)")
def test_a_directory_it_cannot_list_is_refused(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o311)
    try:
        for kw in ({}, {"default": str(d), "absent_default": _lib_input.ABSENT_EMPTY}):
            with pytest.raises(CallerError) as exc:
                _select([] if kw else ["--d", str(d)], **kw)
            assert exc.value.reason == "dir_not_accessible"
            assert str(exc.value) == f"--d {d}: {os.strerror(errno.EACCES)}"
    finally:
        d.chmod(0o755)


def test_a_listing_that_fails_is_refused_not_empty(tmp_path, monkeypatch):
    """The listing's OSError (whatever denies it) is dir_not_accessible —
    measured without a mode bit, which root ignores."""
    def denied(_d):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(tmp_path))
    monkeypatch.setattr(_lib_input, "_list", denied)
    with pytest.raises(CallerError) as exc:
        _select(["--d", str(tmp_path)])
    assert exc.value.reason == "dir_not_accessible"
    assert str(exc.value) == f"--d {tmp_path}: {os.strerror(errno.EACCES)}"
    with pytest.raises(CallerError) as exc:
        _select([], default=str(tmp_path), absent_default=_lib_input.ABSENT_EMPTY)
    assert exc.value.reason == "dir_not_accessible"


def test_list_raises_never_answers_empty(tmp_path):
    assert _lib_input._list(tmp_path) == []
    with pytest.raises(FileNotFoundError):
        _lib_input._list(tmp_path / "nope")
    (tmp_path / "b").mkdir()
    (tmp_path / "a").write_text("", encoding="utf-8")
    assert _lib_input._list(tmp_path) == ["a", "b"]


def test_da_guard_lists_selects_the_directory_itself(tmp_path, monkeypatch):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--src", select=_lib_input.DA_GUARD_LISTS, required=True)

    def no_listing(_d):
        raise AssertionError("a directory da-guard lists is not listed here")
    monkeypatch.setattr(_lib_input, "_list", no_listing)
    assert _lib_input.selected(p.parse_args(["--src", str(tmp_path)]), "src",
                               lang="en") == [tmp_path]


def test_a_flag_with_no_value_selects_nothing():
    p, _ = _parser()
    assert _lib_input.selected(p.parse_args([]), "d", lang="en") == []


def test_an_undeclared_dest_is_a_programming_error():
    with pytest.raises(TypeError):
        _lib_input.selected(argparse.Namespace(d="x"), "d", lang="en")


@pytest.mark.parametrize("kw, error", [
    ({"type": str}, TypeError), ({"nargs": "+"}, TypeError),
    ({"select": "*.json"}, TypeError), ({"empty": "maybe"}, ValueError),
    ({"default": "x"}, ValueError), ({"absent_default": "empty"}, ValueError),
    ({"default": "x", "absent_default": "skip"}, ValueError),
])
def test_add_input_refuses_a_declaration_it_cannot_honour(kw, error):
    with pytest.raises(error):
        _lib_input.add_input(argparse.ArgumentParser(), "--d",
                             **{"select": _lib_input.TopLevel("*"), **kw})


def test_read_bytes(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"\x00a\xff")
    assert _lib_input.read_bytes(f, flag="--f", lang="en") == b"\x00a\xff"


@pytest.mark.parametrize("lang, said", [("en", os.strerror(errno.ENOENT)), ("zh", "不存在")])
def test_read_bytes_refuses_a_missing_file(tmp_path, lang, said):
    with pytest.raises(CallerError) as exc:
        _lib_input.read_bytes(tmp_path / "gone", flag="--f", lang=lang)
    assert exc.value.reason == "file_unreadable"
    assert str(exc.value) == f"--f {tmp_path / 'gone'}: {said}"


def test_read_bytes_refuses_a_directory(tmp_path):
    with pytest.raises(CallerError) as exc:
        _lib_input.read_bytes(tmp_path, flag=None, lang="en")
    assert exc.value.reason == "file_unreadable"
    assert str(exc.value) == f"{tmp_path}: {os.strerror(errno.EISDIR)}"


# ── #3462 / #3548 ────────────────────────────────────────────────────────

def test_a_default_that_is_there_but_no_directory_is_refused_even_if_absent_reads_empty(tmp_path):
    """#3548: `absent` is nothing there. A regular file at the default was
    read as an empty state directory, and state-reconcile overwrote the
    manifest with no state, rc 0."""
    there = tmp_path / "state"
    there.write_text("x", encoding="utf-8")
    with pytest.raises(CallerError) as exc:
        _select([], default=str(there), absent_default=_lib_input.ABSENT_EMPTY)
    assert exc.value.reason == "dir_not_found"
    assert str(exc.value) == f"--d {there}: not a directory"


def test_a_default_under_a_regular_file_is_refused(tmp_path):
    """ENOTDIR on a parent (`.da` is a regular file, the default is
    `.da/state`): something is in the way, not nothing — refused (#3548
    review: it was read as no state and the manifest overwritten)."""
    (tmp_path / "da").write_text("x", encoding="utf-8")
    with pytest.raises(CallerError):
        _select([], default=str(tmp_path / "da" / "state"),
                absent_default=_lib_input.ABSENT_EMPTY)


def test_a_default_that_is_a_dangling_link_is_refused(tmp_path):
    """stat calls a dangling link ENOENT; `ls` lists it — not absent."""
    link = tmp_path / "state"
    symlink_or_skip(tmp_path / "nowhere", link)
    with pytest.raises(CallerError):
        _select([], default=str(link), absent_default=_lib_input.ABSENT_EMPTY)


def test_a_default_under_a_dangling_link_is_refused(tmp_path):
    """`.da` → nowhere: `.da/state` is ENOENT to lstat, yet `.da` is listed."""
    symlink_or_skip(tmp_path / "nowhere", tmp_path / "da")
    with pytest.raises(CallerError):
        _select([], default=str(tmp_path / "da" / "state"),
                absent_default=_lib_input.ABSENT_EMPTY)


def test_a_default_under_a_link_to_a_directory_is_absent(tmp_path):
    (tmp_path / "real").mkdir()
    symlink_or_skip(tmp_path / "real", tmp_path / "da")
    assert _select([], default=str(tmp_path / "da" / "state"),
                   absent_default=_lib_input.ABSENT_EMPTY) == []


def test_a_default_whose_parent_is_missing_is_absent(tmp_path):
    assert _select([], default=str(tmp_path / "da" / "state"),
                   absent_default=_lib_input.ABSENT_EMPTY) == []


@pytest.mark.parametrize("default", ["rule-packs/", "a/b/"])
def test_a_default_with_a_trailing_separator_is_refused(default):
    """The default is shown in messages as typed; one spelling of it."""
    with pytest.raises(ValueError):
        _parser(default=default, absent_default=_lib_input.ABSENT_EMPTY)


def test_a_selector_judges_the_one_listing(tmp_path, monkeypatch):
    """`select` is any declaration with choose(directory, names) / wanted:
    `selected` lists once and hands the names over — the selector never
    lists (`RULE_PACKS`, the rule-pack declaration, is one)."""
    import _lib_consumer_read
    (tmp_path / "rule-pack-a.yaml").write_text("groups: []\n", encoding="utf-8")
    (tmp_path / "threshold-registry.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    calls = []
    real = _lib_input._list
    monkeypatch.setattr(_lib_input, "_list", lambda d: calls.append(d) or real(d))
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--rp", select=_lib_consumer_read.RULE_PACKS,
                         empty=_lib_input.EMPTY_REFUSED)
    got = _lib_input.selected(p.parse_args(["--rp", str(tmp_path)]), "rp", lang="en")
    assert got == [tmp_path / "rule-pack-a.yaml"] and len(calls) == 1
    (tmp_path / "rule-pack-a.yaml").unlink()
    with pytest.raises(CallerError) as exc:
        _lib_input.selected(p.parse_args(["--rp", str(tmp_path)]), "rp", lang="en")
    assert exc.value.reason == "input_empty"
    assert str(exc.value) == (f"--rp {tmp_path}: selects nothing "
                              f"({' / '.join(_lib_consumer_read.RULE_PACKS.rule_files)})")
    (tmp_path / "other.yaml").write_text("x: 1\n", encoding="utf-8")
    with pytest.raises(_lib_consumer_read.SourcesUnlisted):
        _lib_input.selected(p.parse_args(["--rp", str(tmp_path)]), "rp", lang="en")


def test_a_given_value_with_nothing_there_is_as_declared_by_missing(tmp_path):
    """#3693: ``missing`` — a value the caller GAVE with nothing at it is
    refused by default (``dir_not_found``); ``ABSENT_EMPTY`` selects nothing
    (config-diff's base side: a first import)."""
    gone = str(tmp_path / "gone")
    with pytest.raises(CallerError) as exc:
        _select(["--d", gone])
    assert exc.value.reason == "dir_not_found"
    assert _select(["--d", gone], missing=_lib_input.ABSENT_EMPTY) == []


@pytest.mark.parametrize("make", ["file", "dangling", "under_a_file"])
def test_missing_empty_is_only_nothing_there(tmp_path, make):
    """#3693: ``missing=ABSENT_EMPTY`` is nothing at the path, never
    something that is no directory — a file, a dangling link, a path
    under a regular file — refused as before."""
    if make == "file":
        target = tmp_path / "f"
        target.write_text("x", encoding="utf-8")
    elif make == "dangling":
        target = tmp_path / "link"
        symlink_or_skip(tmp_path / "nowhere", target)
    else:
        (tmp_path / "f").write_text("x", encoding="utf-8")
        target = tmp_path / "f" / "d"
    with pytest.raises(CallerError) as exc:
        _select(["--d", str(target)], missing=_lib_input.ABSENT_EMPTY)
    assert exc.value.reason.startswith("dir_"), exc.value.reason


def test_absent_reads_what_selected_judged(tmp_path):
    """#3721: `absent` is the one answer to "was it there" — set by
    `selected`, read after; never a second stat."""
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--d", select=_lib_input.DA_GUARD_LISTS,
                         missing=_lib_input.ABSENT_EMPTY)
    _lib_input.add_input(p, "--s", select=_lib_input.TopLevel("*.json"),
                         default=str(tmp_path / "default"),
                         absent_default=_lib_input.ABSENT_EMPTY)
    args = p.parse_args(["--d", str(tmp_path / "gone")])
    with pytest.raises(TypeError):
        _lib_input.absent(args, "d")  # nothing judged yet: no answer to read
    assert _lib_input.selected(args, "d", lang="en") == []
    assert _lib_input.absent(args, "d") is True
    assert _lib_input.selected(args, "s", lang="en") == []
    assert _lib_input.absent(args, "s") is True
    (tmp_path / "gone").mkdir()
    (tmp_path / "default").mkdir()
    args = p.parse_args(["--d", str(tmp_path / "gone")])
    _lib_input.selected(args, "d", lang="en")
    _lib_input.selected(args, "s", lang="en")
    assert (_lib_input.absent(args, "d"), _lib_input.absent(args, "s")) == (False, False)
    # read, not judged again: the directory going away later changes nothing
    (tmp_path / "gone").rmdir()
    assert _lib_input.absent(args, "d") is False
    assert _lib_input.absent(argparse.Namespace(d=None), "d") is True
    with pytest.raises(TypeError):
        _lib_input.absent(argparse.Namespace(d="x"), "d")


def test_absent_does_not_leak_through_the_shared_default(tmp_path):
    """#3721: argparse hands every parse the same default object; what one
    run judged must not answer for the next parse."""
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--s", select=_lib_input.TopLevel("*.json"),
                         default=str(tmp_path / "default"),
                         absent_default=_lib_input.ABSENT_EMPTY)
    first = p.parse_args([])
    _lib_input.selected(first, "s", lang="en")
    assert _lib_input.absent(first, "s") is True
    with pytest.raises(TypeError):
        _lib_input.absent(p.parse_args([]), "s")  # a new parse: not judged yet


def test_missing_is_declared_and_checked():
    _, action = _parser(missing=_lib_input.ABSENT_EMPTY)
    assert action.input_spec.missing == _lib_input.ABSENT_EMPTY
    assert _parser()[1].input_spec.missing == _lib_input.ABSENT_REFUSED
    with pytest.raises(ValueError):
        _parser(missing="maybe")
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f",
                             decode=_lib_input.Document("json", dict),
                             missing=_lib_input.ABSENT_EMPTY)


class _Root:
    """A stand-in for `_lib_parse_tree.Entry` (the fields `require_listed` reads)."""

    def __init__(self, role, problem=None):
        self.role = role
        self.problem = problem

    @property
    def unlistable_dir(self):
        return self.role == "unlistable_dir"


def test_require_listed_refuses_a_root_da_guard_cannot_list(tmp_path):
    """#3693: the one refusal of a tree whose root da-guard reports
    `unlistable_dir` — never read as one with no config file."""
    why = ("read_failure", "the directory cannot be listed; nothing under it is read")
    for root in (None, _Root("dir")):
        assert _lib_input.require_listed("--d", tmp_path, root, lang="en") is None
    with pytest.raises(CallerError) as exc:
        _lib_input.require_listed("--d", tmp_path, _Root("unlistable_dir", why), lang="en")
    assert exc.value.reason == "dir_not_accessible"
    assert str(exc.value) == (f"--d {tmp_path}: cannot list the directory (da-guard: "
                              "the directory cannot be listed; nothing under it is read)")
    with pytest.raises(CallerError) as exc:
        _lib_input.require_listed("--d", tmp_path, _Root("unlistable_dir"), lang="zh")
    assert str(exc.value) == f"--d {tmp_path}: 無法列出目錄內容（da-guard: unlistable_dir）"


def test_require_selection_judges_a_selection_made_elsewhere(tmp_path):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--src", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED)
    _lib_input.add_input(p, "--ok", select=_lib_input.DA_GUARD_LISTS)
    args = p.parse_args(["--src", str(tmp_path), "--ok", str(tmp_path)])
    assert _lib_input.require_selection(args, "src", ["x"], wanted="*.yaml", lang="en") == ["x"]
    assert _lib_input.require_selection(args, "ok", [], wanted="*.yaml", lang="en") == []
    with pytest.raises(CallerError) as exc:
        _lib_input.require_selection(args, "src", [], wanted="*.yaml", lang="zh")
    assert exc.value.reason == "input_empty"
    assert str(exc.value) == f"--src {tmp_path}: 沒有選到任何項目（*.yaml）"
    with pytest.raises(TypeError):
        _lib_input.require_selection(argparse.Namespace(src="x"), "src", [], wanted="w",
                                     lang="en")


@pytest.mark.parametrize("where", ["path", "ancestor"])
def test_a_stat_that_fails_is_not_nothing_there(tmp_path, monkeypatch, where):
    """An lstat that fails with anything but ENOENT — at the path, or at the
    nearest ancestor that is there — is something in the way: refused."""
    default = tmp_path / "da" / "state"
    real = os.lstat

    def lstat(p, *a, **k):
        if (where == "path" and pathlib.Path(p) == default) or \
           (where == "ancestor" and pathlib.Path(p) == tmp_path):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(p))
        if where == "path":
            return real(p, *a, **k)
        if pathlib.Path(p) in (default, default.parent):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(_lib_input.os, "lstat", lstat)
    assert _lib_input._nothing_at(default) is False


# ── #3445: a file whose content is read (decode=) ────────────────────────

def _doc_args(path, doc):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--f", decode=doc)
    return p.parse_args(["--f", str(path)])


def _read(tmp_path, content, doc, lang="en"):
    path = tmp_path / "in"
    path.write_bytes(content)
    return path, _lib_input.document(_doc_args(path, doc), "f", lang=lang)


def test_a_document_declaration_is_on_the_action():
    p = argparse.ArgumentParser()
    doc = _lib_input.Document("json", dict)
    action = _lib_input.add_input(p, "--f", "-f", decode=doc)
    assert action.input_spec == _lib_input.InputSpec("--f", "f", None, "allowed", None, None, doc)
    assert p.parse_args(["-f", "x"]).f == "x"


@pytest.mark.parametrize("kw", [
    {}, {"select": _lib_input.TopLevel("*"), "decode": _lib_input.Document("json", dict)},
    {"decode": ("json", dict)}, {"decode": _lib_input.Document("toml", dict)},
])
def test_add_input_takes_exactly_one_of_select_and_decode(kw):
    with pytest.raises(TypeError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f", **kw)


@pytest.mark.parametrize("kw", [
    {"empty": "refused"}, {"default": "x", "absent_default": "empty"},
])
def test_a_document_takes_no_selection_keywords(kw):
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f",
                             decode=_lib_input.Document("json", dict), **kw)


def test_empty_is_is_for_yaml_only():
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f",
                             decode=_lib_input.Document("json", dict, empty_is=dict))


@pytest.mark.parametrize("fmt, content, data", [
    ("json", b'{"a": 1}', {"a": 1}), ("yaml", b"a: 1\n", {"a": 1}),
    ("yaml", "﻿a: 1\n".encode(), {"a": 1}), ("text", b"x,y\n", "x,y\n"),
])
def test_a_document_is_its_parsed_content(tmp_path, fmt, content, data):
    top = str if fmt == "text" else dict
    assert _read(tmp_path, content, _lib_input.Document(fmt, top))[1] == data


@pytest.mark.parametrize("fmt, content, lang, said", [
    ("json", b"\xff", "en", "not valid UTF-8 (byte 0)"),
    ("text", b"ab\xfe", "zh", "不是有效的 UTF-8（第 2 個位元組）"),
    ("json", b"{bad", "en",
     "not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
    ("json", b"[]", "en", "the top level is not a mapping (got a list)"),
    ("json", b"true", "en", "the top level is not a mapping (got a boolean)"),
    ("yaml", b"", "en", "the top level is not a mapping (got an empty document)"),
    ("yaml", b"- a\n", "zh", "頂層不是映射（實為清單）"),
])
def test_content_it_cannot_read_is_refused_named(tmp_path, fmt, content, lang, said):
    top = str if fmt == "text" else dict
    with pytest.raises(CallerError) as exc:
        _read(tmp_path, content, _lib_input.Document(fmt, top), lang)
    assert exc.value.reason == "file_unreadable"
    assert str(exc.value) == f"--f {tmp_path / 'in'}: {said}"


def test_a_yaml_syntax_error_is_one_line_naming_the_file(tmp_path):
    with pytest.raises(CallerError) as exc:
        _read(tmp_path, b"a: [\n", _lib_input.Document("yaml", dict))
    assert "\n" not in str(exc.value)
    assert str(exc.value).startswith(f"--f {tmp_path / 'in'}: not valid YAML: ")
    assert f'in "{tmp_path / "in"}"' in str(exc.value)


def test_an_empty_yaml_document_is_empty_only_as_declared(tmp_path):
    assert _read(tmp_path, b"~\n", _lib_input.Document("yaml", dict, empty_is=dict))[1] == {}


def test_a_tuple_of_tops(tmp_path):
    doc = _lib_input.Document("json", (list, dict))
    assert _read(tmp_path, b"[]", doc)[1] == []
    with pytest.raises(CallerError) as exc:
        _read(tmp_path, b"7", doc)
    assert str(exc.value).endswith("the top level is not a list or a mapping (got a number)")


def test_a_document_path_is_judged_as_a_file(tmp_path):
    with pytest.raises(CallerError) as exc:
        _lib_input.document(_doc_args(tmp_path, _lib_input.Document("json", dict)), "f",
                            lang="en")
    assert exc.value.reason == "file_not_found"
    assert str(exc.value) == f"--f {tmp_path}: not a regular file"


def test_a_document_not_given_is_none_and_kinds_do_not_mix():
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--f", decode=_lib_input.Document("json", dict))
    _lib_input.add_input(p, "--d", select=_lib_input.TopLevel("*"))
    args = p.parse_args(["--f", "x", "--d", "y"])
    assert _lib_input.document(p.parse_args([]), "f", lang="en") is None
    with pytest.raises(TypeError):
        _lib_input.selected(args, "f", lang="en")
    with pytest.raises(TypeError):
        _lib_input.document(args, "d", lang="en")


def test_decode_bytes_keeps_the_decoders_cause():
    with pytest.raises(_lib_input.DocumentError) as exc:
        _lib_input.decode_bytes(b"\xff", fmt="yaml", name="n")
    assert isinstance(exc.value.cause, UnicodeDecodeError)
    with pytest.raises(ValueError):
        _lib_input.decode_bytes(b"", fmt="toml", name="n")


def test_refuse_top_is_the_one_wording():
    exc = _lib_input.refuse_top("--x f.yaml", "scalar", dict, lang="en")
    assert (str(exc), exc.reason) == ("--x f.yaml: the top level is not a mapping (got a scalar)",
                                      "file_unreadable")


def test_the_module_reads_json_without_pyyaml(tmp_path):
    """Standard library only: importing `_lib_input` and reading a JSON or
    text document pulls in no PyYAML (only the YAML branch imports it)."""
    import subprocess
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    probe = (
        "import builtins, sys, argparse\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] == 'yaml':\n"
        "        raise ImportError('yaml is not installed here')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        f"sys.path.insert(0, {str(REPO / 'scripts' / 'tools')!r})\n"
        "import _lib_input as li\n"
        "assert '_lib_exitcodes' not in sys.modules\n"
        "p = argparse.ArgumentParser()\n"
        "li.add_input(p, '--f', decode=li.Document('json', dict))\n"
        f"print(li.document(p.parse_args(['--f', {str(tmp_path / 'a.json')!r}]), 'f', lang='en'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert (proc.returncode, proc.stdout.strip()) == (0, "{}"), proc.stderr


# ── #3445 review: a parser error that is not the parser's own class ─────

@pytest.mark.parametrize("fmt", ["json", "yaml"])
def test_a_recursion_error_while_parsing_is_a_document_error(fmt, monkeypatch):
    """Nesting past the recursion limit raised RecursionError past the
    decoder's own error class: a traceback. Whether a given depth does is
    the Python build's business, so the error is forced here."""
    import json

    import yaml

    def deep(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(json if fmt == "json" else yaml, "loads" if fmt == "json" else "load",
                        deep)
    with pytest.raises(_lib_input.DocumentError) as exc:
        _lib_input.decode_bytes(b"[1]", fmt=fmt, name="x")
    assert str(exc.value) == f"not valid {fmt.upper()}: nested too deeply to read"


def test_a_value_safeloader_cannot_build_is_a_document_error():
    with pytest.raises(_lib_input.DocumentError) as exc:
        _lib_input.decode_bytes(b"a: 2020-99-99\n", fmt="yaml", name="x")
    assert str(exc.value).startswith("not valid YAML: ")


def test_an_empty_is_that_is_not_a_declared_top_is_refused_at_declaration():
    """#3445 review: `Document("yaml", list, empty_is=dict)` read an empty
    file as `{}` past the top-level check."""
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f",
                             decode=_lib_input.Document("yaml", list, empty_is=dict))


# ── #3662: a csv document ───────────────────────────────────────────────

@pytest.mark.parametrize("doc", [
    _lib_input.Document("csv", list),                      # no columns
    _lib_input.Document("csv", dict, columns=("a",)),      # top is not list
    _lib_input.Document("json", dict, columns=("a",)),     # columns on another format
])
def test_a_csv_declaration_needs_its_header_and_only_csv_has_one(doc):
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--f", decode=doc)


def test_a_csv_document_is_its_rows_against_the_declared_header(tmp_path):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--f", decode=_lib_input.Document("csv", list, columns=("a", "b")))
    f = tmp_path / "r.csv"
    f.write_text("\ufeffa,b\n1,2\n\n3,4\n", encoding="utf-8")  # BOM, a blank line
    assert _lib_input.document(p.parse_args(["--f", str(f)]), "f", lang="en") == [
        {"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_a_csv_document_whose_records_end_in_a_bare_cr_is_read(tmp_path):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--f", decode=_lib_input.Document("csv", list, columns=("a", "b")))
    f = tmp_path / "r.csv"
    f.write_bytes(b"a,b\r1,2\r")
    assert _lib_input.document(p.parse_args(["--f", str(f)]), "f", lang="en") == [
        {"a": "1", "b": "2"}]


# ── #3751: a list of directories, one value ───────────────────────────────

def _list_parser():
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--dirs", select=_lib_input.DA_GUARD_LISTS, separator=",")
    return p


def test_a_list_selects_each_item_as_given(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    args = _list_parser().parse_args(["--dirs", f"{tmp_path / 'a'},{tmp_path / 'b'}"])
    assert _lib_input.selected(args, "dirs", lang="en") == [tmp_path / "a", tmp_path / "b"]
    assert _lib_input.absent(args, "dirs") is False


@pytest.mark.parametrize("value", ["a,,b", ",a", "a,", ","])
def test_a_list_with_an_empty_item_is_refused_at_parse(value, capsys):
    """The rule of every list flag (`url_list_type`): an empty item is no
    item to skip — it was dropped (`a,,b` read as `a,b`)."""
    with pytest.raises(SystemExit) as e:
        _list_parser().parse_args(["--dirs", value])
    assert e.value.code == 2
    assert f"argument --dirs: an empty item in {value!r}" in capsys.readouterr().err


def test_a_list_item_is_not_stripped(tmp_path):
    """`a, b`: the space is part of the path it names — never stripped into
    a directory the caller did not give."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    args = _list_parser().parse_args(["--dirs", f"{tmp_path / 'a'}, {tmp_path / 'b'}"])
    with pytest.raises(CallerError) as e:
        _lib_input.selected(args, "dirs", lang="en")
    assert f"--dirs  {tmp_path / 'b'}" in str(e.value)


def test_every_item_of_a_list_that_is_no_directory_is_named(tmp_path):
    args = _list_parser().parse_args(["--dirs", f"{tmp_path / 'x'},{tmp_path / 'y'}"])
    with pytest.raises(CallerError) as e:
        _lib_input.selected(args, "dirs", lang="en")
    assert e.value.reason == "dir_not_found"
    assert f"--dirs {tmp_path / 'x'}" in str(e.value) and f"--dirs {tmp_path / 'y'}" in str(e.value)


def test_an_empty_list_item_names_the_item(tmp_path):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--dirs", select=_lib_input.DA_GUARD_LISTS, separator=",",
                         empty=_lib_input.EMPTY_REFUSED)
    args = p.parse_args(["--dirs", "a,b"])
    with pytest.raises(CallerError) as e:
        _lib_input.require_selection(args, "dirs", [], wanted="w", lang="en", item="b")
    assert str(e.value).startswith("--dirs b: ")


@pytest.mark.parametrize("kw", [
    {"select": _lib_input.TopLevel("*")}, {"separator": ""},
    {"default": "x", "absent_default": _lib_input.ABSENT_EMPTY},
    {"missing": _lib_input.ABSENT_EMPTY},
])
def test_a_list_is_a_da_guard_listed_input_with_no_default(kw):
    with pytest.raises(ValueError):
        _lib_input.add_input(argparse.ArgumentParser(), "--dirs",
                             **{"select": _lib_input.DA_GUARD_LISTS, "separator": ",", **kw})


# ── #3751: a declared conf.d, judged once (`_lib_parse_cache.declared_trees`) ─

@pytest.mark.usefixtures("da_guard_env")
def test_every_reader_of_a_declared_tree_names_the_flag(tmp_path):
    """A library that opens a reader of its own (tenant-verify's scanner)
    still names the flag for that tree — not the path alone."""
    import _lib_parse_cache
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "t.yaml").write_text("tenants:\n  t: {}\n", encoding="utf-8")
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--c", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED)
    assert _lib_parse_cache.declared_trees(p.parse_args(["--c", str(conf)]), "c",
                                           lang="en") == [conf]
    assert _lib_parse_cache.ConfdReader(f"{conf}/./").declared == ("--c", "en")
    assert _lib_parse_cache.ConfdReader(tmp_path).declared is None


@pytest.mark.usefixtures("da_guard_env")
def test_a_declared_tree_with_no_config_file_is_refused_by_its_declaration(tmp_path):
    import _lib_parse_cache
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--c", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED)
    with pytest.raises(CallerError) as e:
        _lib_parse_cache.declared_trees(p.parse_args(["--c", str(tmp_path)]), "c", lang="en")
    assert e.value.reason == "input_empty"
    assert str(e.value).startswith(f"--c {tmp_path}: selects nothing")


@pytest.mark.usefixtures("da_guard_env")
def test_a_library_reader_does_not_judge_a_declared_tree_again(tmp_path, monkeypatch):
    """#3755 review: `_grar_parse.load_tenant_tree` judged --config-dir again
    after its CLI's declaration had (generate-routes, explain-route,
    test-notification, validate-config). A tree no declaration named — a
    library caller — is still judged there."""
    sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
    import _grar_parse
    import _lib_parse_cache
    seen = []
    real = _grar_parse.classify_path
    monkeypatch.setattr(_grar_parse, "classify_path",
                        lambda *a, **k: seen.append(a[0]) or real(*a, **k))
    undeclared = tmp_path / "lib"
    undeclared.mkdir()
    (undeclared / "t.yaml").write_text("tenants:\n  t: {}\n", encoding="utf-8")
    _grar_parse.load_tenant_tree(str(undeclared))
    assert seen == [str(undeclared)]
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "t.yaml").write_text("tenants:\n  t: {}\n", encoding="utf-8")
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--config-dir", select=_lib_input.DA_GUARD_LISTS)
    _lib_parse_cache.declared_trees(p.parse_args(["--config-dir", str(conf)]), "config_dir",
                                    lang="en")
    _grar_parse.load_tenant_tree(str(conf))
    assert seen == [str(undeclared)], "the declared tree was judged a second time"


# ── #3797: `-` is standard input for a `stdin=True` document ──────────────

def _stdin_args(argv, *, second=False):
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--s", decode=_lib_input.Document("json", dict), stdin=True)
    if second:
        _lib_input.add_input(p, "--t", decode=_lib_input.Document("json", dict), stdin=True)
    return p.parse_args(argv)


class _Stdin:
    def __init__(self, data):
        import io
        self.buffer = io.BytesIO(data)


def test_dash_is_standard_input_when_declared(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin(b'{"a": 1}'))
    events = []
    monkeypatch.setattr(_lib_input, "judged", lambda flag, path: events.append((flag, path)))
    assert _lib_input.document(_stdin_args(["--s", "-"]), "s", lang="en") == {"a": 1}
    assert events == [("--s", "-")]


def test_standard_input_is_judged_as_a_file_is(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin(b"{bad"))
    with pytest.raises(CallerError, match=r"^--s -: ") as exc:
        _lib_input.document(_stdin_args(["--s", "-"]), "s", lang="en")
    assert exc.value.reason == "file_unreadable"


def test_two_flags_cannot_both_read_standard_input(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin(b"{}"))
    args = _stdin_args(["--s", "-", "--t", "-"], second=True)
    with pytest.raises(CallerError, match=r"^--s -: standard input can be read by one flag "
                                          r"only \(also --t\)"):
        _lib_input.document(args, "s", lang="en")


def test_closed_standard_input_is_refused_named(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(CallerError, match=r"^--s -: standard input cannot be read"):
        _lib_input.document(_stdin_args(["--s", "-"]), "s", lang="en")


def test_dash_is_a_path_unless_declared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = argparse.ArgumentParser()
    _lib_input.add_input(p, "--f", decode=_lib_input.Document("json", dict))
    with pytest.raises(CallerError) as exc:
        _lib_input.document(p.parse_args(["--f", "-"]), "f", lang="en")
    assert exc.value.reason == "file_not_found"


def test_a_directory_is_never_standard_input():
    with pytest.raises(ValueError, match="stdin= / remedy= are for a decode= input only"):
        _lib_input.add_input(argparse.ArgumentParser(), "--d",
                             select=_lib_input.TopLevel("*"), stdin=True)
