"""Tests for scripts/ops/check_image_refs_resolve.py (#902 L1-B).

Exercises the EXTRACTION (parse) logic via `--list` — no network, so it runs
in the plain Python Tests CI job — and the verdict over `_lib_registry.resolve`
answers (the registry conversation itself is tests/shared/test_lib_registry.py;
the live run is the image-ref-resolve workflow).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "check_image_refs_resolve.py"
sys.path.insert(0, str(SCRIPT.parents[1] / "tools"))
import _lib_registry  # noqa: E402  (the module main() imports: same object)

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


def test_timeout_reaches_the_registry_as_an_int(tmp_path: Path, monkeypatch) -> None:
    """--timeout is the per-request timeout handed to the registry client."""
    _write(tmp_path / "helm/x/values.yaml", "image:\n  repository: quay.io/o/p\n  tag: v1\n")
    seen: list = []
    monkeypatch.setattr(_lib_registry, "resolve",
                        lambda ref, timeout: (seen.append(timeout),
                                              _lib_registry.Answer(_lib_registry.RESOLVED, ""))[1])
    monkeypatch.setattr(sys, "argv", ["check_image_refs_resolve.py",
                                      "--root", str(tmp_path), "--timeout", "7"])
    assert _cir.main() == 0
    assert seen == [7] and type(seen[0]) is int


@pytest.mark.parametrize("outcomes, rc", [
    ({"quay.io/o/a:v1": "not-resolved"}, 1),
    ({"quay.io/o/a:v1": "no-answer"}, 2),
    ({"quay.io/o/a:v1": "not-resolved", "quay.io/o/b:v1": "no-answer"}, 2),
], ids=["finding", "no-answer", "both-no-answer-wins"])
def test_a_registry_that_gave_no_answer_is_not_a_finding(
        tmp_path: Path, monkeypatch, capsys, outcomes, rc) -> None:
    """No answer says nothing about the ref: 2, not the 1 of a ref that does
    not resolve, and ahead of it when both happen in one run (#3577, #3611)."""
    _write(tmp_path / "helm/a/values.yaml", "image:\n  repository: quay.io/o/a\n  tag: v1\n")
    if len(outcomes) > 1:
        _write(tmp_path / "helm/b/values.yaml", "image:\n  repository: quay.io/o/b\n  tag: v1\n")
    monkeypatch.setattr(_lib_registry, "resolve",
                        lambda ref, timeout: _lib_registry.Answer(outcomes[ref], "synthetic"))
    monkeypatch.setattr(sys, "argv", ["check_image_refs_resolve.py", "--root", str(tmp_path)])
    assert _cir.main() == rc
    if rc == 2:
        assert "NOT checked" in capsys.readouterr().err


def test_no_answer_then_an_answer_is_the_answer(tmp_path: Path, monkeypatch) -> None:
    """The one retry: a registry that gave no answer and then RESOLVED is a
    resolved ref (0), and it was asked twice (review on #3626)."""
    _write(tmp_path / "helm/a/values.yaml", "image:\n  repository: quay.io/o/a\n  tag: v1\n")
    replies = iter([_lib_registry.Answer(_lib_registry.NO_ANSWER, "HTTP 429"),
                    _lib_registry.Answer(_lib_registry.RESOLVED, "")])
    calls: list = []
    monkeypatch.setattr(_lib_registry, "resolve",
                        lambda ref, timeout: (calls.append(ref), next(replies))[1])
    monkeypatch.setattr(sys, "argv", ["check_image_refs_resolve.py", "--root", str(tmp_path)])
    assert _cir.main() == 0
    assert len(calls) == 2


@pytest.mark.parametrize("ci", [None, "true", "false"], ids=["no-CI", "CI=true", "CI=false"])
def test_the_verdict_does_not_depend_on_the_ci_variable(
        tmp_path: Path, monkeypatch, ci) -> None:
    """#3611: the `CI` environment switch is gone. It made "no resolver" rc 0
    on a dev box and rc 2 in CI (and `CI=false` counted as set); with no binary
    to be missing, an unanswered registry is 2 everywhere."""
    _write(tmp_path / "helm/a/values.yaml", "image:\n  repository: quay.io/o/a\n  tag: v1\n")
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(_lib_registry, "resolve",
                        lambda ref, timeout: _lib_registry.Answer(_lib_registry.NO_ANSWER, "x"))
    monkeypatch.setattr(sys, "argv", ["check_image_refs_resolve.py", "--root", str(tmp_path)])
    assert _cir.main() == 2


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


# ── scope `rendered` (#1040): what a chart's defaults run ────────────────────
_REPO = SCRIPT.parents[2]
_needs_helm = pytest.mark.skipif(__import__("shutil").which("helm") is None,
                                 reason="helm CLI not on PATH")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)


