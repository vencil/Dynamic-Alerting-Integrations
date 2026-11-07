"""Every VictoriaLogs consumer is registered with vmauth and actually authenticates (#1295).

helm/victorialogs holds the consumer registry (`auth.consumers`: who may talk
to the store and on which paths). Each consumer declares itself on its own
side, structurally: a chart through `victorialogs.auth.username`, Grafana
through the datasource's `basicAuthUser`. Three things can drift apart, and
each has a test here:

1. registry ↔ declarations — a consumer the registry lists that nothing
   declares (it will be locked out under `auth.mode: enforce`), or a
   declaration the registry does not list (its requests fall to
   unauthorized_user). Equality, both directions.
2. declaration ↔ wiring — a chart that declares a username but never sends
   it. For every registered consumer there is a probe that renders the real
   chart (or reads the real manifest) with a Secret named and follows the
   password from the Secret reference to the client that sends it; where the
   client is code that ships with the chart or the repo (chargeback's script,
   the reconciler), the probe runs it against a local server and reads the
   Authorization header it actually sent. The probe table must cover the
   registry exactly, so a new consumer without a probe is red.
3. paths — what a consumer really requests must fall inside the paths the
   registry grants it (vmauth matches `src_paths` as anchored regexes).

Discovery does not grep for URLs: a chart is a consumer when its values carry
a top-level `victorialogs` block (all four consumer charts use that shape),
and a raw manifest is one when it provisions a datasource of the
VictoriaLogs plugin type. A chart with a `victorialogs` block and no
`auth.username` is red — it talks to the store without saying who it is.

How deep each probe goes differs, and the difference matters when reading a
green run: chargeback and the reconciler are executed and the Authorization
header they really send is read; Vector, the gateway and Grafana are checked
statically (Secret reference → env/file → the config or Lua that uses it).
What this cannot see: whether Grafana's plugin really sends basic auth (it is
Grafana's standard datasource HTTP setting), and the gateway's Lua at
runtime — tests/federation-e2e/test_victorialogs_e2e.py runs that against a
real vmauth in enforce mode (VL1/VL7 go red when the gateway sends no
credential).
"""
from __future__ import annotations

import base64
import http.server
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _tree import repo_files  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HELM = REPO / "helm"
STORE_CHART = HELM / "victorialogs"
GRAFANA_PLUGIN = "victoriametrics-logs-datasource"
SECRET = "vl-consumer-secret"
PASSWORD = "0123456789abcdef"

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def registry() -> dict[str, list[str]]:
    """helm/victorialogs auth.consumers: name -> granted path regexes."""
    consumers = _load(STORE_CHART / "values.yaml")["auth"]["consumers"]
    names = [c["name"] for c in consumers]
    assert len(names) == len(set(names)), f"registry lists a consumer twice: {names}"
    return {c["name"]: list(c["paths"]) for c in consumers}


def chart_declarations() -> dict[str, str]:
    """chart dir name -> declared username, for every chart that talks to the store."""
    out: dict[str, str] = {}
    for chart in sorted(p.parent for p in HELM.glob("*/Chart.yaml")):
        if chart == STORE_CHART:
            continue
        block = _load(chart / "values.yaml").get("victorialogs")
        if block is None:
            continue
        user = ((block or {}).get("auth") or {}).get("username")
        assert user, (
            f"helm/{chart.name} has a `victorialogs` block but no victorialogs.auth.username: "
            "it talks to the store without saying who it is — declare it and register it in "
            "helm/victorialogs auth.consumers")
        out[chart.name] = user
    return out


