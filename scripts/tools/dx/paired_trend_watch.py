#!/usr/bin/env python3
"""Judge a series of PAIRED nightly runs for a sustained regression (ADR-032).

⛔ The first line above is deliberately ONE WHOLE SENTENCE on ONE LINE.
`generate_tool_map.py` publishes only a docstring's first line into
`docs/internal/tool-map{,.en}.md`, so a wrapped opening sentence ships to the
tool map truncated mid-clause — which is exactly what this module did until
review caught it ("...whether main has a sustained", no verb, no period, in
both languages). `converge_status.py` carries the same warning for the same
reason; this is the second time the trap has been walked into.

This is ADR-032 §待決 5, phase 2 / PR-B1.

    # replay the frozen six-night dataset (no network)
    python3 scripts/tools/dx/paired_trend_watch.py \
        --dataset docs/internal/audit-reports/bench-paired-2026-08

    # read the last N nights' real `bench-paired.json` artifacts
    python3 scripts/tools/dx/paired_trend_watch.py --from-gh --limit 14

⛔ SUMMARY-ONLY. THIS TOOL OPENS, UPDATES AND CLOSES NOTHING.
==============================================================
It writes a verdict to stdout and to `$GITHUB_STEP_SUMMARY`, and that is all.
It holds no issue state, parses no hidden marker, and calls no GitHub write API.

That restraint is the whole point of running it now. ADR-032 §待決 6 pinned
"新舊並行，不直接切換": the old absolute-series watchdog
(`analyze_bench_history.py --trend-watch`) keeps running and keeps owning the
`perf-trend` issue, while this one accumulates two-to-four weeks of verdicts
beside it so the two can be compared BEFORE anything is cut over. A summary-only
engine that turns out to be wrong costs a paragraph in a job summary; the same
engine wired to the closing path costs an auto-closed regression ticket, which
is issue #1432, which is why ADR-032 exists.

Why a separate module instead of a `--paired-watch` flag on the old watchdog:
that file is ~2200 lines whose centre of gravity is the marker state machine and
the open/update/close lifecycle. A flag would share those helpers, and "must not
touch the closing path" enforced by a branch is one careless import away from
being untrue. Physical separation makes it true by construction, and when the
parallel period ends the old file gets deleted rather than untangled.

THREE STATES, NOT TWO — AND THE SAME RULE ONE LEVEL UP
======================================================
`pair_bench_ratio.py` already refuses to let a benchmark it could not measure
look clean. This tool inherits that per-benchmark discipline and applies the
identical rule to the two levels above it:

    per benchmark  evaluated / inconclusive(reason)   (from the night's JSON)
    per night      counted / not-counted(reason) / unreadable(reason)
    per series     FINDINGS / CLEAR / INCONCLUSIVE

⛔ At every level, "could not measure" must never render as "measured and
clean". Every guard below exists because that conflation is the failure mode
this entire ADR line keeps re-learning, and because a wrong number that renders
correctly is worse than a missing one.

THE RULE, AS PINNED
===================
ADR-032 §待決 5 settled four parameters, and this implements them verbatim:

  * baseline is 1.000 by construction — no window median, no anchor
  * sustained := the ratio exceeded the threshold on K CONSECUTIVE nights (K=2)
  * threshold is FIXED at 5%, with 3/2/1% reported as counterfactuals so the
    two-to-four-week threshold decision has real data instead of an argument
  * the slow-creep rule is DELETED (it existed to chase a sliding anchor)
  * the control canary is a GATE on whether a night counts at all, NOT a
    per-benchmark noise floor — ADR-032 measured the IO/CPU correlation at 0.58,
    so a CPU-bound canary cannot scale an IO-bound benchmark's threshold

⚠️ THE GATE'S THRESHOLD IS PROVISIONAL. ADR-032 left it to "two to four weeks of
real distribution" and there is no measured value yet. The default here (1.0%)
is a placeholder that this tool exists to replace: every run prints what 0.5 /
1 / 2% would have done. Do not cite it as decided.

⚠️ AND THE GATE IS MEASURABLY WEAK. On the frozen six nights the canary never
deviated more than 0.12%, while two of those same nights carried a −19.34% and a
+25.02% reading on one benchmark. The gate answers "did the paired measurement
break tonight", not "is this benchmark trustworthy tonight" — those came apart
in the data (see `audit-reports/bench-paired-2026-08/README.md` §3). Any gate
value in the plausible range passes all six nights, so no threshold choice here
would have suppressed the outliers.

WHAT A NOT-COUNTED NIGHT DOES TO A RUN — AN OPEN QUESTION, NOT A DECISION
=========================================================================
ADR-032 says a gated-out night is "不判定也不關票". It does NOT say whether such a
night BREAKS a consecutive run or is SKIPPED over as if absent. The two differ:

    break  a noisy night resets the counter → detection is delayed, never invented
    skip   the run continues across the gap → asserts continuity across a night
           whose measurement was rejected

⛔ This tool defaults to BREAK and prints SKIP as a counterfactual, for two
reasons. Break is the conservative reading of "量不到 ≠ 量了沒事" applied to
continuity itself; and it is bit-identical to the semantics of the archived
`analyze_paired.py::fires()`, which is what keeps the regression anchor below a
real check rather than a restatement.

⚠️ The frozen six nights CANNOT decide this question: the canary gate never
fires on them, so both variants produce identical output. Do not read the anchor
test as evidence for the default. It is an owner decision, recorded here as open.

DIGEST TRANSITIONS — WHY THIS IS THE RIGHT PLACE FOR THEM
=========================================================
`pair_bench_ratio.py` records each night's `workload_digest` and explicitly
refuses to compare it with last night's: the nightly job holds no cross-run
state, because per-benchmark cross-run state is what sank the frozen-anchor
prototype (ADR-032 §凍結基準值). That constraint binds the NIGHTLY JOB. This
tool is not the nightly job — it already reads N nights to evaluate a K-night
rule, so deriving "the work definition moved on night X" costs it no new state
at all. `pair_bench_ratio.py`'s own docstring hands the derivation to "whoever
reads the series"; this is that reader.

Without this, the `workload_digest` field added in PR-A has no consumer at all
until PR-B2, i.e. it would sit unverified for weeks.

⛔ A digest that is absent (schema v1), `not-requested`, or `unreadable` yields
transition state UNKNOWN. It must never yield "did not move" — the digest exists
precisely to answer "did tonight move it", and a silent no is the wrong answer.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

# Same two sys.path inserts every sibling dx tool uses: parent for the repo
# layout where _lib_compat.py sits one directory up, self-dir for the flat
# Docker layout.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# ── The pinned rule (ADR-032 §待決 5) ─────────────────────────────────────
DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_CONSECUTIVE = 2
# ⚠️ PROVISIONAL — see the module docstring. Not a decided parameter.
DEFAULT_CANARY_GATE_PCT = 1.0

COUNTERFACTUAL_THRESHOLDS = (5.0, 3.0, 2.0, 1.0)
COUNTERFACTUAL_GATES = (0.5, 1.0, 2.0)

# Both canaries run the SAME compiled binary on both sides, so their true ratio
# is 1.000 by construction; any deviation is that night's residual paired noise.
# Neither is a product benchmark, so neither is ever judged for regressions.
CANARY_BENCHES = frozenset({
    "BenchmarkControlCanaryCPU",
    "BenchmarkControlCanarySleep",
})

# ⛔ ONLY THE CPU CANARY GATES. This is not a simplification — it is what the
# harness says, and an earlier version of this module got it wrong in the
# dangerous direction.
#
# `scripts/tools/ops/bench-canary/canary_test.go` states it in as many words:
#
#     BenchmarkControlCanaryCPU   — ... GATING: this is the canary that drives
#                                   the INCONCLUSIVE verdict.
#     BenchmarkControlCanarySleep — ... INFORMATIONAL ONLY: a 3-4% drift here
#                                   is only ~30-40us, well inside the jitter a
#                                   virtualised GH runner shows even when
#                                   healthy, so gating on it would flap
#                                   INCONCLUSIVE constantly. Emitted + rendered
#                                   for human eyes; NOT part of the gate
#                                   decision.
#
# and the other two consumers agree: `analyze_bench_history.py` has
# `CANARY_BENCH = "BenchmarkControlCanaryCPU"` (singular) and
# `bench_gate_compare.sh` greps `ControlCanaryCPU` for its presence fail-safe.
#
# ⛔ The earlier version gated on BOTH, and justified it in a comment claiming
# the two "are deliberately different shapes ... so they are not substitutes for
# one another". That sentence was invented, not read: it is true that they
# measure different things, and false that both therefore gate. Measured
# consequence — six nights with a benchmark at +9% EVERY night and the Sleep
# canary jittering 3% on alternate nights (the level `canary_test.go` calls
# healthy) never fires: `status: INCONCLUSIVE, fired: {}`. A guard that blanks
# the detector is the same disease as a guard that lets it lie, and this one was
# introduced while fixing the other.
GATING_CANARIES = frozenset({"BenchmarkControlCanaryCPU"})

# ⛔ v1 is accepted deliberately, not by omission. Every night before
# 2026-08-23 is v1, so rejecting it would leave the engine with no real series
# to run on for two weeks. What v1 does NOT have is `workload_digest`, and that
# absence is carried as DIGEST_ABSENT rather than quietly treated as "no move".
SUPPORTED_SCHEMAS = ("bench-paired/v1", "bench-paired/v2")

# The tri-state vocabulary `pair_bench_ratio.py` writes. Anything outside it is
# an input we do not understand, and an input we do not understand is unreadable
# — never clean.
DISCLOSURE_STATES = ("not-requested", "checked", "unreadable")
DIGEST_ABSENT = "absent-in-schema-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Night-level outcomes.
NIGHT_COUNTED = "counted"
NIGHT_NOT_COUNTED = "not-counted"
NIGHT_UNREADABLE = "unreadable"

# Series-level verdict.
STATUS_FINDINGS = "FINDINGS"
STATUS_CLEAR = "CLEAR"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"

# How a gated-out or unreadable night interacts with a consecutive run.
GAP_BREAK = "break"
GAP_SKIP = "skip"

# ⛔ Two codes, and they mean different things — 0 "reported a verdict" and
# 2 "could not do the job". 1 is never CHOSEN, so it can never be read as
# "found a regression": this tool is a reporter, and a reporter that exits
# non-zero on FINDINGS is a gate wearing a reporter's docstring.
#
# ⚠️ "Never chosen" is the honest claim, not "never seen". An unanticipated
# exception still leaves Python's default 1 — with a traceback, which is what
# distinguishes it from a verdict. That is precisely why every input boundary
# below carries its own shape guard converting malformed input into a named
# exit 2: the contract is kept by the guards, not by a catch-all, because a
# catch-all would turn a real logic bug into a tidy "could not check".
#
# 2, not 1, for the cannot-check case, because that is the repo's convention and
# the distinction is load-bearing here: `check_workload_closure_drift.py` pins
# exactly these three codes and its tests say collapsing any two of them IS the
# bug class. Reported in review — this exited 1 with a raw traceback when `gh`
# failed, while the sibling `analyze_bench_history.py` printed one line and
# exited 2 on the identical failure.
EXIT_OK = 0
EXIT_CANNOT_CHECK = 2


class Night:
    """One night's `bench-paired.json`, after the input guards have run.

    A Night is always constructed — a bad input produces a Night whose
    ``outcome`` is ``unreadable`` and whose ``reason`` names what broke. It is
    never dropped: a night that vanishes from the series silently shortens the
    window, which is the same false-all-clear in a different costume.
    """

    __slots__ = ("night_utc", "run_id", "schema", "cpu", "reference_sha",
                 "ratios_pct", "canary_pct", "inconclusive",
                 "drift_status", "drift_files", "drift_count",
                 "digest_status", "digest_sides", "digest_n_files",
                 "outcome", "reason")

    def __init__(self, night_utc, run_id):
        self.night_utc = night_utc
        self.run_id = run_id
        self.schema = None
        self.cpu = None
        self.reference_sha = None
        self.ratios_pct = {}      # non-canary, evaluated only
        self.canary_pct = {}
        self.inconclusive = {}    # bench -> reason
        self.drift_status = DIGEST_ABSENT
        self.drift_files = []
        # ⛔ Separate from len(drift_files) on purpose. The archival snapshot
        # carries a COUNT with no names (the job log never had them), so
        # rendering len() of a one-element placeholder would print "1 file"
        # where the night actually saw 4 — a wrong number that renders
        # perfectly. Count and identities are different facts; keep them apart.
        self.drift_count = None
        self.digest_status = DIGEST_ABSENT
        self.digest_sides = {}    # side -> digest hex
        self.digest_n_files = {}  # side -> int
        self.outcome = NIGHT_COUNTED
        self.reason = None

    @property
    def readable(self):
        return self.outcome != NIGHT_UNREADABLE

    def unreadable(self, reason):
        self.outcome = NIGHT_UNREADABLE
        self.reason = reason
        return self

    @property
    def unreadable_canaries(self):
        """Canaries the payload TRIED to produce and failed on.

        A canary whose ratio was NaN or whose record was malformed landed in
        `inconclusive` — evidence about the measurement, not absence of it.
        """
        return sorted(b for b in self.inconclusive if b in GATING_CANARIES)

    @property
    def missing_canaries(self):
        """Declared canaries the payload does not mention AT ALL.

        ⛔ A third case, and review found it open after the first two were
        closed. `unreadable_canaries` only sees a canary that reached
        `inconclusive`; one that appears in NEITHER bucket was invisible, so the
        gate judged the night on the surviving canary alone and rendered
        `counted | +0.01%`. Reachable without anything exotic:
        `pair_bench_ratio.py` builds its benchmark set from
        `set(reference) | set(main)`, so a name absent from both sides' raw
        `go test -bench` output — a `-bench` filter that stops matching, a
        renamed function — never enters the payload in any form.

        ⚠️ This requires the GATING set, not every declared canary — an
        informational canary going missing must not discard a night.

        ⛔ CORRECTION: this docstring used to say "the DECLARED set" and cite
        the `CANARY_BENCHES` pin as the friction that keeps it safe. Both
        halves were wrong after the gating split, and wrong in a directive way:
        they told the next person the pin covers the set that gates. It did
        not. Adding a third GATING canary to `canary_test.go` would have forced
        an update to `CANARY_BENCHES` — and silently demoted the new canary to
        informational. `test_canary_roles_match_the_real_benchmark_source` now
        pins BOTH sets, reading each canary's role out of the Go doc comment
        ("GATING:" vs "INFORMATIONAL ONLY"), so neither can drift quietly.
        """
        seen = set(self.ratios_pct) | set(self.canary_pct) | set(self.inconclusive)
        return sorted(GATING_CANARIES - seen)

    @property
    def canary_deviation_pct(self):
        """Worst |ratio - 1| across the canaries that were read, or None.

        ⛔ None means "the gate could not be evaluated", which fails closed at
        the call site. It does not mean zero.

        ⛔ This is the WORST of the READABLE canaries only, so it is not on its
        own a sufficient gate input — see `unreadable_canaries`. Reported in
        review: with two canaries configured, one returning NaN and the other
        calm, the max over the survivor is a reassuring +0.01% and the night
        was counted. Half the gate's instrumentation had failed and the page
        said so nowhere. The gate now consults both properties.
        """
        readings = [v for b, v in self.canary_pct.items()
                    if b in GATING_CANARIES]
        if not readings:
            return None
        # ⛔ SIGNED, and the magnitude is taken at the comparison instead.
        # This used to return `max(abs(...))`, so a canary measured at −0.12%
        # rendered as `+0.12%` — the page asserting main was SLOWER on the
        # control when it was faster, next to an informational column carrying
        # the true sign. For a control canary the direction is the diagnostic
        # (thermal throttle vs frequency boost), and "a wrong number that
        # renders correctly" is this module's own worst case.
        return max(readings, key=abs)

    @property
    def informational_canary_pct(self):
        """Non-gating canary readings, for display only.

        `canary_test.go` says the sleep canary is "emitted + rendered for human
        eyes; NOT part of the gate decision" — so it is rendered, and it does
        not decide anything.
        """
        return {b: v for b, v in self.canary_pct.items()
                if b not in GATING_CANARIES}


def _finite_pct(value):
    """Return a percent reading, or None if the input is not a usable number.

    ⛔ NaN and ±inf are rejected explicitly. They compare False against every
    threshold, so an unfiltered NaN is a benchmark that is silently never
    flagged — a false all-clear that renders as a perfectly normal row.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


