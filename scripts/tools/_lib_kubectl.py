"""Read Kubernetes resources through ``kubectl get -o json`` (#3207, #3210).

One reader for the ops tools that ask a cluster whether something exists.
Its rule is the one #3133 set for Prometheus: a query that got no answer is
never "no data". ``kubectl_list`` returns the listed objects only when
kubectl answered (rc 0, a JSON list); every other outcome raises
``KubectlError``:

* kubectl is not on PATH, or does not finish within the timeout;
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
from typing import Any, Optional

from _lib_exitcodes import CallerError


class KubectlError(CallerError):
    """kubectl gave no usable answer to a ``get`` (see the module docstring)."""

    reason = "kubectl_error"


def _last_line(text: str) -> str:
    """kubectl's own message is its last stderr line: before it, client-go
    may log (``E1024 … memcache.go … Couldn't get current server API group
    list``) on an unreachable cluster."""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()[:300]
    return ""


def _answer(cmd: list[str], what: str, timeout: float) -> str:
    """kubectl's stdout when it answered (rc 0), else ``KubectlError``: not
    on PATH, no answer within *timeout*, or a non-zero exit (named by
    kubectl's own last stderr line). The one place that decides it."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise KubectlError(f"{what}: kubectl is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubectlError(f"{what}: no answer within {timeout:g}s") from exc
    if proc.returncode != 0:
        why = _last_line(proc.stderr) or "no message on stderr"
        raise KubectlError(f"{what} failed (exit {proc.returncode}): {why}")
    return proc.stdout


def _kubectl(kubeconfig: Optional[str]) -> list[str]:
    return ["kubectl", "--kubeconfig", kubeconfig] if kubeconfig else ["kubectl"]


def kubectl_list(kind: str, *, namespace: Optional[str] = None,
                 selector: Optional[str] = None,
                 field_selector: Optional[str] = None,
                 kubeconfig: Optional[str] = None,
                 timeout: float = 10) -> list[dict[str, Any]]:
    """The objects of *kind* kubectl lists, or ``KubectlError``.

    *selector* is a label selector (``-l``), *field_selector* a field
    selector; *namespace* None lists in kubectl's current namespace.
    """
    cmd = _kubectl(kubeconfig) + ["get", kind]
    if namespace is not None:
        cmd += ["-n", namespace]
    if selector:
        cmd += ["-l", selector]
    if field_selector:
        cmd += ["--field-selector", field_selector]
    cmd += ["-o", "json"]
    what = f"`kubectl get {kind}`" + (f" in namespace {namespace!r}" if namespace is not None else "")
    out = _answer(cmd, what, timeout)
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
    cmd = _kubectl(kubeconfig) + ["get", kind, name]
    if namespace is not None:
        cmd += ["-n", namespace]
    cmd += ["--ignore-not-found", "-o", "json"]
    what = f"`kubectl get {kind} {name}`" + (f" in namespace {namespace!r}" if namespace is not None else "")
    out = _answer(cmd, what, timeout)
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
    cmd = _kubectl(kubeconfig) + ["logs", "-n", namespace, target]
    if container:
        cmd += ["-c", container]
    if tail is not None:
        cmd += [f"--tail={tail}"]
    return _answer(cmd, f"`kubectl logs {target}` in namespace {namespace!r}", timeout)
