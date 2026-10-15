"""#2909: the try-local nightly must go red when recipe-preview is broken.

For 6 weeks the recipe-preview image could not import app.py (#2873) and the
nightly stayed green, through two separate gaps:

  * `try-local/smoke.sh` never touched recipe-preview at all;
  * the workflow's wait step only failed on a non-empty, non-"healthy"
    `.Health`, so a container with no health status — one that exited, was
    restarting, or was not listed by `docker compose ps` — passed.

Neither can be exercised for real here (no docker daemon in Python Tests). The
end-to-end proof is a `workflow_dispatch` run on the PR (see its body). What
this module pins is the LOGIC of both checks, run as the real scripts:

  * smoke.sh against a stub HTTP server that plays every endpoint it probes,
    with recipe-preview healthy or broken in the ways #2873 showed up;
  * scripts/ops/trylocal_wait_ready.sh against a stub `docker` on PATH that
    serves canned `compose config` / `compose ps` / `inspect` output.

Each failing case is paired with a passing one built from the same fixture, so
a red here means the check bit, not that the fixture never worked.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Resolved, not bare: on Windows a bare "bash" is the WSL launcher (#2560).
BASH = shutil.which("bash") or "bash"
SMOKE = REPO_ROOT / "try-local" / "smoke.sh"
WAIT = REPO_ROOT / "scripts" / "ops" / "trylocal_wait_ready.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32"
    or not all(shutil.which(t) for t in ("bash", "curl", "jq")),
    reason="needs bash, curl and jq (the scripts' own dependencies)",
)


# ── smoke.sh ────────────────────────────────────────────────────────────

GOOD_PREVIEW = {"healthz": "ok", "preview": "real"}


def _preview_verdict(body: dict, mode: str):
    """What the stub /preview answers. `real` mimics the service: firing when
    the scenario value crosses the recipe's threshold, inactive otherwise."""
    if mode == "500":
        return 500, {"error": "internal error"}
    if mode == "error":
        return 200, {"alertname": "Custom_x", "supported": True,
                     "states": [{"state": "error", "reason": "promtool eval error (rc=1)"}]}
    if mode == "always-firing":
        return 200, {"alertname": "Custom_x", "supported": True,
                     "states": [{"state": "firing", "reason": "stub"}]}
    if mode == "no-states":
        return 200, {"alertname": "Custom_x", "supported": True, "states": [],
                     "warnings": ["promtool not available"]}
    thr = float(body["recipe"]["threshold"].split(":")[0])
    value = float(body["scenario"]["value"])
    state = "firing" if value > thr else "inactive"
    return 200, {"alertname": "Custom_x", "supported": True,
                 "states": [{"state": state, "reason": "stub"}]}