def _ratio_to_pct(value):
    """`ratio` (main/reference, 1.000 == parity) → percent change.

    ⛔ A non-positive ratio is rejected rather than converted: a median of zero
    or a negative duration is a broken measurement, and -100% would render as a
    spectacular improvement.
    """
    ratio = _finite_pct(value)
    if ratio is None or ratio <= 0:
        return None
    return (ratio - 1.0) * 100.0


def _read_disclosure(obj, key):
    """Read a `workload_drift` / `workload_digest` block's tri-state status.

    Returns (status, payload). A block that is absent yields DIGEST_ABSENT
    (schema v1); a block whose status is outside the pinned vocabulary yields
    `unreadable`, because an unrecognised state is not a state we may interpret.
    """
    block = obj.get(key)
    if block is None:
        return DIGEST_ABSENT, {}
    if not isinstance(block, dict):
        return "unreadable", {}
    status = block.get("status")
    if status not in DISCLOSURE_STATES:
        return "unreadable", {}
    return status, block


def load_night(payload, *, night_utc, run_id):
    """Turn one parsed `bench-paired.json` into a Night, guards first.

    Every rejection below was chosen because the un-guarded behaviour renders
    normally. That is the selection criterion: a malformed input that crashes is
    an inconvenience, a malformed input that prints a confident wrong verdict is
    the defect ADR-032 exists to remove.
    """
    night = Night(night_utc, run_id)

    if not isinstance(payload, dict):
        return night.unreadable(
            f"payload is {type(payload).__name__}, expected a JSON object")

    night.schema = payload.get("schema")
    if night.schema not in SUPPORTED_SCHEMAS:
        return night.unreadable(
            f"unsupported schema {night.schema!r} (known: "
            f"{', '.join(SUPPORTED_SCHEMAS)}) — refusing to interpret fields "
            "whose meaning this tool does not know")

    night.cpu = payload.get("cpu")
    night.reference_sha = payload.get("reference_sha")

    status = payload.get("status")
    if status not in ("OK", "INCONCLUSIVE"):
        return night.unreadable(f"unknown night status {status!r}")
    if status == "INCONCLUSIVE":
        # The nightly's own fallback: the reference could not be built or
        # measured (ADR-032 §待決 3). It is data, not an error — but it is
        # emphatically not a clean night.
        return night.unreadable(
            f"nightly reported INCONCLUSIVE: {payload.get('reason') or 'no reason given'}")

    evaluated = payload.get("evaluated")
    if not isinstance(evaluated, dict):
        return night.unreadable(
            f"'evaluated' is {type(evaluated).__name__}, expected an object")

    for bench, rec in evaluated.items():
        if not isinstance(rec, dict):
            night.inconclusive[bench] = "unreadable-record"
            continue
        pct = _ratio_to_pct(rec.get("ratio"))
        if pct is None:
            # ⛔ Into `inconclusive`, never dropped. A dropped benchmark is one
            # the series stops asking about, which is silence by attrition.
            night.inconclusive[bench] = f"unreadable-ratio ({rec.get('ratio')!r})"
            continue
        if bench in CANARY_BENCHES:
            night.canary_pct[bench] = pct
        else:
            night.ratios_pct[bench] = pct

    incon = payload.get("inconclusive")
    if not isinstance(incon, dict):
        # ⛔ Absent is NOT empty, and the sibling key already knew that: an
        # absent `evaluated` is unreadable, while an absent `inconclusive` used
        # to mean "no benchmark lacked a denominator". `pair_bench_ratio.py`
        # argues the mirror case in its own source — "a success payload that
        # simply omitted the key would force every consumer to encode 'absent
        # means OK', a default that is wrong the first time a third status
        # exists". This consumer encoded exactly that default. Reported in review.
        return night.unreadable(
            f"'inconclusive' is {type(incon).__name__}, expected an object — "
            "an absent disclosure is not an empty one")
    for bench, rec in incon.items():
        reason = rec.get("reason") if isinstance(rec, dict) else rec
        night.inconclusive.setdefault(bench, str(reason or "unspecified"))

    night.drift_status, drift = _read_disclosure(payload, "workload_drift")
    if night.drift_status == "checked":
        files = drift.get("files")
        if not isinstance(files, list):
            # ⛔ Fail closed, matching the digest path three lines down.
            # Reported in review as an asymmetry: `checked` with a missing or
            # non-list `files` rendered "checked, 0 file(s)" — byte-identical to
            # a genuinely clean night, and the opposite claim.
            night.drift_status = "unreadable"
        else:
            night.drift_files = [str(f) for f in files]
            night.drift_count = len(night.drift_files)

    night.digest_status, digest = _read_disclosure(payload, "workload_digest")
    if night.digest_status == "checked":
        sides = digest.get("sides")
        if not isinstance(sides, dict):
            night.digest_status = "unreadable"
        else:
            for side in ("reference", "main"):
                rec = sides.get(side)
                value = rec.get("digest") if isinstance(rec, dict) else None
                if not isinstance(value, str) or not _SHA256_RE.match(value):
                    # ⛔ Shape-checked, exactly as `pair_bench_ratio.py` does at
                    # its own input boundary. A digest that is not a digest still
                    # compares unequal night-to-night, i.e. it manufactures a
                    # work-definition transition out of nothing.
                    night.digest_status = "unreadable"
                    night.digest_sides = {}
                    night.digest_n_files = {}
                    break
                night.digest_sides[side] = value
                n = rec.get("n_files")
                if isinstance(n, int) and not isinstance(n, bool):
                    night.digest_n_files[side] = n
    return night


