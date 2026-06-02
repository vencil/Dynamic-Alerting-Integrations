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

Exit codes:
    0  every KSM deployment allowlists app.kubernetes.io/version
    1  a KSM deployment is missing it (--ci)
    2  error (YAML parse / no KSM deployment found)

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

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

REQUIRED_LABEL = "app.kubernetes.io/version"
_PODS_LIST = re.compile(r"pods=\[([^\]]*)\]")
_KSM_IMAGE = "kube-state-metrics"


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def _iter_docs(path: Path):
    """Yield every YAML document (manifests are often multi-doc)."""
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, dict):
            yield doc


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


def check_file(path: Path) -> List[Tuple[str, str]]:
    """Return [(deployment_name, reason)] for KSM deployments missing the label.
    Empty list = no KSM deployment here, or all compliant."""
    findings: List[Tuple[str, str]] = []
    for doc in _iter_docs(path):
        if doc.get("kind") != "Deployment":
            continue
        spec = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})
        containers = spec.get("containers") or []
        for c in containers:
            if _KSM_IMAGE not in str(c.get("image", "")):
                continue
            ok, reason = ksm_allowlist_ok(c.get("args") or [])
            if not ok:
                name = (doc.get("metadata") or {}).get("name", "?")
                findings.append((name, reason))
    return findings


def _ksm_deployment_files(repo: Path) -> List[Path]:
    """Manifests that define a kube-state-metrics Deployment."""
    out: List[Path] = []
    for path in sorted((repo / "k8s").rglob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _KSM_IMAGE in text and "kind: Deployment" in text:
            out.append(path)
    return out


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="KSM version-allowlist invariant lint (ADR-024)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    repo = _repo_root()
    targets = _ksm_deployment_files(repo)
    if not targets:
        print("ERROR: no kube-state-metrics Deployment manifest found under k8s/",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = 0
    try:
        for path in targets:
            for name, reason in check_file(path):
                violations += 1
                print(f"  ❌ {path.relative_to(repo)} [{name}]: {reason}")
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

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
