"""Behavioural tests for paired_trend_watch.py (ADR-032 phase 2, PR-B1).

The module decides whether main has a sustained regression against the pinned
reference. Everything it can get wrong renders as a perfectly ordinary table —
a benchmark silently dropped, a night silently skipped, a NaN that compares
False against every threshold, a digest transition invented out of a malformed
hash. So the tests here are organised around ONE question per case: *does the
could-not-measure path stay distinguishable from the measured-and-clean path?*

Three groups, and each pins something different:

1. THE ANCHOR (`test_dataset_replay_*`). The frozen six-night dataset in
   `docs/internal/audit-reports/bench-paired-2026-08/` was analysed by a
   SEPARATE, independently written script (`analyze_paired.py`) whose results
   are quoted verbatim in that directory's README. Replaying the same nights
   through this engine must reproduce them exactly. That is a cross-check
   between two implementations, not a restatement of this one — which is the
   only reason it is worth anything as a regression anchor.

   ⚠️ It validates the RULE, not the JSON input boundary: the archival schema is
   a different format, so `load_night()`'s shape guards are not on that path.
   They get their own group below. Saying so explicitly matters, because
   "the replay passes" would otherwise be read as "the input boundary is
   verified" — the same conflation the module exists to refuse.

2. THE INPUT BOUNDARY (`test_guard_*`). One case per malformed input, each
   checked against the UN-GUARDED behaviour first (counterfactual output is in
   the PR body). Most are built by mutating a REAL-SHAPED v2 payload.

   ⛔ The `bench-paired/v1` cases are NOT, and the correction is worth stating
   because this docstring's earlier "each built by mutating a v2 payload" is
   how the hole got in. `bench-paired/v1` names TWO incompatible payloads —
   #1455 (`60f4523`) added the required `status` field without bumping the
   schema string — and a v1 derived from v2 by deleting a key KEEPS `status`,
   so it is a shape no producer ever wrote. The only v1 production actually
   rejected was therefore the only one no test held. Both v1 fixtures are now
   built up from the older producer and pinned to those two commits by
   `test_the_v1_fixtures_match_the_producer_source_at_those_commits`, which
   reads `pair_bench_ratio.py` at each revision instead of trusting a dict
   somebody typed. See #1571 / TRK-367.

   ⛔ CORRECTION — an earlier draft of this docstring claimed "none of them
   crash without the guard, all of them produce a confident wrong answer".
   That was measured FALSE for one case. Removing the gate's fail-closed branch
   raises `TypeError: '>' not supported between instances of 'NoneType' and
   'float'` rather than counting the night. Four of the five sampled cases do
   render a confident wrong answer:

       transition guard   five "unchanged" rows for a window whose digest was
                          never measured — printed directly above the ⛔ note
                          saying UNKNOWN does not mean unchanged
       tri-state verdict  `CLEAR` with 0 counted nights and 0 benchmarks
       NaN filter         the benchmark vanishes from both buckets; the verdict
                          is rendered from the others and nothing says why
       digest shape       a work-definition MOVE manufactured from the string
                          "not-a-hash", with status still `checked`

   The gate guard stays, but its justification is the weaker one: a traceback
   in the nightly, not a false all-clear. Saying so matters more than keeping
   the tidier claim — an argument that overstates its own evidence is the
   pattern this ADR line has already had to correct twice.

3. THE SUMMARY-ONLY PROPERTY (`test_never_writes_*`). PR-B1's entire safety
   argument is "this tool cannot close a ticket". A comment saying so is worth
   nothing; these assert it against the module source and its imports.

⛔ Every guard below was dogfooded by intentional break — the guard was removed
from the module, the test was confirmed RED, and the module restored. A guard
whose test has never been seen to fail is not a tested guard. The evidence is
in the PR body.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TOOLS_DIR = _REPO / "scripts" / "tools" / "dx"
sys.path.insert(0, str(_TOOLS_DIR))

import paired_trend_watch as ptw  # noqa: E402

DATASET = _REPO / "docs" / "internal" / "audit-reports" / "bench-paired-2026-08"

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _payload(**over):
    """A real-shaped `bench-paired/v2` night, as `pair_bench_ratio.py` emits it.

    Field names and nesting were taken from an actual run of that tool, not
    from its docstring — a fixture that agrees only with the documentation
    would pass while the real artifact fails.
    """
    base = {
        "schema": "bench-paired/v2",
        "status": "OK",
        "cpu": "AMD EPYC 7763 64-Core Processor",
        "reference_tag": "exporter/v2.9.0",
        "reference_sha": "3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0",
        "evaluated": {
            "BenchmarkAlpha": {"ratio": 1.10, "main_ns": 110.0,
                               "reference_ns": 100.0, "n_main": 6, "n_reference": 6},
            "BenchmarkBeta": {"ratio": 1.00, "main_ns": 100.0,
                              "reference_ns": 100.0, "n_main": 6, "n_reference": 6},
            # ⛔ BOTH canaries, because the real harness runs both:
            # `bench_interleave.sh` compiles `bench-canary/` and invokes it with
            # `-test.bench=.`. The first version of this fixture carried only
            # the CPU one, which is why a whole class of "one canary missing"
            # never got exercised — review found the hole, and the fixture
            # being unrealistic is why it could hide there.
            "BenchmarkControlCanaryCPU": {"ratio": 1.0002, "main_ns": 100.02,
                                          "reference_ns": 100.0, "n_main": 6,
                                          "n_reference": 6},
            "BenchmarkControlCanarySleep": {"ratio": 1.0000, "main_ns": 100.0,
                                            "reference_ns": 100.0, "n_main": 6,
                                            "n_reference": 6},
        },
        "inconclusive": {},
        "workload_drift": {"status": "checked", "files": ["config_test.go"]},
        "workload_digest": {
            "status": "checked",
            "files": ["config_test.go"],
            "sides": {"reference": {"digest": _SHA_A, "n_files": 8},
                      "main": {"digest": _SHA_B, "n_files": 8}},
        },
    }
    base.update(over)
    return base


def _night(night_utc="2026-08-23", run_id=1, **over):
    return ptw.load_night(_payload(**over), night_utc=night_utc, run_id=run_id)


# ── the two shapes that share the string `bench-paired/v1` ────────────────
#
# ⛔ Built up from the older producer, NOT down from the v2 fixture. Deriving a
# v1 by deleting keys from v2 is what let the real v1 go untested: the
# subtraction kept `status`, which the pre-#1455 producer never wrote, so the
# only shape production ever rejected was the one shape no test held.

def _v1_pre_status_payload(**over):
    """`pair_bench_ratio.py`'s payload at `60f4523^` — five keys, no `status`."""
    base = {
        "schema": "bench-paired/v1",
        "reference_tag": "exporter/v2.9.0",
        "cpu": "AMD EPYC 7763 64-Core Processor",
        "evaluated": {
            "BenchmarkAlpha": {"ratio": 1.10, "main_ns": 110.0,
                               "reference_ns": 100.0, "n_main": 6,
                               "n_reference": 6},
            "BenchmarkControlCanaryCPU": {"ratio": 1.0002, "main_ns": 100.02,
                                          "reference_ns": 100.0, "n_main": 6,
                                          "n_reference": 6},
            "BenchmarkControlCanarySleep": {"ratio": 1.0000, "main_ns": 100.0,
                                            "reference_ns": 100.0, "n_main": 6,
                                            "n_reference": 6},
        },
        "inconclusive": {},
    }
    base.update(over)
    return base


def _v1_post_status_payload(**over):
    """The same schema string at `60f4523` — `status` and two more fields.

    Still v1, still no `workload_digest` (that is what `c7d0586` bumped to v2
    for), and this one IS readable. Both facts have to hold at once, which is
    the whole reason the string is ambiguous.
    """
    base = dict(_v1_pre_status_payload())
    base["status"] = "OK"
    base["reference_sha"] = "3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0"
    base["workload_drift"] = {"status": "checked", "files": ["config_test.go"]}
    base.update(over)
    return base


# ── 1. THE ANCHOR ─────────────────────────────────────────────────────────
#
# Quoted from `bench-paired-2026-08/README.md` §1, which quotes
# `analyze_paired.py`'s stdout. If these drift apart, one of the two engines is
# wrong and the disagreement is the finding.
_README_FIRES = {
    (5.0, 2): {"BenchmarkMergePartialConfigs_1000": "2026-08-17"},
    (5.0, 3): {"BenchmarkMergePartialConfigs_1000": "2026-08-18"},
    (3.0, 2): {"BenchmarkIncrementalLoad_1000_OneFileChanged": "2026-08-20",
               "BenchmarkMergePartialConfigs_1000": "2026-08-17",
               "BenchmarkResolveSilentModes_1000": "2026-08-18"},
    (1.0, 2): 9,   # count only; the nine names are in the README
    (1.0, 3): 6,
}


@pytest.mark.parametrize("params", sorted(_README_FIRES))
def test_dataset_replay_matches_the_independent_analysis(params):
    threshold, k = params
    nights = ptw.nights_from_dataset(DATASET)
    result = ptw.decide(nights, threshold_pct=threshold, k=k)
    expected = _README_FIRES[params]
    if isinstance(expected, int):
        assert len(result["fired"]) == expected
    else:
        assert result["fired"] == expected


def test_dataset_replay_counts_every_night_and_finds_the_known_regression():
    result = ptw.decide(ptw.nights_from_dataset(DATASET))
    assert result["status"] == ptw.STATUS_FINDINGS
    assert len(result["counted"]) == 6
    assert len(result["benches"]) == 20      # 22 minus the two canaries
    assert set(result["fired"]) == {"BenchmarkMergePartialConfigs_1000"}


def test_dataset_replay_claims_no_digest_transition_it_cannot_see():
    """⛔ Six v1 nights carry no digest, so every transition must be UNKNOWN.

    The failure this pins is the attractive one: `absent == absent` is True, so
    a naive comparison reports "unchanged" for five consecutive nights and the
    reader concludes the work definition held still across a window in which it
    was never measured at all.
    """
    result = ptw.decide(ptw.nights_from_dataset(DATASET))
    assert len(result["transitions"]) == 5
    for rec in result["transitions"]:
        assert rec["sides"]["reference"] is None
        assert rec["sides"]["main"] is None
        assert rec["reference_pin_changed"] is False


def test_dataset_replay_keeps_the_archived_drift_count_apart_from_its_names():
    """The job log carried a COUNT and no names; rendering must not fake names."""
    nights = {n.night_utc: n for n in ptw.nights_from_dataset(DATASET)}
    assert nights["2026-08-16"].drift_status == "absent"
    assert nights["2026-08-16"].drift_count is None
    assert nights["2026-08-17"].drift_status == "checked"
    assert nights["2026-08-17"].drift_count == 4
    assert nights["2026-08-17"].drift_files == []


def test_render_prints_the_archived_drift_count_not_the_placeholder_length():
    """⛔ Added because the break harness found this guard UNTESTED.

    A first cut of the loader stored the archived count as a one-element
    placeholder list and the renderer printed `len()` of it, so a night that saw
    4 drifted files rendered as "1 file". Nothing about that row looks wrong.
    The loader-level test above pins `drift_count`; only this one pins that the
    renderer actually uses it.
    """
    body = ptw.render(ptw.decide(ptw.nights_from_dataset(DATASET)))
    assert "checked, 4 file(s)" in body
    assert "checked, 1 file(s)" not in body


def test_render_never_labels_an_unknown_transition_as_unchanged():
    body = ptw.render(ptw.decide(ptw.nights_from_dataset(DATASET)))
    assert "**UNKNOWN**" in body
    assert "unchanged" not in body


# ── 2. THE INPUT BOUNDARY ─────────────────────────────────────────────────

def test_guard_unknown_schema_is_unreadable_not_clean():
    night = _night(schema="bench-paired/v3")
    assert night.outcome == ptw.NIGHT_UNREADABLE
    assert "bench-paired/v3" in night.reason


def test_guard_schema_v1_after_the_status_commit_is_counted_and_has_no_digest():
    """v1 nights must still be usable — rejecting them empties the series.

    ⛔ REPLACES a test that built its v1 by deleting `workload_digest` from the
    v2 fixture and leaving `status` in place. That subtraction produced a shape
    no producer ever wrote, so it asserted `readable` about a payload that does
    not exist while the one that DOES exist — the pre-#1455 v1, which carries no
    `status` — was covered by nothing. Second time in this file that a fixture
    resembling nothing real hid a whole case; the first is recorded in
    `_payload()`'s own comment. Both fixtures below are built from what
    `pair_bench_ratio.py` actually emitted, and pinned to those commits by
    `test_the_v1_fixtures_match_the_producer_source_at_those_commits`.
    """
    night = ptw.load_night(_v1_post_status_payload(),
                           night_utc="2026-08-20", run_id=1)
    assert night.outcome == ptw.NIGHT_COUNTED
    assert night.digest_status == ptw.DIGEST_ABSENT
    assert night.digest_sides == {}


def test_guard_schema_v1_before_the_status_commit_names_the_version_boundary():
    """⛔ The shape production actually rejected, which had NO test at all.

    ⚠️ COUNTERFACTUAL, stated plainly because most of this test buys nothing
    new: `unreadable` was ALREADY true before this change — a payload with no
    `status` key reaches `payload.get("status") -> None`, which is not in
    ("OK", "INCONCLUSIVE"). Delete the whole split-version branch from the
    module and the first assertion below still passes. The detection this test
    adds is confined to the reason: the old message was
    `unknown night status None`, which names a symptom and reads like a corrupt
    payload. So the assertions that can fail are the ones about WHAT IT SAYS,
    and the intentional-break evidence in the PR body breaks exactly those.
    """
    night = ptw.load_night(_v1_pre_status_payload(),
                           night_utc="2026-08-16", run_id=1)
    assert night.outcome == ptw.NIGHT_UNREADABLE
    # The named cause, not the symptom.
    assert "unknown night status" not in night.reason
    assert ptw.SCHEMA_V1_STATUS_COMMIT in night.reason
    assert ptw.SCHEMA_V1_STATUS_PR in night.reason
    # ⛔ And it must NOT claim to know the run is old — that is provenance, and
    # this branch only ever read the payload. Over-claiming here would be the
    # same inference-dressed-as-measurement this ADR line has corrected before.
    assert "shape" in night.reason


