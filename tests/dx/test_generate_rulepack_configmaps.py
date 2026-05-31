"""Tests for generate_rulepack_configmaps.py — configmap generator (PR3-pre-2).

Pinned contracts
----------------
1. **Split**: record-only groups → `<pack>-recording.yml`; alert-bearing
   groups → `<pack>-alert.yml`.
2. **Metadata**: ConfigMap name/namespace/labels/data-key naming match the
   layout consumed by deployment-prometheus.yaml's projected volume.
3. **Round-trip fidelity**: the generated configmap's rules are SEMANTICALLY
   identical to the source groups (no rule lost/altered).
4. **--check**: a stale configmap is flagged; a freshly-generated one passes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
_LINT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _DX)
sys.path.insert(0, _LINT)

import generate_rulepack_configmaps as gen  # noqa: E402
import check_rulepack_sync as sync  # noqa: E402

_SRC = {
    "groups": [
        {"name": "t-normalization", "interval": "15s", "rules": [
            {"record": "tenant:x:rate", "expr": "rate(x[5m])"}]},
        {"name": "t-threshold-normalization", "interval": "15s", "rules": [
            {"record": "tenant:alert_threshold:x", "expr": "max by(tenant) (user_threshold{metric=\"x\"})"}]},
        {"name": "t-alerts", "rules": [
            {"alert": "XHigh", "expr": "tenant:x:rate > on(tenant) tenant:alert_threshold:x",
             "labels": {"severity": "warning", "tenant": "{{ $labels.tenant }}"},
             "annotations": {"summary": "x high", "summary_zh": "x 高"}}]},
    ]
}


def test_split_recording_vs_alert():
    cm = gen.build_configmap("t", _SRC)
    assert set(cm["data"]) == {"t-recording.yml", "t-alert.yml"}
    rec = yaml.safe_load(cm["data"]["t-recording.yml"])
    alr = yaml.safe_load(cm["data"]["t-alert.yml"])
    rec_groups = {g["name"] for g in rec["groups"]}
    alr_groups = {g["name"] for g in alr["groups"]}
    assert rec_groups == {"t-normalization", "t-threshold-normalization"}
    assert alr_groups == {"t-alerts"}


def test_metadata():
    cm = gen.build_configmap("redis", _SRC)
    assert cm["metadata"]["name"] == "prometheus-rules-redis"
    assert cm["metadata"]["namespace"] == "monitoring"
    assert cm["metadata"]["labels"] == {"app": "prometheus", "rule-pack": "redis"}


def test_round_trip_semantic_fidelity():
    """Generated configmap rules == source rules (semantically)."""
    cm = gen.build_configmap("t", _SRC)
    gen_groups = []
    for v in cm["data"].values():
        gen_groups.extend(yaml.safe_load(v)["groups"])
    src_map = sync._extract(_SRC["groups"])
    gen_map = sync._extract(gen_groups)
    assert sync._diff_maps(src_map, gen_map) == []


def test_check_detects_stale(tmp_path, monkeypatch):
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "k8s" / "03-monitoring").mkdir(parents=True)
    (tmp_path / "rule-packs" / "rule-pack-t.yaml").write_text(yaml.dump(_SRC), encoding="utf-8")
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)

    # No committed configmap yet → drift (missing everything).
    monkeypatch.setattr(sys, "argv", ["g", "--check"])
    assert gen.main() == 1

    # Generate it, then --check passes.
    monkeypatch.setattr(sys, "argv", ["g"])
    assert gen.main() == 0
    monkeypatch.setattr(sys, "argv", ["g", "--check"])
    assert gen.main() == 0


def test_pack_filter_scopes_to_one(tmp_path, monkeypatch):
    """--pack restricts (re)generation to named pack(s) — ADR-024 pilot scoping.

    A pilot must regenerate ONLY its pack without disturbing deferred packs.
    """
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "k8s" / "03-monitoring").mkdir(parents=True)
    (tmp_path / "rule-packs" / "rule-pack-alpha.yaml").write_text(yaml.dump(_SRC), encoding="utf-8")
    (tmp_path / "rule-packs" / "rule-pack-beta.yaml").write_text(yaml.dump(_SRC), encoding="utf-8")
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)
    out = tmp_path / "k8s" / "03-monitoring"

    # --pack alpha writes ONLY alpha's configmap, leaving beta absent.
    monkeypatch.setattr(sys, "argv", ["g", "--pack", "alpha"])
    assert gen.main() == 0
    assert (out / "configmap-rules-alpha.yaml").exists()
    assert not (out / "configmap-rules-beta.yaml").exists()

    # Unknown pack name is a hard error (exit 2), not a silent no-op.
    monkeypatch.setattr(sys, "argv", ["g", "--pack", "ghost"])
    assert gen.main() == 2
