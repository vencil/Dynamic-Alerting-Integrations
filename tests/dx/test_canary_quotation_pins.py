"""TRK-364 / #1557 — the sleep canary's INFORMATIONAL-ONLY sentence, pinned.

`scripts/tools/ops/bench-canary/canary_test.go` explains, in the package doc,
why `BenchmarkControlCanarySleep` must never gate. Several modules and
documents quote that one sentence to justify their own behaviour, and in the
adversarial blind-review rounds of #1439 / TRK-359 the quotation drifted
twice:

  1. **Unmarked elision, rewritten punctuation** — it was written as
     `... would flap INCONCLUSIVE constantly; NOT part of the gate decision`,
     while the source reads `constantly. Emitted + rendered for human eyes;
     NOT part of the gate decision.` A mechanical comparison confirmed the
     written string does not occur in the source at all.
  2. **Fixed in two places, missed in the third** — the next round caught the
     stale copy in the test file.

WHAT THIS DOES: `CITATIONS` names every place that quotes the sentence and
which of its words that place presents as the source's. Each is checked by
flattening comment syntax and line wrapping away and comparing verbatim. The
keystone ties `FULL` to the Go file by EQUALITY, and every other fragment is
a slice of `FULL`, so nothing here can drift or be quietly shortened without
a test going red.

⛔ WHAT THIS DELIBERATELY DOES NOT DO, AND WHY IT USED TO.
An earlier version of this file also patrolled: it scanned every tracked file
for phrases resembling the sentence, so that a NEW citation site could not be
added without being declared, and cross-checked every such hit inside the
declared files. That half is gone. It is worth knowing what it cost, because
the reasoning applies to the next guard someone is tempted to add here:

  - Every environment defect this module ever had came from it. It shelled
    out to `git`, and telling "no checkout" apart from "git failed for
    another reason" took three attempts, each of which introduced a new way
    to silently skip: too-broad exception handling, then a `.git` existence
    check that skipped where git would have worked, then a tracked-list check
    that a leaked `GIT_DIR` could turn into a silent skip in a real checkout.
  - It fired on ordinary English. Its trigger phrases (`NOT part of the gate
    decision`, `flap INCONCLUSIVE`, `rendered for human eyes`) are normal
    prose. Adding an unrelated line to `docs/internal/benchmark-playbook.md`
    about a different informational-only field turned CI red under a message
    about canary citations.
  - Almost none of it was exercised. A coverage audit mutated it line by
    line: removing either loop of its hit-scanner, or any single trigger
    phrase, left the suite green.

What it bought, in exchange, was one thing the pins below do not: noticing a
citation site nobody declared. That is now a human step, and this is the
command for it:

    git grep -inE "informational[ -]only" | grep -i canary

⇒ Losing it is a real loss and is stated as one. The pins still catch the
failure mode the ticket was filed for — a citation drifting away from the
source, including the "fixed two places, missed the third" shape, since every
declared site is checked independently. What is no longer caught is a
brand-new site that is never declared.

⛔ NO COUNTS IN THIS FILE'S PROSE. Across the review chain that produced it,
most of the prose findings were a number being wrong: how many citations, how
many mutations, how many characters a paraphrase kept. They came from a
harness outside the repository, so no committed command reproduced them and
every restatement was a fresh chance to be wrong. Do not reintroduce a number
here that no committed command prints.

⛔ COUNTERFACTUAL, measured before these pins existed. Drift shape 1 was
injected verbatim into `analyze_bench_history.py`'s citation (`constantly.
Emitted + rendered for human eyes; NOT part of the` → `constantly;`) and the
pre-existing gates — the whole of `tests/dx/` and `pre-commit` on that file —
came back green. So the detection here is new, not a second opinion.

⛔ INTENTIONAL-BREAK DOGFOOD. Every shape below was mutated and confirmed to
turn this module red: each citation in `CITATIONS`; a second copy of a pinned
fragment inside the same window; the source sentence reworded; the source
sentence duplicated; `FULL` shortened at either end.

Must-not-fire controls, confirmed to stay green: the same citation re-wrapped
at different columns; prose edited beside a citation; an unrelated file, and
an unrelated line elsewhere in an already-cited file, reusing the sentence's
ordinary English for a different subject.

⚠️ Two limits found by dogfooding these, both stated rather than patched:

  - Narrowing one of the `_slice` bounds leaves the suite green. A slice is
    always genuine source text, so this cannot make a pin claim something
    false — but nothing forces a slice to stay as WIDE as it is, so a pin can
    still be narrowed deliberately.
  - Reusing the sentence's ordinary English WITHIN `SPAN` of a citation's
    anchor turns that citation's pin red, because `_assert_unambiguous` then
    sees the fragment twice in one window. Measured: the same edit anywhere
    else in the same file stays green. That is the price of matching inside a
    character window, and it is now bounded to the citation's neighbourhood
    instead of the whole file.

⚠️ One asymmetry worth knowing before reading a red run: rewording the SOURCE
turns the keystone red and leaves the citation pins GREEN — the citations
still faithfully hold what the source used to say. That is the diagnosis, not
a gap.

⛔ SCOPE. This pins ONE sentence. The general "every quotation matches its
source" gate is TRK-365 and needs a citation convention designed first — and,
on the evidence above, needs a mechanism that understands citations rather
than one that greps for phrases.

⚠️ What these tests do NOT establish: that a citation is *appropriate*, that
its surrounding prose reads correctly, or that a paraphrase is faithful. They
establish that the substrings a citation presents as the source's words are
the source's words.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE_REL = "scripts/tools/ops/bench-canary/canary_test.go"

# ---------------------------------------------------------------------------
# The sentence
#
# ⛔ ONE literal, and every other fragment is a SLICE of it.
#
# The first version declared each fragment as its own string and the keystone
# asserted `fragment in source`. Blind review broke that: `in` survives
# SHORTENING, so a fragment could be quietly truncated to almost nothing and
# the whole suite stayed green (measured: `TAIL` cut down to its last clause,
# and `EYES` with one letter removed, both left 18 tests passing). A pin that
# can be silently weakened is not a pin.
#
# So the fragments are cut out of `FULL` by index. There is nothing left to
# truncate independently, and `FULL` itself is compared to the source by
# EQUALITY, not membership — see the keystone below.
# ---------------------------------------------------------------------------
FULL = (
    "INFORMATIONAL ONLY: a 3-4% drift here is only ~30-40us, well inside the "
    "jitter a virtualised GH runner shows even when healthy, so gating on it "
    "would flap INCONCLUSIVE constantly. Emitted + rendered for human eyes; "
    "NOT part of the gate decision."
)


# The two ends of the sentence, used to cut it out of the source file. They
# are the only strings compared against the source by anything other than
# equality, and they are as short as they can be while still being unique.
_FIRST = "INFORMATIONAL ONLY"
_LAST = "NOT part of the gate decision."


def _slice(first: str, last: str) -> str:
    """The stretch of `FULL` from `first` through the end of `last`."""
    i = FULL.index(first)
    j = FULL.index(last, i) + len(last)
    return FULL[i:j]


OPEN = _slice("INFORMATIONAL ONLY", "~30-40us")
TAIL = _slice("so gating", "gate decision.")
FLAP = _slice("gating on it", "constantly")
GATE = _slice("NOT part", "gate decision")
EYES = _slice("Emitted", "human eyes")
# ⚠️ Some sites lowercase the sentence's initial `Emitted` so the quotation
# reads as part of a sentence of their own — they are the ones whose `Cite`
# below carries `EYES_LC` or `EYES_GATE_LC`. That is a real deviation from
# verbatim, and it is left alone rather than "fixed": the pin simply starts
# after the adjusted word, so what it checks is still source text. One
# lowercased word is therefore NOT covered at those sites.
EYES_LC = _slice("rendered", "human eyes")
EYES_GATE_LC = _slice("rendered", "gate decision")


class Cite(NamedTuple):
    path: str
    anchor: str          # unique in its file; identifies WHICH citation
    segments: tuple      # ordered, each verbatim source text
    note: str


# ⛔ Derived by enumeration (see the module docstring), not by hand.
CITATIONS = (
    Cite("scripts/tools/dx/analyze_bench_history.py",
         "canary_test.go` says so in as many words",
         (OPEN, TAIL),
         "The `CANARY_BENCH` vs `CANARY_BENCHES` comment — the site that "
         "drifted first in #1439."),
    Cite("CHANGELOG.md",
         "原文逐字為",
         (OPEN, TAIL),
         "The #1439 entry that reproduces the sentence and marks its elision."),
    Cite("CHANGELOG.md",
         "漂移落在健康 runner 的抖動內",
         (FLAP, GATE),
         "A Chinese paraphrase built around verbatim fragments. ⚠️ Not named "
         "by the ticket; found by enumeration."),
    Cite("CHANGELOG.md",
         "Sleep 仍解析並顯示",
         (EYES_LC,),
         "⚠️ Another quoted fragment in that same entry, near enough to the "
         "previous anchor to fall inside its window yet not asserted by it — "
         "so it sat unpinned until a blind reviewer mutated it and the suite "
         "stayed green. Being in range is not the same as being checked. "
         "Lowercased `emitted`."),
    Cite("docs/internal/benchmark-playbook.md",
         "3-4% 漂移只有 ~30-40us",
         (FLAP, GATE),
         "The same paraphrase in the playbook. ⚠️ Not named by the ticket; "
         "found by enumeration."),
    Cite("scripts/tools/dx/paired_trend_watch.py",
         "canary_test.go` states it in as many words",
         (FULL,),
         "The only site that quotes the sentence unelided — so this is the "
         "pin that covers the middle clause."),
    Cite("scripts/tools/dx/paired_trend_watch.py",
         "says the sleep canary is",
         (EYES_GATE_LC,),
         "`informational_canary_pct`'s docstring. Lowercased `emitted`."),
    Cite("scripts/tools/dx/paired_trend_watch.py",
         "canary is informational, and `canary_test.go` says it is",
         (EYES_LC,),
         "The renderer's justification for having a second canary column. "
         "Lowercased `emitted`."),
    Cite("tests/dx/test_analyze_bench_history.py",
         "canary_test.go` calls it",
         (OPEN, TAIL),
         "`TestCanariesAreNeverJudgedAsProductBenchmarks` — the copy that was "
         "missed by the first fix in #1439."),
    Cite("tests/dx/test_paired_trend_watch.py",
         "calls the sleep canary",
         ("INFORMATIONAL ONLY", GATE),
         "⚠️ Weak first segment: a phrase that also appears as a role marker "
         "elsewhere in the same source. `GATE` carries this pin."),
    Cite("tests/dx/test_paired_trend_watch.py",
         "is the stated reason the sleep",
         (EYES,),
         "The quotation precedes its anchor here — hence the symmetric "
         "window below."),
    Cite("tests/dx/test_paired_trend_watch.py",
         "the sleep canary's says",
         ("INFORMATIONAL ONLY", GATE),
         "⚠️ A further citation in a file that already declared others, so "
         "file-level enumeration could not see it; a blind reviewer clustered "
         "trigger hits by proximity and found it. ⛔ The same docstring also "
         "quotes the CPU canary's `GATING: this is the canary that drives the "
         "INCONCLUSIVE verdict` — a DIFFERENT sentence of the same package "
         "doc, deliberately left unpinned because the scope here is one "
         "sentence. It is a known unpinned sibling, not an oversight."),
)

# Distinctive enough to find every citation, generic enough to find one nobody
# declared. ⛔ Deliberately excludes the bare phrase `INFORMATIONAL ONLY`: it
# is generic English, and `coverage-delta.yml` and `check_k8s_manifests.py`
# use it for entirely unrelated things.
#
# How far from its anchor a citation may sit. Big enough for the unelided
# block quote (anchor, then the CPU canary's line, then the sentence).
#
# ⚠️ It used to say "small enough that a deleted citation cannot be satisfied
# by another copy in the same file — the nearest two copies are thousands of
# characters apart". That stopped being true the moment a further
# `CHANGELOG.md` citation was declared, with its anchor inside this window of
# its neighbour's. So the separation is no longer an invariant
# and is not claimed as one — `_assert_unambiguous` asserts the property that
# actually matters instead, that each declared segment occurs exactly ONCE
# inside the window it is checked in.
SPAN = 600


def flat(text: str) -> str:
    """Strip comment prefixes and collapse wrapping, so a citation matches
    through whatever syntax carries it.

    The source is a Go block comment, the citations live in Python comments,
    Python docstrings and Markdown prose, and every one of them wraps the
    sentence at a different column. Comparing raw text would only ever pin the
    line breaks.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        elif s.startswith("//"):
            s = s.lstrip("/").strip()
        out.append(s)
    return re.sub(r"\s+", " ", " ".join(out))


