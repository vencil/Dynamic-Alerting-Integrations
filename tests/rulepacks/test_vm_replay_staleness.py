"""VictoriaMetrics ``vmalert -replay`` STORAGE-STALENESS characterization bench
(#947; ADR-025 backend-compatibility, Part 2 — on-demand).

Role (read first)
-----------------
The per-PR gate (``test_vm_alert_parity.py`` — ``vmalert-tool unittest``) and the
on-demand engine-equivalence anchor (``test_vm_backend_parity.py`` — dense
fixed-epoch series against a real vmsingle) BOTH explicitly defer ONE axis:
real-TSDB **staleness / scrape-gap timing**. Their in-memory / dense-series models
cannot reproduce *when* a series is considered "gone" after a real gap, which
governs (a) when a firing value-alert RESOLVES across a gap and (b) when an
absence-based sentinel FIRES. This bench closes that honest residual: it drives
synthetic histories containing real gaps through ``vmalert -replay`` against a real
vmsingle — the SAME query path production vmalert uses — and characterizes the
divergence against a Prometheus reference (promtool).

Why characterize, not enforce parity
-------------------------------------
MetricsQL derives staleness from the sample INTERVAL (≈1–2 scrape intervals); Prometheus
uses a fixed 5m lookback. They diverge BY DESIGN across gaps, so parity is the wrong bar.
The value is a *pinned characterization*: a soak dual-run WILL see VM fire/resolve
staleness-driven alerts off from Prometheus, and this bench is the machine-checked
explanation of by how much and in which direction (see
docs/integration/victoriametrics-integration.md §3.2). A material drift in VM's staleness
timing (e.g. an engine-version bump) fails these pins.

⚠️ The magnitude is INTERVAL-COUPLED, not a universal constant (Gemini #968): because VM
staleness tracks the sample interval, the VM-vs-Prometheus delta shrinks as the scrape
interval grows (measured VM absent() first-fire after a last sample at +300s: 15s→+330,
30s→+345, 60s→+375). These cases pin the 30s-interval benchmark; the doc states the coupling.

Cases (materialization parity: one logical history -> vmsingle import AND promtool fixture)
  TC2 Staleness    : a gauge stops reporting (end gap). VM resolves the value-alert &
                     fires ``absent()`` ≈1–2 scrape intervals after the last sample (~+360s
                     at 30s); Prometheus holds ~5m (~+600s — the 3.x left-open lookback
                     drops the +300s last sample exactly at +600). => VM ~240-270s EARLIER
                     at 30s (absence sentinels like the ADR-025 watchdog are "noisier" on VM).
                     MITIGATION control: ``absent_over_time(X[5m])`` fires ~+600s on VM too —
                     i.e. a customer who finds ``absent(X)`` too noisy on VM can restore
                     Prometheus-parity by changing the alert syntax (Gemini #968; documented).
  TC1 Threshold+Gap: a value-alert with ``for:3m`` across a mid-series missed scrape. VM
                     stales -> the ``for:`` timer resets -> fires late (~+480s); Prometheus
                     carries the last value 5m across the gap -> ``for:`` keeps accumulating
                     -> fires ~+180s.  => VM ~300s LATER, i.e. UNDER-fires a ``for:``-gated
                     value alert across a gap.

NOT covered here: ``rate()`` / ``increase()`` cold-start extrapolation. That is an
ENGINE-MATH divergence (MetricsQL vs PromQL), reproduced IDENTICALLY on ``vmalert-tool``
and on real vmsingle (measured: 3.333 vs Prometheus 1.667, storage-path invariant), so it
is already owned by gate A (``test_vm_alert_parity.py`` catalog + teeth-test).

Determinism / faithfulness
--------------------------
* Fixed epoch ``T0``; vmsingle MUST run ``-retentionPeriod=100y`` (default 1-month
  retention silently drops the 2023 ``T0`` samples — import 204s but query is empty).
* One GAP-separated window per case. Isolation is by a UNIQUE per-run ``run_id`` label on
  every synthetic series (Gemini #968): each run writes physically new series, so no
  cross-run interference — and we do NOT rely on ``/api/v1/admin/tsdb/delete_series`` (VM
  deletes are async tombstones + background compaction; a delete->re-import->query race on
  slow dev-container IO could flake). The ``run_id`` also propagates into ``ALERTS`` (it is
  carried by the alert expressions' output labels), so ALERTS reads are per-run scoped.
* promtool ``_`` = a MISSING sample (Prometheus 5m lookback carries the last value across
  it) — NOT a stale marker; so a mid-series gap (TC1) and a series that simply ends (TC2)
  are both faithful Prometheus references.
* The promtool reference fixtures are aligned to **promtool 3.x lookback semantics** (the
  CI pin — see the nightly workflow's ``PROM_VERSION``): Prometheus 3.0 made the 5m
  lookback window left-open, so a sample sitting EXACTLY on the window's lower boundary is
  excluded. TC2's flip therefore lands exactly at +600s (last sample +300s + 5m). On
  promtool **2.x** the boundary sample is still included → the +600s (10m) expectations
  are INVERTED (2.x still sees the series at 10m; flip only at +630/+660) — a 2.x run
  failing TC2's Prometheus-reference assert is EXPECTED version skew, not fixture drift.

Provisioning: needs a real vmsingle at ``$VM_REPLAY_ENDPOINT`` (default
``http://localhost:8428``), the ``vmalert`` binary (``$VMALERT`` / PATH / the
dev-container ``/tmp/vm/vmalert-prod``) and ``promtool``. Skips when any is missing (the
normal local / python-tests case); set ``VM_REPLAY_REQUIRE=1`` to force-run (a missing
dependency then HARD-fails instead of skipping to green). This is an on-demand bench, not
a per-PR CI job — run it in a dev-container with a pinned vmsingle, or on a VM-version bump.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vm_harness import (  # shared #968 harness — see vm_harness.py module docstring
    STEP,
    T0,
    VMClient,
    alert_offsets,
    find_vmalert,
    new_run_id,
    replay,
)

VM = os.environ.get("VM_REPLAY_ENDPOINT", "http://localhost:8428")
_VM = VMClient(VM)
# T0 (fixed epoch base, never `now`) + STEP (the pinned 30s benchmark interval) are the
# shared vm_harness constants — the interval-coupling caveat above pins STEP=30 semantics.
CASE_GAP = 100_000          # s between per-case windows (>> any window; no cross-talk)
_REQUIRE = os.environ.get("VM_REPLAY_REQUIRE") == "1"
# Unique per pytest-process isolation tag → each run writes physically-new series (no delete,
# no tombstone race — Gemini #968). Module-level new_run_id() call keeps the original
# per-module-run uniqueness semantics; assertions pin OFFSETS, not the tag.
_RUN = new_run_id()

_VMALERT = find_vmalert()
_PROMTOOL = shutil.which("promtool")
_missing = (
    # VM reachability is INDEPENDENT of _REQUIRE: under VM_REPLAY_REQUIRE=1 with vmsingle
    # down, _require_deps_or_fail() must hard-fail with the clear "no VictoriaMetrics"
    # message, not proceed and blow up later with a raw urllib error (CodeRabbit #968).
    "no VictoriaMetrics" if not _VM.reachable() else
    "no vmalert binary" if _VMALERT is None else
    "no promtool" if _PROMTOOL is None else None
)
pytestmark = pytest.mark.skipif(
    not _REQUIRE and _missing is not None,
    reason=f"{_missing} (on-demand replay bench; start a pinned vmsingle -retentionPeriod=100y, "
           f"provide vmalert + promtool, or VM_REPLAY_REQUIRE=1 to force — see "
           f"docs/integration/victoriametrics-integration.md §3.2)",
)


def _require_deps_or_fail() -> None:
    if _REQUIRE and _missing is not None:
        pytest.fail(f"VM_REPLAY_REQUIRE=1 but {_missing} — the replay bench must not "
                    f"silently skip to green (vmsingle not started / binary absent?)")


# ---- thin bindings onto the shared vm_harness (module state: _VM/_RUN/_VMALERT) ----
def _alert_offsets(name: str, state: str, w_start: int, w_end: int) -> list[int]:
    # run_id-scoped: ALERTS inherit run_id from the alert expressions' output labels, so this
    # read only sees THIS run's alerts even on a shared, never-deleted vmsingle.
    return alert_offsets(_VM, name, state, w_start, w_end, run_id=_RUN)


def _replay(rules_text: str, w_start: int, w_end: int, tmp: Path, tag: str) -> None:
    replay(_VMALERT, rules_text, w_start, w_end, tmp, tag, datasource_url=VM)
    _VM.flush()   # make the replay-written ALERTS series queryable


def _promtool_ok(fixture_text: str, rules_text: str, tmp: Path, tag: str) -> subprocess.CompletedProcess:
    (tmp / f"{tag}_pr_rules.yml").write_text(rules_text, encoding="utf-8")
    (tmp / f"{tag}_pr_test.yml").write_text(fixture_text, encoding="utf-8")
    return subprocess.run([_PROMTOOL, "test", "rules", f"{tag}_pr_test.yml"],
                          cwd=str(tmp), capture_output=True, text=True, timeout=60)


# ===========================================================================
# TC2 — staleness: value-alert resolve + absence() fire across an END gap
#       (+ absent_over_time mitigation control)
# ===========================================================================
def test_tc2_staleness_resolve_and_absence(tmp_path):
    """VM stales ≈1–2 scrape intervals after the last sample; Prometheus holds ~5m. The
    absence sentinel fires — and the value-alert resolves — MUCH earlier on VM (at 30s).
    Also pin the ``absent_over_time(X[5m])`` mitigation: it fires ~5m late on VM too, so a
    customer bothered by the noise can restore Prometheus-parity by changing the syntax."""
    _require_deps_or_fail()
    m = "replay_tc2_probe"
    sel = f'{m}{{run_id="{_RUN}"}}'          # run_id-scoped selector for the rule exprs
    ws = T0
    we = ws + 1800
    # SSOT: probe=100 (>80) for 0..300s @30s, then STOPS. Series tagged with run_id (unique
    # per run) → physically-new series, no delete needed (Gemini #968 tombstone-race fix).
    _VM.import_prometheus([f'{m}{{instance="t",run_id="{_RUN}"}} 100 {(ws + t) * 1000}' for t in range(0, 301, STEP)])
    _VM.flush()

    rules = (f"groups:\n  - name: tc2\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: ProbeHigh\n        expr: {sel} > 80\n        for: 1m\n"
             f"      - alert: ProbeAbsent\n        expr: absent({sel})\n        for: 0s\n"
             f"      - alert: ProbeAbsentOT\n        expr: absent_over_time({sel}[5m])\n        for: 0s\n")
    _replay(rules, ws, we, tmp_path, "tc2")

    vm_absent = _alert_offsets("ProbeAbsent", "firing", ws, we)
    vm_high = _alert_offsets("ProbeHigh", "firing", ws, we)
    vm_absent_ot = _alert_offsets("ProbeAbsentOT", "firing", ws, we)
    assert vm_absent, "replay wrote no ProbeAbsent firing series — replay/plumbing no-op?"
    assert vm_high, "replay wrote no ProbeHigh firing series — replay/plumbing no-op?"
    assert vm_absent_ot, "replay wrote no ProbeAbsentOT firing series — replay/plumbing no-op?"

    # VM: absent() fires ≈1–2 scrape intervals after the last sample (+300); value-alert
    # resolves there too. At 30s: last sample +300 -> present through ~+330 -> flip ~+360.
    assert 330 <= vm_absent[0] <= 420, (
        f"VM absent() first-fire @+{vm_absent[0]}s outside the pinned 30s staleness band "
        f"[330,420] — VM storage staleness timing drifted (engine bump?).")
    assert vm_high[-1] <= 420, (
        f"VM ProbeHigh last-firing @+{vm_high[-1]}s > 420 — value-alert held longer than "
        f"the pinned VM staleness band; staleness timing drifted.")

    # Prometheus reference (promtool 3.x, 5m LEFT-OPEN lookback): STILL firing high & quiet
    # on absent at +360 (where VM already flipped) and at +570 (the last pre-flip eval: the
    # (+270,+570] window still holds the +300s sample); the left-open (+300,+600] window
    # excludes the boundary sample exactly at +600 -> both alerts flip AT +600 (10m). The
    # 9m30s/10m pair brackets the flip to one eval step, so no probe beyond 10m is needed
    # (the state is monotone after the flip — the old 11m probe carried no extra boundary
    # information once 10m itself asserts the flipped state).
    prom_fix = ("rule_files:\n  - tc2_pr_rules.yml\nevaluation_interval: 30s\n"
                "tests:\n  - interval: 30s\n    input_series:\n"
                "      - series: 'probe{instance=\"t\"}'\n        values: '100x10'\n"
                "    alert_rule_test:\n"
                "      - eval_time: 6m\n        alertname: ProbeHigh\n"
                "        exp_alerts:\n          - exp_labels: {instance: \"t\"}\n"
                "      - eval_time: 6m\n        alertname: ProbeAbsent\n        exp_alerts: []\n"
                "      - eval_time: 9m30s\n        alertname: ProbeHigh\n"
                "        exp_alerts:\n          - exp_labels: {instance: \"t\"}\n"
                "      - eval_time: 9m30s\n        alertname: ProbeAbsent\n        exp_alerts: []\n"
                "      - eval_time: 10m\n        alertname: ProbeHigh\n        exp_alerts: []\n"
                "      - eval_time: 10m\n        alertname: ProbeAbsent\n"
                "        exp_alerts:\n          - exp_labels: {}\n")
    prom_rules = ("groups:\n  - name: tc2\n    rules:\n"
                  "      - alert: ProbeHigh\n        expr: probe > 80\n        for: 1m\n"
                  "      - alert: ProbeAbsent\n        expr: absent(probe)\n        for: 0s\n")
    r = _promtool_ok(prom_fix, prom_rules, tmp_path, "tc2")
    assert r.returncode == 0, (
        f"Prometheus reference drifted — promtool no longer holds the 5m-lookback timeline "
        f"(materialization parity broken?):\n{r.stdout}\n{r.stderr}")

    # The divergence: Prometheus fires absent only at +600 (the left-open lookback boundary
    # pinned by the fixture above); VM fired by +{vm_absent[0]}. Margin holds even at the
    # loosest VM pin: the band above caps vm_absent[0] at 420, and 600 - 420 >= 180.
    PROM_ABSENT_FIRE = 600
    assert PROM_ABSENT_FIRE - vm_absent[0] >= 180, (
        f"expected VM absent() to fire >=180s EARLIER than Prometheus at 30s; got VM "
        f"@+{vm_absent[0]}s vs Prometheus ~+{PROM_ABSENT_FIRE}s")

    # MITIGATION (Gemini #968): absent_over_time(X[5m]) fires ~5m after the last sample on VM
    # (~+600-660) — i.e. it restores Prometheus-parity. Prove it fires MUCH later than plain
    # absent() (the customer's opt-in fix if absent() is "too noisy" on VM).
    assert 540 <= vm_absent_ot[0] <= 690, (
        f"VM absent_over_time([5m]) first-fire @+{vm_absent_ot[0]}s outside the ~5m band "
        f"[540,690] — the documented Prometheus-parity mitigation drifted.")
    assert vm_absent_ot[0] - vm_absent[0] >= 180, (
        f"absent_over_time([5m]) should fire >=180s LATER than plain absent() (restoring Prom "
        f"tolerance); got OT @+{vm_absent_ot[0]}s vs absent @+{vm_absent[0]}s")


# ===========================================================================
# TC1 — threshold + gap: for:-gated value alert across a mid-series missed scrape
# ===========================================================================
def test_tc1_threshold_gap_for_reset(tmp_path):
    """A missed scrape stales the series on VM -> the for:3m timer resets -> VM fires late.
    Prometheus carries the last value 5m across the gap -> for: keeps accumulating -> fires
    early. Pin VM's late fire and confirm (via promtool) Prometheus fires early."""
    _require_deps_or_fail()
    m = "replay_tc1_gauge"
    sel = f'{m}{{run_id="{_RUN}"}}'
    ws = T0 + CASE_GAP
    we = ws + 900
    # SSOT: g=85 (>80) present 0..120s, MISSED scrapes 150..270, resume 300..600s @30s.
    present = list(range(0, 121, STEP)) + list(range(300, 601, STEP))
    _VM.import_prometheus([f'{m}{{instance="t",run_id="{_RUN}"}} 85 {(ws + t) * 1000}' for t in present])
    _VM.flush()

    rules = (f"groups:\n  - name: tc1\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: HighFor\n        expr: {sel} > 80\n        for: 3m\n")
    _replay(rules, ws, we, tmp_path, "tc1")

    vm_fire = _alert_offsets("HighFor", "firing", ws, we)
    assert vm_fire, "replay wrote no HighFor firing series — replay/plumbing no-op?"
    # VM: series stales in the gap -> for: resets -> re-accumulates 3m from the +300 resume
    # -> fires ~+480. (A faithful Prometheus carry would have fired at +180.)
    assert 450 <= vm_fire[0] <= 540, (
        f"VM HighFor first-fire @+{vm_fire[0]}s outside the pinned band [450,540] — VM "
        f"staleness/for-reset timing drifted (engine bump?).")

    # Prometheus reference (promtool): `_` = missing sample -> 5m carry across the gap ->
    # for:3m satisfied -> fires at +180.
    prom_fix = ("rule_files:\n  - tc1_pr_rules.yml\nevaluation_interval: 30s\n"
                "tests:\n  - interval: 30s\n    input_series:\n"
                "      - series: 'g{instance=\"t\"}'\n"
                "        values: '85x4 _ _ _ _ _ 85x10'\n"
                "    alert_rule_test:\n"
                "      - eval_time: 3m\n        alertname: HighFor\n"
                "        exp_alerts:\n          - exp_labels: {instance: \"t\"}\n")
    prom_rules = ("groups:\n  - name: tc1\n    rules:\n"
                  "      - alert: HighFor\n        expr: g > 80\n        for: 3m\n")
    r = _promtool_ok(prom_fix, prom_rules, tmp_path, "tc1")
    assert r.returncode == 0, (
        f"Prometheus reference drifted — promtool no longer fires HighFor at +180 across an "
        f"`_` gap (5m carry):\n{r.stdout}\n{r.stderr}")

    PROM_FIRE = 180
    assert vm_fire[0] - PROM_FIRE >= 240, (
        f"expected VM to fire >=240s LATER than Prometheus across the gap; got VM @+{vm_fire[0]}s "
        f"vs Prometheus ~+{PROM_FIRE}s")


# ===========================================================================
# positive control — the bench measures a REAL replay, not a no-op
# ===========================================================================
def test_harness_measures_real_replay(tmp_path):
    """If replay silently produced nothing, every 'VM flipped early/late' assertion above
    would pass vacuously via the emptiness guards. Prove one replay writes a firing ALERTS
    series we can read back."""
    _require_deps_or_fail()
    m = "replay_selftest_probe"
    sel = f'{m}{{run_id="{_RUN}"}}'
    ws = T0 + 2 * CASE_GAP
    we = ws + 300
    _VM.import_prometheus([f'{m}{{instance="t",run_id="{_RUN}"}} 100 {(ws + t) * 1000}' for t in range(0, 301, STEP)])
    _VM.flush()
    rules = (f"groups:\n  - name: st\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: SelfHigh\n        expr: {sel} > 80\n        for: 0s\n")
    _replay(rules, ws, we, tmp_path, "st")
    firing = _alert_offsets("SelfHigh", "firing", ws, we)
    assert firing and firing[0] == 0, (
        f"positive control failed: SelfHigh should fire from +0s, got offsets {firing[:5]} — "
        f"replay/import/query path is not measuring real vmsingle state")
