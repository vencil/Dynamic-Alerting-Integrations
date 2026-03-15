#!/usr/bin/env python3
"""
sync_glossary_abbr.py — Sync abbreviations from glossary.md to MkDocs snippet.

Automatically extracts term definitions from docs/glossary.md and generates
a MkDocs abbreviations snippet file (docs/includes/abbreviations.md) in the
format: *[ABBR]: Full expansion — short description

This enables MkDocs to auto-expand abbreviations in rendered pages.

Usage:
    python3 sync_glossary_abbr.py [--check] [--verbose] [--glossary PATH] [--output PATH]

Flags:
    --check         Don't write; exit 1 if out of sync (for CI)
    --verbose       Show each extracted term
    --glossary PATH Override glossary path (default: docs/glossary.md)
    --output PATH   Override output path (default: docs/includes/abbreviations.md)

Examples:
    # Sync and write abbreviations
    python3 sync_glossary_abbr.py

    # Check if glossary and snippet are in sync (for CI)
    python3 sync_glossary_abbr.py --check

    # Show what will be synced with verbose output
    python3 sync_glossary_abbr.py --verbose
"""
import argparse
import os
import re
import sys
from pathlib import Path


# Known abbreviations that don't require parentheses to be recognized
# Maps abbreviation → (full_expansion, none if to be extracted from context)
KNOWN_ABBRS = {
    "PromQL": ("Prometheus Query Language", None),
    "TSDB": ("Time Series Database", None),
    "AST": ("Abstract Syntax Tree", None),
    "HA": ("High Availability", None),
    "OCI": ("Open Container Initiative", None),
    "BYO": ("Bring Your Own", None),
    "SRE": ("Site Reliability Engineering", None),
    "NOC": ("Network Operations Center", None),
    "ADR": ("Architecture Decision Record", None),
    "SSRF": ("Server-Side Request Forgery", None),
    "RBAC": ("Role-Based Access Control", None),
    "CI": ("Continuous Integration", None),
    "CD": ("Continuous Delivery", None),
    "CRD": ("Custom Resource Definition", None),
    "DBA": ("Database Administrator", None),
    "SOP": ("Standard Operating Procedure", None),
}


