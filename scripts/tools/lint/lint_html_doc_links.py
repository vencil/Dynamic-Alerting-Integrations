#!/usr/bin/env python3
"""lint_html_doc_links.py — Raw HTML doc-link validator for MkDocs output.

Why this exists
---------------
``check_doc_links.py`` scans Markdown only. Raw HTML files embedded under
``docs/`` (notably ``docs/interactive/*.html``) have historically slipped
through because they author links assuming **two incompatible mental models**
at once:

1. **GitHub repo browsing** — treats ``docs/`` as a real folder, so
   ``../../docs/foo.md`` works.
2. **MkDocs rendered site** — flattens ``docs/`` into the site root and
   (with ``use_directory_urls: true``) rewrites ``foo.md`` → ``foo/``.

With GH Pages subpath deployment (``/Dynamic-Alerting-Integrations/``),
``../../`` from ``interactive/index.html`` escapes the project root entirely
and lands at ``https://vencil.github.io/`` → 404.

This linter catches that class of bug in raw ``.html`` files under ``docs/``.

Rules enforced
--------------
For every ``href=`` / ``src=`` attribute in ``docs/**/*.html``:

- **R1** — Forbid ``../../`` (two-level parent). From any file under
  ``docs/interactive/`` or ``docs/getting-started/``, two levels up exits the
  site root. Use ``../<name>/`` (single level) or an external URL instead.

- **R2** — Forbid ``.md`` extensions in ``href``. MkDocs with
  ``use_directory_urls: true`` rewrites ``foo.md`` → ``foo/``. Raw HTML must
  use the rewritten form.

- **R3** — Forbid ``docs/`` prefix. After MkDocs build, ``docs/`` becomes
  the site root; any link starting with ``docs/`` is wrong.

- **R4** — Forbid links to top-level repo files that are not MkDocs pages
  (``README.md``, ``CLAUDE.md``, ``LICENSE``). They should point to the
  external GitHub URL instead.

Whitelist
---------
- External URLs (``http://``, ``https://``, ``mailto:``, ``//``)
- Fragment-only (``#anchor``)
- Data URIs (``data:``)
- Asset paths kept as-is (``../assets/``, ``assets/``, ``img/``)

Usage
-----
::

    python3 scripts/tools/lint/lint_html_doc_links.py          # report mode
    python3 scripts/tools/lint/lint_html_doc_links.py --ci      # exit 1 on failures
    python3 scripts/tools/lint/lint_html_doc_links.py --verbose # show all links

Exit codes
----------
- ``0`` — clean or report-only mode
- ``1`` — violations found and ``--ci`` flag set
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DOCS_ROOT = PROJECT_ROOT / "docs"

# Attribute extractor: href="..." or src="..." (single or double quote)
_ATTR_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# Top-level repo files not published by MkDocs (README.md excluded via
# mkdocs.yml exclude_docs). Linking to these with a relative path is wrong.
_REPO_ONLY_FILES = {
    "README.md",
    "README.en.md",
    "CLAUDE.md",
    "LICENSE",
    "LICENSE.md",
    "CONTRIBUTING.md",
}


@dataclass
class Violation:
    """A single rule violation found in a raw HTML file under docs/."""

    path: str
    line: int
    rule: str
    target: str
    hint: str


def _is_external_or_anchor(target: str) -> bool:
    """True if the target is external, a fragment, or a data URI — skip."""
    t = target.strip()
    if not t:
        return True
    if t.startswith(("http://", "https://", "mailto:", "tel:", "ftp://", "//", "data:", "#")):
        return True
    return False


def _check_target(target: str) -> list[tuple[str, str]]:
    """Return list of (rule_id, hint) violations for a single target."""
    issues: list[tuple[str, str]] = []

    if _is_external_or_anchor(target):
        return issues

    # Strip query / fragment for analysis
    clean = target.split("#", 1)[0].split("?", 1)[0]
    if not clean:
        return issues

    # R1: two-level parent escape
    if "../../" in clean:
        issues.append((
            "R1",
            "'../../' escapes site root under GH Pages subpath. "
            "Use single-level '../<name>/' or an external https:// URL.",
        ))

    # R2: .md extension
    if re.search(r"\.md(?:/|$)", clean):
        issues.append((
            "R2",
            "MkDocs with use_directory_urls rewrites '.md' → '/'. "
            "Use '<name>/' (trailing slash) instead of '<name>.md'.",
        ))

    # R3: docs/ prefix
    if re.match(r"^(?:\./)?docs/", clean) or "/docs/" in clean:
        issues.append((
            "R3",
            "'docs/' is the MkDocs site root after build. "
            "Drop the 'docs/' prefix from the link target.",
        ))

    # R4: linking to repo-only top-level files
    # Match basenames that appear at the tail of the path.
    basename = clean.rstrip("/").split("/")[-1]
    if basename in _REPO_ONLY_FILES:
        issues.append((
            "R4",
            f"'{basename}' is not published by MkDocs. Use the external GitHub URL: "
            f"'https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/{basename}'.",
        ))

    return issues


def scan_file(path: Path) -> list[Violation]:
    """Scan a single HTML file and return any violations found."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(path.relative_to(PROJECT_ROOT))
    violations: list[Violation] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _ATTR_RE.finditer(line):
            target = match.group(1)
            for rule_id, hint in _check_target(target):
                violations.append(
                    Violation(
                        path=rel,
                        line=lineno,
                        rule=rule_id,
                        target=target,
                        hint=hint,
                    )
                )

    return violations


def collect_html_files() -> list[Path]:
    """Return all .html files under docs/ (recursive).

    Uses os.walk with onerror=ignore so stray unreadable entries (e.g. a
    broken symlink on a mounted filesystem) do not abort the whole scan.
    """
    import os

    if not DOCS_ROOT.exists():
        return []

    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(DOCS_ROOT, onerror=lambda _e: None):
        # Skip symlinked dirs that would resolve outside docs/
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.endswith(".html"):
                results.append(Path(dirpath) / name)
    return sorted(results)


def main() -> int:
    """CLI entry point: scan raw HTML docs for link-pattern violations."""
    parser = argparse.ArgumentParser(
        description="Lint raw HTML files under docs/ for MkDocs-incompatible link patterns."
    )
    parser.add_argument("--ci", action="store_true", help="Exit 1 on violations")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="List every scanned file"
    )
    args = parser.parse_args()

    files = collect_html_files()
    if not files:
        print("✓ No HTML files found under docs/")
        return 0

    all_violations: list[Violation] = []
    for f in files:
        if args.verbose:
            print(f"  scan {f.relative_to(PROJECT_ROOT)}")
        all_violations.extend(scan_file(f))

    if not all_violations:
        print(f"✓ {len(files)} HTML file(s) — no link-pattern violations")
        return 0

    # Group violations by file for cleaner reporting
    by_file: dict[str, list[Violation]] = {}
    for v in all_violations:
        by_file.setdefault(v.path, []).append(v)

    print(f"✗ {len(all_violations)} violation(s) in {len(by_file)} file(s):\n")
    for path, items in sorted(by_file.items()):
        print(f"  {path}")
        for v in items:
            print(f"    [{v.rule}] line {v.line}: {v.target}")
            print(f"          → {v.hint}")
        print()

    print(
        "Fix guide: MkDocs flattens docs/ and GH Pages deploys under a project "
        "subpath. Raw HTML under docs/ must use link forms that match the rendered "
        "site, not the repo file tree."
    )

    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
