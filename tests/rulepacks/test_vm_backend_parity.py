"""vmalert-tool == live-vmsingle ENGINE-EQUIVALENCE ANCHOR (on-demand; ADR-025 backend
compatibility, Part 1).

Role (read first)
-----------------
This is NOT a per-PR gate. Per-PR VM rule-pack parity is owned SOLELY by the sibling
``test_vm_alert_parity.py`` (gate A): it runs the FULL fixture set through ``vmalert-tool
unittest`` — the MetricsQL engine production vmalert actually runs — on every PR
(informational step in "Lint Rule Packs"), catalog-gated by ``vm_deviation_catalog.yaml``.

This file is the on-demand ANCHOR that LICENSES treating gate A's in-memory ``vmalert-tool``
as a faithful proxy for a real ``vmsingle`` (storage + query). It imports a representative
subset of the SAME promtool goldens into a REAL VictoriaMetrics (docker, HTTP import + query)
and proves the live engine evaluates our COMPILED idioms — ``and on(...)`` topology joins,
``group_left`` enrichment, ``or vector(0)`` sentinels, ``label_replace``, ``max by(tenant)``
dedup, exact-match version joins, + recording-rule VALUE/epsilon — identically to the
Prometheus golden. Gate A separately proves ``vmalert-tool == golden`` on the full set; with
both at the SAME pinned engine version (test_engine_version_matches_pin), that gives
``vmalert-tool == vmsingle`` on these idioms, so gate A can stand alone per-PR.

Why on-demand, not a per-PR job (consolidation, #947)
-----------------------------------------------------
The old per-PR ``backend-compat-parity`` docker-VM job was redundant: its 3+1-case subset is
a SUBSET of gate A's full-set fire/no-fire + labels + annotations parity (gate A even checks
annotation templating this expr-level harness structurally cannot), and the one axis where
the two engines genuinely diverge — real-TSDB STALENESS / scrape-gap timing — is DEFERRED by
BOTH (this harness uses dense fixed-epoch series, GAP >> staleness). So per-PR it only
re-verified the shared-math instant layer gate A already covers more broadly. The equivalence
is a property of the PINNED engine version, so it only needs re-verifying when the pin
changes — not every PR (wasteful), not never (rots). Run it on a VM-version bump and in any
dev-container with a pinned vmsingle; it skips when no VM is reachable.

Honest residual — still UNCOVERED by either gate: real storage-layer staleness / ``absence``
over real gaps / ``predict_linear`` temporal semantics. The ADR-025 defer-trigger ("first
customer on their own backend") has FIRED (VM-migration customers); the remaining slice —
``vmalert -replay`` over real gaps against a real vmsingle — is now characterized on-demand by
``test_vm_replay_staleness.py`` (#947), not here (``predict_linear`` temporal semantics still open).

Reference & determinism (unchanged)
-----------------------------------
The promtool golden's asserted outcome IS the Prometheus reference (validated by the
``Rule-pack promtool unit tests`` CI step). We import each golden's ``input_series`` into VM,
materialize the recording-rule chain (so multi-layer leaf→recording→alert exprs resolve),
then compare VM's result to the golden. Each logical test ingests at a fixed, unique epoch
window (``T0 + slot*GAP``, GAP >> VM staleness, slot unique per worker×case×block) so tests
can't cross-talk and CI speed never shifts the result (Gemini trap #1); re-import is
idempotent. Queries pass ``nocache=1``; values compare with a float epsilon (Gemini trap #2).
Set VM_PARITY_REQUIRE=1 to force-run (unreachable VM then hard-fails, never skip-to-green).
"""
from __future__ import annotations

import os
import urllib.request
import re
from pathlib import Path

import pytest
import yaml

from vm_harness import (  # shared #968 harness — see vm_harness.py module docstring
    VMClient,
    expand_values,
    parse_dur,
    window_start,
)

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPO / "tests" / "rulepacks"

VM_ENDPOINT = os.environ.get("VM_PARITY_ENDPOINT", "http://localhost:8428")
_VM = VMClient(VM_ENDPOINT)
# Fixed-epoch slot layout (T0 / GAP / MAX_BLOCKS / WORKER_SPAN) lives in vm_harness;
# this anchor uses the harness defaults via window_start() (identical values to the
# pre-extraction _T0/_GAP/_MAX_BLOCKS/_WORKER_SPAN constants).
_EPSILON = 1e-6

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


# ON-DEMAND anchor (no per-PR CI job): skips when no VM is reachable (the normal case —
# local dev, python-tests). Set VM_PARITY_REQUIRE=1 to force it (unreachable VM then HARD-
# fails instead of skipping) — used when re-verifying equivalence on a VM-version bump
# (docs/internal/backend-compat-baseline.md).
_REQUIRE_VM = os.environ.get("VM_PARITY_REQUIRE") == "1"
_needs_vm = pytest.mark.skipif(
    not _REQUIRE_VM and not _VM.reachable(),
    reason=f"no VictoriaMetrics at {VM_ENDPOINT} (on-demand anchor; start a pinned vmsingle "
           f"+ set VM_PARITY_ENDPOINT, or VM_PARITY_REQUIRE=1 to force — see backend-compat-baseline.md)",
)


def _require_vm_or_fail() -> None:
    if _REQUIRE_VM and not _VM.reachable():
        pytest.fail(f"VM_PARITY_REQUIRE=1 but no VictoriaMetrics at {VM_ENDPOINT} — the "
                    f"equivalence anchor must not silently skip to green (vmsingle not started?)")


