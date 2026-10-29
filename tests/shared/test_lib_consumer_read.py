"""`_lib_consumer_read`'s several-inputs readings (ADR-036 §12, #3206).

`kubernetes_documents_of` and `kubernetes_text_documents_each` run one
da-crdecode per input, a few at a time: the answers must come back in the
order given, a refusal must be raised (or kept, when asked) for the input it
belongs to, and a missing binary is never an answer about any input.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import _lib_consumer_read as cr  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_crdecode_env")


def test_text_is_read_as_kubectl_reads_it() -> None:
    assert cr.kubernetes_text_documents("a: no\nb: 010\n---\n---\nc: x\n") == [
        {"a": False, "b": 8}, {"c": "x"}]


def test_files_come_back_in_the_order_given(tmp_path) -> None:
    paths = []
    for i in range(20):
        p = tmp_path / f"f{i}.yaml"
        p.write_text(f"num: {i}\n", encoding="utf-8")
        paths.append(p)
    got = cr.kubernetes_documents_of(reversed(paths))
    assert list(got) == [str(p) for p in reversed(paths)]
    assert [docs[0]["num"] for docs in got.values()] == list(range(19, -1, -1))


def test_a_refused_file_is_raised_naming_it(tmp_path) -> None:
    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text("a: 1\n", encoding="utf-8")
    bad.write_text("--- a: 1\n", encoding="utf-8")
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_documents_of([good, bad])
    assert str(bad) in str(exc.value) and exc.value.stderr_lines


def test_a_refused_file_is_kept_when_asked(tmp_path) -> None:
    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text("a: 1\n", encoding="utf-8")
    bad.write_text("--- a: 1\n", encoding="utf-8")
    got = cr.kubernetes_documents_of([good, bad], keep_refusals=True)
    assert got[str(good)] == [{"a": 1}]
    assert isinstance(got[str(bad)], cr.ConsumerReadError)


def test_texts_come_back_in_order_and_refusals_kept_when_asked() -> None:
    got = cr.kubernetes_text_documents_each(["a: 1\n", "--- x\n", "b: 2\n"], keep_refusals=True)
    assert got[0] == [{"a": 1}] and got[2] == [{"b": 2}]
    assert isinstance(got[1], cr.ConsumerReadError)
    with pytest.raises(cr.ConsumerReadError, match="text #2"):
        cr.kubernetes_text_documents_each(["a: 1\n", "--- x\n"])


def test_no_binary_is_raised_even_when_refusals_are_kept(tmp_path, monkeypatch) -> None:
    """A run that could not happen is not a refusal of the file."""
    p = tmp_path / "a.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    monkeypatch.setenv(cr.CRDECODE_ENV, str(tmp_path / "no-such-da-crdecode"))
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_documents_of([p], keep_refusals=True)
    assert not exc.value.stderr_lines
    with pytest.raises(cr.ConsumerReadError):
        cr.kubernetes_text_documents_each(["a: 1\n"], keep_refusals=True)


def test_not_found_is_said_before_any_run(monkeypatch) -> None:
    monkeypatch.delenv(cr.CRDECODE_ENV, raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(cr.ConsumerReadError, match="was not found"):
        cr.kubernetes_documents_of(["/nonexistent.yaml"])
    assert cr.kubernetes_documents_of([]) == {}
