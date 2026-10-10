"""Reader-divergence catalog: every Go != generator verdict is accepted, once.

ADR-036 step 2. The route generator's reader (PyYAML) is the authority; no Go
reader may be looser. This test is the one place that ranks each difference
and matches it against tests/shared/reader_divergence_catalog.yaml, for two
layers:

Policy layer. The Go corpus tests write the rows where a Go reader's verdict
on a `_domain_policy.yaml` differs from PyYAML's (`_parse_config_files`) to
tests/shared/merge_key_go_verdicts.json (per reader, keyed by corpus row
`id`).

- direction() computes each snapshot row's direction from the rules each
  side actually enforces (go_looser / go_stricter / value_differs); a file
  the generator blocks on as production runs it (`--validate --strict`, the
  row's `pyyaml_strict_blocks`) counts as unusable, so a Go reader that
  neither refuses it nor reports a blocking problem is looser;
- classify(): tenant-api refusing a file the generator reads is `go_refuses`,
  not go_stricter — the watcher then serves that file's last good content
  (or none), and with none --policy-unavailable-open lets writes through;
- each entry pins its exact row count and an example row.

Routing layer. tests/shared/routing_policy_parity_matrix.json carries, per
tree and tenant, the Go readers' values and — where the YAML readers
disagree — the generator's in `python_differs` (asserted by
tests/shared/test_routing_policy_parity.py). routing_differences() turns
every field where they differ into one difference per Go half that asserts
that field (FIELD_READERS), ranked by routing_direction(); an entry lists
its cells as [tree, tenant, field], so one entry never covers another field
of the same tree.

Both layers:
- every difference must match exactly one entry, of the same direction; an
  entry (or a listed row / cell) that matches nothing — the difference
  healed — is red, as is an uncatalogued difference;
- go_looser and value_differs entries list their rows / cells one by one
  and stay `fix-pending`: they block ADR-036's phase 2 (phase2_blockers());
  go_stricter and go_refuses entries are `accept-document` with a sign_off;
- expire_at: an expired entry warns; with READER_DIVERGENCE_STRICT_EXPIRY=1
  (`make pre-tag` sets it) it fails. READER_DIVERGENCE_TODAY=YYYY-MM-DD
  injects "today".
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import warnings
from collections import Counter
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
CORPUS_PATH = HERE / "merge_key_policy_corpus.json"
SNAPSHOT_PATH = HERE / "merge_key_go_verdicts.json"
CATALOG_PATH = HERE / "reader_divergence_catalog.yaml"
MATRIX_PATH = HERE / "routing_policy_parity_matrix.json"

# Policy layer: the snapshot's readers. Routing layer: the matrix's Go
# halves — pkg/routingpolicy's (the package da-guard and tenant-api share),
# da-guard's CLI and tenant-api's handler.
READERS = ("da-guard", "tenant-api")
ROUTING_READERS = ("routingpolicy", "da-guard", "tenant-api")
LAYERS = ("policy", "routing")
DIRECTIONS = ("go_looser", "go_stricter", "go_refuses", "value_differs")
DISPOSITIONS = ("accept-document", "fix-pending")
# Directions that block ADR-036's phase 2: they must be listed row by row.
BLOCKING = ("go_looser", "value_differs")
# Directions an owner may accept (sign_off): Go blocks where the generator
# does not. go_refuses is tenant-api's alone (a policy file it refuses, or
# a PUT it answers 503 POLICY_UNAVAILABLE for one).
ACCEPTABLE = ("go_stricter", "go_refuses")
REQUIRED = ("id", "reader", "layer", "match", "mechanism", "direction", "disposition", "ref",
            "expire_at", "count")
OPTIONAL = ("sign_off", "example")
# The `python_differs` fields of the routing matrix and the Go halves that
# assert each against the Go column (tenant_api's model in pkg/routingpolicy
# is a consistency check of the table; the handler is tenant-api's reader).
FIELD_READERS = {
    "targets": ("routingpolicy",),
    "policy": ("routingpolicy", "da-guard"),
    "rejected_routes": ("routingpolicy", "da-guard"),
    "group_by_invalid": ("routingpolicy", "da-guard"),
    "refused": ("routingpolicy", "da-guard"),
    "tenant_api": ("tenant-api",),
}
_ROW_ID = re.compile(r"^[0-9a-f]{16}$")
_ISSUE_URL = re.compile(r"^https://github\.com/vencil/Dynamic-Alerting-Integrations/(issues|pull)/\d+$")
# A sign-off is a comment (or the ticket itself while one is pending): only
# its format can be checked, never that the owner wrote it.
_SIGN_OFF_URL = re.compile(
    r"^https://github\.com/vencil/Dynamic-Alerting-Integrations/(issues|pull)/\d+(#issuecomment-\d+)?$")

STRICT_EXPIRY_ENV = "READER_DIVERGENCE_STRICT_EXPIRY"
TODAY_ENV = "READER_DIVERGENCE_TODAY"


# --- direction ----------------------------------------------------------------

def _tenant_rules(verdict: dict) -> dict:
    """Per tenant, the rules a verdict enforces: (forbidden types, allowed
    types or None for no allowlist, escalation required). A tenant in
    several domains gets all of them: forbidden lists add up, allowlists
    intersect."""
    out: dict = {}
    for dom in verdict.values():
        for tenant in dom["tenants"]:
            forbidden, allowed, escalation = out.get(tenant, (frozenset(), None, False))
            forbidden = forbidden | frozenset(dom["forbidden"])
            if "allowed" in dom:
                mine = frozenset(dom["allowed"])
                allowed = mine if allowed is None else allowed & mine
            out[tenant] = (forbidden, allowed, escalation or dom.get("escalation") is True)
    return out


_NO_RULES = (frozenset(), None, False)


def _misses(want: tuple, have: tuple) -> bool:
    """True when `have` lets through something `want` stops.

    A receiver type is disallowed when it is forbidden, or when an allowlist
    exists and does not name it. `have` misses a rule of `want` when some
    type `want` disallows is allowed by `have` — a type forbidden by `want`
    only; with `want`'s allowlist, any type `have`'s allowlist adds (or, with
    no allowlist in `have`, a type nobody names) — or when `want` requires
    escalation and `have` does not."""
    f_want, a_want, e_want = want
    f_have, a_have, e_have = have

    def have_allows(t: str) -> bool:
        return t not in f_have and (a_have is None or t in a_have)

    if any(have_allows(t) for t in f_want):
        return True
    if a_want is not None:
        if a_have is None:
            return True  # a type no list names: `want` stops it, `have` does not
        if any(t not in a_want and t not in f_have for t in a_have):
            return True
    return e_want and not e_have


def effective_pyyaml(row: dict) -> object:
    """The verdict a Go reader is held to: PyYAML's, or "unusable" when the
    generator reads the file but blocks on it as production runs it
    (`--validate --strict`: the row's `pyyaml_strict_blocks`)."""
    return "unusable" if row.get("pyyaml_strict_blocks") else row["pyyaml"]


def direction(pyyaml: object, go: object, strict_blocks: bool = False) -> str:
    """Which way a Go verdict leans from PyYAML's, by enforced-rule inclusion.

    Both are "unusable" or {domain: {tenants, forbidden, allowed?,
    escalation?}} (the corpus's verdict shape). Go's "unusable" means it
    blocks: refuses the file, or (da-guard) reports a Problem that fails its
    run. `strict_blocks`: the generator reads the file but blocks on it
    under --validate --strict (`pyyaml_strict_blocks`) — as good as unusable.
    - The generator blocks (unusable, or strict_blocks) and Go does not
      (whatever it enforces, `{}` included): looser — production refuses
      the file, Go lets it through. Both blocking is no difference.
    - Go blocks and the generator does not: stricter.
    - Both read: Go enforcing less for some tenant is looser, more is
      stricter; both at once is value_differs. A difference that changes no
      enforced rule cannot be ranked and is value_differs too (blocking).
    """
    if strict_blocks:
        pyyaml = "unusable"
    if pyyaml == go:
        raise ValueError("the verdicts are equal: no direction")
    if pyyaml == "unusable":
        return "go_looser"
    if go == "unusable":
        return "go_stricter"
    py_rules, go_rules = _tenant_rules(pyyaml), _tenant_rules(go)
    tenants = set(py_rules) | set(go_rules)
    looser = any(_misses(py_rules.get(t, _NO_RULES), go_rules.get(t, _NO_RULES)) for t in tenants)
    stricter = any(_misses(go_rules.get(t, _NO_RULES), py_rules.get(t, _NO_RULES)) for t in tenants)
    if looser and not stricter:
        return "go_looser"
    if stricter and not looser:
        return "go_stricter"
    return "value_differs"


def _dom(tenants, forbidden=(), allowed=None, escalation=False) -> dict:
    d: dict = {"tenants": list(tenants), "forbidden": list(forbidden)}
    if allowed is not None:
        d["allowed"] = list(allowed)
    if escalation:
        d["escalation"] = True
    return d


@pytest.mark.parametrize("py, go, want", [
    # Unusable on either side.
    ("unusable", {"fin": _dom(["t1"], ["slack"])}, "go_looser"),
    ("unusable", {}, "go_looser"),
    ({"fin": _dom(["t1"], ["slack"])}, "unusable", "go_stricter"),
    ({}, "unusable", "go_stricter"),
    # Forbidden: fewer forbidden is looser, more is stricter.
    ({"fin": _dom(["t1"], ["slack", "email"])}, {"fin": _dom(["t1"], ["slack"])}, "go_looser"),
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1"], ["slack", "email"])}, "go_stricter"),
    ({"fin": _dom(["t1"], ["slack"])}, {}, "go_looser"),
    ({}, {"fin": _dom(["t1"], ["slack"])}, "go_stricter"),
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1"], ["email"])}, "value_differs"),
    # Tenants: a domain applying to fewer tenants is looser.
    ({"fin": _dom(["t1", "t2"], ["slack"])}, {"fin": _dom(["t1"], ["slack"])}, "go_looser"),
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1", "t2"], ["slack"])}, "go_stricter"),
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t2"], ["slack"])}, "value_differs"),
    # The domain's name is no rule: the same rules under another name.
    ({"fin": _dom(["t1"], ["slack"])}, {"ops": _dom(["t1"], ["slack"])}, "value_differs"),
    # Allowed: a restriction. Missing it, or allowing more, is looser.
    ({"fin": _dom(["t1"], allowed=["email"])}, {"fin": _dom(["t1"])}, "go_looser"),
    ({"fin": _dom(["t1"], allowed=["email"])}, {}, "go_looser"),
    ({"fin": _dom(["t1"], allowed=["email"])}, {"fin": _dom(["t1"], allowed=["email", "slack"])}, "go_looser"),
    ({"fin": _dom(["t1"], allowed=["email", "slack"])}, {"fin": _dom(["t1"], allowed=["email"])}, "go_stricter"),
    ({"fin": _dom(["t1"])}, {"fin": _dom(["t1"], allowed=["email"])}, "go_stricter"),
    ({"fin": _dom(["t1"], allowed=["email"])}, {"fin": _dom(["t1"], allowed=[])}, "go_stricter"),
    ({"fin": _dom(["t1"], allowed=[])}, {"fin": _dom(["t1"], allowed=["email"])}, "go_looser"),
    ({"fin": _dom(["t1"], allowed=["email"])}, {"fin": _dom(["t1"], allowed=["slack"])}, "value_differs"),
    # Allowing a type the other side forbids anyway changes nothing.
    ({"fin": _dom(["t1"], ["slack"], allowed=["email"])},
     {"fin": _dom(["t1"], ["slack"], allowed=["email", "slack"])}, "value_differs"),
    # ...whereas forbidding a type the allowlist already stops is no rule
    # the other side misses.
    ({"fin": _dom(["t1"], ["slack"], allowed=["email"])},
     {"fin": _dom(["t1"], [], allowed=["email"])}, "value_differs"),
    # Escalation: Py true / Go false is looser, the reverse stricter.
    ({"fin": _dom(["t1"], ["slack"], escalation=True)}, {"fin": _dom(["t1"], ["slack"])}, "go_looser"),
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1"], ["slack"], escalation=True)}, "go_stricter"),
    # Escalation alone (no receiver-type list) is a rule like any other.
    ({"fin": _dom(["t1"], escalation=True)}, {}, "go_looser"),
    ({"fin": _dom(["t1", "t2"], escalation=True)}, {"fin": _dom(["t1"], escalation=True)}, "go_looser"),
    ({"fin": _dom(["t1"], escalation=True)}, {"fin": _dom(["t1"])}, "go_looser"),
    ({}, {"fin": _dom(["t1"], escalation=True)}, "go_stricter"),
    ({"fin": _dom(["t1"], escalation=True)}, {"fin": _dom(["t1", "t2"], escalation=True)}, "go_stricter"),
    ({"fin": _dom(["t1"], escalation=True)}, {"fin": _dom(["t2"], escalation=True)}, "value_differs"),
    # Mixed: looser for one tenant, stricter for another.
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t2"], ["slack", "email"]), "o": _dom(["t1"])},
     "value_differs"),
    ({"fin": _dom(["t1"], ["slack"], escalation=True)}, {"fin": _dom(["t1"], ["slack", "email"])},
     "value_differs"),
    # Several domains for one tenant: forbidden adds up, allowlists intersect.
    ({"a": _dom(["t1"], ["slack"]), "b": _dom(["t1"], ["email"])},
     {"a": _dom(["t1"], ["slack", "email"])}, "value_differs"),
    ({"a": _dom(["t1"], allowed=["email", "slack"]), "b": _dom(["t1"], allowed=["email"])},
     {"a": _dom(["t1"], allowed=["email", "slack"])}, "go_looser"),
])
def test_direction(py, go, want) -> None:
    assert direction(py, go) == want


