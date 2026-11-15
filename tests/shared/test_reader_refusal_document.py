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
* no answer — usage, a promql-selectors <series> or a JSON framing of
  standard input the subcommand does not take, a file that cannot be
  opened, standard input that cannot be read, stdout that cannot be
  written: exit 2, no refusal document, never ``InputRefused``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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
    doc = cr._refusal(proc, beside="files")
    assert doc is not None and set(doc) == {cr.REFUSAL_KEY, "files"}, proc.stdout
    assert cr._refusal(proc) is None  # not the single-input document
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
    # nor the batch document (`answer` beside `files`): a no-answer taken
    # for prometheus-rules' refusal would turn red here too
    assert cr._refusal(proc, beside="files") is None, proc.stdout


@pytest.mark.parametrize("row", USAGES, ids=[r[0] for r in USAGES])
def test_a_usage_error_is_no_refusal(row) -> None:
    _id, env, name, args = row
    _assert_no_answer(_run(env, args, b""))
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr._run(os.environ[env], name, env, "make", args, b"")
    assert not isinstance(exc.value, cr.InputRefused)


@pytest.mark.parametrize("args,stdin", [
    # a <series> promql-selectors does not take (no metric name; a regex
    # matcher), with expressions it would read
    (["promql-selectors", '{filter="x"}', "-"], json.dumps(["up"])),
    (["promql-selectors", 'up{filter=~"x"}', "-"], json.dumps(["up +"])),
    # standard input that is not the request object
    (["promql-label-flow", "tenant", "-"], json.dumps(["up +"])),
    # json.Unmarshal reads null as no list and a null element as "": the
    # framing, never an expression the parser refuses
    (["promql-selectors", 'user_state_filter{filter="maintenance"}', "-"], "null"),
    (["promql-selectors", 'user_state_filter{filter="maintenance"}', "-"], "[null]"),
    (["promql-label-flow", "tenant", "-"], json.dumps({"expressions": [None]})),
    (["promql-label-flow", "tenant", "-"],
     json.dumps({"expressions": ["up"], "records": {"r": [{"expr": None}]}})),
    (["promql-label-flow", "tenant", "-"],
     json.dumps({"expressions": ["up"], "aliases": ["namespace", None]})),
], ids=["series-no-metric", "series-regex", "label-flow-framing", "selectors-null",
        "selectors-null-element", "label-flow-null-element", "label-flow-null-record-expr",
        "label-flow-null-alias"])
def test_the_callers_argument_is_no_refusal(args, stdin) -> None:
    """The <series> argument and the JSON framing of standard input are the
    caller's: wrong, they are a caller error (no refusal document), even
    when an expression in it would be refused."""
    _assert_no_answer(_run(cr.DA_READ_ENV, args, stdin.encode("utf-8")))
    with pytest.raises(cr.ConsumerReadError) as exc:
        if args[0] == "promql-selectors" and isinstance(json.loads(stdin), list):
            cr.promql_selectors(args[1], json.loads(stdin))
        else:
            cr._run(os.environ[cr.DA_READ_ENV], cr.DA_READ_BINARY_NAME, cr.DA_READ_ENV,
                    "make", args, stdin.encode("utf-8"))
    assert not isinstance(exc.value, cr.InputRefused)


def test_a_file_that_cannot_be_opened_is_no_answer_of_the_binaries(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    for env, args in ((cr.CRDECODE_ENV, [str(missing)]),
                      (cr.CRDECODE_ENV, ["--any-shape", str(missing)]),
                      (cr.DA_READ_ENV, ["alertmanager", str(missing)]),
                      (cr.DA_READ_ENV, ["prometheus", str(missing)]),
                      (cr.DA_READ_ENV, ["prometheus-rules", str(missing)])):
        _assert_no_answer(_run(env, args))
    # The binaries give no answer for it; `_lib_consumer_read` never hands
    # them such a file: it refuses it itself, naming it with CANNOT_OPEN and
    # the system's reason, so one unreadable file is that file's refusal.
    for read in (lambda: cr.kubernetes_documents(str(missing)),
                 lambda: cr.any_yaml_documents(str(missing)),
                 lambda: cr.prometheus_config(missing)):
        with pytest.raises(cr.InputRefused) as exc:
            read()
        assert exc.value.stderr_lines == [
            f"{exc.value.args[0].split()[0]}: {missing}: {cr.CANNOT_OPEN}: "
            "No such file or directory"], exc.value.stderr_lines
    [rf] = cr.prometheus_rule_file_results([missing])
    assert rf.problem == f"{cr.CANNOT_OPEN}: No such file or directory"
    # Per-file readings: that file's refusal, the other files still read.
    good = tmp_path / "good.yaml"
    good.write_text("groups: []\n", encoding="utf-8")
    got = cr.any_yaml_documents_of([missing, good], keep_refusals=True)
    assert isinstance(got[str(missing)], cr.InputRefused) and got[str(good)] == [{"groups": []}]
    found = cr.rule_files_in([missing, good], keep_refusals=True)
    [unread] = found[str(missing)]
    assert unread.unread and unread.problem == f"{cr.CANNOT_OPEN}: No such file or directory"
    assert [r.problem for r in found[str(good)]] == [None]


#: Run in a child, so a reading that blocks (opening a FIFO waits for a
#: writer; /dev/zero never reads to EOF) is a timeout here, not a hung suite.
_NOT_REGULAR_CHILD = """
import json, pathlib, sys
import _lib_consumer_read as cr
path, good = sys.argv[1], sys.argv[2]
out = {"unopenable": cr._unopenable(path)}
try:
    cr.prometheus_config(pathlib.Path(path))  # a str would be the text
except cr.InputRefused as e:
    out["refused"] = e.stderr_lines
out["rules"] = [r.problem for r in cr.prometheus_rule_file_results([path, good])]
print(json.dumps(out))
"""


@pytest.mark.parametrize("kind", ["fifo", "device", "symlink-to-regular"])
def test_a_path_that_is_not_a_regular_file_is_refused_unopened(tmp_path, kind) -> None:
    """A FIFO or a device is refused with ``NOT_REGULAR`` before it is
    opened: the read Python does before the Go program runs is under no
    timeout of the program's. A symlink to a regular file reads as that
    file."""
    good = tmp_path / "good.yml"
    good.write_text("groups: []\n", encoding="utf-8")
    if kind == "fifo":
        path = tmp_path / "fifo.yml"
        os.mkfifo(path)
    elif kind == "device":
        path = Path("/dev/zero")
        if not path.exists():
            pytest.skip("needs /dev/zero")
    else:
        target = tmp_path / "real.yml"
        target.write_text("groups: []\n", encoding="utf-8")
        path = tmp_path / "link.yml"
        path.symlink_to(target)
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([os.path.dirname(cr.__file__),
                                          os.environ.get("PYTHONPATH", "")])}
    try:
        proc = subprocess.run([sys.executable, "-c", _NOT_REGULAR_CHILD, str(path), str(good)],
                              capture_output=True, check=False, timeout=30, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"reading {kind} {path} blocked: opened before it was judged")
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    if kind == "symlink-to-regular":
        assert got["unopenable"] is None and got["rules"] == [None, None], got
        return
    assert got["unopenable"] == cr.NOT_REGULAR == f"{cr.CANNOT_OPEN}: not a regular file"
    assert got["refused"] == [f"{cr.DA_READ_BINARY_NAME}: {path}: {cr.NOT_REGULAR}"], got
    assert got["rules"] == [cr.NOT_REGULAR, None], got


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
