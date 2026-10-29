"""A file outside conf.d, read the way its consumer reads it (ADR-036 §12, #3206).

The Python tools do not read these files with PyYAML: PyYAML's reading is
YAML 1.1 as PyYAML implements it, which is neither kubectl's nor
Alertmanager's. They ask the Go binary that runs the consumer's own reading:

* ``kubernetes_documents(path)`` — a Kubernetes manifest (or Helm values
  file) as kubectl reads it: ``da-crdecode`` (sigs.k8s.io/yaml, documents
  split as ``kubectl apply -f`` splits them). One entry per document kubectl
  would apply, in file order; an empty file has none. ``instance: 010`` is
  the number 8 here, as it is to kubectl.
* ``kubernetes_text_documents(text)`` — the same reading of text held in
  memory (a manifest ``kubectl ... -o yaml`` or ``helm template`` printed, a
  ConfigMap a tool assembled), handed over on standard input.
* ``kubernetes_documents_of(paths)`` / ``kubernetes_text_documents_each(texts)``
  — the same for several files / texts. da-crdecode reads one input per run,
  so these run one per input, a few at a time (``PARALLEL_RUNS``), and return
  them in the order given.
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
  from). ``rule_file_declaration(text)`` says whether a text sets out to be
  a rule file (yes / no / undecided: kubectl's reading refuses it);
  ``may_be_rule_file`` (a whole file: undecided goes to the rule loader)
  and ``configmap_value_is_rule_file`` (a ConfigMap value, which may be a
  template no reader reads) build on it.

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
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

CRDECODE_BINARY_NAME = "da-crdecode"
CRDECODE_ENV = "DA_CRDECODE_BINARY"
DA_READ_BINARY_NAME = "da-read"
DA_READ_ENV = "DA_READ_BINARY"
TIMEOUT_SECONDS = 60


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
    about the input. Not found, could not run, did not finish, any other exit
    code, exit 0 without the JSON contract: a plain ConsumerReadError.
    """


#: A Go runtime crash: the first stderr line of an unrecovered panic or a
#: fatal runtime error (both exit 2, as a refusal does).
_GO_CRASH_PREFIXES = ("panic: ", "fatal error: ")


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


def kubernetes_documents(path: str, binary: str | None = None) -> list[Any]:
    """The documents of the Kubernetes manifest at *path*, as kubectl reads
    them (``da-crdecode``): one per document kubectl would apply, in order."""
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)
    # An absolute path never starts with `-`, so it is not read as stdin.
    out = _run(resolved, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build",
               [os.path.abspath(path)], None)
    return out["documents"]


def kubernetes_text_documents(text: str | bytes, binary: str | None = None) -> list[Any]:
    """The documents of the Kubernetes manifest *text*, as kubectl reads them
    (``da-crdecode -``): for text a tool holds rather than a file."""
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)
    data = text.encode("utf-8") if isinstance(text, str) else text
    out = _run(resolved, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build",
               ["-"], data)
    return out["documents"]


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
    keys = [str(p) for p in paths]
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)

    def one(path: str) -> "list[Any] | ConsumerReadError":
        try:
            return kubernetes_documents(path, binary=resolved)
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


def kubernetes_text_documents_each(texts: Iterable[str | bytes], binary: str | None = None, *,
                                   keep_refusals: bool = False
                                   ) -> "list[list[Any] | ConsumerReadError]":
    """``kubernetes_text_documents`` for every text in *texts*, in order, a
    few runs at a time. Errors as :func:`kubernetes_documents_of` (by
    position instead of path)."""
    items = list(texts)
    if not items:
        return []
    resolved = resolve_binary(binary, CRDECODE_ENV, CRDECODE_BINARY_NAME)
    if not resolved:
        _run(None, CRDECODE_BINARY_NAME, CRDECODE_ENV, "make da-crdecode-build", [], None)

    def one(text: str | bytes) -> "list[Any] | ConsumerReadError":
        try:
            return kubernetes_text_documents(text, binary=resolved)
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

    __slots__ = ("path", "problem", "_documents", "manifest", "kind", "key")

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


#: ``rule_file_declaration``'s answers.
RULE_FILE = "rule-file"
NOT_A_RULE_FILE = "not-a-rule-file"
UNDECIDED = "undecided"


def documents_declare_rule_groups(docs: "list[Any]") -> bool:
    """Whether a file whose Kubernetes reading (da-crdecode) is *docs* sets
    out to be a rule file: one of the documents is a mapping with a
    ``groups`` key. ``rule_file_declaration`` and a caller that already
    holds the documents both use it."""
    return any(isinstance(d, dict) and "groups" in d for d in docs)


