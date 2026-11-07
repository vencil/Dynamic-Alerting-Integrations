"""victorialogs chart: consumer authorization through a vmauth sidecar (#1295).

ADR-028's evidence rows live in this store, and anyone who can POST /insert
can write a row with any `log_type`. The chart therefore puts vmauth in front
of VictoriaLogs and holds each consumer to its own paths. These tests pin what
that rests on:

- VictoriaLogs listens on loopback only, and the Service port is vmauth's —
  otherwise vmauth is a door beside an open wall;
- the default consumer registry lets exactly one consumer write (vector), and
  none reach /delete;
- observe passes requests without credentials (the migration step) and
  enforce passes only /health, /ping and /metrics without them;
- passwords reach vmauth only through Secret-backed env vars, never the
  rendered ConfigMap; enforce refuses to render without a Secret per consumer;
- probes reach VictoriaLogs through vmauth, and the pod rolls when the auth
  config changes.

What a render cannot show — that vmauth really answers 401/400 per user and
path — was measured against the rendered config and args (PR description);
the chart cannot change that behaviour, only the config these tests pin.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "helm" / "victorialogs"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")

TWO_CONSUMERS = ('auth.consumers=[{"name":"vector","paths":["/insert/.*"],"existingSecret":"vl-vector"},'
                 '{"name":"federation-gateway","paths":["/select/.*"],"existingSecret":"vl-gw"}]')


# The default is auth.mode=enforce, which refuses to render until every
# consumer names its Secret; tests not about that render the shared baseline
# (one Secret per consumer). `--set` after `-f` still overrides it.
BASELINE = Path(__file__).resolve().parent / "victorialogs_render_baseline.yaml"


def _helm(*args: str, baseline: bool = True) -> subprocess.CompletedProcess:
    base = ["-f", str(BASELINE)] if baseline else []
    return subprocess.run(["helm", "template", "t", str(CHART), "-n", "monitoring", *base, *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


def _render(*args: str, baseline: bool = True) -> list[dict]:
    out = _helm(*args, baseline=baseline)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict], kind: str, suffix: str = "") -> dict:
    hits = [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    assert len(hits) == 1, f"expected one {kind} *{suffix}, got {[d['metadata']['name'] for d in hits]}"
    return hits[0]


def _containers(docs: list[dict]) -> dict[str, dict]:
    pod = _one(docs, "Deployment")["spec"]["template"]["spec"]
    return {c["name"]: c for c in pod["containers"]}


def _auth(docs: list[dict]) -> dict:
    return yaml.safe_load(_one(docs, "ConfigMap", "-vmauth")["data"]["auth.yml"])


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


# ── the store is reachable only through vmauth ──────────────────────────────


def test_victorialogs_listens_on_loopback_only() -> None:
    docs = _render()
    args = _containers(docs)["victorialogs"]["args"]
    listen = [a for a in args if a.startswith("-httpListenAddr=")]
    backend = _values()["auth"]["backendPort"]
    assert listen == [f"-httpListenAddr=127.0.0.1:{backend}"], (
        f"VictoriaLogs must listen on loopback only, behind vmauth; got {listen}")
    assert "ports" not in _containers(docs)["victorialogs"], (
        "the VictoriaLogs container declares a port: nothing outside the pod should address it")


def test_service_port_belongs_to_vmauth() -> None:
    docs = _render()
    svc = _one(docs, "Service")
    target = svc["spec"]["ports"][0]["targetPort"]
    owners = [name for name, c in _containers(docs).items()
              for p in c.get("ports", []) if target in (p["containerPort"], p["name"])]
    assert owners == ["vmauth"], f"Service targetPort {target!r} is served by {owners}, not vmauth"
    vmauth_args = _containers(docs)["vmauth"]["args"]
    assert f"-httpListenAddr=:{_values()['listenPort']}" in vmauth_args


def test_vmauth_serves_its_own_endpoints_off_the_service_port() -> None:
    """Without -httpInternalListenAddr, vmauth answers /health and /metrics on
    the Service port itself: the probes would measure vmauth, not the store,
    and the scrape would collect vmauth's metrics instead of VictoriaLogs'."""
    args = _containers(_render())["vmauth"]["args"]
    internal = [a for a in args if a.startswith("-httpInternalListenAddr=")]
    assert internal == [f"-httpInternalListenAddr=:{_values()['auth']['internalPort']}"], args
    assert _values()["auth"]["internalPort"] != _values()["listenPort"]


def test_probes_reach_victorialogs_through_vmauth() -> None:
    """kubelet probes the pod IP; VictoriaLogs is on loopback, so its probes go
    through vmauth, which forwards /health without credentials in both modes."""
    for mode in ("observe", "enforce"):
        sets = ["--set", f"auth.mode={mode}"] + (["--set-json", TWO_CONSUMERS] if mode == "enforce" else [])
        docs = _render(*sets)
        vl = _containers(docs)["victorialogs"]
        for probe in ("readinessProbe", "livenessProbe"):
            get = vl[probe]["httpGet"]
            # kubelet resolves a NAMED probe port only among the probe's own
            # container's ports; `http` is vmauth's, so a name here fails
            # every probe while rendering fine. It must be the number.
            port = get["port"]
            own = {p["name"]: p["containerPort"] for p in vl.get("ports", [])}
            resolved = own.get(port) if isinstance(port, str) else port
            assert get["path"] == "/health" and resolved == _values()["listenPort"], (mode, probe, get)
        unauth = {p for e in _auth(docs)["unauthorized_user"]["url_map"] for p in e["src_paths"]}
        assert any(re.fullmatch(p, "/health") for p in unauth), (mode, unauth)


# ── who may write, and what passes without credentials ──────────────────────


def _reaches(paths: list[str], path: str) -> bool:
    return any(re.fullmatch(p, path) for p in paths)


def test_only_vector_can_write_and_nobody_can_delete_by_default() -> None:
    consumers = _values()["auth"]["consumers"]
    writers = [c["name"] for c in consumers if _reaches(c["paths"], "/insert/elasticsearch/_bulk")]
    assert writers == ["vector"], f"consumers that can write: {writers} (ADR-028: only the shipper)"
    deleters = [c["name"] for c in consumers if _reaches(c["paths"], "/delete/run_task")]
    assert not deleters, f"consumers that can reach /delete: {deleters}"


def test_enforce_passes_only_probes_and_metrics_without_credentials() -> None:
    unauth = _auth(_render("--set", "auth.mode=enforce", "--set-json", TWO_CONSUMERS))["unauthorized_user"]
    paths = sorted(p for e in unauth["url_map"] for p in e["src_paths"])
    assert paths == ["/health", "/metrics", "/ping"], paths


def test_observe_still_passes_unauthenticated_traffic() -> None:
    """The migration step: until every consumer authenticates, requests without
    credentials keep reaching every path (it is NOT a boundary)."""
    unauth = _auth(_render("--set", "auth.mode=observe"))["unauthorized_user"]
    assert [e["src_paths"] for e in unauth["url_map"]] == [["/.*"]], unauth


def test_each_consumer_is_held_to_its_paths() -> None:
    users = {u["username"]: u for u in _auth(_render("--set-json", TWO_CONSUMERS))["users"]}
    assert users["vector"]["url_map"][0]["src_paths"] == ["/insert/.*"]
    assert users["federation-gateway"]["url_map"][0]["src_paths"] == ["/select/.*"]
    for u in users.values():
        assert u["url_map"][0]["url_prefix"] == f"http://127.0.0.1:{_values()['auth']['backendPort']}"


# ── passwords ───────────────────────────────────────────────────────────────


def test_passwords_come_from_secrets_not_the_configmap() -> None:
    docs = _render("--set-json", TWO_CONSUMERS)
    users = _auth(docs)["users"]
    for u in users:
        assert re.fullmatch(r"%\{VMAUTH_PASSWORD_[A-Z0-9_]+\}", u["password"]), u
    env = {e["name"]: e for e in _containers(docs)["vmauth"]["env"]}
    for u in users:
        var = u["password"][2:-1]
        assert env[var]["valueFrom"]["secretKeyRef"]["key"] == "password", env[var]
    assert env["VMAUTH_PASSWORD_VECTOR"]["valueFrom"]["secretKeyRef"]["name"] == "vl-vector"
    assert env["VMAUTH_PASSWORD_FEDERATION_GATEWAY"]["valueFrom"]["secretKeyRef"]["name"] == "vl-gw"


def test_observe_skips_consumers_without_a_secret() -> None:
    assert "users" not in _auth(_render("--set", "auth.mode=observe", baseline=False)), (
        "observe rendered a user with no Secret behind it")


@pytest.mark.parametrize("args, needle", [
    (["--set-json", 'auth.consumers=[{"name":"a","paths":["/x"],"existingSecret":"s"},'
                    '{"name":"b","paths":["/y"]}]'], "missing: b."),
    (["--set", "auth.mode=open"], "auth.mode must be observe or enforce"),
    (["--set-json", 'auth.consumers=[{"name":"a","paths":["/x"],"existingSecret":"s"},'
                    '{"name":"a","paths":["/y"],"existingSecret":"s"}]'], "is listed twice"),
    (["--set-json", 'auth.consumers=[{"name":"a","paths":[],"existingSecret":"s"}]'], "paths is empty"),
    (["--set-json", 'auth.consumers=[{"name":"A_b","paths":["/x"],"existingSecret":"s"}]'], "lowercase letters"),
    (["--set", "auth.mode=enforce", "--set-json", "auth.consumers=[]"], "would lock every consumer out"),
])
def test_render_refuses(args: list[str], needle: str) -> None:
    out = _helm(*args)
    assert out.returncode != 0 and needle in out.stderr, (out.returncode, out.stderr[-400:])


def test_unauthenticated_traffic_is_not_capped_below_the_global_limit() -> None:
    """In observe every request without credentials is ONE vmauth user; the
    default per-user cap (100) would be a new ceiling on all of them."""
    args = _containers(_render("--set", "auth.mode=observe"))["vmauth"]["args"]
    cap = _values()["auth"]["maxConcurrentRequests"]
    assert f"-maxConcurrentPerUserRequests={cap}" in args and f"-maxConcurrentRequests={cap}" in args, args


def test_pod_rolls_when_the_auth_config_changes() -> None:
    def checksum(*args: str) -> str:
        ann = _one(_render(*args), "Deployment")["spec"]["template"]["metadata"]["annotations"]
        return ann["checksum/vmauth"]
    assert checksum() != checksum("--set-json", TWO_CONSUMERS)


# ── enforce is the default (#1295 phase 3) ──────────────────────────────────


def test_enforce_is_the_default() -> None:
    assert _values()["auth"]["mode"] == "enforce"


def test_a_bare_install_names_every_consumer_it_must_provision() -> None:
    """Breaking on purpose: with no Secrets named the render fails, and it says
    which consumers at once rather than one per attempt."""
    out = _helm(baseline=False)
    names = [c["name"] for c in _values()["auth"]["consumers"]]
    assert out.returncode != 0 and f"missing: {', '.join(names)}." in out.stderr, out.stderr[-600:]
    assert "auth.mode=observe" in out.stderr, "the error must name the migration way out"


def test_render_baseline_restates_the_registry() -> None:
    """Helm replaces lists wholesale, so the baseline repeats the registry; it
    must repeat it exactly, or every test using it judges a different store."""
    def strip(consumers: list[dict]) -> list[dict]:
        return [{k: v for k, v in c.items() if k != "existingSecret"} for c in consumers]
    baseline = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))["auth"]["consumers"]
    assert strip(baseline) == strip(_values()["auth"]["consumers"])
    assert all(c.get("existingSecret") for c in baseline), "the baseline must name a Secret per consumer"
