"""A file outside conf.d, read the way its consumer reads it (ADR-036 §12, #3206).

The Python tools do not read these files with PyYAML: PyYAML's reading is
YAML 1.1 as PyYAML implements it, which is neither kubectl's nor
Alertmanager's. They ask the Go binary that runs the consumer's own reading:

* ``kubernetes_documents(path)`` — a Kubernetes manifest as kubectl reads
  it: ``da-crdecode`` (sigs.k8s.io/yaml, documents split as ``kubectl apply
  -f`` splits them). One entry per document kubectl would apply, in file
  order; an empty file has none. ``instance: 010`` is the number 8 here, as
  it is to kubectl. ⛔ Every entry is a Kubernetes object — a ``dict`` with
  a non-empty ``str`` ``kind``, never a list: a ``kind: List`` is replaced by
  its items, as kubectl applies them (#3426). A file with any other document,
  or an item kubectl would not apply, is refused, naming it, so a caller
  never skips one with an ``isinstance`` of its own.
* ``kubernetes_text_documents(text)`` — the same reading of text held in
  memory (a manifest ``kubectl ... -o yaml`` or ``helm template`` printed, a
  ConfigMap a tool assembled), handed over on standard input.
* ``kubernetes_documents_of(paths)`` / ``kubernetes_text_documents_each(texts)``
  — the same for several files / texts. da-crdecode reads one input per run,
  so these run one per input, a few at a time (``PARALLEL_RUNS``), and return
  them in the order given.
* ``any_yaml_documents(path)`` / ``any_yaml_text_documents(text)`` /
  ``any_yaml_documents_of(paths)`` / ``any_yaml_text_documents_each(texts)``
  — YAML that is NOT a manifest (a Helm values file, ``Chart.yaml``, a plain
  rule file, a ConfigMap data value) with the same conversion
  (``da-crdecode --any-shape``). Nothing about a document's shape is
  promised. The two groups are separate names, never one name with a flag:
  which reading a call is, is in its name.
* ``alertmanager_config(text)`` — an Alertmanager config as Alertmanager
  loads it: ``da-read alertmanager`` (Alertmanager's own ``config.Load``, at
  the amtool version the da-tools image ships), keeping only the keys the
  text writes. ``match: {tenant: 010}`` is the string ``"010"`` here, as it is
  to Alertmanager. Secrets are their values.
* ``prometheus_config(source)`` — a ``prometheus.yml`` as Prometheus loads it:
  ``da-read prometheus`` (Prometheus' own ``config.Load``, every service
  discovery registered, at the version the platform deploys), keeping only
  the keys the text writes. *source* is the text (``str`` / ``bytes``) or a
  path (``os.PathLike``).
* ``prometheus_rule_files(paths)`` — Prometheus rule files as Prometheus'
  rule manager loads them (``da-read prometheus-rules``: ``rulefmt.Parse``),
  one :class:`RuleFile` per path, in order, all from one run of the binary.
  When any file is refused it raises :class:`RuleFilesRefused` (which carries
  every file's result) — a caller cannot read the readable ones and miss the
  refused one by accident. A lint that reports each refused file by name asks
  ``prometheus_rule_file_results(paths)`` instead: it returns every result,
  and reading ``RuleFile.documents`` / ``.groups`` of a refused one raises
  :class:`RuleFileRefused`. ``require_read(results)`` turns a result list
  into ``prometheus_rule_files``' answer.
* ``prometheus_rule_text_results(texts)`` — rule files held as text
  (``(name, text)`` pairs; a ConfigMap's inner strings), all in one run:
  ``-`` can be given once, so the texts go to files in a private temporary
  directory (0700, removed before it returns) and come back under their
  names. ``manifest_rule_file_results(paths)`` is the two-layer reading built
  on it: each manifest the way kubectl reads it, then every ConfigMap
  ``data`` / ``binaryData`` value and every PrometheusRule ``spec`` by the
  rule loader (``RuleFile.manifest`` / ``.kind`` / ``.key`` say where it came
  from), for files known to be manifests.
* ``rule_files_in(paths)`` — ⛔ the one answer to "which rule files does each
  file hand to Prometheus" for a file that may be a manifest or a plain rule
  file (#3426): the manifest reading first, then the any-YAML reading, then
  the rule loader; a file none of them reads as either is refused, named —
  which files are read is the caller's declaration by path
  (``files_to_read`` with ``--exclude``, ``rule_pack_files``), never a
  judgement of a file's content. No tool classifies files from a reading
  of its own (``tests/shared/test_rule_file_classifier_is_one.py``).

Each raises :class:`ConsumerReadError` when the binary is missing, cannot
be run, does not finish, refuses the input (its reason is in
``stderr_lines``) or prints something that is not its JSON contract; a
refusal is the subclass :class:`InputRefused` (a lint names the file, a
missing binary it does not). Nothing here returns "empty" for "could not be
read".

Binaries: an explicit path (a tool's ``--da-crdecode-binary`` /
``--da-read-binary``), else ``$DA_CRDECODE_BINARY`` / ``$DA_READ_BINARY``,
else ``da-crdecode`` / ``da-read`` on ``$PATH`` (the da-tools image ships
both in /usr/local/bin). In this repo ``make da-crdecode-build`` /
``make da-read-build`` build them into ``.build/``.

Stdlib only.
"""
from __future__ import annotations

import base64
import binascii
import fnmatch
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

CRDECODE_BINARY_NAME = "da-crdecode"
DA_READ_BINARY_NAME = "da-read"
TIMEOUT_SECONDS = 60

# The one register of the environment variables that name a reader binary
# (#3424): every tool reads these names from here, not from a literal of its
# own. da-guard's is here too, though da-guard is not run by this module:
# `absolute_reader_binaries` has to cover all three, and a second register
# would be the one that falls behind.
DA_GUARD_ENV = "DA_GUARD_BINARY"
DA_READ_ENV = "DA_READ_BINARY"
CRDECODE_ENV = "DA_CRDECODE_BINARY"
#: Every reader binary's environment variable, in no particular order.
READER_BINARY_ENVS: "tuple[str, ...]" = (DA_GUARD_ENV, DA_READ_ENV, CRDECODE_ENV)


def absolute_reader_binaries(environ: "dict[str, str] | None" = None) -> None:
    """Make each variable of ``READER_BINARY_ENVS`` in *environ* (default:
    this process's environment) an absolute path, against the current
    working directory.

    A relative path in one of them means "relative to the directory of the
    shell that set it" — which is this process's working directory only
    until the process changes directory, or starts a process with another
    ``cwd``. Call this before either, so the binary the user named is the
    one that runs wherever it is resolved. A value that is unset, blank or
    already absolute is left as it is (blank still means "not set": the
    resolvers then look on ``$PATH``)."""
    env = os.environ if environ is None else environ
    for name in READER_BINARY_ENVS:
        value = env.get(name, "")
        if value.strip() and not os.path.isabs(value):
            env[name] = os.path.abspath(value)


