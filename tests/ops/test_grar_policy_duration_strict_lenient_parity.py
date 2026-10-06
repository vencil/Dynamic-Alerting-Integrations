"""#2490 (vibe-converge dev/2490-pr6c): the domain-policy timing check reaches
the SAME conclusion with and without ``--strict`` — only the level differs
(strict: ``ERROR`` + fix hint, blocking; lenient: ``WARN``, exit code
unchanged).

Before, the lenient path kept a comparison of its own (single-unit parser
for the policy bound, truthiness skips), so the blind review's inputs below
came out differently in the two modes: ``1.5s`` / ``1h30m`` bounds unread,
``0s`` / ``"0"`` / ``0`` skipped as falsy, and so on. Both modes now call one
comparison (``_grar_validate._timing_bound_violations``).
"""
from __future__ import annotations

import re

import pytest

from _grar_validate import check_domain_policies  # noqa: E402

CASES = [
    # (constraint, bound, tenant field, tenant value, expected phrase or None)
    ("max_repeat_interval", "1.5s", "repeat_interval", "1500ms", None),
    ("max_repeat_interval", "1.99s", "repeat_interval", "1s500ms", None),
    ("max_repeat_interval", "0.5s", "repeat_interval", "1h", "exceeds max"),
    ("max_repeat_interval", "1h30m", "repeat_interval", "2h", "exceeds max"),
    ("min_group_wait", "1m30s", "group_wait", "10s", "below minimum"),
    ("min_group_wait", "30s", "group_wait", "0s", "below minimum"),
    ("min_group_wait", "30s", "group_wait", "0", "below minimum"),
    ("min_group_wait", "30s", "group_wait", 0, "below minimum"),
    ("max_repeat_interval", "1.5h", "repeat_interval", "1h30m", None),
    ("max_repeat_interval", "1.5h", "repeat_interval", "1h31m", "exceeds max"),
    ("max_repeat_interval", "1d", "repeat_interval", "1d1h", "exceeds max"),
    ("max_repeat_interval", "1d", "repeat_interval", 90000, "not a valid duration"),
    ("max_repeat_interval", "1d", "repeat_interval", True, "not a valid duration"),
    ("max_repeat_interval", "1d", "repeat_interval", None, None),
    ("min_group_wait", "30s", "group_wait", 90000, "not a valid duration"),
    ("min_group_wait", "30s", "group_wait", True, "not a valid duration"),
    ("min_group_wait", "30s", "group_wait", None, None),
]


def _base(line: str) -> str:
    """The finding without its level and (strict-only) fix hint."""
    return re.sub(r"^\s*(ERROR|WARN):\s*", "", line).split(" — fix: ")[0]


@pytest.mark.parametrize("constraint,bound,field,value,expect", CASES,
                         ids=[f"{c[0]}={c[1]}:{c[3]!r}" for c in CASES])
def test_strict_and_lenient_agree_and_differ_only_in_level(
        constraint, bound, field, value, expect):
    routing = {"tenant-x": {field: value}}
    policies = {"pol": {"tenants": ["tenant-x"],
                        "constraints": {constraint: bound}}}
    strict = check_domain_policies(routing, policies, strict=True)
    lenient = check_domain_policies(routing, policies)
    assert [_base(m) for m in strict] == [_base(m) for m in lenient], (strict, lenient)
    assert all(m.lstrip().startswith("ERROR:") for m in strict), strict
    assert all(m.lstrip().startswith("WARN:") and " — fix: " not in m
               for m in lenient), lenient
    if expect is None:
        assert strict == [], strict
    else:
        assert len(strict) == 1 and expect in strict[0], strict
