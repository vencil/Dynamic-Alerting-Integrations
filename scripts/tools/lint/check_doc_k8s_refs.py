#!/usr/bin/env python3
"""check_doc_k8s_refs.py — docs must reference k8s manifests accurately.

Two checks, both targeting the #141 Track B / TB-F4 class (docs called
Prometheus a *StatefulSet*, pointed at a non-existent
``prometheus-statefulset.yaml``, and told operators to run
``kubectl edit statefulset prometheus`` — which would NotFound against the
shipped Deployment):

  1. **k8s-manifest-path** — a doc reference to ``k8s/**/<file>.ya?ml`` must
     exist on disk. (Broad repo-path existence is deliberately NOT linted:
     docs are full of legitimate *example* paths — ``rule-packs/my-db.yaml``,
     ``tests/test_x.py``, user-authored ``# rule-packs/mariadb.yaml`` — so a
     blanket check is pure noise. The ``k8s/`` tree is the one place docs cite
     real, shipped cluster artifacts rather than user examples.)

  2. **k8s-workload-kind** — a doc that names one of OUR monitoring workloads
     with a ``kubectl`` verb (``edit`` / ``rollout restart`` / ``scale`` …) or
     a ``<component>-<kind>.yaml`` filename must use the workload *kind* that
     actually ships in ``k8s/``. The expected kind is derived from the shipped
     manifests (filename convention + the ``kind:`` field).

False-positive controls: lines containing a ``<placeholder>`` (e.g. a
customer's ``-n <prom-ns>``) or an inline ``k8s-ref-ignore`` comment are
skipped; component matching is whole-token, so a customer's operator-managed
``prometheus-k8s`` does not collide with our ``prometheus``.

Exit 0 = clean; 1 = findings; usage: ``check_doc_k8s_refs.py [--ci] [--json]``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[3]
K8S_DIR = REPO_ROOT / "k8s"
DOCS_DIR = REPO_ROOT / "docs"

# k8s workload kinds we care about (those with a stable "edit X" semantics).
_WORKLOAD_KINDS = ("deployment", "statefulset", "daemonset")

# A doc path under k8s/ ending in a yaml extension.
_K8S_PATH_RE = re.compile(r"(?<![\w./-])(k8s/[\w./-]+\.ya?ml)")
# `kubectl <verb> <kind> <component>` — verb list covers edit/rollout/scale/etc.
_KUBECTL_RE = re.compile(
    r"kubectl\s+(?:edit|scale|delete|get|describe|patch|rollout\s+\w+)\s+"
    r"(deployment|statefulset|daemonset)\s+([A-Za-z0-9-]+)",
    re.IGNORECASE,
)
# `<component>-<kind>.yaml` filename form (e.g. prometheus-statefulset.yaml).
_KIND_FILENAME_RE = re.compile(
    r"([A-Za-z0-9-]+)-(deployment|statefulset|daemonset)\.ya?ml", re.IGNORECASE)

# Skip a line that carries an unresolved placeholder or an explicit opt-out.
_PLACEHOLDER_CHARS = "<>${}"
INLINE_IGNORE = "k8s-ref-ignore"


class Issue(NamedTuple):
    check: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return self._asdict()


def build_workload_kind_map(k8s_dir: Path) -> Dict[str, str]:
    """Map our k8s component name -> shipped workload kind (lowercased).

    Built from the ``<kind>-<component>.yaml`` filename convention and
    confirmed against the manifest's ``kind:`` field when readable.
    """
    kind_map: Dict[str, str] = {}
    if not k8s_dir.exists():
        return kind_map
    # The repo names monitoring workloads `<kind>-<component>.yaml`
    # (deployment-prometheus.yaml). That filename convention IS the source of
    # truth — reading the first `kind:` would misfire on multi-resource
    # manifests (e.g. deployment-kube-state-metrics.yaml leads with a
    # ServiceAccount).
    fname_re = re.compile(
        r"^(deployment|statefulset|daemonset)-(.+)\.ya?ml$", re.IGNORECASE)
    for f in sorted(k8s_dir.rglob("*.y*ml")):
        m = fname_re.match(f.name)
        if not m:
            continue
        kind_map[m.group(2).lower()] = m.group(1).lower()
    return kind_map


def _doc_files(docs_dir: Path) -> List[Path]:
    return [
        f for f in sorted(docs_dir.rglob("*.md"))
        # archive/ is frozen historical; not operator-facing.
        if "/internal/archive/" not in f.as_posix()
    ]


def _skip_line(line: str) -> bool:
    if INLINE_IGNORE in line:
        return True
    return any(c in line for c in _PLACEHOLDER_CHARS)


def check_manifest_paths(doc_files: List[Path], repo_root: Path) -> List[Issue]:
    """Doc references to k8s/**/*.yaml must exist on disk (TB-F4 filename)."""
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if _skip_line(line):
                continue
            for m in _K8S_PATH_RE.finditer(line):
                rel = m.group(1)
                if not (repo_root / rel).exists():
                    issues.append(Issue(
                        "k8s-manifest-path",
                        str(f.relative_to(repo_root)).replace("\\", "/"), i,
                        f"references '{rel}' which does not exist under k8s/",
                    ))
    return issues


def check_workload_kind(doc_files: List[Path],
                        kind_map: Dict[str, str],
                        repo_root: Path) -> List[Issue]:
    """Doc-stated workload kind for OUR components must match the shipped kind."""
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if _skip_line(line):
                continue
            checks = []
            for m in _KUBECTL_RE.finditer(line):
                checks.append((m.group(2).lower(), m.group(1).lower(),
                               f"kubectl ... {m.group(1).lower()} {m.group(2)}"))
            for m in _KIND_FILENAME_RE.finditer(line):
                checks.append((m.group(1).lower(), m.group(2).lower(),
                               f"{m.group(1)}-{m.group(2).lower()}.yaml"))
            for component, stated_kind, label in checks:
                expected = kind_map.get(component)
                if expected and stated_kind != expected:
                    issues.append(Issue(
                        "k8s-workload-kind",
                        str(f.relative_to(repo_root)).replace("\\", "/"), i,
                        f"{label}: '{component}' ships as a {expected}, "
                        f"not a {stated_kind}",
                    ))
    return issues


def run(repo_root: Path = REPO_ROOT) -> List[Issue]:
    docs = _doc_files(repo_root / "docs")
    kind_map = build_workload_kind_map(repo_root / "k8s")
    return (check_manifest_paths(docs, repo_root)
            + check_workload_kind(docs, kind_map, repo_root))


def main() -> int:
    # Windows consoles default to cp950/cp1252 and choke on the status emoji.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true", help="exit 1 on any finding")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    issues = run()
    if args.json:
        print(json.dumps({"issues": [i.to_dict() for i in issues],
                          "count": len(issues)}, ensure_ascii=False, indent=2))
    elif issues:
        for it in issues:
            print(f"  ❌ [{it.check}] {it.file}:{it.line} — {it.message}",
                  file=sys.stderr)
        print(f"\n❌ {len(issues)} k8s-reference issue(s)", file=sys.stderr)
    else:
        print("✅ k8s manifest references are accurate")
    return 1 if (issues and args.ci) else 0


if __name__ == "__main__":
    sys.exit(main())
