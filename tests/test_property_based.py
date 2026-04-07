#!/usr/bin/env python3
"""Property-based tests using Hypothesis for core config parsing functionality.

Tests core areas:
  1. Tenant name validation (RFC 1123 compliant)
  2. SHA-256 file hashing (consistency and format)
  3. Drift detection symmetry (manifest comparison)
  4. YAML round-trip parsing (dict -> yaml -> dict)
  5. Kustomization builder (apiVersion/kind + resources)
"""
from __future__ import annotations

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
_OPS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _OPS_DIR)
sys.path.insert(0, os.path.join(_OPS_DIR, '..'))

# Import functions to test
from migrate_to_operator import validate_tenant_name  # noqa: E402
from drift_detect import _file_sha256, compare_manifests, FileManifest  # noqa: E402
from operator_generate import build_kustomization  # noqa: E402


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
