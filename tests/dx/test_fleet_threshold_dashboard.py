"""promtool regression goldens for the Fleet Threshold Distribution dashboard (#655).

The dashboard (`k8s/03-monitoring/fleet-threshold-distribution.json`) consumes the
`user_threshold` gauge with non-trivial PromQL idioms — `quantile` aggregation,
Tukey 1.5×IQR fences via `scalar()` broadcasting, an `or vector(0)` outlier-count
fallback, and a `label_replace` or-union outlier table. None of that is caught by
JSON-validity or paren-balance checks: a semantically wrong query silently renders
an empty/wrong panel — exactly the silent-failure class this repo keeps getting
burned by.

DRIFT-PROOF: the queries under test are READ FROM THE DASHBOARD JSON (never copied
here), substituted with concrete template-var values, and run through the real
Prometheus engine against a synthetic `user_threshold` fixture with hand-computed
golden expectations. If someone edits a covered query and changes its semantics the
golden fails; if they rename/remove a covered panel the lookup fails (drift-aware).

A11Y (ADR-012 / WCAG 1.4.1): a separate pure-JSON check asserts the two colour-coded
stat panels encode their tier/state with a symbol + text (Grafana value mappings),
not background colour alone — it needs no promtool and runs everywhere.

Skips cleanly when promtool is absent (host / a CI job without it); runs fully in
the dev container and CI. Mirrors tests/dx/test_custom_alerts_promtool.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO / "k8s" / "03-monitoring" / "fleet-threshold-distribution.json"
_PROMTOOL = shutil.which("promtool")

pytestmark = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")

# ── Synthetic fleet (the test fixture, legitimately fixed) ──────────────────
# Scenario LAG (metric=lag, severity=warning, component=db): a spread distribution
# with ONE extreme high outlier. Sorted values, n=10, quantile rank = phi*(n-1):
#   P05=52.25 P25=61.25 P50=72.5 P75=83.75 P95=1140.499999999998  IQR=22.5
#   upper fence=117.5  lower fence=27.5  -> 1 high outlier (2000), 0 low
# Scenario MEM (metric=mem): 8 tenants all 70 -> IQR=0, healthy fleet -> 0 outliers.
_LAG_VALUES = [50, 55, 60, 65, 70, 75, 80, 85, 90, 2000]


def _series(tenant: str, metric: str, component: str, severity: str, value) -> dict:
    sel = (
        f'user_threshold{{tenant="{tenant}",metric="{metric}",'
        f'component="{component}",severity="{severity}"}}'
    )
    return {"series": sel, "values": f"{value}x40"}


def _input_series() -> list[dict]:
    rows = [
        _series(f"t{i:02d}", "lag", "db", "warning", v)
        for i, v in enumerate(_LAG_VALUES, start=1)
    ]
    # NOISE that the dashboard's selector must exclude:
    rows += [
        _series("t01", "lag", "db", "critical", 99999),   # other severity
        _series("t01", "cpu", "db", "warning", 88),        # other metric
        _series("t11", "lag", "cache", "warning", 9999),   # other component
    ]
    # Healthy uniform fleet (IQR=0 -> must yield ZERO outliers):
    rows += [_series(f"m{i}", "mem", "db", "warning", 70) for i in range(1, 9)]
    # Documented DEGENERATION scenarios (pin the known reliability limits from the
    # #655 caveat so a future change to the behaviour is conscious, not silent):
    #   s3s: N=3 spread -> Tukey MISSES the extreme (under-detection)
    rows += [_series(f"s3s{i}", "s3s", "db", "warning", v) for i, v in enumerate([50, 60, 2000])]
    #   s4o: N=4 tight cluster -> Tukey OVER-flags a 1-unit deviation
    rows += [_series(f"s4o{i}", "s4o", "db", "warning", v) for i, v in enumerate([50, 50, 50, 51])]
    #   mh: mode-heavy (40 on default + 10 customizers) -> IQR=0 -> flags ALL 10
    rows += [_series(f"mhd{i}", "mh", "db", "warning", 70) for i in range(40)]
    rows += [_series(f"mhc{v}", "mh", "db", "warning", v) for v in [50, 60, 65, 80, 90, 100, 150, 200, 500, 2000]]
    return rows


# ── Golden expectations, keyed to panels by a stable ASCII title substring ──
# (title_substr, target_discriminator_or_None, wrap, metric, expected)
#   target_discriminator matches a target's legendFormat or refId (multi-target panels)
#   wrap: None = assert the expr directly (scalar panels);
#         "count"/"max" = wrap a series-producing panel expr to a scalar to assert
_P95 = 1140.499999999998  # 0.55 quantile weight is not exactly representable in float
_GOLDENS = [
    # --- top-row stat panels (scalar) ---
    ("Tenants configuring", None, None, "lag", 10),
    ("Fleet median", None, None, "lag", 72.5),
    ("P95", None, None, "lag", _P95),               # title is exactly "P95"
    ("IQR (P75", None, None, "lag", 22.5),
    ("Tukey fence", "Upper", None, "lag", 117.5),
    ("Tukey fence", "Lower", None, "lag", 27.5),
    ("Outliers", None, None, "lag", 1),             # title is exactly "Outliers"
    # --- distribution panels (series -> wrapped) ---
    ("value distribution", None, "count", "lag", 10),     # histogram source = 10 tenants
    ("quantile band", "P5", None, "lag", 52.25),
    ("quantile band", "P50 (median)", None, "lag", 72.5),
    ("quantile band", "P95", None, "lag", _P95),
    # --- table panels (series -> wrapped) ---
    ("value & deviation", "Val", "count", "lag", 10),     # one row per tenant
    ("value & deviation", "Dev", "max", "lag", 1927.5),   # max dev = 2000 - 72.5
    ("Statistical outliers", "Outliers", "count", "lag", 1),  # label_replace or-union = 1 row
    # --- healthy-fleet scenario: the "empty when healthy" claim ---
    ("Fleet median", None, None, "mem", 70),
    ("Outliers", None, None, "mem", 0),               # IQR=0 uniform -> ZERO outliers
    # --- DOCUMENTED degeneration limits (pinned; see the #655 reliability caveat) ---
    ("Outliers", None, None, "s3s", 0),               # N=3 spread MISSES the 2000 extreme
    ("Outliers", None, None, "s4o", 1),               # N=4 tight OVER-flags the 51
    ("Outliers", None, None, "mh", 10),               # mode-heavy IQR=0 flags ALL 10 customizers
    ("Tenants configuring", None, None, "mh", 50),
]


def _load_panels() -> list[dict]:
    import json

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert data.get("uid") == "fleet-threshold-distribution", "dashboard uid drift"
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


def _substitute(expr: str, metric: str) -> str:
    # Concrete values for the Grafana template vars used in the dashboard exprs.
    return (
        expr.replace("$metric", metric)
        .replace("$severity", "warning")
        .replace("$component", "db")
    )


def _build_test_file() -> dict:
    panels = _load_panels()
    cases = []
    for title_substr, disc, wrap, metric, expected in _GOLDENS:
        raw = _find_expr(panels, title_substr, disc)
        expr = _substitute(raw, metric)
        if wrap:
            expr = f"{wrap}({expr})"
        cases.append(
            {
                "expr": expr,
                "eval_time": "3m",
                "exp_samples": [{"labels": "{}", "value": expected}],
            }
        )
    return {
        "evaluation_interval": "15s",
        "tests": [{"interval": "15s", "input_series": _input_series(), "promql_expr_test": cases}],
    }


def test_dashboard_promql_goldens(tmp_path):
    """Every covered dashboard query, read from the JSON, returns its golden value."""
    test_file = tmp_path / "fleet_dashboard_promql_test.yaml"
    test_file.write_text(yaml.safe_dump(_build_test_file(), sort_keys=False), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", str(test_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"promtool goldens failed for the fleet dashboard:\n{result.stdout}\n{result.stderr}"
    )


def test_dashboard_is_valid_grafana_shape():
    """Light structural guard: known panel types, defined var refs, no gridPos overlap."""
    import json
    import re

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    known_types = {"stat", "histogram", "timeseries", "table"}
    known_tf = {"merge", "organize", "sortBy"}
    varnames = {v["name"] for v in data["templating"]["list"]}
    used = set()
    rects = []
    for p in data["panels"]:
        assert p["type"] in known_types, f"unknown panel type {p['type']}"
        for t in p.get("transformations", []):
            assert t["id"] in known_tf, f"unknown transformation {t['id']}"
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


# ── ADR-012 / WCAG 1.4.1: severity & state must NOT be colour-only ──────────
# The two colour-coded stat panels convey a tier (sample adequacy) / state
# (outliers present) that a red-green-colourblind operator cannot read from the
# background alone. They MUST carry that signal in a non-colour channel — a
# Unicode symbol + words via Grafana value mappings (ADR-012 pattern: symbol +
# text + colour, never colour alone). Codified here so the a11y fix can't
# silently regress; pure JSON, so it runs even where promtool is absent.
_A11Y_SYMBOLS = ("✓", "⚠", "❌", "✗", "✅", "🟢", "🟡", "🔴")


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


def test_colour_coded_panels_carry_noncolour_severity_channel():
    """ADR-012 / WCAG 1.4.1: the colour-coded stat panels encode their tier/state
    with a symbol + text (value mappings), not background colour alone."""
    panels = _load_panels()
    for title_substr in ("sample adequacy", "Outliers"):
        panel = next((p for p in panels if title_substr in p.get("title", "")), None)
        assert panel is not None, (
            f"no panel title contains {title_substr!r} (renamed/removed? a11y check is drift-aware)"
        )
        texts = _mapping_texts(panel)
        assert texts, (
            f"panel {panel['title']!r} is colour-coded but has NO value mappings — "
            f"tier/state is colour-only (ADR-012 / WCAG 1.4.1 violation)"
        )
        joined = " ".join(texts)
        assert any(sym in joined for sym in _A11Y_SYMBOLS), (
            f"panel {panel['title']!r} mappings {texts!r} carry no a11y symbol from "
            f"{_A11Y_SYMBOLS} — colour-blind operators can't read the tier"
        )
