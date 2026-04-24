#!/usr/bin/env python3
"""Property-based tests using Hypothesis for core config parsing functionality.

Tests core areas:
  1. Tenant name validation (RFC 1123 compliant)
  2. SHA-256 file hashing (consistency and format)
  3. Drift detection symmetry (manifest comparison)
  4. YAML round-trip parsing (dict -> yaml -> dict)
  5. Kustomization builder (apiVersion/kind + resources)
  6. deep_merge algebraic properties (ADR-018 semantics, v2.8.0 A-8)
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
import tempfile
import hashlib
import json
import yaml as yaml_lib

from pathlib import Path
from typing import Any, Dict

import pytest
from hypothesis import given, assume, settings, HealthCheck
from hypothesis import strategies as st

# Setup paths for imports
_OPS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _OPS_DIR)
sys.path.insert(0, os.path.join(_OPS_DIR, '..'))

# Import functions to test
from migrate_to_operator import validate_tenant_name  # noqa: E402
from drift_detect import _file_sha256, compare_manifests, FileManifest  # noqa: E402
from operator_generate import build_kustomization  # noqa: E402

# describe_tenant.deep_merge lives in scripts/tools/dx/ — load via importlib so
# we don't pollute sys.path with the dx/ directory (module names collide
# across scripts/tools/{ops,dx}/).
_DESCRIBE_TENANT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "describe_tenant.py"
_spec = importlib.util.spec_from_file_location("_describe_tenant_for_tests", _DESCRIBE_TENANT)
_describe_tenant_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_describe_tenant_mod)
deep_merge = _describe_tenant_mod.deep_merge


# ─────────────────────────────────────────────────────────────────────────────
# Hypothesis Strategies
# ─────────────────────────────────────────────────────────────────────────────

@st.composite
def valid_tenant_names(draw):
    """Generate valid RFC 1123 compliant tenant names.

    Rules:
      - lowercase alphanumeric + hyphens
      - 1-63 characters
      - must start and end with alphanumeric
    """
    # Single alphanumeric (1 char is valid)
    if draw(st.booleans()):
        return draw(st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789'))

    # Multi-character: start with alphanum, optional middle chars, end with alphanum
    start = draw(st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789'))
    middle_len = draw(st.integers(min_value=0, max_value=61))
    middle = draw(
        st.lists(
            st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789-'),
            min_size=middle_len,
            max_size=middle_len,
        )
    )

    if middle:
        end = draw(st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789'))
        return start + ''.join(middle) + end
    else:
        return start


@st.composite
def invalid_tenant_names(draw):
    """Generate invalid tenant names (should fail validation)."""
    invalid_type = draw(st.sampled_from([
        'uppercase',      # uppercase letters
        'spaces',         # leading/trailing spaces
        'start_hyphen',   # starts with hyphen
        'end_hyphen',     # ends with hyphen
        'too_long',       # > 63 chars
        'special',        # special characters
        'empty',          # empty string
    ]))

    if invalid_type == 'uppercase':
        # Must have at least one uppercase
        base = draw(st.text(alphabet='abc123', min_size=0, max_size=5))
        upper = draw(st.sampled_from('ABCDEFGHIJKLMNOPQRSTUVWXYZ'))
        return (base + upper + 'a').replace('a', '', 1) or upper
    elif invalid_type == 'spaces':
        name = draw(st.text(alphabet='abc123', min_size=1, max_size=5))
        return ' ' + name + ' '
    elif invalid_type == 'start_hyphen':
        name = draw(st.text(alphabet='abc123', min_size=1, max_size=5))
        return '-' + name
    elif invalid_type == 'end_hyphen':
        name = draw(st.text(alphabet='abc123', min_size=1, max_size=5))
        return name + '-'
    elif invalid_type == 'too_long':
        return 'a' * 64
    elif invalid_type == 'special':
        # Include at least one special char
        base = draw(st.text(alphabet='abc123', min_size=0, max_size=5))
        special = draw(st.sampled_from('!@#$%^&*()'))
        return base + special + 'a'
    else:  # empty
        return ''


@st.composite
def yaml_dicts(draw):
    """Generate valid tenant config-like dicts with text keys/values."""
    return draw(st.dictionaries(
        keys=st.text(
            alphabet='abcdefghijklmnopqrstuvwxyz0123456789_',
            min_size=1,
            max_size=20
        ),
        values=st.text(
            alphabet='abcdefghijklmnopqrstuvwxyz0123456789_-./@: ',
            min_size=0,
            max_size=50
        ),
        min_size=1,
        max_size=10
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: Tenant Name Validation
# ─────────────────────────────────────────────────────────────────────────────

class TestTenantNameValidation:
    """Property-based tests for tenant name validation."""

    @given(name=valid_tenant_names())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_valid_names_pass_validation(self, name: str):
        """Property: valid RFC 1123 names should pass validation."""
        assert validate_tenant_name(name) is True

    @given(name=invalid_tenant_names())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_invalid_names_fail_validation(self, name: str):
        """Property: invalid names should fail validation."""
        assert validate_tenant_name(name) is False

    def test_empty_string_invalid(self):
        """Edge case: empty string is invalid."""
        assert validate_tenant_name("") is False

    def test_single_lowercase_letter_valid(self):
        """Edge case: single lowercase letter is valid."""
        assert validate_tenant_name("a") is True

    def test_single_digit_valid(self):
        """Edge case: single digit is valid."""
        assert validate_tenant_name("5") is True

    def test_max_length_63_valid(self):
        """Edge case: exactly 63 characters should be valid."""
        name = "a" + "b" * 61 + "c"
        assert len(name) == 63
        assert validate_tenant_name(name) is True

    def test_length_64_invalid(self):
        """Edge case: 64 characters should be invalid."""
        name = "a" * 64
        assert len(name) == 64
        assert validate_tenant_name(name) is False


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: SHA-256 File Hashing
# ─────────────────────────────────────────────────────────────────────────────

class TestFileSha256:
    """Property-based tests for SHA-256 file hashing."""

    @given(content=st.binary(min_size=1, max_size=10000))
    @settings(max_examples=50)
    def test_same_content_same_hash(self, content: bytes):
        """Property: identical file content should produce identical hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path1 = Path(tmpdir) / "file1.txt"
            path2 = Path(tmpdir) / "file2.txt"
            path1.write_bytes(content)
            path2.write_bytes(content)

            hash1 = _file_sha256(path1)
            hash2 = _file_sha256(path2)

            assert hash1 == hash2

    @given(
        content1=st.binary(min_size=1, max_size=1000),
        content2=st.binary(min_size=1, max_size=1000),
    )
    @settings(max_examples=50)
    def test_different_content_different_hash(self, content1: bytes, content2: bytes):
        """Property: different content should (with very high probability) produce different hashes."""
        assume(content1 != content2)

        with tempfile.TemporaryDirectory() as tmpdir:
            path1 = Path(tmpdir) / "file1.txt"
            path2 = Path(tmpdir) / "file2.txt"
            path1.write_bytes(content1)
            path2.write_bytes(content2)

            hash1 = _file_sha256(path1)
            hash2 = _file_sha256(path2)

            # Hashes should be different (probabilistically)
            assert hash1 != hash2

    @given(content=st.binary(min_size=0, max_size=10000))
    @settings(max_examples=50)
    def test_hash_is_64_char_hex(self, content: bytes):
        """Property: hash should always be 64-character hex string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "file.txt"
            path.write_bytes(content)

            hash_val = _file_sha256(path)

            assert len(hash_val) == 64
            assert all(c in '0123456789abcdef' for c in hash_val)

    @given(content=st.binary(min_size=1, max_size=1000))
    @settings(max_examples=20)
    def test_hash_matches_hashlib(self, content: bytes):
        """Property: our hash should match Python's hashlib.sha256."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "file.txt"
            path.write_bytes(content)

            our_hash = _file_sha256(path)
            expected_hash = hashlib.sha256(content).hexdigest()

            assert our_hash == expected_hash


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: Drift Detection Symmetry
# ─────────────────────────────────────────────────────────────────────────────

