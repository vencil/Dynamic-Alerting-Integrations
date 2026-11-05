#!/usr/bin/env python3
"""One RFC 1123 accept/reject matrix for the three tenant-name gates (#1713).

`operator_generate.validate_tenant_name`, `migrate_to_operator.validate_tenant_name`
and `init_project._validate_tenant_name` are the same K8s object-name gate
written three times — the first two share one regex verbatim, the third
spells the 63-character limit as `len(name) <= 63` instead of `{0,61}`.
Before this file only two of the three had a direct test, and the copy that
`operator_generate.discover_tenant_configs` actually uses had none:
`test_operator_generate_v2.py::test_carriers_the_exporter_would_not_serve_are_not_tenants`
reaches it with a dot-prefixed stem only.

Measured on #1713's base: with that copy's body replaced by
`return not name.startswith(".")`, `discover_tenant_configs` on a conf.d of
`ok-a / DB-A / db_a / db-a- / .hidden` returned
`['DB-A', 'db-a-', 'db_a', 'ok-a']` with 1 warning line instead of `['ok-a']`
with 4, and `test_operator_generate_v2.py` stayed at 0 failed.

Why one matrix over three copies rather than three files: the copies are
supposed to be interchangeable, so a probe is parametrised over all three and
a copy that drifts fails BY NAME (`operator_generate-DB-A`), not as a
disagreement someone has to bisect. `test_the_three_copies_agree_on_every_probe`
is the same fact stated once more as an equality so a probe added to only one
face is still checked across copies.

ADR-035: the three are no longer copies. Each calls
`_lib_validation.is_valid_tenant_id`, which reads the one rule (the
tenant-config schema's `definitions.tenantId`, a DNS-1123 label); the matrix
now pins that every face still answers by that rule, the shared function
itself included. `alert_quality`'s `--tenant` whitelist (formerly its own
`^[a-zA-Z0-9_-]+$`, which took `DB_A`) calls it too and is pinned in
test_alert_quality.py.
"""
from __future__ import annotations

# #3572: operator_generate / migrate_to_operator no longer read tenant ids
# (they emit no AlertmanagerConfig and read no conf.d), so their two faces and
# the discover-level tests that drove `discover_tenant_configs` are gone; the
# remaining faces are init_project's and the shared rule itself.

import os
import sys

import pytest

_OPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _OPS_DIR)
sys.path.insert(0, os.path.join(_OPS_DIR, '..'))

import init_project  # noqa: E402
from _lib_validation import is_valid_tenant_id  # noqa: E402


IMPLEMENTATIONS = [
    ("init_project", init_project._validate_tenant_name),
    ("is_valid_tenant_id", is_valid_tenant_id),
]

# 63 characters is the RFC 1123 label ceiling; the last ACCEPT row sits on it
# and the last REJECT row is one past it.
_ON_CEILING = "a" + "-" * 61 + "a"       # 63 characters
_ONE_PAST_CEILING = "a" + "-" * 62 + "a"  # 64 characters
ACCEPT = ["ok-a", "a", "0", "db1", "svc-01", "a-b-c", _ON_CEILING]
# `a.b` separates the DNS-1123 LABEL these gates enforce from the DNS-1123
# SUBDOMAIN a K8s `metadata.name` would also take: a copy rewritten to the
# subdomain regex passes every other row (`db..a` is an empty label under
# both) and fails only here.
# `ok-a\n` is the #1779 row: `re.match(r"^...$")` lets `$` succeed before a
# trailing newline, so a conf.d stem literally named `evil\n` reached
# `metadata.name: da-tenant-evil\n`; only `re.fullmatch` refuses it.
REJECT = ["DB-A", "db_a", "db-a-", "-lead", "db..a", "a.b", "a b", "",
          _ONE_PAST_CEILING, "ok-a\n"]

_IMPL_IDS = [name for name, _ in IMPLEMENTATIONS]


def _probe_id(probe: str) -> str:
    if probe == "":
        return "empty"
    if probe.endswith("\n"):
        return "trailing-newline"
    if len(probe) > 20:
        return f"len{len(probe)}"
    return probe


@pytest.mark.parametrize("probe", ACCEPT, ids=[_probe_id(p) for p in ACCEPT])
@pytest.mark.parametrize("impl", [f for _, f in IMPLEMENTATIONS], ids=_IMPL_IDS)
def test_accepts(impl, probe):
    assert impl(probe) is True


@pytest.mark.parametrize("probe", REJECT, ids=[_probe_id(p) for p in REJECT])
@pytest.mark.parametrize("impl", [f for _, f in IMPLEMENTATIONS], ids=_IMPL_IDS)
def test_rejects(impl, probe):
    assert impl(probe) is False


def test_the_boundary_rows_sit_on_the_63_character_ceiling():
    """Named rows, not positions: a row appended later cannot silently
    change what is measured here (blind review)."""
    assert len(_ON_CEILING) == 63 and _ON_CEILING in ACCEPT
    assert len(_ONE_PAST_CEILING) == 64 and _ONE_PAST_CEILING in REJECT


def test_the_three_copies_agree_on_every_probe():
    """Every face, one answer per probe — a drifted face shows up here too."""
    disagreements = {}
    for probe in ACCEPT + REJECT:
        answers = {name: bool(fn(probe)) for name, fn in IMPLEMENTATIONS}
        if len(set(answers.values())) != 1:
            disagreements[probe] = answers
    assert not disagreements, disagreements


def test_accept_face_is_not_vacuous():
    """A gate that rejects everything satisfies every REJECT row; this stops it."""
    assert ACCEPT
    for name, fn in IMPLEMENTATIONS:
        assert any(fn(p) for p in ACCEPT), (
            f"{name} accepts none of the ACCEPT probes")
