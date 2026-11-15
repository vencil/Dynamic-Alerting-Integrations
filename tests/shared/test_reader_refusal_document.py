"""#3943: da-read and da-crdecode say "the input is refused" with a document,
not with exit 2 alone.

Both binaries exit 2 for a refusal and for much that is no answer about the
input: a usage error, a file they cannot open, standard input they cannot
read, stdout they cannot write, a Go crash. ``_lib_consumer_read`` takes
exit 2 as a refusal (``InputRefused``) only when stdout holds the refusal
document (``_refusal``: a JSON object whose ``REFUSAL_KEY`` is ``REFUSED``);
everything else is a fault of the tool.

The matrix runs the **real** binaries, built from this checkout, so it is
also the parity check of the document's one definition: the Python constants
(``REFUSAL_KEY`` / ``REFUSED``) against the Go copies in
components/da-read/main.go and cmd/da-crdecode/main.go (``answerRefused``).
A field renamed on one side turns the refusal rows red.

* every reading's refusal: exit 2, the refusal document, ``InputRefused``;
  for every reading but ``prometheus-rules`` the document is the constants
  alone, with no text of the input (ADR-036, #3703);
* no answer — usage, a file that cannot be opened, standard input that
  cannot be read, stdout that cannot be written: exit 2, no refusal
  document, never ``InputRefused``.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import _lib_consumer_read as cr

pytestmark = pytest.mark.usefixtures("da_read_env")

#: Text of the input that must never reach the refusal document.
MARKER = "conf-d-author-text-3943"

BAD_PROMQL = (f"groups:\n- name: {MARKER}\n  rules:\n"
              f"  - {{alert: {MARKER}, expr: 'rate(x[5m]'}}\n")

#: (row id, binary env, binary name, argv, stdin, the public reading that
#: must raise InputRefused on the same input)
REFUSALS = [
    ("crdecode-manifest", cr.CRDECODE_ENV, cr.CRDECODE_BINARY_NAME, ["-"],
     f"{MARKER}: 1\n", lambda t: cr.kubernetes_text_documents(t)),
    ("crdecode-any-shape", cr.CRDECODE_ENV, cr.CRDECODE_BINARY_NAME, ["--any-shape", "-"],
     f"--- {MARKER}\n", lambda t: cr.any_yaml_text_documents(t)),
    ("read-alertmanager", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME, ["alertmanager", "-"],
     f"{MARKER}: [\n", lambda t: cr.alertmanager_config(t)),
    ("read-alertmanager-reason", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME,
     ["alertmanager", cr.SHOW_LOADER_REASON_FLAG, "-"],
     f"route: {{receiver: {MARKER}}}\n", lambda t: cr.alertmanager_config(t, show_loader_reason=True)),
    ("read-prometheus", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME, ["prometheus", "-"],
     f"{MARKER}: []\n", lambda t: cr.prometheus_config(t)),
    ("read-promql-selectors", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME,
     ["promql-selectors", 'user_state_filter{filter="maintenance"}', "-"],
     json.dumps([f"{MARKER} +"]),
     lambda t: cr.promql_selectors('user_state_filter{filter="maintenance"}', [f"{MARKER} +"])),
    ("read-promql-label-flow", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME,
     ["promql-label-flow", "tenant", "-"], json.dumps({"expressions": [f"{MARKER} +"]}),
     lambda t: cr.promql_label_flow("tenant", [f"{MARKER} +"])),
]


def _run(env: str, args: "list[str]", stdin: "bytes | None" = None) -> subprocess.CompletedProcess:
    # bytes in, bytes out: stdout is JSON compared as bytes, never decoded here
    return subprocess.run([os.environ[env], *args], input=stdin, capture_output=True,
                          check=False, timeout=60)


@pytest.mark.parametrize("row", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_refusal_prints_the_refusal_document(row) -> None:
    _id, env, name, args, text, reading = row
    proc = _run(env, args, text.encode("utf-8"))
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert cr._refusal(proc) is not None, proc.stdout
    # The constants alone: nothing of the input (ADR-036, #3703).
    assert json.loads(proc.stdout) == {cr.REFUSAL_KEY: cr.REFUSED}, proc.stdout
    assert MARKER.encode() not in proc.stdout
    assert proc.stderr.strip(), "the reason is on stderr"
    with pytest.raises(cr.InputRefused):
        reading(text)


def test_a_refused_rule_file_prints_the_refusal_document_beside_its_files(tmp_path) -> None:
    good, bad = tmp_path / "good.yml", tmp_path / "bad.yml"
    good.write_text("groups: []\n", encoding="utf-8")
    bad.write_text(BAD_PROMQL, encoding="utf-8")
    proc = _run(cr.DA_READ_ENV, ["prometheus-rules", str(good), str(bad)])
    assert proc.returncode == 2, proc.stderr
    doc = cr._refusal(proc)
    assert doc is not None and set(doc) == {cr.REFUSAL_KEY, "files"}, proc.stdout
    with pytest.raises(cr.RuleFilesRefused) as exc:
        cr.prometheus_rule_files([good, bad])
    assert [r.path for r in exc.value.refused] == [str(bad)]
    # Every file read: no `answer` at all.
    ok = _run(cr.DA_READ_ENV, ["prometheus-rules", str(good)])
    assert ok.returncode == 0 and cr.REFUSAL_KEY not in json.loads(ok.stdout), ok.stdout


# --- No answer: exit 2, no refusal document, never InputRefused -------------

#: (row id, binary env, binary name, argv) of a usage error.
USAGES = [
    ("crdecode-two-args", cr.CRDECODE_ENV, cr.CRDECODE_BINARY_NAME, ["a", "b"]),
    ("crdecode-flag-alone", cr.CRDECODE_ENV, cr.CRDECODE_BINARY_NAME, ["--any-shape"]),
    ("read-unknown-subcommand", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME, ["vmalert", "-"]),
    ("read-rules-stdin-twice", cr.DA_READ_ENV, cr.DA_READ_BINARY_NAME,
     ["prometheus-rules", "-", "-"]),
]


def _assert_no_answer(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stderr.strip()
    assert cr._refusal(proc) is None, proc.stdout


@pytest.mark.parametrize("row", USAGES, ids=[r[0] for r in USAGES])
def test_a_usage_error_is_no_refusal(row) -> None:
    _id, env, name, args = row
    _assert_no_answer(_run(env, args, b""))
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr._run(os.environ[env], name, env, "make", args, b"")
    assert not isinstance(exc.value, cr.InputRefused)


def test_a_file_that_cannot_be_opened_is_no_refusal(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    for env, args in ((cr.CRDECODE_ENV, [str(missing)]),
                      (cr.CRDECODE_ENV, ["--any-shape", str(missing)]),
                      (cr.DA_READ_ENV, ["alertmanager", str(missing)]),
                      (cr.DA_READ_ENV, ["prometheus", str(missing)]),
                      (cr.DA_READ_ENV, ["prometheus-rules", str(missing)])):
        _assert_no_answer(_run(env, args))
    for read in (lambda: cr.kubernetes_documents(str(missing)),
                 lambda: cr.any_yaml_documents(str(missing)),
                 lambda: cr.prometheus_config(missing),
                 lambda: cr.prometheus_rule_file_results([missing])):
        with pytest.raises(cr.ConsumerReadError) as exc:
            read()
        assert not isinstance(exc.value, cr.InputRefused)


def test_standard_input_that_cannot_be_read_is_no_refusal(tmp_path) -> None:
    """A directory as standard input: read(2) fails (EISDIR)."""
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        for env, args in ((cr.CRDECODE_ENV, ["-"]),
                          (cr.DA_READ_ENV, ["alertmanager", "-"]),
                          (cr.DA_READ_ENV, ["prometheus-rules", "-"]),
                          (cr.DA_READ_ENV, ["promql-selectors", "up", "-"])):
            proc = subprocess.run([os.environ[env], *args], stdin=fd, capture_output=True,
                                  check=False, timeout=60)
            _assert_no_answer(proc)
    finally:
        os.close(fd)


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
@pytest.mark.parametrize("env,args,text", [
    (cr.CRDECODE_ENV, ["-"], "kind: A\n"),          # would be read
    (cr.CRDECODE_ENV, ["-"], "a: 1\n"),             # would be refused
    (cr.DA_READ_ENV, ["alertmanager", "-"], "route: {receiver: r}\nreceivers: [{name: r}]\n"),
    (cr.DA_READ_ENV, ["alertmanager", "-"], "x: [\n"),
])
def test_stdout_that_cannot_be_written_is_no_refusal(env, args, text) -> None:
    """stdout on /dev/full (ENOSPC): exit 2 naming the write, and the caller
    sees no refusal document — whether the input would be read or refused."""
    with open("/dev/full", "wb") as full:
        proc = subprocess.run([os.environ[env], *args], input=text.encode("utf-8"),
                              stdout=full, stderr=subprocess.PIPE, check=False, timeout=60)
    assert proc.returncode == 2 and b"writing stdout" in proc.stderr, (proc.returncode, proc.stderr)
    seen = subprocess.CompletedProcess(proc.args, proc.returncode, b"", proc.stderr)
    assert cr._refusal(seen) is None
