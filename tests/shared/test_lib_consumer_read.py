"""`_lib_consumer_read`'s several-inputs readings (ADR-036 §12, #3206).

`kubernetes_documents_of` and `kubernetes_text_documents_each` run one
da-crdecode per input, a few at a time: the answers must come back in the
order given, a refusal must be raised (or kept, when asked) for the input it
belongs to, and a missing binary is never an answer about any input.

#3426: the two readings are two groups of names. The manifest reading
(`kubernetes_*`) gives only Kubernetes objects — anything else is refused,
named — and the any-YAML reading (`any_yaml_*`) gives whatever the YAML is.
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
    assert cr.kubernetes_text_documents("kind: A\na: no\nb: 010\n---\n---\nkind: C\n") == [
        {"kind": "A", "a": False, "b": 8}, {"kind": "C"}]
    assert cr.any_yaml_text_documents("a: no\nb: 010\n---\n---\n- x\n") == [
        {"a": False, "b": 8}, ["x"]]


_NOT_OBJECTS = [
    ("- a\n", "the document is array, not a mapping"),
    ("text\n", "the document is string, not a mapping"),
    ("a: 1\n", "it has no `kind`"),
    ("kind: ''\n", "its `kind` is empty"),
    ("kind: [x]\n", "its `kind` is array, not a string"),
    # a list is read as its items (#3426); an item kubectl would not apply,
    # or a list inside a list, is refused
    ("kind: List\nitems:\n- {kind: A}\n", "items[0] (line 5): it is not a Kubernetes object: "
                                            "it has no `apiVersion`"),
    ("kind: List\nitems:\n- {apiVersion: v1, kind: List, items: []}\n",
     "a list inside a list is not read"),
]


def test_a_list_is_read_as_its_items() -> None:
    """`kubectl get … -o yaml` writes a List; kubectl applies its items."""
    assert cr.kubernetes_text_documents(
        "apiVersion: v1\nkind: List\nitems:\n- {apiVersion: v1, kind: A}\n"
        "- {apiVersion: v1, kind: B}\n---\nkind: C\n---\nkind: List\nitems: null\n") == [
        {"apiVersion": "v1", "kind": "A"}, {"apiVersion": "v1", "kind": "B"}, {"kind": "C"}]


@pytest.mark.parametrize("text,reason", _NOT_OBJECTS)
def test_the_manifest_reading_gives_only_kubernetes_objects(tmp_path, text, reason) -> None:
    """Its guarantee: a caller needs no isinstance of its own — and the
    any-YAML reading of the same text is no refusal."""
    with pytest.raises(cr.InputRefused) as exc:
        cr.kubernetes_text_documents("kind: A\n---\n" + text)
    assert any("the document starting at line 3: " in ln and reason in ln
               for ln in exc.value.stderr_lines), exc.value.stderr_lines
    p = tmp_path / "f.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(cr.InputRefused):
        cr.kubernetes_documents(str(p))
    assert len(cr.any_yaml_text_documents(text)) == 1
    assert len(cr.any_yaml_documents(str(p))) == 1


def test_a_binary_without_the_reading_is_no_answer(tmp_path) -> None:
    """An older da-crdecode prints no `reading` and refuses no shape: its
    output carries no guarantee, so it is an error, not documents."""
    old = _fake(tmp_path, 'cat >/dev/null; echo \'{"documents":[[1]],"differs_from_text":[[]]}\'')
    for read in (cr.kubernetes_text_documents, cr.any_yaml_text_documents):
        with pytest.raises(cr.ConsumerReadError, match="older da-crdecode") as exc:
            read("kind: A\n", binary=old)
        assert not isinstance(exc.value, cr.InputRefused)


def test_the_other_reading_is_no_answer(tmp_path) -> None:
    """The reading the output names must be the one asked for."""
    swapped = _fake(tmp_path, 'cat >/dev/null; echo \'{"documents":[],"differs_from_text":[],'
                              '"reading":"any-shape"}\'')
    with pytest.raises(cr.ConsumerReadError, match="not 'manifest'"):
        cr.kubernetes_text_documents("kind: A\n", binary=swapped)


def test_a_usage_line_is_no_refusal(tmp_path) -> None:
    """An older da-crdecode does not know --any-shape: it prints its usage
    and exits 2. That is no answer about the input."""
    old = _fake(tmp_path, "echo 'usage: da-crdecode <file|->' >&2; exit 2")
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.any_yaml_text_documents("a: 1\n", binary=old)
    assert not isinstance(exc.value, cr.InputRefused)


def test_files_come_back_in_the_order_given(tmp_path) -> None:
    paths = []
    for i in range(20):
        p = tmp_path / f"f{i}.yaml"
        p.write_text(f"num: {i}\n", encoding="utf-8")
        paths.append(p)
    got = cr.any_yaml_documents_of(reversed(paths))
    assert list(got) == [str(p) for p in reversed(paths)]
    assert [docs[0]["num"] for docs in got.values()] == list(range(19, -1, -1))


def test_a_refused_file_is_raised_naming_it(tmp_path) -> None:
    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text("kind: A\n", encoding="utf-8")
    bad.write_text("--- a: 1\n", encoding="utf-8")
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_documents_of([good, bad])
    assert str(bad) in str(exc.value) and exc.value.stderr_lines


def test_a_refused_file_is_kept_when_asked(tmp_path) -> None:
    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text("kind: A\n", encoding="utf-8")
    bad.write_text("a: 1\n", encoding="utf-8")   # no kind (#3426)
    got = cr.kubernetes_documents_of([good, bad], keep_refusals=True)
    assert got[str(good)] == [{"kind": "A"}]
    assert isinstance(got[str(bad)], cr.InputRefused)
    got = cr.any_yaml_documents_of([good, bad], keep_refusals=True)
    assert got[str(bad)] == [{"a": 1}]


def test_texts_come_back_in_order_and_refusals_kept_when_asked() -> None:
    got = cr.kubernetes_text_documents_each(["kind: A\n", "--- x\n", "kind: B\n"],
                                            keep_refusals=True)
    assert got[0] == [{"kind": "A"}] and got[2] == [{"kind": "B"}]
    assert isinstance(got[1], cr.ConsumerReadError)
    with pytest.raises(cr.ConsumerReadError, match="text #2"):
        cr.kubernetes_text_documents_each(["kind: A\n", "--- x\n"])
    got = cr.any_yaml_text_documents_each(["a: 1\n", "- x\n", "--- x\n"], keep_refusals=True)
    assert got[:2] == [[{"a": 1}], [["x"]]] and isinstance(got[2], cr.InputRefused)


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


# --- InputRefused: one judgement of "the binary refused this input" --------
# (CodeRabbit on #3407: stderr alone is no refusal.)

def _fake(tmp_path, body: str) -> str:
    script = tmp_path / "fake-reader"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_exit_2_with_a_reason_is_input_refused(tmp_path) -> None:
    fake = _fake(tmp_path, "echo 'da-crdecode: <stdin>: bad' >&2; exit 2")
    with pytest.raises(cr.InputRefused) as exc:
        cr.kubernetes_text_documents("a: 1\n", binary=fake)
    assert exc.value.stderr_lines == ["da-crdecode: <stdin>: bad"]
    assert exc.value.detail() == "da-crdecode refused the input | da-crdecode: <stdin>: bad"


def test_a_real_refusal_is_input_refused() -> None:
    with pytest.raises(cr.InputRefused):
        cr.kubernetes_text_documents("--- a: 1\n")


@pytest.mark.parametrize("body", [
    "echo 'boom' >&2; exit 3",                                   # another exit code
    "echo 'panic: runtime error' >&2; echo 'goroutine 1' >&2; exit 2",  # a Go crash
    "echo 'fatal error: out of memory' >&2; exit 2",
    "exit 2",                                                    # no reason given
    "echo 'not json'; echo 'warn' >&2; exit 0",                  # no JSON contract
])
def test_other_failures_are_not_input_refused(tmp_path, body) -> None:
    fake = _fake(tmp_path, body)
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_text_documents("a: 1\n", binary=fake)
    assert not isinstance(exc.value, cr.InputRefused)


def test_a_run_that_does_not_finish_is_not_input_refused(tmp_path, monkeypatch) -> None:
    fake = _fake(tmp_path, "echo 'slow' >&2; exec sleep 5")
    monkeypatch.setattr(cr, "TIMEOUT_SECONDS", 0.5)
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_text_documents("a: 1\n", binary=fake)
    assert not isinstance(exc.value, cr.InputRefused)


def test_keep_refusals_keeps_only_input_refused(tmp_path) -> None:
    """A crash is raised even with keep_refusals, naming the input; a refusal
    is kept, and a refusal raised with a prefix stays InputRefused."""
    p = tmp_path / "a.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    crash = _fake(tmp_path, "echo 'panic: x' >&2; exit 2")
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_documents_of([p], binary=crash, keep_refusals=True)
    assert not isinstance(exc.value, cr.InputRefused) and str(p) in str(exc.value)
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.kubernetes_text_documents_each(["a: 1\n"], binary=crash, keep_refusals=True)
    assert not isinstance(exc.value, cr.InputRefused)
    with pytest.raises(cr.InputRefused, match="text #1"):
        cr.kubernetes_text_documents_each(["--- x\n"])


def test_rule_file_refusals_are_input_refused() -> None:
    """Aligned with the rule-file readers' branch: a refused rule file is an
    InputRefused too."""
    assert issubclass(cr.RuleFileRefused, cr.InputRefused)
    assert issubclass(cr.RuleFilesRefused, cr.InputRefused)
