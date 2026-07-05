"""promtool regression goldens for the Federation Revocation Reconciler dashboard (#1002).

The dashboard (`k8s/03-monitoring/federation-revocation-dashboard.json`) is the
operational field view for the ADR-028 D1 un-revoke tamper-evidence control (#924).
It consumes the six platform-global reconciler metrics emitted by
`_federation_revocation_reconciler.py` and reuses two non-trivial PromQL idioms this
repo keeps getting burned by:

  * a `time() - <last_reconcile_timestamp>` STALENESS delta whose panel threshold
    must line up with the FederationRevocationReconcileStale alert (> 1800s), and
  * a coverage-erosion ratio `dropped / clamp_min(checked + dropped, 1)` whose
    `clamp_min` is the divide-by-zero guard — without it an IDLE feed (checked=0,
    dropped=0) renders `0/0 = NaN`, a silent-wrong panel with no JSON error.

DRIFT-PROOF: the queries under test are READ FROM THE DASHBOARD JSON (never copied
here), substituted with concrete template-var values, and run through the real
Prometheus engine against synthetic fixtures with hand-computed golden expectations.
Edit a covered query's semantics → the golden fails; rename/remove a covered panel →
the lookup fails (drift-aware). Mirrors tests/dx/test_fleet_threshold_dashboard.py
and tests/dx/test_tenant_log_query_dashboard.py.

A11Y (ADR-012 / WCAG 1.4.1): a separate pure-JSON check asserts every colour-coded
stat panel encodes its state with a symbol + text (Grafana value mappings), not
background colour alone — one symbol-bearing mapping per colour tier — so no state
(including the alarming one) is left distinguished by colour alone. It needs no
promtool and runs everywhere.

DRIFT-GUARD (Q2 auto-provisioning): a third pure-JSON check asserts the copy of this
dashboard embedded in `k8s/03-monitoring/configmap-grafana.yaml` (baked into the
shipped Grafana) is byte-for-byte the same object as the standalone SOT file, so the
two copies cannot silently diverge.

Only the promtool golden test skips when promtool is absent (host / a CI job without
it); the shape, a11y, and drift-guard checks are pure JSON and run everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO / "k8s" / "03-monitoring" / "federation-revocation-dashboard.json"
_GRAFANA_CM = _REPO / "k8s" / "03-monitoring" / "configmap-grafana.yaml"
_PROMTOOL = shutil.which("promtool")

# Only the promtool golden test needs a real Prometheus engine; the shape + a11y +
# drift-guard checks are pure JSON, so the skip is scoped to that one test.
_needs_promtool = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")


# ── Synthetic reconciler state (the test fixture, legitimately fixed) ───────────
# All six metrics are platform-global with ZERO labels, so each is a single series.
#
# SCENARIO "incident" — a tamper+stale+drift state chosen so most panels carry a
# non-trivial value. Evaluated at t=2000s (promtool epoch starts at 0, so at
# eval_time=2000s the engine's time() == 2000):
#   tamper_suspected            = 2      -> "Tamper status"/"headline" == 2
#   last_reconcile_timestamp    = 100    -> staleness = time()-ts = 2000-100 = 1900
#                                           ( > 1800 = the ReconcileStale threshold )
#   events_checked              = 7
#   events_dropped              = 3      -> erosion = 3 / (7+3) = 0.3  (exact)
#   gateway_revocation_load_errors = 1   -> "Gateway fail-open" == 1
#   reconcile_errors_total (counter) ramps 0+2x…  -> rate[5m] > 0 (fail-closed marker)
_INCIDENT = {
    "federation_revocation_tamper_suspected": "2",
    "federation_revocation_last_reconcile_timestamp_seconds": "100",
    "federation_revocation_events_checked": "7",
    "federation_revocation_events_dropped": "3",
    "federation_gateway_revocation_load_errors": "1",
}
# counter that increases 2 per step so rate() is unambiguously positive
_INCIDENT_COUNTER = ("federation_revocation_reconcile_errors_total", "0+2x200")


def _const_series(name: str, value: str) -> dict:
    # Constant gauge held across the whole window (Nx200 samples at 15s interval).
    return {"series": name, "values": f"{value}x200"}


def _incident_input() -> list[dict]:
    rows = [_const_series(n, v) for n, v in _INCIDENT.items()]
    rows.append({"series": _INCIDENT_COUNTER[0], "values": _INCIDENT_COUNTER[1]})
    return rows


# SCENARIO "idle" — the divide-by-zero guard: an idle feed where NOTHING was checked
# and NOTHING dropped. The erosion ratio MUST read 0 (via clamp_min), never NaN.
#   events_checked = 0, events_dropped = 0  -> 0 / clamp_min(0, 1) = 0 / 1 = 0
_IDLE = {
    "federation_revocation_events_checked": "0",
    "federation_revocation_events_dropped": "0",
}


def _idle_input() -> list[dict]:
    return [_const_series(n, v) for n, v in _IDLE.items()]


# ── Golden expectations, keyed to panels by a stable ASCII title substring ──────
# (title_substr, disc, scenario, eval_time, expected, exp_labels)
#   disc: target legendFormat/refId discriminator (None = single-target panel)
#   scenario: "incident" or "idle" -> which input fixture + which test block
#   exp_labels: the promtool label set of the result series. A BARE single-metric
#     selector preserves `{__name__="<metric>"}`; any arithmetic / rate / aggregation
#     strips it to `{}`. Asserting the exact label set (not just the value) also pins
#     that the panel's expr keeps the intended shape (e.g. a stray aggregation that
#     changed the label set would fail even at the same scalar value).
_M = "federation_revocation_"  # metric-name prefix
_GOLDENS = [
    # --- Row 0 health-summary stat panels + Row 4 headline (incident state) ---
    # Bare-selector panels: result carries {__name__="<metric>"}.
    ("Tamper status", None, "incident", 2900, 2, f'{{__name__="{_M}tamper_suspected"}}'),
    ("tamper headline", None, "incident", 2900, 2, f'{{__name__="{_M}tamper_suspected"}}'),
    ("Gateway fail-open", None, "incident", 2900, 1, f'{{__name__="federation_gateway_revocation_load_errors"}}'),
    ("read failures (fail-open", None, "incident", 2900, 1, f'{{__name__="federation_gateway_revocation_load_errors"}}'),
    ("Coverage integrity", None, "incident", 2900, 3, f'{{__name__="{_M}events_dropped"}}'),
    ("Events dropped (schema", None, "incident", 2900, 3, f'{{__name__="{_M}events_dropped"}}'),
    ("Events checked", None, "incident", 2900, 7, f'{{__name__="{_M}events_checked"}}'),
    # Arithmetic / rate panels: result label set is {}.
    ("Reconciler freshness", None, "incident", 2000, 1900, "{}"),
    ("staleness (fail-closed", None, "incident", 2000, 1900, "{}"),
    ("erosion ratio", None, "incident", 2900, 0.3, "{}"),
    ("error rate (fail-closed", None, "incident", 2900, 2.0 / 15.0, "{}"),  # 2 per 15s step
    # --- the divide-by-zero guard (idle feed): 0/clamp_min(0,1)=0, NOT NaN ---
    ("erosion ratio", None, "idle", 2900, 0, "{}"),
]


def _load_panels() -> list[dict]:
    import json

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert data.get("uid") == "federation-revocation", "dashboard uid drift"
    return data["panels"]


def _find_expr(panels: list[dict], title_substr: str, disc: str | None) -> str:
    """Extract a target's raw expr from the JSON by panel-title substring + target disc."""
    for p in panels:
        if title_substr in p.get("title", ""):
            targets = p.get("targets", [])
            if disc is None:
                assert len(targets) >= 1, f"panel {p['title']!r} has no targets"
                return targets[0]["expr"]
            for t in targets:
                if t.get("legendFormat") == disc or t.get("refId") == disc:
                    return t["expr"]
            raise AssertionError(
                f"panel {p['title']!r}: no target with legend/refId {disc!r}"
            )
    raise AssertionError(f"no panel title contains {title_substr!r} (panel renamed/removed?)")