@pytest.mark.parametrize("py, go, strict_blocks, want", [
    # The generator reads the file but blocks under --strict; Go reads
    # nothing and reports nothing: production refuses, Go permits.
    ({}, {}, True, "go_looser"),
    # ...or Go reads the same policies (no blocking problem): still looser.
    ({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1"], ["slack"])}, True, "go_looser"),
    # #2759 F: the generator drops `fin` (a `!!set`, strict ERROR), Go
    # enforces it: looser, although Go enforces more rules.
    ({}, {"fin": _dom(["t1"], ["slack"])}, True, "go_looser"),
    # The same Go verdict without the strict block: Go only enforces more.
    ({}, {"fin": _dom(["t1"], ["slack"])}, False, "go_stricter"),
    # Go blocks (refuses, or a blocking Problem) and the generator does not.
    ({"fin": _dom(["t1"], ["slack"])}, "unusable", False, "go_stricter"),
])
def test_direction_with_strict_blocks(py, go, strict_blocks, want) -> None:
    assert direction(py, go, strict_blocks) == want


def test_both_blocking_is_no_difference() -> None:
    with pytest.raises(ValueError):
        direction({"fin": _dom(["t1"], ["slack"])}, "unusable", strict_blocks=True)
    with pytest.raises(ValueError):
        direction("unusable", "unusable")


def test_direction_refuses_equal_verdicts() -> None:
    with pytest.raises(ValueError):
        direction({"fin": _dom(["t1"], ["slack"])}, {"fin": _dom(["t1"], ["slack"])})


def classify(reader: str, row: dict, go: object) -> str:
    """A snapshot row's direction for `reader`.

    tenant-api refusing ("unusable") a file the generator reads — and does
    not block on under --strict — is `go_refuses`, not go_stricter: the
    watcher keeps serving that file's last good content, which may be a
    stale policy, and with none --policy-unavailable-open (if set) lets the
    writes the policy judges through with no policy at all. Everything else
    is direction(). Equal verdicts (both blocking included) raise."""
    if reader == "tenant-api" and go == "unusable":
        if effective_pyyaml(row) == "unusable":
            raise ValueError("both refuse: no direction")
        return "go_refuses"
    return direction(row["pyyaml"], go, bool(row.get("pyyaml_strict_blocks")))


_READ = {"id": "r", "pyyaml": {"fin": _dom(["t1"], ["slack"])}}


@pytest.mark.parametrize("reader, row, go, want", [
    ("tenant-api", _READ, "unusable", "go_refuses"),
    ("da-guard", _READ, "unusable", "go_stricter"),
    # A tenant-api read is ranked like any other.
    ("tenant-api", _READ, {}, "go_looser"),
    ("tenant-api", {"id": "r", "pyyaml": {}}, {"fin": _dom(["t1"], ["slack"])}, "go_stricter"),
    ("tenant-api", {"id": "r", "pyyaml": "unusable"}, {}, "go_looser"),
])
def test_classify(reader, row, go, want) -> None:
    assert classify(reader, row, go) == want


def test_classify_both_refusing_is_no_difference() -> None:
    """A file the generator drops, or blocks on under --strict, that
    tenant-api refuses: both block — equal, not go_refuses."""
    with pytest.raises(ValueError):
        classify("tenant-api", {"id": "r", "pyyaml": "unusable"}, "unusable")
    with pytest.raises(ValueError):
        classify("tenant-api", {**_READ, "pyyaml_strict_blocks": [{"kind": "k"}]}, "unusable")


# --- routing direction --------------------------------------------------------

# Finding fields: each element is a blocking finding (a strict ERROR, a
# skipped route entry, a domain_policy_violation) — compared as multisets.
_FINDING_FIELDS = ("policy", "rejected_routes", "group_by_invalid")


def _blocks(py_blocks: bool, go_blocks: bool) -> str:
    if py_blocks and not go_blocks:
        return "go_looser"
    if go_blocks and not py_blocks:
        return "go_stricter"
    return "value_differs"


def _verdict_direction(py: str, go: str, ok: str = "ok") -> str | None:
    """One tenant_api verdict (put or batch): None when equal. tenant-api
    refusing for an unusable policy file (503 POLICY_UNAVAILABLE) where the
    generator accepts is go_refuses, as for the policy layer: it hangs on
    tenant-api's last good policy and on --policy-unavailable-open."""
    if py == go:
        return None
    if go == "503" and py == ok:
        return "go_refuses"
    return _blocks(py != ok, go != ok)


def routing_direction(field: str, py: object, go: object) -> str:
    """Which way the Go column of a routing-matrix cell leans from the
    generator's (`python_differs`).

    - The generator blocks, refuses or drops and Go does not: go_looser.
    - Go blocks and the generator does not: go_stricter.
    - Anything else that differs: value_differs (blocking, fix-pending).

    Per field: policy / rejected_routes / group_by_invalid are lists of
    findings — the generator's a strict superset is looser, Go's stricter,
    neither value_differs. refused: one side refusing alone. tenant_api:
    {put, batch} — a verdict other than 'ok' blocks, and tenant-api's 503
    (an unusable policy file) where the generator says 'ok' is go_refuses;
    put and batch ranked alone, then combined (two directions, or a side not judged, is
    value_differs). targets is rendered routes, not a verdict:
    value_differs whenever it differs. Equal values raise."""
    if py == go:
        raise ValueError(f"{field}: the values are equal: no direction")
    if field == "targets":
        return "value_differs"
    if field in _FINDING_FIELDS:
        p = Counter(json.dumps(x) for x in py)
        g = Counter(json.dumps(x) for x in go)
        if all(g[k] <= p[k] for k in g):
            return "go_looser"
        if all(p[k] <= g[k] for k in p):
            return "go_stricter"
        return "value_differs"
    if field == "refused":
        return _blocks(py is not None, go is not None)
    if field == "tenant_api":
        if py is None or go is None:
            return "value_differs"  # one side not judged: cannot be ranked
        subs = {_verdict_direction(py["put"], go["put"])}
        if py["batch"] != go["batch"]:
            pb, gb = py["batch"], go["batch"]
            same_op = (pb is not None and gb is not None
                       and {k: v for k, v in pb.items() if k != "verdict"}
                       == {k: v for k, v in gb.items() if k != "verdict"})
            subs.add(_verdict_direction(pb["verdict"], gb["verdict"]) if same_op else "value_differs")
        subs.discard(None)
        return subs.pop() if len(subs) == 1 else "value_differs"
    raise ValueError(f"{field}: not a python_differs field")


_PUT = {"put": "ok", "batch": None}
_BATCH = {"patch": {"_routing_profile": "p"}, "verdict": "ok"}


@pytest.mark.parametrize("field, py, go, want", [
    ("targets", [{"ref": "receiver", "type": "slack"}], None, "value_differs"),
    ("targets", None, [{"ref": "receiver", "type": "pagerduty"}], "value_differs"),
    ("targets", [{"ref": "receiver", "type": "slack"}], [], "value_differs"),
    # Findings: the generator reports more (Go misses one) is looser.
    ("policy", [["fin", "receiver", "forbidden_receiver_types"]], [], "go_looser"),
    ("policy", [], [["fin", "receiver", "forbidden_receiver_types"]], "go_stricter"),
    ("policy", [["fin", "receiver", "forbidden_receiver_types"]],
     [["ops", "receiver", "forbidden_receiver_types"]], "value_differs"),
    ("policy", [["fin", "receiver", "forbidden_receiver_types"]] * 2,
     [["fin", "receiver", "forbidden_receiver_types"]], "go_looser"),
    ("rejected_routes", ["routes[0]", "routes[1]"], ["routes[0]"], "go_looser"),
    ("rejected_routes", ["routes[0]"], ["routes[0]", "routes[1]"], "go_stricter"),
    ("group_by_invalid", [["group_by[0]", "empty"]], [["group_by[0]", "not_string"]], "value_differs"),
    ("group_by_invalid", [], [["group_by[0]", "empty"]], "go_stricter"),
    # refused: the generator refusing alone is looser.
    ("refused", "tenant_file_unreadable", None, "go_looser"),
    ("refused", None, "routing_not_mapping", "go_stricter"),
    ("refused", "tenant_file_unreadable", "routing_not_mapping", "value_differs"),
    # tenant_api: the generator refusing a PUT tenant-api lets through.
    ("tenant_api", {"put": "403", "batch": None}, _PUT, "go_looser"),
    ("tenant_api", {"put": "400", "batch": None}, _PUT, "go_looser"),
    ("tenant_api", _PUT, {"put": "403", "batch": None}, "go_stricter"),
    # tenant-api's policy file unusable (503) where the generator accepts:
    # go_refuses. Both refusing (403 / 503) is no difference: the routing
    # parity test holds python_differs to tenant-api's own cell there, so it
    # never reaches this; ranked anyway, two codes are value_differs.
    ("tenant_api", _PUT, {"put": "503", "batch": None}, "go_refuses"),
    ("tenant_api", {"put": "403", "batch": None}, {"put": "503", "batch": None}, "value_differs"),
    ("tenant_api", {"put": "403", "batch": None}, {"put": "400", "batch": None}, "value_differs"),
    ("tenant_api", {"put": "403", "batch": None}, None, "value_differs"),
    ("tenant_api", {"put": "ok", "batch": {**_BATCH, "verdict": "policy_violation"}},
     {"put": "ok", "batch": _BATCH}, "go_looser"),
    ("tenant_api", {"put": "ok", "batch": _BATCH},
     {"put": "ok", "batch": {**_BATCH, "verdict": "policy_violation"}}, "go_stricter"),
    # put looser, batch stricter: two directions.
    ("tenant_api", {"put": "403", "batch": _BATCH},
     {"put": "ok", "batch": {**_BATCH, "verdict": "policy_violation"}}, "value_differs"),
    # Different batch ops cannot be ranked.
    ("tenant_api", {"put": "ok", "batch": _BATCH}, {"put": "ok", "batch": None}, "value_differs"),
])
def test_routing_direction(field, py, go, want) -> None:
    assert routing_direction(field, py, go) == want


def test_routing_direction_refuses_equal_and_unknown() -> None:
    with pytest.raises(ValueError):
        routing_direction("policy", [], [])
    with pytest.raises(ValueError):
        routing_direction("escalation", None, {"verdict": "violation", "leaks": []})


# --- loading ------------------------------------------------------------------

def _corpus() -> dict:
    rows = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    return {r["id"]: r for r in rows}


def _snapshot() -> dict:
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if k != "_comment"}


def _catalog() -> list:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))["entries"]


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _date(value: object) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return dt.date.fromisoformat(str(value))


