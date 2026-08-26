"""Tests for scripts/tools/dx/converge_status.py (TRK-360).

The tool is the observable half of the ``vibe-converge`` protocol: it is the
only thing that turns a ROUNDS.jsonl ledger into a verdict, and nothing in CI
re-derives that verdict, so a silent regression here would be invisible. The
protocol's whole claim is that no rule treats a LOW finding count as a reason
to stop -- CHANGE-SUBJECT and UNREVIEWED-FIX key on the SUBJECT and on fixes
instead, and both get the replay test below, driven by the numbers actually
measured on the #1411 -> #1457 chain. (SURFACE-DEBT is advisory, not a stop
rule.)

Coverage:
  - check_record: unknown kind / non-int round / bad tier / banned
    'speculative' / bad status / verified-without-evidence /
    dead-end-without-evidence / inferred needs no evidence
  - build_rounds: per-kind folding, multi-subject rounds, reviewer set
  - evaluate: EMPTY-LEDGER / ORACLE-MISSING (+ the narrow undecidable
    exemption and its same-round boundary) / ROUND-CAP (both sides of the
    boundary, and its merge with UNREVIEWED-FIX at the cap) / CHANGE-SUBJECT
    (fires, and clears when a later round changes the subject) /
    UNREVIEWED-FIX / LEDGER-GAP / SURFACE-DEBT advisory / SELF-REVIEW-ZERO /
    UNDECIDABLE / the always-printed FINISHED-or-not verdict line
  - find_ledgers: file directly, nested dirs, none
  - parse_ledger encoding: non-UTF-8 line reported not raised, one bad line does
    not blind the rest of the ledger, BOM stripped, CRLF tolerated, unreadable
    path reported
  - surface counters: non-int / negative / bool rejected by check_record and
    degraded (not raised) by Round
  - main CLI: missing scope → 2, no ledger → 2, unparsable → 2, blocking → 1,
    format violation → 1, clean → 0
  - the real #1411 -> #1457 chain replayed: the blocking rule fires at the
    round where the chain historically wrote its third dead predicate
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx",
))
import converge_status as cs  # noqa: E402


# ============================================================
# helpers
# ============================================================


def rec(**kw):
    """A ledger record with the mandatory scaffolding filled in."""
    base = {"ts": "2026-08-20T00:00:00Z", "round": 1, "kind": "finding"}
    base.update(kw)
    return base


def write_ledger(tmp_path, records, subdir=""):
    d = tmp_path / subdir if subdir else tmp_path
    d.mkdir(parents=True, exist_ok=True)
    path = d / cs.LEDGER_NAME
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def rounds_of(records):
    return cs.build_rounds([dict(r, _where="t:1") for r in records])


def subject(round_no, name, ins=0, dels=0, reviewer="blind"):
    return rec(round=round_no, kind="subject", subject=name,
               insertions=ins, deletions=dels, reviewer=reviewer)


def dead_end(round_no, name, claim="v?"):
    return rec(round=round_no, kind="dead-end", subject=name, claim=claim,
               evidence="ran it => measured bypass")


def finding(round_no, tier="verified", status="open", evidence="cmd => output"):
    return rec(round=round_no, kind="finding", tier=tier, status=status,
               claim="c", evidence=evidence)


def oracle(round_no=1, command="pytest tests/x.py -q",
           falsifier="delete the guard body"):
    return rec(round=round_no, kind="oracle", command=command,
               falsifier=falsifier)


# ============================================================
# check_record — the format contract
# ============================================================


def test_unknown_kind_is_a_violation_and_stops_further_checks():
    out = cs.check_record(rec(kind="musing", _where="t:1"))
    assert len(out) == 1
    assert "unknown kind" in out[0]


def test_non_integer_round_is_a_violation():
    out = cs.check_record(rec(round="1", tier="inferred", status="open",
                              _where="t:1"))
    assert any("must be an integer" in m for m in out)


def test_oracle_without_a_command_is_a_violation():
    out = cs.check_record(rec(kind="oracle", falsifier="f", _where="t:1"))
    assert any("'command'" in m for m in out)


def test_oracle_without_a_falsifier_is_a_violation():
    """The falsifier is the half that cannot be filled in retroactively."""
    out = cs.check_record(rec(kind="oracle", command="c", _where="t:1"))
    assert any("'falsifier'" in m for m in out)


def test_oracle_with_whitespace_only_fields_is_a_violation():
    out = cs.check_record(rec(kind="oracle", command="  ", falsifier="\t",
                              _where="t:1"))
    assert len([m for m in out if "oracle must carry" in m]) == 2


def test_a_complete_oracle_passes():
    """Paired with the three above so the check cannot drift to always-reject."""
    assert cs.check_record(rec(kind="oracle", command="make x",
                               falsifier="delete the assert", _where="t:1")) == []


def test_speculative_tier_is_banned_by_name():
    out = cs.check_record(rec(tier="speculative", status="open", _where="t:1"))
    assert any("banned from the ledger" in m for m in out)
    # and it is not ALSO reported as an unknown tier — one defect, one message
    assert not any("needs tier verified|inferred" in m for m in out)


def test_unknown_tier_is_a_violation():
    out = cs.check_record(rec(tier="hunch", status="open", _where="t:1"))
    assert any("needs tier verified|inferred" in m for m in out)


def test_unknown_status_is_a_violation():
    out = cs.check_record(rec(tier="inferred", status="maybe", _where="t:1"))
    assert any("needs status" in m for m in out)


def test_verified_finding_without_evidence_is_a_violation():
    out = cs.check_record(rec(tier="verified", status="open", evidence="  ",
                              _where="t:1"))
    assert any("verified finding must carry non-empty 'evidence'" in m
               for m in out)


def test_inferred_finding_needs_no_evidence():
    out = cs.check_record(rec(tier="inferred", status="open", _where="t:1"))
    assert out == []


def test_dead_end_without_evidence_is_a_violation():
    out = cs.check_record(rec(kind="dead-end", subject="s", _where="t:1"))
    assert any("dead-end must carry non-empty 'evidence'" in m for m in out)


def test_clean_verified_finding_passes():
    assert cs.check_record(rec(tier="verified", status="fixed",
                               evidence="pytest -k x => 1 failed",
                               _where="t:1")) == []


# ============================================================
# _is_valid_count — the one predicate both call sites share
# ============================================================


@pytest.mark.parametrize("value,expected", [
    (0, True), (7, True),
    (-1, False),            # negative
    (True, False),          # bool is an int in Python; must not count as one
    (False, False),
    ("7", False), (7.0, False), (None, False),
])
def test_is_valid_count(value, expected):
    assert cs._is_valid_count(value) is expected


def test_check_record_and_round_count_agree_on_every_case():
    """The drift this predicate exists to prevent: reported bad, still summed."""
    for value in (0, 7, -1, True, False, "7", 7.0, None):
        record = rec(round=1, kind="subject", subject="s", insertions=value,
                     _where="t:1")
        reported = any("insertions" in m for m in cs.check_record(record))
        summed = cs.Round._count(record, "insertions")
        if reported:
            assert summed == 0, f"{value!r} reported as bad but still summed"
        else:
            assert summed == (value or 0)


# ============================================================
# build_rounds — folding
# ============================================================


def test_build_rounds_folds_every_kind():
    rounds = rounds_of([
        subject(1, "s", ins=100, dels=10, reviewer="blind"),
        finding(1, tier="verified", status="fixed"),
        finding(1, tier="inferred", status="open"),
        dead_end(1, "s"),
        rec(round=1, kind="question", status="open", claim="q"),
        rec(round=1, kind="decidability", subject="s", verdict="undecidable",
            note="same shape"),
    ])
    assert len(rounds) == 1
    r = rounds[0]
    assert (r.verified, r.inferred, r.fixed) == (1, 1, 1)
    assert len(r.dead_ends) == 1
    assert r.open_questions == 1
    assert len(r.undecidable) == 1
    assert r.insertions == 100 and r.deletions == 10
    assert r.reviewers == {"blind"}
    assert r.subject_names == ["s"]


def test_build_rounds_sums_multiple_subjects_in_one_round():
    r = rounds_of([subject(1, "a", ins=10, dels=1),
                   subject(1, "b", ins=5, dels=2)])[0]
    assert (r.insertions, r.deletions) == (15, 3)
    assert sorted(r.subject_names) == ["a", "b"]


def test_build_rounds_skips_records_with_a_non_integer_round():
    assert rounds_of([rec(round="oops", kind="subject", subject="s")]) == []


def test_build_rounds_is_ascending_regardless_of_append_order():
    rounds = rounds_of([subject(3, "c"), subject(1, "a"), subject(2, "b")])
    assert [r.number for r in rounds] == [1, 2, 3]


def test_closed_question_is_not_counted_as_open():
    r = rounds_of([subject(1, "s"),
                   rec(round=1, kind="question", status="fixed", claim="q")])[0]
    assert r.open_questions == 0


# ============================================================
# evaluate — the three stop rules
# ============================================================


def test_change_subject_fires_on_two_dead_ends_for_one_subject():
    blocking, _ = cs.evaluate(rounds_of([
        subject(1, "pred"), dead_end(1, "pred"),
        subject(2, "pred"), dead_end(2, "pred"),
    ]))
    assert any("CHANGE-SUBJECT" in m for m in blocking)


def test_change_subject_does_not_fire_across_different_subjects():
    blocking, _ = cs.evaluate(rounds_of([
        subject(1, "pred-a"), dead_end(1, "pred-a"),
        subject(2, "pred-b"), dead_end(2, "pred-b"),
    ]))
    assert not any("CHANGE-SUBJECT" in m for m in blocking)


def test_unreviewed_fix_fires_when_the_last_round_shipped_a_fix():
    blocking, _ = cs.evaluate(rounds_of([
        subject(1, "s"), finding(1, status="fixed"),
    ]))
    assert any("UNREVIEWED-FIX" in m for m in blocking)


def test_unreviewed_fix_clears_once_a_later_round_reviews_the_fix():
    blocking, _ = cs.evaluate(rounds_of([
        subject(1, "s"), finding(1, status="fixed"),
        subject(2, "the fix from round 1"),
    ]))
    assert not any("UNREVIEWED-FIX" in m for m in blocking)


def test_ledger_gap_fires_when_a_round_number_is_missing():
    blocking, _ = cs.evaluate(rounds_of([subject(1, "s"), subject(3, "s")]))
    assert any("LEDGER-GAP" in m for m in blocking)


def test_ledger_opened_mid_chain_is_not_a_gap():
    """Rounds 5-6 of a chain whose earlier rounds predate the ledger are legal.

    LEDGER-GAP checks contiguity, NOT that numbering starts at 1 -- the tool
    docstring says so, and nothing in the file separates "opened mid-chain" from
    "lost the first four rounds". Pinned so a later tightening of `expected` in
    evaluate() has to be a deliberate, reviewed change rather than a silent one.
    """
    blocking, _ = cs.evaluate(rounds_of([subject(5, "s"), subject(6, "s")]))
    assert not any("LEDGER-GAP" in m for m in blocking)


# The five CONVERGED tests are deleted, not ported (four stood here, the
# fifth was in the CLI section). Three of
# them asserted CONVERGED was ABSENT, so removing the rule left them green and
# vacuous -- the exact shape this repo keeps burning on: an absence assertion is
# satisfied by deleting the thing it watches. Deleting a rule means deleting the
# tests that only said when it stays quiet.


def test_a_chain_with_no_oracle_blocks():
    """The production change this catches: dropping Rule 0 from evaluate()."""
    blocking, _ = cs.evaluate(rounds_of([subject(1, "s"), subject(2, "s")]))
    assert any("ORACLE-MISSING" in m for m in blocking)


def test_one_oracle_anywhere_in_the_chain_satisfies_rule_zero():
    """Declared once, not once per round -- and a later round still counts."""
    blocking, _ = cs.evaluate(rounds_of([
        subject(1, "s"),
        subject(2, "s"), oracle(2),
    ]))
    assert not any("ORACLE-MISSING" in m for m in blocking)


def test_the_cap_is_five():
    """Pinned, not derived.

    The boundary tests below build their fixtures from literal round numbers on
    purpose: a fixture built from cs.ROUND_CAP moves with the constant, so
    raising the cap would leave every boundary test green. Measured -- that was
    the first version of these tests, and setting ROUND_CAP = 500 survived the
    whole file. This pin is what makes changing the cap a reviewed act.
    """
    assert cs.ROUND_CAP == 5


def test_round_cap_blocks_past_the_cap_and_stays_quiet_at_it():
    """Both sides: at 5 rounds it must NOT fire, at 6 it must.

    A one-sided version would pass an implementation that blocks every chain.
    """
    at_cap = [oracle(1)] + [subject(n, "s") for n in range(1, 6)]
    blocking, _ = cs.evaluate(rounds_of(at_cap))
    assert not any("ROUND-CAP" in m for m in blocking)

    past = at_cap + [subject(6, "s")]
    blocking, _ = cs.evaluate(rounds_of(past))
    assert any("ROUND-CAP" in m for m in blocking)


def test_round_cap_counts_the_span_not_the_record_count():
    """A ledger opened mid-chain (rounds 5..10) is 6 rounds, not 10."""
    mid = [oracle(5)] + [subject(n, "s") for n in range(5, 11)]
    blocking, _ = cs.evaluate(rounds_of(mid))
    assert any("ROUND-CAP" in m and "(6 rounds)" in m for m in blocking)


def test_the_cap_boundary_with_a_pending_fix_has_a_reachable_state():
    """Fails if ROUND-CAP stops subsuming UNREVIEWED-FIX at the boundary.

    Measured before the fix: a 5-round chain that honestly recorded a fix got
    UNREVIEWED-FIX ("open a later round"); opening round 6 got ROUND-CAP. Both
    rc=1, no legal move, and the only rc=0 exit was relabelling the fixed
    finding as still open -- a lie aimed at the rule meant to stop fixes
    escaping review.
    """
    at_cap_with_fix = ([oracle(1)] + [subject(n, "s") for n in range(1, 6)]
                       + [finding(5, status="fixed")])
    blocking, _ = cs.evaluate(rounds_of(at_cap_with_fix))
    assert any("ROUND-CAP" in m for m in blocking)
    # exactly one verdict, and it must name the pending fix rather than
    # instructing an action the other rule forbids
    assert not any("UNREVIEWED-FIX" in m for m in blocking)
    assert any("unreviewed fix" in m for m in blocking)


def test_round_cap_is_silent_at_a_five_round_span_opened_mid_chain():
    """Rounds 5..9 is a span of 5 -- exactly the cap, and legal.

    Deliberately sits ON the boundary. An earlier version of this test used
    5..8 (span 4), which moved it off the boundary for no reason and gave up
    the coverage.
    """
    mid = [oracle(5)] + [subject(n, "s") for n in range(5, 10)]
    blocking, _ = cs.evaluate(rounds_of(mid))
    assert not any("ROUND-CAP" in m for m in blocking)


def test_the_report_always_states_whether_the_chain_is_finished(tmp_path, capsys):
    """A quiet run and a finished chain must not look the same.

    Measured before this line existed: a blind round reporting zero findings
    produced a silent report and exit 0 -- byte-identical to a chain that had
    actually passed its oracle. That is the substitution CONVERGED was deleted
    for, one level up: silence carrying the meaning "done".
    """
    write_ledger(tmp_path, [oracle(1), subject(1, "s")], subdir="quiet")
    cs.main(["--scope", str(tmp_path)])
    assert "VERDICT: NOT FINISHED" in capsys.readouterr().out

    write_ledger(tmp_path, [
        oracle(1), subject(1, "s"),
        rec(round=1, kind="oracle-result", passed=True,
            evidence="py -m pytest tests/dx/test_x.py => 12 passed, rc=0"),
    ], subdir="done")
    cs.main(["--scope", str(tmp_path) + os.sep + "done"])
    out = capsys.readouterr().out
    assert "VERDICT: FINISHED" in out
    assert "12 passed" in out


def test_an_oracle_result_needs_a_real_boolean_and_evidence():
    """`passed: "yes"` is a string; it must not read as a pass."""
    out = cs.check_record(rec(kind="oracle-result", passed="yes", _where="t:1"))
    assert any("'passed' as a real boolean" in m for m in out)
    assert any("non-empty 'evidence'" in m for m in out)
    assert cs.check_record(rec(kind="oracle-result", passed=False,
                               evidence="cmd => rc=1", _where="t:1")) == []


def test_a_failed_oracle_result_does_not_read_as_finished(tmp_path, capsys):
    """Paired with the above so FINISHED cannot drift to 'any result present'."""
    write_ledger(tmp_path, [
        oracle(1), subject(1, "s"),
        rec(round=1, kind="oracle-result", passed=False,
            evidence="py -m pytest ... => 1 failed, rc=1"),
    ])
    cs.main(["--scope", str(tmp_path)])
    assert "VERDICT: NOT FINISHED" in capsys.readouterr().out


def test_a_bare_undecidable_record_is_a_violation():
    """The exemption is load-bearing, so the record cannot be a one-liner.

    Measured before this check: {"kind":"decidability","round":1,
    "verdict":"undecidable"} -- no subject, question, evidence or note --
    returned 0 for the entire run while the report printed "no stated terminal
    condition". Cheaper than the fabricated oracle it was meant to replace.
    """
    out = cs.check_record(rec(kind="decidability", verdict="undecidable",
                              _where="t:1"))
    for field in ("subject", "question", "evidence_set", "note"):
        assert any(f"'{field}'" in m for m in out), field


def test_a_decidable_verdict_needs_none_of_those_fields():
    """Paired with the above so the check cannot drift to always-reject."""
    assert cs.check_record(rec(kind="decidability", verdict="decidable",
                               _where="t:1")) == []


def test_the_undecidable_exemption_needs_the_chain_to_have_stopped():
    """Fails if the exemption goes back to being chain-wide.

    Measured: one undecidable record about an unrelated subject in round 1 let
    five further rounds run with an open finding and no oracle at all, exit 0 --
    the cheapest green in the tool, and one that needs no lie because a claim of
    undecidability is not falsifiable offline.
    """
    undec = rec(round=1, kind="decidability", subject="a side question",
                question="q", evidence_set="e", verdict="undecidable",
                note="isomorphic")
    still_running = [undec] + [subject(n, "the real work") for n in range(1, 6)]
    blocking, _ = cs.evaluate(rounds_of(still_running))
    assert any("ORACLE-MISSING" in m for m in blocking)

    stopped = [subject(1, "the real work"), dict(undec, round=2)]
    blocking, _ = cs.evaluate(rounds_of(stopped))
    assert not any("ORACLE-MISSING" in m for m in blocking)


def test_a_subject_in_the_same_round_as_the_verdict_is_still_working():
    """The boundary: `>=`, not `>`.

    Measured with the strict comparison, and missed by the test above because
    its fixture put the verdict in a round with no subject: moving the
    undecidable line to the LAST round restored the entire bypass -- five
    rounds, an open verified finding, no oracle, exit 0. A chain that declared
    a subject in the same round it claimed to be unable to decide was still
    working when it said it had stopped.
    """
    undec = rec(round=5, kind="decidability", subject="a side question",
                question="q", evidence_set="e", verdict="undecidable",
                note="isomorphic")
    same_round = [subject(n, "the real work") for n in range(1, 6)] + [undec]
    blocking, _ = cs.evaluate(rounds_of(same_round))
    assert any("ORACLE-MISSING" in m for m in blocking)


def test_the_report_names_the_undecidable_verdict_as_the_terminal_condition(
        tmp_path, capsys):
    """Report and exit code must not contradict each other.

    Before: a chain exempted by an undecidable verdict exited 0 while the
    report said "no stated terminal condition".
    """
    write_ledger(tmp_path, [
        subject(1, "s"),
        rec(round=2, kind="decidability", subject="the exemption",
            question="q", evidence_set="e", verdict="undecidable", note="iso"),
    ])
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_OK
    out = capsys.readouterr().out
    assert "NONE DECLARED" not in out
    assert "UNDECIDABLE verdict on the exemption" in out


def test_an_undecidable_verdict_is_a_terminal_state_not_a_missing_oracle():
    """Fails if the undecidable exemption is removed from Rule 0.

    Section 0 says a subject whose legal and defective cases are isomorphic
    must be dropped, not given a predicate. Demanding an oracle there asked for
    one to be invented for a question the same ledger had certified unanswerable.
    """
    blocking, advisory = cs.evaluate(rounds_of([
        rec(round=1, kind="decidability", subject="exemption",
            question="is this exemption legitimate?",
            evidence_set="the tree after the exemption was added",
            verdict="undecidable", note="legal and defective are isomorphic"),
    ]))
    assert not any("ORACLE-MISSING" in m for m in blocking)
    assert any("UNDECIDABLE" in m for m in advisory)


def test_an_oracle_never_manufactures_a_round():
    """Fails if build_rounds goes back to materialising oracle records.

    The skill's template carries `"round":1`. On a ledger whose work starts at
    round 5 that used to create a phantom Round(1), which tripped LEDGER-GAP
    and inflated the ROUND-CAP span to 7.
    """
    mid = [oracle(1)] + [subject(n, "s") for n in (5, 6, 7)]
    rounds = rounds_of(mid)
    assert [r.number for r in rounds] == [5, 6, 7]
    blocking, _ = cs.evaluate(rounds)
    assert blocking == []


def test_an_oracle_needs_no_round_at_all():
    """It is a property of the chain, written before any round happens."""
    rec_no_round = rec(kind="oracle", command="pytest x", falsifier="delete y")
    rec_no_round.pop("round")
    assert cs.check_record(dict(rec_no_round, _where="t:1")) == []
    blocking, _ = cs.evaluate(rounds_of([rec_no_round, subject(1, "s")]))
    assert not any("ORACLE-MISSING" in m for m in blocking)


def test_a_bare_question_no_longer_silences_unreviewed_fix():
    """The rule's message always said "no later round declares a subject".

    Keying on "is the last Round object" let one `question` record silence it
    while the report printed `(no subject declared)` for that round.
    """
    blocking, _ = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "s"),
        subject(2, "s"), finding(2, status="fixed"),
        rec(round=3, kind="question", status="open", claim="q",
            evidence="what would close it"),
    ]))
    assert any("UNREVIEWED-FIX" in m for m in blocking)


def test_unreviewed_fix_names_the_round_that_actually_holds_the_fix():
    """Fails if the rule picks the FIRST round with a fix instead of the last.

    Found by mutation: `fixed_pre[-1]` -> `fixed_pre[0]` survived the whole
    file, and the message it produced said "round 2 ... and no later round
    declares a subject" on a ledger where round 3 declares one.
    """
    blocking, _ = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "s"),
        subject(2, "s"), finding(2, status="fixed"),
        subject(3, "the fix from round 2"),
        subject(4, "s"), finding(4, status="fixed"),
    ]))
    msg = [m for m in blocking if "UNREVIEWED-FIX" in m]
    assert len(msg) == 1
    assert "round 4" in msg[0]
    assert "round 2" not in msg[0]


def test_advisory_messages_name_their_ledger_too(tmp_path, capsys):
    """Found by mutation: the path prefix was only tested on blocking."""
    write_ledger(tmp_path, [oracle(1), subject(1, "s", reviewer="self")],
                 subdir="only")
    cs.main(["--scope", str(tmp_path)])
    err = capsys.readouterr().err
    assert "SELF-REVIEW-ZERO" in err
    assert "only" + os.sep + cs.LEDGER_NAME in err


def test_an_oracle_round_that_is_present_but_not_an_integer_is_a_violation():
    """Found by mutation: deleting the oracle round check survived.

    `round` is optional on an oracle, but a present-and-wrong one would be
    silently ignored, and the report would then print no round where the
    author thought they had given one.
    """
    out = cs.check_record(rec(kind="oracle", round="1", command="c",
                              falsifier="f", _where="t:1"))
    assert any("oracle 'round' is optional" in m for m in out)


def test_a_later_round_with_a_subject_does_clear_unreviewed_fix():
    """Paired with the above so the rule cannot drift to always-fire."""
    blocking, _ = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "s"),
        subject(2, "s"), finding(2, status="fixed"),
        subject(3, "the fix from round 2"),
    ]))
    assert not any("UNREVIEWED-FIX" in m for m in blocking)


def test_a_json_null_oracle_field_is_a_violation():
    """`str(None).strip()` is "None" -- non-empty, so null used to pass.

    null is exactly what a serializer emits for a field nobody filled in, and
    the report printed the word "None" as the command.
    """
    out = cs.check_record(rec(kind="oracle", command=None, falsifier=None,
                              _where="t:1"))
    assert len([m for m in out if "oracle must carry" in m]) == 2


def test_a_non_string_oracle_field_is_a_violation():
    """A command is text you can paste, not a number or a list.

    Found by mutation: an implementation that only maps null to "" passes a
    numeric command, and `str(42)` is non-empty so the required check would
    have nothing to say about it.
    """
    out = cs.check_record(rec(kind="oracle", command=42, falsifier=["a"],
                              _where="t:1"))
    assert len([m for m in out if "oracle must carry" in m]) == 2


def test_an_empty_ledger_blocks_rather_than_passing():
    """Was: "an empty ledger says nothing" -- that pinned the defect.

    Silence on an empty ledger made truncating the file the cheapest way to
    satisfy every other rule, since an honest one-round ledger with no oracle
    exited 1 while a 0-byte one exited 0. Measured before the fix:
    empty => rc 0, honest => rc 1.
    """
    blocking, _ = cs.evaluate([])
    assert any("EMPTY-LEDGER" in m for m in blocking)


def test_an_oracle_with_no_rounds_is_still_empty():
    """Declaring the oracle and then never recording a round is not a chain."""
    blocking, _ = cs.evaluate(rounds_of([oracle(1)]))
    assert any("EMPTY-LEDGER" in m for m in blocking)
    assert not any("ORACLE-MISSING" in m for m in blocking)


# ============================================================
# evaluate — advisories
# ============================================================


def test_surface_debt_fires_above_both_thresholds():
    _, advisory = cs.evaluate(rounds_of([subject(1, "s", ins=2882, dels=67)]))
    assert any("SURFACE-DEBT" in m and "43:1" in m for m in advisory)


def test_surface_debt_is_silent_below_the_insertions_floor():
    """A small lopsided round is noise, not debt."""
    _, advisory = cs.evaluate(rounds_of([subject(1, "s", ins=50, dels=0)]))
    assert not any("SURFACE-DEBT" in m for m in advisory)


def test_surface_debt_is_silent_when_deletions_keep_the_ratio_down():
    _, advisory = cs.evaluate(rounds_of([subject(1, "s", ins=1000, dels=200)]))
    assert not any("SURFACE-DEBT" in m for m in advisory)


def test_surface_debt_never_blocks():
    blocking, advisory = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "s", ins=2882, dels=67),
        subject(2, "s", ins=1018, dels=19),
    ]))
    assert any("SURFACE-DEBT" in m for m in advisory)
    assert blocking == []


def test_self_review_zero_fires_only_for_a_self_reviewed_quiet_round():
    _, advisory = cs.evaluate(rounds_of([subject(1, "s", reviewer="self")]))
    assert any("SELF-REVIEW-ZERO" in m for m in advisory)


def test_self_review_zero_is_silent_when_the_self_pass_found_something():
    _, advisory = cs.evaluate(rounds_of([
        subject(1, "s", reviewer="self"), finding(1),
    ]))
    assert not any("SELF-REVIEW-ZERO" in m for m in advisory)


def test_self_review_zero_is_silent_when_a_blind_pass_also_ran():
    _, advisory = cs.evaluate(rounds_of([
        subject(1, "s", reviewer="self"), subject(1, "s", reviewer="blind"),
    ]))
    assert not any("SELF-REVIEW-ZERO" in m for m in advisory)


def test_undecidable_verdict_is_surfaced():
    _, advisory = cs.evaluate(rounds_of([
        subject(1, "s"),
        rec(round=1, kind="decidability", subject="s", verdict="undecidable",
            note="legal and defective states are identical"),
    ]))
    assert any("UNDECIDABLE" in m and "identical" in m for m in advisory)


def test_decidable_verdict_is_not_surfaced():
    _, advisory = cs.evaluate(rounds_of([
        subject(1, "s"),
        rec(round=1, kind="decidability", subject="s", verdict="decidable"),
    ]))
    assert not any("UNDECIDABLE" in m for m in advisory)


# ============================================================
# find_ledgers
# ============================================================


def test_find_ledgers_accepts_the_file_itself(tmp_path):
    path = write_ledger(tmp_path, [subject(1, "s")])
    assert cs.find_ledgers(str(path)) == [str(path)]


def test_find_ledgers_walks_nested_scopes(tmp_path):
    write_ledger(tmp_path, [subject(1, "s")], subdir="verify1")
    write_ledger(tmp_path, [subject(1, "s")], subdir="verify2")
    assert len(cs.find_ledgers(str(tmp_path))) == 2


def test_find_ledgers_returns_empty_when_there_is_none(tmp_path):
    assert cs.find_ledgers(str(tmp_path)) == []


# ============================================================
# parse_ledger
# ============================================================


def test_parse_ledger_separates_unreadable_from_violating(tmp_path):
    path = tmp_path / cs.LEDGER_NAME
    path.write_text(
        json.dumps(subject(1, "s")) + "\n"
        + "not json at all\n"
        + "\n"
        + json.dumps(finding(1, evidence="")) + "\n",
        encoding="utf-8")
    records, unparsable, violations = cs.parse_ledger(str(path))
    assert len(records) == 2          # the blank line is skipped, not an error
    assert len(unparsable) == 1 and "not JSON" in unparsable[0]
    assert len(violations) == 1 and "evidence" in violations[0]


def test_parse_ledger_rejects_a_bare_json_scalar(tmp_path):
    path = tmp_path / cs.LEDGER_NAME
    path.write_text('"just a string"\n', encoding="utf-8")
    _records, unparsable, _violations = cs.parse_ledger(str(path))
    assert len(unparsable) == 1 and "not a JSON object" in unparsable[0]


# ============================================================
# parse_ledger — encoding (the ledger is written by shells on any host)
# ============================================================


def test_non_utf8_line_is_reported_not_raised(tmp_path):
    """A Windows shell writes the local codepage; #1372 / #1363 burned on this."""
    path = tmp_path / cs.LEDGER_NAME
    path.write_bytes(
        json.dumps(subject(1, "\u95be\u503c\u5b88\u885b"),
                   ensure_ascii=False).encode("big5"))
    _records, unparsable, _violations = cs.parse_ledger(str(path))
    assert len(unparsable) == 1
    assert "not UTF-8" in unparsable[0]


