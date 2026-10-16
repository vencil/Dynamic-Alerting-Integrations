"""Python half of tests/shared/routing_policy_parity_matrix.json (#2280).

The route generator's reader is the SSOT for how a tenant's routing resolves
(`_routing_defaults` -> routing profile -> tenant `_routing`) and for which
receiver types an ADR-007 domain policy rejects. The Go readers
(`pkg/routingpolicy`, da-guard) assert the same table; none of the three
reads another's source (tenant-api left the readers with #2903: it no
longer judges routing or policy itself).

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
  (#2326) its routing-tree findings, `TenantTree.routing_tree_problems` —
  `routing_in_unread_location` (#2291) and `routing_profiles_unusable`
  included since #2904, when the generator took both over from da-guard;
* `route_findings` (#2904, per tree) — the generator's own findings document
  (`--validate --strict --findings-json`) rows [tenant, kind, field] for the
  `route_finding_kinds` (receiver contract, override matcher, dead
  overrides), against da-guard's findings of the same kinds; a tree's
  `route_findings_python_differs` replaces the Go rows where they disagree,
  with the reason;
* `go_severity` (#2904) — every generator finding of a kind da-guard reports
  as an error is in force under `--validate --strict` (never looser than
  da-guard).

`test_no_routing_check_is_go_only` (#2904, ADR-036 §9) is the gate for
deleting da-guard's routing checks: the routing / policy finding kinds are
the ones registered on the routing plane in internal/guard/findingkind (the
closed set of da-guard's kinds; internal/guard/finding_kind_gate_test.go,
run by `go_finding_kinds`, writes them out), and every one must be pinned
against the generator by this half.

A cell's `python_differs` replaces the Go value where the YAML readers
disagree (see the matrix `_comment`): `targets`, `policy`, `rejected_routes`,
`group_by_invalid` and `refused` are asserted against it here. A tree
the generator refuses whole because a tenant file cannot be read
(`tenant_file_errors`, #1460: rc 1, nothing rendered) is one only through
that: every tenant's `python_differs.refused` is `tenant_file_unreadable`.
"""
from __future__ import annotations

import ast
import contextlib
import functools
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_merge import FINDING_KINDS  # noqa: E402
from _grar_parse import BLOCKING_TREE_KINDS, _parse_config_files, load_tenant_tree  # noqa: E402
from _grar_validate import list_tenant_subroutes, route_entry_matchers  # noqa: E402
from _lib_validation import is_valid_tenant_id  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

MATRIX = json.loads((Path(__file__).parent / "routing_policy_parity_matrix.json")
                    .read_text(encoding="utf-8"))

# Exact key sets: a misspelt key read as absent would turn a row into one
# that tests nothing while staying green.
TOP_KEYS = {"_comment", "blocking_kinds", "route_finding_kinds", "go_severity", "tenant_ids",
            "trees"}
TREE_KEYS = {"name", "files", "platform", "expect", "enforced_group_by_invalid", "route_findings",
             "route_findings_python_differs"}
PLATFORM_KINDS = {"routing_defaults_routes_ignored", "routing_in_unread_location",
                  "domain_policy_unusable",
                  # #2326: the hierarchical routing plane's tree findings.
                  "routing_enforced_below_root", "routing_defaults_null_below_root",
                  "routing_profile_duplicate", "duplicate_tenant",
                  "domain_policy_out_of_scope",
                  # #2341: a `_routing_defaults` that is not a mapping.
                  "routing_defaults_not_mapping",
                  # #2960: a key in a profile / `_routing_defaults` /
                  # `_routing_enforced` the generator does not read.
                  "routing_key_ignored",
                  # #2989: a `_routing_enforced.match` that is not a list.
                  "routing_enforced_match_invalid",
                  # #2904: a `routing_profiles:` block that is not a mapping.
                  "routing_profiles_unusable"}
ROUTE_FINDING_KINDS = set(MATRIX["route_finding_kinds"])
GO_SEVERITY = MATRIX["go_severity"]
EXPECT_KEYS = {"targets", "policy", "rejected_routes", "values_not_string",
               "group_by_invalid", "unknown_profile",
               "python_differs", "escalation", "refused"}
