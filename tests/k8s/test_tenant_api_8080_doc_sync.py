"""tenant-api 8080 allow-list: the security doc names exactly what the chart admits (#1666).

WHY: port 8080 trusts identity headers as sent, so the NetworkPolicy allow-list
IS that port's identity control. `docs/governance-security{,.en}.md` described
it as "two workloads" while both shipped carriers admitted four — a reader doing
threat modelling would have missed two paths into the header-trust surface, and
the EN page did not mention the port at all.

WHAT IS CHECKED: the set of `key=value` selectors under
`networkPolicy.internalPortAllow` in `helm/tenant-api/values.yaml` equals the set
of backticked `key=value` selectors on the tenant-api row of each doc's
NetworkPolicy table. Adding or removing a workload in the chart without updating
both pages turns this red, and so does a doc row that names one the chart does
not admit. The raw manifest (`k8s/04-tenant-api/`) is not compared here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALUES = _REPO_ROOT / "helm" / "tenant-api" / "values.yaml"
_DOCS = (
    _REPO_ROOT / "docs" / "governance-security.md",
    _REPO_ROOT / "docs" / "governance-security.en.md",
)
_SELECTOR = re.compile(r"`([A-Za-z0-9./-]+=[A-Za-z0-9._-]+)`")


def _chart_selectors() -> set[str]:
    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    allow = values["networkPolicy"]["internalPortAllow"]
    return {f"{k}={v}" for sel in allow.values() for k, v in sel.items()}


def _netpol_section(doc: Path) -> str:
    # Other tables on the page (container images) also have a tenant-api row.
    text = doc.read_text(encoding="utf-8")
    start = text.find("### NetworkPolicy")
    assert start != -1, f"{doc.name}: no '### NetworkPolicy' section"
    end = text.find("\n### ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _doc_row_selectors(doc: Path) -> set[str]:
    rows = [
        line for line in _netpol_section(doc).splitlines()
        if line.startswith("| tenant-api |")
    ]
    assert len(rows) == 1, f"{doc.name}: expected one tenant-api table row, found {len(rows)}"
    return set(_SELECTOR.findall(rows[0]))


def test_chart_allow_list_is_not_empty():
    # Non-vacuity: an empty set on both sides would compare equal.
    assert len(_chart_selectors()) >= 2


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
def test_doc_names_every_admitted_workload(doc):
    assert _doc_row_selectors(doc) == _chart_selectors(), (
        f"{doc.name}: the tenant-api row must list the same selectors as "
        "networkPolicy.internalPortAllow in helm/tenant-api/values.yaml"
    )
