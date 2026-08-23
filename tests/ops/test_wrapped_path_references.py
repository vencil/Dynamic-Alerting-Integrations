"""A file reference must not be split across a line break (#1378).

A path written in a comment can wrap mid-token:

    # ... see docs/integration/example-long-guide-
    # name.md §3.1

`git grep docs/integration/example-long-guide-name.md` then returns NOTHING for
that file. The reference exists, is correct, and is invisible.

(That illustration uses a path that does not exist — with a real one the guard
flags its own docstring, which is how the first run of it went.)

⛔ Why that is worse than it sounds: the answer you get is "clean", not
"unknown". #1373 renamed a guard module and swept for references with exactly
that grep; one reference had wrapped mid-identifier, the sweep reported the tree
clean, and a pointer to a now-deleted file shipped in the PR. It was caught by
an external reviewer, not by the sweep. The fix landed as a prose ⛔ note next to
the one line involved — which protects one filename in one place, so this guard
replaces it with the derivable rule.

THE RULE: rejoin comment continuations; every path-like token that then names a
real repo file must ALSO appear contiguously in the raw text.

THE SECOND RULE (#1453), same window and same rejoin: every identifier-shaped
token that then names something this repo DEFINES must also appear contiguously.
The accident is the same one — #1373 was a rename sweep, and a sweep greps for a
symbol as readily as for a path — but a symbol carries no slash and no
extension, so it needs its own resolver: an `ast` inventory of names defined
exactly once across the tracked Python tree, filtered by the bare-name bounds
below transposed onto symbols. What that inventory is, what its bounds are
actually worth, and what it cannot see are all on `_identifier_inventory`.

⛔ It is a SEPARATE test with a SEPARATE message, and that is not tidiness. A
path-like token that resolves IS a reference; an identifier-shaped one can be
prose that spelled a symbol by accident, so the path side's "a reflow is the
only fix" is false here and following it would damage the file. The two halves
also fail for different reasons and deserve to go red separately.

⛔ The metric is defined HERE, not in a ticket. Four different counts of "how
many wrapped references exist" were produced while scoping this (16 / 28 / 35 /
44) — every one honest, every one measuring something slightly different
(full-path only, plus bare names, plus more extensions, minus a regex bug).
A number that needs its definition restated to mean anything is not a baseline.
Since the tree is now at zero, the definition below IS the number.

⛔ KNOWN GAP, hit while writing this: a path split across two STRING LITERALS is
invisible here, because the quotes sit between the halves and break the token —

    "# ... （components/threshold-exporter/",
    "# app/pkg/config/resolve.go）。",

That is `scripts/tools/ops/_registry_lib.py`, which GENERATES the comment blocks
spliced into `rule-packs/*.yaml`. This guard saw the generated outputs — six of
them at the time, nine today — and not their source, so fixing only the outputs
was silently reverted by
`check_threshold_registry.py --regen` — the repo's own staleness gate is what
caught it. If you are unwrapping a path and the gate keeps coming back, look
for a generator.

⛔ That class was swept to zero by hand once, is held there by NOTHING, and is
NO LONGER AT ZERO. The sweep: for each adjacent pair of lines where the first
ends in a quote and the second starts with one, splice them — ⚠️ stripping the
second literal's own comment marker, without which the token stays broken and
the sweep reports a clean tree it never actually looked at — and ask the same
question this guard asks. It found 7 in #1383; all 7 were fixed. Re-run for
#1452 — same answer on the merge base and on this branch, so it is a property
of the tree and not of one commit — **2**, one of them live —

  * `tests/ops/test_generated_ci_artifacts.py:3591` splits
    `tools/portal/…/cicd-setup-wizard/utils/generators.js`. `git grep` on the
    whole path returns that file (line 311) and NOT line 3591, so a rename
    sweep working from `grep -n` fixes one and leaves the other. #1373 verbatim.
  * `:35` of this file — the illustration above uses a REAL tracked path and
    escapes its own guard only because this class is unmodelled. Contrast the
    top-of-file illustration, which uses a path that does not exist for exactly
    this reason.

Neither is fixed here: the mechanism is the TOKENISER (quotes break the token
before the resolver is reached), not the resolver that #1452 is about, and it
already has a ticket — #1394. What is fixed here is this paragraph, which
asserted zero while two counter-examples sat in the tree. That is the same
sentence-shape #1452 exists to punish, fifteen lines above the one it punished.

What is deliberately NOT modelled (under-detection, the safe direction):
  * a token split across two string literals — see the gap above;
  * a token split across THREE or more lines — the window is two lines.
    Measured: a three-line window adds 0 reports on this tree, so the gap is
    structural and currently empty, not a backlog;
  * a reference assembled from variables, or reached through a glob;
  * a comment marker outside the five `LINE_PREFIX` knows (`>` blockquote,
    `::`/`REM`, `%`, `!`) — blind review demonstrated every one of them.
    Measured the same way: teaching `LINE_PREFIX` all five adds 0 reports;
  * a PREFIXED spelling — relative (`./x`, `../x`), anchored (`/x`, `~/x`) or
    partial (`pkg/config/x`) — whose basename does not clear the bare-name
    bounds three lines down. Since #1452 the basename is asked when the whole
    token resolves to nothing, so `../check_pint.py` is caught; `./__init__.py`
    (under `_MIN_BARE_NAME`), `./.claudeignore` (no separator) and
    `./_defaults.yaml` (16 tracked paths, so not unique) are not. ⚠️ Measured,
    because the first wording of this bullet named only non-uniqueness and used
    `./utils.py` as its example — and `utils.py` names no file in this repo at
    all, so it was excluded for a different reason than the one given: of the
    1970 repo-unique basenames, 132 are excluded by the separator bound alone
    and 22 by the length bound alone;
  * one of OUR paths cited with EXTRA leading segments — the
    `https://github.com/vencil/…/blob/main/<our path>` form, and `../blob/main/…`
    beside it. The basename fallback requires a SUFFIX-compatible spelling, and
    a URL is longer than the path it ends with, so it stays silent: 120 distinct
    such tokens on this branch (118 on the merge base), none of them wrapped on
    either. ⛔ Accepting the other direction (token longer, ending in our whole
    path) is not a free fix — it re-admits `/src/.pre-commit-config.yaml`, the
    CUSTOMER's copy at pre-commit's docker mount, for every single-segment path
    of ours;
  * the MIRROR of that, and it is over-detection rather than under: somebody
    else's file written as a spelling that happens to be suffix-compatible with
    one of ours — `ops/protect_main_push.sh` cited as upstream's, while ours is
    `scripts/ops/protect_main_push.sh`. Structure cannot separate that from our
    own partial spellings, because it is exactly what our own references look
    like. It is named here rather than fixed because the remedy the guard asks
    for does not damage the file when it is wrong — ⚠️ though it is not free
    either: reflowing a citation of somebody else's file buys this repo
    nothing;
  * a bare filename that is not repo-unique, shorter than `_MIN_BARE_NAME`, or
    containing neither `_` nor `-` — that last condition alone excludes 132
    otherwise-unique names, and all three are here to keep generic ones out of
    the scan: `README.md` names 37 tracked files and `values.yaml` 11, while
    `index.md` and `resolve.go` are unique but fall under the floor. ⚠️ Pick
    those examples from the tree: two earlier wordings of this bullet named
    `utils.py` and then `config.py`, and NEITHER names a file in this repo, so
    both were illustrating a bound with something the bound never touched;
  * binary content is read too, decoded with replacement — nothing is
    skipped, because silently dropping input is how a scan reports "clean"
    while never having looked (and pinning WHICH files get dropped turned out
    to be environment-dependent; see the note by `_read_tracked`).

What the IDENTIFIER half additionally does not model:
  * every definition side that is not Python — a Go `func`, a shell function, a
    Make target, a YAML anchor, a JS export. The ticket proposed reaching them
    by also accepting any name that occurs contiguously somewhere in the tree;
    measured, that admits 25 further reports and every one is prose glue
    (`the_defaults`, `when_defaults`, `d_custom_alerts` — an English word
    running into the next line's identifier). Under that evidence a legitimate
    sentence and a broken reference are the same object, so this is disclosed
    rather than closed. It is the largest gap here, and the shape of it is worth
    stating precisely: 4 of the 20 references the scan first reported sit in
    non-Python FILES, but all 20 name Python DEFINITIONS, and nothing measures
    how many non-Python definitions are wrapped, because there is no way to ask;
  * a genuinely dangling name — one whose spelling exists nowhere in the tree
    any more — is undetectable in principle, because the resolver has nothing
    left to resolve against. Same shape as the path half's equivalent;
  * ⛔ AND ONE FREE DISARM THE PATH HALF DOES NOT HAVE. Uniqueness is a bound,
    so defining the same name a second time ANYWHERE silences a real report
    permanently — one `def test_defaults(): ...` in any test file does it, and
    that line looks entirely ordinary in review. The path half needs a second
    tracked file with the same basename to achieve the same thing, which is
    conspicuous; this needs one line. Named rather than fixed, because dropping
    uniqueness is what re-admits the prose-glue family above.

⛔ GOING GREEN WITHOUT FIXING ANYTHING is easy, and the class is OPEN: anything
that stops the two halves being joined does it. Measured examples, on a real
defect, are a trailing space on the first line, an inserted blank or
comment-only line, a `.` after the break, swapping the second line's marker for
one this guard does not model, wrapping either half in backticks or quotes, and
renaming or deleting the file the reference points AT — that last one silences
a real defect with a single character, and it is here rather than in the
message for the same reason as the rest. That is a sample, not a list — do not
read it as a boundary.

The failure message forbids all of it and deliberately does NOT describe it,
because a hurried contributor reading a red test needs a fix, and any
description of the cheap wrong ones is a set of instructions. ⚠️ The quoting
example is the one to worry about: putting a path in backticks is ordinary
Markdown hygiene, and it is the same mechanism as the string-literal gap at the
top of this file — somebody tidying prose can disarm this guard believing they
were formatting. That is also why the false-positive shapes below matter more
than they look: every false report teaches somebody one of these edits.

⚠️ FALSE POSITIVES THIS GUARD IS KNOWN TO PRODUCE. The join is mechanical, so
anything that leaves a directory-ish string at the end of one line and a
filename-ish string at the start of the next reads as a wrapped reference:

  * a directory-tree listing — `dir/` and then an indented member. Connector
    characters (`├──`, `|--`, `- `) happen to silence it, by accident of the
    token character class rather than by design;
  * any list whose consecutive items are a directory and a file, under ANY of
    the five continuation markers — measured, all five produce it, and `#` is
    far commoner in this tree than the Markdown `*` case that first showed it.
    ⛔ Dropping a marker is NOT the fix: `*` is load-bearing for the
    block-comment case `test_detector_finds_a_synthetic_wrap` pins;
  * a sentence whose last word runs into a filename on the next line. The join
    invents a name nobody wrote, and reports it the day somebody adds a file
    that happens to have that name — from a file they never touched.

⚠️ THE IDENTIFIER HALF PRODUCES MORE OF THEM, because a symbol name is built out
of ordinary English words and so can be spelled by accident in a way a path
cannot. The same shapes recur — prose running on, a directory listing, a YAML
key under a scalar, an indented two-column table, a `*` bullet list — and the
exposure is measurable rather than hypothetical: of the 11977 names in the
inventory, 156 have a split whose first half already ends some line in this tree
while its second half already starts some line, so an edit that puts two such
lines next to each other reports a symbol nobody wrote. That is 1.3%, and the
tree reports none of them today, so all 156 are latent.

⛔ That exposure is why the identifier message offers no remedy except reflow and
otherwise sends people at the guard. Every OTHER way to make one of those
reports quiet costs something real — a listing loses its shape, a generated file
gets its keys reordered, a bullet marker becomes one the false-positive note
above says must not be dropped — and a message that named them would be handing
out those trades to whoever is in a hurry. They are described here, where
somebody fixing the guard reads them, and not there.

⛔ None of these is fixable by REFLOWING, because there is no reference to
reflow. Each does have a harmless rewrite, measured rather than guessed: a
connector character in the tree listing (`├──`, `|--`, `- `), a different
bullet, or moving one word in the sentence — and the failure message already
says prose may wrap wherever it likes. ⚠️ Do not read those as fixes: the
guard still cannot see that shape afterwards, so the report was noise and the
silence that follows is also noise. What this module does NOT have is a
per-line exemption, so a false report cannot be annotated away — it is
rewritten harmlessly, or the guard is fixed. The tree is at zero today, so
all three are latent, not live.

⚠️ A fixture that must genuinely contain a wrapped path should cite a path that
does not exist — the `docs/integration/…` illustration at the very top of this
module, which is deliberately not a tracked file. ⛔ That belongs HERE and not
in the failure message: applied to a REAL defect the same edit is a
one-character disarm, and it is worse than the ones above because it also
leaves the pointer aimed at nothing. (The other illustration — the one in the
KNOWN GAP paragraph above — uses a REAL tracked path on purpose; do not copy
that one. Naming it by paragraph rather than by line distance is deliberate:
an earlier wording said "fifteen lines lower", which was copied from a nearby
sentence and was wrong by half.)

⚠️ SELECTION, not coverage: this guard reads the whole tree, but
`verify_diff.py` maps it through `text_map`, i.e. only the handful of paths it
happens to NAME. Wrap a path somewhere else and a local `verify_diff --base
origin/main` will not pick this test. That is a dev-loop latency cost, not a
hole in the same sense: the pre-commit hook only runs `verify_diff --check`
(map freshness), never selection, and CI's `python-tests-run` runs `pytest
tests/` in full. The rules schema cannot express "any tracked file" —
`always_run` takes a single `trigger` (`verify_diff.py` validates it) — so
widening selection would mean changing that schema. Raised by CodeRabbit
on #1383.

⛔ FIXED, and worth keeping the shape written down. `python-tests-run` used to
be genuinely path-gated while 101 tracked files — counted then, against the
pre-`**` filter — fell outside ci.yml's `python` filter, so a PR touching only
such a file did not run this guard at all.
⚠️ The required check `Python Tests (3.13)` is reported by the always-run
aggregate, not by the skipped leg — so it reported SUCCESS, never `skipped`.
The defect was never "a skipped required check satisfies branch protection";
it was `detect-changes` answering "not needed" for a suite that was needed.
Same #1368 shape, one level up.

⛔ The fix is a catch-all rather than "add those 101 paths" because of this
module: it reads the WHOLE tracked tree, so no filter NARROWER than everything
can express its input set — any such filter is a guess that is wrong by
construction. ci.yml's `python` filter now carries `**`, pinned by
`test_python_tests_run_cannot_be_path_skipped`; the enumerated entries stay
beside it and stay machine-checked, because the guard asks its coverage
questions against the enumerated view only.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Longest extension worth treating as one. What the cap EXCLUDES is measured
# rather than guessed: ten tails in this tree are longer (`gitattributes`,
# `trufflehogignore`, `dockerignore`, `doclinkignore`, `claudeignore`,
# `helmignore`, `gitignore`, `nojekyll`, `gitkeep`, `example`), and every one of
# them is a REAL extension — not the dotted version fragment an earlier wording
# claimed. Nothing is lost: a path the extension pattern cannot fullmatch is
# exactly what `_extensionless_token` takes, so those files are tokenised there.
_MAX_EXTENSION = 6


@lru_cache(maxsize=1)
def _extensions() -> tuple[str, ...]:
    """Every extension the tree actually uses, LONGEST FIRST.

    ⛔ Derived, not listed, for two reasons that both bit this module:

    1. A hand-written list omits what the repo already has. The first version
       missed `png`, so a wrapped reference to one of the tracked `.png`
       baselines would have passed. Caught in review, not by any test here.
    2. A hand-written list has to be ORDERED by hand, and Python's `|` is
       first-match, not longest-match: with `js` before `jsx`, `foo.jsx` matches
       as `foo.js` — and because the truncated name is often a real build
       artefact, the scan reports a reference to the WRONG file, plausibly
       enough to survive a full read-through. It did. Sorting by length
       descending makes that bug structurally impossible rather than pinned.
    """
    found = set()
    for path in _tracked():
        name = path.rsplit("/", 1)[-1]
        if "." not in name:
            continue
        ext = name.rsplit(".", 1)[-1]
        if ext.isalnum() and 0 < len(ext) <= _MAX_EXTENSION:
            found.add(ext.lower())
    return tuple(sorted(found, key=lambda e: (-len(e), e)))


@lru_cache(maxsize=1)
def _extension_token() -> re.Pattern[str]:
    return re.compile(
        r"[A-Za-z0-9_.\-/]+\.(?:" + "|".join(_extensions()) + r")(?![A-Za-z0-9])")


@lru_cache(maxsize=1)
def _extensionless_token() -> re.Pattern[str]:
    """The tracked paths an extension-anchored pattern can never produce.

    ⛔ `Makefile`, `Dockerfile` ×9, `LICENSE`, every `.gitignore`-family file,
    `components/da-tools/app/VERSION`, `scripts/hooks/commit-msg`,
    `tests/rulepacks/vm_engine_version` — 43 tracked files whose basename has no
    usable extension. The first version required `.<ext>`, so none of them was
    ever a token: not "the resolver let it through", but "the tokeniser never
    saw it". A live violation was sitting in `ci.yml` the whole time
    (`tests/rulepacks/` / `vm_engine_version`, wrapped across two comment lines,
    invisible to a whole-name grep) and the guard was green. Blind review.

    Derived from `_tracked()` for the same reason `_extensions()` is: a list
    would rot the moment someone adds a `Dockerfile`.
    """
    names = {p for p in _tracked() if not _extension_token().fullmatch(p)}
    names |= {
        name for name, path in _unique_basenames().items()
        if path in names
    }
    if not names:  # pragma: no cover - the repo always has a Makefile
        return re.compile(r"(?!)")
    ordered = sorted(names, key=lambda n: (-len(n), n))
    return re.compile(
        # ⛔ THE OPTIONAL PREFIX MAKES THIS SYMMETRIC WITH THE EXTENSION HALF,
        # which is `[A-Za-z0-9_.\-/]+\.<ext>` and so swallows any directory
        # prefix. Without it the lookbehind's `/` killed every PREFIXED
        # extensionless spelling before the resolver ever saw it: blind review
        # measured `wrong/dir/vm_engine_version` producing no token at all while
        # the bare name produced one — so #1452's whole class stayed open for
        # the 43 tracked files with no usable extension, including the very file
        # this docstring cites as the live violation. The name still has to be
        # in the alternation, so this widens the SPELLING, not the inventory.
        r"(?<![A-Za-z0-9_.\-/])(?:[A-Za-z0-9_.\-]+/)*(?:"
        + "|".join(re.escape(n) for n in ordered)
        + r")(?![A-Za-z0-9_.\-/])")


def _tokens(text: str) -> set[str]:
    return set(_extension_token().findall(text)) | set(
        _extensionless_token().findall(text))


# A continuation: the newline, the next line's indent, and its comment marker if
# it has one. `*` covers `/* … */` blocks that carry a leading star; a block
# comment without one is just indentation, which the `?` allows.
# ⚠️ Five markers only. `>` (Markdown blockquote), `::`/`REM` (batch), `%`, `!`
# are NOT modelled — a path wrapped inside one of those is invisible. Blind
# review demonstrated all of them; none occurs in the 46 wraps this change
# fixed, but the gap is real, so it is named rather than implied.
LINE_PREFIX = re.compile(r"^[ \t]*(?:#|//|\*|--|;)?[ \t]*")

# A bare filename counts as a reference only if it is unambiguous, at least this
# long, and carries a separator. What each bound EXCLUDES is measured over the
# tree: uniqueness drops `README.md` (37 tracked paths), `_defaults.yaml` (16)
# and `values.yaml` (11); the length/separator floor drops a further 264
# otherwise-unique basenames, `index.md` and `resolve.go` among them.
# ⚠️ That is not the same as "measured to earn their place". Relaxing any one of
# them — or all three at once — does not change what this guard reports: zero on
# this branch, and the same two references on the merge base, where those two
# were still live (#1452). They keep generic names out of the scan, and on
# today's corpus that value is prospective, not demonstrated.
_MIN_BARE_NAME = 12

# Coverage floors — they answer "did the scan run at all?", nothing finer.
# ⛔ Do NOT read them as protecting the token model: blind review cut
# `_extensions()` down to a SINGLE extension and all three floors still passed
# (the token floor alone carries 11.2× slack as shipped). What holds the
# model honest is
# `test_extensions_are_derived_and_longest_first`. `_MIN_FILES` is weaker
# still — `_unread_drift` runs first and demands every tracked file, so by the
# time this is reached the count is always the full tree; it only fires if the
# repo itself shrinks. Kept as a cheap tripwire, described for what it is.
# Independent of anything `_tracked()` computes, on purpose: a floor taken from
# the thing it protects is not a floor.
_MIN_TRACKED_FILES = 1000

_MIN_FILES = 1500
_MIN_TOKENS = 2000
_MIN_RESOLVING = 300

# An identifier-shaped token. Deliberately NOT anchored on backticks: 4 of the 20
# references this scan found when it was written carry none, and a guard that
# required them would have reported 16.
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Anti-vacuity floor for the identifier scan, counted over identifiers seen
# OUTSIDE the file that defines them. An independent literal, derived from
# nothing this module computes.
#
# ⛔ THE OBVIOUS METRIC IS TAUTOLOGICAL, which is why this one is stranger than
# it looks. "How many inventory names appear in the corpus" measures NOTHING:
# every name is defined in a tracked `.py` that is itself part of the corpus, so
# the answer is the size of the inventory, always. Measured twice from separate
# implementations: 11977 names, 11977 seen, zero unseen. A floor on that number
# would have been the inventory reciting itself, and it would have cleared any
# threshold under 11977 no matter how broken the scan was.
#
# Excluding the defining file is what makes it move, because a definition site
# can no longer vouch for itself. One broken thing at a time, all from a single
# run so the numbers share a definition:
#
#   today                                                       1774
#   the separator bound inverted (`"_" not in name`)             114
#   the length bound raised 12 -> 40                              69
#   every file truncated to its first 4096 BYTES                 106
#   `scripts/` deleted outright (a legitimate edit)               262
#   `tests/` deleted outright (a legitimate edit)                 529
#
# ⚠️ SO THE USABLE WINDOW IS 114..262, i.e. 2.3x, and that is NARROW — the path
# side's `_MIN_RESOLVING` sits in a 300..2385 window, nearly 8x. 250 catches
# every silent failure above and clears the smallest legitimate deletion above,
# the latter by 1.05x. Deleting all of `scripts/` is the only measured edit that
# comes near it, and that is not a change anyone makes by accident. Written down
# rather than smoothed over: a floor with one part in twenty of slack is a
# tripwire, not a guarantee.
#
# ⛔ WHAT THIS FLOOR CANNOT SEE, measured rather than reasoned:
#   * the rejoin itself breaking — the number does not move by one count, so
#     the synthetic wrap in `test_identifier_detector_finds_a_synthetic_wrap` is
#     the only thing standing between a dead detector and a green suite;
#   * the corpus shrinking to Python only — 1774 becomes 1604, a tenth, still
#     far clear of the floor. What actually refuses that corpus is
#     `_unread_drift`, which is why the identifier scan is handed the SAME list
#     of files rather than reading the tree a second time.
_MIN_IDENT_OFFSITE = 250

# ⛔ NOTHING is skipped: every tracked file is decoded with `errors="replace"`.
#
# The first version instead pinned the exact set of files that fail to decode as
# UTF-8, on the reasoning that a decoder silently dropping input turns "found
# nothing" into "clean". The reasoning was right; the implementation was not —
# WHICH files fail to decode is ENVIRONMENT-DEPENDENT. The three names it pinned
# are LFS-backed PNG baselines (`.gitattributes` routes `tests/e2e/*-snapshots/`
# through Git LFS): on a dev machine with LFS smudged they are real PNGs and
# undecodable, while `actions/checkout` does not fetch LFS by default, so on CI
# they are UTF-8 pointer files and decode fine. Green locally, red on CI.
#
# Replacing bytes instead of skipping files removes the question entirely, and
# `_unread_drift` keeps the original intent in a form that does not depend on
# where the test runs: every tracked file must have been READ.


@lru_cache(maxsize=1)
def _tracked() -> tuple[str, ...]:
    out = subprocess.run(
        # ⛔ `-z` + a NUL split, not the newline default. Under the default
        # `core.quotePath` this command C-quotes any non-ASCII tracked path, so
        # a newline-split listing silently misrepresents it — and this scan's
        # whole point is that nothing is quietly dropped. The sibling guard
        # (tests/ops/test_ci_path_filter_coverage.py `_split_tracked`) fixed
        # this and documented the half-fix trap; this copy was left behind in
        # the same change and blind review caught the asymmetry.
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True,
        # An inherited invalid stdin handle makes subprocess raise WinError 6
        # on some Windows pytest runners; see the same note in
        # tests/ops/test_ci_path_filter_coverage.py's `_tracked_files`.
        stdin=subprocess.DEVNULL,
        check=True, timeout=120).stdout
    return _split_listing(out)


def _split_listing(raw: str) -> tuple[str, ...]:
    """Pure, so the floor below can be handed degenerate input.

    ⛔ Inline over live data, neither check below can fail — nothing tells
    "the check is here" from "the check was deleted". Split out so
    `test_tracked_split_refuses_a_narrowed_listing` can drive them directly.
    """
    files = tuple(sorted(p for p in raw.split("\0") if p))
    assert len(files) >= _MIN_TRACKED_FILES, (
        f"`git ls-files -z` yielded {len(files)} path(s) — not a plausible "
        "listing of this repo. `_unread_drift` compares the scan against THIS "
        "sequence, so a truncated or wrongly-split listing makes the "
        "'nothing was skipped' check agree with itself and pass.")
    assert len(files) >= raw.count("\0"), (
        f"{raw.count(chr(0))} NUL separators but only {len(files)} paths "
        "survived — something between git and here is dropping entries.")
    return files


@lru_cache(maxsize=1)
def _unique_basenames() -> dict[str, str]:
    """Repo-unique basenames that are distinctive enough to match on."""
    seen: dict[str, list[str]] = {}
    for path in _tracked():
        seen.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return {
        name: paths[0]
        for name, paths in seen.items()
        if len(paths) == 1 and len(name) >= _MIN_BARE_NAME and re.search(r"[_-]", name)
    }


def _resolves(token: str) -> bool:
    return token in set(_tracked()) or token in _unique_basenames()


def _names_a_file_through_its_basename(token: str, raw: str) -> str | None:
    """The tracked file a PREFIXED token names, when the prefix resolves to nothing.

    ⛔ THE GAP THIS CLOSES IS ONE THE MODULE ALREADY CLAIMED TO HAVE CLOSED.
    `_resolves` compares the WHOLE token, so a rejoined
    `pkg/config/docs_defaults_sample_test.go` matches no tracked path — the real
    file lives under `components/threshold-exporter/app/`. The basename does
    resolve, and is repo-unique, and is exactly what a rename sweep greps for.
    Two such pointers were sitting in the tree while this file said the tree was
    at zero (#1452).

    ⛔ DERIVED, NOT LISTED: it reuses `_unique_basenames()` — the same bounds
    (repo-unique, `_MIN_BARE_NAME` long, contains a separator) that already keep
    `utils.py` out of the scan. No new inventory of "important" names.

    ⛔ THE PREFIX MUST BE A SPELLING OF OUR PATH, not merely carry a familiar
    basename. Blind review measured the first version reporting
    `vendor/github.com/prometheus/common/config/config_test.go`,
    `https://github.com/prometheus/prometheus/blob/main/config/config_test.go`,
    `/src/.pre-commit-config.yaml` and `~/work/myrepo/.pre-commit-config.yaml`
    — every one of them a correct citation of somebody ELSE's file, reported as
    ours, because the prefix was thrown away. That is worse than a false red:
    the remedy it demands (reflow so a rename sweep can grep it) is unfounded,
    since nobody sweeping THIS repo greps a vendored or customer-side path.
    Requiring the token to be a suffix-compatible spelling of the real path
    keeps the class the ticket is about: `../x.md` and `docs/x.md` still
    resolve, because they are spellings of our file.

    ⚠️ WHAT THAT FILTER COSTS AND BUYS. Occurrences, over every tracked file,
    and the corpus is named for each number because they are not all from the
    same one. ON THE MERGE BASE (where the two live defects still sat): as
    shipped the filter removes ZERO reports — nothing wrapped there
    misattributes — and its effect is only visible once `base not in raw` is
    removed as well, where it takes 322 reports down to 20. The material is real
    even though none of it is wrapped: ON THIS BRANCH 276 distinct prefixed
    tokens carry one of our basenames without being a spelling of our path (269
    on the merge base), of which 156 name something else (151) and 120 are our
    own file cited with EXTRA leading segments (118) — `…/blob/main/<our path>`,
    which this filter silences too. That half is a gap and it is in the gap
    list.

    ⛔ HOW A CANDIDATE IS CLASSIFIED, because the counts above are not
    reproducible without it. A candidate is a token that appears in the rejoined
    window and NOT in the raw one; it is then sorted by the FIRST of these that
    applies, in this order: `_resolves` as a whole (the pre-#1452 class) → no
    prefix at all → basename fails the bare-name bounds → basename appears
    contiguously in the window → not a spelling of our path → reported here.
    Reordering those tests redistributes the same population into different
    buckets, so a bucket count without this order attached means nothing.

    ⛔ `base not in raw` IS THE WHOLE POINT, not a nicety. When the basename
    already appears contiguously somewhere in the two-line window, `git grep`
    finds it and the break hides nothing. Dropping it costs 18 extra reports:
    2 → 20 on the merge base, 0 → 18 on this branch. Seventeen of those 18 are
    joins like `//git_shell.go` and `/tenant_custom_alerts.go`, where a comment
    marker or a bare slash is all that was glued on; the eighteenth is a real
    partial path, correctly let through because its basename is right there in
    the window.
    """
    base = token.rsplit("/", 1)[-1]
    if base == token:
        # ⛔ Equivalent to falling through ONLY because the sole caller asks
        # `_resolves` first, and a bare token that resolves is reported there.
        # Measured over a full scan: 1181 invocations, 593 of them bare, and
        # removing this line changes NOTHING — every bare token that gets here
        # has already failed `_resolves`, so its basename does not resolve
        # either and the lookup below would return None anyway. A second caller
        # that does not ask `_resolves` first would break that, and no test
        # would notice.
        return None            # no prefix — `_resolves` has already had its say
    if base in raw:
        return None            # grep still finds it; the line break hides nothing
    real = _unique_basenames().get(base)
    if real is None:
        return None
    return real if _is_a_spelling_of(token, real) else None


def _is_a_spelling_of(token: str, real: str) -> bool:
    """Could `token` be this repo's `real` path, written from somewhere else?

    Anchored, relative and partial spellings of our own path all end with the
    same segments; a vendored copy, an upstream URL or a container path carries
    segments ours does not have, so it is longer than the real path or diverges
    inside it. Compared on SEGMENT boundaries, so `.../app/config_test.go` is
    not satisfied by `.../myapp/config_test.go`.

    ⛔ NO LIST OF PREFIXES TO STRIP. A first spelling peeled `~`, `src`, `http:`
    off the front, which is an enumeration that would need a new entry for every
    mount point anyone ever documents — and it re-admitted
    `/src/.pre-commit-config.yaml` (the CUSTOMER's file at pre-commit's docker
    mount) as ours. Only `.` and `..` are dropped, because they name no segment.
    """
    parts = [p for p in token.split("/") if p not in ("", ".", "..")]
    if not parts:
        return False
    real_parts = real.split("/")
    return len(parts) <= len(real_parts) and real_parts[-len(parts):] == parts


def _wrapped_references(text: str) -> list[tuple[int, str]]:
    """(1-based line of the break, token) for every reference split in two.

    A token is reported only when it appears in the REJOINED two-line window and
    NOT in the raw window — i.e. the line break is what hides it.

    ⛔ THE CARRIAGE RETURN IS DROPPED HERE, and that is not cosmetic. Splitting
    on `\\n` alone leaves a CR at the end of the first half, and the token
    character class does not contain it, so the rejoin produces a BROKEN token
    and the file is structurally invisible to this guard. `.gitattributes` puts
    `*.bat`, `*.cmd` and `*.ps1` on CRLF deliberately ("Windows-specific shells
    — MUST be CRLF"), so this was a permanent blind spot over six tracked files,
    three of them `.ps1` whose `#` continuation marker `LINE_PREFIX` already
    models. Measured both ways: the same text is reported under LF and silent
    under CRLF (pinned below).

    ⚠️ It buys nothing on today's tree — stripping the CR across the whole
    corpus unlocks ZERO new reports, so this removes a structural blind spot
    rather than finding a live defect. Done HERE and not in `_read_tracked`,
    whose contract is that every tracked byte reached the scan; normalising line
    endings belongs to the consumer that cares about line structure.
    """
    lines = [line[:-1] if line.endswith("\r") else line
             for line in text.split("\n")]
    found: list[tuple[int, str]] = []
    for index in range(len(lines) - 1):
        raw = lines[index] + "\n" + lines[index + 1]
        prefix = LINE_PREFIX.match(lines[index + 1]).group(0)
        rejoined = lines[index] + lines[index + 1][len(prefix):]
        for token in sorted(_tokens(rejoined)):
            if token in raw:
                continue
            if not (_resolves(token)
                    or _names_a_file_through_its_basename(token, raw)):
                continue
            found.append((index + 1, token))
    return found


# ⛔ THE TRIPWIRES BELOW ARE PURE FUNCTIONS, and that shape is the whole point.
#
# Each one answers "is anything wrong?" for a condition that is currently FALSE:
# the tree has no wrapped reference, the unreadable set matches, coverage is
# ample. An `assert` written inline against a currently-false condition cannot
# be tested — deleting it is invisible, because no input distinguishes the two
# implementations. Mutation testing said so out loud: clearing `offenders`,
# neutering the unreadable check, and flattening the coverage floor ALL survived
# a full run of the first version of this module.
#
# Splitting each tripwire into a function the tests can feed degenerate input to
# is what makes "the tripwire still exists" an assertion rather than a hope.
# (testing-playbook §v2.10.0 ⑤ — a mutant that survives because no case can tell
# the difference is a test-DESIGN bug, not coverage.)


def _offenders(files: list[tuple[str, str]]) -> dict[str, list[str]]:
    """path -> human-readable hits, for every file with a wrapped reference."""
    found: dict[str, list[str]] = {}
    for path, text in files:
        for line, token in _wrapped_references(text):
            found.setdefault(path, []).append(f"line {line} → {token}")
    return found


def _unread_drift(read_paths: set[str]) -> list[str]:
    """Non-empty when a tracked file never reached the scan.

    Environment-independent by construction: it compares against `git ls-files`
    rather than against a pinned list of names, which is what made the first
    version pass locally and fail on CI.
    """
    missing = sorted(set(_tracked()) - read_paths)
    if not missing:
        return []
    return [f"{len(missing)} tracked file(s) were never read: {missing[:5]}"]


def _decoded_shortfall(read_chars: int, on_disk: int) -> str | None:
    """⛔ Guards the CONSUMED corpus, not the bytes handed to the decoder.

    `_decode_whole` asserts `len(raw) == st_size`, which is an assertion about
    its INPUT — moving the truncation one line later (`raw.decode(...)[:4096]`)
    satisfies it and still drops most of the corpus. Blind review measured that
    edit: every path survived, all three floors below cleared, the run got 2.7x
    faster, and a wrapped reference injected past the cut went unseen.

    The bound is arithmetic, not a tuned constant: UTF-8 uses at most 4 bytes
    per code point, so decoding N bytes yields at least N/4 characters. Any
    complete decode clears it with room; a prefix truncation cannot.
    """
    if read_chars * 4 < on_disk:
        return (f"only {read_chars} character(s) decoded from {on_disk} byte(s) "
                "on disk — below the arithmetic floor for a complete UTF-8 "
                "decode, so content was dropped AFTER the per-file size check. "
                "The path set can be intact while the corpus is a prefix.")
    return None


def _coverage_shortfalls(files: list[tuple[str, str]]) -> list[str]:
    """Non-empty when the scan stopped reaching enough of the tree."""
    tokens = [t for _, text in files for t in _tokens(text)]
    resolving = {t for t in tokens if _resolves(t)}
    problems = []
    on_disk = 0
    for path in _tracked():
        try:
            on_disk += (ROOT / path).stat().st_size
        except OSError:
            continue
    truncated = _decoded_shortfall(sum(len(t) for _, t in files), on_disk)
    if truncated:
        problems.append(truncated)
    if len(files) <= _MIN_FILES:
        problems.append(f"only {len(files)} tracked files read (floor {_MIN_FILES})")
    if len(tokens) <= _MIN_TOKENS:
        problems.append(f"only {len(tokens)} path-like tokens seen (floor {_MIN_TOKENS})")
    if len(resolving) <= _MIN_RESOLVING:
        problems.append(
            f"only {len(resolving)} distinct tokens resolved to real files "
            f"(floor {_MIN_RESOLVING}) — the resolver, not the regex, is what "
            "decides whether a hit is a reference")
    return problems


# ---------------------------------------------------------------------------
# The identifier side (#1453). Same window, same rejoin, different resolver.
# ---------------------------------------------------------------------------


def _identifier_inventory(files: list[tuple[str, str]]) -> dict[str, str]:
    """Every name this repo DEFINES exactly once, mapped to the file defining it.

    ⛔ DERIVED, NOT LISTED, and the derivation is `ast` rather than a regex: the
    question "is this token a symbol somebody could rename" is answered by the
    definition sites, and those are structure. A list of "important names" would
    be the enumeration this module refuses everywhere else.

    ⛔ THE THREE BOUNDS ARE THE BARE-NAME BOUNDS, TRANSPOSED. The path side
    accepts a bare filename only when it is repo-unique, at least
    `_MIN_BARE_NAME` long and carries a separator. The symbol side asks the same
    three questions of a name: defined in exactly one place, at least
    `_MIN_BARE_NAME` long, and contains an underscore. `_MIN_BARE_NAME` is
    SHARED rather than copied, so the transposition cannot drift apart from the
    thing it was transposed from.

    ⚠️ WHAT THOSE BOUNDS ARE ACTUALLY WORTH, because the honest answer is less
    than "measured to earn their place". Relaxing any ONE of them changes what
    this scan reports by nothing at all. Relaxing all three together adds 4
    reports, and all 4 are prose glue — an English word ending one line running
    into the start of the next, spelling `check4`, `alertname`, `filename`,
    `score`. So the bounds are worth 4 as a conjunction and zero individually,
    which means NO MUTATION CAN KILL ANY ONE OF THEM: do not read a surviving
    single-bound mutant as a hole. They are kept because the 4 they exclude are
    exactly the family #1500 is about, not because a test would notice.

    ⛔ ASSIGNMENTS COUNT, BUT ONLY AT MODULE AND CLASS LEVEL, and the boundary is
    measured rather than conventional. A module constant is precisely the thing
    a rename sweep greps for, so it belongs in. A function-local name is not:
    admitting them grows the inventory by 838 names (11977 -> 12815) and grows
    the set of names an innocent edit could accidentally spell by 96 (156 ->
    252). ⚠️ "Accidentally spell" is measured as: some line in this tree ends
    with the name's first half and some line starts with its second, so a future
    edit that puts those two lines next to each other reports a symbol nobody
    wrote. Both halves of that trade are cost; the benefit measured on the tree
    this landed against was one further real reference, and it was a module
    constant, not a local.

    ⚠️ NOT MODELLED, and it is the biggest gap here: every definition side that
    is not Python. A Go `func`, a shell function, a Make target, a YAML anchor,
    a JS export — `ast` never parses any of them, so a wrapped reference to one
    is invisible. The ticket proposed closing that by also accepting any name
    that occurs contiguously somewhere in the tree; measured, that admits 25
    further reports and every one of them is prose glue, because an identifier
    has no analogue of the slash and extension that give a path its shape. Under
    that evidence a legitimate sentence and a broken reference are the same
    object, so the gap is disclosed rather than closed.
    """
    sites: dict[str, list[str]] = {}
    for path, text in files:
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - the tree parses today
            continue
        names = [node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))]
        bodies = [tree.body]
        bodies += [node.body for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef)]
        for body in bodies:
            for stmt in body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        names += [sub.id for sub in ast.walk(target)
                                  if isinstance(sub, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and isinstance(
                        stmt.target, ast.Name):
                    names.append(stmt.target.id)
        for name in names:
            sites.setdefault(name, []).append(path)
    return {
        name: paths[0]
        for name, paths in sites.items()
        if len(paths) == 1 and len(name) >= _MIN_BARE_NAME and "_" in name
    }


def _wrapped_identifiers(text: str,
                         inventory: dict[str, str]) -> list[tuple[int, str]]:
    """(1-based line of the break, name) for every symbol split in two.

    The window, the carriage-return strip and the rejoin are the path side's,
    deliberately: a reference broken by a comment continuation is one mechanism,
    and modelling it twice would be two things to keep in step. Only the
    question asked of the rejoined token differs.
    """
    lines = [line[:-1] if line.endswith("\r") else line
             for line in text.split("\n")]
    found: list[tuple[int, str]] = []
    for index in range(len(lines) - 1):
        raw = lines[index] + "\n" + lines[index + 1]
        prefix = LINE_PREFIX.match(lines[index + 1]).group(0)
        rejoined = lines[index] + lines[index + 1][len(prefix):]
        for token in sorted(set(_IDENT_TOKEN.findall(rejoined))):
            if token in raw:
                continue
            if token in inventory:
                found.append((index + 1, token))
    return found


def _identifier_offenders(files: list[tuple[str, str]],
                          inventory: dict[str, str]) -> dict[str, list[str]]:
    """path -> human-readable hits, for every file with a wrapped identifier."""
    found: dict[str, list[str]] = {}
    for path, text in files:
        for line, token in _wrapped_identifiers(text, inventory):
            found.setdefault(path, []).append(f"line {line} → {token}")
    return found


def _identifier_shortfalls(files: list[tuple[str, str]],
                           inventory: dict[str, str]) -> list[str]:
    """Non-empty when too few defined names were seen away from their definition.

    ⛔ OFFSITE IS THE WHOLE POINT — see the note on `_MIN_IDENT_OFFSITE`. Counted
    over the corpus this scan was HANDED, so truncating or narrowing the corpus
    moves it; counted excluding each name's own defining file, so the
    inventory cannot vouch for itself.
    """
    offsite = set()
    for path, text in files:
        for token in set(_IDENT_TOKEN.findall(text)) & inventory.keys():
            if inventory[token] != path:
                offsite.add(token)
    if len(offsite) > _MIN_IDENT_OFFSITE:
        return []
    return [
        f"only {len(offsite)} defined name(s) were seen anywhere other than the "
        f"file defining them (floor {_MIN_IDENT_OFFSITE}) — so the scan below "
        "would be asking its question of almost nothing. ⛔ The fix is upstream "
        "of this number: check that the corpus is complete and that the `ast` "
        "walk still returns definitions. ⛔ Do NOT reach this floor by widening "
        "the inventory bounds; that direction re-admits the prose-glue family "
        "(#1500) and buys silence rather than coverage."]


def _read_tracked() -> list[tuple[str, str]]:
    """Every tracked file, decoded with replacement so none is ever skipped.

    Binary content turns into replacement characters, which cannot form a token
    that RESOLVES to a real repo path — so reading everything costs nothing in
    precision and removes the "which files did we quietly not look at?" question
    that the pinned-set version got wrong across environments.
    """
    files: list[tuple[str, str]] = []
    for path in _tracked():
        try:
            # ⛔ ONE handle, and `fstat` on it — not two path-based calls.
            # Separate `read_bytes()` + `Path.stat()` can resolve to different
            # inodes if the path is replaced between them, and swapping their
            # ORDER does not help: the check below is an equality, so a file
            # that changes size trips it either way (read-first reports short,
            # stat-first reports long). Same handle makes both numbers describe
            # the same file.
            with (ROOT / path).open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                raw = handle.read()
        except OSError:
            continue
        files.append((path, _decode_whole(raw, size, path)))
    return files


def _decode_whole(raw: bytes, size: int, path: str) -> str:
    """Decode, refusing a PARTIAL read.

    ⛔ Every other guard here pins the PATH SET — `_unread_drift`, the
    independent `git ls-files` cross-check, the no-slicing check. None of them
    sees content: `read_bytes()[:4096]` keeps all 2315 paths, satisfies all
    three, and still truncates 1636 files, dropping ~76% of the corpus and
    ~58% of the path-like tokens. ⚠️ Count files by BYTES here, which is the
    unit that slice cuts in; counting decoded characters gives 1608 and is the
    wrong question. Measured: a wrapped reference injected past
    the cut is invisible while the suite stays green (and the run gets 2.4x
    faster, which is what makes the edit attractive).

    Pure, so `test_partial_read_is_refused` can hand it a short buffer — an
    inline check over real files can never fail and so can never be shown to
    still exist.
    """
    assert len(raw) == size, (
        f"{path}: read {len(raw)} byte(s) of a {size}-byte file. The scan then "
        "covers a PREFIX of the tree's content while every path-set guard "
        "still reports a full sweep — the failure this module exists to "
        "prevent, one layer in.")
    return raw.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def corpus() -> list[tuple[str, str]]:
    """The whole tracked tree, read ONCE and shared by both scans.

    ⛔ SHARED RATHER THAN RE-READ, and that is load-bearing rather than thrift.
    `_unread_drift` and `_decoded_shortfall` are assertions about THIS list; a
    scan that fetched the tree for itself would sit outside both of them. It
    matters here specifically because the identifier scan's own floor barely
    notices a narrowed corpus — measured, dropping every non-Python file costs
    it 10% and leaves it far above its floor, while `_unread_drift` refuses that
    corpus outright.
    """
    return _read_tracked()


def test_no_reference_is_split_across_a_line_break(
        corpus: list[tuple[str, str]]) -> None:
    files = corpus

    drift = _unread_drift({path for path, _ in files})
    assert not drift, (
        "the scan never looked at part of the tree, so a green result below "
        "would mean nothing: " + "; ".join(drift))

    shortfalls = _coverage_shortfalls(files)
    assert not shortfalls, (
        "the scan stopped reaching the tree, so a green result below would mean "
        "nothing: " + "; ".join(shortfalls))

    offenders = _offenders(files)
    assert not offenders, (
        "a file reference is split across a line break, so grepping the string "
        "as written does not find it. Reflow so the reference sits on ONE "
        "line; the surrounding prose can wrap wherever you like.\n"
        "⚠️ Reflowing restores exactly one property: the spelling AS WRITTEN "
        "becomes greppable. If the reference is a PARTIAL path, grepping the "
        "full tracked path still will not return this site — and it may return "
        "OTHER files, so a sweep reads a non-empty result as coverage it does "
        "not have. The reflow is the floor here, not the ceiling.\n"
        "⛔ A reflow is the only fix. Going green any other way leaves the "
        "reference exactly as invisible as it is now.\n"
        "⚠️ If this fired on something that is NOT a reference — a directory "
        "listing, two unrelated list items, a sentence whose last word ran "
        "into a filename — then there is nothing to reflow and the report is "
        "wrong. That is a defect in THIS GUARD, not in your change, and this "
        "module has no per-line exemption on purpose, so raise it against the "
        "guard.\n"
        + "\n".join(
            f"    {path}:\n" + "\n".join(f"      {hit}" for hit in hits)
            for path, hits in sorted(offenders.items())))


def test_detector_finds_a_synthetic_wrap() -> None:
    """Anti-vacuity: the tree is at zero, so the assertion above proves nothing
    on its own. A scanner that returned `[]` unconditionally would pass it.

    ⛔ EVERY marker `LINE_PREFIX` models needs a case here, and that is not
    tidiness. The false-positive note in the module docstring generalises a
    prohibition to all five ("dropping a marker is NOT the fix"), and a
    prohibition nothing enforces is a promise. Measured: with only the first
    four cases, deleting `--` or `;` from `LINE_PREFIX` left this module fully
    green, so two of the five could be removed for free — silently ending
    detection under a marker that heads thousands of tracked lines.
    """
    target = "tests/ops/test_wrapped_path_references.py"
    assert target in _tracked(), "this module moved; re-point the fixture"
    head, tail = target[:20], target[20:]

    for wrapped, label in (
        (f"# see {head}\n# {tail} for the rule\n", "hash comment"),
        (f"// see {head}\n//     {tail} for the rule\n", "slash comment"),
        (f" * see {head}\n *  {tail} for the rule\n", "star block"),
        (f"-- see {head}\n--  {tail} for the rule\n", "sql/lua comment"),
        (f"; see {head}\n;  {tail} for the rule\n", "semicolon comment"),
        (f"    see {head}\n    {tail} for the rule\n", "bare indent"),
    ):
        hits = _wrapped_references(wrapped)
        assert [t for _, t in hits] == [target], f"{label}: {hits}"

    contiguous = f"# see {target} for the rule\n# and more prose here\n"
    assert _wrapped_references(contiguous) == [], (
        "a reference that is NOT split must not be reported — otherwise every "
        "normal citation reds and the fix is to delete the guard")


def test_no_identifier_is_split_across_a_line_break(
        corpus: list[tuple[str, str]]) -> None:
    """#1453: the same accident, with a symbol instead of a path.

    #1373 was a rename sweep that grepped for a moved module, got a clean
    answer, and shipped a dangling pointer — the reference had wrapped mid-name.
    The path scan above closes that for paths. A symbol wrapped the same way is
    just as invisible to `git grep` and to `sed`, and nothing was watching it:
    when this was written the tree carried 20 such references, all real.

    ⚠️ THE HARM IS AN OCCURRENCE, NOT USUALLY A FILE. Measured over those 20:
    only 7 are invisible at file granularity. For the other 13 the name does
    appear contiguously elsewhere in the same file — because that file is where
    the symbol is defined — so a sweep opens the file, fixes what it can see,
    and silently leaves this one behind having counted one occurrence fewer than
    exists. The ticket's wording said all of them were file-invisible; measured,
    that is true of 7.

    ⛔ THIS IS A FLOW, NOT A BACKLOG, which is the argument for spending a guard
    on it rather than sweeping once. Run against the tree as it stood when the
    ticket was filed, the same scan reports 14 — and those 14 are a strict
    subset of the 20 this landed with, the other 6 having arrived over the
    following six days in two merged commits. ⚠️ Whether the path half sees the
    same rate was not measured, so no comparison is claimed here.
    """
    inventory = _identifier_inventory(corpus)

    shortfalls = _identifier_shortfalls(corpus, inventory)
    assert not shortfalls, (
        "the identifier scan stopped reaching the tree, so a green result below "
        "would mean nothing: " + "; ".join(shortfalls))

    offenders = _identifier_offenders(corpus, inventory)
    assert not offenders, (
        "a name this repo defines is split across a line break, so grepping it "
        "as written does not find this site.\n"
        "⚠️ READ THIS FIRST, because unlike the path scan this one can be wrong "
        "about what it found: the rejoin is mechanical, so ordinary prose, a "
        "listing or a table can spell a real symbol by accident. If what is "
        "named below is NOT a reference to that symbol, there is nothing to "
        "reflow — the report is a defect in THIS GUARD, and this module has no "
        "per-line exemption on purpose, so raise it against the guard.\n"
        "⛔ Either way, do not make this quiet by reshaping the surrounding "
        "lines into something the scan cannot see. If the report is wrong the "
        "guard is what needs fixing; if it is right, only moving the name onto "
        "one line fixes anything.\n"
        "If it IS a reference: reflow so the whole name sits on ONE line. The "
        "surrounding prose can wrap wherever you like. ⚠️ That restores one "
        "occurrence to `git grep`, and usually nothing else — most of these "
        "sites are in the file that defines the name, so a sweep was already "
        "opening the file and still miscounting.\n"
        + "\n".join(
            f"    {path}:\n" + "\n".join(f"      {hit}" for hit in hits)
            for path, hits in sorted(offenders.items())))


def test_identifier_detector_finds_a_synthetic_wrap(
        corpus: list[tuple[str, str]]) -> None:
    """Anti-vacuity, and the ONLY thing that notices a dead rejoin.

    ⛔ The floor cannot do this job. Break the continuation strip and the
    identifier scan reports nothing while `_identifier_shortfalls` does not move
    by a single count — measured, 1771 before and after — because a coverage
    number answers "was there anything to look at", not "did the detector run".
    Everything standing between that mutation and a green suite is below.

    ⛔ EVERY marker `LINE_PREFIX` models gets a case, for the reason the path
    side's twin gives: the module docstring generalises a prohibition to all
    five, and a prohibition nothing enforces is a promise.
    """
    probe = "_MIN_IDENT_OFFSITE"
    inventory = _identifier_inventory(corpus)
    assert probe in inventory, (
        f"{probe} is no longer a uniquely-defined name of its own module, so "
        "this test's premise is gone — re-derive a subject from the inventory "
        "rather than relaxing the assertions")
    head, tail = probe[:8], probe[8:]

    for wrapped, label in (
        (f"# see {head}\n# {tail} for the rule\n", "hash comment"),
        (f"// see {head}\n//     {tail} for the rule\n", "slash comment"),
        (f" * see {head}\n *  {tail} for the rule\n", "star block"),
        (f"-- see {head}\n--  {tail} for the rule\n", "sql/lua comment"),
        (f"; see {head}\n;  {tail} for the rule\n", "semicolon comment"),
        (f"    see {head}\n    {tail} for the rule\n", "bare indent"),
    ):
        hits = _wrapped_identifiers(wrapped, inventory)
        assert [t for _, t in hits] == [probe], f"{label}: {hits}"

    contiguous = f"# see {probe} for the rule\n# and more prose here\n"
    assert _wrapped_identifiers(contiguous, inventory) == [], (
        "a name that is NOT split must not be reported — otherwise every "
        "ordinary mention reds and the cheapest fix is to delete the guard")

    # ⛔ The inventory, not the regex, is what decides. Without this the whole
    # thing could be "any token that spans the seam" and every case above would
    # still pass — and that predicate reports thousands.
    assert _wrapped_identifiers(f"# see {head}\n# {tail} onwards\n", {}) == [], (
        "with an empty inventory nothing can resolve, so nothing may be "
        "reported; if this fires, the resolver is not what gates a hit")


def test_a_prefixed_token_is_resolved_through_its_basename() -> None:
    """⛔ THE #1452 CASE, and it sits inside the class this file called empty.

    A wrapped token that rejoins into a PARTIAL spelling of a real path —
    `pkg/config/x_test.go` for a file under `components/…/app/` — matches no
    tracked path, so `_resolves` says no, while the basename resolves and is
    exactly what a rename sweep greps for. Two such pointers were live in the
    tree when this was written.

    Every case below is DERIVED from the tree, so none can rot into fiction;
    the subject is this module itself.
    """
    target = "tests/ops/test_wrapped_path_references.py"
    assert target in _tracked(), "this module moved; re-point the fixture"
    base = target.rsplit("/", 1)[-1]
    assert base in _unique_basenames(), (
        base + " stopped being a repo-unique basename; this test's premise is "
        "gone — re-derive a subject rather than relaxing the assertions")

    head, tail = base[:12], base[12:]
    partial = "ops/" + base          # a real, shorter spelling of `target`
    assert target.endswith(partial) and partial not in _tracked(), (
        "the fixture must be a PARTIAL spelling of a tracked path, not a "
        "tracked path itself — otherwise `_resolves` handles it and this test "
        "stops exercising the basename fallback")

    # MUST REPORT: partial spelling of our own path, and the basename is split,
    # so grepping either spelling finds nothing.
    wrapped = "# see ops/" + head + "\n# " + tail + " for the rule\n"
    hits = _wrapped_references(wrapped)
    assert [t for _, t in hits] == [partial], hits

    # MUST NOT REPORT: the basename ALSO appears contiguously in the window —
    # `git grep <base>` finds it, so the break hides nothing. This half is what
    # keeps the guard off the ~320 innocent wraps in this tree; without it the
    # cheapest way to go green is deleting the guard.
    grepable = ("# see " + base + " at ops/" + head
                + "\n# " + tail + " for the rule\n")
    assert _wrapped_references(grepable) == [], (
        "a prefixed token whose basename is still greppable in the same window "
        "must NOT be reported")

    # MUST NOT REPORT: somebody ELSE's file that merely shares a basename with
    # one of ours. Blind review measured the first version reporting a vendored
    # path, an upstream URL and a container mount as if they were this repo's
    # files — asking for a reflow that no rename sweep here would ever need.
    foreign = "vendor/github.com/example/" + base
    assert foreign.rsplit("/", 1)[-1] in _unique_basenames(), (
        "this case only means something while the basename still resolves")
    hits = _wrapped_references(
        "# copied from vendor/github.com/example/" + head
        + "\n# " + tail + " (Apache-2.0)\n")
    assert hits == [], (
        "a citation of another project's file must NOT be reported as ours: " + repr(hits))

    # MUST NOT REPORT: the prefix has to match on SEGMENT boundaries. The tail
    # of one of our directory names is not one of our directories, and comparing
    # the joined strings with `endswith` would accept it — mutation testing said
    # so: swapping the segment comparison for a string suffix left every other
    # case in this module green.
    parent = target.rsplit("/", 2)[1]
    assert len(parent) > 1, (
        parent + " can no longer be clipped; pick a subject whose parent "
        "directory has more than one character")
    clipped = parent[1:] + "/" + base
    assert target.endswith(clipped) and clipped.split("/")[0] not in target.split("/"), (
        "this case only means something while the fixture is a suffix of the "
        "STRING and not a suffix of the PATH")
    assert _wrapped_references(
        "# see " + parent[1:] + "/" + head + "\n# " + tail + " here\n") == [], (
        "a prefix that only matches mid-segment must NOT be reported: "
        + clipped + " is not a spelling of " + target)

    # MUST NOT REPORT: an AMBIGUOUS basename stays out with a prefix exactly as
    # it does without one. Derived, because the first spelling here used a name
    # that is not in the tree at all and is below `_MIN_BARE_NAME` — it could
    # never have failed, so it pinned nothing.
    counts: dict[str, int] = {}
    for path in _tracked():
        name = path.rsplit("/", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    ambiguous = sorted(
        n for n, k in counts.items()
        if k > 1 and len(n) >= _MIN_BARE_NAME and re.search(r"[_-]", n))
    assert ambiguous, (
        "no basename is both ambiguous and past this module's bare-name bounds "
        "any more — re-derive this case instead of deleting it")
    amb = ambiguous[0]
    assert amb not in _unique_basenames(), amb
    split = max(1, len(amb) // 2)
    assert _wrapped_references(
        "# see wrong/dir/" + amb[:split] + "\n# " + amb[split:] + " here\n") == [], (
        "a prefixed token whose basename is ambiguous must NOT be reported")


def test_a_wrapped_reference_is_seen_in_a_crlf_file() -> None:
    """⛔ CRLF made this guard structurally BLIND, not merely quieter.

    `.gitattributes` puts `*.bat`, `*.cmd` and `*.ps1` on CRLF on purpose. A
    split on the newline alone leaves the CR at the end of the first half, and
    CR is not in the token character class — so the rejoin produced a broken
    token and no wrapped reference in those files could ever be reported. Six
    tracked files were affected when this was written, three of them `.ps1`,
    whose `#` continuation marker this guard already models: a file type it
    looked covered for and never was.

    ⛔ SYNTHETIC ON PURPOSE. The input carries its own line endings, so this
    does not depend on how a given checkout expanded `.gitattributes` — the
    same mistake the pinned-unreadable-set version of this module made (see the
    note by `_read_tracked`).
    """
    target = "tests/ops/test_wrapped_path_references.py"
    assert target in _tracked(), "this module moved; re-point the fixture"
    base = target.rsplit("/", 1)[-1]
    head, tail = base[:12], base[12:]

    lf = "# see ops/" + head + "\n# " + tail + " for the rule\n"
    expected = [tok for _, tok in _wrapped_references(lf)]
    assert expected, (
        "the LF control did not report, so the CRLF case below would pass for "
        "the wrong reason — re-derive the fixture rather than dropping this")

    crlf = lf.replace("\n", "\r\n")
    assert [tok for _, tok in _wrapped_references(crlf)] == expected, (
        "the same reference must be reported when the file is CRLF; a trailing "
        "CR breaks the rejoined token and silences EVERY wrapped reference in "
        "the `*.bat` / `*.cmd` / `*.ps1` trees")

    # MUST NOT REPORT: dropping the CR must not turn ordinary CRLF text into
    # findings — the raw window has to lose it too, or every reference that sits
    # at a line end starts looking wrapped.
    for quiet in ("# see ops/" + base + " for the rule\r\n# and more\r\n",
                  "# see " + base + " at ops/" + head + "\r\n# " + tail + "\r\n"):
        assert _wrapped_references(quiet) == [], (
            "a CRLF reference that is NOT hidden by the break must stay "
            "silent: " + repr(quiet))


def test_extensions_are_derived_and_longest_first() -> None:
    """Two properties, both of which failed in earlier versions of this module.

    Derived: the set must cover what the tree actually contains — the first
    version hand-listed 16 extensions and omitted `png`, which the tree
    contains three tracked files of.
    Longest-first: Python's `|` is first-match, so `js` before `jsx` makes
    `.jsx` unreachable and the scan reports the truncated name, which is often
    a real build artefact. Sorting by length removes the failure mode instead
    of pinning it.
    """
    exts = _extensions()
    assert exts == tuple(sorted(exts, key=lambda e: (-len(e), e))), (
        "extensions must be ordered longest-first before joining into an "
        "alternation")

    for required in ("jsx", "tsx", "yaml", "png", "go", "py"):
        assert required in exts, (
            f"{required!r} is used in the tree but missing from the derived set")

    for name in ("tools/portal/src/a-really-long-name.jsx",
                 "tests/e2e/some-long-spec.tsx",
                 "helm/vector/tests/projection_tests.yaml",
                 "tests/e2e/visual.spec.ts-snapshots/a-really-long-shot.png"):
        assert _extension_token().findall(name) == [name], (
            f"{name} was truncated by the extension alternation")


def test_extensionless_paths_are_tokenised_too() -> None:
    """The class the first version could not see at all.

    43 tracked files have no usable extension (`Makefile`, eight `Dockerfile`s,
    `LICENSE`, the `.gitignore` family, `components/da-tools/app/VERSION`,
    `tests/rulepacks/vm_engine_version`, …). An extension-anchored pattern never
    produces them as tokens, so a wrapped reference to one was not "let through
    by the resolver" — it was never looked at. One was live in `ci.yml`.
    """
    blind = [p for p in _tracked() if not _extension_token().fullmatch(p)]
    assert len(blind) > 20, (
        f"only {len(blind)} extensionless tracked paths — if that collapsed, "
        "the derivation stopped seeing them and this whole class went dark")

    for path in ("Makefile", "tests/rulepacks/vm_engine_version"):
        assert path in _tracked(), f"{path} moved; re-point this fixture"
        assert _extension_token().findall(path) == [], (
            f"{path} should be invisible to the extension-anchored pattern")
        assert path in _tokens(path), (
            f"{path} must still be tokenised by the extensionless pattern")

    target = "tests/rulepacks/vm_engine_version"
    head, tail = target[:16], target[16:]
    wrapped = f"# see {head}\n# {tail} here\n"
    assert [tok for _, tok in _wrapped_references(wrapped)] == [target], (
        "a wrapped extensionless path must be reported")
    contiguous = f"# see {target} here\n# and more\n"
    assert _wrapped_references(contiguous) == [], (
        "a contiguous extensionless path must NOT be reported")

    # ⛔ The BARE-name half of `_extensionless_token`, which mutation testing
    # showed was inert: deleting it changed nothing, because every case above
    # uses a full path. It is kept for symmetry with the extension half (which
    # accepts unique basenames via `_resolves`), so it needs a case that can
    # tell the difference — otherwise it is a branch nobody would notice going.
    bare = sorted(
        name for name, path in _unique_basenames().items()
        if not _extension_token().fullmatch(path)
        and name != path  # root dotfiles are already covered as full paths
    )
    assert bare, (
        "no repo-unique extensionless basename qualifies any more, so the "
        "bare-name half of `_extensionless_token` has no subject left. "
        "⚠️ That population is ONE member deep today, so the usual trigger is "
        "somebody adding a second file with that basename — which says nothing "
        "about whether the half earns its place. ⛔ Two legitimate exits, and "
        "deleting this block is neither: re-derive a subject if the tree still "
        "has one, or — if it genuinely has none — say so HERE, in place, "
        "because this block is the only case that drives that half and a quiet "
        "deletion is the half going dark.")
    subject = bare[0]
    split = max(1, len(subject) // 2)
    hits = _wrapped_references(
        f"# see {subject[:split]}\n# {subject[split:]} here\n")
    assert [tok for _, tok in hits] == [subject], (
        f"a wrapped bare extensionless name ({subject}) must be reported")

    # ⛔ The optional directory PREFIX in `_extensionless_token`, which is what
    # makes it symmetric with the extension half. Without it the lookbehind's
    # `/` kills a prefixed spelling before it is ever a token, so #1452's class
    # stays open for all of these files — and this very path is the live
    # violation `ci.yml` was carrying. Mutation testing put this case here: with
    # only the two cases above, deleting the prefix left the module green.
    name = target.rsplit("/", 1)[-1]
    partial = target.split("/", 1)[1]
    assert name in _unique_basenames() and partial not in _tracked(), (
        "this case needs a PARTIAL spelling whose basename still resolves; "
        f"{target} no longer provides one")
    cut = max(1, len(name) // 2)
    prefixed = _wrapped_references(
        f"# see {partial[:len(partial) - len(name) + cut]}\n"
        f"# {name[cut:]} here\n")
    assert [tok for _, tok in prefixed] == [partial], (
        f"a wrapped PARTIAL spelling of an extensionless path ({partial}) must "
        f"be reported, got {prefixed}")


def test_tracked_split_refuses_a_narrowed_listing() -> None:
    """The floor and the NUL cross-check, driven directly.

    ⛔ Inline over the real listing neither can fail, so deleting them is
    invisible. `_split_listing` is pure precisely so they can be driven.
    """
    blob = "\0".join(f"zdir/zfile{i}.txt" for i in range(2000))
    assert len(_split_listing(blob)) == 2000

    # the half-fix: `-z` requested, newline split kept -> ONE element
    with pytest.raises(AssertionError, match="plausible"):
        _split_listing(blob.replace("\0", "\n"))
    with pytest.raises(AssertionError, match="plausible"):
        _split_listing("")

    # ⛔ The NUL cross-check needs its OWN case. Both inputs above trip the
    # >=_MIN_TRACKED_FILES floor first, so the separator assert never ran and
    # replacing it with `assert True` left this test green (blind review).
    # Drive it with enough paths to clear the floor AND more separators than
    # survivors — doubled NULs, the shape a sloppy join produces.
    doubled = "\0\0".join(f"zdir/zfile{i}.txt" for i in range(2000))
    with pytest.raises(AssertionError, match="NUL separators"):
        _split_listing(doubled)


def test_decoded_shortfall_refuses_a_truncated_corpus() -> None:
    """The corpus floor, driven directly.

    Over the real tree it can never fail, so nothing distinguishes "the check
    is here" from "the check was deleted" — the reason it is a pure function.
    """
    assert _decoded_shortfall(1_000_000, 1_000_000) is None   # ASCII
    assert _decoded_shortfall(400_000, 1_000_000) is None     # dense CJK
    assert "arithmetic floor" in (_decoded_shortfall(4096, 1_000_000) or "")


def test_partial_read_is_refused() -> None:
    """The content-length check, driven directly.

    ⛔ Over real files it can never fail, so nothing distinguishes "the check
    is here" from "the check was deleted" — the same reason `_split_listing`
    is a separate function.
    """
    body = b"zline-one\nzline-two\n"
    assert _decode_whole(body, len(body), "zfake.txt").endswith("two\n")
    with pytest.raises(AssertionError, match="byte\\(s\\) of a"):
        _decode_whole(body[:5], len(body), "zfake.txt")


def test_tracked_set_matches_an_independent_git_listing() -> None:
    """⛔ The floor has ~1300 files of headroom, so it cannot see a narrowing
    that stays above it — blind review dropped every `.md` (309 files, the
    exact surface this scan exists for: CHANGELOG.md alone carries 631 distinct
    resolvable references) and the whole module stayed green, because
    `_unread_drift` compares the scan against the SAME narrowed sequence and
    so agrees with itself.

    Independent BY CONSTRUCTION: its own argv, its own split.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
        stdin=subprocess.DEVNULL, text=True, check=True, timeout=120).stdout
    expected = {p for p in out.split("\0") if p}
    actual = set(_tracked())
    assert actual == expected, (
        f"`_tracked()` is not `git ls-files -z`: {len(actual)} path(s) "
        f"against {len(expected)}. Every file missing here is a file this "
        "guard silently never scans.\n"
        f"    dropped: {sorted(expected - actual)[:10]}\n"
        f"    extra:   {sorted(actual - expected)[:10]}")


def test_tracked_is_never_narrowed_at_a_use_site() -> None:
    """The same disarm one frame out, where the in-helper checks cannot see it.

    ⚠️ Same honest bound as the sibling module's copy: direct subscript, a
    local bound from the call, and a wrapper call are recognised; `islice`
    and filtering comprehensions are not.
    """
    tree = ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))

    def _is_call(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_tracked")

    bound = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and _is_call(node.value)
        for target in node.targets if isinstance(target, ast.Name)
    }
    sliced = sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and (_is_call(node.value)
             or (isinstance(node.value, ast.Name) and node.value.id in bound)
             or (isinstance(node.value, ast.Call)
                 and any(_is_call(a) for a in node.value.args)))
    )
    assert not sliced, (
        f"`_tracked()` is subscripted at line(s) {sliced}. The scan then "
        "covers a SUBSET while `_unread_drift` compares against the same "
        "subset and reports nothing skipped. Iterate the whole set.")


