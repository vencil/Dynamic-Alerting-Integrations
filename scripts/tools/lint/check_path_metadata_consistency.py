#!/usr/bin/env python3
"""Warn when conf.d/ hierarchical path disagrees with tenant `_metadata`.

Implements the design contract from `docs/schemas/tenant-config.schema.json`
(`definitions.metadata.$comment`, ADR-016):

    When files are in hierarchical conf.d/ (domain/region/env/), the
    directory path infers default values for domain, region, and
    environment. Explicit _metadata fields override path-inferred values.
    CI may warn when path and field disagree.

This tool is **warning-only** (exits 0 on any finding). Its job is to
surface drift between filesystem placement and declared metadata without
blocking merges — the schema already permits override. It reads the tree
through `da-guard parse-tree` (ADR-036 step 4, #2906): no usable da-guard
is a caller error, exit 2 — nothing was checked.

Rules:
  1. Scan the tenant files `da-guard parse-tree` selects under
     `<config-dir>` (default: components/threshold-exporter/config/conf.d/),
     as yaml.v3 reads them. Files whose basename starts with `_` are
     skipped (they are defaults/policies/profiles, not tenant files). A
     file parse-tree cannot read is named as NOT checked.
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
import os
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)
# ADR-036 step 4 (#2906): the tenant files, and what each holds, are
# `da-guard parse-tree`'s — the exporter's walk, yaml.v3's reading (tenant
# ids as source text, #2216).
import _lib_parse_cache  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_parse_tree import FileUnreadable, ParseTreeError  # noqa: E402

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
    """This tree's root (#2852): the tool's own tree, never the cwd's.

    It used to walk up from the cwd to the first `.git`, so a run from inside
    another checkout took THAT checkout's conf.d as the default.
    """
    return require_project_root(_THIS_DIR)


def iter_tenant_files(config_dir: Path) -> Iterable[Path]:
    """Tenant files under `config_dir`: the ones `da-guard parse-tree`
    selects (the exporter's walk: both spellings in any case, #1603;
    `.`-prefixed entries pruned, #2081; a ConfigMap mount's payload read
    once), minus `_`-prefixed platform files — readable or not (`main`
    names an unreadable one). Raises ParseTreeError."""
    tree = _lib_parse_cache.reader_for(config_dir).tree()
    for rel in _lib_parse_cache.tenant_files(tree):
        if rel not in tree.unreadable:
            yield config_dir / rel


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
    if not filepath.is_file():
        return []
    try:
        data = _lib_parse_cache.reader_for(config_dir).read(filepath)
    except (FileUnreadable, _lib_parse_cache.NotSelected):
        # Unreadable: named as NOT checked by `main` (parse-tree's
        # `unreadable`). Not selected (outside the tree, a hidden name): no
        # reader reads it, so there is nothing to check.
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


def _display_path(file: str, repo_root: Path) -> Path:
    """The path to SHOW for a finding: the file as scanned, repo-relative.

    ⛔ Only `abspath`, never `resolve()` on the file itself (#2127). The
    finding is about the path the exporter READ — its directory segments are
    what disagree with `_metadata`. `resolve()` follows the leaf link, so a
    ConfigMap mount (`acme.yaml -> ..data/acme.yaml`) was shown as the
    `..<ts>/` payload, and `prod/acme.yaml -> ../staging/acme.yaml` was shown
    as `staging/acme.yaml`: the one copy that is consistent, i.e. the wrong
    file to edit.

    `repo_root` IS resolved: `main` resolves `config_dir`, so the scanned
    paths are physical, and a `repo_root` reached through a symlink must be
    compared in the same terms or every finding falls back to absolute.
    """
    try:
        return Path(os.path.abspath(file)).relative_to(repo_root.resolve())
    except ValueError:
        return Path(file)


def _format_mismatch(m: Mismatch, repo_root: Path) -> str:
    display = _display_path(m.file, repo_root).as_posix()
    return (
        f"WARN path/metadata mismatch: {display}\n"
        f"  tenant={m.tenant}  field={m.field}"
        f"  path={m.path_value}  metadata={m.metadata_value}\n"
        f"  (non-fatal -- directory placement vs _metadata disagree; "
        f"resolve or accept as override)"
    )


def main() -> int:
    with _lib_parse_cache.session():
        return _main()


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Warn when conf.d/ directory path disagrees with tenant "
            "_metadata.{domain,region,environment}. Exits 0 on any finding; "
            "2 when da-guard parse-tree cannot read the tree."
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
        return EXIT_OK

    # #1607: an entry the walk could not read (a config-named directory, a
    # broken symlink, a directory link, a file that does not parse) is one
    # this lint did NOT check, so it is named, once — or the "N tenant
    # file(s)" tail below would read as coverage it does not have. ONE
    # parse-tree document answers the warnings, the findings and the tail
    # (#2906: parse-tree's `unreadable`, not a second Python walk).
    try:
        tree = _lib_parse_cache.reader_for(config_dir).tree()
        tenant_files = list(iter_tenant_files(config_dir))
    except ParseTreeError as exc:
        print(_lib_parse_cache.parse_tree_error_line(exc), file=sys.stderr)
        return EXIT_CALLER_ERROR
    for rel, (kind, message) in sorted(tree.unreadable.items()):
        # The `_` name filter is for FILES only. An entry the walk does not
        # descend is named whatever it is called: the exporter descends
        # `_`-prefixed directories, so a `_shared` directory link hides
        # tenants too (#1972).
        entry = config_dir / rel
        if (not rel.rsplit("/", 1)[-1].startswith("_")
                or (entry.is_symlink() and entry.is_dir())):
            print(f"{safe_label(str(entry))}:0: warning: not checked — "
                  f"{safe_label(message)} ({kind})",
                  file=sys.stderr)

    mismatches: list[Mismatch] = []
    for filepath in tenant_files:
        mismatches.extend(scan_file(filepath, config_dir))
    files_scanned = len(tenant_files)

    if args.ci:
        for m in mismatches:
            display = _display_path(m.file, repo_root).as_posix()
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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
