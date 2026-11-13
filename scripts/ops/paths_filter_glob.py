"""paths_filter_glob.py — the one matcher for workflow path-filter patterns
(`dorny/paths-filter` filters and GitHub's own `on.<event>.paths`).

Imported by `tests/ops/test_ci_path_filter_coverage.py` (every filter-coverage
assertion there), by `scripts/ops/go_test_reads.py` (the Go legs' runtime read
check, #1399) and by `scripts/ops/workflow_path_filter.py` (the generated
`on.pull_request.paths` block, its runtime check and the dead-entry check,
#3756). It lives here, not in the test module, because the workflow jobs run it
without pytest installed; a second copy would be a second definition of
"covered" that could drift from the first.
"""
from __future__ import annotations

import re

SUFFIX_SEGMENT = re.compile(r"\*\.\w+$")
# One `[...]` character class: literal characters and `a-z` ranges only. A
# negated class (`[!x]`, `[^x]`) is spelled differently by the two matchers
# this module stands in for, so it is not modelled.
_CLASS = re.compile(r"\[([^\]\[!^/\\]+)\]")
# Characters with a glob meaning in GitHub's `on.paths` or in picomatch that a
# modelled segment may not contain outside the three shapes above.
_SEGMENT_REFUSED = re.compile(r"[?+{}()\\\[\]]")


class Unmodelled(AssertionError):
    """A pattern this module cannot grade. An AssertionError so the guards
    that already refuse an unmodelled shape with one keep a single type."""


def unmodelled(pattern: str) -> str | None:
    """Why `pattern` is outside the modelled alphabet, or None when it is in.

    The alphabet, per `/`-separated segment: `**` as a WHOLE segment; a literal;
    and a segment built from literals, `*` (any run of characters inside the
    segment) and `[...]` classes of literal characters and ranges. Those are
    the shapes both GitHub's `on.<event>.paths` matcher and dorny's picomatch
    read the same way, and every pattern in `.github/workflows/` is one of
    them (`scripts/ops/workflow_path_filter.py generate --check` holds that).

    ⛔ This is what tells "matches nothing" from "cannot say": a dead-entry
    check that asked `covers` alone would report an unmodelled pattern as dead,
    and the fix it suggests — delete the entry — removes a working trigger.
    """
    if not isinstance(pattern, str) or not pattern:
        return "not a non-empty string"
    if pattern.startswith("!"):
        return "a `!` exclusion (an OR over positive patterns cannot express it)"
    if pattern.startswith("./") or pattern.startswith("/") or pattern.endswith("/"):
        return "a `./` or `/` prefix or a trailing `/` has no documented meaning"
    for segment in pattern.split("/"):
        if segment == "**":
            continue
        if not segment:
            return "an empty segment (`//`)"
        if "**" in segment:
            return f"`**` inside the segment {segment!r} (it must be a whole segment)"
        rest = _CLASS.sub("", segment)
        hit = _SEGMENT_REFUSED.search(rest)
        if hit:
            return (f"{hit.group()!r} in the segment {segment!r} (only literals, "
                    "`*` and `[...]` classes of literals and ranges are modelled)")
        try:
            _segment_regex(segment)
        except re.error as exc:
            # `[z-a]`: a class the two matchers do not agree on either
            # (picomatch reads a reversed range as literals).
            return f"a character class in the segment {segment!r} is not valid ({exc})"
    return None


def covers(pattern: str, path: str) -> bool:
    """Does a paths-filter pattern cover `path` (a file OR a directory)?

    Segment matcher over the alphabet `unmodelled` documents. Until #3756 it
    was narrower (a literal, `**`, `*.ext`), and four patterns the workflows
    really use — `_grar_*.py`, `values*.yaml` and guard-defaults-impact's two
    `_*.[Yy][Aa][Mm][Ll]` — read as covering nothing.

    Anything OUTSIDE the alphabet (`?`, `+`, braces, a negated class, `**`
    glued to other characters) returns False, i.e. reports the path as
    uncovered. That is the loud direction: a new glob shape surfaces as a
    filter-coverage failure a human must look at, rather than being silently
    mis-matched. A caller that must tell that case from "matches nothing" asks
    `unmodelled` first.

    ⛔ "False is loud" holds only for a POSITIVE pattern, because callers ask
    `any(covers(p, path) for p in patterns)` — one False narrows nothing. A
    NEGATED pattern (`!x`) therefore fails the opposite way: it would be read as
    the literal segment `"!x"`, return False, and be absorbed while the positive
    patterns still say "covered", making an excluded path look included. That
    is why negation is rejected by every caller that loads dorny filters rather
    than handled here, and why `on_paths_covers` (GitHub's ordered rule) matches
    a negated pattern with `negation_may_match` instead of this function.

    ⛔ This is the single disarm surface for every filter-coverage assertion —
    all of them are negative ("nothing uncovered"), so an over-permissive
    `covers` turns them green and quiet.
    `test_covers_matches_only_the_shapes_in_use` pins both directions; keep it
    that way.
    """
    if unmodelled(pattern) is not None:
        return False
    return match_segments(pattern.split("/"), path.split("/"))