def test_each_tripwire_fires_on_degenerate_input() -> None:
    """The three tripwires above all guard conditions that are currently FALSE.

    Without this test, deleting any of them is invisible: no input distinguishes
    "checks and passes" from "does not check". Mutation testing proved it — all
    three survived until they were given a case that can tell the difference.
    """
    target = "tests/ops/test_wrapped_path_references.py"
    # ⛔ The same premise guard its three siblings carry. Without it, renaming
    # this module makes the assertion below report that the DETECTOR is broken,
    # and the cheapest way to believe that message is to loosen the detector.
    assert target in _tracked(), "this module moved; re-point the fixture"
    head, tail = target[:24], target[24:]
    wrapped = f"# see {head}\n# {tail} for the rule\n"
    assert _offenders([("fake.py", wrapped)]), (
        "the offender aggregation reported nothing for a file that plainly "
        "contains a wrapped reference")
    assert _offenders([("fake.py", "# nothing to see here\n# at all\n")]) == {}

    assert _unread_drift(set()), "reading nothing at all must be drift"
    # ⛔ A literal, NOT a slice of `_tracked()`: a degenerate input taken from
    # the thing it protects moves with it, and slicing the sweep at a use site
    # is itself the disarm `test_tracked_is_never_narrowed_at_a_use_site`
    # exists to refuse.
    assert _unread_drift({"README.md"}), (
        "reading only a handful of tracked files must be drift")
    assert _unread_drift(set(_tracked())) == []

    assert _coverage_shortfalls([]), "an empty scan must be a shortfall"
    # 4 = files / tokens / resolving / decoded-corpus. The last one guards the
    # CONSUMED corpus rather than the bytes fed to the decoder; adding it moved
    # this count, which is exactly what this assertion is for.
    assert len(_coverage_shortfalls([])) == 4, (
        "every floor must report independently, so one collapsing does not hide "
        "the others")

    # ⛔ AND ONE NON-EMPTY CASE, which is what pins the floors' VALUES. With `[]`
    # every floor fires for free (`0 <= 0`), so the empty case cannot tell a
    # floor of 1500 from a floor of 0 — blind review set all three to zero and
    # the whole module stayed green. 200 files carrying one resolvable
    # reference between them is still degenerate against a tree of 2315, so
    # every floor must still speak.
    degenerate = [(f"f{i}.py", "see " + target + " for the rule\n")
                  for i in range(200)]
    assert len(_coverage_shortfalls(degenerate)) == 4, (
        "a 200-file scan of a 2000+ file repo must still trip all four floors. "
        "If it does not, one of them has been lowered until this scan no "
        "longer falls below it — ⛔ raise it back; do not shrink this case.")
    # ⚠️ WHAT THIS PINS, exactly, because it is less than the sentence above
    # may suggest. Measured, one floor at a time:
    #   `_MIN_FILES`      >= 200  (199 fails, 200 PASSES, 750 passes)
    #   `_MIN_TOKENS`     >= 200  (199 fails, 200 PASSES)
    #   `_MIN_RESOLVING`  >= 1 ONLY — this corpus carries ONE distinct resolving
    #                     reference, so `_MIN_RESOLVING = 1` passes. Raising
    #                     that bar means giving each synthetic file a different
    #                     real path, i.e. taking the case's discrimination from
    #                     the tree it protects; disclosed instead of taken.
    # `_MIN_FILES = 3` is the 500x weakening an earlier three-file version of
    # this case let through, and is the whole reason the corpus here is 200.
    # Gradual drift is not covered: pinning the literals against it needs a
    # second source of truth for how big the corpus ought to be, which is the
    # thing these floors are too crude to have. 200 is an independent constant,
    # deliberately NOT derived from `_tracked()`, because a floor taken from
    # the thing it protects is not a floor.