def extract_abbreviations_from_glossary(glossary_path: Path) -> dict:
    """
    Extract abbreviations from glossary.md.

    Looks for patterns like:
    - **ADR (Architecture Decision Record)** → ADR: Architecture Decision Record
    - **AST 遷移引擎 (AST Migration Engine)** → AST: Abstract Syntax Tree
    - **PromQL** → PromQL: (known abbreviation)
    - **BYO (Bring Your Own)** → BYO: Bring Your Own

    Returns dict: {abbr: (full_expansion, short_description)}
    """
    abbreviations = {}

    if not glossary_path.exists():
        print(f"ERROR: Glossary file not found: {glossary_path}", file=sys.stderr)
        sys.exit(1)

    with open(glossary_path, encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for bold term at start of line: **TERM (Expansion)** or **TERM**
        match = re.match(r"^\*\*([^*]+)\*\*", line)
        if match:
            term_part = match.group(1).strip()

            # Pattern 1: **ABBR Chinese (ABBR English Expansion)** like **AST 遷移引擎 (AST Migration Engine)**
            nested_match = re.match(r"^([A-Z][A-Za-z0-9]*)\s+[^\(]*\s*\(\s*\1\s+(.+?)\s*\)$", term_part)
            if nested_match:
                abbr = nested_match.group(1)  # Use English abbreviation
                rest_expansion = nested_match.group(2).strip()  # e.g., "Migration Engine"

                # If we have a known expansion for the abbr, use it
                if abbr in KNOWN_ABBRS:
                    known_exp, _ = KNOWN_ABBRS[abbr]
                    # Combine: "Abstract Syntax Tree Migration Engine"
                    expansion = f"{known_exp} {rest_expansion}"
                else:
                    expansion = rest_expansion
                abbreviations[abbr] = (expansion, "")
            else:
                # Pattern 2: **ABBR (Full Expansion)**
                simple_paren_match = re.match(r"^([A-Z][A-Za-z0-9]*)\s*\(([^)]+)\)$", term_part)
                if simple_paren_match:
                    abbr = simple_paren_match.group(1)
                    expansion = simple_paren_match.group(2).strip()
                    abbreviations[abbr] = (expansion, "")
                elif term_part in KNOWN_ABBRS:
                    # Pattern 3: Known abbreviation without explicit expansion
                    abbr = term_part
                    default_expansion, _ = KNOWN_ABBRS[abbr]
                    abbreviations[abbr] = (default_expansion, "")

            # Look ahead for definition line (lines starting with :)
            if i + 1 < len(lines):
                def_line = lines[i + 1]
                if def_line.startswith(":   "):
                    # Extract short description (first sentence or up to 120 chars)
                    description = def_line[4:].strip()
                    # Truncate at first period/Chinese period/comma if needed
                    first_sentence = re.split(r"[。，；]", description)[0]
                    short_desc = first_sentence.strip()
                    if len(short_desc) > 120:
                        short_desc = short_desc[:117] + "..."

                    # Update with description if we found a term
                    if nested_match:
                        abbr = nested_match.group(1)
                        rest_expansion = nested_match.group(2).strip()
                        # Apply the same expansion logic as above
                        if abbr in KNOWN_ABBRS:
                            known_exp, _ = KNOWN_ABBRS[abbr]
                            expansion = f"{known_exp} {rest_expansion}"
                        else:
                            expansion = rest_expansion
                        abbreviations[abbr] = (expansion, short_desc)
                    elif simple_paren_match:
                        abbr = simple_paren_match.group(1)
                        expansion = simple_paren_match.group(2).strip()
                        abbreviations[abbr] = (expansion, short_desc)
                    elif term_part in KNOWN_ABBRS:
                        abbr = term_part
                        default_expansion, _ = KNOWN_ABBRS[abbr]
                        abbreviations[abbr] = (default_expansion, short_desc)

        i += 1

    return abbreviations


def generate_abbreviations_file(abbreviations: dict) -> str:
    """Generate the MkDocs abbreviations snippet content."""
    lines = []

    # Sort by abbreviation for consistency
    for abbr in sorted(abbreviations.keys()):
        expansion, description = abbreviations[abbr]
        if description:
            line = f"*[{abbr}]: {expansion} — {description}"
        else:
            line = f"*[{abbr}]: {expansion}"
        lines.append(line)

    return "\n".join(lines) + "\n" if lines else ""


def main():
    """CLI entry point: Sync abbreviations from glossary.md to MkDocs snippet."""
    parser = argparse.ArgumentParser(
        description="Sync abbreviations from glossary.md to MkDocs snippet file."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Don't write; exit 1 if out of sync (for CI)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show each extracted term",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=Path("docs/glossary.md"),
        help="Override glossary path (default: docs/glossary.md)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/includes/abbreviations.md"),
        help="Override output path (default: docs/includes/abbreviations.md)",
    )

    args = parser.parse_args()

    # Convert to absolute paths if relative
    glossary_path = args.glossary if args.glossary.is_absolute() else Path.cwd() / args.glossary
    output_path = args.output if args.output.is_absolute() else Path.cwd() / args.output

    # Extract abbreviations
    abbreviations = extract_abbreviations_from_glossary(glossary_path)

    if args.verbose:
        print(f"Extracted {len(abbreviations)} abbreviations:")
        for abbr in sorted(abbreviations.keys()):
            expansion, description = abbreviations[abbr]
            desc_preview = f" — {description[:60]}" if description else ""
            print(f"  {abbr}: {expansion}{desc_preview}")

    # Generate content
    new_content = generate_abbreviations_file(abbreviations)

    # Check mode
    if args.check:
        if output_path.exists():
            with open(output_path, encoding="utf-8") as f:
                existing_content = f.read()
            if existing_content == new_content:
                print(
                    f"✓ In sync: {len(abbreviations)} abbreviations match glossary.md"
                )
                sys.exit(0)
            else:
                print(
                    f"✗ Out of sync: abbreviations.md does not match glossary.md",
                    file=sys.stderr,
                )
                print(f"  Glossary has {len(abbreviations)} abbreviations", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"✗ Output file not found: {output_path}", file=sys.stderr)
            sys.exit(1)

    # Write mode
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    # Set permissions to 0o600 for security (SAST convention)
    os.chmod(output_path, 0o600)

    print(f"Synced {len(abbreviations)} abbreviations from glossary.md → abbreviations.md")


if __name__ == "__main__":
    main()
