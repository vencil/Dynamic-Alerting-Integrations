"""test_federation_audit_metrics.py — the gateway's audit-metrics sidecar (#1278 D1).

The federation-gateway turns its Envoy audit log (sink 2) into three metrics:
``tenant_federation_requests_total{tenant,status}`` and the ADR-021 pair
``tenant_log_query_requests_total`` / ``tenant_log_query_duration_ms``. That was
an mtail program until #1278 D1 replaced it with upstream Vector running
``helm/federation-gateway/files/audit-metrics.vector.yaml``.

⭐ This module RUNS the shipped program. The gate is behavioural, not a compile
check: it starts the Vector binary on the exact file the chart embeds, appends
Envoy-shaped lines, scrapes /metrics and compares every series. The expected
table below is the mtail program's semantics, written out: at the swap the two
implementations were run side by side on the same lines and produced the same 26
series (mtail's automatic ``prog`` label aside). So a red here means the metric
contract moved — dashboards, the runbooks and the chargeback readme read it.

Locally without ``vector`` the runtime tests skip; CI's Python Tests job installs
the same Vector version the chart deploys and sets ``VIBE_REQUIRE_VECTOR=1``, so
there a missing binary FAILS (test_vector_projection_vrl.py owns that guard).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CHART = _REPO / "helm" / "federation-gateway"
_PROGRAM = _CHART / "files" / "audit-metrics.vector.yaml"
_CONFIGMAP = _CHART / "templates" / "configmap-audit-metrics.yaml"
_VALUES = _CHART / "values.yaml"
_VECTOR_VALUES = _REPO / "helm" / "vector" / "values.yaml"
_DOCKERFILE = _CHART / "audit-sidecar" / "Dockerfile"

_needs_vector = pytest.mark.skipif(shutil.which("vector") is None, reason="vector not on PATH")

# The only environment the program may read. The sidecar's env is exactly these
# plus the interpolation switch, which is what makes enabling that switch safe.
_ENV_NAMES = {"AUDIT_LOG_PATH", "AUDIT_METRICS_PORT", "AUDIT_METRICS_DATA"}


# ── structure (runs everywhere) ─────────────────────────────────────────────

def test_the_chart_embeds_the_program_verbatim() -> None:
    """The ConfigMap must carry the file through Files.Get, never tpl: the
    program's double-brace field templates would be eaten by Helm."""
    text = _CONFIGMAP.read_text(encoding="utf-8")
    assert '.Files.Get "files/audit-metrics.vector.yaml"' in text
    calls = re.findall(r"\{\{-?([^}]*)\}\}", text)
    assert not [c for c in calls if re.search(r"\btpl\b", c)], f"tpl in template actions: {calls}"


def test_the_program_reads_only_its_three_env_vars() -> None:
    """Vector interpolates env vars anywhere in the file — comments included —
    once VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION is set. Any other name
    is either a startup failure (unset) or a way to pull a secret into config."""
    names = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", _PROGRAM.read_text(encoding="utf-8")))
    assert names == _ENV_NAMES, f"env references in the program: {sorted(names)}"


def test_the_sidecar_env_is_exactly_what_the_program_reads() -> None:
    """Rendered-template check without helm: the deployment sets the switch and
    the three names, and nothing else, on the audit-metrics container."""
    deploy = (_CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    block = deploy.split("- name: audit-metrics\n", 1)[1].split("\n        - name: ", 1)[0]
    env = set(re.findall(r"- name: ([A-Z_]+)\n", block))
    assert env == _ENV_NAMES | {"VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION"}, sorted(env)


def test_the_metrics_image_is_helm_vectors_image() -> None:
    """One Vector on the platform: same repository, tag and digest as
    helm/vector. The nightly scan matrix is set-equal to the deployed refs, so a
    second Vector ref would need its own row; Renovate bumps both in one PR."""
    ours = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))["auditLog"]["metrics"]["image"]
    theirs = yaml.safe_load(_VECTOR_VALUES.read_text(encoding="utf-8"))["image"]
    for key in ("repository", "tag", "digest"):
        assert ours[key] == theirs[key], f"auditLog.metrics.image.{key}: {ours[key]!r} != helm/vector {theirs[key]!r}"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", ours["digest"]), ours["digest"]


