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
what actually happened per round, and evaluates the three stop rules.

WHAT IT CHECKS AND WHAT IT CANNOT
=================================
It checks the SHAPE of the ledger: that a claim labelled ``verified`` carries an
``evidence`` field, that rounds do not skip numbers, that every record names a
known ``kind``.

It CANNOT check that the evidence is real. Nothing offline can prove a command
was executed; the tier label is a discipline, not a measurement. This is the
same boundary caveman states for its own labels ("offline never says
verified"), and it is why stop rules 2 and 3 key on the SUBJECT and the SURFACE
rather than on the reported finding count -- those two survive a round that was
not honestly reviewed, and rule 1 does not.

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
  0  ledger read; nothing the caller must act on (CONVERGED and advisory
     SURFACE-DEBT / self-review notes land here)
  1  a blocking stop rule fired (CHANGE-SUBJECT / UNREVIEWED-FIX) or the ledger
     breaks the format contract (verified claim with no evidence, banned
     ``speculative`` tier, non-integer surface counter, round numbers skipped)
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

KINDS = {"subject", "decidability", "finding", "dead-end", "question"}
TIERS = {"verified", "inferred"}
BANNED_TIERS = {"speculative"}
STATUSES = {"open", "fixed", "rejected", "deferred"}

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
    if not isinstance(rec.get("round"), int):
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
    """Fold records into Round objects keyed by round number, ascending."""
    rounds = {}
    for rec in records:
        num = rec.get("round")
        if not isinstance(num, int):
            continue
        rnd = rounds.setdefault(num, Round(num))
        kind = rec.get("kind")
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
    return [rounds[k] for k in sorted(rounds)]


def evaluate(rounds):
    """Apply the three stop rules plus the advisory notes.

    Returns (blocking, advisory) -- two lists of strings.
    """
    blocking, advisory = [], []
    if not rounds:
        return blocking, advisory

    numbers = [r.number for r in rounds]
    expected = list(range(numbers[0], numbers[0] + len(numbers)))
    if numbers != expected:
        blocking.append(
            f"LEDGER-GAP: round numbers {numbers} skip -- a missing round is a "
            "round whose findings never crossed into the next one")

    # Rule 2 -- same subject, two dead predicates already. Checked before rule 1
    # so a chain cannot report CONVERGED while sitting on a banned third version.
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
    last = rounds[-1]
    if last.fixed:
        blocking.append(
            f"UNREVIEWED-FIX: round {last.number} marks {last.fixed} finding(s) "
            "fixed and no later round declares a subject. The fix is a new "
            "surface, measured at 1.6x the reviewed one (#1431, 814 -> 1336 "
            "lines, where the second pass found the chain's only Critical). "
            "Open a round whose subject IS that fix.")

    # Rule 1 -- two consecutive quiet rounds.
    if len(rounds) >= 2 and not blocking:
        tail = rounds[-2:]
        if all(r.verified == 0 for r in tail):
            advisory.append(
                f"CONVERGED: rounds {tail[0].number} and {tail[1].number} each "
                "produced 0 verified findings. Stop -- a third confirming round "
                "is the surface growth this protocol exists to prevent.")

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
                "findings, author self-review 0). Treat as not-yet-reviewed.")

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
    for rnd in rounds:
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
        all_blocking.extend(blocking)
        all_advisory.extend(advisory)

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