def test_one_non_utf8_line_does_not_blind_the_rest_of_the_ledger(tmp_path):
    """Per-line decoding is the whole point — 1 bad line, not 20 lost rounds."""
    path = tmp_path / cs.LEDGER_NAME
    good = json.dumps(subject(2, "later round")).encode("utf-8")
    bad = json.dumps(subject(1, "\u95be\u503c"), ensure_ascii=False).encode("big5")
    path.write_bytes(bad + b"\n" + good + b"\n")
    records, unparsable, _violations = cs.parse_ledger(str(path))
    assert len(unparsable) == 1
    assert [r["subject"] for r in records] == ["later round"]


def test_leading_bom_is_stripped_not_reported(tmp_path):
    """PowerShell Out-File writes a BOM; that is not the writer's visible doing."""
    path = tmp_path / cs.LEDGER_NAME
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(subject(1, "s")).encode("utf-8"))
    records, unparsable, violations = cs.parse_ledger(str(path))
    assert unparsable == [] and violations == []
    assert len(records) == 1


def test_crlf_line_endings_are_tolerated(tmp_path):
    path = tmp_path / cs.LEDGER_NAME
    path.write_bytes(json.dumps(subject(1, "s")).encode("utf-8") + b"\r\n")
    records, unparsable, _violations = cs.parse_ledger(str(path))
    assert unparsable == [] and len(records) == 1