def test_guard_v1_missing_status_with_an_unknown_key_set_is_named_separately():
    """A truncated payload must not borrow the version boundary's explanation.

    Same rejection, deliberately different reason: one is a known producer
    version, the other is unaccounted for. Collapsing them would put a
    measured-and-explained label on something nobody measured.
    """
    payload = _v1_pre_status_payload()
    del payload["cpu"]
    night = ptw.load_night(payload, night_utc="2026-08-16", run_id=1)
    assert night.outcome == ptw.NIGHT_UNREADABLE
    assert "neither the pre-" in night.reason
    assert ptw.SCHEMA_V1_STATUS_COMMIT not in night.reason


def test_the_v1_fixtures_match_the_producer_source_at_those_commits():
    """⛔ Pins both v1 fixtures — and the module's key constant — to history.

    Without this the fixtures are just two dicts somebody typed, which is the
    failure mode they exist to end. Read from `pair_bench_ratio.py` at the two
    commits by AST, so a reformat does not move it and a comment cannot fake it.

    ⚠️ FAIL-CLOSED, NOT SKIPPED. A shallow clone makes this error rather than
    quietly pass; `ci.yml`'s Python job takes `fetch-depth: 0` for exactly this
    reason and `tests/lint/test_check_image_pin_capability.py` already relies on
    it. A test that skips itself when its evidence is missing reports the same
    green as one that checked — the conflation this whole file refuses.
    """
    def payload_keys(rev):
        src = subprocess.run(
            ["git", "show", f"{rev}:scripts/tools/dx/pair_bench_ratio.py"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(_REPO), timeout=60)
        assert src.returncode == 0, (
            f"cannot read pair_bench_ratio.py at {rev}: {src.stderr}")
        found = [node.value for node in ast.walk(ast.parse(src.stdout))
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "payload"
                         for t in node.targets)
                 and isinstance(node.value, ast.Dict)]
        assert len(found) == 1, f"expected one `payload = {{...}}` at {rev}"
        return frozenset(k.value for k in found[0].keys)

    pre = payload_keys(f"{ptw.SCHEMA_V1_STATUS_COMMIT}^")
    post = payload_keys(ptw.SCHEMA_V1_STATUS_COMMIT)
    assert pre == ptw.SCHEMA_V1_PRE_STATUS_KEYS
    assert pre == frozenset(_v1_pre_status_payload())
    assert post == frozenset(_v1_post_status_payload())
    # The split itself, asserted rather than described: same schema string,
    # and `status` required by this consumer arrives without a bump.
    assert "status" not in pre and "status" in post


def test_guard_nightly_inconclusive_is_unreadable_and_keeps_its_reason():
    night = _night(status="INCONCLUSIVE", reason="unreadable-reference-pin")
    assert night.outcome == ptw.NIGHT_UNREADABLE
    assert "unreadable-reference-pin" in night.reason


def test_guard_unknown_night_status_is_unreadable():
    assert _night(status="FINE").outcome == ptw.NIGHT_UNREADABLE


def test_guard_evaluated_wrong_type_is_unreadable():
    assert _night(evaluated=[]).outcome == ptw.NIGHT_UNREADABLE


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0,
                                 "1.10", None, True])
def test_guard_unusable_ratio_becomes_inconclusive_never_dropped(bad):
    """⛔ The NaN case is the dangerous one and the reason this is parametrised.

    `nan > 5.0` is False, so an unfiltered NaN is a benchmark that can never
    fire — silence that renders as an ordinary clean row. Dropping the entry
    instead would be no better: the series simply stops asking about it.
    """
    payload = _payload()
    payload["evaluated"]["BenchmarkAlpha"]["ratio"] = bad
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert "BenchmarkAlpha" not in night.ratios_pct
    assert "BenchmarkAlpha" in night.inconclusive


def test_guard_unusable_ratio_keeps_the_night_out_of_clear():
    """⛔ k=1 AND a positive control, both for the same reason.

    Review found the first version vacuous: it asserted INCONCLUSIVE on a
    one-night series at the default k=2, where the later `judgeable()` gate
    makes EVERY one-night series INCONCLUSIVE — so it passed with the NaN
    replaced by a healthy ratio. At k=1 a healthy counted night is judgeable
    and reads CLEAR, so the two branches are distinguishable and the control
    below fails loudly if the guard is removed.
    """
    payload = _payload()
    for bench in ("BenchmarkAlpha", "BenchmarkBeta"):
        payload["evaluated"][bench]["ratio"] = float("nan")
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert ptw.decide([night], k=1)["status"] == ptw.STATUS_INCONCLUSIVE
    # positive control — the same night with real, quiet ratios IS judged.
    # (`_payload()`'s Alpha is +10%, which at k=1 legitimately fires; the
    # control has to be under the threshold to distinguish CLEAR from
    # INCONCLUSIVE rather than from FINDINGS.)
    quiet = _payload()
    quiet["evaluated"]["BenchmarkAlpha"]["ratio"] = 1.001
    healthy = ptw.load_night(quiet, night_utc="2026-08-23", run_id=1)
    assert ptw.decide([healthy], k=1)["status"] == ptw.STATUS_CLEAR


@pytest.mark.parametrize("status", ["clean", "", None, "CHECKED"])
def test_guard_disclosure_status_outside_the_vocabulary_is_unreadable(status):
    night = _night(workload_digest={"status": status, "sides": {}})
    assert night.digest_status == "unreadable"


@pytest.mark.parametrize("bad", ["not-a-hash", _SHA_A[:63], _SHA_A.upper(),
                                 "", 123, None])
def test_guard_malformed_digest_is_unreadable_and_clears_both_sides(bad):
    """A digest that is not a digest still compares unequal night to night.

    ⛔ i.e. it MANUFACTURES a work-definition transition. Partial acceptance is
    worse than rejection here, so both sides are dropped, not just the bad one.
    """
    payload = _payload()
    payload["workload_digest"]["sides"]["main"]["digest"] = bad
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.digest_status == "unreadable"
    assert night.digest_sides == {}


def test_guard_digest_missing_one_side_is_unreadable():
    payload = _payload()
    del payload["workload_digest"]["sides"]["reference"]
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.digest_status == "unreadable"


def test_guard_disclosure_block_wrong_type_is_unreadable():
    assert _night(workload_drift=["config_test.go"]).drift_status == "unreadable"


def test_guard_unparseable_file_becomes_an_unreadable_night_not_a_gap(tmp_path):
    """⛔ A dropped night silently shortens the window.

    A K-of-N rule over a window the operator believes is 14 nights but is
    actually 11 fires later than advertised, and nothing on the page says so.
    """
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_payload()), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    nights = ptw.nights_from_paths([("2026-08-23", 1, good),
                                    ("2026-08-24", 2, bad),
                                    ("2026-08-25", 3, tmp_path / "absent.json")])
    assert len(nights) == 3
    assert [n.readable for n in nights] == [True, False, False]
    assert all(n.reason for n in nights if not n.readable)


# ── The canary gate ───────────────────────────────────────────────────────

def test_gate_without_a_canary_reading_fails_closed():
    """No canary is no evidence the measurement worked — and no evidence it
    broke is not evidence it did not.

    The reason names the missing GATING canary rather than saying "no reading":
    with one gating canary those are the same night, and the specific message is
    the one an operator can act on.
    """
    payload = _payload()
    for canary in ("BenchmarkControlCanaryCPU", "BenchmarkControlCanarySleep"):
        del payload["evaluated"][canary]
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_NOT_COUNTED
    assert "not established" in night.reason
    assert "BenchmarkControlCanaryCPU" in night.reason


def test_gate_no_reading_branch_is_reachable_and_fails_closed():
    """⛔ Covers the branch that `load_night` can no longer produce.

    With a single gating canary, every route through the loader that leaves no
    reading also populates `missing_canaries` or `unreadable_canaries`, so the
    bare "no reading" arm is unreachable from real input. It stays because it is
    the fail-closed DEFAULT of a safety predicate — deleting it would make the
    function fall through to `return True` for a night with nothing to judge —
    and it is tested here rather than left as untested dead surface, which is
    what this repo deletes elsewhere.
    """
    night = ptw.Night("2026-08-23", 1)
    night.schema = "bench-paired/v2"
    # constructed directly: no canary anywhere, and `missing_canaries` bypassed
    # by pre-seeding the name so the specific branch does not claim it
    night.ratios_pct["BenchmarkControlCanaryCPU"] = 0.0
    assert night.missing_canaries == []
    assert night.unreadable_canaries == []
    assert night.canary_deviation_pct is None
    counts, reason = ptw.gate_verdict(night, 1.0)
    assert counts is False
    assert "cannot be evaluated" in reason


def test_gate_with_a_canary_absent_from_the_payload_entirely_fails_closed():
    """⛔ The third canary case, and the one review found still open.

    Distinct from a canary that ran and produced garbage (`inconclusive`) and
    from no canary at all. A canary present in NEITHER bucket was invisible to
    the guard, so the gate judged the night on the survivor and rendered
    `counted | +0.01%`. Reachable without anything exotic: the payload's
    benchmark set is `set(reference) | set(main)` over the raw `go test -bench`
    output, so a name that stops matching on both sides never appears at all.
    """
    payload = _payload()
    del payload["evaluated"]["BenchmarkControlCanaryCPU"]   # Sleep survives
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.missing_canaries == ["BenchmarkControlCanaryCPU"]
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_NOT_COUNTED
    assert "not established" in night.reason
    assert "BenchmarkControlCanaryCPU" in night.reason


def test_the_informational_canary_going_missing_does_not_gate_the_night():
    """⛔ The counterpart, and the reason the rule above is safe rather than
    brittle.

    `canary_test.go` calls the sleep canary "INFORMATIONAL ONLY ... NOT part of
    the gate decision", so its absence must not discard a night. An earlier
    version required BOTH canaries and gated on BOTH — measured consequence: a
    benchmark at +9% on every one of six nights never fired, because the sleep
    canary jittered 3% on alternate nights, which that same file calls healthy.
    """
    payload = _payload()
    del payload["evaluated"]["BenchmarkControlCanarySleep"]
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.missing_canaries == []
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_COUNTED
    assert night.reason is None


def test_the_informational_canary_jittering_does_not_gate_the_night():
    payload = _payload()
    payload["evaluated"]["BenchmarkControlCanarySleep"]["ratio"] = 1.03  # +3%
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_COUNTED
    assert night.informational_canary_pct["BenchmarkControlCanarySleep"] > 2.9


def test_canary_roles_match_the_real_benchmark_source():
    """⛔ Pins BOTH sets, and pins them by ROLE rather than by name.

    An earlier version pinned only `CANARY_BENCHES` — the recognised set — and
    a docstring claimed that friction protected the set that GATES. It did not:
    `GATING_CANARIES` was pinned to nothing. Adding a third gating canary to
    `canary_test.go` would have forced an update to `CANARY_BENCHES`, the
    docstring would have said that was enough, and the new canary would have
    been silently demoted to informational — the gate quietly weaker with every
    surface reporting normal.

    So the role is read out of the Go doc comment, which states it explicitly:
    the CPU canary's block says "GATING: this is the canary that drives the
    INCONCLUSIVE verdict", the sleep canary's says "INFORMATIONAL ONLY ... NOT
    part of the gate decision".
    """
    src = (_REPO / "scripts" / "tools" / "ops" / "bench-canary"
           / "canary_test.go").read_text(encoding="utf-8")

    declared = set(re.findall(r"^func (Benchmark\w+)\(b \*testing\.B\)", src, re.M))
    assert declared == set(ptw.CANARY_BENCHES), (
        f"canary_test.go declares {sorted(declared)} but CANARY_BENCHES is "
        f"{sorted(ptw.CANARY_BENCHES)} — a canary this module does not "
        "recognise would be judged as an ordinary product benchmark")

    # Split the header comment into one block per canary and read its role.
    blocks, current = {}, None
    for line in src.splitlines():
        if not line.startswith("//"):
            continue
        text = line.lstrip("/").strip()
        match = re.match(r"^(Benchmark\w+)\s+[—-]\s*(.*)$", text)
        if match:
            current = match.group(1)
            blocks[current] = match.group(2)
        elif current:
            blocks[current] += " " + text
    assert set(blocks) == declared, (
        f"the doc comment documents {sorted(blocks)} but the file declares "
        f"{sorted(declared)} — a canary with no documented role cannot be "
        "classified, and guessing is how the gate came to include an "
        "informational probe in the first place")

    gating = {b for b, doc in blocks.items() if "GATING:" in doc}
    informational = {b for b, doc in blocks.items() if "INFORMATIONAL ONLY" in doc}
    assert gating and informational, "expected both roles to be documented"
    assert not (gating & informational), (
        f"{sorted(gating & informational)} is documented as both — the source "
        "of truth contradicts itself")
    assert gating == set(ptw.GATING_CANARIES), (
        f"canary_test.go marks {sorted(gating)} as GATING but GATING_CANARIES "
        f"is {sorted(ptw.GATING_CANARIES)}. Too wide and a night is discarded "
        "for jitter the harness calls healthy; too narrow and a real "
        "measurement failure passes as a good night.")
    assert set(ptw.GATING_CANARIES) <= set(ptw.CANARY_BENCHES)


def test_gate_rejects_a_night_whose_canary_moved():
    payload = _payload()
    payload["evaluated"]["BenchmarkControlCanaryCPU"]["ratio"] = 1.05
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_NOT_COUNTED
    assert "5.00%" in night.reason


def test_gate_keeps_a_quiet_night():
    night = _night()
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_COUNTED
    assert night.reason is None


# ── The fire rule ─────────────────────────────────────────────────────────

def _series(values, **over):
    """One benchmark, one reading per night. `None` = not measured that night."""
    out = []
    for i, value in enumerate(values):
        payload = _payload(**over)
        payload["evaluated"] = {
            "BenchmarkControlCanaryCPU": {"ratio": 1.0002},
            "BenchmarkControlCanarySleep": {"ratio": 1.0000},
        }
        if value is not None:
            payload["evaluated"]["BenchmarkAlpha"] = {"ratio": 1.0 + value / 100.0}
        out.append(ptw.load_night(payload, night_utc=f"2026-08-{10 + i:02d}",
                                  run_id=i + 1))
    return out


def test_fire_needs_k_consecutive_nights():
    assert ptw.decide(_series([9.0]), k=2)["fired"] == {}
    assert ptw.decide(_series([9.0, 9.0]), k=2)["fired"] == {
        "BenchmarkAlpha": "2026-08-11"}