REFUSED_KINDS = {"routing_not_mapping", "invalid_tenant_id"}
GROUP_BY_KINDS = {"not_string", "empty", "duplicate", "wildcard_mixed"}
ESCALATION_KEYS = {"verdict", "leaks"}
ESCALATION_VERDICTS = {"compliant", "violation"}
DIFFERS_KEYS = {"reason", "targets", "policy", "rejected_routes", "group_by_invalid", "refused"}
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
                     "group-by-elements", "routing-unset-reenable",
                     # #2904: the checks moved from da-guard.
                     "override-and-receiver-findings-2904",
                     "routing-profiles-not-a-mapping-2904",
                     "routing-in-unread-location-nested-2904",
                     "unread-location-wrapped-non-string-key-2904",
                     "unread-location-non-string-key-elsewhere-2904",
                     "override-not-a-mapping-2904"):
        assert required in names, required


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        assert set(tree) == TREE_KEYS, (tree.get("name"), set(tree) ^ TREE_KEYS)
        for row in tree["platform"]:
            assert len(row) == 3 and row[0] in PLATFORM_KINDS, (tree["name"], row)
        for row in tree["enforced_group_by_invalid"]:
            assert len(row) == 3 and row[2] in GROUP_BY_KINDS, (tree["name"], row)
        differs = tree["route_findings_python_differs"]
        assert differs is None or (set(differs) == {"reason", "rows"} and str(differs["reason"]).strip()
                                   and differs["rows"] != tree["route_findings"]), tree["name"]
        # A differs row may name the generator's own kind for what da-guard
        # calls one of route_finding_kinds (#2904 N4): any kind of its
        # catalog.
        for rows, kinds in [(tree["route_findings"], ROUTE_FINDING_KINDS)] + (
                [(differs["rows"], set(FINDING_KINDS))] if differs else []):
            assert rows == sorted(rows), tree["name"]
            for row in rows:
                assert (len(row) == 3 and all(isinstance(c, str) for c in row)
                        and row[1] in kinds), (tree["name"], row)
        for tenant, want in tree["expect"].items():
            where = (tree["name"], tenant)
            assert set(want) == EXPECT_KEYS, (where, set(want) ^ EXPECT_KEYS)
            for row in want["policy"]:
                assert len(row) == 3 and row[2] in CONSTRAINTS, (where, row)
            for row in want["group_by_invalid"]:
                assert len(row) == 2 and row[1] in GROUP_BY_KINDS, (where, row)
            differs = want["python_differs"]
            assert differs is None or (set(differs) == DIFFERS_KEYS and differs["reason"]), where
            if differs is not None:
                assert differs["refused"] is None or differs["refused"] in DIFFERS_REFUSED_KINDS, where
                assert differs["refused"] is None or differs["targets"] is None, where
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


def _differs_kinds(tree: dict) -> set[str]:
    """The generator-only kinds a tree's route_findings_python_differs names
    (#2904 N4: the generator's name for what da-guard reports under one of
    route_finding_kinds, e.g. invalid_override_entry for an override that
    is not a mapping)."""
    differs = tree["route_findings_python_differs"]
    return {r[1] for r in differs["rows"]} - ROUTE_FINDING_KINDS if differs else set()


def route_rows(doc: dict, extra_kinds: set[str] = frozenset()) -> list[list[str]]:
    """The `route_findings` rows of a findings document: [tenant, kind,
    field] per finding of a `route_finding_kinds` kind, or of *extra_kinds*
    ("" for an absent tenant / field), sorted."""
    kinds = ROUTE_FINDING_KINDS | extra_kinds
    return sorted([f["tenant"] or "", f["kind"], f["field"] or ""]
                  for f in doc["findings"] if f["kind"] in kinds)


