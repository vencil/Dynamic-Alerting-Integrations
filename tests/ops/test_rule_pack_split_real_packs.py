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

    def test_sentinel_groups_split_recordings_to_edge_alerts_to_central(self):
        # #1168: the 2 KSM sentinels are MIXED groups now — their edge
        # normalisation recordings (tenant-labeled → federated) land on edge,
        # the alerts stay central and read ONLY those recordings.
        edge, central = self._kube_split()

        def rules_for(groups, gname, kind):
            return [
                r[kind] for g in groups if g["name"] == gname
                for r in g.get("rules", []) if kind in r
            ]

        assert rules_for(edge, "kubernetes-version-aware-sentinel", "record") == [
            "tenant:kube_pod_info:count", "tenant:kube_pod_labels:count",
        ]
        assert rules_for(central, "kubernetes-version-aware-sentinel", "alert") == [
            "VersionAwareThresholdInert",
        ]
        assert rules_for(central, "kubernetes-version-aware-sentinel", "record") == []

        assert rules_for(edge, "kubernetes-custom-disk-recipe-sentinel", "record") == [
            "tenant:kube_pods_running:count", "tenant:kubelet_volume_stats:count",
        ]
        assert rules_for(central, "kubernetes-custom-disk-recipe-sentinel", "alert") == [
            "CustomRecipeDiskInert",
        ]
        assert rules_for(central, "kubernetes-custom-disk-recipe-sentinel", "record") == []

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


_SYNTHETIC_RAW_ON_CENTRAL_PACK = (
    "groups:\n"
    "- name: synthetic-alerts\n"
    "  rules:\n"
    "  - alert: BadCentralRawKube\n"
    "    expr: count(kube_pod_info{namespace=~\"db-.+\"}) > 0\n"
)


class TestCentralRawExemptionLedger:
    def test_ledger_is_empty_after_sentinel_edge_normalisation(self):
        # #1168 exit-lock: both grandfathered KSM sentinels
        # (VersionAwareThresholdInert / CustomRecipeDiskInert) got edge
        # normalisation recordings, so NOTHING is exempt anymore. Growing this
        # ledger again is a reviewed, issue-tracked decision — not a convenience.
        assert grps.KNOWN_CENTRAL_RAW_EXEMPTIONS == {}

    def test_real_packs_emit_no_raw_on_central_warnings(self):
        # Ledger empty AND packs fixed → the run must be clean on BOTH channels:
        # no mismatches (exit-lock) and no grandfather WARNs left.
        report = grps.process_rule_packs(
            str(_RULE_PACKS), str(_REPO_ROOT / "_never_written"), dry_run=True,
        )
        assert report["validation"]["metric_mismatches"] == []
        assert not any("KNOWN raw-on-central" in w for w in report["warnings"])

    def test_new_kube_on_central_fails(self, tmp_path):
        # Meta-test / mutation guard: a NEW central alert reading raw
        # kube_*/kubelet_* (namespace-labeled → not federated) is a genuine
        # violation → metric mismatch → exit 1. Uses a synthetic pack because
        # the real packs no longer contain the defect (#1168 fixed both).
        (tmp_path / "rule-pack-synthetic.yaml").write_text(
            _SYNTHETIC_RAW_ON_CENTRAL_PACK, encoding="utf-8",
        )
        report = grps.process_rule_packs(
            str(tmp_path), str(tmp_path / "_never_written"), dry_run=True,
        )
        mismatches = report["validation"]["metric_mismatches"]
        kube = next(m for m in mismatches if m["file"] == "rule-pack-synthetic.yaml")
        assert "kube_pod_info" in kube["missing_in_edge"]

    def test_ledger_mechanism_still_grandfathers_listed_entries(
        self, tmp_path, monkeypatch,
    ):
        # The WARN-not-fail path must stay alive for a future audited entry:
        # the same synthetic defect, once LISTED, downgrades to a fail-loud
        # WARN with no mismatch (audited-ledger semantics preserved).
        (tmp_path / "rule-pack-synthetic.yaml").write_text(
            _SYNTHETIC_RAW_ON_CENTRAL_PACK, encoding="utf-8",
        )
        monkeypatch.setattr(grps, "KNOWN_CENTRAL_RAW_EXEMPTIONS", {
            "rule-pack-synthetic.yaml": {
                "kube_pod_info": "test-only: audited grandfather entry",
            },
        })
        report = grps.process_rule_packs(
            str(tmp_path), str(tmp_path / "_never_written"), dry_run=True,
        )
        assert report["validation"]["metric_mismatches"] == []
        assert any("KNOWN raw-on-central" in w for w in report["warnings"])