def _segment_regex(segment: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(segment):
        klass = _CLASS.match(segment, i)
        if klass:
            out.append("[" + re.sub(r"([\\\]^])", r"\\\1", klass.group(1)) + "]")
            i = klass.end()
        elif segment[i] == "*":
            # Any run of characters inside the segment, the empty one included:
            # `*.md` covers the dotfile `.md`, `*foo` covers `foo`. Measured
            # against picomatch with `dot: true` (what dorny/paths-filter
            # passes), and held against it for every pattern in the workflows
            # by tests/ops/test_workflow_path_filter.py. ⚠️ Before #3756 the
            # `*.ext` branch refused the bare dotfile; that was this module's
            # own rule, not picomatch's.
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(segment[i]))
            i += 1
    return re.compile("".join(out))


def match_segments(pat: list[str], parts: list[str]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        # `**` absorbs any number of segments, including zero — so
        # `docs/**` covers `docs` itself and `a/**/*.md` covers `a/x.md`.
        return any(match_segments(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    if "*" in head or "[" in head:
        if _SEGMENT_REFUSED.search(_CLASS.sub("", head)) or "**" in head:
            return False  # outside the modelled alphabet — fail loud
        try:
            if not _segment_regex(head).fullmatch(parts[0]):
                return False
        except re.error:
            return False  # `[z-a]`: `unmodelled` names it; never a traceback
    elif "?" in head:
        return False  # outside the modelled alphabet — fail loud
    elif parts[0] != head:
        return False
    return match_segments(rest, parts[1:])


# ── GitHub's `on.<event>.paths`: ordered, with `!` exclusions ────────────────
#
# Moved here from tests/ops/test_ci_path_filter_coverage.py (#3756): invariant 4
# there and the runtime check in scripts/ops/workflow_path_filter.py both ask
# "does this workflow start for this file", and two copies of the rule would be
# two answers.

# Every character GitHub's filter cheat sheet gives a glob meaning to that can
# sit inside one segment. `+` ("one or more of the preceding character") is
# easy to forget — round 2 of #3601's review caught it missing.
_GLOB_CHARS = re.compile(r"[*?\[+]")
# Shapes refused outright in a `!` pattern rather than approximated: a brace or
# extglob group can contain `/`, so splitting on `/` would cut it apart, and a
# `./` prefix or trailing `/` has no documented meaning in `on.paths`.
_NEGATION_REFUSED = re.compile(r"[{}()]|^\./|/$")


def on_paths_covers(patterns: list[str], path: str) -> bool:
    """GitHub's `on.<event>.paths` rule: in order, the last match decides.

    "A matching negative pattern (prefixed with `!`) after a positive match
    will exclude the path. A matching positive pattern after a negative match
    will include the path again." — GitHub workflow syntax docs.

    ⛔ The two kinds are matched in OPPOSITE approximations, each failing loud:
    a positive pattern through `covers` (an unmodelled glob answers "no match"
    → uncovered), a negated one through `negation_may_match` (an unmodelled
    glob answers "match" → excluded → uncovered).
    """
    covered = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if negation_may_match(pattern[1:], path):
                covered = False
        elif covers(pattern, path):
            covered = True
    return covered


def negation_may_match(pattern: str, path: str) -> bool:
    """An OVER-approximation of GitHub's match, for `!` patterns only.

    A segment containing `**` anywhere (`**`, `**.py`, `ops**`) absorbs ANY
    run of segments — GitHub's `**` matches any characters, `/` included. Any
    other segment holding a glob character matches any single segment. A
    literal segment must be equal. Every path GitHub's matcher accepts is
    therefore accepted here, so an exclusion is never under-applied; the cost
    is a possible false "excluded", the loud direction for a negative pattern.
    """
    if _NEGATION_REFUSED.search(pattern):
        raise Unmodelled(
            f"`!{pattern}`: braces, extglob groups, a `./` prefix and a "
            "trailing `/` are not modelled in a negated `on.paths` pattern — "
            "an exclusion this guard cannot bound might hide an uncovered "
            "script. Rewrite it as plain segments.")

    def walk(pat: list[str], parts: list[str]) -> bool:
        if not pat:
            return not parts
        head, rest = pat[0], pat[1:]
        if "**" in head:
            return any(walk(rest, parts[i:]) for i in range(len(parts) + 1))
        if not parts:
            return False
        if not _GLOB_CHARS.search(head) and parts[0] != head:
            return False
        return walk(rest, parts[1:])
    return walk(pattern.split("/"), path.split("/"))
