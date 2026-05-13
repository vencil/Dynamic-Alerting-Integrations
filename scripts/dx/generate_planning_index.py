#!/usr/bin/env python3
"""generate_planning_index.py — Auto-render the unified planning index.

Implements [ADR-020](docs/adr/020-planning-ssot.md) Layer 2 (Discovery-based Index Generator)
+ [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2a deliverable.

Sources (each entry filtered to require ``tracking_kind:`` in the enum)
----------------------------------------------------------------------
1. **Top-of-file frontmatter** in ``docs/**/*.md`` — the markdown file is one planning entry.
2. **Embedded YAML code blocks** inside a markdown section (triple-backtick yaml fence
   immediately after an H2/H3 heading) — used in long backlog files where each section is
   its own entry.
3. **``flaky-tests.yaml``** top-level list — when an entry carries ``tracking_kind:`` it
   becomes a planning entry (existing entries with only ``tracked_by:`` are skipped).
4. **Code-comment annotations** that begin at the start of a (line-stripped) line, of the
   form ``// TECH-DEBT(id=TRK-042, status=in-progress, tracking_kind=tech-debt)`` —
   key=value pairs separated by commas; whitespace tolerant. Works in ``//`` and ``#``
   comment styles across .go / .py / .ts / .tsx / .js / .jsx / .yaml. Matches inside
   docstrings / prose / code-spans are intentionally ignored via the leading-whitespace
   anchor.

Output: `docs/internal/planning-index.md` between `<!-- PLANNING_INDEX_START/END -->`
sentinels. Entries grouped by `status`, then sorted by `tracking_kind`, then `id`. Each row
links to the source file (+ line number when available) so reviewers can jump to the SOT.

Modes
-----
- `--check` exit 1 if the rendered output differs from disk (drift gate; CI default)
- `--write` apply the rendered output

Until chunk 3 (frontmatter migration of existing backlogs) lands, this generator's discovery
typically finds 0 entries from real sources. That is the *correct* steady state: the tool
exists so the FIRST entry that adds proper frontmatter immediately appears in the index
without anyone having to remember to update a hub doc.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

import yaml

# Reuse the shared atomic-write helper (LF-forcing + sibling-tmp + os.replace).
# Same import pattern as generate_adr_index.py.
_TOOLS_DX = Path(__file__).resolve().parent.parent / "tools" / "dx"
sys.path.insert(0, str(_TOOLS_DX))
from _atomic_write import atomic_write_text  # noqa: E402

# Make stdout tolerate non-ASCII on Windows shells.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_DOC = REPO_ROOT / "docs" / "internal" / "planning-index.md"
SENTINEL_START = "<!-- PLANNING_INDEX_START -->"
SENTINEL_END = "<!-- PLANNING_INDEX_END -->"

# Used to build absolute GitHub URLs for source files outside the mkdocs site
# (`docs/` tree). Edit if forking.
GITHUB_BLOB_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main"

# Per ADR-020 §Frontmatter Contract.
TRACKING_KINDS = {"tech-debt", "feature", "dx", "regression", "adr", "sprint"}
STATUSES = {
    "proposed", "accepted", "in-progress", "done", "abandoned", "superseded",
}
# Display order: open work first, terminal states last.
STATUS_ORDER = [
    "in-progress", "accepted", "proposed", "done", "abandoned", "superseded",
]

# Frontmatter blocks: opening `---` on its own line through the next closing `---`.
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Embedded YAML code block after an H2/H3 heading. Captures the YAML body for downstream
# parsing. Heading line + optional blank lines + `\`\`\`yaml\n<body>\n\`\`\``.
SECTION_YAML_RE = re.compile(
    r"^(#{2,3})\s+(?P<heading>.+?)\s*\n+```ya?ml\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.MULTILINE,
)

# Code-comment annotation: `// TECH-DEBT(id=..., status=..., tracking_kind=...)`
# Also accept `# TECH-DEBT(...)` for shell / yaml / python.
# The leading `^\s*` anchor (with MULTILINE) is critical: it requires the comment marker
# to be the FIRST non-whitespace token on its line, so prose / docstring / inline-code
# mentions like "such as `// TECH-DEBT(id=...)`" do NOT trigger discovery. Without this
# anchor, this very script's docstring would self-register as a planning entry.
COMMENT_ANNOTATION_RE = re.compile(
    r"^\s*(?://|#)\s*TECH-DEBT\(\s*(?P<body>[^)]+)\)",
    re.MULTILINE,
)

# Scan paths.
DOCS_GLOB = "docs/**/*.md"
FLAKY_TESTS_FILE = REPO_ROOT / "flaky-tests.yaml"
CODE_GLOBS = [
    "components/**/*.go",
    "components/**/*.py",
    "tools/**/*.ts",
    "tools/**/*.tsx",
    "tools/**/*.js",
    "tools/**/*.jsx",
    "tests/**/*.go",
    "tests/**/*.py",
    "tests/**/*.ts",
    "tests/**/*.tsx",
    "scripts/**/*.py",
    "scripts/**/*.sh",
]


@dataclass(frozen=True)
class PlanningEntry:
    id: str
    title: str
    tracking_kind: str
    status: str
    source_path: str  # relative to repo root, forward-slashed
    source_line: int = 0  # 0 == not tracked (file-level frontmatter)
    domain: str = ""
    pr_ref: str = ""
    target_version: str = ""
    owner: str = ""

    def source_link(self) -> str:
        """Markdown link to source.

        Path-resolution rules (avoids the mkdocs-strict ``../../`` jump-out-of-site
        warning that PR #476 self-review caught for migration-guide.md):

        - **Source under ``docs/``** → site-relative path from ``docs/internal/``
          (one ``../`` to reach site root). ``#L<n>`` is intentionally omitted for
          ``.md`` sources because GitHub renders markdown and never anchors to
          source-line numbers there.
        - **Source outside ``docs/``** (scripts/, components/, flaky-tests.yaml, …)
          → absolute GitHub URL; not part of the mkdocs site at all. ``#L<n>``
          anchors work on GitHub's blob view for code files.
        """
        is_md = self.source_path.endswith(".md")
        if self.source_path.startswith("docs/"):
            rel_from_internal = self.source_path[len("docs/"):]
            href = f"../{rel_from_internal}"
            if self.source_line and not is_md:
                href += f"#L{self.source_line}"
        else:
            href = f"{GITHUB_BLOB_BASE}/{self.source_path}"
            if self.source_line and not is_md:
                href += f"#L{self.source_line}"
        display = (
            f"{self.source_path}:{self.source_line}"
            if self.source_line and not is_md
            else self.source_path
        )
        return f"[{display}]({href})"


class PlanningParseError(ValueError):
    """Raised for malformed planning frontmatter / annotation."""


# ---------------------------------------------------------------------------
# Frontmatter + YAML helpers
# ---------------------------------------------------------------------------
def _safe_yaml_load(text: str, *, where: str) -> Optional[dict]:
    """Parse YAML; on failure return None and let caller skip the entry."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PlanningParseError(f"{where}: YAML parse error: {e}") from e
    return loaded if isinstance(loaded, dict) else None


def _coerce_str(value: object) -> str:
    """Convert YAML scalar to a stripped string. Lists collapsed via `,` for display."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(x).strip() for x in value if x is not None)
    return str(value).strip()


def _entry_from_meta(
    meta: dict,
    *,
    source_path: str,
    source_line: int,
    fallback_title: str = "",
) -> Optional[PlanningEntry]:
    """Build a PlanningEntry from a parsed YAML mapping. Returns None if not eligible."""
    tracking_kind = _coerce_str(meta.get("tracking_kind"))
    if tracking_kind not in TRACKING_KINDS:
        return None
    entry_id = _coerce_str(meta.get("id"))
    if not entry_id:
        return None
    status = _coerce_str(meta.get("status"))
    if status not in STATUSES:
        # Spec says status must be in the enum; skip otherwise rather than fail loudly.
        return None
    title = _coerce_str(meta.get("title")) or fallback_title or entry_id
    return PlanningEntry(
        id=entry_id,
        title=title,
        tracking_kind=tracking_kind,
        status=status,
        source_path=source_path,
        source_line=source_line,
        domain=_coerce_str(meta.get("domain")),
        pr_ref=_coerce_str(meta.get("pr_ref")),
        target_version=_coerce_str(meta.get("target_version")),
        owner=_coerce_str(meta.get("owner")),
    )


# ---------------------------------------------------------------------------
# Discovery — Source 1: top-of-file frontmatter in docs/**/*.md
# ---------------------------------------------------------------------------
def _discover_doc_frontmatter(root: Path) -> Iterable[PlanningEntry]:
    for path in sorted(root.glob(DOCS_GLOB)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm_match = FRONTMATTER_RE.match(text)
        if not fm_match:
            continue
        try:
            meta = _safe_yaml_load(
                fm_match.group(1),
                where=str(path.relative_to(root)),
            )
        except PlanningParseError:
            continue
        if not meta:
            continue
        rel = path.relative_to(root).as_posix()
        entry = _entry_from_meta(meta, source_path=rel, source_line=0)
        if entry is not None:
            yield entry


# ---------------------------------------------------------------------------
# Discovery — Source 2: embedded YAML code blocks in docs/**/*.md
# ---------------------------------------------------------------------------
def _discover_section_yaml(root: Path) -> Iterable[PlanningEntry]:
    for path in sorted(root.glob(DOCS_GLOB)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in SECTION_YAML_RE.finditer(text):
            try:
                meta = _safe_yaml_load(
                    m.group("body"),
                    where=f"{path.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}",
                )
            except PlanningParseError:
                continue
            if not meta:
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            rel = path.relative_to(root).as_posix()
            entry = _entry_from_meta(
                meta,
                source_path=rel,
                source_line=line_no,
                fallback_title=m.group("heading").strip(),
            )
            if entry is not None:
                yield entry


# ---------------------------------------------------------------------------
# Discovery — Source 3: flaky-tests.yaml top-level list
# ---------------------------------------------------------------------------
def _discover_flaky_tests(flaky_path: Path, repo_root: Path) -> Iterable[PlanningEntry]:
    if not flaky_path.is_file():
        return
    try:
        text = flaky_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    if not isinstance(loaded, list):
        return
    rel = flaky_path.relative_to(repo_root).as_posix()
    for idx, item in enumerate(loaded):
        if not isinstance(item, dict):
            continue
        # Approximate line number via key occurrence (yaml load drops positions).
        # Fall back to 0 if we cannot find a unique marker.
        line_no = 0
        item_id = _coerce_str(item.get("id") or item.get("test"))
        if item_id:
            for i, raw_line in enumerate(text.splitlines(), 1):
                if item_id in raw_line and raw_line.lstrip().startswith(("- ", "test:")):
                    line_no = i
                    break
        entry = _entry_from_meta(
            item,
            source_path=rel,
            source_line=line_no,
            fallback_title=_coerce_str(item.get("test")),
        )
        if entry is not None:
            yield entry


# ---------------------------------------------------------------------------
# Discovery — Source 4: code-comment annotations
# ---------------------------------------------------------------------------
def _parse_comment_body(body: str) -> Optional[dict]:
    """Parse `key=value, key2=value2` annotation body. Returns dict or None."""
    out: dict = {}
    for part in body.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"\'')
        if k:
            out[k] = v
    return out or None


def _discover_code_annotations(root: Path) -> Iterable[PlanningEntry]:
    seen_globs: set[Path] = set()
    for pattern in CODE_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen_globs:
                continue
            seen_globs.add(path)
            # Skip vendored / archived trees.
            rel = path.relative_to(root).as_posix()
            if any(part in rel for part in ("/node_modules/", "/archive/", "/.git/")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in COMMENT_ANNOTATION_RE.finditer(text):
                fields = _parse_comment_body(m.group("body"))
                if not fields:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                entry = _entry_from_meta(
                    fields,
                    source_path=rel,
                    source_line=line_no,
                )
                if entry is not None:
                    yield entry


# ---------------------------------------------------------------------------
# Aggregation + render
# ---------------------------------------------------------------------------
def discover_all(repo_root: Path = REPO_ROOT) -> List[PlanningEntry]:
    """Run all 4 source discoverers, deduplicate by (id, source_path, source_line)."""
    entries: List[PlanningEntry] = []
    entries.extend(_discover_doc_frontmatter(repo_root))
    entries.extend(_discover_section_yaml(repo_root))
    entries.extend(_discover_flaky_tests(FLAKY_TESTS_FILE, repo_root))
    entries.extend(_discover_code_annotations(repo_root))
    # Dedup: same (id, path, line) — frontmatter and section-yaml on the same file
    # at line 0 vs. line N do not collide.
    seen: set[tuple] = set()
    unique: List[PlanningEntry] = []
    for e in entries:
        key = (e.id, e.source_path, e.source_line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique


def _status_sort_key(status: str) -> int:
    try:
        return STATUS_ORDER.index(status)
    except ValueError:
        return len(STATUS_ORDER)


def render_index(entries: List[PlanningEntry]) -> str:
    """Render the index body that lives between the sentinel markers."""
    if not entries:
        return (
            "_目前 discovery 沒有任何 entry。各 source 加上 ADR-020 §Frontmatter Contract_\n"
            "_要求的 `tracking_kind:` / `id:` / `status:` 欄位後會自動出現於此。_\n"
        )
    # Group by status, preserving STATUS_ORDER.
    by_status: dict[str, List[PlanningEntry]] = {}
    for e in entries:
        by_status.setdefault(e.status, []).append(e)
    lines: List[str] = []
    for status in sorted(by_status, key=_status_sort_key):
        bucket = sorted(by_status[status], key=lambda e: (e.tracking_kind, e.id))
        lines.append(f"### {status} ({len(bucket)})")
        lines.append("")
        lines.append("| ID | Kind | Title | Domain | PR | Source |")
        lines.append("|----|------|-------|--------|------|--------|")
        for e in bucket:
            pr_cell = f"#{e.pr_ref}" if e.pr_ref else "—"
            domain_cell = e.domain or "—"
            lines.append(
                f"| `{e.id}` | {e.tracking_kind} | {e.title} | {domain_cell} | {pr_cell} | {e.source_link()} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def replace_sentinel_block(content: str, body: str) -> str:
    pattern = re.compile(
        r"(" + re.escape(SENTINEL_START) + r"\n)"
        r".*?"
        r"(\n" + re.escape(SENTINEL_END) + r")",
        re.DOTALL,
    )
    if not pattern.search(content):
        raise ValueError(
            f"Sentinel block missing in target doc. Add the two lines "
            f"`{SENTINEL_START}` and `{SENTINEL_END}` around an empty region."
        )
    return pattern.sub(r"\1" + body + r"\2", content)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate the unified planning index (ADR-020 Layer 2).",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if rendered output drifts")
    mode.add_argument("--write", action="store_true", help="apply rendered output to target doc")
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (default: auto-detect from script location)",
    )
    ap.add_argument(
        "--target",
        type=Path,
        default=TARGET_DOC,
        help="target doc to update (default: docs/internal/planning-index.md)",
    )
    args = ap.parse_args()

    entries = discover_all(args.repo_root)
    body = render_index(entries)
    current = args.target.read_text(encoding="utf-8")
    try:
        new = replace_sentinel_block(current, body)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        rel_target = args.target.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel_target = str(args.target)

    if args.check:
        if new == current:
            print(f"OK: planning index up-to-date in {rel_target} ({len(entries)} entries)")
            return 0
        print(
            f"DRIFT: planning index in {rel_target} is stale.\n"
            f"  Run `make planning-index` (or "
            f"`python scripts/dx/generate_planning_index.py --write`) to sync.",
            file=sys.stderr,
        )
        return 1

    # --write
    if new == current:
        print(f"OK: no change ({len(entries)} entries)")
    else:
        atomic_write_text(args.target, new)
        print(f"WROTE: {rel_target} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