class ConsumerReadError(Exception):
    """The consumer's reading could not be had.

    ``str()`` is one line; ``stderr_lines`` is the binary's stderr, every
    non-empty line as written (the reason a refusal gives).
    """

    def __init__(self, message: str, stderr_lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.stderr_lines = list(stderr_lines or [])

    def detail(self) -> str:
        """The one line, then the binary's reason, joined with `` | `` — for a
        finding that has one line to say it in."""
        return " | ".join([str(self), *self.stderr_lines])


class InputRefused(ConsumerReadError):
    """The binary ran and refused its input (exit 2 with its reason on
    stderr): the consumer does not read this text. Every other
    :class:`ConsumerReadError` means the reading could not be had at all —
    a caller that reports a refused input as a finding must not report a
    missing binary that way.

    ⛔ Only :func:`_refused` decides this, so there is one judgement of it:
    exit 2 (da-crdecode's and da-read's answer to the input; the arguments
    are ours, so not a usage error) with a reason on stderr that is not a Go
    runtime crash — a panic also exits 2 with stderr, and is not an answer
    about the input — nor a usage line (a binary too old for our arguments). Not found, could not run, did not finish, any other exit
    code, exit 0 without the JSON contract: a plain ConsumerReadError.
    """


#: A Go runtime crash: the first stderr line of an unrecovered panic or a
#: fatal runtime error (both exit 2, as a refusal does) — and the usage
#: line, which a binary older than the arguments we give it prints (exit 2;
#: an old da-crdecode does not know ``--any-shape``, #3426). None of them is
#: an answer about the input.
_GO_CRASH_PREFIXES = ("panic: ", "fatal error: ", "usage: ")


def _refused(returncode: int, lines: list[str]) -> bool:
    """Whether a run that exited *returncode* with stderr *lines* is the
    binary refusing its input (see :class:`InputRefused`)."""
    return (returncode == 2 and bool(lines)
            and not lines[0].startswith(_GO_CRASH_PREFIXES))


def error_text(e: ConsumerReadError) -> str:
    """*e* as a tool prints it: its one line, then the binary's reason, each
    line indented."""
    return "\n".join([str(e), *(f"  {ln}" for ln in e.stderr_lines)])


def _stderr_lines(raw: bytes | None) -> list[str]:
    """Every non-empty line of *raw*, split on ``\\n`` only. Bytes that are
    not UTF-8 are shown escaped."""
    if not raw:
        return []
    text = raw.decode("utf-8", errors="backslashreplace")
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]


def resolve_binary(explicit: str | None, env: str, name: str) -> str | None:
    """*explicit* when given, else ``$env`` when set (non-blank), else *name*
    on ``$PATH``; None when there is none."""
    if explicit:
        return explicit
    from_env = os.environ.get(env, "")
    if from_env.strip():
        return from_env
    return shutil.which(name)


def _invoke(binary: str | None, name: str, env: str, build_hint: str,
            args: list[str], stdin: bytes | None) -> "tuple[subprocess.CompletedProcess, list[str]]":
    if not binary:
        raise ConsumerReadError(
            f"{name} was not found: set ${env} to its path or put it on $PATH "
            f"(the da-tools image ships it; in this repo `{build_hint}` builds "
            f".build/{name})")
    try:
        proc = subprocess.run([binary, *args], input=stdin, capture_output=True,
                              check=False, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise ConsumerReadError(f"{name} did not finish within {TIMEOUT_SECONDS}s",
                                _stderr_lines(e.stderr)) from e
    except OSError as e:
        raise ConsumerReadError(f"cannot run {name} ({binary}): {e}") from e
    return proc, _stderr_lines(proc.stderr)


def _json(proc: subprocess.CompletedProcess, name: str, lines: list[str]) -> Any:
    try:
        return json.loads(proc.stdout)
    except ValueError as e:  # JSONDecodeError, or stdout that is not UTF-8
        raise ConsumerReadError(f"{name} printed output that is not JSON: {e}", lines) from e


def _run(binary: str | None, name: str, env: str, build_hint: str,
         args: list[str], stdin: bytes | None) -> dict:
    proc, lines = _invoke(binary, name, env, build_hint, args, stdin)
    if proc.returncode == 2 and lines and lines[0].startswith("usage: "):
        raise ConsumerReadError(
            f"{name} printed its usage for the arguments {args[:-1] or args} "
            f"(an older {name}? rebuild it)", lines)
    if _refused(proc.returncode, lines):
        raise InputRefused(f"{name} refused the input", lines)
    if proc.returncode != 0:
        raise ConsumerReadError(f"{name} exited {proc.returncode}", lines)
    out = _json(proc, name, lines)
    docs = out.get("documents") if isinstance(out, dict) else None
    if not isinstance(docs, list):
        raise ConsumerReadError(
            f"{name} printed JSON without a \"documents\" list (an older {name}? rebuild it)",
            lines)
    return out


#: da-crdecode's flag for YAML that is not a manifest, and the ``reading``
#: each of its two readings reports (#3426).
_ANY_SHAPE_FLAG = "--any-shape"
_READING_MANIFEST = "manifest"
_READING_ANY_SHAPE = "any-shape"


def _crdecode(source: "str | None", data: "bytes | None", binary: str | None,
              any_shape: bool) -> list[Any]:
    """One da-crdecode run on the file *source* (or *data* on stdin), with
    the manifest reading or ``--any-shape``. The public functions below each
    fix *any_shape*; none takes it as an argument, so a name says which
    reading it is."""
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)
    # An absolute path never starts with `-`, so it is not read as stdin.
    target = ["-"] if source is None else [os.path.abspath(source)]
    args = ([_ANY_SHAPE_FLAG] if any_shape else []) + target
    out = _run(resolved, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build",
               args, data)
    want = _READING_ANY_SHAPE if any_shape else _READING_MANIFEST
    if out.get("reading") != want:
        # An older da-crdecode prints no `reading`: its manifest reading does
        # not refuse a document that is no Kubernetes object, so the
        # guarantee below would be silently missing.
        raise ConsumerReadError(
            f"{CRDECODE_BINARY_NAME} printed \"reading\": {out.get('reading')!r}, not "
            f"{want!r} (an older {CRDECODE_BINARY_NAME}? rebuild it)")
    return out["documents"]


def _as_bytes(text: "str | bytes") -> bytes:
    return text.encode("utf-8") if isinstance(text, str) else text


# --- The manifest reading: what kubectl applies -----------------------------


def kubernetes_documents(path: str, binary: str | None = None) -> list[Any]:
    """The documents of the Kubernetes manifest at *path*, as kubectl reads
    them (``da-crdecode``): one per document kubectl would apply, in order.

    ⛔ The guarantee (#3426): every document is a Kubernetes object — a
    ``dict`` whose ``kind`` is a non-empty ``str`` — and none is a list: a
    document with a top-level ``items`` (``kind: List``, the output of
    ``kubectl get … -o yaml``) is replaced by its items, in order, as kubectl
    applies them. A file with a document that is no object (an array, a
    scalar, a mapping with no ``kind``), or a list with an item kubectl would
    not apply (no ``apiVersion`` or ``kind``, a list inside it), is refused
    (:class:`InputRefused`, the line and the reason in ``stderr_lines``), as
    kubectl refuses it; nothing is passed over. A
    caller therefore needs no ``isinstance(doc, dict)`` of its own, and
    must not skip a document with one. YAML that is not a manifest (Helm
    values, a rule file, a ConfigMap data value) is read with
    :func:`any_yaml_documents` instead."""
    return _crdecode(path, None, binary, any_shape=False)


def kubernetes_text_documents(text: str | bytes, binary: str | None = None) -> list[Any]:
    """The documents of the Kubernetes manifest *text*, as kubectl reads them
    (``da-crdecode -``): for text a tool holds rather than a file. The
    guarantee of :func:`kubernetes_documents` holds."""
    return _crdecode(None, _as_bytes(text), binary, any_shape=False)


#: How many da-crdecode runs ``kubernetes_documents_of`` keeps going at once.
PARALLEL_RUNS = 8