def _make_handler(preview: dict, seen: list):
    class H(BaseHTTPRequestHandler):
        def _send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            seen.append(("GET", path))
            if path == "/api/v1/alerts":
                self._send(200, {"data": {"alerts": [
                    {"labels": {"severity": "critical"}, "state": "firing"}]}})
            elif path == "/api/v1/me":
                self._send(200, {"email": "dev@local"})
            elif path == "/api/v1/tenants":
                self._send(200, [{"id": "tenant-one"}, {"id": "tenant-two"}])
            elif path == "/":
                self._send(200, {})
            elif path == "/metrics":
                # #2903: tenant-api's view of the generator container.
                text = ("tenant_api_up 1\ntenant_api_policy_available "
                        f"{preview.get('generator', '1')}\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text)
            elif path == "/healthz":
                mode = preview["healthz"]
                if mode == "down":
                    self._send(503, {"error": "starting"})
                elif mode == "no-promtool":
                    self._send(200, {"status": "ok", "promtool": "absent"})
                else:
                    self._send(200, {"status": "ok", "promtool": "promtool, version 3"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append(("POST", path, body))
            if path != "/preview":
                self._send(404, {"error": "not found"})
                return
            self._send(*_preview_verdict(body, preview["preview"]))

        def log_message(self, *a):
            pass

    return H


def _run_smoke(tmp_path: Path, preview: dict):
    seen: list = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(preview, seen))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # A copy, so a developer's try-local/.env (which smoke.sh sources and
        # which would override the ports below) cannot leak in.
        script = tmp_path / "smoke.sh"
        shutil.copy(SMOKE, script)
        port = str(srv.server_address[1])
        env = {**os.environ, "SMOKE_HOST": "127.0.0.1", "SMOKE_ALERT_TIMEOUT": "5",
               "SMOKE_GENERATOR_TIMEOUT": "2",
               "EXPOSE_PROM_PORT": port, "EXPOSE_API_PORT": port,
               "EXPOSE_PORTAL_PORT": port, "EXPOSE_PREVIEW_PORT": port}
        proc = subprocess.run([BASH, str(script)], capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=60)
    finally:
        srv.shutdown()
        srv.server_close()
    return proc, seen


def test_smoke_passes_when_recipe_preview_answers_real_verdicts(tmp_path):
    proc, seen = _run_smoke(tmp_path, GOOD_PREVIEW)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL CHECKS PASSED" in proc.stdout
    posts = [s for s in seen if s[0] == "POST"]
    # Both verdicts are asked for, against a tenant taken from /api/v1/tenants.
    assert [p[2]["scenario"]["value"] for p in posts] == [1500, 1]
    assert {p[2]["tenant"] for p in posts} == {"tenant-one"}


@pytest.mark.parametrize("preview, needle", [
    ({"healthz": "down", "preview": "real"}, "/healthz returned HTTP 503"),
    ({"healthz": "no-promtool", "preview": "real"}, "reports no promtool"),
    # The service listens, but the compiler / promtool path is broken: a 500,
    # an `error` verdict, or no verdict. (#2873 itself never gets this far —
    # app.py fails at import, so nothing listens; see the connection-refused
    # test below.)
    ({"healthz": "ok", "preview": "500"}, "returned HTTP 500"),
    ({"healthz": "ok", "preview": "error"}, "gave 'error'"),
    ({"healthz": "ok", "preview": "no-states"}, "gave 'none'"),
    # One verdict is not enough: an always-firing answer must not pass.
    ({"healthz": "ok", "preview": "always-firing"}, "gave 'firing' (want inactive"),
])
def test_smoke_fails_when_recipe_preview_is_broken(tmp_path, preview, needle):
    proc, _ = _run_smoke(tmp_path, preview)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SMOKE FAIL" in proc.stderr and needle in proc.stderr, proc.stderr
    assert "ALL CHECKS PASSED" not in proc.stdout


def test_smoke_fails_when_the_generator_container_cannot_judge(tmp_path):
    """#2903: tenant-api answers, but its generator container does not (a
    different uid, not running, no amtool) — every write would answer 503,
    so the stack is not working even though every page loads."""
    proc, _ = _run_smoke(tmp_path, {**GOOD_PREVIEW, "generator": "0"})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "tenant_api_policy_available is not 1" in proc.stderr, proc.stderr


def test_smoke_fails_when_nothing_listens_on_the_preview_port(tmp_path):
    """#2873 as smoke.sh sees it: app.py fails at import, the container
    crash-loops, nothing listens — connection refused, no HTTP status."""
    seen: list = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(GOOD_PREVIEW, seen))
    dead = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(GOOD_PREVIEW, seen))
    dead_port = str(dead.server_address[1])
    dead.server_close()  # the port is now closed
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        script = tmp_path / "smoke.sh"
        shutil.copy(SMOKE, script)
        port = str(srv.server_address[1])
        env = {**os.environ, "SMOKE_HOST": "127.0.0.1", "SMOKE_ALERT_TIMEOUT": "5",
               "SMOKE_GENERATOR_TIMEOUT": "2",
               "EXPOSE_PROM_PORT": port, "EXPOSE_API_PORT": port,
               "EXPOSE_PORTAL_PORT": port, "EXPOSE_PREVIEW_PORT": dead_port}
        proc = subprocess.run([BASH, str(script)], capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=60)
    finally:
        srv.shutdown()
        srv.server_close()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "recipe-preview /healthz returned HTTP 000" in proc.stderr, proc.stderr


# ── scripts/ops/trylocal_wait_ready.sh ──────────────────────────────────

# A cut-down compose model: two long-running services (one with a healthcheck,
# one without) and one one-shot.
CONFIG = {"services": {
    "recipe-preview": {"restart": "unless-stopped"},
    "threshold-exporter": {"restart": "unless-stopped"},
    "seed-git": {"restart": "no"},
}}


def _ctr(service, status="running", health=None, restarts=0, exit_code=0):
    c = {"Config": {"Labels": {"com.docker.compose.service": service}},
         "RestartCount": restarts,
         "State": {"Status": status, "ExitCode": exit_code}}
    if health is not None:
        c["State"]["Health"] = {"Status": health}
    return c


HEALTHY = [
    _ctr("recipe-preview", health="healthy"),
    _ctr("threshold-exporter"),
    _ctr("seed-git", status="exited"),
]

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Test stub: serves canned output from $FAKE_DOCKER_DIR. `inspect` answers
# from inspect.<n>.json for the n-th poll (the last file repeats).
set -euo pipefail
d="$FAKE_DOCKER_DIR"
case "$*" in
  "compose config --format json") cat "$d/config.json" ;;
  "compose ps --all --quiet")
    n=$(cat "$d/polls" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/polls"
    f="$d/inspect.$n.json"; [ -f "$f" ] || f="$(ls "$d"/inspect.*.json | sort -V | tail -n 1)"
    cp "$f" "$d/current.json"
    jq -r 'keys[] | "id\(.)"' "$f" ;;
  inspect\ *) cat "$d/current.json" ;;
  *) echo "fake docker: unexpected args: $*" >&2; exit 99 ;;
