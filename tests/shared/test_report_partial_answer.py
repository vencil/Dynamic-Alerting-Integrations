"""#3617: a caller error carries a document only as a partial answer.

`_lib_exitcodes.Report` with the caller-error status needs ``unanswered`` —
the parts the run could not answer — and ``unanswered`` goes with that
status only. An early exit (nothing answered) ends with ``CallerError`` /
``die_caller_error``: stdout empty. `test_json_stdout_contract.py` drives
every --json tool's paths against it; these pin the constructor itself.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "tools"))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, Report  # noqa: E402


def test_a_caller_error_without_unanswered_is_refused():
    with pytest.raises(TypeError, match="unanswered"):
        Report({"status": "caller_error", "reason": "x"}, EXIT_CALLER_ERROR)


def test_a_partial_answer_names_what_it_did_not_answer():
    r = Report({"checks": [1]}, EXIT_CALLER_ERROR, unanswered=["kubectl: rules"])
    assert (r.status, r.unanswered) == (EXIT_CALLER_ERROR, ("kubectl: rules",))


@pytest.mark.parametrize("status", [EXIT_OK, EXIT_VIOLATION])
def test_unanswered_goes_with_the_caller_error_status_only(status):
    with pytest.raises(TypeError, match="caller-error status only"):
        Report({}, status, unanswered=["x"])


@pytest.mark.parametrize("parts", [[""], [None], [3]])
def test_each_unanswered_part_is_named(parts):
    with pytest.raises(TypeError, match="non-empty str"):
        Report({}, EXIT_CALLER_ERROR, unanswered=parts)


@pytest.mark.parametrize("status", [EXIT_OK, EXIT_VIOLATION])
def test_other_statuses_are_unchanged(status):
    assert Report({"n": 1}, status).unanswered == ()
    assert Report({"n": 1}, status, unanswered=[]).unanswered == ()


# ── partial_answer: the run's parts decide which of the three it is ──────

from _lib_exitcodes import CallerError, partial_answer  # noqa: E402


def test_partial_answer_passes_other_statuses_through():
    r = partial_answer({"n": 1}, EXIT_VIOLATION, unanswered=[], of=3)
    assert (r.status, r.unanswered) == (EXIT_VIOLATION, ())


def test_some_parts_answered_is_a_partial_answer():
    r = partial_answer({"n": 1}, EXIT_CALLER_ERROR, unanswered=["a: down"], of=3)
    assert (r.status, r.unanswered) == (EXIT_CALLER_ERROR, ("a: down",))


def test_no_part_answered_is_no_answer():
    """Every part unanswered: no document, rc 2, every part named."""
    with pytest.raises(CallerError) as exc:
        partial_answer({"n": 1}, EXIT_CALLER_ERROR, unanswered=["a: down", "b: down"], of=2)
    assert "a: down" in str(exc.value) and "b: down" in str(exc.value)


def test_the_caller_error_status_needs_a_part_named():
    with pytest.raises(TypeError):
        partial_answer({"n": 1}, EXIT_CALLER_ERROR, unanswered=[], of=2)
