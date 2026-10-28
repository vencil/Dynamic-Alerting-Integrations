"""Every not_served reason the exporter can give is defined in the CLI
reference (#3250 review).

The reasons are a closed set held by pkg/config's ``NotServed*`` constants
(not_served.go). ``docs/cli-reference.md`` (and its ``.en.md``) is the one
place that defines them for readers; the tenant-api `/effective` annotation
points there instead of listing them again. This test holds the doc to the
constants: a reason added in Go and not documented turns it red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NOT_SERVED_GO = REPO / "components/threshold-exporter/app/pkg/config/not_served.go"

# The paragraph that opens the closed set, per language.
_MARKERS = {
    "docs/cli-reference.md": "`reason` 是封閉集合",
    "docs/cli-reference.en.md": "`reason` is a closed set",
}


def _reasons() -> set[str]:
    src = NOT_SERVED_GO.read_text(encoding="utf-8")
    block = re.search(r"// Reasons of NotServedKey\.Reason — a closed set\.\nconst \((.*?)\n\)",
                      src, re.S)
    assert block, f"{NOT_SERVED_GO}: the NotServed* const block moved — update this test"
    found = set(re.findall(r'^\s*NotServed\w+\s*=\s*"([a-z_]+)"', block.group(1), re.M))
    assert found, "no NotServed* constant read"
    return found


def _paragraph(rel: str) -> str:
    text = (REPO / rel).read_text(encoding="utf-8")
    marker = _MARKERS[rel]
    assert text.count(marker) == 1, f"{rel}: expected the closed-set paragraph exactly once"
    start = text.index(marker)
    end = text.find("\n", start)
    return text[start:end if end >= 0 else len(text)]


@pytest.mark.parametrize("rel", sorted(_MARKERS))
def test_every_reason_is_defined_in_the_cli_reference(rel):
    documented = set(re.findall(r"`([a-z_]+)`", _paragraph(rel)))
    missing = sorted(_reasons() - documented)
    assert not missing, f"{rel}: not_served reasons the exporter gives but the doc does not define: {missing}"

