#!/usr/bin/env python3
"""
TRK-222: schemathesis contract tests for tenant-api.

Spins up tenant-api against a temp config dir, runs schemathesis against
the OpenAPI spec at components/tenant-api/docs/swagger.json, and tears
down. CI / local entry point: `make contract-test`.

Why this script (not direct schemathesis CLI):
  - tenant-api needs a running instance (not file-based testing) since
    the API depends on filesystem state (conf.d/) and async tasks.
  - We need temp dirs / clean state per run.
  - Schemathesis output should be deterministic and capturable.

Fixture design (full-method fuzz):
  - The temp config dir is a throwaway git repo (`git init` + initial
    commit). tenant-api is commit-on-write (internal/gitops/writer.go):
    without a repo every write path 500s on `git commit`. Committer
    identity uses inline `-c user.name/email` fallback to the author,
    so no global git config is required — only the initial commit here
    needs the local repo config.
  - ADR-027 §2, §3: the same topology as production and try-local. The API
    is served only on tenant-api's Unix socket; an oauth2-proxy (the image and
    digest the tenant-api chart pins, `docker run`; or OAUTH2_PROXY_BIN) runs
    in front of it with every path exempt from login, from the structured
    config file try-local uses (try-local/oauth2-proxy/oauth2-proxy.yaml), and
    tenant-api reads the same file. No request carries an identity: the dev
    bypass (ADR-022) injects the fuzz identity on the socket plane.
    schemathesis talks to oauth2-proxy; the TCP port is only polled for
    /health.
  - `_rbac.yaml` grants the fuzz group wildcard read+write+admin so the
    authz layer passes and fuzzing exercises handler/validation logic,
    not just 401/403 short-circuits.
  - The generator container's service runs beside tenant-api (#2903,
    ADR-036 §3): tenant-api refuses to start without --generator-socket, and
    every write asks it before committing. It is the REAL service
    (scripts/tools/ops/generator_service.py, the da-tools image's code) over
    the same repo, as the same uid, with the reader binaries it runs built
    from this checkout (build_readers).
  - TA_RATE_LIMIT_PER_MIN=0 disables the per-caller rate limiter
    (cmd/server/main.go); hundreds of fuzz requests/minute from one
    caller identity would otherwise trip 429s unrelated to the spec.
  - `_domain_policy.yaml` lives INSIDE conf.d/ and binds the seed tenant
    to a domain that forbids one receiver type. tenant-api does not read
    it (#2903, ADR-036 §4): the route generator judges every write on the
    tree it produces, the policy included, and refuses a violation with
    422 GENERATOR_FINDINGS. Because domain→tenant matching is an exact
    tenant list (no wildcard) and a fuzzed body rarely hits
    `_routing.receiver.type` on the seed tenant, a DETERMINISTIC smoke
    (run_policy_gate_smoke) fires that refusal before the fuzz — the 422
    path never depends on fuzz luck.

Skipped checks (and why — see also docs/internal/testing-playbook.md):
  - `auth-required`: identity comes from the fronting oauth2-proxy, which
    this fixture runs without login (the dev bypass supplies the caller).
    Auth is covered by Go-level handler tests.

Excluded operations (known gaps + reopen conditions):
  - /api/v1/federation/tokens* + /api/v1/federation/accounts/backfill
    (4 ops): registered only when --federation-key is set
    (cmd/server/routes.go `deps.Federation != nil`), and the token
    store requires an in-cluster Kubernetes ConfigMap — not startable
    in this local fixture. Reopen when a file-backed federation record
    store (or a fake ConfigMap seam) exists; until then these ops are
    covered by Go-level handler tests with a stubbed store.
  - GET /api/v1/prs (1 op): registered only in PR write-mode
    (`deps.PRTracker != nil`), which needs a forge token; the fixture
    runs write-mode=direct so the op 404s. Reopen if the fixture grows
    a stub forge backend.
  - POST /api/v1/tenants/batch (1 op): its body is generation-hostile —
    hypothesis filters out nearly every example and raises
    `filter_too_much`, which schemathesis 4.x reports as an engine-level
    error (exit 1) that `--suppress-health-check` does not cover, so the
    op flakily reds the run without ever fuzzing (#1158). Covered by
    Go handler tests. Reopen when
    the batch schema is widened / given examples so it is fuzzable.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

def _find_repo_root() -> Path:
    """Locate repo root regardless of where this script lives.

    Order: $REPO_ROOT env, then walk up from this file until we hit a dir
    containing components/tenant-api/, then fall back to cwd. The walk
    matters because CI / dev container paths may not match the layout
    where this file was checked in.
    """
    env_root = os.environ.get("REPO_ROOT")
    if env_root and (Path(env_root) / "components" / "tenant-api").is_dir():
        return Path(env_root).resolve()
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "components" / "tenant-api").is_dir():
            return parent
    return Path.cwd().resolve()


REPO_ROOT = _find_repo_root()
TENANT_API_DIR = REPO_ROOT / "components" / "tenant-api"
DA_GUARD_DIR = REPO_ROOT / "components" / "threshold-exporter" / "app"
SWAGGER_JSON = TENANT_API_DIR / "docs" / "swagger.json"
GENERATOR_SERVICE = REPO_ROOT / "scripts" / "tools" / "ops" / "generator_service.py"


def _go_build_argv(module_dir: Path, package: str, output: Path) -> list:
    """#3425: the shipped shape — the build flags live only in
    scripts/ops/go_build.sh, and tests/_go_build.py builds the argv for it."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        from _go_build import go_build_argv
    finally:
        sys.path.pop(0)
    return go_build_argv(module_dir, package, output)