def test_a_night_with_no_reading_breaks_the_run():
    """⛔ 'not measured' is not 'under the threshold'.

    ⚠️ The `decide()` assertion below CANNOT discriminate that sentence, and
    saying so is the point of this docstring. Inside `fires()` a `None` reading
    and a `False` (under-threshold) reading both reset the run to 0, so a code
    change that conflates the two leaves this line green — review demonstrated
    exactly that. The tri-state return is asserted where it is observable, at
    `_night_reading`; this stays as the integration check it actually is, with
    a positive control so at least the run-breaking half is real.
    """
    assert ptw._night_reading([_run("2026-08-11", 2)], "BenchmarkAlpha", 5.0) is None
    assert ptw.decide(_series([9.0, None, 9.0]), k=2)["fired"] == {}
    assert ptw.decide(_series([9.0, 9.0, 9.0]), k=2)["fired"]   # control


def _exact(values):
    """Nights carrying EXACT percent readings, bypassing the ratio conversion.

    ⛔ Not a convenience. `_series()` builds `ratio = 1 + pct/100` and the module
    converts it back, and that round trip is not exact: `5.0` returns as
    `5.000000000000004`, which is on the far side of a 5.0 threshold. Testing the
    boundary through that path would be testing float representation, not the
    rule. (This is also why `nights_from_dataset()` refuses to reconstruct a
    ratio from the archived percent — same trap, on the anchor path.)
    """
    out = []
    for i, value in enumerate(values):
        night = ptw.Night(f"2026-08-{10 + i:02d}", i + 1)
        night.schema = "bench-paired/v2"
        night.canary_pct["BenchmarkControlCanaryCPU"] = 0.02
        night.canary_pct["BenchmarkControlCanarySleep"] = 0.00
        if value is not None:
            night.ratios_pct["BenchmarkAlpha"] = value
        out.append(night)
    return out


def test_exactly_at_the_threshold_does_not_fire():
    """Strict `>`, matching the archived analyser. Pinned because flipping it to
    `>=` changes the answer on real data and nothing on the page would say so."""
    assert ptw.decide(_exact([5.0, 5.0]), threshold_pct=5.0, k=2)["fired"] == {}
    assert ptw.decide(_exact([5.01, 5.01]), threshold_pct=5.0, k=2)["fired"]


def test_ratio_to_percent_round_trip_is_not_exact_at_the_boundary():
    """A caveat pinned rather than papered over.

    The nightly emits a RATIO; the rule compares a PERCENT. A ratio sitting
    exactly on the threshold is therefore decided by float representation, and
    `1.05` lands just above 5.00%. Real readings are medians of ns/op and never
    land exactly on a boundary, so this is documented, not defended against —
    rounding to fix it would just move the edge somewhere less visible.
    """
    assert ptw._ratio_to_pct(1.05) > 5.0
    assert ptw._ratio_to_pct(1.05) == pytest.approx(5.0)


def test_uncounted_night_breaks_the_run_by_default_and_is_skipped_on_request():
    """The open question ADR-032 did not settle, pinned in both directions.

    ⚠️ This is the ONLY place either behaviour is observable — the frozen
    six-night dataset never gates a night out, so the anchor above cannot
    distinguish them. Recorded as an open question, not a decision.
    """
    nights = _series([9.0, 9.0, 9.0])
    nights[1].canary_pct["BenchmarkControlCanaryCPU"] = 4.0   # will be gated out
    assert ptw.decide(copy.deepcopy(nights), k=2, gap=ptw.GAP_BREAK)["fired"] == {}
    fired = ptw.decide(copy.deepcopy(nights), k=2, gap=ptw.GAP_SKIP)["fired"]
    assert fired == {"BenchmarkAlpha": "2026-08-12"}


# ── The three-state verdict ───────────────────────────────────────────────

def test_no_counted_night_is_inconclusive_not_clear():
    """⛔ The whole point. A window that judged nothing is not a clean window.

    ⛔ Same correction as the test above, found the same way: at the default
    k=2 this passed with the canary put back, because one night is never
    judgeable at k=2. k=1 plus a positive control makes the assertion mean what
    its name says.
    """
    payload = _payload()
    del payload["evaluated"]["BenchmarkControlCanaryCPU"]
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert ptw.decide([night], k=1)["status"] == ptw.STATUS_INCONCLUSIVE
    quiet = _payload()
    quiet["evaluated"]["BenchmarkAlpha"]["ratio"] = 1.001
    withcanary = ptw.load_night(quiet, night_utc="2026-08-23", run_id=1)
    assert ptw.decide([withcanary], k=1)["status"] == ptw.STATUS_CLEAR


def test_empty_series_is_inconclusive():
    assert ptw.decide([])["status"] == ptw.STATUS_INCONCLUSIVE


def test_counted_and_quiet_is_clear():
    assert ptw.decide(_series([0.5, 0.5]))["status"] == ptw.STATUS_CLEAR


def test_benchmark_missing_from_the_reference_is_reported_never_clean():
    """ADR-032 §待決 2: a benchmark added to main after the reference was cut."""
    payload = _payload()
    payload["inconclusive"] = {
        "BenchmarkBrandNew": {"reason": "missing-in-reference"}}
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    result = ptw.decide([night])
    assert "BenchmarkBrandNew" in result["inconclusive"]
    assert "missing-in-reference" in result["inconclusive"]["BenchmarkBrandNew"][
        (ptw.KEY_DATED, "2026-08-23")]


# ── Digest transitions ────────────────────────────────────────────────────

def test_transition_reports_moved_and_unchanged_when_both_ends_are_checked():
    same = _night("2026-08-23", 1)
    moved = _night("2026-08-24", 2, workload_digest={
        "status": "checked", "files": ["config_test.go"],
        "sides": {"reference": {"digest": _SHA_A, "n_files": 8},
                  "main": {"digest": "c" * 64, "n_files": 8}}})
    recs = ptw.digest_transitions([same, moved])
    assert recs[0]["sides"]["reference"] is False
    assert recs[0]["sides"]["main"] is True


def test_transition_is_unknown_when_either_end_is_unreadable():
    ok = _night("2026-08-23", 1)
    broken = _night("2026-08-24", 2, workload_digest={"status": "unreadable"})
    recs = ptw.digest_transitions([ok, broken])
    assert recs[0]["sides"] == {"reference": None, "main": None}


def test_transition_flags_a_changed_reference_pin_as_its_own_event():
    """⛔ An absorption event (ADR-032 §待決 1), not a fixture edit. Ratios
    either side of it are measured against different baselines."""
    before = _night("2026-08-23", 1)
    after = _night("2026-08-24", 2, reference_sha="0" * 40)
    assert ptw.digest_transitions([before, after])[0]["reference_pin_changed"]


# ── 3. THE SUMMARY-ONLY PROPERTY ──────────────────────────────────────────

