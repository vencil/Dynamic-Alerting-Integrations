"""Backend-compat parity smoke: do our COMPILED rule-pack exprs evaluate the same
on VictoriaMetrics as on Prometheus? (ADR-025 deferred "backend compatibility",
Part 1 — PromQL parity.)

Why this exists
---------------
We market the platform as storage-backend-agnostic (ADR-020/021) and ship a
`docs/integration/victoriametrics-integration.md`, but every promtool golden runs
on the *Prometheus* engine only. This re-runs a representative subset of those
SAME goldens (SSOT — no second fixture to drift) against a real VictoriaMetrics,
to catch cross-engine PromQL divergence in the idioms our compiler emits:
`and on(...)` topology joins, `group_left` enrichment, `or vector(0)` sentinels,
`label_replace`, `max by(tenant)` dedup, exact-match version joins.

Scope (honest boundary)
-----------------------
- IN scope — does the BACKEND evaluate our `expr` to the same RESULT VECTOR?
  * alert rules (`alert_rule_test`) → ALERT-DECISION parity: the fired label-sets
    + fire/no-fire (the decision-relevant output). promtool alert goldens assert
    labels, not values, so alert-level value *magnitude* isn't checked there —
    only a sign-flip that changes the threshold cross is visible.
  * recording-rule exprs (`promql_expr_test`) → label-set **+ VALUE parity (float
    epsilon)** against the `exp_samples` golden — this is where numeric divergence
    (`rate()` extrapolation, division) is caught. The positive-control test also
    proves the value/epsilon path end-to-end.
- Covered by the SIBLING gate (test_vm_alert_parity.py), NOT here:
  * `for:` duration + range-function evaluation → the rule *evaluator*'s job
    (promtool / vmalert). This was assumed "identical regardless of storage backend"
    — EMPIRICALLY FALSE: MetricsQL's range-function cold-start semantics (changes() /
    rate() while the window still predates the series' first sample) feed the `for:`
    timer differently, so an alert can fire ~10m late (or spuriously) on vmalert. Now
    gated by test_vm_alert_parity.py (FULL fixture set on the vmalert MetricsQL engine,
    fire/no-fire) + vm_deviation_catalog.yaml. Worked example: TenantHAReplicasDegraded.
  * staleness / `absence`-over-real-gaps + `predict_linear` temporal semantics →
    the deferred-with-trigger condition ("first customer on their own backend") has
    FIRED with the VictoriaMetrics-migration customers. The vmalert-tool gate covers
    the for:/range-function layer; full real-gap staleness parity is the remaining
    slice. See ADR-025.

Reference & determinism
-----------------------
The promtool golden's asserted outcome IS the Prometheus reference (validated by
the existing `Rule-pack promtool unit tests` CI step). We import the golden's
`input_series` into VM, materialize the recording-rule chain (so multi-layer
leaf→recording→alert exprs resolve), then compare VM's result to the golden.
Each logical test ingests at a fixed, unique epoch window (`T0 + slot*GAP`,
GAP >> VM staleness, slot unique per worker×case×block) so tests can't cross-talk
and CI speed never shifts the result (Gemini trap #1); re-import is idempotent.
Queries pass `nocache=1`; values compare with a float epsilon (Gemini trap #2).
The CI job sets VM_PARITY_REQUIRE=1 so an unreachable VM hard-fails (never a
silent skip-to-green).
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPO / "tests" / "rulepacks"

VM_ENDPOINT = os.environ.get("VM_PARITY_ENDPOINT", "http://localhost:8428")
_T0 = 1_700_000_000          # fixed epoch base — deterministic, never `now`
_GAP = 3_600                 # ingest-window gap (s); >> VM default staleness (5m)
_EPSILON = 1e-6
_MAX_BLOCKS = 50             # max test-blocks per case (slot budget)
_WORKER_SPAN = 1_000         # slots reserved per xdist worker (>> groups*_MAX_BLOCKS)

# Representative subset of ALERT goldens — idioms most likely to diverge cross-engine.
# (fixture, alertname). A smoke (pattern-breaking detection), not 100% coverage.
_CASES = [
    # `and on(node)` topology fan-out + group_left enrichment + Running filter (#809)
    ("rule-pack-kubernetes-node-health_test.yaml", "NodeNotReady"),
    # version exact-match join + group_left + rate() counter + default fallback (ADR-024)
    ("rule-pack-kubernetes-version-aware_test.yaml", "PodContainerHighCPU"),
    # per-severity split must NOT cross-fire (no-fire assertion; ADR-024 AC-3)
    ("rule-pack-kubernetes-version-aware_test.yaml", "PodContainerHighCPUCritical"),
]

# Representative subset of recording-rule VALUE goldens (promql_expr_test → exp_samples).
# (fixture, expr/record-name). Exercises numeric (value+epsilon) parity.
_EXPR_CASES = [
    # #731 exporter↔rule-pack metric-label contract: threshold resolves to its value
    ("rule-pack-db2-threshold_test.yaml", "tenant:alert_threshold:db2_connections_active"),
]


def _vm_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{VM_ENDPOINT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# In CI the dedicated job sets VM_PARITY_REQUIRE=1: an unreachable VM is then a hard
# FAILURE (the job's whole point), never a silent skip-to-green. Locally (unset) it
# skips when no VM is present.
_REQUIRE_VM = os.environ.get("VM_PARITY_REQUIRE") == "1"
_needs_vm = pytest.mark.skipif(
    not _REQUIRE_VM and not _vm_reachable(),
    reason=f"no VictoriaMetrics at {VM_ENDPOINT} (CI job provides it + sets "
           f"VM_PARITY_REQUIRE=1; skipped locally — set VM_PARITY_ENDPOINT to run)",
)


def _require_vm_or_fail() -> None:
    if _REQUIRE_VM and not _vm_reachable():
        pytest.fail(f"VM_PARITY_REQUIRE=1 but no VictoriaMetrics at {VM_ENDPOINT} — the "
                    f"parity job must not silently skip to green (service container down?)")


# ---- promtool series-values notation --------------------------------------
def _expand_values(spec: str) -> list[float | None]:
    """Expand promtool `values:` notation → list of samples (None == gap `_`).

    Grammar: space-separated tokens, each: `v` | `_` gap | `vxN` | `v+dxN` | `v-dxN`.
    `v+dxN` = v, v+d, ..., v+N*d  (N+1 samples). `vxN` = v repeated N+1 times.
    Scientific notation (`1e-3`) is supported as a plain value.
    """
    out: list[float | None] = []
    for tok in spec.split():
        if tok == "_":
            out.append(None)
            continue
        if "x" in tok:
            base, count_s = tok.rsplit("x", 1)
            count = int(count_s)
            delta = 0.0
            if "+" in base:                       # `a+d` incrementing
                a_s, d_s = base.split("+", 1)
                a, delta = float(a_s), float(d_s)
            else:
                try:
                    a = float(base)               # plain repeat (incl `1e-3`, `-5`)
                except ValueError:                # `a-d` decrementing (a may be negative)
                    lead = "-" if base.startswith("-") else ""
                    a_s, d_s = base[len(lead):].split("-", 1)
                    a, delta = float(lead + a_s), -float(d_s)
            for i in range(count + 1):
                out.append(a + i * delta)
        else:
            out.append(float(tok))
    return out


# ---- VM import / query -----------------------------------------------------
def _import_lines(lines: list[str]) -> None:
    if not lines:
        return
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(
        f"{VM_ENDPOINT}/api/v1/import/prometheus", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status in (200, 204), f"VM import failed: {r.status}"


def _import_series(series_label_str: str, values: list[float | None],
                   t0_ms: int, interval_ms: int) -> None:
    """Import one series to VM (line per sample, absolute ms timestamps)."""
    _import_lines([f"{series_label_str} {v} {t0_ms + i * interval_ms}"
                   for i, v in enumerate(values) if v is not None])


def _flush_import() -> None:
    """Force VM to flush in-memory buffers so just-imported data is queryable.
    Single-node-only endpoint; when VM is REQUIRED (CI), a flush failure is fatal
    (a cluster VM without this endpoint would race unflushed data into false
    divergences) — never silently degrade."""
    try:
        with urllib.request.urlopen(
                f"{VM_ENDPOINT}/internal/force_flush", timeout=10) as r:
            r.read()
    except Exception:
        if _REQUIRE_VM:
            raise


def _query(expr: str, at_s: int) -> list[dict]:
    """Instant query VM at absolute time `at_s`; nocache=1 (Gemini trap #2)."""
    qs = urllib.parse.urlencode({"query": expr, "time": str(at_s), "nocache": "1"})
    req = urllib.request.Request(f"{VM_ENDPOINT}/api/v1/query?{qs}")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    assert data.get("status") == "success", f"VM query error: {data}"
    return data["data"]["result"]


# ---- fixture / rule-pack parsing ------------------------------------------
def _load_packs(fixture_path: Path) -> list[dict]:
    doc = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    return [yaml.safe_load((fixture_path.parent / rel).resolve().read_text(encoding="utf-8"))
            for rel in doc["rule_files"]]


def _find_alert(packs: list[dict], alertname: str) -> tuple[str, dict]:
    for pack in packs:
        for grp in pack.get("groups", []):
            for rule in grp.get("rules", []):
                if rule.get("alert") == alertname:
                    return rule["expr"], dict(rule.get("labels") or {})
    raise AssertionError(f"alert {alertname!r} not found in rule_files")


def _materialize_recording_rules(packs: list[dict], at_s: int) -> int:
    """Evaluate every `record:` rule on VM at `at_s` and write the result back, in
    pack/group/rule order — mirroring Prometheus's sequential evaluation so a
    multi-layer chain (leaf → recording → alert) resolves. Returns the number of
    recording-rule series written, so a caller can assert the chain actually ran
    (an empty alert result is then genuine no-fire, not a silent no-op). Also runs
    the recording rules' OWN PromQL through the engine (execution check; their
    results are validated transitively via the final alert / `promql_expr_test`).
    Instant-only: a recording rule consumed over a RANGE by another isn't modelled
    (no chosen fixture needs it)."""
    at_ms = at_s * 1000
    written = 0
    for pack in packs:
        for grp in pack.get("groups", []):
            for rule in grp.get("rules", []):
                rec = rule.get("record")
                if not rec:
                    continue
                rule_lines = []
                for r in _query(rule["expr"], at_s):
                    labels = {k: v for k, v in r["metric"].items() if k != "__name__"}
                    lbl = ("{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"
                           ) if labels else ""
                    rule_lines.append(f"{rec}{lbl} {r['value'][1]} {at_ms}")
                if rule_lines:
                    _import_lines(rule_lines)
                    _flush_import()   # make this rule visible to the next in the chain
                    written += len(rule_lines)
    return written


_LBL_RE = re.compile(r'(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"')


def _parse_series_str(s: str) -> frozenset:
    """Parse a promtool series string `name{k="v",...}` → label frozenset (no name)."""
    brace = s[s.index("{"):] if "{" in s else ""
    return frozenset(_LBL_RE.findall(brace))


def _labelset(metric: dict) -> frozenset:
    return frozenset((k, v) for k, v in metric.items() if k != "__name__")


def _fmt(sets) -> list:
    """Render a set of label-set frozensets readably (sortable for diff output)."""
    return sorted(tuple(sorted(s)) for s in sets)


def _worker_offset() -> int:
    """Per-xdist-worker slot offset so parallel workers sharing one VM can't collide."""
    w = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return int("".join(c for c in w if c.isdigit()) or "0") * _WORKER_SPAN


def _window_s(group_id: int, ti: int) -> int:
    """Unique, DETERMINISTIC ingest-window start for one (worker, case, test-block).
    group_id is a global per-case index; each window is _GAP apart (>> VM staleness)
    so no two logical tests cross-talk, and re-runs re-import identically (idempotent)."""
    slot = group_id * _MAX_BLOCKS + ti
    assert slot < _WORKER_SPAN, f"slot {slot} overflows worker span (raise _WORKER_SPAN)"
    return _T0 + (_worker_offset() + slot) * _GAP


def _parse_dur(d) -> int:
    """Minimal promtool duration → seconds (`15s`,`5m`,`1h`,`2h30m`...)."""
    if isinstance(d, (int, float)):
        return int(d)
    total, num = 0, ""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for ch in str(d):
        if ch.isdigit():
            num += ch
        elif ch in units:
            total += int(num or 0) * units[ch]
            num = ""
    return total or int(num or 0)


# ---- alert-decision parity (label-sets + fire/no-fire) ---------------------
@_needs_vm
@pytest.mark.parametrize("fixture_name,alertname", _CASES,
                         ids=[f"{f}:{a}" for f, a in _CASES])
def test_vm_alert_decision_parity(fixture_name, alertname):
    _require_vm_or_fail()
    fpath = _FIXTURE_DIR / fixture_name
    packs = _load_packs(fpath)
    expr, static_labels = _find_alert(packs, alertname)
    doc = yaml.safe_load(fpath.read_text(encoding="utf-8"))
    group_id = _CASES.index((fixture_name, alertname))

    saw_block = False
    for ti, test in enumerate(doc["tests"]):
        art = [a for a in test.get("alert_rule_test", []) if a.get("alertname") == alertname]
        if not art:
            continue
        saw_block = True
        interval_ms = _parse_dur(test.get("interval", doc.get("evaluation_interval", "1m"))) * 1000
        window_s = _window_s(group_id, ti)
        t0_ms = window_s * 1000

        for s in test["input_series"]:
            _import_series(s["series"], _expand_values(s["values"]), t0_ms, interval_ms)
        _flush_import()

        for case in art:
            at_s = window_s + _parse_dur(case["eval_time"])
            written = _materialize_recording_rules(packs, at_s)
            # plumbing guard: the chain MUST have produced something — otherwise an
            # empty alert result is a silent no-op masquerading as "no-fire" (false green).
            assert written > 0, (
                f"{fixture_name} [{alertname}] test#{ti}: recording-rule chain wrote "
                f"0 series — plumbing no-op'd, so a no-fire result would be meaningless")
            got = {_labelset(r["metric"]) for r in _query(expr, at_s)}
            # Expected expr-output label-sets = golden exp_labels MINUS the LITERAL
            # static labels the evaluator adds (e.g. `severity: warning`). Templated
            # passthroughs (`tenant: "{{ $labels.tenant }}"`) re-expose labels the expr
            # already produces, so they are retained.
            literal_static_keys = {k for k, v in static_labels.items() if "{{" not in str(v)}
            want = {
                frozenset((k, v) for k, v in (a.get("exp_labels") or {}).items()
                          if k not in literal_static_keys)
                for a in (case.get("exp_alerts") or [])
            }
            assert got == want, (
                f"VM↔Prometheus alert-decision divergence in {fixture_name} [{alertname}] "
                f"test#{ti} @ {case['eval_time']}:\n  expr: {expr}\n"
                f"  VM produced: {_fmt(got)}\n  Prometheus golden expects: {_fmt(want)}\n"
                f"  (missing on VM: {_fmt(want - got)}; extra on VM: {_fmt(got - want)})"
            )
    assert saw_block, f"no alert_rule_test for {alertname!r} in {fixture_name} (stale _CASES?)"


# ---- recording-rule VALUE parity (label-sets + values, epsilon) -----------
@_needs_vm
@pytest.mark.parametrize("fixture_name,expr", _EXPR_CASES,
                         ids=[f"{f}:{e}" for f, e in _EXPR_CASES])
def test_vm_expr_value_parity(fixture_name, expr):
    _require_vm_or_fail()
    fpath = _FIXTURE_DIR / fixture_name
    packs = _load_packs(fpath)
    doc = yaml.safe_load(fpath.read_text(encoding="utf-8"))
    group_id = len(_CASES) + _EXPR_CASES.index((fixture_name, expr))

    saw_block = False
    for ti, test in enumerate(doc["tests"]):
        pet = [c for c in test.get("promql_expr_test", []) if c.get("expr") == expr]
        if not pet:
            continue
        saw_block = True
        interval_ms = _parse_dur(test.get("interval", doc.get("evaluation_interval", "1m"))) * 1000
        window_s = _window_s(group_id, ti)
        t0_ms = window_s * 1000

        for s in test["input_series"]:
            _import_series(s["series"], _expand_values(s["values"]), t0_ms, interval_ms)
        _flush_import()

        for case in pet:
            at_s = window_s + _parse_dur(case["eval_time"])
            _materialize_recording_rules(packs, at_s)
            got = {_labelset(r["metric"]): float(r["value"][1]) for r in _query(expr, at_s)}
            want = {_parse_series_str(s["labels"]): float(s["value"])
                    for s in case["exp_samples"]}
            assert set(got) == set(want), (
                f"VM↔Prometheus label-set divergence in {fixture_name} [{expr}] test#{ti}:\n"
                f"  VM: {_fmt(set(got))}\n  golden: {_fmt(set(want))}")
            for ls, exp_v in want.items():
                assert abs(got[ls] - exp_v) < _EPSILON, (
                    f"VM↔Prometheus VALUE divergence in {fixture_name} [{expr}] "
                    f"test#{ti} for {dict(ls)}: VM={got[ls]} golden={exp_v}")
    assert saw_block, f"no promql_expr_test for {expr!r} in {fixture_name} (stale _EXPR_CASES?)"


@_needs_vm
def test_harness_measures_real_vm_result():
    """Positive control: prove import→query→compare reflects VM's ACTUAL evaluation
    (not a no-op) and exercise the float-epsilon path. A known probe `p{...}=5`,
    queried as `p*2`, must return exactly one series with the probe's labels and
    value 10±ε. If this drifts, every parity assertion above is suspect."""
    _require_vm_or_fail()
    window = _window_s(len(_CASES) + len(_EXPR_CASES), 0)
    _import_lines([f'parity_selftest_probe{{tenant="x",k="v"}} 5 {window * 1000}'])
    _flush_import()
    res = _query("parity_selftest_probe * 2", window)
    assert len(res) == 1, f"expected exactly 1 series, got {res}"
    assert _labelset(res[0]["metric"]) == frozenset({("tenant", "x"), ("k", "v")})
    assert abs(float(res[0]["value"][1]) - 10.0) < _EPSILON, res[0]["value"]


# ---- pure-function unit tests (always run; no VM needed) -------------------
@pytest.mark.parametrize("spec,expected", [
    ("1x3", [1.0, 1.0, 1.0, 1.0]),            # repeat (N+1 samples)
    ("0+13.5x2", [0.0, 13.5, 27.0]),          # incrementing counter
    ("10-2x2", [10.0, 8.0, 6.0]),             # decrementing
    ("-5x1", [-5.0, -5.0]),                    # leading-negative base
    ("1e-3x2", [1e-3, 1e-3, 1e-3]),           # scientific notation + repeat (no crash)
    ("5", [5.0]),                              # single literal
    ("1 2 3", [1.0, 2.0, 3.0]),               # space-separated literals
    ("1 _ 3", [1.0, None, 3.0]),              # gap
])
def test_expand_values(spec, expected):
    assert _expand_values(spec) == expected


@pytest.mark.parametrize("dur,sec", [
    ("15s", 15), ("5m", 300), ("11m", 660), ("1h", 3600), ("2h30m", 9000), (60, 60),
])
def test_parse_dur(dur, sec):
    assert _parse_dur(dur) == sec


@pytest.mark.parametrize("s,expected", [
    ('m{tenant="t1"}', frozenset({("tenant", "t1")})),
    ('a:b:c{tenant="t1",severity="warning"}', frozenset({("tenant", "t1"), ("severity", "warning")})),
    ('bare_metric', frozenset()),
])
def test_parse_series_str(s, expected):
    assert _parse_series_str(s) == expected