def kubernetes_documents_of(paths: Iterable[Any], binary: str | None = None, *,
                            keep_refusals: bool = False
                            ) -> "dict[str, list[Any] | ConsumerReadError]":
    """``kubernetes_documents`` for every path in *paths*, keyed by
    ``str(path)`` in the order given. The first path (in that order) whose
    reading cannot be had raises its :class:`ConsumerReadError`; the message
    names the path. With *keep_refusals*, a file da-crdecode refuses (its
    reason on stderr) is not raised: its value is the error, for a caller
    that reports per file. A run that gave no reason (no binary, it could not
    run, it printed no JSON contract) is raised either way — that is not an
    answer about the file."""
    return _files_of(paths, binary, keep_refusals, kubernetes_documents)


def kubernetes_text_documents_each(texts: Iterable[str | bytes], binary: str | None = None, *,
                                   keep_refusals: bool = False
                                   ) -> "list[list[Any] | ConsumerReadError]":
    """``kubernetes_text_documents`` for every text in *texts*, in order, a
    few runs at a time. Errors as :func:`kubernetes_documents_of` (by
    position instead of path)."""
    return _texts_each(texts, binary, keep_refusals, kubernetes_text_documents)


# --- The any-YAML reading: the same conversion, no manifest shape -----------


def any_yaml_documents(path: str, binary: str | None = None) -> list[Any]:
    """The documents of the YAML file at *path* that is NOT a Kubernetes
    manifest — a Helm values file, ``Chart.yaml``, a plain rule file —
    with the conversion kubectl and Helm use (``da-crdecode --any-shape``:
    sigs.k8s.io/yaml, split as ``kubectl apply -f`` splits a file). A
    document may be anything (a mapping, a list, a scalar); nothing about
    its shape is promised, so the caller checks the shape it needs."""
    return _crdecode(path, None, binary, any_shape=True)


def any_yaml_text_documents(text: str | bytes, binary: str | None = None) -> list[Any]:
    """:func:`any_yaml_documents` of *text* held in memory — a ConfigMap
    data value, a spliced values file (``da-crdecode --any-shape -``)."""
    return _crdecode(None, _as_bytes(text), binary, any_shape=True)


def any_yaml_documents_of(paths: Iterable[Any], binary: str | None = None, *,
                          keep_refusals: bool = False
                          ) -> "dict[str, list[Any] | ConsumerReadError]":
    """:func:`any_yaml_documents` for every path, as
    :func:`kubernetes_documents_of` runs and reports them."""
    return _files_of(paths, binary, keep_refusals, any_yaml_documents)


def any_yaml_text_documents_each(texts: Iterable[str | bytes], binary: str | None = None, *,
                                 keep_refusals: bool = False
                                 ) -> "list[list[Any] | ConsumerReadError]":
    """:func:`any_yaml_text_documents` for every text, as
    :func:`kubernetes_text_documents_each` runs and reports them."""
    return _texts_each(texts, binary, keep_refusals, any_yaml_text_documents)


