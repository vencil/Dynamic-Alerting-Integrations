#!/usr/bin/env python3
"""check_portal_audience_enum.py — portal `audience` values must come from a closed vocabulary.

Why this exists
---------------
The Hub role filter (docs/interactive/index.html) binds its buttons —
Platform / Domain / Tenant / SRE — to the `audience` taxonomy that lives
in TWO hand-maintained places:

  1. docs/assets/tool-registry.yaml  — each tool's `audience:` list (the
     SSOT the dynamic Hub cards render from); and
  2. docs/interactive/index.html      — the static `#linter-cards` mirror
     block's `data-audience="…"` attributes (+ the quick-access chips).

Before this lint these two drifted: the same persona appeared as both
`domain` and `domain-expert`, and one-off `management` / `contributor`
tokens matched NO filter button, so those tools silently vanished under
every non-"All" filter. We normalised the stragglers
(domain-expert→domain, management+contributor→maintainer) and this lint
LOCKS the vocabulary closed so it can't drift again (codified-beats-
documented).

Closed vocabulary
-----------------
  platform | tenant | domain | sre | maintainer | sec

`sec` is reserved (Security/Compliance role per the governance matrix)
even though no portal tool targets it yet — so adding a sec-audience tool
later does not require touching this lint. `maintainer` is the portal-
internal bucket for tools the platform TEAM uses (ROI calculators,
release-notes) — it deliberately has no top-level filter button (it lives
in the collapsed "Internal" section); a button filtering to a collapsed
niche would itself be a findability bug.

Usage
-----
  python3 scripts/tools/lint/check_portal_audience_enum.py

Exit codes:
  0 = every audience / data-audience value is in the closed set
  1 = at least one out-of-vocabulary value
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY = REPO_ROOT / "docs" / "assets" / "tool-registry.yaml"
HUB_HTML = REPO_ROOT / "docs" / "interactive" / "index.html"

# The closed audience vocabulary. Aligned to the governance matrix action
# roles {PE, TA, SRE, DE, SEC} (docs/internal/monitoring-lifecycle-governance
# -matrix.md) plus `maintainer` for platform-team-internal tools. Keep this
# the SINGLE source of the allowed set; the Hub role-filter buttons are a
# (deliberate) SUBSET of it.
ALLOWED_AUDIENCES = {"platform", "tenant", "domain", "sre", "maintainer", "sec"}

_DATA_AUDIENCE_RE = re.compile(r'data-audience="([^"]*)"')


def _check_registry() -> list[str]:
    issues: list[str] = []
    with REGISTRY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for entry in data.get("tools", []):
        key = entry.get("key", "<no-key>")
        for aud in entry.get("audience", []) or []:
            if aud not in ALLOWED_AUDIENCES:
                issues.append(
                    f"tool-registry.yaml: tool '{key}' has audience "
                    f"'{aud}' not in the closed set"
                )
    return issues


def _check_hub_html() -> list[str]:
    issues: list[str] = []
    text = HUB_HTML.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _DATA_AUDIENCE_RE.finditer(line):
            for token in match.group(1).split(","):
                token = token.strip()
                # Skip non-identifier tokens. renderTools() emits a literal
                # `data-audience="' + audiences + '"` whose value is built at
                # runtime FROM the registry (already covered by
                # _check_registry); only static, audience-shaped tokens
                # (lowercase + hyphen) are lintable here. A real drifted value
                # like `domain-expert` IS [a-z-]+ so it is still caught.
                if not token or not re.fullmatch(r"[a-z-]+", token):
                    continue
                if token not in ALLOWED_AUDIENCES:
                    issues.append(
                        f"index.html:{lineno}: data-audience value "
                        f"'{token}' not in the closed set"
                    )
    return issues


def main() -> int:
    # argparse with no flags — enforces the repo-wide CLI contract
    # (test_help_exits_zero / test_invalid_args_exits_nonzero): `--help`
    # exits 0; unknown flags exit 2 (argparse default).
    parser = argparse.ArgumentParser(
        description=(
            "Validate that every portal `audience` / `data-audience` value "
            "is in the closed vocabulary "
            f"({', '.join(sorted(ALLOWED_AUDIENCES))})."
        ),
    )
    parser.parse_args()

    for path, label in ((REGISTRY, "tool-registry.yaml"), (HUB_HTML, "index.html")):
        if not path.exists():
            print(f"ERROR: {label} not found at {path}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    issues = _check_registry() + _check_hub_html()

    if not issues:
        print(
            "OK: all portal audience / data-audience values are in the "
            f"closed set ({', '.join(sorted(ALLOWED_AUDIENCES))})."
        )
        return EXIT_OK

    print(f"FAIL: {len(issues)} out-of-vocabulary audience value(s):")
    for issue in issues:
        print(f"  - {issue}")
    print(
        "\nFix: use one of "
        f"{', '.join(sorted(ALLOWED_AUDIENCES))}. If you renamed a persona, "
        "update BOTH docs/assets/tool-registry.yaml and the #linter-cards "
        "mirror in docs/interactive/index.html. To introduce a genuinely new "
        "audience, add it to ALLOWED_AUDIENCES here (and a filter button if it "
        "warrants one)."
    )
    return EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
