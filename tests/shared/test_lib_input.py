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

from _platform_fs import has_posix_modes  # noqa: E402


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
    assert str(exc.value).startswith(f"--d {tmp_path}: no entry matches *.json")


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
