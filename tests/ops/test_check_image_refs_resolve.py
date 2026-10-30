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

import pytest

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
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
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
        # A k8s/ file is a manifest: kubectl reads only objects with a kind (#3426).
        "kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n"
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


# ── #2966: --timeout must be a whole number of seconds above 0 ────────────────


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "nan", "inf", "abc",
                                 "2147484", "9223372036", "9223372037"])
def test_timeout_that_judges_nothing_is_refused(tmp_path: Path, bad: str) -> None:
    """`--timeout 0` (or negative) reported every ref as "timeout after 0s" — a
    false verdict about registry state. Refused at parse time (rc 2), so even a
    `--list` run (no network) says so before doing anything."""
    proc = _run("--root", str(tmp_path), "--list", "--timeout", bad)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "--timeout" in proc.stderr


@pytest.mark.parametrize("good", ["1", "5", "2147483"])
def test_a_positive_timeout_is_accepted(tmp_path: Path, good: str) -> None:
    """Must-fire control for the refusal above, up to the ceiling itself."""
    proc = _run("--root", str(tmp_path), "--list", "--timeout", good)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the poll() ceiling is POSIX")
def test_the_ceiling_is_what_subprocess_can_actually_wait() -> None:
    """MAX_TIMEOUT_S is the poll() millisecond C-int ceiling: subprocess.run with
    capture_output accepts it and raises OverflowError one second past it (the
    rc 1 traceback threading.TIMEOUT_MAX used to let through)."""
    assert _cir.MAX_TIMEOUT_S == 2147483
    subprocess.run([sys.executable, "-c", ""], capture_output=True,
                   timeout=_cir.MAX_TIMEOUT_S, check=True)
    with pytest.raises(OverflowError):
        subprocess.run([sys.executable, "-c", ""], capture_output=True,
                       timeout=_cir.MAX_TIMEOUT_S + 1)


def test_timeout_reaches_the_resolver_as_an_int(tmp_path: Path, monkeypatch) -> None:
    """The verdict text says "timeout after {N}s"; it stays an int, as before."""
    _write(tmp_path / "helm/x/values.yaml", "image:\n  repository: quay.io/o/p\n  tag: v1\n")
    seen: list = []
    monkeypatch.setattr(_cir, "_resolver", lambda: (lambda ref: ["true"], "stub"))
    monkeypatch.setattr(_cir, "_resolve_once",
                        lambda cmd, timeout: (seen.append(timeout), (True, ""))[1])
    monkeypatch.setattr(sys, "argv", ["check_image_refs_resolve.py",
                                      "--root", str(tmp_path), "--timeout", "7"])
    assert _cir.main() == 0
    assert seen == [7] and type(seen[0]) is int


# ── #3206 (ADR-036 §12): the sources are read as kubectl / Helm read them ──
def test_a_file_kubectls_reading_refuses_stops_the_run(tmp_path: Path) -> None:
    """It used to be a `::warning::` and a skip — a clean-looking run that
    checked less. Now exit 2, naming the file."""
    _write(tmp_path / "helm/a/values.yaml", 'image:\n  repository: foo/bar\n  tag: "1.0"\n')
    _write(tmp_path / "k8s/bad.yaml", "--- kind: x\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "bad.yaml" in proc.stderr and "the way kubectl does" in proc.stderr


def test_a_tag_is_kubectls_reading_of_it(tmp_path: Path) -> None:
    """A quoted `tag: "1.10"` is the text."""
    _write(tmp_path / "helm/b/values.yaml", 'image:\n  repository: foo/baz\n  tag: "1.10"\n')
    assert _list_refs(tmp_path) == {"foo/baz:1.10"}


@pytest.mark.parametrize("values, key_path", [
    ("image:\n  repository: foo/bar\n  tag: 1.10\n", "image.tag"),     # the number 1.1
    ("app:\n  image:\n    repository: foo/bar\n    tag: 1e3\n", "app.image.tag"),  # 1000
    ("c:\n  - image: 7\n", "c[0].image"),
])
def test_a_non_text_image_field_stops_the_run_naming_it(tmp_path: Path, values, key_path) -> None:
    """#3407 review: an unquoted `tag: 1.10` / `1e3` is a number to Helm, so
    no ref can be built from it. It used to be skipped silently — a ref never
    checked; now rc 2 naming the file and the key path."""
    _write(tmp_path / "helm/a/values.yaml", values)
    _write(tmp_path / "helm/b/values.yaml", 'image:\n  repository: foo/baz\n  tag: "1.10"\n')
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "helm/a/values.yaml" in proc.stderr and key_path in proc.stderr, proc.stderr


def test_no_da_crdecode_is_exit_2_not_a_pyyaml_reading(tmp_path: Path) -> None:
    import os
    _write(tmp_path / "helm/a/values.yaml", 'image:\n  repository: foo/bar\n  tag: "1.0"\n')
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        env={**os.environ, "DA_CRDECODE_BINARY": str(tmp_path / "no-such-da-crdecode")})
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