def severity_floor_errors(doc: dict, stand_ins: set[str] = frozenset()) -> list[str]:
    """The findings of *doc* whose kind da-guard reports as an error
    (`go_severity`) — or that stand in for one (*stand_ins*: the generator
    kinds a tree's differs rows name in place of an `error` row) — but which
    are not in force in the run (the generator would let pass what da-guard
    blocks). A `warn` kind may be anything: the row comparison already pins
    that it is reported."""
    in_force = gar.in_force_blocks(validate=doc["validate"], strict=doc["strict"])
    return [f"{f['kind']} {f['tenant']} {f['file']} {f['field']}: blocks={f['blocks']}"
            for f in doc["findings"]
            if (GO_SEVERITY.get(f["kind"]) == "error" or f["kind"] in stand_ins)
            and f["blocks"] not in in_force]


# ── #2904 (ADR-036 §9): which da-guard routing checks only Go has ─────────
# The da-guard finding kinds and their plane (routing / threshold) come from
# Go. A kind is a findingkind.Kind (internal/guard/findingkind): a struct
# with one unexported field, whose only constructor registers it with its
# plane and a reason, so the registry is every kind there can be and no kind
# exists unclassified. internal/guard/finding_kind_gate_test.go adds the
# files of the guard package that use each kind (a kind routing.go uses must
# be routing; a registered kind no file uses is red) and, run with
# DA_GUARD_EMIT_KINDS=<path>, writes {kind: {"plane", "files"}} there; that
# table is what this half reads.
#
# Earlier ways of listing the kinds were each beaten: a regex over the Go
# source here, an AST rule limiting kinds to one written shape, a go/types
# computation of every constant (a kind made at run time, or in a file
# go/build skips, was invisible to it), and a check at Run's exit. See the
# findingkind package comment.
GO_APP = REPO_ROOT / "components" / "threshold-exporter" / "app"
GUARD_KINDS_TEST = "TestFindingKindPlanes"
PLANES = {"routing", "threshold"}
_HAS_GO = shutil.which("go") is not None
_REQUIRE_GO = os.environ.get("VIBE_REQUIRE_GO") == "1"