def _grafana_datasources() -> list[tuple[Path, dict, dict]]:
    """(file, ConfigMap, datasource) for every provisioned VictoriaLogs datasource."""
    hits = []
    for f in repo_files(".yaml", ".yml"):
        if "k8s" not in f.relative_to(REPO).parts:
            continue
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
                continue
            for key, text in (doc.get("data") or {}).items():
                if not key.endswith((".yaml", ".yml")) or "datasources" not in text:
                    continue
                parsed = yaml.safe_load(text) or {}
                for ds in parsed.get("datasources") or []:
                    if ds.get("type") == GRAFANA_PLUGIN:
                        hits.append((f, doc, ds))
    return hits


def manifest_declarations() -> dict[str, str]:
    """datasource label -> declared username, for raw-manifest consumers."""
    out = {}
    for f, _cm, ds in _grafana_datasources():
        user = ds.get("basicAuthUser")
        label = f"{f.relative_to(REPO)}:{ds.get('name')}"
        assert user, f"{label} queries VictoriaLogs without basicAuthUser"
        out[label] = user
    return out


def all_declared_usernames() -> list[str]:
    return list(chart_declarations().values()) + list(manifest_declarations().values())


# ── 1. registry ↔ declarations ──────────────────────────────────────────────


def test_every_registered_consumer_is_declared_and_nothing_else_is() -> None:
    declared = all_declared_usernames()
    assert len(declared) == len(set(declared)), (
        f"two consumers declare the same username: {sorted(declared)} — vmauth would hold "
        "both to one consumer's paths and they could not be told apart")
    reg = set(registry())
    missing = reg - set(declared)
    extra = set(declared) - reg
    assert not missing and not extra, (
        f"registered but declared by nothing (locked out under enforce): {sorted(missing)}; "
        f"declared but not registered (falls to unauthorized_user): {sorted(extra)}")


def test_discovery_finds_every_known_consumer_shape() -> None:
    """The discovery above must not shrink to nothing: if the `victorialogs`
    block were renamed in every chart, test 1 would compare two empty sets."""
    assert chart_declarations(), "no chart declares a VictoriaLogs consumer"
    assert manifest_declarations(), "no raw manifest provisions a VictoriaLogs datasource"


# ── 2. declaration ↔ wiring, 3. paths ───────────────────────────────────────


@dataclass
class Observed:
    username: str
    paths: list[str]  # request paths the consumer really sends; [] = not observable here


def _render(chart: str, *sets: str) -> list[dict]:
    args = ["helm", "template", "t", str(HELM / chart), "-n", "monitoring"]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _pod_spec(doc: dict) -> dict:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def _secret_env(container: dict) -> dict[str, dict]:
    """env name -> secretKeyRef, for Secret-backed env vars."""
    return {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container.get("env") or []
            if "secretKeyRef" in (e.get("valueFrom") or {})}


def _literal_env(container: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container.get("env") or [] if "value" in e}


def _the_password_env(container: dict, where: str) -> str:
    refs = {n: r for n, r in _secret_env(container).items()
            if r.get("name") == SECRET and r.get("key") == "password"}
    assert len(refs) == 1, f"{where}: expected one env var from Secret {SECRET}/password, got {refs}"
    return next(iter(refs))