def test_unreadable_path_is_reported_not_raised(tmp_path):
    """A directory where the ledger should be — OSError must not escape."""
    (tmp_path / cs.LEDGER_NAME).mkdir()
    records, unparsable, violations = cs.parse_ledger(str(tmp_path / cs.LEDGER_NAME))
    assert records == [] and violations == []
    assert len(unparsable) == 1 and "cannot read" in unparsable[0]


def test_main_exits_caller_error_on_a_non_utf8_ledger(tmp_path, capsys):
    path = tmp_path / cs.LEDGER_NAME
    path.write_bytes(json.dumps(subject(1, "\u95be\u503c"),
                                ensure_ascii=False).encode("big5"))
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_CALLER_ERROR
    assert "not UTF-8" in capsys.readouterr().err


# ============================================================
# surface counters — reported by check_record, survived by Round
# ============================================================


def test_non_integer_insertions_is_a_violation():
    out = cs.check_record(subject(1, "s") | {"insertions": "lots", "_where": "t:1"})
    assert any("'insertions' must be an integer" in m for m in out)


def test_negative_deletions_is_a_violation():
    out = cs.check_record(subject(1, "s") | {"deletions": -3, "_where": "t:1"})
    assert any("'deletions' must not be negative" in m for m in out)


def test_boolean_insertions_is_a_violation():
    """bool is an int subclass — `isinstance(True, int)` is True, so this needs
    its own branch or `insertions: true` silently counts as 1."""
    out = cs.check_record(subject(1, "s") | {"insertions": True, "_where": "t:1"})
    assert any("'insertions' must be an integer" in m for m in out)