def _ssot_vm_version() -> str:
    """The single VM engine-version pin (tests/rulepacks/vm_engine_version), also sourced by
    the CI vmalert-tool install, so the gate's engine and this anchor's engine can't drift."""
    for line in (_FIXTURE_DIR / "vm_engine_version").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("VM_VERSION="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"VM_VERSION not found in {_FIXTURE_DIR / 'vm_engine_version'}")


def _vm_server_version() -> str | None:
    """Live vmsingle's semver from /metrics ``vm_app_version{...v<X.Y.Z>...}`` (label-name
    agnostic). None if unreachable / not exposed."""
    try:
        with urllib.request.urlopen(f"{VM_ENDPOINT}/metrics", timeout=5) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    line = re.search(r"vm_app_version\{[^}]*\}", text)
    if not line:
        return None
    ver = re.search(r"v(\d+\.\d+\.\d+)", line.group(0))
    return ver.group(1) if ver else None


# ---- VM flush policy (anchor-specific wrapper over vm_harness) -------------
def _flush_import() -> None:
    """Force VM to flush in-memory buffers so just-imported data is queryable.
    Single-node-only endpoint; when VM is REQUIRED (CI), a flush failure is fatal
    (a cluster VM without this endpoint would race unflushed data into false
    divergences) — never silently degrade."""
    try:
        _VM.flush()
    except Exception:
        if _REQUIRE_VM:
            raise


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
                for r in _VM.query_instant(rule["expr"], at_s):
                    labels = {k: v for k, v in r["metric"].items() if k != "__name__"}
                    lbl = ("{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"
                           ) if labels else ""
                    rule_lines.append(f"{rec}{lbl} {r['value'][1]} {at_ms}")
                if rule_lines:
                    _VM.import_prometheus(rule_lines)
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


# (worker_offset / window_start / parse_dur / expand_values now live in vm_harness)


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
        interval_ms = parse_dur(test.get("interval", doc.get("evaluation_interval", "1m"))) * 1000
        window_s = window_start(group_id, ti)
        t0_ms = window_s * 1000

        for s in test["input_series"]:
            _VM.import_series(s["series"], expand_values(s["values"]), t0_ms, interval_ms)
        _flush_import()

        for case in art:
            at_s = window_s + parse_dur(case["eval_time"])
            written = _materialize_recording_rules(packs, at_s)
            # plumbing guard: the chain MUST have produced something — otherwise an
            # empty alert result is a silent no-op masquerading as "no-fire" (false green).
            assert written > 0, (
                f"{fixture_name} [{alertname}] test#{ti}: recording-rule chain wrote "
                f"0 series — plumbing no-op'd, so a no-fire result would be meaningless")
            got = {_labelset(r["metric"]) for r in _VM.query_instant(expr, at_s)}
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
        interval_ms = parse_dur(test.get("interval", doc.get("evaluation_interval", "1m"))) * 1000
        window_s = window_start(group_id, ti)
        t0_ms = window_s * 1000

        for s in test["input_series"]:
            _VM.import_series(s["series"], expand_values(s["values"]), t0_ms, interval_ms)
        _flush_import()

        for case in pet:
            at_s = window_s + parse_dur(case["eval_time"])
            _materialize_recording_rules(packs, at_s)
            got = {_labelset(r["metric"]): float(r["value"][1]) for r in _VM.query_instant(expr, at_s)}
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
def test_engine_version_matches_pin():
    """Pin-coupling guard: this anchor only LICENSES trusting the per-PR vmalert-tool gate
    (test_vm_alert_parity.py) as a real-vmsingle proxy if the live vmsingle it validates runs
    the SAME engine version the gate's vmalert-tool does. Both read the one pin
    (tests/rulepacks/vm_engine_version); assert the running VM matches it, so a silent version
    drift can't make this anchor validate the wrong engine."""
    _require_vm_or_fail()
    want = _ssot_vm_version()
    got = _vm_server_version()
    assert got is not None, (
        f"could not read vm_app_version from {VM_ENDPOINT}/metrics — cannot confirm the live "
        f"vmsingle matches the pinned engine v{want}; the equivalence anchor must not run blind")
    assert got == want, (
        f"VM engine pin drift: anchor is validating vmsingle v{got} but the SSOT pin "
        f"(tests/rulepacks/vm_engine_version, also used by the CI vmalert-tool install) is "
        f"v{want}. Run the anchor against a v{want} vmsingle, or bump both in lockstep.")


@_needs_vm
def test_harness_measures_real_vm_result():
    """Positive control: prove import→query→compare reflects VM's ACTUAL evaluation
    (not a no-op) and exercise the float-epsilon path. A known probe `p{...}=5`,
    queried as `p*2`, must return exactly one series with the probe's labels and
    value 10±ε. If this drifts, every parity assertion above is suspect."""
    _require_vm_or_fail()
    window = window_start(len(_CASES) + len(_EXPR_CASES), 0)
    _VM.import_prometheus([f'parity_selftest_probe{{tenant="x",k="v"}} 5 {window * 1000}'])
    _flush_import()
    res = _VM.query_instant("parity_selftest_probe * 2", window)
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
    assert expand_values(spec) == expected


@pytest.mark.parametrize("dur,sec", [
    ("15s", 15), ("5m", 300), ("11m", 660), ("1h", 3600), ("2h30m", 9000), (60, 60),
])
def test_parse_dur(dur, sec):
    assert parse_dur(dur) == sec


@pytest.mark.parametrize("s,expected", [
    ('m{tenant="t1"}', frozenset({("tenant", "t1")})),
    ('a:b:c{tenant="t1",severity="warning"}', frozenset({("tenant", "t1"), ("severity", "warning")})),
    ('bare_metric', frozenset()),
])
def test_parse_series_str(s, expected):
    assert _parse_series_str(s) == expected