def _policy_match_errors(name: str, e: dict, corpus_ids: set) -> list:
    """match: {shape}, {shape, verdict: unusable} or {rows: [...]}. A shape
    of `seeded` names the rows that have none (the fixed-seed ones)."""
    errors = []
    m = e["match"]
    if not isinstance(m, dict) or set(m) not in ({"shape"}, {"shape", "verdict"}, {"rows"}):
        return [f"{name}: match must be {{shape: ...}}, {{shape: ..., verdict: unusable}} or {{rows: [...]}}"]
    if "verdict" in m and m["verdict"] != "unusable":
        errors.append(f"{name}: match.verdict {m['verdict']!r} is not 'unusable'")
    if "shape" in m and e["direction"] in BLOCKING:
        errors.append(f"{name}: a {e['direction']} entry must list its rows (match.rows), not a shape")
    if "example" not in e:
        errors.append(f"{name}: a policy entry needs example (one of its rows)")
    elif not _ROW_ID.match(str(e["example"])):
        errors.append(f"{name}: example {e['example']!r} is no row id")
    if "rows" in m:
        rows = m["rows"]
        if not isinstance(rows, list) or not rows:
            return errors + [f"{name}: match.rows must be a non-empty list"]
        if len(set(rows)) != len(rows):
            errors.append(f"{name}: match.rows repeats a row")
        for r in rows:
            if r not in corpus_ids:
                errors.append(f"{name}: match.rows {r!r} is in no corpus row")
    return errors