# ── behaviour (needs the vector binary) ─────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scrape(port: int) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
        text = r.read().decode("utf-8")
    out = {}
    for line in text.splitlines():
        if not line.startswith("tenant_"):
            continue
        m = re.fullmatch(r"(\w+)(?:\{(.*)\})?\s+(\S+)", line)
        assert m, f"unparsed exposition line: {line!r}"
        labels = tuple(sorted(re.findall(r'(\w+)="([^"]*)"', m.group(2) or "")))
        out[(m.group(1), labels)] = float(m.group(3))
    return out


def _fed(tenant: str, status: str):
    return ("tenant_federation_requests_total", (("status", status), ("tenant", tenant)))


def _lq(account: str, status: str):
    return ("tenant_log_query_requests_total",
            (("account_id", account), ("project_id", "0"), ("status", status)))


def _lqd(account: str, suffix: str, le: str | None = None):
    labels = [("account_id", account), ("project_id", "0")]
    if le is not None:
        labels.append(("le", le))
    return (f"tenant_log_query_duration_ms_{suffix}", tuple(sorted(labels)))


# Envoy json_format lines (files/envoy.yaml `&audit_json`), one per case.
_LINES = [
    '{"ts":"t01","tenant_id":"db-x","status":200,"duration_ms":12,"account_id":null}',
    '{"ts":"t02","tenant_id":"db-x","status":"200","duration_ms":7}',   # quoted status
    '{"ts":"t03","tenant_id":"db-x","status":429}',
    '{"ts":"t04","tenant_id":"db-y","status":403}',
    '{"ts":"t05","tenant_id":"db-y","status":0}',                        # client abort
    '{"ts":"t06","tenant_id":"db-y","status":499}',
    '{"ts":"t07","tenant_id":"db-y","status":422}',
    '{"ts":"t08","tenant_id":"db-y","status":503}',
    '{"ts":"t09","tenant_id":"db-y","status":301}',                      # 3xx: no bucket
    '{"ts":"t10","tenant_id":null,"status":401}',                        # no tenant claim
    '{"ts":"t11","tenant_id":"lq","account_id":"1234","status":200,"duration_ms":"30"}',
    '{"ts":"t12","tenant_id":"lq","account_id":1234,"status":500,"duration_ms":3000}',
    '{"ts":"t13","tenant_id":"lq","account_id":"","status":200,"duration_ms":5}',  # no partition
    "not json at all",
]

_BUCKETS = ["5", "10", "25", "50", "100", "250", "500", "1000", "2500", "5000",
            "10000", "15000", "25000", "+Inf"]
_EXPECTED = {
    _fed("db-x", "ok"): 2, _fed("db-x", "rate_limited"): 1,
    _fed("db-y", "auth_failed"): 1, _fed("db-y", "client_aborted"): 2,
    _fed("db-y", "bad_request"): 1, _fed("db-y", "backend_error"): 1,
    _fed("lq", "ok"): 2, _fed("lq", "backend_error"): 1,
    _lq("1234", "ok"): 1, _lq("1234", "backend_error"): 1,
    _lqd("1234", "count"): 2, _lqd("1234", "sum"): 3030,
    # durations 30 and 3000 ms, cumulative buckets
    **{_lqd("1234", "bucket", le): (0 if int(le if le != "+Inf" else 10**9) < 30
                                    else 1 if int(le if le != "+Inf" else 10**9) < 3000 else 2)
       for le in _BUCKETS},
}


@pytest.fixture()
def vector_run(tmp_path: Path):
    """Start Vector on the shipped program; yield (log path, port); always stop it."""
    log_dir = tmp_path / "log"
    data = tmp_path / "data"
    log_dir.mkdir()
    data.mkdir()
    log = log_dir / "audit.log"
    log.write_text("", encoding="utf-8")
    port = _free_port()
    env = {**os.environ,
           "VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION": "true",
           "AUDIT_LOG_PATH": str(log), "AUDIT_METRICS_PORT": str(port),
           "AUDIT_METRICS_DATA": str(data)}
    stderr = tmp_path / "vector.stderr"
    with stderr.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(["vector", "--config", str(_PROGRAM), "--quiet"],
                                env=env, stdout=subprocess.DEVNULL, stderr=err)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    _scrape(port)
                    break
                except OSError:
                    assert proc.poll() is None, (
                        f"vector exited rc={proc.returncode}:\n{stderr.read_text(encoding='utf-8')}")
                    assert time.monotonic() < deadline, "vector /metrics never came up"
                    time.sleep(0.2)
            yield log, port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def _settle(port: int, want: dict, timeout: float = 20.0) -> dict:
    """Poll until /metrics equals `want` (the file source is asynchronous)."""
    deadline = time.monotonic() + timeout
    got = _scrape(port)
    while got != want and time.monotonic() < deadline:
        time.sleep(0.25)
        got = _scrape(port)
    return got


