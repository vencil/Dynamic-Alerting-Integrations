"""paths_filter_glob.py — the one matcher for `dorny/paths-filter` patterns.

Imported by `tests/ops/test_ci_path_filter_coverage.py` (every filter-coverage
assertion there) and by `scripts/ops/go_test_reads.py` (the Go legs' runtime
read check, #1399). It lives here, not in the test module, because the Go legs
run it without pytest installed; a second copy would be a second definition of
"covered" that could drift from the first.
"""
from __future__ import annotations

import re

SUFFIX_SEGMENT = re.compile(r"\*\.\w+$")


def covers(pattern: str, path: str) -> bool:
    """Does a paths-filter pattern cover `path` (a file OR a directory)?

    Segment matcher over the CLOSED alphabet the repo's filters actually use:
    a literal, `**` (any run of segments, including none), and `*.ext`. That
    set was not guessed — every pattern in every `dorny/paths-filter` block in
    `.github/workflows/` was enumerated, and nothing else occurs. Within that
    alphabet this is complete, not a half-built glob engine.

    Anything OUTSIDE the alphabet (a bare `*`, `a*b`, `?`) returns False, i.e.
    reports the path as uncovered. That is the loud direction: a new glob shape
    surfaces as a filter-coverage failure a human must look at, rather than
    being silently mis-matched.

    ⛔ "False is loud" holds only for a POSITIVE pattern, because callers ask
    `any(covers(p, path) for p in patterns)` — one False narrows nothing. A
    NEGATED pattern (`!x`) therefore fails the opposite way: it would be read as
    the literal segment `"!x"`, return False, and be absorbed while the positive
    patterns still say "covered", making an excluded path look included. That
    is why negation is rejected by every caller that loads filters rather than
    handled here; do not "fix" it by teaching this function about `!`, which
    would still leave the OR unable to express an override.

    ⛔ This is the single disarm surface for every filter-coverage assertion —
    all of them are negative ("nothing uncovered"), so an over-permissive
    `covers` turns them green and quiet.
    `test_covers_matches_only_the_shapes_in_use` pins both directions; keep it
    that way.
    """
    return match_segments(pattern.split("/"), path.split("/"))


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
    if SUFFIX_SEGMENT.fullmatch(head):
        suffix = head[len("*"):]
        if not (parts[0].endswith(suffix) and parts[0] != suffix):
            return False
    elif "*" in head or "?" in head:
        return False  # outside the modelled alphabet — fail loud
    elif parts[0] != head:
        return False
    return match_segments(rest, parts[1:])