class TestDriftDetectionSymmetry:
    """Property-based tests for drift detection symmetry."""

    def test_same_files_no_drift(self):
        """Property: if both manifests have same files/hashes, no drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_a = Path(tmpdir) / "a"
            dir_b = Path(tmpdir) / "b"
            dir_a.mkdir()
            dir_b.mkdir()

            # Create identical files in both directories
            (dir_a / "test.yaml").write_text("content: value")
            (dir_b / "test.yaml").write_text("content: value")

            manifest_a = FileManifest(label="dir-a", path=str(dir_a))
            manifest_a.files["test.yaml"] = _file_sha256(dir_a / "test.yaml")

            manifest_b = FileManifest(label="dir-b", path=str(dir_b))
            manifest_b.files["test.yaml"] = _file_sha256(dir_b / "test.yaml")

            report = compare_manifests(manifest_a, manifest_b)

            assert len(report.items) == 0

    def test_removed_added_symmetry(self):
        """Property: if A has file X and B doesn't, it's 'removed' A→B and 'added' B→A."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_a = Path(tmpdir) / "a"
            dir_b = Path(tmpdir) / "b"
            dir_a.mkdir()
            dir_b.mkdir()

            # Only in A
            (dir_a / "only_in_a.yaml").write_text("content: a")

            manifest_a = FileManifest(label="dir-a", path=str(dir_a))
            manifest_a.files["only_in_a.yaml"] = _file_sha256(dir_a / "only_in_a.yaml")

            manifest_b = FileManifest(label="dir-b", path=str(dir_b))

            # A -> B: file is removed from A
            report_a_to_b = compare_manifests(manifest_a, manifest_b)
            assert len(report_a_to_b.items) == 1
            assert report_a_to_b.items[0].drift_type == "removed"

            # B -> A: file is added to B (equivalent to removed from A)
            report_b_to_a = compare_manifests(manifest_b, manifest_a)
            assert len(report_b_to_a.items) == 1
            assert report_b_to_a.items[0].drift_type == "added"

    def test_modified_file_detection(self):
        """Property: different content in same filename is 'modified'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_a = Path(tmpdir) / "a"
            dir_b = Path(tmpdir) / "b"
            dir_a.mkdir()
            dir_b.mkdir()

            (dir_a / "config.yaml").write_text("value: 1")
            (dir_b / "config.yaml").write_text("value: 2")

            manifest_a = FileManifest(label="dir-a", path=str(dir_a))
            manifest_a.files["config.yaml"] = _file_sha256(dir_a / "config.yaml")

            manifest_b = FileManifest(label="dir-b", path=str(dir_b))
            manifest_b.files["config.yaml"] = _file_sha256(dir_b / "config.yaml")

            report = compare_manifests(manifest_a, manifest_b)

            assert len(report.items) == 1
            assert report.items[0].drift_type == "modified"


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: YAML Round-trip
# ─────────────────────────────────────────────────────────────────────────────

class TestYamlRoundTrip:
    """Property-based tests for YAML serialization round-trip."""

    @given(data=yaml_dicts())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_dict_yaml_dict_roundtrip(self, data: Dict[str, Any]):
        """Property: dict -> YAML -> dict should equal original."""
        # Serialize to YAML
        yaml_str = yaml_lib.dump(data, default_flow_style=False)

        # Deserialize back
        result = yaml_lib.safe_load(yaml_str)

        # Should equal original
        assert result == data

    @given(data=yaml_dicts())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_yaml_output_is_string(self, data: Dict[str, Any]):
        """Property: YAML output should be a valid string."""
        yaml_str = yaml_lib.dump(data, default_flow_style=False)
        assert isinstance(yaml_str, str)
        assert len(yaml_str) > 0

    @given(data=yaml_dicts())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_safe_load_succeeds(self, data: Dict[str, Any]):
        """Property: yaml.safe_load should succeed on all generated YAML."""
        yaml_str = yaml_lib.dump(data, default_flow_style=False)
        result = yaml_lib.safe_load(yaml_str)
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: Kustomization Builder
# ─────────────────────────────────────────────────────────────────────────────

@st.composite
def crd_filenames(draw):
    """Generate valid CRD filenames ending in .yaml."""
    name = draw(st.text(
        alphabet='abcdefghijklmnopqrstuvwxyz0123456789-',
        min_size=1,
        max_size=20
    ))
    return name + '.yaml'


class TestKustomizationBuilder:
    """Property-based tests for kustomization.yaml building."""

    @given(
        filenames=st.lists(
            crd_filenames(),
            min_size=1,
            max_size=20,
            unique=True
        ),
        namespace=st.just('monitoring')
    )
    @settings(max_examples=50)
    def test_kustomization_has_correct_api_version(
        self,
        filenames: list,
        namespace: str
    ):
        """Property: output always has correct apiVersion."""
        kust = build_kustomization(filenames, namespace)
        assert kust.get("apiVersion") == "kustomize.config.k8s.io/v1beta1"

    @given(
        filenames=st.lists(
            crd_filenames(),
            min_size=1,
            max_size=20,
            unique=True
        ),
        namespace=st.just('monitoring')
    )
    @settings(max_examples=50)
    def test_kustomization_has_correct_kind(
        self,
        filenames: list,
        namespace: str
    ):
        """Property: output always has correct kind."""
        kust = build_kustomization(filenames, namespace)
        assert kust.get("kind") == "Kustomization"

    @given(
        filenames=st.lists(
            crd_filenames(),
            min_size=1,
            max_size=20,
            unique=True
        ),
        namespace=st.just('monitoring')
    )
    @settings(max_examples=50)
    def test_all_filenames_in_resources(
        self,
        filenames: list,
        namespace: str
    ):
        """Property: all input filenames appear in resources list."""
        kust = build_kustomization(filenames, namespace)
        resources = kust.get("resources", [])

        # All input filenames should be in resources
        for filename in filenames:
            assert filename in resources

    @given(
        filenames=st.lists(
            crd_filenames(),
            min_size=1,
            max_size=20,
            unique=True
        ),
        namespace=st.just('monitoring')
    )
    @settings(max_examples=50)
    def test_resources_are_sorted(
        self,
        filenames: list,
        namespace: str
    ):
        """Property: resources list is sorted."""
        kust = build_kustomization(filenames, namespace)
        resources = kust.get("resources", [])

        assert resources == sorted(resources)

    def test_kustomization_structure(self):
        """Test kustomization has expected structure."""
        filenames = ["a.yaml", "b.yaml"]
        namespace = "monitoring"

        kust = build_kustomization(filenames, namespace)

        assert "apiVersion" in kust
        assert "kind" in kust
        assert "resources" in kust
        assert "commonLabels" in kust
        assert "namespace" in kust
        assert kust["namespace"] == namespace
        assert "app.kubernetes.io/part-of" in kust["commonLabels"]
        assert "app.kubernetes.io/managed-by" in kust["commonLabels"]


# ─────────────────────────────────────────────────────────────────────────────
# Property-based Tests: deep_merge (ADR-018 semantics, v2.8.0 A-8)
# ─────────────────────────────────────────────────────────────────────────────
#
# ADR-018 override rules (see scripts/tools/dx/describe_tenant.py:34):
#   - Dict fields  → deep merge (child adds new keys, overrides same keys)
#   - Array fields → REPLACE (not concat)
#   - Scalar       → child overrides parent
#   - None / null  → delete parent's key ("opt-out")
#   - _metadata    → never inherited (skipped at every depth)
#
# Properties under test:
#   P1  Identity:   merge(a, {}) == a                      (empty override is no-op)
#   P2  Idempotent: merge(merge(a, b), b) == merge(a, b)   (re-applying same override changes nothing)
#   P3  Null-delete persistence: once deleted, cannot be resurrected by {} follow-up
#   P4  Determinism: same inputs → byte-identical canonical JSON
#   P5  No mutation: inputs are not modified (defensive copy via deepcopy)
#   P6  _metadata never propagates from override (at any depth)

def _canonical_json(d: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace — used for byte-identical parity."""
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# Scalar leaves — includes None because None has special delete semantics.
_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(alphabet='abcdef0123456789-_', min_size=0, max_size=12),
)

