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

def test_main_without_ci_is_report_only(monkeypatch):
    """Bare `main([])` never fails on errors — it is report-only, so a green
    local commit is not gated on the (grandfathered) unwired set."""
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["some drift"], "infos": []})
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    """`--ci` escalates any error (new unreachable OR exit-lock breach) to
    EXIT_VIOLATION — this is what the CI hook relies on."""
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["a breach"], "infos": []})
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    """`--ci` returns 0 when there are no errors, even with INFO-level
    grandfathered entries present."""
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": [], "infos": ["18 known-unwired"]})
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
