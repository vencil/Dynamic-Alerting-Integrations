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

⛔ The metric is defined HERE, not in a ticket. Four different counts of "how
many wrapped references exist" were produced while scoping this (16 / 28 / 35 /
44) — every one honest, every one measuring something slightly different
(full-path only, plus bare names, plus more extensions, minus a regex bug).
A number that needs its definition restated to mean anything is not a baseline.
Since the tree is now at zero, the definition below IS the number.

⛔ A path split across two STRING LITERALS is invisible to the rejoin above,
because the quotes sit between the halves and break the token —

    "# ... （components/threshold-exporter/",
    "# app/pkg/config/resolve.go）。",

⚠️ HALF of that class is now guarded, and which half matters. When the two
literals are IMPLICITLY CONCATENATED — one string written across lines, no comma
— `test_no_reference_is_split_across_implicit_concatenation` catches it, because
Python's parser has already joined them. The illustration just above is the
OTHER half: comma-separated, so it is two strings and stays silent. It is still
here, still a real tracked path, and still makes the point.

That is `scripts/tools/ops/_registry_lib.py`, which GENERATES the comment blocks
spliced into `rule-packs/*.yaml`. This guard saw the generated outputs and not
their source, so fixing only the outputs was silently reverted by
`check_threshold_registry.py --regen` — the repo's own staleness gate is what
caught it. If you are unwrapping a path and the gate keeps coming back, look
for a generator.

⛔ That class was swept to zero by hand once, is held there by NOTHING, and is
NO LONGER AT ZERO. The sweep: for each adjacent pair of lines where the first
ends in a quote and the second starts with one, splice them — ⚠️ stripping the
second literal's own comment marker, without which the token stays broken and
the sweep reports a clean tree it never actually looked at — and ask the same
question this guard asks. It found 7 in #1383; all 7 were fixed. Re-run for
#1452 — same answer on that PR's merge base and on its head, so it is a
property of the tree and not of one commit — **2**, one of them live. ⚠️ That
was #1452's tree; NOTHING re-checks it, so treat the two below as the shapes to
look for rather than as today's inventory —

  * `tests/ops/test_generated_ci_artifacts.py` splits
    `tools/portal/…/cicd-setup-wizard/utils/generators.js` across two string
    literals. `git grep` on the whole path returns that file at its OTHER,
    contiguous mention and NOT at the split one, so a rename sweep working from
    `grep -n` fixes one and leaves the other. #1373 verbatim.
  * the KNOWN GAP illustration in this very docstring — it uses a REAL tracked
    path and stays silent because it is COMMA-SEPARATED, which is two strings
    rather than one written across lines. Contrast the top-of-file illustration,
    which uses a path that does not exist for exactly this reason.
  ⚠️ Both were cited by absolute line number until #1453. Measured at that
  point: two of the three numbers had rotted and the third still happened to be
  right. ⛔ The corrected values are deliberately NOT repeated here, because the
  first draft of this paragraph did repeat them and one had already gone stale
  by a line inside the commit that recorded it — moved by an unrelated hunk of
  that same commit, which also touched the file being cited. Cite the symbol,
  the paragraph, or the other mention — never the line.
  A line number that is correct today is not a different KIND of thing from one
  that has already rotted; it is the same thing earlier.