def find_free_port() -> int:
    """Pick an unused TCP port for the test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout: float = 30.0) -> None:
    """Poll /health until 200 or timeout. Raises on timeout."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
            last_err = e
        time.sleep(0.2)
    raise TimeoutError(f"tenant-api not ready at {url} after {timeout}s; last err: {last_err}")


# IdP group granted wildcard access in the fuzz _rbac.yaml; the dev bypass
# injects it on every request (ADR-027 §3).
FUZZ_GROUP = "contract-fuzz-admins"

# Caller identity for every request (schemathesis AND the deterministic
# policy smoke) — one identity, so TA_RATE_LIMIT_PER_MIN=0 stays required.
FUZZ_EMAIL = "schemathesis@example.com"

# ADR-027 §3: the structured oauth2-proxy config the dev topologies share.
TRY_LOCAL_PROXY_CONFIG = REPO_ROOT / "try-local" / "oauth2-proxy" / "oauth2-proxy.yaml"
# The oauth2-proxy image (repository, tag, digest) the tenant-api chart pins.
TENANT_API_VALUES = REPO_ROOT / "helm" / "tenant-api" / "values.yaml"

# Neutral fixture tenant (dev-rule #2: no real tenant ids in fixtures).
# Seeded in conf.d so list endpoints have ≥1 tenant, and listed in the
# domain policy fixture so the generator's policy check can match a tenant
# (an exact-list match, no wildcard).
SEED_TENANT = "db-seed"

# Domain policy fixture knobs. webhook is forbidden / email is allowed —
# both are real receiver types from tenant-config.schema.json, so the
# violating body is schema-valid and only the policy can reject it (a 400
# would mean the smoke broke, not the policy).
POLICY_DOMAIN = "contract-fixture-domain"
POLICY_FORBIDDEN_RECEIVER = "webhook"
POLICY_ALLOWED_RECEIVER = "email"