_SOURCE = (_TOOLS_DIR / "paired_trend_watch.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

# Every name in `analyze_bench_history` that can mutate an issue. Listed by name
# rather than checked by intent, so adding a new writer over there does not
# silently widen what this module may reach for.
_WRITERS = (
    "render_trend_issue_body", "_render_state_marker", "_carry_forward_state",
    "upsert_issue", "close_issue", "comment_issue", "trend_watch", "_gh",
)


def _referenced_names(tree):
    """Every identifier the module actually references — not every string that
    happens to appear in it.

    ⛔ This replaces a substring scan that was WRONG in both directions: it
    matched `trend_watch` inside this module's own filename in a comment (a
    false positive that made the test fail for no reason), and it would equally
    have matched a banned name inside a docstring while missing one reached via
    `getattr`. Prose is not code; ask the parser.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
    return out


_REFERENCED = _referenced_names(_TREE)


@pytest.mark.parametrize("writer", _WRITERS)
def test_never_writes_names_no_issue_mutating_helper(writer):
    """PR-B1's safety argument in executable form.

    The module imports two `gh` wrappers from `analyze_bench_history` (one
    implementation of 'list runs' and 'download artifact', not two). That import
    is also the crack through which a closing path could arrive, so the ban is
    asserted rather than promised.
    """
    assert writer not in _REFERENCED


@pytest.mark.parametrize("module", ["subprocess", "requests", "urllib",
                                    "http", "socket"])
def test_never_writes_cannot_reach_the_network_itself(module):
    """Structural, not enumerative: a GitHub write needs a subprocess or an HTTP
    client, and this module imports neither. Enumerating banned function names
    only catches the ones somebody thought of."""
    assert module not in _REFERENCED


def test_never_writes_imports_only_the_two_gh_wrappers():
    imported = {alias.name
                for node in ast.walk(_TREE)
                if isinstance(node, ast.ImportFrom)
                and node.module == "analyze_bench_history"
                for alias in node.names}
    assert imported == {"download_artifact", "list_recent_runs"}


def test_never_writes_no_issue_mutating_gh_verb_in_any_string():
    """The `gh` wrappers take a list of argv fragments, so a write would appear
    as literal strings. Checked over string CONSTANTS only — a scan of the raw
    file text matches this module's own prose about not doing it."""
    literals = [node.value for node in ast.walk(_TREE)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    for verb in ("issue", "--method", "POST", "PATCH", "api"):
        offenders = [s for s in literals if s.strip() == verb]
        assert not offenders, f"argv-shaped literal {verb!r} present: {offenders}"


def test_cli_exits_zero_on_findings():
    """⛔ A non-zero exit would turn the nightly red on a verdict nobody has
    agreed to trust yet — which is how a summary-only tool becomes a gate."""
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--dataset", str(DATASET)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0
    assert "**FINDINGS**" in proc.stdout
    assert "opens, updates and closes nothing" in proc.stdout


def test_cli_appends_to_the_summary_file(tmp_path):
    target = tmp_path / "summary.md"
    target.write_text("pre-existing\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--dataset", str(DATASET), "--summary-file", str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0
    body = target.read_text(encoding="utf-8")
    assert body.startswith("pre-existing\n")
    assert "Paired trend watch" in body


def test_cli_rejects_a_dataset_whose_unit_is_not_a_ratio(tmp_path):
    """⛔ Absolute ns/op fed to a ratio engine renders a full, wrong table."""
    payload = json.loads((DATASET / "nights.json").read_text(encoding="utf-8"))
    payload["unit"] = "median ns/op"
    (tmp_path / "nights.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected unit"):
        ptw.nights_from_dataset(tmp_path)


def test_never_writes_the_workflow_job_has_no_issue_token():
    """The strongest of the three guarantees, and the only one that holds if the
    other two are wrong.

    A comment can be ignored and an AST test can be deleted in the same commit
    that adds a writer. A GitHub token without `issues: write` cannot close an
    issue no matter what the code asks for. During a parallel-run period whose
    whole premise is that nobody trusts this verdict yet, that is the one worth
    pinning in CI.
    """
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "bench-record.yaml").read_text(
            encoding="utf-8"))
    job = workflow["jobs"]["paired-trend-watch"]
    assert "issues" not in job["permissions"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    # And the job that DOES hold the token must still be the old watchdog, so a
    # future edit cannot quietly move the paired engine under it.
    assert workflow["jobs"]["trend-watch"]["permissions"]["issues"] == "write"
    steps = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "paired_trend_watch.py" in steps
    assert "analyze_bench_history.py" not in steps


# ── 4. THE `--from-gh` PATH — the only one CI actually runs ───────────────
#
# ⛔ Added after review pointed out that all of the above exercised `--dataset`
# and none of it exercised the path the nightly job invokes. The tests below
# drive the real code with a STUB `gh` on PATH, so `nights_from_gh` and the two
# imported wrappers execute for real; only the GitHub API is replaced.

_GH_STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
mode = os.environ["STUB_MODE"]
if mode == "auth-fail":
    sys.stderr.write("gh: authentication required\n")
    raise SystemExit(4)
if argv[:2] == ["run", "list"]:
    limit = int(argv[argv.index("--limit") + 1])
    # A DISTINCT head per run, on purpose: a shared literal would pass a
    # consumer that stamped every night with whichever sha it saw last.
    # ⛔ The digits must differ in the FIRST SEVEN characters — the renderer
    # abbreviates, so trailing-digit variation would compare equal on screen
    # and this stub would stop being able to tell the two cases apart.
    runs = [{"databaseId": 900 + i,
             "createdAt": "2026-08-%02dT03:35:00Z" % (20 + i),
             "headSha": ("%x" % (0xC0FFEE0 + i)).ljust(40, "0"),
             "conclusion": "success"}
            for i in range(min(limit, int(os.environ["STUB_RUNS"])))]
    print(json.dumps(runs))
    raise SystemExit(0)
if argv[:2] == ["run", "download"]:
    run_id = argv[2]
    dest = pathlib.Path(argv[argv.index("--dir") + 1])
    dest.mkdir(parents=True, exist_ok=True)
    if mode == "no-artifact":
        sys.stderr.write("no artifact matches\n")
        raise SystemExit(1)
    (dest / "bench-baseline.txt").write_text("BenchmarkX-4 1 1 ns/op\n")
    if mode == "no-paired-json":
        raise SystemExit(0)
    (dest / "bench-paired.json").write_text(json.dumps({
        "schema": "bench-paired/v2", "status": "OK", "cpu": "stub",
        "reference_sha": "3f" + "0" * 38,
        "evaluated": {
            "BenchmarkAlpha": {"ratio": 1.09},
            "BenchmarkControlCanaryCPU": {"ratio": 1.0002},
            "BenchmarkControlCanarySleep": {"ratio": 1.0000}},
        "inconclusive": {},
        "workload_drift": {"status": "checked", "files": ["config_test.go"]},
        "workload_digest": {
            "status": "checked", "files": ["config_test.go"],
            "sides": {"reference": {"digest": "a" * 64, "n_files": 8},
                      "main": {"digest": "b" * 64, "n_files": 8}}}}))
    raise SystemExit(0)
sys.stderr.write("unexpected argv: %r\n" % (argv,))
raise SystemExit(9)
"""


@pytest.fixture
def gh_stub(tmp_path):
    """Put a stub `gh` first on PATH and hand back a runner for the CLI."""
    binhome = tmp_path / "bin"
    binhome.mkdir()
    stub = binhome / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    def run(mode, runs=2, extra=()):
        env = dict(os.environ,
                   PATH=f"{binhome}{os.pathsep}{os.environ['PATH']}",
                   STUB_MODE=mode, STUB_RUNS=str(runs))
        return subprocess.run(
            [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
             "--from-gh", "--limit", "3",
             "--cache-dir", str(tmp_path / "cache"), *extra],
            capture_output=True, text=True, encoding="utf-8", env=env,
            timeout=120)
    return run


def test_from_gh_reads_real_artifacts_end_to_end(gh_stub):
    """The CI path, executed: list runs → download → parse → verdict."""
    proc = gh_stub("ok", runs=2)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    assert "**FINDINGS**" in proc.stdout
    assert "BenchmarkAlpha" in proc.stdout
    # Both nights carry the same digests, so the transition is a real
    # `unchanged` here — unlike the v1 dataset, where it must be UNKNOWN.
    assert "unchanged" in proc.stdout


def test_from_gh_reports_cannot_check_rather_than_crashing(gh_stub):
    """⛔ Exit 2, one line, no traceback.

    Reported in review: this raised `RuntimeError` out of `main()` for an exit
    of 1 and a stack trace, while the sibling `analyze_bench_history.py` prints
    one line and exits 2 on the identical failure. 1 vs 2 is not cosmetic in
    this repo — 1 is "checked, something is wrong" and 2 is "could not check",
    and `check_workload_closure_drift.py`'s tests pin that distinction as the
    bug class.
    """
    proc = gh_stub("auth-fail")
    assert proc.returncode == ptw.EXIT_CANNOT_CHECK
    assert proc.stderr.startswith("error:")
    assert "Traceback" not in proc.stderr


def test_from_gh_missing_gh_binary_is_cannot_check(tmp_path):
    env = dict(os.environ, PATH=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--from-gh", "--limit", "3"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert proc.returncode == ptw.EXIT_CANNOT_CHECK
    assert "not on PATH" in proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("mode", ["no-artifact", "no-paired-json"])
def test_from_gh_degrades_to_unreadable_never_to_clean(gh_stub, mode):
    """⛔ A night whose artifact is missing or lacks the paired file is
    `unreadable`, and a series of only those is INCONCLUSIVE — not CLEAR."""
    proc = gh_stub(mode, runs=2)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    assert "**INCONCLUSIVE**" in proc.stdout
    assert "**CLEAR**" not in proc.stdout
    assert "unreadable" in proc.stdout


def test_from_gh_two_runs_on_one_calendar_night_do_not_satisfy_k_of_2(tmp_path):
    """⛔ The review finding, pinned on the CI path rather than in isolation.

    `bench-record.yaml` carries `workflow_dispatch` alongside its cron, and a
    flaky nightly gets re-run, so two runs sharing a UTC date is routine. Before
    the fix, `fires()` walked the run LIST, so those two entries satisfied
    "2 consecutive nights" from ONE measurement occasion and the header read
    `FINDINGS over 2 counted night(s) ... (2026-08-20 .. 2026-08-20)`.
    """
    binhome = tmp_path / "bin"
    binhome.mkdir()
    stub = binhome / "gh"
    # Same stub, but every run reports the SAME createdAt date.
    stub.write_text(_GH_STUB.replace('"2026-08-%02dT03:35:00Z" % (20 + i)',
                                     '"2026-08-20T0%d:35:00Z" % (3 + i)'),
                    encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{binhome}{os.pathsep}{os.environ['PATH']}",
               STUB_MODE="ok", STUB_RUNS="2")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--from-gh", "--limit", "3", "--cache-dir", str(tmp_path / "cache")],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    # ⚠️ The assertion changed shape with #1571 §7 and is now STRICTLY
    # stronger. It used to read `1 counted calendar night(s) (2 run(s))`, whose
    # trailing `of {len(nights)}` was a RUN count sitting in a
    # calendar-night sentence. Both halves of both ratios are pinned here, so
    # the units cannot silently swap back.
    assert "1 of 1 calendar night(s)" in proc.stdout
    assert "2 of 2 run(s) counted" in proc.stdout
    assert "**FINDINGS**" not in proc.stdout


# ── head sha: the provenance that was downloaded and discarded ────────────

def _stub_head(i):
    """The head sha `_GH_STUB` reports for run `i`. Restated, not imported —
    the stub runs in another process, so the two have to be read side by side.
    """
    return ("%x" % (0xC0FFEE0 + i)).ljust(40, "0")


def _nights_table_rows(body):
    """The rows of the `### Nights` table ONLY.

    ⛔ Written because the first version of the test below matched every line
    starting `| 2026-08-`, which also catches the digest-transition table and
    compared its `**UNKNOWN**` cell against this table's head column. A row
    filter that spans tables silently reads the wrong column.
    """
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("| night |"))
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return lines[start], rows


def test_from_gh_stamps_each_night_with_its_own_head_sha(gh_stub):
    """⛔ Per-night, not per-series. `list_recent_runs` has always asked `gh`
    for `headSha` and this consumer dropped it, so a v1 rejection could describe
    the payload's shape and never the producer that wrote it.

    The stub gives every run a DIFFERENT head on purpose: a consumer that
    stamped all nights with whichever sha it read last would satisfy a
    "head_sha is not None" assertion and fail this one.
    """
    proc = gh_stub("ok", runs=2)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    assert _stub_head(0)[:7] != _stub_head(1)[:7], "stub heads collide"
    _, rows = _nights_table_rows(proc.stdout)
    heads = [row.split("|")[3].strip().strip("`") for row in rows]
    assert heads == [_stub_head(0)[:7], _stub_head(1)[:7]]


def test_from_gh_records_the_head_sha_even_when_the_artifact_never_arrives(
        gh_stub):
    """⛔ The night with the LEAST to go on is the one that needs it most.

    This is why the stamp happens after loading rather than being threaded into
    the loader: a download failure never reaches `load_night`, so a
    loader-parameter design would have left exactly these rows blank.
    """
    proc = gh_stub("no-artifact", runs=2)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    _, rows = _nights_table_rows(proc.stdout)
    assert rows and all("unreadable" in row for row in rows)
    heads = {row.split("|")[3].strip().strip("`") for row in rows}
    assert heads == {_stub_head(0)[:7], _stub_head(1)[:7]}


def test_render_writes_a_dash_for_an_unknown_head_not_an_empty_cell():
    """The archival replay path has no run record, so its head is unknown.

    ⛔ An empty Markdown cell is indistinguishable from a broken renderer, and
    this is the column whose entire job is to say what is and is not known —
    the same could-not-measure/measured conflation, one column wide.
    """
    body = ptw.render(ptw.decide(ptw.nights_from_dataset(DATASET)))
    header, rows = _nights_table_rows(body)
    assert "| head |" in header
    assert len(rows) == 6, rows
    assert {row.split("|")[3].strip() for row in rows} == {"—"}


def test_headline_never_folds_undated_runs_into_one_calendar_night():
    """⛔ Both blind reviewers of this change found this, independently.

    `nights_from_gh` writes `night_utc = created[:10] or None`, so every run
    `gh` reports without a usable `createdAt` collapses onto the single key
    None. The first cut of the unit fix deduplicated ALL nights by date, which
    turned three distinct dateless runs into "1 calendar night" — a night
    nobody measured, printed as a known quantity, in the very line the fix was
    meant to make honest. ⚠️ The un-dated group is also the one most likely to
    be the download failures, i.e. the rows with the least left to say.

    Counting them as N would be a different fabrication (nothing knows whether
    they share a night), so they leave the calendar ratio entirely and are
    reported as a count that cannot be placed.
    """
    # ⚠️ These two are un-dated AND unreadable, but those are INDEPENDENT
    # axes and this fixture must not be read as a causal pair: `nights_from_gh`
    # derives `night_utc` from `createdAt` before it tries the download, so a
    # download failure keeps its date. An earlier comment on this change
    # asserted the link and was wrong; the sibling test below covers the
    # un-dated-and-counted quadrant so neither case can be assumed.
    nights = [ptw.Night(None, 101).unreadable("artifact download failed"),
              ptw.Night(None, 102).unreadable("artifact download failed"),
              _run("2026-08-20", 103, alpha=1.0)]
    body = ptw.render(ptw.decide(nights))
    headline = [line for line in body.splitlines() if line.startswith("**")][0]
    # 1 dated calendar night, and the run half still accounts for all three.
    assert "1 of 1 calendar night(s)" in headline
    assert "1 of 3 run(s) counted" in headline
    assert "2 of 2" not in headline and "1 of 2 calendar" not in headline
    # ⛔ And they must not simply vanish: excluded from the ratio, named below.
    assert "2 run(s) carry no usable date" in body
    # ⛔ The disclosure must not contradict the headline it sits under. Its
    # first draft said "(the run ratio still counts them)" while the headline
    # said "1 of 3 run(s) counted" — and these two are UNREADABLE, so they are
    # not counted at all. Found by review of the fix round, which is the round
    # this repo's own history says is most likely to carry a new defect.
    assert "0 of them are counted" in body
    assert "still counts them" not in body
    # And the span must not print a night label nobody wrote (pre-existing,
    # fixed here because the line above now promises `?`).
    assert "None .." not in body and "?#1 .. 2026-08-20" in body


def test_undated_runs_do_not_manufacture_a_multi_run_night_warning():
    """The `workflow_dispatch` caveat is a claim about re-runs; nothing here
    measured one. Comparing calendar nights against ALL counted runs made a
    single undated counted run print it anyway."""
    night = ptw.Night(None, 101)
    night.canary_pct["BenchmarkControlCanaryCPU"] = 0.02
    night.canary_pct["BenchmarkControlCanarySleep"] = 0.0
    night.ratios_pct["BenchmarkAlpha"] = 1.0
    body = ptw.render(ptw.decide([night, _run("2026-08-20", 102, alpha=1.0)]))
    assert "carry no usable date" in body
    assert "more than one run" not in body
    # ⛔ The positive control for the assertion above: here the undated run IS
    # counted, so the disclosure must say so rather than reusing one phrasing
    # for both cases. Without this pair, "0 of them are counted" could be
    # hard-coded and nothing would notice.
    assert "1 of them is counted" in body


@pytest.mark.parametrize("canary_pct, expect_counted", [
    # ⛔ The first row is the dangerous one and the reason this is parametrised.
    # 0.8% PASSES the default 1.0% gate — the night is COUNTED — and still fails
    # the 0.5% candidate, so the counterfactual table alone was enough to kill
    # the page for a night the engine had just accepted.
    (0.8, True),
    (4.0, False),
])
def test_an_undated_night_never_takes_the_whole_page_down(canary_pct,
                                                          expect_counted):
    """⛔ PRE-EXISTING crash, verified against `origin/main` before fixing.

    `counterfactual_gates` put `night.night_utc` in a set and sorted it, so one
    readable night without a date mixed `None` with `str` and raised TypeError.
    render() died with exit 1 — the code this module documents as NEVER USED so
    that a non-zero exit cannot be read as "found a regression".

    Reproduction on a clean worktree at `origin/main` (5b7f6c3):

        CRASH TypeError sequence item 0: expected str instance, NoneType found

    ⇒ this test's counterfactual is NOT zero: it fails on unpatched main, which
    is the strongest form of evidence available here and the reason this case is
    in the suite rather than in a follow-up ticket.
    """
    undated = ptw.Night(None, 101)
    undated.canary_pct["BenchmarkControlCanaryCPU"] = canary_pct
    undated.canary_pct["BenchmarkControlCanarySleep"] = 0.0
    undated.ratios_pct["BenchmarkAlpha"] = 1.0
    result = ptw.decide([undated, _run("2026-08-20", 102, alpha=1.0)])
    assert (undated.outcome == ptw.NIGHT_COUNTED) is expect_counted
    body = ptw.render(result)          # ⛔ this is the assertion: it must return
    # Named as its own term, never folded into the calendar-night count — two
    # different undated nights must not collapse into one rejection.
    assert "+ 1 undated run(s)" in body
    assert "| 0.5% | 0 + 1 undated run(s) |" in body


def test_one_predicate_decides_a_nights_label_everywhere_it_is_printed():
    """⛔ The span, the Nights table and the calendar ratio must agree.

    Review found them disagreeing: two used `night_utc or '?'` and one used an
    `isinstance` test, so a truthy non-string date was classified as un-dated by
    the ratio while the span and the table printed it verbatim — the page
    saying a run appears as `?` while showing a night label nobody wrote.

    ⚠️ HONEST REACHABILITY. Both production loaders reject this shape already
    (`nights_from_dataset` raises, `nights_from_gh` only makes `str` or `None`),
    so this test's counterfactual detection against CI inputs is ZERO. It is
    here for the reason `_ordering_key` documents for its own guard: Nights are
    also built by hand, and the disagreement between the three call sites was
    real regardless of who could reach it.
    """
    hand_built = ptw.Night(20260816, 1)
    hand_built.canary_pct["BenchmarkControlCanaryCPU"] = 0.02
    hand_built.canary_pct["BenchmarkControlCanarySleep"] = 0.0
    hand_built.ratios_pct["BenchmarkAlpha"] = 1.0
    body = ptw.render(ptw.decide([hand_built, _run("2026-08-20", 2, alpha=1.0)]))
    assert "20260816" not in body, "a night label nobody wrote reached the page"
    assert "carry no usable date" in body
    _, rows = _nights_table_rows(body)
    assert "| ?#1 |" in rows[0]


def test_from_gh_never_borrows_a_head_sha_for_an_unidentifiable_run(tmp_path):
    """⛔ Found by blind review: `{run["databaseId"]: ...}` is last-one-wins.

    Two runs reported without a `databaseId` would share the key None, so every
    night whose run id is also None would be stamped with whichever head came
    last — a wrong sha that renders exactly like a right one, in the column
    that exists to attribute payloads to producers.
    """
    binhome = tmp_path / "bin"
    binhome.mkdir()
    stub = binhome / "gh"
    stub.write_text(_GH_STUB.replace('"databaseId": 900 + i,',
                                     '"databaseId": None,'),
                    encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{binhome}{os.pathsep}{os.environ['PATH']}",
               STUB_MODE="ok", STUB_RUNS="2")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--from-gh", "--limit", "3", "--cache-dir", str(tmp_path / "cache")],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert proc.returncode == ptw.EXIT_OK, proc.stderr
    _, rows = _nights_table_rows(proc.stdout)
    assert rows, "no night rows rendered"
    assert {row.split("|")[3].strip() for row in rows} == {"—"}
    for i in (0, 1):
        assert _stub_head(i)[:7] not in proc.stdout


def test_summary_headline_keeps_both_halves_of_each_ratio_in_one_unit():
    """#1571 §7, as a property rather than a fixed string.

    ⚠️ NARROWER THAN THE TICKET. #1571 §7 reads as though the whole headline
    were undisclosed; it is not — `render()` already appends a caveat when a
    calendar night carries more than one run. The undisclosed part was the
    denominator `of {len(nights)}`, a run count in a calendar-night sentence.
    Only that changed, and the ticket's framing is corrected in the PR body
    rather than quietly satisfied.
    """
    body = ptw.render(ptw.decide(ptw.nights_from_dataset(DATASET)))
    headline = [line for line in body.splitlines() if line.startswith("**")][0]
    assert re.search(r"\b6 of 6 calendar night\(s\)", headline)
    assert re.search(r"\b6 of 6 run\(s\) counted", headline)
    # ⛔ The old mixed form must not come back under a different wording.
    assert "counted calendar night(s) (" not in headline


def _run(date, run_id, alpha=None):
    night = ptw.Night(date, run_id)
    night.canary_pct["BenchmarkControlCanaryCPU"] = 0.02
    night.canary_pct["BenchmarkControlCanarySleep"] = 0.00
    if alpha is not None:
        night.ratios_pct["BenchmarkAlpha"] = alpha
    return night


def test_two_runs_of_one_night_that_disagree_are_undecidable_not_a_vote():
    """⛔ Fail closed. Never pick a winner, never average — an averaged reading
    is a number nobody measured.

    ⛔ ASSERTED AT `_night_reading`, NOT THROUGH `decide()`, and the reason is a
    correction. The first version of this test went through `decide()` and
    asserted `fired == {}` — and the break harness showed it STILL PASSED with
    the disagreement guard removed. With the guard gone the code falls through
    to `verdicts.pop()`, and CPython pops `False` first out of `{True, False}`
    (bools hash as 0 and 1), which breaks the run just as undecidable does. The
    test was green for a reason that had nothing to do with the guard. Pinning
    the tri-state return directly is the only assertion that survives the
    implementation detail.
    """
    over, under = _run("2026-08-20", 1, 9.0), _run("2026-08-20", 2, 0.5)
    assert ptw._night_reading([over, under], "BenchmarkAlpha", 5.0) is None
    assert ptw._night_reading([over], "BenchmarkAlpha", 5.0) is True
    assert ptw._night_reading([under], "BenchmarkAlpha", 5.0) is False
    nxt = _run("2026-08-21", 3, 9.0)
    assert ptw.decide([over, under, nxt], k=2)["fired"] == {}


def test_a_run_missing_the_benchmark_makes_its_whole_night_undecidable():
    """⛔ Also asserted at `_night_reading` — for the same reason, found the
    same way.

    A calendar night with two runs where only ONE measured the benchmark is not
    a night that measured it. Replacing the `return None` with a `continue`
    (skip that run, judge on the other) left every `decide()`-level test green,
    because the single-run groups those tests use never reach the difference.
    """
    assert ptw._night_reading(
        [_run("2026-08-20", 1, 9.0), _run("2026-08-20", 2)],
        "BenchmarkAlpha", 5.0) is None
    assert ptw._night_reading(
        [_run("2026-08-20", 1)], "BenchmarkAlpha", 5.0) is None


def test_partial_canary_failure_fails_closed():
    """⛔ Two canaries, one unreadable: the survivor's calm reading is evidence
    about that canary, not about the night. Reported in review — the night was
    counted and the table showed a reassuring `+0.01%`."""
    night = ptw.Night("2026-08-20", 1)
    night.canary_pct["BenchmarkControlCanarySleep"] = 0.01
    night.inconclusive["BenchmarkControlCanaryCPU"] = "unreadable-ratio (nan)"
    # (Sleep present and calm, CPU — the ONLY gating canary — present but
    #  unreadable. The survivor is informational and must not stand in.)
    night.ratios_pct["BenchmarkAlpha"] = 0.5
    ptw.apply_gate(night, 1.0)
    assert night.outcome == ptw.NIGHT_NOT_COUNTED
    assert "not established" in night.reason
    assert "BenchmarkControlCanaryCPU" in night.reason
    assert ptw.decide([night])["status"] == ptw.STATUS_INCONCLUSIVE


# ── 5. FIXES FOR THE SECOND REVIEW ROUND ──────────────────────────────────
#
# Each of these pins a defect that was found by an independent reviewer against
# the first round of this change, reproduced by the author before being fixed,
# and is dogfooded by intentional break like everything above.

def test_rule_that_cannot_complete_reports_inconclusive_not_clear():
    """⛔ The worst defect this change had, and ADR-032 pinned the requirement.

    Six nights, one benchmark at +42% EVERY night, canary gating out alternate
    nights so no two counted nights are adjacent. `fires()` cannot return
    anything, and "nothing fired" rendered as CLEAR with the benchmark's name
    and its +42% appearing NOWHERE on the page.

    ADR-032 §待決 6, verbatim: 切換當下比值序列從零開始，持續判準需要幾夜才能發射
    ——那幾夜必須回報「無法評估」，不得回報「無發現」。
    """
    nights = []
    for i in range(6):
        night = ptw.Night(f"2026-08-{10 + i}", i)
        night.canary_pct["BenchmarkControlCanaryCPU"] = 9.0 if i % 2 else 0.02
        night.canary_pct["BenchmarkControlCanarySleep"] = 0.00
        night.ratios_pct["BenchmarkAlpha"] = 42.0
        nights.append(night)
    result = ptw.decide(nights, k=2)
    assert result["status"] == ptw.STATUS_INCONCLUSIVE
    assert result["unjudgeable"] == ["BenchmarkAlpha"]
    body = ptw.render(result)
    assert "BenchmarkAlpha" in body
    assert "**CLEAR**" not in body


def test_the_jobs_own_first_nights_are_inconclusive():
    """The CI job runs `--from-gh --limit 14`, and nights predating the paired
    pipeline load as `unreadable` — so at first there is ONE counted night and
    a k=2 rule that cannot complete."""
    first = ptw.Night("2026-08-20", 1)
    first.canary_pct["BenchmarkControlCanaryCPU"] = 0.02
    first.canary_pct["BenchmarkControlCanarySleep"] = 0.00
    first.ratios_pct["BenchmarkAlpha"] = 42.0
    older = [ptw.Night(f"2026-08-{15 + i}", 90 + i).unreadable("pre-pipeline")
             for i in range(3)]
    assert ptw.decide(older + [first], k=2)["status"] == ptw.STATUS_INCONCLUSIVE


def test_judgeable_needs_k_consecutive_readings_not_k_readings():
    over = [_run(f"2026-08-{10 + i}", i, 9.0) for i in range(3)]
    assert ptw.judgeable(over, ["BenchmarkAlpha"], 5.0, 2)["BenchmarkAlpha"] is True
    gapped = [_run("2026-08-10", 1, 9.0), _run("2026-08-11", 2),
              _run("2026-08-12", 3, 9.0)]
    assert ptw.judgeable(gapped, ["BenchmarkAlpha"], 5.0, 2)["BenchmarkAlpha"] is False


def test_judgeable_needs_every_run_of_a_night_to_carry_the_reading():
    """⛔ `all`, not `any`, and the break harness is why this test exists.

    A calendar night where only ONE of two runs measured the benchmark did not
    measure it — the same reasoning `_night_reading` uses. With `any` the
    single-run tests above stay green (there `all` and `any` coincide), so the
    weaker predicate shipped unnoticed until the guard was deliberately broken.
    """
    partial = [_run("2026-08-10", 1, 9.0), _run("2026-08-10", 2),
               _run("2026-08-11", 3, 9.0)]
    assert ptw.judgeable(partial, ["BenchmarkAlpha"], 5.0, 2)["BenchmarkAlpha"] is False
    full = [_run("2026-08-10", 1, 9.0), _run("2026-08-10", 2, 9.0),
            _run("2026-08-11", 3, 9.0)]
    assert ptw.judgeable(full, ["BenchmarkAlpha"], 5.0, 2)["BenchmarkAlpha"] is True


def test_two_runs_of_one_night_are_one_night_for_the_consecutive_rule():
    """⛔ Pins the grouping itself, which the CI-path test above no longer can.

    That test asserts `FINDINGS` is absent, and after the `judgeable()` gate
    landed it would pass even with grouping broken — one calendar night is not
    judgeable at k=2, so the verdict is INCONCLUSIVE either way. It was green
    for the wrong reason; the harness caught that. Here both calendar nights ARE
    judgeable, so the only thing separating correct from broken is whether the
    rule counts nights or list entries.
    """
    nights = [_run("2026-08-10", 1, 9.0),   # night A, run 1 — over
              _run("2026-08-10", 2, 9.0),   # night A, run 2 — over
              _run("2026-08-11", 3, 0.5)]   # night B — under
    result = ptw.decide(nights, k=2)
    # Two calendar nights, only the first over ⇒ the run never reaches 2.
    assert result["fired"] == {}
    assert result["status"] == ptw.STATUS_CLEAR
    # And when a second calendar night IS over, the fire is dated to THAT night.
    nights[2] = _run("2026-08-11", 3, 9.0)
    assert ptw.decide(nights, k=2)["fired"] == {"BenchmarkAlpha": "2026-08-11"}


def test_an_undated_run_never_completes_a_k_consecutive_sequence():
    """⛔ The mirror of the test above, and the same disease with sides swapped.

    That one pins "two runs of ONE occasion are not two nights". This one pins
    "ZERO known occasions are not one night" — a run whose date nobody knows
    used to become a calendar night of its own (`None` is a valid dict key), sit
    beside the real nights, and COMPLETE the run.

    ⚠️ The positive control is the whole test. Asserting only that the pair does
    not fire proves nothing: a single dated night does not fire at k=2 either,
    so the assertion passes with the guard removed AND with the rule broken in
    the other direction. The two halves below differ by exactly one thing —
    whether the second night has a date — and they must disagree.
    """
    dated_only = [_run("2026-08-20", 102, 9.0)]
    with_undated = [_run(None, 101, 9.0), _run("2026-08-20", 102, 9.0)]
    two_dated = [_run("2026-08-19", 101, 9.0), _run("2026-08-20", 102, 9.0)]

    # ⛔ The un-dated run must buy NOTHING: same verdict as the lone night.
    assert ptw.decide(with_undated, k=2)["fired"] == {}
    assert ptw.decide(with_undated, k=2)["status"] == \
        ptw.decide(dated_only, k=2)["status"]
    # ⭐ Positive control: give that second night a real date and it DOES fire.
    # Without this the assertions above are satisfied by a rule that never fires.
    assert ptw.decide(two_dated, k=2)["fired"] == {"BenchmarkAlpha": "2026-08-20"}


def test_a_series_of_only_undated_runs_is_inconclusive_never_clear():
    """⛔ The guard above must not become a new way to render silence as CLEAR.

    Excluding un-dated runs from the calendar-night rule means a series of
    nothing but un-dated runs produces no groups at all. `judgeable()` has to
    read that as "the rule could not run", not as "the rule ran and found
    nothing" — the single distinction this whole module exists to preserve.
    """
    result = ptw.decide([_run(None, 101, 9.0), _run(None, 102, 9.0)], k=2)
    assert result["status"] == ptw.STATUS_INCONCLUSIVE
    assert result["status"] != ptw.STATUS_CLEAR
    assert result["fired"] == {}


def test_excluding_undated_runs_from_the_rule_does_not_hide_them():
    """⛔ Excluded from the RULE, not dropped from the PAGE.

    A night that vanishes from the report is the failure this module refuses;
    the guard would trade one silence for another if these rows disappeared.
    """
    body = ptw.render(ptw.decide([_run(None, 101, 9.0),
                                  _run("2026-08-20", 102, 9.0)], k=2))
    header, rows = _nights_table_rows(body)
    assert len(rows) == 2, rows                      # both listed
    assert "| ?#1 |" in rows[0]                        # the un-dated one, labelled
    assert "1 of 1 calendar night(s)" in body        # calendar ratio excludes it
    assert "2 of 2 run(s) counted" in body           # run total still carries it
    assert "1 run(s) carry no usable date" in body   # and it is named
    # ⛔ And its READING survives too. This assertion is why the test exists at
    # all: writing it found a further site of the same defect family
    # (`over_not_sustained` keyed by the raw date), which took the whole page
    # down with a TypeError before this run reached the table.
    # ⚠️ The label is `?#101`, not a bare `?`: a later round found that a shared
    # `?` merged distinct un-dated runs and dropped a reading, so the key now
    # carries the run id. See `_night_key`.
    assert "| `BenchmarkAlpha` | 1 (08-20) + 1 undated run(s) (?#1) |" in body


def test_two_undated_runs_are_two_readings_not_one():
    """⛔ The defect the PREVIOUS round's own fix introduced.

    That round keyed `over_not_sustained` on `_night_label()`, which returns a
    bare `?` for every un-dated run. Two distinct un-dated runs over the
    threshold therefore shared one dict key: the smaller reading was overwritten
    and left the page entirely, while the disclosure two lines above said both
    runs were counted. The docstring justifying that same fix asserted
    "excluded is not dropped" — and dropped one.

    ⚠️ The one-reading-only assertion is the point. Asserting `len == 2` alone
    would pass on a rule that merges nothing anywhere; asserting the smaller
    value is PRESENT is what pins the merge.
    """
    result = ptw.decide([_run(None, 1, 9.0), _run(None, 2, 12.0)], k=5)
    hits = result["over_not_sustained"]["BenchmarkAlpha"]
    assert len(hits) == 2, hits
    assert sorted(hits.values()) == [9.0, 12.0]      # ⛔ neither reading lost
    body = ptw.render(result)
    assert "| `BenchmarkAlpha` | 0 (—) + 2 undated run(s) (?#1, ?#2) |" in body


def test_inconclusive_reasons_from_an_undated_night_do_not_kill_the_page():
    """⛔ PRE-EXISTING, reproduced on `origin/main` before fixing.

    `inconclusive` was keyed on the raw `night_utc`, so an un-dated COUNTED
    night missing a reading put `None` in a dict beside a date string, and
    `render()`'s `sorted(per_night.items())` raised TypeError — the whole page,
    Nights table included, replaced by a traceback and exit 1.

        origin/main 7fe8515f, same probe:
        TypeError: '<' not supported between instances of 'str' and 'NoneType'

    ⚠️ The previous round's prose called `over_not_sustained` the "third call
    site", which read as "all of them". This was the fourth.
    """
    result = ptw.decide([_run("2026-08-18", 1, 9.0),
                         _run("2026-08-20", 2),      # dated, no reading
                         _run(None, 3)], k=2)        # un-dated, no reading
    body = ptw.render(result)                        # ⛔ must not raise
    keys = result["inconclusive"]["BenchmarkAlpha"]
    # ⛔ Tuples, not `?#1`: a later round made the key structured so that a
    # night whose date IS the string `?#1` cannot collide with the first
    # un-dated run. `key_label` is what the page prints.
    assert set(keys) == {(ptw.KEY_UNDATED, 1),
                         (ptw.KEY_DATED, "2026-08-20")}, keys
    assert "?#1" in body

    # ⛔ BOTH assignment sites, because `inconclusive` is populated twice and a
    # first cut of this test only reached one. `decide()` fills it from
    # `night.inconclusive` (a reason the payload itself carried) AND from the
    # union pass for benchmarks a night mentioned nowhere. The break harness
    # said so: reverting the first site turned nothing red.
    carried = _run(None, 4)
    carried.inconclusive["BenchmarkAlpha"] = "unreadable-ratio (nan)"
    result = ptw.decide([_run("2026-08-18", 1, 9.0), carried], k=2)
    keys = result["inconclusive"]["BenchmarkAlpha"]
    assert set(keys) == {(ptw.KEY_UNDATED, 1)}, keys
    assert "?#1" in ptw.render(result)


def test_undated_nights_do_not_swallow_a_work_definition_move():
    """⛔ The site review did NOT report — found by enumerating every key/sort.

    `digest_transitions` kept one entry per `night_utc`, so N un-dated nights
    collapsed onto the single key `None` and only the last survived. Three
    un-dated nights whose work-definition digest changed twice reported ZERO
    transitions: two MOVES gone, from the one table whose entire job is to say
    the two sides stopped measuring the same thing.

    ⚠️ That is worse than a wrong number. ADR-032 §工作定義漂移 rests on this
    table — a ratio only means anything while both sides do the same work.
    """
    sha = {"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}

    def digested(run_id, which):
        night = _run(None, run_id, 1.0)
        night.digest_status = "checked"
        night.digest_sides = {"reference": sha[which], "main": sha[which]}
        return night

    transitions = ptw.digest_transitions(
        [digested(1, "a"), digested(2, "b"), digested(3, "c")])
    assert len(transitions) == 2, transitions
    assert [t["from"] for t in transitions] == ["?#1", "?#2"]
    assert all(t["sides"]["reference"] is True for t in transitions)  # MOVED


@pytest.mark.parametrize("ids", [
    (None, None),        # ⛔ both loaders allow a null run_id
    (101, "101"),        # ⛔ and both allow int AND str for the same value
])
def test_undated_runs_stay_distinct_even_when_their_run_ids_are_not(ids):
    """⛔ The previous round keyed un-dated runs on `f"?#{run_id}"`.

    `run_id` is not an identity. `nights_from_dataset` passes `run_id is None`
    through explicitly and accepts int OR str; `databaseId` can be absent from a
    `gh` record. Both cases formatted to the SAME key — `?#None`, or `?#101` for
    an int and a string of the same digits — and the readings merged, dropping
    the smaller one. That is the exact defect the key was introduced to end,
    surviving inside the fix for it.

    ⇒ the key is an ordinal over the series now. Position is the only thing an
    un-dated run reliably has.
    """
    left, right = ids
    result = ptw.decide([_run(None, left, 9.0), _run(None, right, 12.0)], k=5)
    hits = result["over_not_sustained"]["BenchmarkAlpha"]
    assert sorted(hits.values()) == [9.0, 12.0], hits   # ⛔ neither lost
    assert set(hits) == {(ptw.KEY_UNDATED, 1), (ptw.KEY_UNDATED, 2)}, hits


def test_a_run_id_ten_characters_long_is_not_mistaken_for_a_date():
    """⛔ `_short_night` used to strip a year from anything of length 10.

    `?#12345678` is ten characters, so it rendered as `45678`: the `?` gone, the
    id truncated, a label nobody wrote — produced by the one helper whose whole
    job is to stop that. Length is a guess about dates; this asks whether the
    label IS one.

    ⚠️ Reachable through `nights_from_dataset`, which accepts a string run_id of
    any length. The `gh` path happens to produce 11-digit ids, which is luck,
    not a guard.
    """
    assert ptw._short_night("?#12345678") == "?#12345678"
    assert ptw._short_night("2026-08-20") == "08-20"
    assert ptw._short_night("?#1") == "?#1"


def test_the_not_sustained_table_counts_nights_not_undated_runs():
    """⛔ Two numbers on one page must not contradict each other.

    Giving each un-dated run its own key fixed the dropped reading and then
    counted those runs as NIGHTS: a page whose headline said `1 of 1 calendar
    night(s)` carried a row claiming `4`. Three of those four are runs nobody
    can place, and one of them may BE the dated night.

    The gate table already answers this with `+ N undated run(s)`; the same
    shape is used here rather than a second invention.
    """
    result = ptw.decide([_run("2026-08-20", 1, 9.7), _run(None, 2, 9.0),
                         _run(None, 3, 9.1), _run(None, 4, 9.2)], k=5)
    body = ptw.render(result)
    assert "**INCONCLUSIVE** over 1 of 1 calendar night(s)" in body
    assert ("| `BenchmarkAlpha` | 1 (08-20) + 3 undated run(s) "
            "(?#1, ?#2, ?#3) |") in body
    # ⛔ The old form claimed four nights. It must not come back in any wording.
    assert "| 4 (" not in body


def test_a_reading_over_the_threshold_is_never_silently_omitted():
    """Not a finding, but a reading the page never mentions is
    indistinguishable from a reading that never happened."""
    nights = [_run("2026-08-10", 1, 9.0), _run("2026-08-11", 2, 0.5),
              _run("2026-08-12", 3, 9.0)]
    result = ptw.decide(nights, k=2)
    assert result["status"] == ptw.STATUS_CLEAR      # the rule DID run
    assert "BenchmarkAlpha" in result["over_not_sustained"]
    assert "Over the threshold, but not sustained" in ptw.render(result)


def test_reference_pin_is_unknown_when_a_side_has_no_sha():
    """⛔ The column rendered `same` for a pin that was never compared, directly
    above the note forbidding that reading.

    ⛔ CORRECTION: this docstring used to say "schema v1 carries no
    `reference_sha`". Measured false in a later review — v1 has carried it
    since #1455. The reachable route is that `pair_bench_ratio.py` defaults
    `--reference-sha` to `None`, so an ordinary v2 payload can carry
    `"reference_sha": null`; that is what the test below constructs.
    """
    older = _run("2026-08-22", 1, 1.0)
    older.reference_sha = None
    newer = _run("2026-08-23", 2, 1.0)
    newer.reference_sha = "9" * 40
    assert ptw.digest_transitions([older, newer])[0]["reference_pin_changed"] is None
    row = [l for l in ptw.render(ptw.decide([older, newer])).splitlines()
           if "→ 2026-08-23" in l][0]
    assert row.count("**UNKNOWN**") == 3
    assert "same" not in row


@pytest.mark.parametrize("files", [None, "config_test.go", {"a": 1}, 3])
def test_drift_checked_with_a_non_list_files_is_unreadable(files):
    """⛔ Was byte-identical to a genuinely clean night. The digest path already
    failed closed on the same malformation; this is the asymmetry closed."""
    payload = _payload()
    payload["workload_drift"] = {"status": "checked"}
    if files is not None:
        payload["workload_drift"]["files"] = files
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.drift_status == "unreadable"
    assert night.drift_count is None


def test_drift_checked_with_a_genuinely_empty_list_stays_checked():
    """The counterpart — the fix must not turn a real clean answer unreadable."""
    payload = _payload()
    payload["workload_drift"] = {"status": "checked", "files": []}
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.drift_status == "checked"
    assert night.drift_count == 0


def test_absent_inconclusive_key_is_unreadable_like_absent_evaluated():
    """⛔ Absent is not empty. `pair_bench_ratio.py` argues the mirror case in
    its own source; this consumer had encoded the default it warns against."""
    payload = _payload()
    del payload["inconclusive"]
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.outcome == ptw.NIGHT_UNREADABLE
    assert "absent disclosure is not an empty one" in night.reason


def test_a_benchmark_missing_from_one_nights_payload_is_disclosed():
    """⛔ The `decide()` docstring claimed this and did not do it — the loop read
    only `night.inconclusive`, so a benchmark carried by neither `evaluated` nor
    `inconclusive` was invisible and the verdict was CLEAR."""
    nights = [_run("2026-08-10", 1, 40.0), _run("2026-08-11", 2),
              _run("2026-08-12", 3, 40.0)]
    result = ptw.decide(nights, k=2)
    assert "BenchmarkAlpha" in result["inconclusive"]
    assert "absent-from-payload" in result["inconclusive"]["BenchmarkAlpha"][
        (ptw.KEY_DATED, "2026-08-11")]
    assert "BenchmarkAlpha" in ptw.render(result)


# ── 6. FIXES FOR THE THIRD REVIEW ROUND ───────────────────────────────────

def test_judgeable_honours_gap_so_it_cannot_contradict_fires():
    """⛔ Two guards on the same page must not disagree.

    `fires()` honoured `--gap skip` and `judgeable()` did not, so under `skip` a
    benchmark that DID fire was listed in the same report under "the rule could
    NOT judge", and the verdict was downgraded FINDINGS → INCONCLUSIVE. That
    switch is the open question this tool exists to help decide, so adopting
    `skip` would have turned every sustained regression into an INCONCLUSIVE.
    """
    nights = [_run("2026-08-10", 1, 9.0),
              _run("2026-08-11", 2, 9.0),      # gated out below
              _run("2026-08-12", 3, 9.0)]
    nights[1].canary_pct["BenchmarkControlCanaryCPU"] = 4.0
    result = ptw.decide(nights, k=2, gap=ptw.GAP_SKIP)
    assert result["fired"] == {"BenchmarkAlpha": "2026-08-12"}
    assert result["unjudgeable"] == []
    assert result["status"] == ptw.STATUS_FINDINGS
    # and `break`, the default, still refuses to bridge the gated-out night
    broken = ptw.decide(nights, k=2, gap=ptw.GAP_BREAK)
    assert broken["fired"] == {}


def test_the_gate_counterfactual_uses_the_same_predicate_as_the_verdict():
    """⛔ The counterfactual table had its own copy of the gate, and the
    partial-canary fix landed in only one of them: the Nights table said
    `not-counted` while the gate table reported `0 nights rejected` at the same
    value. That table is the data ADR-032 §待決 5 says will pick the real
    threshold."""
    good = _run("2026-08-20", 1, 1.0)
    half = _run("2026-08-21", 2, 1.0)
    half.canary_pct = {"BenchmarkControlCanarySleep": 0.01}
    half.inconclusive["BenchmarkControlCanaryCPU"] = "unreadable-ratio (nan)"
    result = ptw.decide([good, half])
    assert [n.outcome for n in result["nights"]] == [
        ptw.NIGHT_COUNTED, ptw.NIGHT_NOT_COUNTED]
    for _gate, rejected, n_undated in ptw.counterfactual_gates(result["nights"]):
        assert rejected == ["2026-08-21"]
        assert n_undated == 0
    body = ptw.render(result)
    assert "| 0.5% | 1 (2026-08-21) |" in body


def test_gate_verdict_is_the_only_implementation_of_the_gate():
    """Structural: both call sites must route through the one predicate, so a
    future gate change cannot land in half the report."""
    # ⛔ Asked of the parser, not of a substring count — the first version of
    # this test asserted `"deviation > gate" not in body`, which is false of the
    # ONE legitimate comparison inside `gate_verdict` itself, and counted a
    # docstring mention of `unreadable_canaries` as a call site. Prose is not
    # code; the same lesson as the summary-only check above.
    deciders = set()
    for func in [n for n in ast.walk(_TREE)
                 if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if not isinstance(node, ast.Compare):
                continue
            # ⛔ ORDERING comparisons only. `render` legitimately does
            # `gate == result["gate_pct"]` to mark which row is the default —
            # that is a label, not an accept/reject decision. The invariant is
            # about who decides, so it is about `<`/`>`, not about `==`.
            if not any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                       for op in node.ops):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"gate_pct", "gate"}:
                deciders.add(func.name)
    assert deciders == {"gate_verdict"}, (
        f"the gate threshold is compared in {sorted(deciders)} — it must be "
        "decided in exactly one place, or a future change lands in half the "
        "report (which is how the counterfactual table came to disagree with "
        "the verdict)")


def test_a_v2_payload_can_carry_a_null_reference_sha():
    """⛔ The route that actually reaches the pin defect — recorded because the
    first attribution ("schema v1 has no `reference_sha`") was measured FALSE:
    v1 has carried it since #1455. `pair_bench_ratio.py` defaults
    `--reference-sha` to `None`, so an ordinary v2 payload can carry null."""
    payload = _payload()
    payload["reference_sha"] = None
    night = ptw.load_night(payload, night_utc="2026-08-23", run_id=1)
    assert night.readable
    assert night.reference_sha is None
    other = ptw.load_night(_payload(), night_utc="2026-08-24", run_id=2)
    assert ptw.digest_transitions(
        [night, other])[0]["reference_pin_changed"] is None


def test_the_new_job_cannot_take_the_nights_data_down_with_it():
    """⛔ Both watchdogs find prior nights with `--status success`, which keys on
    the RUN's conclusion, and one failed job fails the run.

    So an exit-2 from this unproven reporter would delete that night's
    `bench-paired.json` and `bench-baseline.txt` from BOTH watchdogs' future
    windows — silently shortening a window the operator believes is 14 nights.
    `continue-on-error` is what makes the "does not entangle" claim in the
    workflow comment actually true.
    """
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "bench-record.yaml").read_text(
            encoding="utf-8"))
    assert workflow["jobs"]["paired-trend-watch"]["continue-on-error"] is True
    # ⚠️ NOT asserted for `trend-watch`: it owns the `perf-trend` issue, so its
    # failure IS the nightly failing. Pinning the asymmetry so a future edit
    # cannot quietly make this reporter load-bearing, or that watchdog silent.
    assert "continue-on-error" not in workflow["jobs"]["trend-watch"]
    # The window query this depends on, pinned at its source.
    watchdog = (_REPO / "scripts" / "tools" / "dx"
                / "analyze_bench_history.py").read_text(encoding="utf-8")
    assert '"--status", "success"' in watchdog


# ── 7. FIXES FOR THE FIFTH REVIEW ROUND ───────────────────────────────────
#
# ⛔ All four of these exist because the break harness reported STILL GREEN for
# the guards the fixes had just added. The code was right and untested, which is
# the state this file's whole discipline exists to refuse.

def test_a_window_of_undecidable_nights_is_inconclusive_not_clear():
    """⛔ The seam between `judgeable()` and `fires()`.

    `judgeable()` used to ask "does every counted run carry a reading" while
    `fires()` asks "do they AGREE about the threshold". Two calendar nights,
    each measured twice with the runs straddling 5% (5.1 and 4.9): every night
    is undecidable, and the report said CLEAR. Both now route through
    `_night_reading`, so the seam cannot reopen.
    """
    nights = [_run("2026-08-20", 1, 5.1), _run("2026-08-20", 2, 4.9),
              _run("2026-08-21", 3, 5.1), _run("2026-08-21", 4, 4.9)]
    assert [ptw._night_reading(g, "BenchmarkAlpha", 5.0)
            for _d, g in ptw.calendar_nights(nights)] == [None, None]
    result = ptw.decide(nights, k=2)
    assert result["status"] == ptw.STATUS_INCONCLUSIVE
    assert result["unjudgeable"] == ["BenchmarkAlpha"]
    assert "**CLEAR**" not in ptw.render(result)


def test_over_not_sustained_counts_calendar_nights_not_runs():
    nights = [_run("2026-08-20", 1, 9.1), _run("2026-08-20", 2, 9.2)]
    result = ptw.decide(nights, k=2)
    assert result["over_not_sustained"]["BenchmarkAlpha"] == {
        (ptw.KEY_DATED, "2026-08-20"): 9.2}
    row = [l for l in ptw.render(result).splitlines()
           if "`BenchmarkAlpha` |" in l][0]
    assert "1 (08-20)" in row
    assert "08-20, 08-20" not in row


def test_the_gate_counterfactual_counts_calendar_nights_not_runs():
    """⛔ This table is the data ADR-032 §待決 5 says will pick the real
    threshold, so a re-run inflating its rejection count biases the very
    decision it exists to feed."""
    nights = [_run("2026-08-21", 1, 1.0), _run("2026-08-21", 2, 1.0)]
    for night in nights:
        night.canary_pct["BenchmarkControlCanaryCPU"] = 4.0
    result = ptw.decide(nights)
    for _gate, rejected, n_undated in ptw.counterfactual_gates(result["nights"]):
        assert rejected == ["2026-08-21"]
        assert n_undated == 0
    assert "2026-08-21, 2026-08-21" not in ptw.render(result)


def test_transitions_never_emit_a_night_to_itself():
    nights = [_run("2026-08-20", 1, 1.0), _run("2026-08-20", 2, 1.0),
              _run("2026-08-21", 3, 1.0)]
    recs = ptw.digest_transitions(nights)
    assert [(r["from"], r["to"]) for r in recs] == [("2026-08-20", "2026-08-21")]


def test_the_informational_canary_is_actually_rendered():
    """⛔ "Emitted + rendered for human eyes" is the stated reason the sleep
    canary is parsed at all, so it has to actually appear.

    The first cut of the gating fix added `informational_canary_pct` and never
    called it — dead code and a false justification in one stroke.
    """
    body = ptw.render(ptw.decide(ptw.nights_from_dataset(DATASET)))
    assert "canary (gating)" in body
    assert "canary (info)" in body
    assert "Sleep" in body
    # and the gating column must NOT be the max over both
    night = ptw.load_night(_payload(), night_utc="2026-08-23", run_id=1)
    night.canary_pct["BenchmarkControlCanarySleep"] = 9.0
    assert night.canary_deviation_pct < 1.0
    assert night.informational_canary_pct == {"BenchmarkControlCanarySleep": 9.0}


def test_the_unjudgeable_section_describes_the_predicate_it_actually_uses():
    """⛔ Self-caught: changing `judgeable()` from a presence check to an
    agreement check left the rendered explanation describing the OLD predicate.

    In the case below every night carried a reading, so "never had K
    consecutive counted nights carrying a reading" was simply false about the
    rows printed underneath it. A wrong explanation attached to a correct
    verdict is still a wrong page.
    """
    nights = [_run("2026-08-20", 1, 5.1), _run("2026-08-20", 2, 4.9),
              _run("2026-08-21", 3, 5.1), _run("2026-08-21", 4, 4.9)]
    result = ptw.decide(nights, threshold_pct=5.0, k=2)
    assert result["unjudgeable"] == ["BenchmarkAlpha"]
    body = ptw.render(result)
    assert "carrying a reading, so the sustained rule" not in body
    assert "the runs disagreed about" in body
    assert "THRESHOLD-DEPENDENT" in body
    # every night DID carry a reading — the old wording would have been a lie
    assert all("BenchmarkAlpha" in n.ratios_pct for n in nights)


def test_judgeable_is_threshold_dependent_and_that_is_deliberate():
    nights = [_run("2026-08-20", 1, 5.1), _run("2026-08-20", 2, 4.9),
              _run("2026-08-21", 3, 5.1), _run("2026-08-21", 4, 4.9)]
    assert ptw.judgeable(nights, ["BenchmarkAlpha"], 5.0, 2) == {
        "BenchmarkAlpha": False}
    assert ptw.judgeable(nights, ["BenchmarkAlpha"], 1.0, 2) == {
        "BenchmarkAlpha": True}


# ── 8. FIXES FOR THE SEVENTH REVIEW ROUND ─────────────────────────────────

def test_counterfactual_rows_distinguish_no_fires_from_no_answer():
    """⛔ The governing rule, one level down, in the table that will pick the
    real threshold.

    `fires()` returns `{}` both when the rule ran clean and when it could not
    produce an answer, and every row printed `0`. Two measured shapes: a
    two-night series makes every `K=3` row arithmetically unreachable, and a
    series whose same-night re-runs straddle 3% reads "5%: 0, 3%: 0, 2%: 0,
    1%: 1" — from which a maintainer concludes "3% buys nothing, go to 2%",
    when at 3% and 2% the rule answered nothing at all.
    """
    nights = [_run("2026-08-20", 1, 4.0), _run("2026-08-20", 2, 2.0),
              _run("2026-08-21", 3, 4.0), _run("2026-08-21", 4, 2.0)]
    rows = {(t, k): (fired, blind) for t, k, fired, blind
            in ptw.counterfactual_thresholds(nights, ["BenchmarkAlpha"],
                                             ptw.GAP_BREAK)}
    assert rows[(5.0, 2)] == ({}, [])                       # ran, found nothing
    assert rows[(3.0, 2)] == ({}, ["BenchmarkAlpha"])       # answered nothing
    assert rows[(1.0, 2)][0] == {"BenchmarkAlpha": "2026-08-21"}
    body = ptw.render(ptw.decide(nights))
    assert "| 3% | 2 | **n/a** | **1/1** |" in body
    assert "| 5% ← pinned | 2 | 0 | — |" in body


def test_a_two_night_series_marks_every_k3_row_unreachable():
    nights = [_run("2026-08-20", 1, 0.3), _run("2026-08-21", 2, 0.4)]
    rows = {(t, k): blind for t, k, _f, blind
            in ptw.counterfactual_thresholds(nights, ["BenchmarkAlpha"],
                                             ptw.GAP_BREAK)}
    assert all(rows[(t, 3)] == ["BenchmarkAlpha"] for t in (5.0, 3.0, 2.0, 1.0))
    assert all(rows[(t, 2)] == [] for t in (5.0, 3.0, 2.0, 1.0))


def test_the_gating_canary_column_carries_the_measured_sign():
    """⛔ A wrong number rendering perfectly: `max(abs(...))` printed a canary
    measured at −0.12% as `+0.12%`, asserting main was SLOWER on the control
    when it was faster — beside an informational column carrying the true sign.
    For a control canary the direction is the diagnostic."""
    night = _run("2026-08-20", 1, 1.0)
    night.canary_pct["BenchmarkControlCanaryCPU"] = -0.12
    night.canary_pct["BenchmarkControlCanarySleep"] = -0.09
    assert night.canary_deviation_pct == -0.12
    row = [l for l in ptw.render(ptw.decide([night])).splitlines()
           if l.startswith("| 2026-08-20 ")][0]
    assert "-0.12%" in row
    assert "+0.12%" not in row
    # and the gate still compares MAGNITUDE, so a negative excursion gates
    night.canary_pct["BenchmarkControlCanaryCPU"] = -4.0
    counts, reason = ptw.gate_verdict(night, 1.0)
    assert counts is False
    assert "-4.00%" in reason


def test_the_real_dataset_renders_the_canary_signs_it_measured():
    nights = {n.night_utc: n for n in ptw.nights_from_dataset(DATASET)}
    # 2026-08-17 measured CPU -0.12 / Sleep -0.09 (see nights.json)
    assert nights["2026-08-17"].canary_deviation_pct == -0.12
    row = [l for l in ptw.render(ptw.decide(list(nights.values()))).splitlines()
           if l.startswith("| 2026-08-17 ")][0]
    assert "| -0.12% | Sleep -0.09% |" in row


def test_the_docstrings_first_line_survives_the_tool_map():
    """⛔ `generate_tool_map.py` publishes only a docstring's FIRST LINE.

    A wrapped opening sentence therefore ships to `tool-map{,.en}.md` truncated
    mid-clause. This module did exactly that ("...whether main has a sustained",
    no verb, no period, in both languages) even though `converge_status.py`
    already carries a standing warning about the same trap — walked into twice,
    so it is pinned here rather than left to the next person's memory.
    """
    src = (_TOOLS_DIR / "paired_trend_watch.py").read_text(encoding="utf-8")
    first = src.split('"""', 2)[1].strip().split("\n")[0]
    assert first.endswith("."), (
        f"first docstring line {first!r} is not a complete sentence — it is "
        "what the tool map publishes verbatim")
    # ⛔ NO LENGTH BOUND. A first draft of this test also asserted
    # `len(first) <= 100`, and self-review found nothing enforces that:
    # `generate_tool_map.py` neither truncates nor wraps, and no lint caps the
    # description column. An assertion with no referent is a false red waiting
    # for a legitimately longer sentence — the "guard of a guard" this repo
    # deletes elsewhere. What matters is that the line is a whole sentence and
    # that the published row matches it, both checked here.
    # and the published row must match it
    row = [l for l in (_REPO / "docs" / "internal" / "tool-map.md")
           .read_text(encoding="utf-8").splitlines()
           if "`paired_trend_watch.py`" in l]
    assert len(row) == 1 and first in row[0]


# ── 9. FIXES FOR THE NINTH REVIEW ROUND ───────────────────────────────────

@pytest.mark.parametrize("payload", ["[]", '"a string"', "42", "null"])
def test_dataset_adapter_rejects_a_bad_shape_with_cannot_check(tmp_path, payload):
    """⛔ Exit 2, not the traceback-and-1 it used to give.

    `load_night()` has had a top-level shape guard from the start; this adapter
    did not, so a JSON array crashed with `AttributeError: 'list' object has no
    attribute 'get'` and Python's default exit 1 — the code this module reserves
    and documents as never chosen, so that non-zero can never read as "found a
    regression". The identical shape had already been fixed on the `--from-gh`
    side; this was the same defect at a second entrance.
    """
    (tmp_path / "nights.json").write_text(payload, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--dataset", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == ptw.EXIT_CANNOT_CHECK
    assert proc.stderr.startswith("error:")
    assert "Traceback" not in proc.stderr


def test_dataset_adapter_rejects_a_non_object_reference_block(tmp_path):
    payload = json.loads((DATASET / "nights.json").read_text(encoding="utf-8"))
    payload["reference"] = ["exporter/v2.9.0"]
    (tmp_path / "nights.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="'reference' is list"):
        ptw.nights_from_dataset(tmp_path)


def test_the_gate_message_never_invokes_a_canary_that_does_not_remain():
    """⛔ With ONE gating canary there is no "remaining" one.

    The old wording named the CPU canary as not-established and, in the same
    sentence, quoted "the remaining gating canary read +0.02%" — which was that
    same canary's own reading.
    """
    night = ptw.load_night(_payload(), night_utc="2026-08-20", run_id=1)
    night.inconclusive["BenchmarkControlCanaryCPU"] = "no denominator"
    counts, reason = ptw.gate_verdict(night, 1.0)
    assert counts is False
    assert reason == ("control canary not established: "
                      "BenchmarkControlCanaryCPU")
    assert "remaining" not in reason


def test_the_invented_justification_is_gone_from_every_copy():
    """⛔ A rationale this file records as INVENTED must not survive anywhere
    but in the record of the correction — a copy left in place quietly argues
    the overturned model back to the next reader."""
    src = (_TOOLS_DIR / "paired_trend_watch.py").read_text(encoding="utf-8")
    # ⛔ Exactly ONE occurrence, and it must be the header's correction record.
    # A first cut of this test asserted the same thing while the fix itself
    # quoted the sentence a second time to explain the deletion — which is the
    # sentence back in the reader's path with a disclaimer attached. One
    # record, in the place that records corrections.
    phrase = "deliberately different shapes"
    assert src.count(phrase) == 1
    header = src.split("GATING_CANARIES = ")[0]
    assert phrase in header


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.__setitem__("nights", {"a": 1}), "'nights' must be"),
    (lambda d: d["nights"].__setitem__(0, "nope"), "every night must be"),
    (lambda d: d["nights"][0].__setitem__("night_utc", 20260816), "night_utc is int"),
    (lambda d: d["nights"][0].__setitem__("night_utc", None), "night_utc is NoneType"),
    (lambda d: d["nights"][0].__setitem__("run_id", {"x": 1}), "run_id is dict"),
])
def test_every_malformed_dataset_shape_exits_cannot_check(tmp_path, mutate, expect):
    """⛔ Exit 2 and one line, for every shape the adapter reads.

    Two of these used to escape the round-17 guards entirely: a numeric
    `night_utc` died in the SORT with `TypeError: '<' not supported between
    instances of 'str' and 'int'` — traceback, exit 1 — and a wrong-typed
    `run_id` is the same defect latent, comparing only when two runs share a
    calendar night, which is precisely the re-run case handled everywhere else.
    Found in self-review by enumerating malformed shapes rather than by
    reasoning about which ones mattered.
    """
    payload = json.loads((DATASET / "nights.json").read_text(encoding="utf-8"))
    mutate(payload)
    (tmp_path / "nights.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--dataset", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == ptw.EXIT_CANNOT_CHECK
    assert "Traceback" not in proc.stderr
    assert expect in proc.stderr


def test_the_ordering_is_total_even_for_hand_built_nights():
    """⚠️ Defence in depth, not the fix — the boundary guard above is the fix.

    Nights are also built by hand (these tests, future adapters), and a sort
    must never be the thing that decides whether the tool runs at all.
    """
    a, b = ptw.Night("2026-08-20", {"x": 1}), ptw.Night("2026-08-20", 2)
    assert ptw.decide([a, b])["status"] == ptw.STATUS_INCONCLUSIVE
    assert ptw.decide([ptw.Night(None, 1), ptw.Night("2026-08-20", 2)])


def test_integer_run_ids_keep_numeric_order_not_string_order():
    """⛔ `digest_transitions` keeps a calendar night's LAST readable run, so a
    string sort putting run 10 before run 9 would change which one that is."""
    nights = [ptw.Night("2026-08-20", 9), ptw.Night("2026-08-20", 10)]
    assert [n.run_id for n in sorted(nights, key=ptw._ordering_key)] == [9, 10]


# ── 10. CLI RULE-PARAMETER VALIDATION (CodeRabbit, PR #1536) ──────────────

def _cli(*extra):
    return subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "paired_trend_watch.py"),
         "--dataset", str(DATASET), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest.mark.parametrize("flag,value", [
    ("--threshold-pct", "nan"),
    ("--threshold-pct", "inf"),
    ("--canary-gate-pct", "nan"),
    ("--canary-gate-pct", "-1"),
    ("--limit", "0"),
    ("--limit", "-5"),
    ("--consecutive", "0"),
])
def test_unusable_rule_parameters_are_rejected_at_the_boundary(flag, value):
    """⛔ A rule that cannot fire must not render a verdict.

    Reported by CodeRabbit for `--threshold-pct` / `--canary-gate-pct`, and the
    enumeration that followed found two more it had not named. Measured before
    the fix, on the frozen six-night series which DOES contain a fire at 5%:

        --threshold-pct nan     → **CLEAR**       (every `>` comparison False)
        --threshold-pct inf     → **CLEAR**       (nothing can exceed infinity)
        --canary-gate-pct nan   → **FINDINGS**    (gate silently disabled —
                                                   `abs(dev) > nan` is False,
                                                   so no night is rejected)
        --canary-gate-pct -1    → **INCONCLUSIVE**(every night rejected)
        --limit 0 / -5          → accepted outright

    Only `-inf` was already refused, and only because argparse cannot parse it.

    ⚠️ The assertion is `stdout == ""`, not merely "no **CLEAR**". Measured: that
    change buys ZERO detection today — removing any one of the four guards is
    caught by `returncode == 2` first, identically under both forms (4/4 breaks,
    same failure counts). It is here for the exit-code change `main()` already
    anticipates: "Exit 0 for FINDINGS as well as CLEAR. This is a REPORTER during
    the parallel-run period". When FINDINGS stops exiting 0, `returncode == 2` no
    longer separates "rejected" from "fired" and this line becomes the
    load-bearing one — at which point excluding only `**CLEAR**` would leave it
    blind to two thirds of the verdict space this very docstring enumerates.
    """
    proc = _cli(flag, value)
    assert proc.returncode == 2
    assert flag in proc.stderr
    assert proc.stdout == ""


def test_a_negative_threshold_is_deliberately_still_allowed():
    """⚠️ The counterpart: the rejection must not over-reach.

    A negative threshold is NOISY, not silent — it fires on nearly everything,
    which is a legitimate way to ask "what would a rule this loose have said".
    What the guards above reject is the parameter that produces a confident,
    EMPTY answer.
    """
    proc = _cli("--threshold-pct", "-3")
    assert proc.returncode == ptw.EXIT_OK
    assert "**FINDINGS**" in proc.stdout


def test_the_pinned_defaults_still_produce_the_anchor_verdict():
    """Positive control for the whole validation block."""
    proc = _cli()
    assert proc.returncode == ptw.EXIT_OK
    assert "**FINDINGS**" in proc.stdout
    assert "`MergePartialConfigs_1000`@08-17" in proc.stdout



def test_a_night_whose_date_looks_like_an_ordinal_is_still_a_night():
    """⛔ Blind review, round 3 of the key scope. My OWN fix introduced this.

    `render()` classified the not-sustained table by `str(k).startswith("?#")`
    — inferring identity from the printed form, one round after the lesson that
    identity cannot be inferred from the run. A night whose `night_utc` is
    literally `"?#7"` is DATED (`_dated()` type-checks; neither loader
    format-checks) and was counted as an un-dated RUN, so the headline and the
    row below it disagreed about the same series. Measured on `0ec854a2`:

        _dated(weird) = True
        **INCONCLUSIVE** over 2 of 2 calendar night(s) … 2 of 2 run(s) counted.
        | `BenchmarkAlpha` | 1 (08-19) + 1 undated run(s) (?#7) | +12.00% |

    ⚠️ Not reachable from the CI loaders today, for the same reason `_dated`
    and `_ordering_key` document their own defence in depth: Nights are also
    built by hand, by tests, and by future adapters.
    """
    weird = _run("?#7", 2, 12.0)
    assert ptw._dated(weird) is True                # premise of the finding
    result = ptw.decide([_run("2026-08-19", 1, 9.0), weird], k=5)
    body = ptw.render(result)
    assert "2 of 2 calendar night(s)" in body
    # ⛔ Two dated nights, zero un-dated runs. The row must agree with that.
    assert "| `BenchmarkAlpha` | 2 (08-19, ?#7) | +12.00% |" in body
    assert "undated run(s)" not in body


def test_a_night_whose_date_looks_like_an_ordinal_does_not_merge_with_one():
    """⛔ The same input, one section deeper — and a DROPPED READING, not just a
    miscount.

    Under the string key both `night_utc == "?#1"` and the first un-dated run
    formatted to the key `"?#1"`, so the two collapsed into one entry in
    `over_not_sustained` and the smaller reading left the page. That is the
    identical silent merge `assign_night_keys` was written to end, re-entering
    through the display alphabet. Structured keys cannot collide.
    """
    result = ptw.decide([_run("?#1", 1, 9.0), _run(None, 2, 12.0)], k=5)
    hits = result["over_not_sustained"]["BenchmarkAlpha"]
    assert sorted(hits.values()) == [9.0, 12.0], hits    # ⛔ neither lost
    assert set(hits) == {(ptw.KEY_DATED, "?#1"), (ptw.KEY_UNDATED, 1)}, hits


def test_undated_runs_are_ordered_as_numbers_not_as_text():
    """The ordinal is a number. Sorting its printed form put `?#10` between
    `?#1` and `?#2`:

        | `BenchmarkAlpha` | 0 (—) + 11 undated run(s) (?#1, ?#10, ?#11, ?#2,
        ?#3, ?#4, ?#5, ?#6, ?#7, ?#8, ?#9) | +9.10% |

    Display only — no reading is lost — but the ordinal's ONLY meaning is
    position in the series, and a page that scrambles it is asserting an order
    nobody measured.
    """
    result = ptw.decide([_run(None, i, 9.0 + i / 100) for i in range(1, 12)],
                        k=99)
    body = ptw.render(result)
    assert ("+ 11 undated run(s) (?#1, ?#2, ?#3, ?#4, ?#5, ?#6, ?#7, ?#8, "
            "?#9, ?#10, ?#11)") in body


def test_the_inconclusive_section_orders_ordinals_the_same_way():
    """The not-sustained table and the inconclusive list read the same keys, and
    only the first one was checked. This sorts by key rather than by text there
    too — the same defect, one section apart.
    """
    nights = [_run(None, i) for i in range(1, 12)]      # no reading at all
    nights[0].ratios_pct["BenchmarkAlpha"] = 9.0        # give the bench a name
    body = ptw.render(ptw.decide(nights, k=99))
    # ⛔ The reasons line, not the `unjudgeable` bullet above it — both start
    # `- \`BenchmarkAlpha\``, and a first cut of this test read the wrong one.
    line = next(ln for ln in body.splitlines()
                if ln.startswith("- `BenchmarkAlpha` — "))
    assert re.findall(r"\?#\d+", line) == [f"?#{i}" for i in range(2, 12)], line


def test_the_page_never_spells_an_ordinal_outside_key_label():
    """`key_label` is the ONLY code in the module that contains `?#` at all.

    ⛔ THE FIRST TWO CUTS OF THIS TEST BOTH CLAIMED MORE THAN THEY CHECKED, and
    the second was caught by blind review rather than by me:

        cut 1   a substring scan of the file. The docstrings recording WHY the
                string key was wrong quote `f"?#{run_id}"` and
                `startswith("?#")` verbatim, so prose counted as live call
                sites (measured 3, not 1) and the test failed on the very
                comment explaining the fix.
        cut 2   an AST scan for two SHAPES — an f-string interpolating after
                `?#`, and a call to `.startswith("?#")`. Review appended two
                functions that reintroduce exactly the defects this test names,
                written differently, and measured it still passing:

                    def _rogue_format(n):    return "?#" + str(n)
                    def _rogue_classify(k):  return "?#" not in str(k)

                ⇒ its docstring said "one place can be wrong about it" while
                the code recognised a list of syntax. Naming two shapes is not
                the same as owning the alphabet — the same over-claim this
                module keeps producing, this time inside its own guard.

    ⇒ this check is stated over the ALPHABET instead: no string literal in the
    module's code, outside `key_label`, may contain `?#`.

    ⛔ AND CUT 3 — this one — OVER-CLAIMED TOO, which is why the wording below
    is this careful. It used to end "Concatenation, `%`, `.format`, `in`, `==`,
    slicing, `startswith` — every one of them needs those two characters
    somewhere, so every one of them is caught." A fifth-round reviewer split
    the literal and measured that sentence false:

            marker = "?" + "#"      # two Constants, neither contains `?#`

        ⇒ invisible here, and `Concatenation` was named as covered.

    ⇒ WHAT THIS CHECK ACTUALLY OWNS: the two characters INSIDE ONE STRING
    LITERAL. A marker assembled from pieces — `"?" + "#"`, `chr(63) + chr(35)`,
    `"".join(...)` — escapes it, and no AST scan closes that without running
    the code. Enumerating one more shape (constant-folding `BinOp`) would just
    be cut 4 of the same mistake, so it is deliberately not done.

    ⇒ WHAT COVERS THE REST: `test_no_second_site_can_put_an_ordinal_on_the_page`
    below neuters `key_label` and asserts `?#` cannot reach the rendered page
    at all. It does not care how a rogue marker is spelled — but it only sees
    code that `render()` actually executes.

    ⚠️ SO NEITHER IS COMPLETE, AND THE HOLES ARE DIFFERENT ONES: this check
    reaches code nothing calls but only single literals; that one reaches any
    spelling but only live paths. Both are stated so a reader can tell which
    half is measured — the pair is a tripwire, not a proof.

    ⚠️ Docstrings are exempt and MUST be: half this module's prose quotes the
    defects it is documenting. Comments never reach the AST at all.

    ⚠️ Which is why `render`'s disclosure paragraph builds its two example
    labels by CALLING `key_label` rather than typing `?#1`, `?#2`.
    """
    tree = ast.parse(Path(ptw.__file__).read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))

    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                owner.setdefault(id(inner), node.name)

    spots = sorted({owner.get(id(n), "<module>") for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and "?#" in n.value and id(n) not in docstrings})
    assert spots == ["key_label"], spots


