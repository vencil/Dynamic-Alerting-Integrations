"""test_ingress_render.py — tenant-api Ingress template (#2027)

The chart carried a full `ingress:` block in values.yaml with NO template
reading it: `helm install` exited 0 and no Ingress existed. (da-portal had the
same defect; since ADR-027 §5 it has no Ingress at all — the deployment has one
Ingress, tenant-api's — pinned by tests/helm/test_portal_static_tier_guard.py.)
These tests pin:

  * the value is actually consumed — enabling it renders an Ingress whose
    host / class / TLS / annotations come from values (default stays off);
  * ⛔ the backend is the authenticated `http` Service port (80 → oauth2-proxy
    :4180) and NEVER the internal 8080 port (GHSA-3g2h-rf85-5rrv): an Ingress
    there would publish a port that skips the login;
  * render fails closed when the backend could not serve (oauth2Proxy off →
    nothing on :4180) or when there are no hosts (API-server-invalid object).

Render layers are helm-gated (skipped without the CLI), same as the sibling
tests in this directory; CI installs helm via setup-helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _render_baseline import baseline_args

_CHARTS = ["helm/tenant-api"]
_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _helm_template(repo_root: Path, chart: str, *args: str):
    cmd = ["helm", "template", "t", str(repo_root / chart), *baseline_args(chart), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def _docs(stdout: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(stdout) if d]


def _kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _enable(*extra: str) -> tuple[str, ...]:
    return ("--set", "ingress.enabled=true", *extra)


# ── static layer ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("chart", _CHARTS)
def test_values_declare_ingress_off_by_default(repo_root: Path, chart: str):
    values = yaml.safe_load((repo_root / chart / "values.yaml").read_text(encoding="utf-8"))
    assert values["ingress"]["enabled"] is False, (
        f"{chart}: ingress must stay opt-in — a default-on Ingress would publish "
        "the service on every install"
    )


# ── render layer ────────────────────────────────────────────────────────────
@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_default_render_has_no_ingress(repo_root: Path, chart: str):
    res = _helm_template(repo_root, chart)
    assert res.returncode == 0, res.stderr
    assert _kind(_docs(res.stdout), "Ingress") == []


@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_enabled_render_consumes_values(repo_root: Path, chart: str):
    """Every ingress.* field a user sets must land on the object — the bug was
    exactly that setting them changed nothing."""
    res = _helm_template(
        repo_root, chart, *_enable(
            "--set", "ingress.className=nginx",
            "--set", "ingress.hosts[0].host=x.example.test",
            "--set", "ingress.hosts[0].paths[0].path=/",
            "--set", "ingress.hosts[0].paths[0].pathType=Prefix",
            "--set", "ingress.tls[0].secretName=x-tls",
            "--set", "ingress.tls[0].hosts[0]=x.example.test",
            "--set", "ingress.annotations.probe\\.example/marker=on",
        ),
    )
    assert res.returncode == 0, res.stderr
    [ing] = _kind(_docs(res.stdout), "Ingress")
    spec = ing["spec"]
    assert spec["ingressClassName"] == "nginx"
    assert spec["rules"][0]["host"] == "x.example.test"
    assert spec["tls"] == [{"hosts": ["x.example.test"], "secretName": "x-tls"}]
    assert ing["metadata"]["annotations"] == {"probe.example/marker": "on"}


@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_backend_is_the_authenticated_port_only(repo_root: Path, chart: str):
    """⛔ Security pin: every Ingress path must target the Service port that
    goes through oauth2-proxy, resolved against the rendered Service rather
    than a hard-coded name — so renaming the port on either side cannot
    silently repoint the Ingress at 8080."""
    res = _helm_template(
        repo_root, chart, *_enable(
            "--set", "ingress.hosts[0].host=a.example.test",
            "--set", "ingress.hosts[0].paths[0].path=/",
            "--set", "ingress.hosts[1].host=b.example.test",
            "--set", "ingress.hosts[1].paths[0].path=/v1",
        ),
    )
    assert res.returncode == 0, res.stderr
    docs = _docs(res.stdout)
    [svc] = _kind(docs, "Service")
    [ing] = _kind(docs, "Ingress")
    by_name = {p["name"]: p for p in svc["spec"]["ports"]}
    backends = [
        p["backend"]["service"]
        for rule in ing["spec"]["rules"]
        for p in rule["http"]["paths"]
    ]
    assert len(backends) == 2
    for be in backends:
        assert be["name"] == svc["metadata"]["name"]
        port = by_name[be["port"]["name"]]
        assert port["targetPort"] == 4180, (
            f"{chart}: Ingress backend resolves to targetPort {port['targetPort']}, "
            "not oauth2-proxy :4180 — this publishes an unauthenticated port"
        )


@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_host_given_without_paths_serves_root(repo_root: Path, chart: str):
    """An install line that sets only `ingress.hosts[0].host=…`. `--set` on a list index REPLACES the list, so
    `paths` is gone; an empty http.paths is rejected by the API server, i.e.
    the documented command would render an uninstallable object."""
    res = _helm_template(
        repo_root, chart, *_enable("--set", "ingress.hosts[0].host=only.example.test"),
    )
    assert res.returncode == 0, res.stderr
    [ing] = _kind(_docs(res.stdout), "Ingress")
    [rule] = ing["spec"]["rules"]
    assert [(p["path"], p["pathType"]) for p in rule["http"]["paths"]] == [("/", "Prefix")]


@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_the_ingress_always_has_oauth2_proxy_behind_it(repo_root: Path, chart: str):
    """The Ingress backend is the authenticated `http` port (oauth2-proxy
    :4180). There is no switch to leave the proxy out (ADR-027 §6; the schema
    refuses the removed oauth2Proxy.enabled), so with the Ingress on the pod
    always carries the oauth2-proxy container that answers it."""
    res = _helm_template(repo_root, chart, *_enable())
    assert res.returncode == 0, res.stderr
    docs = _docs(res.stdout)
    assert _kind(docs, "Ingress"), "the Ingress did not render"
    [dep] = _kind(docs, "Deployment")
    names = [c["name"] for c in dep["spec"]["template"]["spec"]["containers"]]
    assert "oauth2-proxy" in names, names


@_needs_helm
@pytest.mark.parametrize("chart", _CHARTS)
def test_fails_closed_without_hosts(repo_root: Path, chart: str):
    res = _helm_template(repo_root, chart, *_enable("--set", "ingress.hosts=null"))
    assert res.returncode != 0
    assert "requires at least one entry in ingress.hosts" in res.stderr