def _files_of(paths: Iterable[Any], binary: str | None, keep_refusals: bool,
              read_one) -> "dict[str, list[Any] | ConsumerReadError]":
    keys = [str(p) for p in paths]
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)

    def one(path: str) -> "list[Any] | ConsumerReadError":
        try:
            return read_one(path, binary=resolved)
        except ConsumerReadError as e:
            return e

    if not keys:
        return {}
    if not resolved:
        # the "not found" message of a single read, once, before any run
        _run(None, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build", [], None)
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        results = list(pool.map(one, keys))
    out: "dict[str, list[Any]]" = {}
    for path, res in zip(keys, results):
        if isinstance(res, ConsumerReadError):
            if keep_refusals and _is_refusal(res):
                out[path] = res
                continue
            raise _same_kind(res)(f"{path}: {res}", res.stderr_lines) from res
        out[path] = res
    return out


def _texts_each(texts: Iterable[str | bytes], binary: str | None, keep_refusals: bool,
                read_one) -> "list[list[Any] | ConsumerReadError]":
    items = list(texts)
    if not items:
        return []
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)
    if not resolved:
        _run(None, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build", [], None)

    def one(text: str | bytes) -> "list[Any] | ConsumerReadError":
        try:
            return read_one(text, binary=resolved)
        except ConsumerReadError as e:
            return e

    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        results = list(pool.map(one, items))
    for i, res in enumerate(results):
        if isinstance(res, ConsumerReadError) and not (
                keep_refusals and _is_refusal(res)):
            raise _same_kind(res)(f"text #{i + 1}: {res}", res.stderr_lines) from res
    return results


def _same_kind(e: ConsumerReadError) -> type:
    """The class to re-raise *e* as with a prefixed message: a refusal stays
    :class:`InputRefused`, everything else is a plain ConsumerReadError."""
    return InputRefused if isinstance(e, InputRefused) else ConsumerReadError


def _is_refusal(e: ConsumerReadError) -> bool:
    """da-crdecode ran and refused the input (:class:`InputRefused`)."""
    return isinstance(e, InputRefused)


def alertmanager_config(text: str | bytes, binary: str | None = None) -> dict:
    """The Alertmanager config *text* as Alertmanager loads it (``da-read
    alertmanager``), only the keys the text writes."""
    resolved = resolve_binary(binary, DA_READ_ENV, DA_READ_BINARY_NAME)
    data = text.encode("utf-8") if isinstance(text, str) else text
    out = _run(resolved, DA_READ_BINARY_NAME, DA_READ_ENV, "make da-read-build",
               ["alertmanager", "-"], data)
    return _one_document(out, "config")


def _one_document(out: dict, what: str) -> dict:
    docs = out["documents"]
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ConsumerReadError(
            f"{DA_READ_BINARY_NAME} printed JSON without exactly one {what} object "
            "under \"documents\"")
    return docs[0]


def prometheus_config(source: "str | bytes | os.PathLike[str]", binary: str | None = None) -> dict:
    """The ``prometheus.yml`` *source* as Prometheus loads it (``da-read
    prometheus``), only the keys the text writes. *source* is the text
    (``str`` or ``bytes``) or the path of the file (``os.PathLike``, e.g. a
    ``pathlib.Path``)."""
    if isinstance(source, os.PathLike):
        # An absolute path never starts with `-`, so it is not read as stdin.
        args, data = ["prometheus", os.path.abspath(os.fspath(source))], None
    elif isinstance(source, (str, bytes)):
        args = ["prometheus", "-"]
        data = source.encode("utf-8") if isinstance(source, str) else source
    else:
        raise TypeError(f"prometheus_config takes text or a path, not {type(source).__name__}")
    resolved = resolve_binary(binary, DA_READ_ENV, DA_READ_BINARY_NAME)
    out = _run(resolved, DA_READ_BINARY_NAME, DA_READ_ENV, "make da-read-build", args, data)
    return _one_document(out, "config")


class RuleFileRefused(InputRefused):
    """Prometheus' rule loader refused this rule file; ``path`` names it and
    ``problem`` is the loader's reason."""

    def __init__(self, path: str, problem: str) -> None:
        super().__init__(f"{path}: {problem}", problem.splitlines())
        self.path = path
        self.problem = problem


class RuleFile:
    """One rule file as ``da-read prometheus-rules`` read it.

    ``problem`` is None when Prometheus loads the file, else the loader's
    reason. ``documents`` (and ``groups``) raise :class:`RuleFileRefused` for
    a refused file, so its contents cannot be taken as empty by accident.
    """

    __slots__ = ("path", "problem", "_documents", "manifest", "kind", "key", "object_name",
                 "unread")

    def __init__(self, path: str, documents: "list[Any] | None", problem: "str | None") -> None:
        self.path = path
        self.problem = problem
        self._documents = documents
        #: for a rule file a Kubernetes manifest delivers
        #: (``manifest_rule_file_results``): the manifest's path, the kind of
        #: the document (``ConfigMap`` / ``PrometheusRule``) and the data key
        #: (``spec`` for a PrometheusRule); ``path`` is ``<manifest>#<key>``.
        #: None for a plain rule file.
        self.manifest: "str | None" = None
        self.kind: "str | None" = None
        self.key: "str | None" = None
        #: the delivering document's ``metadata.name`` (a manifest's rule file)
        self.object_name: "str | None" = None
        #: True for a file ``rule_files_in`` could not read as a manifest or a
        #: rule file (``keep_refusals``): ``problem`` is its readers' reason
        self.unread = False

    @property
    def documents(self) -> list:
        """The file's one document, in a list (``da-read``'s ``documents``)."""
        if self.problem is not None:
            raise RuleFileRefused(self.path, self.problem)
        return self._documents  # type: ignore[return-value]

    @property
    def groups(self) -> list:
        """The rule groups Prometheus loads from the file (``[]`` for an
        empty file)."""
        groups = self.documents[0].get("groups")
        return groups if isinstance(groups, list) else []

    def __repr__(self) -> str:
        state = f"problem={self.problem!r}" if self.problem is not None else "read"
        return f"RuleFile({self.path!r}, {state})"


class RuleFilesRefused(InputRefused):
    """At least one rule file is refused; ``results`` holds every file's
    :class:`RuleFile` (in the order given) and ``refused`` the refused ones."""

    def __init__(self, results: "list[RuleFile]") -> None:
        self.results = results
        self.refused = [r for r in results if r.problem is not None]
        lines = [f"{r.path}: {r.problem}" for r in self.refused]
        super().__init__(
            f"Prometheus' rule loader refuses {len(self.refused)} of {len(results)} "
            "rule file(s)", lines)


def prometheus_rule_file_results(paths: "Iterable[str | os.PathLike[str]]",
                                 binary: str | None = None) -> "list[RuleFile]":
    """Every rule file of *paths* as Prometheus loads it (``da-read
    prometheus-rules``, one run for all), one :class:`RuleFile` per path in
    the order given — refused ones included, each with its ``problem``.
    ``RuleFile.path`` is the path as given. For callers that report each
    refused file themselves; everyone else calls ``prometheus_rule_files``."""
    given = [os.fspath(p) for p in paths]
    if not given:
        return []
    resolved = resolve_binary(binary, DA_READ_ENV, DA_READ_BINARY_NAME)
    # Absolute paths never start with `-`, so none is read as stdin.
    absolute = [os.path.abspath(p) for p in given]
    proc, lines = _invoke(resolved, DA_READ_BINARY_NAME, DA_READ_ENV, "make da-read-build",
                          ["prometheus-rules", *absolute], None)
    # rc 0 is "every file read"; a refusal (`_refused`: rc 2 with a reason on
    # stderr that is not a Go crash — da-read names each refused file there)
    # with the JSON on stdout is "a file is refused". Anything else — rc 2
    # with nothing on stdout (a usage error), a crash, any other rc — is not
    # an answer about the files.
    if not proc.stdout.strip() or not (
            proc.returncode == 0 or _refused(proc.returncode, lines)):
        raise ConsumerReadError(f"{DA_READ_BINARY_NAME} exited {proc.returncode}", lines)
    out = _json(proc, DA_READ_BINARY_NAME, lines)
    files = out.get("files") if isinstance(out, dict) else None
    if not isinstance(files, list) or len(files) != len(given):
        raise ConsumerReadError(
            f"{DA_READ_BINARY_NAME} printed JSON without a \"files\" list of one entry per "
            "file (an older da-read? rebuild it)", lines)
    results = []
    for want, original, entry in zip(absolute, given, files):
        if not isinstance(entry, dict) or entry.get("path") != want:
            raise ConsumerReadError(
                f"{DA_READ_BINARY_NAME} printed an entry that is not {want}'s", lines)
        problem, docs = entry.get("problem"), entry.get("documents")
        if problem is not None:
            if (not isinstance(problem, dict) or not isinstance(problem.get("message"), str)
                    or docs is not None):
                raise ConsumerReadError(
                    f"{DA_READ_BINARY_NAME} printed a malformed problem for {want}", lines)
            results.append(RuleFile(original, None, problem["message"]))
        elif isinstance(docs, list) and len(docs) == 1 and isinstance(docs[0], dict):
            results.append(RuleFile(original, docs, None))
        else:
            raise ConsumerReadError(
                f"{DA_READ_BINARY_NAME} printed neither documents nor a problem for {want}", lines)
    refused = any(r.problem is not None for r in results)
    if refused != (proc.returncode == 2):
        raise ConsumerReadError(
            f"{DA_READ_BINARY_NAME} exited {proc.returncode} but its JSON "
            f"{'refuses' if refused else 'refuses no'} file", lines)
    return results


def require_read(results: "list[RuleFile]") -> "list[RuleFile]":
    """*results* when every file is read; :class:`RuleFilesRefused` (carrying
    them all) when any is refused. For a tool that must not go on past a
    rule file Prometheus would not load — a generator, say."""
    if any(r.problem is not None for r in results):
        raise RuleFilesRefused(results)
    return results


def prometheus_rule_files(paths: "Iterable[str | os.PathLike[str]]",
                          binary: str | None = None) -> "list[RuleFile]":
    """Every rule file of *paths* as Prometheus loads it, in the order given.
    Raises :class:`RuleFilesRefused` when Prometheus' loader refuses any of
    them (its ``results`` still holds every file's)."""
    return require_read(prometheus_rule_file_results(paths, binary))


def prometheus_rule_text_results(texts: "Iterable[tuple[str, str | bytes]]",
                                 binary: str | None = None) -> "list[RuleFile]":
    """Rule files held as text — the inner strings of a rules ConfigMap, say —
    as Prometheus loads them, all in one run of ``da-read prometheus-rules``.

    *texts* is ``(name, text)`` pairs; the result is one :class:`RuleFile`
    per pair, in order, whose ``path`` is that *name* (refused ones included,
    as ``prometheus_rule_file_results``). ``da-read`` takes standard input
    once, so the texts are written to files in a private temporary directory
    (mode 0700, removed before this returns) and handed over by path; nothing
    of the temporary paths is left in the result.
    """
    pairs = [(name, text.encode("utf-8") if isinstance(text, str) else text)
             for name, text in texts]
    if not pairs:
        return []
    with tempfile.TemporaryDirectory(prefix="da-read-rules-") as tmp:
        os.chmod(tmp, 0o700)   # mkdtemp's mode already; said here, not assumed
        paths = []
        for i, (_name, data) in enumerate(pairs):
            p = os.path.join(tmp, f"{i}.yml")
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            paths.append(p)
        results = prometheus_rule_file_results(paths, binary)
    out = []
    for (name, _data), r in zip(pairs, results):
        problem = None if r.problem is None else r.problem.replace(r.path, name)
        out.append(RuleFile(name, None if problem is not None else r.documents, problem))
    return out


#: ``_rule_file_declaration``'s answers.
RULE_FILE = "rule-file"
NOT_A_RULE_FILE = "not-a-rule-file"
UNDECIDED = "undecided"


def _documents_declare_rule_groups(docs: "list[Any]") -> bool:
    """Whether a reading *docs* sets out to be a rule file: one of the
    documents is a mapping with a ``groups`` key. ⛔ Only this module asks
    it: which files hold rule files is ``rule_files_in``'s answer, never a
    tool's own (``tests/shared/test_rule_file_classifier_is_one.py``)."""
    return any(isinstance(d, dict) and "groups" in d for d in docs)


def _rule_file_declaration(text: "str | bytes", binary: str | None = None) -> str:
    """Whether *text* — a ConfigMap data value — sets out to be a rule file,
    by its any-YAML reading (``da-crdecode --any-shape``: a rule file is no
    Kubernetes object, so the manifest reading would refuse every one).
    Three answers:

    * ``RULE_FILE`` — a document is a mapping with a ``groups`` key;
    * ``NOT_A_RULE_FILE`` — it reads, and no document has one (a recipe,
      an ``alertmanager.yml``);
    * ``UNDECIDED`` — that reading refuses the text, so it cannot say. This
      is not "no": sigs.k8s.io/yaml's conversion refuses some text
      Prometheus' rule loader reads (an annotation value ``.inf`` is +Inf to
      sigs.k8s.io/yaml and has no JSON; rulefmt reads the string ``".inf"``),
      and a broken rule file (a tab in its indentation) is refused by both."""
    try:
        docs = any_yaml_text_documents(text, binary)
    except InputRefused:
        return UNDECIDED
    return RULE_FILE if _documents_declare_rule_groups(docs) else NOT_A_RULE_FILE


def _configmap_value_is_rule_file(text: "str | bytes", crdecode_binary: str | None = None,
                                  read_binary: str | None = None) -> bool:
    """Whether a ConfigMap data value goes to the rule loader. ``RULE_FILE``
    does, ``NOT_A_RULE_FILE`` does not. An ``UNDECIDED`` value goes only when
    the rule loader loads a ``groups`` key from it.

    ⚠️ A limit, not a verdict: whether a data value IS a rule file is decided
    by its consumer — where the ConfigMap is mounted and which keys the
    Prometheus ``rule_files`` glob takes — and none of that is in the value.
    A ConfigMap also carries files that are no YAML at all (the Alertmanager
    templates of k8s/03-monitoring/configmap-alertmanager.yaml, which both
    readings refuse), so a value both readings refuse is taken for such a
    file. That is a silent skip: a rule file broken so badly that neither
    da-crdecode nor the rule loader reads it (a tab in its indentation, say)
    is not read and not reported when it sits in a ConfigMap — as on main,
    where PyYAML's parse error on a data value was passed over. A whole file
    is not judged this way (``rule_files_in``)."""
    decl = _rule_file_declaration(text, crdecode_binary)
    if decl != UNDECIDED:
        return decl == RULE_FILE
    (rf,) = prometheus_rule_text_results([("text", text)], read_binary)
    return rf.problem is None and _documents_declare_rule_groups(rf.documents)


#: ``(manifest, kind, key, text | None, problem | None, metadata.name | None)``
_Keyed = "tuple[str, str, str, str | None, str | None, str | None]"


#: The kinds whose documents deliver rule files to Prometheus.
RULE_FILE_KINDS = ("ConfigMap", "PrometheusRule")

#: The PrometheusRule CRD schema (its openAPIV3Schema), the one the
#: docs' CRD lint validates against (`make crd-schemas` vendors it from the
#: pinned upstream CRD; docs/schemas/crd/SOURCES.yaml). The da-tools image
#: ships it beside this module (build.sh REPO_DATA_FILES).
PROMETHEUSRULE_SCHEMA = "PrometheusRule_v1.json"
_PROMETHEUSRULE_SCHEMA_IN_REPO = ("docs", "schemas", "crd", "monitoring.coreos.com",
                                  PROMETHEUSRULE_SCHEMA)
_SCHEMA_CACHE: "dict[str, dict]" = {}


def _prometheusrule_schema() -> dict:
    """The vendored PrometheusRule schema: beside this module (the image),
    else under the repo root above it. Raises ConsumerReadError when it is in
    neither place — no shape is then judged, and that is no answer."""
    if PROMETHEUSRULE_SCHEMA in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[PROMETHEUSRULE_SCHEMA]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, PROMETHEUSRULE_SCHEMA)]
    d = here
    while True:
        candidates.append(os.path.join(d, *_PROMETHEUSRULE_SCHEMA_IN_REPO))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for c in candidates:
        if os.path.isfile(c):
            with open(c, encoding="utf-8") as fh:
                _SCHEMA_CACHE[PROMETHEUSRULE_SCHEMA] = json.load(fh)
            return _SCHEMA_CACHE[PROMETHEUSRULE_SCHEMA]
    raise ConsumerReadError(f"the PrometheusRule CRD schema ({PROMETHEUSRULE_SCHEMA}) was "
                            "not found beside _lib_consumer_read.py or under "
                            + "/".join(_PROMETHEUSRULE_SCHEMA_IN_REPO))