def gate_verdict(night, gate_pct):
    """Would this readable night count at `gate_pct`? → (counts, reason).

    ⛔ ONE implementation, deliberately. The counterfactual table used to carry
    a second copy of this logic, and review caught the predictable result: the
    partial-canary fix landed in `apply_gate` and not in the copy, so the main
    table said `not-counted` while the "Canary gate" table three sections below
    reported `0 nights rejected` at the very same gate value. That table is the
    data ADR-032 §待決 5 says will decide the real threshold, and it was
    under-counting exactly the rejections that mean "could not evaluate".
    """
    deviation = night.canary_deviation_pct
    broken = night.unreadable_canaries + night.missing_canaries
    # ⛔ The specific reason first. With one gating canary, "it is broken" and
    # "there is no reading" are the same night, and the generic message wins the
    # race unless this is ordered — losing the one thing an operator needs,
    # which is WHICH canary.
    if broken:
        # ⛔ Partial instrumentation is not instrumentation: a gating canary
        # that is missing or unreadable means the night has no complete
        # evidence its paired measurement held.
        #
        # ⛔ The justification that used to sit here is the one the header
        # records as INVENTED rather than read (see GATING_CANARIES). It is
        # deleted rather than quoted again: an overturned rationale needs
        # exactly ONE record — the correction — and a second copy, even one
        # framed as "this was wrong", is still the sentence sitting in the
        # reader's path.
        #
        # ⛔ And "the remaining canary" only exists if one remains. With a
        # single gating canary the old wording produced a message that named
        # the CPU canary as not-established and, in the same sentence, quoted
        # "the remaining gating canary read +0.02%" — which was that same
        # canary's own reading.
        others = sorted(set(GATING_CANARIES) - set(broken))
        readable = [night.canary_pct[b] for b in others if b in night.canary_pct]
        rest = ("" if not readable else
                f" — the remaining gating canary read "
                f"{max(readable, key=abs):+.2f}%, which is evidence about that "
                "canary only, not about the night")
        return False, ("control canary not established: "
                       + ", ".join(broken) + rest)
    if deviation is None:
        return False, ("no control-canary reading — the gate cannot be "
                       "evaluated, so this night is not counted")
    if abs(deviation) > gate_pct:
        return False, (f"control canary deviated {deviation:+.2f}% "
                       f"(gate ±{gate_pct:.2f}%) — paired measurement suspect")
    return True, None