def test_absent_surface_counters_are_legal():
    rec_no_counts = {"ts": "t", "round": 1, "kind": "subject", "subject": "s",
                     "_where": "t:1"}
    assert cs.check_record(rec_no_counts) == []


def test_round_degrades_a_bad_counter_instead_of_raising():
    """One typo must not take the other rounds down with it."""
    r = rounds_of([subject(1, "s") | {"insertions": "lots", "deletions": 4}])[0]
    assert (r.insertions, r.deletions) == (0, 4)


def test_main_reports_a_bad_counter_as_a_violation_without_crashing(tmp_path,
                                                                    capsys):
    write_ledger(tmp_path, [subject(1, "s") | {"insertions": "lots"}])
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_VIOLATION
    assert "'insertions' must be an integer" in capsys.readouterr().err


# ============================================================
# main — exit-code contract
# ============================================================


def test_main_exits_caller_error_when_scope_is_absent(tmp_path, capsys):
    assert cs.main(["--scope", str(tmp_path / "nope")]) == cs.EXIT_CALLER_ERROR
    assert "scope not found" in capsys.readouterr().err


def test_main_exits_caller_error_when_no_ledger_exists(tmp_path, capsys):
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_CALLER_ERROR
    assert cs.LEDGER_NAME in capsys.readouterr().err