class _Capture(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append((self.path.split("?", 1)[0], self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_a) -> None:
        pass


def _serve() -> tuple[http.server.HTTPServer, str]:
    _Capture.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _basic_user(header: str | None, where: str) -> str:
    assert header and header.startswith("Basic "), f"{where} sent no basic auth: {header!r}"
    user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    assert pw == PASSWORD, f"{where} sent the wrong password: {pw!r}"
    return user


def probe_vector(*extra: str) -> Observed:
    docs = _render("vector", f"victorialogs.auth.existingSecret={SECRET}", *extra,
                   "tenantProjections[0].tenantId=tenant-alpha",
                   "tenantProjections[0].accountId=1000",
                   "projectionGate.registry.configMapName=reg")
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    vector = next(c for c in _pod_spec(ds)["containers"] if c["name"] == "vector")
    env = _the_password_env(vector, "vector container")
    values = _load(HELM / "vector" / "values.yaml")["victorialogs"]
    store = f"http://{values['host']}:{values['port']}/"
    # The config is split across keys of one ConfigMap (vector.yaml, plus the
    # gated tenant-projection fragment): collect sinks from every fragment.
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "vector.yaml" in d.get("data", {}))
    sinks: dict[str, dict] = {}
    for text in cm["data"].values():
        frag = yaml.safe_load(text)
        if isinstance(frag, dict):
            sinks.update(frag.get("sinks") or {})
    users, paths = set(), []
    store_sinks = {n: s for n, s in sinks.items()
                   if any(str(u).startswith(store) for u in s.get("endpoints") or [])}
    assert len(store_sinks) >= 2, f"expected the 0:0 sink and a tenant projection sink, got {sorted(store_sinks)}"
    for name, sink in store_sinks.items():
        auth = sink.get("auth") or {}
        assert auth.get("strategy") == "basic" and auth.get("password") == f"${{{env}}}", (
            f"vector sink {name} writes to the store without the Secret-backed basic auth: {auth}")
        users.add(auth.get("user"))
        # The elasticsearch sink POSTs to <endpoint>_bulk; check that, not the
        # endpoint base, against the grant.
        paths += ["/" + u[len(store):].rstrip("/") + "/_bulk" for u in sink["endpoints"]]
    assert len(users) == 1, f"vector sinks disagree on the username: {users}"
    return Observed(users.pop(), paths)


def _run_chargeback(docs: list[dict], tmp: Path) -> Observed:
    script = next(d for d in docs if d["kind"] == "ConfigMap"
                  and "aggregate.py" in d.get("data", {}))["data"]["aggregate.py"]
    path = tmp / "aggregate.py"
    path.write_text(script, encoding="utf-8")
    users = set()
    for kind in ("CronJob", "Job"):
        c = _pod_spec(next(d for d in docs if d["kind"] == kind))["containers"][0]
        env_name = _the_password_env(c, f"chargeback {kind}")
        srv, url = _serve()
        try:
            env = {**os.environ, **_literal_env(c), "VICTORIALOGS_URL": url, "OUTPUT_DIR": str(tmp),
                   env_name: PASSWORD}
            run = subprocess.run([sys.executable, "-I", str(path)], env=env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=60, check=False)
            assert run.returncode == 0, run.stderr
        finally:
            srv.shutdown()
        assert len(_Capture.seen) == 1, _Capture.seen
        users.add(_basic_user(_Capture.seen[0][1], f"chargeback {kind}"))
        sent_path = _Capture.seen[0][0]
    assert len(users) == 1, f"CronJob and Job disagree on the username: {users}"
    return Observed(users.pop(), [sent_path])


def probe_chargeback(tmp: Path, *extra: str) -> Observed:
    docs = _render("chargeback-aggregator", f"victorialogs.auth.existingSecret={SECRET}",
                   "manualJob.enabled=true", *extra)
    return _run_chargeback(docs, tmp)


def probe_reconciler(*extra: str) -> Observed:
    docs = _render("federation-reconciler", f"victorialogs.auth.existingSecret={SECRET}", *extra)
    c = next(c for c in _pod_spec(next(d for d in docs if d["kind"] == "Deployment"))["containers"]
             if c["name"] == "reconciler")
    env_name = _the_password_env(c, "reconciler container")
    sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
    import _federation_revocation_reconciler as rec  # noqa: PLC0415
    srv, url = _serve()
    old = dict(os.environ)
    try:
        os.environ.update(_literal_env(c))
        os.environ[env_name] = PASSWORD
        assert rec.query_victorialogs(url, "*") == []
    finally:
        os.environ.clear()
        os.environ.update(old)
        srv.shutdown()
    (path, header), = _Capture.seen
    return Observed(_basic_user(header, "reconciler"), [path])


def probe_gateway(*extra: str) -> Observed:
    docs = _render("federation-gateway", "mode=victorialogs", "jwt.audience=tenant-federation-logs",
                   f"victorialogs.auth.existingSecret={SECRET}", *extra)
    pod = _pod_spec(next(d for d in docs if d["kind"] == "Deployment"))
    vols = [v["name"] for v in pod["volumes"] if (v.get("secret") or {}).get("secretName") == SECRET]
    assert len(vols) == 1, f"gateway mounts Secret {SECRET} {len(vols)} times"
    items = {i["key"]: i["path"] for i in next(v for v in pod["volumes"] if v["name"] == vols[0])["secret"]["items"]}
    envoy = next(c for c in pod["containers"] if c["name"] == "envoy")
    mount = next(m["mountPath"] for m in envoy["volumeMounts"] if m["name"] == vols[0])
    lua = next(d for d in docs if d["kind"] == "ConfigMap"
               and "revoked_check.lua" in d.get("data", {}))["data"]["revoked_check.lua"]
    consts = dict(re.findall(r'^local (VL_AUTH_USER|VL_AUTH_FILE) = "([^"]*)"$', lua, re.M))
    assert consts.get("VL_AUTH_FILE") == f"{mount}/{items['password']}", (
        f"the Lua reads {consts.get('VL_AUTH_FILE')!r} but the Secret's password is mounted at "
        f"{mount}/{items.get('password')}")
    # Static only — this cannot run the Lua. It does pin the send to the
    # victorialogs branch of envoy_on_request (moving it into another mode's
    # branch would otherwise stay green); that the gateway really gets past an
    # enforcing vmauth is the federation E2E's VL1/VL7.
    body = lua.split("function envoy_on_request(handle)", 1)[1]
    branch = body.split('if MODE == "victorialogs" then', 1)
    assert len(branch) == 2, "envoy_on_request has no victorialogs branch"
    sends = [i for i, part in enumerate(branch) if 'replace("authorization", vl_auth_header)' in part]
    assert sends == [1], (
        "the store credential must be sent inside envoy_on_request's victorialogs branch, and only there")
    endpoints = _load(HELM / "federation-gateway" / "values.yaml")["victorialogs"]["allowedEndpoints"]
    return Observed(consts["VL_AUTH_USER"], list(endpoints))


def probe_grafana() -> Observed:
    (f, _cm, ds), = _grafana_datasources()
    pw = (ds.get("secureJsonData") or {}).get("basicAuthPassword", "")
    m = re.fullmatch(r"\$\{(\w+)\}", pw)
    assert ds.get("basicAuth") is True and m, f"{f}: datasource does not send a Secret-backed basic auth: {ds}"
    deploys = [d for p in repo_files(".yaml") if "k8s" in p.relative_to(REPO).parts
               for d in yaml.safe_load_all(p.read_text(encoding="utf-8"))
               if isinstance(d, dict) and d.get("kind") == "Deployment"
               and (d.get("metadata") or {}).get("name") == "grafana"]
    (deploy,) = deploys
    grafana = next(c for c in _pod_spec(deploy)["containers"] if c["name"] == "grafana")
    ref = _secret_env(grafana).get(m.group(1))
    assert ref and ref.get("key") == "password", (
        f"the datasource reads ${m.group(1)} but the grafana container has no Secret-backed env of that name")
    return Observed(ds["basicAuthUser"], [])  # the plugin chooses its paths


PROBES: dict[str, Callable[..., Observed]] = {
    "vector": probe_vector,
    "grafana": probe_grafana,
    "chargeback-aggregator": probe_chargeback,
    "federation-gateway": probe_gateway,
    "federation-reconciler": probe_reconciler,
}


def test_every_registered_consumer_has_a_wiring_probe() -> None:
    assert set(PROBES) == set(registry()), (
        f"registry {sorted(registry())} vs probes {sorted(PROBES)}: a consumer without a probe here "
        "can declare a username and never send it")


@_needs_helm
@pytest.mark.parametrize("consumer", sorted(PROBES))
def test_consumer_sends_its_registered_identity_on_granted_paths(consumer: str, tmp_path: Path) -> None:
    probe = PROBES[consumer]
    obs = probe(tmp_path) if probe is probe_chargeback else probe()
    assert obs.username == consumer, (
        f"{consumer} authenticates as {obs.username!r}: vmauth would treat it as an unknown user")
    granted = registry()[consumer]
    denied = [p for p in obs.paths if not any(re.fullmatch(g, p) for g in granted)]
    assert not denied, f"{consumer} requests {denied}, outside its registered paths {granted}"


# ── no Secret, no credentials (observe-mode installs keep working) ──────────


# ── values from an older chart (#1295 review) ──────────────────────────────
# `helm upgrade --reuse-values` from a chart older than #1295 keeps the old
# values and does NOT merge the new defaults in, so victorialogs.auth is
# absent; an operator following the README may then add only existingSecret.
CHART_CONSUMERS = ["vector", "chargeback-aggregator", "federation-reconciler", "federation-gateway"]


@_needs_helm
@pytest.mark.parametrize("chart", CHART_CONSUMERS)
def test_values_without_an_auth_block_still_render(chart: str) -> None:
    _render(chart, "victorialogs.auth=null")


@_needs_helm
@pytest.mark.parametrize("consumer", CHART_CONSUMERS)
def test_a_secret_without_a_username_still_sends_the_registered_identity(consumer: str, tmp_path: Path) -> None:
    probe = PROBES[consumer]
    unset = "victorialogs.auth.username=null"
    obs = probe(tmp_path, unset) if probe is probe_chargeback else probe(unset)
    assert obs.username == consumer, (
        f"{consumer} with no username authenticates as {obs.username!r}; the fallback must be the "
        "registered name")


@_needs_helm
@pytest.mark.parametrize("chart,sets", [
    ("vector", ["tenantProjections[0].tenantId=tenant-alpha", "tenantProjections[0].accountId=1000",
                "projectionGate.registry.configMapName=reg"]),
    ("chargeback-aggregator", ["manualJob.enabled=true"]),
    ("federation-reconciler", []),
    ("federation-gateway", ["mode=victorialogs", "jwt.audience=tenant-federation-logs"]),
])
def test_without_a_secret_no_credential_is_rendered(chart: str, sets: list[str]) -> None:
    docs = _render(chart, *sets)
    for d in docs:
        if d["kind"] in ("Deployment", "DaemonSet", "CronJob", "Job"):
            pod = _pod_spec(d)
            env = [e["name"] for c in pod["containers"] for e in c.get("env") or []]
            assert not [n for n in env if n.startswith("VICTORIALOGS_") and n != "VICTORIALOGS_URL"], (
                f"helm/{chart} {d['kind']} carries store credentials with no Secret named: {env}")
            assert not [v for v in pod.get("volumes") or [] if "secret" in v], (
                f"helm/{chart} {d['kind']} mounts a Secret with no Secret named")
        data = d.get("data") or {} if d["kind"] == "ConfigMap" else {}
        if "vector.yaml" in data:
            for key, text in data.items():
                frag = yaml.safe_load(text)
                sinks = (frag.get("sinks") or {}) if isinstance(frag, dict) else {}
                assert not [n for n, s in sinks.items() if "auth" in s], f"vector {key} sinks carry auth"
        if "revoked_check.lua" in data:
            assert "local VL_AUTH_FILE = nil" in data["revoked_check.lua"]


def test_gateway_refuses_a_secret_outside_victorialogs_mode() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm CLI not on PATH")
    out = subprocess.run(["helm", "template", "t", str(HELM / "federation-gateway"),
                          "--set", f"victorialogs.auth.existingSecret={SECRET}"],
                         capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert out.returncode != 0 and "only used in mode=victorialogs" in out.stderr, out.stderr
