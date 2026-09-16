#!/usr/bin/env python3
"""Generate CHANGELOG draft entries from conventional commits.

Usage:
    # Draft from last tag to HEAD
    generate_changelog.py

    # Draft from specific tag
    generate_changelog.py --since v1.12.0

    # Draft with version label
    generate_changelog.py --version v1.13.0

    # Output as markdown file
    generate_changelog.py --version v1.13.0 -o changelog-draft.md

    # Check mode: verify all commits since tag follow conventional format
    generate_changelog.py --check
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo tools root
from _lib_python import write_text_or_die  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
# ⛔ One ruler. `agent_output_metrics` is the tool that MEASURED the changelog
# (entry definition, cap); the lint below must count with the same functions
# or "over the cap" here and "over the cap" there will drift apart.
from agent_output_metrics import (  # noqa: E402
    DEFAULT_CAP as ENTRY_CAP,
    iter_entries,
    non_negative_int,
    section_lines,
    split_lines,
)

# ── Constants ────────────────────────────────────────────────────────

# Conventional commit type → display section
TYPE_SECTIONS: Dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build & Dependencies",
    "ci": "CI/CD",
    "chore": "Chores",
    "style": "Style",
    "revert": "Reverts",
}

# Emoji prefixes matching existing CHANGELOG style
TYPE_EMOJI: Dict[str, str] = {
    "feat": "🏷️",
    "fix": "🐛",
    "perf": "📈",
    "refactor": "♻️",
    "docs": "📝",
    "test": "📊",
    "build": "📦",
    "ci": "🔧",
    "chore": "🔧",
}

# Conventional commit regex
COMMIT_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r":\s*(?P<desc>.+)$"
)


# ── Git helpers ──────────────────────────────────────────────────────

def git_cmd(args: List[str]) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode != 0:
        print(f"ERROR: git {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    return result.stdout.strip()


def get_latest_tag() -> Optional[str]:
    """Get the most recent tag."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def get_commits_since(since_ref: Optional[str]) -> List[Tuple[str, str]]:
    """Return list of (hash, subject) tuples since a ref."""
    if since_ref:
        log_range = f"{since_ref}..HEAD"
    else:
        log_range = "HEAD"
    raw = git_cmd(["log", log_range, "--pretty=format:%H|%s", "--no-merges"])
    if not raw:
        return []
    commits = []
    for line in raw.split("\n"):
        parts = line.split("|", 1)
        if len(parts) == 2:
            commits.append((parts[0][:12], parts[1]))
    return commits