def _build_test_file() -> dict:
    panels = _load_panels()
    incident_cases: list[dict] = []
    idle_cases: list[dict] = []
    for title_substr, disc, scenario, eval_time, expected, exp_labels in _GOLDENS:
        expr = _find_expr(panels, title_substr, disc)
        case = {
            "expr": expr,
            "eval_time": f"{eval_time}s",
            "exp_samples": [{"labels": exp_labels, "value": expected}],
        }
        (incident_cases if scenario == "incident" else idle_cases).append(case)
    return {
        "evaluation_interval": "15s",
        "tests": [
            {
                "interval": "15s",
                "input_series": _incident_input(),
                "promql_expr_test": incident_cases,
            },
            {
                "interval": "15s",
                "input_series": _idle_input(),
                "promql_expr_test": idle_cases,
            },
        ],
    }


@_needs_promtool
def test_dashboard_promql_goldens(tmp_path):
    """Every covered dashboard query, read from the JSON, returns its golden value."""
    # yaml is local to this promtool-gated path, so the pure-JSON checks import even
    # where pyyaml is absent.
    import yaml

    test_file = tmp_path / "federation_revocation_promql_test.yaml"
    test_file.write_text(yaml.safe_dump(_build_test_file(), sort_keys=False), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", str(test_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"promtool goldens failed for the federation-revocation dashboard:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_dashboard_is_valid_grafana_shape():
    """Light structural guard: known panel types, defined var refs, no gridPos overlap,
    and the div-by-zero clamp_min guard is present on the erosion-ratio panel."""
    import json
    import re

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    known_types = {"stat", "timeseries", "table"}
    varnames = {v["name"] for v in data["templating"]["list"]}
    used = set()
    rects = []
    for p in data["panels"]:
        assert p["type"] in known_types, f"unknown panel type {p['type']}"
        for tg in p.get("targets", []):
            used |= set(re.findall(r"\$(\w+)", tg["expr"]))
        g = p["gridPos"]
        rects.append((g["x"], g["y"], g["w"], g["h"], p["title"]))
    undefined = {u for u in used if u not in varnames and not u.startswith("__")}
    assert not undefined, f"undefined template vars referenced: {undefined}"
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax, ay, aw, ah, at = rects[i]
            bx, by, bw, bh, bt = rects[j]
            overlap = not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)
            assert not overlap, f"gridPos overlap: {at!r} <> {bt!r}"

    # The erosion-ratio panel MUST guard its denominator with clamp_min — else an
    # idle feed (checked=0, dropped=0) renders 0/0 = NaN (the div-by-zero trap the
    # promtool 'idle' golden also pins). Assert the guard is present in the source.
    erosion = next((p for p in data["panels"] if "erosion ratio" in p.get("title", "")), None)
    assert erosion is not None, "erosion-ratio panel renamed/removed (drift-aware)"
    ero_expr = erosion["targets"][0]["expr"]
    assert "clamp_min" in ero_expr, (
        f"erosion-ratio panel dropped its clamp_min divide-by-zero guard: {ero_expr}"
    )