_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}

#: A ConfigMap's top-level fields (k8s.io/api core/v1 ConfigMap: TypeMeta,
#: ObjectMeta, Immutable, Data, BinaryData). Any other top-level key is an
#: unknown field, which ``kubectl apply`` (``--validate=strict``, the
#: default) refuses: ``strict decoding error: unknown field "<name>"``.
CONFIGMAP_FIELDS = ("apiVersion", "kind", "metadata", "data", "binaryData", "immutable")


def binary_data_bytes(value: Any) -> bytes:
    """A ConfigMap ``binaryData`` value's bytes, decoded as the API server
    decodes a ``[]byte`` field: Go's ``encoding/json`` uses
    ``base64.StdEncoding``, whose decoder skips ``\\r`` and ``\\n`` (a value
    folded over lines is read) and refuses every other character outside the
    standard alphabet and its ``=`` padding. Raises ``binascii.Error`` /
    ``ValueError`` for a value it refuses (and for a value that is not a
    string)."""
    if not isinstance(value, str):
        raise ValueError(f"a binaryData value is {type(value).__name__}, not a string")
    return base64.b64decode(value.replace("\r", "").replace("\n", ""), validate=True)


def rule_object_problem(doc: "dict[str, Any]") -> "str | None":
    """Why the API server would refuse the ConfigMap or PrometheusRule *doc*
    (one Kubernetes object of kubectl's reading) as a whole, or None — the
    one judgement of these two kinds' shape (#3426); None for another kind.

    * Both: ``metadata.name`` must be a non-empty string (kubectl: "resource
      name may not be empty").
    * ConfigMap (its Go type, k8s.io/api core/v1): a top-level key that is
      not one of its fields (``CONFIGMAP_FIELDS``) is an unknown field, which
      the API server's strict field validation refuses with ``strict
      decoding error: unknown field "<name>"`` — a data key written one
      indent short (``rules.yml: |`` beside ``data:``) is one. ``data`` is
      a ``map[string]string`` and ``binaryData`` a ``map[string][]byte``, so
      either, when present and not null, must be a mapping. (A value of the
      wrong type is a per-key problem of its rule file.)
    * PrometheusRule (the vendored CRD schema, ``PROMETHEUSRULE_SCHEMA``):
      every key the schema ``required`` must be there; a top-level key the
      schema's ``properties`` does not name is an unknown field, which
      ``kubectl apply`` (``--validate=strict``, the default) refuses — the
      API server's strict field validation prunes it and answers
      ``strict decoding error: unknown field "groups"`` (measured with
      apiextensions-apiserver v0.37.0's pruning on this schema); a
      top-level property of the wrong JSON type (``spec`` not a mapping) is
      refused by the schema's ``type``. Below the top level the rule loader
      judges ``spec``."""
    kind = doc.get("kind")
    if kind not in RULE_FILE_KINDS:
        return None
    meta = doc.get("metadata")
    name = meta.get("name") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name:
        return f"a {kind} with no string metadata.name (kubectl: resource name may not be empty)"
    if kind == "ConfigMap":
        unknown = sorted(k for k in doc if k not in CONFIGMAP_FIELDS)
        if unknown:
            fields = ", ".join(f'unknown field "{k}"' for k in unknown)
            return (f"ConfigMap {name}: the API server refuses it (strict decoding error: "
                    f"{fields}; a ConfigMap has no such top-level field — a data key goes "
                    "under data, indented)")
        for field in ("data", "binaryData"):
            value = doc.get(field)
            if value is not None and not isinstance(value, dict):
                return (f"ConfigMap {name}: its {field} is {type(value).__name__}, not a "
                        f"mapping (a ConfigMap's {field} is a map of key to "
                        f"{'string' if field == 'data' else 'base64'})")
        return None
    schema = _prometheusrule_schema()
    props = schema.get("properties") or {}
    unknown = sorted(k for k in doc if k not in props)
    if unknown:
        fields = ", ".join(f'unknown field "{k}"' for k in unknown)
        return (f"PrometheusRule {name}: the API server refuses it (strict decoding error: "
                f"{fields}; the CRD schema has no such top-level field — rule groups go "
                "under spec)")
    missing = [k for k in schema.get("required") or [] if doc.get(k) is None]
    if missing:
        return f"PrometheusRule {name}: {', '.join(missing)}: Required value (the CRD schema)"
    for key, sub in props.items():
        want = _JSON_TYPES.get(sub.get("type") if isinstance(sub, dict) else None)
        if want is not None and key in doc and doc[key] is not None \
                and not isinstance(doc[key], want):
            return (f"PrometheusRule {name}: {key} in body must be of type {sub['type']} "
                    f"(the CRD schema), not {type(doc[key]).__name__}")
    return None


