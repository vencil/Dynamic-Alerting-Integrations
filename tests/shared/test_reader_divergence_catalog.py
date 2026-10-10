"""Reader-divergence catalog: every Go != generator verdict is accepted, once.

ADR-036 step 2 (policy layer). The route generator's reader (PyYAML,
`_parse_config_files`) is the authority; no Go reader may be looser. The Go
corpus tests write the rows where a Go reader's verdict differs from PyYAML's
to tests/shared/merge_key_go_verdicts.json (per reader, keyed by corpus row
`id`); this test is the one place that ranks each difference and matches it
against tests/shared/reader_divergence_catalog.yaml:

- direction() computes each snapshot row's direction from the rules each
  side actually enforces (go_looser / go_stricter / value_differs); a file
  the generator blocks on as production runs it (`--validate --strict`, the
  row's `pyyaml_strict_blocks`) counts as unusable, so a Go reader that
  neither refuses it nor reports a blocking problem is looser;
- every snapshot row must match exactly one entry, of the same direction;
- each entry pins its exact row count and an example row; an entry that
  matches nothing (the difference healed) is red, as is an uncatalogued row;
- go_looser and value_differs entries list their rows one by one and stay
  `fix-pending`: they block ADR-036's phase 2 (phase2_blockers());
- expire_at: an expired entry warns; with READER_DIVERGENCE_STRICT_EXPIRY=1
  (`make pre-tag` sets it) it fails. READER_DIVERGENCE_TODAY=YYYY-MM-DD
  injects "today".

tenant-api's refusals are excused wholesale and not recorded yet (they get a
direction of their own, `go_refuses`, in ADR-036 step 2's PR-C).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import warnings
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
CORPUS_PATH = HERE / "merge_key_policy_corpus.json"
SNAPSHOT_PATH = HERE / "merge_key_go_verdicts.json"
CATALOG_PATH = HERE / "reader_divergence_catalog.yaml"

READERS = ("da-guard", "tenant-api")
LAYERS = ("policy",)
DIRECTIONS = ("go_looser", "go_stricter", "value_differs")
DISPOSITIONS = ("accept-document", "fix-pending")
# Directions that block ADR-036's phase 2: they must be listed row by row.
BLOCKING = ("go_looser", "value_differs")
REQUIRED = ("id", "reader", "layer", "match", "mechanism", "direction", "disposition", "ref",
            "expire_at", "count", "example")
OPTIONAL = ("sign_off",)
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


# --- loading ------------------------------------------------------------------

def _corpus() -> dict:
    rows = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    return {r["id"]: r for r in rows}


def _snapshot() -> dict:
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if k != "_comment"}


def _catalog() -> list:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))["entries"]


def _date(value: object) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return dt.date.fromisoformat(str(value))


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
        if e["reader"] not in READERS:
            errors.append(f"{name}: reader {e['reader']!r} not in {READERS}")
        if e["layer"] not in LAYERS:
            errors.append(f"{name}: layer {e['layer']!r} not in {LAYERS}")
        if e["direction"] not in DIRECTIONS:
            errors.append(f"{name}: direction {e['direction']!r} not in {DIRECTIONS}")
        if e["disposition"] not in DISPOSITIONS:
            errors.append(f"{name}: disposition {e['disposition']!r} not in {DISPOSITIONS}")
        if e["disposition"] == "accept-document" and e["direction"] != "go_stricter":
            errors.append(f"{name}: accept-document is only for go_stricter, not {e['direction']}")
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
        if not _ROW_ID.match(str(e["example"])):
            errors.append(f"{name}: example {e['example']!r} is no row id")
        if not isinstance(e["mechanism"], str) or not e["mechanism"].strip():
            errors.append(f"{name}: mechanism is empty")
        m = e["match"]
        if not isinstance(m, dict) or len(m) != 1 or next(iter(m)) not in ("shape", "rows"):
            errors.append(f"{name}: match must be exactly one of {{shape: ...}} or {{rows: [...]}}")
            continue
        if "shape" in m and e["direction"] in BLOCKING:
            errors.append(f"{name}: a {e['direction']} entry must list its rows (match.rows), not a shape")
        if "rows" in m:
            rows = m["rows"]
            if not isinstance(rows, list) or not rows:
                errors.append(f"{name}: match.rows must be a non-empty list")
                continue
            if len(set(rows)) != len(rows):
                errors.append(f"{name}: match.rows repeats a row")
            for r in rows:
                if r not in corpus_ids:
                    errors.append(f"{name}: match.rows {r!r} is in no corpus row")
    return errors


def _matches(entry: dict, reader: str, row: dict) -> bool:
    if entry["reader"] != reader:
        return False
    m = entry["match"]
    if "shape" in m:
        return row.get("shape") == m["shape"]
    return row["id"] in m["rows"]


def catalog_errors(entries: list, snapshot: dict, corpus: dict) -> list:
    """Every way the catalog and the snapshot disagree, one message each."""
    errors = schema_errors(entries, set(corpus))
    if errors:
        return errors
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
            if reader == "tenant-api" and go == "unusable":
                errors.append(f"{reader} {row_id}: tenant-api refusals are not recorded yet (PR-C)")
                continue
            if go == effective_pyyaml(row):
                errors.append(f"{reader} {row_id}: the snapshot records PyYAML's own verdict")
                continue
            got = direction(row["pyyaml"], go, bool(row.get("pyyaml_strict_blocks")))
            hits = [e for e in entries if _matches(e, reader, row)]
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
    errors = catalog_errors(_catalog(), _snapshot(), _corpus())
    assert not errors, "reader_divergence_catalog.yaml vs merge_key_go_verdicts.json:\n  " + "\n  ".join(errors)


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
                                "pyyaml_strict_blocks": _STRICT}}


def test_gate_is_green_on_a_matching_catalog() -> None:
    assert catalog_errors([_entry()], {"da-guard": {"aaaaaaaaaaaaaaaa": _POL}}, _CORPUS) == []


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
    ([_entry(disposition="accept-document")], {}, "accept-document is only for go_stricter"),
    ([_entry(match={"shape": "s"})], {}, "must list its rows"),
    ([_entry(direction="go_stricter", disposition="accept-document", match={"shape": "s"})], {},
     "needs sign_off"),
    ([_entry(direction="go_stricter", disposition="accept-document", match={"shape": "s"},
             sign_off="https://example.com/x")], {}, "sign_off"),
    ([_entry(expire_at="soon")], {}, "expire_at"),
    ([_entry(match={"rows": ["cccccccccccccccc"]})], {}, "in no corpus row"),
    ([_entry(extra=1)], {}, "unknown keys"),
    ([_entry()], {"tenant-api": {"bbbbbbbbbbbbbbbb": "unusable"}}, "not recorded yet"),
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