def test_main_exits_caller_error_on_an_unreadable_line(tmp_path):
    (tmp_path / cs.LEDGER_NAME).write_text("{oops\n", encoding="utf-8")
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_CALLER_ERROR


def test_main_exits_violation_on_a_blocking_rule(tmp_path, capsys):
    write_ledger(tmp_path, [subject(1, "s"), finding(1, status="fixed")])
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_VIOLATION
    assert "UNREVIEWED-FIX" in capsys.readouterr().err


def test_main_exits_violation_on_a_format_break_alone(tmp_path, capsys):
    """No stop rule fires here — the ledger shape alone is user-actionable."""
    write_ledger(tmp_path, [subject(1, "s"), subject(2, "s"),
                            finding(2, status="rejected", evidence="")])
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_VIOLATION
    assert "FORMAT" in capsys.readouterr().err


def test_main_exits_ok_on_a_chain_that_declared_an_oracle(tmp_path, capsys):
    write_ledger(tmp_path, [
        oracle(1, command="pytest tests/ops/test_x.py -q",
               falsifier="revert the width flag"),
        subject(1, "s"), finding(1, status="rejected"),
        subject(2, "s"),
        subject(3, "s"),
    ])
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_OK
    captured = capsys.readouterr()
    assert "round 3" in captured.out
    # the oracle prints unconditionally, on a clean chain too
    assert "pytest tests/ops/test_x.py -q" in captured.out
    assert "revert the width flag" in captured.out