def _routing_match_errors(name: str, e: dict) -> list:
    """match: {cells: [[tree, tenant, field], ...]} — every cell named, so an
    entry never covers another field (or tenant) of the same tree."""
    m = e["match"]
    if not isinstance(m, dict) or set(m) != {"cells"}:
        return [f"{name}: a routing entry's match must be {{cells: [[tree, tenant, field], ...]}}"]
    errors = []
    if "example" in e:
        errors.append(f"{name}: a routing entry lists every cell; it takes no example")
    if e["reader"] not in ROUTING_READERS:
        errors.append(f"{name}: reader {e['reader']!r} not in {ROUTING_READERS}")
    cells = m["cells"]
    if not isinstance(cells, list) or not cells:
        return errors + [f"{name}: match.cells must be a non-empty list"]
    seen = set()
    for c in cells:
        if not (isinstance(c, list) and len(c) == 3 and all(isinstance(x, str) for x in c)):
            errors.append(f"{name}: match.cells {c!r} is no [tree, tenant, field]")
            continue
        if tuple(c) in seen:
            errors.append(f"{name}: match.cells repeats {c}")
        seen.add(tuple(c))
        if c[2] not in FIELD_READERS:
            errors.append(f"{name}: field {c[2]!r} not in {tuple(FIELD_READERS)}")
        elif e["reader"] not in FIELD_READERS[c[2]]:
            errors.append(f"{name}: {e['reader']} does not assert {c[2]!r} "
                          f"(asserted by {FIELD_READERS[c[2]]})")
    if isinstance(e["count"], int) and len(cells) != e["count"]:
        errors.append(f"{name}: lists {len(cells)} cells, count says {e['count']}")
    return errors