def run_git(config_dir: Path, *args: str) -> None:
    """Run a git command inside the temp config dir (check=True)."""
    subprocess.run(["git", "-C", str(config_dir), *args], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def init_git_repo(config_dir: Path) -> None:
    """Turn the temp config dir into a git repo with an initial commit.

    tenant-api's write plane is commit-on-write (gitops.Writer): `git add`
    + `git commit` run against --config-dir (its default --git-dir). The
    Writer's own commits use inline `-c user.name/user.email`, but OUR
    initial commit needs a local identity — set repo-local config so the
    runner works on hosts/containers without global git config.
    """
    run_git(config_dir, "init")
    run_git(config_dir, "config", "user.name", "contract-fixture")
    run_git(config_dir, "config", "user.email", "contract-fixture@example.com")
    run_git(config_dir, "add", "-A")
    run_git(config_dir, "commit", "-m", "contract-test fixture: initial state")


def write_rbac_fixture(workdir: Path) -> Path:
    """Write the fuzz _rbac.yaml and return its path.

    One group, wildcard tenants, full permissions (admin ⊇ write ⊇ read):
    the goal is response-contract conformance of the real handler paths,
    so authz must not short-circuit everything to 401/403. The group
    `name` IS the matched IdP group (legacy shape, internal/rbac/rbac.go)
    and must equal the X-Forwarded-Groups value sent below.
    Kept OUTSIDE conf.d/ so the RBAC policy file is not part of the
    tenant-config tree the fuzz mutates.
    """
    rbac_path = workdir / "_rbac.yaml"
    rbac_path.write_text(
        "groups:\n"
        f"  - name: {FUZZ_GROUP}\n"
        "    tenants: [\"*\"]\n"
        "    permissions: [read, write, admin]\n"
    )
    return rbac_path


def write_domain_policy_fixture(config_dir: Path) -> None:
    """Write conf.d/_domain_policy.yaml binding SEED_TENANT to a domain.

    Shape mirrors the real example
    (components/threshold-exporter/config/conf.d/examples/_domain_policy.yaml):
    forbidden_receiver_types + allowed_receiver_types on one domain. Must
    live INSIDE conf.d: the route generator reads it from the tree it
    judges (#2903). The scanner skips `_`-prefixed files
    (internal/handler/tenant_list.go), so the file never shows up as a
    tenant.
    """
    (config_dir / "_domain_policy.yaml").write_text(
        "domain_policies:\n"
        f"  {POLICY_DOMAIN}:\n"
        "    description: \"Contract-test fixture: the route generator's policy check\"\n"
        f"    tenants: [{SEED_TENANT}]\n"
        "    constraints:\n"
        f"      allowed_receiver_types: [{POLICY_ALLOWED_RECEIVER}, pagerduty]\n"
        f"      forbidden_receiver_types: [{POLICY_FORBIDDEN_RECEIVER}, slack]\n"
    )


def _tenant_body(receiver_yaml: str) -> bytes:
    """Tenant-only PUT body (conf.d/{id}.yaml shape) with a _routing receiver.

    Deliberately ONLY `_routing`: this fixture ships no defaults, so a
    plain metric key (cpu, ...) is one no reader reads, which the write
    gate's shared key verdict refuses (ADR-038 §11) before proving anything
    about the policy gate.
    `_`-prefixed structural keys skip that resolution.
    """
    return (
        "tenants:\n"
        f"  {SEED_TENANT}:\n"
        "    _routing:\n"
        "      receiver:\n"
        f"{receiver_yaml}"
    ).encode()


def _put_tenant(base_url: str, body: bytes) -> tuple[int, str, bytes]:
    """PUT the seed tenant through oauth2-proxy (the dev bypass supplies the
    caller); never raises on 4xx/5xx."""
    req = urllib.request.Request(
        f"{base_url}/api/v1/tenants/{SEED_TENANT}",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/yaml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def run_policy_gate_smoke(base_url: str) -> bool:
    """Deterministic contract case for a write the domain policy refuses.

    schemathesis almost never generates a body that sets
    `_routing.receiver.type` on the seed tenant, so without this the
    policy refusal would only be exercised by fuzz luck. Two fixed
    requests, run BEFORE the fuzz (fail fast, and immune to whatever
    state the fuzz leaves behind):

      1. violating PUT (forbidden receiver type) → MUST be 422 with the
         ErrorResponse envelope: code=GENERATOR_FINDINGS + findings[]
         holding the route generator's finding for the seed tenant's
         policy (#2903: tenant-api no longer judges the policy itself;
         the generator does, on the tree the write produces). The body
         is schema-valid apart from the policy, so a 400 here means the
         smoke itself broke — not a pass.
      2. control PUT (allowed receiver type) → MUST be 200. Guards
         against a miswired fixture where the policy (or the file's mere
         presence in conf.d) blocks every write and the fuzz would
         silently degrade into an all-422 run.
    """
    ok = True

    status, ctype, raw = _put_tenant(base_url, _tenant_body(
        f"        type: {POLICY_FORBIDDEN_RECEIVER}\n"
        "        url: https://alerts.invalid/contract-fixture\n"
    ))
    detail = f"status={status} content-type={ctype} body={raw[:400]!r}"
    if status != 422:
        print(f"[contract] FAIL policy smoke: violating PUT expected 422, got {detail}")
        ok = False
    else:
        try:
            body = json.loads(raw)
        except ValueError:
            print(f"[contract] FAIL policy smoke: 422 body is not JSON: {detail}")
            return False
        findings = body.get("findings") or []
        if body.get("code") != "GENERATOR_FINDINGS":
            print(f"[contract] FAIL policy smoke: expected code=GENERATOR_FINDINGS, got {detail}")
            ok = False
        elif not body.get("error") or not findings:
            print(f"[contract] FAIL policy smoke: 422 missing error/findings: {detail}")
            ok = False
        elif not any(
            f.get("tenant") == SEED_TENANT
            and f.get("related") is True
            and f"domain_policy '{POLICY_DOMAIN}'" in (f.get("message") or "")
            and f"'{POLICY_FORBIDDEN_RECEIVER}' is forbidden" in (f.get("message") or "")
            for f in findings
        ):
            print(f"[contract] FAIL policy smoke: no related finding of the domain policy: {detail}")
            ok = False
        elif not ctype.startswith("application/json"):
            print(f"[contract] FAIL policy smoke: 422 content-type not JSON: {detail}")
            ok = False

    status, _, raw = _put_tenant(base_url, _tenant_body(
        f"        type: {POLICY_ALLOWED_RECEIVER}\n"
        "        to: oncall@example.com\n"
        "        smarthost: smtp.example.invalid:587\n"
        # The route generator refuses a receiver Alertmanager cannot load
        # (email without `from`), so the control must be a complete one.
        "        from: alerts@example.com\n"
    ))
    if status != 200:
        print(f"[contract] FAIL policy smoke: control PUT (allowed receiver) expected 200, "
              f"got status={status} body={raw[:400]!r}")
        ok = False

    if ok:
        print("[contract] policy smoke OK: violating PUT → 422 GENERATOR_FINDINGS, control PUT → 200")
    return ok


def build_readers(workdir: Path) -> dict:
    """Every reader the generator service execs, as its environment: da-guard
    (conf.d, ADR-036 step 4), da-crdecode and da-read (the shipped platform
    rules). Each is `$<VAR>` when set, else built from this checkout. The list
    and where each comes from is `_lib_consumer_read.READER_BUILD_SOURCES` —
    without one reader the service fails every write with exit 2
    (run_failed), which is how the policy smoke broke when the list lived in
    a workflow instead (#3343)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
    try:
        from _lib_consumer_read import READER_BUILD_SOURCES
    finally:
        sys.path.pop(0)
    env = {}
    for var, (module, package, name) in READER_BUILD_SOURCES.items():
        preset = os.environ.get(var, "")
        if preset.strip():  # a blank value counts as unset, as _lib_parse_tree reads it
            env[var] = preset
            continue
        binary = workdir / name
        print(f"[contract] building {name} → {binary}")
        subprocess.run(_go_build_argv(REPO_ROOT / module, package, binary),
                       check=True, timeout=180)
        env[var] = str(binary)
    return env


def start_generator(config_dir: Path, workdir: Path, log_fh) -> tuple[subprocess.Popen, str]:
    """Start the generator service over config_dir (the repository root, as
    the chart mounts it) and wait until its status endpoint answers ready.

    The socket lives in a short temp dir (a Unix socket path is limited to
    ~107 bytes).
    Returns the process and the socket path; the caller stops the process.
    """
    sock_dir = Path(tempfile.mkdtemp(prefix="cg"))
    sock = str(sock_dir / "g.sock")
    env = {**os.environ, **build_readers(workdir)}
    proc = subprocess.Popen(
        [sys.executable, str(GENERATOR_SERVICE), "--socket", sock, "--repo", str(config_dir),
         "--config-subdir", ".", "--work-dir", str(workdir / "generator-work")],
        env=env, stdout=log_fh, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"generator service exited with code {proc.returncode}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(2.0)
                c.connect(sock)
                c.sendall(b"GET /v1/status HTTP/1.1\r\nHost: g\r\n\r\n")
                raw = b""
                while chunk := c.recv(65536):
                    raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            if head.startswith(b"HTTP/1.1 200 ") and json.loads(body).get("ready"):
                return proc, sock
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    proc.kill()
    proc.wait()
    raise TimeoutError("generator service not ready after 30s")


def oauth2_proxy_image() -> str:
    """The oauth2-proxy image the tenant-api chart pins, tag and digest."""
    with TENANT_API_VALUES.open(encoding="utf-8") as fh:
        img = yaml.safe_load(fh)["oauth2Proxy"]["image"]
    return f"{img['repository']}:{img['tag']}@{img['digest']}"


def write_proxy_config(workdir: Path, upstream_socket: str) -> Path:
    """try-local's oauth2-proxy config with this run's listener and socket.

    Only the two fields that depend on where things run change; the
    injectRequestHeaders tenant-api trusts are the try-local file's.
    """
    cfg = yaml.safe_load(TRY_LOCAL_PROXY_CONFIG.read_text(encoding="utf-8"))
    cfg["server"]["bindAddress"] = "0.0.0.0:4180"
    [api] = [u for u in cfg["upstreamConfig"]["upstreams"] if u["path"] == "/api/"]
    api["uri"] = f"unix://{upstream_socket}"
    cfg_dir = workdir / "oauth2-proxy"
    cfg_dir.mkdir(exist_ok=True)
    path = cfg_dir / "oauth2-proxy.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8", newline="\n")
    os.chmod(path, 0o644)
    return path


# oauth2-proxy flags of the dev topology (ADR-027 §3), the same as
# try-local's: every request is exempt from login, so no session exists and
# nothing is injected; client copies of the injected headers are still
# stripped. Exempt by trusted source address (all of them), not by
# --skip-auth-route: oauth2-proxy refuses a path exemption for a path it finds
# ambiguous (a control character, `;`, `//`), which the fuzz generates, and
# would answer those with its sign-in page instead of passing them on.
PROXY_FLAGS = [
    "--trusted-ip=0.0.0.0/0",
    "--trusted-ip=::/0",
    "--email-domain=*",
    "--cookie-secret=contract-not-a-secret-32-bytes!!",
    "--cookie-secure=false",
]


def start_oauth2_proxy(workdir: Path, sock_dir: Path, port: int, log_fh) -> tuple[list, subprocess.Popen]:
    """Start oauth2-proxy in front of tenant-api's socket on 127.0.0.1:port.

    OAUTH2_PROXY_BIN set → that binary, with the socket's host path. Else
    `docker run` of the chart's pinned image, as this uid (the socket is
    0660) with the socket directory mounted at /run/ta-human. Returns the
    argv that stops it ([] for a plain process) and the process.
    """
    binary = os.environ.get("OAUTH2_PROXY_BIN", "").strip()
    if binary:
        cfg = write_proxy_config(workdir, str(sock_dir / "human.sock"))
        argv = [binary, f"--alpha-config={cfg}", *PROXY_FLAGS]
        # bindAddress comes from the file; point it at this run's port.
        text = cfg.read_text(encoding="utf-8").replace("0.0.0.0:4180", f"127.0.0.1:{port}")
        cfg.write_text(text, encoding="utf-8", newline="\n")
        return [], subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT)
    if not shutil.which("docker"):
        raise RuntimeError("neither OAUTH2_PROXY_BIN nor docker is available to run oauth2-proxy")
    cfg = write_proxy_config(workdir, "/run/ta-human/human.sock")
    name = f"contract-oauth2-proxy-{os.getpid()}"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{sock_dir}:/run/ta-human",
        "-v", f"{cfg.parent}:/etc/oauth2-proxy:ro",
        "-p", f"127.0.0.1:{port}:4180",
        oauth2_proxy_image(),
        "--alpha-config=/etc/oauth2-proxy/oauth2-proxy.yaml", *PROXY_FLAGS,
    ]
    return ["docker", "rm", "-f", name], subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT)


def wait_for_proxy(url: str, proc: subprocess.Popen, timeout: float = 120.0) -> None:
    """Poll oauth2-proxy's /ping (a pull of the image counts against timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"oauth2-proxy exited with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"oauth2-proxy not ready at {url} after {timeout}s")


def build_tenant_api(workdir: Path) -> Path:
    """Build the tenant-api binary into workdir/tenant-api. Returns path."""
    binary = workdir / "tenant-api"
    print(f"[contract] building tenant-api → {binary}")
    # go_build.sh passes -buildvcs=false: needed when running inside dev
    # container against a worktree where .git is a file pointing to a
    # Windows path the container can't read.
    subprocess.run(
        _go_build_argv(TENANT_API_DIR, "./cmd/server", binary),
        check=True,
        timeout=180,  # cold cache go build is ~60s; 3min is generous
    )
    return binary


def main() -> int:
    if not SWAGGER_JSON.exists():
        print(f"[contract] FATAL: {SWAGGER_JSON} not found. Run `make api-docs` first.")
        return 2

    if not shutil.which("schemathesis"):
        print("[contract] FATAL: schemathesis not in PATH. `pip install -r tests/contract/requirements.txt`.")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="tenant-api-contract-"))
    config_dir = workdir / "conf.d"
    config_dir.mkdir()
    # Minimal seed file so list endpoints have at least one tenant.
    (config_dir / f"{SEED_TENANT}.yaml").write_text(
        "tenants:\n"
        f"  {SEED_TENANT}:\n"
        "    cpu: \"80\"\n"
        "    environment: production\n"
    )
    # Domain policy fixture — written before the initial commit so the
    # policy file is part of the repo's baseline state.
    write_domain_policy_fixture(config_dir)
    # Commit-on-write needs a real repo; see init_git_repo docstring.
    init_git_repo(config_dir)
    rbac_path = write_rbac_fixture(workdir)

    generator, generator_sock, human_sock_dir = None, None, None
    gen_log_fh = (workdir / "generator.log").open("w", encoding="utf-8")
    try:
        binary = build_tenant_api(workdir)
        generator, generator_sock = start_generator(config_dir, workdir, gen_log_fh)
        print(f"[contract] generator service ready on {generator_sock}")
        port = find_free_port()
        addr = f"127.0.0.1:{port}"
        probe_url = f"http://{addr}"
        proxy_port = find_free_port()
        # Both ports are only free when picked; tenant-api binds the first
        # one later, so the proxy must not get the same number.
        while proxy_port == port:
            proxy_port = find_free_port()
        base_url = f"http://127.0.0.1:{proxy_port}"
        # ADR-027 §2: the API's socket. A short temp dir (a Unix socket path
        # is limited to ~107 bytes); 0777 so a container running as this uid
        # under another mount namespace can still traverse it.
        human_sock_dir = Path(tempfile.mkdtemp(prefix="ch"))
        os.chmod(human_sock_dir, 0o777)
        proxy_config = write_proxy_config(workdir, "/run/ta-human/human.sock")

        env = {
            **os.environ,
            "TA_ADDR": addr,
            "TA_CONFIG_DIR": str(config_dir),
            # #2856: groups / views / federation records live in a state
            # store, never in conf.d; a plain directory beside it here.
            "TA_STATE_STORE": "dir",
            "TA_STATE_DIR": str(workdir / "state"),
            # Disable the per-caller rate limiter: all fuzz traffic shares one
            # X-Forwarded-Email, so the default 100 req/min would 429 the run.
            "TA_RATE_LIMIT_PER_MIN": "0",
            # #2903: every write asks the generator started above.
            "TA_GENERATOR_SOCKET": generator_sock,
            # ADR-027 §2, §3: the API only on this socket, behind oauth2-proxy;
            # identity headers named by the config file oauth2-proxy runs
            # with; the dev bypass injects the fuzz identity on the socket.
            "TA_HUMAN_SOCKET": str(human_sock_dir / "human.sock"),
            "TA_OAUTH2_PROXY_CONFIG": str(proxy_config),
            "TA_DEV_BYPASS_AUTH": "true",
            "TA_DEV_BYPASS_EMAIL": FUZZ_EMAIL,
            "TA_DEV_BYPASS_GROUPS": FUZZ_GROUP,
            # #3175: tenant-api refuses to start when its git config names
            # a filter / diff / merge driver, and a host with git-lfs
            # installed has filter.lfs.* in its system or global config.
            # The fixture repo needs neither file (its identity is
            # repo-local, see init_git_repo).
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        print(f"[contract] starting tenant-api on {addr} (config={config_dir})")
        # Server output goes to a FILE, not subprocess.PIPE: nothing drains a
        # pipe during the fuzz, and full-method fuzzing logs enough request
        # lines to fill the 64KB pipe buffer — tenant-api then blocks on the
        # log write and every in-flight request hangs until the timeout.
        # (Bit us for real: the GET-only era stayed under the buffer.)
        server_log = workdir / "tenant-api.log"
        log_fh = server_log.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [str(binary), "-addr", addr, "-config-dir", str(config_dir),
             "-rbac", str(rbac_path)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        proxy_stop: list = []
        proxy = None
        proxy_log_fh = (workdir / "oauth2-proxy.log").open("w", encoding="utf-8")
        try:
            wait_for_health(f"{probe_url}/health", timeout=15.0)
            proxy_stop, proxy = start_oauth2_proxy(workdir, human_sock_dir, proxy_port, proxy_log_fh)
            wait_for_proxy(f"{base_url}/ping", proxy)
            print(f"[contract] tenant-api ready behind oauth2-proxy on {base_url}, running policy-gate smoke")

            # Deterministic policy-gate contract case — BEFORE the fuzz so a
            # dead gate fails fast and the checks run against pristine state.
            if not run_policy_gate_smoke(base_url):
                return 1

            print("[contract] running schemathesis")

            # Schemathesis 4.x: run against the spec, base URL is our local
            # server. ALL methods are fuzzed — writes land in the throwaway
            # git-repo fixture, so state mutation is free (rmtree'd below).
            #
            # --max-examples 10 keeps CI runtime bounded; bump locally via
            # CONTRACT_MAX_EXAMPLES=50 for deeper fuzz when investigating.
            # No identity header: oauth2-proxy (no login) injects none and
            # strips client copies; tenant-api's dev bypass injects
            # FUZZ_EMAIL / FUZZ_GROUP on the socket plane (ADR-027 §3), the
            # group mapping to the wildcard write fixture in _rbac.yaml.
            #
            # --exclude-path-regex: federation token ops need --federation-key
            # + a Kubernetes ConfigMap store (see module docstring, "Excluded
            # operations"). The regex targets /federation/tokens* and
            # /federation/accounts/* only — /federation/policy and
            # /tenants/{id}/federation stay fuzzed.
            #
            # --exclude-path /api/v1/prs: the route registers only in PR
            # write-mode (cmd/server/routes.go `deps.PRTracker != nil`), which
            # needs a forge token — this fixture runs write-mode=direct, so
            # the op 404s and would (correctly) fail status_code_conformance.
            # Reopen if the fixture ever grows a stub forge backend.
            #
            # Checks: response_schema_conformance (body matches declared
            # schema) + status_code_conformance (no undocumented status
            # codes) + content_type_conformance. The latter two were enabled
            # after the 4xx responses were back-filled into the swag
            # annotations (handler.ErrorResponse migration).
            result = subprocess.run(
                [
                    "schemathesis", "run",
                    str(SWAGGER_JSON),
                    "--url", base_url,
                    "--exclude-path-regex", "^/api/v1/federation/(tokens|accounts)",
                    "--exclude-path", "/api/v1/prs",
                    # POST /tenants/batch is excluded from fuzzing: its body
                    # (operations[] of tenant_id+patch) is generation-hostile
                    # enough that hypothesis filters out nearly every example
                    # and raises `filter_too_much`. Schemathesis 4.x surfaces
                    # that as an ENGINE-level error and exits 1 even WITH
                    # `--suppress-health-check filter_too_much` below (that flag
                    # only governs hypothesis's own health check, not the 4.x
                    # engine report — verified on CI seed 3258…9317, #1158). So
                    # the op flakily reds the run without ever effectively
                    # fuzzing. It stays covered by Go handler tests + the
                    # deterministic policy-gate smoke, and the Coverage phase
                    # still exercises every OTHER op. Reopen (re-include) when
                    # the batch body schema is widened / given examples so it
                    # is actually fuzzable.
                    "--exclude-path", "/api/v1/tenants/batch",
                    # Best-effort for any OTHER constraint-heavy op: governs
                    # hypothesis's own filter_too_much check (actual contract
                    # violations still fail the run). Kept because it is free
                    # insurance even though it does not cover the 4.x engine
                    # report that made /tenants/batch above need exclusion.
                    "--suppress-health-check", "filter_too_much",
                    "--checks",
                    "response_schema_conformance,status_code_conformance,content_type_conformance",
                    "--max-examples", os.environ.get("CONTRACT_MAX_EXAMPLES", "10"),
                ],
                timeout=600,  # 10min cap; default fuzz is ~30s, deep fuzz can stretch
            )
            return result.returncode
        finally:
            if proxy is not None:
                print("[contract] tearing down oauth2-proxy")
                if proxy_stop:
                    try:
                        subprocess.run(proxy_stop, timeout=30, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.STDOUT, check=False)
                    except subprocess.TimeoutExpired:
                        # Keep tearing down: tenant-api below must still stop.
                        print("[contract] stopping the oauth2-proxy container timed out; continuing teardown")
                proxy.terminate()
                try:
                    proxy.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait()
                if proxy.returncode not in (0, -signal.SIGTERM, 143, 137):
                    tail = (workdir / "oauth2-proxy.log").read_text(errors="replace").splitlines()[-30:]
                    print(f"[contract] oauth2-proxy exited with code {proxy.returncode}; log tail:")
                    print("\n".join(tail))
            proxy_log_fh.close()
            print("[contract] tearing down tenant-api")
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log_fh.close()
            # Surface server log tail if it died unexpectedly
            if proc.returncode not in (0, -signal.SIGTERM):
                tail = server_log.read_text(errors="replace").splitlines()[-50:]
                print(f"[contract] tenant-api exited with code {proc.returncode}; log tail:")
                print("\n".join(tail))
    finally:
        if generator is not None:
            generator.send_signal(signal.SIGTERM)
            try:
                generator.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                generator.kill()
                generator.wait()
        gen_log_fh.close()
        if generator_sock is not None:
            shutil.rmtree(Path(generator_sock).parent, ignore_errors=True)
        if human_sock_dir is not None:
            shutil.rmtree(human_sock_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