@_needs_vector
def test_every_series_matches_the_metric_contract(vector_run) -> None:
    log, port = vector_run
    _append(log, _LINES)
    got = _settle(port, _EXPECTED)
    missing = {k: v for k, v in _EXPECTED.items() if got.get(k) != v}
    extra = {k: v for k, v in got.items() if k not in _EXPECTED}
    assert not missing and not extra, (
        f"metric contract drifted.\n  wrong/missing: {missing}\n  unexpected: {extra}")


@_needs_vector
def test_a_logrotate_cycle_loses_and_repeats_nothing(vector_run) -> None:
    """templates/configmap-logrotate.yaml rotates by RENAME, then POSTs Envoy's
    /reopen_logs. Between the two, Envoy still writes into the renamed file. All
    three phases must be counted exactly once. (mtail dropped the post-reopen
    phase in the side-by-side run; Vector counts all 100.)"""
    log, port = vector_run
    _append(log, [f'{{"ts":"a{i:03d}","tenant_id":"before","status":200}}' for i in range(50)])
    _settle(port, {_fed("before", "ok"): 50})
    rotated = log.with_name(log.name + ".1")
    log.rename(rotated)
    _append(rotated, [f'{{"ts":"b{i:03d}","tenant_id":"late","status":200}}' for i in range(30)])
    time.sleep(1)
    _append(log, [f'{{"ts":"c{i:03d}","tenant_id":"reopened","status":200}}' for i in range(20)])
    want = {_fed("before", "ok"): 50, _fed("late", "ok"): 30, _fed("reopened", "ok"): 20}
    assert _settle(port, want) == want


# ── logrotate image: recipe ↔ tag (#1337) ───────────────────────────────────
# The chart pins the logrotate image as `alpine<version>-<build revision>`. There
# is NO digest knob for that container and the pod template's checksum
# annotations hash only ConfigMaps, so if the recipe changes and the tag does not,
# `helm upgrade` renders a byte-identical pod spec: no rollout, and with the
# default `IfNotPresent` the node keeps serving the previous build while the
# nightly scan reports the new one. The digest covers the Dockerfile's
# INSTRUCTION lines only (comments and blanks stripped).
_RECIPE_DIGEST = "b036803fac522f2ff153f766e840c9e014dc8f0113efb1201f13d2cda72b18f6"
_EXPECTED_TAG = "alpine3.23.6-1"


def _recipe_digest(dockerfile: Path) -> str:
    lines = [ln.rstrip() for ln in dockerfile.read_text(encoding="utf-8").splitlines()]
    body = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert body, "no instruction lines parsed out of the Dockerfile — parser broken"
    return hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()


def test_sidecar_recipe_change_forces_a_tag_bump() -> None:
    """Recipe and image tag must move together — the chart has no other lever."""
    tag = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))["auditLog"]["image"]["tag"]
    froms = re.findall(r"^FROM\s+alpine:(\S+)\s*$", _DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert len(froms) == 1, f"expected one `FROM alpine:<version>`, found {froms}"
    assert tag == _EXPECTED_TAG, (
        f"auditLog.image.tag is {tag!r}, this guard expects {_EXPECTED_TAG!r}. If you "
        "bumped the tag deliberately, update _EXPECTED_TAG (and _RECIPE_DIGEST).")
    assert tag.startswith(f"alpine{froms[0]}-"), (
        f"tag {tag!r} must be 'alpine<version>-<build revision>' and the Dockerfile "
        f"builds FROM alpine:{froms[0]}")
    actual = _recipe_digest(_DOCKERFILE)
    assert actual == _RECIPE_DIGEST, (
        "the audit-sidecar Dockerfile's INSTRUCTIONS changed "
        f"({_RECIPE_DIGEST[:12]}… -> {actual[:12]}…).\n"
        "⛔ Bump auditLog.image.tag in the same change, then update _EXPECTED_TAG "
        "and _RECIPE_DIGEST here. Leaving the tag alone renders a byte-identical "
        "pod spec: no rollout, and `IfNotPresent` keeps the OLD image on every node.")