def schema_errors(entries: list, corpus_ids: set) -> list:
    """Every way a catalog fails its schema, one message each."""
    errors = []
    seen_ids: set = set()
    for n, e in enumerate(entries):
        name = e.get("id", f"#{n}") if isinstance(e, dict) else f"#{n}"
        if not isinstance(e, dict):
            errors.append(f"{name}: not a mapping")
            continue
        missing = [k for k in REQUIRED if k not in e]
        unknown = [k for k in e if k not in REQUIRED + OPTIONAL]
        if missing:
            errors.append(f"{name}: missing {missing}")
        if unknown:
            errors.append(f"{name}: unknown keys {unknown}")
        if missing:
            continue
        if name in seen_ids:
            errors.append(f"{name}: duplicate id")
        seen_ids.add(name)
        if e["layer"] not in LAYERS:
            errors.append(f"{name}: layer {e['layer']!r} not in {LAYERS}")
            continue
        if e["layer"] == "policy" and e["reader"] not in READERS:
            errors.append(f"{name}: reader {e['reader']!r} not in {READERS}")
        if e["direction"] not in DIRECTIONS:
            errors.append(f"{name}: direction {e['direction']!r} not in {DIRECTIONS}")
        if e["direction"] == "go_refuses" and e["reader"] != "tenant-api":
            errors.append(f"{name}: go_refuses is tenant-api's refusal over a policy file alone")
        if e["disposition"] not in DISPOSITIONS:
            errors.append(f"{name}: disposition {e['disposition']!r} not in {DISPOSITIONS}")
        if e["disposition"] == "accept-document" and e["direction"] not in ACCEPTABLE:
            errors.append(f"{name}: accept-document is only for {ACCEPTABLE}, not {e['direction']}")
        if e["direction"] in BLOCKING and e["disposition"] != "fix-pending":
            errors.append(f"{name}: a {e['direction']} entry blocks phase 2 and must be fix-pending")
        if e["disposition"] == "accept-document" and "sign_off" not in e:
            errors.append(f"{name}: accept-document needs sign_off (the owner's sign-off URL)")
        if "sign_off" in e and not _SIGN_OFF_URL.match(str(e["sign_off"])):
            errors.append(f"{name}: sign_off {e['sign_off']!r} is no issue/PR (comment) URL")
        if not _ISSUE_URL.match(str(e["ref"])):
            errors.append(f"{name}: ref {e['ref']!r} is no issue/PR URL")
        try:
            _date(e["expire_at"])
        except ValueError:
            errors.append(f"{name}: expire_at {e['expire_at']!r} is no ISO date")
        if not isinstance(e["count"], int) or isinstance(e["count"], bool) or e["count"] < 1:
            errors.append(f"{name}: count {e['count']!r} is no positive integer")
        if not isinstance(e["mechanism"], str) or not e["mechanism"].strip():
            errors.append(f"{name}: mechanism is empty")
        if e["layer"] == "policy":
            errors += _policy_match_errors(name, e, corpus_ids)
        else:
            errors += _routing_match_errors(name, e)
    return errors


def _matches(entry: dict, reader: str, row: dict, go: object = None) -> bool:
    if entry["layer"] != "policy" or entry["reader"] != reader:
        return False
    m = entry["match"]
    if "shape" in m:
        if "verdict" in m and go != m["verdict"]:
            return False
        return row.get("shape", "seeded") == m["shape"]
    return row["id"] in m["rows"]


def _policy_errors(entries: list, snapshot: dict, corpus: dict) -> list:
    """The policy-layer entries against the snapshot, one message each."""
    errors = []
    entries = [e for e in entries if e["layer"] == "policy"]
    matched: dict = {e["id"]: [] for e in entries}
    for reader, rows in snapshot.items():
        if reader not in READERS:
            errors.append(f"snapshot reader {reader!r} not in {READERS}")
            continue
        for row_id, go in rows.items():
            row = corpus.get(row_id)
            if row is None:
                errors.append(f"{reader} {row_id}: in the snapshot, in no corpus row")
                continue
            if go == effective_pyyaml(row):
                errors.append(f"{reader} {row_id}: the snapshot records PyYAML's own verdict")
                continue
            got = classify(reader, row, go)
            hits = [e for e in entries if _matches(e, reader, row, go)]
            if not hits:
                blocked = " — blocked under --validate --strict" if row.get("pyyaml_strict_blocks") else ""
                errors.append(f"{reader} {row_id} [{row.get('shape', 'seeded')}]: uncatalogued {got} "
                              f"difference (PyYAML {json.dumps(row['pyyaml'])}{blocked}, Go {json.dumps(go)})")
                continue
            if len(hits) > 1:
                errors.append(f"{reader} {row_id}: matched by more than one entry: {[e['id'] for e in hits]}")
                continue
            entry = hits[0]
            matched[entry["id"]].append(row_id)
            if got != entry["direction"]:
                errors.append(f"{entry['id']}: row {row_id} is {got}, the entry says {entry['direction']}")
    for e in entries:
        ids = matched[e["id"]]
        if not ids:
            errors.append(f"{e['id']}: matches no snapshot row (the difference healed): remove the entry")
            continue
        if len(ids) != e["count"]:
            errors.append(f"{e['id']}: matches {len(ids)} rows, count says {e['count']}")
        if e["example"] not in ids:
            errors.append(f"{e['id']}: example {e['example']} is not among its matched rows")
        if "rows" in e["match"]:
            for r in e["match"]["rows"]:
                if r not in ids:
                    errors.append(f"{e['id']}: row {r} is no longer in the {e['reader']} snapshot (healed)")
    return errors


