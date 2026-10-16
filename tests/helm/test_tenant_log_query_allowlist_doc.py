"""test_tenant_log_query_allowlist_doc.py — the log-query endpoint list in the docs matches the chart

docs/integration/tenant-log-query.md §4.1 tells tenants which VictoriaLogs
endpoints the federation gateway forwards. The list that decides it is
``victorialogs.allowedEndpoints`` in helm/federation-gateway/values.yaml; the
doc is a copy, so this test compares the two both ways:

  - the ``| 可用`` row: every backticked name after ``/select/logsql/`` 下的
    is ``/select/logsql/<name>``, and the set equals allowedEndpoints;
  - the ``| **不支援**`` row: every backticked ``/select/...`` path in it is
    not in allowedEndpoints.

A render layer (helm CLI) checks what values.yaml's comment says about the
match: the rendered route regex accepts the endpoint with or without a
trailing slash, and rejects a sub-path and a sibling name.

NOT COVERED: the prose around the table.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/federation-gateway"
_VALUES = "helm/federation-gateway/values.yaml"
_DOC = "docs/integration/tenant-log-query.md"
_PREFIX = "/select/logsql/"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="module")
def allowed(repo_root: Path) -> list[str]:
    values = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    eps = values["victorialogs"]["allowedEndpoints"]
    assert eps, "victorialogs.allowedEndpoints is empty"
    return eps


def _doc_row(repo_root: Path, start: str) -> str:
    rows = [ln for ln in (repo_root / _DOC).read_text(encoding="utf-8").splitlines()
            if ln.startswith(start)]
    assert len(rows) == 1, f"{_DOC}: expected one row starting {start!r}, found {len(rows)}"
    return rows[0]


def test_doc_lists_exactly_the_allowed_endpoints(repo_root: Path, allowed: list[str]):
    row = _doc_row(repo_root, "| 可用")
    marker = f"`{_PREFIX}` 下的"
    assert marker in row, f"{_DOC}: the 可用 row must name the endpoints after {marker!r}"
    names = re.findall(r"`([^`]+)`", row.split(marker, 1)[1])
    assert names, f"{_DOC}: no backticked endpoint names in the 可用 row"
    bad = [n for n in names if not re.fullmatch(r"[a-z_]+", n)]
    assert not bad, f"{_DOC}: 可用 row entries must be bare names under {_PREFIX}, got {bad}"
    doc = {_PREFIX + n for n in names}
    assert len(doc) == len(names), f"{_DOC}: the 可用 row repeats a name"
    assert doc == set(allowed), (
        f"{_DOC} 可用 row vs {_VALUES} allowedEndpoints — "
        f"only in doc: {sorted(doc - set(allowed))}, only in values: {sorted(set(allowed) - doc)}"
    )


def test_doc_unsupported_paths_are_not_allowed(repo_root: Path, allowed: list[str]):
    row = _doc_row(repo_root, "| **不支援**")
    paths = re.findall(r"`(/select/[^`]+)`", row)
    assert paths, f"{_DOC}: the 不支援 row names no /select/ path"
    assert not set(paths) & set(allowed), f"{_DOC}: 不支援 lists allowed endpoints {sorted(set(paths) & set(allowed))}"


@_needs_helm
def test_rendered_routes_match_exact_path_only(repo_root: Path, allowed: list[str]):
    out = subprocess.run(
        ["helm", "template", "t", str(repo_root / _CHART),
         "--set", "mode=victorialogs", "--set", "jwt.audience=tenant-federation-logs"],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert out.returncode == 0, out.stderr
    regexes = re.findall(r'regex: "(\^/select/logsql/[^"]*)"', out.stdout)
    for ep in allowed:
        hits = [r for r in regexes if re.fullmatch(r, ep)]
        assert len(hits) == 1, f"{ep}: expected one rendered route regex matching it, got {hits}"
        rx = hits[0]
        assert re.fullmatch(rx, ep + "/"), f"{rx} rejects {ep}/"
        assert not re.fullmatch(rx, ep + "/foo"), f"{rx} accepts the sub-path {ep}/foo"
        assert not re.fullmatch(rx, ep + "_other"), f"{rx} accepts the sibling {ep}_other"
