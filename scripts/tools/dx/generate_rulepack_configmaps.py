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
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


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
    """The pack a rule pack file carries, by the one declaration (#3471)."""
    return _lib_consumer_read.RULE_PACKS.member(path)


def target_name(pack: str) -> str:
    """The file name of the ConfigMap copy of *pack*: derived from the
    declaration of the rules ConfigMaps (`CONFIGMAP_RULES.file_name`), so the
    file this generator writes is one the declaration lists (#3471)."""
    return _lib_consumer_read.CONFIGMAP_RULES.file_name(pack)


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
    out_dir = repo / _lib_consumer_read.CONFIGMAP_RULES.directory
    # #3471: the source packs are the one declaration's (rule-pack-*.yaml|yml;
    # an undeclared YAML beside them is named, rc 2).
    try:
        packs = _lib_consumer_read.rule_pack_files(repo / _lib_consumer_read.RULE_PACKS.directory)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not packs:
        print("ERROR: no rule packs found", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if args.pack:
        wanted = set(args.pack)
        packs = [p for p in packs if _pack_name(p) in wanted]
        missing = wanted - {_pack_name(p) for p in packs}
        if missing:
            print(f"ERROR: --pack name(s) not found: {sorted(missing)}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    # #3206 (ADR-036 §12): every rule file is read the way Prometheus' rule
    # loader reads it (`da-read prometheus-rules`) — the source packs, and
    # under --check the generated data values and the committed ConfigMaps'
    # (their outer layer the way kubectl reads it) — a few da-read runs for
    # the whole set. A file the loader refuses stops the generator (rc 2).
    source_of = {_pack_name(p): p for p in packs}
    try:
        sources = _lib_consumer_read.prometheus_rule_files(packs)
        cms = {_pack_name(p): build_configmap(_pack_name(p), rf.documents[0], args.namespace)
               for p, rf in zip(packs, sources)}
        if args.check:
            generated = _lib_consumer_read.require_read(
                _lib_consumer_read.prometheus_rule_text_results(
                    [(name, v) for name, cm in cms.items() for v in cm["data"].values()]))
            targets = [out_dir / target_name(name) for name in cms]
            committed = _lib_consumer_read.require_read(
                _lib_consumer_read.manifest_rule_file_results([t for t in targets if t.exists()]))
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    drift = []
    wrote = 0
    for name, cm in cms.items():
        target = out_dir / target_name(name)

        if args.check:
            # Semantic compare: regenerated groups vs committed configmap groups.
            gen_groups = [g for rf in generated if rf.path == name for g in rf.groups]
            gen_map = sync._extract(gen_groups)
            committed_map = sync._extract(
                [g for rf in committed
                 if rf.kind == "ConfigMap" and rf.manifest == str(target) for g in rf.groups])
            findings = sync._diff_maps(gen_map, committed_map)
            if findings:
                drift.append((name, findings))
        else:
            rendered = yaml.dump(cm, sort_keys=False, default_flow_style=False, allow_unicode=True)
            header = (
                f"# ============================================================\n"
                f"# {name} Rule Pack — Recording & Alert Rules (ConfigMap)\n"
                f"# GENERATED from {_lib_consumer_read.RULE_PACKS.directory}/"
                f"{source_of[name].name} by\n"
                f"# scripts/tools/dx/generate_rulepack_configmaps.py — DO NOT EDIT.\n"
                f"# Run `make rulepack-configmaps` after editing the rule pack.\n"
                f"# ============================================================\n"
            )
            target.write_text(header + rendered, encoding="utf-8", newline="\n")
            wrote += 1

    if args.check:
        if drift:
            for name, findings in drift:
                print(f"  ❌ {target_name(name)} drifted from source:")
                for f in findings:
                    print(f"       {f}")
            print(f"\n❌ {len(drift)} configmap(s) out of sync with rule-packs/. "
                  f"Run `make rulepack-configmaps` to regenerate.", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"✅ All {len(packs)} rule-pack configmaps match source.")
        return EXIT_OK

    print(f"✅ Generated {wrote} configmaps into {out_dir.relative_to(repo)}/.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