def routing_differences(matrix: dict) -> tuple[list, list]:
    """Every reader difference the routing matrix carries, and every way its
    python_differs cells are malformed for that purpose.

    A difference is (reader, tree, tenant, field, direction): a field where
    a cell's python_differs (the generator's value) differs from the Go
    column, once per Go half that asserts that field (FIELD_READERS). A
    python_differs that differs in no field is stale: it must be null."""
    diffs, errors = [], []
    for tree in matrix["trees"]:
        for tenant, want in tree["expect"].items():
            differs = want.get("python_differs")
            if differs is None:
                continue
            fields = [f for f in FIELD_READERS if differs.get(f) != want.get(f)]
            if not fields:
                errors.append(f"{tree['name']} {tenant}: python_differs differs from the Go columns in no "
                              f"field (healed): set it null")
            for field in fields:
                d = routing_direction(field, differs.get(field), want.get(field))
                diffs += [(reader, tree["name"], tenant, field, d) for reader in FIELD_READERS[field]]
    return diffs, errors


def _routing_errors(entries: list, matrix: dict | None) -> list:
    """The routing-layer entries against the matrix, one message each."""
    entries = [e for e in entries if e["layer"] == "routing"]
    diffs, errors = routing_differences(matrix) if matrix is not None else ([], [])
    matched: dict = {e["id"]: set() for e in entries}
    for reader, tree, tenant, field, got in diffs:
        cell = [tree, tenant, field]
        hits = [e for e in entries if e["reader"] == reader and cell in e["match"]["cells"]]
        if not hits:
            errors.append(f"{reader} {cell}: uncatalogued {got} routing difference")
            continue
        if len(hits) > 1:
            errors.append(f"{reader} {cell}: matched by more than one entry: {[e['id'] for e in hits]}")
            continue
        matched[hits[0]["id"]].add(tuple(cell))
        if got != hits[0]["direction"]:
            errors.append(f"{hits[0]['id']}: cell {cell} is {got}, the entry says {hits[0]['direction']}")
    for e in entries:
        if not matched[e["id"]]:
            errors.append(f"{e['id']}: matches no routing difference (healed): remove the entry")
            continue
        for c in e["match"]["cells"]:
            if tuple(c) not in matched[e["id"]]:
                errors.append(f"{e['id']}: cell {c} is no {e['reader']} routing difference (healed, or "
                              f"no such cell)")
    return errors


def catalog_errors(entries: list, snapshot: dict, corpus: dict, matrix: dict | None = None) -> list:
    """Every way the catalog disagrees with the snapshot (policy layer) and
    the routing matrix (routing layer), one message each. Without a matrix,
    no routing difference exists."""
    errors = schema_errors(entries, set(corpus))
    if errors:
        return errors
    return _policy_errors(entries, snapshot, corpus) + _routing_errors(entries, matrix)


def today() -> dt.date:
    value = os.environ.get(TODAY_ENV)
    return dt.date.fromisoformat(value) if value else dt.date.today()


def expired(entries: list, on: dt.date) -> list:
    return [f"{e['id']} (expire_at {_date(e['expire_at'])})" for e in entries if _date(e["expire_at"]) < on]


def phase2_blockers(entries: list) -> list:
    """Entries that keep ADR-036 from entering phase 2: any go_looser or
    value_differs difference still catalogued."""
    return [e["id"] for e in entries if e["direction"] in BLOCKING]


# --- the gate -----------------------------------------------------------------

def test_catalog_accepts_every_go_difference_exactly() -> None:
    errors = catalog_errors(_catalog(), _snapshot(), _corpus(), _matrix())
    assert not errors, ("reader_divergence_catalog.yaml vs merge_key_go_verdicts.json and "
                        "routing_policy_parity_matrix.json:\n  " + "\n  ".join(errors))


def test_catalog_expiry() -> None:
    """An expired entry warns in PR CI and fails when strict expiry is asked
    for (`make pre-tag` sets READER_DIVERGENCE_STRICT_EXPIRY=1)."""
    gone = expired(_catalog(), today())
    if not gone:
        return
    msg = f"reader_divergence_catalog.yaml: {len(gone)} entries expired on {today()}: " + ", ".join(gone)
    if os.environ.get(STRICT_EXPIRY_ENV, "") not in ("", "0"):
        pytest.fail(msg + f" ({STRICT_EXPIRY_ENV} is set)")
    warnings.warn(msg, UserWarning, stacklevel=1)


def test_phase2_readiness_is_reported(capsys) -> None:
    """Informational: never fails while blockers remain (ADR-036 phase 2
    waits on them by design)."""
    blockers = phase2_blockers(_catalog())
    with capsys.disabled():
        if blockers:
            print(f"\nADR-036 phase 2 ready: no — {len(blockers)} go_looser/value_differs entries: "
                  + ", ".join(blockers))
        else:
            print("\nADR-036 phase 2 ready: yes — no go_looser/value_differs entries")


# --- the gate's own failure modes -------------------------------------------

def _entry(**over) -> dict:
    e = {"id": "e", "reader": "da-guard", "layer": "policy", "match": {"rows": ["aaaaaaaaaaaaaaaa"]},
         "mechanism": "m", "direction": "go_looser", "disposition": "fix-pending",
         "ref": "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759",
         "expire_at": "2027-01-10", "count": 1, "example": "aaaaaaaaaaaaaaaa"}
    e.update(over)
    return e


_POL = {"fin": _dom(["t1"], ["slack"])}
_STRICT = [{"kind": "domain_policy_unusable", "policy": "fin", "field": "constraints.require_critical_escalation"}]
_CORPUS = {"aaaaaaaaaaaaaaaa": {"id": "aaaaaaaaaaaaaaaa", "pyyaml": "unusable", "shape": "s"},
           "bbbbbbbbbbbbbbbb": {"id": "bbbbbbbbbbbbbbbb", "pyyaml": _POL, "shape": "s"},
           # Read by the generator, but blocked under --validate --strict.
           "dddddddddddddddd": {"id": "dddddddddddddddd", "pyyaml": _POL, "shape": "s",
                                "pyyaml_strict_blocks": _STRICT},
           "eeeeeeeeeeeeeeee": {"id": "eeeeeeeeeeeeeeee", "pyyaml": _POL, "shape": "s"}}


