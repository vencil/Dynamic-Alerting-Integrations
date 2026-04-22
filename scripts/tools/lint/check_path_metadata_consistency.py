#!/usr/bin/env python3
"""Warn when conf.d/ hierarchical path disagrees with tenant `_metadata`.

Implements the design contract from `docs/schemas/tenant-config.schema.json`
(`definitions.metadata.$comment`, ADR-017):

    When files are in hierarchical conf.d/ (domain/region/env/), the
    directory path infers default values for domain, region, and
    environment. Explicit _metadata fields override path-inferred values.
    CI may warn when path and field disagree.

This tool is **warning-only** (always exits 0). Its job is to surface
drift between filesystem placement and declared metadata without
blocking merges — the schema already permits override.

Rules:
  1. Scan `<config-dir>/**/*.yaml` (default: components/threshold-exporter
     /config/conf.d/). Files whose basename starts with `_` are skipped
     (they are defaults/policies/profiles, not tenant files).
  2. For each tenant block, extract `_metadata.{domain,region,environment}`
     values if present.
  3. Walk the file's parent-directory segments. If a segment exactly matches
     a known environment name AND the tenant declares `_metadata.environment`
     with a different value, warn. Same for domain (first segment).
  4. If the field is absent, or the path has no matching segment, skip.

Usage:
  python3 scripts/tools/lint/check_path_metadata_consistency.py
  python3 scripts/tools/lint/check_path_metadata_consistency.py \\
      --config-dir tests/golden/fixtures/full-l0-l3/conf.d
  python3 scripts/tools/lint/check_path_metadata_consistency.py --ci
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

import yaml

# Conservative allowlist of strings that carry environment semantics when
# they appear as a directory segment. Keep narrow to avoid false positives
# from segments like "metrics" or "team-a".
ENVIRONMENT_TOKENS = {
    "prod",
    "production",
    "staging",
    "stage",
    "dev",
    "development",
    "test",
    "qa",
}

DEFAULT_CONFIG_DIR = "components/threshold-exporter/config/conf.d"


class Mismatch(NamedTuple):
    file: str
    tenant: str
    field: str
    path_value: str
    metadata_value: str


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parents[3]


def iter_tenant_files(config_dir: Path) -> Iterable[Path]:
    for path in sorted(config_dir.rglob("*.yaml")):
        if path.name.startswith("_"):
            continue
        yield path


def _path_inferences(
    filepath: Path,
    config_dir: Path,
) -> dict[str, str]:
    """Return {field_name: path_value} for path segments that map to a
    known metadata field. Only conservative mappings are emitted:
      - any segment in ENVIRONMENT_TOKENS maps to "environment"
      - the first segment below config_dir maps to "domain"
    """
    try:
        rel = filepath.relative_to(config_dir)
    except ValueError:
        return {}
    # Drop the filename — only parent segments infer metadata.
    segments = list(rel.parent.parts)
    if not segments:
        return {}

    inferred: dict[str, str] = {}
    # Domain = first (outermost) segment.
    inferred["domain"] = segments[0]
    # Environment = any segment matching the allowlist (prefer the
    # deepest match to handle nested layouts like db/mariadb/prod/).
    for seg in reversed(segments):
        if seg.lower() in ENVIRONMENT_TOKENS:
            inferred["environment"] = seg
            break
    return inferred


def _extract_tenant_metadata(
    data: object,
) -> dict[str, dict[str, str]]:
    """Return {tenant_id: {metadata_field: value}} for `_metadata` blocks
    declared under each tenant. Returns {} if the file has no `tenants:`
    top-level key or the shape is unexpected.
    """
    if not isinstance(data, dict):
        return {}
    tenants = data.get("tenants")
    if not isinstance(tenants, dict):
        return {}

    result: dict[str, dict[str, str]] = {}
    for tenant_id, block in tenants.items():
        if not isinstance(block, dict):
            continue
        metadata = block.get("_metadata")
        if not isinstance(metadata, dict):
            continue
        fields: dict[str, str] = {}
        for key in ("domain", "region", "environment"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                fields[key] = value
        if fields:
            result[str(tenant_id)] = fields
    return result


def scan_file(
    filepath: Path,
    config_dir: Path,
) -> list[Mismatch]:
    try:
        raw = filepath.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        # Malformed YAML is another tool's problem — we don't block on it.
        return []

    inferences = _path_inferences(filepath, config_dir)
    if not inferences:
        return []

    declared = _extract_tenant_metadata(data)
    if not declared:
        return []

    mismatches: list[Mismatch] = []
    for tenant_id, fields in declared.items():
        for field, metadata_value in fields.items():
            path_value = inferences.get(field)
            if path_value is None:
                continue
            if path_value.lower() == metadata_value.lower():
                continue
            mismatches.append(
                Mismatch(
                    file=str(filepath),
                    tenant=tenant_id,
                    field=field,
                    path_value=path_value,
                    metadata_value=metadata_value,
                )
            )
    return mismatches


def scan(config_dir: Path) -> list[Mismatch]:
    all_mismatches: list[Mismatch] = []
    for filepath in iter_tenant_files(config_dir):
        all_mismatches.extend(scan_file(filepath, config_dir))
    return all_mismatches


def _format_mismatch(m: Mismatch, repo_root: Path) -> str:
    try:
        display = Path(m.file).resolve().relative_to(repo_root.resolve())
    except ValueError:
        display = Path(m.file)
    return (
        f"WARN path/metadata mismatch: {display}\n"
        f"  tenant={m.tenant}  field={m.field}"
        f"  path={m.path_value}  metadata={m.metadata_value}\n"
        f"  (non-fatal -- directory placement vs _metadata disagree; "
        f"resolve or accept as override)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Warn when conf.d/ directory path disagrees with tenant "
            "_metadata.{domain,region,environment}. Always exits 0."
        )
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help=(
            "conf.d root to scan (default: "
            "components/threshold-exporter/config/conf.d)"
        ),
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help=(
            "CI-friendly output (one line per mismatch, no multi-line "
            "explanations); still exits 0."
        ),
    )
    args = parser.parse_args()

    repo_root = find_repo_root()
    config_dir = (
        Path(args.config_dir).resolve()
        if args.config_dir
        else (repo_root / DEFAULT_CONFIG_DIR).resolve()
    )

    if not config_dir.exists():
        print(
            f"config dir not found: {config_dir} (skipping)",
            file=sys.stderr,
        )
        return 0

    mismatches = scan(config_dir)
    files_scanned = sum(1 for _ in iter_tenant_files(config_dir))

    if args.ci:
        for m in mismatches:
            try:
                display = Path(m.file).resolve().relative_to(
                    repo_root.resolve()
                )
            except ValueError:
                display = Path(m.file)
            print(
                f"{display}:0: warning: path/metadata mismatch "
                f"tenant={m.tenant} field={m.field} "
                f"path={m.path_value} metadata={m.metadata_value}"
            )
    else:
        for m in mismatches:
            print(_format_mismatch(m, repo_root))

    tail = (
        f"{len(mismatches)} mismatch(es) across {files_scanned} tenant file(s)"
    )
    if mismatches:
        print(f"\n{tail}", file=sys.stderr)
    else:
        print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
