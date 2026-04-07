#!/usr/bin/env python3
"""
Auto-generate "相關資源 / Related Resources" tables in documentation files.

Scans all .md files, extracts front matter (tags, audience, lang), computes
related docs by tag/audience matching, and replaces the related resources section.

Modes:
  --check    : Compare current sections with auto-generated, exit 1 if drift
  --update   : Rewrite sections in-place
  --dry-run  : Print what would change (default)

Scoring: +2 for matching tag, +1 for matching audience. Top 5-8 by score.
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def extract_frontmatter(content: str) -> Tuple[Dict[str, any], str]:
    """Extract YAML front matter from markdown file.

    Returns: (frontmatter_dict, body_content)
    """
    if not content.startswith("---"):
        return {}, content

    match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    frontmatter_str = match.group(1)
    body = match.group(2)

    fm = {}
    for line in frontmatter_str.split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            # Parse simple YAML-ish values
            if val.startswith("[") and val.endswith("]"):
                # List value
                val = [v.strip() for v in val[1:-1].split(",")]
            elif val.lower() in ("true", "false"):
                val = val.lower() == "true"

            fm[key] = val

    return fm, body


def get_related_docs(
    doc_path: str,
    doc_fm: Dict[str, any],
    all_docs: Dict[str, Tuple[str, Dict[str, any]]],
    min_score: int = 2
) -> List[Tuple[str, str, int]]:
    """Compute related docs for a given document.

    Returns: [(rel_path, title, score), ...] sorted by score descending.
    """
    doc_tags = set(doc_fm.get("tags", []) if isinstance(doc_fm.get("tags"), list) else [])
    doc_audience = set(doc_fm.get("audience", []) if isinstance(doc_fm.get("audience"), list) else [])

    scores = []
    for other_path, (title, other_fm) in all_docs.items():
        if other_path == doc_path:
            continue

        other_tags = set(other_fm.get("tags", []) if isinstance(other_fm.get("tags"), list) else [])
        other_audience = set(other_fm.get("audience", []) if isinstance(other_fm.get("audience"), list) else [])

        # Skip if different language and not .en.md variant
        doc_lang = doc_fm.get("lang", "zh")
        other_lang = other_fm.get("lang", "zh")
        if doc_lang != other_lang and not (
            doc_path.endswith(".md") and other_path == doc_path.replace(".md", ".en.md") or
            doc_path.endswith(".en.md") and other_path == doc_path.replace(".en.md", ".md")
        ):
            continue

        score = 0
        score += 2 * len(doc_tags & other_tags)
        score += len(doc_audience & other_audience)

        if score >= min_score:
            scores.append((other_path, title, score))

    # Sort by score descending, then alphabetically
    scores.sort(key=lambda x: (-x[2], x[0]))
    return scores[:8]  # Top 8


def generate_related_section(
    related_docs: List[Tuple[str, str, int]],
    lang: str = "zh"
) -> str:
    """Generate markdown table for related resources.

    lang: 'zh' for Chinese, 'en' for English
    """
    if not related_docs:
        if lang == "zh":
            return "## 相關資源\n\n暫無相關資源。\n"
        else:
            return "## Related Resources\n\nNo related resources.\n"

    if lang == "zh":
        header = "## 相關資源"
        table_header = "| 資源 | 相關性 |"
        table_sep = "|------|--------|"
    else:
        header = "## Related Resources"
        table_header = "| Resource | Relevance |"
        table_sep = "|----------|-----------|"

    lines = [header, "", table_header, table_sep]
    for path, title, score in related_docs:
        # Convert path to markdown link
        link = f"[{title}](./{path})" if "/" not in path else f"[{title}]({path})"
        # Score interpretation
        if score >= 4:
            relevance = "⭐⭐⭐" if lang == "zh" else "★★★"
        elif score >= 2:
            relevance = "⭐⭐" if lang == "zh" else "★★"
        else:
            relevance = "⭐" if lang == "zh" else "★"
        lines.append(f"| {link} | {relevance} |")

    lines.append("")
    return "\n".join(lines)


def extract_existing_section(body: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Extract existing related resources section from body.

    Returns: (existing_section, body_before, remaining_after_section)

    Matches from "## 相關資源" (or variants) to end of file or next "---".
    """
    # Patterns: match the entire section from heading to EOF or next ---
    patterns = [
        r"^## 相關資源.*$",
        r"^## 參考資源.*$",
        r"^## Related Resources.*$"
    ]

    for pattern in patterns:
        match = re.search(pattern, body, re.MULTILINE)
        if match:
            # Found the heading. Now extract everything from it to EOF.
            start = match.start()
            before = body[:start].rstrip()

            # Check if there's a --- separator after this section
            rest = body[match.end():]
            sep_match = re.search(r"\n---\n", rest)
            if sep_match:
                # There's a --- separator after section
                after = rest[sep_match.start():]
                existing_section = rest[:sep_match.start()].rstrip()
            else:
                # No separator, take everything to EOF
                existing_section = rest.rstrip()
                after = ""

            return existing_section, before, after

    return None, body, None


