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


def test_the_alert_reference_it_links_exists():
    for name in ("ALERT-REFERENCE.md", "ALERT-REFERENCE.en.md"):
        assert (REPO / "rule-packs" / name).is_file(), name


def test_the_page_carries_no_hand_copied_rules():
    # The copy that drifted (#3681): `alert:`-shaped literals in the page.
    text = JSX.read_text(encoding="utf-8")
    assert not re.search(r"name:\s*'tenant:", text)
    assert not re.search(r"severity:\s*'(warning|critical)'", text)
