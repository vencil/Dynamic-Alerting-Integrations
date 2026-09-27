"""docs-ci's promtool must be the same Prometheus MAJOR as the one we ship (#1949).

The I-4 runbook smoke test (``docs-ci.yaml`` job ``i4-runbook-smoke-test``)
judges customer-facing PromQL with ``promtool check rules``. Across a major
the accepted language differs in both directions — a 2.x promtool accepts
functions 3.x removed, and rejects syntax 3.x accepts — so a lagging major
lets the check pass on something a customer's promtool rejects.

Only the MAJOR is compared: the shipped image is bumped by Renovate within a
major, and an exact-version pin here would turn every such bump red for a
difference this check does not need. ``PROMTOOL_VERSION`` is deliberately not
tied to the rule-pack gate's ``PROM_VERSION`` (see
``tests/preview/test_promtool_pin_parity.py`` and
``.devcontainer/install-promtool.sh``). amtool is not compared: Alertmanager
is still 0.x, so there is no major to align.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_DOCS_CI = _REPO / ".github" / "workflows" / "docs-ci.yaml"
_PROM_DEPLOYMENT = _REPO / "k8s" / "03-monitoring" / "deployment-prometheus.yaml"

_ASSIGN = re.compile(r"""^\s*PROMTOOL_VERSION=["']?([^\s"'#]+)""", re.MULTILINE)


def _major(version: str) -> int:
    return int(version.lstrip("v").split(".", 1)[0])


def _docs_ci_promtool_version() -> str:
    workflow = yaml.safe_load(_DOCS_CI.read_text(encoding="utf-8"))
    found = [
        m.group(1)
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or []
        for m in _ASSIGN.finditer(str(step.get("run", "")))
    ]
    assert len(found) == 1, f"expected one PROMTOOL_VERSION= in {_DOCS_CI.name} run steps, found {found}"
    return found[0]


def _shipped_prometheus_version() -> str:
    tags = []
    for doc in yaml.safe_load_all(_PROM_DEPLOYMENT.read_text(encoding="utf-8")):
        spec = ((doc or {}).get("spec") or {}).get("template", {}).get("spec", {})
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            image = str(container.get("image", ""))
            if image.startswith("prom/prometheus:"):
                tags.append(image.split(":", 1)[1].split("@", 1)[0])
    assert len(tags) == 1, f"expected one prom/prometheus image in {_PROM_DEPLOYMENT.name}, found {tags}"
    return tags[0]


def test_docs_ci_promtool_major_matches_shipped_prometheus() -> None:
    pinned = _docs_ci_promtool_version()
    shipped = _shipped_prometheus_version()
    assert _major(pinned) == _major(shipped), (
        f"docs-ci.yaml pins PROMTOOL_VERSION={pinned}, but "
        f"{_PROM_DEPLOYMENT.relative_to(_REPO).as_posix()} ships Prometheus {shipped}. "
        "Bump PROMTOOL_VERSION to the shipped major and update PROMTOOL_SHA256 "
        "(upstream sha256sums.txt) in the same step."
    )