class _ObjectRefused(Exception):
    """A rule-file-delivering object the API server would refuse
    (``rule_object_problem``)."""


def _manifest_keyed(path: str, docs: "list[Any]", only_declared: bool,
                    crdecode_binary: str | None, binary: str | None) -> "list[Any]":
    """The rule files the documents *docs* of the manifest *path* (kubectl's
    reading, every one an object) deliver, before the rule loader reads them
    (see ``manifest_rule_file_results``)."""
    keyed: "list[Any]" = []
    for i, doc in enumerate(docs, start=1):   # each a Kubernetes object
        kind = doc["kind"]
        # An object the API server would refuse makes the file unreadable,
        # not one without rule files.
        problem = rule_object_problem(doc)
        if problem is not None:
            raise _ObjectRefused(f"object {i} of kubectl's reading: {problem}")
        if kind not in RULE_FILE_KINDS:
            continue
        name = doc["metadata"]["name"]
        if kind == "PrometheusRule":
            keyed.append((path, kind, "spec", json.dumps(doc["spec"]), None, name))
        elif kind == "ConfigMap":
            data = doc.get("data") or {}
            for key, value in data.items():
                if isinstance(value, str):
                    if only_declared and not _configmap_value_is_rule_file(
                            value, crdecode_binary, binary):
                        continue
                    keyed.append((path, kind, key, value, None, name))
                else:
                    keyed.append((path, kind, key, None,
                                  "a ConfigMap data value must be a string", name))
            bdata = doc.get("binaryData") or {}
            for key, value in bdata.items():
                try:
                    text = binary_data_bytes(value).decode("utf-8")
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    keyed.append((path, kind, key, None,
                                  "binaryData value is not base64 of UTF-8 text", name))
                else:
                    if only_declared and not _configmap_value_is_rule_file(
                            text, crdecode_binary, binary):
                        continue
                    keyed.append((path, kind, key, text, None, name))
    return keyed


def _read_keyed(keyed: "list[Any]", binary: str | None) -> "list[RuleFile]":
    """The rule loader's reading of every keyed rule file, all in one run."""
    readable = [(f"{p}#{k}", t) for p, _kind, k, t, _, _n in keyed if t is not None]
    read = iter(prometheus_rule_text_results(readable, binary))
    out = []
    for path, kind, key, text, problem, name in keyed:
        r = next(read) if text is not None else RuleFile(f"{path}#{key}", None, problem)
        r.manifest, r.kind, r.key, r.object_name = path, kind, key, name
        out.append(r)
    return out


def manifest_rule_file_results(paths: "Iterable[str | os.PathLike[str]]", *,
                               only_declared: bool = False,
                               crdecode_binary: str | None = None,
                               binary: str | None = None) -> "list[RuleFile]":
    """The rule files the Kubernetes manifests at *paths* deliver to
    Prometheus, as Prometheus loads them — for files a caller KNOWS are
    manifests (a file that may be a plain rule file goes to
    ``rule_files_in``). Each manifest is read the way kubectl reads it
    (``da-crdecode``; a ``kind: List`` as its items); then, all in one
    ``da-read prometheus-rules`` run:

    * every ``data`` value and every ``binaryData`` value (base64, UTF-8) of a
      ``kind: ConfigMap`` document — the files a mounted ConfigMap holds;
    * the ``spec`` of a ``kind: PrometheusRule`` document (as JSON, which is
      YAML) — the rule file the Prometheus Operator writes from it.

    One :class:`RuleFile` per (manifest, key), in file order, with
    ``manifest`` / ``kind`` / ``key`` / ``object_name`` (the document's
    ``metadata.name``) set (``key`` is the data key, or ``spec`` for a
    PrometheusRule) and ``path`` ``<manifest>#<key>``. A value that cannot be
    a file (a data value that is not a string; ``binaryData`` that is not
    base64 of UTF-8) is a :class:`RuleFile` with that problem. Documents of
    other kinds are not read. With *only_declared*, a ConfigMap value is read
    only when it sets out to be a rule file (a ``groups`` key in its YAML
    reading, or — when no YAML reading reads it — in the rule loader's) — for
    a ConfigMap that may also carry other files (an Alertmanager template);
    without it every value is a rule file and one that is not is refused.
    Raises :class:`InputRefused` naming the manifest when kubectl's reading
    refuses it — a document in it that is no Kubernetes object (a top-level
    list, a mapping with no ``kind``), or a ``kind: List`` item kubectl would
    not apply, included (#3426): no document of a manifest is passed over
    because of its shape — and when a ConfigMap or PrometheusRule in it is
    one the API server would refuse (``rule_object_problem``: a ``data`` that
    is no mapping, a PrometheusRule with a top-level ``groups`` or no
    ``spec``), never taken for one with no rule files.
    """
    keyed: "list[Any]" = []
    for path in paths:
        path = os.fspath(path)
        try:
            docs = kubernetes_documents(path, binary=crdecode_binary)
        except InputRefused as e:
            raise InputRefused(f"{path}: {e}", e.stderr_lines) from e
        try:
            keyed += _manifest_keyed(path, docs, only_declared, crdecode_binary, binary)
        except _ObjectRefused as e:
            raise InputRefused(f"{path}: the API server would refuse it", [str(e)]) from e
    return _read_keyed(keyed, binary)


class RuleFileSourcesRefused(InputRefused):
    """Files ``rule_files_in`` cannot tell the rule files of: no reader reads
    them as a manifest or as a rule file. ``files`` is ``[(path, reason)]``,
    every such file, in the order given; ``stderr_lines`` names each."""

    def __init__(self, files: "list[tuple[str, str]]") -> None:
        super().__init__(f"{len(files)} file(s) no reader reads as a Kubernetes manifest "
                         "or a rule file", [f"{p}: {why}" for p, why in files])
        self.files = list(files)