def _refuses(**over) -> dict:
    """A go_refuses entry: tenant-api's refusals of shape `s`."""
    e = _entry(id="r", reader="tenant-api", match={"shape": "s", "verdict": "unusable"},
               direction="go_refuses", disposition="accept-document", example="bbbbbbbbbbbbbbbb",
               sign_off="https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759")
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


def test_gate_is_green_on_a_matching_catalog() -> None:
    assert catalog_errors([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, _CORPUS) == []
    assert catalog_errors([_entry(), _refuses()],
                          {"da-guard": {"aaaaaaaaaaaaaaaa": _POL},
                           "tenant-api": {"bbbbbbbbbbbbbbbb": "unusable"}}, _CORPUS) == []


# --- routing layer ------------------------------------------------------------

def _cell(targets, policy=(), refused=None, tenant_api=None, differs=None) -> dict:
    return {"targets": targets, "policy": [list(p) for p in policy], "rejected_routes": [],
            "values_not_string": [], "group_by_invalid": [], "unknown_profile": None,
            "tenant_api": tenant_api, "python_differs": differs, "escalation": None, "refused": refused}


_SLACK = [{"ref": "receiver", "type": "slack"}]
_VIOLATION = [["fin", "receiver", "forbidden_receiver_types"]]


def _differs(**over) -> dict:
    d = {"reason": "r", "targets": None, "policy": [], "rejected_routes": [], "group_by_invalid": [],
         "refused": None, "tenant_api": {"put": "ok", "batch": None}}
    d.update(over)
    return d


def _tree(name="t", tenant="t1", **cell) -> dict:
    return {"name": name, "expect": {tenant: _cell(**cell)}}


# Go reads no routing for t1; the generator renders a slack receiver that
# breaks the domain policy, and would refuse the PUT tenant-api accepts.
_ROUTING_MATRIX = {"trees": [
    _tree(targets=None, tenant_api={"put": "ok", "batch": None},
          differs=_differs(targets=_SLACK, policy=_VIOLATION, tenant_api={"put": "403", "batch": None})),
    _tree(name="same", targets=_SLACK, policy=_VIOLATION),
]}


def _routing(id_, reader, cells, direction="go_looser") -> dict:
    return {"id": id_, "reader": reader, "layer": "routing", "match": {"cells": cells},
            "mechanism": "m", "direction": direction, "disposition": "fix-pending",
            "ref": "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700",
            "expire_at": "2027-01-10", "count": len(cells)}


_ROUTING_ENTRIES = [
    _routing("rp-targets", "routingpolicy", [["t", "t1", "targets"]], "value_differs"),
    _routing("rp-policy", "routingpolicy", [["t", "t1", "policy"]]),
    _routing("dg-policy", "da-guard", [["t", "t1", "policy"]]),
    _routing("ta-put", "tenant-api", [["t", "t1", "tenant_api"]]),
]


def test_routing_differences() -> None:
    diffs, errors = routing_differences(_ROUTING_MATRIX)
    assert errors == []
    assert sorted(diffs) == sorted([
        ("routingpolicy", "t", "t1", "targets", "value_differs"),
        ("routingpolicy", "t", "t1", "policy", "go_looser"),
        ("da-guard", "t", "t1", "policy", "go_looser"),
        ("tenant-api", "t", "t1", "tenant_api", "go_looser"),
    ])


def test_routing_gate_is_green_on_a_matching_catalog() -> None:
    assert catalog_errors(_ROUTING_ENTRIES, {}, _CORPUS, _ROUTING_MATRIX) == []


def _healed(**cell_over) -> dict:
    """_ROUTING_MATRIX with the first tree's cell changed."""
    m = json.loads(json.dumps(_ROUTING_MATRIX))
    m["trees"][0]["expect"]["t1"].update(cell_over)
    return m


@pytest.mark.parametrize("entries, matrix, needle", [
    # An uncatalogued cell: per reader, per field.
    (_ROUTING_ENTRIES[:2] + _ROUTING_ENTRIES[3:], _ROUTING_MATRIX,
     "da-guard ['t', 't1', 'policy']: uncatalogued go_looser routing difference"),
    (_ROUTING_ENTRIES[1:], _ROUTING_MATRIX, "routingpolicy ['t', 't1', 'targets']: uncatalogued value_differs"),
    # A tree's entry does not swallow another field of the same tree.
    ([_routing("rp", "routingpolicy", [["t", "t1", "targets"]], "value_differs")] + _ROUTING_ENTRIES[2:],
     _ROUTING_MATRIX, "routingpolicy ['t', 't1', 'policy']: uncatalogued"),
    # Healed: python_differs set null while the entries remain.
    (_ROUTING_ENTRIES, _healed(python_differs=None), "rp-policy: matches no routing difference (healed)"),
    # Healed in one field only (Go fixed: its column now says what the
    # generator says), and a python_differs that differs in nothing.
    (_ROUTING_ENTRIES, _healed(policy=_VIOLATION), "dg-policy: matches no routing difference (healed)"),
    (_ROUTING_ENTRIES, _healed(targets=_SLACK, policy=_VIOLATION, tenant_api={"put": "403", "batch": None}),
     "python_differs differs from the Go columns in no field (healed)"),
    # A wrong direction, a wrong count, a cell its reader does not assert.
    ([_routing("rp-targets", "routingpolicy", [["t", "t1", "targets"]])] + _ROUTING_ENTRIES[1:],
     _ROUTING_MATRIX, "is value_differs, the entry says go_looser"),
    ([{**_ROUTING_ENTRIES[0], "count": 2}] + _ROUTING_ENTRIES[1:], _ROUTING_MATRIX, "count says 2"),
    ([_routing("x", "da-guard", [["t", "t1", "targets"]], "value_differs")], _ROUTING_MATRIX,
     "da-guard does not assert 'targets'"),
    ([_routing("x", "routingpolicy", [["t", "t1", "escalation"]])], _ROUTING_MATRIX, "field 'escalation'"),
    ([_routing("x", "routingpolicy", [["t", "t1"]])], _ROUTING_MATRIX, "is no [tree, tenant, field]"),
    ([{**_ROUTING_ENTRIES[0], "match": {"tree": "t"}}], _ROUTING_MATRIX, "match must be {cells"),
    ([{**_ROUTING_ENTRIES[0], "example": "aaaaaaaaaaaaaaaa"}], _ROUTING_MATRIX, "takes no example"),
    ([{**_routing("rp-policy", "routingpolicy", [["t", "t1", "policy"]], "go_stricter"),
       "disposition": "accept-document",
       "sign_off": "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759"}]
     + [e for e in _ROUTING_ENTRIES if e["id"] != "rp-policy"], _ROUTING_MATRIX,
     "is go_looser, the entry says go_stricter"),
    ([{**_ROUTING_ENTRIES[1], "disposition": "accept-document",
       "sign_off": "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759"}], _ROUTING_MATRIX,
     "accept-document is only for"),
    # A listed cell that is no difference.
    ([{**_ROUTING_ENTRIES[1], "match": {"cells": [["t", "t1", "policy"], ["same", "t1", "policy"]]},
       "count": 2}] + [_ROUTING_ENTRIES[0]] + _ROUTING_ENTRIES[2:], _ROUTING_MATRIX,
     "cell ['same', 't1', 'policy'] is no routingpolicy routing difference"),
])
def test_routing_gate_is_red(entries, matrix, needle) -> None:
    errors = catalog_errors(entries, {}, _CORPUS, matrix)
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize("entries, snapshot, needle", [
    ([], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, "uncatalogued go_looser"),
    ([_entry(), _entry(id="f", match={"rows": ["aaaaaaaaaaaaaaaa"]})],
     {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, "more than one entry"),
    ([_entry(direction="go_stricter", disposition="accept-document",
             sign_off="https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759")],
     {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, "is go_looser, the entry says go_stricter"),
    ([_entry(count=2)], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, "count says 2"),
    ([_entry()], {"da-guard": {}}, "healed"),
    ([_entry(example="bbbbbbbbbbbbbbbb")], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, "example"),
    ([_entry(disposition="accept-document")], {}, "accept-document is only for"),
    ([_entry(match={"shape": "s"})], {}, "must list its rows"),
    ([_entry(direction="go_stricter", disposition="accept-document", match={"shape": "s"})], {},
     "needs sign_off"),
    ([_entry(direction="go_stricter", disposition="accept-document", match={"shape": "s"},
             sign_off="https://example.com/x")], {}, "sign_off"),
    ([_entry(expire_at="soon")], {}, "expire_at"),
    ([_entry(match={"rows": ["cccccccccccccccc"]})], {}, "in no corpus row"),
    ([_entry(extra=1)], {}, "unknown keys"),
    # tenant-api refusing a file the generator reads: go_refuses, never
    # go_stricter, and a refusal of one the generator drops is no difference.
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}, "tenant-api": {"bbbbbbbbbbbbbbbb": "unusable"}},
     "uncatalogued go_refuses"),
    ([_entry(), _refuses(direction="go_stricter")],
     {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}, "tenant-api": {"bbbbbbbbbbbbbbbb": "unusable"}},
     "is go_refuses, the entry says go_stricter"),
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}, "tenant-api": {"aaaaaaaaaaaaaaaa": "unusable"}},
     "PyYAML's own verdict"),
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}, "tenant-api": {"dddddddddddddddd": "unusable"}},
     "PyYAML's own verdict"),
    ([_entry(), _refuses(count=2)],
     {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}, "tenant-api": {"bbbbbbbbbbbbbbbb": "unusable"}}, "count says 2"),
    ([_entry(), _refuses(sign_off=None)], {}, "sign_off"),
    ([_refuses(reader="da-guard")], {}, "go_refuses is tenant-api's"),
    ([_refuses(match={"shape": "s", "verdict": "{}"})], {}, "match.verdict"),
    # The verdict filter: a tenant-api read in the same shape is not matched
    # by the refusals' entry — it needs its own.
    ([_entry(), _refuses()],
     {"da-guard": {"aaaaaaaaaaaaaaaa": _POL},
      "tenant-api": {"bbbbbbbbbbbbbbbb": "unusable", "eeeeeeeeeeeeeeee": {}}},
     "eeeeeeeeeeeeeeee [s]: uncatalogued go_looser"),
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL, "bbbbbbbbbbbbbbbb": _POL}},
     "PyYAML's own verdict"),
    # The generator strict-blocks: Go reading the same policies is a looser
    # difference that needs an entry, and Go blocking too is none at all.
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL, "dddddddddddddddd": _POL}},
     "dddddddddddddddd [s]: uncatalogued go_looser"),
    ([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL, "dddddddddddddddd": "unusable"}},
     "PyYAML's own verdict"),
])
def test_gate_is_red(entries, snapshot, needle) -> None:
    errors = catalog_errors(entries, snapshot, _CORPUS)
    assert any(needle in e for e in errors), errors


