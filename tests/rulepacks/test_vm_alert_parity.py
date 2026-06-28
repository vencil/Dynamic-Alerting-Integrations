"""Dual-engine rule-pack alerting parity: run every promtool unit-test fixture through
VictoriaMetrics' ``vmalert-tool unittest`` (the MetricsQL engine vmalert runs in
production) and require parity with Prometheus (promtool) EXCEPT for divergences that are
explicitly catalogued in ``vm_deviation_catalog.yaml``.

Why this exists (see docs/integration/victoriametrics-integration.md known-limitations):
MetricsQL deviates from PromQL *by design* (rate / increase / changes at series cold-start,
etc.; VM explicitly does not target 100% PromQL compatibility). A fixture that is green on
promtool can therefore diverge on vmalert, so VM-backend tenants need the production engine
in the loop. promtool stays the Prometheus-backend oracle (Makefile ``rulepack-promtool-test`` + CI
"Lint Rule Packs"); this is the SOLE per-PR VictoriaMetrics-backend oracle — the full fixture
set (fire/no-fire + labels + annotations + ``for:`` + range-function layer) on the MetricsQL
engine. The on-demand ``test_vm_backend_parity.py`` anchor (run on VM-version bumps) licenses
trusting this in-memory tool as a real-vmsingle proxy by cross-checking the two at the pinned
engine version (#947 consolidation).

Gate semantics:
  * uncatalogued fixture diverges on vmalert  -> FAIL (a new, undocumented divergence)
  * catalogued fixture still diverges         -> pass (documented + accepted)
  * catalogued fixture now passes             -> FAIL (stale entry; remove it so the
                                                 catalog stays == reality)

Scope (honest boundary): this is **in-memory rule-evaluation parity** — the MetricsQL math
+ the ``for:`` state machine on synthetic ``input_series``. It does NOT replicate real-TSDB
storage semantics: stale markers, scrape-gap handling and ``-search.maxStalenessInterval``
are modelled by the unittest harness, not the production vmstorage. The MetricsQL math is
verified bit-identical to a live vmsingle (rate cold-start spot-check), but the staleness/
gap *timing* is not, so a storage-layer staleness / absence-over-real-gaps divergence can
still pass here. Full storage-layer parity needs ``vmalert -replay`` against a real vmsingle
over real gaps and stays deferred (Phase 2; tracked in #947 + ADR-025).

vmalert-tool is located via $VMALERT_TOOL, then PATH (vmalert-tool / vmalert-tool-prod),
then the dev-container default /tmp/vm/vmalert-tool-prod. If none is found the whole module
is skipped (CI provisions it like promtool).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_HERE = Path(__file__).resolve().parent          # <repo>/tests/rulepacks
_REPO = _HERE.parents[1]                          # <repo>
_RULE_PACKS = _REPO / "rule-packs"
_CATALOG = _HERE / "vm_deviation_catalog.yaml"


def _find_vmalert_tool() -> str | None:
    env = os.environ.get("VMALERT_TOOL")
    if env and Path(env).exists():
        return env
    for name in ("vmalert-tool", "vmalert-tool-prod"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path("/tmp/vm/vmalert-tool-prod")  # jitter-harness dev-container download
    return str(fallback) if fallback.exists() else None


_VMALERT_TOOL = _find_vmalert_tool()

pytestmark = pytest.mark.skipif(
    _VMALERT_TOOL is None,
    reason="vmalert-tool not found (set $VMALERT_TOOL or put it on PATH); CI provisions it",
)


def _catalogued_fixtures() -> set[str]:
    if not _CATALOG.exists():
        return set()
    data = yaml.safe_load(_CATALOG.read_text(encoding="utf-8")) or {}
    return {d["fixture"] for d in (data.get("deviations") or [])}


def _fixtures() -> list[str]:
    return sorted(p.name for p in _HERE.glob("*_test.yaml"))


@pytest.fixture(scope="session")
def xlate_dir(tmp_path_factory) -> Path:
    """Copy tests/rulepacks/*.yaml into a tmp dir, transforming each ``*_test.yaml`` so it
    is consumable by vmalert-tool: rename ``promql_expr_test`` -> ``metricsql_expr_test``
    (VM uses the latter key) and resolve ``../../rule-packs/`` to an absolute path. Sibling
    ``*.rules.yaml`` are copied alongside so relative ``rule_files`` still resolve — a
    single-file copy would make those silently "not found" and look like a divergence.
    """
    dst = tmp_path_factory.mktemp("rulepacks_vm")
    for src in _HERE.glob("*.yaml"):
        shutil.copy2(src, dst / src.name)
    for fixture in dst.glob("*_test.yaml"):
        text = fixture.read_text(encoding="utf-8")
        text = text.replace("promql_expr_test", "metricsql_expr_test")
        text = text.replace("../../rule-packs/", f"{_RULE_PACKS.as_posix()}/")
        fixture.write_text(text, encoding="utf-8")
    return dst


@pytest.mark.parametrize("fixture", _fixtures())
def test_rulepack_parity_on_vmalert(fixture: str, xlate_dir: Path) -> None:
    catalogued = fixture in _catalogued_fixtures()
    proc = subprocess.run(
        [
            _VMALERT_TOOL, "unittest", "--disableAlertgroupLabel",
            f"--files={(xlate_dir / fixture).as_posix()}",
        ],
        cwd=str(xlate_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    diverged = proc.returncode != 0

    if catalogued:
        assert diverged, (
            f"{fixture} is listed in vm_deviation_catalog.yaml but now PASSES on "
            f"vmalert-tool — the divergence appears healed. Remove the stale catalog entry "
            f"so the catalog stays in sync with reality."
        )
    else:
        # Distinguish a real MetricsQL behavioural divergence from a harness/format gap:
        # the promql_expr_test -> metricsql_expr_test key rename is a blunt string swap, so a
        # future promtool-only schema field would make vmalert-tool fail to PARSE (non-zero)
        # without any behavioural divergence. Don't mislabel that as a rule divergence.
        schema_err = "unmarshal" in proc.stderr or "not found in type" in proc.stderr
        reason = (
            "could NOT be parsed by vmalert-tool — schema drift / a construct the "
            "promql->metricsql key rename does not cover. This is a HARNESS/format gap, "
            "NOT a MetricsQL behavioural divergence: fix the xlate step or the fixture."
            if schema_err else
            "DIVERGES on vmalert-tool (MetricsQL) but is green on promtool (PromQL). For "
            "VM-backend tenants this rule behaves differently than its promtool unit test "
            "asserts. Either make the rule backend-portable, or add an entry to "
            "tests/rulepacks/vm_deviation_catalog.yaml (mechanism + prod impact + disposition)."
        )
        assert not diverged, (
            f"{fixture} {reason}\n\n"
            f"--- vmalert-tool stdout ---\n{proc.stdout}\n"
            f"--- vmalert-tool stderr ---\n{proc.stderr}"
        )


def test_gate_detects_a_known_divergence(tmp_path: Path) -> None:
    """Teeth check: the harness must actually flag a known MetricsQL cold-start divergence,
    so a future vmalert-tool flag/format change cannot silently turn the parity gate into a
    no-op. rate(c[1m]) @30s on a fresh counter is 1.667 on promtool (no fire) but 3.333 on
    vmalert (fires), so an ``exp_alerts: []`` assertion at 30s must FAIL on vmalert-tool."""
    (tmp_path / "rules.yml").write_text(
        "groups:\n"
        "  - name: teeth\n"
        "    rules:\n"
        "      - alert: ColdStartRate\n"
        "        expr: rate(c[1m]) > 3.2\n"
        "        for: 0s\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "teeth_test.yaml"
    fixture.write_text(
        "rule_files:\n  - rules.yml\n"
        "evaluation_interval: 15s\n"
        "tests:\n"
        "  - interval: 15s\n"
        "    input_series:\n"
        "      - series: 'c{instance=\"t\"}'\n"
        "        values: '0+50x16'\n"
        "    alert_rule_test:\n"
        "      - eval_time: 30s\n"
        "        alertname: ColdStartRate\n"
        "        exp_alerts: []\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_VMALERT_TOOL, "unittest", "--disableAlertgroupLabel",
         f"--files={fixture.as_posix()}"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, (
        "vmalert-tool did NOT flag the known rate() cold-start divergence — the parity "
        "gate may have become a no-op (vmalert-tool flag/format drift?).\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