# Small arrays of scalars (no nesting: ADR-018 says arrays are replaced wholesale,
# so internal structure is irrelevant to merge semantics).
_array = st.lists(
    st.one_of(
        st.booleans(),
        st.integers(min_value=-100, max_value=100),
        st.text(alphabet='abcdef', min_size=0, max_size=5),
    ),
    max_size=4,
)

# Recursive nested dict strategy — mimics tenant config shape.
# Keys: short lowercase names (no "_metadata" so tests can inject it deliberately).
_dict_keys = st.text(alphabet='abcdefghij', min_size=1, max_size=5).filter(lambda k: k != "_metadata")


def _config_dicts(max_leaves: int = 10):
    return st.recursive(
        st.one_of(_scalar, _array),
        lambda children: st.dictionaries(
            keys=_dict_keys,
            values=children,
            min_size=0,
            max_size=4,
        ),
        max_leaves=max_leaves,
    ).filter(lambda v: isinstance(v, dict))  # top-level must be a dict


# Scalars for "well-formed" overrides: no None at nested dict-value position.
# Rationale: `deep_merge` has a known asymmetry where `{"new_key": {"inner": None}}`
# applied to a base lacking `new_key` copies the None verbatim (because recursion
# is gated on "both sides are dicts"), but re-applying the same override then
# recurses and applies delete-semantics, breaking idempotency. The narrow
# `no-None-inside-new-subtree` constraint is captured explicitly by
# `test_override_new_subtree_with_none_is_non_idempotent` below; the algebraic
# properties (idempotency, canonical-JSON determinism) use this cleaned strategy.
_scalar_no_none = st.one_of(
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(alphabet='abcdef0123456789-_', min_size=0, max_size=12),
)