The mechanism is the TOKENISER (quotes break the token before the resolver is
reached), not the resolver that #1452 is about. The implicit-concatenation half
is now guarded (#1394); the first of the two above was a live defect and is
reflowed. What was fixed EARLIER in this paragraph is the sentence that asserted
zero while two counter-examples sat in the tree — the same sentence-shape #1452
exists to punish, a few lines above the one it punished.

What is deliberately NOT modelled (under-detection, the safe direction):
  * a token split across two COMMA-SEPARATED string literals — that is two
    strings, not one written across lines, and joining them is what arms a
    check over every list of paths in the repo (measured on #1394);
  * a token split across THREE or more lines — the window is two lines.
    Measured when this bullet was written, and nothing re-checks it: a
    three-line window added no reports, so the gap was structural and empty
    rather than a backlog;
  * a reference assembled from variables, or reached through a glob;
  * a comment marker outside the five `LINE_PREFIX` knows (`>` blockquote,
    `::`/`REM`, `%`, `!`) — blind review demonstrated every one of them.
    Measured the same way, and equally unchecked since: teaching `LINE_PREFIX`
    all five added no reports;
  * a PREFIXED spelling — relative (`./x`, `../x`), anchored (`/x`, `~/x`) or
    partial (`pkg/config/x`) — whose basename does not clear the bare-name
    bounds three lines down. Since #1452 the basename is asked when the whole
    token resolves to nothing, so `../check_pint.py` is caught; `./__init__.py`
    (under `_MIN_BARE_NAME`), `./.claudeignore` (no separator) and
    `./_defaults.yaml` (not repo-unique) are not. ⚠️ Measured, because the first
    wording of this bullet named only non-uniqueness and used `./utils.py` as
    its example — and `utils.py` names no file in this repo at all, so it was
    excluded for a different reason than the one given. EACH of the two bounds
    excludes repo-unique names on its own, the separator bound the larger share
    (see COUNTS);
  * one of OUR paths cited with EXTRA leading segments — the
    `https://github.com/vencil/…/blob/main/<our path>` form, and `../blob/main/…`
    beside it. The basename fallback requires a SUFFIX-compatible spelling, and
    a URL is longer than the path it ends with, so it stays silent. Measured
    when #1452 landed: many such tokens, none of them wrapped (see COUNTS).
    ⛔ Accepting the other direction (token longer, ending in our whole
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
    containing neither `_` nor `-` — that last condition excludes otherwise-
    unique names on its own, and all three are here to keep generic ones out of
    the scan: `README.md` and `values.yaml` each name many tracked files, while
    `index.md` and `resolve.go` are unique but fall under the floor. ⚠️ Pick
    those examples from the tree: two earlier wordings of this bullet named
    `utils.py` and then `config.py`, and NEITHER names a file in this repo, so
    both were illustrating a bound with something the bound never touched;
  * binary content is read too, decoded with replacement — nothing is
    skipped, because silently dropping input is how a scan reports "clean"
    while never having looked (and pinning WHICH files get dropped turned out
    to be environment-dependent; see the note by `_read_tracked`).

⛔ AND THE SIBLING CLASS THIS GUARD DOES NOT COVER: a wrapped IDENTIFIER. The
accident is the same one — #1373 was a rename sweep, and a sweep greps for a
symbol as readily as for a path — but a symbol carries no slash and no
extension, so nothing here can resolve one. Measured while #1453 was scoped:
20 such references were live in this tree (that ticket's PR reflowed all 20),
and the same scan run against the tree six days earlier reports 14, a strict
subset — so the class REGENERATES rather than being a backlog.

⛔ A scan for it was built and then WITHDRAWN, and the reason is a property of
the problem rather than of the implementation: any resolver for a symbol has to
consult a GLOBAL set of names, so its verdict is not attributable to the change
that produced it. Blind review demonstrated that end to end — an ordinary new
helper in one file turned an untouched, entirely correct comment in another file
red — and the cheapest way back to green is to rename your own function, which
leaves a clean diff, passes review, and records nothing about why the name got
worse. A guard that rewards that edit is worse than no guard.

Three predicates were tried, each measured, each failed differently. The counts,
the definitions they were measured under, and the nine latent names are on #1453
rather than here. ⚠️ That ticket carries NO runnable command — an earlier
wording of this sentence promised "reproductions", and a re-attempt in fact
starts by rebuilding the scan from those written definitions:
  * also accepting any name that occurs contiguously in the tree reaches the
    non-Python definition side and finds 8 further REAL references there — but
    it is self-contaminating: writing prose about the guard changes what the
    guard reports, demonstrated by an earlier draft of this very file;
  * scoping to the change's own diff separates nothing — the true positive and
    the false positive have the same shape under it (symbol in the diff, carrier
    file not), so one variant deletes the detection and the other keeps the
    false positive;
  * a minimum-length bound only moves the threshold: the demonstrated false
    positive is 21 characters, and the bound can be raised far enough to drop
    real references while every test stays green.
⚠️ Do not read this as "identifiers are fine". They are unguarded, the class is
growing, and #1453 holds the measurements anyone re-attempting it should start
from — including the shapes that make it hard, which is the expensive part.

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

⛔ COUNTS. This module does not say how many of anything the tree holds TODAY.
A count that survives here is ANCHORED to the commit or ticket it was taken on
("counted then", "measured on #1499") — a sentence about the past cannot rot,
because it was never about now. A count about now belongs in an assertion,
where it goes red instead of going quietly wrong. Where an old number was worth
keeping, the fix applied on #1404 was to re-anchor it, not to delete it; where
it was decorative, it went. ⚠️ The deleted ones are not lost: `git log -S<the
number>` on the paragraph that used to carry it is the way back. `(see COUNTS)`
here means "a number stood at this spot and was removed on purpose".

⛔ The sibling module reached this rule first and for the same reason — see its
`Deliberately NO counts here` note, whose three numbers all drifted within two
days. This paragraph is that decision arriving here, not a new one.

Three findings from #1404 are why. First, being RIGHT is not evidence of being
guarded: an AST sweep of every assert in this module finds only structural
floors and synthetic-corpus constants, so NOT ONE prose count here had an
assertion behind it — including those that still happened to be correct, which
had simply not moved yet. Second, most had already rotted, and one — a share of
"the path-like tokens" — is not reproduced by ANY reading of the phrase it
used, so these are not merely stale. Third: one of them was never well defined
at all. How many files a 4096-byte slice truncates differs between two
checkouts of the SAME commit, because `.gitattributes` puts the visual
snapshots behind Git LFS and an unsmudged pointer fits inside the slice while
the real PNG does not. The difference is only a handful of files, so this is
the rarest of the three rather than the largest — but it is the only one no
re-measurement can fix, because a count over file CONTENTS in this repo is not
a property of the commit at all.

⛔ THIS MODULE IS NOT CLEARED OF THEM, and the class is not only digits: a
present-tense claim carrying no number at all ("the suite stays green") rots the
same way and is harder to find, because nothing about it looks like a count.
Every sweep for this class so far has missed instances — the misses and how each
sweep was blind are recorded in the commit messages on #1404, which is where
that archaeology belongs. What matters here is the scanning rule it cost: REJOIN
ADJACENT LINES BEFORE SCANNING THIS FILE FOR ANYTHING, PROSE INCLUDED —
docstrings as much as comments, and most of this module's prose is docstring. A
line-by-line grep cannot see a claim whose own text wraps, which is this guard's
subject applied to its own audit.

⚠️ So do not restore a count to "complete" a sentence that reads vague. The
vagueness is the fix. If the number is load-bearing, put it in an assertion —
and if you cannot write that assertion, that is the finding, not the number.
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
# rather than guessed: the tails longer than it are the dotfile family
# (`gitattributes`, `trufflehogignore`, `dockerignore`, `doclinkignore`,
# `claudeignore`, `helmignore`, `gitignore`, `nojekyll`, `gitkeep`, `example`),
# and every one of them is a REAL extension — not the dotted version fragment an
# earlier wording claimed. Nothing is lost: a path the extension pattern cannot
# fullmatch is exactly what `_extensionless_token` takes, so those files are
# tokenised there.
#
# ⛔ THIS BOUND IS NOT GUARDED, deliberately, and that is measured rather than
# assumed. #1566 lists it as one of three unguarded properties, having shown
# that raising it to 20 leaves the whole module green. Asked the other way —
# what does raising it actually DO — the answer is nothing this guard can see:
# the dotfile family moves out of `_extensionless_token` and into the extension
# pattern, and the set of tokens the scan RESOLVES over the whole tree is
# identical either way. A bound with no detection consequence has nothing to
# assert about, and an assertion pinning its VALUE would only pin today's
# choice. ⚠️ If you change it, re-run that comparison rather than trusting this
# note. The other two properties #1566 names DID buy something and are pinned,
# by `test_extensions_are_derived_and_longest_first` and
# `test_the_reported_line_is_the_line_of_the_break`.
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

    ⛔ `Makefile`, every `Dockerfile`, `LICENSE`, every `.gitignore`-family file,
    `components/da-tools/app/VERSION`, `scripts/hooks/commit-msg`,
    `tests/rulepacks/vm_engine_version` — the tracked files whose basename has no
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
        # every tracked file with no usable extension, including the very file
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
# review demonstrated all of them; none occurred in the 46 wraps #1383
# flattened, but the gap is real, so it is named rather than implied.
LINE_PREFIX = re.compile(r"^[ \t]*(?:#|//|\*|--|;)?[ \t]*")

# A bare filename counts as a reference only if it is unambiguous, at least this
# long, and carries a separator. What each bound EXCLUDES is measured over the
# tree: uniqueness drops `README.md`, `_defaults.yaml` and `values.yaml`, each
# of which names many tracked paths; the length/separator floor drops a further
# set of otherwise-unique basenames, `index.md` and `resolve.go` among them.
# (Counts omitted deliberately — see COUNTS in the module docstring.)
# ⚠️ That is not the same as "measured to earn their place". Relaxing any one of
# them — or all three at once — does not change what this guard reports: zero on
# #1452's head, and the same two references on its merge base, where those two
# were still live. ⛔ Those MEASUREMENTS were re-anchored on #1404 for the reason
# COUNTS gives; the bound itself came from #1383 and has never moved. They keep
# generic names out of the scan, and that value is prospective, not demonstrated.
_MIN_BARE_NAME = 12

# Coverage floors — they answer "did the scan run at all?", nothing finer.
# ⛔ Do NOT read them as protecting the token model: blind review cut
# `_extensions()` down to a SINGLE extension and every floor still passed —
# the token floor sits an order of magnitude below what this tree produces, so
# it has nothing to say about the model. (The multiplier that used to stand here
# had drifted; see COUNTS.) What holds the
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

# ⛔ THE #1394 HALF HAS NO CORPUS FLOOR, AND THE ABSENCE IS A DECISION.
# Three designs were tried here across three rounds of blind review and all
# three were removed; the reasoning and the measurements live next to the scan
# in `test_no_reference_is_split_across_implicit_concatenation`, because that is
# where anyone tempted to add a fourth will be standing.
# ⚠️ The one thing worth keeping HERE, because it is about this constants block
# rather than that call site: the first attempt counted "resolving path tokens
# inside multi-line constants", which is a PARALLEL REWRITE of the check rather
# than the check — neutering the check's verdict left its reading completely
# unmoved. That failure mode is not about floors; it recurs whenever a meter is
# derived by re-walking the same structure the judgement walks.

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

    ⚠️ WHAT THAT FILTER COSTS AND BUYS. Occurrences, over every tracked file.
    ⛔ These are a HISTORICAL measurement, taken on #1499 — its merge base (where
    the two live defects still sat) and its head — and they are labelled that way
    on purpose: an earlier wording said "on this branch", which stopped naming
    anything the day that branch merged. Do not re-measure them into the present
    tense; see COUNTS. On #1499's merge base, as shipped the filter removes ZERO
    reports — nothing wrapped there misattributes — and its effect is only
    visible once `base not in raw` is removed as well, where it takes 322 reports
    down to 20. The material is real even though none of it is wrapped: on
    #1499's head, 276 distinct prefixed tokens carry one of our basenames without
    being a spelling of our path (269 on its merge base), of which 156 name
    something else (151) and 120 are our own file cited with EXTRA leading
    segments (118) — `…/blob/main/<our path>`, which this filter silences too.
    That half is a gap and it is in the gap list.

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
    finds it and the break hides nothing. Measured on #1499, dropping it costs 18
    extra reports: 2 → 20 on its merge base, 0 → 18 on its head. Seventeen of the 18 are
    joins like `//git_shell.go` and `/tenant_custom_alerts.go`, where a comment
    marker or a bare slash is all that was glued on; the eighteenth is a real
    partial path, correctly let through because its basename is right there in
    the window.

    ⚠️ THIS GATE HAS A REAL GAP AND IT WAS DELIBERATELY LEFT OPEN (#1579).
    A RELATIVE spelling of our own path — `../<tracked path>`, and equally
    `./`, `../../`, `/`, `//` — is not itself a tracked path, so it arrives
    here rather than at `_resolves`; but it CONTAINS the real path, so when the
    break falls inside it a full-path `git grep` loses the site while the
    basename stays greppable and this gate stays silent. The gap is real.

    ⛔ Closing it was implemented, measured and WITHDRAWN (#1579, PR #1644).
    Yielding the gate when the rejoined window spells the real path reports
    zero extra sites on this tree, and arms a false-positive surface of several
    hundred contiguous occurrences, concentrated in `docs/`, `README` and
    `Makefile` — the prose that gets reflowed most. It fires on two adjacent
    Markdown list items (a directory, then a file), which is the SECOND false
    positive class this module's own docstring already lists; and the cheapest
    ways back to green are one-character edits — change the bullet marker, add
    a backtick, add a trailing space — each of which also silences a real
    defect. A gate that teaches that edit is worse than the gap it closes.

    ⛔ THE SIZE OF THAT SURFACE HAS NO MEANING WITHOUT ITS DEFINITION, so the
    definition travels with it. Counting contiguous tokens whose `.`/`..`/empty
    segments strip to a tracked path OF AT LEAST TWO SEGMENTS gives 551
    occurrences / 329 distinct tokens / 187 files (`cefb5652`; byte-identical
    on `b9f13937` and `d4135983`, so this surface is not moving). Seven other
    plausible readings of "the same" surface give, ON THAT SAME TREE: 393, 624,
    627, 2237 and 3213 occurrences (dropping the two-segment bound; restricting
    to extension-anchored tokens; going through `_unique_basenames` rather than
    the tracked set; accepting a SUFFIX rather than the whole path), plus 465
    LINES and 500 (file, token) PAIRS — ⚠️ different units, listed here so the
    range is visible, NOT so the numbers can be compared with each other.
    ⚠️ An earlier wording gave 545 / 326 / 189 with no definition attached;
    none of the eight readings above reproduces it, and the nearest is 1.1%
    away. ⛔ So do not re-derive this from `../` and `./` alone either: the
    first attempt did, reported 97, and was low by more than fourfold because
    `_is_a_spelling_of` drops EVERY `.`/`..`/empty segment, so every deeper
    spelling is admitted too.
    """
    base = token.rsplit("/", 1)[-1]
    if base == token:
        # ⛔ Equivalent to falling through ONLY because the sole caller asks
        # `_resolves` first, and a bare token that resolves is reported there.
        # Measured over a full scan on #1499 (1181 invocations, 593 of them
        # bare), removing this line changed NOTHING — every bare token that gets here
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


def _implicit_concat_references(text: str, path: str = "<synthetic>") -> list[tuple[int, str]]:
    """The sibling class `_wrapped_references` cannot see: a path split across
    IMPLICIT STRING CONCATENATION (#1394).

    ⛔ The quotes sit between the halves, so the two-line rejoin above produces a
    broken token and the reference is invisible to it AND to `git grep`. That is
    not hypothetical: the one live instance was an assert message split for line
    width, and a rename sweep found this same file's OTHER, contiguous mention
    and left the split one pointing at nothing.

    DERIVED, and that is the whole reason this is tractable. Python's parser
    already joins implicitly concatenated literals into ONE `ast.Constant`, so
    the question is asked of the constant's VALUE and compared against the raw
    source of the LINES it sits on. Nothing here guesses at quote, comma or
    continuation shapes.

    ⚠️ THE AXIS WAS CHOSEN BY MEASUREMENT, not taste. Asking the same question
    with a line-pair rule — first line ends in a quote, next starts with one, as
    #1394 proposed — joins every adjacent pair of string literals, i.e. every
    list of paths, and arms over a thousand tokens that nobody wrote. Narrowing
    THAT by carrier (both halves carry a comment marker) drops the armed set to
    single digits but also drops the real defect, keeping only a decorative
    example: narrowing the false positives was narrowing the coverage. Narrowing
    by token shape barely moves it, because the noise is genuinely deep paths.
    The counts are on #1394; what belongs here is the shape of the answer.

    ⚠️ Deliberately NOT modelled, each measured rather than assumed:
      * a comma-separated list of literals — that is not one string, and the
        illustration in this module's own KNOWN GAP paragraph is exactly that
        shape, which is why it is still there and still silent;
      * implicit concatenation that fits on ONE line (`"tests/ops/" "x.py"`).
        It is joined and it is ungreppable, so this is a real gap and not, as
        an earlier comment in the body claimed, a case where nothing was
        joined. Measured: modelling it adds no reports on this tree;
      * the basename fallback `_wrapped_references` applies. Adding it here
        changes nothing on this tree — same reports, same armed set — so the
        narrower predicate is kept and this note records that it was tried;
      * non-Python carriers. Go and JS concatenate with `+`, and this reads the
        Python AST. #1394's own inventory was entirely Python, but the gap is
        real and named rather than implied.
    """
    # ⛔ FAIL-CLOSED, and `filename` is not decoration: without it every parse
    # error reads `<unknown>, line 1` and the contributor is told a file broke
    # without being told WHICH — blind review measured the consequence, which is
    # that the cheapest way back to green is to wrap the CALL SITE in
    # `except SyntaxError: continue`. That is why there is exactly one call site
    # (`_implicit_concat_offenders`) and why the control drives THAT rather than
    # this function: a control that only proves this raises says nothing about
    # whether anybody still listens.
    # ⚠️ There is no per-file isolation: ONE unparseable tracked `.py` aborts the
    # whole scan. Today the tree has zero of those, but a deliberately broken
    # fixture and a BOM-prefixed file are both legal inputs, and every route
    # back to green from that traceback disarms something — #1632.
    # ⛔ `filename=` ALONE does not put the path in front of the reader, and the
    # axis is the PLATFORM, not the interpreter: `SyntaxError.__str__` trims the
    # filename at the platform separator (`\` on Windows, `/` on POSIX), so the
    # very same `zfake/broken.py` renders whole on a Windows box and as bare
    # `broken.py` on the Linux runner. Measured on one machine, both ways:
    #     3.14.3 and 3.13.12, `zfake/broken.py`  -> whole path (identical)
    #     3.14.3 and 3.13.12, `zfake\broken.py`  -> `broken.py`
    # ⚠️ An earlier version of this comment blamed the interpreter version. It
    # was wrong: host and runner differed in BOTH OS and version, and it named
    # the one that does not matter. Anyone reading it on Linux + 3.14 would have
    # expected containment to work, and it does not.
    # ⛔ So the path goes in the MESSAGE. It is written by MUTATING `msg` rather
    # than by constructing a new exception, because a fresh `SyntaxError(str)`
    # drops `filename`, `lineno`, `offset` and `text` to None — the structured
    # diagnostics this whole paragraph exists to protect.
    # ⚠️ Re-raising is not swallowing — the ban above is on `continue`.
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        # ⚠️ The ticket pointer is the only guidance a contributor gets
        # here. Blind review measured what its absence costs: a legitimately
        # unparseable `.py` (a linter fixture, a BOM'd file that runs fine)
        # turns this scan into a bare traceback, and every cheap way back to
        # green disarms something. #1632 is where that policy question lives.
        exc.msg = f"{path}: {exc.msg} (this file must parse for the #1394 scan; see #1632)"
        raise
    lines = text.split("\n")
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.end_lineno == node.lineno:
            # ⚠️ A real gap, not "nothing was joined" — see the NOT-modelled
            # bullet on same-line concatenation in this function's docstring.
            continue
        # ⛔ THE LINES THE CONSTANT SITS ON — deliberately NOT its exact source
        # span, and the granularity IS the predicate. The question this guard
        # asks is not "is the token inside this constant"; it is "does
        # `git grep <path>` return this site", because that is what a rename
        # sweep works from. Grep answers per LINE.
        #
        # ⛔ THIS IS AN APPROXIMATION OF THAT QUESTION, AND SO IS THE OBVIOUS
        # ALTERNATIVE. Both were measured against the exact criterion — a site
        # is hidden iff NO line grep returns is a line carrying one of the
        # fragments the token is split across, computed with `tokenize` rather
        # than guessed:
        #
        #   case                                    hidden  these lines  AST span
        #   3 fragments, neighbour on closing line    yes     silent✗     report✓
        #   2 fragments, neighbour on closing line    no      silent✓     report✗
        #   2 fragments, neighbour on opening line    no      silent✓     report✗
        #   plain split, no neighbour                 yes     report✓     report✓
        #
        # Narrowing to `ast.get_source_segment` was tried and reverted (#1608):
        # it is right on the first row and wrong on the next two, and its error
        # direction is a FALSE RED on a shipped idiom (an assert message
        # repeating the value it compares, wrapped at the column) whose cheapest
        # cure is deleting the repeated path from the diagnostic. This version's
        # error direction is a MISS. Both reported ZERO over every tracked `.py`
        # file when the choice was made, so it was made on error direction rather
        # than on yield. (The corpus size is deliberately not written down here:
        # it moves with every merge, and this sentence outlived `610` by two
        # within a day of being written.)
        # ⚠️ WHAT THIS LEAVES UNGUARDED, and it is the cost of that choice, not
        # a pre-existing limitation: a constant of THREE or more fragments whose
        # closing line carries a contiguous mention while the split sits in
        # earlier fragments. Grep sends the sweep to the closing line, the break
        # is above it, and this check is silent. The exact predicate above is
        # implementable (~25 lines, `tokenize` + fragment/value mapping) and was
        # prototyped; it needs its own handling for f-strings, so it is deferred
        # rather than half-built here — #1633 carries the prototype, the four
        # scored cases, and the three questions to settle before starting.
        raw = "\n".join(lines[node.lineno - 1:node.end_lineno])
        for token in sorted(_tokens(node.value)):
            # ⛔ `raw` is this constant's OWN lines, never the whole file. Blind
            # review measured what file-global costs: the live instance had a
            # contiguous mention of the same path elsewhere in the same file, so
            # `token in text` silences it — which is #1373's shape exactly, and
            # is the property `a split whose token appears elsewhere` pins. That
            # mention sits on a DIFFERENT line, which is exactly why grep sends
            # the sweep somewhere else and the split stays hidden.
            if token in raw or "/" not in token:
                continue
            if _resolves(token):
                found.append((node.lineno, token))
    return found


def _implicit_concat_offenders(files: list[tuple[str, str]]) -> dict[str, list[str]]:
    """path -> hits, for every file with a path split across concatenation.

    ⛔ The only call site of the check above that walks the corpus, on purpose. Iterating the corpus
    inline in the test put the parse behind a `for` loop a hurried contributor
    can wrap in `except SyntaxError: continue` — and the control at the time
    asserted only that the CHECK raises, so it stayed green through exactly that
    edit. Production and control now call the same name.
    """
    found: dict[str, list[str]] = {}
    for path, text in files:
        for line, token in _implicit_concat_references(text, path):
            found.setdefault(path, []).append(f"line {line} -> {token}")
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
    satisfies it and still drops most of the corpus. That is what this tripwire
    exists for. ⚠️ Blind review measured that edit on #1402, BEFORE this tripwire
    existed: every path survived, the floors that existed then all cleared, the
    run got 2.7x faster, and a wrapped reference injected past the cut went
    unseen. That is the argument FOR this tripwire, not a description of today.
    ⛔ Its margin is not asserted anywhere and it narrows as the tree gains small
    files, so re-measure rather than assume — that is the finding, not a number
    to write down here.

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
    sees content: `read_bytes()[:4096]` keeps EVERY path and satisfies all
    three, while truncating most files in the tree and dropping most of the
    corpus with them. ⚠️ If you re-measure that, count files by BYTES — the unit
    the slice cuts in; decoded characters answer a different question. An
    earlier wording gave both as absolute counts, and also gave a share of "the
    path-like tokens" that NEITHER reading of that phrase reproduces (see
    COUNTS). ⚠️ Measured on #1402, before this check existed: a wrapped reference
    injected past the cut was invisible while the suite stayed green, and the run
    got 2.4x faster, which is what makes the edit attractive. That is the
    argument FOR the assertion below, not a description of a live hole.

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


def test_no_reference_is_split_across_a_line_break() -> None:
    files = _read_tracked()

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
        "rejoining two lines below produces a token that names a real repo "
        "file, and that token does not appear in the raw text — so grepping it "
        "as written does not return this site.\n"
        "⚠️ FIRST DECIDE WHICH ONE YOU HAVE, because this guard cannot. If the "
        "thing named below is NOT a reference — a directory listing, two "
        "unrelated list items, a sentence whose last word ran into a filename — "
        "then there is nothing to reflow and the report is wrong. That is a "
        "defect in THIS GUARD, not in your change: this module has no per-line "
        "exemption on purpose, so raise it against the guard. The paragraph "
        "beginning `⚠️ FALSE POSITIVES THIS GUARD IS KNOWN TO PRODUCE` in this "
        "module's docstring names those three shapes and, for each, a rewrite "
        "that is harmless — and that fixes NOTHING, which is why it is there "
        "and not here.\n"
        "⛔ If it IS a reference: reflow so it sits on ONE line; the "
        "surrounding prose can wrap wherever you like. That is the only fix — "
        "going green any other way leaves it exactly as invisible as it is "
        "now.\n"
        "⚠️ And it restores exactly one property: the spelling AS WRITTEN "
        "becomes greppable. If the reference is a PARTIAL path, grepping the "
        "full tracked path still will not return this site — and it may return "
        "OTHER files, so a sweep reads a non-empty result as coverage it does "
        "not have. The reflow is the floor here, not the ceiling.\n"
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
    # keeps the guard off the great majority of innocent wraps in this tree
    # (see COUNTS); without it the cheapest way to go green is deleting the guard.
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
    version hand-listed 16 extensions and omitted `png`, which the tree does
    contain tracked files of.
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

    # ⛔ AND THE HALF THE NAME PROMISES BUT THE ABOVE CANNOT SHOW. Everything so
    # far is satisfied by a frozen tuple of today's extensions: the ordering
    # holds, the required members are all in it, and the spellings above still
    # match. #1566 measured exactly that — `return <today's tuple>` left this
    # module green — which is how a derivation quietly becomes a hand-list, the
    # failure the docstring above says it exists to prevent.
    #
    # Set equality against a SYNTHETIC tree is what no constant can satisfy:
    # neither extension below names anything in this repo, and the repeat proves
    # the set de-duplicates. ⚠️ `_tracked` is read from module globals at call
    # time, so swapping it here is enough. The cache_clear BEFORE the swap is
    # load-bearing — dropping it turns this assertion red. The three AFTER it
    # are not: blind review deleted all three and the module stayed green,
    # because `_extension_token` has already cached the real pattern by the time
    # the swap happens and nothing else calls `_extensions()` again. They are
    # kept as defence for the next caller, and this note says which half is
    # measured rather than claiming both are.
    fake_tree = ("zfake/one.zzzq", "zfake/two.qqq",
                 "zfake/three.qqq", "zfake/Makefile")
    real_tracked = globals()["_tracked"]
    _extensions.cache_clear()
    globals()["_tracked"] = lambda: fake_tree
    try:
        assert set(_extensions()) == {"zzzq", "qqq"}, (
            "_extensions() must be DERIVED from the tree it is given — it "
            "returned something other than the extensions of the synthetic "
            "listing, so it is not reading the tree at all")
    finally:
        globals()["_tracked"] = real_tracked
        _extensions.cache_clear()
        _extension_token.cache_clear()
        _extensionless_token.cache_clear()


def test_the_reported_line_is_the_line_of_the_break() -> None:
    """`_offenders` renders this as `line {line} → {token}`, and that string is
    the ONLY locating information a blocked contributor gets.

    ⛔ Nothing pinned it. Every other assertion over `_wrapped_references` in
    this module reads `[t for _, t in hits]` or compares against `[]`, so the
    first element of the tuple is discarded six times over and never checked
    once; #1566 measured the consequence — reporting `index + 2` instead of
    `index + 1` left the module fully green. An off-by-one here sends somebody
    to a line with nothing wrong on it while the suite says everything is fine.

    ⚠️ The expected number is CONSTRUCTED from the fixture — the break sits on
    the line after the padding — and not copied from what the function returned,
    which would only re-state today's behaviour including its bugs.

    ⛔ The subject is this module, and the premise guard below is not
    decoration. The first version of this test borrowed an unrelated tracked
    script as its subject (#1566 names it); blind review moved that script, and
    the test went red saying the line number was wrong when in fact the fixture
    had simply gone away — the exact message shape
    `test_each_tripwire_fires_on_degenerate_input` warns about, where the
    cheapest way to believe it is to loosen the detector. Every other test here
    whose subject is a tracked path carries this guard; this one shipped
    without it.

    ⚠️ That script is named on #1566 and not here: `verify_diff` maps a file to
    the tests that MENTION its path, so naming it here would keep sending its
    editors to this guard after the dependency is gone.
    """
    target = "tests/ops/test_wrapped_path_references.py"
    assert target in _tracked(), f"{target} moved; re-point this fixture"
    head, tail = target[:20], target[20:]

    for lead in (0, 4):
        text = "\n".join(["# padding"] * lead + [f"# see {head}", f"# {tail}", ""])
        hits = _wrapped_references(text)
        assert hits == [(lead + 1, target)], (
            f"with {lead} line(s) of padding the break is on line {lead + 1}, "
            f"so that is the line the contributor must be sent to; got {hits}")


def test_extensionless_paths_are_tokenised_too() -> None:
    """The class the first version could not see at all.

    More than twenty tracked files have no usable extension (`Makefile`, the
    `Dockerfile`s, `LICENSE`, the `.gitignore` family,
    `components/da-tools/app/VERSION`, `tests/rulepacks/vm_engine_version`, …) —
    stated as the bound the assertion below actually pins rather than as an
    exact count, which nothing here would keep honest (see COUNTS). An
    extension-anchored pattern never
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
    """⛔ The floor sits far below the size of the real tree, so it cannot see a
    narrowing that stays above it — blind review dropped every `.md`, the exact
    surface this scan exists for, and the whole module stayed green, because
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
    # ⛔ The same premise guard every tracked-path subject here carries. Without
    # it, renaming this module makes the assertion below report that the
    # DETECTOR is broken, and the cheapest way to believe that message is to
    # loosen the detector.
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
    # reference between them is still degenerate against a tree this size, so
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


def test_no_reference_is_split_across_implicit_concatenation() -> None:
    """The #1394 half. Same question, different join.

    ⛔ EVERY CASE BELOW PINS A PROPERTY A ONE-LINE EDIT WAS MEASURED TO SILENCE,
    and the live instance is what measured them: blind review weakened the check
    one clause at a time and re-asked whether it still saw that instance.

        `token in raw` -> `token in text`   defect silenced   -> "elsewhere" case
        the lines -> the AST span           FALSE REDS armed  -> boundary cases
        token predicate narrowed to `.py`   defect silenced   -> non-`.py` case
        skip constants inside an f-string   defect silenced   -> f-string case
        `ast.walk` -> `tree.body`           all verdicts gone -> plain case
        swallow SyntaxError inside the scan  file leaves silently -> corpus case
        drop the path from the message       reader loses the file -> fail-closed case
        drop entries from the corpus         nothing to find      -> twins case
        narrow the corpus at the use site    nothing to find      -> NOTHING; see
                                                                    the disclosure
                                                                    above the scan

    ⛔ WHAT IS STILL NOT GUARDED, measured rather than implied. Wrapping the
    call to the scan BELOW in `except SyntaxError: continue` passes: the
    `pytest.raises` case proves the function raises, which says nothing about
    whether this line still listens. The refactor to one call site moved that
    hole out a level, it did not close it — the same shape
    `test_tracked_is_never_narrowed_at_a_use_site` names for its own subject.
    ⚠️ Which assertion fires was read from the failing LINE NUMBER. The first
    attempt matched the assertion's own source text in the traceback, which
    `--tb=long` prints, and would have agreed with any outcome.
    """
    files = [(p, t) for p, t in _read_tracked() if p.endswith(".py")]

    subject = "tests/ops/test_wrapped_path_references.py"
    assert subject in _tracked(), f"{subject} moved; re-point this fixture"
    head, tail = subject[:24], subject[24:]

    # MUST REPORT: plain implicit concatenation.
    plain = 'x = (\n    "see %s"\n    "%s here"\n)\n' % (head, tail)
    assert _implicit_concat_references(plain) == [(2, subject)], (
        f"a path split across implicit concatenation must be reported; "
        f"got {_implicit_concat_references(plain)}")

    # MUST REPORT: the same split INSIDE an f-string — the carrier of the live
    # instance. Reading only plain literals silences that instance.
    fstring = 'x = (\n    f"{v} see %s"\n    "%s here"\n)\n' % (head, tail)
    assert [t for _, t in _implicit_concat_references(fstring)] == [subject], (
        "the live instance of this class was an f-string assert message; a "
        "check that only reads plain literals would have missed it: "
        + repr(_implicit_concat_references(fstring)))

    # MUST REPORT: a path that is NOT `.py`. The live instance was a `.js` path
    # while the other report-cases here use `.py`, so narrowing the predicate to it
    # silenced it and nothing noticed.
    # ⛔ It also spans THREE lines and carries TWO paths in one constant. Every
    # other case here is exactly two lines with one token, so "only judge a
    # two-line span" and "only judge the first token" were both silent — two
    # more one-line edits that reached the live instance.
    other = "docs/internal/dev-rules.md"
    assert other in _tracked(), f"{other} moved; re-point this fixture"
    mixed = ('x = (\n    "see %s"\n    "%s and also %s"\n    "%s here"\n)\n'
             % (other[:18], other[18:], subject[:24], subject[24:]))
    assert sorted(t for _, t in _implicit_concat_references(mixed)) == sorted(
        [other, subject]), (
        "the extension must not narrow the predicate (the live instance was a "
        "`.js` path), and neither the span nor the token index may: "
        + repr(_implicit_concat_references(mixed)))

    # MUST REPORT: a split whose token ALSO appears contiguously ELSEWHERE in
    # the same file. Comparing against the whole file instead of the constant's
    # own lines silences exactly this, and the live instance had precisely that
    # shape — the same path written contiguously earlier in the same file, which
    # is why the rename sweep fixed one site and left the other. ⚠️ Same
    # SYMPTOM as #1373, different mechanism (that one wrapped an identifier).
    elsewhere = ('OTHER = "%s"\nx = (\n    "see %s"\n    "%s here"\n)\n'
                 % (subject, head, tail))
    assert [t for _, t in _implicit_concat_references(elsewhere)] == [subject], (
        "a contiguous mention elsewhere in the file does not make the split one "
        "greppable; comparing against the file instead of the constant's lines "
        "is how a rename sweep reports a clean tree: "
        + repr(_implicit_concat_references(elsewhere)))

    # ⛔ MUST NOT REPORT: the same contiguous mention moved onto a BOUNDARY LINE
    # of the constant. This is the witness for the granularity, and it exists
    # because the opposite was shipped and reverted (#1608): a review asked for
    # the constant's exact `ast` span instead of the lines it sits on, the
    # request reads correct, and the two cases below are the reason it is not.
    # A constant's boundary lines ARE the lines carrying the split's halves, so
    # `git grep -n <path>` returns a line that carries the break — the sweep is
    # looking straight at it and nothing is hidden. Reporting it would be a
    # false red whose cheapest cure is deleting the repeated path from a
    # diagnostic, i.e. making the message worse.
    # ⚠️ Read this against the `elsewhere` case above, which MUST report: there
    # the contiguous mention is on a DIFFERENT line, so grep sends the sweep
    # somewhere else. Same shape, opposite verdict, and the deciding fact is
    # which LINE the mention is on — not whether it is inside the constant.
    opens_with = 'x = ["%s", "see %s"\n    "%s here"]\n' % (subject, head, tail)
    assert _implicit_concat_references(opens_with) == [], (
        "a contiguous mention on the line the constant OPENS on shares a line "
        "with the split, so grep returns it and there is nothing to fix: "
        + repr(_implicit_concat_references(opens_with)))

    closes_with = ('x = (\n    "see %s"\n    "%s here"), "%s"\n'
                   % (head, tail, subject))
    assert _implicit_concat_references(closes_with) == [], (
        "same on the line the constant CLOSES on — narrowing `raw` to the AST "
        "span reports this, and it is greppable: "
        + repr(_implicit_concat_references(closes_with)))

    # MUST NOT REPORT: contiguous. Nothing is hidden, so there is nothing to fix.
    whole = 'x = (\n    "see %s"\n    " here"\n)\n' % subject
    assert _implicit_concat_references(whole) == [], (
        "the path is contiguous in the source, so grep finds it: "
        + repr(_implicit_concat_references(whole)))

    # MUST NOT REPORT: a comma-separated list. Two literals are two strings, not
    # one written across lines — this is the documented boundary, and the
    # illustration in this module's KNOWN GAP paragraph has exactly this shape.
    listed = 'x = [\n    "see %s",\n    "%s here",\n]\n' % (head, tail)
    assert _implicit_concat_references(listed) == [], (
        "a comma-separated list is not implicit concatenation; reporting it "
        "would arm this check over every list of paths in the repo: "
        + repr(_implicit_concat_references(listed)))

    # ⛔ FAIL-CLOSED, driven through the SAME function production uses. Asserting
    # only that the check raises left the caller free to swallow it: blind review
    # wrapped the call sites in `except SyntaxError: continue` and the whole
    # module stayed green while an unparseable file left the scan silently.
    # ⚠️ THE PREFIX IS PINNED, not mere containment, and that is what makes this
    # case portable. `SyntaxError.__str__` trims the filename at the PLATFORM
    # separator, so `"zfake/broken.py" in str(...)` passed on a Windows host and
    # failed on the Linux runner — the verdict depended on where it ran, not on
    # the code. ⛔ Not on the interpreter VERSION: 3.13 and 3.14 were measured on
    # one machine and render identically. Requiring the message to START with
    # the path pins the mutation that puts it there, on every platform.
    # ⛔ THE TWO ASSERTIONS AFTER IT ARE NOT DECORATION. Blind review weakened
    # the first version of this fix one layer at a time and each of the four
    # weakenings below left the module green; three are pinned now, and the
    # measured mapping is one-to-one rather than the tidy story it first read as:
    #
    #   cut the message to the bare path      -> caught by the FIRST assertion
    #   drop `filename=` from `ast.parse`     -> caught by the structured fields
    #   replace the diagnosis, keep the prefix-> caught by the LAST assertion
    #   rebuild the exception from the tuple  -> NOT caught (see below)
    #
    # ⚠️ That last one is a disclosure, not a claim, and the reason is narrower
    # than an earlier version of this comment said. Nothing in this file asserts
    # on `__cause__` or `__context__` (measured, zero assertions). But rebuilding
    # the exception inside an `except` block does NOT lose the chain: implicit
    # chaining still sets `__context__` to the original, so the traceback still
    # prints it. What such an edit loses is nothing measurable here — which is
    # exactly why it is unpinned and why saying "it drops the chain" was wrong.
    with pytest.raises(SyntaxError) as parse_failure:
        _implicit_concat_offenders([("zfake/broken.py", "def (\n")])
    failure = parse_failure.value
    assert str(failure).startswith("zfake/broken.py: "), (
        "the error must name the file, and name it the same way on every "
        "platform. Without that the report is a bare `line 1` over hundreds of "
        "files, and the cheapest way back to green is to stop parsing rather "
        "than to fix the file: "
        + str(failure))
    # ⚠️ `filename` and `lineno` only. `offset` and `text` are NOT pinned —
    # nulling them was measured to leave this module green — so they are not
    # claimed here either; naming a field the assertion does not check is how a
    # message becomes a promise nothing keeps.
    assert failure.filename == "zfake/broken.py" and failure.lineno == 1, (
        "filename and lineno must survive. Building a NEW SyntaxError from a "
        "bare string drops them, and they are what an editor and a traceback "
        "read: "
        + repr((failure.filename, failure.lineno)))
    assert "invalid syntax" in str(failure), (
        "the prefix must not replace the diagnosis — a message that names the "
        "file but not what is wrong with it sends the reader back to square "
        "one: " + str(failure))

    # ⛔ MUST SCAN EVERY FILE IT IS GIVEN. Truncating the corpus — `files[:1]`,
    # an `islice` — is invisible to any count of FINDINGS, because that count
    # is zero either way; blind review made exactly that edit and nothing
    # moved. ⚠️ These names sit under `tests/ops/` on purpose: an earlier
    # version used `zfake/…`, which a filter keyed on a REAL directory walks
    # straight past — so the case claimed to cover filters and did not.
    twins = [(f"tests/ops/zfake_twin{i}.py", plain) for i in range(3)]
    assert sorted(_implicit_concat_offenders(twins)) == [
        "tests/ops/zfake_twin0.py", "tests/ops/zfake_twin1.py",
        "tests/ops/zfake_twin2.py"], (
        "the scan dropped entries from the corpus it was handed: "
        + repr(sorted(_implicit_concat_offenders(twins))))

    # ⛔ THERE IS NO CORPUS ANTI-VACUITY GUARD HERE, AND THAT IS A DECISION
    # WITH A COST — this paragraph is the disclosure, not an omission.
    #
    # Three designs were shipped and measured on this line over three rounds of
    # blind review, and each failed differently:
    #
    #   count floor `>= 450` over 612       let 26% of the corpus leave silently
    #                                       (`tests/ops/` dropped, `[:450]`)
    #   set equality vs the tracked set     vacuous under one ordinary edit —
    #                                       changing the suffix on BOTH sides
    #                                       scanned zero files, all green
    #   both together                       the floor's VALUE was watched by
    #                                       nothing (`450 -> 0` green), so it was
    #                                       decoration; and the equality's own
    #                                       deletion stayed invisible
    #
    # ⛔ AND THE PAIR ACTIVELY MISLED. Adding a legitimately unparseable `.py`
    # (a fixture for a linter's error path, or a BOM'd file that runs fine) makes
    # the scan below raise. The honest fix is to exclude that one file; the
    # equality's message said "⛔ Do not narrow this comparison ... widen the
    # corpus back", which is not a route back to green — so the next move it left
    # was to write the filter INSIDE the call expression, where neither guard can
    # see it. A message that routes an honest contributor into the one hole the
    # guard cannot see is worse than no message.
    #
    # ⇒ Removed. The judgement is this module's own: a guard is worth keeping
    # only when its silent failure lets the ORIGINAL defect back. A narrowed
    # corpus does not resurrect #1394 — it costs future coverage, which is what
    # this paragraph is for.
    # ⚠️ WHAT IS NOW UNGUARDED, so nobody has to re-derive it: narrowing the
    # corpus anywhere between the binding above and the call below, or inside
    # the call expression itself, is silent. `_unread_drift` still reports a
    # tracked file that was never read, which covers the accidental half.
    offenders = _implicit_concat_offenders(files)
    assert not offenders, (
        "a path is broken apart in the SOURCE — implicit concatenation, a "
        "backslash continuation, or an escape — while the runtime string is "
        "whole. `git grep` on the path does not return this site, so a rename "
        "sweep reports the tree clean (#1394).\n"
        "⚠️ FIRST DECIDE WHICH ONE YOU HAVE, because this check cannot. If the "
        "thing named below is NOT a reference to one of our files — a fixture "
        "holding somebody else's config, a string that merely happens to read "
        "like a path — then there is nothing to fix and the report is wrong. "
        "That is a defect in THIS CHECK, not in your change: there is no "
        "per-line exemption here either, so raise it against the check.\n"
        "⛔ If it IS a reference: put the whole path in ONE literal. The runtime "
        "string does not change, only where the source breaks. Do NOT split the "
        "literals with a comma — that changes what the code means and leaves "
        "the reference just as invisible.\n"
        + "\n".join(f"  {p}:\n    " + "\n    ".join(v)
                    for p, v in sorted(offenders.items())))