esac
"""


def _run_wait(tmp_path: Path, *polls, timeout="0", config=CONFIG):
    d = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    d.mkdir()
    bin_dir.mkdir()
    (d / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for i, ctrs in enumerate(polls, 1):
        (d / f"inspect.{i}.json").write_text(json.dumps(ctrs), encoding="utf-8")
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "FAKE_DOCKER_DIR": str(d), "TRYLOCAL_WAIT_INTERVAL": "0"}
    return subprocess.run([BASH, str(WAIT), timeout], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60, cwd=tmp_path)


def test_wait_passes_when_every_service_is_ready(tmp_path):
    proc = _run_wait(tmp_path, HEALTHY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all try-local services ready" in proc.stdout
    # The one-shot exception is derived from compose, and printed.
    assert 'one-shot (restart: "no"): seed-git' in proc.stdout


def test_wait_keeps_waiting_until_a_starting_service_turns_healthy(tmp_path):
    starting = [_ctr("recipe-preview", health="starting")] + HEALTHY[1:]
    no_ctr_yet = HEALTHY[1:]
    proc = _run_wait(tmp_path, no_ctr_yet, starting, HEALTHY, timeout="60")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("broken, needle", [
    # #2873's shape: crash-looping, so no health status at all — the old
    # predicate `.Health != "" and .Health != "healthy"` let exactly this pass.
    (_ctr("recipe-preview", status="restarting", restarts=3),
     "recipe-preview is not running: restarted 3 time(s)"),
    # Caught between restarts: "running", but it has restarted.
    (_ctr("recipe-preview", status="running", health="starting", restarts=1),
     "recipe-preview is not running: restarted 1 time(s)"),
    # A long-running service with no healthcheck that exited.
    (_ctr("threshold-exporter", status="exited", exit_code=2),
     "threshold-exporter is not running: state exited"),
])
def test_wait_fails_on_a_long_running_service_that_is_not_running(tmp_path, broken, needle):
    ctrs = [broken] + [c for c in HEALTHY
                       if c["Config"]["Labels"]["com.docker.compose.service"]
                       != broken["Config"]["Labels"]["com.docker.compose.service"]]
    # A generous timeout: these must fail on the first poll, not by timing out.
    proc = _run_wait(tmp_path, ctrs, timeout="60")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert needle in proc.stdout, proc.stdout


def test_wait_waits_on_a_created_container_instead_of_failing(tmp_path):
    created = [_ctr("recipe-preview", status="created")] + HEALTHY[1:]
    proc = _run_wait(tmp_path, created, HEALTHY, timeout="60")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wait_fails_when_a_service_never_gets_a_container(tmp_path):
    proc = _run_wait(tmp_path, HEALTHY[1:])  # no recipe-preview container
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "not ready within 0s: recipe-preview" in proc.stdout, proc.stdout


def test_wait_fails_when_a_healthcheck_never_passes(tmp_path):
    ctrs = [_ctr("recipe-preview", health="unhealthy")] + HEALTHY[1:]
    proc = _run_wait(tmp_path, ctrs)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "not ready within 0s: recipe-preview" in proc.stdout, proc.stdout


def test_wait_fails_when_a_one_shot_exits_non_zero(tmp_path):
    ctrs = HEALTHY[:2] + [_ctr("seed-git", status="exited", exit_code=1)]
    proc = _run_wait(tmp_path, ctrs, timeout="60")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "seed-git is not running: one-shot exited 1" in proc.stdout, proc.stdout


def test_an_exited_service_is_only_excused_when_compose_says_restart_no(tmp_path):
    """The exception list is derived from compose: the same exited-0 container
    fails once its service is no longer `restart: "no"`."""
    config = {"services": {**CONFIG["services"], "seed-git": {"restart": "unless-stopped"}}}
    proc = _run_wait(tmp_path, HEALTHY, config=config, timeout="60")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "seed-git is not running: state exited" in proc.stdout, proc.stdout


def test_the_nightly_runs_this_wait_script_and_smoke():
    """The two scripts above only guard the nightly if it runs them."""
    import yaml

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "try-local-smoke.yaml")
                        .read_text(encoding="utf-8"))
    steps = wf["jobs"]["smoke"]["steps"]
    runs = [(s.get("working-directory"), s.get("run", "").strip()) for s in steps]
    assert ("try-local", "bash ../scripts/ops/trylocal_wait_ready.sh 300") in runs, runs
    assert ("try-local", "bash smoke.sh") in runs, runs
    names = [s.get("name", "") for s in steps]
    assert names.index("Run smoke.sh") > next(
        i for i, s in enumerate(steps) if "trylocal_wait_ready.sh" in s.get("run", ""))
    # Neither step may be softened into a non-gate.
    for s in steps:
        if "trylocal_wait_ready.sh" in s.get("run", "") or s.get("run", "").strip() == "bash smoke.sh":
            assert not s.get("continue-on-error"), s
            assert "if" not in s, s
