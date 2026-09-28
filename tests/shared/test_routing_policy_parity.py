"""Python half of tests/shared/routing_policy_parity_matrix.json (#2280).

The route generator's reader is the SSOT for how a tenant's routing resolves
(`_routing_defaults` -> routing profile -> tenant `_routing`) and for which
receiver types an ADR-007 domain policy rejects. The Go readers
(`pkg/routingpolicy`, da-guard, tenant-api) assert the same table; none of
the four reads another's source.

What this half measures, per tree written to a tmp dir:

* `targets` — `load_tenant_tree(...).routing_configs[t]`: the main receiver,
  then `list_tenant_subroutes` (the generator's render order);
* `rejected_routes` — the `routes` entries `route_entry_matchers` rejects;
* `policy` — the receiver-type lines `check_domain_policies` (strict) put in
  `schema_warnings`, parsed back to (domain, ref, constraint);
* `unknown_profile` — the `_routing_profile references unknown profile` WARN.

A cell's `python_differs` replaces the Go value where the YAML readers
disagree (see the matrix `_comment`).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_validate import list_tenant_subroutes, route_entry_matchers  # noqa: E402

MATRIX = json.loads((Path(__file__).parent / "routing_policy_parity_matrix.json")
                    .read_text(encoding="utf-8"))

# Exact key sets: a misspelt key read as absent would turn a row into one
# that tests nothing while staying green.
TOP_KEYS = {"_comment", "trees"}
TREE_KEYS = {"name", "files", "expect"}
EXPECT_KEYS = {"targets", "policy", "rejected_routes", "unknown_profile",
               "tenant_api", "python_differs"}
TENANT_API_KEYS = {"put", "batch"}
BATCH_KEYS = {"patch", "verdict"}
DIFFERS_KEYS = {"reason", "targets", "policy", "rejected_routes"}
CONSTRAINTS = {"forbidden_receiver_types", "allowed_receiver_types"}

# One receiver-type violation line of check_domain_policies. `ref` is absent
# for the tenant's main route; the parenthesised match text follows a sub-route.
_POLICY_LINE = re.compile(
    r"domain_policy '(?P<domain>[^']*)', tenant '(?P<tenant>[^']*)'"
    r"(?: (?P<ref>(?:override|routes)\[\d+\]) \(.*?\))?"
    r": receiver type '[^']*' (?P<kind>is forbidden|not in allowed types)")
_UNKNOWN_PROFILE = re.compile(
    r"WARN: (?P<tenant>\S+): _routing_profile references unknown profile '(?P<name>[^']*)'")


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])
    names = {t["name"] for t in MATRIX["trees"]}
    # The owner's step-0 cases and the ADR-007 example are pinned by name:
    # dropping one would narrow the pin silently.
    for required in ("i-routes-entry-unknown-receiver-type", "ii-routes-entry-forbidden-type",
                     "iii-override-unknown-receiver-type", "iv-override-forbidden-type",
                     "adr007-five-tenants", "yaml11-bool-match-value"):
        assert required in names, required


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        assert set(tree) == TREE_KEYS, (tree.get("name"), set(tree) ^ TREE_KEYS)
        for tenant, want in tree["expect"].items():
            where = (tree["name"], tenant)
            assert set(want) == EXPECT_KEYS, (where, set(want) ^ EXPECT_KEYS)
            for row in want["policy"]:
                assert len(row) == 3 and row[2] in CONSTRAINTS, (where, row)
            api = want["tenant_api"]
            assert api is None or set(api) == TENANT_API_KEYS, (where, api)
            if api is not None:
                assert api["put"] in ("403", "ok"), where
                batch = api["batch"]
                assert batch is None or (set(batch) == BATCH_KEYS
                                         and batch["verdict"] in ("ok", "policy_violation")), where
            differs = want["python_differs"]
            assert differs is None or (set(differs) == DIFFERS_KEYS and differs["reason"]), where


def _build(tree: dict, root: Path) -> None:
    for rel, content in tree["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _type(receiver: object) -> str | None:
    if isinstance(receiver, dict):
        t = receiver.get("type")
        if isinstance(t, str) and t:
            return t
    return None


def _targets(rc: dict | None) -> list[dict] | None:
    if rc is None:
        return None
    if not rc.get("receiver"):
        return []
    out = [{"ref": "receiver", "type": _type(rc["receiver"])}]
    for ref, _match, cfg, _inherited in list_tenant_subroutes(rc):
        # The Python ref spells the overrides list in the singular.
        out.append({"ref": ref.replace("override[", "overrides[", 1),
                    "type": _type(cfg.get("receiver"))})
    return out


def _rejected(tenant: str, rc: dict | None) -> list[str]:
    if rc is None or rc.get("routes") is None:
        return []
    routes = rc["routes"]
    if not isinstance(routes, list):
        return ["routes"]  # the generator renders nothing from it
    return [f"routes[{i}]" for i, entry in enumerate(routes)
            if route_entry_matchers(entry, i, tenant)[0] is None]


def _policy_rows(warnings: list[str]) -> list[tuple[str, str, str, str]]:
    rows = []
    for line in warnings:
        m = _POLICY_LINE.search(line)
        if not m:
            continue
        ref = m["ref"] or "receiver"
        ref = ref.replace("override[", "overrides[", 1)
        constraint = ("forbidden_receiver_types" if m["kind"] == "is forbidden"
                      else "allowed_receiver_types")
        rows.append((m["tenant"], m["domain"], ref, constraint))
    return rows


def _want(want: dict, key: str):
    differs = want["python_differs"]
    return differs[key] if differs is not None else want[key]


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_python_reader_matches_the_table(tree, tmp_path: Path) -> None:
    _build(tree, tmp_path)
    got = load_tenant_tree(str(tmp_path), strict_policies=True)
    assert not got.tenant_file_errors, (tree["name"], got.tenant_file_errors)

    rows = _policy_rows(got.schema_warnings)
    unknown = {m["tenant"]: m["name"] for m in map(_UNKNOWN_PROFILE.search, got.schema_warnings) if m}
    for tenant, want in tree["expect"].items():
        where = (tree["name"], tenant)
        rc = got.routing_configs.get(tenant)
        assert _targets(rc) == _want(want, "targets"), (where, rc)
        assert _rejected(tenant, rc) == _want(want, "rejected_routes"), (where, rc)
        mine = sorted((d, r, c) for t, d, r, c in rows if t == tenant)
        assert mine == sorted(tuple(p) for p in _want(want, "policy")), (where, mine)
        assert unknown.get(tenant) == want["unknown_profile"], (where, unknown)

    # Nothing the table does not name: every routed tenant and every policy
    # line belongs to a listed tenant, and the line count is the table's.
    assert set(got.routing_configs) <= set(tree["expect"]), (tree["name"], set(got.routing_configs))
    expected_total = sum(len(_want(w, "policy")) for w in tree["expect"].values())
    assert len(rows) == expected_total, (tree["name"], rows)
    assert Counter(t for t, *_ in rows).keys() <= set(tree["expect"]), (tree["name"], rows)
