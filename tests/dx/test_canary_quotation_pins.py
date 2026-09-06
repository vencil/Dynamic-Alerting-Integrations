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

⛔ NO COUNTS IN THIS FILE'S PROSE, AND THAT IS THE POINT.
An earlier version of this docstring carried tallies — how many citations, how
many mutations, how many test failures, how many characters a paraphrase kept
verbatim. Across the review chain that produced this file, 25 findings were
raised and **15 of them were prose, of which most were one of those numbers
being wrong**: they were produced by a mutation harness that lives outside the
repository, so no committed command reproduced them and every restatement was
a fresh chance to be wrong. They are gone. What remains is either checkable
from the code beside it (`CITATIONS`, `_LIFT_W`, `_LIFT_S`) or is a shape you
can go and reproduce. Do not reintroduce a number here that no committed
command prints.

⛔ COUNTERFACTUAL, measured before these pins existed. Drift shape 1 was
injected verbatim into `analyze_bench_history.py`'s citation (`constantly.
Emitted + rendered for human eyes; NOT part of the` → `constantly;`) and the
pre-existing gates — the whole of `tests/dx/` and `pre-commit` on that file —
came back green. So the detection these tests add is new, not a second
opinion. (The file was restored and its sha256 matched.)

⛔ INTENTIONAL-BREAK DOGFOOD. Every shape below was mutated and confirmed to
turn this module red: each citation in `CITATIONS`; a second copy of a pinned
fragment inside the same window; a further citation appended to a file that
already declares one; the last sampled window dropped from the derived
detector; the source sentence reworded; `SELF_REL` pointed elsewhere; a new
file quoting the sentence via a listed trigger; a new file lifting a clause no
trigger names.

Must-not-fire controls, confirmed to stay green: the same citation re-wrapped
at different columns; prose edited beside a citation; a new file using the
generic words `INFORMATIONAL ONLY` for an unrelated purpose; a new file
reusing a run of the sentence shorter than the derived detector's guarantee.

Environment shapes, each run rather than reasoned about, because a skip is
exactly the kind of thing that fails silently and this guard has been wrong in
both directions: a normal checkout RUNS; `GIT_DIR`/`GIT_WORK_TREE` set with no
`.git` in place RUNS; no `git` binary SKIPS; an export with no repo above it
SKIPS; an export unpacked INSIDE another checkout SKIPS, because git answers
there but about a different tree; a `git` that exits non-zero for any other
reason (dubious ownership) FAILS LOUDLY.

⚠️ One asymmetry worth knowing before reading a red run: rewording the SOURCE
turns the keystone red and leaves the citation pins GREEN — the citations
still faithfully hold what the source used to say. That is the diagnosis, not
a gap.

⛔ SCOPE — and the ticket's own count was already stale, which is the same
disease this file is now careful not to repeat. #1557 named three citation
sites; enumerating the sentence's distinctive fragments across `git ls-files`
found more, in more files, because #1536's `paired_trend_watch.py` landed
after the ticket was written and two Chinese-prose paraphrases were never
counted. `CITATIONS` below is the list, and
`test_every_file_quoting_the_sentence_is_declared` keeps it honest: a new
citation site fails until it is declared.

This still pins ONE sentence. The general "every quotation matches its source"
gate is TRK-365 and needs a citation convention designed first.

⚠️ What these tests do NOT establish: that a citation is *appropriate*, that
its surrounding prose reads correctly, or that a paraphrase is faithful. They
establish that the substrings a citation presents as the source's words are
the source's words.
"""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE_REL = "scripts/tools/ops/bench-canary/canary_test.go"
SELF_REL = "tests/dx/test_canary_quotation_pins.py"

# ---------------------------------------------------------------------------
# The sentence
#
# Every fragment below is asserted to be a verbatim substring of the flattened
# source by `test_every_declared_fragment_is_verbatim_source_text`, so the
# citations are pinned THROUGH these constants to the Go file — not to each
# other. If the source is reworded, that test is the one that fails first and
# names the rewording as the cause.
# ---------------------------------------------------------------------------
FULL = (
    "INFORMATIONAL ONLY: a 3-4% drift here is only ~30-40us, well inside the "
    "jitter a virtualised GH runner shows even when healthy, so gating on it "
    "would flap INCONCLUSIVE constantly. Emitted + rendered for human eyes; "
    "NOT part of the gate decision."
)
OPEN = "INFORMATIONAL ONLY: a 3-4% drift here is only ~30-40us"
TAIL = (
    "so gating on it would flap INCONCLUSIVE constantly. Emitted + rendered "
    "for human eyes; NOT part of the gate decision."
)
FLAP = "gating on it would flap INCONCLUSIVE constantly"
GATE = "NOT part of the gate decision"
EYES = "Emitted + rendered for human eyes"
# ⚠️ Some sites lowercase the sentence's initial `Emitted` so the quotation
# reads as part of a sentence of their own — they are the ones whose `Cite`
# below carries `EYES_LC` or `EYES_GATE_LC`. That is a real deviation from
# verbatim, and it is left alone rather than "fixed": the pin simply starts
# after the adjusted word, so what it checks is still source text. One
# lowercased word is therefore NOT covered at those sites.
EYES_LC = "rendered for human eyes"
EYES_GATE_LC = "rendered for human eyes; NOT part of the gate decision"


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
# ⛔ KNOWN BLIND SPOT — a CLASS, not a single site. Several files restate this
# canary's role in the author's own words instead of quoting it, and no
# verbatim comparison can reach a paraphrase.
#
# ⛔ The members are NOT listed here, and neither is how many there are. Both
# were written out before; both went stale and both were wrong, in the same
# way each time — a hand-copied list with nothing that reproduces it. This
# reproduces it, and it does not go stale:
#
#     git grep -inE "informational[ -]only" | grep -i canary
#
# ⚠️ Two REASONS were written here before and both were falsified by
# measurement:
#
#  - "word-for-word the same paraphrase" for two of them. They differ by
#    `INFORMATIONAL ONLY` vs `informational-only` — precisely the difference
#    that decides whether a case-sensitive instrument sees them at all.
#  - "`INFORMATIONAL ONLY` survives in all of them". It survives in some and
#    is lowercased or hyphenated in the rest.
#
# So the honest reason is both of the ones tried, each for a different part of
# the class: some members keep no case-exact fragment at all, and for the ones
# that do, promoting that generic phrase to a trigger drags in files using the
# same two generic words for unrelated things (`coverage-delta.yml`,
# `check_k8s_manifests.py`) while still not reaching the rest. Members sitting
# inside an already-declared file cannot be surfaced by file-level enumeration
# at all, whatever the triggers are. Verbatim comparison is the wrong
# instrument for a paraphrase; that is why TRK-365 needs a citation convention
# rather than more regexes.
TRIGGERS = ("flap INCONCLUSIVE", "rendered for human eyes",
            "NOT part of the gate decision", "30-40us")

# ⛔ TRIGGERS alone was not enough, and a blind reviewer proved it: a file
# quoting the sentence's MIDDLE clause ("well inside the jitter a virtualised
# GH runner shows even when healthy") contains none of them, so a brand-new
# undeclared file carrying a long verbatim lift passed clean. The list was
# hand-written, and a hand-written list of clauses is the same kind of stale
# measurement as a hand-written list of sites.
#
# So the second detector is DERIVED from the sentence instead: sample windows
# of `FULL` and match any of them. With window W and stride S, any verbatim run
# of at least `_LIFT_W + _LIFT_S - 1` characters must contain a whole sampled
# window, so this catches every lift that long without naming a single clause.
# ⛔ Read the bound off the constants, do not copy the arithmetic into prose —
# a literal here is one more number that can go stale silently.
# ⛔ The two are a UNION because the derived half is not sufficient. The
# reason first written here named the wrong file and quoted a character count
# that was off by one — both symptoms of stating a measurement instead of
# taking one. The file that actually needs TRIGGERS is
# `tests/dx/test_paired_trend_watch.py`: it only ever quotes short elided
# fragments, none long enough for the guarantee, so the derived detector alone
# does not see it (`_LIFT.search` → None, `TRIGGERS` → True).
_LIFT_W, _LIFT_S = 40, 8
_LIFT = re.compile("|".join(
    re.escape(FULL[i:i + _LIFT_W])
    for i in range(0, len(FULL) - _LIFT_W + 1, _LIFT_S)))


def _quotes_the_sentence(flat_text: str) -> bool:
    return any(t in flat_text for t in TRIGGERS) or bool(_LIFT.search(flat_text))

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
def test_every_declared_fragment_is_verbatim_source_text(source):
    """⛔ Run this first when the suite goes red.

    If the Go package doc is reworded, every citation is stale by definition
    — and without this test that is invisible, because the citation pins
    compare against the constants above rather than against the file. Every
    one of them stays green while every citation in the repo quietly goes
    stale.

    ⚠️ Not the only test that reads the source: `_the_sentence_occurs_once_`
    does too, which is why a reworded source produces two failures and not
    one. It is the only one that checks the fragments the citations are
    pinned to.
    """
    for name, frag in (("FULL", FULL), ("OPEN", OPEN), ("TAIL", TAIL),
                       ("FLAP", FLAP), ("GATE", GATE), ("EYES", EYES),
                       ("EYES_LC", EYES_LC),
                       ("EYES_GATE_LC", EYES_GATE_LC)):
        assert frag in source, (
            f"{name} is not in {SOURCE_REL} any more. If the sentence was "
            "reworded on purpose, update this module AND every citation in "
            "CITATIONS — that is the whole point of the pin.")


def test_the_derived_detector_catches_every_run_of_the_stated_length():
    """The `_LIFT_W + _LIFT_S - 1` guarantee is arithmetic, not a hope: any run
    that long spans a whole sampled window, so one of them must match.

    ⚠️ The boundary is asserted in BOTH directions on purpose, and the second
    assertion is the one that matters: "n or more is caught" does NOT mean
    "n - 1 is missed". Most shorter runs are caught too; the bound is where the
    GUARANTEE stops, not where detection stops. Without the second assertion
    that distinction decays into the stronger, false claim.

    ⛔ WHAT THIS CANNOT SEE, dogfooded rather than assumed: `n` is derived from
    the same two constants, so changing them moves the goalposts with the ball
    and this test stays green. It guards the CONSTRUCTION of the window set,
    not the CHOICE of constants — dropping tail windows from the range,
    dropping even the single last window, and sampling windows narrower than
    `_LIFT_W` are what it catches.

    ⛔ An earlier version of this docstring claimed losing the last window was
    green "and correctly so". That came from a mutation labelled by its intent
    rather than checked by its effect: removing the `+ 1` from the range bound
    removes no window at all. The claim was about a mutation that never
    happened — which is also why no character counts appear in this docstring
    any more.

    The choice of the two constants is a judgement, argued above, not asserted
    here.
    """
    n = _LIFT_W + _LIFT_S - 1
    assert not [i for i in range(len(FULL) - n + 1)
                if not _LIFT.search(FULL[i:i + n])], (
        f"a verbatim run of {n} characters escaped the derived detector, so "
        "the stated guarantee is false")
    assert [i for i in range(len(FULL) - n + 2)
            if not _LIFT.search(FULL[i:i + n - 1])], (
        f"every run of {n - 1} characters is caught too — the guarantee is "
        "understated and the comment above should say so")


def test_the_sentence_occurs_once_in_the_source(source):
    """A second copy in the source would make `in` ambiguous about which one
    the citations track."""
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
    # turn one rewording into twelve identical failures.
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
            "`test_every_declared_fragment_is_verbatim_source_text` is red too.")
        at = i + len(seg)


# ---------------------------------------------------------------------------
# 3. The site list stays honest.
# ---------------------------------------------------------------------------
def _hit_spans(text: str):
    """Every position in `text` that looks like this sentence."""
    spans = []
    for trig in TRIGGERS:
        start = 0
        while (i := text.find(trig, start)) != -1:
            spans.append((i, i + len(trig)))
            start = i + 1
    for mo in _LIFT.finditer(text):
        spans.append(mo.span())
    return sorted(spans)


def _covered_spans(text: str, cites):
    """Where in `text` each declared citation actually matched."""
    out = []
    for cite in cites:
        i = text.find(cite.anchor)
        assert i != -1, f"{cite.path}: anchor {cite.anchor!r} is gone"
        w0 = max(0, i - SPAN)
        win = text[w0:i + len(cite.anchor) + SPAN]
        at, lo, hi = 0, None, None
        for seg in cite.segments:
            j = win.find(seg, at)
            assert j != -1, f"{cite.path}: {seg!r} missing near {cite.anchor!r}"
            lo = w0 + j if lo is None else lo
            hi = w0 + j + len(seg)
            at = j + len(seg)
        # ⚠️ The margin is `_LIFT_W`, not a taste-chosen number: a sampled
        # window can start up to `_LIFT_W - 1` characters before a declared
        # segment and end that far after it, so without it a citation would
        # look like it had a stray hit of its own hanging off each end.
        out.append((lo - _LIFT_W, hi + _LIFT_W))
    return out


def test_every_occurrence_in_a_declared_file_belongs_to_a_declared_citation():
    """⛔ The file-level guard below is not enough, and that is measured, not
    predicted: the two citations blind review found hiding in this repo were
    BOTH second occurrences inside files that already declared one.

    So every place that looks like this sentence has to belong to a declared
    citation, not merely live in a declared file.

    ⚠️ It still only looks inside files that CITATIONS names. A whole new file
    is the other test's job, and the two together are what the site list rests
    on.
    """
    per_file = defaultdict(list)
    for cite in CITATIONS:
        per_file[cite.path].append(cite)

    for path, cites in per_file.items():
        text = flat_file(path)
        covered = _covered_spans(text, cites)
        stray = [sp for sp in _hit_spans(text)
                 if not any(lo <= sp[0] and sp[1] <= hi for lo, hi in covered)]
        assert not stray, (
            f"{path}: {len(stray)} occurrence(s) of the sentence outside every "
            f"declared citation, first at {stray[0]}:\n"
            f"  …{text[max(0, stray[0][0] - 80):stray[0][1] + 60]}…\n"
            "Add a `Cite(...)` for it. A second quotation in an already-"
            "declared file is exactly the shape that hid twice before.")


def test_every_file_quoting_the_sentence_is_declared():
    """⛔ This is the half that #1557's own "three places" got wrong.

    A hand-written site list is a measurement, and measurements go stale:
    #1536 added three citations after the ticket was filed. Enumerating
    instead means a new citation site cannot be silently left unpinned.

    ⚠️ This half only answers "is this FILE declared". A second citation
    inside an already-declared file is the other test's job — it was this
    test's blind spot, it hid two real citations, and it is now covered by
    `_every_occurrence_in_a_declared_file_belongs_to_a_declared_citation`
    rather than merely documented here.

    ⚠️ The limit that remains: it reads `git ls-files`, so a new citation in
    an UNTRACKED file passes. The guard bites at `git add` time, not at write
    time.
    """
    # ⛔ ASK GIT, do not guess from the filesystem — this line has now been
    # wrong in both directions and each version was caught by review:
    #
    #  v1 `except (OSError, SubprocessError)` skipped on ANY non-zero exit and
    #     called all of them "no git checkout". `detected dubious ownership`
    #     is exit 128 too and happens in containers and on bind mounts, so a
    #     live guard turned into a green skip in a real checkout, under a
    #     message that named the wrong cause — and CI prints no skip reasons.
    #  v2 skipped when `REPO/.git` was missing. That marker is not the same
    #     question: with `GIT_DIR`/`GIT_WORK_TREE` set (git hooks, some CI
    #     wrappers) `git ls-files` works with no `.git` in place, and v2
    #     skipped without even trying.
    #
    # `rev-parse` answers the actual question, and everything else raises.
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO,
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        pytest.skip("no `git` binary on PATH, so the site list cannot be "
                    "enumerated here")
    if probe.returncode != 0:
        if "not a git repository" in probe.stderr:
            pytest.skip(f"{REPO} is not inside a git work tree, so the site "
                        "list cannot be enumerated here (an exported tree or "
                        "a tarball)")
        raise RuntimeError(  # dubious ownership, broken index, …
            f"git could not answer whether {REPO} is a work tree, and that is "
            f"NOT the same as 'no checkout': {probe.stderr.strip()}")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
        check=True, timeout=120).stdout.split()
    found = set()
    for rel in tracked:
        p = REPO / rel
        # Symlinks would double-count (`docs/CHANGELOG.md` → `CHANGELOG.md`).
        if p.is_symlink() or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _quotes_the_sentence(flat(text)):
            found.add(rel)

    declared = {SOURCE_REL, SELF_REL} | {c.path for c in CITATIONS}
    # ⛔ An export unpacked INSIDE another checkout is inside a work tree, so
    # the probe above says yes and `git ls-files` answers — about the OTHER
    # repository. Nothing declared here would be in that answer, and the
    # difference between "this tree has no citations" and "git described a
    # different tree" has to stay visible.
    if not (set(tracked) & declared):
        pytest.skip(
            f"git answered for a work tree that does not contain {SELF_REL}, "
            "so this is an export sitting inside someone else's checkout and "
            "the site list cannot be enumerated here")
    assert found == declared, (
        f"undeclared citation sites: {sorted(found - declared)}\n"
        f"declared but no longer quoting: {sorted(declared - found)}\n"
        "Add a `Cite(...)` entry (or remove one) — the site list is derived "
        "by enumeration precisely so it cannot go stale.")


def test_this_module_is_where_it_says_it_is():
    """`SELF_REL` excludes this file from the enumeration above, so a wrong
    value would silently turn that test into a tautology about itself."""
    assert (REPO / SELF_REL).resolve() == Path(__file__).resolve()
