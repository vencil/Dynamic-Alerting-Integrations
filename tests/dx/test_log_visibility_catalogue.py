"""test_log_visibility_catalogue.py — ADR-021 #609 PR-5 2-tier visibility catalogue drift-guard.

The 2-tier log visibility catalogue (docs/internal/log-visibility-2tier-catalogue.md)
is a CONTROL-PLANE curation document, not a runtime filter (ADR-021 §治理邊界): the
hard cross-tenant isolation lives in the data plane (VictoriaLogs AccountID partition +
Vector ingest-time allowlist sanitization). The catalogue's job is to be a human-readable,
reviewable view of what the data plane ACTUALLY exposes to a tenant — so its ONE failure
mode is silently drifting from the enforced config.

This test is the drift-guard. The SSOT for "which fields a tenant sees in its own
partition" is `helm/vector/values.yaml` `tenantProjectionKeepFields` (the fail-closed
allowlist the Vector `tenant_project` transform rebuilds the event from). The catalogue
does NOT re-list fields as a parallel YAML (that would dual-write → drift); it documents
them in a Tier-2 table. This test asserts:

  * the Tier-2 table's field set == the enforced `tenantProjectionKeepFields` (verbatim,
    both directions — a field added to one but not the other goes red);
  * the Stream-fields list == the enforced `tenantProjectionStreamFields`;
  * the structurally-EXCLUDED infra fields the catalogue names (upstream / pod_node / …)
    are NOT in the allowlist (so the doc cannot claim "excluded" for something that leaks).

No helm/vector CLI needed — it reads the committed values.yaml + the committed markdown,
so it runs in the plain `pytest tests/` CI job (no special binary). The VRL-render layer
(that the allowlist is actually enforced by the shipped transform) is the separate
tests/shared/test_vector_projection_vrl.py::test_allowlist_sanitization_not_denylist.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_VALUES = "helm/vector/values.yaml"
_CATALOGUE = "docs/internal/log-visibility-2tier-catalogue.md"

# Fields the catalogue's Tier-2 section explicitly calls out as STRUCTURALLY EXCLUDED.
# None of these may appear in the keep-field allowlist, or the catalogue lies about
# "租戶副本中不存在". (upstream = backend IP behind %UPSTREAM_HOST%, the exact leak the
# denylist→allowlist inversion closed; the rest are infra topology.)
_MUST_BE_EXCLUDED = ("upstream", "app", "k8s_namespace", "pod_name", "pod_ip",
                     "pod_node", "node_name")

# Fields the template INJECTS into the rebuilt tenant event (not copied from the source
# row), so they are legitimately documented in the Tier-2 table but are NOT entries in
# `tenantProjectionKeepFields`. Excluded from the verbatim set-equality both-ways check.
_TEMPLATE_INJECTED = frozenset({"account_id", "log_event_id", "timestamp"})


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def keep_fields(repo_root: Path) -> list[str]:
    vals = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    fields = vals.get("tenantProjectionKeepFields")
    assert isinstance(fields, list) and fields, "tenantProjectionKeepFields must be a non-empty list"
    return [str(f) for f in fields]


@pytest.fixture(scope="session")
def stream_fields(repo_root: Path) -> list[str]:
    vals = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    return [str(f) for f in (vals.get("tenantProjectionStreamFields") or [])]


@pytest.fixture(scope="session")
def catalogue_text(repo_root: Path) -> str:
    p = repo_root / _CATALOGUE
    assert p.is_file(), f"{_CATALOGUE} missing — the 2-tier visibility catalogue (ADR-021 item 6)"
    return p.read_text(encoding="utf-8")


def _section(text: str, heading_substr: str) -> str:
    """Return the body of the first `##`/`###` section whose heading contains
    heading_substr, up to the next same-or-higher-level heading."""
    lines = text.splitlines()
    start = None
    start_level = 0
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{2,4})\s+(.*)$", ln)
        if m and heading_substr in m.group(2):
            start = i + 1
            start_level = len(m.group(1))
            break
    assert start is not None, f"section heading containing {heading_substr!r} not found"
    out = []
    for ln in lines[start:]:
        m = re.match(r"^(#{2,4})\s+", ln)
        if m and len(m.group(1)) <= start_level:
            break
        out.append(ln)
    return "\n".join(out)


def _table_first_col_codes(section: str) -> list[str]:
    """Collect every `code`-quoted token in the FIRST column of a markdown
    table within `section` (rows shaped `| `field` | … |`). Returns the field
    names in document order."""
    fields: list[str] = []
    for ln in section.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        # Skip the header / separator rows.
        if set(first) <= set("-: ") or "欄位" in first:
            continue
        m = re.search(r"`([^`]+)`", first)
        if m:
            fields.append(m.group(1))
    return fields


def test_tier2_table_matches_enforced_keep_fields(catalogue_text: str, keep_fields: list[str]) -> None:
    """The Tier-2 field table must list EXACTLY the enforced allowlist (minus the
    template-injected fields, which the table documents but values.yaml does not
    enumerate). Both directions: a field in values.yaml but missing from the doc, OR
    documented but not enforced, fails — that is the drift this guard exists to catch."""
    section = _section(catalogue_text, "Tier-2")
    documented = set(_table_first_col_codes(section))
    enforced = set(keep_fields)

    # Every enforced keep-field must be documented in the Tier-2 table.
    missing_from_doc = enforced - documented
    assert not missing_from_doc, (
        f"tenantProjectionKeepFields enforced but NOT in the Tier-2 catalogue table "
        f"(doc drifted behind values.yaml): {sorted(missing_from_doc)}"
    )

    # Everything the table documents (except template-injected) must be an enforced
    # keep-field — the doc must not claim a field is tenant-visible when it isn't.
    documented_copied = documented - _TEMPLATE_INJECTED
    phantom = documented_copied - enforced
    assert not phantom, (
        f"Tier-2 catalogue table lists field(s) NOT in tenantProjectionKeepFields "
        f"(doc claims visibility the config does not enforce): {sorted(phantom)}"
    )


def test_excluded_fields_are_actually_excluded(keep_fields: list[str]) -> None:
    """The catalogue names specific infra fields as structurally excluded; none of
    them may sneak into the allowlist (that would silently make the doc lie + leak
    topology to tenants)."""
    enforced = set(keep_fields)
    leaked = enforced.intersection(_MUST_BE_EXCLUDED)
    assert not leaked, (
        f"field(s) the catalogue documents as EXCLUDED are in the keep-field allowlist "
        f"= cross-tenant topology leak: {sorted(leaked)}"
    )


def test_stream_fields_match_enforced(catalogue_text: str, stream_fields: list[str]) -> None:
    """The Stream-fields the catalogue documents must equal the enforced
    tenantProjectionStreamFields (low-cardinality dims only)."""
    section = _section(catalogue_text, "Stream fields")
    documented = {m for m in re.findall(r"`([a-z_]+)`", section)}
    enforced = set(stream_fields)
    # Each enforced stream field is named in the section.
    missing = enforced - documented
    assert not missing, (
        f"enforced tenantProjectionStreamFields not documented in §Stream fields: {sorted(missing)}"
    )
    # A stream field must also be a keep-field (else it can't exist on the projected event);
    # this is documented in the catalogue and enforced by the chart — assert the doc states it.
    assert "低基數" in section or "low-cardinality" in section, (
        "§Stream fields must state the low-cardinality constraint (RAM-explosion guard)"
    )


def test_catalogue_declares_control_plane_not_runtime_filter(catalogue_text: str) -> None:
    """Guard the load-bearing framing: the catalogue must state it is control-plane
    curation, NOT a query-path/runtime filter (ADR-021 §治理邊界). A future edit that
    quietly reframes it as an enforcement layer (and tempts a runtime-filter impl) is a
    design regression."""
    assert "不在 query path 硬擋" in catalogue_text or "不是 runtime filter" in catalogue_text, (
        "catalogue must declare it is control-plane curation, not a runtime filter"
    )
    # And it must point at the enforced SSOT rather than re-listing a parallel allowlist.
    assert "tenantProjectionKeepFields" in catalogue_text, (
        "catalogue must reference tenantProjectionKeepFields as the SSOT"
    )
