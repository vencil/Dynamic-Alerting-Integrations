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

import importlib.util
import re

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
                  "artifacts_without_defaults": -1},
    }


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
        "artifacts_without_defaults"}, sorted(result["stats"])


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
    assert len(non_shipped) >= 2, non_shipped
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
    """Each field, against an independent recount of the same real faces."""
    generators, artifacts = gate._defaults_faces()
    stats = gate.run_check(demand=set(), supply=set(), deferred=set(),
                           known_unwired={})["stats"]

    assert stats["generator_faces"] == len(generators)
    assert stats["artifact_faces"] == len(artifacts)
    assert stats["generator_keys"] == sum(len(v) for v in generators.values())
    assert stats["artifact_keys"] == sum(len(v) for v in artifacts.values())
    assert stats["artifacts_without_defaults"] == sum(
        1 for v in artifacts.values() if not v)
    # …and none of them may be trivially equal to another, or a copy-paste
    # between fields would satisfy every line above.
    assert stats["generator_faces"] != stats["artifact_faces"]
    assert stats["generator_keys"] != stats["artifact_keys"]


def test_main_prints_the_coverage_line_on_every_run(capsys):
    """It is printed pass OR fail — that is the whole basis for not writing the
    number in a comment. Asserted on `main()`'s real output, with the numbers
    cross-checked against a live recount."""
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
    kept = [rel for rel in gate._tracked_defaults_artifacts()
            if not rel.startswith("tests/e2e-bench/")]
    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / r for r in kept])


@pytest.mark.parametrize("trip", [
    _trip_scan_floor, _trip_read_floor, _trip_shipped_root_floor,
    _trip_generator_key_floor, _trip_artifact_key_floor,
], ids=["scan", "read", "shipped-root", "generator-keys", "artifact-keys"])
def test_every_floor_breach_is_a_violation_not_a_crash(monkeypatch, capsys, trip):
    """⛔ Exit-code semantics, per `scripts/tools/_lib_exitcodes.py`: 1 is "the
    tool ran correctly and found something the USER must act on", 2 is "the tool
    could NOT do its job". A deleted shipped `_defaults.yaml` is the first, and
    reporting it as `crashed` / rc=2 tells the reader their environment is
    broken — which is read as flaky-lint-skip-it.

    ⛔ EVERY floor, parametrised. A single case that tripped only the scan floor
    left the other three free to go back to a bare `RuntimeError`: mutation put
    the shipped-root floor back on the crash path and nothing went red, because
    no test walked that one.
    """
    trip(monkeypatch)
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "crashed" not in err, err[-1500:]
    assert "floor breach" in err, err[-1500:]


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


def test_the_artifact_key_floor_sees_a_group_stop_yielding(monkeypatch):
    """End-to-end, on the real tree, with the FILE count still above both file
    floors — which is the case those two floors structurally cannot see."""
    tracked = gate._tracked_defaults_artifacts()
    kept = [rel for rel in tracked if not rel.startswith("tests/e2e-bench/")]
    assert len(kept) >= gate._DEFAULTS_ARTIFACT_FLOOR, kept

    monkeypatch.setattr(gate, "_tracked_defaults_artifacts", lambda: kept)
    monkeypatch.setattr(gate, "_defaults_artifacts",
                        lambda: [gate.PROJECT_ROOT / rel for rel in kept])
    with pytest.raises(RuntimeError, match="ARTIFACT faces yielded"):
        gate.run_check(demand=set(), supply=set(), deferred=set(), known_unwired={})


def test_each_key_floor_is_bracketed_by_what_it_has_to_catch():
    """⛔ The floors need an UPPER and a LOWER bound, and "greater than zero" is
    neither.

    Measured (blind review, mutation): with only `>= floor` and `floor > 0`
    asserted, setting the generator floor to 2, to 51, or to today's exact 102
    all left the suite green — and the name of the previous test said "with
    room" while its body could not see any of them. So the bracket is derived
    from the two things the floor is FOR:

      lower — it must still fire when the biggest single producer / conf.d root
              stops yielding, i.e. floor > (total − biggest group);
      upper — it must have room left, i.e. floor < today's total. A floor at
              exactly today's value goes red on the next legitimate deletion,
              which trains people to edit floors.
    """
    generators, artifacts = gate._defaults_faces()

    g_total = sum(len(v) for v in generators.values())
    g_biggest = max(len(v) for v in generators.values())
    assert g_total - g_biggest < gate._DEFAULTS_GENERATOR_KEYS_FLOOR < g_total, (
        f"generator floor {gate._DEFAULTS_GENERATOR_KEYS_FLOOR} must sit strictly "
        f"between {g_total - g_biggest} (the biggest producer, {g_biggest} keys, "
        f"going silent) and today's {g_total}")

    groups = _artifact_groups(artifacts)
    a_total = sum(groups.values())
    a_biggest = max(groups.values())
    assert a_total - a_biggest < gate._DEFAULTS_ARTIFACT_KEYS_FLOOR < a_total, (
        f"artifact floor {gate._DEFAULTS_ARTIFACT_KEYS_FLOOR} must sit strictly "
        f"between {a_total - a_biggest} (the biggest conf.d root, {a_biggest} "
        f"keys, going silent) and today's {a_total}; groups={groups}")


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
        entry = cfg.split(f"id: {hook_id}\n", 1)[1].split("language:", 1)[0]
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
    search_dirs = [REPO_ROOT / "scripts" / "tools" / "ops",
                   REPO_ROOT / "scripts" / "tools"]
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
