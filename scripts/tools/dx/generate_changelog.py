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
import difflib
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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
    lines = content.splitlines()
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

# Column-0 lines that are structure inside a `## [Unreleased]` block and may
# legitimately sit between entries. Everything else in the block must belong
# to an entry: `iter_entries` drops any other line (out-dented text, a bullet
# indented before any entry is open, text after a splitter) and a dropped
# line is exactly how length hides from the cap. New lines of that shape are
# errors; the legacy ones already in CHANGELOG.md are keyed against the base.
_STRUCTURAL_PREFIXES = ("### ", "<!--")

# Above this many over-cap groups the base is probably not the commit this
# branch grew from (release wrap-up moved [Unreleased], unrelated ref...).
# Listing 400 findings hides that one fact, so say it and show the first few.
_BASE_MISMATCH_THRESHOLD = 25

# A bullet line this similar to a base bullet line IS that entry (a typo fix
# in a legacy headline). A near-copy of a legacy headline above new prose is
# charged as that entry's excess, which is what makes copying pointless.
_NEAR_KEY_RATIO = 0.85

# Similarity is O(unmatched head entries x base entries). With the length
# bound below it is ~0.02 s per unmatched headline against this file's base;
# the worst case (every legacy headline rewritten at once) is a one-off cost,
# not a reason for a budget: a budget that stops probing turns a near-copy
# into its own group, and per-group charging is not additive, so it is
# LENIENT, not strict (blind review, round 5).

# A `### ` or `<!--` line longer than this is prose wearing a structural
# prefix, not structure; it is charged like any other new line.
_STRUCTURAL_LINE_MAX = 200


def _entry_key(body: str) -> str:
    """The bullet line identifies an entry across edits below it."""
    return body.splitlines()[0]


def _entry_tail(body: str) -> str:
    """Everything after the bullet line; identifies a multi-line legacy entry
    whose bullet line was rewritten beyond recognition."""
    return "\n".join(body.splitlines()[1:]).strip()


def _match_key(key: str, base_keys: Sequence[str]) -> Optional[str]:
    """The base bullet line this one is (exact, else the most similar one at
    or above ``_NEAR_KEY_RATIO``); None when it is a new headline."""
    if key in base_keys:
        return key
    best, best_ratio = None, 0.0
    for candidate in base_keys:
        shorter, longer = sorted((len(key), len(candidate)))
        # difflib's own upper bound (its real_quick_ratio) is 2s/(s+l), NOT
        # s/l: the latter pruned true matches with length ratio in
        # [0.739, 0.85) — every legacy headline trimmed by 16-28% read as a
        # brand-new entry (blind review, round 5). Skip only what the bound
        # rules out, before paying for the matcher.
        if longer == 0 or 2 * shorter / (shorter + longer) < _NEAR_KEY_RATIO:
            continue
        sm = difflib.SequenceMatcher(None, key, candidate)
        if sm.real_quick_ratio() < _NEAR_KEY_RATIO or sm.quick_ratio() < _NEAR_KEY_RATIO:
            continue
        ratio = sm.ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best if best_ratio >= _NEAR_KEY_RATIO else None


