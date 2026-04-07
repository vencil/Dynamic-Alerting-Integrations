#!/usr/bin/env python3
"""drift_detect.py — Cross-Cluster Configuration Drift Detection

Compare multiple config-dir directories (from different clusters or GitOps
branches) and detect unexpected configuration drift.

Produces a structured report of added/removed/modified files with per-file
diff summaries and reconciliation suggestions.

Usage:
    da-tools drift-detect --dirs cluster-a/conf.d,cluster-b/conf.d
    da-tools drift-detect --dirs dir-a,dir-b --json
    da-tools drift-detect --dirs dir-a,dir-b,dir-c --ci --markdown

Exit codes:
    0  no unexpected drift (or --dry-run)
    1  unexpected drift detected (CI mode)
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Library imports
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_python import detect_cli_lang, i18n_text  # noqa: E402

# ---------------------------------------------------------------------------
# Bilingual help text
# ---------------------------------------------------------------------------
_HELP: Dict[str, Dict[str, str]] = {
    "description": {
        "zh": "跨叢集配置漂移偵測 — 比對多個 config-dir 目錄或 PrometheusRule CRD 的差異",
        "en": "Cross-cluster config drift detection — compare config dirs or PrometheusRule CRDs",
    },
    "dirs": {
        "zh": "以逗號分隔的配置目錄列表 (configmap 模式需≥2個；operator 模式需1個)",
        "en": "Comma-separated config directories (configmap: ≥2; operator: 1)",
    },
    "labels": {
        "zh": "對應每個目錄的標籤 (預設: dir-1,dir-2,...)",
        "en": "Labels for each directory (default: dir-1,dir-2,...)",
    },
    "ignore_prefix": {
        "zh": "忽略此前綴的檔案 (預設: _cluster_)",
        "en": "Ignore files with this prefix (default: _cluster_)",
    },
    "mode": {
        "zh": "執行模式: configmap (多目錄比對) 或 operator (叢集 CRD vs 本地)",
        "en": "Mode: configmap (multi-dir compare) or operator (cluster CRD vs local)",
    },
    "kubeconfig": {
        "zh": "kubeconfig 檔案路徑 (operator 模式)",
        "en": "kubeconfig file path (operator mode)",
    },
    "context": {
        "zh": "Kubernetes context 名稱 (operator 模式)",
        "en": "Kubernetes context name (operator mode)",
    },
    "namespace": {
        "zh": "監視命名空間 (operator 模式，預設: monitoring)",
        "en": "Monitoring namespace (operator mode, default: monitoring)",
    },
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

EXPECTED_PREFIXES = ("_cluster_", "_local_")


@dataclass
class FileManifest:
    """SHA-256 manifest for a single config directory."""

    label: str
    path: str
    files: Dict[str, str] = field(default_factory=dict)  # filename → sha256


@dataclass
class DriftItem:
    """A single drift finding between two directories."""

    filename: str
    drift_type: str  # "added" | "removed" | "modified"
    source_label: str
    target_label: str
    expected: bool = False  # True if file matches EXPECTED_PREFIXES
    source_sha: str = ""
    target_sha: str = ""


@dataclass
class DriftReport:
    """Comparison result between a pair of directories."""

    source_label: str
    target_label: str
    items: List[DriftItem] = field(default_factory=list)

    @property
    def unexpected_count(self) -> int:
        return sum(1 for i in self.items if not i.expected)

    @property
    def expected_count(self) -> int:
        return sum(1 for i in self.items if i.expected)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compute_dir_manifest(dir_path: str, label: str = "") -> FileManifest:
    """Build a SHA-256 manifest for all YAML files in a directory.

    Skips hidden files (starting with '.').
    """
    p = Path(dir_path)
    manifest = FileManifest(
        label=label or p.name,
        path=str(p.resolve()),
    )
    if not p.is_dir():
        return manifest

    for f in sorted(p.glob("*.yaml")):
        if f.name.startswith("."):
            continue
        manifest.files[f.name] = _file_sha256(f)

    return manifest


def compare_manifests(
    source: FileManifest,
    target: FileManifest,
    ignore_prefixes: Tuple[str, ...] = EXPECTED_PREFIXES,
) -> DriftReport:
    """Compare two directory manifests and classify drift.

    Returns a DriftReport with items categorized as added/removed/modified.
    """
    report = DriftReport(
        source_label=source.label,
        target_label=target.label,
    )

    all_files = sorted(set(source.files) | set(target.files))

    for filename in all_files:
        in_source = filename in source.files
        in_target = filename in target.files
        is_expected = any(filename.startswith(p) for p in ignore_prefixes)

        if in_source and not in_target:
            report.items.append(DriftItem(
                filename=filename,
                drift_type="removed",
                source_label=source.label,
                target_label=target.label,
                expected=is_expected,
                source_sha=source.files[filename],
            ))
        elif not in_source and in_target:
            report.items.append(DriftItem(
                filename=filename,
                drift_type="added",
                source_label=source.label,
                target_label=target.label,
                expected=is_expected,
                target_sha=target.files[filename],
            ))
        elif source.files[filename] != target.files[filename]:
            report.items.append(DriftItem(
                filename=filename,
                drift_type="modified",
                source_label=source.label,
                target_label=target.label,
                expected=is_expected,
                source_sha=source.files[filename],
                target_sha=target.files[filename],
            ))

    return report


def analyze_drift(
    dirs: List[str],
    labels: Optional[List[str]] = None,
    ignore_prefixes: Tuple[str, ...] = EXPECTED_PREFIXES,
) -> List[DriftReport]:
    """Pairwise comparison of all config directories.

    Returns list of DriftReports (one per pair).
    """
    if labels is None:
        labels = [f"dir-{i + 1}" for i in range(len(dirs))]

    manifests = [
        compute_dir_manifest(d, label=labels[i])
        for i, d in enumerate(dirs)
    ]

    reports = []
    for i in range(len(manifests)):
        for j in range(i + 1, len(manifests)):
            report = compare_manifests(
                manifests[i], manifests[j],
                ignore_prefixes=ignore_prefixes,
            )
            reports.append(report)

    return reports


def suggest_reconcile(item: DriftItem, is_operator: bool = False) -> str:
    """Generate a reconciliation suggestion for a drift item."""
    if is_operator:
        return _operator_suggest_reconcile(item)

    # ConfigMap mode
    if item.drift_type == "added":
        return (
            f"Copy {item.filename} from {item.target_label} to "
            f"{item.source_label}, or remove from {item.target_label} "
            f"if unintended: "
            f"cp {item.target_label}/{item.filename} "
            f"{item.source_label}/{item.filename}"
        )
    elif item.drift_type == "removed":
        return (
            f"Copy {item.filename} from {item.source_label} to "
            f"{item.target_label}, or remove from {item.source_label} "
            f"if deprecated: "
            f"cp {item.source_label}/{item.filename} "
            f"{item.target_label}/{item.filename}"
        )
    else:  # modified
        return (
            f"Review diff for {item.filename} between "
            f"{item.source_label} and {item.target_label}: "
            f"diff {item.source_label}/{item.filename} "
            f"{item.target_label}/{item.filename}"
        )


def _operator_suggest_reconcile(item: DriftItem) -> str:
    """Generate CRD-specific reconciliation suggestions."""
    if item.drift_type == "added":
        return (
            f"CRD exists in cluster but no local source: "
            f"review PrometheusRule {item.filename} in cluster"
        )
    elif item.drift_type == "removed":
        return (
            f"CRD missing from cluster: "
            f"kubectl apply -f {item.filename}"
        )
    else:  # modified
        return (
            f"Drift detected in PrometheusRule {item.filename}: "
            f"review and re-apply from local source"
        )


# ---------------------------------------------------------------------------
# Operator mode: CRD manifest & comparison
# ---------------------------------------------------------------------------

def compute_crd_manifest(
    namespace: str,
    kubeconfig: Optional[str] = None,
    context: Optional[str] = None,
) -> FileManifest:
    """Build manifest from in-cluster PrometheusRule CRDs.

    Runs: kubectl get prometheusrules -n {namespace} -o json
    For each CRD, computes SHA-256 of spec.groups JSON.

    Returns FileManifest with label="cluster-crd" and files={name: sha256}.
    """
    cmd = ["kubectl", "get", "prometheusrules", "-n", namespace, "-o", "json"]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    if context:
        cmd.extend(["--context", context])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"ERROR: kubectl failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: kubectl command timeout", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: kubectl not found", file=sys.stderr)
        sys.exit(1)

    manifest = FileManifest(label="cluster-crd", path=f"kubernetes:{namespace}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON from kubectl: {e}", file=sys.stderr)
        sys.exit(1)

    items = data.get("items", [])
    for item in items:
        name = item.get("metadata", {}).get("name", "unknown")
        spec_groups = item.get("spec", {}).get("groups", [])
        spec_json = json.dumps(spec_groups, sort_keys=True, separators=(',', ':'))
        sha = hashlib.sha256(spec_json.encode()).hexdigest()
        manifest.files[f"{name}.yaml"] = sha

    return manifest


def compare_local_vs_crd(
    local_dir: str,
    crd_manifest: FileManifest,
    ignore_prefixes: Tuple[str, ...] = EXPECTED_PREFIXES,
) -> DriftReport:
    """Compare local directory vs in-cluster CRD manifest.

    Builds local manifest using compute_dir_manifest, then compares
    using existing compare_manifests logic.

    Returns DriftReport showing drift between local and cluster CRD state.
    """
    local_manifest = compute_dir_manifest(local_dir, label="local")
    return compare_manifests(local_manifest, crd_manifest,
                             ignore_prefixes=ignore_prefixes)


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

def build_summary(reports: List[DriftReport], is_operator: bool = False) -> dict:
    """Build a structured summary from all pairwise reports."""
    total_unexpected = sum(r.unexpected_count for r in reports)
    total_expected = sum(r.expected_count for r in reports)
    total_items = sum(len(r.items) for r in reports)

    pairs = []
    for r in reports:
        pair_data = {
            "source": r.source_label,
            "target": r.target_label,
            "total_drift": len(r.items),
            "unexpected": r.unexpected_count,
            "expected": r.expected_count,
            "items": [],
        }
        for item in r.items:
            item_data = {
                "filename": item.filename,
                "type": item.drift_type,
                "expected": item.expected,
                "suggestion": (
                    suggest_reconcile(item, is_operator=is_operator)
                    if not item.expected else ""
                ),
            }
            if item.source_sha:
                item_data["source_sha"] = item.source_sha[:12]
            if item.target_sha:
                item_data["target_sha"] = item.target_sha[:12]
            pair_data["items"].append(item_data)
        pairs.append(pair_data)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair_count": len(reports),
        "total_drift": total_items,
        "unexpected_drift": total_unexpected,
        "expected_drift": total_expected,
        "drift_free": total_unexpected == 0,
        "pairs": pairs,
    }


def format_text_report(summary: dict) -> str:
    """Format a human-readable text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("Cross-Cluster Configuration Drift Report")
    lines.append("=" * 60)
    lines.append(f"Pairs compared: {summary['pair_count']}")
    lines.append(f"Total drift:    {summary['total_drift']}")
    lines.append(f"  Unexpected:   {summary['unexpected_drift']}")
    lines.append(f"  Expected:     {summary['expected_drift']}")
    lines.append("")

    if summary["drift_free"]:
        lines.append("✓ No unexpected drift detected.")
        return "\n".join(lines)

    for pair in summary["pairs"]:
        if not pair["items"]:
            continue
        lines.append(f"--- {pair['source']} ↔ {pair['target']} ---")
        for item in pair["items"]:
            marker = "  " if item["expected"] else "✗ "
            sha_info = ""
            if item.get("source_sha") and item.get("target_sha"):
                sha_info = (
                    f" [{item['source_sha']}→{item['target_sha']}]"
                )
            lines.append(
                f"  {marker}{item['type']:10s} {item['filename']}{sha_info}"
            )
            if item.get("suggestion"):
                lines.append(f"    → {item['suggestion']}")
        lines.append("")

    return "\n".join(lines)


