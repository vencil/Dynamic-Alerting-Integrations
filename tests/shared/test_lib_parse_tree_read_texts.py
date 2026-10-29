"""`_lib_parse_tree.read_texts`: texts that are no file of a tree on disk,
each read as one conf.d file by `da-guard parse-tree` (ADR-036 §12, #3206).

The real binary (the session's `$DA_GUARD_BINARY`, tests/conftest.py) for what
it reads; a stand-in script for each way it can fail to answer. ⛔ Fail-closed:
a da-guard that is missing, crashes or prints no document is a
ParseTreeError for the whole call — never one FileUnreadable per text, which
a caller would report as "every text refused" (or pass over).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import _lib_parse_tree as lpt  # noqa: E402


def test_each_text_is_read_in_order_or_refused() -> None:
    got = lpt.read_texts(["a: 1\n", "a: 1\na: 2\n", "", "- x\n", b"on: yes\n"])
    assert len(got) == 5
    assert lpt.to_python(got[0]) == {"a": 1}
    assert isinstance(got[1], lpt.FileUnreadable) and got[1].kind == "duplicate_key"
    assert lpt.to_python(got[2]) == {}
    assert isinstance(got[3], lpt.FileUnreadable) and got[3].kind == "top_level_not_mapping"
    # yaml.v3: `on` / `yes` are strings, not YAML 1.1 booleans
    assert lpt.to_python(got[4]) == {"on": "yes"}


def test_no_text_runs_nothing(tmp_path: Path) -> None:
    assert lpt.read_texts([], binary=str(tmp_path / "no-such-da-guard")) == []


def test_no_da_guard_is_an_error_for_the_whole_call(tmp_path: Path) -> None:
    with pytest.raises(lpt.BinaryMissing):
        lpt.read_texts(["a: 1\n"], binary=str(tmp_path / "no-such-da-guard"))


def _stand_in(tmp_path: Path, body: str) -> str:
    p = tmp_path / "da-guard"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.mark.skipif(os.name == "nt", reason="a /bin/sh stand-in")
@pytest.mark.parametrize("body", [
    # a Go runtime crash exits 2 with stderr, as a usage error does
    'echo "panic: runtime error: index out of range" >&2\nexit 2\n',
    # exit 0 without the JSON contract
    'cat >/dev/null\necho "not json"\n',
    # one document for two texts
    'cat >/dev/null\necho "{}"\n',
])
def test_a_da_guard_that_does_not_answer_is_an_error_not_a_refusal(tmp_path: Path, body: str) -> None:
    binary = _stand_in(tmp_path, body)
    with pytest.raises(lpt.ParseTreeError) as exc:
        lpt.read_texts(["a: 1\n", "b: 2\n"], binary=binary)
    assert not isinstance(exc.value, lpt.FileUnreadable), exc.value
