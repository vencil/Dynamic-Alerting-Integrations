"""#2421: a threshold /metrics treats as unset falls back to the layer
below — in `diagnose`'s `resolved` too, at the profile layer as well as the
tenant layer.

Measured before the fix (main ca7adc10): `_profiles.yaml` `p: {K: null}`
resolved to `{K: None}` and a tenant `K: ''` resolved to `{K: ''}`, while
the exporter (LoadDir + ResolveAt) served the defaults' 10 for both.

Every expected value below was measured against the exporter the same way;
`GO_PARSE_FLOAT` is `strconv.ParseFloat(s, 64)`'s own verdict on each text.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
import diagnose  # noqa: E402

K = "mysql_connections"
DEFAULTS = f"defaults:\n  {K}: 10\n"

# (YAML text written as the value, what /metrics serves for K)
PROFILE_LAYER = [
    ("null", 10), ("~", 10), ("''", 10), ("'  '", 10), ("abc", 10),
    ("'7O:critical'", 10), ("true", 10), ("yes", 10), (".inf", 10),
    ("1.0e+400", 10), ("'+nan'", 10), ("'1e400'", 10),
    # controls: real values stay
    ("5", 5), ("'10'", "10"), ("'70:critical'", "70:critical"),
    ("disable", "disable"), ("1e3", "1e3"),
]
# The profile writes 20; the tenant's key keeps it out whatever the value,
# so an unset tenant value lands on the defaults' 10, never the profile's 20.
TENANT_LAYER = [
    ("null", 10), ("''", 10), ('""', 10), ("'  '", 10), ("abc", 10),
    ("'7O:critical'", 10), ("true", 10), (".inf", 10), ("'1e400'", 10),
    ("5", 5), ("'70:critical'", "70:critical"), ("disable", "disable"),
]


def _resolved(tmp_path: Path, files: dict) -> dict:
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    return diagnose.resolve_inheritance_chain("tx", str(tmp_path))["resolved"]


@pytest.mark.parametrize("text,want", PROFILE_LAYER, ids=[t for t, _ in PROFILE_LAYER])
def test_profile_layer_value_resolves_as_on_metrics(text, want, tmp_path) -> None:
    got = _resolved(tmp_path, {
        "_defaults.yaml": DEFAULTS,
        "_profiles.yaml": f"profiles:\n  p:\n    {K}: {text}\n",
        "tx.yaml": "tenants:\n  tx:\n    _profile: p\n",
    })
    assert got.get(K) == want, got


@pytest.mark.parametrize("text,want", TENANT_LAYER, ids=[t for t, _ in TENANT_LAYER])
def test_tenant_layer_value_resolves_as_on_metrics(text, want, tmp_path) -> None:
    got = _resolved(tmp_path, {
        "_defaults.yaml": DEFAULTS,
        "_profiles.yaml": f"profiles:\n  p:\n    {K}: 20\n",
        "tx.yaml": f"tenants:\n  tx:\n    _profile: p\n    {K}: {text}\n",
    })
    assert got.get(K) == want, got


def test_chain_still_shows_what_the_file_says(tmp_path) -> None:
    """Only `resolved` follows /metrics; the chain layers report the files."""
    (tmp_path / "_defaults.yaml").write_text(DEFAULTS, encoding="utf-8")
    (tmp_path / "_profiles.yaml").write_text(f"profiles:\n  p:\n    {K}: null\n",
                                             encoding="utf-8")
    (tmp_path / "tx.yaml").write_text("tenants:\n  tx:\n    _profile: p\n",
                                      encoding="utf-8")
    chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))["chain"]
    assert {c["layer"]: c["keys"] for c in chain}["profile"] == {K: None}


# strconv.ParseFloat(s, 64) == nil error, measured with Go 1.26.
GO_PARSE_FLOAT = [
    ("10", True), ("1e3", True), ("5.", True), (".5", True), ("+.5", True),
    ("1_000", True), ("1e1_0", True), ("0x1p3", True), ("0X.8P0", True),
    ("0x_1p1", True), ("inf", True), ("+Inf", True), ("-Infinity", True),
    ("NaN", True), ("1e-400", True),
    ("", False), (".", False), ("abc", False), ("7O", False), ("+nan", False),
    ("-nan", False), ("infinit", False), ("1e400", False), ("-1e400", False),
    ("0x10", False), ("0x1p", False), ("1__0", False), ("_1", False),
    ("1_", False), ("1e", False), ("0b1", False), ("١٢", False),
]


@pytest.mark.parametrize("text,ok", GO_PARSE_FLOAT, ids=[repr(t) for t, _ in GO_PARSE_FLOAT])
def test_parse_float_mirror_agrees_with_go(text, ok) -> None:
    assert diagnose._go_parse_float_ok(text) is ok


@pytest.mark.parametrize("value,unset", [
    (None, True), ("", True), ("  ", True), ("abc", True), ("7O:critical", True),
    ("disable:critical", True), (True, True), (float("inf"), True),
    (float("nan"), True),
    ("10", False), (" 70 : critical ", False), ("Disabled", False),
    (" off ", False), ("0x1p3", False), (5, False), (5.5, False),
    # a loaded False is `false`/`off` (disable) or `no` (unset): not guessed.
    # A scheduled mapping resolves by time of day: out of this helper's scope.
    (False, False), ({"default": ""}, False),
])
def test_metrics_treats_as_unset(value, unset) -> None:
    assert diagnose.metrics_treats_as_unset(value) is unset