@_needs_helm
def test_rendered_scope_sees_the_derived_and_first_party_refs() -> None:
    """`deploy` reads values files and skips ghcr.io/vencil; the tenant-api
    chart's recipe-preview tag exists only in the render (it follows
    generator.image.tag). The rendered scope must list it, first-party."""
    proc = _run("--root", str(_REPO), "--scope", "rendered", "--chart", "helm/tenant-api", "--list")
    assert proc.returncode == 0, proc.stderr
    refs = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    by_repo = {_cir._repo_of(r): r for r in refs}
    preview = by_repo["ghcr.io/vencil/recipe-preview"]
    tools = by_repo["ghcr.io/vencil/da-tools"]
    assert preview.rsplit(":", 1)[1] == tools.rsplit(":", 1)[1], refs
    assert "ghcr.io/vencil/tenant-api" in by_repo
    # …and none of it is visible to the deploy scope.
    assert "ghcr.io/vencil/recipe-preview" not in {_cir._repo_of(r) for r in _list_refs(_REPO)}


@pytest.mark.parametrize("args", [
    ("--scope", "rendered"),                       # no --chart
    ("--chart", "helm/tenant-api"),                # --chart without the scope
    ("--scope", "deploy", "--chart", "helm/tenant-api"),
], ids=["rendered-without-chart", "chart-without-scope", "chart-with-deploy"])
def test_chart_and_rendered_scope_go_together(args) -> None:
    proc = _run("--root", str(_REPO), *args, "--list")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)


def test_rendered_scope_without_helm_is_exit_2_not_a_clean_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        _cir.rendered_refs(_REPO, "helm/tenant-api")
    assert exc.value.code == 2
    assert "helm is not available" in capsys.readouterr().err


@_needs_helm
def test_rendered_scope_refuses_a_render_with_no_image(tmp_path: Path, capsys) -> None:
    _write(tmp_path / "helm/empty/Chart.yaml", "apiVersion: v2\nname: empty\nversion: 0.1.0\n")
    _write(tmp_path / "helm/empty/templates/cm.yaml",
           "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    with pytest.raises(SystemExit) as exc:
        _cir.rendered_refs(tmp_path, "helm/empty")
    assert exc.value.code == 2
    assert "holds no image ref" in capsys.readouterr().err


def test_release_checks_the_chart_defaults_before_pushing_it() -> None:
    """release-tenant-api runs the rendered scope after its own image is
    promoted (so that ref resolves) and before `helm push` (so a chart whose
    defaults cannot be pulled is never published)."""
    import yaml
    wf = yaml.safe_load((_REPO / ".github/workflows/release.yaml").read_text(encoding="utf-8"))
    runs = [step.get("run") or "" for step in wf["jobs"]["release-tenant-api"]["steps"]]
    check = [i for i, r in enumerate(runs)
             if "check_image_refs_resolve.py --scope rendered --chart helm/tenant-api" in r]
    # #3848: promotion and `helm push` run inside release_publish.py.
    push = [i for i, r in enumerate(runs) if "release_publish.py chart" in r]
    promote = [i for i, r in enumerate(runs) if "release_publish.py image" in r]
    assert len(check) == 1 and len(push) == 1 and len(promote) == 1, (check, push, promote)
    assert promote[0] < check[0] < push[0]
    assert "with_da_crdecode.sh" in runs[check[0]]
