"""Every key a shipped conf.d tree's tenants write must reach /metrics (#3014).

`components/threshold-exporter/config/conf.d/examples/` shipped three tenants
with 15 threshold keys that /metrics never listed: their base values sat in
`examples/_defaults-multidb.yaml`, a name the loader does not read as a defaults
carrier. Nothing went red, because no gate asked the exporter what it actually
serves. This one does: it reads `da-guard served-values` (the exporter's own
load) and allows an `unserved` key only when it is one of the stated exceptions.

⛔ The roots are `_SHIPPED_CONFD_ROOTS`, not a list of our own — that table is
pinned to `.pre-commit-config.yaml` by `test_check_threshold_reachability.py`,
so a new shipped tree enters here without anyone remembering to add it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
from _lib_tenant_values import load_served_values  # noqa: E402

_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_reachability.py"
_spec = importlib.util.spec_from_file_location("check_threshold_reachability", _SCRIPT)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)

SHIPPED_ROOTS = sorted(_gate._SHIPPED_CONFD_ROOTS)


def _is_stated_exception(key: str, value: object) -> bool:
    """The only `unserved` keys a shipped tree may carry.

    * `_routing_profile` — read by the route generator, not by the exporter.
    * a dimensional key set to `disable` — deliberately emits no row.
    """
    if key == "_routing_profile":
        return True
    return "{" in key and str(value).strip().lower() == "disable"


@pytest.mark.parametrize("root", SHIPPED_ROOTS)
def test_shipped_example_tree_serves_every_tenant_key(root, da_guard_binary):
    tenants = load_served_values(REPO_ROOT / root, binary=da_guard_binary)
    assert tenants, f"{root}: the exporter loaded no tenant — nothing was measured"
    dead = {
        tid: sorted(k for k, v in tv.unserved.items() if not _is_stated_exception(k, v))
        for tid, tv in tenants.items()
    }
    dead = {tid: keys for tid, keys in dead.items() if keys}
    assert not dead, (
        f"{root}: these tenant keys reach no /metrics row — their base value is "
        f"missing from a defaults carrier the exporter reads, or no rule pack "
        f"knows them: {dead}")


def test_a_key_with_no_base_is_reported_unserved(tmp_path, da_guard_binary):
    """Positive control: the measurement this file rests on can say "no".

    A tenant key whose base is in no defaults carrier — the #3014 shape — must
    come back in `unserved`, or every assertion above passes vacuously.
    """
    (tmp_path / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "_defaults-multidb.yaml").write_text(
        "defaults:\n  redis_connected_clients: 200\n", encoding="utf-8")
    (tmp_path / "t1.yaml").write_text(
        'tenants:\n  t1:\n    mysql_connections: "70"\n'
        '    redis_connected_clients: "500"\n', encoding="utf-8")
    tv = load_served_values(tmp_path, binary=da_guard_binary)["t1"]
    assert "redis_connected_clients" in tv.unserved
    assert "mysql_connections" not in tv.unserved


@pytest.mark.parametrize("key,value,expected", [
    ("_routing_profile", "domain-finance-tier1", True),
    ('redis_db_keys{db="db2"}', "disable", True),
    ('redis_db_keys{db="db2"}', " Disable ", True),
    ('redis_db_keys{db="db2"}', "100", False),
    ("redis_memory_used_bytes", "disable", False),
    ("_routng", "x", False),
    ("_routing_profiles", "x", False),
])
def test_the_exceptions_are_exactly_the_stated_ones(key, value, expected):
    assert _is_stated_exception(key, value) is expected
