"""Tests for scripts/ops/check_image_refs_resolve.py (#902 L1-B).

Exercises the EXTRACTION (parse) logic via `--list` — no network / skopeo, so it
runs in the plain Python Tests CI job. The resolution (skopeo) path is enforced
by the image-ref-resolve workflow, not here.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "check_image_refs_resolve.py"

# Load the script as a module to unit-test pure helpers directly (no subprocess).
_spec = importlib.util.spec_from_file_location("_check_image_refs_resolve", SCRIPT)
_cir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cir)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _list_refs(root: Path) -> set[str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--list"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def test_extracts_all_shapes_and_skips_empty_and_templated(tmp_path: Path) -> None:
    # Shape A: repository + tag.
    _write(tmp_path / "helm/a/values.yaml", 'image:\n  repository: foo/bar\n  tag: "1.0"\n')
    # Shape A nested in a sub-block (oauth2-proxy pattern) + empty tag (first-party) skipped.
    _write(
        tmp_path / "helm/b/values.yaml",
        'image:\n  repository: ghcr.io/x/y\n  tag: ""\n'           # empty → skip (appVersion)
        'oauth2Proxy:\n  image:\n    repository: quay.io/o/p\n    tag: v1\n',
    )
    # Shape B: single-string `image:` (mariadb-instance pattern).
    _write(
        tmp_path / "helm/c/values.yaml",
        'mariadb:\n  image: "mariadb:11.8.8"\nexporter:\n  image: "prom/e:v2"\n',
    )
    # Templated tag → skip (not a real ref).
    _write(tmp_path / "helm/d/values.yaml", 'image:\n  repository: t\n  tag: "{{ .Chart.AppVersion }}"\n')
    # Raw k8s manifest container image (nested under spec.template...containers[]).
    _write(
        tmp_path / "k8s/mon/deploy.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n"
        '      containers:\n        - name: c\n          image: "registry.k8s.io/foo:v3"\n',
    )
    # registry + repository + tag (explicit registry key).
    _write(tmp_path / "helm/e/values.yaml", 'image:\n  registry: example.com\n  repository: r\n  tag: t9\n')

    refs = _list_refs(tmp_path)
    assert refs == {
        "foo/bar:1.0",
        "quay.io/o/p:v1",
        "mariadb:11.8.8",
        "prom/e:v2",
        "registry.k8s.io/foo:v3",
        "example.com/r:t9",
    }, f"unexpected ref set: {sorted(refs)}"


def test_digest_is_preserved(tmp_path: Path) -> None:
    _write(
        tmp_path / "helm/a/values.yaml",
        'image:\n  repository: foo/bar\n  tag: "1.0"\n  digest: "sha256:abc"\n',
    )
    assert _list_refs(tmp_path) == {"foo/bar:1.0@sha256:abc"}


def test_empty_tree_yields_nothing(tmp_path: Path) -> None:
    _write(tmp_path / "helm/a/values.yaml", "replicaCount: 2\nservice:\n  port: 80\n")
    assert _list_refs(tmp_path) == set()


def test_local_built_image_is_skipped(tmp_path: Path) -> None:
    # federation-audit-sidecar is built locally (no registry, never published) → it
    # must NOT be resolve-checked, or the lint false-fails. A real third-party ref
    # alongside it is still collected.
    _write(
        tmp_path / "helm/fg/values.yaml",
        "auditLog:\n  image:\n    repository: federation-audit-sidecar\n    tag: \"3.0.8\"\n"
        "image:\n  repository: envoyproxy/envoy\n  tag: distroless-v1.0\n",
    )
    assert _list_refs(tmp_path) == {"envoyproxy/envoy:distroless-v1.0"}


def test_first_party_namespace_is_skipped(tmp_path: Path) -> None:
    # ghcr.io/vencil/* (first-party) is the release pipeline's job + needs ghcr auth
    # to resolve → skipped by L1-B; a public third-party ref alongside is still kept.
    _write(
        tmp_path / "k8s/x/deploy.yaml",
        "spec:\n  template:\n    spec:\n      containers:\n"
        '        - image: "ghcr.io/vencil/tenant-api:v2.7.0"\n'
        '        - image: "quay.io/o/p:v1"\n',
    )
    assert _list_refs(tmp_path) == {"quay.io/o/p:v1"}


def test_resolvable_drops_tag_when_digest_present() -> None:
    # skopeo/docker reject a ref carrying BOTH a :tag and an @digest ("Error
    # parsing reference"); resolve `repo@digest` (the digest is authoritative).
    # #902 L2 pins as `repo:tag@digest`, so the resolver MUST normalize it.
    assert _cir._resolvable("quay.io/o/p:v1@sha256:abc") == "quay.io/o/p@sha256:abc"
    assert (
        _cir._resolvable("envoyproxy/envoy:distroless-v1.0@sha256:def")
        == "envoyproxy/envoy@sha256:def"
    )
    # tag-only and digest-only refs pass through unchanged.
    assert _cir._resolvable("foo/bar:1.0") == "foo/bar:1.0"
    assert _cir._resolvable("foo/bar@sha256:xyz") == "foo/bar@sha256:xyz"