#: What a tool whose files are chosen by its caller (a CLI taking files and
#: directories) adds to the reason a file is refused: the file was chosen by
#: path, so leaving it out is the caller's call, by path (#3426).
EXCLUDE_HINT = "if it is no rule file, leave it out with --exclude"


def rule_files_in(paths: "Iterable[str | os.PathLike[str]]", *,
                  only_declared: bool = True,
                  keep_refusals: bool = False,
                  crdecode_binary: str | None = None,
                  binary: str | None = None) -> "dict[str, list[RuleFile]]":
    """⛔ THE one answer to "which rule files does each file hand to
    Prometheus" (#3426). No tool decides it from a reading of its own.

    ⛔ Which files are read is the CALLER's declaration, by path
    (``files_to_read``'s ``--exclude``, a lint's glob list): every path given
    here is taken to be a rule file source, and one that turns out to be
    neither a manifest nor a rule file is refused, named — never taken for a
    file "with no rule files". Nothing here judges from a file's content that
    it is not meant to be read (#3426: no content test told a manifest with
    no ``kind`` or a rule pack with ``grups:`` from a Chart.yaml).

    Each path (keyed ``os.fspath(path)``, in the order given) is read:

    1. as the Kubernetes manifest kubectl reads (``da-crdecode``; a
       ``kind: List`` is its items): its rule files are its ConfigMaps'
       ``data`` / ``binaryData`` values and its PrometheusRules' ``spec``
       (``manifest_rule_file_results``, *only_declared* as there). A manifest
       kubectl reads whose objects are of other kinds (a Deployment) hands
       Prometheus no rule file (``[]``): it is read, as kubectl reads it,
       and holds none. A file with no object at all (empty, comments only,
       ``---`` only, a List with no items) is refused, as kubectl refuses
       it ("no objects passed to apply"). A ConfigMap or PrometheusRule the API server would
       refuse (``rule_object_problem``) makes the file refused.
    2. When that reading refuses it: with the any-YAML reading
       (``da-crdecode --any-shape``). A file with a document that is a
       mapping with a ``groups`` key is a plain rule file (one
       :class:`RuleFile`, ``kind`` / ``key`` None, ``manifest`` the path);
       the rule loader gives the verdict on its content (a second document,
       say). Any other file is refused, with kubectl's reason.
    3. When that reading refuses it too (sigs.k8s.io/yaml has no JSON for an
       annotation value ``.inf``, say): Prometheus' rule loader decides. A
       file it reads ``groups`` from is a rule file; any other is refused.

    Refused files — every one, each with its readers' reason — raise
    :class:`RuleFileSourcesRefused`; with *keep_refusals* each is instead a
    :class:`RuleFile` with that ``problem`` and ``unread`` True, for a caller
    that reports per file. A rule file the rule loader refuses is a
    :class:`RuleFile` with its ``problem`` either way (``require_read``
    raises on it). Raises :class:`ConsumerReadError` when a binary cannot
    be had."""
    keys = [os.fspath(p) for p in paths]
    out: "dict[str, list[RuleFile]]" = {p: [] for p in keys}
    if not keys:
        return out
    manifests = kubernetes_documents_of(keys, crdecode_binary, keep_refusals=True)
    not_manifests = [p for p in keys if isinstance(manifests[p], ConsumerReadError)]
    as_yaml = any_yaml_documents_of(not_manifests, crdecode_binary, keep_refusals=True)
    plain, undecided, refused = [], [], {}
    for p in not_manifests:
        docs = as_yaml[p]
        if isinstance(docs, ConsumerReadError):
            undecided.append(p)
        elif _documents_declare_rule_groups(docs):
            plain.append(p)
        else:
            refused[p] = (f"kubectl's reading refuses it ({_reason(manifests[p])}), and it is "
                          "no rule file (no document is a mapping with a `groups` key)")
    keyed: "list[Any]" = []
    for p in keys:
        if p not in manifests or isinstance(manifests[p], ConsumerReadError):
            continue
        if not manifests[p]:
            # Empty, comments only, `---` only, a List with no items: no
            # object and no rule group. kubectl refuses to apply it.
            refused[p] = ("kubectl's reading finds no object in it (kubectl: no objects "
                          "passed to apply), and it is no rule file (no `groups`)")
            continue
        try:
            keyed += _manifest_keyed(p, manifests[p], only_declared, crdecode_binary, binary)
        except _ObjectRefused as e:
            refused[p] = f"the API server would refuse it: {e}"
    for rf in _read_keyed(keyed, binary):
        out[rf.manifest].append(rf)
    for p, rf in zip(plain, prometheus_rule_file_results(plain, binary)):
        rf.manifest = p
        out[p].append(rf)
    for p, rf in zip(undecided, prometheus_rule_file_results(undecided, binary)):
        if rf.problem is None and _documents_declare_rule_groups(rf.documents):
            rf.manifest = p
            out[p].append(rf)
            continue
        loader = (f"Prometheus' rule loader refuses it: {rf.problem}" if rf.problem is not None
                  else "Prometheus' rule loader finds no `groups` in it")
        refused[p] = (f"neither kubectl's reading ({_reason(manifests[p])}) nor the "
                      f"any-YAML reading ({_reason(as_yaml[p])}) reads it; {loader}")
    if refused and not keep_refusals:
        raise RuleFileSourcesRefused([(p, refused[p]) for p in keys if p in refused])
    for p, why in refused.items():
        rf = RuleFile(p, None, why)
        rf.manifest, rf.unread = p, True
        out[p] = [rf]
    return out


#: The suffixes a directory given to ``files_to_read`` is walked for,
#: compared without regard to case (``.YAML`` and ``.Yml`` are YAML too).
YAML_SUFFIXES = (".yaml", ".yml")


def _is_yaml_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in YAML_SUFFIXES


class SourcesUnlisted(RuleFileSourcesRefused):
    """Entries ``files_to_read`` / ``rule_pack_files`` would have chosen but
    cannot read — a dangling symlink, a FIFO or other non-regular file with
    a YAML suffix, a directory that cannot be listed — or (in rule-packs/)
    a YAML file the declaration does not cover. Each is named, never
    skipped (#3426)."""

    def __init__(self, files: "list[tuple[str, str]]") -> None:
        InputRefused.__init__(self, f"{len(files)} rule-file source entry/entries cannot "
                              "be chosen to read", [f"{p}: {why}" for p, why in files])
        self.files = list(files)

class EverythingExcluded(InputRefused):
    """``--exclude`` left no file to read: the files found were all
    excluded. A run that reads nothing because of its own exclusions is no
    clean run (#3426)."""

    def __init__(self, found: int, patterns: "list[str]") -> None:
        super().__init__(
            f"--exclude left no file to read: {found} file(s) found, every one excluded "
            f"by {', '.join(repr(p) for p in patterns)}")
        self.found = found


def excluded(pattern: str, name: str, relative: str) -> bool:
    """Whether the ``--exclude`` *pattern* leaves out a file whose file name
    is *name* and whose path relative to the directory it was found under
    (or, for a file given as such, its path as given) is *relative*, POSIX
    separators. A pattern with no ``/`` is matched against the file name
    (``Chart.yaml``, ``*-values.yaml``: at any depth); a pattern with a ``/``
    against the relative path (``recipes/*``). ``fnmatch.fnmatchcase``:
    case-sensitive, and ``*`` also matches ``/`` (``recipes/*`` takes
    ``recipes/a/b.yaml`` too)."""
    if "/" in pattern:
        return fnmatch.fnmatchcase(relative, pattern)
    return fnmatch.fnmatchcase(name, pattern)


#: Why a symlinked directory under a walked target is named, not walked.
_DIR_SYMLINK = "a symlink to a directory is not followed; give its target instead"


