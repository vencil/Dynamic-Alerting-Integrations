"""test_revoked_set_contract_guard.py — revoked-set line contract guards (#1235 / TRK-349)

`revokedSet.tokenIdPattern` decides which `revoked.txt` lines the ENFORCEMENT
plane accepts. It is templated into `files/revoked_check.lua` rather than
hard-coded there, because `revoked.txt` has two independently-implemented
readers and a copy of the contract living in each script is the drift #1235 is
about. That design choice puts a security-relevant value on the values surface,
where an operator can change it — so the chart has to refuse the ways of
changing it that fail SILENTLY.

Three template-time guards, all of which must abort the render:

  1. empty — Lua's `string.match` against "" succeeds for every input, so an
     empty value accepts any line while still looking configured.
  2. unanchored — an un-anchored pattern matches a SUBSTRING, so a line could
     carry arbitrary bytes around a valid-looking id.
  3. regex-only syntax — the value is a LUA PATTERN. Lua has no brace
     quantifiers, no backslash escapes and no alternation, so those characters
     match literally. The trap is that the natural "tightening" (pinning the id
     length with braces) is valid regex, passes guards 1 and 2, and matches
     NOTHING — the gateway then refuses every reload and silently keeps the set
     it already had, while a freshly started worker enforces none at all. No
     attacker required; this one is reachable by an operator trying to help.

Mirrors test_victorialogs_gateway_guard.py: a static layer that needs no helm
(so helm-less runners still catch a regression) and a render layer gated on the
helm CLI.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/federation-gateway"
_HELPERS = "helm/federation-gateway/templates/_helpers.tpl"
_VALUES = "helm/federation-gateway/values.yaml"
_LUA = "helm/federation-gateway/files/revoked_check.lua"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer (no helm) ───────────────────────────────────────────────────
def test_the_guard_is_reachable_from_a_render(repo_root: Path):
    """A `fail` nobody calls is not a guard. `validateRevokedSet` has to be
    included from something every render evaluates."""
    helpers = (repo_root / _HELPERS).read_text(encoding="utf-8")
    assert 'define "federation-gateway.validateRevokedSet"' in helpers
    assert 'include "federation-gateway.validateRevokedSet"' in helpers, \
        "the guard is defined but never included — it would never run"


def test_the_lua_consumes_the_chart_value(repo_root: Path):
    """If the filter carried its own copy of the pattern, every guard here
    would be checking a value the enforcement plane does not use."""
    lua = (repo_root / _LUA).read_text(encoding="utf-8")
    assert ".Values.revokedSet.tokenIdPattern" in lua


def test_the_shipped_default_is_expressible_as_a_lua_pattern(repo_root: Path):
    """The default has to survive its own guard — a chart whose default value
    fails validation is a chart that cannot be installed."""
    values = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8"))
    pattern = values["revokedSet"]["tokenIdPattern"]
    assert pattern.startswith("^") and pattern.endswith("$")
    for ch in ("{", "}", "\\", "|", "%"):
        assert ch not in pattern, f"the shipped default contains {ch!r}"


# ── render layer (helm-gated) ────────────────────────────────────────────────
def _render(repo_root: Path, pattern: str | None) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    if pattern is not None:
        # -f, not --set: --set applies its own escaping rules to the very
        # characters under test, so a --set-based case can pass for the wrong
        # reason (or never reach the guard at all).
        cmd += ["-f", "-"]
        return subprocess.run(
            cmd, input=yaml.safe_dump({"revokedSet": {"tokenIdPattern": pattern}}),
            capture_output=True, text=True, timeout=60)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@_needs_helm
def test_default_renders(repo_root: Path):
    r = _render(repo_root, None)
    assert r.returncode == 0, r.stderr
    assert 'local TOKEN_ID_PATTERN = "^ftk_' in r.stdout, \
        "the contract did not reach the rendered filter"


@_needs_helm
@pytest.mark.parametrize("pattern,fingerprint", [
    ("", "must not be empty"),
    ("ftk_[0-9a-f]+", "must be anchored"),
    ("^ftk_[0-9a-f]+", "must be anchored"),
    ("ftk_[0-9a-f]+$", "must be anchored"),
    # The operator foot-gun: valid regex, passes the anchor and empty checks,
    # matches nothing under Lua pattern semantics.
    ("^ftk_[0-9a-f]{16}$", "does not mean the same thing"),
    (r"^ftk_\w+$", "does not mean the same thing"),
    ("^(ftk|xtk)_[0-9a-f]+$", "does not mean the same thing"),
    # The mirror-image trap: valid and correct as a Lua pattern, taken
    # literally by the Go writer and the Python reconciler. Rejected for the
    # same reason as the regex-only ones — the three sides would stop meaning
    # the same thing while their literals still matched each other.
    ("^ftk_%x+$", "does not mean the same thing"),
])
def test_a_silently_broken_pattern_aborts_the_render(repo_root: Path, pattern, fingerprint):
    r = _render(repo_root, pattern)
    assert r.returncode != 0, \
        f"{pattern!r} rendered successfully — the gateway would accept it and enforce nothing"
    assert fingerprint in r.stderr, (pattern, r.stderr[-400:])


@_needs_helm
def test_a_legitimate_tightening_still_renders(repo_root: Path):
    """The guards must reject what is silently broken, not everything that is
    not the default — otherwise the value stops being a knob and the next
    contract change has to fight the chart."""
    r = _render(repo_root, "^ftk_[0-9a-f][0-9a-f]+$")
    assert r.returncode == 0, r.stderr
