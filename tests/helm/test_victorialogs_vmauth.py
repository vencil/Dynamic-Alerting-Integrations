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

TWO_CONSUMERS = ('auth.consumers=[{"name":"vector","paths":["/insert/.*"],'
                 '"from":{"namespace":"vector"}},'
                 '{"name":"federation-gateway","paths":["/select/.*"],'
                 '"from":{"podLabels":{"app.kubernetes.io/name":"federation-gateway"}}}]')
TWO_SECRETS = 'auth.existingSecrets={"vector":"vl-vector","federation-gateway":"vl-gw"}'
# Rendered WITHOUT the baseline: its existingSecrets names all five consumers,
# and a null in --set cannot delete a key another -f file supplied (Helm drops
# nulls only against chart defaults) — the three extra keys would be unknown.
TWO = ("--set-json", TWO_CONSUMERS, "--set-json", TWO_SECRETS)


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
        sets = ["--set", f"auth.mode={mode}"] + (list(TWO) if mode == "enforce" else [])
        docs = _render(*sets, baseline=mode != "enforce")
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
    unauth = _auth(_render("--set", "auth.mode=enforce", *TWO, baseline=False))["unauthorized_user"]
    paths = sorted(p for e in unauth["url_map"] for p in e["src_paths"])
    assert paths == ["/health", "/metrics", "/ping"], paths


def test_observe_still_passes_unauthenticated_traffic() -> None:
    """The migration step: until every consumer authenticates, requests without
    credentials keep reaching every path (it is NOT a boundary)."""
    unauth = _auth(_render("--set", "auth.mode=observe"))["unauthorized_user"]
    assert [e["src_paths"] for e in unauth["url_map"]] == [["/.*"]], unauth


def test_each_consumer_is_held_to_its_paths() -> None:
    users = {u["username"]: u for u in _auth(_render(*TWO, baseline=False))["users"]}
    assert users["vector"]["url_map"][0]["src_paths"] == ["/insert/.*"]
    assert users["federation-gateway"]["url_map"][0]["src_paths"] == ["/select/.*"]
    for u in users.values():
        assert u["url_map"][0]["url_prefix"] == f"http://127.0.0.1:{_values()['auth']['backendPort']}"


# ── passwords ───────────────────────────────────────────────────────────────


def test_passwords_come_from_secrets_not_the_configmap() -> None:
    docs = _render(*TWO, baseline=False)
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
    (["--set-json", 'auth.consumers=[{"name":"a","paths":["/x"]},{"name":"b","paths":["/y"]}]',
      "--set-json", 'auth.existingSecrets={"a":"s"}'], "missing: b."),
    (["--set", "auth.mode=open"], "auth.mode must be observe or enforce"),
    (["--set-json", 'auth.consumers=[{"name":"a","paths":["/x"]},{"name":"a","paths":["/y"]}]',
      "--set-json", 'auth.existingSecrets={"a":"s"}'], "is listed twice"),
    (["--set-json", 'auth.consumers=[{"name":"a","paths":[]}]',
      "--set-json", 'auth.existingSecrets={"a":"s"}'], "paths is empty"),
    (["--set-json", 'auth.consumers=[{"name":"A_b","paths":["/x"]}]'], "lowercase letters"),
    (["--set", "auth.mode=enforce", "--set-json", "auth.consumers=[]"], "would lock every consumer out"),
    # #3847: a key naming no consumer (a typo) — the Secret would never be used
    (["--set", "auth.existingSecrets.vectr=s"], "auth.existingSecrets names vectr, which auth.consumers does not list"),
    # #3847: values from <0.7.0 named the Secret on the consumer
    (["--set-json", 'auth.consumers=[{"name":"vector","paths":["/insert/.*"],"existingSecret":"s",'
                    '"from":{"namespace":"vector"}}]'], "auth.consumers[vector].existingSecret was removed"),
    (["--set-json", 'auth.existingSecrets={"vector":{"name":"s"}}'], "auth.existingSecrets.vector must be a Secret name"),
    (["--set-json", 'auth.existingSecrets=["s"]'], "auth.existingSecrets must be a map"),
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
        ann = _one(_render(*args, baseline=not args), "Deployment")["spec"]["template"]["metadata"]["annotations"]
        return ann["checksum/vmauth"]
    assert checksum() != checksum(*TWO)


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


def test_render_baseline_names_a_secret_per_consumer_and_nothing_else() -> None:
    """The baseline only names Secrets (#3847): restating the registry there was
    a second copy that had to be held equal to values.yaml."""
    baseline = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))["auth"]
    assert "consumers" not in baseline, "the baseline must not restate the registry (#3847)"
    names = [c["name"] for c in _values()["auth"]["consumers"]]
    assert sorted(baseline["existingSecrets"]) == sorted(names), (
        "the baseline names a Secret for exactly the registry's consumers")
    assert all(baseline["existingSecrets"].values()), "every baseline Secret name is non-empty"