def load_ignored_commits() -> List[str]:
    """Load the changelog-lint ignore list from .changelog-lint-ignore.

    The file is optional and lives at repo root. Each non-comment,
    non-blank line is a commit SHA prefix (typically 12 chars, matching
    the abbreviated hash produced by get_commits_since()). Lines
    starting with '#' are comments. Trailing whitespace is stripped.

    This exists because GitHub squash-merges can land commits on main
    whose subject doesn't follow conventional commits (e.g. when a PR
    title was never corrected). Rewriting main history is dangerous,
    and the offending commit would otherwise block every subsequent
    `--check` run until a new tag is cut. Listing the SHA here is the
    least-destructive escape hatch.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode != 0:
            return []
        root = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    ignore_path = str(Path(root) / ".changelog-lint-ignore")
    if not Path(ignore_path).exists():
        return []
    prefixes: List[str] = []
    try:
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Allow inline comments after whitespace
                token = line.split()[0]
                if token:
                    prefixes.append(token.lower())
    except OSError:
        return []
    return prefixes


# ── Parsing ──────────────────────────────────────────────────────────

def parse_commit(subject: str) -> Optional[Dict]:
    """Parse a conventional commit subject line."""
    m = COMMIT_RE.match(subject)
    if not m:
        return None
    return {
        "type": m.group("type"),
        "scope": m.group("scope") or "",
        "breaking": bool(m.group("breaking")),
        "desc": m.group("desc"),
    }


# ── Formatting ───────────────────────────────────────────────────────

def format_changelog(
    grouped: Dict[str, List[Dict]],
    version: str,
    breaking: List[Dict],
) -> str:
    """Format grouped commits into CHANGELOG markdown."""
    lines = []
    lines.append(f"## [{version}] — TITLE (DATE)")
    lines.append("")
    lines.append("ONE-LINE SUMMARY")
    lines.append("")

    # Breaking changes first
    if breaking:
        lines.append("### ⚠️ Breaking Changes")
        lines.append("")
        for c in breaking:
            scope = f"**{c['scope']}**: " if c["scope"] else ""
            lines.append(f"- {scope}{c['desc']}")
        lines.append("")

    # Grouped sections
    for commit_type, section_name in TYPE_SECTIONS.items():
        if commit_type not in grouped:
            continue
        emoji = TYPE_EMOJI.get(commit_type, "")
        header = f"### {emoji} {section_name}" if emoji else f"### {section_name}"
        lines.append(header)
        lines.append("")

        # Sub-group by scope
        by_scope: Dict[str, List[str]] = defaultdict(list)
        for c in grouped[commit_type]:
            by_scope[c["scope"]].append(c["desc"])

        if len(by_scope) == 1 and "" in by_scope:
            # No scopes, flat list
            for desc in by_scope[""]:
                lines.append(f"- {desc}")
        else:
            for scope, descs in sorted(by_scope.items()):
                if scope:
                    lines.append(f"- **{scope}**:")
                    for desc in descs:
                        lines.append(f"  - {desc}")
                else:
                    for desc in descs:
                        lines.append(f"- {desc}")
        lines.append("")

    return "\n".join(lines)


# ── CHANGELOG Lint ───────────────────────────────────────────────────

def lint_changelog(changelog_path: str = "CHANGELOG.md") -> List[str]:
    """Lint the existing CHANGELOG.md for format issues.

    Checks:
    - Each version header has semver format [vX.Y.Z...]
    - Date format is YYYY-MM-DD in parenthetical suffix (released sections only)
    - Each section has at least one ### subsection
    - No duplicate section entries
    Returns list of issue strings (empty = clean).

    ⛔ `## [Unreleased]` counts as a section (#1765). It did not, because the
    header pattern was semver-only and every check below was gated on having
    seen a version — so the one section every PR actually edits (here: the top
    ~1250 lines) was structurally invisible. Two measured holes: deleting all
    of its `###` subheadings, and a second `## [Unreleased]` heading, both
    linted clean.

    ⛔ Repeated subsection names within one section are NOT an issue. Each PR
    prepends its own block, so a second `### Fixed` under Unreleased is the
    convention working; `CHANGELOG.md`'s own header comment puts condensation
    at release time.
    """
    from pathlib import Path
    path = Path(changelog_path)
    if not path.exists():
        return [f"{changelog_path} not found"]

    content = path.read_text(encoding="utf-8")
    # ⛔ Not `splitlines()`: it also breaks on U+2028 / U+2029, so one source
    # line could forge headings (blind review, PR-C). Same splitter as the
    # section reader in agent_output_metrics.
    lines = split_lines(content)
    issues: List[str] = []
    seen_versions: Dict[str, int] = {}
    current_version: Optional[str] = None
    has_subsection = False
    has_content = False

    # ⛔ The `v` is REQUIRED on a released heading, because
    # `bump_docs._RELEASED_CHANGELOG_HEADING` requires it to decide which part
    # of the file is frozen history. `## [2.10.0]` linted clean here while
    # bump_docs treated it as live in-flight content and would rewrite the
    # version strings inside it. One predicate, two tools, same answer.
    # ⛔ The `v` stays outside the capture: issue text says `[2.9.0]`, and a
    # test pins that. Widening a pattern is also a chance to change what it
    # captures — those are two different edits.
    semver_re = re.compile(
        r"^\#\# \[(?:(Unreleased)|v(\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?))\]"
    )
    # ⛔ Anything shaped like a section heading but NOT recognised above. Every
    # check here is gated on having entered a section, so an unrecognised
    # heading does not fail — it makes its whole section invisible, silently.
    # `## [Unrelesed]` (typo) and `## [2.10.0]` (missing `v`, which
    # `bump_docs._RELEASED_CHANGELOG_HEADING` requires) both linted clean.
    # ⛔ `^##\s` not `^## \[`: `##  [Unreleased]` (two spaces) matched
    # neither pattern, so the section vanished from BOTH this lint and the
    # entry cap with no output at all (blind review, PR-C).
    heading_re = re.compile(r"^##\s")
    date_re = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
    subsection_re = re.compile(r"^### ")
    # Fence tracking: without it a fenced example of changelog markup is read
    # as real structure — in both directions (a fenced `## [Unreleased]` was
    # reported as a duplicate, a fenced `### Fixed` satisfied a section that
    # had none).
    fence_re = re.compile(r"^\s*(```|~~~)")

    def close_section():
        # An EMPTY `## [Unreleased]` is the shape release wrap-up leaves
        # behind after cutting the section into `## [vX.Y.Z]`. Demanding a
        # `###` there would make the gate red on the release commit itself.
        # ⛔ The exemption is Unreleased-only: a RELEASED section with nothing
        # under it is a defect in its own right, and an existing test pins it.
        if current_version is None or has_subsection:
            return
        if current_version == "Unreleased" and not has_content:
            return
        issues.append(
            f"L{seen_versions[current_version]}: [{current_version}] "
            f"has no ### subsections"
        )

    in_fence = False
    for i, line in enumerate(lines, 1):
        if fence_re.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        vm = semver_re.match(line)
        if vm:
            close_section()
            ver = vm.group(1) or vm.group(2)
            if ver in seen_versions:
                issues.append(
                    f"L{i}: duplicate version [{ver}] "
                    f"(first at L{seen_versions[ver]})"
                )
            seen_versions[ver] = i
            current_version = ver
            has_subsection = False
            has_content = False

            # Check date — released sections only. An Unreleased section has
            # no date by definition; demanding one would make the common case
            # permanently red, which is how a gate gets switched off.
            if ver != "Unreleased" and not date_re.search(line):
                issues.append(f"L{i}: [{ver}] missing date (YYYY-MM-DD)")
            continue

        if heading_re.match(line):
            issues.append(
                f"L{i}: {line.strip()[:60]} is not a recognised section "
                f"heading — expected [Unreleased] or [vX.Y.Z]; everything "
                f"under it is unchecked"
            )
            continue

        if current_version:
            if subsection_re.match(line):
                has_subsection = True
            if line.strip() and not line.lstrip().startswith("<!--"):
                has_content = True

    close_section()
    return issues


# ── Entry cap (new entries only) ────────────────────────────────────

CAP_SECTION = "Unreleased"


def _section_size(text: str, section: str) -> Optional[Tuple[int, int]]:
    """(characters, top-level entries) of ``## [section]``; None when absent.
    Characters are the heading line plus the whole block (text appended to
    the heading line itself counts too); entries are counted by
    ``agent_output_metrics.iter_entries``, so a `- ` inside a column-0 fence
    is code, not a new entry."""
    found = section_lines(text, section)
    if found is None:
        return None
    heading_no, block = found
    heading = split_lines(text)[heading_no - 1]
    return len("\n".join([heading] + block)), sum(1 for _ in iter_entries(block, heading_no + 1))


def lint_entry_caps(
    text: str,
    base_text: Optional[str],
    cap: int = ENTRY_CAP,
    section: str = CAP_SECTION,
    base_label: str = "the base",
) -> List[str]:
    """Cap how much ``## [section]`` GROWS over the base, per new entry.

    ``growth`` = the section's characters minus the base's; ``new`` = its
    entries minus the base's. The section may grow by ``cap`` per new entry,
    and by ``cap`` in total when it adds none (so a legacy entry may be
    corrected or extended a little, but not fattened). One finding, or none.

    This deliberately does NOT match entries to each other. Five review
    rounds of "which entry is new" found a hole in every version: each
    exemption (same headline, similar headline, same tail, structural line)
    was a splitter, and the matching machinery itself grew bugs. Growth of
    the section has no exemptions inside the section. Boundaries, stated
    rather than defended: deleting old text credits new text (the section
    did not grow); every extra top-level bullet buys ``cap`` more, even an
    empty one or one inside an HTML comment; text placed in a released
    section or above the heading is not this section. All of these are in
    plain sight in the diff, which is where a reviewer catches them.
    """
    head = _section_size(text, section)
    if head is None:
        return []
    head_chars, head_entries = head
    base = _section_size(base_text, section) if base_text is not None else None
    base_chars, base_entries = base if base else (0, 0)
    growth = head_chars - base_chars
    new = max(head_entries - base_entries, 0)
    allowed = cap * max(new, 1)
    if growth <= allowed:
        return []
    return [
        f"[{section}] grew by {growth} chars over {base_label} with {new} new "
        f"entr{'y' if new == 1 else 'ies'} (allowed {allowed} = {cap} per new entry, "
        f"or {cap} in total with none); keep the conclusions in the changelog and "
        f"move the measurements to the PR body or an issue comment"
    ]


def _ref_exists(ref: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def default_cap_base(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Which commit "new" is measured against.

    On a PR run GitHub sets ``GITHUB_BASE_REF`` (e.g. ``main``) and the merge
    ref's HEAD already CONTAINS the new entries, so HEAD would see nothing
    new; the answer is ``origin/<base>``. ⛔ If that ref is not fetched the
    answer is None, NOT HEAD: falling back to HEAD on a PR is a cap that
    silently does not exist, and a green run would look identical. The
    caller turns None into a caller error naming the fetch. Off a PR run
    it is HEAD, which is what the pre-commit hook wants: staged file vs
    last commit.
    """
    env = os.environ if env is None else env
    base_ref = env.get("GITHUB_BASE_REF", "").strip()
    if base_ref:
        candidate = f"origin/{base_ref}"
        return candidate if _ref_exists(candidate) else None
    return "HEAD"


def _git_show(ref: str, path: str) -> Optional[str]:
    """Contents of ``path`` at ``ref``; None when git cannot produce it
    (not a repo, ref unknown, file absent at that ref)."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30,
        )
        if top.returncode != 0:
            return None
        rel = Path(path).resolve().relative_to(Path(top.stdout.strip()).resolve())
        r = subprocess.run(
            ["git", "show", f"{ref}:{rel.as_posix()}"],
            capture_output=True, timeout=60,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point: Generate CHANGELOG draft entries from conventional commits."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Generate CHANGELOG draft from conventional commits"
    )
    parser.add_argument(
        "--since",
        help="Git ref (tag/commit) to start from (default: latest tag)",
    )
    parser.add_argument(
        "--version",
        default="UNRELEASED",
        help="Version label for the changelog section",
    )
    parser.add_argument(
        "-o", "--output",
        help="Write output to file instead of stdout",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: verify all commits follow conventional format",
    )
    parser.add_argument(
        "--lint",
        nargs="*",
        metavar="PATH",
        # ⛔ Takes paths so the pre-commit hook can pass the files it matched.
        # Hardcoding CHANGELOG.md here made the hook's `files:` pattern a lie:
        # it also matches CHANGELOG-archive.md, and editing that would have run
        # a check that never opened it (#1765).
        help="Lint changelog file format (semver/Unreleased headers, dates, "
             "subsections) and cap how much [Unreleased] grows over the base per "
             "new entry; defaults to CHANGELOG.md",
    )
    parser.add_argument(
        "--cap",
        type=non_negative_int,
        default=ENTRY_CAP,
        metavar="N",
        help=f"Max characters [Unreleased] may grow over the base per new entry in "
             f"--lint (default {ENTRY_CAP}; 0 disables); with no new entry the whole "
             f"section may grow by at most that.",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        help="Git ref that decides which entries are new (default: "
             "origin/$GITHUB_BASE_REF on a PR run, else HEAD)",
    )
    args = parser.parse_args()

    # Lint mode — validate existing changelog format. ⛔ `is not None`: bare
    # `--lint` yields an empty list, which is falsy.
    if args.lint is not None:
        # ⛔ De-duplicated: pre-commit can hand the same path twice, and the
        # issue count is what an operator acts on.
        targets = list(dict.fromkeys(args.lint or ["CHANGELOG.md"]))
        issues = []
        for target in targets:
            # ⛔ `_lib_exitcodes` splits these: a bad path or an unreadable
            # file is the CALLER's error (2), not a finding the user must act
            # on in the file (1). An uncaught traceback exits 1 too, which is
            # indistinguishable from "this changelog has one issue".
            p = Path(target)
            if not p.is_file():
                print(f"ERROR: not a readable file: {target}", file=sys.stderr)
                return EXIT_CALLER_ERROR
            try:
                found = lint_changelog(target)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"ERROR: could not read {target}: {exc}", file=sys.stderr)
                return EXIT_CALLER_ERROR
            issues += [f"{target}: {i}" for i in found]
            if args.cap == 0:
                print(f"notice: --cap 0, the new-entry cap is off for {target}")
            if args.cap > 0:
                base = args.base or default_cap_base()
                if base is None:
                    ref = os.environ.get("GITHUB_BASE_REF", "").strip()
                    msg = (f"ERROR: GITHUB_BASE_REF={ref} is set but origin/{ref} "
                           f"is not fetched, so the new-entry cap has no base; run "
                           f"`git fetch --no-tags origin {ref}` (checkout "
                           f"fetch-depth: 0 alone does not guarantee that ref)")
                    # Both streams on purpose: validate_all's runner shows
                    # stdout only, the hook shows both.
                    print(msg)
                    print(msg, file=sys.stderr)
                    return EXIT_CALLER_ERROR
                base_text = _git_show(base, target)
                if base_text is None:
                    # stdout on purpose: validate_all's runner shows stdout only.
                    # Skipped, not "everything is new": a first commit or a
                    # renamed file would be judged against zero and a real
                    # changelog of legacy-sized entries is always over.
                    print(f"notice: {target} not found at {base}; the "
                          f"[{CAP_SECTION}] growth cap has no base and is skipped")
                else:
                    try:
                        text = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        print(f"ERROR: could not read {target}: {exc}", file=sys.stderr)
                        return EXIT_CALLER_ERROR
                    issues += [f"{target}: {i}" for i in
                               lint_entry_caps(text, base_text, args.cap, base_label=base)]
        if issues:
            print(f"❌ {len(issues)} changelog format issue(s):")
            for issue in issues:
                print(f"  {issue}")
            return EXIT_VIOLATION
        print(f"✅ changelog format is clean ({', '.join(targets)})")
        return EXIT_OK

    # Determine starting point
    since_ref = args.since
    if not since_ref:
        since_ref = get_latest_tag()
        if since_ref:
            print(f"Using latest tag: {since_ref}", file=sys.stderr)
        else:
            print("No tags found, reading all commits", file=sys.stderr)

    # Get commits
    commits = get_commits_since(since_ref)
    if not commits:
        print("No commits found since reference point.", file=sys.stderr)
        return EXIT_OK

    print(f"Found {len(commits)} commits", file=sys.stderr)

    # Parse
    grouped: Dict[str, List[Dict]] = defaultdict(list)
    breaking: List[Dict] = []
    non_conventional: List[Tuple[str, str]] = []

    for sha, subject in commits:
        parsed = parse_commit(subject)
        if parsed:
            grouped[parsed["type"]].append(parsed)
            if parsed["breaking"]:
                breaking.append(parsed)
        else:
            non_conventional.append((sha, subject))

    # Check mode
    if args.check:
        # Filter out commits listed in .changelog-lint-ignore (if any).
        # This is the escape hatch for squash-merge artifacts on main
        # that can't be rewritten without dangerous history rewrite.
        ignored_prefixes = load_ignored_commits()
        ignored_found: List[Tuple[str, str]] = []
        real_failures: List[Tuple[str, str]] = []
        for sha, subject in non_conventional:
            sha_lower = sha.lower()
            if any(
                sha_lower.startswith(p) or p.startswith(sha_lower)
                for p in ignored_prefixes
            ):
                ignored_found.append((sha, subject))
            else:
                real_failures.append((sha, subject))
        if ignored_found:
            print(
                f"ℹ️  {len(ignored_found)} non-conventional commit(s) "
                f"skipped via .changelog-lint-ignore:",
                file=sys.stderr,
            )
            for sha, subject in ignored_found:
                print(f"  {sha} {subject}", file=sys.stderr)
        if real_failures:
            print(
                f"\n❌ {len(real_failures)} non-conventional commits:",
                file=sys.stderr,
            )
            for sha, subject in real_failures:
                print(f"  {sha} {subject}", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"✅ All {len(commits)} commits follow conventional format", file=sys.stderr)
        return EXIT_OK

    # Generate
    output = format_changelog(grouped, args.version, breaking)

    # Append non-conventional commits as uncategorized
    if non_conventional:
        output += "\n### Uncategorized\n\n"
        for sha, subject in non_conventional:
            output += f"- {sha} {subject}\n"
        output += "\n"

    # Stats
    stats = ", ".join(
        f"{TYPE_SECTIONS.get(t, t)}: {len(cs)}"
        for t, cs in sorted(grouped.items())
        if cs
    )
    output += f"<!-- Stats: {len(commits)} commits ({stats}) -->\n"

    if args.output:
        # #1641: an unwritable -o is rc=2 + one line, not a traceback at rc=1.
        write_text_or_die(args.output, output, flag="-o/--output")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