def apply_gate(night, gate_pct):
    """Stamp a readable night's outcome from `gate_verdict`.

    ⛔ Fails closed. A night with no canary reading has no evidence that the
    paired measurement worked, and "no evidence it broke" is not "evidence it
    did not". That asymmetry is the entire subject of this ADR.
    """
    if not night.readable:
        return night
    counts, reason = gate_verdict(night, gate_pct)
    night.outcome = NIGHT_COUNTED if counts else NIGHT_NOT_COUNTED
    night.reason = reason
    return night


def calendar_nights(nights):
    """Group runs by `night_utc`, preserving order, one entry per CALENDAR night.

    ⛔ Added after review: `fires()` walked the run list, so K counted LIST
    ENTRIES satisfied "K consecutive nights". Two runs sharing a date — routine,
    since `bench-record.yaml` carries `workflow_dispatch` alongside the cron and
    a flaky nightly gets re-run — met k=2 from ONE measurement occasion and
    rendered `**FINDINGS** over 2 counted night(s) ... (2026-08-20 ..
    2026-08-20)`. The span printed the same date twice and nothing flagged it.
    The rule's entire purpose is to not fire on a single occasion.
    """
    groups = {}
    for night in nights:
        groups.setdefault(night.night_utc, []).append(night)
    return [(date, runs) for date, runs in groups.items()]


def _night_reading(runs, bench, threshold_pct):
    """Is `bench` over the threshold on this calendar night?

    Returns True / False / None, where None is UNDECIDABLE and breaks a run.

    ⛔ Two counted runs of the same calendar night that DISAGREE about the
    threshold are not a majority to be taken; they are the night failing to
    produce an answer. Fail closed — never pick one, never average. Averaging
    would invent a reading nobody measured, which is the one thing every guard
    in this file exists to prevent.
    """
    counted = [n for n in runs if n.outcome == NIGHT_COUNTED]
    if not counted:
        return None
    verdicts = set()
    for night in counted:
        value = night.ratios_pct.get(bench)
        if value is None:
            return None      # "not measured" is not "under the threshold"
        verdicts.add(value > threshold_pct)
    if len(verdicts) != 1:
        return None
    return verdicts.pop()


def fires(nights, benches, threshold_pct, k, gap=GAP_BREAK):
    """First CALENDAR NIGHT on which a benchmark completed `k` consecutive
    nights over `threshold_pct`.

    ⛔ For a series with one run per night — every real series so far — this is
    bit-identical to the archived
    `audit-reports/bench-paired-2026-08/analyze_paired.py::fires()`, so
    replaying the frozen dataset stays a real cross-check against an
    independently written implementation rather than a restatement of this one.
    A night with no reading for a benchmark BREAKS the run: "not measured" is
    not "under the threshold".
    """
    grouped = calendar_nights(nights)
    out = {}
    for bench in benches:
        run = 0
        for date, runs in grouped:
            over = _night_reading(runs, bench, threshold_pct)
            if over is None:
                if gap == GAP_SKIP and not any(
                        n.outcome == NIGHT_COUNTED for n in runs):
                    continue
                run = 0
                continue
            run = run + 1 if over else 0
            if run >= k:
                out[bench] = date
                break
    return out


def judgeable(nights, benches, threshold_pct, k, gap=GAP_BREAK):
    """Which benchmarks the K-consecutive rule was even ABLE to judge.

    ⛔ THE RULE CANNOT RETURN A VERDICT IT WAS NEVER IN A POSITION TO REACH.
    A benchmark needs K consecutive counted calendar nights, each carrying a
    reading, before "over the threshold on K consecutive nights" is a question
    with an answer. Below that, `fires()` returns nothing — and "nothing fired"
    was being rendered as CLEAR.

    Reported in review, and it is the most serious defect this change had.
    Measured: six nights, one benchmark at **+42% on every single night**, with
    the canary gating out alternate nights so no two counted nights are
    adjacent. Verdict `CLEAR`, and the benchmark's name and its +42% appeared
    NOWHERE in the rendered report. The job's own first nights hit this too —
    it runs `--from-gh --limit 14` and nights that predate the paired pipeline
    are `unreadable`, so there is exactly one counted night at first.

    ⛔ ADR-032 §待決 6 pinned this requirement in as many words, and this
    implementation of that ADR broke it:

        切換當下比值序列從零開始，持續判準需要幾夜才能發射
        ——那幾夜必須回報「無法評估」，不得回報「無發現」。

    ("At cutover the ratio series starts from nothing and the sustained rule
    needs several nights before it can fire — those nights MUST report
    無法評估 / INCONCLUSIVE, and must NOT report 無發現 / CLEAR.")
    """
    grouped = calendar_nights(nights)
    out = {}
    for bench in benches:
        best = run = 0
        for _date, runs in grouped:
            counted = [n for n in runs if n.outcome == NIGHT_COUNTED]
            # ⛔ `_night_reading`, not a presence check. Review found the seam:
            # this used to ask "does every counted run carry a reading", while
            # `fires()` asks "do they AGREE about the threshold". A calendar
            # night measured twice with the two runs straddling the threshold
            # (5.1 and 4.9) is undecidable to `fires()` and was judgeable here —
            # so a window in which EVERY night was undecidable rendered as
            # CLEAR. One predicate now answers both questions, which is the only
            # way the seam cannot reopen.
            if counted and _night_reading(runs, bench, threshold_pct) is not None:
                run += 1
                best = max(best, run)
                continue
            # ⛔ `gap` must mean the same thing here as in `fires()`. Review
            # caught them disagreeing: `fires()` honoured `--gap skip` and this
            # did not, so under `skip` a benchmark that DID fire was listed in
            # the same report as one the rule "was not in a position to judge",
            # and the verdict was downgraded from FINDINGS to INCONCLUSIVE.
            # Worse, that switch is precisely the open question this tool exists
            # to help the owner decide — adopting `skip` would have turned every
            # sustained regression into an INCONCLUSIVE.
            if gap == GAP_SKIP and not counted:
                continue
            run = 0
        out[bench] = best >= k
    return out