def format_json_report(summary: dict) -> str:
    """Format a JSON report."""
    return json.dumps(summary, indent=2, ensure_ascii=False)


def format_markdown_report(summary: dict) -> str:
    """Format a Markdown report."""
    lines = []
    lines.append("# Cross-Cluster Configuration Drift Report")
    lines.append("")
    lines.append(f"**Generated**: {summary['timestamp']}")
    lines.append(f"**Pairs**: {summary['pair_count']} | "
                 f"**Drift**: {summary['total_drift']} "
                 f"({summary['unexpected_drift']} unexpected, "
                 f"{summary['expected_drift']} expected)")
    lines.append("")

    if summary["drift_free"]:
        lines.append("> ✅ No unexpected drift detected.")
        return "\n".join(lines)

    for pair in summary["pairs"]:
        if not pair["items"]:
            continue
        lines.append(f"## {pair['source']} ↔ {pair['target']}")
        lines.append("")
        lines.append("| File | Type | Expected | SHA (source→target) |")
        lines.append("|------|------|----------|---------------------|")
        for item in pair["items"]:
            expected_str = "✅" if item["expected"] else "❌"
            sha_str = ""
            if item.get("source_sha") and item.get("target_sha"):
                sha_str = f"`{item['source_sha']}`→`{item['target_sha']}`"
            elif item.get("source_sha"):
                sha_str = f"`{item['source_sha']}`→(none)"
            elif item.get("target_sha"):
                sha_str = f"(none)→`{item['target_sha']}`"
            lines.append(
                f"| {item['filename']} | {item['type']} "
                f"| {expected_str} | {sha_str} |"
            )
        lines.append("")

        # Suggestions for unexpected
        unexpected = [i for i in pair["items"] if not i["expected"]]
        if unexpected:
            lines.append("### Reconciliation Suggestions")
            lines.append("")
            for item in unexpected:
                if item.get("suggestion"):
                    lines.append(f"- **{item['filename']}**: {item['suggestion']}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with bilingual help."""
    lang = detect_cli_lang()
    h = {k: v[lang] for k, v in _HELP.items()}

    parser = argparse.ArgumentParser(
        description=h["description"],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dirs", required=True,
        help=h["dirs"],
    )
    parser.add_argument(
        "--mode", default="configmap", choices=["configmap", "operator"],
        help=h["mode"],
    )
    parser.add_argument(
        "--labels", default=None,
        help=h["labels"],
    )
    parser.add_argument(
        "--ignore-prefix", default="_cluster_,_local_",
        help=h["ignore_prefix"],
    )
    parser.add_argument(
        "--kubeconfig", default=None,
        help=h["kubeconfig"],
    )
    parser.add_argument(
        "--context", default=None,
        help=h["context"],
    )
    parser.add_argument(
        "--namespace", default="monitoring",
        help=h["namespace"],
    )
    parser.add_argument("--json", action="store_true",
                        help="JSON output")
    parser.add_argument("--markdown", action="store_true",
                        help="Markdown output")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on unexpected drift")
    return parser


def main():
    """CLI entry point for drift detection."""
    parser = build_parser()
    args = parser.parse_args()

    dirs = [d.strip() for d in args.dirs.split(",") if d.strip()]
    ignore_prefixes = tuple(
        p.strip() for p in args.ignore_prefix.split(",") if p.strip()
    )

    # Operator mode: compare local vs in-cluster CRDs
    if args.mode == "operator":
        if len(dirs) != 1:
            print("ERROR: operator mode requires exactly 1 directory",
                  file=sys.stderr)
            sys.exit(1)

        local_dir = dirs[0]
        if not Path(local_dir).is_dir():
            print(f"ERROR: directory not found: {local_dir}",
                  file=sys.stderr)
            sys.exit(1)

        crd_manifest = compute_crd_manifest(
            namespace=args.namespace,
            kubeconfig=args.kubeconfig,
            context=args.context,
        )
        report = compare_local_vs_crd(
            local_dir, crd_manifest,
            ignore_prefixes=ignore_prefixes
        )
        summary = build_summary([report], is_operator=True)

    # ConfigMap mode: multi-directory comparison
    else:
        if len(dirs) < 2:
            print("ERROR: configmap mode requires at least 2 directories",
                  file=sys.stderr)
            sys.exit(1)

        labels = None
        if args.labels:
            labels = [l.strip() for l in args.labels.split(",")]
            if len(labels) != len(dirs):
                print("ERROR: --labels count must match --dirs count",
                      file=sys.stderr)
                sys.exit(1)

        # Check directories exist
        missing = [d for d in dirs if not Path(d).is_dir()]
        if missing:
            for m in missing:
                print(f"ERROR: directory not found: {m}",
                      file=sys.stderr)
            sys.exit(1)

        reports = analyze_drift(dirs, labels=labels,
                                ignore_prefixes=ignore_prefixes)
        summary = build_summary(reports, is_operator=False)

    # Output (same for both modes)
    if args.json:
        print(format_json_report(summary))
    elif args.markdown:
        print(format_markdown_report(summary))
    else:
        print(format_text_report(summary))

    if args.ci and not summary["drift_free"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
