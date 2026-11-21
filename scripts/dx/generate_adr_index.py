#!/usr/bin/env python3
"""generate_adr_index.py — Auto-render the ADR index tables.

Discovers `docs/adr/[0-9][0-9][0-9]-*.md` (ZH primary; `.en.md` siblings excluded),
parses each ADR's frontmatter `title` + `## 狀態` H2 block, and renders a Markdown table
between the `<!-- ADR_INDEX_START -->` / `<!-- ADR_INDEX_END -->` sentinels in each target:
`docs/architecture-and-design.md`, `docs/adr/README.md` and `docs/adr/README.en.md` (the
EN table takes title and link from the `.en.md` sibling when there is one). `--target`
renders a single ZH target instead.

Modes:
    --check   exit 1 if rendered output differs from current target file (drift gate)
    --write   apply the rendered table to the target file

Both modes first check that each ADR states one status in all the places it is
written (#2664), and exit 1 without writing when they disagree:
    - frontmatter `status:` — read by `generate_planning_index.py`
    - the `## 狀態` line   — read here, rendered into the index tables
    - the `.en.md` sibling's `## Status` line — read by no generator, so
      nothing else would notice it going stale
The two status lines are compared after normalising the name (`In Progress`
→ `in-progress`); the emoji is not compared. The frontmatter value is not
normalised: it must be spelled exactly as one of `generate_planning_index`'s
STATUSES, because the planning index silently skips an entry whose status is
not — so a missing `status:`, `status: Accepted` or a status name outside that
set (`Extended`) is a mismatch too, as is an `.en.md` without a readable
`## Status` line.

Only the first `<emoji> **Name**` in each status block is read — the same one
the index table shows. A block that appends a later status after the first
(`✅ **Accepted** → ⛔ **Superseded**`) is read as the first.

Why discovery-based (not hand-curated): each new ADR previously required a hub-doc edit
that was easy to forget. ADR-018 / ADR-019 / ADR-020 all reached merge before any hub
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

# Reuse the atomic-write helper already shared by generate_doc_map.py /
# generate_tool_map.py. It defaults to `newline="\n"` (forces LF so the same
# regen run on Windows and Linux produces byte-identical output — without it,
# Path.write_text translates "\n" to os.linesep and Windows local invocations
# emit CRLF, creating noise diffs even though .gitattributes normalises on
# commit) and writes via a private tmp + os.replace, defending against FUSE
# Trap #60 mid-flush corruption.
_TOOLS_DX = Path(__file__).resolve().parent.parent / "tools" / "dx"
sys.path.insert(0, str(_TOOLS_DX))
sys.path.insert(0, str(_TOOLS_DX.parent))
from _lib_yaml_strict import strict_safe_load  # noqa: E402  (#4111: duplicate keys refused)
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#2128)
from _lib_io import write_repo_artifact  # noqa: E402  (#3107 v2)
# The frontmatter status vocabulary: the planning index's own set (#2664).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_planning_index import STATUSES as PLANNING_STATUSES  # noqa: E402

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
TARGET_DOC = REPO_ROOT / "docs" / "architecture-and-design.md"
# (path, link prefix, language). Links are relative to the target's own directory.
DEFAULT_TARGETS = (
    (TARGET_DOC, "adr/", "zh"),
    (ADR_DIR / "README.md", "./", "zh"),
    (ADR_DIR / "README.en.md", "./", "en"),
)
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
# The `.en.md` sibling's status block, read with the same STATUS_LINE_RE.
EN_STATUS_BLOCK_RE = re.compile(
    r"^##\s+Status\s*\n(.*?)(?=^##\s|\Z)",
    re.DOTALL | re.MULTILINE,
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
    en_title: str = ""     # title from the `.en.md` sibling, "" when there is none
    en_filename: str = ""  # the `.en.md` sibling's name, "" when there is none
    fm_status: str = ""    # frontmatter `status:` as written, "" when absent
    en_status_name: str = ""  # the `.en.md` sibling's `## Status` name, "" when unreadable


class AdrParseError(ValueError):
    """Raised when an ADR cannot be parsed deterministically."""


def parse_adr(path: Path) -> AdrEntry:
    """Parse a single ZH ADR. Raises AdrParseError on malformed inputs."""
    text = path.read_text(encoding="utf-8")

    fm = FRONTMATTER_RE.match(text)
    if not fm:
        raise AdrParseError(f"{path.name}: missing YAML frontmatter")
    try:
        meta = strict_safe_load(fm.group(1)) or {}
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

    en_title, en_filename, en_status_name = "", "", ""
    en_path = path.with_name(path.name[: -len(".md")] + ".en.md")
    if en_path.is_file():
        en_text = en_path.read_text(encoding="utf-8")
        en_fm = FRONTMATTER_RE.match(en_text)
        en_meta = (strict_safe_load(en_fm.group(1)) or {}) if en_fm else {}
        en_title = TITLE_PREFIX_RE.sub("", (en_meta.get("title") or "").strip()).strip()
        en_filename = en_path.name
        en_sb = EN_STATUS_BLOCK_RE.search(en_text)
        en_sl = STATUS_LINE_RE.search(en_sb.group(1)) if en_sb else None
        en_status_name = en_sl.group("name").strip() if en_sl else ""

    return AdrEntry(
        number=number,
        title=title,
        status_emoji=emoji,
        status_name=status_name,
        version=version,
        rel_path=f"adr/{path.name}",
        en_title=en_title,
        en_filename=en_filename,
        fm_status=str(meta.get("status") or "").strip(),
        en_status_name=en_status_name,
    )


def _norm_status(name: str) -> str:
    """`In Progress` / `in-progress` / `Accepted` → the frontmatter spelling."""
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def status_mismatches(entries: List[AdrEntry]) -> List[str]:
    """One line per ADR whose status places disagree (#2664). Empty = consistent."""
    out = []
    for e in entries:
        body = _norm_status(e.status_name)
        zh_file = e.rel_path.split("/", 1)[1]
        if not e.fm_status:
            out.append(
                f"ADR-{e.number}: {zh_file} has no frontmatter `status:` "
                f"(`## 狀態` says {e.status_name!r})"
            )
        elif e.fm_status not in PLANNING_STATUSES:
            out.append(
                f"ADR-{e.number}: {zh_file} frontmatter `status: {e.fm_status}` is not one of "
                f"{sorted(PLANNING_STATUSES)} (the planning index drops the entry)"
            )
        elif e.fm_status != body:
            out.append(
                f"ADR-{e.number}: {zh_file} frontmatter `status: {e.fm_status}` "
                f"!= `## 狀態` {e.status_name!r}"
            )
        if e.en_filename:
            if not e.en_status_name:
                out.append(
                    f"ADR-{e.number}: {e.en_filename} has no `<emoji> **Status**` line "
                    f"under `## Status` (`## 狀態` says {e.status_name!r})"
                )
            elif _norm_status(e.en_status_name) != body:
                out.append(
                    f"ADR-{e.number}: {e.en_filename} `## Status` {e.en_status_name!r} "
                    f"!= {zh_file} `## 狀態` {e.status_name!r}"
                )
    return out


def discover_adrs(adr_dir: Path = ADR_DIR) -> List[Path]:
    """Return ZH-primary ADR files sorted by number, excluding `.en.md` siblings."""
    files = []
    for p in sorted(adr_dir.iterdir()):
        if not p.is_file() or p.name.endswith(".en.md"):
            continue
        if ADR_FILE_RE.match(p.name):
            files.append(p)
    return files


def render_table(entries: List[AdrEntry], link_prefix: str = "adr/", lang: str = "zh") -> str:
    """Render a Markdown table. Trailing newline included so sentinel block stays clean.

    `lang="en"` uses the `.en.md` sibling's title and link where one exists and falls
    back to the ZH file otherwise (some ADRs are ZH-only by language policy).
    """
    header = "| ADR | Title | Status | Version |" if lang == "en" else "| ADR | 標題 | 狀態 | 版本 |"
    lines = [header, "|-----|------|------|------|"]
    for e in entries:
        title, filename = e.title, e.rel_path.split("/", 1)[1]
        if lang == "en" and e.en_filename:
            title, filename = e.en_title or e.title, e.en_filename
        title_cell = f"[{title}]({link_prefix}{filename})"
        status_cell = f"{e.status_emoji} {e.status_name}"
        version_cell = e.version or "—"
        lines.append(f"| ADR-{e.number} | {title_cell} | {status_cell} | {version_cell} |")
    return "\n".join(lines) + "\n"


def replace_sentinel_block(content: str, table: str, target: Path = TARGET_DOC) -> str:
    """Replace whatever sits between SENTINEL_START / SENTINEL_END with the rendered table."""
    pattern = re.compile(
        r"(" + re.escape(SENTINEL_START) + r"\n)"
        r"(?:.*?\n)?"  # optional: the two sentinels may sit on adjacent lines
        r"(" + re.escape(SENTINEL_END) + r")",
        re.DOTALL,
    )
    if not pattern.search(content):
        raise ValueError(
            f"Sentinel block missing in target doc. Add the following two lines around an empty "
            f"region in {_display(target)}:\n"
            f"  {SENTINEL_START}\n  {SENTINEL_END}"
        )
    return pattern.sub(lambda m: m.group(1) + table + "\n" + m.group(2), content, count=1)


def _display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # `--target` lives outside REPO_ROOT (e.g., test fixtures under /tmp);
        # fall back to the absolute path so the message stays readable.
        return str(path)


@exit_on_output_write_error
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate the ADR index tables (architecture-and-design.md + docs/adr/README{,.en}.md).",
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
        default=None,
        help="Render a single ZH target doc instead of the default three",
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

    mismatches = status_mismatches(entries)
    if mismatches:
        for line in mismatches:
            print(f"STATUS MISMATCH: {line}", file=sys.stderr)
        print(
            "  An ADR's status is written in up to three places: frontmatter `status:`, "
            "the `## 狀態` line, and the `.en.md` `## Status` line. Change all of them "
            "together; nothing was written.",
            file=sys.stderr,
        )
        return 1

    targets = [(args.target, "adr/", "zh")] if args.target else list(DEFAULT_TARGETS)
    stale, changed = [], []
    for target, prefix, lang in targets:
        current = target.read_text(encoding="utf-8")
        try:
            new = replace_sentinel_block(current, render_table(entries, prefix, lang), target)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if new != current:
            stale.append(_display(target))
            if args.write:
                # #2128: rc 2 + one line naming the doc; --target is named
                # only when the operator gave it.
                flag = "--target" if args.target else None
                with output_write(target, flag=flag):
                    write_repo_artifact(target, new, flag=flag)
                changed.append(_display(target))

    if args.check:
        if not stale:
            print(f"OK: ADR index up-to-date in {len(targets)} target(s) ({len(entries)} entries)")
            return 0
        for name in stale:
            print(f"DRIFT: ADR index in {name} is stale.", file=sys.stderr)
        print(
            "  Run `make adr-index` (or `python scripts/dx/generate_adr_index.py --write`) to sync.",
            file=sys.stderr,
        )
        return 1

    # --write
    if not changed:
        print(f"OK: no change ({len(entries)} entries)")
    for name in changed:
        print(f"WROTE: {name} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