def digest_transitions(nights):
    """Per consecutive night pair, did each side's work definition move?

    Yields dicts with `moved` in {True, False, None}; None is UNKNOWN and is
    what an absent / not-requested / unreadable digest produces on either end.
    ⛔ UNKNOWN is never rendered as "did not move".
    """
    out = []
    # ⛔ One entry per calendar night, so a re-run cannot emit a
    # `2026-08-20 → 2026-08-20` transition row. The night's LAST readable run
    # is the one carried, matching "what the work definition was by the end of
    # that night".
    per_night = {}
    for night in nights:
        if night.readable:
            per_night[night.night_utc] = night
    usable = list(per_night.values())
    for prev, cur in zip(usable, usable[1:]):
        rec = {"from": prev.night_utc, "to": cur.night_utc, "sides": {}}
        for side in ("reference", "main"):
            if (prev.digest_status != "checked" or cur.digest_status != "checked"
                    or side not in prev.digest_sides or side not in cur.digest_sides):
                rec["sides"][side] = None
                continue
            rec["sides"][side] = prev.digest_sides[side] != cur.digest_sides[side]
        # A reference SHA change is an ABSORPTION EVENT (ADR-032 §待決 1), which
        # is a different and much louder thing than a fixture edit on main.
        #
        # ⛔ Tri-state, like everything else on this row. It used to return
        # False when either side had no `reference_sha`, so the column rendered
        # "same" for a pin that was never compared — printed directly above the
        # note forbidding exactly that reading.
        #
        # ⛔ CORRECTION. The first version of this comment justified the fix
        # with "schema v1 has no `reference_sha`". That is FALSE and a later
        # review measured it false: `pair_bench_ratio.py` has written
        # `reference_sha` into the v1 payload since #1455 (`60f4523`,
        # 2026-08-16), which covers every v1 night a 14-night window can reach.
        # The one earlier shape that lacks it (#1441) also lacks `status`, so
        # `load_night` marks it unreadable and `digest_transitions` — which
        # walks readable nights only — never sees it.
        #
        # The fix stands; the reachable route is different. `--reference-sha`
        # defaults to `None` (`pair_bench_ratio.py`), so a perfectly ordinary
        # v2 payload can carry `"reference_sha": null` — verified by running
        # that tool without the flag.
        if prev.reference_sha is None or cur.reference_sha is None:
            rec["reference_pin_changed"] = None
        else:
            rec["reference_pin_changed"] = prev.reference_sha != cur.reference_sha
        out.append(rec)
    return out


def decide(nights, *, threshold_pct=DEFAULT_THRESHOLD_PCT,
           k=DEFAULT_CONSECUTIVE, gate_pct=DEFAULT_CANARY_GATE_PCT,
           gap=GAP_BREAK):
    """The whole engine, as one pure function over already-loaded nights.

    No IO, no clock, no network — so every rule below is testable by handing it
    a list of Nights, and the CI path and the offline replay path exercise
    exactly the same code.
    """
    nights = sorted(nights, key=lambda n: (n.night_utc or "", n.run_id or 0))
    for night in nights:
        apply_gate(night, gate_pct)

    counted = [n for n in nights if n.outcome == NIGHT_COUNTED]
    benches = sorted({b for n in counted for b in n.ratios_pct})
    fired = fires(nights, benches, threshold_pct, k, gap=gap)

    # ⛔ Any benchmark that some counted night could not evaluate is reported,
    # even if other nights did evaluate it. The question a reader has is "what
    # did tonight fail to see", and answering it only for benchmarks that failed
    # everywhere is the saturating-list mistake from PR-A one level up.
    #
    # ⛔ That claim used to be FALSE, and review measured it false: this loop
    # read only `night.inconclusive`, so a benchmark that a counted night simply
    # did not carry — in neither `evaluated` nor `inconclusive` — was invisible,
    # and three counted nights with one benchmark missing from the middle one
    # rendered `CLEAR` without naming it. The union pass below makes the claim
    # true rather than softening it.
    inconclusive = {}
    for night in counted:
        for bench, reason in night.inconclusive.items():
            inconclusive.setdefault(bench, {})[night.night_utc] = reason
    for night in counted:
        for bench in benches:
            if bench in night.ratios_pct or bench in night.inconclusive:
                continue
            inconclusive.setdefault(bench, {})[night.night_utc] = (
                "absent-from-payload — the night carried neither a ratio nor a "
                "reason for this benchmark")

    can_judge = judgeable(nights, benches, threshold_pct, k, gap=gap)
    unjudgeable = sorted(b for b, ok in can_judge.items() if not ok)

    # Readings that crossed the threshold at least once without ever completing
    # K consecutive nights. Previously invisible: not a fire, so nothing on the
    # page mentioned them at all.
    # ⛔ Keyed by CALENDAR NIGHT, not by run. Review caught the same run-vs-night
    # confusion the header already had: a re-run of one night rendered as
    # "2 (08-20, 08-20)". The worst reading of that night is kept.
    seen_over = {}
    for night in counted:
        for bench, value in night.ratios_pct.items():
            if value > threshold_pct and bench not in fired:
                per_night = seen_over.setdefault(bench, {})
                if value > per_night.get(night.night_utc, float("-inf")):
                    per_night[night.night_utc] = value

    if not counted or not benches or not any(can_judge.values()):
        # ⛔ Not CLEAR. No benchmark was in a position to be judged, so the
        # window established nothing — see `judgeable()`.
        status = STATUS_INCONCLUSIVE
    elif fired:
        status = STATUS_FINDINGS
    else:
        status = STATUS_CLEAR

    return {
        "status": status,
        "threshold_pct": threshold_pct,
        "k": k,
        "gate_pct": gate_pct,
        "gap": gap,
        "nights": nights,
        "counted": counted,
        "benches": benches,
        "fired": fired,
        "inconclusive": inconclusive,
        "unjudgeable": unjudgeable,
        "over_not_sustained": seen_over,
        "transitions": digest_transitions(nights),
    }


# ── Adapters ──────────────────────────────────────────────────────────────
#
# Two ways in, one decision core. The CI path reads real `bench-paired.json`
# artifacts; the replay path reads the frozen six-night archival dataset. Both
# produce Night objects and hand them to the same `decide()`.

def nights_from_paths(pairs):
    """Load Nights from (night_utc, run_id, path-to-bench-paired.json) triples.

    ⛔ A file that will not parse becomes an UNREADABLE Night, not a skipped
    one. Skipping shortens the window silently, and a silently shorter window
    makes a K-night rule fire later than the operator believes it does.
    """
    out = []
    for night_utc, run_id, path in pairs:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            night = Night(night_utc, run_id)
            out.append(night.unreadable(f"cannot read {path}: {exc}"))
            continue
        out.append(load_night(payload, night_utc=night_utc, run_id=run_id))
    return out


# The archival dataset's own contract, re-declared here rather than imported.
# ⛔ Deliberate duplication: `analyze_paired.py` lives under `audit-reports/`
# and is pinned to that snapshot. Importing it would couple this tool's
# lifetime to a frozen artefact, and — worse — a shared constant would make the
# cross-check circular. The point of replaying that dataset is that TWO
# independently written implementations agree on the same numbers.
DATASET_SCHEMA = "bench-paired-series/v1"
DATASET_UNIT = "percent change of main vs the pinned reference (M/R - 1) * 100"