def test_expiry_warns_then_fails_when_strict(monkeypatch) -> None:
    entries = [_entry(expire_at="2026-01-01")]
    assert expired(entries, dt.date(2026, 1, 1)) == []
    assert expired(entries, dt.date(2026, 1, 2)) == ["e (expire_at 2026-01-01)"]
    monkeypatch.setenv(TODAY_ENV, "2026-01-02")
    assert today() == dt.date(2026, 1, 2)


def test_strict_expiry_fails_the_gate(monkeypatch) -> None:
    """With today injected past every entry, the expiry test warns without
    strict mode and fails with it."""
    monkeypatch.setenv(TODAY_ENV, "2999-01-01")
    monkeypatch.delenv(STRICT_EXPIRY_ENV, raising=False)
    with pytest.warns(UserWarning, match="expired"):
        test_catalog_expiry()
    monkeypatch.setenv(STRICT_EXPIRY_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="expired"):
        test_catalog_expiry()


def test_phase2_blockers() -> None:
    assert phase2_blockers([_entry()]) == ["e"]
    assert phase2_blockers([_entry(direction="go_stricter", disposition="accept-document")]) == []


def test_pre_tag_runs_strict_expiry() -> None:
    """The expiry only bites if the tag gate asks for it."""
    makefile = (HERE.parents[1] / "Makefile").read_text(encoding="utf-8")
    pre_tag = next(ln for ln in makefile.splitlines() if ln.startswith("pre-tag:"))
    assert "reader-divergence-expiry" in pre_tag.split("##")[0].split()
    recipe = makefile.split("\nreader-divergence-expiry:", 1)[1].split("\n\n", 1)[0]
    assert f"{STRICT_EXPIRY_ENV}=1" in recipe and "test_reader_divergence_catalog.py" in recipe, recipe
    # The gate's clock is the real date: a stale injected "today" in the
    # caller's environment must not reach it.
    assert f"env -u {TODAY_ENV} " in recipe, recipe