def test_a_date_shaped_like_an_ordinal_costs_a_label_but_never_a_reading():
    """⛔ Blind review, round 4: the no-collision claim covers KEYS, not LABELS.

    A night whose `night_utc` is `"?#1"` keeps its own key — nothing merges —
    and still PRINTS `?#1`, the same text the first un-dated run prints:

        | `BenchmarkAlpha` | 1 (?#1) + 2 undated run(s) (?#1, ?#2) | +20.00% |

    ⚠️ THIS TEST PINS THE ACCEPTED STATE, not a bug waiting to be fixed. Every
    repair available here decides what a value IS from the shape of its text —
    `key_label` escaping a "date that looks like an ordinal", or `_dated()`
    format-checking `night_utc` — and that inference is the defect the round
    before this one existed to remove. A `night_utc` of `"?#1"` is garbage no
    alphabet disambiguates, and neither production loader can emit one.

    ⇒ what must never regress is the half that IS decidable: no reading lost,
    every count right. That is what is asserted; the ambiguous label is
    asserted as PRESENT so that removing it by inference has to argue with
    this docstring first.
    """
    result = ptw.decide([_run("?#1", 1, 9.0), _run(None, 2, 12.0),
                         _run(None, 3, 20.0)], k=99)
    hits = result["over_not_sustained"]["BenchmarkAlpha"]
    # ⛔ Three distinct keys, three distinct readings — nothing merged.
    assert sorted(hits.values()) == [9.0, 12.0, 20.0], hits
    assert set(hits) == {(ptw.KEY_DATED, "?#1"), (ptw.KEY_UNDATED, 1),
                         (ptw.KEY_UNDATED, 2)}, hits

    body = ptw.render(result)
    assert "1 of 1 calendar night(s)" in body        # one dated night
    assert "3 of 3 run(s) counted" in body           # all three readings kept
    assert "| `BenchmarkAlpha` | 1 (?#1) + 2 undated run(s) (?#1, ?#2) |" in body


