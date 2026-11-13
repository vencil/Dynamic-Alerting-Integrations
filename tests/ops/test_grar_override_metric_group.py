"""#3692: a routing override's `metric_group` against the shipped rule packs.

`SHIPPED_METRIC_GROUPS` (scripts/tools/ops/_grar_routes.py) is the generator's
copy of every group the shipped rule packs carry — a copy because the
generator runs where the packs are not. This file pins it equal to the packs
as Prometheus reads them, and pins the advisory an override gets when its
group is none of them. Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in ("scripts/tools/ops", "scripts/tools", "scripts/tools/lint"):
    sys.path.insert(0, str(REPO_ROOT / p))

import _grar_routes  # noqa: E402
import check_metric_group_pairs  # noqa: E402


def test_shipped_groups_are_the_rule_packs_groups():
    by_pack = check_metric_group_pairs.collect_groups_by_pack()
    computed = {mg for groups in by_pack.values() for mg in groups}
    assert computed, "no metric_group read from the rule packs"
    assert _grar_routes.SHIPPED_METRIC_GROUPS == computed, (
        "SHIPPED_METRIC_GROUPS drifted from the rule packs: "
        f"only in the constant {sorted(_grar_routes.SHIPPED_METRIC_GROUPS - computed)}, "
        f"only in the packs {sorted(computed - _grar_routes.SHIPPED_METRIC_GROUPS)}")


def _override_findings(metric_group):
    routing = {"receiver": {"type": "webhook", "url": "https://hooks.example.com/a"},
               "overrides": [{"metric_group": metric_group,
                              "receiver": {"type": "webhook", "url": "https://hooks.example.com/b"}}]}
    _routes, _receivers, warnings = _grar_routes.expand_routing_overrides("t-x", routing)
    return [w for w in warnings if getattr(w, "kind", None) == "unknown_override_metric_group"]


def test_renamed_group_gets_the_advisory_and_still_renders():
    found = _override_findings("liveness")
    assert len(found) == 1, found
    assert found[0].blocks == "never"
    assert found[0].field == "overrides[0].metric_group"
    assert "mariadb_liveness" in found[0]


def test_shipped_and_slo_groups_get_none():
    assert _override_findings("mariadb_liveness") == []
    assert _override_findings("slo_checkout") == []
