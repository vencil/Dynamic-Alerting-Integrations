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
  - `_rbac.yaml` grants the fuzz group wildcard read+write+admin so the
    authz layer passes and fuzzing exercises handler/validation logic,
    not just 401/403 short-circuits. Requests carry X-Forwarded-Email +
    X-Forwarded-Groups, matching the oauth2-proxy-fronted contract.
  - TA_RATE_LIMIT_PER_MIN=0 disables the per-caller rate limiter
    (cmd/server/main.go); hundreds of fuzz requests/minute from one
    caller identity would otherwise trip 429s unrelated to the spec.
  - `_domain_policy.yaml` lives INSIDE conf.d/ (unlike _rbac.yaml there
    is no flag: policy.NewManager hard-joins configDir — cmd/server/main.go
    → internal/policy/policy.go) and binds the seed tenant to a domain
    that forbids one receiver type. This arms the API-layer policy gate
    (CheckWrite → 403 POLICY_VIOLATION, internal/handler/tenant_put.go)
    so the declared 403 contract is reachable in this fixture. Because
    domain→tenant matching is an exact tenant list (no wildcard) and a
    fuzzed body rarely hits `_routing.receiver.type` on the seed tenant,
    a DETERMINISTIC smoke (run_policy_gate_smoke) fires the gate before
    the fuzz — the 403 path never depends on fuzz luck.

Skipped checks (and why — see also docs/internal/testing-playbook.md):
  - `auth-required`: tenant-api auth is via X-Forwarded-* headers from
    a fronting proxy; schemathesis can't simulate the proxy. Auth is
    covered by Go-level handler tests.

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
SWAGGER_JSON = TENANT_API_DIR / "docs" / "swagger.json"


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


# IdP group granted wildcard access in the fuzz _rbac.yaml; must match the
# X-Forwarded-Groups header sent with every schemathesis request.
FUZZ_GROUP = "contract-fuzz-admins"

# Caller identity for every request (schemathesis AND the deterministic
# policy smoke) — one identity, so TA_RATE_LIMIT_PER_MIN=0 stays required.
FUZZ_EMAIL = "schemathesis@example.com"

# Neutral fixture tenant (dev-rule #2: no real tenant ids in fixtures).
# Seeded in conf.d so list endpoints have ≥1 tenant, and listed in the
# domain policy fixture so the policy gate can actually match a tenant
# (policy.go isTenantInPolicy is an exact-list match, no wildcard).
SEED_TENANT = "db-seed"

