"""Tests for the pint linter wrapper (check_pint.py).

Codifies the one hand-maintained sync point the wrapper depends on: the pint
version pinned in `check_pint.py` (docker fallback tag) must match the version
the CI step installs as a binary — otherwise CI runs a mixed setup (the
binary-on-PATH path wins, so the wrapper's pinned version may never execute and
a bump to one side silently drifts).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_pint_version_sync_between_wrapper_and_ci():
    wrapper = _read("scripts/tools/lint/check_pint.py")
    ci = _read(".github/workflows/ci.yml")

    m_wrapper = re.search(r'PINT_VERSION\s*=\s*"([\d.]+)"', wrapper)
    m_ci = re.search(r'\bPINT=([\d.]+)', ci)

    assert m_wrapper, "PINT_VERSION not found in check_pint.py"
    assert m_ci, "PINT=<version> not found in the ci.yml pint install step"
    assert m_wrapper.group(1) == m_ci.group(1), (
        f"pint version drift: check_pint.py PINT_VERSION={m_wrapper.group(1)} "
        f"vs ci.yml PINT={m_ci.group(1)} — keep them in sync "
        f"(docker tag has no `v`; the GitHub release tag the curl uses does)."
    )


def test_pint_config_scopes_to_rule_pack_sources():
    """The gate must scope to the canonical rule-pack sources (the ConfigMap +
    operator copies are unparseable by pint and synced by check_rulepack_sync.py)."""
    hcl = _read(".pint.hcl")
    assert re.search(r'include\s*=\s*\[[^\]]*rule-packs/rule-pack-', hcl), (
        "parser.include must scope to rule-packs/rule-pack-*.yaml"
    )
    # alerts/template (the killer check) must stay enabled globally — only the
    # match-all idiom disables (comparison/impossible/dependency) belong there.
    global_disable = re.search(r'rule\s*\{\s*disable\s*=\s*\[([^\]]*)\]', hcl)
    assert global_disable, "expected a global match-all `rule { disable = [...] }`"
    assert "alerts/template" not in global_disable.group(1), (
        "alerts/template must NOT be globally disabled — it is the gate's reason to exist"
    )
