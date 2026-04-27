"""Tests for scripts/tools/dx/generate_tenant_fixture.py.

Coverage focus: synthetic-v2 distribution mode (B-1 Phase 2 calibration
baseline). Pre-existing flat / hierarchical layouts are exercised
indirectly via the bench fixtures and don't have dedicated tests yet.

Distribution assertions are statistical, not exact-value: we verify
shape (Zipf skew ratio, power-law long-tail mass) on a sample size
large enough that variance is bounded. Per S#32 lesson, prefer
invariant-based asserts over absolute equality where the underlying
process is randomized — even with a fixed seed, internal RNG layout
changes between Python versions could break exact-value asserts.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_tenant_fixture.py"


@pytest.fixture(scope="module")
def fixture_module():
    """Load generate_tenant_fixture.py as a module without putting
    scripts/tools/dx/ on sys.path globally (avoids polluting import
    namespaces for sibling tests)."""
    spec = importlib.util.spec_from_file_location("generate_tenant_fixture", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_tenant_fixture"] = mod
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# Zipfian / power-law distribution helpers
# ============================================================


def test_zipfian_sizes_dominated_by_small_values(fixture_module):
    """Zipf alpha=1.5 should put most mass at size=1. We verify
    that >50% of sampled sizes are 1, and that sizes >= 4 are rare
    (<10%). Sample size 5000 → variance bounded enough that these
    inequalities hold across reasonable seed choices.
    """
    rng = fixture_module._seed_rng(42)
    sizes = fixture_module._zipfian_sizes(count=5000, alpha=1.5, max_size=6, rng=rng)

    p_small = sum(1 for s in sizes if s == 1) / len(sizes)
    p_large = sum(1 for s in sizes if s >= 4) / len(sizes)
    # Theoretical for Zipf alpha=1.5 / max_size=6: P(X=1)≈0.547, P(X>=4)≈0.154
    # Bounds chosen with ~30% slack on either side for sample-size jitter
    # (n=5000 → ~σ ≈ 0.7%; bounds at ±5% are safe).
    assert p_small > 0.5, f"size=1 should dominate; got {p_small:.3f}"
    assert p_large < 0.20, f"size>=4 should be a tail; got {p_large:.3f}"
    # All values within bounds.
    assert all(1 <= s <= 6 for s in sizes)


def test_zipfian_sizes_higher_alpha_more_skewed(fixture_module):
    """Increasing alpha should sharpen the drop-off — fewer large sizes."""
    rng_low = fixture_module._seed_rng(123)
    rng_high = fixture_module._seed_rng(123)
    low = fixture_module._zipfian_sizes(count=2000, alpha=1.0, max_size=10, rng=rng_low)
    high = fixture_module._zipfian_sizes(count=2000, alpha=2.5, max_size=10, rng=rng_high)
    p_high_at_low_alpha = sum(1 for s in low if s >= 5) / len(low)
    p_high_at_high_alpha = sum(1 for s in high if s >= 5) / len(high)
    assert p_high_at_high_alpha < p_high_at_low_alpha, (
        f"higher alpha should reduce p(size>=5): low_alpha={p_high_at_low_alpha:.3f} "
        f"high_alpha={p_high_at_high_alpha:.3f}"
    )


def test_power_law_depths_long_tail(fixture_module):
    """Power-law alpha=2.0 should put most mass at depth=0 with a
    long tail at higher depths. Verify >60% are 0 and depth=3 rare (<10%).
    """
    rng = fixture_module._seed_rng(42)
    depths = fixture_module._power_law_depths(count=5000, alpha=2.0, max_depth=3, rng=rng)

    p_zero = sum(1 for d in depths if d == 0) / len(depths)
    p_max = sum(1 for d in depths if d == 3) / len(depths)
    assert p_zero > 0.6, f"depth=0 should dominate; got {p_zero:.3f}"
    assert p_max < 0.10, f"depth=3 should be rare; got {p_max:.3f}"
    assert all(0 <= d <= 3 for d in depths)


def test_seeded_rng_reproducible(fixture_module):
    """Same seed → same output. Critical for benchmark reproducibility."""
    a = fixture_module._zipfian_sizes(
        count=100, alpha=1.5, max_size=6, rng=fixture_module._seed_rng(42)
    )
    b = fixture_module._zipfian_sizes(
        count=100, alpha=1.5, max_size=6, rng=fixture_module._seed_rng(42)
    )
    assert a == b


# ============================================================
# generate_synthetic_v2 end-to-end
# ============================================================


def test_synthetic_v2_creates_hierarchical_tree(fixture_module, tmp_path):
    """Output dir should mirror generate_hierarchical layout: tenants
    distributed across domain/region/env subdirectories."""
    out = tmp_path / "synthetic-v2"
    fixture_module.generate_synthetic_v2(count=144, output_dir=out, with_defaults=True, seed=42)

    domains = list((out).iterdir())
    assert len(domains) >= 8  # 8 DOMAINS expected (or fewer if count < slots)
    # _defaults.yaml at root.
    assert (out / "_defaults.yaml").exists()


def test_synthetic_v2_count_matches_request(fixture_module, tmp_path):
    """Generated tenant file count == --count, regardless of layout."""
    out = tmp_path / "synthetic-v2"
    fixture_module.generate_synthetic_v2(count=200, output_dir=out, with_defaults=False, seed=42)

    tenant_files = [
        p for p in out.rglob("*.yaml") if p.name != "_defaults.yaml"
    ]
    assert len(tenant_files) == 200


def test_synthetic_v2_yaml_has_extra_threshold_keys(fixture_module, tmp_path):
    """At least some tenants should have `_extra_threshold_NN` keys
    from the Zipfian size>1 sample. Sample size 500 ensures we hit
    at least a few size>=2 tenants under any reasonable seed.
    """
    out = tmp_path / "synthetic-v2"
    fixture_module.generate_synthetic_v2(count=500, output_dir=out, with_defaults=False, seed=42)

    # Read all tenant files and look for the marker key.
    found_extra = False
    for p in out.rglob("*.yaml"):
        if p.name == "_defaults.yaml":
            continue
        text = p.read_text(encoding="utf-8")
        if "_extra_threshold_" in text:
            found_extra = True
            break
    assert found_extra, "expected at least one tenant with _extra_threshold_NN key (Zipf size>1)"


def test_synthetic_v2_yaml_has_overlay_blocks(fixture_module, tmp_path):
    """At least some tenants should have `_overlay_l0` nested overlay
    blocks from the power-law depth>0 sample.
    """
    out = tmp_path / "synthetic-v2"
    fixture_module.generate_synthetic_v2(count=500, output_dir=out, with_defaults=False, seed=42)

    found_overlay = False
    for p in out.rglob("*.yaml"):
        if p.name == "_defaults.yaml":
            continue
        text = p.read_text(encoding="utf-8")
        if "_overlay_l0" in text:
            found_overlay = True
            break
    assert found_overlay, "expected at least one tenant with _overlay_l0 nested block (power-law depth>0)"


def test_synthetic_v2_seed_reproducible(fixture_module, tmp_path):
    """Same seed → byte-identical fixture trees."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    fixture_module.generate_synthetic_v2(count=50, output_dir=a, with_defaults=True, seed=2026)
    fixture_module.generate_synthetic_v2(count=50, output_dir=b, with_defaults=True, seed=2026)

    files_a = sorted(p.relative_to(a) for p in a.rglob("*.yaml"))
    files_b = sorted(p.relative_to(b) for p in b.rglob("*.yaml"))
    assert files_a == files_b
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), f"diverged: {rel}"


