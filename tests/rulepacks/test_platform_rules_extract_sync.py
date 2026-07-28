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

⚠️ Discovery is TWO-PRONGED because filename alone is not a reliable marker of
"this file mirrors the platform pack":
  * `platform-*.rules.yaml`  — STRICT. Every alert in these must exist in the
    ConfigMap (a file named `platform-*` testing a rule that does not ship is
    itself the bug).
  * every other `tests/rulepacks/**/*.rules.yaml` — MIRROR-BY-NAME. Any alert
    whose name matches a ConfigMap alert is treated as an extract and compared;
    alerts with no platform counterpart are ignored (those files legitimately
    hold non-platform rules).
The second prong exists because `tenant-liveness-platform.rules.yaml` and
`tenant-log-query-platform.rules.yaml` mirror two platform alerts each (four in
total) while escaping the `platform-*` glob — they were silently outside this
gate. Keying
the mirror set on ALERT NAME instead of filename means a future extract cannot
opt out of the drift guard just by being named something else.
"""
from __future__ import annotations

import glob
import os

import pytest
import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIGMAP = os.path.join(_REPO, "k8s", "03-monitoring", "configmap-rules-platform.yaml")
_RULEPACKS_DIR = os.path.join(_REPO, "tests", "rulepacks")
# Prong 1: files whose NAME claims to be a platform extract — strict.
# ⚠️ RECURSIVE, matching prong 2. A non-recursive prong-1 glob against a
# recursive prong-2 glob is a silent-downgrade seam: moving the platform extracts
# into a subdirectory would keep them discovered (prong 2 still finds them) but
# drop them out of the STRICT set, losing the prong-1-only assertion that every
# alert in a `platform-*` file must exist in the ConfigMap. Both globs must be
# walked the same way or the strictness depends on directory layout.
_EXTRACT_GLOB = os.path.join(_RULEPACKS_DIR, "**", "platform-*.rules.yaml")
# Prong 2: every other bare-rules file in the tree — mirrors matched by alertname.
_ALL_RULES_GLOB = os.path.join(_RULEPACKS_DIR, "**", "*.rules.yaml")

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


def _alerts_in(path: str) -> list[tuple[str, dict]]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return [(rule["alert"], rule)
            for group in (doc or {}).get("groups", [])
            for rule in group.get("rules", [])
            if "alert" in rule]


def _strict_extract_paths() -> set[str]:
    """Absolute paths of the prong-1 (STRICT) `platform-*.rules.yaml` files."""
    return {os.path.abspath(p) for p in glob.glob(_EXTRACT_GLOB, recursive=True)}


def _extracted_alerts() -> list[tuple[str, str, dict]]:
    """(extract_file, alertname, rule) for every platform-mirroring alert.

    Prong 1 (`platform-*.rules.yaml`): EVERY alert, whether or not it matches a
    shipped one — so a stale/renamed alert in a file that calls itself a platform
    extract still trips the "not in the ConfigMap" assertion below.
    Prong 2 (any other `*.rules.yaml` in the tree): only alerts whose NAME also
    exists in the ConfigMap; the rest are genuinely non-platform rules.
    """
    named = _strict_extract_paths()
    shipped = _shipped_alerts()
    out: list[tuple[str, str, dict]] = []
    for path in sorted(glob.glob(_ALL_RULES_GLOB, recursive=True)):
        strict = os.path.abspath(path) in named
        rel = os.path.relpath(path, _RULEPACKS_DIR).replace(os.sep, "/")
        for alertname, rule in _alerts_in(path):
            if strict or alertname in shipped:
                out.append((rel, alertname, rule))
    return out


def test_extracts_are_discovered():
    """Guard the guard: a broken glob would make every assertion below vacuous."""
    found = _extracted_alerts()
    assert len(found) >= 6, f"expected several platform extracts, found {len(found)}"
    assert _shipped_alerts(), "no alerts parsed out of the ConfigMap"
    # Non-vacuous on the SECOND prong specifically: these two files mirror
    # platform alerts while escaping the `platform-*` glob, and were the reason
    # mirror-by-name discovery was added. If they stop being covered the wider
    # discovery has silently regressed to the old filename-only behaviour.
    covered = {(f, a) for f, a, _ in found}
    assert covered >= {
        ("tenant-liveness-platform.rules.yaml", "TenantExporterJobAbsent"),
        ("tenant-liveness-platform.rules.yaml", "MassExporterOutage"),
        ("tenant-log-query-platform.rules.yaml", "TenantLogQueryRejectionRateAnomaly"),
        ("tenant-log-query-platform.rules.yaml", "TenantProjectionFanoutDiscardSpike"),
    }, sorted(covered)


def test_strict_prong_walks_the_tree_like_the_discovery_prong():
    """Anti-regression for the recursion seam between the two globs.

    Prong 2 is recursive; prong 1 must be too. If prong 1 reverts to a
    non-recursive glob, every `platform-*.rules.yaml` under a subdirectory keeps
    being *discovered* but silently loses STRICT status — the failure is invisible
    because the suite stays green. Asserted two ways: the strict set must contain
    every platform-named file the recursive walk finds, and it must be non-empty.
    """
    strict = _strict_extract_paths()
    assert strict, "no platform-*.rules.yaml found — prong 1 is vacuous"
    all_platform_named = {
        os.path.abspath(p)
        for p in glob.glob(_ALL_RULES_GLOB, recursive=True)
        if os.path.basename(p).startswith("platform-")
    }
    missing = sorted(os.path.relpath(p, _RULEPACKS_DIR)
                     for p in all_platform_named - strict)
    assert missing == [], (
        "these platform-named extracts are discovered but NOT strict — prong 1's "
        f"glob is walking less of the tree than prong 2's: {missing}")
    # Floor so a shrinking strict set is loud. 9 `platform-*` files today; the
    # other 2 platform-mirroring fixtures (tenant-liveness-platform /
    # tenant-log-query-platform, 11 in total) are prong-2 by name, not strict.
    assert len(strict) >= 9, sorted(
        os.path.relpath(p, _RULEPACKS_DIR) for p in strict)


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
