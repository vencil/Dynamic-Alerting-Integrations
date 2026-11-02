#!/usr/bin/env python3
"""Tenant metadata reaches alerts through annotations only, never a join (#3483).

The file keeps its historical name (ADR-024 PR3-pre Commit 3 introduced it as the
left-outer-join enrichment lint); its contract changed with #3483.

WHY: tenant metadata (runbook_url, owner, tier — `tenant_metadata_info`, served
by every threshold-exporter replica) used to be joined into each alert with
`* on(tenant) group_left(runbook_url, owner, tier) …`, which made the metadata
part of the alert's IDENTITY. threshold-exporter runs several replicas and the
platform scrapes every pod, and the replicas reload their config independently
(each polls every reloadInterval; the ConfigMap reaches each through its own
node's kubelet), so for up to a minute or two one replica can serve a tenant's
new runbook_url while the other still serves the old one. Whatever the join's
right-hand side then picks, it can flip between the two (MetricsQL's `topk`
picks a different tied series per evaluation), the alert's labels change, its
`for:` restarts, and the tenant never fires during the window. So metadata is
not a label at all: each annotation that shows it reads it with ONE canonical
query template (METADATA_ANNOTATIONS), evaluated at notification time. The
alert's identity no longer depends on mutable metadata; an annotation may show
either replica's value during a reload, and an absent `tenant_metadata_info`
renders an empty string.

WHAT THIS CHECKS, per alert/recording rule of every rule pack (read the way
Prometheus' rule loader reads it, ADR-036 §12):
  1. expr: `tenant_metadata_info` may appear only as the right-hand operand of a
     set operator (`and` / `or` / `unless`, which match on presence and never
     copy labels). Any other use — a group_left / group_right / one-to-one
     arithmetic or comparison match, on either side, an aggregation of it — is a
     metadata join and is refused.
  2. annotations (and labels): no `$labels.runbook_url` / `$labels.owner` /
     `$labels.tier` (those labels no longer exist on the alert), and every
     metadata query template is exactly METADATA_ANNOTATIONS[label] — nothing
     else that reads `tenant_metadata_info`.

METADATA_LABELS is pinned to the exporter's own label list, and
METADATA_QUERY to the Go custom-alert compiler's `metadataQuery`, by
tests/lint/test_check_leftouterjoin_enrichment.py.

Exit codes: 0 ok / 1 violation (--ci) / 2 caller error.

Usage:
    python scripts/tools/lint/check_leftouterjoin_enrichment.py        # report
    python scripts/tools/lint/check_leftouterjoin_enrichment.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)
import _lib_consumer_read  # noqa: E402  (#3471 / ADR-036 §12: rule packs as Prometheus reads them)

# The labels tenant_metadata_info carries besides `tenant` — the one place they
# are spelled. Pinned to the exporter's NewDesc label list by the test suite.
METADATA_LABELS = ("runbook_url", "owner", "tier")
# The one query an annotation reads a tenant's metadata with. `topk(1, …)`: the
# replicas each serve a copy; during a reload the copies may differ and either
# is acceptable for display. The Go compiler (custom_alert_compile.go:
# metadataQuery) emits the same string; a test pins it.
METADATA_QUERY = "topk(1, tenant_metadata_info{tenant=%q})"


def metadata_annotation(label: str) -> str:
    """The canonical annotation template showing *label* of the alert's tenant.

    Backtick string: no quote escaping in YAML. `with query` skips an empty
    result, so a tenant without `tenant_metadata_info` renders "" — no template
    error — on both Prometheus and vmalert."""
    return ("{{ with query (printf `" + METADATA_QUERY + "` $labels.tenant) }}"
            "{{ with index . 0 }}{{ .Labels." + label + " }}{{ end }}{{ end }}")


METADATA_ANNOTATIONS: Dict[str, str] = {lbl: metadata_annotation(lbl) for lbl in METADATA_LABELS}

_SET_OPERAND = re.compile(
    r"\b(?:and|or|unless)\s*(?:(?:on|ignoring)\s*\([^)]*\)\s*)?\(?\s*"
    r"tenant_metadata_info\b(?:\s*\{[^}]*\})?", re.IGNORECASE)
_TMI = re.compile(r"\btenant_metadata_info\b")
_LABEL_REF = re.compile(r"\$labels\.(" + "|".join(METADATA_LABELS) + r")\b")


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def expr_violations(expr: str) -> List[str]:
    """Pure core: why *expr* joins tenant metadata (empty list = it does not)."""
    allowed = [m.span() for m in _SET_OPERAND.finditer(expr)]
    bad = [m for m in _TMI.finditer(expr)
           if not any(s <= m.start() and m.end() <= e for s, e in allowed)]
    if not bad:
        return []
    m = bad[0]
    context = " ".join(expr[max(0, m.start() - 60):m.end() + 20].split())
    return [f"uses `tenant_metadata_info` outside a set operator (…{context}…) — "
            f"a metadata join; show metadata through annotations "
            f"(METADATA_ANNOTATIONS) instead"]


def template_violations(field: str, value: str) -> List[str]:
    """Pure core: why one annotation/label value reads metadata non-canonically."""
    out = []
    for m in _LABEL_REF.finditer(value):
        out.append(f"{field}: `$labels.{m.group(1)}` — alerts carry no metadata labels; "
                   f"use the canonical template `{METADATA_ANNOTATIONS[m.group(1)]}`")
    residual = value
    for tpl in METADATA_ANNOTATIONS.values():
        residual = residual.replace(tpl, "")
    if "tenant_metadata_info" in residual:
        out.append(f"{field}: reads `tenant_metadata_info` with a template that is not "
                   f"one of METADATA_ANNOTATIONS")
    return out


def rule_violations(rule: dict) -> List[str]:
    """Pure core: every violation of one rule (an alert or a recording rule)."""
    out = []
    expr = rule.get("expr")
    if isinstance(expr, str):
        out += expr_violations(expr)
    for section in ("annotations", "labels"):
        for k, v in (rule.get(section) or {}).items():
            if isinstance(v, str):
                out += template_violations(f"{section}.{k}", v)
    return out


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Tenant metadata reaches alerts through annotations only (#3483)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    # SOURCE only: the generated configmaps are guaranteed `== source` by
    # generate_rulepack_configmaps.py --check.
    repo = _repo_root()
    try:
        # #3471: the rule packs are the one declaration's (rule-pack-*.yaml|yml;
        # an undeclared YAML beside them is named).
        targets: List[Path] = _lib_consumer_read.rule_pack_files(
            repo / _lib_consumer_read.RULE_PACKS.directory)
        found = _lib_consumer_read.rule_files_in(targets, keep_refusals=True)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not targets:
        print("ERROR: no rule-pack files found", file=sys.stderr)
        return EXIT_CALLER_ERROR

    violations = 0
    for rfs in found.values():
        for rf in rfs:
            rel = Path(rf.path).relative_to(repo) if Path(rf.path).is_absolute() else rf.path
            if rf.problem is not None:
                print(f"ERROR: {rel}: Prometheus' rule loader refuses it: {rf.problem}",
                      file=sys.stderr)
                return EXIT_CALLER_ERROR
            for grp in rf.groups:
                for rule in grp.get("rules") or []:
                    name = rule.get("alert") or rule.get("record")
                    for why in rule_violations(rule):
                        violations += 1
                        print(f"  ❌ {rel}:{name}: {why}")

    if violations:
        print(f"\n❌ {violations} metadata violation(s). Alerts carry no metadata labels "
              f"(#3483): a join makes runbook_url/owner/tier part of the alert's identity, "
              f"and the replicas briefly disagree during a config reload — the identity "
              f"flips and `for:` restarts. Read metadata in annotations with "
              f"METADATA_ANNOTATIONS.", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ All {len(targets)} rule pack(s): no metadata join; metadata annotations "
          f"use the canonical query `{METADATA_QUERY}`.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