def test_synthetic_v2_different_seeds_produce_different_trees(fixture_module, tmp_path):
    """Different seeds → different fixture content (sanity: seeding works)."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    fixture_module.generate_synthetic_v2(count=100, output_dir=a, with_defaults=False, seed=1)
    fixture_module.generate_synthetic_v2(count=100, output_dir=b, with_defaults=False, seed=2)

    # Concatenate all tenant YAML content from each tree; the totals
    # must differ (extremely unlikely both seeds collapse identically
    # to same Zipf+power-law samples by accident).
    blob_a = b"".join(sorted(p.read_bytes() for p in a.rglob("*.yaml")))
    blob_b = b"".join(sorted(p.read_bytes() for p in b.rglob("*.yaml")))
    assert blob_a != blob_b


# ── _gen_defaults_yaml numeric contract regression (cycle-6 RCA, PR #105) ──
#
# The exporter's _defaults.yaml parser only accepts numeric (float64) values
# in the `defaults:` block. Scheduled-threshold strings ("17838:critical")
# and "disable" sentinels are valid only in tenant override files. If those
# forms appear in defaults, the parser silently rejects the entire file
# (`cannot unmarshal !!str into float64`) — every default is dropped, and
# downstream tenant overrides break with `unknown key not in defaults`.
#
# This was the root cause that took 6 RCA cycles to surface during B-1
# Phase 2 e2e harness rollout (planning archive §S#37d cycle-6 cause #1).
# The fix in PR #105 made `_gen_defaults_yaml` emit ints only; this test
# locks that contract so a future regression (e.g. someone adding a
# scheduled-form metric to METRIC_TEMPLATES + reusing the same generator)
# fails fast instead of silently breaking the e2e harness.

def test_gen_defaults_yaml_emits_only_numeric_values(fixture_module):
    """Every value in the `defaults:` block must be int/float — never
    str (scheduled threshold) or list/dict. Regression guard for cycle-6
    RCA; see archive §S#37d."""
    import random

    rng = random.Random(0xDEFA17)  # arbitrary fixed seed
    out = fixture_module._gen_defaults_yaml(rng)

    assert "defaults" in out, "result must have top-level 'defaults' key"
    defaults_block = out["defaults"]
    assert isinstance(defaults_block, dict), "'defaults' must be a dict"
    assert len(defaults_block) > 0, "defaults block must be non-empty"

    for key, value in defaults_block.items():
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"_defaults.yaml value {key!r}={value!r} is type {type(value).__name__}; "
            f"must be numeric (cycle-6 RCA: parser drops file on str). "
            f"If you need scheduled or 'disable' sentinels, put them in tenant "
            f"override files, NOT in _defaults.yaml."
        )


