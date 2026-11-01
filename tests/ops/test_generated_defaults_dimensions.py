"""The onboarding generators write the `dimensions:` the chart ships (#3475).

`da-tools init` and `scaffold_tenant` write the customer-side root
`_defaults.yaml`. On the GitOps topology (tenant-api reading the customer
repo) and on an `existingConfigMap` / kustomize deployment that file is the
only `dimensions:` the exporter and the write gate see, so without it a
`container_cpu{version="v2"}` the Helm chart serves is unread there.
Asserted on the shared key verdict over the generated file, not on its text.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import _lib_key_verdict as kv
import _registry_lib as lib
import init_project as ip
import scaffold_tenant as st

pytestmark = pytest.mark.usefixtures("da_guard_env")

_TENANT = 'tenants:\n  t-a:\n    container_cpu{version="v2"}: "60"\n    container_memory{version="v2"}: "70"\n'


def _unread(tmp_path: Path, defaults_text: str) -> list[str]:
    (tmp_path / "_defaults.yaml").write_text(defaults_text, encoding="utf-8")
    (tmp_path / "t-a.yaml").write_text(_TENANT, encoding="utf-8")
    verdict = kv.judge_tree(str(tmp_path))
    return sorted(f.key for f in verdict.findings if f.severity == kv.ERROR)


def test_init_project_defaults_serve_the_shipped_dimensions(tmp_path):
    assert _unread(tmp_path, ip._gen_defaults_yaml(["kubernetes", "mariadb"], "monitoring")) == []


def test_scaffold_defaults_serve_the_shipped_dimensions(tmp_path):
    text = yaml.safe_dump(st.generate_defaults(["mariadb"]), sort_keys=False)
    assert _unread(tmp_path, text) == []


def test_without_the_declaration_the_same_keys_are_unread(tmp_path):
    """Control: the generated file is what makes them served."""
    doc = st.generate_defaults(["mariadb"])
    doc.pop("dimensions")
    assert _unread(tmp_path, yaml.safe_dump(doc, sort_keys=False)) == [
        'container_cpu{version="v2"}', 'container_memory{version="v2"}']


def test_a_full_selection_is_the_registrys_declaration():
    """Same SSOT as the chart: every pack selected gives the registry's list."""
    doc = lib.build_registry_doc()
    packs = list(st.RULE_PACKS)
    assert lib.shipped_dimensions_for_packs(packs) == dict(lib.dimension_entries(doc))
    assert lib.shipped_dimensions_for_packs(["mariadb"]) == {}