def test_no_second_site_can_put_an_ordinal_on_the_page(monkeypatch):
    """The behavioural half of the pair above: spelling-independent, path-bound.

    ⛔ Written because the static scan owns only the two characters inside one
    string literal, and round 5 measured a live way past it (`"?" + "#"`). This
    asks the question the scan cannot: with `key_label` NEUTERED, can `?#`
    still reach the page? If it can, something other than `key_label` put it
    there — and it does not matter whether that something used an f-string,
    concatenation, `%`, `.format`, or `chr(63)`.

    ⚠️ ITS OWN HOLE, stated so the pair is not read as a proof: it only sees
    code `render()` actually executes. A rogue formatter nothing calls is
    invisible HERE and visible to the static scan. That is the whole reason
    both exist; neither is sufficient and they fail in opposite directions.
    """
    nights = [_run("2026-08-20", 1, 9.0), _run(None, 2, 12.0),
              _run(None, 3, 20.0)]

    # ⛔ Positive control FIRST. Without it a scenario that never renders a
    # single ordinal would pass this test by carrying nothing to find.
    assert "?#" in ptw.render(ptw.decide(copy.deepcopy(nights), k=99))

    monkeypatch.setattr(ptw, "key_label", lambda key: "<LBL>")
    body = ptw.render(ptw.decide(copy.deepcopy(nights), k=99))
    assert "<LBL>" in body, "the patch never took effect — vacuous otherwise"
    assert "?#" not in body, body