def test_upgrade_checks_named_secrets_exist_before_recreating_the_pod() -> None:
    """A named Secret that is missing (or lacks `password`) is an outage on
    upgrade: the strategy is Recreate, so the running pod goes first and the new
    one stays in CreateContainerConfigError. The helper looks the Secrets up —
    only once this release's Deployment exists, which is both the case with a
    pod to lose and proof that `lookup` reaches a cluster (it returns empty
    under `helm template`, so offline renders must keep working).

    `lookup` needs a real API server, so this pins the thin wiring around it;
    the judgement itself is exercised below with fake lookup results. Measured
    on k3s v1.31 (PR description): missing Secret / missing key / empty value
    each fail `helm upgrade`, a fresh install does not, a deployer without
    `get secrets` fails every upgrade unless checkSecretsOnUpgrade=false, and
    `--reuse-values` from 0.4.0 (where the key is absent) is still checked."""
    tmpl = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    block = tmpl[tmpl.index('{{- define "victorialogs.authConsumers"'):]
    block = block[:block.index("{{- toYaml $out")]
    gate = re.search(
        r'\{\{- if and \(include "victorialogs\.checkSecretsOnUpgrade" \.\) \(lookup "apps/v1" "Deployment" '
        r'\.Release\.Namespace \(include "victorialogs\.name" \.\)\) -\}\}\n'
        r'\{\{- \$found := dict -\}\}\n'
        r'\{\{- range \$out -\}\}\n'
        r'\{\{- \$_ := set \$found \(toString \.existingSecret\) \(lookup "v1" "Secret" '
        r'\$\.Release\.Namespace \(toString \.existingSecret\)\) -\}\}\n'
        r'\{\{- end -\}\}\n'
        r'\{\{- \$broken := include "victorialogs\.secretProblems" '
        r'\(dict "consumers" \$out "secrets" \$found\) -\}\}\n'
        r'\{\{- if \$broken -\}\}\n\{\{- fail ', block)
    assert gate, "the upgrade gate must look up every mounted Secret and fail on any problem"
    assert _values()["auth"]["checkSecretsOnUpgrade"] is True, "the check must be on by default"
    assert _helm().returncode == 0, "offline render must not treat an unreachable cluster as missing Secrets"


_PROBLEMS_TEMPLATE = (
    'problems: {{ include "victorialogs.secretProblems" '
    '(dict "consumers" .Values.consumers "secrets" .Values.secrets) | quote }}\n'
)


def _probe(tmp_path: Path, template: str, values: dict) -> str:
    """Render `template` (one `problems:` line) against the chart's own
    _helpers.tpl, in a throwaway chart that holds a copy of it and nothing else."""
    chart = tmp_path / "probe"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: victorialogs\nversion: 0.0.0\n", encoding="utf-8")
    (chart / "templates" / "_helpers.tpl").write_text(
        (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8"), encoding="utf-8")
    (chart / "templates" / "probe.yaml").write_text(template, encoding="utf-8")
    values_file = tmp_path / "v.yaml"
    values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(["helm", "template", "p", str(chart), "-f", str(values_file)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return yaml.safe_load(out.stdout.split("---", 1)[1].split("\n", 2)[2])["problems"]


def _secret_problems(tmp_path: Path, consumers: list[dict], secrets: dict) -> str:
    """victorialogs.secretProblems with fake lookup results."""
    return _probe(tmp_path, _PROBLEMS_TEMPLATE, {"consumers": consumers, "secrets": secrets})


def test_secret_problems_flags_missing_keyless_and_empty_secrets(tmp_path: Path) -> None:
    consumers = [{"name": n, "existingSecret": f"s-{n}"} for n in ("ok", "gone", "nokey", "empty", "nodata")]
    secrets = {
        "s-ok": {"data": {"password": "YWJj"}},
        # s-gone: absent, as `lookup` answers NotFound (an empty map)
        "s-nokey": {"data": {"pw": "YWJj"}},
        "s-empty": {"data": {"password": ""}},
        "s-nodata": {"metadata": {"name": "s-nodata"}},
    }
    assert _secret_problems(tmp_path, consumers, secrets) == (
        'gone: Secret "s-gone" not found; '
        'nokey: Secret "s-nokey" has no non-empty key `password`; '
        'empty: Secret "s-empty" has no non-empty key `password`; '
        'nodata: Secret "s-nodata" has no non-empty key `password`')


def test_secret_problems_is_empty_when_every_secret_serves(tmp_path: Path) -> None:
    consumers = [{"name": "a", "existingSecret": "s-a"}, {"name": "b", "existingSecret": "s-b"}]
    secrets = {"s-a": {"data": {"password": "YWJj"}}, "s-b": {"data": {"password": "ZGVm", "x": "eQ=="}}}
    assert _secret_problems(tmp_path, consumers, secrets) == ""


@pytest.mark.parametrize(("auth", "expected"), [
    ({}, "true"),  # --reuse-values from a chart that predates the key
    ({"checkSecretsOnUpgrade": None}, "true"),
    ({"checkSecretsOnUpgrade": True}, "true"),
    ({"checkSecretsOnUpgrade": False}, ""),
])
def test_only_an_explicit_false_turns_the_secret_check_off(tmp_path: Path, auth: dict, expected: str) -> None:
    """`helm upgrade --reuse-values` from 0.4.x carries the old release's values,
    where the key is absent; a plain truthiness test skipped the check on exactly
    the upgrade that names new Secrets (measured on k3s)."""
    template = 'problems: {{ include "victorialogs.checkSecretsOnUpgrade" . | quote }}\n'
    assert _probe(tmp_path, template, {"auth": auth}) == expected