def test_the_report_never_invents_a_round_for_a_round_less_oracle(tmp_path, capsys):
    """Fails if the label goes back to the Round object's synthetic number.

    An oracle carries no round; filing it under a holder and printing that
    holder's number put back the phantom "round 0" the chain never had.
    """
    o = oracle(command="pytest x", falsifier="delete y")
    o.pop("round")
    write_ledger(tmp_path, [o])
    cs.main(["--scope", str(tmp_path)])
    out = capsys.readouterr().out
    assert "oracle: pytest x" in out
    assert "round 0" not in out


def test_report_says_so_when_no_oracle_was_declared(tmp_path, capsys):
    """Absence has to be visible in the report, not only in the verdict."""
    write_ledger(tmp_path, [subject(1, "s")])
    cs.main(["--scope", str(tmp_path)])
    assert "NONE DECLARED" in capsys.readouterr().out


def test_main_requires_scope():
    with pytest.raises(SystemExit) as excinfo:
        cs.main([])
    assert excinfo.value.code == 2


def test_main_reports_every_round_on_stdout(tmp_path, capsys):
    write_ledger(tmp_path, [subject(1, "alpha", ins=10, dels=1),
                            subject(2, "beta", ins=20, dels=2)])
    cs.main(["--scope", str(tmp_path)])
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "+10/-1" in out and "+20/-2" in out