def test_metric_names_are_the_reconciler_contract():
    """Pin the exact source-metric names the reconciler emits — a rename on either
    side (_federation_revocation_reconciler.py or this dashboard) breaks the data
    flow silently. These are the six ADR-028 D1 metrics."""
    import json

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    exprs = " ".join(tg["expr"] for p in data["panels"] for tg in p.get("targets", []))
    for metric in (
        "federation_revocation_tamper_suspected",
        "federation_revocation_last_reconcile_timestamp_seconds",
        "federation_revocation_events_checked",
        "federation_revocation_events_dropped",
        "federation_revocation_reconcile_errors_total",
        "federation_gateway_revocation_load_errors",
    ):
        assert metric in exprs, f"reconciler metric {metric!r} missing from dashboard"


# ── ADR-012 / WCAG 1.4.1: state must NOT be colour-only ─────────────────────────
# Every colour-coded stat panel conveys a state (tamper / stale / fail-open / drift)
# that a red-green-colourblind operator cannot read from the background alone. Each
# MUST carry that signal in a non-colour channel — a Unicode symbol + words via
# Grafana value mappings. Codified here so the a11y property can't silently regress;
# pure JSON, so it runs even where promtool is absent.
_A11Y_SYMBOLS = ("✓", "⚠", "❌", "✗", "✅", "\U0001f7e2", "\U0001f7e1", "\U0001f534")