def replace_related_section(body: str, new_section: str) -> str:
    """Replace or append related resources section."""
    existing, before, after = extract_existing_section(body)

    if existing:
        # Replace existing section
        result = before + "\n\n" + new_section
        if after and after.strip():
            result += "\n" + after
    else:
        # Append at end
        result = body.rstrip() + "\n\n" + new_section

    return result


def process_file(
    file_path: str,
    all_docs: Dict[str, Tuple[str, Dict[str, any]]],
    min_score: int = 2,
    mode: str = "dry-run"
) -> Tuple[bool, Optional[str]]:
    """Process a single markdown file.

    Returns: (changed, error_msg)
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        fm, body = extract_frontmatter(content)
        if not fm or not body:
            return False, None

        lang = fm.get("lang", "zh")
        title = fm.get("title", Path(file_path).stem)

        # Compute related docs
        rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
        related = get_related_docs(rel_path, fm, all_docs, min_score)

        new_section = generate_related_section(related, lang)
        new_body = replace_related_section(body, new_section)

        # Reconstruct full content
        new_content = f"---\n{_dict_to_yaml(fm)}---\n{new_body}"

        if content == new_content:
            return False, None

        if mode == "dry-run":
            print(f"Would update: {file_path}")
            print(f"  Added {len(related)} related docs\n")
            return True, None
        elif mode == "check":
            return True, None
        elif mode == "update":
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.chmod(file_path, 0o644)
            print(f"Updated: {file_path}")
            return True, None

    except OSError as e:
        return False, str(e)


def _dict_to_yaml(d: Dict[str, any]) -> str:
    """Convert dict to YAML front matter format."""
    lines = []
    for key, val in d.items():
        if isinstance(val, list):
            val_str = "[" + ", ".join(str(v) for v in val) + "]"
        elif isinstance(val, bool):
            val_str = "true" if val else "false"
        else:
            val_str = str(val)
        lines.append(f"{key}: {val_str}")
    return "\n".join(lines) + "\n"


def main():
    """CLI entry point: Auto-generate "相關資源 / Related Resources" tables in documentation files."""
    parser = argparse.ArgumentParser(
        description="Auto-generate 相關資源 / Related Resources tables in markdown docs"
    )
    parser.add_argument("--check", action="store_true", help="Check mode (exit 1 if drift)")
    parser.add_argument("--update", action="store_true", help="Update files in-place")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without modifying (default)")
    parser.add_argument("--docs-dir", default="docs", help="Docs directory (default: docs/)")
    parser.add_argument("--min-score", type=int, default=2, help="Minimum relevance score (default: 2)")

    args = parser.parse_args()

    mode = "update" if args.update else ("check" if args.check else "dry-run")
    docs_dir = args.docs_dir

    if not os.path.isdir(docs_dir):
        print(f"Error: docs directory not found: {docs_dir}", file=sys.stderr)
        sys.exit(1)

    # Scan all markdown files
    all_docs = {}
    for root, dirs, files in os.walk(docs_dir):
        for fname in files:
            if fname.endswith(".md"):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    fm, _ = extract_frontmatter(content)
                    title = fm.get("title", fname.replace(".md", ""))
                    rel_path = os.path.relpath(fpath, docs_dir)
                    all_docs[rel_path] = (title, fm)
                except OSError:
                    pass

    # Process each file
    changed_count = 0
    error_count = 0
    for file_path in all_docs.keys():
        full_path = os.path.join(docs_dir, file_path)
        changed, error = process_file(full_path, all_docs, args.min_score, mode)
        if error:
            print(f"Error processing {file_path}: {error}", file=sys.stderr)
            error_count += 1
        elif changed:
            changed_count += 1

    if mode == "check" and changed_count > 0:
        print(f"Found {changed_count} files with drift", file=sys.stderr)
        sys.exit(1)

    if mode in ("dry-run", "check"):
        print(f"\nTotal: {changed_count} files would change, {error_count} errors")
    else:
        print(f"\nTotal: {changed_count} files updated, {error_count} errors")


if __name__ == "__main__":
    main()
