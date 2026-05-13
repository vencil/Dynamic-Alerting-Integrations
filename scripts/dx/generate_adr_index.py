#!/usr/bin/env python3
"""generate_adr_index.py — Auto-render the ADR index table inside docs/architecture-and-design.md.

Discovers `docs/adr/[0-9][0-9][0-9]-*.md` (ZH primary; `.en.md` siblings excluded),
parses each ADR's frontmatter `title` + `## 狀態` H2 block, and renders a Markdown table
between the `<!-- ADR_INDEX_START -->` / `<!-- ADR_INDEX_END -->` sentinels in the target doc.

Modes:
    --check   exit 1 if rendered output differs from current target file (drift gate)
    --write   apply the rendered table to the target file

Why discovery-based (not hand-curated): each new ADR previously required a hub-doc edit
that was easy to forget. ADR-019 / ADR-020 / ADR-021 all reached merge before any hub
cross-reference was added. Pre-commit drift gate kills that class of bug.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
TARGET_DOC = REPO_ROOT / "docs" / "architecture-and-design.md"
SENTINEL_START = "<!-- ADR_INDEX_START -->"
SENTINEL_END = "<!-- ADR_INDEX_END -->"

ADR_FILE_RE = re.compile(r"^(\d{3})-[a-z0-9\-]+\.md$")
TITLE_PREFIX_RE = re.compile(r"^ADR-\d+[:：]\s*")
# Capture the `## 狀態` block until the next `## ` heading or EOF.
STATUS_BLOCK_RE = re.compile(
    r"^##\s+狀態\s*\n(.*?)(?=^##\s|\Z)",
    re.DOTALL | re.MULTILINE,
)
# Match `<emoji> **Status Name**` — covers ✅/🟢/🟡/🔵/⛔/❌/🟠/⚪ + ASCII letter status names.
STATUS_LINE_RE = re.compile(
    r"(?P<emoji>[✅🟢🟡🔵⛔❌🟠⚪])\s*\*\*(?P<name>[A-Za-z][A-Za-z\- ]*?)\*\*"
)
VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class AdrEntry:
    number: str       # zero-padded "001"
    title: str        # bare title, with "ADR-NNN: " prefix stripped
    status_emoji: str
    status_name: str  # e.g. "Accepted", "Proposed", "Extended"
    version: str      # first vX.Y.Z found in the status block (may be "")
    rel_path: str     # relative to architecture-and-design.md, e.g. "adr/001-...-md"


class AdrParseError(ValueError):
    """Raised when an ADR cannot be parsed deterministically."""


def parse_adr(path: Path) -> AdrEntry:
    """Parse a single ZH ADR. Raises AdrParseError on malformed inputs."""
    text = path.read_text(encoding="utf-8")

    fm = FRONTMATTER_RE.match(text)
    if not fm:
        raise AdrParseError(f"{path.name}: missing YAML frontmatter")
    try:
        meta = yaml.safe_load(fm.group(1)) or {}
    except yaml.YAMLError as e:
        raise AdrParseError(f"{path.name}: frontmatter YAML parse error: {e}") from e

    title_raw = (meta.get("title") or "").strip()
    if not title_raw:
        raise AdrParseError(f"{path.name}: frontmatter `title` missing or empty")
    title = TITLE_PREFIX_RE.sub("", title_raw).strip()

    name_match = ADR_FILE_RE.match(path.name)
    if not name_match:
        raise AdrParseError(
            f"{path.name}: filename does not match NNN-kebab-case.md pattern"
        )
    number = name_match.group(1)

    sb = STATUS_BLOCK_RE.search(text)
    if not sb:
        raise AdrParseError(f"{path.name}: missing `## 狀態` section")
    status_block = sb.group(1).strip()
    if not status_block:
        raise AdrParseError(f"{path.name}: `## 狀態` section is empty")

    sl = STATUS_LINE_RE.search(status_block)
    if not sl:
        raise AdrParseError(
            f"{path.name}: no `<emoji> **Status**` pattern found in `## 狀態` section"
        )
    emoji = sl.group("emoji")
    status_name = sl.group("name").strip()

    v = VERSION_RE.search(status_block)
    version = v.group(0) if v else ""

    return AdrEntry(
        number=number,
        title=title,
        status_emoji=emoji,
        status_name=status_name,
        version=version,
        rel_path=f"adr/{path.name}",
    )


def discover_adrs(adr_dir: Path = ADR_DIR) -> List[Path]:
    """Return ZH-primary ADR files sorted by number, excluding `.en.md` siblings."""
    files = []
    for p in sorted(adr_dir.iterdir()):
        if not p.is_file() or p.name.endswith(".en.md"):
            continue
        if ADR_FILE_RE.match(p.name):
            files.append(p)
    return files


def render_table(entries: List[AdrEntry]) -> str:
    """Render a Markdown table. Trailing newline included so sentinel block stays clean."""
    lines = [
        "| ADR | 標題 | 狀態 | 版本 |",
        "|-----|------|------|------|",
    ]
    for e in entries:
        title_cell = f"[{e.title}]({e.rel_path})"
        status_cell = f"{e.status_emoji} {e.status_name}"
        version_cell = e.version or "—"
        lines.append(f"| ADR-{e.number} | {title_cell} | {status_cell} | {version_cell} |")
    return "\n".join(lines) + "\n"


def replace_sentinel_block(content: str, table: str) -> str:
    """Replace whatever sits between SENTINEL_START / SENTINEL_END with the rendered table."""
    pattern = re.compile(
        r"(" + re.escape(SENTINEL_START) + r"\n)"
        r".*?"
        r"(\n" + re.escape(SENTINEL_END) + r")",
        re.DOTALL,
    )
    if not pattern.search(content):
        raise ValueError(
            f"Sentinel block missing in target doc. Add the following two lines around an empty "
            f"region in {TARGET_DOC.relative_to(REPO_ROOT).as_posix()}:\n"
            f"  {SENTINEL_START}\n  {SENTINEL_END}"
        )
    return pattern.sub(r"\1" + table + r"\2", content)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate the ADR index table inside docs/architecture-and-design.md.",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if rendered table differs (drift gate)")
    mode.add_argument("--write", action="store_true", help="apply rendered table to target doc")
    ap.add_argument(
        "--adr-dir",
        type=Path,
        default=ADR_DIR,
        help="ADR directory (default: docs/adr/)",
    )
    ap.add_argument(
        "--target",
        type=Path,
        default=TARGET_DOC,
        help="Target doc to update (default: docs/architecture-and-design.md)",
    )
    args = ap.parse_args()

    adr_files = discover_adrs(args.adr_dir)
    if not adr_files:
        print(f"ERROR: no ADRs found under {args.adr_dir}", file=sys.stderr)
        return 2

    try:
        entries = [parse_adr(p) for p in adr_files]
    except AdrParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    table = render_table(entries)
    current = args.target.read_text(encoding="utf-8")
    try:
        new = replace_sentinel_block(current, table)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        rel_target = args.target.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # `--target` lives outside REPO_ROOT (e.g., test fixtures under /tmp);
        # fall back to the absolute path so the message stays readable.
        rel_target = str(args.target)

    if args.check:
        if new == current:
            print(f"OK: ADR index up-to-date in {rel_target} ({len(entries)} entries)")
            return 0
        print(
            f"DRIFT: ADR index in {rel_target} is stale.\n"
            f"  Run `make adr-index` (or `python scripts/dx/generate_adr_index.py --write`) to sync.",
            file=sys.stderr,
        )
        return 1

    # --write
    if new == current:
        print(f"OK: no change ({len(entries)} entries)")
    else:
        args.target.write_text(new, encoding="utf-8")
        print(f"WROTE: {rel_target} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