def _mapping_texts(panel: dict) -> list[str]:
    """Every result `text` across a panel's value mappings (range / value / special)."""
    texts: list[str] = []
    for m in panel.get("fieldConfig", {}).get("defaults", {}).get("mappings", []):
        opts = m.get("options", {})
        if "result" in opts:  # range / special mapping
            texts.append(opts["result"].get("text", ""))
        else:  # value mapping: {"<value>": {text, color, ...}, ...}
            for v in opts.values():
                if isinstance(v, dict):
                    texts.append(v.get("text", ""))
    return texts


def test_colour_coded_stat_panels_carry_noncolour_state_channel():
    """ADR-012 / WCAG 1.4.1: every colour-coded stat panel encodes its state with a
    symbol + text (value mappings), not background colour alone — and EVERY colour
    tier (threshold step) must have a symbol-bearing counterpart, so no state
    (including the alarming one) is left distinguished by colour alone."""
    panels = _load_panels()
    stat_panels = [p for p in panels if p.get("type") == "stat"]
    assert len(stat_panels) == 5, (
        f"expected 5 colour-coded stat panels (4 health summary + erosion ratio), "
        f"found {len(stat_panels)} — the a11y coverage assumption drifted"
    )
    for panel in stat_panels:
        texts = _mapping_texts(panel)
        assert texts, (
            f"panel {panel['title']!r} is colour-coded but has NO value mappings — "
            f"state is colour-only (ADR-012 / WCAG 1.4.1 violation)"
        )
        symbol_texts = [t for t in texts if any(sym in t for sym in _A11Y_SYMBOLS)]
        n_steps = len(
            panel.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps", [])
        )
        # One symbol-bearing mapping per colour tier → no tier is colour-only.
        assert len(symbol_texts) >= n_steps, (
            f"panel {panel['title']!r}: {len(symbol_texts)} symbol-bearing mapping text(s) "
            f"{symbol_texts!r} for {n_steps} colour tiers — a state (e.g. the alarming one) "
            f"is still distinguished by colour alone (ADR-012 / WCAG 1.4.1)"
        )


def test_configmap_embed_matches_standalone():
    """Q2 auto-provisioning drift-guard: the dashboard baked into the shipped Grafana
    (configmap-grafana.yaml, data key `federation-revocation-dashboard.json`) must be
    the SAME object as the standalone SOT file. Parse both and assert deep equality so
    the two copies cannot silently diverge (a stale embedded copy would provision an
    outdated dashboard while the SOT test stays green)."""
    import json

    import yaml

    standalone = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    cm = yaml.safe_load(_GRAFANA_CM.read_text(encoding="utf-8"))
    key = "federation-revocation-dashboard.json"
    assert key in cm["data"], (
        f"{key!r} is not embedded in configmap-grafana.yaml — the auto-provisioned "
        f"copy is missing (Q2 requires it baked into the shipped Grafana)"
    )
    embedded = json.loads(cm["data"][key])
    assert embedded == standalone, (
        "embedded configmap copy has DRIFTED from the standalone dashboard SOT — "
        "regenerate the configmap data key from "
        "k8s/03-monitoring/federation-revocation-dashboard.json"
    )