def test_main_blocks_on_a_ledger_with_no_rounds(tmp_path, capsys):
    (tmp_path / cs.LEDGER_NAME).write_text("", encoding="utf-8")
    assert cs.main(["--scope", str(tmp_path)]) == cs.EXIT_VIOLATION
    captured = capsys.readouterr()
    assert "no round records" in captured.out
    assert "EMPTY-LEDGER" in captured.err


def test_every_blocking_message_names_its_ledger(tmp_path, capsys):
    """Two ledgers, one verdict each -- the reader must be able to tell which.

    FORMAT violations always carried path:lineno; blocking and advisory did
    not, so a scope with several ledgers produced byte-identical messages.
    """
    write_ledger(tmp_path, [subject(1, "s")], subdir="a")
    write_ledger(tmp_path, [subject(1, "s")], subdir="b")
    cs.main(["--scope", str(tmp_path)])
    err = capsys.readouterr().err
    assert err.count("ORACLE-MISSING") == 2
    assert "a" + os.sep + cs.LEDGER_NAME in err
    assert "b" + os.sep + cs.LEDGER_NAME in err


# ============================================================
# replay of the real chain
# ============================================================


def test_the_real_1443_chain_blocks_before_its_third_dead_predicate():
    """#1411 -> #1415 -> #1434 -> #1442 -> #1443 -> #1457, as it happened.

    Two predicates had already been measured dead when the third was written.
    The protocol's claim is that this is the point a chain must change its
    subject rather than write version N+1; the assertion below is that the rule
    fires there, on real numbers, and not one round later.
    """
    ledger = [
        subject(1, "_DEFAULTS_ROOTS_MAY_BE_EMPTY predicate", ins=2882, dels=67),
        dead_end(1, "_DEFAULTS_ROOTS_MAY_BE_EMPTY predicate", claim="v1 name:"),
        subject(2, "_DEFAULTS_ROOTS_MAY_BE_EMPTY predicate", ins=788, dels=41),
        dead_end(2, "_DEFAULTS_ROOTS_MAY_BE_EMPTY predicate",
                 claim="v2 all-numeric"),
    ]
    blocking, _ = cs.evaluate(rounds_of(ledger))
    assert any("CHANGE-SUBJECT" in m for m in blocking), (
        "the rule must fire on the two measured dead ends, i.e. BEFORE v3 is "
        "written, not after it also dies")


