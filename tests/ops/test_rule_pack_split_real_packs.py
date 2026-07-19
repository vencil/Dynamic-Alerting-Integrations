"""Real-pack regression + drift gate for generate_rule_pack_split.py.

Distinct from test_generate_rule_pack_split.py (which unit-tests the pure
helpers + orchestrator against monkeypatched fixtures): this module exercises
the tool END-TO-END against the ACTUAL repo `rule-packs/`.

Why it exists
-------------
The shipped `da-tools rule-pack-split` command returned EXIT_VIOLATION (1) on
the real repo packs — of 16 packs, 14 tripped EXIT_VIOLATION and 2 (liveness,
operational) were silently emptied by the split — from v2.3.0 until the
defect-i/ii/iii fix. Nothing in CI ever ran the tool against the real packs,
so a systematic tool bug (not a data error) went unnoticed for ~4 years.
These tests codify the missing coverage so the tool cannot silently re-break:

  * exit-0 smoke gate — the tool must split the real packs cleanly, and
  * golden preservation — the split must not silently DROP whole groups
    (defect iii lost 8 groups across 4 packs, incl. the entire liveness +
    operational packs and kubernetes' node-health / ha-replicas / sentinels /
    state-matching recording group).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULE_PACKS = _REPO_ROOT / "rule-packs"
_TOOL = _REPO_ROOT / "scripts" / "tools" / "ops" / "generate_rule_pack_split.py"

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops")
sys.path.insert(0, _TOOLS_DIR)

import generate_rule_pack_split as grps  # noqa: E402


def _load_all_packs():
    for pack in sorted(_RULE_PACKS.glob("rule-pack-*.yaml")):
        data = grps.load_rule_pack(str(pack))
        yield pack, data.get("groups", [])


class TestRealPackSmokeGate:
    def test_real_packs_split_cleanly_exit_zero(self):
        # cp950 host: force utf-8 for the child (report JSON carries `→`).
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.run(
            [
                sys.executable, str(_TOOL),
                "--rule-packs-dir", str(_RULE_PACKS),
                "--dry-run", "--json",
            ],
            capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(_REPO_ROOT), timeout=120,
        )
        if proc.returncode != 0:
            try:
                report = json.loads(proc.stdout)
                detail = report.get("validation", {}).get("metric_mismatches", [])
            except (ValueError, json.JSONDecodeError):
                detail = (proc.stdout or "") + (proc.stderr or "")
            pytest.fail(
                f"rule-pack-split exited {proc.returncode} on real packs "
                f"(expected 0); mismatches: {detail}"
            )
        assert proc.returncode == 0

    def test_real_packs_report_has_no_metric_mismatches(self):
        report = grps.process_rule_packs(
            str(_RULE_PACKS), str(_REPO_ROOT / "_never_written"), dry_run=True,
        )
        assert report["validation"]["metric_mismatches"] == []
        assert report["status"] == "success"


class TestNoGroupDropped:
    def test_every_input_group_appears_in_split_output(self):
        # Defect iii guard: the split must never discard a group. A MIXED
        # non-suffix group is split PER RULE and so appears on BOTH planes (same
        # name) — compare as sets: every input group name must appear, none lost.
        for pack, groups in _load_all_packs():
            edge, central = grps.split_rule_pack(groups)
            in_names = {g["name"] for g in groups}
            out_names = {g["name"] for g in (edge + central)}
            assert in_names == out_names, (
                f"{pack.name}: split dropped groups "
                f"{sorted(in_names - out_names)}"
            )

    def test_every_rule_lands_exactly_once(self):
        # Per-rule split must be a partition of rules too — no rule dropped, none
        # duplicated across planes.
        for pack, groups in _load_all_packs():
            edge, central = grps.split_rule_pack(groups)
            n_in = sum(len(g.get("rules", [])) for g in groups)
            n_out = sum(len(g.get("rules", [])) for g in (edge + central))
            assert n_in == n_out, f"{pack.name}: {n_in} rules in, {n_out} out"

    def test_liveness_and_operational_packs_not_emptied(self):
        # Both packs' only group has a non-standard suffix — before the fix the
        # split produced EMPTY output for them (whole packs lost).
        for name in ("rule-pack-liveness.yaml", "rule-pack-operational.yaml"):
            data = grps.load_rule_pack(str(_RULE_PACKS / name))
            edge, central = grps.split_rule_pack(data["groups"])
            assert edge + central, f"{name}: split produced no groups"


class TestKubernetesGoldenPreservation:
    def _kube_split(self):
        data = grps.load_rule_pack(str(_RULE_PACKS / "rule-pack-kubernetes.yaml"))
        return grps.split_rule_pack(data["groups"])

    def test_state_matching_group_preserved(self):
        edge, central = self._kube_split()
        names = {g["name"] for g in (edge + central)}
        # Before the fix this recording group was silently dropped.
        assert "kubernetes-state-matching" in names
        # It normalises raw kube-state-metrics → routed to the edge side.
        assert "kubernetes-state-matching" in {g["name"] for g in edge}

    def test_state_matching_recording_output_survives(self):
        edge, central = self._kube_split()
        records = {
            r["record"]
            for g in (edge + central)
            for r in g.get("rules", [])
            if "record" in r
        }
        # Consumed by the central ContainerCrashLoop / ContainerImagePullFailure
        # alerts — its loss made those alerts dangling.
        assert "tenant:container_waiting_reason:count" in records

    def test_pure_alert_sentinels_on_central(self):
        edge, central = self._kube_split()
        central_names = {g["name"] for g in central}
        for name in ("kubernetes-version-aware-sentinel",
                     "kubernetes-custom-disk-recipe-sentinel"):
            assert name in central_names, name

    def test_mixed_groups_split_recordings_to_edge_alerts_to_central(self):
        # Correctness fix (per-rule data-locality): node-health / ha-replicas
        # recordings read namespace-labeled raw kube_* → MUST land on edge (where
        # raw exists); their alert reads the federated :core recording → central.
        # Whole-group-to-central would make the recordings produce nothing at
        # central → NodeNotReady / TenantHAReplicasDegraded would never fire.
        edge, central = self._kube_split()

        def rules_for(groups, gname, kind):
            return [
                r[kind] for g in groups if g["name"] == gname
                for r in g.get("rules", []) if kind in r
            ]

        # node-health: 2 recordings on edge, 1 alert on central.
        assert rules_for(edge, "kubernetes-node-health", "record") == [
            "tenant:node_owner:info", "rule_pack_kubernetes:node_not_ready:core",
        ]
        assert rules_for(central, "kubernetes-node-health", "alert") == ["NodeNotReady"]
        assert rules_for(central, "kubernetes-node-health", "record") == []

        # ha-replicas: recording on edge, alert on central.
        assert rules_for(edge, "kubernetes-ha-replicas", "record") == [
            "rule_pack_kubernetes:ha_replicas_degraded:core",
        ]
        assert rules_for(central, "kubernetes-ha-replicas", "alert") == [
            "TenantHAReplicasDegraded",
        ]


class TestCentralRawExemptionLedger:
    def test_only_two_kube_sentinels_grandfathered(self):
        # The ledger is exactly the 2 namespace-labeled KSM sentinels — per-tenant
        # exporter raw (db liveness) is federated and must NOT be listed.
        assert set(grps.KNOWN_CENTRAL_RAW_EXEMPTIONS) == {"rule-pack-kubernetes.yaml"}
        assert set(grps.KNOWN_CENTRAL_RAW_EXEMPTIONS["rule-pack-kubernetes.yaml"]) == {
            "kube_pod_info", "kube_pod_labels", "kube_pod_status_phase",
            "kubelet_volume_stats_available_bytes",
            "kubelet_volume_stats_capacity_bytes",
        }

    def test_grandfathered_kube_sentinels_do_not_fail_the_run(self, monkeypatch):
        report = grps.process_rule_packs(
            str(_RULE_PACKS), str(_REPO_ROOT / "_never_written"), dry_run=True,
        )
        assert report["validation"]["metric_mismatches"] == []
        # …but each exemption is announced fail-loud.
        assert any("KNOWN raw-on-central" in w for w in report["warnings"])

    def test_new_unlisted_kube_on_central_fails(self, monkeypatch):
        # Meta-test / mutation guard: the ledger is NOT a blanket escape hatch.
        # Emptying it makes the (now unlisted) kube sentinels genuine violations
        # → exit 1. Proves a NEW kube_/kubelet_-on-central reference is caught.
        monkeypatch.setattr(grps, "KNOWN_CENTRAL_RAW_EXEMPTIONS", {})
        report = grps.process_rule_packs(
            str(_RULE_PACKS), str(_REPO_ROOT / "_never_written"), dry_run=True,
        )
        mismatches = report["validation"]["metric_mismatches"]
        assert any(m["file"] == "rule-pack-kubernetes.yaml" for m in mismatches)
        kube = next(m for m in mismatches if m["file"] == "rule-pack-kubernetes.yaml")
        assert "kube_pod_info" in kube["missing_in_edge"]