def nights_from_dataset(directory):
    """Load Nights from an archival `nights.json` (the frozen six-night set).

    ⚠️ SCOPE OF WHAT THIS VALIDATES. This path builds Nights from percent
    readings directly, so it exercises the RULE (`fires`, the gate, the verdict)
    and NOT the `bench-paired.json` shape guards in `load_night()` — the archival
    schema is a different, already-validated format. The shape guards are
    covered by their own tests against real-shaped payloads. Saying so matters:
    "the replay passes" would otherwise be read as "the input boundary is
    verified", which is the same could-not-measure/measured-clean conflation
    this file is built to refuse, applied to its own test evidence.

    ⚠️ Conversion is deliberately absent. The dataset already holds percent, so
    nothing is reconstructed into a ratio and back — a round trip through
    `(1 + p/100)` would introduce float error into the one comparison
    (`value > threshold`) that the anchor check depends on.
    """
    path = Path(directory) / "nights.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    # ⛔ Shape first. `load_night()` has had this guard from the start; this
    # adapter did not, so a top-level JSON array crashed with
    # `AttributeError: 'list' object has no attribute 'get'` and exit 1 — the
    # code this module reserves and documents as NEVER USED, so that a
    # non-zero exit can never be read as "found a regression". Review found it
    # after the identical shape had already been fixed on the `--from-gh` side:
    # same defect, second entrance.
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: top level is {type(payload).__name__}, "
                         "expected a JSON object")
    reference = payload.get("reference")
    if reference is not None and not isinstance(reference, dict):
        raise ValueError(f"{path}: 'reference' is {type(reference).__name__}, "
                         "expected an object")

    if payload.get("schema") != DATASET_SCHEMA:
        raise ValueError(f"{path}: unsupported dataset schema "
                         f"{payload.get('schema')!r} (expected {DATASET_SCHEMA!r})")
    if payload.get("unit") != DATASET_UNIT:
        raise ValueError(f"{path}: unexpected unit {payload.get('unit')!r} — every "
                         "comparison below assumes a percent ratio")

    reference_sha = (reference or {}).get("sha")
    nights = payload.get("nights")
    if not isinstance(nights, list) or not nights:
        raise ValueError(f"{path}: 'nights' must be a non-empty list")

    out = []
    for rec in nights:
        if not isinstance(rec, dict):
            raise ValueError(f"{path}: every night must be an object")
        night = Night(rec.get("night_utc"), rec.get("run_id"))
        night.schema = DATASET_SCHEMA
        # The archival snapshot names it `cpu_model`; the nightly JSON names it
        # `cpu`. Both are accepted so neither adapter has to lie about the other.
        night.cpu = rec.get("cpu_model", rec.get("cpu"))
        night.reference_sha = reference_sha
        ratios = rec.get("ratios_pct")
        if not isinstance(ratios, dict) or not ratios:
            out.append(night.unreadable("night carries no ratios_pct mapping"))
            continue
        for bench, value in ratios.items():
            pct = _finite_pct(value)
            if pct is None:
                night.inconclusive[bench] = f"unreadable-ratio ({value!r})"
            elif bench in CANARY_BENCHES:
                night.canary_pct[bench] = pct
            else:
                night.ratios_pct[bench] = pct
        # The dataset records the drift STATUS per night but never carried a
        # digest — it predates PR-A. Absent, therefore UNKNOWN, therefore no
        # transition may be claimed from it either way.
        # The snapshot archived the drift disclosure as PROSE ("absent",
        # "4 files") because that is all the job log carried — the file names
        # were never in the log. So a count survives and the identities do not.
        # ⛔ "absent" here means the 2026-08-16 run predates `workload_drift`
        # existing at all (#1455 merged later that day). It is carried as
        # `absent`, never as `checked` with zero files, because those two render
        # the same in a count and mean opposite things.
        drift = rec.get("workload_drift")
        if isinstance(drift, dict) and drift.get("status") in DISCLOSURE_STATES:
            night.drift_status = drift["status"]
            files = drift.get("files")
            night.drift_files = [str(f) for f in files] if isinstance(files, list) else []
            if night.drift_status == "checked":
                night.drift_count = len(night.drift_files)
        elif isinstance(drift, str):
            match = re.match(r"^(\d+) files?$", drift.strip())
            if match:
                night.drift_status = "checked"
                night.drift_count = int(match.group(1))
                night.drift_files = []  # names were never in the job log
            else:
                night.drift_status = drift.strip() or "unreadable"
        out.append(night)
    return out


def nights_from_gh(workflow, limit, cache_dir):
    """Download the last N nightly runs' `bench-paired.json` via `gh`.

    ⛔ The two `gh` wrappers are IMPORTED from `analyze_bench_history`, not
    copied — one implementation of "list runs" and "download this run's
    artifact", so a fix to either reaches both. Nothing else is imported from
    it, and in particular no function that opens, comments on, or closes an
    issue. `tests/dx/test_paired_trend_watch.py` asserts that by name, so the
    summary-only property is checked rather than promised.
    """
    from analyze_bench_history import download_artifact, list_recent_runs

    runs = list_recent_runs(workflow, limit)
    pairs = []
    for run in runs:
        run_id = run.get("databaseId")
        created = str(run.get("createdAt") or "")
        night_utc = created[:10] or None
        txt = download_artifact(run_id, Path(cache_dir))
        if txt is None:
            night = Night(night_utc, run_id)
            pairs.append(night.unreadable("artifact download failed or incomplete"))
            continue
        paired = txt.parent / "bench-paired.json"
        if not paired.exists():
            # Nights before ADR-032's measurement pipeline landed have no paired
            # file at all. Not clean, not an error — unreadable, and named.
            night = Night(night_utc, run_id)
            pairs.append(night.unreadable(
                "run predates the paired pipeline (no bench-paired.json in artifact)"))
            continue
        pairs.append((night_utc, run_id, paired))

    ready = [p for p in pairs if isinstance(p, tuple)]
    failed = [p for p in pairs if not isinstance(p, tuple)]
    return nights_from_paths(ready) + failed


# ── Rendering ─────────────────────────────────────────────────────────────
#
# English, like every sibling under `scripts/tools/dx/` and like the existing
# `bench-record` step summary this appends to (owner ruling on #1496).

def _pct(value):
    return f"{value:+.2f}%"


def counterfactual_thresholds(nights, benches, gap):
    """What each candidate threshold WOULD have opened, on this same series.

    ADR-032 §待決 5 pinned 5% "先維持" and asked for two-to-four weeks of real
    distribution before deciding. This is that data, produced every run at no
    extra measurement cost — the alternative is deciding the threshold from the
    experiment harness's median, which the ADR explicitly warns against.

    ⛔ EACH ROW CARRIES ITS OWN JUDGEABILITY. `fires()` returns `{}` both when
    the rule ran and found nothing and when it could not produce an answer at
    all, and printing `0` for both is this module's governing failure one level
    down — in the one table ADR-032 §待決 5 designates as the data that will
    pick the real threshold.

    Two ways it bites, both measured. A two-night series makes every `K=3` row
    arithmetically unreachable, and all four printed `0`. And since
    `judgeable()` became threshold-dependent, a series whose same-night re-runs
    straddle 3% reads "5%: 0, 3%: 0, 2%: 0, 1%: 1" — from which a maintainer
    concludes "tightening to 3% buys nothing, go to 2%", when at 3% and 2% the
    rule produced no answer whatsoever.

    (`gate_pct` used to be a parameter here and was never read — the vestige of
    the same intent, removed rather than left as decoration.)
    """
    rows = []
    for threshold in COUNTERFACTUAL_THRESHOLDS:
        for k in (2, 3):
            fired = fires(nights, benches, threshold, k, gap=gap)
            can_judge = judgeable(nights, benches, threshold, k, gap=gap)
            rows.append((threshold, k, fired,
                         sorted(b for b, ok in can_judge.items() if not ok)))
    return rows


