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
MetricsQL derives staleness from the sample interval (~1 scrape interval); Prometheus
uses a fixed 5m lookback. They diverge BY DESIGN across gaps, so parity is the wrong
bar. The value is a *pinned characterization*: a soak dual-run WILL see VM fire/resolve
staleness-driven alerts minutes away from Prometheus, and this bench is the
machine-checked explanation of by how much and in which direction (see
docs/integration/victoriametrics-integration.md §3.1). A material drift in VM's
staleness timing (e.g. an engine-version bump) fails these pins.

Cases (materialization parity: one logical history -> vmsingle import AND promtool fixture)
  TC2 Staleness    : a gauge stops reporting (end gap). VM resolves the value-alert &
                     fires ``absent()`` ~1 scrape interval after the last sample (~+360s);
                     Prometheus holds ~5m (~+600-660s).  => VM ~240-300s EARLIER, i.e.
                     absence sentinels (e.g. the ADR-025 watchdog) are "noisier" on VM.
  TC1 Threshold+Gap: a value-alert with ``for:3m`` across a mid-series missed scrape. VM
                     stales -> the ``for:`` timer resets -> fires late (~+480s); Prometheus
                     carries the last value 5m across the gap -> ``for:`` keeps accumulating
                     -> fires ~+180s.  => VM ~300s LATER, i.e. UNDER-fires a ``for:``-gated
                     value alert across a gap.

NOT covered here: ``rate()`` / ``increase()`` cold-start extrapolation. That is an
ENGINE-MATH divergence (MetricsQL vs PromQL), reproduced IDENTICALLY on ``vmalert-tool``
and on real vmsingle (measured: 3.333 vs Prometheus 1.667, storage-path invariant), so it
is already owned by gate A (``test_vm_alert_parity.py`` catalog + teeth-test). Adding it
here would only duplicate coverage.

Determinism / faithfulness
--------------------------
* Fixed epoch ``T0``; vmsingle MUST run ``-retentionPeriod=100y`` (default 1-month
  retention silently drops the 2023 ``T0`` samples — import 204s but query is empty).
* One GAP-separated window per case; delete-before-import + idempotent re-import.
* promtool ``_`` = a MISSING sample (Prometheus 5m lookback carries the last value across
  it) — NOT a stale marker; verified empirically, so a mid-series gap (TC1) and a series
  that simply ends (TC2) are both faithful Prometheus references.

Provisioning: needs a real vmsingle at ``$VM_REPLAY_ENDPOINT`` (default
``http://localhost:8428``), the ``vmalert`` binary (``$VMALERT`` / PATH / the
dev-container ``/tmp/vm/vmalert-prod``) and ``promtool``. Skips when any is missing (the
normal local / python-tests case); set ``VM_REPLAY_REQUIRE=1`` to force-run (a missing
dependency then HARD-fails instead of skipping to green). This is an on-demand bench, not
a per-PR CI job — run it in a dev-container with a pinned vmsingle, or on a VM-version bump.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

VM = os.environ.get("VM_REPLAY_ENDPOINT", "http://localhost:8428")
T0 = 1_700_000_000          # fixed epoch base — deterministic, never `now`
STEP = 30                   # scrape + replay eval interval (s)
CASE_GAP = 100_000          # s between per-case windows (>> any window; no cross-talk)
_REQUIRE = os.environ.get("VM_REPLAY_REQUIRE") == "1"


