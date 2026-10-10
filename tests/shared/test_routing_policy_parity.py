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
* `values_not_string` (#2431) — the strict ERROR lines for a routes match
  value / override alertname / metric_group that PyYAML does not read as a
  string (`_grar_validate.routing_values_not_string`), parsed back to fields;
* `group_by_invalid` (#2503) — the strict ERROR lines for a bad group_by
  element (`_grar_validate.routing_group_by_invalid`), parsed back to
  (field, kind);
* `enforced_group_by_invalid` (#2503, per tree) — the strict ERROR lines for
  a bad group_by element of the rendered `_routing_enforced` route(s)
  (`_grar_routes.enforced_group_by_problems`), parsed back to (field, kind)
  with the line's context (`_routing_enforced` / `_routing_enforced
  (<tenant>)`) in the field; the line names no file, so the file column is
  the Go readers' alone;
* `policy` — the receiver-type lines `check_domain_policies` (strict) put in
  `schema_warnings`, parsed back to (domain, ref, constraint);
* `unknown_profile` — the `_routing_profile references unknown profile` WARN;
* `refused` (#2341) — the blocking WARN for a `_routing` that is neither a
  mapping nor a disabling string (`routing_not_mapping`) or for a tenant id
  the routing plane refuses (`invalid_tenant_id`; the `tenant_ids` table pins
  `_lib_validation.is_valid_tenant_id` itself);
* `escalation` (#2325) — the `require_critical_escalation` lines
  `check_domain_policies` (strict) put in `schema_warnings` (the non-compliance
  ERROR, one WARN per leaking destination, in render order), for the domains
  the generator's own reader (`_parse_config_files`) says require it (root
  policies only: no row carries the constraint in a subtree policy yet, and
  `_escalation` asserts on any line from a domain not listed here);
* `platform` — the generator's blocking `_routing_defaults.routes` WARN, its
  blocking non-mapping `_routing_defaults` WARN (#2341), its
  strict non-boolean `require_critical_escalation` line (#2325,
  `domain_policy_unusable`, kind and field only: the line names no file), its
  strict "domain policy file could not be used" line (#2659: the whole file
  dropped — field `domain_policies` when that block is not a mapping, `""`
  when the file does not load, as da-guard names them) and
  (#2326) its routing-tree findings, `TenantTree.routing_tree_problems`; the
  Go-only `routing_in_unread_location` rows (#2291) are left out here, their
  `targets` column pins that the generator renders nothing from those bytes.

A cell's `python_differs` replaces the Go value where the YAML readers
disagree (see the matrix `_comment`): `targets`, `policy`, `rejected_routes`,
`group_by_invalid` and `refused` are asserted against it here, and its
`tenant_api` (the verdict the generator would give the PUT) against what this
reader found (`_generator_put`). Every field where it differs from the Go
column is a reader difference that tests/shared/test_reader_divergence_catalog.py
ranks and matches to a `layer: routing` entry of
tests/shared/reader_divergence_catalog.yaml (ADR-036 step 2, PR-C). A tree
the generator refuses whole because a tenant file cannot be read
(`tenant_file_errors`, #1460: rc 1, nothing rendered) is one only through
that: every tenant's `python_differs.refused` is `tenant_file_unreadable`.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_parse import BLOCKING_TREE_KINDS, _parse_config_files, load_tenant_tree  # noqa: E402
from _grar_validate import list_tenant_subroutes, route_entry_matchers  # noqa: E402
from _lib_validation import is_valid_tenant_id  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

MATRIX = json.loads((Path(__file__).parent / "routing_policy_parity_matrix.json")
                    .read_text(encoding="utf-8"))

# Exact key sets: a misspelt key read as absent would turn a row into one
# that tests nothing while staying green.
TOP_KEYS = {"_comment", "blocking_kinds", "tenant_ids", "tenant_api_unmeasured", "trees"}
# Why a tenant_api cell may be null (tenant-api's verdict is not measured).
UNMEASURED_KINDS = {"nested_tree", "no_put_body", "outside_table"}
# Every `outside_table` cell, with the refusal tenant-api actually answers
# there (its reason must name it): a measurable PUT parked outside the table
# is red until it is added here, in the diff.
OUTSIDE_TABLE = {
    ("tenant-merge-tag-key-2700", "t1"): '400 BAD_REQUEST',
    ("hier-e-duplicate-tenant-id", "t-x"): "409 TENANT_DECLARED_ELSEWHERE",
}
TREE_KEYS = {"name", "files", "platform", "expect", "enforced_group_by_invalid"}
PLATFORM_KINDS = {"routing_defaults_routes_ignored", "routing_in_unread_location",
                  "domain_policy_unusable",
                  # #2326: the hierarchical routing plane's tree findings.
                  "routing_enforced_below_root", "routing_defaults_null_below_root",
                  "routing_profile_duplicate", "duplicate_tenant",
                  "domain_policy_out_of_scope",
                  # #2341: a `_routing_defaults` that is not a mapping.
                  "routing_defaults_not_mapping"}
# #2291: routing where the generator never reads it. The Go side reports it;
# the generator says nothing (it does not read those bytes), so this half
# leaves the kind out of its platform comparison — the tree's `targets`
# column is what pins "not rendered" here.
GO_ONLY_PLATFORM_KINDS = {"routing_in_unread_location"}
EXPECT_KEYS = {"targets", "policy", "rejected_routes", "values_not_string",
               "group_by_invalid", "unknown_profile", "tenant_api",
               "python_differs", "escalation", "refused"}
REFUSED_KINDS = {"routing_not_mapping", "invalid_tenant_id"}
GROUP_BY_KINDS = {"not_string", "empty", "duplicate", "wildcard_mixed"}
ESCALATION_KEYS = {"verdict", "leaks"}
ESCALATION_VERDICTS = {"compliant", "violation"}
TENANT_API_KEYS = {"put", "batch"}
BATCH_KEYS = {"patch", "verdict"}
# B2 (#2341): the keys a batch op may remove (tenant-api's unsetAllowedKeys).
BATCH_OPTIONAL_KEYS = {"unset"}
BATCH_UNSET_KEYS = {"_routing"}
DIFFERS_KEYS = {"reason", "targets", "policy", "rejected_routes", "group_by_invalid", "refused",
                "tenant_api"}
# The generator's refusals: the Go readers' kinds, and (python_differs only)
# a tenant file it cannot read, which refuses the whole tree.
DIFFERS_REFUSED_KINDS = REFUSED_KINDS | {"tenant_file_unreadable"}
CONSTRAINTS = {"forbidden_receiver_types", "allowed_receiver_types"}

# One receiver-type violation line of check_domain_policies. `ref` is absent
# for the tenant's main route; the parenthesised match text follows a sub-route.
_POLICY_LINE = re.compile(
    r"domain_policy '(?P<domain>[^']*)', tenant '(?P<tenant>[^']*)'"
    r"(?: (?P<ref>(?:override|routes)\[\d+\]) \(.*?\))?"
    r": receiver type '[^']*' (?P<kind>is forbidden|not in allowed types)")
# The blocking WARN _grar_parse records when it drops `_routing_defaults.routes`.
_DEFAULTS_ROUTES = re.compile(
    r"WARN: _routing_defaults in (?P<file>\S+): 'routes' is not supported here")
# #2325: the strict line for a `require_critical_escalation` that is not a
# boolean (as PyYAML reads it). It names no file, so its platform row is
# compared on kind and field only.
_ESC_NOT_BOOL = re.compile(
    r"domain_policy '(?P<domain>[^']*)': constraint 'require_critical_escalation' must be a boolean")
# #2659: the strict line for a policy file the generator dropped whole.
_POLICY_FILE_DROPPED = re.compile(
    r"domain policy file could not be used — every domain policy is silently dropped: "
    r"(?P<file>[^:]+): (?P<reason>.*)")
# #2431: the strict line for a matcher value PyYAML does not read as a string.
_NOT_STRING = re.compile(
    r"ERROR: tenant '(?P<tenant>[^']*)': (?P<field>\S+) must be a string, got ")
# #2503: the strict line for a bad group_by element; the wording after the
# field names the kind (_grar_validate.group_by_problem_text).
_GROUP_BY = re.compile(
    r"ERROR: tenant '(?P<tenant>[^']*)': (?P<field>\S*group_by\[\d+\]) "
    r"(?P<why>is an empty string|repeats label|is '\.\.\.' alongside|is .*?, not a string)")
# #2503: the same refusal for a `_routing_enforced` route; the context is
# `_routing_enforced` or, for the `{{tenant}}` shape, `_routing_enforced (<tenant>)`.
_ENFORCED_GROUP_BY = re.compile(
    r"ERROR: (?P<ctx>_routing_enforced(?: \([^)]*\))?): (?P<field>group_by\[\d+\]) "
    r"(?P<why>is an empty string|repeats label|is '\.\.\.' alongside|is .*?, not a string)")
_GROUP_BY_WHY = {"is an empty string": "empty", "repeats label": "duplicate",
                 "is '...' alongside": "wildcard_mixed"}
# #2341: the blocking lines for routing that cannot be read at all.
_DEFAULTS_NOT_MAPPING = re.compile(
    r"WARN: _routing_defaults in (?P<file>\S+) must be a mapping, got ")
_ROUTING_NOT_MAPPING = re.compile(
    r"WARN: (?P<tenant>\S+): _routing must be a mapping or a disabling string")
_INVALID_TENANT_ID = re.compile(
    r"WARN: tenant id (?P<repr>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") is not a valid tenant id")
_UNKNOWN_PROFILE = re.compile(
    r"WARN: (?P<tenant>\S+): _routing_profile references unknown profile '(?P<name>[^']*)'")
# #2325: the require_critical_escalation lines — the non-compliance line
# (strict: ERROR), and one WARN per leaking destination (a sub-route by ref,
# or the main receiver).
_ESC_MISSING = re.compile(
    r"domain_policy '(?P<domain>[^']*)', tenant '(?P<tenant>[^']*)': "
    r"require_critical_escalation is set but")
_ESC_LEAK = re.compile(
    r"WARN: domain_policy '(?P<domain>[^']*)', tenant '(?P<tenant>[^']*)'"
    r"(?: (?P<ref>(?:override|routes)\[\d+\]) \(.*?\): receiver type '[^']*' catches alerts"
    r"|: severity=critical alerts that no sub-route catches go to the main receiver)")


def _python_reports(row: list[str]) -> bool:
    """A Go-only kind row the generator reports too (#2326 review F3): a top-level
    `_routing_defaults` in a `_` non-carrier file below the root. No other row
    of that kind has this bare field (the others name a key path inside a
    defaults block or a profile)."""
    return row[0] == "routing_in_unread_location" and row[2] == "_routing_defaults"


def test_blocking_kinds_are_the_generators() -> None:
    """#2326 review F5: the table's blocking kinds ARE the generator's; Go
    asserts `routingpolicy.IsBlocking` against the same list."""
    assert set(MATRIX["blocking_kinds"]) == set(BLOCKING_TREE_KINDS)
    assert set(MATRIX["blocking_kinds"]) <= PLATFORM_KINDS


def test_tenant_id_table() -> None:
    """#2341 R8 / ADR-035: the routing plane's tenant-id predicate, both columns."""
    table = MATRIX["tenant_ids"]
    assert set(table) == {"valid", "invalid", "schema_search_accepts"}
    assert table["valid"] and table["invalid"] and table["schema_search_accepts"]
    assert "" in table["invalid"]
    for tid in table["valid"]:
        assert is_valid_tenant_id(tid), repr(tid)
    for tid in table["invalid"] + table["schema_search_accepts"]:
        assert not is_valid_tenant_id(tid), repr(tid)


def _schema_tenant_key_errors(tid: str) -> list:
    """The `propertyNames` errors the tenant schema gives a `tenants` key."""
    schema = json.loads((REPO_ROOT / "docs" / "schemas" / "tenant-config.schema.json")
                        .read_text(encoding="utf-8"))
    errors = jsonschema.Draft7Validator(schema).iter_errors({"tenants": {tid: {}}})
    return [e for e in errors if "propertyNames" in e.schema_path]


def test_schema_property_names_follow_the_table() -> None:
    """ADR-035 D2: `check_confd_schema` judges tenant keys through the
    schema's `propertyNames` with JSON Schema SEARCH semantics. It agrees with
    the decoded-key readers on both columns, and lets `schema_search_accepts`
    through (Python's `$` matches before a trailing newline) — the gap the ADR
    accepts because every decoded-key reader refuses those ids."""
    table = MATRIX["tenant_ids"]
    for tid in table["valid"]:
        assert not _schema_tenant_key_errors(tid), repr(tid)
    for tid in table["invalid"]:
        if tid.endswith("\n") and is_valid_tenant_id(tid[:-1]):
            continue  # the search gap, as schema_search_accepts pins
        assert _schema_tenant_key_errors(tid), repr(tid)
    for tid in table["schema_search_accepts"]:
        assert not _schema_tenant_key_errors(tid), repr(tid)


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])
    names = {t["name"] for t in MATRIX["trees"]}
    # The owner's step-0 cases and the ADR-007 example are pinned by name:
    # dropping one would narrow the pin silently.
    for required in ("i-routes-entry-unknown-receiver-type", "ii-routes-entry-forbidden-type",
                     "iii-override-unknown-receiver-type", "iv-override-forbidden-type",
                     "adr007-five-tenants", "yaml11-bool-match-value",
                     "require-critical-escalation", "routing-values-yaml11",
                     "group-by-elements", "routing-unset-reenable"):
        assert required in names, required


def unmeasured_errors(matrix: dict) -> list:
    """A null tenant_api cell hides whatever tenant-api would answer, so it
    is allowed only where the verdict genuinely is not measured: the cell is
    listed in tenant_api_unmeasured with a kind the tree bears out and a
    reason — and every listed cell is null."""
    errors = []
    table = matrix["tenant_api_unmeasured"]
    trees = {t["name"]: t for t in matrix["trees"]}
    for tree in matrix["trees"]:
        for tenant, want in tree["expect"].items():
            listed = table.get(tree["name"], {}).get(tenant)
            if want["tenant_api"] is None and listed is None:
                errors.append(f"{tree['name']} {tenant}: tenant_api is null but not in tenant_api_unmeasured "
                              f"(measure it, or state why it cannot be)")
            if want["tenant_api"] is not None and listed is not None:
                errors.append(f"{tree['name']} {tenant}: tenant_api is measured, drop it from tenant_api_unmeasured")
    for name, cells in table.items():
        tree = trees.get(name)
        for tenant, why in cells.items():
            where = f"tenant_api_unmeasured {name} {tenant}"
            if tree is None or tenant not in tree["expect"]:
                errors.append(f"{where}: no such cell")
                continue
            if not (isinstance(why, dict) and set(why) == {"kind", "reason"}
                    and why["kind"] in UNMEASURED_KINDS and str(why["reason"]).strip()):
                errors.append(f"{where}: must be {{kind: one of {sorted(UNMEASURED_KINDS)}, reason}}")
                continue
            root_body = f"{tenant}.yaml" in tree["files"]
            nested_own = any("/" in f and f.rsplit("/", 1)[1] == f"{tenant}.yaml" for f in tree["files"])
            if why["kind"] == "nested_tree" and (root_body or not nested_own):
                errors.append(f"{where}: nested_tree needs the tenant's own file below the root "
                              f"(<sub>/{tenant}.yaml) and none at the root")
            if why["kind"] == "no_put_body" and (root_body or nested_own):
                errors.append(f"{where}: no_put_body, but a {tenant}.yaml exists (measure it, or nested_tree)")
            if why["kind"] == "outside_table" and not root_body:
                errors.append(f"{where}: outside_table needs a measurable PUT (a root files[{tenant}.yaml])")
            if why["kind"] == "outside_table":
                pinned = OUTSIDE_TABLE.get((name, tenant))
                if pinned is None:
                    errors.append(f"{where}: outside_table is not in OUTSIDE_TABLE — measure the cell, or add "
                                  f"(tree, tenant) with tenant-api's actual refusal to that list")
                elif pinned not in str(why["reason"]):
                    errors.append(f"{where}: the reason does not name the pinned refusal {pinned!r}")
    for (name, tenant) in OUTSIDE_TABLE:
        if table.get(name, {}).get(tenant, {}).get("kind") != "outside_table":
            errors.append(f"OUTSIDE_TABLE {name} {tenant}: no such outside_table cell (drop it from the list)")
    return errors


def test_null_tenant_api_cells_are_unmeasured_only() -> None:
    errors = unmeasured_errors(MATRIX)
    assert not errors, "\n".join(errors)


def test_unmeasured_guard_is_red() -> None:
    """Nulling a measured cell, or listing a measured one, is red."""
    m = json.loads(json.dumps(MATRIX))
    cell = next(w for t in m["trees"] if t["name"] == "domain-policy-merge-key-order"
                for k, w in t["expect"].items() if k == "t-mo-own")
    cell["tenant_api"] = None
    assert any("t-mo-own: tenant_api is null but not in tenant_api_unmeasured" in e
               for e in unmeasured_errors(m))
    m = json.loads(json.dumps(MATRIX))
    m["tenant_api_unmeasured"]["domain-policy-null"] = {"t-null": {"kind": "outside_table", "reason": "r"}}
    assert any("drop it from tenant_api_unmeasured" in e for e in unmeasured_errors(m))
    m = json.loads(json.dumps(MATRIX))
    m["tenant_api_unmeasured"]["invalid-tenant-ids"]["UPPER"]["kind"] = "nested_tree"
    assert any("nested_tree needs the tenant's own file below the root" in e for e in unmeasured_errors(m))
    # A measurable cell parked as outside_table, not in OUTSIDE_TABLE: red.
    m = json.loads(json.dumps(MATRIX))
    next(t for t in m["trees"] if t["name"] == "domain-policy-null")["expect"]["t-null"]["tenant_api"] = None
    m["tenant_api_unmeasured"]["domain-policy-null"] = {"t-null": {"kind": "outside_table", "reason": "r"}}
    assert any("domain-policy-null t-null: outside_table is not in OUTSIDE_TABLE" in e
               for e in unmeasured_errors(m))
    # A pinned cell whose reason no longer names its refusal: red.
    m = json.loads(json.dumps(MATRIX))
    m["tenant_api_unmeasured"]["hier-e-duplicate-tenant-id"]["t-x"]["reason"] = "something else"
    assert any("does not name the pinned refusal '409 TENANT_DECLARED_ELSEWHERE'" in e
               for e in unmeasured_errors(m))
    # A root-level tenant of a nested tree is measurable: parking it is red.
    m = json.loads(json.dumps(MATRIX))
    next(t for t in m["trees"] if t["name"] == "hier-d-subtree-policy-additive-and-scoped")[
        "expect"]["t-root"]["tenant_api"] = None
    m["tenant_api_unmeasured"]["hier-d-subtree-policy-additive-and-scoped"]["t-root"] = {
        "kind": "nested_tree", "reason": "r"}
    assert any("hier-d-subtree-policy-additive-and-scoped t-root: nested_tree needs" in e
               for e in unmeasured_errors(m))


# [未驗] batch: the tenant_api.batch sub-cells are not held to the generator
# (_generator_put keeps them as tenant-api's). Pinned so that the unjudged
# set cannot grow silently: a new batch cell changes this number in a diff.
UNJUDGED_BATCH_CELLS = 12


def test_batch_cells_are_pinned() -> None:
    n = sum(1 for t in MATRIX["trees"] for w in t["expect"].values()
            if w["tenant_api"] is not None and w["tenant_api"]["batch"] is not None)
    assert n == UNJUDGED_BATCH_CELLS, (
        f"{n} tenant_api.batch cells, UNJUDGED_BATCH_CELLS says {UNJUDGED_BATCH_CELLS}: the batch "
        "verdict is not held to the generator ([未驗] batch) — update the count, the matrix _comment, "
        "the changelog fragment and ADR-036's appendix together")


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        assert set(tree) == TREE_KEYS, (tree.get("name"), set(tree) ^ TREE_KEYS)
        for row in tree["platform"]:
            assert len(row) == 3 and row[0] in PLATFORM_KINDS, (tree["name"], row)
        for row in tree["enforced_group_by_invalid"]:
            assert len(row) == 3 and row[2] in GROUP_BY_KINDS, (tree["name"], row)
        for tenant, want in tree["expect"].items():
            where = (tree["name"], tenant)
            assert set(want) == EXPECT_KEYS, (where, set(want) ^ EXPECT_KEYS)
            for row in want["policy"]:
                assert len(row) == 3 and row[2] in CONSTRAINTS, (where, row)
            for row in want["group_by_invalid"]:
                assert len(row) == 2 and row[1] in GROUP_BY_KINDS, (where, row)
            api = want["tenant_api"]
            assert api is None or set(api) == TENANT_API_KEYS, (where, api)
            if api is not None:
                assert api["put"] in ("403", "400", "503", "ok"), where
                batch = api["batch"]
                assert batch is None or (BATCH_KEYS <= set(batch) <= BATCH_KEYS | BATCH_OPTIONAL_KEYS
                                         and batch["verdict"] in ("ok", "policy_violation", "400")), where
                if batch is not None and "unset" in batch:
                    unset = batch["unset"]
                    # The cell models a request tenant-api accepts: a non-empty
                    # list of allowed keys, none repeated, none also patched.
                    assert (isinstance(unset, list) and unset and len(set(unset)) == len(unset)
                            and set(unset) <= BATCH_UNSET_KEYS
                            and not set(unset) & set(batch["patch"])), where
            differs = want["python_differs"]
            assert differs is None or (set(differs) == DIFFERS_KEYS and differs["reason"]), where
            if differs is not None:
                assert differs["refused"] is None or differs["refused"] in DIFFERS_REFUSED_KINDS, where
                assert differs["refused"] is None or differs["targets"] is None, where
                assert differs["tenant_api"] is None or set(differs["tenant_api"]) == TENANT_API_KEYS, where
            assert want["refused"] is None or want["refused"] in REFUSED_KINDS, where
            assert want["refused"] is None or want["targets"] is None, where
            esc = want["escalation"]
            assert esc is None or (set(esc) == ESCALATION_KEYS
                                   and esc["verdict"] in ESCALATION_VERDICTS
                                   and (esc["verdict"] == "compliant" or esc["leaks"] == [])), where


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


def _requiring_domains(policies: dict, tenant: str) -> list[str]:
    """Domains (name order, with repeats) whose `require_critical_escalation`
    is True and that list *tenant* — what check_domain_policies judges."""
    out = []
    for name, policy in sorted(policies.items()):
        if not isinstance(policy, dict):
            continue
        constraints = policy.get("constraints")
        tenants = policy.get("tenants")
        if (isinstance(constraints, dict) and isinstance(tenants, list)
                and constraints.get("require_critical_escalation") is True):
            out.extend(name for t in tenants if t == tenant)
    return out


def _escalation(tenant: str, rc: dict | None, domains: list[str],
                warnings: list[str]) -> dict | None:
    """The `escalation` cell, read back from the generator's strict lines."""
    lines: dict[str, dict] = {}
    for line in warnings:
        m = _ESC_MISSING.search(line)
        if m and m["tenant"] == tenant:
            lines.setdefault(m["domain"], {"verdict": "violation", "leaks": []})
            continue
        m = _ESC_LEAK.search(line)
        if m and m["tenant"] == tenant:
            ref = (m["ref"] or "receiver").replace("override[", "overrides[", 1)
            lines.setdefault(m["domain"], {"verdict": "compliant", "leaks": []})["leaks"].append(ref)
    if not domains or not isinstance(rc, dict) or not rc.get("receiver"):
        assert not lines, (tenant, lines)  # judged only under a requiring domain
        return None
    assert set(lines) <= set(domains), (tenant, lines, domains)
    cells = [lines.get(d, {"verdict": "compliant", "leaks": []}) for d in domains]
    assert all(c == cells[0] for c in cells), (tenant, cells)
    return cells[0]


def _want(want: dict, key: str):
    differs = want["python_differs"]
    return differs[key] if differs is not None else want[key]


# Blocking findings that are the domain policy's verdict (tenant-api: 403);
# any other blocking finding of the write is a 400-class refusal.
_POLICY_KINDS = {"domain_policy_violation", "critical_escalation_missing"}


def generator_findings(config_dir: Path) -> dict:
    """The generator's own verdict on a tree: the findings document of an
    in-process run of its `main()` with the conf.d-only arguments
    `--validate --strict` (as `make validate-routes` runs it) plus
    `--findings-json`. The CLI `--policy` file (a deployment's
    allowed_domains, which .github/workflows/validate.yaml passes) is out
    of scope by owner decision: see issue #2826. Nothing is
    reconstructed from load_tenant_tree — generation-stage findings
    (invalid_route_entry, missing_receiver_field …) and refusals are only
    there."""
    with tempfile.TemporaryDirectory() as out_dir:
        out = Path(out_dir) / "findings.json"
        argv = ["generate_alertmanager_routes.py", "--config-dir", str(config_dir),
                "--validate", "--strict", "--findings-json", str(out)]
        sink = io.StringIO()
        saved = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                gar.main()
        except SystemExit:
            pass
        finally:
            sys.argv = saved
        return json.loads(out.read_text(encoding="utf-8"))


def blocking_kinds(doc: dict, tenant: str) -> set:
    """The kinds of the in-force findings of a findings document (blocks in
    the run's in-force set, gar.in_force_blocks of its validate / strict
    fields) whose subject is the write of *tenant*'s own file: the finding
    names the tenant, or names the file the PUT writes (`<tenant>.yaml`,
    e.g. a tenant_file_unreadable). A run that failed for a reason it does
    not itemise (`run_failed`) blocks every write. A finding naming neither
    (a platform file's, a refusal summary) is no finding of the write."""
    in_force = gar.in_force_blocks(validate=doc["validate"], strict=doc["strict"])
    live = [f for f in doc["findings"] if f["blocks"] in in_force]
    if any(f["kind"] == "run_failed" for f in live):
        return {"run_failed"}
    return {f["kind"] for f in live if f["tenant"] == tenant or f["file"] == f"{tenant}.yaml"}


def _generator_put(want: dict, blocking: set) -> dict | None:
    """The tenant_api cell as the generator would judge that PUT (the
    tenant's own file verbatim): it refuses the write when an in-force
    finding of that run is the write's (`blocking`, from
    blocking_kinds) — 403 for the domain policy's (_POLICY_KINDS), else 400
    (any other kind: values_not_string, group_by_invalid,
    routing_not_mapping, invalid_route_entry, tenant_file_unreadable,
    run_failed …), whatever layer the value came from.

    Ranked against tenant-api's cell on whether each side refuses: the
    generator refusing what tenant-api answers 'ok' gives the generator's
    code; tenant-api answering 503 (POLICY_UNAVAILABLE: a policy file it
    cannot use) where the generator refuses nothing gives 'ok'; otherwise
    (both refuse, or neither) tenant-api's own cell: no difference.

    What stays out: the `batch` sub-cell is kept as tenant-api's — judging
    it would need the generator run on the tree with the patch laid over
    the block on disk, which this half does not build ([未驗] batch; the
    count of such cells is pinned by test_batch_cells_are_pinned)."""
    api = want["tenant_api"]
    if api is None:
        return None
    gen = None
    if blocking & _POLICY_KINDS:
        gen = "403"
    elif blocking:
        gen = "400"
    if gen is not None and api["put"] == "ok":
        return {**api, "put": gen}
    if gen is None and api["put"] == "503":
        return {**api, "put": "ok"}
    return api


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_python_reader_matches_the_table(tree, tmp_path: Path) -> None:
    _build(tree, tmp_path)
    got = load_tenant_tree(str(tmp_path), strict_policies=True)
    policies = _parse_config_files(str(tmp_path))["domain_policies"]
    doc = generator_findings(tmp_path)

    rows = _policy_rows(got.schema_warnings)
    unknown = {m["tenant"]: m["name"] for m in map(_UNKNOWN_PROFILE.search, got.schema_warnings) if m}
    refused = {m["tenant"]: "routing_not_mapping"
               for m in map(_ROUTING_NOT_MAPPING.search, got.schema_warnings) if m}
    refused.update({ast.literal_eval(m["repr"]): "invalid_tenant_id"
                    for m in map(_INVALID_TENANT_ID.search, got.schema_warnings) if m})
    assert set(refused) <= set(tree["expect"]), (tree["name"], refused)
    # A tenant file the generator cannot read refuses the whole tree (#1460,
    # rc 1, nothing rendered) — a reader difference (#2713) that only
    # python_differs can carry: every tenant's refusal is that one.
    unreadable = [f for f, _reason in got.tenant_file_errors]
    assert set(unreadable) <= set(tree["files"]), (tree["name"], got.tenant_file_errors)
    if unreadable:
        refused = dict.fromkeys(tree["expect"], "tenant_file_unreadable")
    for tenant, want in tree["expect"].items():
        where = (tree["name"], tenant)
        rc = got.routing_configs.get(tenant)
        assert _targets(rc) == _want(want, "targets"), (where, rc)
        assert _rejected(tenant, rc) == _want(want, "rejected_routes"), (where, rc)
        not_string = sorted(m["field"] for m in map(_NOT_STRING.search, got.schema_warnings)
                            if m and m["tenant"] == tenant)
        assert not_string == sorted(want["values_not_string"]), (where, not_string)
        group_by = [[m["field"], _GROUP_BY_WHY.get(m["why"], "not_string")]
                    for m in map(_GROUP_BY.search, got.schema_warnings)
                    if m and m["tenant"] == tenant]
        assert group_by == _want(want, "group_by_invalid"), (where, group_by)
        mine = sorted((d, r, c) for t, d, r, c in rows if t == tenant)
        assert mine == sorted(tuple(p) for p in _want(want, "policy")), (where, mine)
        assert unknown.get(tenant) == want["unknown_profile"], (where, unknown)
        assert refused.get(tenant) == _want(want, "refused"), (where, refused)
        esc = _escalation(tenant, rc, _requiring_domains(policies, tenant), got.schema_warnings)
        assert esc == want["escalation"], (where, esc)
        # Every measured tenant_api cell, python_differs or not: a verdict the
        # generator would give differently is a difference python_differs
        # must carry (and the catalog then accept), never an unrecorded one.
        api = _generator_put(want, blocking_kinds(doc, tenant))
        assert _want(want, "tenant_api") == api, (where, api)

    # Platform-file findings: the table's rows, and no other.
    got_platform = sorted(
        [["routing_defaults_routes_ignored", m["file"], "_routing_defaults.routes"]
         for m in map(_DEFAULTS_ROUTES.search, got.schema_warnings) if m]
        + [["routing_defaults_not_mapping", m["file"], "_routing_defaults"]
           for m in map(_DEFAULTS_NOT_MAPPING.search, got.schema_warnings) if m]
        # #2326: the tree findings travel as data on the tree, not as text.
        + [[kind, fname, fld] for kind, fname, fld, _msg in got.routing_tree_problems])
    # domain_policy_unusable (#2325) is compared on kind and field below.
    want_platform = sorted(r for r in tree["platform"]
                           if (r[0] not in GO_ONLY_PLATFORM_KINDS or _python_reports(r))
                           and r[0] != "domain_policy_unusable")
    assert got_platform == want_platform, (tree["name"], got_platform)
    got_unusable = sorted(
        [f"domain_policies.{m['domain']}.constraints.require_critical_escalation"
         for m in map(_ESC_NOT_BOOL.search, got.schema_warnings) if m]
        + ["domain_policies" if m["reason"].startswith("'domain_policies:' must be a mapping") else ""
           for m in map(_POLICY_FILE_DROPPED.search, got.schema_warnings) if m])
    want_unusable = sorted(r[2] for r in tree["platform"] if r[0] == "domain_policy_unusable")
    assert got_unusable == want_unusable, (tree["name"], got_unusable)

    # #2503: the enforced route(s)' group_by, in the generator's order (tenants
    # in name order, element index order). The file column is Go's alone.
    enforced = [[f"{m['ctx']}.{m['field']}", _GROUP_BY_WHY.get(m["why"], "not_string")]
                for m in map(_ENFORCED_GROUP_BY.search, got.schema_warnings) if m]
    assert enforced == [[f, k] for _file, f, k in tree["enforced_group_by_invalid"]], (
        tree["name"], enforced)

    # Nothing the table does not name: every routed tenant and every policy
    # line belongs to a listed tenant, and the line count is the table's.
    assert set(got.routing_configs) <= set(tree["expect"]), (tree["name"], set(got.routing_configs))
    # #2519: a `{{tenant}}` enforced route expands over every tenant, routed
    # or not — the Go halves judge expect's tenants, so expect lists them all.
    if got.enforced_routing:
        assert set(got.dedup_configs) <= set(tree["expect"]), (tree["name"], set(got.dedup_configs))
        # And the other way (#2519 F2): every valid tenant of expect is one the
        # generator loads — a null body it skips must not be listed, or the Go
        # halves (which judge expect's tenants) would judge a route never rendered.
        valid = {t for t in tree["expect"] if is_valid_tenant_id(t)}
        assert valid <= set(got.dedup_configs), (tree["name"], valid - set(got.dedup_configs))
    expected_total = sum(len(_want(w, "policy")) for w in tree["expect"].values())
    assert len(rows) == expected_total, (tree["name"], rows)
    assert Counter(t for t, *_ in rows).keys() <= set(tree["expect"]), (tree["name"], rows)
    not_string_tenants = {m["tenant"] for m in map(_NOT_STRING.search, got.schema_warnings) if m}
    assert not_string_tenants <= set(tree["expect"]), (tree["name"], not_string_tenants)
    group_by_tenants = {m["tenant"] for m in map(_GROUP_BY.search, got.schema_warnings) if m}
    assert group_by_tenants <= set(tree["expect"]), (tree["name"], group_by_tenants)