def _dir_symlink_problem(link: str, root_real: str) -> "str | None":
    """Why the symlink to a directory *link*, under a walked tree whose
    real path is *root_real*, must be named — or None when nothing behind
    it can be lost. It is never followed (a loop, a walk out of the tree);
    a target that resolves INSIDE the tree is walked by its real path
    anyway (kubelet's ConfigMap mount: ``..data -> ..2024_01_01/``; a
    ``loop -> .``), so it is passed over. A target outside the tree, or one
    that does not resolve (a loop of links), is named."""
    try:
        target = os.path.realpath(link, strict=True)
    except (OSError, RuntimeError):
        return f"{_DIR_SYMLINK} (it does not resolve)"
    if os.path.commonpath([target, root_real]) == root_real:
        return None
    return _DIR_SYMLINK


def _not_regular(path: str) -> str:
    """Why *path* — an entry that exists as a link or a node but is no
    regular file or directory — cannot be read."""
    return ("a dangling symlink" if os.path.islink(path) and not os.path.exists(path)
            else "not a regular file")


def _walk_yaml(root: str) -> "tuple[list[tuple[str, str]], list[tuple[str, str]]]":
    """(files, problems) under the directory *root*: every entry with a
    YAML suffix (any case) as ``(path, path relative to root)``, and every
    entry that cannot be read as ``(path, why)`` — a directory that cannot
    be listed, a YAML-suffixed entry that is not a regular file (a dangling
    symlink, a FIFO), and a symlink to a directory whose target is outside
    the tree or does not resolve (``_dir_symlink_problem``). A symlinked
    directory is never followed (a loop, or a walk out of the tree); one
    whose target is inside the tree is passed over, its content walked by
    its real path; any other is named, so the caller gives its target
    instead — nothing behind it is lost in silence."""
    files, problems = [], []
    root_real = os.path.realpath(root)

    def unlisted(err: OSError) -> None:
        problems.append((err.filename or root, f"cannot list this directory: {err.strerror}"))

    for d, dirs, names in os.walk(root, onerror=unlisted):
        for n in dirs:
            f = os.path.join(d, n)
            if os.path.islink(f):
                why = _dir_symlink_problem(f, root_real)
                if why is not None:
                    problems.append((f, why))
        for n in names:
            if not _is_yaml_name(n):
                continue
            f = os.path.join(d, n)
            if os.path.isfile(f):
                files.append((f, pathlib.Path(os.path.relpath(f, root)).as_posix()))
            else:
                problems.append((f, _not_regular(f)))
    return files, problems


def files_to_read(targets: "Iterable[str | os.PathLike[str]]",
                  exclude: "Iterable[str]" = ()) -> "list[str]":
    """⛔ The one answer to "which files does a rule-file CLI read" (#3426):
    ``lint``, ``silencer-drift-check --rule-source`` and ``onboard
    --rule-files`` all take their files from here, so their ``--exclude``
    means one thing.

    A target that is a file is read as given, whatever its suffix; a
    directory is walked, recursively, for files whose suffix is ``.yaml``
    or ``.yml`` in any case (``YAML_SUFFIXES``). A file any *exclude*
    pattern matches (``excluded``: relative to the directory target it was
    found under) is left out and not read. Returns the paths, sorted, each
    once. A target that is not there at all (``os.path.lexists`` false)
    adds nothing: callers check their targets first; a target that is
    there but neither a directory nor a regular file (a dangling symlink a
    glob returned, a FIFO) is named like an entry of a walked directory.

    Raises :class:`SourcesUnlisted` naming every entry that would be
    chosen but cannot be read (a dangling symlink, a non-regular file, a
    symlink to a directory outside the tree — not followed — or a directory
    that cannot be listed), unless an *exclude* pattern leaves it out;
    :class:`EverythingExcluded` when entries were found — readable or not
    — and *exclude* left none of them."""
    patterns = list(exclude)
    chosen: "set[str]" = set()
    problems: "list[tuple[str, str]]" = []
    found = 0
    for target in targets:
        t = os.fspath(target)
        if os.path.isdir(t):
            candidates, unread = _walk_yaml(t)
        elif os.path.isfile(t):
            candidates, unread = [(t, pathlib.Path(t).as_posix())], []
        elif os.path.lexists(t):
            candidates, unread = [], [(t, _not_regular(t))]
        else:
            continue
        for f, relative in candidates:
            found += 1
            if not any(excluded(pat, os.path.basename(f), relative) for pat in patterns):
                chosen.add(f)
        for f, why in unread:
            found += 1
            relative = (pathlib.Path(t).as_posix() if f == t
                        else pathlib.Path(os.path.relpath(f, t)).as_posix())
            if not any(excluded(pat, os.path.basename(f), relative) for pat in patterns):
                problems.append((f, why))
    if problems:
        raise SourcesUnlisted(sorted(problems))
    if found and not chosen and patterns:
        raise EverythingExcluded(found, patterns)
    return sorted(chosen)


#: rule-packs/, declared by path (#3426): the rule packs are the files these
#: globs take; the other YAML that sits beside them is named here, not
#: judged by its content. A YAML file in rule-packs/ (top level, any case of
#: suffix) that is neither is refused, so a pack named another way cannot
#: slip past. ⛔ The one declaration: `lint --rule-pack-dir`,
#: `silencer-drift-check --rule-pack-dir`, `validate-config --rule-packs`,
#: the CI workflows and the repo lints all choose rule packs through
#: ``rule_pack_files`` — no caller lists `--exclude` patterns for it.
RULE_PACK_GLOBS = ("rule-pack-*.yaml", "rule-pack-*.yml")
RULE_PACKS_OTHER_YAML = ("threshold-registry.yaml",)


def rule_pack_files(rule_packs: "str | os.PathLike[str]") -> "list[pathlib.Path]":
    """The rule packs of the directory *rule_packs* (``RULE_PACK_GLOBS``,
    top level), sorted. Raises :class:`SourcesUnlisted` naming every
    top-level entry with a YAML suffix (any case) that is neither a rule
    pack nor one of ``RULE_PACKS_OTHER_YAML``, every such entry that is not
    a regular file (a dangling symlink), and a directory that cannot be
    listed — a declaration that does not cover the directory is no
    answer."""
    root = pathlib.Path(rule_packs)
    try:
        entries = sorted(root.iterdir())
    except OSError as e:
        raise SourcesUnlisted([(str(root), f"cannot list this directory: {e.strerror}")]) from e
    packs, problems = [], []
    for f in entries:
        if not _is_yaml_name(f.name):
            continue
        is_pack = any(fnmatch.fnmatchcase(f.name, g) for g in RULE_PACK_GLOBS)
        if not (is_pack or f.name in RULE_PACKS_OTHER_YAML):
            problems.append((str(f), (
                "a YAML file in rule-packs/ that is neither a rule pack ("
                + " / ".join(RULE_PACK_GLOBS) + ") nor declared beside them ("
                + ", ".join(RULE_PACKS_OTHER_YAML) + "); name a rule pack rule-pack-*.yaml, "
                "or declare the file in _lib_consumer_read.RULE_PACKS_OTHER_YAML")))
        elif not f.is_file():
            problems.append((str(f), "a dangling symlink" if f.is_symlink() and not f.exists()
                             else "not a regular file"))
        elif is_pack:
            packs.append(f)
    if problems:
        raise SourcesUnlisted(problems)
    return packs


def _reason(e: ConsumerReadError) -> str:
    """A refusal's reason in one line: the binary's stderr, else the message."""
    return "; ".join(e.stderr_lines) or str(e)
