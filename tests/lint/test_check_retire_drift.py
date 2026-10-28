#!/usr/bin/env python3
"""check_retire_drift — the conf.d side of the RETIRE-ordering gate (#869).

#1603 (extension-SPELLING axis): the gate enumerated `rglob("*.yaml")`, so
a tenant whose carrier is `alpha.yml` or `Alpha.YAML` was not in `declared`
at all. Measured before the fix with byte-identical bodies: `alpha.yaml` →
{alpha: postgres}; `alpha.yml` / `Alpha.YAML` → {} — the same answer as a
tree with no tenant. The consequence is the gate's own failure mode: the
exporter (which reads both spellings) keeps emitting
`tenant_expected_exporter{tenant="alpha"}=1` after the K8s target is
removed, TenantExporterAbsent fires a false-positive critical, and this
gate said rc=0.

#2360 (hidden axis): `.`-prefixed files and everything under a `.`-prefixed
directory are skipped, as the exporter's walker skips them. Before the fix
`.ghost.yaml` and `.snap/s.yaml` were declared tenants here. The cross-tool
cell is `tests/shared/test_confd_hidden_axis_across_tools.py`; the tests
below add the operator-facing consequence and the dot-named-root boundary.

#2858 (db_type source): the db_type is the exporter's own reading, from
`da-guard served-values`. Before, the lint read each tenant file's own
`_metadata` and skipped every `_` file, so a db_type the exporter takes from
a profile or a root platform file was not declared (the exporter emits
tenant_expected_exporter for it all the same) and a non-string db_type read
as none. `test_declared_db_type_matches_served_values` compares the two on
each way of writing it. da-guard is built by the conftest session fixture;
no go → fail, not skip.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "tools", "lint"))

import _lib_tenant_values as tv  # noqa: E402
import check_retire_drift as crd  # noqa: E402


@pytest.fixture(autouse=True)
def _da_guard(da_guard_env):
    return da_guard_env

_BODY = ("tenants:\n  alpha:\n    pg_connections: 90\n"
         "    _metadata:\n      db_type: postgres\n")


def _tree(root: Path, carrier: str | None) -> Path:
    """One conf.d: reserved defaults + an examples/ template + optional tenant.

    The reserved file and the examples/ subtree each carry a `tenants:` block
    on purpose — that is the only shape in which "still skipped" is
    OBSERVABLE (a carrier without one contributes no tenant either way).
    """
    root.mkdir(parents=True)
    (root / "_defaults.yaml").write_text(
        _BODY.replace("alpha", "ghost_reserved"), encoding="utf-8")
    (root / "examples").mkdir()
    (root / "examples" / "ex.yaml").write_text(
        _BODY.replace("alpha", "ghost_example"), encoding="utf-8")
    if carrier:
        (root / carrier).write_text(_BODY, encoding="utf-8")
    return root


@pytest.mark.parametrize("carrier", ["alpha.yml", "Alpha.YAML"])
def test_declared_tenants_see_yml_and_upper_case_the_same_as_yaml(
        tmp_path: Path, carrier: str) -> None:
    """A `.yml` / `.YAML` tenant must be declared exactly like a `.yaml` one.

    The `notenants` arm is the sensitivity control: it must differ from the
    `yaml` arm, otherwise "same answer" is vacuous.
    """
    control = crd.conf_d_declared_db_type_tenants(_tree(tmp_path / "none", None))
    ref = crd.conf_d_declared_db_type_tenants(_tree(tmp_path / "yaml", "alpha.yaml"))
    assert ref == {"alpha": "postgres"}, ref
    assert control != ref, "fixture is vacuous — removing the tenant changed nothing"
    assert crd.conf_d_declared_db_type_tenants(
        _tree(tmp_path / "other", carrier)) == ref


def test_reserved_and_examples_skips_survive_the_wider_spelling(
        tmp_path: Path) -> None:
    """Widening the extension must not widen WHAT is skipped.

    `_defaults.yml` and `examples/ex.yml` carry tenants; neither may be
    declared, the same way their `.yaml` twins are not.
    """
    root = _tree(tmp_path / "yml", "alpha.yml")
    (root / "_defaults.yml").write_text(
        _BODY.replace("alpha", "ghost_reserved_yml"), encoding="utf-8")
    (root / "examples" / "ex.yml").write_text(
        _BODY.replace("alpha", "ghost_example_yml"), encoding="utf-8")
    assert crd.conf_d_declared_db_type_tenants(root) == {"alpha": "postgres"}


def test_yml_tenant_without_a_k8s_target_is_a_violation(tmp_path: Path) -> None:
    """The operator-facing consequence: a `.yml` tenant stranded without a
    K8s target must now be reported, not pass with rc=0.
    """
    root = _tree(tmp_path / "conf.d", "alpha.yml")
    helm = tmp_path / "helm"
    helm.mkdir()
    result = crd.evaluate(root, helm, tmp_path / "no-namespaces.yaml")
    assert result["declared"] == {"alpha": "postgres"}
    assert len(result["violations"]) == 1, result["violations"]
    assert "'alpha'" in result["violations"][0]


def test_hidden_carriers_declare_no_tenant(tmp_path: Path) -> None:
    """#2360: a hidden file and a file under a hidden directory carry a
    tenant the exporter never reads, so neither may be declared — else the
    gate demands a K8s target for a tenant that emits no series."""
    root = _tree(tmp_path / "conf.d", "alpha.yaml")
    (root / ".ghost.yaml").write_text(
        _BODY.replace("alpha", "ghost_hidden_file"), encoding="utf-8")
    (root / ".snap").mkdir()
    (root / ".snap" / "s.yaml").write_text(
        _BODY.replace("alpha", "ghost_hidden_dir"), encoding="utf-8")
    assert crd.conf_d_declared_db_type_tenants(root) == {"alpha": "postgres"}
    helm = tmp_path / "helm"
    helm.mkdir()
    result = crd.evaluate(root, helm, tmp_path / "no-namespaces.yaml")
    assert not any("ghost" in v for v in result["violations"]), result


def test_a_dot_named_conf_d_root_is_still_read(tmp_path: Path) -> None:
    """Only names BELOW the conf.d root are judged: the exporter reads the
    directory it is handed whatever it is called."""
    root = _tree(tmp_path / ".confd", "alpha.yaml")
    assert crd.conf_d_declared_db_type_tenants(root) == {"alpha": "postgres"}


# ── #2858: db_type is what /metrics serves, on every way of writing it ──

_DEFAULTS = "defaults:\n  pg_connections: 80\n"
_PG_PROFILE = "profiles:\n  pg:\n    _metadata:\n      db_type: postgres\n"
_ON_PROFILE = "tenants:\n  alpha:\n    _profile: pg\n"

# shape -> (files, the db_type alpha must be declared with; None = not declared)
SHAPES = {
    "tenant-file": ({"_defaults.yaml": _DEFAULTS, "alpha.yaml": _BODY}, "postgres"),
    "profile": ({"_defaults.yaml": _DEFAULTS, "_profiles.yaml": _PG_PROFILE,
                 "alpha.yaml": _ON_PROFILE}, "postgres"),
    "profile-yml": ({"_defaults.yaml": _DEFAULTS, "_profiles.yml": _PG_PROFILE,
                     "alpha.yaml": _ON_PROFILE}, "postgres"),
    "platform-file": ({"_defaults.yaml": _DEFAULTS,
                       "_platform.yaml": "tenants:\n  alpha:\n    _metadata:\n"
                                         "      db_type: postgres\n",
                       "alpha.yaml": "tenants:\n  alpha:\n    pg_connections: 90\n"},
                      "postgres"),
    "non-string": ({"_defaults.yaml": _DEFAULTS,
                    "alpha.yaml": "tenants:\n  alpha:\n    _metadata:\n      db_type: 5\n"},
                   "5"),
    "tenant-metadata-over-profile": (
        {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": _PG_PROFILE,
         "alpha.yaml": "tenants:\n  alpha:\n    _profile: pg\n"
                       "    _metadata:\n      db_type: mysql\n"}, "mysql"),
    # The profile fills `_metadata` only when the tenant's layer writes none:
    # a tenant `_metadata` without db_type leaves it empty.
    "tenant-metadata-without-db-type": (
        {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": _PG_PROFILE,
         "alpha.yaml": "tenants:\n  alpha:\n    _profile: pg\n"
                       "    _metadata:\n      owner: team-a\n"}, None),
    "optional-overrides": (
        {"_defaults.yaml": _DEFAULTS + "optional_overrides:\n  - _metadata\n",
         "_profiles.yaml": _PG_PROFILE, "alpha.yaml": _ON_PROFILE}, None),
    "subdir-profiles": ({"_defaults.yaml": _DEFAULTS, "team/_profiles.yaml": _PG_PROFILE,
                         "team/alpha.yaml": _ON_PROFILE}, None),
    "examples-profiles": ({"_defaults.yaml": _DEFAULTS,
                           "examples/_profiles.yaml": _PG_PROFILE,
                           "alpha.yaml": _ON_PROFILE}, None),
}


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return root


def _served_db_type(root: Path, tenant: str) -> str:
    meta = tv.load_served_tree(root).tenants[tenant].values.get("_metadata")
    return meta.get("db_type", "") if isinstance(meta, dict) else ""


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_declared_db_type_matches_served_values(tmp_path: Path, shape: str) -> None:
    files, want = SHAPES[shape]
    root = _write(tmp_path / "conf.d", files)
    assert _served_db_type(root, "alpha") == (want or ""), "fixture premise"
    declared = crd.conf_d_declared_db_type_tenants(root)
    assert declared == ({"alpha": want} if want else {}), declared


def test_profile_tenant_without_a_k8s_target_is_a_violation(tmp_path: Path) -> None:
    """The operator-facing consequence: a tenant whose db_type comes from a
    profile emits tenant_expected_exporter, so removing its K8s target must
    be reported (before #2858 it passed with rc=0)."""
    files, _ = SHAPES["profile"]
    root = _write(tmp_path / "conf.d", files)
    helm = tmp_path / "helm"
    helm.mkdir()
    result = crd.evaluate(root, helm, tmp_path / "no-namespaces.yaml")
    assert len(result["violations"]) == 1 and "'alpha'" in result["violations"][0], result


# ── #3206 (ADR-036 §12): the namespaces manifest, read as kubectl reads it ──

def _ns(name: str, instance: str) -> str:
    return (f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {name}\n"
            f"  labels:\n    instance: {instance}\n")


def test_every_namespace_document_is_read(tmp_path: Path) -> None:
    """da-crdecode returns every document `kubectl apply -f` would apply:
    several Namespaces, separators with nothing between them, a comment-only
    document, and a document that is not a Namespace."""
    f = tmp_path / "ns.yaml"
    f.write_text("---\n" + _ns("db-a", "db-a") + "---\n---\n# nothing here\n---\n"
                 + "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, labels: {instance: other}}\n"
                 + "---\n" + _ns("db-b", '"010"'), encoding="utf-8")
    assert crd.namespace_declared_targets(f) == {"db-a", "010"}


@pytest.mark.parametrize("value,read", [("010", "8"), ("yes", "true"), ("1e3", "1000")])
def test_a_label_kubectl_refuses_is_an_error_naming_the_document(
        tmp_path: Path, value: str, read: str) -> None:
    """`instance: 010` is the number 8 to kubectl, which refuses a label that
    is not a string: the Namespace is never created. Reported with the file
    and the document's position; neither the text nor the number is taken."""
    f = tmp_path / "ns.yaml"
    f.write_text(_ns("db-a", "db-a") + "---\n" + _ns("db-b", value), encoding="utf-8")
    with pytest.raises(crd.NamespacesFileError) as exc_info:
        crd.namespace_declared_targets(f)
    msg = str(exc_info.value)
    assert f"ns.yaml: document 2 (Namespace 'db-b'): label `instance` is {read} " in msg, msg


def test_main_exits_2_on_a_label_kubectl_refuses(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _tree(tmp_path / "conf.d", "alpha.yaml")
    helm = tmp_path / "helm"
    helm.mkdir()
    (helm / "values-alpha.yaml").write_text("{}\n", encoding="utf-8")
    ns = tmp_path / "ns.yaml"
    ns.write_text(_ns("db-x", "010"), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_retire_drift.py", "--config-dir", str(root),
                                      "--helm-dir", str(helm), "--namespaces-file", str(ns)])
    assert crd.main() == 2
    err = capsys.readouterr().err
    assert "document 1 (Namespace 'db-x'): label `instance` is 8" in err, err


def test_a_manifest_kubectl_cannot_read_is_an_error(tmp_path: Path) -> None:
    """A `---` line carrying content is refused by kubectl's splitter; the
    reason is da-crdecode's, passed on, not a silent empty set."""
    f = tmp_path / "ns.yaml"
    f.write_text(_ns("db-a", "db-a") + "--- kind: Namespace\n", encoding="utf-8")
    with pytest.raises(crd.NamespacesFileError) as exc_info:
        crd.namespace_declared_targets(f)
    assert any("invalid YAML document separator" in ln for ln in exc_info.value.detail_lines), (
        exc_info.value.detail_lines)


def test_no_da_crdecode_is_an_error_not_an_empty_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DA_CRDECODE_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    f = tmp_path / "ns.yaml"
    f.write_text(_ns("db-a", "db-a"), encoding="utf-8")
    with pytest.raises(crd.NamespacesFileError, match="da-crdecode was not found"):
        crd.namespace_declared_targets(f)
