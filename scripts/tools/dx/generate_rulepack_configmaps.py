#!/usr/bin/env python3
"""Generate k8s/03-monitoring/configmap-rules-<pack>.yaml from rule-packs/.

ADR-024 PR3-pre-2: makes the live-deploy ConfigMap copies a BUILD ARTIFACT
of the canonical `rule-packs/` source, ending the manual 3-way sync that let
a ~2-month drift ship silently (configmaps missing metadata enrichment /
bilingual / tenant-label; see #423 PR3-pre).

Each rule pack's groups are split across two ConfigMap data keys, matching
the existing layout consumed by k8s/03-monitoring/deployment-prometheus.yaml
(projected volume → /etc/prometheus/rules/, ADR-005):
  - `<pack>-recording.yml` — groups whose rules are `record:`
  - `<pack>-alert.yml`     — groups whose rules contain `alert:`

`--check` regenerates in memory and SEMANTICALLY compares against the
committed file (via check_rulepack_sync's serialization-agnostic
normalization), so a stale / hand-edited configmap is a hard failure. This
is robust to YAML-emitter / whitespace differences — only logical drift
fails (unlike a raw `git diff --exit-code`).

Exit codes:
    0  wrote files (default) / all in sync (--check)
    1  drift detected (--check)
    2  error
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "lint"))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
import check_rulepack_sync as sync  # noqa: E402

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def _has_alert(group: dict) -> bool:
    return any("alert" in r for r in group.get("rules", []))


def _dump_groups(groups: List[dict]) -> str:
    # width high so long single-line exprs are not wrapped mid-token.
    return yaml.dump(
        {"groups": groups},
        sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000,
    )


def build_configmap(name: str, rule_pack_data: dict, namespace: str = "monitoring") -> dict:
    """Build the ConfigMap dict for one rule pack (split recording/alert)."""
    groups = rule_pack_data.get("groups", [])
    recording = [g for g in groups if not _has_alert(g)]
    alerts = [g for g in groups if _has_alert(g)]
    data: Dict[str, str] = {}
    if recording:
        data[f"{name}-recording.yml"] = _dump_groups(recording)
    if alerts:
        data[f"{name}-alert.yml"] = _dump_groups(alerts)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"prometheus-rules-{name}",
            "namespace": namespace,
            "labels": {"app": "prometheus", "rule-pack": name},
        },
        "data": data,
    }


def render_configmap(name: str, rule_pack_data: dict, namespace: str = "monitoring") -> str:
    cm = build_configmap(name, rule_pack_data, namespace)
    return yaml.dump(cm, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _pack_name(path: Path) -> str:
    return path.stem.replace("rule-pack-", "")


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Generate rule-pack ConfigMaps from source")
    parser.add_argument("--check", action="store_true",
                        help="verify committed configmaps match source (semantic); exit 1 on drift")
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--pack", action="append", default=None,
                        help="restrict to specific pack name(s), e.g. --pack kubernetes "
                             "(repeatable). Default: all packs. Used to scope a pilot "
                             "regeneration without disturbing deferred packs.")
    args = parser.parse_args()

    repo = _repo_root()
    out_dir = repo / "k8s" / "03-monitoring"
    packs = sorted((repo / "rule-packs").glob("rule-pack-*.yaml"))
    if not packs:
        print("ERROR: no rule packs found", file=sys.stderr)
        return 2
    if args.pack:
        wanted = set(args.pack)
        packs = [p for p in packs if _pack_name(p) in wanted]
        missing = wanted - {_pack_name(p) for p in packs}
        if missing:
            print(f"ERROR: --pack name(s) not found: {sorted(missing)}", file=sys.stderr)
            return 2

    drift = []
    wrote = 0
    for pack in packs:
        name = _pack_name(pack)
        data = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
        cm = build_configmap(name, data, args.namespace)
        target = out_dir / f"configmap-rules-{name}.yaml"

        if args.check:
            # Semantic compare: regenerated groups vs committed configmap groups.
            gen_groups = []
            for v in cm["data"].values():
                gen_groups.extend((yaml.safe_load(v) or {}).get("groups", []))
            gen_map = sync._extract(gen_groups)
            committed_map = sync._extract(sync._groups_from_configmap(target)) if target.exists() else {}
            findings = sync._diff_maps(gen_map, committed_map)
            if findings:
                drift.append((name, findings))
        else:
            rendered = yaml.dump(cm, sort_keys=False, default_flow_style=False, allow_unicode=True)
            header = (
                f"# ============================================================\n"
                f"# {name} Rule Pack — Recording & Alert Rules (ConfigMap)\n"
                f"# GENERATED from rule-packs/rule-pack-{name}.yaml by\n"
                f"# scripts/tools/dx/generate_rulepack_configmaps.py — DO NOT EDIT.\n"
                f"# Run `make rulepack-configmaps` after editing the rule pack.\n"
                f"# ============================================================\n"
            )
            target.write_text(header + rendered, encoding="utf-8")
            wrote += 1

    if args.check:
        if drift:
            for name, findings in drift:
                print(f"  ❌ configmap-rules-{name}.yaml drifted from source:")
                for f in findings:
                    print(f"       {f}")
            print(f"\n❌ {len(drift)} configmap(s) out of sync with rule-packs/. "
                  f"Run `make rulepack-configmaps` to regenerate.", file=sys.stderr)
            return 1
        print(f"✅ All {len(packs)} rule-pack configmaps match source.")
        return 0

    print(f"✅ Generated {wrote} configmaps into {out_dir.relative_to(repo)}/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
