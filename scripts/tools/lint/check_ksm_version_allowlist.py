#!/usr/bin/env python3
"""KSM version-allowlist invariant lint (ADR-024 partial-misconfig defense).

Codifies a cross-component invariant the runtime VersionAwareThresholdInert
sentinel CANNOT catch:

WHY: the kubernetes rule pack's (0a) version-injection join reads
`kube_pod_labels{label_app_kubernetes_io_version=...}`. kube-state-metrics emits
that label ONLY when started with
`--metric-labels-allowlist=pods=[app.kubernetes.io/version]` (proven on a real
kind cluster — test/rulepack-e2e/). Without it, every pod silently resolves to
`version="default"` and version-aware thresholds never apply.

The runtime sentinel detects "KSM emits NO pod labels at all" (allowlist fully
off). But if an operator sets the allowlist to a DIFFERENT label
(`pods=[app.kubernetes.io/managed-by]`), KSM DOES emit kube_pod_labels — just
without the version key — so the sentinel stays silent while the feature is
inert (Gemini Pass-4 adversarial review). This STATIC check closes that gap at
CI time, the moment the KSM deployment args are edited: it asserts every
kube-state-metrics Deployment's allowlist includes `app.kubernetes.io/version`.

Two layers, complementary: static (this) catches wrong-allowlist before deploy;
runtime sentinel catches no-allowlist in the live cluster.

Which files: every `*.yaml` under k8s/ is read the way kubectl reads it
(da-crdecode, one batch; #3426), and a KSM Deployment is a `kind: Deployment`
of that reading (a `kind: List`'s items included, as kubectl applies them)
with a container whose `image` names kube-state-metrics. The
raw text decides nothing: a file that mentions the image only in a comment
is no KSM Deployment, and a file kubectl's reading refuses is named (rc 2)
rather than passed over.

Exit codes:
    0  every KSM deployment allowlists app.kubernetes.io/version
    1  a KSM deployment is missing it (--ci)
    2  error (a k8s/ file kubectl's reading refuses — each named —, no
       da-crdecode, or no KSM Deployment in kubectl's reading of k8s/)

Usage:
    python scripts/tools/lint/check_ksm_version_allowlist.py        # report
    python scripts/tools/lint/check_ksm_version_allowlist.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)
import _lib_consumer_read  # noqa: E402  (ADR-036 §12: manifests read kubectl's way)

REQUIRED_LABEL = "app.kubernetes.io/version"
_PODS_LIST = re.compile(r"pods=\[([^\]]*)\]")
_KSM_IMAGE = "kube-state-metrics"


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def ksm_allowlist_ok(args: List[str]) -> Tuple[bool, str]:
    """Pure core: given a KSM container's args, is app.kubernetes.io/version
    allowlisted for pods? Returns (ok, reason)."""
    joined = " ".join(str(a) for a in (args or []))
    if "--metric-labels-allowlist" not in joined:
        return False, "no --metric-labels-allowlist arg at all"
    m = _PODS_LIST.search(joined)
    if not m:
        return False, "--metric-labels-allowlist present but no pods=[...] entry"
    labels = [s.strip() for s in m.group(1).split(",") if s.strip()]
    if REQUIRED_LABEL not in labels:
        return False, f"pods allowlist {labels} is missing {REQUIRED_LABEL!r}"
    return True, "ok"


def ksm_containers(docs: list) -> List[Tuple[str, dict]]:
    """[(deployment_name, container)] for every kube-state-metrics container
    of a `kind: Deployment` in *docs* — kubectl's reading of one file, every
    document a Kubernetes object (`kubernetes_documents`' guarantee)."""
    out: List[Tuple[str, dict]] = []
    for doc in docs:
        if doc["kind"] != "Deployment":
            continue
        spec = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})
        for c in spec.get("containers") or []:
            if isinstance(c, dict) and _KSM_IMAGE in str(c.get("image", "")):
                out.append(((doc.get("metadata") or {}).get("name", "?"), c))
    return out


def check_docs(docs: list) -> List[Tuple[str, str]]:
    """Return [(deployment_name, reason)] for KSM deployments in *docs*
    missing the label. Empty list = no KSM deployment here, or all
    compliant."""
    findings: List[Tuple[str, str]] = []
    for name, c in ksm_containers(docs):
        ok, reason = ksm_allowlist_ok(c.get("args") or [])
        if not ok:
            findings.append((name, reason))
    return findings


def check_file(path: Path) -> List[Tuple[str, str]]:
    """`check_docs` of kubectl's reading of *path*. Raises ConsumerReadError
    when that reading cannot be had (a refusal included)."""
    return check_docs(_lib_consumer_read.kubernetes_documents(str(path)))


def read_k8s_tree(repo: Path) -> "dict[Path, list]":
    """kubectl's reading of every `*.yaml` under *repo*/k8s, one batch.
    Raises :class:`ReadRefused` naming every file that reading refuses, and
    ConsumerReadError when there is no reading to be had (no da-crdecode)."""
    paths = sorted((repo / "k8s").rglob("*.yaml"))
    read = _lib_consumer_read.kubernetes_documents_of(paths, keep_refusals=True)
    refused = [(p, read[str(p)]) for p in paths
               if isinstance(read[str(p)], _lib_consumer_read.ConsumerReadError)]
    if refused:
        raise ReadRefused(refused)
    return {p: read[str(p)] for p in paths}


class ReadRefused(Exception):
    """k8s/ files kubectl's reading refuses: ``refused`` is
    ``[(path, InputRefused)]``. Not one of them is passed over: a KSM
    Deployment in a file nobody reads is a Deployment this lint never saw."""

    def __init__(self, refused: "list[tuple[Path, Exception]]") -> None:
        super().__init__(f"{len(refused)} file(s) under k8s/ that kubectl's reading refuses")
        self.refused = refused


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="KSM version-allowlist invariant lint (ADR-024)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    repo = _repo_root()
    try:
        tree = read_k8s_tree(repo)
    except ReadRefused as exc:
        print(f"ERROR: {exc}; a KSM Deployment in one would never be checked:",
              file=sys.stderr)
        for path, err in exc.refused:
            print(f"  {path.relative_to(repo)}: {err.detail()}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: cannot read a manifest the way kubectl does: {exc}", file=sys.stderr)
        for line in exc.stderr_lines:
            print(f"  {line}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    targets = [path for path, docs in tree.items() if ksm_containers(docs)]
    if not targets:
        print("ERROR: no kube-state-metrics Deployment under k8s/ in kubectl's reading "
              "(a `kind: Deployment` with a container image naming kube-state-metrics)",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = 0
    for path in targets:
        for name, reason in check_docs(tree[path]):
            violations += 1
            print(f"  ❌ {path.relative_to(repo)} [{name}]: {reason}")

    if violations:
        print(f"\n❌ {violations} kube-state-metrics Deployment(s) do not allowlist "
              f"{REQUIRED_LABEL!r}. The kubernetes rule pack's (0a) version join needs "
              f"it; without it version-aware thresholds are silently inert. Add "
              f"`--metric-labels-allowlist=pods=[{REQUIRED_LABEL}]`. "
              f"See test/rulepack-e2e/ + ADR-024.", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ All {len(targets)} kube-state-metrics Deployment(s) allowlist "
          f"{REQUIRED_LABEL}.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