# Domain policy fixture knobs. webhook is forbidden / email is allowed —
# both are real receiver types from tenant-config.schema.json, so the
# violating body is schema-valid and only the policy gate can reject it
# (a 400 would mean the smoke broke, not the policy).
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
    live INSIDE conf.d — policy.NewManager joins configDir directly
    (cmd/server/main.go), there is no -policy flag. The scanner skips
    `_`-prefixed files (internal/handler/tenant_list.go), so the file
    never shows up as a tenant.
    """
    (config_dir / "_domain_policy.yaml").write_text(
        "domain_policies:\n"
        f"  {POLICY_DOMAIN}:\n"
        "    description: \"Contract-test fixture: arms the API-layer policy gate\"\n"
        f"    tenants: [{SEED_TENANT}]\n"
        "    constraints:\n"
        f"      allowed_receiver_types: [{POLICY_ALLOWED_RECEIVER}, pagerduty]\n"
        f"      forbidden_receiver_types: [{POLICY_FORBIDDEN_RECEIVER}, slack]\n"
    )


def _tenant_body(receiver_yaml: str) -> bytes:
    """Tenant-only PUT body (conf.d/{id}.yaml shape) with a _routing receiver.

    Deliberately ONLY `_routing`: the write validation resolves plain
    metric keys (cpu, ...) against the merged _defaults.yaml, and this
    fixture ships no defaults — a metric key would 400 with "unknown key
    not in defaults" before proving anything about the policy gate.
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
    """PUT the seed tenant with fuzz-identity headers; never raises on 4xx/5xx."""
    req = urllib.request.Request(
        f"{base_url}/api/v1/tenants/{SEED_TENANT}",
        data=body,
        method="PUT",
        headers={
            "X-Forwarded-Email": FUZZ_EMAIL,
            "X-Forwarded-Groups": FUZZ_GROUP,
            "Content-Type": "application/yaml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def run_policy_gate_smoke(base_url: str) -> bool:
    """Deterministic contract case for the domain-policy 403 path.

    schemathesis almost never generates a body that sets
    `_routing.receiver.type` on the seed tenant, so without this the
    policy gate's declared 403 would only be exercised by fuzz luck.
    Two fixed requests, run BEFORE the fuzz (fail fast, and immune to
    whatever state the fuzz leaves behind):

      1. violating PUT (forbidden receiver type) → MUST be 403 with the
         ErrorResponse envelope: code=POLICY_VIOLATION + violations[]
         carrying the policy.Violation shape (domain/constraint/message).
         The body is schema-valid apart from the policy, so a 400 here
         means the smoke itself broke — not a pass.
      2. control PUT (allowed receiver type) → MUST be 200. Guards
         against a miswired fixture where the policy (or the file's mere
         presence in conf.d) blocks every write and the fuzz would
         silently degrade into an all-403 run.
    """
    ok = True

    status, ctype, raw = _put_tenant(base_url, _tenant_body(
        f"        type: {POLICY_FORBIDDEN_RECEIVER}\n"
        "        url: https://alerts.invalid/contract-fixture\n"
    ))
    detail = f"status={status} content-type={ctype} body={raw[:400]!r}"
    if status != 403:
        print(f"[contract] FAIL policy smoke: violating PUT expected 403, got {detail}")
        ok = False
    else:
        try:
            body = json.loads(raw)
        except ValueError:
            print(f"[contract] FAIL policy smoke: 403 body is not JSON: {detail}")
            return False
        violations = body.get("violations") or []
        if body.get("code") != "POLICY_VIOLATION":
            print(f"[contract] FAIL policy smoke: expected code=POLICY_VIOLATION, got {detail}")
            ok = False
        elif not body.get("error") or not violations:
            print(f"[contract] FAIL policy smoke: 403 missing error/violations: {detail}")
            ok = False
        elif not any(
            v.get("domain") == POLICY_DOMAIN
            and v.get("constraint") == "forbidden_receiver_types"
            and v.get("message")
            for v in violations
        ):
            print(f"[contract] FAIL policy smoke: violations lack the policy.Violation shape: {detail}")
            ok = False
        elif not ctype.startswith("application/json"):
            print(f"[contract] FAIL policy smoke: 403 content-type not JSON: {detail}")
            ok = False

    status, _, raw = _put_tenant(base_url, _tenant_body(
        f"        type: {POLICY_ALLOWED_RECEIVER}\n"
        "        to: oncall@example.com\n"
        "        smarthost: smtp.example.invalid:587\n"
    ))
    if status != 200:
        print(f"[contract] FAIL policy smoke: control PUT (allowed receiver) expected 200, "
              f"got status={status} body={raw[:400]!r}")
        ok = False

    if ok:
        print("[contract] policy-gate smoke OK: violating PUT → 403 POLICY_VIOLATION, control PUT → 200")
    return ok


def build_tenant_api(workdir: Path) -> Path:
    """Build the tenant-api binary into workdir/tenant-api. Returns path."""
    binary = workdir / "tenant-api"
    print(f"[contract] building tenant-api → {binary}")
    # -buildvcs=false: needed when running inside dev container against a
    # worktree where .git is a file pointing to a Windows path the
    # container can't read. CI (real checkout) doesn't need it but
    # passing it is harmless.
    subprocess.run(
        ["go", "build", "-buildvcs=false", "-o", str(binary), "./cmd/server"],
        cwd=TENANT_API_DIR,
        check=True,
        timeout=180,  # cold cache go build is ~60s; 3min is generous
    )
    return binary


def main() -> int:
    if not SWAGGER_JSON.exists():
        print(f"[contract] FATAL: {SWAGGER_JSON} not found. Run `make api-docs` first.")
        return 2

    if not shutil.which("schemathesis"):
        print("[contract] FATAL: schemathesis not in PATH. `pip install schemathesis`.")
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

    try:
        binary = build_tenant_api(workdir)
        port = find_free_port()
        addr = f"127.0.0.1:{port}"
        base_url = f"http://{addr}"

        env = {
            **os.environ,
            "TA_ADDR": addr,
            "TA_CONFIG_DIR": str(config_dir),
            # Disable the per-caller rate limiter: all fuzz traffic shares one
            # X-Forwarded-Email, so the default 100 req/min would 429 the run.
            "TA_RATE_LIMIT_PER_MIN": "0",
        }
        print(f"[contract] starting tenant-api on {addr} (config={config_dir})")
        # Server output goes to a FILE, not subprocess.PIPE: nothing drains a
        # pipe during the fuzz, and full-method fuzzing logs enough request
        # lines to fill the 64KB pipe buffer — tenant-api then blocks on the
        # log write and every in-flight request hangs until the timeout.
        # (Bit us for real: the GET-only era stayed under the buffer.)
        server_log = workdir / "tenant-api.log"
        log_fh = server_log.open("w")
        proc = subprocess.Popen(
            [str(binary), "-addr", addr, "-config-dir", str(config_dir),
             "-rbac", str(rbac_path)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            wait_for_health(f"{base_url}/health", timeout=15.0)
            print("[contract] tenant-api ready, running policy-gate smoke")

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
            # tenant-api auth: X-Forwarded-Email + X-Forwarded-Groups match
            # the proxy-fronted contract; the group maps to the wildcard
            # write fixture in _rbac.yaml. Without these every endpoint
            # returns 401/403 and the fuzz never reaches handler logic.
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
                    "-H", f"X-Forwarded-Email: {FUZZ_EMAIL}",
                    "-H", f"X-Forwarded-Groups: {FUZZ_GROUP}",
                    "--exclude-path-regex", "^/api/v1/federation/(tokens|accounts)",
                    "--exclude-path", "/api/v1/prs",
                    # filter_too_much is a hypothesis generation-efficiency
                    # health check, not a contract check: on some seeds the
                    # generated bodies for constraint-heavy ops (seen on
                    # POST /tenants/batch) are mostly filtered out and the
                    # whole run ERRORs flakily. Suppressing it only accepts
                    # lower fuzz throughput on those ops — actual contract
                    # violations still fail the run.
                    "--suppress-health-check", "filter_too_much",
                    "--checks",
                    "response_schema_conformance,status_code_conformance,content_type_conformance",
                    "--max-examples", os.environ.get("CONTRACT_MAX_EXAMPLES", "10"),
                ],
                timeout=600,  # 10min cap; default fuzz is ~30s, deep fuzz can stretch
            )
            return result.returncode
        finally:
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
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
