"""platform-*.rules.yaml extracts must match the shipped ConfigMap (drift guard).

WHY this exists:
  promtool cannot consume the deployed ConfigMap directly — it needs a top-level
  `groups:` document, not `data: platform-alert.yml: |`. So every platform alert
  that has a promtool behavioural test also has an EXTRACTED COPY under
  tests/rulepacks/platform-*.rules.yaml, each carrying only a hand-written
  "⚠️ KEEP IN SYNC" comment.

  Nothing enforced that. `scripts/tools/lint/check_rulepack_sync.py` globs only
  `rule-packs/rule-pack-*.yaml` and derives its configmap/operator copies from
  those names; there is no `rule-packs/rule-pack-platform.yaml`, so
  `k8s/03-monitoring/configmap-rules-platform.yaml` and its extracts were never
  compared. Failure mode: someone tightens or renames an alert in the ConfigMap
  and forgets the extract — promtool keeps passing against the stale extract and
  CI ships a rule nobody tested.

  This test closes that gap for every platform extract at once (found during the
  adversarial review of the TenantApiConfigReloadFailing alert, which inherited
  the unguarded pattern).

Contract: each alert in an extract must exist in the ConfigMap and agree on the
fields that determine behaviour — `expr`, `for`, `labels`, and the `summary`
annotation that promtool tests assert on. The extracts are deliberately a SUBSET
of the ConfigMap (not every shipped alert needs a promtool test), so the reverse
direction is NOT asserted here; promtool firing-coverage is tracked separately by
scripts/tools/lint/check_vmalert_coverage.py.
"""
from __future__ import annotations

import glob
import os

import pytest
import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIGMAP = os.path.join(_REPO, "k8s", "03-monitoring", "configmap-rules-platform.yaml")
_EXTRACT_GLOB = os.path.join(_REPO, "tests", "rulepacks", "platform-*.rules.yaml")

# Fields whose drift changes what the alert DOES (or what promtool asserts on).
_BEHAVIOURAL_FIELDS = ("expr", "for", "labels")


def _shipped_alerts() -> dict[str, dict]:
    """alertname -> rule, parsed out of the ConfigMap's embedded rules document."""
    with open(_CONFIGMAP, encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d and d.get("kind") == "ConfigMap"]
    assert docs, f"no ConfigMap document in {_CONFIGMAP}"
    blob = docs[0]["data"]["platform-alert.yml"]
    out: dict[str, dict] = {}
    for group in yaml.safe_load(blob)["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                out[rule["alert"]] = rule
    return out


def _extracted_alerts() -> list[tuple[str, str, dict]]:
    """(extract_path, alertname, rule) for every alert in every platform extract."""
    out: list[tuple[str, str, dict]] = []
    for path in sorted(glob.glob(_EXTRACT_GLOB)):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for group in doc["groups"]:
            for rule in group.get("rules", []):
                if "alert" in rule:
                    out.append((os.path.basename(path), rule["alert"], rule))
    return out


def test_extracts_are_discovered():
    """Guard the guard: a broken glob would make every assertion below vacuous."""
    found = _extracted_alerts()
    assert len(found) >= 6, f"expected several platform extracts, found {len(found)}"
    assert _shipped_alerts(), "no alerts parsed out of the ConfigMap"


@pytest.mark.parametrize("extract,alertname,rule", _extracted_alerts(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_extract_matches_shipped_configmap(extract: str, alertname: str, rule: dict):
    shipped = _shipped_alerts()
    assert alertname in shipped, (
        f"{extract}: alert {alertname!r} is not in configmap-rules-platform.yaml — "
        f"the extract tests a rule that does not ship.")
    live = shipped[alertname]

    for field in _BEHAVIOURAL_FIELDS:
        assert rule.get(field) == live.get(field), (
            f"{extract}: alert {alertname!r} field {field!r} has DRIFTED from "
            f"configmap-rules-platform.yaml.\n"
            f"  extract: {rule.get(field)!r}\n"
            f"  shipped: {live.get(field)!r}\n"
            f"promtool is testing a rule that is not what ships — sync the extract.")

    got = rule.get("annotations", {}).get("summary")
    want = live.get("annotations", {}).get("summary")
    assert got == want, (
        f"{extract}: alert {alertname!r} annotations.summary has DRIFTED "
        f"(promtool exp_annotations assert on it).\n  extract: {got!r}\n  shipped: {want!r}")
