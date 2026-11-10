"""The Rule Pack Detail page's per-pack links resolve (#3681).

Since #3681 the page carries no hand-copied rules: it shows the generated
pack data and links each pack to `rule-packs/rule-pack-<key>.yaml` and to the
generated ALERT-REFERENCE. Its pack list is the generated platform data's, so
the one thing that can still break is a pack whose YAML lives under another
name — the link would 404. This pins every listed pack to its file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JSX = REPO / "tools" / "portal" / "src" / "interactive" / "tools" / "rule-pack-detail.jsx"


def test_every_listed_pack_links_to_its_rule_pack_file():
    packs = json.loads((REPO / "docs" / "assets" / "platform-data.json").read_text(encoding="utf-8"))["rulePacks"]
    assert packs, "platform-data lists no rule packs"
    missing = [k for k in packs if not (REPO / "rule-packs" / f"rule-pack-{k}.yaml").is_file()]
    assert missing == [], missing
    assert "rule-packs/rule-pack-${selectedPack}.yaml" in JSX.read_text(encoding="utf-8")


def _alerts(pack: str) -> set[str]:
    text = (REPO / "rule-packs" / f"rule-pack-{pack}.yaml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s*-\s*alert:\s*(\S+)\s*$", text, re.M))


def test_the_alert_reference_link_is_shown_exactly_for_the_packs_it_covers():
    """The page hides the alert-reference link for NOT_IN_ALERT_REFERENCE;
    every other pack must have its alerts in the generated reference, and
    every excluded one must not (else the link is wrongly hidden)."""
    packs = json.loads((REPO / "docs" / "assets" / "platform-data.json").read_text(encoding="utf-8"))["rulePacks"]
    m = re.search(r"NOT_IN_ALERT_REFERENCE = \[([^\]]*)\]", JSX.read_text(encoding="utf-8"))
    assert m, "NOT_IN_ALERT_REFERENCE not found in the page"
    excluded = set(re.findall(r"'([^']+)'", m.group(1)))
    for name in ("ALERT-REFERENCE.md", "ALERT-REFERENCE.en.md"):
        ref = (REPO / "rule-packs" / name).read_text(encoding="utf-8")
        for pack in packs:
            alerts = _alerts(pack)
            if not alerts:
                continue
            listed = any(f"| {a} |" in ref for a in alerts)
            assert listed is (pack not in excluded), (name, pack, listed)


def test_the_page_carries_no_hand_copied_rules():
    # The copy that drifted (#3681): rule-shaped literals in the page, in any
    # quoting.
    text = JSX.read_text(encoding="utf-8")
    q = "[\"'`]"
    assert not re.search(rf"name:\s*{q}tenant:", text)
    assert not re.search(rf"severity:\s*{q}", text)
    assert not re.search(r"\b(expr|record|alert)\s*:\s*" + q, text)
