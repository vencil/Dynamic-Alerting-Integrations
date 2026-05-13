#!/usr/bin/env python3
"""check_flaky_registry.py — Validate `flaky-tests.yaml` schema + expire_at.

TRK-010 observability layer (PR #325/#326 follow-up). Closes the design
loop on the flaky-test retry registry: every entry MUST have a tracked
issue + a deadline version, and the deadline MUST advance the
root-cause fix forward. Without this validator, entries can sit forever
because no one notices when they should be removed.

What this validates
-------------------

For each entry under `known_flakes:`:

  Required fields (FATAL on missing/empty):
    - test         : human-readable name shown in CI logs
    - pattern      : Go test name regex (used by `go test -run`)
    - max_retries  : integer 1..N (typical 1-3)
    - owner        : GitHub team / handle
    - tracked_by   : issue ref (HA-NN, issue #NNN, or free-text > 10 chars)
    - expire_at    : version string (`v2.9.0`, `exporter/v2.9.0`, etc.)

  Field-shape (FATAL on mis-shape):
    - max_retries is a positive integer ≤ 5 (sanity cap)
    - expire_at parses as a semver-ish version
    - pattern is a valid regex

  Lifecycle (FATAL on stale):
    - expire_at version > current shipped version (read from CHANGELOG.md
      latest `## [vX.Y.Z]` heading, or `--current-version` override).
      An entry where current >= expire_at MUST be removed; this is the
      forcing function for root-cause fixes.

Behavior on empty registry
--------------------------

`known_flakes: []` (or missing key) is the **healthy** state — empty
registry means no flakes are currently registered. The validator
accepts it and exits 0.

Usage
-----

    # Default: validate the repo's flaky-tests.yaml against CHANGELOG.md
    python3 scripts/tools/lint/check_flaky_registry.py --ci

    # Override current version (for tests + dev cycles)
    python3 scripts/tools/lint/check_flaky_registry.py --current-version v2.9.0

    # Custom registry path
    python3 scripts/tools/lint/check_flaky_registry.py --registry path/to/file

Exit codes
----------

  0  registry valid, no expired entries
  1  schema errors OR expired entries (lists them on stderr)
  2  configuration error (missing CHANGELOG, can't parse YAML, etc.)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = REPO_ROOT / "flaky-tests.yaml"
DEFAULT_CHANGELOG = REPO_ROOT / "CHANGELOG.md"


# Required fields per entry (with min-length expectations for non-int fields).
# tracked_by min length 5 keeps "HA-N" / "issue#NN" allowable but rejects
# placeholders like "TBD" / "x".
_REQUIRED_FIELDS: dict[str, int] = {
    "test": 3,
    "pattern": 1,
    "owner": 2,
    "tracked_by": 5,
    "expire_at": 4,
}
_MAX_RETRIES_CAP = 5


@dataclass(frozen=True)
class Version:
    """Represents a parsed version string for comparison.

    Supports `vX.Y.Z` and `prefix/vX.Y.Z` (the project's release-line
    convention). The prefix is preserved on the parsed Version so a
    threshold-exporter expire_at like `exporter/v2.9.0` is only compared
    against threshold-exporter's release line — not the platform `v*`
    line. Cross-line comparison errors at parse time.
    """

    prefix: str  # "" for plain v*, otherwise "exporter", "tools", etc.
    major: int
    minor: int
    patch: int

    def __lt__(self, other: "Version") -> bool:
        if self.prefix != other.prefix:
            raise ValueError(
                f"cannot compare versions across release lines: "
                f"{self.prefix or '<root>'!r} vs {other.prefix or '<root>'!r}"
            )
        return (self.major, self.minor, self.patch) < (
            other.major, other.minor, other.patch,
        )

    def __ge__(self, other: "Version") -> bool:
        return not (self < other)

    def __str__(self) -> str:
        base = f"v{self.major}.{self.minor}.{self.patch}"
        return f"{self.prefix}/{base}" if self.prefix else base


_VERSION_RE = re.compile(
    r"^(?:(?P<prefix>[a-z][a-z0-9-]*)/)?v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$"
)


def parse_version(s: str) -> Version:
    """Parse a version string. Raises ValueError on bad shape."""
    m = _VERSION_RE.match(s.strip())
    if not m:
        raise ValueError(
            f"version {s!r} does not match expected shape "
            f"`vX.Y.Z` or `prefix/vX.Y.Z`"
        )
    return Version(
        prefix=m.group("prefix") or "",
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
    )


def latest_version_from_changelog(path: Path) -> Optional[Version]:
    """Find the latest `## [vX.Y.Z]` heading in CHANGELOG.md.

    The CHANGELOG is reverse-chronological (newest at top), so the FIRST
    matching heading is the current shipped version. Returns None if no
    matching heading is found.
    """
    if not path.is_file():
        return None
    pattern = re.compile(r"^##\s+\[(v\d+\.\d+\.\d+)\]")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                try:
                    return parse_version(m.group(1))
                except ValueError:
                    continue
    return None


# ── Validation ──────────────────────────────────────────────────────


@dataclass
class Issue:
    """One validation finding. severity ∈ {error}."""
    entry_index: int    # zero-based index in known_flakes list
    test_name: str      # may be empty if `test` field is missing
    field: str          # the offending field (or '<entry>' for entry-level)
    message: str

    def format(self) -> str:
        ref = f"#{self.entry_index} ({self.test_name or '<unnamed>'})"
        return f"  {ref} [{self.field}]: {self.message}"


def validate_entry(
    idx: int, entry: dict, current: Optional[Version],
) -> list[Issue]:
    """Validate a single entry. Returns list of Issues (empty = healthy)."""
    issues: list[Issue] = []
    test_name = str(entry.get("test", ""))

    # Schema — required fields
    for field, min_len in _REQUIRED_FIELDS.items():
        val = entry.get(field)
        if val is None:
            issues.append(Issue(
                idx, test_name, field, f"missing required field"
            ))
            continue
        if not isinstance(val, str) or len(val.strip()) < min_len:
            issues.append(Issue(
                idx, test_name, field,
                f"must be a non-empty string (≥{min_len} chars), got {val!r}",
            ))

    # max_retries shape
    mr = entry.get("max_retries")
    if mr is None:
        issues.append(Issue(idx, test_name, "max_retries", "missing required field"))
    elif not isinstance(mr, int) or mr < 1 or mr > _MAX_RETRIES_CAP:
        issues.append(Issue(
            idx, test_name, "max_retries",
            f"must be an integer in [1..{_MAX_RETRIES_CAP}], got {mr!r}",
        ))

    # pattern validity
    pattern = entry.get("pattern")
    if isinstance(pattern, str) and pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            issues.append(Issue(
                idx, test_name, "pattern",
                f"not a valid regex: {e}",
            ))

    # expire_at parse + lifecycle
    expire_raw = entry.get("expire_at")
    if isinstance(expire_raw, str) and expire_raw:
        try:
            expire_v = parse_version(expire_raw)
        except ValueError as e:
            issues.append(Issue(idx, test_name, "expire_at", str(e)))
        else:
            if current is not None:
                try:
                    if current >= expire_v:
                        issues.append(Issue(
                            idx, test_name, "expire_at",
                            f"EXPIRED — current shipped version {current} >= "
                            f"expire_at {expire_v}. Remove this entry "
                            f"(root-cause fix should have landed by now).",
                        ))
                except ValueError as e:
                    # Cross-line comparison; surface as a soft schema error
                    # rather than expiry — different release lines are
                    # legitimate.
                    issues.append(Issue(idx, test_name, "expire_at", str(e)))

    return issues


def validate_registry(
    registry: dict, current: Optional[Version],
) -> list[Issue]:
    """Validate the parsed registry dict. Returns list of Issues."""
    issues: list[Issue] = []

    if not isinstance(registry, dict):
        issues.append(Issue(
            -1, "", "<root>",
            f"top-level YAML must be a mapping, got {type(registry).__name__}",
        ))
        return issues

    raw = registry.get("known_flakes")
    if raw is None:
        # Missing key is treated as empty — healthy.
        return issues
    if not isinstance(raw, list):
        issues.append(Issue(
            -1, "", "known_flakes",
            f"must be a list, got {type(raw).__name__}",
        ))
        return issues

    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            issues.append(Issue(
                idx, "", "<entry>",
                f"must be a mapping, got {type(entry).__name__}",
            ))
            continue
        issues.extend(validate_entry(idx, entry, current))

    return issues


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate flaky-tests.yaml schema + expire_at lifecycle.",
    )
    parser.add_argument(
        "--registry", default=str(DEFAULT_REGISTRY),
        help=f"Registry YAML path (default: {DEFAULT_REGISTRY.name})",
    )
    parser.add_argument(
        "--changelog", default=str(DEFAULT_CHANGELOG),
        help=f"CHANGELOG.md path (default: {DEFAULT_CHANGELOG.name})",
    )
    parser.add_argument(
        "--current-version", default=None,
        help="Override the current-shipped version (for testing). If "
             "omitted, parsed from CHANGELOG.md latest `## [vX.Y.Z]` heading.",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: terse output, exit 1 on any finding (default behavior).",
    )
    args = parser.parse_args(argv)

    # Resolve current version
    current: Optional[Version] = None
    if args.current_version:
        try:
            current = parse_version(args.current_version)
        except ValueError as e:
            print(f"ERROR: --current-version: {e}", file=sys.stderr)
            return 2
    else:
        current = latest_version_from_changelog(Path(args.changelog))
        if current is None:
            print(
                f"WARN: could not parse current version from {args.changelog}; "
                "expire_at lifecycle check will be skipped.",
                file=sys.stderr,
            )

    # Load registry
    registry_path = Path(args.registry)
    if not registry_path.is_file():
        # Missing registry is healthy: same semantics as empty.
        return 0
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed", file=sys.stderr)
        return 2
    try:
        with open(registry_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: cannot parse {registry_path}: {e}", file=sys.stderr)
        return 2

    issues = validate_registry(data, current)
    if not issues:
        # All clean — print a short success line for CI logs.
        if not args.ci:
            count = len(data.get("known_flakes") or [])
            current_str = str(current) if current else "<unknown>"
            print(
                f"flaky-tests.yaml: {count} entries, all valid "
                f"(current version: {current_str})"
            )
        return 0

    # Surface findings
    print(
        f"flaky-tests.yaml validation FAILED: {len(issues)} issue(s)",
        file=sys.stderr,
    )
    for issue in issues:
        print(issue.format(), file=sys.stderr)
    print(
        "\nTo fix: edit flaky-tests.yaml. EXPIRED entries should be "
        "removed once the root-cause fix has landed (registry shrinks "
        "as bugs get fixed — that's the design).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