def test_identifier_tripwires_fire_on_degenerate_input(
        corpus: list[tuple[str, str]]) -> None:
    """Same shape as its path-side twin, and for the same reason: both the
    aggregation and the floor guard conditions that are currently FALSE, so
    deleting either is invisible without an input that can tell the difference.
    """
    inventory = _identifier_inventory(corpus)

    # ⛔ THE MUST-SUCCEED MEMBER, first. A group of only must-fail cases looks
    # exactly like a harness that never ran the thing under test.
    assert _identifier_shortfalls(corpus, inventory) == [], (
        "the real corpus must CLEAR the floor, otherwise every case below "
        "passes for the wrong reason")

    assert _identifier_shortfalls([], inventory), "an empty scan must be a shortfall"
    assert _identifier_shortfalls(corpus, {}), (
        "an empty inventory must be a shortfall — that is the `ast` walk having "
        "died, which is precisely the silent failure this floor exists for")

    # ⛔ AND A NON-EMPTY CASE, which is what pins the floor's VALUE. With `[]`
    # the floor fires for free at any setting, so the empty case cannot tell 250
    # from 0.
    sample = sorted(inventory)[:100]
    assert len(sample) == 100, "inventory too small to build this case"
    degenerate = [(f"synthetic{i}.py", " ".join(sample)) for i in range(3)]
    assert _identifier_shortfalls(degenerate, inventory), (
        "a corpus carrying 100 defined names must still trip the floor. If it "
        "does not, the floor has been lowered until this case no longer falls "
        "below it — ⛔ raise it back; do not shrink this case.")
    # ⚠️ WHAT THIS PINS, exactly: 100 <= `_MIN_IDENT_OFFSITE` < 1771, the upper
    # bound coming from the must-succeed member above. It does NOT pin the
    # inventory's correctness — the sample is drawn from the inventory, so a
    # broken inventory degenerates this case rather than failing it, which is
    # why the empty-inventory assertion above is separate. Disclosed rather
    # than fixed: making the case independent of the inventory means hard-coding
    # names, and a hard-coded name is the enumeration this module refuses.

    # ⛔ AND THE OFFSITE CONDITION ITSELF, which nothing above can see. A corpus
    # where every name appears ONLY in the file defining it has zero offsite
    # sightings and must trip the floor.
    # ⚠️ That this case is what catches it is measured, not assumed: delete the
    # `inventory[token] != path` test — reverting to counting sightings anywhere
    # — and with this case present the module goes red, while with this case
    # removed the same edit leaves all 15 tests passing. The reverted metric
    # cannot fall below ANY threshold, because it equals the inventory's size.
    by_definer: dict[str, list[str]] = {}
    for name, definer in inventory.items():
        by_definer.setdefault(definer, []).append(name)
    picked = list(by_definer.items())[:400]
    carried = sum(len(names) for _, names in picked)
    assert carried > _MIN_IDENT_OFFSITE, (
        f"this case carries {carried} name(s) and needs more than "
        f"{_MIN_IDENT_OFFSITE} to tell the two metrics apart")
    onsite_only = [(definer, " ".join(names)) for definer, names in picked]
    assert _identifier_shortfalls(onsite_only, inventory), (
        "a corpus in which every name is seen only where it is defined has ZERO "
        "offsite sightings and must trip the floor. If it does not, the floor "
        "is counting sightings anywhere — the inventory reciting itself.")

    probe = "_MIN_IDENT_OFFSITE"
    assert probe in inventory, "re-derive a subject; this one moved"
    head, tail = probe[:8], probe[8:]
    assert _identifier_offenders(
        [("fake.py", f"# see {head}\n# {tail} onwards\n")], inventory), (
        "the offender aggregation reported nothing for a file that plainly "
        "contains a wrapped identifier")
    assert _identifier_offenders(
        [("fake.py", "# nothing to see here\n# at all\n")], inventory) == {}
