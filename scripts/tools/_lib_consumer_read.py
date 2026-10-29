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
  :class:`RuleFileRefused`.

Each raises :class:`ConsumerReadError` when the binary is missing, cannot
be run, does not finish, refuses the input (its reason is in
``stderr_lines``) or prints something that is not its JSON contract. Nothing
here returns "empty" for "could not be read".

Binaries: an explicit path (a tool's ``--da-crdecode-binary`` /
``--da-read-binary``), else ``$DA_CRDECODE_BINARY`` / ``$DA_READ_BINARY``,
else ``da-crdecode`` / ``da-read`` on ``$PATH`` (the da-tools image ships
both in /usr/local/bin). In this repo ``make da-crdecode-build`` /
``make da-read-build`` build them into ``.build/``.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
        raise ConsumerReadError(f"cannot run {binary}: {e}") from e
    return proc, _stderr_lines(proc.stderr)


def _json(proc: subprocess.CompletedProcess, name: str, lines: list[str]) -> Any:
    try:
        return json.loads(proc.stdout)
    except ValueError as e:  # JSONDecodeError, or stdout that is not UTF-8
        raise ConsumerReadError(f"{name} printed output that is not JSON: {e}", lines) from e


def _run(binary: str | None, name: str, env: str, build_hint: str,
         args: list[str], stdin: bytes | None) -> dict:
    proc, lines = _invoke(binary, name, env, build_hint, args, stdin)
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
            raise ConsumerReadError(f"{path}: {res}", res.stderr_lines) from res
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
            raise ConsumerReadError(f"text #{i + 1}: {res}", res.stderr_lines) from res
    return results


def _is_refusal(e: ConsumerReadError) -> bool:
    """da-crdecode ran and refused the input, its reason on stderr."""
    return bool(e.stderr_lines) and str(e).startswith(f"{CRDECODE_BINARY_NAME} exited")


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


class RuleFileRefused(ConsumerReadError):
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

    __slots__ = ("path", "problem", "_documents")

    def __init__(self, path: str, documents: "list[Any] | None", problem: "str | None") -> None:
        self.path = path
        self.problem = problem
        self._documents = documents

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


class RuleFilesRefused(ConsumerReadError):
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
    # rc 2 with the JSON on stdout is "a file is refused"; rc 2 with nothing
    # on stdout is a usage error.
    if proc.returncode not in (0, 2) or not proc.stdout.strip():
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


def prometheus_rule_files(paths: "Iterable[str | os.PathLike[str]]",
                          binary: str | None = None) -> "list[RuleFile]":
    """Every rule file of *paths* as Prometheus loads it, in the order given.
    Raises :class:`RuleFilesRefused` when Prometheus' loader refuses any of
    them (its ``results`` still holds every file's)."""
    results = prometheus_rule_file_results(paths, binary)
    if any(r.problem is not None for r in results):
        raise RuleFilesRefused(results)
    return results