def lint_entry_caps(
    text: str,
    base_text: Optional[str],
    cap: int = ENTRY_CAP,
    section: str = CAP_SECTION,
    base_label: str = "the base",
) -> List[str]:
    """Cap what ``## [section]`` ADDS, per entry, at ``cap`` characters.

    Every entry is matched to the base entries it is a version of — the ones
    with the same (or a near-identical) bullet line, else the ones with the
    same tail (the lines below the bullet), else none — and entries whose
    matches overlap form one group (union-find over base entries, so a base
    entry funds exactly one group: keeping a legacy entry AND pasting its
    tail under a new headline lands both in the same group). A group is
    charged ``sum(len of its entries) - sum(len of the base entries it
    matched)``: a typo fix in a legacy headline or a new sub-bullet under it
    costs its delta; a brand-new entry costs its full length; a copy of a
    base headline or tail costs the copy's own length wherever it sits.
    Within one group a deleted base entry credits the others (the group did
    not grow) — that is the same "editing history" boundary as below.
    Length is the raw entry text, sub-bullets and evidence included: when
    this file was measured only one over-cap entry owed it to a table
    (#1737). ⛔ Rewriting a legacy entry's body UNDER its own headline is an
    edit of history, not new length, by design — a reviewer sees that diff.
    Splitting new content across several entries each under the cap is the
    per-entry cap's nature, not a hole it claims to close.

    Every non-blank line in the section must be part of an entry, or a
    ``### `` / ``<!--`` line of at most ``_STRUCTURAL_LINE_MAX`` chars, or a
    line the base already had. Anything else (out-dented text, a bullet
    indented before any entry is open, text after a heading/comment/column-0
    fence that closed the entry above) is what ``iter_entries`` cannot see,
    so it is an error.
    """
    found = section_lines(text, section)
    if found is None:
        return []
    heading_no, block = found
    base_found = section_lines(base_text, section) if base_text is not None else None
    base_block = base_found[1] if base_found else []
    base_entries = [b for _, b in iter_entries(base_block, 1)]
    by_key: Dict[str, List[int]] = defaultdict(list)
    by_tail: Dict[str, List[int]] = defaultdict(list)
    for i, b in enumerate(base_entries):
        by_key[_entry_key(b)].append(i)
        tail = _entry_tail(b)
        if tail:
            by_tail[tail].append(i)
    base_keys = list(by_key)
    base_lines = set(base_block)

    # union-find over base entry indices: a base entry funds ONE group
    parent = list(range(len(base_entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(ids: List[int]) -> int:
        root = find(ids[0])
        for j in ids[1:]:
            parent[find(j)] = root
        return root

    head: List[Tuple[int, str, Optional[List[int]]]] = []   # (line, body, matched base ids)
    consumed = set()
    for no, body in iter_entries(block, heading_no + 1):
        start = no - (heading_no + 1)
        consumed.update(range(start, start + body.count("\n") + 1))
        key = _entry_key(body)
        if key in by_key:
            matched = by_key[key]
        else:
            near = _match_key(key, base_keys)
            if near is not None:
                matched = by_key[near]
            else:
                tail = _entry_tail(body)
                matched = by_tail.get(tail) if tail else None
        if matched:
            union(matched)
        head.append((no, body, matched))

    # group id -> (line numbers, head total, base ids)
    groups: Dict[str, Tuple[List[int], int, List[int]]] = {}
    for no, body, matched in head:
        if matched:
            root = find(matched[0])
            gid = f"base:{root}"
            base_ids = [i for i in range(len(base_entries)) if find(i) == root]
        else:
            gid, base_ids = f"new:{no}", []
        nos, total, _ = groups.get(gid, ([], 0, base_ids))
        groups[gid] = (nos + [no], total + len(body), base_ids)

    over: List[str] = []
    for nos, total, base_ids in groups.values():
        had = sum(len(base_entries[i]) for i in base_ids)
        added = total - had
        if added <= cap:
            continue
        where = ", ".join(f"L{n}" for n in nos)
        tail_msg = ("; keep the conclusion here and move the measurements to the "
                    "PR body or an issue comment")
        if not base_ids:
            over.append(f"{where}: new entry adds {added} chars over {base_label} (> {cap}){tail_msg}")
            continue
        headline = _entry_key(base_entries[base_ids[0]])[:40]
        if len(nos) == 1:
            over.append(f"{where}: entry grew by {added} chars over its version in {base_label} "
                        f"«{headline}…» (> {cap}){tail_msg}")
        else:
            over.append(f"{where}: entries matched to the entry «{headline}…» in {base_label} "
                        f"add {added} chars in total (> {cap}){tail_msg}")
    if len(over) > _BASE_MISMATCH_THRESHOLD:
        first = "; ".join(o.split(":")[0] for o in over[:3])
        over = [
            f"{len(over)} entries add more than {cap} chars over {base_label} "
            f"(first: {first}). Either {base_label} is not the commit this branch "
            f"grew from (release wrap-up, unrelated ref: pass --base <that commit>) "
            f"or the section really grew that much"
        ]
    issues = over
    for offset, line in enumerate(block):
        if offset in consumed or not line.strip() or line in base_lines:
            continue
        if line.startswith(_STRUCTURAL_PREFIXES) and len(line) <= _STRUCTURAL_LINE_MAX:
            continue
        issues.append(
            f"L{heading_no + 1 + offset}: line belongs to no entry in [{section}] "
            f"(out-dented, indented before any bullet, split off by a "
            f"heading/comment/column-0 fence, or a heading/comment longer than "
            f"{_STRUCTURAL_LINE_MAX} chars), so no cap can see it; make it "
            f"continuation of the `- ` bullet above (two-space indent under an "
            f"open bullet) or its own `- ` entry"
        )
    return issues


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
             "subsections) and cap what each [Unreleased] entry ADDS over the base; "
             "defaults to CHANGELOG.md",
    )
    parser.add_argument(
        "--cap",
        type=non_negative_int,
        default=ENTRY_CAP,
        metavar="N",
        help=f"Max characters an [Unreleased] entry (or the entries sharing one base "
             f"entry) may ADD over the base in --lint (default {ENTRY_CAP}; 0 disables). "
             f"A legacy entry is charged only its growth; a new entry its full length.",
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
                    print(f"notice: {target} not found at {base}; every "
                          f"[{CAP_SECTION}] entry counts as new for the cap")
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