def rule_file_declaration(text: "str | bytes", binary: str | None = None) -> str:
    """Whether *text* — a file, a ConfigMap data value — sets out to be a
    rule file, by its Kubernetes reading (da-crdecode). Three answers:

    * ``RULE_FILE`` — a document is a mapping with a ``groups`` key;
    * ``NOT_A_RULE_FILE`` — it reads, and no document has one (a recipe,
      the registry beside the packs);
    * ``UNDECIDED`` — that reading refuses the text, so it cannot say. This
      is not "no": kubectl's reading refuses some text Prometheus' rule
      loader reads (an annotation value ``.inf`` is +Inf to sigs.k8s.io/yaml
      and has no JSON; rulefmt reads the string ``".inf"``), and a broken
      rule file (a tab in its indentation) is refused by both.

    A whole file that is ``UNDECIDED`` is handed to the rule loader, whose
    refusal is the authoritative reason (``may_be_rule_file``). This only
    picks what to hand to the rule loader; what the file says is the rule
    loader's reading."""
    try:
        docs = kubernetes_text_documents(text, binary)
    except InputRefused:
        return UNDECIDED
    return RULE_FILE if documents_declare_rule_groups(docs) else NOT_A_RULE_FILE


def may_be_rule_file(text: "str | bytes", binary: str | None = None) -> bool:
    """Whether a whole file whose text is *text* goes to the rule loader:
    anything ``rule_file_declaration`` does not rule out."""
    return rule_file_declaration(text, binary) != NOT_A_RULE_FILE


def configmap_value_is_rule_file(text: "str | bytes", crdecode_binary: str | None = None,
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
    is not judged this way (``may_be_rule_file``)."""
    decl = rule_file_declaration(text, crdecode_binary)
    if decl != UNDECIDED:
        return decl == RULE_FILE
    (rf,) = prometheus_rule_text_results([("text", text)], read_binary)
    return rf.problem is None and documents_declare_rule_groups(rf.documents)


def manifest_rule_file_results(paths: "Iterable[str | os.PathLike[str]]", *,
                               only_declared: bool = False,
                               crdecode_binary: str | None = None,
                               binary: str | None = None) -> "list[RuleFile]":
    """The rule files the Kubernetes manifests at *paths* deliver to
    Prometheus, as Prometheus loads them. Each manifest is read the way
    kubectl reads it (``da-crdecode``); then, all in one ``da-read
    prometheus-rules`` run:

    * every ``data`` value and every ``binaryData`` value (base64, UTF-8) of a
      ``kind: ConfigMap`` document — the files a mounted ConfigMap holds;
    * the ``spec`` of a ``kind: PrometheusRule`` document (as JSON, which is
      YAML) — the rule file the Prometheus Operator writes from it.

    One :class:`RuleFile` per (manifest, key), in file order, with
    ``manifest`` / ``kind`` / ``key`` set (``key`` is the data key, or
    ``spec`` for a PrometheusRule) and ``path`` ``<manifest>#<key>``. A value
    that cannot be a file (a data value that is not a string; ``binaryData``
    that is not base64 of UTF-8) is a :class:`RuleFile` with that problem.
    Documents of other kinds are not read. With *only_declared*, a
    ConfigMap value is read only when it sets out to be a rule file
    (``configmap_value_is_rule_file``) — for a ConfigMap that may also carry other
    files (an Alertmanager template); without it every value is a rule file
    and one that is not is refused. Raises :class:`InputRefused` naming the
    manifest when kubectl's reading refuses it.
    """
    keyed: "list[tuple[str, str, str, str | None, str | None]]" = []
    for path in paths:
        path = os.fspath(path)
        try:
            docs = kubernetes_documents(path, binary=crdecode_binary)
        except InputRefused as e:
            raise InputRefused(f"{path}: {e}", e.stderr_lines) from e
        for doc in docs:
            kind = doc.get("kind") if isinstance(doc, dict) else None
            if kind == "PrometheusRule":
                spec = doc.get("spec")
                keyed.append((path, kind, "spec", json.dumps(spec if spec is not None else {}),
                              None))
            elif kind == "ConfigMap":
                data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
                for key, value in data.items():
                    if isinstance(value, str):
                        if only_declared and not configmap_value_is_rule_file(
                                value, crdecode_binary, binary):
                            continue
                        keyed.append((path, kind, key, value, None))
                    else:
                        keyed.append((path, kind, key, None,
                                      "a ConfigMap data value must be a string"))
                bdata = doc.get("binaryData") if isinstance(doc.get("binaryData"), dict) else {}
                for key, value in bdata.items():
                    try:
                        text = base64.b64decode(str(value), validate=True).decode("utf-8")
                    except (ValueError, UnicodeDecodeError, binascii.Error):
                        keyed.append((path, kind, key, None,
                                      "binaryData value is not base64 of UTF-8 text"))
                    else:
                        if only_declared and not configmap_value_is_rule_file(
                                text, crdecode_binary, binary):
                            continue
                        keyed.append((path, kind, key, text, None))
    readable = [(f"{p}#{k}", t) for p, _kind, k, t, _ in keyed if t is not None]
    read = iter(prometheus_rule_text_results(readable, binary))
    out = []
    for path, kind, key, text, problem in keyed:
        r = next(read) if text is not None else RuleFile(f"{path}#{key}", None, problem)
        r.manifest, r.kind, r.key = path, kind, key
        out.append(r)
    return out