@functools.lru_cache(maxsize=1)
def _go_finding_kinds() -> dict[str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "kinds.json"
        proc = subprocess.run(
            ["go", "test", "./internal/guard/", "-run", f"^{GUARD_KINDS_TEST}$", "-count=1"],
            cwd=GO_APP, env={**os.environ, "DA_GUARD_EMIT_KINDS": str(out)},
            capture_output=True, text=True, encoding="utf-8", timeout=600, check=False)
        assert proc.returncode == 0, (
            f"go test -run {GUARD_KINDS_TEST} (rc {proc.returncode}):\n{proc.stdout}{proc.stderr}")
        # rc 0 with no file: -run matched nothing, or the test stopped writing.
        assert out.is_file(), f"{GUARD_KINDS_TEST} passed but wrote no kind table:\n{proc.stdout}"
        return json.loads(out.read_text(encoding="utf-8"))


def go_finding_kinds() -> dict[str, dict]:
    """{kind: {"plane": "routing" | "threshold", "files": [...]}}, from
    da-guard's own test (see above). Without `go` this skips — or fails
    when VIBE_REQUIRE_GO=1 (CI's Python test jobs set Go up), so a lost Go
    setup is never a gate that silently stopped looking."""
    if not _HAS_GO:
        if _REQUIRE_GO:
            pytest.fail("VIBE_REQUIRE_GO=1 but `go` is not on PATH: the da-guard finding kinds "
                        f"come from `go test ./internal/guard/ -run {GUARD_KINDS_TEST}`")
        pytest.skip(f"go not on PATH: the da-guard finding kinds come from `go test "
                    f"./internal/guard/ -run {GUARD_KINDS_TEST}` (set up Go, or "
                    "VIBE_REQUIRE_GO=1 to fail instead)")
    return _go_finding_kinds()


def routing_kinds(kinds: dict[str, dict]) -> set[str]:
    return {k for k, v in kinds.items() if v.get("plane") == "routing"}


def unclassified_guard_kinds(kinds: dict[str, dict]) -> list[str]:
    """Kinds in the table on neither plane. The Go test refuses to write
    such a table; this keeps a reader of it from treating a plane it does
    not know as "not routing"."""
    return sorted(k for k, v in kinds.items() if v.get("plane") not in PLANES)


# The da-guard kinds this half pins through a dedicated column (the column ->
# the kind its Go half reads). Not a list of routing kinds: what decides
# whether a kind is routing is go_finding_kinds.
COLUMN_KINDS = {
    "rejected_routes": {"invalid_route_entry"},
    "values_not_string": {"routing_value_not_string"},
    "group_by_invalid / enforced_group_by_invalid": {"routing_group_by_invalid"},
    "policy": {"domain_policy_violation"},
    "escalation": {"critical_escalation_missing", "critical_escalation_leak"},
    "unknown_profile": {"unknown_routing_profile"},
    "refused": REFUSED_KINDS,
}


def pinned_kinds(matrix: dict) -> set[str]:
    """Every da-guard kind this half compares with the generator: the
    dedicated columns, every platform kind, and `route_finding_kinds`."""
    out = set().union(*COLUMN_KINDS.values()) | PLATFORM_KINDS | set(matrix["route_finding_kinds"])
    return out


def go_only_routing_kinds(matrix: dict, kinds: dict[str, dict] | None = None) -> list[str]:
    kinds = go_finding_kinds() if kinds is None else kinds
    return sorted(routing_kinds(kinds) - pinned_kinds(matrix))


def test_every_guard_kind_is_classified() -> None:
    """#2904 review N2: every da-guard finding kind is routing / policy (the
    gate below holds it to the generator) or threshold (stays in da-guard).
    Deciding which is done in Go, where a kind is registered with its plane
    (internal/guard/findingkind); here: the table that came back is whole."""
    kinds = go_finding_kinds()
    left = unclassified_guard_kinds(kinds)
    assert not left, f"da-guard kinds on neither plane: {left}"
    assert all(v.get("files") for v in kinds.values()), kinds


def test_unclassified_kind_is_red() -> None:
    """A table entry on no known plane is unclassified, not "threshold"."""
    kinds = {"cardinality_exceeded": {"plane": "threshold", "files": ["types.go"]},
             "domain_policy_violation": {"plane": "routing", "files": ["routing.go"]}}
    assert unclassified_guard_kinds(kinds) == []
    kinds["zz_new_check"] = {"plane": "policy", "files": ["zz_new.go"]}
    kinds["zz_no_plane"] = {"files": ["zz_new.go"]}
    assert unclassified_guard_kinds(kinds) == ["zz_new_check", "zz_no_plane"]


def test_routing_kinds_are_read_from_the_go_source() -> None:
    """Not vacuous, and the split is the one da-guard makes: both planes
    non-empty, and every kind this half pins exists in da-guard."""
    kinds = go_finding_kinds()
    routing = routing_kinds(kinds)
    assert routing and set(kinds) - routing, kinds
    assert "domain_policy_violation" in routing and "cardinality_exceeded" not in routing
    unknown = pinned_kinds(MATRIX) - set(kinds)
    assert not unknown, f"pinned kinds da-guard does not have: {sorted(unknown)}"


def test_no_routing_check_is_go_only() -> None:
    """ADR-036 §9: before a da-guard routing check is deleted (#2903,
    #2905), the generator has it and this table pins the two against each
    other. Red while any routing kind da-guard emits is pinned by no column."""
    left = go_only_routing_kinds(MATRIX)
    assert not left, (
        f"da-guard routing / policy kinds the generator is not pinned against: {left} — implement "
        "each in the generator and add it to tests/shared/routing_policy_parity_matrix.json "
        "(a column, `platform` rows or `route_finding_kinds`) before deleting the Go check")


def test_go_only_gate_is_red() -> None:
    """A routing kind the Go table reports and no column pins is red; a
    threshold one is not; a kind dropped from route_finding_kinds is Go-only
    again."""
    kinds = {k: dict(v) for k, v in go_finding_kinds().items()}
    assert go_only_routing_kinds(MATRIX, kinds) == []
    kinds["zz_new_routing"] = {"plane": "routing", "files": ["routing.go", "types.go"]}
    kinds["zz_new_threshold"] = {"plane": "threshold", "files": ["types.go"]}
    assert go_only_routing_kinds(MATRIX, kinds) == ["zz_new_routing"]
    m = json.loads(json.dumps(MATRIX))
    m["route_finding_kinds"].remove("duplicate_override_matcher")
    assert go_only_routing_kinds(m) == ["duplicate_override_matcher"]


def test_route_finding_kinds_are_exercised() -> None:
    """Every kind the gate counts as pinned through `route_findings` /
    `go_severity` occurs in some tree, on both sides — else the row
    comparison would pin "never reported" only."""
    assert ROUTE_FINDING_KINDS <= set(GO_SEVERITY), ROUTE_FINDING_KINDS - set(GO_SEVERITY)
    assert set(GO_SEVERITY.values()) <= {"error", "warn"}
    go_seen = {r[1] for t in MATRIX["trees"] for r in t["route_findings"]}
    go_seen |= {r[0] for t in MATRIX["trees"] for r in t["platform"]}
    py_seen = {r[1] for t in MATRIX["trees"]
               for r in (t["route_findings_python_differs"] or {"rows": t["route_findings"]})["rows"]}
    py_seen |= {r[0] for t in MATRIX["trees"] for r in t["platform"]}
    assert set(GO_SEVERITY) <= go_seen, set(GO_SEVERITY) - go_seen
    assert set(GO_SEVERITY) <= py_seen, set(GO_SEVERITY) - py_seen


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

    # Platform-file findings: the table's rows, and no other.
    got_platform = sorted(
        [["routing_defaults_routes_ignored", m["file"], "_routing_defaults.routes"]
         for m in map(_DEFAULTS_ROUTES.search, got.schema_warnings) if m]
        + [["routing_defaults_not_mapping", m["file"], "_routing_defaults"]
           for m in map(_DEFAULTS_NOT_MAPPING.search, got.schema_warnings) if m]
        # #2960: read off the finding's own fields, never its text.
        + [["routing_key_ignored", w.file, w.field] for w in got.schema_warnings
           if getattr(w, "kind", None) == "routing_key_ignored" and w.tenant is None]
        # #2989: an enforced `match` that is not a list, off the finding's fields.
        + [["routing_enforced_match_invalid", w.file, w.field] for w in got.schema_warnings
           if getattr(w, "kind", None) == "routing_enforced_match_invalid"]
        # #2326: the tree findings travel as data on the tree, not as text.
        + [[kind, fname, fld] for kind, fname, fld, _msg in got.routing_tree_problems])
    # domain_policy_unusable (#2325) is compared on kind and field below.
    want_platform = sorted(r for r in tree["platform"] if r[0] != "domain_policy_unusable")
    assert got_platform == want_platform, (tree["name"], got_platform)
    got_unusable = sorted(
        [f"domain_policies.{m['domain']}.constraints.require_critical_escalation"
         for m in map(_ESC_NOT_BOOL.search, got.schema_warnings) if m]
        + ["domain_policies" if m["reason"].startswith("'domain_policies:' must be a mapping") else ""
           for m in map(_POLICY_FILE_DROPPED.search, got.schema_warnings) if m])
    want_unusable = sorted(r[2] for r in tree["platform"] if r[0] == "domain_policy_unusable")
    assert got_unusable == want_unusable, (tree["name"], got_unusable)

    # #2904: the receiver / override findings, from the generator's own
    # findings document, against da-guard's rows (or the tree's recorded
    # difference); and no finding of a kind da-guard reports as an error is
    # looser in the generator than "in force under --validate --strict".
    stand_ins = _differs_kinds(tree)
    got_route = route_rows(doc, stand_ins)
    differs = tree["route_findings_python_differs"]
    assert got_route == (differs["rows"] if differs else tree["route_findings"]), (
        tree["name"], got_route)
    # A generator kind standing in for a da-guard `error` row is held to the
    # same floor (#2904 N4).
    if not any(GO_SEVERITY[r[1]] == "error" for r in tree["route_findings"]):
        stand_ins = set()
    loose = severity_floor_errors(doc, stand_ins)
    assert not loose, (tree["name"], loose)

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