def _well_formed_overrides(max_leaves: int = 10):
    """Override dicts where None only appears at top-level keys (unambiguous delete-semantics)."""
    # Nested values may be scalars (no None), arrays, or nested dicts (no None inside them).
    nested = st.recursive(
        st.one_of(_scalar_no_none, _array),
        lambda children: st.dictionaries(
            keys=_dict_keys,
            values=children,
            min_size=0,
            max_size=3,
        ),
        max_leaves=max_leaves,
    )
    # Top-level values may be None (delete semantics) OR any nested value.
    return st.dictionaries(
        keys=_dict_keys,
        values=st.one_of(st.none(), nested),
        min_size=0,
        max_size=4,
    )


class TestDeepMergeProperties:
    """Property-based tests for deep_merge under ADR-018 override semantics."""

    # ---- P1 Identity ----------------------------------------------------------

    @given(base=_config_dicts())
    @settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
    def test_empty_override_is_identity(self, base: Dict[str, Any]):
        """merge(a, {}) == a — empty override is a no-op."""
        result = deep_merge(base, {})
        assert result == base

    # ---- P2 Idempotency -------------------------------------------------------

    @given(base=_config_dicts(), override=_well_formed_overrides())
    @settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
    def test_idempotent_reapply(self, base: Dict[str, Any], override: Dict[str, Any]):
        """merge(merge(a, b), b) == merge(a, b) — re-applying a well-formed override is a fixed point.

        "Well-formed" = None only at top-level override keys; see
        `test_override_new_subtree_with_none_is_non_idempotent` for the documented
        asymmetry that this strategy sidesteps.
        """
        once = deep_merge(base, override)
        twice = deep_merge(once, override)
        assert twice == once

    def test_override_new_subtree_with_none_is_non_idempotent(self):
        """Behavior-lock: override introducing a NEW subtree containing None is NOT idempotent.

        Root cause (scripts/tools/dx/describe_tenant.py:34 `deep_merge`):
          - 1st merge: base lacks 'a', so `result["a"] = deepcopy({"b": None})` — None preserved verbatim.
          - 2nd merge: base now has dict at 'a', recursion fires, None-delete kicks in → {"a": {}}.

        This is a latent quirk versus the ADR-018 contract (None = delete at any depth).
        Fixing it needs coordinated Go/Python change + ADR-018 clarification; out of
        scope for A-8 (test-harness expansion). Test pins current behavior so any
        future fix must update this assertion deliberately.
        """
        base = {}
        override = {"a": {"b": None}}

        once = deep_merge(base, override)
        twice = deep_merge(once, override)

        assert once == {"a": {"b": None}}, "1st merge preserves None verbatim"
        assert twice == {"a": {}}, "2nd merge applies None-delete via recursion"
        assert twice != once, "asymmetry exists — see docstring for future-fix plan"

    # ---- P3 Null-delete persistence ------------------------------------------

    @given(
        base=_config_dicts().filter(lambda d: len(d) > 0),
        data=st.data(),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
    def test_null_delete_is_persistent(self, base: Dict[str, Any], data):
        """A key deleted via None cannot be resurrected by a subsequent {} or same-None override.

        This guards against a subtle bug: if deep_merge ever started treating None
        at dict-value-position as a no-op (leaving the old value), deletes would
        silently resurrect.
        """
        # Pick a real top-level key from base to nuke.
        key = data.draw(st.sampled_from(sorted(base.keys())))
        delete_override = {key: None}

        stage1 = deep_merge(base, delete_override)
        assert key not in stage1, f"key {key!r} should be gone after None override"

        # Re-applying {} or the same None override must not restore it.
        stage2 = deep_merge(stage1, {})
        assert key not in stage2

        stage3 = deep_merge(stage1, delete_override)
        assert key not in stage3

    # ---- P4 Canonical JSON determinism ---------------------------------------

    @given(base=_config_dicts(), override=_config_dicts())
    @settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
    def test_canonical_json_is_deterministic(self, base: Dict[str, Any], override: Dict[str, Any]):
        """Calling deep_merge twice with the same inputs yields byte-identical canonical JSON.

        This is the Python-side guarantee that the Go/Python golden parity rests on:
        deep_merge is a pure function of its inputs, no hidden state, no dict-ordering
        surprises after canonical serialization.
        """
        a = deep_merge(base, override)
        b = deep_merge(copy.deepcopy(base), copy.deepcopy(override))
        assert _canonical_json(a) == _canonical_json(b)

    # ---- P5 Input immutability -----------------------------------------------

    @given(base=_config_dicts(), override=_config_dicts())
    @settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
    def test_inputs_are_not_mutated(self, base: Dict[str, Any], override: Dict[str, Any]):
        """deep_merge does not mutate base or override (defensive deepcopy contract)."""
        base_snapshot = _canonical_json(base)
        override_snapshot = _canonical_json(override)
        _ = deep_merge(base, override)
        assert _canonical_json(base) == base_snapshot
        assert _canonical_json(override) == override_snapshot

    # ---- P6 _metadata isolation ----------------------------------------------

    @given(
        base=_config_dicts(),
        override_meta=st.dictionaries(
            keys=st.text(alphabet='abcdef', min_size=1, max_size=4),
            values=st.text(alphabet='xyz', min_size=0, max_size=6),
            max_size=3,
        ),
    )
    @settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
    def test_metadata_in_override_never_propagates(
        self, base: Dict[str, Any], override_meta: Dict[str, Any]
    ):
        """_metadata at override top-level is skipped — base's _metadata (if any) survives unchanged."""
        override = {"_metadata": override_meta}
        result = deep_merge(base, override)

        # Result's _metadata == base's _metadata (original or absent).
        assert result.get("_metadata") == base.get("_metadata")

    def test_metadata_skipped_at_nested_depth(self):
        """Concrete fixture: _metadata at a nested path is also skipped (recursion preserves the rule)."""
        base = {"tenant_a": {"cpu": 80, "_metadata": {"owner": "team-base"}}}
        override = {"tenant_a": {"cpu": 90, "_metadata": {"owner": "team-override"}}}
        result = deep_merge(base, override)
        # Nested _metadata at override position is skipped; base's is kept.
        assert result["tenant_a"]["cpu"] == 90
        assert result["tenant_a"]["_metadata"] == {"owner": "team-base"}

    # ---- Concrete fixture: override semantics spot-check ----------------------

    def test_scalar_override_wins(self):
        assert deep_merge({"k": 1}, {"k": 2}) == {"k": 2}

    def test_array_is_replaced_not_concatenated(self):
        """ADR-018: arrays REPLACE, never concat (differs from common "merge" intuition)."""
        result = deep_merge({"tags": ["a", "b"]}, {"tags": ["c"]})
        assert result == {"tags": ["c"]}

    def test_nested_dict_recurses(self):
        base = {"thresholds": {"cpu": 80, "mem": 90}}
        override = {"thresholds": {"cpu": 70}}  # mem untouched, cpu wins
        assert deep_merge(base, override) == {"thresholds": {"cpu": 70, "mem": 90}}

    def test_null_at_nested_depth_deletes(self):
        base = {"thresholds": {"cpu": 80, "mem": 90}}
        override = {"thresholds": {"mem": None}}
        assert deep_merge(base, override) == {"thresholds": {"cpu": 80}}