def _find_vmalert() -> str | None:
    env = os.environ.get("VMALERT")
    if env and Path(env).exists():
        return env
    for name in ("vmalert", "vmalert-prod"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path("/tmp/vm/vmalert-prod")   # dev-container provisioning
    return str(fallback) if fallback.exists() else None


def _vm_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{VM}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


_VMALERT = _find_vmalert()
_PROMTOOL = shutil.which("promtool")
_missing = (
    "no VictoriaMetrics" if not (_REQUIRE or _vm_reachable()) else
    "no vmalert binary" if _VMALERT is None else
    "no promtool" if _PROMTOOL is None else None
)
pytestmark = pytest.mark.skipif(
    not _REQUIRE and _missing is not None,
    reason=f"{_missing} (on-demand replay bench; start a pinned vmsingle -retentionPeriod=100y, "
           f"provide vmalert + promtool, or VM_REPLAY_REQUIRE=1 to force — see "
           f"docs/integration/victoriametrics-integration.md §3.1)",
)


def _require_deps_or_fail() -> None:
    if _REQUIRE and _missing is not None:
        pytest.fail(f"VM_REPLAY_REQUIRE=1 but {_missing} — the replay bench must not "
                    f"silently skip to green (vmsingle not started / binary absent?)")


# ---- vmsingle HTTP ---------------------------------------------------------
def _imp(lines: list[str]) -> None:
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status in (200, 204), f"VM import failed: {r.status}"


def _flush() -> None:
    with urllib.request.urlopen(f"{VM}/internal/force_flush", timeout=10) as r:
        r.read()


def _delete(match: str) -> None:
    """Best-effort delete so re-runs are deterministic (stale ALERTS from a prior run
    at the same window can't linger). Open on single-node vmsingle by default."""
    qs = urllib.parse.urlencode({"match[]": match})
    try:
        with urllib.request.urlopen(
                urllib.request.Request(f"{VM}/api/v1/admin/tsdb/delete_series?{qs}",
                                       method="POST"), timeout=10) as r:
            r.read()
    except Exception:
        if _REQUIRE:
            raise


def _q_range(expr: str, start: int, end: int, step: int) -> list[dict]:
    qs = urllib.parse.urlencode({"query": expr, "start": str(start), "end": str(end),
                                 "step": f"{step}s", "nocache": "1"})
    with urllib.request.urlopen(f"{VM}/api/v1/query_range?{qs}", timeout=20) as r:
        d = json.loads(r.read())
    assert d.get("status") == "success", f"VM query error: {d}"
    return d["data"]["result"]


def _alert_offsets(name: str, state: str, w_start: int, w_end: int) -> list[int]:
    res = _q_range(f'ALERTS{{alertname="{name}",alertstate="{state}"}}', w_start, w_end, STEP)
    return sorted(int(v[0]) - w_start for s in res for v in s["values"])


# ---- vmalert -replay + promtool --------------------------------------------
def _replay(rules_text: str, w_start: int, w_end: int, tmp: Path, tag: str) -> None:
    rf = tmp / f"{tag}_rules.yml"
    rf.write_text(rules_text, encoding="utf-8")
    tf = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(w_start))
    tt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(w_end))
    p = subprocess.run(
        [_VMALERT, f"-rule={rf.as_posix()}", f"-datasource.url={VM}", f"-remoteWrite.url={VM}",
         f"-replay.timeFrom={tf}", f"-replay.timeTo={tt}", "-replay.disableProgressBar"],
        capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "replay succeed" in p.stderr, (
        f"vmalert -replay failed for {tag}:\n{p.stderr[-1500:]}")
    _flush()


def _promtool_ok(fixture_text: str, rules_text: str, tmp: Path, tag: str) -> subprocess.CompletedProcess:
    (tmp / f"{tag}_pr_rules.yml").write_text(rules_text, encoding="utf-8")
    (tmp / f"{tag}_pr_test.yml").write_text(fixture_text, encoding="utf-8")
    return subprocess.run([_PROMTOOL, "test", "rules", f"{tag}_pr_test.yml"],
                          cwd=str(tmp), capture_output=True, text=True, timeout=60)


# ===========================================================================
# TC2 — staleness: value-alert resolve + absence() fire across an END gap
# ===========================================================================
def test_tc2_staleness_resolve_and_absence(tmp_path):
    """VM stales ~1 scrape interval after the last sample; Prometheus holds ~5m. The
    absence sentinel fires — and the value-alert resolves — MUCH earlier on VM. Pin the
    VM timing and confirm (via promtool) Prometheus is still quiet where VM already fired."""
    _require_deps_or_fail()
    m = "replay_tc2_probe"
    ws = T0
    we = ws + 1800
    _delete(m)
    _delete('ALERTS{alertname="ProbeHigh"}')
    _delete('ALERTS{alertname="ProbeAbsent"}')
    # SSOT: probe=100 (>80) for 0..300s @30s, then STOPS.
    _imp([f'{m}{{instance="t"}} 100 {(ws + t) * 1000}' for t in range(0, 301, STEP)])
    _flush()

    rules = (f"groups:\n  - name: tc2\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: ProbeHigh\n        expr: {m} > 80\n        for: 1m\n"
             f"      - alert: ProbeAbsent\n        expr: absent({m})\n        for: 0s\n")
    _replay(rules, ws, we, tmp_path, "tc2")

    vm_absent = _alert_offsets("ProbeAbsent", "firing", ws, we)
    vm_high = _alert_offsets("ProbeHigh", "firing", ws, we)
    assert vm_absent, "replay wrote no ProbeAbsent firing series — replay/plumbing no-op?"
    assert vm_high, "replay wrote no ProbeHigh firing series — replay/plumbing no-op?"

    # VM: absent() fires ~1 scrape interval after the last sample (+300); value-alert
    # resolves there too. last sample +300 -> present through ~+330 -> flip ~+360.
    assert 330 <= vm_absent[0] <= 420, (
        f"VM absent() first-fire @+{vm_absent[0]}s outside the pinned staleness band "
        f"[330,420] — VM storage staleness timing drifted (engine bump?).")
    assert vm_high[-1] <= 420, (
        f"VM ProbeHigh last-firing @+{vm_high[-1]}s > 420 — value-alert held longer than "
        f"the pinned VM staleness band; staleness timing drifted.")

    # Prometheus reference (promtool, 5m lookback): STILL firing high & quiet on absent at
    # +360 (where VM already flipped) and at +600; flips only by +660.
    prom_fix = ("rule_files:\n  - tc2_pr_rules.yml\nevaluation_interval: 30s\n"
                "tests:\n  - interval: 30s\n    input_series:\n"
                "      - series: 'probe{instance=\"t\"}'\n        values: '100x10'\n"
                "    alert_rule_test:\n"
                "      - eval_time: 6m\n        alertname: ProbeHigh\n"
                "        exp_alerts:\n          - exp_labels: {instance: \"t\"}\n"
                "      - eval_time: 6m\n        alertname: ProbeAbsent\n        exp_alerts: []\n"
                "      - eval_time: 10m\n        alertname: ProbeHigh\n"
                "        exp_alerts:\n          - exp_labels: {instance: \"t\"}\n"
                "      - eval_time: 10m\n        alertname: ProbeAbsent\n        exp_alerts: []\n"
                "      - eval_time: 11m\n        alertname: ProbeAbsent\n"
                "        exp_alerts:\n          - exp_labels: {}\n")
    prom_rules = ("groups:\n  - name: tc2\n    rules:\n"
                  "      - alert: ProbeHigh\n        expr: probe > 80\n        for: 1m\n"
                  "      - alert: ProbeAbsent\n        expr: absent(probe)\n        for: 0s\n")
    r = _promtool_ok(prom_fix, prom_rules, tmp_path, "tc2")
    assert r.returncode == 0, (
        f"Prometheus reference drifted — promtool no longer holds the 5m-lookback timeline "
        f"(materialization parity broken?):\n{r.stdout}\n{r.stderr}")

    # The divergence: Prometheus fires absent only by ~+660; VM fired by +{vm_absent[0]}.
    PROM_ABSENT_FIRE = 660
    assert PROM_ABSENT_FIRE - vm_absent[0] >= 180, (
        f"expected VM absent() to fire >=180s EARLIER than Prometheus; got VM @+{vm_absent[0]}s "
        f"vs Prometheus ~+{PROM_ABSENT_FIRE}s")


# ===========================================================================
# TC1 — threshold + gap: for:-gated value alert across a mid-series missed scrape
# ===========================================================================
def test_tc1_threshold_gap_for_reset(tmp_path):
    """A missed scrape stales the series on VM -> the for:3m timer resets -> VM fires late.
    Prometheus carries the last value 5m across the gap -> for: keeps accumulating -> fires
    early. Pin VM's late fire and confirm (via promtool) Prometheus fires early."""
    _require_deps_or_fail()
    m = "replay_tc1_gauge"
    ws = T0 + CASE_GAP
    we = ws + 900
    _delete(m)
    _delete('ALERTS{alertname="HighFor"}')
    # SSOT: g=85 (>80) present 0..120s, MISSED scrapes 150..270, resume 300..600s @30s.
    present = list(range(0, 121, STEP)) + list(range(300, 601, STEP))
    _imp([f'{m}{{instance="t"}} 85 {(ws + t) * 1000}' for t in present])
    _flush()

    rules = (f"groups:\n  - name: tc1\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: HighFor\n        expr: {m} > 80\n        for: 3m\n")
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
    ws = T0 + 2 * CASE_GAP
    we = ws + 300
    _delete(m)
    _delete('ALERTS{alertname="SelfHigh"}')
    _imp([f'{m}{{instance="t"}} 100 {(ws + t) * 1000}' for t in range(0, 301, STEP)])
    _flush()
    rules = (f"groups:\n  - name: st\n    interval: {STEP}s\n    rules:\n"
             f"      - alert: SelfHigh\n        expr: {m} > 80\n        for: 0s\n")
    _replay(rules, ws, we, tmp_path, "st")
    firing = _alert_offsets("SelfHigh", "firing", ws, we)
    assert firing and firing[0] == 0, (
        f"positive control failed: SelfHigh should fire from +0s, got offsets {firing[:5]} — "
        f"replay/import/query path is not measuring real vmsingle state")
