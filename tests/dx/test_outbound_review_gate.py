"""The external-review step in `vibe-brainstorm` must point at the outbound gate (#1486).

`.agents/skills/vibe-brainstorm/SKILL.md` tells the agent to send a locked design
to an external model for adversarial review. The conditions for sending anything
out (approved provider, what must be removed, otherwise use an internal reviewer)
live in one place: the outbound section of `docs/internal/engagement-deid-policy.md`.
This test pins the link between the two, and the minimum content of that section,
so the instruction to send cannot lose its precondition without going red.

What goes red:
- the link is dropped from the `## 外審` section of the skill, or survives only
  inside an HTML comment;
- the link points at any heading other than the outbound one (e.g. `#三層分類`);
- the outbound heading is renamed, so the link no longer resolves;
- the outbound section loses its approved-provider list or its fallback to an
  internal reviewer.

The doc-link linter (`check_doc_links.py`, which also scans `.agents/`) catches a
dangling anchor too; it cannot tell WHICH heading the link should point at.

Honest scope: this guards that ONE instruction. A new skill or role that tells the
agent to send content to an external model is not detected here; the only such
instruction under `.agents/` today is this one. The test reads strings, not
meaning: a sentence that keeps the link but negates it ("no need to pass [gate]")
stays green.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".agents" / "skills" / "vibe-brainstorm" / "SKILL.md"
POLICY = REPO_ROOT / "docs" / "internal" / "engagement-deid-policy.md"
OUTBOUND_HEADING_MARK = "出站"
# Minimum content of the outbound section: the approval list and the fallback.
OUTBOUND_MUST_CONTAIN = ("已核准的 provider", "改用內部 reviewer")

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
from check_doc_links import _gfm_anchor  # noqa: E402  (same slugger the link lint uses)

_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)#([^)\s]+)\)")
_FENCE = re.compile(r"^(```|~~~)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _sections(text: str) -> dict[str, str]:
    """Level-2 heading text -> its body (HTML comments removed, fences respected)."""
    out: dict[str, list[str]] = {}
    current = None
    in_fence = False
    for line in _COMMENT.sub("", text).splitlines():
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = line[3:].strip()
            out[current] = []
            continue
        if current is not None:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _review_section() -> str:
    sections = _sections(SKILL.read_text(encoding="utf-8"))
    assert "外審" in sections, f"section '## 外審' not found in {SKILL}"
    return sections["外審"]


def _outbound_heading() -> str:
    heads = [h for h in _sections(POLICY.read_text(encoding="utf-8")) if OUTBOUND_HEADING_MARK in h]
    assert len(heads) == 1, f"expected exactly one '## ...{OUTBOUND_HEADING_MARK}...' heading in the policy, got {heads}"
    return heads[0]


def _policy_anchors(section: str) -> list[str]:
    return [
        anchor
        for target, anchor in _LINK.findall(section)
        if (SKILL.parent / target).resolve() == POLICY.resolve()
    ]


def test_external_review_section_links_to_the_policy():
    assert _policy_anchors(_review_section()), (
        "the external-review section of vibe-brainstorm no longer links to "
        "engagement-deid-policy.md; the instruction to send content to an external "
        "model would carry no precondition (#1486)"
    )


def test_the_link_points_at_the_outbound_section():
    anchors = _policy_anchors(_review_section())
    assert anchors, "no link to the policy to check (see the test above)"
    want = _gfm_anchor(_outbound_heading())
    assert want in anchors, (
        f"vibe-brainstorm links to engagement-deid-policy.md#{anchors}, "
        f"but the outbound section's anchor is #{want}"
    )


def test_the_outbound_section_keeps_its_minimum_content():
    body = _sections(POLICY.read_text(encoding="utf-8"))[_outbound_heading()]
    missing = [s for s in OUTBOUND_MUST_CONTAIN if s not in body]
    assert not missing, f"the outbound section of the policy lost: {missing}"