def counterfactual_gates(nights):
    """How many nights each candidate canary gate would reject.

    ⚠️ Expect this to be flat. On the frozen six nights the canary never moved
    more than 0.12%, so every candidate gate keeps every night — which is itself
    the finding: the gate is not what suppresses the outliers, and nobody should
    expect it to.
    """
    rows = []
    readable = [n for n in nights if n.readable]
    for gate in COUNTERFACTUAL_GATES:
        # ⛔ Distinct calendar nights, same reason as above — this table is the
        # data ADR-032 §待決 5 says will pick the real threshold, so a re-run
        # inflating its rejection count biases the decision it exists to feed.
        rejected = sorted({n.night_utc for n in readable
                           if not gate_verdict(n, gate)[0]})
        rows.append((gate, rejected))
    return rows


def render(result):
    """Render the verdict as GitHub-flavoured Markdown."""
    nights = result["nights"]
    counted = result["counted"]
    lines = []
    span = (f"{nights[0].night_utc} .. {nights[-1].night_utc}" if nights else "empty series")

    lines.append("## Paired trend watch — SUMMARY ONLY (ADR-032 phase 2, PR-B1)")
    lines.append("")
    n_calendar = len({n.night_utc for n in counted})
    lines.append(f"**{result['status']}** over {n_calendar} counted calendar "
                 f"night(s) ({len(counted)} run(s)) of {len(nights)} in the "
                 f"series ({span}).")
    if n_calendar != len(counted):
        lines.append("")
        lines.append("⚠️ Some calendar nights carry more than one run "
                     "(a `workflow_dispatch` or a re-run). The K-consecutive "
                     "rule counts CALENDAR nights, and a night whose runs "
                     "disagree about the threshold counts as undecidable, not "
                     "as a majority vote.")
    lines.append("")
    lines.append(f"Rule: ratio > **{result['threshold_pct']:.2f}%** on "
                 f"**{result['k']}** consecutive counted nights. Baseline is 1.000 "
                 "by construction — no window median, no anchor.")
    lines.append("")
    lines.append("> ⛔ **This tool opens, updates and closes nothing.** The "
                 "`perf-trend` issue is still owned by the old absolute-series "
                 "watchdog. This runs beside it so the two can be compared before "
                 "any cutover (ADR-032 §待決 6).")
    lines.append("")

    if result["fired"]:
        lines.append("### Would have fired")
        lines.append("")
        lines.append("| benchmark | first night the rule was met |")
        lines.append("|---|---|")
        for bench, night_utc in sorted(result["fired"].items()):
            lines.append(f"| `{bench}` | {night_utc} |")
        lines.append("")
        # ⛔ No suppression list here, on purpose. A benchmark whose cost was
        # measured and ACCEPTED (#1474) still shows up as a bare fire, because
        # the accepted-cost ledger is PR-B2's job and a temporary hand-written
        # exclusion list is the prototype of the silence this line keeps
        # removing. It is annotated, not hidden.
        lines.append("⚠️ A fire here is the RULE's raw output. No accepted-cost "
                     "ledger is applied yet — that is PR-B2. A benchmark already "
                     "measured, attributed and accepted (e.g. `#1474`) will still "
                     "appear above, and that is deliberate: annotate, never suppress.")
        lines.append("")

    # Night-by-night, including why a night did not count.
    lines.append("### Nights")
    lines.append("")
    # ⛔ Two canary columns, not one. Only the CPU canary gates; the sleep
    # canary is informational, and `canary_test.go` says it is "emitted +
    # rendered for human eyes". A first cut of the gating fix added an
    # `informational_canary_pct` property and then never rendered it — dead code
    # AND a false justification in the same stroke, since "still displayed" was
    # the stated reason for keeping the reading at all. Caught in self-review
    # before it reached a reviewer, which is not the same as it never happening.
    lines.append("| night | run | outcome | canary (gating) | canary (info) "
                 "| drift | digest |")
    lines.append("|---|---|---|---|---|---|---|")
    for night in nights:
        deviation = night.canary_deviation_pct
        canary = _pct(deviation) if deviation is not None else "—"
        info = night.informational_canary_pct
        info_cell = ", ".join(f"{b.replace('BenchmarkControlCanary', '')} "
                              f"{_pct(v)}" for b, v in sorted(info.items())) or "—"
        note = "" if night.outcome == NIGHT_COUNTED else f" ({night.reason})"
        drift = night.drift_status
        if drift == "checked":
            count = "?" if night.drift_count is None else night.drift_count
            named = "" if night.drift_files else ", names not archived"
            drift = f"checked, {count} file(s){named}"
        lines.append(
            f"| {night.night_utc or '?'} | `{night.run_id or '?'}` | "
            f"{night.outcome}{note} | {canary} | {info_cell} | {drift} "
            f"| {night.digest_status} |")
    lines.append("")

    # ⛔ These two sections are why a CLEAR is trustworthy. Without them a
    # verdict of "nothing fired" covered both "the rule ran and found nothing"
    # and "the rule could not run", and a +42% reading on every night of the
    # window appeared nowhere on the page.
    if result.get("unjudgeable"):
        lines.append("### Benchmarks the rule could NOT judge")
        lines.append("")
        lines.append(f"These never produced an answer on **{result['k']}** "
                     "consecutive counted nights, so the sustained rule was not "
                     "in a position to judge them. A night produces no answer "
                     "when it carried no reading for the benchmark, or when it "
                     "was measured more than once and the runs disagreed about "
                     f"the **{result['threshold_pct']:.2f}%** threshold:")
        lines.append("")
        for bench in result["unjudgeable"]:
            lines.append(f"- `{bench}`")
        lines.append("")
        lines.append("⛔ Not judged is not clean. ADR-032 §待決 6: the nights "
                     "before the rule can fire **must** report 無法評估, never "
                     "無發現.")
        lines.append("")
        lines.append("⚠️ This list is THRESHOLD-DEPENDENT, so it does not "
                     "contradict a counterfactual row below that shows a fire "
                     "for the same benchmark: runs that straddle one threshold "
                     "can agree about another.")
        lines.append("")

    if result.get("over_not_sustained"):
        lines.append("### Over the threshold, but not sustained")
        lines.append("")
        lines.append("| benchmark | nights over | worst |")
        lines.append("|---|---|---|")
        for bench, hits in sorted(result["over_not_sustained"].items()):
            worst = max(hits.values())
            dates = ", ".join(d[5:] for d in sorted(hits))
            lines.append(f"| `{bench}` | {len(hits)} ({dates}) | {_pct(worst)} |")
        lines.append("")
        lines.append("⚠️ These did not meet the consecutive-nights rule, so they "
                     "are not findings. They are shown because a reading the "
                     "page never mentions is indistinguishable from a reading "
                     "that never happened.")
        lines.append("")

    if result["inconclusive"]:
        lines.append("### Inconclusive benchmarks — NOT clean, NOT judged")
        lines.append("")
        for bench, per_night in sorted(result["inconclusive"].items()):
            reasons = ", ".join(f"{d}: {r}" for d, r in sorted(per_night.items()))
            lines.append(f"- `{bench}` — {reasons}")
        lines.append("")
        lines.append("⛔ These have no denominator on the nights listed "
                     "(ADR-032 §待決 2). They are never counted toward CLEAR.")
        lines.append("")

    # Work-definition transitions — the consumer PR-A's digest was added for.
    lines.append("### Work-definition transitions")
    lines.append("")
    transitions = result["transitions"]
    if not transitions:
        lines.append("_Fewer than two readable nights — no transition can be derived._")
    else:
        lines.append("| from → to | reference side | main side | reference pin |")
        lines.append("|---|---|---|---|")
        for rec in transitions:
            def cell(value):
                if value is None:
                    return "**UNKNOWN**"
                return "**MOVED**" if value else "unchanged"
            changed = rec["reference_pin_changed"]
            pin = ("**UNKNOWN**" if changed is None
                   else "⛔ **CHANGED**" if changed else "same")
            lines.append(f"| {rec['from']} → {rec['to']} | "
                         f"{cell(rec['sides']['reference'])} | "
                         f"{cell(rec['sides']['main'])} | {pin} |")
        lines.append("")
        lines.append("⛔ `UNKNOWN` means the digest was absent (schema v1), "
                     "not requested, or unreadable on one end. It does **not** "
                     "mean the work definition held still.")
        lines.append("")
        lines.append("⛔ A changed reference pin is an **absorption event** "
                     "(ADR-032 §待決 1), not a fixture edit: ratios either side "
                     "of it are measured against different baselines and must not "
                     "be read as one series.")
    lines.append("")

    # Counterfactuals — the two-to-four-week parameter decisions, fed with data.
    lines.append("### Counterfactuals (nothing below changes the verdict)")
    lines.append("")
    lines.append("**Threshold × consecutive nights**")
    lines.append("")
    lines.append("| threshold | K | fires | not judgeable | benchmarks |")
    lines.append("|---|---|---|---|---|")
    total = len(result["benches"])
    for threshold, k, fired, blind in counterfactual_thresholds(
            nights, result["benches"], result["gap"]):
        names = ", ".join(f"`{b.replace('Benchmark', '')}`@{d[5:]}"
                          for b, d in sorted(fired.items())) or "—"
        marker = " ← pinned" if (threshold == result["threshold_pct"]
                                 and k == result["k"]) else ""
        # ⛔ A row where NOTHING was judgeable does not print `0 fires`; `0`
        # there means "the rule ran and found nothing", which is the opposite
        # of what happened.
        count = "**n/a**" if blind and len(blind) == total else str(len(fired))
        blind_cell = ("—" if not blind else
                      f"**{len(blind)}/{total}**" if len(blind) == total
                      else f"{len(blind)}/{total}")
        lines.append(f"| {threshold:.0f}%{marker} | {k} | {count} "
                     f"| {blind_cell} | {names} |")
    lines.append("")
    lines.append("⛔ `not judgeable` is how many benchmarks the rule could not "
                 "answer for at that `(threshold, K)` — no run of K consecutive "
                 "counted nights that produced an answer. A row reading "
                 "**n/a** judged nothing at all, and must not be read as "
                 "\"0 fires\". Judgeability is threshold-dependent, so it can "
                 "differ row to row.")
    lines.append("")

    lines.append("**Canary gate**")
    lines.append("")
    lines.append("| gate | nights rejected |")
    lines.append("|---|---|")
    for gate, rejected in counterfactual_gates(nights):
        marker = " ← provisional default" if gate == result["gate_pct"] else ""
        lines.append(f"| {gate:.1f}%{marker} | "
                     f"{len(rejected)}{' (' + ', '.join(rejected) + ')' if rejected else ''} |")
    lines.append("")
    lines.append("⚠️ The gate threshold is **provisional** — ADR-032 §待決 5 left "
                 "it to two-to-four weeks of real distribution and no value has "
                 "been decided. It answers *did the paired measurement break "
                 "tonight*, not *is this benchmark trustworthy tonight*; those two "
                 "came apart in the six-night data.")
    lines.append("")

    # The open question the ADR did not settle.
    other_gap = GAP_SKIP if result["gap"] == GAP_BREAK else GAP_BREAK
    alt = fires(nights, result["benches"], result["threshold_pct"],
                result["k"], gap=other_gap)
    lines.append("**Gap handling** — what an uncounted night does to a run")
    lines.append("")
    lines.append(f"- `{result['gap']}` (in effect): {len(result['fired'])} fire(s)")
    lines.append(f"- `{other_gap}`: {len(alt)} fire(s)")
    lines.append("")
    lines.append("⚠️ **Open question, not a decision.** ADR-032 says a gated-out "
                 "night is *不判定也不關票* (neither judged nor used to close a "
                 "ticket) but does not say whether it BREAKS a "
                 "consecutive run or is SKIPPED. `break` is the default here "
                 "because it never invents continuity across a night whose "
                 "measurement was rejected. If both rows above are equal, this "
                 "series cannot decide the question — it only differs once a "
                 "night is actually gated out.")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Paired-measurement trend verdict (ADR-032 phase 2). "
                    "SUMMARY ONLY — never opens, updates or closes an issue.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path,
                        help="replay an archival dataset directory containing nights.json")
    source.add_argument("--from-gh", action="store_true",
                        help="download the last N nightly bench-paired.json artifacts")
    parser.add_argument("--workflow", default="bench-record.yaml")
    parser.add_argument("--limit", type=int, default=14,
                        help="nights to consider with --from-gh (default 14)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".build/paired-watch"))
    parser.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    parser.add_argument("--consecutive", type=int, default=DEFAULT_CONSECUTIVE)
    parser.add_argument("--canary-gate-pct", type=float, default=DEFAULT_CANARY_GATE_PCT)
    parser.add_argument("--gap", choices=(GAP_BREAK, GAP_SKIP), default=GAP_BREAK,
                        help="what an uncounted night does to a consecutive run")
    parser.add_argument("--summary-file", type=Path, default=None,
                        help="append the rendered Markdown here "
                             "(defaults to $GITHUB_STEP_SUMMARY when set)")
    args = parser.parse_args(argv)

    if args.consecutive < 1:
        parser.error("--consecutive must be at least 1")

    try:
        if args.dataset:
            nights = nights_from_dataset(args.dataset)
        else:
            if shutil.which("gh") is None:
                print("error: gh is not on PATH — cannot read prior nights' "
                      "artifacts", file=sys.stderr)
                return EXIT_CANNOT_CHECK
            nights = nights_from_gh(args.workflow, args.limit, args.cache_dir)
    except (RuntimeError, OSError, UnicodeError, ValueError) as exc:
        # ⛔ "Could not check" is its own outcome and must not be dressed up as
        # either a verdict or a crash. A traceback here says nothing a reader
        # can act on, and — during a parallel-run period whose whole purpose is
        # to build confidence in this reporter — an unexplained stack trace in
        # the nightly is exactly the wrong first impression.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CANNOT_CHECK

    result = decide(nights,
                    threshold_pct=args.threshold_pct,
                    k=args.consecutive,
                    gate_pct=args.canary_gate_pct,
                    gap=args.gap)
    body = render(result)
    print(body)

    target = args.summary_file or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(body + "\n")

    # ⛔ Exit 0 for FINDINGS as well as CLEAR. This is a REPORTER during the
    # parallel-run period, and a non-zero exit would turn the nightly red on a
    # verdict nobody has agreed to trust yet — which is how a summary-only tool
    # quietly becomes a gate. "Could not do the job" exits 2 above, so the two
    # outcomes stay distinguishable from the outside.
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
