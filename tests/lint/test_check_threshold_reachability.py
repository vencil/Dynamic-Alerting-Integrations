"""Tests for scripts/tools/lint/check_threshold_reachability.py (TRK-337).

The gate asserts every alert-demanded threshold key is producible by the
platform-defaults path, grandfathering the keys already dead when it landed
(18 then; 9 A-class remain after the #1231 B/C/D/E identity repairs).
Each test below pins one branch of that contract (they are regression pins for a
declared-but-unwired guard, not decoration):
  - the live repo is green (the A-class rump grandfathered, 0 new drift)
  - a NEW dead key fails --ci
  - the _critical reachability rule (base in supply) is honoured
  - the KNOWN_UNWIRED exit-lock fires when a grandfathered key is fixed or removed
"""
from __future__ import annotations

import contextlib
import importlib.util
import re
import subprocess
import sys

import pytest
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_reachability.py"

_spec = importlib.util.spec_from_file_location("check_threshold_reachability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_has_no_new_drift():
    """Grandfathered keys are INFO; there must be zero NEW unreachable keys and
    zero stale exemptions on the real artifacts."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


def test_grandfather_list_is_exactly_the_remaining_dead_keys():
    """Every KNOWN_UNWIRED key must actually be dead on the real repo (else it is
    a stale exemption). Guards against the list drifting from reality.
    18 at gate-landing; the #1231 B/C/D/E identity repairs shrank it to the
    9 A-class tier moves (all `A:`-tagged) still tracked under TRK-337."""
    result = gate.run_check()
    # the whole rump shows up as info; none as a STALE-EXEMPTION error.
    # Scoped by prefix since P1-C added a second info class (not-chart-armed) —
    # counting all infos would silently couple this pin to that ledger's size.
    unwired_infos = [m for m in result["infos"] if m.startswith("known-unwired ")]
    assert len(unwired_infos) == len(gate.KNOWN_UNWIRED) == 9
    assert all(tag.startswith("A:") for tag in gate.KNOWN_UNWIRED.values())
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


def test_remediation_text_matches_the_decision_that_was_actually_taken():
    """documented != codified — pin the CONTENT, not just the `A:` prefix.

    #1314 rewrote the module comment to say the A-class fix was no longer
    necessarily "move it to defaults", but the strings that get PRINTED still
    said exactly that, and the only assertion on them was `startswith("A:")` —
    which cannot tell the two apart. #1310 then MADE the decision (ship the key
    on the declared list, tenant calibrates), so the remediation a reader acts
    on must describe that, and must not keep prescribing the option the owner
    explicitly did not take (arming a platform-chosen number for every tenant).
    """
    assert gate.KNOWN_UNWIRED, "an emptied dict would make every loop below vacuous"
    for key, tag in gate.KNOWN_UNWIRED.items():
        assert tag.startswith("A:"), (key, tag)
        assert "optional_overrides" in tag, (key, tag)
        assert "resolveDeclaredRows" in tag, (key, tag)
        assert "move to defaults" not in tag, (
            f"{key}: remediation still prescribes promoting the key into "
            "defaults:, which is the option #1311 rejected")


def test_the_remediation_text_is_what_the_reader_actually_sees():
    """The tag is only worth pinning if it reaches the operator: `main()` prints
    the dict VALUE verbatim, so assert the emitted INFO line carries it."""
    infos = gate.run_check()["infos"]
    for key, tag in gate.KNOWN_UNWIRED.items():
        assert f"known-unwired {key} — {tag}" in infos, key


# ── new drift is caught ───────────────────────────────────────────────────

def test_new_unreachable_key_is_an_error():
    """A freshly-added alert demanding an unsupplied, non-grandfathered key fails."""
    result = gate.run_check(
        demand={"oracle_sessions_active", "newpack_brand_new_metric"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        known_unwired={},
    )
    assert any("newpack_brand_new_metric" in e and "UNREACHABLE" in e
               for e in result["errors"]), result


def test_supplied_key_is_not_flagged():
    result = gate.run_check(
        demand={"oracle_sessions_active"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        known_unwired={},
    )
    assert result["errors"] == []


# ── _critical reachability rule ───────────────────────────────────────────

def test_critical_key_reachable_when_base_is_supplied():
    """A `_critical` key rides resolveCriticalRows: reachable iff its BASE is in
    supply, even though the `_critical` key itself is not."""
    result = gate.run_check(
        demand={"mysql_connections_critical"},
        supply={"mysql_connections"},          # base only
        deferred=set(),
        known_unwired={},
    )
    assert result["errors"] == [], result


def test_critical_key_dead_when_base_missing():
    result = gate.run_check(
        demand={"mysql_connections_critical"},
        supply=set(),                          # base absent
        deferred=set(),
        known_unwired={},
    )
    assert any("mysql_connections_critical" in e for e in result["errors"])


def test_known_deferred_is_exempt():
    """A key whose threshold lives in a :core recording rule is deferred, not dead."""
    result = gate.run_check(
        demand={"container_cpu"},
        supply=set(),
        deferred={"container_cpu"},
        known_unwired={},
    )
    assert result["errors"] == []


# ── the exit-lock: the allowlist must shrink as fixes land ────────────────

def test_grandfathered_key_that_became_reachable_is_a_stale_exemption():
    """Once the real fix lands (key now supplied), leaving it in KNOWN_UNWIRED is
    a hard error — forces the list to shrink so it can't rot into a silent
    permanent exemption."""
    result = gate.run_check(
        demand={"oracle_wait_time_rate"},
        supply={"oracle_wait_time_rate"},      # fixed: now supplied
        deferred=set(),
        known_unwired={"oracle_wait_time_rate": "A: ..."},
    )
    assert any("STALE-EXEMPTION" in e and "REACHABLE" in e
               for e in result["errors"]), result


def test_grandfathered_key_no_longer_demanded_is_a_stale_exemption():
    """If the alert (and its key) was deleted, the exemption must go too."""
    result = gate.run_check(
        demand=set(),                          # no alert demands it anymore
        supply=set(),
        deferred=set(),
        known_unwired={"redis_keyspace_misses_ratio": "E: ..."},
    )
    assert any("STALE-EXEMPTION" in e and "demands it" in e
               for e in result["errors"]), result


# ── the CLI exit-code contract (main), independent of the real artifacts ──────

def _stub_result(errors=(), infos=()):
    """A `run_check` return value of the CURRENT shape, for `main()` stubs.

    One helper rather than three literals: `main()` reads `stats` as well as
    `errors`/`infos`, and a stub that silently lags the real shape turns a
    contract change into three identical KeyErrors far from their cause.
    `test_run_check_returns_all_three_result_keys` pins the shape itself.

    ⛔ The values are deliberately ABSURD, not today's real coverage. An earlier
    revision copied the measured 4/17/102/70/2 in here — reintroducing, inside
    the very change that removed them from a comment, a set of numbers that go
    stale with nothing turning red. These stubs are about `main()`'s exit-code
    contract; the real figures are asserted against a real call elsewhere.
    """
    return {
        "errors": list(errors),
        "infos": list(infos),
        "stats": {"generator_faces": -1, "artifact_faces": -1,
                  "generator_keys": -1, "artifact_keys": -1,
                  "artifacts_without_defaults": -1,
                  "confd_roots_scanned": -1,
                  "confd_roots_exempt_from_fifth_floor": -1,
                  "confd_roots_watched_by_fifth_floor": -1,
                  "roots_only_the_fifth_floor_notices": ["<stub, not measured>"]},
    }


# Sources read once for the citation guard at the end of this file.
GATE_SRC = (REPO_ROOT / "scripts" / "tools" / "lint"
            / "check_threshold_reachability.py").read_text(encoding="utf-8")
TEST_FILE_SRC = Path(__file__).read_text(encoding="utf-8")


def _flat(*paths: str) -> dict[str, gate.KeyInfo]:
    """A synthetic face: these key paths, all at the top level of `defaults:`.

    Faces map path → `KeyInfo` since #1392 — depth, leaf name and "does it have
    children" all come from the walk rather than being re-derived from a dot in
    the rendered path, because a flat key may legitimately contain one (and a
    dimensional key's label value routinely does).

    A flat face therefore means: depth 0, the leaf IS the path, no children.
    """
    return {p: gate.KeyInfo(0, p, False) for p in paths}


def _nested(**subtrees: dict[str, object]) -> dict[str, gate.KeyInfo]:
    """A synthetic face built by the REAL walk, so tests cannot drift from it.

    ⛔ Hand-writing `{path: KeyInfo(...)}` in a test would let the test assert
    against a structure the walk never produces. Build the YAML shape, walk it.
    """
    return gate._walk_defaults_keys(dict(subtrees))


def test_run_check_returns_all_three_result_keys():
    """`main()` consumes `stats` unconditionally, so its presence is a contract.

    Asserted on the REAL call, not a stub — a stub asserting its own shape is
    the tautology this file has been burned by before.
    """
    result = gate.run_check(demand=set(), supply=set(), deferred=set(),
                            known_unwired={}, defaults_faces=({}, {}))
    assert set(result) == {"errors", "infos", "stats"}, sorted(result)
    assert set(result["stats"]) == {
        "generator_faces", "artifact_faces", "generator_keys", "artifact_keys",
        "artifacts_without_defaults", "confd_roots_scanned",
        "confd_roots_exempt_from_fifth_floor",
        "confd_roots_watched_by_fifth_floor",
        "roots_only_the_fifth_floor_notices"}, sorted(result["stats"])


def test_main_without_ci_is_report_only(monkeypatch):
    """Bare `main([])` never fails on errors — it is report-only, so a green
    local commit is not gated on the (grandfathered) unwired set."""
    monkeypatch.setattr(gate, "run_check", lambda: _stub_result(["some drift"]))
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    """`--ci` escalates any error (new unreachable OR exit-lock breach) to
    EXIT_VIOLATION — this is what the CI hook relies on."""
    monkeypatch.setattr(gate, "run_check", lambda: _stub_result(["a breach"]))
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    """`--ci` returns 0 when there are no errors, even with INFO-level
    grandfathered entries present."""
    monkeypatch.setattr(gate, "run_check",
                        lambda: _stub_result(infos=["18 known-unwired"]))
    assert gate.main(["--ci"]) == gate.EXIT_OK


# ── chart supply face (P1-C) ──────────────────────────────────────────────
#
# The gate's original supply face is `scaffold_tenant.generate_defaults()` — a
# TOOL'S CAPABILITY, not the shipped deployment. These pin the second face: a
# key the scaffold path can supply but the chart does not ship leaves an
# operator with an alert that cannot fire.

_BASE = dict(
    demand={"a_key"},
    supply={"a_key"},
    deferred=set(),
    known_unwired={},
)


def test_chart_armed_key_is_silent():
    """Shipped by the chart → neither error nor info."""
    r = gate.run_check(**_BASE, chart_supply={"a_key"}, not_chart_armed=frozenset())
    assert r["errors"] == []
    assert r["infos"] == []


def test_reachable_but_not_shipped_is_an_error_when_unledgered():
    """The whole point of P1-C: scaffold can supply it, the chart cannot."""
    r = gate.run_check(**_BASE, chart_supply=set(), not_chart_armed=frozenset())
    assert any("NOT-CHART-ARMED" in e and "a_key" in e for e in r["errors"])


def test_ledgered_not_chart_armed_is_info_only():
    r = gate.run_check(**_BASE, chart_supply=set(), not_chart_armed=frozenset({"a_key"}))
    assert r["errors"] == []
    assert any(m.startswith("not-chart-armed a_key") for m in r["infos"])


def test_ledger_exit_lock_fires_when_chart_starts_shipping_it():
    """Ledger can only shrink: once shipped, the entry must be removed."""
    r = gate.run_check(**_BASE, chart_supply={"a_key"}, not_chart_armed=frozenset({"a_key"}))
    assert any("STALE-EXEMPTION" in e and "chart now" in e for e in r["errors"])


def test_ledger_exit_lock_fires_when_no_alert_demands_it():
    r = gate.run_check(
        demand=set(), supply={"a_key"}, deferred=set(), known_unwired={},
        chart_supply=set(), not_chart_armed=frozenset({"a_key"}),
    )
    assert any("STALE-EXEMPTION" in e and "no alert demands" in e for e in r["errors"])


def test_dead_key_is_not_also_reported_as_not_chart_armed():
    """A key dead on BOTH faces is reported once, as unreachable — adding
    '...and the chart lacks it too' would be noise."""
    r = gate.run_check(
        demand={"a_key"}, supply=set(), deferred=set(),
        known_unwired={"a_key": "grandfathered"},
        chart_supply=set(), not_chart_armed=frozenset(),
    )
    assert not any("NOT-CHART-ARMED" in e for e in r["errors"])
    assert any(m.startswith("known-unwired a_key") for m in r["infos"])


def test_ledger_entry_that_became_fully_dead_is_redirected():
    """Wrong-ledger drift: it belongs in KNOWN_UNWIRED now, not here."""
    r = gate.run_check(
        demand={"a_key"}, supply=set(), deferred=set(),
        known_unwired={"a_key": "grandfathered"},
        chart_supply=set(), not_chart_armed=frozenset({"a_key"}),
    )
    assert any("STALE-EXEMPTION" in e and "UNREACHABLE" in e for e in r["errors"])


def test_real_repo_chart_face_is_ledgered_and_non_vacuous():
    """Live-repo pin. The literal lower bound guards against the ledger being
    emptied or the chart face silently resolving to everything — an assertion
    derived from the thing it guards would not guard it."""
    r = gate.run_check()
    assert r["errors"] == []
    not_armed = [m for m in r["infos"] if m.startswith("not-chart-armed ")]
    assert len(not_armed) >= 20, (
        f"only {len(not_armed)} not-chart-armed keys — if the chart genuinely "
        "started shipping them the exit-lock above would have fired, so this "
        "more likely means the chart supply face stopped being read"
    )
    assert len(gate._chart_supply()) < len(gate._supply()), (
        "the shipped chart supplies at least as much as the scaffold generator — "
        "that would make this whole face a no-op; verify values.yaml parsed"
    )


# ── declared-list containment (#1310) ─────────────────────────────────────
#
# A KNOWN_UNWIRED key is one the platform supplies no value for. That is a
# posture only while the TENANT can supply one, which requires the key to be on
# the shipped `optional_overrides:` list — `ValidateTenantKeys` refuses an
# undeclared key. Off the list, nobody can set it and the alert is structurally
# unable to fire, which is precisely what nothing measured before.
#
# "the shipped list" is one list per FACE, and the faces reach different
# processes (see `_declared_faces`): the chart's feeds threshold-exporter, the
# onboarding generators' feeds the customer conf.d that tenant-api — the writer
# whose refusal this error is about — actually validates against.

_UNWIRED = dict(demand={"a_key"}, supply=set(), deferred=set(),
                chart_supply=set(), not_chart_armed=frozenset())


def test_grandfathered_key_off_the_declared_list_is_an_error():
    r = gate.run_check(**_UNWIRED, known_unwired={"a_key": "A: ..."},
                       declared_faces={"chart": set()})
    assert any("UNSETTABLE" in e and "a_key" in e for e in r["errors"]), r


def test_grandfathered_key_on_the_declared_list_stays_info_only():
    r = gate.run_check(**_UNWIRED, known_unwired={"a_key": "A: ..."},
                       declared_faces={"chart": {"a_key"}})
    assert r["errors"] == [], r
    assert any(m.startswith("known-unwired a_key") for m in r["infos"]), r


def test_a_key_present_on_one_face_and_missing_on_another_is_an_error():
    """The failure this PR was written for: the chart carries the list and the
    customer-side `_defaults.yaml` does not, so threshold-exporter recognises
    the key while tenant-api still answers 400. One green face must not mask
    the other, and the message must NAME the missing one — the repair is a
    different producer per face."""
    r = gate.run_check(
        **_UNWIRED, known_unwired={"a_key": "A: ..."},
        declared_faces={"chart": {"a_key"}, "onboarding/scaffold": set()},
    )
    bad = [e for e in r["errors"] if "UNSETTABLE" in e]
    assert len(bad) == 1, r
    assert "onboarding/scaffold" in bad[0], bad[0]
    assert "chart" not in bad[0].split("declared list of:")[1], (
        "the green face must not be named as missing: " + bad[0])


def test_containment_is_vacuous_for_callers_that_injected_a_synthetic_ledger():
    """Hermeticity pin, the same rule the chart face states: a caller that said
    nothing about the shipped list gets no narrowing, so every pre-existing
    synthetic test keeps meaning what it meant instead of being flagged for
    made-up keys that could not possibly be in the real values.yaml."""
    r = gate.run_check(**_UNWIRED, known_unwired={"a_key": "A: ..."})
    assert not any("UNSETTABLE" in e for e in r["errors"]), r


def test_a_reachable_grandfathered_key_is_not_double_reported():
    """Scope pin: an entry that is no longer dead is already an exit-lock error;
    adding '...and it is off the list too' would just duplicate it."""
    r = gate.run_check(
        demand={"a_key"}, supply={"a_key"}, deferred=set(),
        known_unwired={"a_key": "A: ..."},
        chart_supply={"a_key"}, not_chart_armed=frozenset(),
        declared_faces={"chart": set()},
    )
    assert any("STALE-EXEMPTION" in e for e in r["errors"]), r
    assert not any("UNSETTABLE" in e for e in r["errors"]), r


def test_real_repo_declares_every_grandfathered_key_on_every_face():
    """Live-repo pin (#1310). The 9 A-class keys are a deliberate posture ONLY
    because the platform ships their names — on every surface a tenant is
    validated against. Drop one anywhere and the posture silently becomes
    'this alert cannot fire and nobody can fix it'."""
    faces = gate._declared_faces()
    assert len(faces) >= 3, (
        "expected the chart face plus BOTH customer-side `_defaults.yaml` "
        f"producers; got {sorted(faces)}")
    for label, declared in faces.items():
        assert declared, (
            f"declared face {label!r} parsed as empty — an empty set would "
            "satisfy nothing and make the containment check vacuous")
        missing = sorted(set(gate.KNOWN_UNWIRED) - declared)
        assert not missing, (
            f"KNOWN_UNWIRED keys absent from {label}: {missing}")
    assert gate.run_check()["errors"] == []


def test_the_three_faces_agree_today():
    """Equivalence pin, and ONLY that: the three producers — the chart's
    values.yaml, `scaffold_tenant.generate_defaults`,
    `init_project._gen_defaults_yaml` — must render the same declared list,
    because a key a tenant is validated against on one surface must not be
    refused on another.

    ⛔ This does NOT prove the faces are independent artifacts. An earlier
    revision also asserted `chart is not scaffold and scaffold is not init`,
    which is vacuously true for EVERY possible implementation — each face
    builds and returns a fresh `set`, so the identity check passes even if all
    three read one file. Artifact-independence is pinned where it can actually
    fail, by `test_shipped_optional_reads_values_yaml_not_the_registry_tier`:
    it points one face at a synthetic values.yaml and requires that face to
    follow the file rather than the registry.
    """
    chart = gate._shipped_optional()
    scaffold = gate._onboarding_declared()
    init = gate._init_project_declared()
    assert chart == scaffold == init, (chart, scaffold, init)


# ── the chart face reads the ARTIFACT, not the registry declaration (#1310) ──

def test_shipped_optional_reads_values_yaml_not_the_registry_tier(
        tmp_path, monkeypatch):
    """Mutation pin for `_shipped_optional`'s stated discipline.

    Its docstring claims "reading values.yaml here means this face keeps
    measuring the deployment even if that equivalence ever breaks". A blind
    review swapped the body for a read of the registry's
    `tier: optional_overrides` declaration and EVERY test still passed — the
    two agree today, so only a DIVERGENCE can tell them apart.

    So: point the face at a synthetic values.yaml carrying a list that is
    nothing like the registry's, and require the face to return THAT. A
    registry-reading implementation ignores the file entirely and returns the
    nine real keys, so it fails here.

    Hermetic on purpose — no repo file is written. Perturbing the real
    values.yaml and restoring it would leave the workspace mangled on an
    interrupted run, which is exactly the defect this suite just fixed in
    test_check_threshold_registry.py's regen test.
    """
    probe = tmp_path / "values.yaml"
    probe.write_text(
        "thresholdConfig:\n"
        "  defaults:\n"
        "    zzz_probe_base: 1\n"
        "  optional_overrides:\n"
        "    - zzz_probe_declared_one\n"
        "    - zzz_probe_declared_two\n",
        encoding="utf-8")
    monkeypatch.setattr(gate, "_CHART_VALUES", probe)

    assert gate._shipped_optional() == {
        "zzz_probe_declared_one", "zzz_probe_declared_two"}, (
        "_shipped_optional did not follow the values.yaml it was pointed at — "
        "it is reading the registry declaration, not the shipped artifact, so "
        "the chart could stop shipping a key with nothing measuring it")
    # the sibling face on the same file must stay artifact-driven too
    assert gate._chart_supply() == {"zzz_probe_base"}


def test_shipped_optional_is_non_vacuous_on_the_real_artifact():
    """The hermetic pin above proves WHERE it reads; this proves the real file
    has something to read (an empty list would satisfy that pin and silently
    make every containment assertion vacuous)."""
    assert len(gate._shipped_optional()) >= len(gate.KNOWN_UNWIRED)


# ── the defaults-tier face: `<base>_critical` placement (#1218 / TRK-344) ──
#
# A different question from every face above. Those ask "can this key be
# produced"; this one asks "is this key in a section the resolver can act on".
# `_defaults.yaml` has exactly one shape where the wrong section fails silently
# in both directions at once, and the exporter behaviour both directions rest on
# is measured in
# components/threshold-exporter/app/pkg/config/critical_tier_placement_test.go —
# not inferred here from reading resolve.go.

def test_critical_key_in_a_defaults_face_is_an_error():
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe face": _flat("pg_connections",
                                             "pg_connections_critical")}, {}),
    )
    hits = [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(hits) == 1, result
    assert "pg_connections_critical" in hits[0]
    assert "probe face" in hits[0], "the message must name the producer to repair"


def test_clean_defaults_face_is_silent():
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe face": _flat("pg_connections",
                                             "mysql_threads_running")}, {}),
    )
    assert result["errors"] == [], result


def test_every_offending_key_is_reported_not_just_the_first():
    """One error per key, because the repair is per key: a message naming only
    the first would read as "fix this one" on a face carrying sixteen."""
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat("a_b", "a_b_critical",
                                        "c_d", "c_d_critical")}, {}),
    )
    hits = [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(hits) == 2, hits


def test_an_empty_defaults_face_is_an_error_not_a_pass():
    """⛔ The vacuity trap this face is most likely to fall into. Every producer
    ships a non-empty `defaults:`, so an empty set means the reader broke — a
    moved file, a renamed section, a generator that started nesting elsewhere —
    and an empty set passes the placement check perfectly.
    """
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe face": {}}, {}),
    )
    assert any("EMPTY-FACE" in e and "probe face" in e for e in result["errors"]), result


def test_dimensional_key_in_a_defaults_face_is_an_error():
    """The other half of the rule `TestDocsDefaultsSamplesHaveNoTenantOnlyKeys`
    pins. Covering only `_critical` (the half #1218 was reported about) leaves
    the equally-inert shape unguarded on every surface this face covers.

    ⛔ No producer COUNT here on purpose: "all six" stood in this docstring and
    in the Go pin, and both went stale the moment the artifact half became
    derived (4 enumerated generators + however many the predicate finds — 17
    today). A number restated in three places is a number that will be corrected
    in one of them.
    """
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat("oracle_tablespace",
                                        'oracle_ts{env="prod"}')}, {}),
    )
    hits = [e for e in result["errors"] if "DIMENSIONAL-IN-DEFAULTS" in e]
    assert len(hits) == 1, result
    assert 'oracle_ts{env="prod"}' in hits[0]


def test_critical_must_be_a_SUFFIX_not_a_substring():
    """⛔ The rule is about the SUFFIX, because that is what the resolver keys
    off: `resolveCriticalRows` filters on `strings.HasSuffix(key, "_critical")`
    and derives the base with `TrimSuffix`. A key that merely CONTAINS the token
    is an ordinary metric and belongs in `defaults:` like any other.

    Pinned because relaxing `endswith` to `in` is invisible to every other case
    here — no shipped key contains `_critical` off the end, so the whole suite
    stays green while the gate starts failing CI on a legitimate key (found by
    mutation, not by review). The failure direction is a false positive rather
    than a miss, which is exactly why nothing else would surface it.
    """
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat("redis_critical_path_latency",
                                        "pg_criticality_score")}, {}),
    )
    assert [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e] == [], result


def test_the_defaults_face_is_wired_into_run_check_not_just_callable(monkeypatch):
    """⛔ The face has to be reachable from the PRODUCTION path, not merely
    correct when called directly.

    Measured (blind review, round 2): setting `defaults_faces = {}` where
    `run_check` defaults it turned the whole #1218 guard off — `--ci` printed
    `✅ threshold reachability OK` — and of 41 lint tests exactly ONE went red,
    a fail-closed test that only noticed as a side effect of its own
    monkeypatch. Every other test here calls `gate._defaults_faces()` directly
    and so cannot see the wiring at all. That is the shape where a guard keeps
    passing its own unit tests while no longer guarding anything.

    So this one injects a dirty face at the SEAM `run_check` resolves — never
    passing `defaults_faces=` — and asserts the error surfaces.
    """
    called = []

    def _dirty():
        called.append(True)
        return ({"injected face": _flat("pg_connections",
                                        "pg_connections_critical")}, {})

    monkeypatch.setattr(gate, "_defaults_faces", _dirty)
    result = gate.run_check(demand=set(), supply=set(), deferred=set(),
                            known_unwired={})

    assert called, "run_check never consulted _defaults_faces — the face is unwired"
    hits = [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(hits) == 1, result
    assert "injected face" in hits[0]


# The four producers the generator half must consist of — EXACTLY these, no
# more and no fewer. Matched as identifiers inside each label rather than by
# comparing whole label strings, so rewording a label's prose does not fail this
# but adding, dropping or renaming a PRODUCER does.
_EXPECTED_GENERATOR_PRODUCERS = {
    "values.yaml",
    "scaffold_tenant.generate_defaults",
    "init_project._gen_defaults_yaml",
    "onboard_platform.generate_defaults_from_candidates",
}


def test_real_repo_defaults_faces_are_all_present_and_clean():
    """The live faces, in their two classes.

    ⛔ The generator half is pinned by SET EQUALITY against the producers it must
    consist of, not by `len(...) == 4`. A count cannot see a fifth generator
    arriving misfiled — that was the whole of #1393: with the old label-prefix
    discriminator, a generator whose label began `artifact (` was tallied as an
    artifact and the count stayed at 4. Blind review then measured the same hole
    surviving a `kind` field, because a hand-written field is as writable as a
    hand-written label. Set equality over producers sees all three shapes
    (added / dropped / renamed), and the two classes now come back from
    `_defaults_faces()` separately so nothing DECLARES which class it is in.

    The ARTIFACT side cannot be a literal (it is a walk, and files come and go),
    so it is pinned by a floor plus named hard-to-guess members in
    `test_the_artifact_face_is_derived_not_enumerated`.

    ⛔ The cleanliness assertion runs over BOTH classes; the NON-EMPTY assertion
    runs over generators only. Measured: two artifacts
    (`rule-packs/recipes/examples/conf.d/_defaults.yaml` and its `finance/`
    child) legitimately declare no `defaults:` at all, so requiring non-empty
    everywhere turned this gate red on two correct files.
    """
    generators, artifacts = gate._defaults_faces()

    matched: dict[str, str] = {}
    for label in generators:
        hits = {p for p in _EXPECTED_GENERATOR_PRODUCERS if p in label}
        assert len(hits) == 1, (
            f"generator face {label!r} names {len(hits)} known producer(s) "
            f"{sorted(hits)} — a new generator must be added to "
            "_EXPECTED_GENERATOR_PRODUCERS, not left to be counted")
        producer = hits.pop()
        assert producer not in matched, (producer, matched[producer], label)
        matched[producer] = label
    assert set(matched) == _EXPECTED_GENERATOR_PRODUCERS, sorted(generators)

    assert len(artifacts) >= gate._DEFAULTS_ARTIFACT_FLOOR, sorted(artifacts)

    for face, keys in generators.items():
        assert keys, f"{face} yielded nothing — the reader is broken"
    for face, keys in {**generators, **artifacts}.items():
        assert not [k for k in keys if k.endswith("_critical")], face
        assert not [k for k in keys if "{" in k], face


def test_the_onboard_face_is_a_live_probe_not_a_constant():
    """⛔ The `onboard` face feeds a synthetic candidate pair, so it is the one
    face that could silently measure nothing at all — a producer that started
    returning `{}` would leave the set empty, and empty is exactly what a clean
    face looks like (the EMPTY-FACE branch catches that, but only if the probe
    still reaches a producer). This pins that the probe's WARNING half comes
    back, which is only true if the producer ran and routed by tier.
    """
    import sys as _sys
    if str(gate._OPS) not in _sys.path:
        _sys.path.insert(0, str(gate._OPS))
    import onboard_platform

    out = onboard_platform.generate_defaults_from_candidates(
        [dict(c) for c in gate._ONBOARD_PROBE])
    assert out["defaults"] == {"zzz_probe_metric": "80"}
    assert out["critical_overrides"] == {"zzz_probe_metric_critical": "150"}

    generators, _artifacts = gate._defaults_faces()
    onboard_face = [v for k, v in generators.items() if "onboard_platform" in k]
    assert onboard_face == [{"zzz_probe_metric": 0}], generators


def test_a_missing_artifact_is_fail_closed_and_names_the_path(monkeypatch, capsys):
    """The EMPTY-FACE message says a MISSING artifact does not reach it. That is
    a claim about control flow, so it is pinned rather than asserted: the reader
    raises, `main()` turns it into EXIT_CALLER_ERROR, and the path is in the
    message. Without this, "fail-closed" is an article of faith about a branch
    nobody walked."""
    from pathlib import Path

    real = gate._defaults_artifacts()
    monkeypatch.setattr(
        gate, "_defaults_artifacts",
        lambda: real[:-1] + [Path(str(real[-1]) + ".gone")])
    rc = gate.main([])
    assert rc == gate.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "crashed" in err and "_defaults.yaml.gone" in err, err


def test_the_reader_supplies_the_path_that_the_parser_does_not(tmp_path):
    """⛔ The branch `_read_artifact_keys` exists for, walked at last.

    Two tests already claim the reader "names the path", and both pass WITHOUT
    the wrapper: they delete a file, and `FileNotFoundError` carries the path by
    itself. The branch the wrapper was actually written for — round 4's finding
    that `yaml.safe_load` reports `<unicode string>` and no filename — had never
    been executed by anything. So the provenance claim rested on the one failure
    mode that does not need it.

    Both halves are pinned here, because either alone is satisfiable by an
    accident: the parser really does hide the file, AND the wrapper really does
    supply it.

    ⛔ `PROJECT_ROOT` is re-pointed rather than a broken file being dropped into
    the real tree: `_read_artifact_keys` uses it only to compute the relative
    path for the message, and a malformed `_defaults.yaml` written into this
    repo — even briefly — is exactly the state four other floors exist to catch.
    """
    rel = "some/conf.d/_defaults.yaml"
    bad = tmp_path / rel
    bad.parent.mkdir(parents=True)
    # genuinely malformed: a mapping value where YAML cannot accept one
    bad.write_text("defaults:\n  a: b: c\n", encoding="utf-8")

    # 1) the parser's own error names no file — the premise for the wrapper.
    with pytest.raises(Exception) as raw:
        gate._defaults_section(bad.read_text(encoding="utf-8"))
    assert "_defaults.yaml" not in str(raw.value), (
        "the parser now names the file on its own; if that is really true the "
        f"wrapper's justification has changed, so re-read it: {raw.value}")

    # 2) …and the wrapper supplies it.
    real_root = gate.PROJECT_ROOT
    try:
        gate.PROJECT_ROOT = tmp_path
        with pytest.raises(RuntimeError) as wrapped:
            gate._read_artifact_keys(bad)
    finally:
        gate.PROJECT_ROOT = real_root
    assert gate.PROJECT_ROOT == real_root
    assert str(wrapped.value).startswith(f"{rel}: "), wrapped.value
    # …and the provenance really came from the wrapper, not from the cause.
    assert rel not in str(wrapped.value.__cause__), wrapped.value.__cause__


def test_the_artifact_face_is_derived_not_enumerated():
    """⛔ The three ways the hardcoded-path version was walked past (blind
    review, round 3 — each demonstrated by injecting `mysql_connections_critical`
    and watching this gate exit 0):

      1. an `examples/` sibling INSIDE the very conf.d tree the constants named;
      2. a NESTED `_defaults.yaml` (the loader enters one at every depth, see
         `config_hierarchy.go`; the constants named depth 0 only);
      3. a second conf.d root entirely (`rule-packs/recipes/examples/conf.d/`).

    Pinned by naming members no enumeration would have guessed, rather than by a
    count alone — a count is satisfied by any ten files.
    """
    found = {p.relative_to(gate.PROJECT_ROOT).as_posix()
             for p in gate._defaults_artifacts()}

    for missed in (
        # 1 — sibling in the same tree
        "components/threshold-exporter/config/conf.d/examples/_defaults-multidb.yaml",
        # 2 — depth: the loader reads L1..L3 too
        "tests/golden/fixtures/full-l0-l3/conf.d/db/_defaults.yaml",
        "tests/golden/fixtures/full-l0-l3/conf.d/db/mariadb/prod/_defaults.yaml",
        # 3 — a second conf.d root
        "rule-packs/recipes/examples/conf.d/_defaults.yaml",
        "rule-packs/recipes/examples/conf.d/finance/_defaults.yaml",
    ):
        assert missed in found, (missed, sorted(found))

    # the two the old version DID cover must not have been dropped in the swap
    for kept in ("components/threshold-exporter/config/conf.d/_defaults.yaml",
                 "try-local/seed/conf.d/_defaults.yaml"):
        assert kept in found, (kept, sorted(found))

    assert len(found) >= gate._DEFAULTS_ARTIFACT_FLOOR, sorted(found)


def test_an_empty_artifact_scan_is_an_error_not_a_pass(monkeypatch):
    """⛔ `EMPTY-FACE` guards a face whose `defaults:` is empty. It cannot guard a
    WALK that returned nothing, because zero faces means zero iterations and the
    per-face loop is then perfectly satisfied — the same vacuity the hardcoded
    pair shipped with, one level up. So the floor raises before the loop.
    """
    # ⛔ Patches the SCAN, not the post-exemption list: the floor deliberately
    # counts what the scan found, so that exempting files cannot walk the total
    # down to the floor and then have the message blame the scan.
    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", list)
    with pytest.raises(RuntimeError, match="below the floor"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})


def test_the_two_floors_blame_the_right_cause(monkeypatch):
    """⛔ Two ways to end up with nothing to check, two remedies — and the
    previous version had only one floor, on the scan side.

    That was itself a fix: with the single floor counted AFTER exemptions, a long
    exemption list tripped it and the message said "repair the scan or restore
    the file(s)", forbidding the only correct action. Moving it removed the
    tripwire on over-exemption entirely — measured: 17/17 exempted raised nothing
    and read ZERO faces, and the test written alongside asserted that silence was
    correct. **The fix pinned the hole as a contract** (blind review, round 5).

    So: a few exemptions must NOT trip the scan floor (the scan is fine), and
    exempting everything MUST trip the read floor with a message naming the
    exemption list.
    """
    scanned = gate._tracked_defaults_artifacts()
    assert len(scanned) >= gate._DEFAULTS_ARTIFACT_FLOOR, scanned

    # A couple of exemptions: scan floor must stay quiet, read floor too.
    # ⛔ They have to be NON-shipped files. Exempting a shipped one now trips
    # `_SHIPPED_CONFD_ROOTS` first — correctly, since the exemption table is the
    # other way a shipped root stops being checked, and it used to be completely
    # silent. That is pinned separately in
    # `test_exempting_a_shipped_artifact_is_not_silent`; this test is about the
    # two file floors, so it must not collide with it.
    non_shipped = [rel for rel in scanned
                   if not any(rel == root or rel.startswith(root + "/")
                              for root in gate._SHIPPED_CONFD_ROOTS)]
    # …and a THIRD collision to dodge, same reason as the two above: the #1411
    # per-root floor fires when a root contributes nothing, and 10 of the 13
    # roots hold a single file — exempting that file empties its root and that
    # floor speaks first. So only files whose root has a sibling are usable
    # here. ⛔ This is picking a probe that isolates the floor under test, NOT
    # weakening it: the per-root floor's own behaviour is pinned in
    # `test_a_root_that_still_has_files_but_zero_keys_is_a_violation`.
    _root_size: dict[str, int] = {}
    for rel in scanned:
        r = gate.conf_d_root(rel)
        if r:
            _root_size[r] = _root_size.get(r, 0) + 1
    non_shipped = [rel for rel in non_shipped
                   if _root_size.get(gate.conf_d_root(rel) or "", 0) >= 2]
    assert len(non_shipped) >= 2, (
        "no multi-file non-shipped root left to probe with; this test needs two "
        f"files that can be exempted without emptying their root: {non_shipped}")
    # …and the two SMALLEST of them, so the artifact key floor (a third floor,
    # pinned in its own test) does not fire first and mask which floor this test
    # is actually about.
    by_keys = sorted(non_shipped, key=lambda rel: len(gate._defaults_section(
        (gate.PROJECT_ROOT / rel).read_text(encoding="utf-8"))))
    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_EXEMPT",
                        {rel: "x" * 40 for rel in by_keys[:2]})
    result = gate.run_check(demand=set(), supply=set(), deferred=set(),
                            known_unwired={})
    assert not [e for e in result["errors"] if "floor" in e], result

    # everything exempted: the READ floor must fire, and must blame exemptions
    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_EXEMPT",
                        {rel: "x" * 40 for rel in scanned})
    assert gate._defaults_artifacts() == []
    with pytest.raises(RuntimeError, match="SHORTEN THE EXEMPTION LIST") as exc:
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})
    # …and must NOT send the reader to repair a scan that is working
    assert "The scan is FINE" in str(exc.value)


# ── the recursive `defaults:` walk (#1392) ───────────────────────────────────
#
# The reader used to look at the top level of `defaults:` only. Measured on the
# tree at the time: an injection one level down was completely silent, while the
# same key at the top level was caught — so for every artifact that keeps its
# keys nested, this gate was reporting on a layer that could not hold the defect.

_NESTED_DOC = """defaults:
  threshold:
    cpu: 70
    mysql_connections_critical: 90
  alert_group: baseline
"""


def test_the_walk_reaches_every_depth_not_just_the_top():
    """The witness for #1392, with its counterfactual spelled out.

    ⛔ Both halves matter. "The recursive reader finds it" alone would also be
    true of a reader that returns every string in the file; the second assertion
    is what makes this a statement about the CHANGE — the top-level view of the
    very same document does not contain the key, which is exactly why the old
    reader was silent here.
    """
    keys = gate._defaults_section(_NESTED_DOC)
    assert "threshold.mysql_connections_critical" in keys, keys

    top_level_only = set(yaml.safe_load(_NESTED_DOC)["defaults"])
    assert not [k for k in top_level_only if k.endswith("_critical")], (
        "the counterfactual is void — this document must be invisible to a "
        "top-level reader, or it proves nothing about the walk")


def test_a_nested_misplacement_is_an_error_end_to_end():
    """…and the walk is wired to the rule, not merely correct in isolation."""
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": gate._defaults_section(_NESTED_DOC)}, {}),
    )
    hits = [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(hits) == 1, result
    assert "threshold.mysql_connections_critical" in hits[0], (
        "the message must name the PATH — 'mysql_connections_critical' alone "
        "sends the reader to the top level, which is not where it is")


def test_the_nested_message_names_the_consequence_that_actually_happens():
    """⛔ A nested key does NOT produce a warning-severity series — the file does
    not decode at all (`Defaults` is `map[string]float64`), so the whole
    platform-defaults block is dropped with a parse-failure metric. Telling the
    reader to go looking for `user_threshold{severity="warning"}` would send
    them after a series that is not there.
    """
    nested = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _nested(threshold={"pg_connections_critical": 1})}, {}),
    )["errors"][0]
    flat = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat("pg_connections_critical")}, {}),
    )["errors"][0]

    # ⛔ The nested branch must NOT hang the nesting's own consequence on this
    # key. Measured (blind review): the previous wording said "the file does not
    # decode at all", a maintainer moved only the `_critical` half, the nesting
    # stayed, and this gate went GREEN on a file that still does not decode.
    assert "FLAT map" in nested, nested
    assert "SECOND, independent problem" in nested, nested
    assert "removing this key does not make the section decodable" in nested, nested
    assert 'severity="warning"' not in nested, nested
    # …and the flat case keeps the silent-series explanation, which IS its shape
    assert 'severity="warning"' in flat, flat
    assert "independent problem" not in flat, flat


def test_the_message_never_spells_a_one_segment_metric_label():
    """⛔ `parseMetricKey` splits on the FIRST underscore, so the emitted series
    is `{component=<prefix>, metric=<rest incl. _critical>}`. This message used
    to print `metric="<whole key>"` — which is not a series that exists, and is
    **the exact shape of #731**: rule packs queried the whole key while the
    exporter emitted the split form, the join was empty, and alerts failed
    silently. `app/rulepack_contract_test.go` exists to stop that coming back and
    warns that a Python re-implementation of the split would be a new echo
    chamber — so the message describes the shape and points at the contract
    instead of re-deriving the labels.

    ⛔ ALL FOUR message branches, not just the flat `_critical` one. Measured
    (blind review, mutation): with only the flat case covered, putting the #731
    spelling back into the NESTED branch left the suite green — `_report_placement`
    emits four different messages and the name of this test claims all of them.
    """
    faces = {
        "flat critical": _flat("pg_connections_critical"),
        "nested critical": _nested(threshold={"pg_connections_critical": 1}),
        "flat dimensional": _flat('pg_conn{env="prod"}'),
        "nested dimensional": _nested(threshold={'pg_conn{env="prod"}': 1}),
        # ⛔ The two shapes the dotted-path split lost entirely: a label VALUE
        # containing a dot. `{tablespace=~"SYS.*"}` is the mainstream dimensional
        # form in this repo (collector_test.go:567), and an IP or a version
        # string does it too. Under `"{" in k.rsplit(".", 1)[-1]` these yielded
        # `*"}` / `1"}` and produced NO message at all — so this test's own
        # `seen == len(faces)` would have counted them as covered while the
        # branch never ran.
        "flat dimensional, dotted label": _flat('ora_ts{tablespace=~"SYS.*"}'),
        "flat dimensional, dotted ip": _flat('node_load{instance="10.0.0.1"}'),
    }
    seen = 0
    for label, face in faces.items():
        for msg in gate.run_check(
            demand=set(), supply=set(), deferred=set(), known_unwired={},
            defaults_faces=({label: face}, {}),
        )["errors"]:
            seen += 1
            for key in face:
                assert f'metric="{key}"' not in msg, (label, msg)
    assert seen == len(faces), f"expected one message per branch, got {seen}"

    msg = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat("pg_connections_critical")}, {}),
    )["errors"][0]
    assert "FIRST underscore" in msg, msg
    assert "rulepack_contract_test.go" in msg, msg
    # the Go contract this defers to must still be there under that name
    contract = (gate.PROJECT_ROOT / "components" / "threshold-exporter" / "app"
                / "rulepack_contract_test.go")
    assert contract.is_file(), contract


def test_the_nested_suggestion_names_a_key_and_not_a_path():
    """⛔ The nested branch must suggest a USABLE key, not the rendered path.

    Distinct from the test above, which polices the #731 metric-label SHAPE.
    This one polices the *identity* of the key the message tells you to create:
    the message says "no key of that name exists at any tier" about the dotted
    path, so suggesting an override keyed by that same dotted path contradicts
    its own previous sentence and sends the reader to write a key that resolves
    nowhere. Measured (round-5 mutation): reverting the `rsplit` left all 87
    tests green — the shape guard cannot see this because the wrong key is not
    spelled `metric="..."`.
    """
    msg = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _nested(threshold={"pg_connections_critical": 1})}, {}),
    )["errors"][0]
    m = re.search(r"override keyed '([^']+)'", msg)
    assert m, f"message no longer names the override key: {msg}"
    suggested = m.group(1)
    assert "." not in suggested, (
        f"suggested override key {suggested!r} is a rendered path, not a key; "
        f"the same message says no key of that name exists. Full message: {msg}"
    )
    # ⛔ The FIRST version of this test asserted `== "pg_connections"` and so
    # pinned a wrong key in place: `resolveCriticalRows` (resolve.go:502) skips
    # any override key that does not end in `_critical`, so an override keyed
    # `pg_connections` yields a warning-severity base row and NO critical row —
    # the exact silent failure this gate exists to prevent. A guard that checks
    # only the SHAPE will happily certify the wrong identity.
    assert suggested == "pg_connections_critical", (suggested, msg)
    # …and the base must be named too, because resolveCriticalRows skips a
    # second time when `defaults[base]` is absent (resolve.go:520).
    assert "'pg_connections'" in msg, msg


def test_a_top_level_critical_key_with_a_mapping_value_is_its_own_case():
    """⛔ Top level AND a mapping is neither flat nor nested.

    `depth == 0` answers "is it top level", not "is it a leaf". Before the walk
    returned `has_children`, this input took the FLAT branch and promised a
    `severity="warning"` series — but `Defaults` is `map[string]float64`
    (types.go:208), so the value does not unmarshal, the whole file is rejected
    and nothing is emitted at all. Measured (round-6 mutation): removing the
    branch left all 88 tests green.
    """
    face = _nested(**{"mysql_connections_critical": {"a": 1}})
    msg = [e for e in gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": face}, {}),
    )["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(msg) == 1, msg
    assert "MAPPING value" in msg[0], msg[0]
    # ⛔ and it must NOT promise the flat consequence…
    assert "severity=" not in msg[0], msg[0]
    # …nor claim the key name is missing, which is the nested branch's wording:
    # the name IS at the top level here, only its value is wrong.
    assert "no key of that name" not in msg[0], msg[0]


def test_the_leaf_comes_from_the_walk_even_when_the_leaf_contains_a_dot():
    """⛔ A leaf key may legally contain a dot; `rsplit('.', 1)` then lies.

    `defaults: {threshold: {"a.b_critical": 1}}` renders as the path
    `threshold.a.b_critical`, whose last dotted segment is `b_critical` — but
    the KEY is `a.b_critical`, so an override keyed `b_critical` resolves
    nowhere. Measured (round-6 mutation): re-splitting the path instead of
    asking the walk left all 88 tests green, because every other nested probe
    in this file has a dot-free leaf.
    """
    face = _nested(threshold={"a.b_critical": 1})
    msg = [e for e in gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": face}, {}),
    )["errors"] if "CRITICAL-IN-DEFAULTS" in e]
    assert len(msg) == 1, msg
    m = re.search(r"override keyed '([^']+)'", msg[0])
    assert m, msg[0]
    assert m.group(1) == "a.b_critical", (m.group(1), msg[0])


def _root_face(root: str, *keys: str):
    """One artifact under `root`, carrying `keys` at the top level."""
    return [(f"{root}/_defaults.yaml", _flat(*keys))]


def test_a_root_that_still_has_files_but_zero_keys_is_a_violation():
    """⛔ The gap #1411 was opened for: files present, keys gone.

    Every file-count floor is unmoved (the file is still there) and the global
    key floor has margin, so before this floor the tree was invisible to all of
    them.

    ⛔ THE FROZEN FIGURES USED TO BE RESTATED HERE, IN FULL, and that is exactly
    how the pair of them came apart. This docstring carried its own copy of both
    measurements and all of their conditions, and the copy had drifted from the
    one beside `_DEFAULTS_CONFD_ROOTS`: it paired ①'s post-exemption numerator
    with its pre-exemption denominator, it called the pre-exemption fraction
    "the same fact stated without the exemption" (it is not — the two count
    different populations), and it said the exemption made no difference to ②
    (true of ②'s numerator, false of its denominator). A duplicated measurement
    is two things that have to be edited together, which is the same defect as a
    number nobody re-measures, one level up.

    ⛔ SINGLE SOURCE, and the pointer is the part that had to be corrected. The
    INTEGERS live in the assertions of
    `test_the_frozen_measurements_replay_against_their_own_commit`, which
    re-measures them inside `72fdaf56` rather than comparing them against
    today's tree. The CONDITIONS that pick out each measurement are stated once,
    in the comment above `_DEFAULTS_CONFD_ROOTS`. ⚠️ The previous wording sent
    the reader to that comment for the figures — after the figures had already
    been taken out of it. Two pointers aimed at each other is what a
    single-source claim looks like when nobody re-reads the other end.

    Today's figure is likewise NOT here: `main()` re-derives and prints it every
    run (`_roots_only_the_fifth_floor_notices`), and
    `test_the_fifth_floor_figure_is_printed_with_its_universe_and_floor_subset`
    pins that it is printed with its conditions attached.

    ⚠️ ONE thing worth keeping HERE, because it is about this test's own probe
    and belongs nowhere else: ①'s post-exemption numerator and the "of 17
    tracked ARTIFACTS" figure in `_walk_defaults_keys` share an integer by
    coincidence. They are different populations answering different questions
    (the latter is the expressiveness of the top-level-only reader, #1392), and
    neither may ever be edited to "agree" with the other.
    """
    root = "tests/golden/fixtures/l0-only/conf.d"
    assert root in gate._DEFAULTS_CONFD_ROOTS, "probe must use a pinned root"
    by_root = {r: _root_face(r, "x") for r in gate._DEFAULTS_CONFD_ROOTS}
    by_root[root] = [(f"{root}/_defaults.yaml", {})]      # renamed `defaults:`

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root)
    msg = str(exc.value)
    assert root in msg and "ZERO threshold keys" in msg, msg
    # the likeliest cause leads, and the exemption list is NOT offered as the fix
    assert "renamed" in msg, msg
    assert "is NOT the remedy" in msg, msg


def test_counterfactual_the_other_floors_are_silent_on_the_exact_same_input():
    """⛔ The claim this floor is sold on: BEFORE it, this input was all green.

    "Injecting the defect turns the suite red" only proves the defect is caught
    NOW. What justifies a fifth floor is that the same input walks past the other
    four — measured here rather than asserted, on real faces with one real root
    emptied. (This repo has been burned three times claiming new detection power
    without measuring the before.)
    """
    _generators, artifacts = gate._defaults_faces()
    victim = "tests/golden/fixtures/l0-only/conf.d"
    kept = {face: ({} if victim in face else keys)
            for face, keys in artifacts.items()}
    assert any(victim in f for f in kept), "probe root not in the real scan"
    assert sum(len(k) for k in kept.values()) < sum(
        len(k) for k in artifacts.values()), "probe removed nothing"

    # floor 3 (global, per class) — silent: the loss is inside its margin
    gate._assert_keys_floor(_generators, kept)
    # floor 4 (per shipped root) — silent: the victim is a fixture, not shipped
    by_root = {r: [(f, k) for f, k in kept.items() if r in f]
               for r in gate._SHIPPED_CONFD_ROOTS}
    gate._assert_shipped_roots_intact(by_root)
    # floors 1-2 count FILES, and the file is still there — nothing to assert.

    # …and the fifth one, on the same input, does fire.
    all_by_root: dict[str, list] = {}
    for face, keys in kept.items():
        rel = face.split("(", 1)[1].rstrip(")")
        root = gate.conf_d_root(rel)
        if root:
            all_by_root.setdefault(root, []).append((rel, keys))
    with pytest.raises(gate._GateViolation, match="ZERO threshold keys"):
        gate._assert_every_root_contributes(all_by_root)


def test_a_root_that_vanished_is_reported_differently_from_one_that_emptied():
    """Gone and empty have different remedies, so they are different messages.

    ⛔ `all_by_root` is deliberately not pre-seeded from the pin — seeding it
    would turn a vanished root into an empty one and hand the reader the wrong
    repair.
    """
    by_root = {r: _root_face(r, "x") for r in gate._DEFAULTS_CONFD_ROOTS}
    gone = by_root.pop("try-local/seed/conf.d")
    assert gone, "sanity: the probe removed something"

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root)
    msg = str(exc.value)
    assert "no artifact at all" in msg, msg
    assert "ZERO threshold keys" not in msg, msg


def test_an_unpinned_new_root_is_a_violation_because_nothing_would_watch_it():
    """A new root is fine; an UNPINNED one is unwatched.

    Without this, adding a fixture tree silently opts it out of the only floor
    that can see it go to zero — and a pin that lags reality cannot detect a
    disappearance either.
    """
    by_root = {r: _root_face(r, "x") for r in gate._DEFAULTS_CONFD_ROOTS}
    by_root["tests/golden/fixtures/brand-new/conf.d"] = _root_face(
        "tests/golden/fixtures/brand-new/conf.d", "y")

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root)
    assert "not pinned" in str(exc.value), exc.value


def test_the_may_be_empty_list_is_not_a_blanket_exemption():
    """Only roots that legitimately declare no thresholds may be empty.

    ⛔ The list must stay a subset of the pin: an entry naming a root that is not
    watched at all would read as an exemption while exempting nothing, and the
    next person would add real roots to it expecting the same harmlessness.
    """
    assert gate._DEFAULTS_ROOTS_MAY_BE_EMPTY <= gate._DEFAULTS_CONFD_ROOTS, (
        gate._DEFAULTS_ROOTS_MAY_BE_EMPTY - gate._DEFAULTS_CONFD_ROOTS)
    # and it must stay small enough to read: every entry needs a stated reason,
    # which only holds while a human is writing them one at a time.
    assert 1 <= len(gate._DEFAULTS_ROOTS_MAY_BE_EMPTY) <= 3, (
        "more than a handful of empty-by-design roots means the rule itself "
        "is wrong (do not grow this list to make the floor quiet); an EMPTY "
        "list makes the exemption below pass vacuously")

    # ⛔ EVERY exempt root is built empty, not just the one under test. Handing
    # the others a key trips a DIFFERENT arm — the one that makes the exemption
    # list earn its entries on every run — so the moment this list grows a
    # second entry the test would go red for a reason that has nothing to do
    # with what it asserts. Measured: with a second (real, pinned) root added to
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY`, the old construction failed on
    # "...is listed in _DEFAULTS_ROOTS_MAY_BE_EMPTY, but it contributes 1
    # threshold key(s) today" — a true message about a fixture the test built
    # itself. Same construction as
    # `test_the_may_be_empty_list_must_earn_its_entries_on_every_run`, and for
    # the same reason.
    by_root = {r: ([(f"{r}/_defaults.yaml", {})]
                   if r in gate._DEFAULTS_ROOTS_MAY_BE_EMPTY
                   else _root_face(r, "x"))
               for r in gate._DEFAULTS_CONFD_ROOTS}
    gate._assert_every_root_contributes(by_root)      # must NOT raise


# ⛔ test_the_pin_matches_what_the_real_repo_actually_has was DELETED here
# (un-backticked deliberately — see
# test_every_test_name_this_module_cites_actually_exists)
# (#1434), and this note is what stops it being written again. It asserted
# `pin == the roots the defaults scan reached`, having first called
# `_defaults_faces()` — which enforces that same equality in both directions
# (`missing` and `unpinned`). Its left side was not merely similar to the gate's
# `seen`, it was the same `read_pairs` through the same `_bucket_by_root`, so
# the equality could not be false at the line that asserted it.
#
# Measured (17 single-point mutations of the gate): it never once spoke while
# the two arms were intact. It reddened in three of them, all of which first
# disabled the arm that would otherwise have answered — and in every one of the
# three, `test_every_tracked_confd_root_is_registered_somewhere` reddened too.
# Its only sole red was a FALSE one: renaming the artifact label format made its
# hand-written `face.split("(", 1)` raise `IndexError` on a legal refactor.
#
# ⚠️ "It was dead code" would be too strong and is not the reason it went: its
# diff message DOES print once an arm is disabled. It went because nothing it
# says is unowned. Who owns what now, precisely — the parts do not overlap and
# no one of them is a superset:
#   * `scanned ⊆ pin` — `test_every_tracked_confd_root_is_registered_somewhere`
#     below. Its two set relations plus `without & scanned` give exactly this,
#     and no more: a pinned root holding no artifact satisfies all of them.
#   * `pin − may_be_empty ⊆ scanned` — the equality at the top of
#     test_every_scanned_root_is_wired_to_the_fifth_floor_through_the_real_entry
#     (written without backticks so the name survives one grep on one line).
#   * both directions at runtime — the gate's own `missing` / `unpinned` arms,
#     which is where a user meets them, each with a test that drives it.
def _scanned_confd_roots() -> set[str]:
    """Roots holding a defaults artifact, DERIVED from the scan, not the pin.

    ⛔ Deriving them from `_DEFAULTS_CONFD_ROOTS` would make every test below
    assert that the gate is consistent with its own pin — true by construction
    the day someone drops a root from the pin, which is one of the two things
    the pin exists to make loud.
    """
    roots = set()
    for rel in gate._tracked_defaults_artifacts():
        root = gate.conf_d_root(rel)
        if root:
            roots.add(root)
    return roots


def _blind_roots_recounted(generators, read_pairs) -> list[str]:
    """The fifth floor's numerator, WITHOUT the function that reports it.

    ⛔ `_roots_only_the_fifth_floor_notices` is an aggregator over
    `_floors_that_notice_a_zeroed_root`, and every test of the printed figure
    used to compute its "expected" by calling that same aggregator — which makes
    the assertion `f(x) == f(x)`, true for every implementation of `f`.
    Measured: dropping the first element of its return value left the suite at
    107 passed and the gate at rc=0, with `main()` printing `6 of 11` where the
    truth is 7 — the whole numerator can be silently short by one, on the one
    figure this floor exists to publish.

    So the contract is restated here over the building block instead: the result
    is EXACTLY the non-exempt scanned roots for which the per-root probe reports
    no earlier floor. Same inputs, one less layer, and an aggregator that drops,
    duplicates, re-orders or filters differently cannot satisfy it.
    """
    roots = {gate.conf_d_root(rel) for rel, _keys in read_pairs} - {None}
    return sorted(
        root for root in roots
        if root not in gate._DEFAULTS_ROOTS_MAY_BE_EMPTY
        and not gate._floors_that_notice_a_zeroed_root(
            generators, read_pairs, root))


def test_every_scanned_root_is_wired_to_the_fifth_floor_through_the_real_entry(
        monkeypatch):
    """⛔ Per ROOT, and through `_defaults_faces()` — this pins the WIRING.

    Every other test of this floor calls `_assert_every_root_contributes`
    directly, which answers "does the function still work" and NOT "is it still
    reached, for this root, by the code path the gate actually runs". Measured:
    with the fifth floor's call site commented out, one test in this file went
    red — and it is a test about the ARTIFACT KEY FLOOR that happens to assert
    the fifth floor fires first, on the vanished-root arm. So the zero-keys arm
    — the gap #1411 was opened for — had no real-entry coverage at all, and the
    incidental guard covers exactly the two roots its own probe happens to name.

    So: for EVERY non-exempt root the scan finds, stand the other four floors
    down, make that one root read empty, and require the fifth to speak up and
    name it. Roots come from the scan, not from the pin.

    ⚠️ SCOPE of that measurement, because it reads like a search and is not one.
    "Comment out the call site, see what goes red" finds tests that need the
    fifth floor; it is structurally BLIND to a test that has come to LEAN on it.
    When the fifth floor is stood down, the older floors it shadows wake up and
    catch the same input — so a case whose probe now trips the fifth floor
    instead of the floor it is named for stays green through the whole
    experiment. There was one (`_trip_artifact_key_floor`, whose two e2e-bench
    roots the fifth floor claims first); it was found by disabling the OTHER
    floor, not this one. Shadowing has to be probed from both sides.
    """
    # ⛔ Not `>= 8`. That was a bare lower bound with three roots of slack, in a
    # file whose stated discipline for every other floor is a bracket, and it
    # would have gone on passing while a third of the loop quietly evaporated.
    # Exact equality needs no hand-maintained number at all and cannot go
    # vacuous: the left side is DERIVED from the defaults scan, the right side is
    # the hand-written pin minus the hand-written exemption, so this is a real
    # comparison of two independent things, over exactly the universe this loop
    # walks — which is the universe whose emptying would make the loop vacuous.
    #
    # ⛔ This used to say it was a restatement of
    # test_the_pin_matches_what_the_real_repo_actually_has
    # — with that name wrapped across two comment lines, so no grep for the
    # test could find this reference (#1383's class, in the very file whose
    # subject is guards that cannot see what they claim to). That test is gone
    # (#1434) and the sentence went with it — but the pairing was
    # wrong anyway, which is worth keeping because it reads so plausibly:
    # that one compared POST-exemption roots against the whole pin, this one
    # compares PRE-exemption roots against the pin minus the exemption, so the
    # two are asking different questions of different universes.
    # ⛔ AN EARLIER WORDING HERE CLAIMED MUTATION HAD SEPARATED THEM IN BOTH
    # DIRECTIONS, and blind review showed the sentence was false in both halves
    # — it had compressed two-part mutant configurations into one clause. The
    # separating case needs the pin edited TOO (exempt a single-file root's only
    # artifact AND re-file the root as artifact-less): only then is the gate
    # green with this test red. Plain exemption reddens the gate first, which
    # reddens the deleted test as well. And there is no reverse case at all:
    # "gate green while the deleted test is red" is exactly what that test
    # being true-by-construction rules out. Stated as the algebra rather than
    # as a mutation anecdote, because the algebra is what survives.
    # ⚠️ What this equality cannot see on its own: adding a root to
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY` subtracts it from BOTH sides at once.
    # An earlier wording said the gate's older exit-lock kept that door shut;
    # blind review disproved it and re-measurement confirmed. That lock is
    # `if not n_keys: continue` — it rejects an entry whose root STILL CARRIES
    # KEYS, and the moment a maintainer reaches for the list is the moment the
    # root has just gone to zero and the floor has said so. Complementary
    # conditions: the lock could not see the case it was cited for. Measured
    # then: rename a `defaults:` section away, add one line, and EIGHT of the
    # twelve roots went back to rc=0 — one of which is the entry that is
    # legitimately on the list, so seven were newly silenceable.
    # ⛔ STILL OPEN, and it stayed open on purpose (#1434). Three arms were
    # written for it inside that ticket and all three were withdrawn: the full
    # account, including which mutations killed which version and why no
    # predicate over today's content can answer this question, is in the
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY` block of
    # `scripts/tools/lint/check_threshold_reachability.py`. Read that before
    # writing a fourth. ⛔ In particular, do not "fix" it by checking whether a
    # `defaults:` section exists: that reads identically for a renamed section
    # and a legitimately absent one, which is the trap all three fell into.
    roots = sorted(_scanned_confd_roots() - gate._DEFAULTS_ROOTS_MAY_BE_EMPTY)
    # ⛔ "Cannot go vacuous" was stated and was false. Both sides of the equality
    # below are `something - _DEFAULTS_ROOTS_MAY_BE_EMPTY`, so an exemption list
    # that swallowed the whole pin made BOTH sides `[]`, the equality hold, the
    # loop run zero times and this test pass — the one test that pins the fifth
    # floor's WIRING, satisfied by exempting everything. One token closes it,
    # and it is a token rather than a number on purpose: any specific count here
    # would fire the day a fixture is legitimately retired.
    assert roots, (
        "no non-exempt root left to walk — _DEFAULTS_ROOTS_MAY_BE_EMPTY has "
        "grown to cover the scan, and this test would pass vacuously")
    assert roots == sorted(
        set(gate._DEFAULTS_CONFD_ROOTS) - gate._DEFAULTS_ROOTS_MAY_BE_EMPTY), {
            "scanned but not pinned": sorted(
                set(roots) - set(gate._DEFAULTS_CONFD_ROOTS)),
            "pinned but not scanned": sorted(
                set(gate._DEFAULTS_CONFD_ROOTS)
                - gate._DEFAULTS_ROOTS_MAY_BE_EMPTY - set(roots))}

    real_read = gate._read_artifact_keys
    for victim in roots:
        # Floors 1-2 count FILES (unmoved here, but the stand-down has to be
        # total or a pass could be somebody else's catch), 3 and 4 are silenced
        # outright. What is left standing is only the fifth.
        monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_FLOOR", 0)
        monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_READ_FLOOR", 0)
        monkeypatch.setattr(gate, "_assert_keys_floor", lambda *_a, **_k: None)
        monkeypatch.setattr(gate, "_assert_shipped_roots_intact",
                            lambda *_a, **_k: None)
        # ⛔ The SIXTH floor stands down here too, and it has to: it runs
        # before the fifth and, for a shipped root, catches the same zeroed-keys
        # input first (measured — this test failed on
        # `components/threshold-exporter/config/conf.d` with the sixth floor's
        # message rather than the fifth's). That is precisely the shadowing this
        # docstring says has to be probed from both sides, and standing it down
        # is what keeps the loop a statement about the FIFTH floor. The sixth
        # floor's own wiring is pinned separately below. (#1458)
        monkeypatch.setattr(gate, "_assert_loader_readable_floor",
                            lambda *_a, **_k: None)

        def _empty_under_victim(path, _victim=victim, _real=real_read):
            rel = path.relative_to(gate.PROJECT_ROOT).as_posix()
            return {} if gate.conf_d_root(rel) == _victim else _real(path)

        monkeypatch.setattr(gate, "_read_artifact_keys", _empty_under_victim)

        with pytest.raises(gate._GateViolation) as exc:
            gate._defaults_faces()
        msg = str(exc.value)
        assert victim in msg, (victim, msg)
        assert "ZERO threshold keys" in msg, (victim, msg)


def test_the_floor_probe_asks_the_real_floors_rather_than_modelling_them(
        monkeypatch):
    """⛔ The probe must follow the real floors' own numbers, not a copy.

    `_floors_that_notice_a_zeroed_root` is what `main()` prints from and what
    the reasoning above rests on, so "it calls the real assertions" cannot just
    be a comment. Both moves below leave the assertion functions untouched and
    change only what those functions read — the artifact key FLOOR, and the
    shipped-root TABLE — so a probe carrying its own `total - keys < FLOOR`, or
    its own idea of which roots are shipped, would keep answering the old way.

    ⛔ Each move is chosen to fire only on the ZEROED input, never on the
    unmodified one, because the probe is a before/after difference: a floor
    that was already breached is not credited with catching anything.
    """
    generators, artifacts = gate._defaults_faces()
    read_pairs = [(gate._artifact_rel(face), keys)
                  for face, keys in artifacts.items()]
    assert all(rel for rel, _k in read_pairs), read_pairs

    quiet = gate._roots_only_the_fifth_floor_notices(generators, read_pairs)
    assert quiet, "no blind-spot root to probe with"
    # ⛔ The victim has to be a root the ARTIFACT KEY FLOOR COUNTS (#1544), and
    # `quiet[0]` is not: the list is sorted and the two `tests/e2e-bench/` roots
    # sort first, and those are exactly the ones outside this floor's
    # denominator. Move 1) below rebases that floor to "just above victim gone"
    # and then expects the zeroed input to trip it — on an excluded victim the
    # total does not move, so the move measures nothing and the whole test
    # would be reporting on a floor it never made speak.
    countable = [r for r in quiet
                 if r not in gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS]
    assert countable, (
        "every blind-spot root is outside the key floor's denominator, so move "
        "1) below cannot be set up at all — pick the probe root differently, or "
        "shorten `_ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS`", quiet)
    victim = countable[0]
    lost = sum(len(k) for rel, k in read_pairs if gate.conf_d_root(rel) == victim)
    # ⛔ …and the baseline `total` is the floor's own denominator, for the same
    # reason: `total - lost + 1` has to be a floor the UNMODIFIED tree clears
    # ("baseline dirty" below), and the scan-wide 72 sets it 13 too high.
    total = sum(len(k) for _rel, k in read_pairs
                if gate.conf_d_root(_rel)
                not in gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS)
    assert lost, victim

    # 1) raise the REAL artifact key floor to just above "victim gone" — still
    #    below today's total, so the unmodified input stays clean.
    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_KEYS_FLOOR", total - lost + 1)
    assert not gate._floors_firing_on(generators, read_pairs), "baseline dirty"
    fired = gate._floors_that_notice_a_zeroed_root(generators, read_pairs, victim)
    assert gate._FLOOR_KEYS in fired, fired
    monkeypatch.undo()

    # 2) declare the victim a SHIPPED root, so the other floor now speaks for it.
    monkeypatch.setattr(gate, "_SHIPPED_CONFD_ROOTS",
                        dict(gate._SHIPPED_CONFD_ROOTS,
                             **{victim: (1, 1, "probe root.")}))
    assert not gate._floors_firing_on(generators, read_pairs), "baseline dirty"
    fired = gate._floors_that_notice_a_zeroed_root(generators, read_pairs, victim)
    assert gate._FLOOR_SHIPPED in fired, fired
    monkeypatch.undo()

    # …and with both back as they are, the same root is silent again — so the
    # two assertions above are about the floors, not about the monkeypatch.
    assert not gate._floors_that_notice_a_zeroed_root(
        generators, read_pairs, victim)

    # ── 3-5) the probe must CALL these two symbols, not reproduce them ───────
    # ⛔ The moves above change what the real floors READ, so a probe carrying
    # its own copy of their arithmetic — reading the same module globals — moves
    # with them and stays green.
    #
    # ⚠️ THE MUTANT, WRITTEN OUT IN FULL, because the earlier wording described
    # one that cannot be reproduced from its own description. It said "rewriting
    # `_floors_firing_on` as a local `n_art < _DEFAULTS_ARTIFACT_KEYS_FLOOR`
    # left the suite at 105 passed" — but `_assert_keys_floor` raises from TWO
    # branches, and a model carrying only the artifact one is already caught by
    # `test_a_floor_already_breached_is_not_credited_with_catching_a_root`,
    # whose probe (`{"a producer that stopped yielding": {}}`) is a GENERATOR
    # starvation. Re-measured on this tree, both variants, to say which is
    # which:
    #   * artifact branch only — 2 failed / 112 passed, killed by the
    #     already-breached test AND by this one. It was never the silent mutant;
    #     the sibling test was covering the half it left out.
    #   * BOTH branches modelled (shipped-root table walked locally, plus
    #     generator and artifact key totals against the same two constants) —
    #     1 failed / 113 passed, and the one failure is THIS test. That is the
    #     mutant this test exists for, and nothing else in the file sees it.
    # ⛔ Keep the first bullet. "Half a model gets killed by a test aimed at the
    # other half" is the reason the mutation had to be written out rather than
    # summarised — a mutant that is weaker than its description overstates the
    # coverage that killed it.
    #
    # Worse than a silent mutant, it SUPPRESSES a red: with the real
    # `_assert_keys_floor` disabled outright, the honest probe turns 6 tests red
    # and moves the printed figure from 7 to 9, while the modelling one turns
    # only 4 red and goes on printing 7 — the gate at its most broken, reporting
    # its healthy number. So these moves replace the FUNCTIONS, which no copy of
    # their arithmetic can follow.
    def _always(*_a, **_k):
        raise gate._GateViolation("probe: this floor was called")

    def _never(*_a, **_k):
        return None

    monkeypatch.setattr(gate, "_assert_keys_floor", _always)
    assert gate._FLOOR_KEYS in gate._floors_firing_on(generators, read_pairs)
    monkeypatch.undo()

    monkeypatch.setattr(gate, "_assert_shipped_roots_intact", _always)
    assert gate._FLOOR_SHIPPED in gate._floors_firing_on(generators, read_pairs)
    monkeypatch.undo()

    # …and quiet fakes must make the probe quiet, so the two assertions above
    # cannot be satisfied by a probe that simply always reports both floors.
    monkeypatch.setattr(gate, "_assert_keys_floor", _never)
    monkeypatch.setattr(gate, "_assert_shipped_roots_intact", _never)
    assert gate._floors_firing_on(generators, read_pairs) == []
    monkeypatch.undo()

    # ⛔ and the same through the BEFORE/AFTER path, which is what `main()`'s
    # figure is actually built from: a fake that fires only once the victim's
    # keys are gone must be credited to the victim.
    # ⛔ This fake measures what it is HANDED, so its baseline is the handed
    # total (every scanned face), not `total` above — that one is the real
    # floor's narrower denominator and using it here would make the fake silent
    # on the zeroed input too, i.e. would test nothing while reading as a pass.
    handed_total = sum(len(k) for _rel, k in read_pairs)
    assert handed_total > total, (handed_total, total)

    def _below_today(_gens, artifacts):
        if sum(len(v) for v in artifacts.values()) < handed_total:
            raise gate._GateViolation("probe: keys dropped")

    monkeypatch.setattr(gate, "_assert_keys_floor", _below_today)
    assert not gate._floors_firing_on(generators, read_pairs), "baseline dirty"
    assert gate._FLOOR_KEYS in gate._floors_that_notice_a_zeroed_root(
        generators, read_pairs, victim)
    monkeypatch.undo()


def test_the_probe_and_the_gate_bucket_through_the_same_implementation(monkeypatch):
    """⛔ `_bucket_by_root`'s docstring claims "ONE implementation, called by both".

    Nothing asserted it. Measured (mutation): giving `_floors_firing_on` its own
    loop over `_SHIPPED_CONFD_ROOTS` — with a `startswith(root)` prefix test
    instead of the real `rel == root or rel.startswith(root + "/")` — left the
    suite at 105 passed and the gate at rc=0. A second bucketing keeps answering
    confidently after the real one changes shape, which is precisely how the
    derived figure would come to describe a partition the gate no longer uses.

    Both call sites are pinned by ONE sentinel, so neither can be re-pointed at
    a private copy without this going red.

    ⚠️ DISCLOSED GAP, and disclosed rather than closed on purpose — this round's
    stopping rule is that a guard earns a guard only when its silent failure
    lets the original defect back. A sentinel that raises pins THAT THE CALL
    HAPPENS; it cannot pin that the RESULT IS USED. A call site keeping a
    vestigial `_bucket_by_root(pairs)` and then bucketing again privately would
    still hit the sentinel and pass here.

    Left open because reaching it takes deliberate construction — a discarded
    call plus a second implementation — and because the payload is covered from
    the other side:
    `test_the_floor_probe_asks_the_real_floors_rather_than_modelling_them`
    replaces the two floor FUNCTIONS, so a probe that bucketed
    privately and fed the result to the real floors still has to agree with
    them, and one that bucketed privately and modelled the floors too is the
    mutant that test is built to kill. What is genuinely unguarded is the
    narrow middle: a private bucketing whose answer happens to match the shared
    one today. That is a latent divergence, not the #1411 defect returning.
    """
    boom = RuntimeError("bucketed through the shared implementation")

    def _sentinel(_pairs):
        raise boom

    monkeypatch.setattr(gate, "_bucket_by_root", _sentinel)

    with pytest.raises(RuntimeError) as probe_exc:
        gate._floors_firing_on({}, [])
    assert probe_exc.value is boom, probe_exc.value

    with pytest.raises(RuntimeError) as gate_exc:
        gate._defaults_faces()
    assert gate_exc.value is boom, gate_exc.value


def test_a_floor_already_breached_is_not_credited_with_catching_a_root():
    """⛔ Before/after, not just after (#1411).

    If the probe only looked at the zeroed input, a floor that was ALREADY
    breached would be recorded as catching every root in turn — and the derived
    figure would report zero blind spots at precisely the moment the gate was
    most broken. Measured here by breaking a floor outright and checking the
    probe still calls the root silent.
    """
    generators, artifacts = gate._defaults_faces()
    read_pairs = [(gate._artifact_rel(face), keys)
                  for face, keys in artifacts.items()]
    victim = gate._roots_only_the_fifth_floor_notices(generators, read_pairs)[0]

    # a floor breached on the UNMODIFIED input, for a reason that has nothing
    # to do with the victim root: the generator half of the key floor.
    starved = {"a producer that stopped yielding": {}}
    assert gate._FLOOR_KEYS in gate._floors_firing_on(starved, read_pairs)

    assert not gate._floors_that_notice_a_zeroed_root(
        starved, read_pairs, victim)


def test_the_may_be_empty_list_must_earn_its_entries_on_every_run(monkeypatch):
    """⛔ The exemption is the one line that turns this floor off, so the GATE
    checks it — not only a test (same place as the `unpinned` arm).

    Measured before this check existed: adding a root carrying 2 real keys to
    `_DEFAULTS_ROOTS_MAY_BE_EMPTY` left the gate at rc=0 AND the whole suite at
    96 passed. The two tests that did catch the same edit on a different root
    only did so because that root is their hardcoded probe — coverage by
    coincidence, which disappears the moment somebody picks another root.
    """
    victim = sorted(_scanned_confd_roots() - gate._DEFAULTS_ROOTS_MAY_BE_EMPTY)[0]
    # ⛔ The already-exempt roots are built EMPTY, the way the real repo has
    # them. Handing them a key would trip this very check on the fixture and
    # make the test pass for the wrong root.
    by_root = {r: ([(f"{r}/_defaults.yaml", {})]
                   if r in gate._DEFAULTS_ROOTS_MAY_BE_EMPTY
                   else _root_face(r, "x"))
               for r in gate._DEFAULTS_CONFD_ROOTS}
    tracked = gate._tracked_confd_roots()

    # sanity: this input is otherwise clean
    gate._assert_every_root_contributes(by_root, tracked)

    monkeypatch.setattr(gate, "_DEFAULTS_ROOTS_MAY_BE_EMPTY",
                        gate._DEFAULTS_ROOTS_MAY_BE_EMPTY | {victim})
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root, tracked)
    msg = str(exc.value)
    assert victim in msg, msg
    assert "_DEFAULTS_ROOTS_MAY_BE_EMPTY" in msg, msg
    # the remedy must not be "delete the keys": the two legitimate readings are
    # a wrong exemption, or a tree that started declaring thresholds.
    assert "Remove the line" in msg, msg


# ── The schema witness (#1443) ───────────────────────────────────────────────
#
# The fifth floor can be switched off for a tree by one line in
# `_DEFAULTS_ROOTS_MAY_BE_EMPTY`, and no predicate over today's content can tell
# a legitimate entry from one added to hide a rename — the reasoning is at the
# end of `_assert_every_root_contributes`, and three predicates were withdrawn
# there. What is left is a witness whose answer does not depend on the exemption
# at all: the schema that says which top-level keys a `_defaults*` file may have.

_WITNESS_OK = "defaults:\n  cpu: 90\n"
_WITNESS_RENAMED = "defalts:\n  cpu: 90\n"
_WITNESS_NESTED = "defaults:\n  threshold:\n    cpu: 90\n"
# ⛔ A REAL recipe shape, copied from the finance domain file under
# `rule-packs/recipes/examples/conf.d/`, not a plausible-looking one. (The path
# is named in two pieces on purpose — writing it whole wrapped it mid-token and
# `tests/ops/test_wrapped_path_references.py` caught it, which is the guard
# doing exactly its job.) The first draft here
# wrote `- name: x / expr: up == 0` and the witness rejected it for a missing
# `recipe` key — correctly, and that is the point: this witness validates the
# WHOLE platform-defaults schema, not only the set of top-level key names. A
# fixture that is merely "shaped like" the tree would have made the two silent
# rows below prove nothing.
_WITNESS_NO_SECTION = (
    "_custom_alerts:\n"
    "  - recipe: ratio\n"
    "    name: payment_failure_ratio\n"
    "    metric: payment_failed_total\n"
    "    denominator_metric: payment_attempts_total\n"
    "    op: \">\"\n"
    "    window: 5m\n"
    "    threshold: \"0.01:critical\"\n")
_WITNESS_PLACEHOLDER = "# nothing declared here yet\n"


def _witness(docs: dict[str, str]) -> None:
    """Drive the REAL witness over synthetic artifact text (reader seam only)."""
    gate._assert_defaults_artifacts_match_schema(
        sorted(docs), read=lambda rel: docs[rel])


def test_the_schema_witness_is_silent_on_the_real_tree():
    """Anti-vacuity first: it must be handed the real scan, and pass on it.

    ⛔ Assert the JUDGED count, not just "did not raise". Silence is what a
    stubbed reader also produces; the count is what tells the two apart. It is
    lower than the artifact count on purpose — a comment-only placeholder
    yields no YAML document at all.
    """
    import yaml

    tracked = gate._tracked_defaults_artifacts()
    assert len(tracked) >= gate._DEFAULTS_ARTIFACT_FLOOR, tracked
    judged = gate._assert_defaults_artifacts_match_schema(tracked)  # must NOT raise

    # ⛔ EQUALITY AGAINST AN INDEPENDENT RECOUNT, not `judged >= floor`. Blind
    # review replaced the returned count with the floor constant itself and the
    # whole suite stayed green: the inequality was satisfied by the very number
    # it was compared against, and the other control stubs the witness out
    # entirely so it never sees a real return value. This side re-derives the
    # document count from the tree without calling the witness at all.
    expected = sum(
        len(list(yaml.safe_load_all(
            (gate.PROJECT_ROOT / rel).read_text(encoding="utf-8"))))
        for rel in tracked)
    assert judged == expected, (judged, expected)
    # ⛔ NOTHING ELSE. `assert judged < len(tracked)` stood here, on the
    # reasoning that one artifact is comment-only today — and blind review
    # reddened it by adding a legal section to that artifact, which is a
    # SHIPPED customer example. It was the same rent this file had just
    # finished condemning in `assert len(silent) >= 10`: a floor whose only
    # basis is a coincidence of today's tree, whose cheapest green is deleting
    # it. The equality above needs no companion — a reader that stops reading
    # cannot match an independent recount.


def test_the_schema_witness_refuses_an_empty_input():
    """A check that validates nothing passes perfectly."""
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_defaults_artifacts_match_schema([])
    assert "no defaults artifacts" in str(exc.value), exc.value

    # …and a population that parses to nothing at all is the other half of it
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"a/conf.d/_defaults.yaml": "", "b/conf.d/_defaults.yaml": ""})
    assert "judged ZERO documents" in str(exc.value), exc.value


def test_the_schema_witness_is_not_reachable_by_either_exemption_table(
        monkeypatch):
    """⛔ THE #1443 TEST. Exempt EVERYTHING; the witness must still speak.

    Measured on `90391b5f` through the real `_defaults_faces()`, renaming a
    tree's `defaults:` section and adding that root to
    `_DEFAULTS_ROOTS_MAY_BE_EMPTY`: 7 of the 12 pinned roots returned rc=0. The
    exemption is what made them green, so the check that answers afterwards must
    not read it — this drives that property directly rather than trusting the
    call order, which was necessary and not sufficient one round ago.
    """
    monkeypatch.setattr(gate, "_DEFAULTS_ROOTS_MAY_BE_EMPTY",
                        frozenset(gate._DEFAULTS_CONFD_ROOTS))
    monkeypatch.setattr(
        gate, "_DEFAULTS_ARTIFACT_EXEMPT",
        {rel: "exempted by this test" for rel in gate._tracked_defaults_artifacts()})

    victim = "tests/golden/fixtures/l0-only/conf.d/_defaults.yaml"
    with pytest.raises(gate._GateViolation) as exc:
        _witness({victim: _WITNESS_RENAMED})
    msg = str(exc.value)
    assert victim in msg, msg
    assert "defalts" in msg, msg
    # ⛔ and the message must not offer either escape as a way out
    assert "AN EXEMPTION IS NOT AVAILABLE" in msg, msg
    assert "Widening it to make this message go away is not local" in msg, msg
    # ⛔ nor may it leave the cheapest wrong fix unnamed: renaming the block to
    # another name the schema accepts silences this check and keeps the
    # declarations unread, which is the defect it exists to report.
    assert "DO NOT move the block under a different top-level section name" in msg, msg


def test_the_witness_catches_a_renamed_section_and_the_pinned_members_stay_caught():
    """The attack reddens, and the two names that must stay CAUGHT are PINNED.

    ⛔ A PIN, NOT A CLASSIFIER, and the distinction is why the enumeration here
    is correct where the previous one was not. The version this replaces built
    `silent`/`caught` by probing every property and then asserted a PARTITION —
    which is true by construction (each name enters exactly one side), so blind
    review measured both directions escaping it: widening the schema so
    `optional_overrides` stops being caught left the suite green, and making the
    witness invent a violation on a legal `_metadata:` section also left it
    green. A classifier over an open input space cannot be enumerated; a pin
    over THIS repo's two shaped properties is closed, single, and any change to
    it should force a review.

    ⚠️ The SILENT side is deliberately NOT asserted here — see the disclosure
    block below for what that costs and why it is still the right trade.
    """
    # the attack this gate exists for: a name the schema does not allow
    for bad in ("defalts", "defaultss", "Defaults"):
        with pytest.raises(gate._GateViolation) as exc:
            _witness({"x/conf.d/_defaults.yaml": f"{bad}:\n  cpu: 90\n"})
        assert bad in str(exc.value), exc.value

    # …and the two properties whose value shape makes them catchable must stay
    # catchable. If a schema change makes one of these silent it widens the
    # #1443 bypass, and that red is correct rather than a maintenance cost.
    for name in ("_custom_alerts", "optional_overrides"):
        with pytest.raises(gate._GateViolation):
            _witness({"x/conf.d/_defaults.yaml": f"{name}:\n  cpu: 90\n"})

    # the shapes the real tree actually has must stay silent — a witness that
    # reddens on today's tree gets deleted, not fixed
    _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
              "b/conf.d/_defaults.yaml": _WITNESS_PLACEHOLDER,
              "c/conf.d/_defaults.yaml": _WITNESS_NO_SECTION,
              "d/conf.d/_defaults.yaml": _WITNESS_NESTED})


def test_the_witness_reads_every_artifact_and_every_document_in_it():
    """A partial reader hides the attack in whatever it stopped reading.

    Blind review truncated the loop to `rels[:1]` and swapped `safe_load_all`
    for `safe_load`; both left the whole suite green, because no test had ever
    handed the witness an offender anywhere but first, and no tracked artifact
    is a multi-document file (measured: 0 of 17).
    """
    # offender is NOT first — `sorted()` puts the clean one ahead of it
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
                  "b/conf.d/_defaults.yaml": _WITNESS_RENAMED})
    assert "b/conf.d/_defaults.yaml" in str(exc.value), exc.value

    # offender is in the SECOND YAML document of a single file
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"a/conf.d/_defaults.yaml":
                  _WITNESS_OK + "---\ndefalts:\n  cpu: 1\n"})
    assert "defalts" in str(exc.value), exc.value

    # ⛔ The two counts are read back as NUMBERS, not matched as a sentence.
    # The first spelling asserted the literal "2 violation(s) across 2 defaults
    # artifact(s)", so re-wording the message reddened a test whose subject is
    # arithmetic. The invariant is that the two populations differ, which needs
    # a fixture where they do: three files, one bad.
    def _counts(docs):
        with pytest.raises(gate._GateViolation) as exc:
            _witness(docs)
        found = re.findall(r"(\d+) violation\(s\) across (\d+)", str(exc.value))
        assert found, exc.value
        return tuple(int(n) for n in found[0])

    assert _counts({"a/conf.d/_defaults.yaml": _WITNESS_RENAMED,
                    "b/conf.d/_defaults.yaml": _WITNESS_RENAMED}) == (2, 2)
    assert _counts({"a/conf.d/_defaults.yaml": _WITNESS_OK,
                    "b/conf.d/_defaults.yaml": _WITNESS_OK,
                    "c/conf.d/_defaults.yaml": _WITNESS_RENAMED}) == (1, 1)


def test_the_witness_validates_artifacts_outside_conf_d_without_refusing_them():
    """A `_defaults*` file outside every conf.d tree is checked, not rejected.

    ⛔ Two wrong shapes preceded this one, both measured by blind review. A
    silent filter dropped such a file with no report, which
    `guard_defaults_scopes.py` forbids for unmanaged paths — ⚠️ though the
    authority cited for that was overstated: that module's own `resolve()`
    collects unmanaged paths and CARRIES ON, it does not stop the run. A hard
    refusal then made a perfectly legal Go-side `testdata/_defaults.yaml` fail
    the whole gate with no in-repo way to go green.

    What is left is the narrow true statement: the schema is a conf.d contract,
    so a violation found OUTSIDE a conf.d tree may be the contract not applying
    rather than the file being wrong — and the message says so instead of
    diagnosing a renamed section.
    """
    # legal content outside conf.d: checked, and silent
    _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
              "components/x/testdata/_defaults.yaml": _WITNESS_OK})

    # a real violation outside conf.d: reported, but NOT as a renamed section
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"tools/portal/_defaults.yaml": "theme: dark\n"})
    msg = str(exc.value)
    assert "tools/portal/_defaults.yaml" in msg, msg
    assert "OUTSIDE every `conf.d` tree" in msg, msg
    assert "mis-spelled" not in msg, msg


_RENAME_HEADLINE = "MOST LIKELY: a `defaults:` section was mis-spelled"


def test_the_rename_headline_is_bound_to_the_artifact_it_diagnoses(monkeypatch):
    """The headline must come from a conf.d artifact's OWN violation.

    ⛔ THE TWO-FILE CASE IS THE WHOLE POINT. The condition used to ask its two
    halves of DIFFERENT populations — "is there any conf.d offender" over
    `offenders`, and "does anything look like a rejected key" over ALL
    `problems`. Every single-file case passes under both spellings, so a round
    of single-file tests could not tell them apart; it took one unrelated
    out-of-conf.d file to re-arm the headline for everybody.

    Measured before the fix, and the reason this test exists: `{x/conf.d:
    "state_filters: 5", tools/portal: "theme: dark"}` told the owner of
    `x/conf.d` to "restore the section name" of a `defaults:` block that file
    never lost — the exact false diagnosis this headline was made conditional
    to stop.
    """
    # ⛔ ASK THE PREDICATE FIRST, and derive its input rather than quoting one.
    # The headline is gated on `_REJECTED_TOP_LEVEL_KEY`, which matches
    # jsonschema's WORDING. A library upgrade that rewords `additionalProperties`
    # /`patternProperties` failures makes the GATE degrade to the ⚠️ branch — on
    # purpose, and safely. Without this check the MUST-FIRE half below would be
    # the thing that reddens, naming neither jsonschema nor the predicate, and
    # its cheapest green is deleting the half that has teeth.
    import json as _json

    import jsonschema as _js
    # ⛔ The literal path is here on purpose, and it is also an assertion. This
    # test's subject is the schema, but it reaches it through
    # `gate._PLATFORM_DEFAULTS_SCHEMA`, and `verify_diff.py`'s text map is built
    # from literals — so without this line a schema-only edit does not select
    # this test locally. (CI's Python leg is a catch-all and runs it regardless;
    # what is bought is the fast feedback, on the one test whose whole job is to
    # notice schema and library drift.)
    assert gate._PLATFORM_DEFAULTS_SCHEMA.as_posix().endswith(
        "docs/schemas/platform-defaults.schema.json"), gate._PLATFORM_DEFAULTS_SCHEMA
    # ⛔ `syspath_prepend`, not a bare `sys.path.insert`: this is process
    # GLOBAL state, and an unguarded insert leaves it behind for every later
    # test in the session (the sibling probe higher up in this file at least
    # guards with `not in sys.path`; this one did not). `scripts/tools/lint`
    # holds a dozen `check_*` modules, so a stale entry is a shadowing hazard,
    # not just untidiness.
    monkeypatch.syspath_prepend(
        str(gate.PROJECT_ROOT / "scripts" / "tools" / "lint"))
    import check_confd_schema as _sibling
    _real = _sibling.defaults_doc_violations(
        "probe/conf.d/_defaults.yaml", {"defalts": {"cpu": 90}},
        _json.loads(gate._PLATFORM_DEFAULTS_SCHEMA.read_text(encoding="utf-8")),
        _js)
    assert _real and any(gate._REJECTED_TOP_LEVEL_KEY(p) for p in _real), (
        "jsonschema no longer words a rejected top-level key the way "
        "`_REJECTED_TOP_LEVEL_KEY` matches it. The gate degrades safely — it "
        "stops printing the MOST LIKELY headline — so the fix is to update the "
        "predicate, NOT to relax anything below.", _real)
    # ⛔ AND THE OTHER SPELLING, which today's schema can never produce: its one
    # `additionalProperties: false` sits at the root and carries
    # `patternProperties`, so jsonschema always says "regexes". Blind review
    # measured the second disjunct silently deletable because of that. Probe it
    # against a stripped copy so it is a branch that has been RUN, not one
    # assumed to work the day a schema edit drops those prefixes.
    _stripped = {k: v for k, v in _json.loads(
        gate._PLATFORM_DEFAULTS_SCHEMA.read_text(encoding="utf-8")).items()
        if k != "patternProperties"}
    _alt = _sibling.defaults_doc_violations(
        "probe/conf.d/_defaults.yaml", {"defalts": {"cpu": 90}}, _stripped, _js)
    assert _alt and any(gate._REJECTED_TOP_LEVEL_KEY(p) for p in _alt), (
        "the `Additional properties are not allowed` spelling is no longer "
        "recognised. That is the wording jsonschema uses when the schema has no "
        "`patternProperties`; keep the predicate able to read both.", _alt)

    # MUST FIRE: the conf.d artifact's own violation IS a rejected top-level key
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"x/conf.d/_defaults.yaml": "defalts:\n  cpu: 90\n"})
    assert _RENAME_HEADLINE in str(exc.value), exc.value

    # MUST NOT FIRE: this conf.d artifact's own fault is a VALUE SHAPE; the
    # rejected key belongs to a different file, outside every conf.d tree
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"x/conf.d/_defaults.yaml": "state_filters: 5\n",
                  "tools/portal/_defaults.yaml": "theme: dark\n"})
    msg = str(exc.value)
    assert _RENAME_HEADLINE not in msg, msg
    # …and neither real finding may be lost in the process
    assert "state_filters" in msg, msg
    assert "tools/portal/_defaults.yaml" in msg, msg


def test_the_undiagnosed_branch_claims_nothing_about_keys_it_never_read():
    """A file that fails to PARSE has no top-level keys to vouch for.

    The wording this replaces said "every top-level key here is one this schema
    accepts" — told to a maintainer whose YAML was broken, about a file where
    not one top-level key had been looked at. Both disjuncts of that sentence
    were false for this input: the file is inside a conf.d tree, and nothing
    was parsed.
    """
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"bad/conf.d/_defaults.yaml": "defaults:\n  cpu: 90\n bad: [\n"})
    msg = str(exc.value)
    assert "cannot read/parse YAML" in msg, msg
    # ⛔ …and the file is still COUNTED. Blind review deleted `offenders.add`
    # from the parse arm and nothing noticed: the header then reads "across 0
    # defaults artifact(s)", and an unparseable file outside conf.d loses its
    # scope note, because `outside` is derived from that same set.
    assert "across 1 defaults artifact" in msg, msg
    assert _RENAME_HEADLINE not in msg, msg
    # ⛔ NON-VACUITY: prove the ⚠️ branch is the one that was taken. Without it
    # the two assertions above are satisfied by messages that never reach it.
    assert "NOT diagnosed as a renamed" in msg, msg
    # ⛔ The defect, pinned as the ABSENCE of the sentence that shipped. The
    # property — "vouches for no key it never read" — has no positive form a
    # test can check, and the first spelling here asserted the PRESENCE of
    # today's wording instead: a pure re-wording of the branch reddened it, and
    # the cheapest green was deleting this line. ⚠️ A differently worded
    # universal claim would still pass. Disclosed, not covered.
    assert "every top-level key here is one this schema accepts" not in msg, msg


def test_the_call_site_hands_the_witness_the_tracked_scan_not_the_exempt_set(
        monkeypatch):
    """⛔ #1443's load-bearing property, and it is only visible at the CALL SITE.

    An entry in `_DEFAULTS_ARTIFACT_EXEMPT` must not ALSO buy schema
    invisibility — that would turn the one-line disarm this gate exists to
    close into a two-line one. The exemption test further up drives the witness
    DIRECTLY, so it cannot observe what `_defaults_faces()` chooses to pass.

    Measured on the real tree: with one exempted, schema-INVALID artifact in a
    non-shipped root, today's call site exits 1 and names the offender, while
    an exempt-filtered one exits 0 with `✅ OK`. Nothing else in this module
    distinguishes the two — swapping the call site kills this test and no other.

    ⛔ ANTI-VACUITY: `_DEFAULTS_ARTIFACT_EXEMPT` is EMPTY today, so "what was
    passed == the tracked scan" would hold whichever population the call site
    used. The table is populated here so the two genuinely differ, and that
    difference is asserted before the run rather than assumed.

    ⚠️ THE PREMISE IS NARROW, AND THAT IS STRUCTURAL. The witness runs LAST, so
    the exemption must not make any floor raise first — which means the victim
    has to come from a root that still holds another artifact. Registering the
    root as may-be-empty instead does NOT help: `_assert_every_root_contributes`
    reads the `_DEFAULTS_CONFD_ROOTS` pin, and its own message says exempting a
    root's last artifact lands there with no legal way out. Measured across all
    13 roots: exactly ONE qualifies today — a non-shipped root holding four.
    Shipped roots are excluded because their `min_artifacts` floor fires first.
    """
    tracked = gate._tracked_defaults_artifacts()
    shipped = set(gate._SHIPPED_CONFD_ROOTS)
    by_root: dict[str | None, list[str]] = {}
    for rel in tracked:
        by_root.setdefault(gate.conf_d_root(rel), []).append(rel)

    candidates = sorted(
        (root, rels[0]) for root, rels in by_root.items()
        if root and root not in shipped and len(rels) > 1)
    if not candidates:
        pytest.fail(
            "no non-shipped conf.d root holds more than one defaults artifact, "
            "so this test can no longer build the state it checks. It is NOT "
            "repairable by relaxing the assertions: the property is that an "
            "artifact exemption cannot hide a file from the schema witness. "
            "Re-express it against whatever shape the tree now has — do not "
            "skip it, and do not delete it.")
    victim_root, victim = candidates[0]

    monkeypatch.setitem(gate._DEFAULTS_ARTIFACT_EXEMPT, victim, "pinned by this test")
    filtered = {str(p.relative_to(gate.PROJECT_ROOT)).replace("\\", "/")
                for p in gate._defaults_artifacts()}
    assert victim not in filtered, (
        "the exemption did not take, so the two populations are identical and "
        "this test cannot tell the call site's two candidates apart", victim)

    seen: list[list[str]] = []
    real = gate._assert_defaults_artifacts_match_schema

    def spy(rels, read=None):
        seen.append(list(rels))
        return real(rels, read)

    monkeypatch.setattr(gate, "_assert_defaults_artifacts_match_schema", spy)
    try:
        gate._defaults_faces()
    except gate._GateViolation as exc:
        if not seen:
            pytest.fail(
                "a floor raised before the witness ran, so this run says "
                f"NOTHING about the call site. Exempting {victim!r} was "
                f"supposed to leave {victim_root!r} non-empty and every "
                f"floor quiet; it did not: {exc}")

    # ⚠️ This `except` also swallows the post-witness judged-count floor, which
    # is the only code between the witness and the return. That floor is
    # therefore invisible to this test BY CONSTRUCTION, not by oversight; its
    # own gap is recorded in the disclosure block below.
    assert seen, "the witness was never called and no floor raised — unreachable"
    assert victim in seen[0], (
        "the exempted artifact never reached the schema witness: an artifact "
        "exemption now buys schema invisibility, which is the two-line disarm "
        "#1443 exists to prevent", victim, len(seen[0]))


# ── ⛔ WHAT THIS FILE DELIBERATELY STOPPED GUARDING (#1443, round 4) ─────────
#
# NINE test functions stood here and were removed, and two were written in
# their place. Not because they were wrong — each killed a real mutation — but
# because every one was measured to REDDEN on a legitimate edit, and four
# rounds of blind review produced more defects in this machinery than in the 60
# lines it guards. The rule applied: a guard earns its place when its silent
# failure lets the ORIGINAL defect back.
#
# ⚠️ A disclosure that is wrong is worse than none — it is read as a
# measurement. Every line below names how it was measured.
#
# What is no longer guarded, with the survival facts measured:
#   * the hook's `additional_dependencies` — nothing asserts this list (repo
#     grep: the only mentions are prose). Dropping `jsonschema` surfaces as a
#     RuntimeError the first time the hook runs, not as a red test.
#   * that `jsonschema` and `check_confd_schema` stay LAZY, in-function imports.
#     Hoisting either to module level is invisible to this module, and it is
#     what keeps `--help` working in an environment with no jsonschema.
#   * the `files:` filter's `check_confd_schema.py` entry — hand-added, because
#     the gate reaches that sibling by `import` and the filter test derives its
#     inputs from imports, path constants and the pack roster within
#     `search_dirs`, which has no `scripts/tools/lint` entry (#1413).
#     ⚠️ NOT the schema JSON, which is still covered. Measured — deleting
#     `docs/schemas/platform-defaults\.schema\.json` from `files:` reddens
#     `test_precommit_filter_covers_every_input_this_gate_reads`, naming that
#     exact path, because `_PLATFORM_DEFAULTS_SCHEMA` is a module-level `Path`
#     and that test derives from every such constant. Overstating a gap costs
#     the next reader a guard they already have.
#   * the call-site floor on the judged count. Measured: deleting those eight
#     lines leaves this module green. The DEFAULT READER half is genuinely
#     subsumed — stubbing the reader makes
#     `test_the_schema_witness_is_silent_on_the_real_tree` fail on its
#     independent recount (17 vs 16) — but the floor itself is not; the two
#     were disclosed as one item and only one of them was true.
#   * (NOW GUARDED — #1458 blind review, round 4) the witness's position.
#     There is still no AST assertion; the CONSEQUENCE is pinned behaviourally
#     instead, once per floor, by
#     `test_every_floor_that_runs_before_the_witness_is_still_heard_over_it`,
#     and for the loader-readable floor's real message by
#     `test_the_schema_witness_speaks_last_so_a_floor_is_heard_first`. Leaving
#     the old disclosure here would cost the next reader a guard they already
#     have, which is the failure this block warns about above.
#   * the order of the floors AMONG THEMSELVES — measured, and NOT an
#     equivalence. `_assert_every_root_contributes` and
#     `_assert_every_root_stays_loader_readable` both raise on one input (a
#     root whose `_defaults.yaml` was renamed AND whose `defaults:` section was
#     renamed — #1458's two moves at once), with different messages, so
#     swapping them changes which diagnosis the caller reads. The swap is green
#     today. Left unguarded on purpose: the witness is the case #1443 is about,
#     and pinning every pairwise order would freeze a call sequence that
#     legitimately grows. ⚠️ An earlier wording called this pair an EQUIVALENT
#     mutation because "their domains do not overlap"; that argument was wrong
#     — the `continue` it rested on hands over one arm, not both.
#   * the witness's threshold BOUNDARY. Replacing `judged < ...` with
#     `judged <= ...` survives: every rig drives `judged` to 0, far under the
#     floor, so nothing exercises `judged == _DEFAULTS_ARTIFACT_FLOOR`.
#   * the assertion that the gate calls the sibling's judgement rather than a
#     private copy. Measured: substituting an inline copy of
#     `defaults_doc_violations` into this gate leaves BOTH modules at baseline
#     — 125 passed/3 errors here, 36 passed there. ⚠️ `check_confd_schema.py`'s
#     own docstring still calls that single implementation "the point"; that
#     sentence is enforced by nothing, and it is the sibling's to reword.
#   * the derived silent/caught partition — its two set assertions were true by
#     construction, and the pin above is what actually held.
#   * part of #1434's reopen trigger (`no tracked artifact has a `None` conf.d
#     root`). `tests/ops/test_guard_defaults_scopes.py` still asserts it for
#     files named exactly `_defaults.yaml`, which is 16 of the 17 tracked
#     artifacts; what nothing observes is this gate's deliberate `_defaults*`
#     PREFIX widening — a `_defaults-multidb.yaml`-shaped name outside every
#     conf.d tree. The replacement test asserts such a file is validated and
#     stays silent when it is schema-clean, which is intended.
#
# ⛔ STILL GUARDED, and deliberately: that the CALL SITE hands the witness the
# TRACKED scan rather than the exempt-filtered one. Measured on the real tree
# with one exempted, schema-INVALID artifact in a non-shipped root — rc=1
# naming the offender as it stands, rc=0 `✅ OK` with the exempt-filtered
# population. Nothing else in this module tells the two apart: with the
# exemption alone the failure set is the same either way (2 failures, unrelated
# to it). Its silent failure brings the original defect back, so it earns its
# place: `test_the_call_site_hands_the_witness_the_tracked_scan_not_the_exempt_set`.
#
# ⚠️ Also unguarded: the `TypeError` re-raise predicate, and the WIDENING of
# the read's `except` from `(OSError, yaml.YAMLError)` to `Exception`. Measured:
# reverting to the narrow pair leaves every test green, because the only test
# that reaches that arm feeds a YAML syntax error through the reader seam and
# never decodes bytes — and `Exception` is there for `UnicodeDecodeError` on a
# non-UTF-8 artifact. Narrowing further, to bare `OSError`, IS caught. So the
# arm has a floor but not a ceiling.
#
# ⚠️ And one shape of OVER-reporting: a witness that invents a violation on a
# legal top-level key is caught only for the keys today's tree actually uses.
# Measured across the 17 tracked artifacts, that is five — `defaults` (15),
# `state_filters` (2), `optional_overrides` (2), `_custom_alerts` (1),
# `_routing_defaults` (1) — so a mutant that reddens on, say, `_metadata:`
# survives. Closing it would mean synthesising a document per schema property,
# which is the derived-classifier shape blind review has now broken twice: the
# probe value that is legal for one property violates another, so the fixture
# ends up encoding the schema a second time.



def test_the_tracked_root_universe_is_derived_from_the_index_not_from_the_pin(
        monkeypatch):
    """⛔ `_tracked_confd_roots` must READ the index, and nothing used to say so.

    Measured (mutation): replacing its whole body with
    `set(_DEFAULTS_CONFD_ROOTS) | set(_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS)`
    — the function answering from the two lists it exists to CHECK — left the
    suite at 105 passed and the gate at rc=0, and a fresh conf.d tree carrying
    no `_defaults*` file then went unreported again: #1411's hole, reopened, in
    silence. Every other test here either takes this function's own output as
    its baseline and adds or removes one entry, or asserts `tracked ==
    registered`, which that body satisfies by construction.

    ⛔ Asserting "it contains some root that the defaults scan cannot reach"
    does NOT kill it either — the two artifact-less roots are exactly what the
    second list holds, so the impostor returns them too. The seam is what tells
    the two bodies apart: feed a SYNTHETIC listing and require the answer to
    follow it, which no body ignoring its input can do.
    """
    synthetic = [
        "a/conf.d/tenants.yaml",                  # a root, one level down
        "b/nested/conf.d/deep/deeper/x.yml",      # a root, several levels down
        "c/conf.d",                               # the directory entry itself…
        "d/notconfd/y.yaml",                      # …and two non-roots
        "README.md",
    ]
    assert gate._tracked_confd_roots(synthetic) == {"a/conf.d", "b/nested/conf.d"}
    assert gate._tracked_confd_roots([]) == set()
    # nesting resolves to the INNER root, the same way `conf_d_root` defines it
    assert gate._tracked_confd_roots(
        ["p/conf.d/q/conf.d/r.yaml"]) == {"p/conf.d/q/conf.d"}


    # …and the no-argument default is the index too, not some third answer.
    # ⛔ The listing is fetched HERE, by this test, rather than through
    # `gate._tracked_paths` — routing both sides through the same helper would
    # make this line true for a `_tracked_paths` that had itself gone constant.
    out = subprocess.run(
        ["git", "-C", str(gate.PROJECT_ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60)
    listing = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    assert gate._tracked_confd_roots() == gate._tracked_confd_roots(listing)
    assert gate._tracked_confd_roots() != set()

    # ⛔⛔ AND THE NO-ARGUMENT PATH HAS TO SEE AN INJECTION, which is the half
    # the two lines above cannot supply. Measured: rewriting this function as
    # "scan honestly when `paths` is given, return the two registration lists
    # unioned when it is not" left the suite at 107 passed and the gate at
    # rc=0 — and with that body, a new conf.d tree carrying no `_defaults*`
    # file went unreported again, i.e. #1411's hole re-opened while every
    # assertion above stayed green. It stayed green because the injected cases
    # all pass `paths`, and the equality above compares two calls that are BOTH
    # the default path: today they agree whether or not the default reads the
    # index, so the comparison cannot tell an honest default from a fake one.
    #
    # Feeding the injection through `_tracked_paths` instead is what closes it:
    # the default path has to consult that seam, so a body that answers from
    # the registration lists cannot follow a synthetic listing it never reads.
    monkeypatch.setattr(gate, "_tracked_paths",
                        lambda: listing + ["zz/injected/conf.d/tenants.yaml"])
    assert "zz/injected/conf.d" in gate._tracked_confd_roots(), (
        "`_tracked_confd_roots()` did not follow `_tracked_paths` on its "
        "no-argument path — it is answering from something other than the index")
    # …and it must DROP what leaves the listing, not merely add what appears in
    # it: a body that unions the registration lists with the seam's output
    # satisfies the line above and still cannot see a disappearance.
    surviving = [p for p in listing if not p.startswith("try-local/")]
    monkeypatch.setattr(gate, "_tracked_paths", lambda: surviving)
    assert "try-local/seed/conf.d" not in gate._tracked_confd_roots(), (
        "a root that left the listing is still being reported — the answer is "
        "being unioned with a registration list rather than derived")


def test_a_conf_d_tree_spelled_in_another_case_is_refused_not_ignored():
    """⛔ The universe above is derived with a case-SENSITIVE match, while the
    artifact predicate folds case on purpose — so a `CONF.D/` tree was read as
    "not a conf.d tree at all" and fell out of this floor, the `unpinned` arm
    and the registration exit-lock together (#1434).

    Measured on the real index BEFORE this refusal existed: adding a `CONF.D`
    tree holding a `_defaults.yaml` left the gate at rc=0, and so did one
    holding none; both spellings of the same tree in lower case reddened it.
    That is the counterfactual — the silence was coverage that had to be
    bought, not an already-covered case being reported a second time.

    ⚠️ SYNTHETIC LISTINGS, never directories on disk. Both filesystems in play
    here (NTFS, and the container's 9p mount of it) are case-insensitive, so a
    real `CONF.D` directory would prove nothing about what CI sees. `git
    ls-files` answers the same everywhere, which is why the rule is written
    about the index.
    """
    real = gate._tracked_paths()
    gate._tracked_confd_roots(real)      # today's index must stay silent

    # ⛔ DEPTH, and the first version of this probe had only one. Every case was
    # `x/{spelling}/_defaults.yaml`, so the immediate parent was always the
    # offending directory — and mutation showed the walk could be narrowed to
    # `.parents[:1]` with the whole suite still green. The real index is not
    # flat: of the 17 tracked artifacts, 6 sit below their root and the deepest
    # is three levels down (`…/full-l0-l3/conf.d/db/mariadb/prod/`), so the
    # narrowed walk would go silent on the layout this repo actually uses.
    for spelling in ("CONF.D", "Conf.d", "conf.D"):
        for probe in (f"x/{spelling}/_defaults.yaml",
                      f"x/{spelling}/db/mariadb/prod/_defaults.yaml"):
            with pytest.raises(gate._GateViolation) as exc:
                gate._tracked_confd_roots(real + [probe])
            assert f"x/{spelling}" in str(exc.value), exc.value
        # …and with no artifact in it either, which is the half the exit-lock
        # owns and the half a scan-derived universe cannot see.
        with pytest.raises(gate._GateViolation):
            gate._tracked_confd_roots(real + [f"x/{spelling}/tenant-a.yaml"])
        # …and a Windows-style listing, because `conf_d_root` normalises those
        # and this refusal has to agree with it rather than quietly disagree.
        with pytest.raises(gate._GateViolation):
            gate._tracked_confd_roots(
                real + [f"x\\{spelling}\\db\\_defaults.yaml"])

    # ⛔ Not a name-similarity check: these are different directories, not the
    # same one misspelled, and reporting them would be a false red on a layout
    # nothing in the repo forbids.
    for benign in ("conf_d", "confd", "conf.dd", "conf.d.bak"):
        gate._tracked_confd_roots(real + [f"x/{benign}/_defaults.yaml"])
    # a FILE that case-folds to conf.d is not a tree either
    gate._tracked_confd_roots(real + ["x/CONF.D"])


def test_the_index_listing_is_a_real_seam_on_both_derivations():
    """⛔ `_tracked_paths` is injectable so BOTH derivations can be fed a world.

    `_tracked_defaults_artifacts`' docstring said "a test injects a synthetic
    listing" while nothing did — the parameter had no caller and no assertion,
    so the sentence described an intention and the artifact half of the seam was
    dead code with a claim attached. A seam nobody pulls is indistinguishable
    from a seam that does not work.
    """
    # ⚠️ The predicate also requires the file to EXIST on disk, so a synthetic
    # listing has to name real files. Real defaults artifacts are used as the
    # positive cases and real non-artifacts as the negatives; what is synthetic
    # is the LISTING, which is the input under test.
    real = gate._tracked_defaults_artifacts()
    assert len(real) >= gate._DEFAULTS_ARTIFACT_FLOOR, real

    assert gate._tracked_defaults_artifacts([]) == []
    assert gate._tracked_defaults_artifacts(real[:2]) == sorted(real[:2])
    # a path that is in the listing but is not an artifact is filtered out…
    assert gate._tracked_defaults_artifacts(
        real[:1] + ["CHANGELOG.md", "Makefile"]) == real[:1]
    # …and one that never reaches the listing is not conjured up from anywhere.
    assert real[-1] not in gate._tracked_defaults_artifacts(real[:-1])
    # a name that matches the predicate but has no file behind it is dropped,
    # so an injected listing cannot manufacture an artifact out of a string.
    assert gate._tracked_defaults_artifacts(
        ["nowhere/at/all/conf.d/_defaults.yaml"]) == []


def test_the_index_listing_really_comes_from_the_index(tmp_path):
    """⛔ The seam moved the question up a level; this is where it stops.

    Injecting `paths` pins the CALLEES. It says nothing about the argument they
    build when nobody passes one, and that is `_tracked_paths` itself. Measured
    against the suite as it stood at `3864b63`, before this test existed:
    replacing its whole body with a literal list of today's answer left that
    suite at 107 passed and the gate at rc=0 — every test either fed its own
    listing or compared this against a `git ls-files` it ran itself, and a
    constant copied from today satisfies both. It would drift eventually, when
    some unrelated commit adds a file; "eventually" is a delay, not a tripwire.

    ⚠️ THE POPULATION IS PART OF THAT NUMBER. Re-run on the final tree, the
    same mutation is 1 failed / 113 passed and the one red is THIS test — which
    is the point, but a reader who meets "107 passed" beside a 114-test file
    has been handed a figure that no longer describes anything. Same defect as
    a fraction quoted without the set it was counted over, which is what this
    whole issue is about.

    So the function is pointed at a DIFFERENT repository. A body that ignores
    `PROJECT_ROOT` cannot follow, and no snapshot of THIS repo can be right
    about a repo that did not exist when the snapshot was taken.
    """
    (tmp_path / "some" / "conf.d").mkdir(parents=True)
    (tmp_path / "some" / "conf.d" / "_defaults.yaml").write_text(
        "defaults:\n  x: 1\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("not added\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(tmp_path), "add",
                    "some/conf.d/_defaults.yaml"], check=True,
                   capture_output=True, timeout=60)

    real_root = gate.PROJECT_ROOT
    try:
        gate.PROJECT_ROOT = tmp_path
        assert gate._tracked_paths() == ["some/conf.d/_defaults.yaml"], (
            "`_tracked_paths` did not follow PROJECT_ROOT — it is not reading "
            "an index, it is reciting one")
    finally:
        # ⛔ Restored by hand rather than with monkeypatch because
        # `_tracked_paths` is module state that everything else in this file
        # reads; a teardown that ran late would hand another test a listing of
        # a directory that has since been deleted.
        gate.PROJECT_ROOT = real_root
    assert gate.PROJECT_ROOT == real_root
    assert len(gate._tracked_paths()) > 100, "the real listing did not come back"


def test_every_tracked_confd_root_is_registered_somewhere():
    """⛔ The comparison base is the INDEX, not the defaults scan (#1411).

    Compared against the scan, "a new root must be registered" cannot fire for a
    root with no `_defaults*` file — it never reaches the scan, so there is
    nothing to find unpinned. Measured: two such roots were already tracked, and
    adding a third left the gate at rc=0 and the suite at 96 passed.

    The two lists are about FILES and must stay disjoint: holding a defaults
    artifact and not holding one are not both true of the same tree.

    ⛔ SCOPE, written down because a deleted test's coverage was accounted to
    this one (#1434). What the assertions below give, exactly, is
    `scanned ⊆ pin`: `tracked == pin ∪ without` plus `without & scanned == ∅`
    leaves a scanned root nowhere else to be. They do NOT give the other
    direction — a root PINNED here while holding no defaults artifact passes
    every line of this test. That half belongs to the gate's `missing` arm and
    to the equality in
    test_every_scanned_root_is_wired_to_the_fifth_floor_through_the_real_entry
    (the second one over the non-exempt universe only). Narrowing any of those
    three narrows the invariant; this test cannot pick up the slack.
    """
    tracked = gate._tracked_confd_roots()
    without = set(gate._DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS)
    assert tracked >= set(gate._DEFAULTS_CONFD_ROOTS) | without, {
        "registered but not tracked": sorted(
            (set(gate._DEFAULTS_CONFD_ROOTS) | without) - tracked)}
    assert not (set(gate._DEFAULTS_CONFD_ROOTS) & without), sorted(
        set(gate._DEFAULTS_CONFD_ROOTS) & without)
    assert tracked - (set(gate._DEFAULTS_CONFD_ROOTS) | without) == set(), {
        "tracked but registered nowhere": sorted(
            tracked - (set(gate._DEFAULTS_CONFD_ROOTS) | without))}

    # …and every "no defaults artifact" claim is re-checked against the scan,
    # in both directions, so the list cannot become a quiet exemption: an entry
    # that grows a `_defaults*` file has to move to the other list.
    scanned = _scanned_confd_roots()
    assert not (without & scanned), sorted(without & scanned)
    for root, why in gate._DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS.items():
        assert len(why) > 40, (root, why)   # a reason, not a placeholder


def test_an_unregistered_new_root_is_a_violation_even_with_no_defaults_file():
    """The arm that was structurally unable to fire before (#1411)."""
    by_root = {r: _root_face(r, "x") for r in gate._DEFAULTS_CONFD_ROOTS}
    tracked = gate._tracked_confd_roots() | {"components/brand-new/conf.d"}

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root, tracked)
    msg = str(exc.value)
    assert "components/brand-new/conf.d" in msg, msg
    assert "registered nowhere" in msg, msg
    # ⛔ and it must NOT send the reader to the empty-by-design list, which is
    # about content and would claim a check that structurally cannot run.
    assert "_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS" in msg, msg
    assert "is NOT _DEFAULTS_ROOTS_MAY_BE_EMPTY" in msg, msg


def test_a_registered_root_that_left_the_index_is_a_violation():
    """A pin that lags reality has already absorbed the disappearance."""
    by_root = {r: _root_face(r, "x") for r in gate._DEFAULTS_CONFD_ROOTS}
    gone = sorted(gate._DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS)[0]
    tracked = gate._tracked_confd_roots() - {gone}

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes(by_root, tracked)
    assert gone in str(exc.value), exc.value
    assert "no longer exist in the index" in str(exc.value), exc.value


def test_the_label_round_trip_floor_fires_and_counts_what_broke(monkeypatch):
    """⛔ The floor that keeps the fifth floor's NUMERATOR from going vacuous.

    `run_check` never sees `read_pairs`; it rebuilds them by inverting the
    artifact labels, so the whole numerator rests on `_artifact_face` and
    `_artifact_rel` agreeing. A label format that stayed human-identical but
    stopped round-tripping made the gate print `0 of 0` — all four conditions
    spelled out word for word — and exit 0 with `✅ threshold reachability OK`.

    ⛔ That floor shipped with NO TEST AT ALL. Measured: disabling it outright
    left the suite at 107 passed and the gate at rc=0. It does work — with the
    inversion broken, a one-sided drift takes the gate from rc=0 to rc=1 — but
    "it works today" and "something will say so tomorrow" are different claims,
    and this file's whole subject is the gap between them.

    ⚠️ TWO WAYS TO BREAK IT, and the message used to be honest about only one.
    A label can fail to invert (`None`) or invert to the WRONG path, and the
    count reported was of the `None`s while the TRIGGER was set inequality — so
    the invertible-but-wrong case fired correctly and then announced `0 of 17`,
    a breach reporting nothing broken.
    """
    real = gate._artifact_rel
    n_artifacts = len(gate._defaults_artifacts())

    # 1) labels that do not invert at all
    monkeypatch.setattr(gate, "_artifact_rel", lambda _face: None)
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    msg = str(exc.value)
    assert "round-trip" in msg, msg
    assert f"{n_artifacts} of {n_artifacts} " in msg, (n_artifacts, msg)
    monkeypatch.undo()

    # 2) labels that invert to something that is not None and not right — the
    #    case the old count reported as `0 of 17`.
    monkeypatch.setattr(gate, "_artifact_rel",
                        lambda face, _r=real: (_r(face) or "") + "-drifted")
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    msg = str(exc.value)
    assert "round-trip" in msg, msg
    assert not msg.startswith("0 of "), (
        "an invertible-but-wrong label set was reported as nothing broken — "
        f"the count is following `is None` rather than the trigger: {msg[:200]}")
    assert f"{n_artifacts} of {n_artifacts} " in msg, (n_artifacts, msg)
    monkeypatch.undo()

    # 3) …and one broken label out of the set is reported as one, not as all of
    #    them: the count has to be a count, not a flag.
    victim = gate._artifact_face(
        gate._defaults_artifacts()[0].relative_to(gate.PROJECT_ROOT).as_posix())
    monkeypatch.setattr(
        gate, "_artifact_rel",
        lambda face, _r=real, _v=victim: None if face == _v else _r(face))
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    assert f"1 of {n_artifacts} " in str(exc.value), str(exc.value)[:200]
    monkeypatch.undo()

    # 4) and with both halves back, the real labels really do round-trip — or
    #    every assertion above is about a floor that is always firing.
    gate._defaults_faces()


def test_the_fifth_floor_figure_is_printed_with_its_universe_and_floor_subset(
        capsys):
    """⛔ The COUNT may never be printed alone (#1411, and #1392's policy).

    A bare number is exactly what went stale here. ⚠️ Stated with what the
    record supports: the claim that 11 of the 12 conf.d roots could go to zero
    unseen was written into SIX PLACES IN ONE COMMIT (`188cfb17`) and then
    rewritten wholesale — the gate comment, this file, the CHANGELOG entry
    twice, the COMMIT SUBJECT and the COMMIT BODY. An earlier wording
    here said it "survived three copy-edits", which nothing in the history
    shows, and the neighbouring sentence's "in three other places" is what that
    seems to have been read off — a count of PLACES turning into a count of
    REVISIONS. One commit is enough to make the point: the number could be
    written six times without anything being able to check it once.

    ⚠️ THREE CORRECTIONS TO THAT CORRECTION, each found by going back to the
    commit rather than to the sentence about it — and the first two were made
    in the same direction twice, which is the joke this docstring keeps being
    the punchline of:
      * FIVE, not four. The commit SUBJECT carries it, and that is the copy
        `git log` puts in front of everyone.
      * SIX, not five. The commit BODY carries it as well ("12 棵 conf.d root
        有 11 棵能整棵歸零而四道 floor 全不響"). A sentence whose entire content
        is "this was written in N places" has now under-counted N twice.
      * the literal string `11 of 12` never appeared anywhere. Two of the six
        are English — "of the 12 distinct conf.d roots, 11" in the gate comment
        and "11 of the 12 conf.d roots" in this file — and the other four are
        the Chinese wording (CHANGELOG ×2, commit subject, commit body), so
        quoting only the English pair as if it covered all of them is its own
        small version of the same error. Writing it in backticks made it look
        like something a reader could grep for, and grepping finds nothing,
        which reads as "the claim is false" rather than "the claim was
        paraphrased".

    ⛔ And it did not go stale by drifting away from a measurement — it went
    stale when its GROUPING was edited and its numerator was not. So the line
    has to carry the population the numerator is drawn from, not just a
    denominator: `N of M` where M counted a different set than N is how a true
    number becomes a false sentence. Hence the grouping, scenario, floor subset,
    exemption AND the scanned/exempt/watched arithmetic all ride on it.
    """
    gate.main([])
    line = [ln for ln in capsys.readouterr().err.splitlines()
            if "fifth-floor coverage" in ln]
    assert len(line) == 1, line
    msg = line[0]

    for token in ("conf_d_root",                       # the grouping
                  "keys go to zero",                   # the scenario…
                  "files left in place",               # …and its other half
                  "all four earlier floors silent",    # the floor subset…
                  "file floors", "per-class key floors", "_SHIPPED_CONFD_ROOTS",
                  "_DEFAULTS_ROOTS_MAY_BE_EMPTY"):     # the exemption
        assert token in msg, (token, msg)

    # and the figure itself is DERIVED, not typed: it must equal what the probe
    # says on the real faces right now.
    #
    # ⛔ RECOUNTED, not re-asked. This line used to call
    # `_roots_only_the_fifth_floor_notices` — the very function whose output the
    # line is checking — which made it true for any implementation. Measured:
    # dropping the first element of that function's return value left the suite
    # at 107 passed with `main()` printing `6 of 11` for a true 7.
    #
    # ⚠️ DISCLOSED, per this round's stopping rule: the recount below and the
    # gate both start from `_defaults_faces()`, so this pins the ARITHMETIC over
    # the faces and not the face set itself. If the scan came back with half the
    # artifacts, both sides would shrink together and this test would stay
    # green.
    # ⛔ AND THE GAP IS BIGGER THAN THE EARLIER WORDING ADMITTED. It said the
    # face set is "guarded ELSEWHERE, by the two file floors, both key floors
    # and the fifth floor's own `missing` arm". What those five actually refuse
    # to let past is the SCAN SHRINKING BELOW A FLOOR — not an artifact
    # leaving. Measured, one file at a time, on all four artifacts under
    # `tests/golden/fixtures/full-l0-l3/conf.d/**`: delete any ONE of them and
    # all five stay silent, the gate exits 0, and the fifth-floor line prints
    # the same fraction it printed before (only the artifact face count and the
    # key total move). So what is disclosed is "a face set that has lost
    # individual members reads as healthy here", which is a real hole with a
    # cheap demonstration, not the narrow one the old sentence described.
    # It is still left unguarded, per the stopping rule: those artifacts are
    # nested layers of ONE fixture tree, the root itself stays non-empty, and
    # the defect #1411 is about — a whole tree going silent — is what the fifth
    # floor does catch. Described at its true size rather than talked down.
    generators, artifacts = gate._defaults_faces()
    read_pairs = [(gate._artifact_rel(f), k) for f, k in artifacts.items()]
    expected = _blind_roots_recounted(generators, read_pairs)
    # …and the reported list must BE that recount, element for element — a
    # numerator that is merely the right LENGTH is satisfied by naming the
    # wrong roots.
    assert gate._roots_only_the_fifth_floor_notices(
        generators, read_pairs) == expected, (
            gate._roots_only_the_fifth_floor_notices(generators, read_pairs),
            expected)
    for root in expected:
        assert root in msg, (root, msg)

    # ⛔ NUMERATOR AND DENOMINATOR OVER THE SAME POPULATION. The numerator is
    # post-exemption (`_roots_only_the_fifth_floor_notices` subtracts
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY` before it starts), so printing it over
    # `scanned` compared a count of watched roots with a count of scanned ones.
    # The predicate the sentence states is true over the pre-exemption
    # population of at least as many roots as the printed numerator; the
    # numerator is correct for the watched population and for nothing else on
    # the line. ⛔ Both counts are recomputed below rather than written down —
    # this comment used to carry today's two integers and a third, which is the
    # same restatement the gate comment it describes was emptied of.
    scanned = {gate.conf_d_root(rel) for rel, _k in read_pairs} - {None}
    watched = scanned - set(gate._DEFAULTS_ROOTS_MAY_BE_EMPTY)
    assert f"{len(expected)} of {len(watched)} WATCHED" in msg, (
        len(expected), len(watched), msg)
    # …and both other terms are printed too, so the reader can redo the
    # subtraction rather than take the denominator on trust.
    assert f"{len(scanned)} scanned" in msg, (len(scanned), msg)
    assert f"{len(scanned) - len(watched)} exempt" in msg, msg


def test_a_floor_breach_says_that_nothing_else_ran_and_means_it(monkeypatch, capsys):
    """⛔ The message and the behaviour are asserted TOGETHER.

    Saying "nothing else ran" is only worth printing if it is true, and the
    previous wording ("the placement checks did NOT run") understated it: the
    floors raise while the faces are being built, before `run_check` appends a
    single error, so the module's headline TRK-337 check and both exit-locked
    ledgers are gone too. Measured: 59 stderr lines → 6.
    """
    monkeypatch.setitem(gate.KNOWN_UNWIRED, "zzz_not_demanded_by_any_alert",
                        "synthetic stale ledger entry")

    gate.main(["--ci"])
    intact = capsys.readouterr().err
    assert "STALE-EXEMPTION" in intact, intact[-800:]

    _trip_shipped_root_floor(monkeypatch)
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION
    broken = capsys.readouterr().err
    assert "NOTHING ELSE IN THIS MODULE RAN" in broken, broken[-800:]
    # …and the claim is true: the ledger exit-lock really did not report
    assert "STALE-EXEMPTION" not in broken, broken[-800:]
    assert "known-unwired " not in broken, broken[-800:]


def test_a_nested_dimensional_key_is_reported_once_with_nested_wording():
    """The `_critical` half's two bugs, repeated one loop down and fixed here:
    the `{` test looked at the whole dotted path (so every CHILD of a dimensional
    key was reported, naming paths that are not keys), and the consequence was
    worded as though the key were flat."""
    walked = gate._defaults_section(
        'defaults:\n  \'redis_q{queue="a"}\':\n    inner: 1\n')
    dim = [e for e in gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": walked}, {}))["errors"]
        if "DIMENSIONAL-IN-DEFAULTS" in e]

    assert len(dim) == 1, dim
    assert 'redis_q{queue="a"}.inner' not in dim[0], dim[0]
    # ⛔ This key is TOP LEVEL but its value is a mapping, and the earlier
    # version of this test asserted the flat wording here — pinning the same
    # defect its `_critical` sibling was fixed for. `Defaults` is
    # map[string]float64, so the file does not decode and resolveDimensionalRows
    # is never reached; saying "resolveDimensionalRows is tenant-only" describes
    # a code path that does not run.
    assert "MAPPING value" in dim[0], dim[0]
    assert "resolveDimensionalRows is tenant-only" not in dim[0], dim[0]

    # a genuinely nested one gets the nested wording
    nested = [e for e in gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _nested(a={'redis_q{queue="b"}': 1})}, {}),
    )["errors"] if "DIMENSIONAL-IN-DEFAULTS" in e]
    assert len(nested) == 1, nested
    assert "SECOND, independent problem" in nested[0], nested[0]

    # …and a genuinely FLAT one still gets the flat wording
    flat = [e for e in gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": _flat('redis_q{queue="c"}')}, {}),
    )["errors"] if "DIMENSIONAL-IN-DEFAULTS" in e]
    assert len(flat) == 1, flat
    assert "resolveDimensionalRows is tenant-only" in flat[0], flat[0]


def test_a_flat_key_containing_a_dot_is_not_diagnosed_as_nested():
    """⛔ Depth comes from the walk, never from a dot in the rendered path.

    `defaults: {"a.b_critical": 90}` is a legal FLAT key. An earlier revision
    decided nesting with `"." in key` and reported it with the nested
    consequence — the wrong failure mode, sending the reader after a
    parse-failure metric that will not exist (found by blind review; no tracked
    artifact has such a key today, so nothing else would have surfaced it).
    """
    walked = gate._defaults_section('defaults:\n  "a.b_critical": 90\n')
    assert walked == {"a.b_critical": 0}, walked

    msg = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": walked}, {}),
    )["errors"][0]
    assert 'severity="warning"' in msg, msg
    assert "independent problem" not in msg, msg


def test_the_walk_descends_through_lists():
    """The list branch had a comment defending it and no test walking it.

    A list ITEM carries no key of its own, but a mapping inside one does, and
    `tests/golden/fixtures/full-l0-l3/conf.d/db/_defaults.yaml` already has a
    list under `defaults:` — so the shape is live in the tree, not hypothetical.
    """
    walked = gate._defaults_section("defaults:\n  a:\n    - b_critical: 1\n")
    assert walked == {"a": 0, "a.b_critical": 2}, walked

    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({"probe": walked}, {}),
    )
    assert [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e], result


# ── the coverage figure that replaced the comment (#1392) ────────────────────
#
# ⛔ This change DELETED a count from a comment on the grounds that `--ci` prints
# a live one instead. Blind review measured that the replacement was bare:
# suppressing the whole print, reporting the artifact total as the generator
# total, and inverting the empty-face count all left the suite green. A number
# that can be wrong with nothing turning red is the thing the comment was
# criticised for being.

def test_every_stats_field_is_recomputed_from_the_faces():
    """Each field, against an independent recount of the same real faces.

    ⛔ EVERY field, and the three the fifth floor added (#1411) had to be
    written in after the fact — measured: pinning `confd_roots_scanned` to a
    literal 0, and separately to 99, each left the suite at 105 passed and the
    gate at rc=0, printing a denominator with no relationship to the tree.
    `confd_roots_exempt_from_fifth_floor` was the same. The contrast is what
    makes it a defect rather than an oversight: falsifying the neighbouring
    `artifacts_without_defaults` DID go red, because that field was in this
    list. "Each field" is a promise this test has to keep as fields are added.

    ⛔ …and it was broken again by the same commit that wrote it. `roots_only_
    the_fifth_floor_notices` — the one field that is a LIST rather than a count,
    and the only one `main()` prints by name — was not here. "EVERY field" with
    an exception in it is the shape of claim this whole file keeps having to
    correct, so the loop below now starts from `stats.keys()` and requires every
    key to have been visited: a field added later cannot be quietly left out.

    ⚠️ DISCLOSED, per this round's stopping rule: the recount and `run_check`
    both begin at `_defaults_faces()`, so what is pinned is the arithmetic over
    the faces, not the face set. A scan that came back half-sized would move
    both sides together. ⛔ What the two file floors, the two key floors and the
    fifth floor's `missing` arm refuse to let past is a scan that shrinks BELOW
    A FLOOR — not one that loses a member: measured, deleting any single
    artifact under `tests/golden/fixtures/full-l0-l3/conf.d/**` leaves all five
    silent and the gate at rc=0. (The same correction is written out in full
    beside the sibling disclosure in
    `test_the_fifth_floor_figure_is_printed_with_its_universe_and_floor_subset`.)
    Still disclosed rather than guarded a sixth time, but at its real size.
    """
    generators, artifacts = gate._defaults_faces()
    stats = gate.run_check(demand=set(), supply=set(), deferred=set(),
                           known_unwired={})["stats"]

    assert stats["generator_faces"] == len(generators)
    assert stats["artifact_faces"] == len(artifacts)
    assert stats["generator_keys"] == sum(len(v) for v in generators.values())
    assert stats["artifact_keys"] == sum(len(v) for v in artifacts.values())
    assert stats["artifacts_without_defaults"] == sum(
        1 for v in artifacts.values() if not v)

    # ⛔ The root counts are recounted from the LABEL text, not via
    # `_artifact_rel` — the same spelling the sibling tests here use. `run_check`
    # derives them through `_artifact_rel`, so recounting the same way would make
    # this line follow a drift between the label and its inverse instead of
    # catching one (measured: a label format that stops round-tripping prints
    # `0 of 0`, and this recount is one of the things that notices).
    roots = {gate.conf_d_root(label[len(gate._ARTIFACT_FACE_PREFIX):-1])
             for label in artifacts} - {None}
    exempt = roots & set(gate._DEFAULTS_ROOTS_MAY_BE_EMPTY)
    assert stats["confd_roots_scanned"] == len(roots)
    assert stats["confd_roots_exempt_from_fifth_floor"] == len(exempt)
    assert stats["confd_roots_watched_by_fifth_floor"] == len(roots - exempt)
    # …and the three are a real subtraction, not three independent literals that
    # happen to agree today.
    assert (stats["confd_roots_watched_by_fifth_floor"]
            + stats["confd_roots_exempt_from_fifth_floor"]
            == stats["confd_roots_scanned"])
    assert stats["confd_roots_exempt_from_fifth_floor"] > 0, (
        "the exempt count must be exercised by a real entry, or the subtraction "
        "above is satisfied by 0 + N == N and pins nothing")

    # ⛔ The LIST field, recounted without the function that produces it — see
    # `_blind_roots_recounted`. Measured before this line existed: dropping the
    # first element of `_roots_only_the_fifth_floor_notices` left the suite at
    # 107 passed and the gate at rc=0, printing a numerator short by one.
    read_pairs = [(gate._artifact_rel(f), k) for f, k in artifacts.items()]
    assert stats["roots_only_the_fifth_floor_notices"] == _blind_roots_recounted(
        generators, read_pairs), stats["roots_only_the_fifth_floor_notices"]
    # …and it is a real subset of the population it is printed over, so the
    # numerator cannot come from a set the denominator does not describe.
    assert set(stats["roots_only_the_fifth_floor_notices"]) <= roots - exempt
    assert len(stats["roots_only_the_fifth_floor_notices"]) < len(roots - exempt), (
        "every watched root is blind — either the tree changed a great deal or "
        "the probe stopped detecting the earlier floors")

    # …and none of them may be trivially equal to another, or a copy-paste
    # between fields would satisfy every line above.
    assert stats["generator_faces"] != stats["artifact_faces"]
    assert stats["generator_keys"] != stats["artifact_keys"]
    assert stats["confd_roots_scanned"] != stats["artifact_faces"]
    assert stats["confd_roots_scanned"] != stats["confd_roots_watched_by_fifth_floor"]

    # ⛔ "EVERY field" made mechanical: a field added to `stats` later and not
    # asserted above turns this red rather than silently joining the four that
    # already shipped unguarded.
    assert set(stats) == {
        "generator_faces", "artifact_faces", "generator_keys", "artifact_keys",
        "artifacts_without_defaults", "confd_roots_scanned",
        "confd_roots_exempt_from_fifth_floor",
        "confd_roots_watched_by_fifth_floor",
        "roots_only_the_fifth_floor_notices",
    }, sorted(stats)


def test_main_prints_the_coverage_line_on_a_pass_and_on_violations(capsys):
    """It is printed whether the run passes or reports violations — that is the
    basis for not writing the number in a comment. Asserted on `main()`'s real
    output, with the numbers cross-checked against a live recount.

    ⚠️ NOT "pass OR fail", which is what this said and is measurably wrong on
    one path: a FLOOR BREACH makes `run_check` raise while the faces are being
    built, so `main()` returns having printed no coverage line at all (measured,
    by raising `_DEFAULTS_ARTIFACT_KEYS_FLOOR` above today's total: no line, and
    rc=1 only under `--ci` — without the flag the same breach is report-only at
    rc=0). The policy survives the correction — a floor breach means the face
    set is not trustworthy, and a coverage figure computed over an untrustworthy
    scan is worth less than the loud error that replaces it — but the sentence
    supporting the policy has to be the true one.

    ⛔ AND THE NAME WAS PART OF THE FALSE SENTENCE. It said "on every run" while
    this very docstring recorded the path where it is not printed, and that path
    had no assertion at all — a documented exception with nothing holding it.
    The negative is now pinned where the breaches already run, in
    `test_every_floor_breach_is_a_violation_not_a_crash`.
    """
    rc = gate.main(["--ci"])
    err = capsys.readouterr().err
    line = [ln for ln in err.splitlines() if "defaults-tier coverage" in ln]
    assert len(line) == 1, err[-2000:]

    generators, artifacts = gate._defaults_faces()
    for value in (len(generators), len(artifacts),
                  sum(len(v) for v in generators.values()),
                  sum(len(v) for v in artifacts.values())):
        assert str(value) in line[0], (value, line[0])
    assert rc == gate.EXIT_OK, err[-2000:]


def test_the_artifact_label_names_the_file():
    """The round-4 repair was "the reader names the file"; nothing pinned it.

    Measured: relabelling artifacts `artifact (0)` … `artifact (16)` left the
    suite green, and with the scope derived rather than hardcoded, "which file"
    is exactly the information a reader cannot reconstruct.
    """
    _generators, artifacts = gate._defaults_faces()
    labelled = {label[len(gate._ARTIFACT_FACE_PREFIX):-1] for label in artifacts}
    on_disk = {p.relative_to(gate.PROJECT_ROOT).as_posix()
               for p in gate._defaults_artifacts()}
    assert labelled == on_disk, sorted(labelled ^ on_disk)


def test_no_artifact_label_can_be_mistaken_for_a_generator():
    """⛔ Set equality over producers sees a generator that is ADDED, DROPPED or
    RENAMED — but only within the generator half.

    Measured (blind review, mutation): a generator face filed into the ARTIFACT
    dict is invisible to it (17 → 18 artifacts, no EMPTY-FACE, green). The
    structural split makes that hard to do by accident rather than impossible,
    so the claim is narrowed here and backed by the check it actually supports.
    """
    _generators, artifacts = gate._defaults_faces()
    for label in artifacts:
        hits = {p for p in _EXPECTED_GENERATOR_PRODUCERS if p in label}
        assert not hits, (
            f"artifact face {label!r} names generator producer(s) {sorted(hits)} "
            "— a producer was filed into the artifact half")


def test_a_generator_that_shrinks_without_emptying_is_caught(monkeypatch):
    """The artifact side has an end-to-end 'a whole group stopped yielding'
    test; the generator side had only a synthetic one, and mutation showed its
    floor could be set to 2 or to 51 unnoticed.

    ⛔ Patched at the PRODUCER, not at `_defaults_faces`. The floors run inside
    `_defaults_faces`, so replacing that function skips the very check under
    test — the first draft of this test did exactly that and reported DID NOT
    RAISE, which reads identically to "the floor is broken".

    ⛔ …and patched via `gate.scaffold_tenant`, the name the gate itself
    resolves. Patching a locally-imported `scaffold_tenant` passed when this file
    ran alone and FAILED in the full `tests/lint/` run — a different module
    object won the import there, so the patch landed on something nobody called.
    """
    monkeypatch.setattr(gate.scaffold_tenant, "generate_defaults",
                        lambda _packs: {"defaults": {"one_surviving_key": 1}})
    # the patch must actually be the thing the gate calls, or a DID-NOT-RAISE
    # below would be read as "the floor is broken"
    assert gate.scaffold_tenant.generate_defaults([])["defaults"] == {
        "one_surviving_key": 1}

    with pytest.raises(RuntimeError, match="GENERATOR faces yielded"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})


def _trip_scan_floor(monkeypatch):
    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", list)


def _trip_case_spelling(monkeypatch):
    # ⛔ Through `_tracked_paths`, the seam the refusal actually reads. Patching
    # `_tracked_confd_roots` itself would skip the check under test — the same
    # mistake `test_a_generator_that_shrinks_without_emptying_is_caught` records
    # having made at the producer level.
    listing = gate._tracked_paths()
    monkeypatch.setattr(gate, "_tracked_paths",
                        lambda: listing + ["zz/CONF.D/_defaults.yaml"])


def _trip_read_floor(monkeypatch):
    scanned = gate._tracked_defaults_artifacts()
    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_EXEMPT",
                        {rel: "x" * 40 for rel in scanned})


def _trip_shipped_root_floor(monkeypatch):
    root = "components/threshold-exporter/config/conf.d"
    kept = [rel for rel in gate._tracked_defaults_artifacts()
            if not (rel == root or rel.startswith(root + "/"))]
    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / r for r in kept])


def _trip_generator_key_floor(monkeypatch):
    monkeypatch.setattr(gate.scaffold_tenant, "generate_defaults",
                        lambda _packs: {"defaults": {"one_surviving_key": 1}})


def _trip_artifact_key_floor(monkeypatch):
    # ⛔ A SEPARATE case from the generator one. `_assert_keys_floor` raises from
    # two branches and the parametrisation used to walk only the generator side —
    # measured: putting the artifact branch back on a bare `RuntimeError` left
    # the suite green, and that branch is the one ordinary maintenance (retiring
    # a fixture) actually trips.
    # ⛔ …and the victim is `_VANISHED_GROUP`, NOT `tests/e2e-bench/` (#1544):
    # the two bench roots are outside this floor's denominator now, so removing
    # them changes its total by zero and this case would pass on the fifth
    # floor's message with the artifact branch never reached — the same silent
    # retirement the paragraph below records.
    kept = [rel for rel in gate._tracked_defaults_artifacts()
            if not rel.startswith(_VANISHED_GROUP)]
    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / r for r in kept])
    # ⛔ …and the fifth floor (#1411) has to be stood down, or this case stops
    # being about the artifact key floor at all. Dropping the victim group
    # empties a PINNED root, so `_assert_every_root_contributes`' `missing`
    # arm raises first and the parametrised assertion passes on ITS message.
    # Measured: with the artifact key floor deleted outright, and again with it
    # put back on a bare `RuntimeError`, this case still passed — it had
    # silently stopped guarding the branch it is named for. (Counterfactual
    # across commits: the same param FAILED at `72fdaf56` with the floor
    # disabled and PASSED at `005ac8d`.) Same stand-down as the sibling
    # `test_the_artifact_key_floor_backstops_a_vanished_group`, which meets the
    # shadowing head-on by asserting both floors in the order a reader hits them.
    monkeypatch.setattr(gate, "_assert_every_root_contributes", lambda _b: None)


@pytest.mark.parametrize("trip,names_its_own_floor", [
    (_trip_scan_floor, "defaults-artifact scan found only"),
    (_trip_read_floor, "survive the exemption table"),
    (_trip_shipped_root_floor, "shipped conf.d root"),
    (_trip_generator_key_floor, "GENERATOR faces yielded"),
    (_trip_artifact_key_floor, "ARTIFACT faces yielded"),
    (_trip_case_spelling, "differ from 'conf.d' only by case"),
], ids=["scan", "read", "shipped-root", "generator-keys", "artifact-keys",
        "case-spelling"])
def test_every_floor_breach_is_a_violation_not_a_crash(
        monkeypatch, capsys, trip, names_its_own_floor):
    """⛔ Exit-code semantics, per `scripts/tools/_lib_exitcodes.py`: 1 is "the
    tool ran correctly and found something the USER must act on", 2 is "the tool
    could NOT do its job". A deleted shipped `_defaults.yaml` is the first, and
    reporting it as `crashed` / rc=2 tells the reader their environment is
    broken — which is read as flaky-lint-skip-it.

    ⛔ EVERY floor, parametrised. A single case that tripped only the scan floor
    left the other three free to go back to a bare `RuntimeError`: mutation put
    the shipped-root floor back on the crash path and nothing went red, because
    no test walked that one.

    ⛔⛔ AND EVERY CASE HAS TO REACH THE FLOOR IT IS NAMED FOR. The
    `artifact-keys` case was found to have stopped doing so — its probe woke the
    fifth floor first and the assertion passed on the neighbour's message — and
    it was repaired by standing the fifth floor down in that helper. ⚠️ That was
    a fix to the cited instance, and the same defect was live in three of the
    other four: `scan`, `read` and `shipped-root` each pass on ANY floor's
    message, because "rc is 1 and the word `crashed` is absent" is a property of
    every breach. Measured: with the floor a case is named for disabled
    outright, the case still passed — a neighbouring floor caught the same
    probe. The whole parametrisation is therefore pinned to the message its own
    floor emits, which is the discipline `test_the_two_floors_blame_the_right_cause`
    and the two `match="shipped conf.d root"` call sites already use.
    """
    trip(monkeypatch)
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "crashed" not in err, err[-1500:]
    assert "floor breach" in err, err[-1500:]
    # ⛔ …and NO coverage figure. The sibling test's name used to claim the line
    # is printed "on every run" while its own docstring recorded this path as the
    # exception — a documented exception with nothing holding it. A coverage
    # figure computed over a face set that just breached a floor would be a
    # measurement of a broken scan, printed in the reassuring shape.
    assert "defaults-tier coverage" not in err, err[-1500:]
    assert names_its_own_floor in err, (
        f"this case is named for the floor that says {names_its_own_floor!r}, "
        "but a different floor answered — the probe is being caught by a "
        f"neighbour and this case no longer guards its own floor:\n{err[-1500:]}")


def test_a_floor_breach_without_ci_is_report_only(monkeypatch):
    """The `--ci` / report-only split, once.

    ⛔ Deliberately NOT a sixth assertion inside the parametrised test above.
    Each `main()` there is a full `run_check()` — demand extraction over every
    rule pack included — so a second call per case doubled the cost of the
    slowest test in the file for a property that belongs to `main()`'s exit
    handling, not to any individual floor. Measured: the parametrisation went
    from ten full runs to five.
    """
    _trip_scan_floor(monkeypatch)
    assert gate.main([]) == gate.EXIT_OK


def test_a_reader_failure_is_still_a_caller_error(monkeypatch, capsys):
    """The other half of the same distinction: an unreadable file genuinely
    means the tool could not do its job, so it keeps rc=2 and `crashed`.

    Separate test rather than a second phase of the one above, because
    monkeypatch only unwinds at teardown — patching the scan there and the
    reader here in one function let the scan floor fire first and mask this.
    """
    real = gate._defaults_artifacts()
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: real[:-1] + [Path(str(real[-1]) + ".gone")])
    assert gate.main(["--ci"]) == gate.EXIT_CALLER_ERROR
    assert "crashed" in capsys.readouterr().err


def test_a_key_whose_value_is_a_mapping_is_still_checked():
    """Intermediate keys count, not just leaves: a misplaced `<base>_critical:`
    that happens to hold a mapping is the same defect in a different shape, and
    a leaves-only walk would step straight over it."""
    keys = gate._defaults_section("defaults:\n  pg_connections_critical:\n    a: 1\n")
    assert "pg_connections_critical" in keys, keys


def test_the_walk_refuses_a_recursive_anchor_instead_of_truncating():
    """⛔ `yaml.safe_load` accepts a self-referential anchor, and a walk without a
    cap recurses until the interpreter stops it. Truncating silently would hand
    back a partial key set that passes the placement check for everything it
    never reached — so the cap RAISES.

    ⛔ The match is anchored on OUR message and the type is checked exactly.
    Measured (blind review, mutation): with `pytest.raises(RuntimeError,
    match="exceeded")`, raising the cap to 10000 — the very edit our own message
    forbids — still passed, because `RecursionError` subclasses `RuntimeError`
    and its text also contains "exceeded". The assertion could not tell "our cap
    fired" from "the interpreter blew the stack".
    """
    with pytest.raises(RuntimeError, match="walk exceeded") as exc:
        gate._defaults_section("defaults: &a\n  x: *a\n")
    assert type(exc.value) is RuntimeError, type(exc.value)


def test_the_walk_leaves_flat_faces_unchanged():
    """Anti-regression for the change itself: on a flat section the path IS the
    key and its depth is 0, so those faces must not have moved at all."""
    chart = gate._CHART_VALUES.read_text(encoding="utf-8")
    chart_defaults = (yaml.safe_load(chart) or {})["thresholdConfig"]["defaults"]
    walked = gate._defaults_section(chart, unwrap_chart=True)
    assert set(walked) == set(chart_defaults)
    assert set(walked.values()) == {0}, walked

    flat = gate._defaults_section("defaults:\n  a: 1\n  b: 2\n")
    assert flat == {"a": 0, "b": 0}, flat


def test_only_the_chart_face_unwraps_thresholdConfig():
    """⛔ `root = doc.get("thresholdConfig") or doc` is a CHART convention, and
    applying it to conf.d artifacts was a hole (blind review): any
    `_defaults.yaml` that grows a top-level `thresholdConfig:` key stopped having
    its real `defaults:` read — the injected `_critical` vanished and the gate
    exited 0.

    A conf.d file has no `thresholdConfig:` in its schema, so for artifacts the
    top level IS the root, and the unwrap is now opt-in.
    """
    doc = ("thresholdConfig:\n  defaults:\n    chart_key: 1\n"
           "defaults:\n  pg_conn_critical: 90\n")
    # artifact reading: the top level IS the root
    assert gate._defaults_section(doc) == {"pg_conn_critical": 0}
    # chart reading of the same bytes: one level down
    assert gate._defaults_section(doc, unwrap_chart=True) == {"chart_key": 0}

    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({}, {"artifact (probe)": gate._defaults_section(doc)}),
    )
    assert [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e], result


# ── the two face classes are BUILT, not declared (#1393) ─────────────────────

def test_a_label_that_looks_like_an_artifact_is_still_a_generator():
    """⛔ The #1393 regression, and it is about the DISCRIMINATOR, not the label.

    Measured on the old code: a generator whose label began `artifact (` was
    filed as an artifact, so its empty (broken) reader was exempt from
    EMPTY-FACE — errors 0 — while `len(generators) == 4` still passed because
    the four old literals were untouched. A `kind` field was measured to
    reproduce it exactly, a hand-written field being as writable as a
    hand-written label. Classes now arrive in separate dicts, so the label is
    decoration: an empty face in the generator dict is an error whatever it is
    called.
    """
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({f"{gate._ARTIFACT_FACE_PREFIX}5th generator)": {}}, {}),
    )
    assert [e for e in result["errors"] if "EMPTY-FACE" in e], result


def test_placement_runs_on_artifact_faces_too():
    """Both classes go through the same `_report_placement`. Splitting the loop
    in two is exactly how one class would quietly stop being checked."""
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({}, {"artifact (probe)": _flat(
            "pg_connections_critical", 'oracle_ts{env="prod"}')}),
    )
    assert [e for e in result["errors"] if "CRITICAL-IN-DEFAULTS" in e], result
    assert [e for e in result["errors"] if "DIMENSIONAL-IN-DEFAULTS" in e], result


def test_an_empty_artifact_face_is_legal():
    """The asymmetry, from the other side: two shipped recipe roots genuinely
    declare no `defaults:`, and applying the generator rule to artifacts turned
    this gate red on arrival for both of them."""
    result = gate.run_check(
        demand=set(), supply=set(), deferred=set(), known_unwired={},
        defaults_faces=({}, {"artifact (probe)": {}}),
    )
    assert [e for e in result["errors"] if "EMPTY-FACE" in e] == [], result


# ── per-class and per-root floors (#1392) ────────────────────────────────────

@pytest.mark.parametrize("root", sorted(gate._SHIPPED_CONFD_ROOTS))
def test_every_shipped_root_has_its_own_floor(monkeypatch, root):
    """⛔ Clear each shipped root in turn and this must go red for that root.

    This is the floor's own emptiness test, applied per source rather than once
    globally — measured before it existed: removing EVERY non-`tests/` artifact
    leaves 12 fixture survivors, clears both file floors, and `run_check`
    reports nothing at all. Fixtures outnumber the shipped files, so a single
    global number can always be held up by the wrong class.
    """
    tracked = gate._tracked_defaults_artifacts()
    kept = [rel for rel in tracked
            if not (rel == root or rel.startswith(root + "/"))]
    assert len(kept) < len(tracked), f"{root} contributed nothing to remove"
    # the file floors must NOT be what fires — that is the whole point
    assert len(kept) >= gate._DEFAULTS_ARTIFACT_FLOOR, kept

    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / rel for rel in kept])
    with pytest.raises(RuntimeError, match="shipped conf.d root") as exc:
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})
    assert root in str(exc.value), exc.value


def test_exempting_a_shipped_artifact_is_not_silent(monkeypatch):
    """The exemption table is the OTHER way a shipped root stops being checked,
    and it used to be completely silent: the read floor counts files repo-wide,
    so exempting the two exporter artifacts left 15 survivors and nothing said
    the shipped tree had gone dark."""
    tracked = gate._tracked_defaults_artifacts()
    root = "components/threshold-exporter/config/conf.d"
    exempt = {rel: "x" * 40 for rel in tracked
              if rel == root or rel.startswith(root + "/")}
    assert exempt, root

    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_EXEMPT", exempt)
    with pytest.raises(RuntimeError, match="shipped conf.d root"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})










def _placement_message_for_a_hashed_fixture() -> str:
    """The UPSTREAM message — the one that tells a reader to add an exemption.

    Driven through the real `_report_placement` rather than reconstructed here,
    because a copy of a message proves things about the copy. It is reached by
    a `_critical` key sitting BELOW the top level — the third of the three
    placement branches, and the only one whose remedy paragraph names the
    exemption table.
    """
    errors: list[str] = []
    gate._report_placement("artifact (probe/conf.d/_defaults.yaml)",
                           _nested(db={"pg_connections_critical": 1}), errors)
    named = [e for e in errors if "_DEFAULTS_ARTIFACT_EXEMPT" in e]
    assert len(named) == 1, errors
    return named[0]


def test_an_exemption_that_empties_a_root_says_so_in_the_message(monkeypatch):
    """⛔ Whichever floor answers, it has to name the thing the reader just did.

    Exempting a file removes it from the read set, so exempting the only
    `_defaults*` file in a conf.d root empties that root — and the floor that
    then speaks used to list three causes (moved, deleted, renamed) of which
    none was true, never mentioning the exemption table at all (#1434).

    ⛔ THE CLASS, not the instance the ticket named. Walking every artifact and
    exempting it alone: two different floors answer depending on which root the
    file is in, and BOTH messages were silent about exemptions. So both are
    asserted here, and the victims are derived rather than written down.

    ⚠️ Why it matters while `_DEFAULTS_ARTIFACT_EXEMPT` is empty: the only edit
    that turns the first message green from there is filing the tree under
    _DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS, which is a claim about files, and
    the file is still present. Measured: that state is GREEN. A message that
    leaves an untrue table entry as the cheapest exit is a defect regardless of
    how many entries the table has today.
    """
    tracked = gate._tracked_defaults_artifacts()
    by_root: dict[str, list[str]] = {}
    for rel in tracked:
        # ⛔ The same `if root:` the gate's own `_bucket_by_root` has, and this
        # test shipped without it. An artifact outside every conf.d tree is a
        # state the gate's comment calls TOLERATED, and it made the line below
        # die on a bare `TypeError` from `sorted()` comparing None to str —
        # a test contradicting the prose of the very commit that wrote it.
        root = gate.conf_d_root(rel)
        if root:
            by_root.setdefault(root, []).append(rel)

    solo_unshipped = [files[0] for root, files in sorted(by_root.items())
                      if len(files) == 1 and root not in gate._SHIPPED_CONFD_ROOTS
                      and root not in gate._DEFAULTS_ROOTS_MAY_BE_EMPTY]
    shipped_any = [files[0] for root, files in sorted(by_root.items())
                   if root in gate._SHIPPED_CONFD_ROOTS]
    assert solo_unshipped and shipped_any, sorted(by_root)

    # ⛔ NAMING THE TABLE IS NOT ENOUGH, and a first version asserted only that.
    # `_DEFAULTS_ARTIFACT_EXEMPT` occurs many times over in the gate — in code,
    # in comments and in other messages — so that substring survives deleting
    # the remedy paragraphs outright; measured, both were removable with the
    # suite green. (An earlier wording put a COUNT here, "appears ten times",
    # which was 13 by then. The argument never needed the total: what matters
    # is that the name is not unique to the paragraph being guarded.)
    #
    # So an anchor from each remedy is required, and comparison is CASEFOLDED
    # because the anchors are bare substrings: reordering three remedies so a
    # different one leads capitalises its first word, which is a pure
    # readability edit that this test used to reject while printing the
    # remedies it claimed were missing.
    #
    # ⛔ ONE wording for one remedy, and this list is what enforces it —
    # across ALL THREE messages that send a reader to the exemption table, not
    # the two that happened to be easy to reach. Blind review deleted the
    # third one's remedies outright with the suite green, and it is the
    # UPSTREAM one: the message that tells you to add the exemption in the
    # first place, i.e. the one a reader meets first.
    # ⛔ The anchors run PAST the point the three messages can diverge. The
    # first version stopped at "move the exempted file", one word short of
    # where two of them said "out of the conf.d tree" and the third said "out
    # of the tree" — mutation changed one of them to "orchard" and the suite
    # stayed green, so the rule this list is supposed to enforce was not
    # enforced at the only place it had already been broken.
    exits = ("sibling `_defaults*` file",
             "move the exempted file out of the conf.d tree",
             "do not exempt it")

    def _missing_exits(message: str) -> list[str]:
        low = message.casefold()
        return [e for e in exits if e.casefold() not in low]

    for victim in (solo_unshipped[0], shipped_any[0]):
        monkeypatch.setitem(gate._DEFAULTS_ARTIFACT_EXEMPT, victim,
                            "probe: " + "x" * 40)
        with pytest.raises(gate._GateViolation) as exc:
            gate._defaults_faces()
        msg = str(exc.value)
        assert "_DEFAULTS_ARTIFACT_EXEMPT" in msg, (
            f"exempting {victim} produced a message that never mentions the "
            f"table the reader just edited: {msg}")
        assert not _missing_exits(msg), (
            f"exempting {victim} produced a message missing these remedies: "
            f"{_missing_exits(msg)}. If you REWORDED one, update the `exits` "
            "tuple a few lines above this assertion in the same edit — the "
            "anchors exist so the three messages cannot drift apart, not to "
            f"freeze the prose. Message was:\n{msg}")
        monkeypatch.undo()

    # …and the THIRD message, the upstream pointer that sends a reader to the
    # table. It is reached by placement rather than by a floor, so it is driven
    # through `_report_placement` directly rather than through a victim.
    placement = _placement_message_for_a_hashed_fixture()
    assert not _missing_exits(placement), (
        "the placement message that TELLS people to add an exemption offers "
        f"different remedies from the floors they will then hit: "
        f"{_missing_exits(placement)}\n{placement}")

    # ⛔ …and the remedy half must NOT be printed when there is nothing to
    # blame: with the table empty these messages spent their longest paragraph
    # on an entry the reader never added. ⛔ BOTH arms, because the conditional
    # was applied to both and only one of them had this control — measured, the
    # shipped-root half could be put back to unconditional with the suite
    # green, i.e. exactly the "fixed the cited instance, not the class" shape
    # this file keeps recording.
    monkeypatch.setattr(gate, "_DEFAULTS_ARTIFACT_EXEMPT", {})
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_contributes({}, tracked_roots=set())
    assert "_DEFAULTS_ARTIFACT_EXEMPT" not in str(exc.value), (
        "with the exemption table empty, the `missing` message still leads "
        f"with the exemption paragraph:\n{exc.value}")

    starved = {root: [] for root in gate._SHIPPED_CONFD_ROOTS}
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_shipped_roots_intact(starved)
    assert "_DEFAULTS_ARTIFACT_EXEMPT" not in str(exc.value), (
        "with the exemption table empty, the shipped-root floor still prints "
        f"the exemption paragraph:\n{exc.value}")


def test_the_key_floors_are_per_class_not_one_global_number():
    """⛔ The first draft of this floor was a single total, and a single total
    cannot see one class collapse: generators contribute 102 of the 172 keys and
    hold any global number up on their own.
    """
    plenty = _flat(*(f"g{i}" for i in range(200)))
    # artifacts at zero, generators enormous → the artifact floor must still fire
    with pytest.raises(RuntimeError, match="ARTIFACT faces yielded"):
        gate._assert_keys_floor({"a generator": plenty}, {"an artifact": {}})
    # …and symmetrically
    with pytest.raises(RuntimeError, match="GENERATOR faces yielded"):
        gate._assert_keys_floor({"a generator": _flat("one")}, {"an artifact": plenty})


def _artifact_groups(artifacts: dict[str, dict[str, int]]) -> dict[str, int]:
    """{conf.d root: key count} — the unit that 'a whole group vanished' means."""
    groups: dict[str, int] = {}
    for label, keys in artifacts.items():
        rel = label[len(gate._ARTIFACT_FACE_PREFIX):-1]
        root = rel.rsplit("/conf.d", 1)[0] + "/conf.d" if "/conf.d" in rel else rel
        groups[root] = groups.get(root, 0) + len(keys)
    return groups


def _counted_artifact_groups(
        artifacts: dict[str, dict[str, int]]) -> dict[str, int]:
    """`_artifact_groups` over the faces the ARTIFACT KEY FLOOR actually counts.

    ⛔ The filter is the gate's own `_artifact_floor_faces` (#1544), not a
    second copy of the exclusion set spelled out here. Every bracket and
    headroom figure below is a statement about that floor's arithmetic, and a
    local copy would go on answering after the gate's denominator changed —
    the same "a probe that models the thing it measures" failure
    `test_the_probe_and_the_gate_bucket_through_the_same_implementation`
    exists for one layer down.

    ⚠️ NOT a replacement for `_artifact_groups`, which still has a caller:
    `test_the_two_artifact_groupings_are_one_partition` compares the two
    spellings of "which root is this" over EVERY scanned artifact, and
    narrowing it to the counted ones would stop it seeing a divergence under
    the excluded trees.
    """
    return _artifact_groups(gate._artifact_floor_faces(artifacts))


def test_the_two_artifact_groupings_are_one_partition():
    """⛔ `_artifact_groups` (rsplit on `/conf.d`) and `conf_d_root` must agree.

    The gate's nine-row note says these two spellings "produce the IDENTICAL
    partition — measured, all 17 tracked artifacts agree", and nothing computed
    it. That is the same shape as every other number this file has had to
    correct: a measurement taken once, written down, and left.

    ⛔ This is NOT scraping a digit out of prose (which this file bans, and for
    good reason). It compares two LIVE derivations against each other. Nothing
    is being verified against a comment, and no hand-maintained number is
    involved — the two functions either partition today's artifacts the same way
    or they do not.

    Divergence is a thing the file wants to hear about anyway: the bracket test
    brackets `_artifact_groups`, the fifth floor works in `conf_d_root`, and if
    they ever split, those two would be bracketing different things while
    appearing to agree. A red here is signal, not maintenance.
    """
    _generators, artifacts = gate._defaults_faces()
    by_bracket_test = _artifact_groups(artifacts)

    by_gate: dict[str, int] = {}
    for label, keys in artifacts.items():
        root = gate.conf_d_root(label[len(gate._ARTIFACT_FACE_PREFIX):-1])
        assert root, label      # every artifact really does sit under a conf.d
        by_gate[root] = by_gate.get(root, 0) + len(keys)

    assert by_bracket_test == by_gate, {
        "only via rsplit": sorted(set(by_bracket_test) - set(by_gate)),
        "only via conf_d_root": sorted(set(by_gate) - set(by_bracket_test)),
        "different key counts": {
            r: (by_bracket_test.get(r), by_gate.get(r))
            for r in set(by_bracket_test) | set(by_gate)
            if by_bracket_test.get(r) != by_gate.get(r)},
    }
    # …and the comparison is non-vacuous: two empty dicts are also equal, and
    # the partition has to be coarser than "one group per artifact" or the two
    # spellings could not disagree even in principle.
    assert 1 < len(by_gate) < len(artifacts), (len(by_gate), len(artifacts))


# ── the exclusion set needs an owner, and the GATE has to say so ─────────────

def _the_excluded_set_with_one_live_root_dropped() -> frozenset[str]:
    """The real exclusion set, minus one root that is genuinely in it TODAY.

    ⛔ DERIVED, and it names no fixture. The first version of the `set-narrowed`
    tamper hardcoded `tests/e2e-bench/fixture/synthetic-v2/conf.d`, which put
    back — one layer over — the very coupling this ticket removed from the
    production constant. Measured on a LEGITIMATE retirement of that fixture
    (`git rm -r` plus its removal from `_ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS`
    and `_DEFAULTS_CONFD_ROOTS`): `--ci` rc=0, so pre-commit passes, and pytest
    rc=1 — a CI-only red. And the red was worse than a stale number: the
    hardcoded name is no longer pinned after a retirement, so the tamper landed
    on clause 3 (`not pinned`) instead of the clause 5 it is named for, and the
    failure output said only that one message substring was not in another.
    Nothing told the maintainer their retirement had invalidated the case.

    ⛔ WHY THIS SHAPE CAN ONLY REACH CLAUSE 5, which is the property the
    hardcoded one lost. The result is a strict SUBSET of the live set, and
    clauses 1-4 iterate over the tampered set alone. Every member of a subset
    of the live set is a member of the live set, and the live set satisfies all
    four clauses on every green run — that is what `--ci` rc=0 means today — so
    no subset of it can trip them. Clause 5 is the only one that reads anything
    else: it compares the roots SCANNED under the tree against the set, and the
    dropped root is scanned (it was taken from the scan) and under the tree
    (clause 2 holds for it on the green run), so it lands in `unlisted`.

    ⚠️ The intersection with the scan is load-bearing, not defensive: a set
    member that is pinned but contributes no artifact is absent from
    `scanned_under_tree`, so dropping THAT one would leave clause 5 silent and
    the case would fail with `DID NOT RAISE`.
    """
    _generators, artifacts = gate._defaults_faces()
    scanned = {gate._artifact_root_of_face(face) for face in artifacts}
    live = sorted(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS & scanned)
    assert live, (
        "no root is BOTH excluded from the artifact key floor and present in "
        "today's scan, so there is nothing to narrow and this case cannot say "
        "anything about clause 5.\n"
        "  If the whole benchmark tree has been retired, that is the real "
        "finding: `_ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS`, "
        "`_ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE` and this case should be retired "
        "together, and `_DEFAULTS_ARTIFACT_KEYS_FLOOR` re-measured against the "
        "scan-wide total it goes back to.\n"
        f"  excluded={sorted(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS)}\n"
        f"  scanned under the tree="
        f"{sorted(r for r in scanned if r and r.startswith(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE))}")
    return frozenset(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS - {live[0]})



# (tamper, fragment of the message it must produce). ⛔ Each case is a DIFFERENT
# clause of `_assert_excluded_roots_still_have_an_owner`; a single case would
# leave the other four free to be deleted, which is exactly how this set shipped
# for a round with no invariant at all.
# ⚠️ TWO OF THE FOUR STILL HOLD A PATH LITERAL, and that is the correct split,
# not an oversight. `shipped` and `set-narrowed` need their root to BE something
# in a live table (shipped / excluded-and-scanned), so a literal there rots the
# day that tree is retired — both are derived. `unrelated-fixture` and
# `unpinned` need the opposite: a path that is NOT under the tree, and one that
# is NOT pinned. Neither has to exist on disk or in any table for its clause to
# fire, so retiring the tree they happen to name changes nothing about them.
# ⛔ Do not "finish the job" by deriving these two — there is nothing live to
# derive them from, and inventing one would make them depend on state their
# assertions do not use.
_EXCLUSION_TAMPERS = [
    # (a) from the round-1 review: a SHIPPED root, which used to be free.
    #     ⛔ Taken from the live table for the same reason `set-narrowed` is
    #     derived: a hardcoded shipped root goes stale the day that tree is
    #     retired, and the case would then slide onto clause 2 with a message
    #     about the bench tree. `sorted()[0]` only needs SOME shipped root to
    #     exist, which `_SHIPPED_CONFD_ROOTS` being non-empty already is.
    ("shipped", lambda: frozenset(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS
                                  | {sorted(gate._SHIPPED_CONFD_ROOTS)[0]}),
     "is SHIPPED"),
    # (b) from the round-1 review: an unrelated fixture root. The gate's stdout
    #     was measured BYTE-IDENTICAL on this one before the invariant existed.
    ("unrelated-fixture",
     lambda: frozenset(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS
                       | {"tests/golden/fixtures/mixed-mode/conf.d"}),
     "does not live under _ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE"),
    # a root nobody pins: the fifth floor never looks at it, so excluding it
    # leaves it watched by nothing.
    ("unpinned", lambda: frozenset(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS
                                   | {"tests/e2e-bench/fixture/customer-anon"}),
     "not pinned in _DEFAULTS_CONFD_ROOTS"),
    # the set shrinks: the tree and the set stop agreeing. ⛔ DERIVED — see
    # `_the_excluded_set_with_one_live_root_dropped` for why naming a fixture
    # here re-created, in the suite, the coupling #1544 removed from the gate.
    ("set-narrowed", _the_excluded_set_with_one_live_root_dropped,
     "but are NOT in _ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS"),
]


@pytest.mark.parametrize("name,tamper,expected", _EXCLUSION_TAMPERS,
                         ids=[c[0] for c in _EXCLUSION_TAMPERS])
def test_a_root_leaves_the_key_floors_denominator_only_with_another_owner(
        monkeypatch, name, tamper, expected):
    """Every clause of the exclusion invariant, through the real entry point.

    ⛔ Through `_defaults_faces()` and not by calling the assertion directly:
    the invariant is only worth anything if the floor actually reaches it, and
    a direct call would keep passing after the call site was deleted. The
    `may-be-empty` clause has no case here ON PURPOSE, and the reason is
    written out in that clause's own disclosure rather than summarised here:
    NEITHER way of reaching it from the constants gets there, and the two are
    stopped by two different mechanisms (clause 1 on one path,
    `_assert_every_root_contributes` on the other). ⛔ Not because the message
    is unproducible — it is, by adding a new zero-key fixture root under the
    tree, which is a change to the repo's contents and outside what this table
    tampers with. That clause is a guard against
    a future combination, and this comment is its disclosure rather than a test
    that cannot be written honestly today.
    """
    monkeypatch.setattr(gate, "_ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS", tamper())
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    assert expected in str(exc.value), (name, str(exc.value))


def test_the_exclusion_invariant_fires_on_the_ci_path_not_only_in_the_suite(
        monkeypatch, capsys):
    """⛔ THE POINT OF THE WHOLE CLAUSE SET, and the reason it is not a test.

    `.pre-commit-config.yaml` runs this module as
    `check_threshold_reachability.py --ci`. It does NOT run pytest. So an
    invariant that lives only in this file is an invariant a local commit never
    meets — measured on the build before this one: adding an unrelated 2-key
    fixture root to the exclusion set left `--ci` at rc=0 with stdout
    byte-identical to a clean run, and the single red was one hardcoded pair in
    `test_the_headroom_note_states_both_directions_and_both_are_current`, i.e.
    a CI-only signal for a change that reaches `main` through a green local
    commit.

    This asserts the exit code and the message on the same path the hook takes.
    """
    monkeypatch.setattr(
        gate, "_ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS",
        frozenset(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS
                  | {"tests/golden/fixtures/mixed-mode/conf.d"}))
    rc = gate.main(["--ci"])
    out = capsys.readouterr()
    blob = out.out + out.err
    assert rc == 1, (rc, blob[-2000:])
    assert "_ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE" in blob, blob[-2000:]
    # …and non-vacuously: the same call on the untampered set is rc=0, so the
    # assertion above is about the tamper and not about `--ci` being red today.
    monkeypatch.undo()
    capsys.readouterr()
    assert gate.main(["--ci"]) == 0


def test_the_excluded_tree_and_the_excluded_set_are_both_load_bearing():
    """Neither spelling may be decoration — the invariant reads BOTH.

    ⛔ Mutation is what this is for. A clause set that only walked the set would
    leave `_ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE` free to be edited to anything at
    all, and a check that only compared against the tree would let the set be
    emptied. Both are asserted as REACHED, by making each one wrong on its own.
    """
    _generators, artifacts = gate._defaults_faces()
    assert gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS, "nothing is excluded"
    assert all(r.startswith(gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_TREE + "/")
               for r in gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS)
    # the real pair is accepted…
    gate._assert_excluded_roots_still_have_an_owner(artifacts)


# The group both end-to-end "a whole group vanished" probes remove. ⛔ ONE
# spelling: they assert two different things about the same input (which floor
# is heard first, and that a breach is a violation rather than a crash), and a
# victim that drifted between them would leave one of the two measuring a
# scenario nobody chose. Kept as a path PREFIX, the shape both probes filter
# with — `tests/golden/fixtures/full-l0-l3/conf.d` is the conf.d root inside it.
_VANISHED_GROUP = "tests/golden/fixtures/full-l0-l3/"


def test_the_artifact_key_floor_backstops_a_vanished_group(monkeypatch):
    """End-to-end, on the real tree, with the FILE count still above both file
    floors — which is the case those two floors structurally cannot see.

    ⛔ BACKSTOP, and the name used to say `sees_a_group_stop_yielding` (#1434).
    It does not see that in production: the assertions below meet the fifth
    floor first and have to stand it down before this floor can speak at all.
    Keeping the old name would have gone on advertising an ownership this floor
    has not had since #1411 — the same claim the bracket test's lower bound was
    written from. What this floor owns is asserted separately, in
    `test_the_artifact_key_floor_owns_erosion_that_empties_no_root`; this one
    keeps the older behaviour from rotting away behind the newer floor.

    ⛔ THE VICTIM GROUP IS NOT `tests/e2e-bench/` ANY MORE (#1544). Those two
    roots left the key floor's denominator, so removing them now moves this
    floor's total by exactly zero — the probe would have gone on passing on the
    FIFTH floor's message alone while the branch it is named for was never
    reached, which is the precise defect `_trip_artifact_key_floor`'s comment
    records having had to fix once already. The replacement is the largest
    group still IN the denominator that is not shipped: `full-l0-l3`, 13 keys
    of the counted 59, taking the total to 46 against a floor of 47.

    ⚠️ That leaves ONE key of margin, on purpose and not by luck — 47 is the
    lowest floor at which dropping the biggest counted non-shipped group still
    speaks, and the bracket test's message is what tells you to raise it if the
    margin ever goes negative. `_VANISHED_GROUP` is shared with
    `_trip_artifact_key_floor` so the two probes cannot drift onto different
    victims."""
    tracked = gate._tracked_defaults_artifacts()
    kept = [rel for rel in tracked if not rel.startswith(_VANISHED_GROUP)]
    assert len(kept) >= gate._DEFAULTS_ARTIFACT_FLOOR, kept
    assert len(kept) < len(tracked), "the victim group is not in the scan"

    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / rel for rel in kept])
    # ⛔ The fifth floor (#1411) fires FIRST on this input — it names the
    # emptied root outright, which is strictly better for the reader. But
    # letting that stand here would silently retire this test: the key floor's
    # ability to see a group stop yielding would no longer be exercised by
    # anything, and the next person to touch that floor would get a green suite.
    # So this asserts BOTH, in the order the reader meets them.
    with pytest.raises(gate._GateViolation, match="contributed no artifact"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})

    # …and with the newer floor stood down, the key floor still catches it alone.
    monkeypatch.setattr(gate, "_assert_every_root_contributes", lambda _b: None)
    with pytest.raises(RuntimeError, match="ARTIFACT faces yielded"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})


def _erosion_plan(need: int) -> dict[str, int]:
    """{artifact: keys to drop} removing `need` keys with no OTHER floor firing.

    Two constraints, and the first draft of this helper had only one — the
    plan started at the biggest root, which is a SHIPPED one, and the per-root
    floor claimed the breach at 8 of its 15 keys. That failure is the shape
    this whole test is about, arriving one layer down: a measurement of what
    floor X owns is worthless if floor Y answers first.

      * no conf.d root may reach zero      → the fifth floor would answer;
      * shipped roots are left alone       → the fourth floor would answer;
      * roots outside the key floor's own denominator are left alone
        (`gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS`, #1544) → NOBODY would
        answer: keys taken from there do not move the total this floor
        compares, so the plan would report a supply it did not deliver and the
        caller's `pytest.raises` would fail with the mutation blamed. Before
        the exclusion existed this constraint was vacuous and the plan drew
        from `synthetic-v2` routinely.

    ⛔ It used to return a shortfall as well, so the caller could tell "the
    mutation did not actually happen" from "it happened and nothing noticed".
    That is the right question and it already has an answer one line down —
    `sum(plan.values()) == band + 1` — so the shortfall was a second copy of
    one check, and mutation confirmed it: `return plan, 0` was green. Deleted
    rather than given a control of its own.

    ⚠️ DISCLOSED, because the same mutation pass found it and the answer is
    "no control, on purpose": the `- 1` below (keep one key alive per root) is
    slack at today's sizes, not a load-bearing constraint — the largest
    non-shipped root carries more keys than the plan ever asks for, so
    removing the `- 1` changes nothing today. It becomes load-bearing if the
    band ever approaches the biggest root, and what would notice then is the
    caller's `pytest.raises(match=...)`: an emptied root is reported by the
    fifth floor, whose message does not match. That is a real tripwire, so no
    further guard is added here.
    """
    real = gate._read_artifact_keys
    per_root: dict[str, list[tuple[str, int]]] = {}
    for path in gate._defaults_artifacts():
        rel = path.relative_to(gate.PROJECT_ROOT).as_posix()
        root = gate.conf_d_root(rel)
        # ⛔ `None` — an artifact under no conf.d tree — is skipped, the same
        # way `_bucket_by_root` skips it. Without this it becomes a bucket of
        # its own and the plan erodes keys from a thing that is not a root, so
        # the measurement stops being about the floor it names.
        # ⚠️ NOT because `sorted()` would raise: the sort below passes an
        # explicit int `key=`, so it never compares the keys (verified). The
        # one `TypeError` this file records — inside
        # `test_an_exemption_that_empties_a_root_says_so_in_the_message` — came
        # from a bare `sorted(by_root.items())` with no `key=`: the same shape
        # with a different consequence, which is why the guard is worth having
        # in both and the reason is worth writing down in neither's shorthand.
        # ⛔ An earlier wording here pointed at "the tenant-stub test", which
        # was a parametrize case in a test deleted one commit earlier — the
        # pointer was dangling the moment it was written.
        if (root is None or root in gate._SHIPPED_CONFD_ROOTS
                or root in gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS):
            continue
        per_root.setdefault(root, []).append((rel, len(real(path))))

    plan, left = {}, need
    for _root, files in sorted(per_root.items(),
                               key=lambda kv: -sum(n for _r, n in kv[1])):
        droppable = sum(n for _r, n in files) - 1
        for rel, n in files:
            if left <= 0 or droppable <= 0:
                break
            take = min(left, droppable, n)
            if take:
                plan[rel] = take
                left -= take
                droppable -= take
    return plan


def _read_with_erosion(monkeypatch, plan: dict[str, int]) -> None:
    real = gate._read_artifact_keys

    def reader(path, _real=real):
        keys = _real(path)
        drop = plan.get(path.relative_to(gate.PROJECT_ROOT).as_posix(), 0)
        return dict(list(keys.items())[: len(keys) - drop]) if drop else keys

    monkeypatch.setattr(gate, "_read_artifact_keys", reader)


def test_the_artifact_key_floor_owns_erosion_that_empties_no_root(monkeypatch):
    """⛔ What the artifact key floor is FOR, asserted instead of described.

    The bracket below is `floor > total − biggest group`, and the sentence that
    used to justify it — "it must still fire when the biggest conf.d root stops
    yielding" — names a scenario this floor never reaches (#1434). Two floors
    run ahead of it in `_defaults_faces` and between them catch every single
    root going to zero, so a reader calibrating this floor was reasoning from a
    case that belongs to someone else.

    The same inequality read in a REACHABLE shape: erosion the size of a whole
    root, spread so that nothing empties, must still trip it. That is the claim,
    and these are its two halves. Everything is re-derived from the constants —
    no number from that comment is copied here.
    """
    _generators, artifacts = gate._defaults_faces()
    # ⛔ The floor's own denominator (#1544): `band` is what this floor is blind
    # to, so it has to be measured in the keys this floor adds up. Taken from
    # the scan-wide total instead, `band` comes out 25 on this tree and step 3
    # would then be asking the non-shipped roots for 26 keys of erosion — more
    # than they hold — turning a statement about the floor into a red about the
    # plan.
    groups = _counted_artifact_groups(artifacts)
    total = sum(groups.values())
    band = total - gate._DEFAULTS_ARTIFACT_KEYS_FLOOR
    biggest_root = max(groups, key=lambda root: groups[root])

    # 1. The scenario the old justification named is reported by an EARLIER
    #    floor, so this one is never reached. Through the real entry point,
    #    because calling the floors one at a time is what hides call order.
    real = gate._read_artifact_keys
    monkeypatch.setattr(gate, "_read_artifact_keys", lambda path, _r=real: (
        {} if gate.conf_d_root(path.relative_to(gate.PROJECT_ROOT).as_posix())
        == biggest_root else _r(path)))
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    reported = str(exc.value)
    assert "ARTIFACT faces yielded" not in reported, (
        "the artifact key floor reported a whole root going to zero — the two "
        "floors that used to run ahead of it no longer do, so the bracket's "
        f"lower bound can go back to naming that scenario: {reported}")
    # ⛔ …and WHICH floor answered, not merely which one did not. Asserting only
    # the negative left the table above free to name the wrong owner: with
    # `_assert_shipped_roots_intact` stubbed out the fifth floor answers
    # instead, the table's first row goes stale, and this test stays green.
    #
    # ⚠️ IDENTITY, NOT A PROSE PREFIX. A first version asserted
    # `reported.startswith("shipped conf.d root")`, and blind review measured
    # two false reds on it: prefixing the message with "The " reddens it while
    # the SAME floor answered, and so does a golden fixture legitimately
    # growing past the shipped root's key count (which moves "the biggest
    # group" to another tree — a real change of owner, but the message that
    # comes back is then correct and the assertion still reads as a bug).
    # The cheapest way to green either was to weaken this very line. Matching
    # the floor's own stable phrase and naming the alternative keeps the claim
    # about which floor answered without pinning the sentence it says it in.
    assert "conf.d root" in reported and "artifact(s) /" in reported, (
        "the table beside the key floors says the biggest conf.d root going "
        "to zero is reported by the SHIPPED-ROOT floor (the one that prints "
        "an artifact/key pair). Something else answered first — most likely "
        "the biggest group is no longer a shipped tree — so update that row "
        f"rather than this assertion: {reported}")
    monkeypatch.undo()

    # 2. Inside the blind band this floor is silent. Asserted on the floor's own
    #    arithmetic rather than through `_defaults_faces`, so that a SIXTH floor
    #    catching this some day is an improvement here and not a red.
    gate._assert_keys_floor(_generators, {"an artifact": _flat(
        *(f"k{i}" for i in range(total - band)))})

    # 3. One key further and it speaks — on the real tree, with every root still
    #    carrying keys, which is the shape floors 1-2 (files) and 4-5 (a root at
    #    zero) all structurally miss.
    plan = _erosion_plan(band + 1)
    supply = sum(plan.values())
    assert supply == band + 1, (
        f"the plan removes {supply} keys, not {band + 1} — the non-shipped "
        "roots can no longer supply that much erosion without one of them "
        "emptying, so this measurement would be about a different floor than "
        "the one it names.\n"
        f"  This happens when keys grow on the SHIPPED side, which the bracket "
        f"test cannot see (its bound moves with the biggest group, not with "
        f"the split). RAISE `_DEFAULTS_ARTIFACT_KEYS_FLOOR` to at least "
        f"{total - supply + 1}, which brings the band back within what the "
        f"non-shipped roots can supply, and say in the commit message what "
        f"grew.\n  plan={plan}")
    _read_with_erosion(monkeypatch, plan)
    with pytest.raises(gate._GateViolation, match="ARTIFACT faces yielded"):
        gate._defaults_faces()
    monkeypatch.undo()

    # ⛔ AND THE BAND IS NOT RE-ASSERTED HERE. A line reading
    # `assert band < biggest` stood at this spot and blind review killed it two
    # ways: it is the bracket test's lower bound restated (`band < biggest` is
    # `total - FLOOR < biggest` is `total - biggest < FLOOR`, equivalent for
    # every possible floor value, so the two can only ever go red together),
    # and its message was a bare tuple, so a legitimate key addition produced
    # three reds of which this one could not say what to do. Deleting a test
    # for being true-by-construction and then writing one in the same commit is
    # the shape this file keeps having to correct. The inequality has an owner:
    # test_each_key_floor_is_bracketed_by_what_it_has_to_catch, whose message
    # explains which direction to move the floor.


def test_each_key_floor_is_bracketed_by_what_it_has_to_catch():
    """⛔ The floors need an UPPER and a LOWER bound, and "greater than zero" is
    neither.

    Measured (blind review, mutation): with only `>= floor` and `floor > 0`
    asserted, setting the generator floor to 2, to 51, or to today's exact 102
    all left the suite green — and the name of the previous test said "with
    room" while its body could not see any of them. So the bracket is derived
    from the two things the floor is FOR:

      lower — its blind band must stay narrower than the biggest single group,
              i.e. floor > (total − biggest group);
      upper — it must have room left, i.e. floor < today's total. A floor at
              exactly today's value goes red on the next legitimate deletion,
              which trains people to edit floors.

    ⛔ THE LOWER BOUND'S SCENARIO IS NOT THE SAME ON BOTH HALVES, and this
    docstring used to give one sentence for both: "it must still fire when the
    biggest single producer / conf.d root stops yielding" (#1434).

      GENERATORS — true as written, and the floor is the FIRST responder:
        EMPTY-FACE lives in `run_check`, i.e. after `_defaults_faces` has
        already run this floor. Measured: `init` (51 keys) losing 23 trips it.

      ARTIFACTS — the arithmetic still holds, but the SCENARIO is unreachable
        here: `_assert_shipped_roots_intact` and `_assert_every_root_contributes`
        both run first and between them catch every single root reaching zero,
        so this floor never reports that. Measured through `_defaults_faces`:
        zeroing the biggest root is reported by the shipped-root floor.
        What the same inequality does say, in a shape this floor CAN reach, is
        that erosion the size of a whole root — spread around so that no root
        empties — still trips it. That is the reachable reading, and it is
        pinned by, rather than left as prose in front of,
        `test_the_artifact_key_floor_owns_erosion_that_empties_no_root`.

    ⛔ AND THE ARTIFACT TOTAL IS THE FLOOR'S OWN DENOMINATOR, not the scan's
    (#1544). `_counted_artifact_groups` drops the roots in
    `gate._ARTIFACT_KEYS_FLOOR_EXCLUDED_ROOTS`, because bracketing a floor
    against keys that floor never adds up is bracketing nothing: measured on
    this tree, the scan-wide total (72, biggest root 19) puts the lower bound
    at 53 while the floor arithmetic runs on 59, so every legal floor would
    have read as too low.
    """
    generators, artifacts = gate._defaults_faces()

    g_total = sum(len(v) for v in generators.values())
    g_biggest = max(len(v) for v in generators.values())
    assert g_total - g_biggest < gate._DEFAULTS_GENERATOR_KEYS_FLOOR < g_total, (
        f"generator floor {gate._DEFAULTS_GENERATOR_KEYS_FLOOR} must sit strictly "
        f"between {g_total - g_biggest} (the biggest producer, {g_biggest} keys, "
        f"going silent) and today's {g_total}")

    groups = _counted_artifact_groups(artifacts)
    a_total = sum(groups.values())
    a_biggest = max(groups.values())
    assert a_total - a_biggest < gate._DEFAULTS_ARTIFACT_KEYS_FLOOR < a_total, (
        f"artifact floor {gate._DEFAULTS_ARTIFACT_KEYS_FLOOR} must sit strictly "
        f"between {a_total - a_biggest} and today's {a_total}. The lower bound "
        f"leaves this floor a blind band of "
        f"{a_total - gate._DEFAULTS_ARTIFACT_KEYS_FLOOR} key(s), and it has to "
        f"stay narrower than the biggest conf.d root ({a_biggest} keys) — so "
        "that erosion the size of a whole root still trips it even when it is "
        "spread around and empties nothing. ⛔ NOT because this floor is what "
        "reports a root going silent: two earlier floors do that, and this one "
        "is never reached in that case.\n"
        "  Adding keys raises the lower bound, so a pure addition asks for a "
        "HIGHER floor here. ⚠️ But satisfying THIS bound alone is not enough "
        "and doing only that leaves other tests red with less helpful "
        "messages: the end-to-end probes trip this floor by dropping one "
        "GROUP, so the floor also has to stay above `total − (that group)`. "
        "Move it high enough for both, in one edit, and say what grew.\n"
        f"  groups={groups}")


def test_the_headroom_note_states_both_directions_and_both_are_current():
    """⛔ The HEADROOM note beside the two key floors carries four numbers, and
    nothing used to make any of them go red.

    It went stale exactly as its own text predicted: it said artifacts tolerate
    "+9 keys" upward when the arithmetic of the time gave +8 (70 total − 19 in
    the biggest conf.d root = 51, and 51 + 9 = 60 is not strictly below a floor
    of 60), in a
    paragraph whose opening sentence is "these numbers need maintaining in BOTH
    directions" and whose closing one is "nobody should discover the direction by
    guessing". A comment that names a direction still needs a tripwire on the
    magnitude.

    ⛔ UP and DOWN are different measurements and are asserted separately, in the
    words the note uses, so a future edit cannot quietly swap one for the other:
      UP   — against "biggest group gone"; a pure addition turns the BRACKET
             test red, and the remedy is a HIGHER floor.
      DOWN — against the floor itself; a removal turns the GATE red, and the
             remedy is repairing the producer (or, for a deliberate retirement,
             a LOWER floor).

    ⛔ BOTH ARTIFACT FIGURES ARE MEASURED ON THE FLOOR'S DENOMINATOR (#1544),
    which is why they did not move when the denominator did: total 72 / floor
    60 and total 59 / floor 47 give the same (+6, 12). That equality is the
    evidence the pairing was a re-basing and not a retune — it is NOT a claim
    that the two are interchangeable, and re-deriving these from the scan-wide
    total would give (-7, 25) here.
    """
    generators, artifacts = gate._defaults_faces()
    g_total = sum(len(v) for v in generators.values())
    g_biggest = max(len(v) for v in generators.values())
    groups = _counted_artifact_groups(artifacts)
    a_total, a_biggest = sum(groups.values()), max(groups.values())

    # UP: the largest pure addition that keeps `total - biggest < floor` true.
    up_gen = gate._DEFAULTS_GENERATOR_KEYS_FLOOR - (g_total - g_biggest) - 1
    up_art = gate._DEFAULTS_ARTIFACT_KEYS_FLOOR - (a_total - a_biggest) - 1
    assert (up_art, up_gen) == (6, 28), (
        f"the HEADROOM note beside the key floors says artifacts +6 / generators "
        f"+28 upward; measured {up_art} / {up_gen}. Update the note and this "
        "assertion together, and keep the DIRECTION word beside each number.")

    # DOWN: how many keys may disappear before the floor itself speaks.
    down_gen = g_total - gate._DEFAULTS_GENERATOR_KEYS_FLOOR
    down_art = a_total - gate._DEFAULTS_ARTIFACT_KEYS_FLOOR
    assert (down_art, down_gen) == (12, 22), (
        f"the note says artifacts 12 / generators 22 downward — and the fifth "
        f"floor's own comment repeats the artifact one as '12 keys of slack'; "
        f"measured {down_art} / {down_gen}. Both places move together.")

    # ⛔ and they must not collide. ⚠️ NOT because they once did: at `188cfb17`,
    # the commit that carried both, DOWN was 10 and UP was 8 — never equal.
    #
    # ⛔⛔ AND "+9" IS NOT WHAT WAS MEASURED THERE, it is what was WRITTEN there.
    # An earlier wording of this note said "measured at `188cfb17` … UP was +9",
    # which quietly promoted the stale digit to a measurement in the one
    # paragraph whose entire subject is the gap between a written number and a
    # measured one. Re-derived by replaying that commit (its own tree, its own
    # constants): artifacts total 70, biggest group 19, floor 60, so UP is
    # 60 − (70 − 19) − 1 = 8. The file said 9 and the tree said 8, from the day
    # both numbers were committed. That is a sharper version of the point, not a
    # softer one: the number was never right, and nothing was checking.
    #
    # What the file's own record says the cause of the confusion was: both are
    # called "headroom", they sit 47 lines apart, and the DIRECTION was written
    # down while the MAGNITUDE had no tripwire. This assertion is
    # forward-looking, not historical: if
    # the two ever do land on the same integer, the only thing distinguishing
    # them at a glance disappears, and whichever note is read second inherits
    # the other's number. If that happens, say which is which in both places
    # rather than deleting this line.
    assert up_art != down_art, (up_art, down_art)


def test_each_shipped_root_floor_is_bracketed_too():
    """The same bracket, per root — and here the lower bound is what catches
    `min artifacts` being a floor of one.

    Measured (blind review, mutation): `(2, 15)` → `(1, 0)` and `(2, 15)` →
    `(2, 19)` both left the suite green, because the only test on these numbers
    empties the whole root, which any positive `min_artifacts` catches. Removing
    ONE file from a root — the case the comment beside the table calls out by
    name — had no test at all.
    """
    _generators, artifacts = gate._defaults_faces()
    per_root: dict[str, list[int]] = {r: [] for r in gate._SHIPPED_CONFD_ROOTS}
    for label, keys in artifacts.items():
        rel = label[len(gate._ARTIFACT_FACE_PREFIX):-1]
        for root in per_root:
            if rel == root or rel.startswith(root + "/"):
                per_root[root].append(len(keys))
                break

    for root, (min_artifacts, min_keys, _why) in sorted(gate._SHIPPED_CONFD_ROOTS.items()):
        sizes = per_root[root]
        assert sizes, f"{root} contributes no artifact at all"
        assert 0 < min_artifacts <= len(sizes), (
            f"{root}: min_artifacts {min_artifacts} vs {len(sizes)} file(s) today")
        total, biggest = sum(sizes), max(sizes)
        if total == 0:
            # A root that legitimately carries no keys (the recipe examples).
            # `min_keys` cannot say anything there; the artifact count is its
            # only guard, so require it to be the real one.
            assert min_keys == 0 and min_artifacts == len(sizes), (
                f"{root}: carries no keys, so min_artifacts must pin the file "
                f"count exactly (got {min_artifacts} vs {len(sizes)})")
            continue
        # ⛔ Strictly below today's total, same as the class floors. `<= total`
        # was the first spelling and mutation walked straight through it: setting
        # exporter's min_keys to its exact 19 stayed green, and a floor with zero
        # room goes red on the next legitimate edit — which is how people learn
        # to treat floors as numbers you adjust.
        #
        # ⛔ …except when the root's keys all live in ONE file, where
        # `total - biggest == 0` and `0 < min_keys < total` has NO INTEGER
        # SOLUTION for total == 1. Blind review walked into that by following
        # this gate's own instruction: add a `_defaults.yaml` to the
        # cardinality-demo tree (the sibling test says "add one, with a floor"),
        # then add `(1, 1, ...)` → `assert 1 < 1`. A one-file root loses
        # everything when it loses that file, so any positive floor detects it.
        lo = total - biggest
        if lo == 0:
            assert 1 <= min_keys <= total, (
                f"{root}: all {total} key(s) live in one file, so min_keys must "
                f"be between 1 and {total} (got {min_keys})")
            continue
        assert lo < min_keys < total, (
            f"{root}: min_keys {min_keys} must be greater than {lo} "
            f"(losing its biggest file, {biggest} keys) and strictly below "
            f"today's {total}")


def test_the_shipped_roots_agree_with_the_confd_schema_hooks():
    """⛔ Second, independent source. `.pre-commit-config.yaml` is the only place
    in this repo that states which conf.d trees are SHIPPED, in those words
    ("shipped config a user reads or runs, NOT every conf.d in the repo"), and
    `_SHIPPED_CONFD_ROOTS` must not drift from it.

    Not set equality: a shipped tree with no `_defaults*.yaml` has nothing for
    this gate to floor. So the relation asserted is — every root here is hooked,
    and every hooked tree that is NOT here has no tracked `_defaults*.yaml`. The
    day one grows a `_defaults.yaml`, this goes red asking for it.
    """
    cfg = (gate.PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hooked: set[str] = set()
    for hook_id in ("confd-schema-check", "confd-schema-check-shipped"):
        # ⛔ Check the hook exists before slicing. `split(...)[1]` on a missing id
        # raises `IndexError: list index out of range`, so renaming or removing
        # the hook hands the reader an index error instead of the drift message
        # this test exists to deliver — and the `len(hooked) >= 4` guard below
        # runs too late to explain it. (CodeRabbit, PR #1410)
        marker = f"id: {hook_id}\n"
        assert marker in cfg, (
            f"pre-commit hook id {hook_id!r} is gone from .pre-commit-config.yaml. "
            "This test pins _SHIPPED_CONFD_ROOTS against those hooks' "
            "--config-dir set; if the hook was renamed, update both."
        )
        entry = cfg.split(marker, 1)[1].split("language:", 1)[0]
        hooked |= {ln.split("--config-dir", 1)[1].strip()
                   for ln in entry.splitlines() if "--config-dir" in ln}
    assert len(hooked) >= 4, hooked  # the parse itself must not go vacuous

    missing = set(gate._SHIPPED_CONFD_ROOTS) - hooked
    assert not missing, (
        f"_SHIPPED_CONFD_ROOTS names {sorted(missing)}, which the confd-schema "
        "hooks do not cover — one of the two is wrong")

    tracked = gate._tracked_defaults_artifacts()
    for tree in sorted(hooked - set(gate._SHIPPED_CONFD_ROOTS)):
        found = [rel for rel in tracked
                 if rel == tree or rel.startswith(tree + "/")]
        assert not found, (
            f"shipped tree {tree!r} now has {found} but no entry in "
            "_SHIPPED_CONFD_ROOTS — add one, with a floor, or this gate is "
            "silent about a tree we ship")


def test_the_scan_is_tracked_scope_not_a_filesystem_walk():
    """⛔ Measured, and the reason this is not an `rglob`: this repo keeps git
    worktrees under `.claude/worktrees/`, so a filesystem walk from the main repo
    root yielded **643 files in 18.7s, 618 of them other branches' working
    copies** — a commit could be blocked by an error naming a path outside the
    author's tree. Tracked scope is 17.
    """
    import subprocess

    tracked = set(gate._tracked_defaults_artifacts())
    assert tracked, "empty scan — the floor should have caught this first"
    listed = subprocess.run(
        ["git", "-C", str(gate.PROJECT_ROOT), "ls-files"],
        capture_output=True, text=True, check=True, timeout=60).stdout.split()
    assert tracked <= set(listed), sorted(tracked - set(listed))
    assert not [r for r in tracked if r.startswith(".claude/")], sorted(tracked)


def test_the_hook_filter_is_as_case_insensitive_as_the_predicate():
    """⛔ A case-SENSITIVE trigger filter beside a case-INSENSITIVE reader is the
    same divergence this round fixed, one layer out: the gate would read
    `_defaults.YAML` while the hook never fired on the commit that added it.

    Derived from the predicate rather than restated — the case variants come from
    whatever `_is_defaults_artifact` accepts, so widening one and not the other
    is what fails here.
    """
    import re

    import yaml

    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"]
             if h["id"] == "threshold-reachability-check"]
    assert len(hooks) == 1
    pattern = re.compile(hooks[0]["files"])

    for name in ("_defaults.yaml", "_defaults.YAML", "_Defaults.yml",
                 "_defaults-multidb.YAML"):
        assert gate._is_defaults_artifact(name), name          # the reader takes it
        rel = f"components/threshold-exporter/config/conf.d/{name}"
        assert pattern.match(rel), (rel, "reader accepts it, hook filter does not")


def test_the_artifact_predicate_matches_the_loader_not_the_sibling_lint():
    """`config_hierarchy.go` lowercases before BOTH its suffix test and its
    filename test, so `_defaults.YAML` is loaded at runtime. Borrowing
    `check_confd_schema.py`'s case-SENSITIVE spelling let that file through on
    every platform, and made `_Defaults.yaml` red on Windows / green on Linux CI
    — the wrong way round (blind review, round 4)."""
    for name in ("_defaults.yaml", "_defaults.YAML", "_Defaults.yml",
                 "_defaults-multidb.YAML"):
        assert gate._is_defaults_artifact(name), name
    for name in ("_defaultsx.txt", "defaults.yaml", "_routing_profiles.yaml"):
        assert not gate._is_defaults_artifact(name), name


def test_the_scan_hands_the_predicate_every_candidate_not_a_narrower_set():
    """⛔ The predicate being case-insensitive buys nothing if what FEEDS it is
    not. `git ls-files -- "*_defaults*"` is case-sensitive (measured under both
    `core.ignorecase` values: `*_DEFAULTS*` → 0), so pairing the two delivered
    half a fix — `_defaults.YAML` got through on its lowercase stem while
    `_Defaults.yaml` and `_DEFAULTS.yml` never reached the predicate at all.

    Neither existing test could see it: one exercises the predicate, the other
    the hook's `(?i:)` trigger filter. **The scan was the untested link.** So
    this asserts the scan lists the whole index and leaves the narrowing to
    `_is_defaults_artifact` — i.e. that there is exactly ONE predicate.
    """
    import subprocess

    listed = subprocess.run(
        ["git", "-C", str(gate.PROJECT_ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60).stdout
    everything = [p for p in listed.decode("utf-8", "replace").split("\0") if p]
    assert len(everything) > 1000, len(everything)   # the whole index, not a slice

    expected = sorted(
        p for p in everything
        if gate._is_defaults_artifact(p.rsplit("/", 1)[-1])
        and (gate.PROJECT_ROOT / p).is_file())
    assert gate._tracked_defaults_artifacts() == expected, (
        "the scan is narrowing before the predicate runs — any filter applied "
        "there is a second, unaudited predicate")


def test_every_exempted_artifact_carries_a_reason_and_still_exists():
    """The exemption table is empty today. If it ever is not, each entry must
    name a real file and say why — an exemption whose path has since moved is an
    exclusion nobody can evaluate."""
    for rel, reason in gate._DEFAULTS_ARTIFACT_EXEMPT.items():
        assert (gate.PROJECT_ROOT / rel).is_file(), rel
        assert reason and len(reason) > 20, (rel, reason)


def test_precommit_filter_covers_every_input_this_gate_reads():
    """⛔ A gate that reads an input its trigger filter does not cover does not
    run on the change that matters. This hook's own comment said exactly that
    about values.yaml while omitting `init_project.py` — a declared face since
    #1310 — so `da-tools init` shipped 16 misplaced keys while the gate that
    reads it never fired on the commit that changed them (#1218).

    Derived, not restated: the inputs come from the module's own imports, its
    path constants, AND the demand-side pack roster.

    ⛔ That third source is not decoration. The first version of this test read
    only imports and module-level `Path` constants — and the DEMAND side is
    neither: `observed_map_lib.default_pack_paths()` is a function returning a
    glob. Measured at the time: the derived set was exactly 8 entries with not
    one `rule-packs/` file in it, so deleting `rule-packs/rule-pack-.*\\.yaml`
    from the `files:` regex left this test green — i.e. the test named after
    covering the gate's inputs could not see the input the gate exists for, and
    the original TRK-337 failure shape (a new alert's commit not triggering the
    reachability gate) walked straight through it. Found by blind review.

    A literal floor plus four named members keeps an empty or collapsed
    derivation from satisfying this vacuously.
    """
    import ast
    import pathlib
    import re

    import yaml

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))

    inputs: set[str] = {"scripts/tools/lint/check_threshold_reachability.py"}

    # the DEMAND side — a function call, invisible to both derivations below
    for p in gate.observed_map_lib.default_pack_paths():
        inputs.add(pathlib.Path(p).resolve().relative_to(REPO_ROOT).as_posix())

    # every module it imports that lives in scripts/tools/ops — module level OR
    # inside a function, since two of these faces import lazily
    # ⛔ BOTH sibling directories the module puts on sys.path, not just `ops/`.
    # The first version mapped import names against `scripts/tools/ops/` alone,
    # so `_lib_compat` / `_lib_exitcodes` / `_lib_validation` — imported at
    # module level from `scripts/tools/` via the `os.path.join(_THIS_DIR, "..")`
    # insert — were invisible to a test whose name says "every input this gate
    # reads" (blind review, round 2). `_lib_exitcodes` in particular owns the
    # gate's exit-code contract, which is the whole meaning of `--ci`.
    # ⛔ Hand-maintained, and that is the defect tracked as #1413: an import from
    # anywhere else is invisible to all three derivations below, so the gate can
    # grow an input that this pin cannot see. #1411 added exactly such an import
    # (`scripts/ops/guard_defaults_scopes`) and had to extend this list by hand —
    # which is the evidence #1413 was opened on, now with a live instance.
    search_dirs = [REPO_ROOT / "scripts" / "tools" / "ops",
                   REPO_ROOT / "scripts" / "tools",
                   REPO_ROOT / "scripts" / "ops"]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    for name in names:
        for d in search_dirs:
            if (d / f"{name}.py").is_file():
                inputs.add((d / f"{name}.py").relative_to(REPO_ROOT).as_posix())
                break

    # every repo file it reads through a module-level Path constant
    for value in vars(gate).values():
        if isinstance(value, pathlib.Path) and value.is_file():
            try:
                inputs.add(value.relative_to(REPO_ROOT).as_posix())
            except ValueError:
                pass

    # ⛔ …and the DERIVED artifact scan, which is neither an import nor a Path
    # constant. When the defaults-tier face stopped naming two files and started
    # walking for them, this test kept passing while silently covering 15 fewer
    # inputs than the gate reads — the same "derivation only sees one kind of
    # source" blind spot that let the demand side (a function returning a glob)
    # slip past the first version. Third source, third shape.
    artifacts = [p.relative_to(REPO_ROOT).as_posix()
                 for p in gate._defaults_artifacts()]
    assert len(artifacts) >= gate._DEFAULTS_ARTIFACT_FLOOR, artifacts
    inputs.update(artifacts)

    assert len(inputs) >= 20, sorted(inputs)
    for expected in ("scripts/tools/ops/init_project.py",
                     "scripts/tools/ops/_registry_lib.py",
                     "scripts/tools/ops/onboard_platform.py",
                     "scripts/tools/_lib_exitcodes.py",
                     "helm/threshold-exporter/values.yaml"):
        assert expected in inputs, (expected, sorted(inputs))
    assert any(p.startswith("rule-packs/") for p in inputs), sorted(inputs)

    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"]
             if h["id"] == "threshold-reachability-check"]
    assert len(hooks) == 1, "hook id moved or was duplicated"
    pattern = re.compile(hooks[0]["files"])

    uncovered = sorted(p for p in inputs if not pattern.match(p))
    assert uncovered == [], (
        "threshold-reachability-check reads these but its `files:` filter does "
        "not fire on them, so a commit touching only one of them skips the "
        f"gate: {uncovered}")


# ── the frozen four-floor measurements, REPLAYED at their own commit (#1411) ──
#
# The fifth floor's comment carries two historical figures measured on the
# four-floor subset as it ran at `72fdaf56`. An earlier revision of that comment
# said "NEITHER HAS A TRIPWIRE, AND NEITHER CAN", and the second half was false
# by construction — what follows is the construction.
#
# The objection it rested on is real and is preserved: re-deriving those numbers
# from TODAY's tree would compare across worlds and would go red the next time a
# fixture is retired, i.e. fire at ordinary maintenance. The move is not to
# compare across worlds. It is to REPLAY the measurement inside the world it was
# taken in — `git archive` that commit, load ITS gate module, run ITS floors —
# so today's tree is not an input at all and cannot move the answer.

_FROZEN_COMMIT = "72fdaf56"


def _load_module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _module_isolation():
    """Let the frozen tree's own helpers win the import, then put ours back.

    ⛔ Not hygiene-for-its-own-sake. The frozen gate imports `scaffold_tenant`,
    `init_project` and `onboard_platform` to build its GENERATOR faces, and
    those names are already in `sys.modules` — pointing at TODAY's files —
    because the live gate at the top of this file imported them. Without this,
    the "frozen" run would build today's generators, and a replay that quietly
    mixes the two worlds is worse than no replay: it would still print three
    confident integers.

    Everything currently loaded out of the live `scripts/` tree is evicted for
    the duration and restored afterwards, derived rather than listed so a helper
    added to the gate later is covered without anyone remembering to add it.
    """
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    live_scripts = str(REPO_ROOT / "scripts")
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if origin.startswith(live_scripts):
            del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n not in saved_modules]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


@pytest.fixture(scope="module")
def _frozen_checkout(tmp_path_factory):
    """`72fdaf56`, materialised: its files and its index. NO modules loaded.

    ⛔ THE SPLIT IS THE POINT. This half is the expensive one (archive, untar,
    `git init`, `git add -A -f`) and it is inert — a directory of files touches
    no interpreter state, so sharing it across the module costs nothing and
    leaks nothing. `frozen_tree` below is the half that mutates `sys.modules`,
    and it is FUNCTION-scoped for exactly that reason.

    ⛔ `git init && git add -A -f` is load-bearing, not tidiness: the frozen
    scan shells out to `git ls-files`, so an archive with no index reads as an
    empty repository and every floor below would go vacuous at once. `-f`
    because a `.gitignore` shipped in the archive must not be allowed to hide a
    file that WAS tracked at that commit — the denominators below would move and
    the replay would report a measurement of a tree that never existed.

    ⛔⛔ AND IT IS NOT ALLOWED TO SKIP. If the commit is unreachable this fails,
    loudly, naming why. A shallow clone is the one realistic cause and it is a
    non-issue in CI — `.github/workflows/ci.yml`'s `python-tests-run` job (the
    job that runs this file) checks out with `fetch-depth: 0`, and the comment
    there records that it does so precisely because another lint test in this
    directory resolves a git object and is "fail-closed by design — it RAISES
    rather than silently passing". Same discipline here, and for a reason this
    repo paid for one commit ago: #1402 was an aggregate gate reading a SKIPPED
    leg as PASS. A skip that upstream reads as green is the failure mode, not
    the safe default.
    """
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{_FROZEN_COMMIT}^{{commit}}"],
        capture_output=True, timeout=60)
    if probe.returncode != 0:
        pytest.fail(
            f"the frozen measurements are pinned to commit {_FROZEN_COMMIT}, "
            "which this clone cannot reach, so they are currently UNGUARDED.\n"
            "  MOST LIKELY: a shallow clone. `git fetch --unshallow` fixes it "
            "locally; in CI the checkout step needs `fetch-depth: 0` (the "
            "`python-tests-run` job already sets it, and says why).\n"
            "  ⛔ THIS IS DELIBERATELY A FAILURE AND NOT A SKIP. The numbers it "
            "guards are frozen historical measurements with no other tripwire — "
            "a skip here means nobody is watching them, and this repo has "
            "already shipped one aggregate gate that read a skipped leg as PASS "
            "(#1402, one commit before this floor). If the commit is genuinely "
            "gone from history, re-measure and re-freeze against whatever "
            "replaced it; do not delete this test and keep the numbers.")

    dest = tmp_path_factory.mktemp("frozen-72fdaf56")
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", _FROZEN_COMMIT],
        capture_output=True, check=True, timeout=120)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout,
                   check=True, timeout=120)
    subprocess.run(["git", "init", "-q", str(dest)], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(dest), "add", "-A", "-f"],
                   check=True, capture_output=True, timeout=120)
    return dest


@pytest.fixture
def frozen_tree(_frozen_checkout):
    """The frozen gate module — alive for ONE test, never longer.

    ⛔ FUNCTION-SCOPED, AND THAT IS A CORRECTION. `_module_isolation()` wraps
    the `yield`, so the frozen world stands for as long as this fixture does.
    While this was module-scoped that meant "from the first test that asks
    until the end of the file", and today's ordering — the two replay tests
    happen to sit last — was the only thing making that harmless.

    Measured, with collection order reversed so the replay runs first: EVERY
    test in this file then ran with SIXTEEN modules resolving into the frozen
    checkout — the two loaded here by name, plus fourteen live-tree names the
    frozen gate pulls in behind them (`scaffold_tenant`, `init_project`,
    `onboard_platform`, `migrate_rule`, `_registry_lib`, `_observed_map_lib`
    and eight `_lib_*` helpers) — and all but the two replay tests have nothing
    to do with the frozen world at all. ⚠️ Two earlier wordings of this
    measurement were low: one said eight modules, listing five and stopping;
    the other said all-but-one test, which is what a probe sampling during the
    call phase does NOT report — the fixture is set up during SETUP, so the
    test that pulls the frozen world in is already inside it by the time it
    runs. The LIVE gate builds its generator
    faces from those, and it imports two of them lazily, inside the call — so
    the live tests were measuring today's tree with that commit's producers, and
    the suite stayed green throughout. The fixture below says "a replay that
    quietly mixes the two worlds is worse than no replay"; module scope made
    that true in the other direction, and adding one test or letting a scheduler
    reorder was all it took.

    ⛔ THE SCOPE HAS A CONSUMER NOW:
    `test_the_frozen_world_does_not_outlive_the_test_that_asked_for_it`. It was
    added because this repair was, for a round, held up by this decorator alone
    — and a repair that only a decorator enforces goes back in silence.

    ⛔ The cost of the fix is the module LOAD, not the checkout: the archive is
    materialised once by `_frozen_checkout` (module-scoped, unchanged), and
    what is paid per test is exec'ing the frozen gate and its imports.
    """
    with _module_isolation():
        frozen = _load_module_by_path(
            "frozen_gate_at_72fdaf56",
            _frozen_checkout / "scripts" / "tools" / "lint"
            / "check_threshold_reachability.py")
        # ⛔ The GROUPING comes from the frozen tree too, and it is honestly
        # NOT load-bearing today: `scripts/ops/guard_defaults_scopes.py` is
        # byte-identical at `72fdaf56` and at HEAD (measured: `git diff` between
        # them is empty), so swapping today's `conf_d_root` in leaves both
        # replay tests green and no test can tell the two apart. It is loaded
        # from the frozen tree anyway, for the same reason the exemption is
        # DERIVED from the frozen tree rather than read out of
        # `_DEFAULTS_ROOTS_MAY_BE_EMPTY`: if today's grouping is ever edited,
        # a frozen measurement must not move with it. The hedge is stated here
        # rather than tested, because a test for it would only be able to
        # compare the two copies — which is the cross-world comparison this
        # whole replay exists to avoid.
        scopes = _load_module_by_path(
            "frozen_scopes_at_72fdaf56",
            _frozen_checkout / "scripts" / "ops" / "guard_defaults_scopes.py")
        # ⛔ The frozen module resolves its own PROJECT_ROOT from `__file__`; if
        # that ever landed back on the live tree the replay would be measuring
        # today under a frozen name, which is the one outcome that would look
        # exactly like success.
        assert frozen.PROJECT_ROOT == _frozen_checkout, (
            frozen.PROJECT_ROOT, _frozen_checkout)
        yield frozen, scopes.conf_d_root


_EMPTIED_ARTIFACT = b"# replayed: the file is still on disk, `defaults:` is not\n{}\n"


def _replay_frozen_measurements(frozen, conf_d_root, *, stand_down=()):
    """Re-run the two frozen scenarios through the frozen gate's OWN floors.

    ⛔ IT CALLS THEM; it does not re-implement their tests. `_defaults_faces()`
    at that commit runs all four floors — the two file floors inline, then
    `_assert_shipped_roots_intact` and `_assert_keys_floor` — so it is the whole
    oracle, and "did a floor speak?" is "did it raise?". A throwaway script
    written while designing this got ① wrong by re-implementing the shipped
    root's check and applying its FILE-COUNT half to the keys-go-to-zero
    scenario, where by definition every file is still there. A
    re-implementation is a second opinion wearing the floors' authority.

    ⛔ BEFORE and AFTER: the FIRST call below is the untouched world, and it is
    made outside the try/except that the scenarios use, so a frozen gate that
    is already breached on its own tree aborts here instead of being credited
    with catching every group in turn. Without that, the replay would report
    zero blind spots at exactly the moment the frozen gate was most broken —
    the same discipline `_floors_that_notice_a_zeroed_root` is built on, one
    world over.

    The scenarios are staged as INPUT, never as arithmetic:
      ① keys to zero, files on disk — the victim's artifacts are rewritten in
         the throwaway checkout to hold no `defaults:` section, so the scan
         still finds every file and the two file floors genuinely cannot move.
      ② the group gone, files and all — the victim's artifacts are dropped from
         what the scan returns, so the file floors see the smaller world too.

    `stand_down` names frozen floors to disable, and takes exactly the two that
    can change an answer here: `"keys"` and `"shipped"`. ⛔ There is deliberately
    no `"files"`: measured, standing the two FILE floors down leaves every
    fraction byte-identical to the baseline, in both scenarios — ① never touches
    a file, and ② removes one directory from a scan that stays far above both
    file floors. The branch existed, was never reached by any caller, and cost
    two hardcoded floor constants in the `finally` above. ⚠️ So "all four floors
    counted together" (the wording beside `_DEFAULTS_CONFD_ROOTS`) means all four
    were IN EFFECT and given the chance to speak — not that all four contribute
    to the answer. Two of them measurably do not.

    ⛔ ONE LEG OF THREE, and the other two are the caller's. A `stand_down` on
    its own only shows the answer RESPONDS to something, and a body that MODELS
    the floors' arithmetic reads `stand_down` too and therefore responds in step
    — measured, such a body left the whole file green. So
    `test_the_replay_measures_rather_than_restates` also (2) wraps the frozen
    module's functions from outside to show they were ENTERED, and (3) forces the
    frozen oracle's verdict on one named group each way to show the reported
    blind set FOLLOWS THE VERDICT — a body that calls `_defaults_faces()`,
    swallows the `_GateViolation` and answers from a table of its own passes (1)
    and (2) and fails (3).

    ⛔ THIS FUNCTION DOES NOT COUNT ITS OWN WORK, and that is deliberate: a
    self-reported "I evaluated the floors N times" is a claim by the very body
    under suspicion, so it cannot tell a measurement from a constant. The
    caller wraps the frozen module's floor functions and counts from out there.
    """
    # ⛔ Deliberately NOT inside the try/except used for the scenarios: on a
    # dirty frozen tree this raises out of the replay rather than reading as a
    # blind spot.
    generators, artifacts = frozen._defaults_faces()          # the CLEAN baseline
    prefix = frozen._ARTIFACT_FACE_PREFIX
    keys_of = {label[len(prefix):-1]: keys for label, keys in artifacts.items()}
    rels = sorted(keys_of)

    real_scan = frozen._tracked_defaults_artifacts
    real_shipped = frozen._assert_shipped_roots_intact
    real_keys_floor = frozen._assert_keys_floor
    if "shipped" in stand_down:
        frozen._assert_shipped_roots_intact = lambda *_a, **_k: None
    if "keys" in stand_down:
        frozen._assert_keys_floor = lambda *_a, **_k: None

    def _all_four_stay_silent():
        try:
            frozen._defaults_faces()
            return True
        except frozen._GateViolation:
            return False

    try:
        blind_roots = []
        for root in sorted({conf_d_root(rel) for rel in rels} - {None}):
            victims = [rel for rel in rels if conf_d_root(rel) == root]
            saved = {frozen.PROJECT_ROOT / rel:
                     (frozen.PROJECT_ROOT / rel).read_bytes() for rel in victims}
            for path in saved:
                path.write_bytes(_EMPTIED_ARTIFACT)
            try:
                if _all_four_stay_silent():
                    blind_roots.append(root)
            finally:
                for path, blob in saved.items():
                    path.write_bytes(blob)

        blind_dirs = []
        for group in sorted({rel.rsplit("/", 1)[0] for rel in rels}):
            survivors = [rel for rel in rels if rel.rsplit("/", 1)[0] != group]
            frozen._tracked_defaults_artifacts = lambda _s=survivors: list(_s)
            try:
                if _all_four_stay_silent():
                    blind_dirs.append(group)
            finally:
                frozen._tracked_defaults_artifacts = real_scan
    finally:
        # ⛔ Every restore here puts back a value that was SAVED above. An
        # earlier spelling also reset the two file-floor constants to hardcoded
        # literals, unconditionally — measured: editing those literals to 0 left
        # the suite at 114 passed, with both file floors dead from the second
        # call onward and nothing to say so. The branch that needed them is gone
        # (see the docstring), and with it the only copy of those numbers here.
        frozen._tracked_defaults_artifacts = real_scan
        frozen._assert_shipped_roots_intact = real_shipped
        frozen._assert_keys_floor = real_keys_floor

    # ⛔ The exemption is DERIVED FROM THE FROZEN TREE, not read out of today's
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY`. That list did not exist at this commit
    # (measured: zero occurrences in the file), and the frozen comment is
    # explicit that the exemption is a condition of how the measurement is
    # RE-RUN. Reading today's list would let an ordinary future addition to it
    # move a frozen number — precisely the "fires at ordinary maintenance"
    # objection that the frozen figures were nearly left unguarded over.
    exempt = {root for root in {conf_d_root(rel) for rel in rels} - {None}
              if not sum(len(keys_of[rel]) for rel in rels
                         if conf_d_root(rel) == root)}
    scanned = sorted({conf_d_root(rel) for rel in rels} - {None})
    watched = [root for root in scanned if root not in exempt]
    dirs = sorted({rel.rsplit("/", 1)[0] for rel in rels})

    # ⛔ THE FRACTIONS ARE BUILT HERE, EACH FROM ONE POPULATION, and that is a
    # correctness measure rather than a convenience. Measured while writing the
    # counterfactuals for this test: with the numerator and the denominator
    # asserted as two loose values, restating ① with its post-exemption
    # numerator over its pre-exemption denominator PASSED — both of those are
    # TRUE numbers about this tree, just not about the same set of roots. That
    # is the whole defect of the original sentence in one line: a fraction can
    # be made of two true integers and still be a false claim, so no assertion
    # over the integers separately can forbid it. Pairing them where the
    # population is known is what makes the mixed fraction a value this function
    # cannot produce.
    return {
        "scanned_roots": scanned,
        "watched_roots": watched,
        "exempt_roots": sorted(exempt),
        "blind_roots": blind_roots,
        "blind_roots_after_exemption": [r for r in blind_roots if r not in exempt],
        # ① over the WATCHED population (post-exemption on both halves)
        "fraction_watched": (len([r for r in blind_roots if r not in exempt]),
                             len(watched)),
        # ① over the SCANNED population (pre-exemption on both halves)
        "fraction_scanned": (len(blind_roots), len(scanned)),
        "artifact_dirs": dirs,
        "blind_dirs": blind_dirs,
        # ② over the artifact-directory population
        "fraction_dirs": (len(blind_dirs), len(dirs)),
        "artifacts": rels,
        "invisible_keys": sum(len(keys_of[rel]) for rel in rels
                              if rel.rsplit("/", 1)[0] in blind_dirs),
        "total_keys": sum(len(keys) for keys in keys_of.values()),
        "generator_keys": sum(len(v) for v in generators.values()),
    }


def test_the_frozen_measurements_replay_against_their_own_commit(frozen_tree):
    """⛔ The two frozen figures beside `_DEFAULTS_CONFD_ROOTS`, re-measured.

    Both halves of every fraction are asserted, and that is the whole point:
    the defect this issue is a correction of was a numerator kept while its
    grouping was rewritten, so a test that pinned only the numerators would be
    blind to the exact failure it exists for.

    ⛔ THE MIXED FRACTION MUST NOT PASS: ①'s post-exemption numerator over its
    pre-exemption denominator. Each half is a true count of this tree, of two
    different sets of roots. It is not a harmless slip either — the
    post-exemption numerator is independently the answer to a different
    (scenario × exemption) pair, so the wrong fraction reads exactly like a
    right one.

    ⚠️ AND ASSERTING THE TWO INTEGERS SEPARATELY DOES NOT FORBID IT. Measured
    while building this test's counterfactuals: an earlier spelling checked the
    numerator and the denominator as two loose values and the mixed fraction
    PASSED, for the good reason that both halves are true of this tree. Each
    fraction is therefore assembled inside `_replay_frozen_measurements` where
    its population is known, and asserted as a single object — the mixed pairing
    is now a value the replay does not produce, rather than one the assertion
    tolerates. ⛔ The integers below are the only copy anywhere: the four places
    that used to repeat them — two comments in the gate, the CHANGELOG entry for
    #1411, and two docstrings in this file — carry conditions only.
    """
    frozen, conf_d_root = frozen_tree
    m = _replay_frozen_measurements(frozen, conf_d_root)

    # ① grouping `conf_d_root`; keys to zero, files on disk; all four floors.
    assert m["fraction_watched"] == (7, 11), m
    assert m["fraction_scanned"] == (8, 12), m
    assert len(m["exempt_roots"]) == 1, m
    # …and the exempt root is the one the comment names, not merely "some root":
    # the count alone is satisfied by exempting the wrong tree.
    assert m["exempt_roots"] == ["rule-packs/recipes/examples/conf.d"], m
    # ⛔ The two numerators are drawn from DIFFERENT SETS, demonstrated rather
    # than asserted as a size difference: the exempt root is blind under ① and
    # is the entire difference between 8 and 7. Anyone tempted to treat the two
    # fractions as interchangeable has to walk past this line.
    assert set(m["blind_roots"]) - set(m["blind_roots_after_exemption"]) == set(
        m["exempt_roots"]), m
    assert set(m["blind_roots_after_exemption"]) <= set(m["watched_roots"]), m
    assert set(m["watched_roots"]) < set(m["scanned_roots"]), m

    # ② grouping = each artifact's own directory; the group gone, files and all.
    assert m["fraction_dirs"] == (11, 17), m

    # ⛔ Cross-checked against a measurement taken by hand, off this file
    # entirely: the CORRECTION COMMENT on #1411 reports ② — same grouping, same
    # four floors, same commit — arrived at independently, and the pair below
    # reproduces it. (⚠️ The issue BODY is a different measurement: same
    # grouping, but only `_assert_keys_floor`, so its figures legitimately
    # differ. A reader who checks against the body concludes a number moved
    # when nothing did.) The KEY figure is asserted alongside the directory one
    # because it is the half a grouping bug does NOT move in step — a partition
    # error changes which directories are blind and would have to change the key
    # total with it to stay consistent.
    assert (m["invisible_keys"], m["total_keys"]) == (35, 70), m
    assert m["generator_keys"] == 102, m


def test_the_replay_measures_rather_than_restates(frozen_tree):
    """⛔ The replay's own tripwire — it guards numbers nothing else guards.

    Per the stopping rule this round ran under, a guard needs guarding exactly
    when its silent failure lets the original defect back, and this one's silent
    failure is "the frozen figures are unwatched again" with three green
    assertions still printing. THREE ways it could go quietly vacuous — the
    third was found by measurement after the first two were closed — and each is
    shut here by construction rather than by reading the code:

      1. IT NEVER RAN. A loop over an empty scan, an exception swallowed into
         "blind", a fixture that yielded nothing — all of these produce tidy
         numbers. So the frozen floor FUNCTIONS are wrapped from out here and
         the calls are counted by THIS test, and both partitions must be
         non-degenerate.
      2. IT IS NOT A MEASUREMENT. Integers that happen to match can be produced
         by a function that never consults a floor. So each frozen floor is
         stood down in turn and the answer MUST move.
      3. IT RAN AND THE ANSWER CAME FROM SOMEWHERE ELSE. ⛔ The one the first
         two miss, and it is not hypothetical: a body that calls
         `_defaults_faces()`, discards the `_GateViolation` with a bare
         `except: pass` and reports a table of its own passes (1) — the counters
         see the real call count — and passes (2), because its table is indexed
         by `stand_down`. Measured at 114 passed; and measured again at 114
         passed with the frozen artifact-key floor genuinely neutered
         underneath it, which is a frozen floor failing with nothing to say so.
         So the verdict is FORCED here, one group each way, and the reported
         blind set has to follow it.

    ⛔⛔ THE COUNT IS TAKEN FROM OUTSIDE, and that is the correction this test
    needed. It used to assert a `floor_evaluations` the replay reported about
    ITSELF, and both sentences that rested on it were measured FALSE:
      * a `_replay_frozen_measurements` replaced by a constants table — no
        frozen tree opened, no floor called, `floor_evaluations` hardcoded to
        the same sum and one row picked by `stand_down` — left this test at 2
        passed;
      * an `_all_four_stay_silent` replaced by an arithmetic MODEL of the four
        floors (the shipped table and both key floors copied out by hand, never
        calling `_defaults_faces()`) left the whole file at 114 passed, because
        the model reads `stand_down` too and therefore "follows the floors".
    A self-reported call count is a claim by the body under suspicion. The
    wrappers below are installed by the test on the frozen MODULE, so the only
    way to move the counters is to actually call that commit's functions.

    ⚠️ The directions are asserted, not just "it changed". Removing a floor can
    only ever make MORE groups blind, never fewer; an assertion that merely
    required difference would be satisfied by a replay that moved the wrong way.
    """
    frozen, conf_d_root = frozen_tree

    # 1) IT REALLY RAN — counted by wrapping the frozen module's own functions.
    real = {name: getattr(frozen, name) for name in
            ("_defaults_faces", "_assert_shipped_roots_intact",
             "_assert_keys_floor")}
    # ⛔ Seeded at zero rather than filled in on first call: a replay that never
    # touches the frozen module must fail on the NUMBER, with the counters
    # visible, not on a KeyError that reads like a broken test.
    seen: dict[str, int] = {name: 0 for name in real}

    def _counting(name, fn):
        def wrapper(*args, **kwargs):
            seen[name] += 1
            return fn(*args, **kwargs)
        return wrapper

    for name, fn in real.items():
        setattr(frozen, name, _counting(name, fn))
    try:
        baseline = _replay_frozen_measurements(frozen, conf_d_root)
    finally:
        # ⛔ before the stand-down runs below: those replace two of these three
        # attributes themselves, and a wrapper left in place would be restored
        # by the replay's own `finally` and go on counting into the next run.
        for name, fn in real.items():
            setattr(frozen, name, fn)

    # one clean baseline, then one per root and one per artifact directory —
    # and the frozen oracle really was entered that many times.
    assert seen["_defaults_faces"] == 1 + 12 + 17, seen
    # …and it went THROUGH the floors, not merely into the function that owns
    # them. ⛔ Stated as relations, not as three more hand-written integers:
    # every evaluation reaches the shipped-root floor, and the keys floor is
    # reached only on the ones where the shipped floor stayed silent — so it
    # must be entered strictly fewer times. Equality there would mean the
    # shipped floor had stopped raising, which is one of the silent failures
    # this test exists for.
    assert seen["_assert_shipped_roots_intact"] == seen["_defaults_faces"], seen
    assert 0 < seen["_assert_keys_floor"] < seen["_defaults_faces"], seen
    assert len(baseline["artifacts"]) == 17, baseline
    # neither partition may be all-blind or none-blind — either extreme is what
    # a broken oracle looks like (nothing ever raised / everything did).
    assert 0 < len(baseline["blind_roots"]) < len(baseline["scanned_roots"])
    assert 0 < len(baseline["blind_dirs"]) < len(baseline["artifact_dirs"])

    # 2) stand each frozen floor down; the blind counts must GROW.
    for floor in ("keys", "shipped"):
        moved = _replay_frozen_measurements(frozen, conf_d_root, stand_down=(floor,))
        assert len(moved["blind_roots"]) > len(baseline["blind_roots"]), (
            floor, moved["blind_roots"], baseline["blind_roots"])
        assert len(moved["blind_dirs"]) > len(baseline["blind_dirs"]), (
            floor, moved["blind_dirs"], baseline["blind_dirs"])
        # the populations are properties of the tree, not of the floors, so they
        # must NOT move — that is what tells "a floor went quiet" apart from
        # "the replay lost half its input".
        assert moved["scanned_roots"] == baseline["scanned_roots"], floor
        assert moved["artifact_dirs"] == baseline["artifact_dirs"], floor

    # 3) THE VERDICT HAS TO TRAVEL — see (3) in the docstring. One root the
    # frozen floors DO catch is forced to stay silent, one they MISS is forced
    # to speak, and the reported blind set must move by exactly those two.
    caught = sorted(set(baseline["scanned_roots"]) - set(baseline["blind_roots"]))
    assert caught, baseline                 # some root is caught today…
    assert baseline["blind_roots"], baseline    # …and some root is not
    forced_silent, forced_loud = caught[0], sorted(baseline["blind_roots"])[0]

    victims_of = {
        root: [rel for rel in baseline["artifacts"] if conf_d_root(rel) == root]
        for root in (forced_silent, forced_loud)}
    # ⛔ Non-empty, or `all()` below is vacuously true and the inversion fires on
    # every call — which would look like a very thorough test and measure nothing.
    assert all(victims_of.values()), victims_of

    def _is_the_victim(root):
        # ⛔ Keyed on the world the replay has STAGED, not on a call index: ①
        # empties exactly one root's artifacts at a time and restores them, so
        # "this root's files are the emptied ones" names the group without this
        # test having to know the replay's call ORDER. ② writes no file, so
        # neither inversion can reach its scenario — which is what makes
        # "②'s partition did not move" an assertion worth making below.
        return all((frozen.PROJECT_ROOT / rel).read_bytes() == _EMPTIED_ARTIFACT
                   for rel in victims_of[root])

    real_faces = frozen._defaults_faces

    def _inverting_the_verdict(*args, **kwargs):
        if _is_the_victim(forced_loud):
            raise frozen._GateViolation(
                f"replay tripwire: verdict forced LOUD for {forced_loud}")
        try:
            return real_faces(*args, **kwargs)
        except frozen._GateViolation:
            if _is_the_victim(forced_silent):
                return {}, {}       # forced SILENT; the caller discards this
            raise

    frozen._defaults_faces = _inverting_the_verdict
    try:
        forced = _replay_frozen_measurements(frozen, conf_d_root)
    finally:
        frozen._defaults_faces = real_faces

    assert forced["blind_roots"] == sorted(
        (set(baseline["blind_roots"]) | {forced_silent}) - {forced_loud}), (
            "the replay reported a blind set that did not follow the oracle's "
            "verdict — an answer that survives having the verdict inverted is "
            "not being read off the frozen floors at all",
            forced_silent, forced_loud, forced["blind_roots"],
            baseline["blind_roots"])
    # …and ONLY the roots moved. The inversion cannot reach ②'s scenario, so a
    # body answering from anywhere but the oracle has to get both halves right
    # at once — move the roots, hold the directories — to stay green.
    assert forced["blind_dirs"] == baseline["blind_dirs"], (
        forced["blind_dirs"], baseline["blind_dirs"])
    assert forced["scanned_roots"] == baseline["scanned_roots"]
    assert forced["artifact_dirs"] == baseline["artifact_dirs"]

    # …and the floors are back, so the stand-downs and the forced verdicts above
    # did not leak into the last run of this test. (The sibling test no longer
    # shares this module object — `frozen_tree` is function-scoped — but a
    # stand-down that failed to restore would still corrupt everything after it
    # inside this test.)
    assert _replay_frozen_measurements(frozen, conf_d_root) == baseline


def _resolving_into(checkout: Path) -> list[str]:
    """Loaded modules whose source file lives inside the frozen checkout."""
    root = str(checkout)
    return sorted(name for name, module in list(sys.modules.items())
                  if (getattr(module, "__file__", None) or "").startswith(root))


def test_the_frozen_world_does_not_outlive_the_test_that_asked_for_it(
        request, _frozen_checkout):
    """⛔ The CONSUMER for `frozen_tree`'s function scope — it had none.

    That scope is the whole isolation between the replay and the other 100-odd
    tests in this file, and it was being held by one decorator argument with
    nothing reading it. Measured, by putting `scope="module"` back: the suite
    stayed at 114 passed, and with collection order reversed so the replay ran
    first, most of the file then executed with the live gate's helpers resolving
    into the frozen checkout — the live tests measuring today's tree with that
    commit's producers, green throughout. A repair that only a decorator
    enforces is a repair one edit away from being undone in silence.

    ⛔ ORDER-INDEPENDENT BY CONSTRUCTION, because "the replay happens to run
    last" is exactly the accident that made module scope look harmless:
      * the finalizer is registered BEFORE `frozen_tree` is set up, so pytest's
        LIFO teardown runs it AFTER that fixture's own teardown. A
        function-scoped fixture is gone by then; a module-scoped one is not, and
        the check fails no matter where this test sits in the file.
      * the same check runs on entry, so a frozen world left standing by an
        EARLIER test is caught here too.
    ⛔ And it is asserted non-vacuous in between: while this test holds the
    fixture the frozen world must be visible, or "nothing leaked" is being
    reported by a probe that can see nothing at all.
    """
    def _no_frozen_leak(when: str):
        leaked = _resolving_into(_frozen_checkout)
        on_path = [entry for entry in sys.path
                   if entry.startswith(str(_frozen_checkout))]
        assert not leaked and not on_path, (
            f"{when}: the frozen checkout is still reachable from this "
            "interpreter, so tests that are supposed to measure TODAY's tree "
            "can silently resolve into `72fdaf56` instead. `frozen_tree` must "
            f"stay function-scoped.\n  modules: {leaked}\n  sys.path: {on_path}")

    _no_frozen_leak("before this test asked for the frozen world")
    request.addfinalizer(
        lambda: _no_frozen_leak("after the frozen world was released"))

    frozen, _conf_d_root = request.getfixturevalue("frozen_tree")
    # A real measurement, not just the import: the gate imports two of its
    # generator producers lazily, INSIDE `_defaults_faces()`, so they only enter
    # `sys.modules` once it is called — and those are the modules the reversed-
    # order run caught pointing at the frozen tree.
    frozen._defaults_faces()

    held = _resolving_into(_frozen_checkout)
    assert "frozen_gate_at_72fdaf56" in held, held
    assert len(held) > 2, (
        "the frozen gate pulls its own helpers in with it; seeing almost "
        "nothing here means this probe would not notice a leak either", held)


def test_every_floor_names_its_own_ticket_because_the_banner_cannot():
    """⛔ Each `_GateViolation` message must name the ticket that owns it.

    The breach banner in `main()` used to carry `TRK-344 / #1392` — the fourth
    face's pair. Measured on the revision that shipped it: of this module's
    `_GateViolation` sites, six spoke for the FIFTH floor (#1411) and the banner
    sent every one of those readers to a ticket that says nothing about what
    fired. A banner printed after the fact cannot know which floor raised, so
    the ticket moved to where it is known — the message itself — and the banner
    now points at that line instead.

    ⛔ This asserts the CLASS, not the six sites that were missing one: a sixth
    floor added without a ticket is the same defect again, and the banner can no
    longer cover for it.
    """
    import ast
    import re

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    src = _SCRIPT.read_text(encoding="utf-8")

    sites, ticketless = 0, []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        if getattr(node.exc.func, "id", "") != "_GateViolation":
            continue
        sites += 1
        seg = ast.get_source_segment(src, node) or ""
        if not re.search(r"#\d{3,4}|TRK-\d+", seg):
            ticketless.append(node.lineno)

    assert sites >= 10, (
        f"only {sites} _GateViolation site(s) found — the walk stopped seeing "
        "them, so this test would pass by looking at nothing")
    assert not ticketless, (
        "these _GateViolation sites name no ticket, and the breach banner no "
        "longer names one for them — a reader who trips them has nowhere to go",
        ticketless)

    # ⛔ Via AST, not a substring of the source: the banner is written as
    # adjacent string literals across several lines, so the raw text contains
    # the quote-newline-indent joins and any substring check against it answers
    # a question about formatting rather than about what the reader sees.
    banner = next(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "defaults-tier floor breach" in n.value)
    # ⛔ `TRK-337` is allowed here and nowhere else in this assertion: the banner
    # names it as one of the things that did NOT run, not as the origin of the
    # breach. What must not come back is a ticket standing where the BREACHING
    # floor's identity goes — the fourth face's pair is the one that was there.
    owning = [t for t in ("TRK-344", "#1392", "#1411") if t in banner]
    assert not owning, (
        "the breach banner names a floor's ticket again; it cannot know which "
        "floor raised, so any ticket it names is right for some and wrong for "
        "others", owning, banner)
    assert "names the floor and its ticket" in banner, (
        "the banner no longer points at the line that does know — without that "
        "pointer, dropping the ticket just removed information", banner)


# ── SIXTH floor: the loader-readable half of a root (#1458) ─────────────────
#
# The defect these cover is not "a floor is too low"; it is that every floor in
# this module counts files the RUNNING EXPORTER may never open. `git mv
# _defaults.yaml _defaults-legacy.yaml` leaves the artifact count, the wide key
# count and every global floor exactly where they were, and stops the platform
# loading a single declaration in that tree. Measured on the ticket, and again
# here (see the control-group test at the end of this block).


def test_the_loader_readable_predicate_is_exactly_the_loaders_test():
    """The predicate must be EQUALITY, not the wide prefix its sibling uses.

    ⛔ Behaviour table only — deliberately NOT a grep of `config_hierarchy.go`.
    This repo measured (#1448 blind review) that a Python guard asserting things
    about Go source text goes red on legitimate Go refactors and states
    something false when it does. What is pinned here is that
    `_is_loader_readable_defaults` and `_is_defaults_artifact` DISAGREE on
    exactly the names the loader skips — if someone "unifies" them, the sixth
    floor still runs and can no longer fire.
    """
    reads = ["_defaults.yaml", "_defaults.yml", "_DEFAULTS.YAML", "_Defaults.Yml"]
    skips = ["_defaults-legacy.yaml", "_defaults.bak.yaml", "_defaults2.yaml",
             "_defaults-multidb.yaml", "defaults.yaml", "_defaults.yaml.bak"]
    for name in reads:
        assert gate._is_loader_readable_defaults(name), name
        assert gate._is_defaults_artifact(name), (name, "sibling must agree here")
    for name in skips:
        assert not gate._is_loader_readable_defaults(name), name
    # ⛔ And the disagreement is the point: these are the names the WIDE
    # predicate keeps counting while the loader ignores them. If this set ever
    # empties, the two predicates have been merged and the floor is dead code.
    divergent = [n for n in skips if gate._is_defaults_artifact(n)]
    assert divergent, (
        "the two predicates no longer disagree anywhere — merging them removes "
        "the sixth floor's reason to exist without removing its code")


def test_the_loader_readable_floor_fires_where_the_wide_floor_stays_quiet():
    """The measurement that justifies a sixth floor, not just an assertion.

    Same input, both floors: the shipped root keeps 2 artifacts and 19 keys
    (so `_assert_shipped_roots_intact` is satisfied), while zero of them is a
    name the exporter opens.
    """
    root = "components/threshold-exporter/config/conf.d"
    by_root = {r: [] for r in gate._SHIPPED_CONFD_ROOTS}
    by_root[root] = [
        (root + "/_defaults-legacy.yaml", _flat(*[f"k{i}" for i in range(8)])),
        (root + "/examples/_defaults-multidb.yaml",
         _flat(*[f"m{i}" for i in range(11)])),
    ]
    for other, (min_a, min_k, _why) in gate._SHIPPED_CONFD_ROOTS.items():
        if other == root:
            continue
        by_root[other] = [(other + "/_defaults.yaml",
                           _flat(*[f"x{i}" for i in range(max(min_k, 1))]))
                          ] * max(min_a, 1)

    # The wide floor is quiet — this is the "before", measured not assumed.
    gate._assert_shipped_roots_intact(by_root)
    # The narrow one is not.
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_loader_readable_floor(by_root)
    msg = str(exc.value)
    assert "WILL ACTUALLY READ" in msg
    assert "2 artifact(s) / 19 key(s)" in msg, (
        "the message must state the wide count too, or the reader cannot tell "
        "why the sibling floor is silent")
    assert "RENAMED" in msg


def test_every_root_stays_loader_readable_catches_the_ticket_reproduction():
    """#1458's own attack was on a bench fixture, which no shipped floor covers.

    ⛔ This arm exists because the sibling floor above is NOT enough: the
    ticket's measured `git mv` was under `tests/e2e-bench/fixture/synthetic-v1`,
    a root with 1 key out of 72 against a global floor of 60 — twelve keys of
    slack, so its whole contribution can vanish inside the rounding.
    """
    victim = "tests/e2e-bench/fixture/synthetic-v1/conf.d"
    assert victim in gate._DEFAULTS_CONFD_ROOTS, "probe root must be pinned"
    all_by_root = {
        r: [(r + "/_defaults.yaml", _flat("k"))]
        for r in gate._DEFAULTS_CONFD_ROOTS}
    # Control: the healthy shape is silent.
    gate._assert_every_root_stays_loader_readable(all_by_root)

    all_by_root[victim] = [(victim + "/_defaults-legacy.yaml", _flat("k"))]
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_stays_loader_readable(all_by_root)
    msg = str(exc.value)
    assert victim in msg
    assert "_defaults-legacy.yaml" in msg, "name the file, not just the root"
    assert "1458" in msg


def test_a_root_absent_entirely_is_left_to_the_fifth_floor():
    """Shadowing, probed from the other side.

    A vanished root must NOT be reported by the sixth floor: the fifth floor
    has a more specific message for it (tree moved / deleted / stopped matching
    the predicate), and two floors answering the same input is how a message
    that fits worse ends up being the one the maintainer reads.
    """
    all_by_root = {
        r: [(r + "/_defaults.yaml", _flat("k"))]
        for r in gate._DEFAULTS_CONFD_ROOTS}
    del all_by_root["tests/golden/fixtures/l0-only/conf.d"]
    gate._assert_every_root_stays_loader_readable(all_by_root)


def test_the_loader_readable_exemption_list_is_empty_and_not_needed():
    """Non-vacuity: the floor must have something to be true of.

    ⛔ An exemption list that grows is how this floor dies quietly, so its
    emptiness is asserted rather than assumed — and asserted TOGETHER with the
    fact that makes emptiness affordable: every pinned root really does carry a
    loader-readable `_defaults.yaml` today. If a legitimate case ever needs an
    entry, this test is the place that has to change with it, in the same
    commit, with the reason.
    """
    assert gate._ROOTS_WITHOUT_LOADER_READABLE_DEFAULTS == frozenset()

    _generators, artifacts = gate._defaults_faces()
    by_root: dict[str, list[str]] = {}
    for face in artifacts:
        rel = gate._artifact_rel(face)
        if rel is None:
            continue
        root = gate.conf_d_root(rel)
        if root is not None:
            by_root.setdefault(root, []).append(rel.rsplit("/", 1)[-1])
    assert by_root, "the scan found no roots — this test would pass vacuously"
    barren = sorted(r for r, names in by_root.items()
                    if not any(gate._is_loader_readable_defaults(n) for n in names))
    assert barren == [], (
        "these roots hold `_defaults*` files that the exporter will never "
        "open; either that is a defect to fix or they need an entry in "
        "_ROOTS_WITHOUT_LOADER_READABLE_DEFAULTS with a reason", barren)


def test_both_new_floors_are_reached_through_the_real_entry(monkeypatch):
    """Wiring, not just behaviour — the failure mode that keeps recurring here.

    ⛔ Each floor is isolated by standing the OTHER one down, because they
    shadow: the shipped-root arm runs first and catches the shipped root's
    input before the per-root arm sees it. Measured — the first version of this
    test asserted the per-root message and got the shipped-root one. A probe
    that does not stand its neighbour down proves the neighbour works.
    """
    real_pred = gate._is_loader_readable_defaults

    def _nothing_is_loader_readable(name, _r=real_pred):
        return False if name.lower() == "_defaults.yaml" else _r(name)

    # (1) The per-root arm, with the shipped-root arm stood down.
    monkeypatch.setattr(gate, "_assert_loader_readable_floor",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(gate, "_is_loader_readable_defaults",
                        _nothing_is_loader_readable)
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    msg = str(exc.value)
    assert "conf.d root(s)" in msg
    assert msg.count("/conf.d") > 1, (
        "the loop must walk the pin, not one hard-coded root", msg)
    assert "1458" in msg
    monkeypatch.undo()

    # (2) The shipped-root arm, with the per-root arm stood down.
    monkeypatch.setattr(gate, "_assert_every_root_stays_loader_readable",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(gate, "_is_loader_readable_defaults",
                        _nothing_is_loader_readable)
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    assert "WILL ACTUALLY READ" in str(exc.value)
    monkeypatch.undo()

    # Control: nothing patched, the real tree is clean through the real entry —
    # so both raises above are the probe's doing, not a broken repo.
    gate._defaults_faces()


def test_the_loader_readable_floor_numbers_have_slack_and_are_not_zero():
    """The NUMBERS in `_LOADER_READABLE_FLOORS`, not just the mechanism.

    ⛔ Added because the mechanism shipped with its values unguarded and the
    gap was measured, not guessed: dropping the exporter root's key floor from
    6 to 0 — which silently deletes that root's protection while leaving every
    line of the floor in place — turned NOTHING in this file red. That is the
    same shape as every other "the guard exists, its threshold does not" defect
    this module has paid for.

    A bracket rather than an equality, for the reason the sibling floors state:
    an exact number would go red on a legitimate edit that ADDS a key, so
    maintainers would learn to edit the table. What is pinned is the two
    properties a floor has to have —

      * not zero (a floor of 0 is not a floor), and
      * STRICTLY below today's measured count (slack, so a legitimate removal
        of one key is not a CI incident and nobody learns to lower it)

    — with the right-hand side DERIVED from the real scan, so this test cannot
    drift from the tree it is about.

    ⚠️ A root that legitimately declares no keys keeps `min_keys == 0`; the
    bracket cannot apply to it, and demanding one would force a fake key into a
    shipped example tree. Its artifact count is what guards it, exactly as the
    sibling table says.
    """
    _generators, artifacts = gate._defaults_faces()
    measured: dict[str, tuple[int, int]] = {}
    for face, keys in artifacts.items():
        rel = gate._artifact_rel(face)
        if rel is None:
            continue
        root = gate.conf_d_root(rel)
        if root not in gate._LOADER_READABLE_FLOORS:
            continue
        if not gate._is_loader_readable_defaults(rel.rsplit("/", 1)[-1]):
            continue
        n_a, n_k = measured.get(root, (0, 0))
        measured[root] = (n_a + 1, n_k + len(keys))

    assert set(measured) == set(gate._LOADER_READABLE_FLOORS), (
        "every root in the table must actually be measurable, or this test "
        "passes vacuously for the ones that are not",
        {"in table": sorted(gate._LOADER_READABLE_FLOORS),
         "measured": sorted(measured)})

    for root, (min_a, min_k, _why) in sorted(gate._LOADER_READABLE_FLOORS.items()):
        got_a, got_k = measured[root]
        assert 1 <= min_a <= got_a, (
            f"{root}: artifact floor {min_a} against {got_a} measured", root)
        if got_k == 0:
            assert min_k == 0, (
                f"{root}: declares no loader-readable keys today, so a "
                f"non-zero key floor of {min_k} would be unsatisfiable")
            continue
        assert 0 < min_k < got_k, (
            f"{root}: key floor {min_k} against {got_k} measured — a floor of "
            f"0 is not a floor, and a floor equal to today's count has no "
            f"slack, which is what teaches people to edit the table")


def _defined_test_names(src: str) -> set[str]:
    """Every `def test_...` in a source text."""
    import re as _re
    return set(_re.findall(r"^def (test_\w+)", src, _re.M))


def _broken_citations(
    sources: tuple[tuple[str, str], ...], defined: set[str],
) -> list[tuple[str, int, str]]:
    """Single-line backtick citations of test names that do not resolve.

    Extracted so the guard below and its POSITIVE CONTROL run the same
    code. See `test_the_citation_guard_can_actually_fail` for why that
    matters.
    """
    import re as _re
    broken: list[tuple[str, int, str]] = []
    for label, src in sources:
        for lineno, line in enumerate(src.splitlines(), 1):
            for cited in _re.findall(r"`(test_\w+)`", line):
                # A citation of a test FILE is a different thing and resolves
                # by path, not by function name.
                if cited.endswith("_py") or f"{cited}.py" in line:
                    continue
                if cited not in defined:
                    broken.append((cited, lineno, label))
    return broken


def test_the_citation_guard_can_actually_fail():
    """Positive control: feed the matcher a broken citation, demand a report.

    ⛔ Surviving mutation (B-tier, mutation review round 3): emptying the
    guard's source list — so it scans nothing at all — left it GREEN, and
    so did narrowing the pattern until it matched nothing. A guard whose
    only assertion is `not broken` is satisfied perfectly by a guard that
    cannot see anything, and this module's whole history is guards that
    were quiet for the wrong reason (`_tracked_paths`' seam note lists two
    more of the same shape).

    ⚠️ The bogus name is ASSEMBLED at runtime rather than written out.
    Spelling it inside backticks in this file would be a real unresolved
    citation, and the guard would correctly turn red on its own control.
    """
    fake = "test_" + "this_name_is_assembled_so_it_is_not_a_citation"
    defined = _defined_test_names(TEST_FILE_SRC)
    assert fake not in defined, "the probe name must not accidentally exist"

    synthetic = "# a docstring claiming coverage: " + chr(96) + fake + chr(96)
    broken = _broken_citations((("synthetic", synthetic),), defined)
    assert [c for c, _l, _s in broken] == [fake], (
        "the matcher must report a single-line backtick citation of a test "
        "that does not exist — without this the real guard passes whenever "
        "it happens to be looking at nothing", broken)

    # ...and it must stay silent on a name that DOES resolve, or "reports
    # something" would be all this control proves.
    real = sorted(defined)[0]
    ok = "# " + chr(96) + real + chr(96)
    assert _broken_citations((("synthetic", ok),), defined) == [], (
        "a citation that resolves must not be reported", real)

    # The convention itself: plain text is a MENTION, not a citation.
    plain = "# " + fake + " was deleted in this PR"
    assert _broken_citations((("synthetic", plain),), defined) == [], (
        "only backticked names are citations — that distinction is what "
        "lets a comment record a removed test without an allowlist")


def test_every_test_name_this_module_cites_actually_exists():
    """A prose claim that names a test must name a REAL one.

    ⛔ The defect this catches is not hypothetical and not rare — it is the
    third instance in one working session. `_is_loader_readable_defaults`'s
    docstring cited test_loader_readable_predicate_mirrors_the_go_test — no
    backticks, per the convention this guard defines below —
    which never existed, and the sentence around it also over-claimed what the
    (differently-named) real test does. Two sibling PRs in the same batch each
    carried one: a docstring saying a boundary was "pinned by the regression
    tests in the README" where the README names no test, and one saying a test
    pins the call site when it pins the callee.

    Why it matters here specifically: this module's guard rails are load-
    bearing prose. Someone deciding whether it is safe to widen a predicate
    reads "X pins the pair", greps for X, finds nothing — and now has to
    reconstruct from scratch what is actually guarded, at the exact moment the
    comment was supposed to save them that. A citation that does not resolve is
    worse than no citation: it asserts coverage that is not there.

    ⚠️ DELIBERATELY UNDER-INCLUSIVE, and that is the whole design. It only
    considers names written inside backticks ON ONE LINE. This module wraps
    long identifiers across comment lines routinely (`test_the_index_listing_is
    _a_real_seam_on_both_derivations` spans two), and a matcher that tried to
    rejoin them would guess — producing false reds whose message states
    something untrue, which is the failure mode this file has paid for more
    than once (#1448). Skipping the ambiguous cases keeps every red this test
    produces literally true. It catches the single-line citation, which is the
    shape that has actually gone wrong.
    """
    defined = _defined_test_names(TEST_FILE_SRC)
    assert defined, "no tests discovered — this guard would pass vacuously"

    broken = _broken_citations(
        (("check_threshold_reachability.py", GATE_SRC),
         ("test_check_threshold_reachability.py", TEST_FILE_SRC)),
        defined)

    assert not broken, (
        "prose in this module cites test(s) that do not exist — either the "
        "test was renamed and the citation was not, or the citation was "
        "written for a test that was never added. Both mean the sentence "
        "around it is claiming coverage nobody can find.\n"
        "  ⛔ IF THE NAME IS ABSENT ON PURPOSE — recording that a test was "
        "deleted, or quoting a citation that turned out to be wrong — write "
        "it WITHOUT backticks. Backticks are what make it a CITATION; plain "
        "text is a MENTION. That distinction is the whole convention, and it "
        "needs no allowlist to maintain",
        [f"{label}:{lineno} cites `{cited}`" for cited, lineno, label in broken])


def _healthy_loader_readable_by_root() -> dict[str, list]:
    """A `by_root` every entry of `_LOADER_READABLE_FLOORS` is satisfied by."""
    by_root: dict[str, list] = {r: [] for r in gate._SHIPPED_CONFD_ROOTS}
    for root, (min_a, min_k, _why) in gate._LOADER_READABLE_FLOORS.items():
        n_files = max(min_a, 1)
        # Spread the required keys over the required files, with one spare
        # so the fixture itself is not sitting exactly on the floor.
        per_file = [_flat(*[f"{root}-f{i}-k{j}" for j in range(min_k + 1)])
                    if i == 0 else _flat()
                    for i in range(n_files)]
        by_root[root] = [(f"{root}/_defaults.yaml" if i == 0
                          else f"{root}/sub{i}/_defaults.yaml", keys)
                         for i, keys in enumerate(per_file)]
    return by_root


@pytest.mark.parametrize("root", sorted(gate._LOADER_READABLE_FLOORS))
def test_the_loader_readable_floor_visits_every_root_in_its_table(root):
    """Each entry of the table must be enforced, not just the first one.

    ⛔ Surviving mutations (B-tier, mutation review round 3): iterating only
    `sorted(...)[:1]` — and, separately, skipping one root — left the suite
    green. Every existing test breaks the SAME root (the exporter's), which
    happens to sort first, so a loop that stops after one iteration answers
    all of them correctly.

    A table with three entries and one enforced entry is two silent roots
    wearing a floor's name, which is the "the guard exists, its threshold
    does not" shape this module has paid for repeatedly.
    """
    healthy = _healthy_loader_readable_by_root()
    gate._assert_loader_readable_floor(healthy)  # control: nothing fires

    broken = _healthy_loader_readable_by_root()
    # Rename every loader-readable file under THIS root only — #1458's
    # attack, which takes the narrow count to 0/0 while the wide count is
    # untouched.
    broken[root] = [(rel.replace("_defaults.yaml", "_defaults-legacy.yaml"), keys)
                    for rel, keys in broken[root]]
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_loader_readable_floor(broken)
    assert root in str(exc.value), (
        "the violation must name the root that broke", str(exc.value)[:400])


@pytest.mark.parametrize("root", sorted(gate._DEFAULTS_CONFD_ROOTS))
def test_every_pinned_root_is_actually_visited_by_the_rename_arm(root):
    """Same coverage question for the twelve-root arm.

    ⛔ Surviving mutation (B-tier): skipping a root in
    `_assert_every_root_stays_loader_readable`'s loop left the suite green —
    the only root any existing test breaks is `synthetic-v1` (the ticket's
    own reproduction). A loop that skips any OTHER root still answers that
    test correctly, and this arm exists precisely because the roots nobody
    thinks about are the ones the shipped floors do not cover.
    """
    healthy = {r: [(r + "/_defaults.yaml", _flat("k"))]
               for r in gate._DEFAULTS_CONFD_ROOTS}
    gate._assert_every_root_stays_loader_readable(healthy)  # control

    broken = dict(healthy)
    broken[root] = [(root + "/_defaults-legacy.yaml", _flat("k"))]
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_every_root_stays_loader_readable(broken)
    assert root in str(exc.value), (
        "the violation must name the root that broke", str(exc.value)[:400])


def test_the_loader_readable_floor_needs_both_halves_not_either():
    """`artifacts >= floor AND keys >= floor`, not `OR`.

    ⛔ Surviving mutation (A-tier): flipping that `and` to `or` left the
    suite green. Every existing breach takes BOTH counts to zero at once
    (the #1458 rename removes the file, so its keys go with it), and either
    operator reports that identically. The shapes that separate them are the
    one-sided ones, and both are reachable:

      * keys short, artifacts fine — a `_defaults.yaml` that stays put while
        its `defaults:` block is emptied out;
      * artifacts short, keys fine — a root whose floor is about FILES (the
        recipe examples declare no keys at all, so `min_keys` is 0 there and
        the artifact count is the only thing guarding it).

    Under `or` each of these satisfies the other half and is waved through.
    """
    # (a) keys short, artifacts satisfied — the exporter root, whose floor
    #     is 1 artifact / 6 keys.
    keys_short = _healthy_loader_readable_by_root()
    root = "components/threshold-exporter/config/conf.d"
    min_a, min_k, _why = gate._LOADER_READABLE_FLOORS[root]
    assert min_a >= 1 and min_k >= 1, (
        "this probe needs a root whose floor constrains both", root)
    keys_short[root] = [(root + "/_defaults.yaml",
                         _flat(*[f"k{i}" for i in range(min_k - 1)]))]
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_loader_readable_floor(keys_short)
    assert root in str(exc.value)

    # (b) artifacts short, keys satisfied — the recipe examples, floor
    #     2 artifacts / 0 keys, so any file count below 2 breaches while
    #     the key half is trivially true.
    files_short = _healthy_loader_readable_by_root()
    rroot = "rule-packs/recipes/examples/conf.d"
    rmin_a, rmin_k, _rwhy = gate._LOADER_READABLE_FLOORS[rroot]
    assert rmin_a >= 2 and rmin_k == 0, (
        "this probe needs a root guarded by its FILE count alone", rroot)
    files_short[rroot] = [(rroot + "/_defaults.yaml", _flat("k"))]
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_loader_readable_floor(files_short)
    assert rroot in str(exc.value)


def test_the_breach_message_says_which_numbers_are_now_and_which_are_the_floor():
    """Two number pairs in one sentence; swapping them stays true-looking.

    ⛔ Surviving mutation (A-tier): exchanging `{len(found)}/{n_keys}` with
    `{min_artifacts}/{min_keys}` in the violation text left the suite green.
    The existing assertions check the WIDE pair and some prose, and a
    swapped message is still a grammatical English sentence with four
    plausible numbers in it — so the only reader who notices is the
    maintainer who acts on it, at the moment they are trying to work out
    whether the tree shrank or the table grew.
    """
    root = "components/threshold-exporter/config/conf.d"
    min_a, min_k, _why = gate._LOADER_READABLE_FLOORS[root]
    by_root = _healthy_loader_readable_by_root()
    by_root[root] = [(root + "/_defaults-legacy.yaml",
                      _flat(*[f"k{i}" for i in range(min_k + 2)]))]

    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_loader_readable_floor(by_root)
    msg = str(exc.value)
    assert f"{0} artifact(s) / {0} key(s) THAT THE EXPORTER" in msg, (
        "the FIRST pair is what the tree contributes right now", msg[:400])
    assert f"floor of {min_a} artifact(s) / {min_k} key(s)" in msg, (
        "the SECOND pair is the floor from the table", msg[:400])
    assert f"floor of {0} artifact(s)" not in msg, (
        "swapped: the floor clause is carrying the measured numbers", msg[:400])


def test_the_two_loader_floors_are_bracketed_by_what_the_sibling_cannot_see():
    """The NUMBERS, bounded below by the erosion the wide floor is blind to.

    ⛔ The bracket that shipped with this table (`0 < min_k < measured`)
    could not see a mutation from 6 to 1: a floor of 1 still catches #1458's
    rename, because the rename takes the count to 0. What it stops catching
    is EROSION — keys disappearing out of a `_defaults.yaml` that stays
    where it is — and that is the half of this floor's job the sibling
    cannot do.

    So the lower bound is derived from the sibling rather than from a
    percentage of today's count (a percentage is a ratchet, which #1411
    locked this project out of):

      * where a root has keys the loader will NOT read, the wide floor has
        slack — `wide_total − wide_floor` — and erosion inside that band is
        invisible to it. This floor has to fire first, so
        `min_k > narrow_total − wide_slack`.
      * where a root is entirely loader-readable, the table says in so many
        words that this floor "mirrors its sibling entry" — there is no band
        for it to own, and the assertion is that the mirror is intact. That
        also pins the artifact halves, which are otherwise unconstrained
        above 1.

    ⚠️ The bound MOVES WITH THE TREE, in the same way the sibling bracket
    does — and an earlier revision of this docstring disclosed only half of
    that, which a blind reviewer caught. Both directions, measured:

      (a) adding loader-readable keys raises `narrow_total`, so the bound
          rises. Disclosed from the start.
      (b) removing keys from the part of the root the loader does NOT read
          shrinks the sibling's slack, which raises the bound too. NOT
          disclosed, and it is the surprising one: it is an edit to a file
          this floor is not about. Measured — deleting 2 of the 11 keys in
          `examples/_defaults-multidb.yaml` leaves the production gate at
          rc=0 and turns this test red.

    ⛔ (b) is a TRUE positive, not collateral damage. A smaller blind band
    means the sibling tolerates less erosion, so the window in which BOTH
    floors stay silent moves up: with slack 2 instead of 4, loader-readable
    keys eroding 8 -> 6 leaves the wide count at exactly the sibling's floor
    and a `min_keys` of 6 silent as well. The remedy is to RAISE the floor.
    ⚠️ Nor is it a new maintenance burden this test invented: the same edit
    already turns `test_the_headroom_note_states_both_directions_and_both_are_current`
    red, because that root's blind-side key count is pinned prose already.
    """
    _generators, artifacts = gate._defaults_faces()
    wide: dict[str, tuple[int, int]] = {}
    narrow: dict[str, tuple[int, int]] = {}
    for face, keys in artifacts.items():
        rel = gate._artifact_rel(face)
        if rel is None:
            continue
        root = gate.conf_d_root(rel)
        if root not in gate._LOADER_READABLE_FLOORS:
            continue
        a, k = wide.get(root, (0, 0))
        wide[root] = (a + 1, k + len(keys))
        if gate._is_loader_readable_defaults(rel.rsplit("/", 1)[-1]):
            a, k = narrow.get(root, (0, 0))
            narrow[root] = (a + 1, k + len(keys))

    assert set(narrow) == set(gate._LOADER_READABLE_FLOORS), (
        "every root in the table must be measurable or this passes vacuously",
        {"table": sorted(gate._LOADER_READABLE_FLOORS), "measured": sorted(narrow)})

    mirrored, owned = [], []
    for root, (min_a, min_k, _why) in sorted(gate._LOADER_READABLE_FLOORS.items()):
        wide_a, wide_k = wide[root]
        narrow_a, narrow_k = narrow[root]
        sib_a, sib_k, _sw = gate._SHIPPED_CONFD_ROOTS[root]
        if (wide_a, wide_k) == (narrow_a, narrow_k):
            mirrored.append(root)
            assert (min_a, min_k) == (sib_a, sib_k), (
                f"{root} is entirely loader-readable, so the table's own "
                f"reason for its numbers is that they mirror the sibling "
                f"entry {(sib_a, sib_k)} — this floor owns no band of its "
                f"own here. Got {(min_a, min_k)}; change both or neither")
            continue
        owned.append(root)
        sib_slack = wide_k - sib_k
        blind_band = narrow_k - sib_slack
        assert blind_band < min_k <= narrow_k, (
            f"{root}: key floor {min_k} must sit strictly above {blind_band} "
            f"and at or below today's {narrow_k}.\n"
            f"  Where {blind_band} comes from: {narrow_k} loader-readable "
            f"key(s) minus the sibling floor's slack of {sib_slack} "
            f"({wide_k} wide key(s) against its floor of {sib_k}). Erosion "
            f"that stops at {blind_band} leaves the wide count at exactly "
            f"{sib_k}, so the sibling stays silent — this floor is the only "
            f"one left, and a floor at or below {blind_band} is silent too.\n"
            f"  ⛔ TWO EDITS MOVE THIS BOUND, and only one of them is about "
            f"this floor's subject:\n"
            f"    (a) ADDING loader-readable keys raises {narrow_k}, so the "
            f"bound rises. Expected; raise {min_k} in the same commit.\n"
            f"    (b) REMOVING keys from the part of this root the loader "
            f"does NOT read shrinks the sibling's slack, which ALSO raises "
            f"the bound. ⚠️ That looks unrelated and is not: a smaller "
            f"blind band means the sibling tolerates less, so the window "
            f"where BOTH floors are silent moves up. The hole is real, and "
            f"the remedy is to raise {min_k} — not to relax this test.\n"
            f"  ⛔ LOWERING {min_k} IS NEVER THE REMEDY. A floor inside the "
            f"band still catches a RENAME (which takes the count to 0) and "
            f"stops catching keys draining out of a file that stays put, "
            f"which is the half of this floor's job the wide one cannot do")
        assert 1 <= min_a <= narrow_a, (
            f"{root}: artifact floor {min_a} against {narrow_a} measured")

    assert mirrored and owned, (
        "this test needs at least one root of each kind, or half of it is "
        "vacuous", {"mirrored": mirrored, "owned": owned})


# ── the witness runs after every floor ───────────────────────────────────────

# The assertions `_defaults_faces()` calls BEFORE the schema witness. Hand-kept
# so the parametrisation reads; the table is held to the gate's own source by
# `test_the_floor_table_matches_what_the_real_entry_actually_calls`, so the two
# cannot drift.
_FLOORS_BEFORE_THE_WITNESS = (
    "_assert_shipped_roots_intact",
    "_assert_loader_readable_floor",
    "_assert_every_root_contributes",
    "_assert_every_root_stays_loader_readable",
    "_assert_keys_floor",
)


def _floors_the_real_entry_calls_before_the_witness() -> list[str]:
    """The `_assert_*` calls in `_defaults_faces`, read off the gate's source."""
    assert GATE_SRC.count("def _defaults_faces(") == 1
    body = GATE_SRC.split("def _defaults_faces(", 1)[1]
    witness = "judged = _assert_defaults_artifacts_match_schema"
    assert witness in body, "the witness call moved or was renamed"
    return re.findall(r"^    (_assert_\w+)\(", body.split(witness, 1)[0], re.M)


def test_the_floor_table_matches_what_the_real_entry_actually_calls():
    """A floor added above the witness must join the table, not slip past it.

    ⛔ Without this, the parametrisation below is only as good as someone
    remembering to extend a tuple — and the defect it exists for is EXACTLY
    that kind of forgetting: the gate comment said "the four floors" while the
    call list held three, then went on saying it once #1458 made it five
    (blind review, round 4).
    """
    derived = _floors_the_real_entry_calls_before_the_witness()
    assert derived, (
        "extraction returned nothing — the regex or the anchor is stale, and a "
        "silently empty list would make this test and the parametrised one "
        "below vacuous together")
    assert derived == list(_FLOORS_BEFORE_THE_WITNESS), (
        "`_FLOORS_BEFORE_THE_WITNESS` no longer matches the calls in "
        "`_defaults_faces`. A floor added before the witness must be added "
        "here too, or nothing checks that the witness stays behind it.",
        {"in_source": derived, "in_table": list(_FLOORS_BEFORE_THE_WITNESS)})


@pytest.mark.parametrize("floor_name", _FLOORS_BEFORE_THE_WITNESS)
def test_every_floor_that_runs_before_the_witness_is_still_heard_over_it(
        monkeypatch, floor_name):
    """Breach this floor AND the witness; the floor must be the one heard.

    ⛔ Blind review (#1458, round 4) measured that the sibling test below —
    which rigs the SECOND floor specifically — buys much less than its name
    promises. Two reorderings left the whole suite green:

        witness moved to just after `_assert_loader_readable_floor`
            → 156 passed, rc=0; floors 3, 4 and 5 all lose their voice
        witness swapped with `_assert_keys_floor` alone — the adjacent pair,
        the one a refactor is likeliest to disturb
            → 156 passed, rc=0; floor 5 loses its voice

    Both were measured through `_defaults_faces()` by standing one floor down
    and reading which message reached the caller: on the real code every floor
    in the table speaks; under either reordering the witness speaks instead.
    The property is therefore per-floor, and this is parametrised over the
    table.

    ⚠️ WHAT THIS DOES NOT PIN: the order of the floors AMONG THEMSELVES. Each
    case stands exactly one floor down, so any permutation of the others is
    invisible here. One such difference is measured and real — see the
    disclosure block near the top of this file.

    Vacuity: `monkeypatch.setattr` defaults to `raising=True`, so a floor this
    table names but the gate no longer has fails here instead of dropping out.
    """
    sentinel = f"SENTINEL-{floor_name}"

    monkeypatch.setattr(gate, "_assert_defaults_artifacts_match_schema",
                        lambda *_a, **_k: 0)

    def _this_floor_speaks(*_a, **_k):
        raise gate._GateViolation(sentinel)

    monkeypatch.setattr(gate, floor_name, _this_floor_speaks)

    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    msg = str(exc.value)
    assert sentinel in msg, (
        f"`{floor_name}` was breached and so was the witness, but the caller "
        "did not read this floor. Either the witness now runs before it — in "
        "which case a maintainer meets a generic count instead of this floor's "
        "remedy — or the real entry stopped calling this floor at all.",
        msg[:400])


def test_the_schema_witness_speaks_last_so_a_floor_is_heard_first(monkeypatch):
    """The loader-readable floor's REAL message survives a breached witness.

    ⛔ Surviving mutation (C-tier, mutation review round 3): moving the schema
    witness and its `_DEFAULTS_ARTIFACT_FLOOR` check out of the END of
    `_defaults_faces` left the suite green. Its comment meanwhile states the
    position is load-bearing — the witness would speak INSTEAD of the floors
    on the roots they already cover, and their messages carry remedies its own
    does not. That is a behavioural claim about WHICH diagnosis a maintainer
    reads, and nothing enforced it.

    ⛔ THE LANDING SPOT IS PART OF THE CLAIM. "To the front" has two readings
    and they do not measure the same, so both are stated rather than one being
    passed off as "the" result (blind review, #1458 round 4):

        inserted before `_assert_shipped_roots_intact`, i.e. at the head of
        the floor sequence              → 1 failed, 155 passed
        inserted after `scanned = _tracked_defaults_artifacts()`, i.e. at the
        head of the function body       → 3 failed, 153 passed
            (the two extra were `test_an_empty_artifact_scan_is_an_error_not_a_pass`
             and `test_every_floor_breach_is_a_violation_not_a_crash[scan]`)

    ⛔ THIS TEST RIGS THE SECOND FLOOR ONLY, which is much less than its name
    suggests; blind review measured two reorderings it does not catch. The
    general property is covered per-floor by
    `test_every_floor_that_runs_before_the_witness_is_still_heard_over_it`.
    What this one adds on top is that the floor's REAL message — not a
    sentinel — is what reaches the caller.

    ⚠️ An earlier wording here claimed that swapping
    `_assert_every_root_contributes` with
    `_assert_every_root_stays_loader_readable` is an EQUIVALENT mutation,
    because the latter has an explicit `continue` for an absent root "so their
    domains do not overlap". ⛔ THAT ARGUMENT IS WRONG and the claim is false.
    The `continue` hands over ONE arm — the root that vanished entirely. The
    former also has a zero-keys arm, and a root whose file was renamed AND
    whose `defaults:` section was renamed trips both; measured on that input,
    each raises its own, different message. The swap is green today, which
    makes it an untested behavioural difference rather than an equivalence. It
    is disclosed near the top of this file, not guarded here.

    The property, therefore: rig BOTH a floor breach and a witness breach at
    once, and the caller must be told the floor's story — the one that names
    the remedy and warns against lowering the number.
    """
    witness_calls = []

    def _witness_is_starving(*_a, **_k):
        witness_calls.append(1)
        return 0  # below _DEFAULTS_ARTIFACT_FLOOR, so the witness would fire

    assert 0 < gate._DEFAULTS_ARTIFACT_FLOOR, (
        "the rig depends on 0 being below the floor", gate._DEFAULTS_ARTIFACT_FLOOR)

    # (1) Non-vacuity: the witness really is reachable and the rig really does
    #     trip it, with every floor left alone and the tree otherwise clean.
    monkeypatch.setattr(gate, "_assert_defaults_artifacts_match_schema",
                        _witness_is_starving)
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    assert "the schema witness judged only" in str(exc.value), (
        "precondition: the rig must be able to trip the witness on its own, "
        "or part (2) proves nothing", str(exc.value)[:300])
    assert witness_calls, "the witness stub was never reached in part (1)"
    monkeypatch.undo()
    witness_calls.clear()

    # (2) The property: with a FLOOR also breached, the floor speaks.
    real_pred = gate._is_loader_readable_defaults

    def _nothing_is_loader_readable(name, _r=real_pred):
        return False if name.lower() == "_defaults.yaml" else _r(name)

    monkeypatch.setattr(gate, "_assert_defaults_artifacts_match_schema",
                        _witness_is_starving)
    monkeypatch.setattr(gate, "_is_loader_readable_defaults",
                        _nothing_is_loader_readable)
    with pytest.raises(gate._GateViolation) as exc:
        gate._defaults_faces()
    msg = str(exc.value)
    assert "the schema witness judged only" not in msg, (
        "the witness spoke INSTEAD of a floor that was also breached — it must "
        "run LAST, so a maintainer meets the most specific diagnosis available "
        "and the remedy warnings that go with it", msg[:400])
    assert "WILL ACTUALLY READ" in msg, (
        "the loader-readable floor is the one that should have spoken here",
        msg[:400])
    assert "LOWERING THESE NUMBERS IS NOT A REMEDY" in msg, (
        "and its message carries a warning the witness's does not — which is "
        "the whole reason the order was chosen", msg[:400])
    # ⛔ MEASURED, not inferred. The three asserts above pass just as happily
    # with no witness stub installed at all, so "both were breached at once"
    # rested on the reader trusting part (1). The spy makes the ordering itself
    # observable: the witness must not have been reached.
    assert not witness_calls, (
        "the witness RAN before the breached floor raised, so the asserts "
        "above say nothing about order", witness_calls)
    monkeypatch.undo()

    # Control: unpatched, the real tree is clean through the real entry, so
    # both raises above are the rig's doing rather than a broken repo.
    gate._defaults_faces()
