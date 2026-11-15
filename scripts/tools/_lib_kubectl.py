"""Run kubectl: the one place that starts it, for reads and writes (#3207, #3210, #3539).

kubectl is an engine of ``_lib_engine`` (``ENGINES``, a proof per verb in
``PROOFS``): ``kubectl_run`` locates it and runs it there, so whether it
started at all is decided once, for every tool. It returns kubectl's
``CompletedProcess`` when kubectl ran to an exit status of its own, and
raises ``KubectlError`` when there is none to read:

* kubectl is not on PATH, or is there and cannot be started (no execute
  permission, a file the kernel cannot execute, a ``#!`` interpreter that
  does not exist) — nothing was sent to the cluster;
* kubectl does not finish within the timeout, or is killed by a signal. ⛔
  For a write (``apply``, ``patch``, ``label``, ``delete``) that says nothing
  about the cluster: the request may have been sent and applied.

kubectl's stdin is the caller's manifest when one is given; otherwise the
tool's own stdin (an exec credential plugin that prompts on the terminal
can), except for the readers below, which give it ``/dev/null`` unless
asked (``inherit_stdin``).

What a non-zero exit status means is the caller's to say (a write the
cluster refused, ``create --dry-run=client`` refusing a key name); the
readers below say it for reads.

One reader for the ops tools that ask a cluster whether something exists.
Its rule is the one #3133 set for Prometheus: a query that got no answer is
never "no data". ``kubectl_list`` returns the listed objects only when
kubectl answered (rc 0, a JSON list); every other outcome raises
``KubectlError``:

* no exit status of kubectl's own (above);
* kubectl exits non-zero — the cluster cannot be reached, the caller may not
  list the kind (forbidden), or the kind does not exist (a CRD that is not
  installed). Measured on kubectl 1.37.1 against a real kube-apiserver: a
  forbidden list exits 1 **and still prints an empty List on stdout**, so the
  exit code is read first and stdout never on its own;
* rc 0 with stdout that is not a JSON object whose ``items`` is a list of
  objects.

An empty list therefore means one thing: kubectl answered, and nothing
matched.

A single named object is read with ``kubectl_get`` — ``kubectl get <kind>
<name> --ignore-not-found -o json``: rc 0 with the object, or rc 0 with
empty stdout when it does not exist; every failure is still rc≠0. It needs
only the ``get`` verb, which a Role restricted by ``resourceNames`` grants
(a ``--field-selector`` list needs ``list``, which such a Role refuses —
measured on kubectl 1.37.1: get rc 0, field-selector list rc 1 Forbidden).

A container's log is read with ``kubectl_logs`` — ``kubectl logs``, the
one read here that is not a ``get``: its text when kubectl answered, else
``KubectlError`` on the same terms (#3349).

``KubectlError`` is a named caller error (reason ``kubectl_error``):
uncaught, ``run()`` ends the process at rc 2 with one ``ERROR:`` line.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Optional, Sequence

import _lib_engine
from _lib_exitcodes import CallerError


class KubectlError(CallerError):
    """kubectl gave no exit status of its own, or no usable answer to a read
    (see the module docstring). ``stderr`` is kubectl's whole stderr when it
    exited non-zero on a read (the message names its last line only)."""

    reason = "kubectl_error"
    stderr: str = ""


def _last_line(text: str) -> str:
    """kubectl's own message is its last stderr line: before it, client-go
    may log (``E1024 … memcache.go … Couldn't get current server API group
    list``) on an unreachable cluster. It goes into an error message, so its
    control characters are replaced like ``printable_stderr``'s: a last line
    of ``failed\x1b[2J`` must not clear the operator's screen."""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip().translate(_UNPRINTABLE)[:300]
    return ""


#: kubectl's stderr is a cluster's and a plugin's words: every control
#: character but the line break and the tab is replaced before it is printed.
_UNPRINTABLE = {c: "?" for c in [*range(0x20), *range(0x7f, 0xa0)] if c not in (0x0a, 0x09)}


def printable_stderr(text: str, limit: int = 4000) -> str:
    """kubectl's stderr for an operator's terminal: whole (its last *limit*
    characters when longer, marked), control characters replaced by ``?``,
    line breaks kept."""
    text = (text or "").replace("\r\n", "\n").strip("\n").translate(_UNPRINTABLE)
    if len(text) > limit:
        text = f"… ({len(text) - limit} earlier characters not shown)\n{text[-limit:]}"
    return text


def kubectl_run(verb: str, args: Sequence[str], *, timeout: float,
                stdin_text: Optional[str] = None,
                kubeconfig: Optional[str] = None, context: Optional[str] = None,
                what: Optional[str] = None,
                inherit_stdin: bool = True) -> subprocess.CompletedProcess:
    """``kubectl <verb> <args>`` once it has exited by itself — whatever its
    exit status, which the caller reads — else ``KubectlError``: not on
    PATH, not startable, no exit within *timeout*, killed by a signal. The
    one place that starts kubectl, reads and writes alike (#3539); *verb*
    is one ``_lib_engine.PROOFS`` registers for kubectl.

    Both streams go to files, not pipes (``_lib_engine.run`` with
    ``capture="file"``): the answer is read once kubectl itself has exited,
    not when a process it leaves behind (an exec credential plugin's helper)
    lets go of a pipe (#3349 review). stdin is *stdin_text* when given, else
    the tool's own stdin — ``/dev/null`` with *inherit_stdin* False. *what*
    names the call in the error (default ``kubectl <verb>`` in backticks)."""
    what = what or f"`kubectl {verb}`"
    located = _lib_engine.locate("kubectl")
    if isinstance(located, _lib_engine.NoVerdict):
        raise KubectlError(f"{what}: {located.reason}")
    flags = (["--kubeconfig", kubeconfig] if kubeconfig else []) + (
        ["--context", context] if context else [])
    result = _lib_engine.run(located, verb, [*flags, verb, *args], timeout=timeout,
                             stdin_text=stdin_text, capture="file",
                             no_stdin=not inherit_stdin)
    if isinstance(result, _lib_engine.NoVerdict):
        raise KubectlError(f"{what}: {result.reason}")
    return result.proc


def _answer(verb: str, args: Sequence[str], what: str, timeout: float, *,
            kubeconfig: Optional[str], context: Optional[str] = None,
            inherit_stdin: bool = False) -> str:
    """kubectl's stdout when it answered a read (rc 0), else ``KubectlError``:
    no exit status of its own (``kubectl_run``), or a non-zero one (named by
    kubectl's own last stderr line; the whole stderr is on the error)."""
    proc = kubectl_run(verb, args, timeout=timeout, kubeconfig=kubeconfig,
                       context=context, what=what, inherit_stdin=inherit_stdin)
    if proc.returncode != 0:
        why = _last_line(proc.stderr) or "no message on stderr"
        error = KubectlError(f"{what} failed (exit {proc.returncode}): {why}")
        error.stderr = proc.stderr
        raise error
    return proc.stdout


def kubectl_list(kind: str, *, namespace: Optional[str] = None,
                 selector: Optional[str] = None,
                 field_selector: Optional[str] = None,
                 kubeconfig: Optional[str] = None,
                 context: Optional[str] = None,
                 timeout: float = 10,
                 inherit_stdin: bool = False) -> list[dict[str, Any]]:
    """The objects of *kind* kubectl lists, or ``KubectlError``.

    *selector* is a label selector (``-l``), *field_selector* a field
    selector; *namespace* None lists in kubectl's current namespace;
    *context* is kubectl's ``--context``; *inherit_stdin* gives kubectl the
    tool's own stdin instead of ``/dev/null``.
    """
    args = [kind]
    if namespace is not None:
        args += ["-n", namespace]
    if selector:
        args += ["-l", selector]
    if field_selector:
        args += ["--field-selector", field_selector]
    args += ["-o", "json"]
    what = f"`kubectl get {kind}`" + (f" in namespace {namespace!r}" if namespace is not None else "")
    out = _answer("get", args, what, timeout, kubeconfig=kubeconfig, context=context,
                  inherit_stdin=inherit_stdin)
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise KubectlError(f"{what}: output is not JSON ({exc})") from exc
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise KubectlError(f"{what}: output is not a list of objects")
    return items


def kubectl_get(kind: str, name: str, *, namespace: Optional[str] = None,
                kubeconfig: Optional[str] = None,
                timeout: float = 10) -> Optional[dict[str, Any]]:
    """The object *kind*/*name*, None when it does not exist, or
    ``KubectlError`` when kubectl does not answer (see the module docstring).
    Needs only ``get`` on that object."""
    args = [kind, name]
    if namespace is not None:
        args += ["-n", namespace]
    args += ["--ignore-not-found", "-o", "json"]
    what = f"`kubectl get {kind} {name}`" + (f" in namespace {namespace!r}" if namespace is not None else "")
    out = _answer("get", args, what, timeout, kubeconfig=kubeconfig)
    if not out.strip():
        return None
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise KubectlError(f"{what}: output is not JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise KubectlError(f"{what}: output is not an object")
    return doc


def kubectl_logs(namespace: str, target: str, *, container: Optional[str] = None,
                 tail: Optional[int] = None, kubeconfig: Optional[str] = None,
                 timeout: float = 10) -> str:
    """The log text of *target* (``pod/x``, ``deploy/x``) in *namespace*, or
    ``KubectlError`` when kubectl does not answer (see the module docstring)."""
    args = ["-n", namespace, target]
    if container:
        args += ["-c", container]
    if tail is not None:
        args += [f"--tail={tail}"]
    return _answer("logs", args, f"`kubectl logs {target}` in namespace {namespace!r}",
                   timeout, kubeconfig=kubeconfig)
