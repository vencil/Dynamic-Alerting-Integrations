"""A file outside conf.d, read the way its consumer reads it (ADR-036 §12, #3206).

The Python tools do not read these files with PyYAML: PyYAML's reading is
YAML 1.1 as PyYAML implements it, which is neither kubectl's nor
Alertmanager's. They ask the Go binary that runs the consumer's own reading:

* ``kubernetes_documents(path)`` — a Kubernetes manifest (or Helm values
  file) as kubectl reads it: ``da-crdecode`` (sigs.k8s.io/yaml, documents
  split as ``kubectl apply -f`` splits them). One entry per document kubectl
  would apply, in file order; an empty file has none. ``instance: 010`` is
  the number 8 here, as it is to kubectl.
* ``alertmanager_config(text)`` — an Alertmanager config as Alertmanager
  loads it: ``da-read alertmanager`` (Alertmanager's own ``config.Load``, at
  the amtool version the da-tools image ships), keeping only the keys the
  text writes. ``match: {tenant: 010}`` is the string ``"010"`` here, as it is
  to Alertmanager. Secrets are their values.

Either raises :class:`ConsumerReadError` when the binary is missing, cannot
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
from typing import Any

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


def _run(binary: str | None, name: str, env: str, build_hint: str,
         args: list[str], stdin: bytes | None) -> dict:
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
    lines = _stderr_lines(proc.stderr)
    if proc.returncode != 0:
        raise ConsumerReadError(f"{name} exited {proc.returncode}", lines)
    try:
        out = json.loads(proc.stdout)
    except ValueError as e:  # JSONDecodeError, or stdout that is not UTF-8
        raise ConsumerReadError(f"{name} printed output that is not JSON: {e}", lines) from e
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


def alertmanager_config(text: str | bytes, binary: str | None = None) -> dict:
    """The Alertmanager config *text* as Alertmanager loads it (``da-read
    alertmanager``), only the keys the text writes."""
    resolved = resolve_binary(binary, DA_READ_ENV, DA_READ_BINARY_NAME)
    data = text.encode("utf-8") if isinstance(text, str) else text
    out = _run(resolved, DA_READ_BINARY_NAME, DA_READ_ENV, "make da-read-build",
               ["alertmanager", "-"], data)
    docs = out["documents"]
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ConsumerReadError(
            f"{DA_READ_BINARY_NAME} printed JSON without exactly one config object "
            "under \"documents\"")
    return docs[0]