def test_changing_the_subject_is_what_clears_the_chain():
    """#1457 changed the subject (schema conformance) instead of the predicate.

    The new subject carries no dead end, so the ban lifts for it — but the old
    subject's ban is history and stays recorded.
    """
    blocking, advisory = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "pred"), dead_end(1, "pred"),
        subject(2, "pred"), dead_end(2, "pred"),
        subject(3, "schema conformance of the defaults doc"),
        subject(4, "the fix from round 3"),
    ]))
    # the graveyard entry survives -- as a note, not as a permanent red
    assert [m for m in advisory if "CHANGE-SUBJECT cleared" in m and "'pred'" in m]
    assert not any("CHANGE-SUBJECT" in m for m in blocking)
    assert not any("schema conformance" in m for m in blocking + advisory)


def test_change_subject_stays_blocking_until_the_subject_actually_changes():
    """The other side: doing nothing must NOT clear it.

    Measured before the fix: doing exactly what the message asks -- record the
    second dead end, then declare a different subject -- left the identical
    blocking message in place forever, and the ledger is append-only so the
    dead end could not be withdrawn. That turned the one act the protocol calls
    more valuable than a finding into a one-way door out of reporting done.
    """
    blocking, _ = cs.evaluate(rounds_of([
        oracle(1),
        subject(1, "pred"), dead_end(1, "pred"),
        subject(2, "pred"), dead_end(2, "pred"),
        subject(3, "pred"),
    ]))
    assert any("CHANGE-SUBJECT" in m and "'pred'" in m for m in blocking)