def test_gen_defaults_yaml_with_db_types_subset_still_numeric(fixture_module):
    """Same numeric contract holds when caller passes a db_types subset
    (the cascading-defaults path used by hierarchical / synthetic-v2)."""
    import random

    rng = random.Random(0xDEFA18)
    out = fixture_module._gen_defaults_yaml(rng, db_types=["mysql", "redis"])

    defaults_block = out["defaults"]
    assert len(defaults_block) > 0, "defaults block must be non-empty for subset"
    for key, value in defaults_block.items():
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"_defaults.yaml value {key!r}={value!r} is non-numeric"
        )


# ── --extra-defaults flag (Track A A6, replaces orchestrator inline-Python) ──

def test_gen_defaults_yaml_extra_defaults_appended(fixture_module):
    """Numeric extras are merged into the defaults block."""
    import random

    rng = random.Random(0xEEE)
    out = fixture_module._gen_defaults_yaml(
        rng, extra_defaults={"bench_trigger": 50, "another_metric": 1.5}
    )
    block = out["defaults"]
    assert block.get("bench_trigger") == 50
    assert block.get("another_metric") == 1.5


def test_gen_defaults_yaml_extra_defaults_rejects_string(fixture_module):
    """Non-numeric extras must be rejected (cycle-6 contract)."""
    import random

    rng = random.Random(0xEEF)
    with pytest.raises(ValueError, match="not numeric"):
        fixture_module._gen_defaults_yaml(
            rng, extra_defaults={"bad_key": "not-a-number"}
        )


def test_gen_defaults_yaml_extra_defaults_rejects_bool(fixture_module):
    """bool is a subclass of int but represents a different contract;
    the parser would coerce True→1 silently. Reject explicitly."""
    import random

    rng = random.Random(0xEF0)
    with pytest.raises(ValueError, match="not numeric"):
        fixture_module._gen_defaults_yaml(
            rng, extra_defaults={"bad_bool": True}
        )