def flat_file(rel: str) -> str:
    return flat((REPO / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source() -> str:
    return flat_file(SOURCE_REL)


def _window(text: str, anchor: str, rel: str) -> str:
    n = text.count(anchor)
    assert n == 1, (
        f"{rel}: the anchor {anchor!r} occurs {n} times, so it no longer "
        "identifies one citation. Pick a distinctive anchor near the "
        "quotation rather than widening this test.")
    i = text.find(anchor)
    return text[max(0, i - SPAN):i + len(anchor) + SPAN]


def _assert_unambiguous(window: str, cite) -> None:
    """Windows may now overlap, so being findable is not enough — each
    segment has to be findable in exactly one place inside the window it is
    judged in, or a deleted citation could be satisfied by its neighbour."""
    for seg in cite.segments:
        n = window.count(seg)
        assert n == 1, (
            f"{cite.path}: {seg!r} occurs {n} times within SPAN of "
            f"{cite.anchor!r}. At 0 the citation is gone; above 1 this pin "
            "can be satisfied by a copy that is not the one it names.")


# ---------------------------------------------------------------------------
# 1. The keystone: the declared fragments ARE the source.
# ---------------------------------------------------------------------------
def test_the_declared_sentence_is_the_source_sentence(source):
    """⛔ Run this first when the suite goes red.

    If the Go package doc is reworded, every citation is stale by definition
    — and without this test that is invisible, because the citation pins
    compare against `FULL` rather than against the file. Every one of them
    stays green while every citation in the repo quietly goes stale.

    ⛔ EQUALITY, not `in`. The first version asserted each fragment was a
    substring of the source, and blind review broke it: `in` survives
    shortening, so `TAIL` cut down to its last clause and `EYES` with one
    letter removed both left the whole suite green. Slicing the sentence out
    of the source by its two ends and comparing it to `FULL` pins the LENGTH
    as well as the words, and every other fragment is a slice of `FULL`, so
    none of them can be shortened on its own either.
    """
    i = source.find(_FIRST)
    assert i != -1, (
        f"{SOURCE_REL} no longer contains {_FIRST!r}, so the sentence this "
        "module pins cannot even be located. If it was reworded on purpose, "
        "update FULL AND every citation in CITATIONS — that is the pin.")
    j = source.find(_LAST, i)
    assert j != -1, (
        f"{SOURCE_REL}: found the start of the sentence but not its end "
        f"({_LAST!r}).")
    assert source[i:j + len(_LAST)] == FULL, (
        f"the sentence in {SOURCE_REL} is no longer the one this module "
        "declares.\n"
        f"  source: {source[i:j + len(_LAST)]!r}\n"
        f"  FULL:   {FULL!r}\n"
        "⛔ If the rewording is intended, update FULL AND every citation in "
        "CITATIONS.")


def test_the_sentence_occurs_once_in_the_source(source):
    """A second copy in the source would make it ambiguous which one the
    citations track — and would make the keystone's slice pick the first."""
    assert source.count(FULL) == 1


# ---------------------------------------------------------------------------
# 2. The pins.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cite", CITATIONS, ids=[f"{c.path}::{c.anchor[:24]}" for c in CITATIONS])
def test_the_citation_quotes_the_source_verbatim(cite):
    # ⚠️ Deliberately NOT parameterised on the `source` fixture. These pins
    # compare against the constants, and the constants are tied to the source
    # by the keystone above — a citation test that re-read the source would
    # turn one rewording into one identical failure per citation.
    win = _window(flat_file(cite.path), cite.anchor, cite.path)
    _assert_unambiguous(win, cite)
    at = 0
    for seg in cite.segments:
        i = win.find(seg, at)
        assert i != -1, (
            f"{cite.path}: the citation at {cite.anchor!r} no longer contains "
            f"the source's words {seg!r}.\n{cite.note}\n"
            "⛔ Fix the citation, not this test — unless the Go source itself "
            "changed, in which case "
            "`test_the_declared_sentence_is_the_source_sentence` is red too.")
        at = i + len(seg)


# ---------------------------------------------------------------------------
# 3. The site list stays honest.
# ---------------------------------------------------------------------------
