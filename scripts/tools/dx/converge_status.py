#!/usr/bin/env python3
"""Judge whether a multi-round fix chain is converging, from its ROUNDS.jsonl ledger.

TRK-360; the protocol itself lives in the ``vibe-converge`` skill. The first line
above is deliberately one whole sentence: generate_tool_map.py publishes only a
docstring's FIRST LINE into docs/internal/tool-map{,.en}.md, so a wrapped opening
sentence ships to the tool map truncated mid-clause.

    python3 scripts/tools/dx/converge_status.py --scope dev/1443
    make converge-status SCOPE=dev/1443

WHAT THIS IS FOR
================
A defect that takes more than one round to fix has a failure mode of its own:
each round ships a fix that is itself a new, unreviewed surface, so the chain
adds holes as fast as it closes them. Measured on the
``_DEFAULTS_ROOTS_MAY_BE_EMPTY`` chain (#1411 -> #1415 -> #1434 -> #1442 ->
#1443 -> #1457, six rounds):

  * insertions:deletions per round was 2882:67, 788:41, 1018:19 -- roughly
    +1000 lines of new surface per round that no lens had scanned;
  * #1431 measured the fix commit at 1.6x the reviewed commit (814 -> 1336
    lines), unreviewed, and the second pass over it found the whole chain's
    only Critical;
  * three consecutive rounds wrote three versions of a predicate for a question
    that #1443 concluded was "informationally impossible" to answer from the
    evidence available at check time.

This tool reads the ledger the protocol asks each round to append to, prints
what actually happened per round, and evaluates the stop rules.

NO RULE TREATS A LOW FINDING COUNT AS A REASON TO STOP
======================================================
One used to. A rule named CONVERGED fired when two consecutive rounds each
reported zero verified findings, and it was removed here because "zero
findings" is a property of the reviewer, not of the work:

  * a single inspection historically surfaces about 30% of the defects present
    (median across studies, Wagner 2006 survey of defect-detection techniques);
  * 61% of reviews find no defect at all (Cisco, 2500 reviews / 3.2M LOC) -- so
    zero findings is the MAJORITY event and cannot discriminate;
  * the classical exit criteria this protocol descends from are "known
    defects fixed and verified", never "no new defects found". Those two read
    alike and are opposites. SOURCING: the Cisco case study and the NASA
    guidebook are first-hand; Fagan 1976 itself could not be retrieved (three
    PDF locations all failed), so that half is second-hand.

What replaced it: declare an oracle (Rule 0) and cap the rounds (Rule 1).

WHAT IT CHECKS AND WHAT IT CANNOT
=================================
It checks the SHAPE of the ledger: that a claim labelled ``verified`` carries an
``evidence`` field, that an ``oracle`` names a command and a falsifier, that
rounds do not skip numbers, that every record names a known ``kind``.

It CANNOT check that the evidence is real, and it cannot check that the oracle
was run. Nothing offline can prove a command was executed; the tier label is a
discipline, not a measurement.

What changed is narrower than "no rule counts anything", which would be false:
CHANGE-SUBJECT counts dead-end records and UNREVIEWED-FIX keys on how many
findings a round marked ``fixed``. The precise claim is that **no rule treats a
LOW finding count as a reason to stop** -- the direction that a round nobody
honestly reviewed satisfies for free. Counting dead ends or fixes runs the other
way: under-reporting them makes those rules quieter, but quieter there means the
chain keeps going, not that it is finished.

(SURFACE-DEBT is advisory, not a stop rule; it exits 0.)

The cheapest way to satisfy each rule is worth stating, since a guard whose
failure message names a cheaper bad fix gets taken apart by whoever reads it.
Measured, on this tool, by a blind reviewer who built ledgers and ran them:

* ORACLE-MISSING is satisfied by writing a plausible oracle line.
* CHANGE-SUBJECT is satisfied by not recording the dead end -- and the skill
  calls the dead-end table the most valuable thing that crosses rounds.
* UNREVIEWED-FIX is satisfied by never marking a finding ``status=fixed``.
* ROUND-CAP is satisfied by splitting one chain across two ledgers under the
  same scope: each is evaluated separately, so six rounds recorded as 3+3 stay
  quiet. Every line of that ledger is true; no lying required. The rules are
  per-ledger by construction and this is not defended against.
* ROUND-CAP at the boundary is also satisfied by editing a fixed finding's
  status to claim it is still open. That one IS a lie, and it is the state the
  rule most wants to see.
* Deleting the ledger used to satisfy everything, because an empty one exited
  0 while an honest one exited 1. EMPTY-LEDGER closes that specific hole; the
  broader point stands, which is that a self-reported ledger cannot be made
  honest by adding predicates to it.

None of these is defended against. They are named because a reader who finds a
red and has to guess will find the cheapest of them anyway.

DELIBERATELY NOT A GATE
=======================
No CI job and no pre-commit hook calls this. #1457 deleted six "guards of the
guards" for being surface without detection value; wiring a gate around the
review process itself would repeat exactly that. Owner class is skill-advised
(docs/internal/hook-vs-skill-coverage.md).

WHAT IT DOES NOT CHECK, BEYOND THE EVIDENCE ITSELF
==================================================
Two further gaps, stated here rather than closed, because closing either would
mean writing a predicate on content that cannot distinguish the legal case from
the defective one -- the exact move the protocol this tool serves exists to ban:

* ``evidence`` is checked for being non-empty, not for being evidence.
  ``"evidence": "yes"`` passes. A stronger shape test (must contain a command,
  must contain "=>") would reject legitimate one-line citations and would still
  pass a fabricated string, so it buys a false red for no true green.
* LEDGER-GAP checks that the round numbers present are contiguous, NOT that they
  start at 1. A ledger opened mid-chain (rounds 5-7 of a chain whose first four
  rounds predate the ledger) is legal and stays silent. There is no in-file
  evidence that separates "opened mid-chain" from "lost the first four rounds".

EXIT CODES (scripts/tools/_lib_exitcodes.py)
============================================
  0  ledger read; nothing the caller must act on (advisory SURFACE-DEBT /
     self-review / UNDECIDABLE notes land here)
  1  a blocking stop rule fired (EMPTY-LEDGER / ORACLE-MISSING / ROUND-CAP /
     CHANGE-SUBJECT / UNREVIEWED-FIX) or the ledger breaks the format contract
     (verified claim with no evidence, oracle with no command or falsifier,
     banned ``speculative`` tier, non-integer surface counter, rounds skipped)
  2  cannot do the job: scope missing, no ROUNDS.jsonl under it, or a line that
     is not readable at all (not UTF-8, not JSON, not a JSON object)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Same two sys.path inserts every sibling dx tool uses: parent for the repo
# root libs (_lib_*), self for local helpers.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR,
    EXIT_OK,
    EXIT_VIOLATION,
)

LEDGER_NAME = "ROUNDS.jsonl"

KINDS = {"subject", "decidability", "finding", "dead-end", "question", "oracle"}
TIERS = {"verified", "inferred"}
BANNED_TIERS = {"speculative"}
STATUSES = {"open", "fixed", "rejected", "deferred"}

# A chain may run this many rounds before the tool stops advising and starts
# blocking. Past it the owner decides whether round N+1 happens, not the chain.
# 5 matches the circuit breaker obra/superpowers shipped in v6.2.0 after hitting
# the same non-convergence; an empirical repair-loop evaluation puts most
# obtainable gain in rounds 1-4 (arXiv:2607.05197 -- NIER, not a survey). Neither source pins the boundary exactly, and
# this repo has no measurement that does -- 5 is the more permissive of the two.
ROUND_CAP = 5

# A round whose reviewed surface grows this much without deleting anything is
# adding predicate versions, not replacing them. Advisory only -- a round that
# adds a test file is legitimately lopsided (#1429 measured 2508:2).
SURFACE_RATIO_LIMIT = 10
SURFACE_INSERTIONS_FLOOR = 300


def find_ledgers(scope):
    """Return every ROUNDS.jsonl at or under `scope`, sorted.

    `scope` may be the ledger file itself, or a directory containing one or
    more (nested per-round scratch dirs are allowed, same as PROGRESS.jsonl).
    """
    if os.path.isfile(scope):
        return [scope]
    found = []
    for root, _dirs, files in os.walk(scope):
        if LEDGER_NAME in files:
            found.append(os.path.join(root, LEDGER_NAME))
    return sorted(found)


def parse_ledger(path):
    """Parse one ledger.

    Returns (records, unparsable, violations). `unparsable` holds lines this tool
    could not read at all -- not valid UTF-8, or not JSON (caller error: the file
    is broken). `violations` holds records that parsed but break the format
    contract (user-actionable).

    Read as BYTES and decoded per line, deliberately. The protocol tells agents to
    append to this ledger with a shell `echo` from whatever host they are on, and
    a Windows host writes cp950/big5 by default -- this repo has already been
    burned twice by exactly that (#1372 read an anchor-debt ledger as mojibake,
    #1363 rewrote a whole file to CRLF). A whole-file text handle raises on the
    first bad byte and takes the other 20 rounds down with it; per-line decoding
    turns one bad line into one reported line. A leading BOM (PowerShell
    `Out-File`) is stripped rather than reported -- it is not the writer's
    mistake in any way they can see.
    """
    records, unparsable, violations = [], [], []
    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc})"], []
    for lineno, raw in enumerate(raw_bytes.splitlines(), start=1):
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            unparsable.append(
                f"{path}:{lineno}: not UTF-8 ({exc.reason} at byte {exc.start}) "
                "-- the ledger must be UTF-8; a Windows shell writes the local "
                "codepage unless told otherwise")
            continue
        line = line.lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError) as exc:
            unparsable.append(f"{path}:{lineno}: not JSON ({exc})")
            continue
        if not isinstance(rec, dict):
            unparsable.append(f"{path}:{lineno}: not a JSON object")
            continue
        rec["_where"] = f"{path}:{lineno}"
        violations.extend(check_record(rec))
        records.append(rec)
    return records, unparsable, violations


def _is_valid_count(value):
    """True when `value` is a non-negative int (and not a bool).

    `check_record` rejects on it and `Round._count` sums on it. One predicate,
    because two copies of "reject bool, reject non-int, reject negative" can
    drift into a value that is reported as a violation and still summed.
    """
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def check_record(rec):
    """Format-contract checks for one record. Returns a list of messages."""
    out = []
    where = rec.get("_where", "?")
    kind = rec.get("kind")
    if kind not in KINDS:
        out.append(f"{where}: unknown kind {kind!r} (expected one of "
                   f"{', '.join(sorted(KINDS))})")
        return out
    # An oracle is a property of the CHAIN, not of a round: it is written
    # before any round happens. Requiring a round number on it forced authors
    # to invent one, and inventing 1 on a ledger opened mid-chain manufactured
    # a phantom round -- which then tripped LEDGER-GAP and inflated the span.
    if kind == "oracle":
        if "round" in rec and not isinstance(rec.get("round"), int):
            out.append(f"{where}: oracle 'round' is optional, but when present "
                       f"it must be an integer, got {rec.get('round')!r}")
    elif not isinstance(rec.get("round"), int):
        out.append(f"{where}: 'round' must be an integer, got "
                   f"{rec.get('round')!r}")
    if kind == "subject":
        for field in ("insertions", "deletions"):
            value = rec.get(field)
            if value is None:
                continue
            if _is_valid_count(value):
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                out.append(f"{where}: subject '{field}' must be an integer, got "
                           f"{value!r}")
            else:
                out.append(f"{where}: subject '{field}' must not be negative, "
                           f"got {value!r}")
    if kind == "oracle":
        # Shape only, same boundary as 'evidence' below: this cannot tell a real
        # command from a plausible string. What it CAN tell is that the author
        # wrote down what would falsify the work -- the field that is hardest to
        # fill in retroactively, and the one that makes the terminal condition
        # external to the review.
        for field in ("command", "falsifier"):
            value = rec.get(field)
            # `str(None).strip()` is "None", which is non-empty -- so a JSON
            # null, the shape a serializer emits for a field nobody filled in,
            # used to pass the required check and print as the word "None".
            if not isinstance(value, str) or not value.strip():
                out.append(
                    f"{where}: oracle must carry non-empty '{field}' "
                    "(command: what to run; falsifier: which production change "
                    "would make it fail). Cannot name a falsifier => there is "
                    "no oracle, and the chain has no terminal condition except "
                    "a reviewer running out of things to say")
    tier = rec.get("tier")
    if kind == "finding":
        if tier in BANNED_TIERS:
            out.append(f"{where}: tier 'speculative' is banned from the ledger "
                       "-- run it and label it verified, or leave it out")
        elif tier not in TIERS:
            out.append(f"{where}: finding needs tier verified|inferred, got "
                       f"{tier!r}")
        status = rec.get("status")
        if status not in STATUSES:
            out.append(f"{where}: finding needs status "
                       f"{'|'.join(sorted(STATUSES))}, got {status!r}")
    needs_evidence = (kind == "dead-end") or (kind == "finding"
                                              and tier == "verified")
    if needs_evidence and not str(rec.get("evidence", "")).strip():
        label = "dead-end" if kind == "dead-end" else "verified finding"
        out.append(f"{where}: {label} must carry non-empty 'evidence' "
                   "(the command and what it actually printed)")
    return out


class Round(object):
    """Everything the ledger says about one round."""

    def __init__(self, number):
        self.number = number
        self.subjects = []          # subject records
        self.verified = 0           # findings, tier=verified
        self.inferred = 0
        self.fixed = 0              # findings, status=fixed
        self.dead_ends = []         # dead-end records
        self.open_questions = 0
        self.undecidable = []       # decidability records with verdict=undecidable
        self.oracles = []           # oracle records (command + falsifier)
        self.is_oracle_only = False  # a holder for a ledger with no real rounds
        self.reviewers = set()

    @staticmethod
    def _count(record, field):
        """A counter that check_record has already reported on if it is bad.

        Returning 0 here rather than raising is the point: the surface figure
        degrades, the round still reports, and the caller still gets exit 1 from
        the format violation. A traceback would lose the other 19 rounds to one
        typo.
        """
        value = record.get(field)
        return value if _is_valid_count(value) else 0

    @property
    def insertions(self):
        return sum(self._count(s, "insertions") for s in self.subjects)

    @property
    def deletions(self):
        return sum(self._count(s, "deletions") for s in self.subjects)

    @property
    def subject_names(self):
        return [str(s.get("subject", "?")) for s in self.subjects]


def build_rounds(records):
    """Fold records into Round objects keyed by round number, ascending.

    Oracle records never CREATE a round. They are chain-level -- written before
    any round happens -- and materialising one into a Round(1) on a ledger whose
    work starts at round 5 invented a phantom round that then tripped LEDGER-GAP
    and inflated the ROUND-CAP span. They are attached to the earliest real
    round instead, or kept even when the ledger has no rounds at all.
    """
    rounds = {}
    oracles = []
    for rec in records:
        kind = rec.get("kind")
        if kind == "oracle":
            oracles.append(rec)
            continue
        num = rec.get("round")
        if not isinstance(num, int):
            continue
        rnd = rounds.setdefault(num, Round(num))
        if kind == "subject":
            rnd.subjects.append(rec)
            reviewer = rec.get("reviewer")
            if reviewer:
                rnd.reviewers.add(str(reviewer))
        elif kind == "finding":
            if rec.get("tier") == "verified":
                rnd.verified += 1
            elif rec.get("tier") == "inferred":
                rnd.inferred += 1
            if rec.get("status") == "fixed":
                rnd.fixed += 1
        elif kind == "dead-end":
            rnd.dead_ends.append(rec)
        elif kind == "question":
            if rec.get("status") == "open":
                rnd.open_questions += 1
        elif kind == "decidability":
            if rec.get("verdict") == "undecidable":
                rnd.undecidable.append(rec)
    ordered = [rounds[k] for k in sorted(rounds)]
    if oracles:
        if ordered:
            ordered[0].oracles.extend(oracles)
        else:
            # An oracle and nothing else: keep it visible rather than dropping
            # it, and let EMPTY-LEDGER say what is missing.
            holder = Round(0)
            holder.oracles.extend(oracles)
            holder.is_oracle_only = True
            ordered = [holder]
    return ordered


def evaluate(rounds):
    """Apply the stop rules plus the advisory notes.

    Returns (blocking, advisory) -- two lists of strings.
    """
    blocking, advisory = [], []

    # An empty ledger used to exit 0 while an honest one-round ledger with no
    # oracle exited 1 -- so the cheapest way to satisfy every rule below was to
    # truncate the file. That is the same "not looking is the cheapest green"
    # shape these rules exist to remove, so silence on an empty ledger is a
    # violation, not a pass.
    work_rounds = [r for r in rounds if not r.is_oracle_only]
    if not work_rounds:
        blocking.append(
            "EMPTY-LEDGER: this ledger records no rounds. An empty ledger is "
            "not a converged chain -- it is an unrecorded one, and exiting 0 on "
            "it would make deleting the file the cheapest way to satisfy every "
            "other rule here. Append the round you are on, or drop the scope.")
        return blocking, advisory
    rounds = work_rounds

    numbers = [r.number for r in rounds]
    expected = list(range(numbers[0], numbers[0] + len(numbers)))
    if numbers != expected:
        blocking.append(
            f"LEDGER-GAP: round numbers {numbers} skip -- a missing round is a "
            "round whose findings never crossed into the next one")

    # Rule 0 -- the chain never wrote down what would end it. Checked first
    # because every other rule is about HOW the chain runs; this one is about
    # whether it has an end condition at all.
    #
    # A chain that recorded an `undecidable` verdict is EXEMPT. That verdict is
    # the protocol's own terminal state -- section 0 says a subject whose legal
    # and defective cases are isomorphic under the available evidence must be
    # dropped, not given a predicate. Demanding an oracle there asked the author
    # to invent one for a question the same ledger had just certified as having
    # no answer, and the message's own escape hatch ("or stop the chain") had no
    # record kind behind it.
    if not any(rnd.oracles for rnd in rounds) \
            and not any(rnd.undecidable for rnd in rounds):
        blocking.append(
            "ORACLE-MISSING: no round declares kind=oracle. Without one the "
            "terminal condition is 'a reviewer stopped finding things', which "
            "is a property of the reviewer, not of the work: a single "
            "inspection historically surfaces ~30% of defects (median, Wagner "
            "2006 survey) and 61% of reviews find none at all (Cisco, 2500 "
            "reviews) -- so 'zero findings' is the majority event. Declare what "
            "to run and what would falsify it, or record the subject as "
            "undecidable and stop.")

    # Rule 1 -- the chain reached the cap. The cap is a circuit breaker, not a
    # quality judgement: it hands the decision to the owner instead of letting
    # the chain decide it has had enough of itself.
    #
    # It fires AT the cap, not past it, and it SUBSUMES rule 3 there. Firing
    # past it left the boundary with no legal state at all: a round-5 chain that
    # honestly recorded a fix got UNREVIEWED-FIX ("open a later round"), and
    # opening round 6 got ROUND-CAP. The only exit that returned 0 was editing
    # the fixed finding's status to claim it was not fixed -- a lie, aimed
    # exactly at the rule that exists to stop fixes escaping review.
    span = numbers[-1] - numbers[0] + 1
    with_subject_pre = [r.number for r in rounds if r.subjects]
    fixed_pre = [r for r in rounds if r.fixed]
    unreviewed_fix = None
    if fixed_pre and not any(n > fixed_pre[-1].number for n in with_subject_pre):
        unreviewed_fix = fixed_pre[-1]
    # ROUND_CAP rounds are ALLOWED -- the cap is the ceiling, not the last legal
    # round. It fires past the cap, and also AT the cap when an unreviewed fix
    # is pending, because that is the one state with no legal move: rule 3 would
    # say "open a later round" and rule 1 would then block that round. Merging
    # the two into one verdict keeps the instruction satisfiable (take it to the
    # owner, which happens out of band) instead of leaving the only rc=0 exit
    # being to edit the fixed finding's status into a lie.
    at_cap = (span > ROUND_CAP) or (span == ROUND_CAP and unreviewed_fix)
    if at_cap:
        pending = (f" Round {unreviewed_fix.number} also has "
                   f"{unreviewed_fix.fixed} unreviewed fix(es); reviewing them "
                   "is part of what you are taking to the owner, not a round "
                   "you may open here." if unreviewed_fix else "")
        blocking.append(
            f"ROUND-CAP: this chain is at round {numbers[-1]} ({span} rounds). "
            f"The cap is {ROUND_CAP}. Rounds past it are an owner decision, not "
            "a chain decision -- most obtainable gain lands in rounds 1-4 "
            "(arXiv:2607.05197) and without an external oracle further rounds "
            "measurably make things worse (GSM8K 95.5 -> 89.0 over two "
            "self-correction rounds, arXiv:2310.01798). Take the open findings "
            "to the owner with the two numbers: how many this round, and how "
            f"many of them your own previous round created.{pending} A "
            "continuation the owner approves belongs in a new scope with its "
            "own ledger and its own oracle -- this one is closed.")

    # Rule 2 -- same subject, two dead predicates already.
    by_subject = {}
    for rnd in rounds:
        for rec in rnd.dead_ends:
            by_subject.setdefault(str(rec.get("subject", "?")), []).append(rnd.number)
    for subject, where in sorted(by_subject.items()):
        if len(where) >= 2:
            blocking.append(
                f"CHANGE-SUBJECT: {subject!r} has {len(where)} dead ends "
                f"(rounds {', '.join(str(w) for w in where)}). A third predicate "
                "on the same subject is banned -- go back to the decidability "
                "gate and change what is being judged (#1443: v1/v2/v3 all died "
                "before #1457 changed the subject).")

    # Rule 3 -- the last round shipped a fix that no later round reviewed.
    # Suppressed at the cap: rule 1 already says where that fix goes, and
    # emitting both leaves the boundary with no reachable green state.
    #
    # "No later round" now means "no later round that declares a SUBJECT",
    # which is what the message always said. Keying on "is the last Round
    # object" let a single `question` record in a later round silence it --
    # the report printed `(no subject declared)` for that round while the rule
    # stayed quiet.
    if unreviewed_fix and not at_cap:
        last = unreviewed_fix
        blocking.append(
            f"UNREVIEWED-FIX: round {last.number} marks {last.fixed} finding(s) "
            "fixed and no later round declares a subject. The fix is a new "
            "surface, measured at 1.6x the reviewed one (#1431, 814 -> 1336 "
            "lines, where the second pass found the chain's only Critical). "
            "Open a round whose subject IS that fix.")

    # REMOVED: a CONVERGED rule keyed on "two consecutive rounds reported 0
    # verified findings". It rewarded the cheapest possible green -- not
    # looking -- and this module's own honesty section already said a round that
    # was never honestly reviewed is indistinguishable from one that was. The
    # replacement is Rule 0 (declare an oracle) plus Rule 1 (cap the rounds):
    # the chain now ends because a named check passes, not because a reviewer
    # went quiet. Deleted rather than reworded: see the skill's stop-rule
    # section for the measurements that killed it.

    # Advisory -- surface budget.
    for rnd in rounds:
        ins, dels = rnd.insertions, rnd.deletions
        if ins > SURFACE_INSERTIONS_FLOOR and ins > SURFACE_RATIO_LIMIT * max(dels, 1):
            advisory.append(
                f"SURFACE-DEBT: round {rnd.number} reviewed +{ins}/-{dels} "
                f"({ins // max(dels, 1)}:1). Answer in the ledger: is this round "
                "changing the subject, or writing predicate version N+1? "
                "(A round that adds a test file is legitimately lopsided.)")

    # Advisory -- a self-reviewed round that found nothing.
    for rnd in rounds:
        if rnd.reviewers == {"self"} and rnd.verified == 0:
            advisory.append(
                f"SELF-REVIEW-ZERO: round {rnd.number} was self-reviewed and "
                "reported 0 verified findings. Measured yield of author "
                "self-review on this repo is 0 (#1457: 8 blind reviewers, 59+ "
                "findings, author self-review 0). Treat as not-yet-reviewed. "
                "UNRESOLVED: that measurement predates the current model, whose "
                "own guidance says to remove harness scaffolding that adds "
                "separate verification steps. Neither side has been measured on "
                "the current model; this note is kept, not acted on.")

    # Advisory -- an undecidable verdict is the thing that should have stopped
    # the chain; surface it even when the round continued anyway.
    for rnd in rounds:
        for rec in rnd.undecidable:
            advisory.append(
                f"UNDECIDABLE: round {rnd.number} recorded "
                f"{str(rec.get('subject', '?'))!r} as undecidable "
                f"({str(rec.get('note', '')).strip() or 'no note'}). Any further "
                "predicate on it is a rewrite of a question that has no answer.")

    return blocking, advisory


def format_report(path, rounds):
    lines = [f"== {path} =="]
    if not rounds:
        lines.append("  (no round records)")
        return lines
    # The oracle prints unconditionally, above the per-round detail. It is the
    # only line here that says what would END this chain; leaving it to fire
    # only on violation would make the chain's terminal condition the one thing
    # the report does not routinely show.
    # The label comes from the record's OWN round field, not from the Round it
    # was filed under: an oracle is chain-level, and printing the holder's
    # synthetic number claimed a "round 0" that never happened.
    declared = [rec for rnd in rounds for rec in rnd.oracles]
    if declared:
        for rec in declared:
            num = rec.get("round")
            where = f" (round {num})" if isinstance(num, int) else ""
            lines.append(f"  oracle{where}: "
                         f"{str(rec.get('command', '')).strip() or '(none)'}")
            lines.append(f"    falsified by: "
                         f"{str(rec.get('falsifier', '')).strip() or '(none)'}")
    else:
        lines.append("  oracle: NONE DECLARED -- this chain has no stated "
                     "terminal condition")
    for rnd in rounds:
        if rnd.is_oracle_only:
            # The holder exists so a round-less oracle stays visible; printing
            # it as "round 0" would put back the phantom round that attaching
            # oracles to rounds created in the first place.
            continue
        subj = ", ".join(rnd.subject_names) or "(no subject declared)"
        reviewer = "/".join(sorted(rnd.reviewers)) or "?"
        lines.append(f"  round {rnd.number}: {subj}  [reviewer={reviewer}]")
        lines.append(
            f"    surface +{rnd.insertions}/-{rnd.deletions} | "
            f"verified {rnd.verified} | inferred {rnd.inferred} | "
            f"fixed {rnd.fixed} | dead-ends {len(rnd.dead_ends)} | "
            f"open questions {rnd.open_questions}")
    return lines


def build_parser():
    parser = argparse.ArgumentParser(
        description=("Judge whether a multi-round fix chain is converging, from "
                     "its dev/<scope>/ROUNDS.jsonl ledger. Protocol: the "
                     "vibe-converge skill."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scope", required=True,
        help=("Directory holding ROUNDS.jsonl (searched recursively), or the "
              "ledger file itself. e.g. dev/1443"))
    return parser


def main(argv=None):
    try_utf8_stdout()
    args = build_parser().parse_args(argv)

    if not os.path.exists(args.scope):
        print(f"ERROR: scope not found: {args.scope}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    ledgers = find_ledgers(args.scope)
    if not ledgers:
        print(f"ERROR: no {LEDGER_NAME} under {args.scope} -- the chain has no "
              "ledger, or the rounds were never appended "
              "(protocol: vibe-converge skill)", file=sys.stderr)
        return EXIT_CALLER_ERROR

    all_unparsable, all_violations, all_blocking, all_advisory = [], [], [], []
    for path in ledgers:
        records, unparsable, violations = parse_ledger(path)
        rounds = build_rounds(records)
        blocking, advisory = evaluate(rounds)
        for line in format_report(path, rounds):
            print(line)
        print("")
        all_unparsable.extend(unparsable)
        all_violations.extend(violations)
        # Every verdict names its ledger. FORMAT violations already carry
        # path:lineno; blocking/advisory did not, so a scope holding several
        # ledgers produced identical messages with no way to tell which one
        # they were about.
        all_blocking.extend(f"{path}: {m}" for m in blocking)
        all_advisory.extend(f"{path}: {m}" for m in advisory)

    for label, items in (("advisory", all_advisory),
                         ("FORMAT", all_violations),
                         ("BLOCKING", all_blocking)):
        if items:
            print(f"-- {label} --", file=sys.stderr)
            for item in items:
                print(f"  {item}", file=sys.stderr)

    if all_unparsable:
        print("-- unreadable lines --", file=sys.stderr)
        for item in all_unparsable:
            print(f"  {item}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if all_blocking or all_violations:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
