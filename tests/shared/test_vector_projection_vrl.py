"""test_vector_projection_vrl.py — ADR-021 Phase 1 (b) projection / #609

Pins the Vector tenant-sanitized projection: the fan-out that writes each
tenant's federation_audit rows, SANITIZED, into its own VictoriaLogs
(AccountID, ProjectID) partition while the platform full copy keeps flowing
to 0:0 unchanged.

Two tiers (each independently gated on a binary being on PATH):

  - helm-render tests (need `helm`): assert the rendered Vector config + the
    VictoriaLogs deployment carry the right shapes — transforms only when
    enabled, static per-AccountID sinks with FIXED headers, Layer-1 search
    flags, maxQueryDuration < gateway 30s.
  - `vector validate` + `vector test` (need BOTH `helm` and `vector`): the AC
    behavior gate. validate = syntax; test runs helm/vector/tests/
    projection_tests.yaml (negative assertion that topology is stripped,
    fail-closed on blank/unknown/parse-error tenant_id, log_event_id in both
    copies, gateway_operational stays 0:0). Mirrors the runbook §4.4 manual
    `vector validate` step, now codified.

Maps to ADR-021 implementation-plan AC (L224-226) and §Ingestion fan-out.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml


# ── Fixtures / gating ────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


_HAS_HELM = shutil.which("helm") is not None
_HAS_VECTOR = shutil.which("vector") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
_needs_vector = pytest.mark.skipif(
    not (_HAS_HELM and _HAS_VECTOR), reason="helm+vector CLIs not on PATH"
)

# Fixture projection: two tenants, ids in the registry's reserved-floor range
# (>=1000, the account package's FirstTenantAccountID). Matches the routes the
# tests.yaml references (t_1000 / t_1001).
_PROJECTION_SETS = {
    "tenantProjections[0].tenantId": "tenant-alpha",
    "tenantProjections[0].accountId": "1000",
    "tenantProjections[1].tenantId": "tenant-beta",
    "tenantProjections[1].accountId": "1001",
}


def _render(chart_dir: Path, *, sets: dict[str, str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir), "-n", "monitoring"]
    for k, v in (sets or {}).items():
        cmd += ["--set", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _vector_yaml(docs: list[dict]) -> dict:
    cm = [d for d in docs
          if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
    return yaml.safe_load(cm["data"]["vector.yaml"])


def _render_result(chart_dir: Path, *, sets: dict[str, str] | None = None,
                   string_sets: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """helm template WITHOUT check=True — for asserting a render-time {{ fail }}
    guard or a values.schema.json rejection (the success path uses _render)."""
    cmd = ["helm", "template", "test-release", str(chart_dir), "-n", "monitoring"]
    for k, v in (sets or {}).items():
        cmd += ["--set", f"{k}={v}"]
    for k, v in (string_sets or {}).items():
        cmd += ["--set-string", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _dur_to_seconds(d: str) -> int:
    """Parse a Go-style duration (25s / 2m / 1h) to seconds — robust so the
    cascade test ASSERTS on an override rather than crashing (int('2m'-'s')
    raised ValueError before; adversarial-review finding)."""
    d = d.strip()
    units = {"s": 1, "m": 60, "h": 3600}
    if d and d[-1] in units:
        return int(float(d[:-1]) * units[d[-1]])
    return int(d)  # bare number = seconds


# ──────────────────────────────────────────────────────────────────────────────
# Render shape — projection DISABLED by default (no #539 regression)
# ──────────────────────────────────────────────────────────────────────────────

class TestProjectionDisabledByDefault:
    @_needs_helm
    def test_no_projection_transforms_or_sinks_by_default(self, repo_root: Path) -> None:
        """Empty tenantProjections (default) → byte-for-byte #539 pipeline:
        no tenant_project / tenant_route transforms, no vl_tenant_* sinks.
        A regression here would silently start (or fail to render) the
        projection for every existing single-tenant install."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        assert "tenant_project" not in cfg["transforms"]
        assert "tenant_route" not in cfg["transforms"]
        assert not any(k.startswith("vl_tenant_") for k in cfg["sinks"])
        # The primary 0:0 sink is still there and still reads demux.
        assert cfg["sinks"]["victorialogs"]["inputs"] == ["demux"]

    @_needs_helm
    def test_log_event_id_injected_even_when_projection_disabled(self, repo_root: Path) -> None:
        """log_event_id lands in the SHARED demux stage, so the 0:0 copy gets
        it regardless of whether any tenant projection is configured (it is the
        cross-partition join key — must exist on the full copy unconditionally,
        ready for the day a projection is added). uuid_v7, not uuid_v4."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        vrl = cfg["transforms"]["demux"]["source"]
        assert ".log_event_id" in vrl
        assert "uuid_v7()" in vrl, "must be time-sortable uuid_v7 (Gemini fold-in), not uuid_v4"
        assert "uuid_v4()" not in vrl


# ──────────────────────────────────────────────────────────────────────────────
# Render shape — projection ENABLED
# ──────────────────────────────────────────────────────────────────────────────

class TestProjectionEnabledShape:
    @_needs_helm
    def test_transforms_render_with_failclosed_error_handling(self, repo_root: Path) -> None:
        """tenant_project must be drop_on_error + NOT reroute_dropped — a VRL
        error drops the event from the tenant branch (never default-allows it
        into a partition). This is the Gemini 1c / §Fork B invariant."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        tp = cfg["transforms"]["tenant_project"]
        assert tp["type"] == "remap"
        assert tp["inputs"] == ["demux"], "projection forks off demux, not the source"
        # abort is governed by drop_on_abort (NOT drop_on_error); the
        # fail-closed eligibility aborts hinge on it, so it's pinned explicitly.
        assert tp["drop_on_abort"] is True, "abort must drop the event (PRIMARY fail-closed path)"
        assert tp["drop_on_error"] is True, "VRL runtime error must also drop (fail-closed)"
        assert tp["reroute_dropped"] is False, "dropped events must NOT be rerouted onward"

    @_needs_helm
    def test_route_has_one_exact_match_route_per_tenant(self, repo_root: Path) -> None:
        """tenant_route maps each AccountID to an EXACT-equality route. No
        catch-all/default route exists — an unmapped row hits the reserved
        _unmatched output (discarded), which is the structural fail-closed."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        rt = cfg["transforms"]["tenant_route"]
        assert rt["type"] == "route"
        assert rt["inputs"] == ["tenant_project"]
        routes = rt["route"]
        assert set(routes) == {"t_1000", "t_1001"}
        assert routes["t_1000"] == ".account_id == 1000"
        assert routes["t_1001"] == ".account_id == 1001"
        # No default/catch-all route key.
        assert "_default" not in routes and "_unmatched" not in routes

    @_needs_helm
    def test_static_n_sinks_each_pinned_to_fixed_account_header(self, repo_root: Path) -> None:
        """Static-N sinks (NOT one dynamic header-templated sink): each
        vl_tenant_<id> consumes ONLY its own route output and pins a CONSTANT
        AccountID header. This is the vectordotdev/vector#21402 mitigation —
        a per-event header on a mixed-tenant batch would mis-stamp."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        for aid in (1000, 1001):
            sink = cfg["sinks"][f"vl_tenant_{aid}"]
            assert sink["type"] == "elasticsearch"
            # Single-tenant input — the constant header below cannot mis-stamp.
            assert sink["inputs"] == [f"tenant_route.t_{aid}"]
            headers = sink["request"]["headers"]
            assert headers["AccountID"] == str(aid), "fixed per-sink AccountID header"
            assert headers["ProjectID"] == "0", "(b) operational logs use ProjectID 0"

    @_needs_helm
    def test_allowlist_sanitization_not_denylist(self, repo_root: Path) -> None:
        """Sanitization is a fail-closed ALLOWLIST (adversarial-review finding):
        tenant_project rebuilds the event from tenantProjectionKeepFields ONLY,
        so the raw .message (which embeds upstream=%UPSTREAM_HOST%, the backend
        IP) and any unlisted/infra field are structurally absent — NOT del()'d
        off a denylist (which left the raw message + `upstream` leaking)."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        vrl = cfg["transforms"]["tenant_project"]["source"]
        # Allowlist-rebuild markers.
        assert "kept = {}" in vrl
        assert "kept.account_id = aid" in vrl, "partition key is the trusted map value, not payload"
        assert "kept.message = encode_json(kept)" in vrl, "re-serialized sanitized _msg (raw line discarded)"
        assert ". = kept" in vrl, "event rebuilt from the allowlist (fail-closed)"
        for safe in ("tenant_id", "log_event_id", "status", "query", "token_id"):
            assert f"kept.{safe} = .{safe}" in vrl, f"{safe} must be allowlisted"
        # Infra / raw fields must NOT be copied into the tenant event.
        for infra in ("upstream", "app", "k8s_namespace", "pod_name", "host"):
            assert f"kept.{infra}" not in vrl, f"{infra} must NOT be allowlisted (would leak)"
        # log_event_id is unconditionally platform-stamped (not payload-trusted).
        assert ".log_event_id = uuid_v7()" in cfg["transforms"]["demux"]["source"]
        assert "if !exists(.log_event_id)" not in cfg["transforms"]["demux"]["source"]

    @_needs_helm
    def test_duplicate_accountid_fails_render(self, repo_root: Path) -> None:
        """A duplicate accountId would co-mingle two tenants into ONE partition
        (cross-tenant leak); the render-time uniqueness guard must {{ fail }}
        (vector validate would NOT catch it — serde_yaml dup-key = last-wins)."""
        r = _render_result(repo_root / "helm/vector", sets={
            "tenantProjections[0].tenantId": "tenant-alpha", "tenantProjections[0].accountId": "1000",
            "tenantProjections[1].tenantId": "tenant-beta", "tenantProjections[1].accountId": "1000",
        })
        assert r.returncode != 0, "duplicate accountId must fail render"
        assert "duplicate accountId" in r.stderr

    @_needs_helm
    def test_duplicate_tenantid_fails_render(self, repo_root: Path) -> None:
        """A duplicate tenantId would mis-route a tenant to a foreign AccountID."""
        r = _render_result(repo_root / "helm/vector", sets={
            "tenantProjections[0].tenantId": "tenant-alpha", "tenantProjections[0].accountId": "1000",
            "tenantProjections[1].tenantId": "tenant-alpha", "tenantProjections[1].accountId": "1001",
        })
        assert r.returncode != 0, "duplicate tenantId must fail render"
        assert "duplicate tenantId" in r.stderr

    @_needs_helm
    def test_noninteger_accountid_fails_schema(self, repo_root: Path) -> None:
        """values.schema.json constrains accountId to integer — a quoted/typo'd
        id (which would render an invalid VRL identifier → silent empty
        partition) must fail at helm template, not silently."""
        r = _render_result(repo_root / "helm/vector",
                           sets={"tenantProjections[0].tenantId": "tenant-alpha"},
                           string_sets={"tenantProjections[0].accountId": "1000"})
        assert r.returncode != 0, "string accountId must fail schema"
        assert "integer" in r.stderr.lower() or "schema" in r.stderr.lower()

    @_needs_helm
    def test_account_map_does_not_hash_tenant_id(self, repo_root: Path) -> None:
        """Enrichment is an explicit committed map (tenant_id string ->
        AccountID), never a hash/derived id (ADR-021 forbids — hash collision
        = cross-tenant merge). The rendered map must contain literal pairings,
        and the VRL must abort (fail-closed) on an unmapped tenant."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        vrl = cfg["transforms"]["tenant_project"]["source"]
        assert '"tenant-alpha": 1000' in vrl
        assert '"tenant-beta": 1001' in vrl
        # Eligibility + fail-closed aborts present.
        assert 'if .log_type != "federation_audit"' in vrl
        assert "abort" in vrl


# ──────────────────────────────────────────────────────────────────────────────
# VictoriaLogs Layer-1 search guardrails
# ──────────────────────────────────────────────────────────────────────────────

class TestVictoriaLogsLayer1Flags:
    @_needs_helm
    def test_search_flags_render_into_args(self, repo_root: Path) -> None:
        docs = _render(repo_root / "helm/victorialogs")
        dep = [d for d in docs if d.get("kind") == "Deployment"][0]
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        joined = "\n".join(args)
        assert "-search.maxQueryTimeRange=7d" in joined
        assert "-search.maxQueryDuration=25s" in joined
        assert "-search.maxConcurrentRequests=6" in joined
        assert "-search.maxQueueDuration=10s" in joined

    @_needs_helm
    def test_max_query_duration_strictly_below_gateway_timeout(self, repo_root: Path) -> None:
        """⛔ Cascading timeout (Gemini fold-in): storage maxQueryDuration MUST
        be < the gateway route timeout (30s) so VictoriaLogs aborts FIRST and
        never leaves a zombie query holding a concurrency slot after the
        gateway gave up. Parse the rendered flag and compare numerically."""
        docs = _render(repo_root / "helm/victorialogs")
        dep = [d for d in docs if d.get("kind") == "Deployment"][0]
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        dur = next(a for a in args if a.startswith("-search.maxQueryDuration="))
        secs = _dur_to_seconds(dur.split("=", 1)[1])
        GATEWAY_TIMEOUT_S = 30
        assert secs < GATEWAY_TIMEOUT_S, (
            f"maxQueryDuration {secs}s must be < gateway {GATEWAY_TIMEOUT_S}s "
            "(cascading-timeout: storage must abort before the gateway)"
        )

    @_needs_helm
    def test_search_flags_overridable_via_extraargs(self, repo_root: Path) -> None:
        """extraArgs is rendered AFTER the search flags, so a duplicate wins
        (last-flag-wins). Operators must be able to retune without forking."""
        docs = _render(repo_root / "helm/victorialogs",
                       sets={"extraArgs[0]": "-search.maxConcurrentRequests=12"})
        dep = [d for d in docs if d.get("kind") == "Deployment"][0]
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        # Both present; the override appears last.
        idx_default = max(i for i, a in enumerate(args) if a == "-search.maxConcurrentRequests=6")
        idx_override = max(i for i, a in enumerate(args) if a == "-search.maxConcurrentRequests=12")
        assert idx_override > idx_default, "extraArgs override must come after the default"


# ──────────────────────────────────────────────────────────────────────────────
# AC behavior gate — vector validate + vector test
# ──────────────────────────────────────────────────────────────────────────────

def _render_vector_config_to_tmp(repo_root: Path, tmp: Path) -> Path:
    """helm template the vector chart WITH the fixture projection, extract the
    vector.yaml, write it to a temp file `vector` can load. Returns the path."""
    cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
    path = tmp / "rendered-vector.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


class TestVectorValidateAndTest:
    @_needs_vector
    def test_vector_validate_default_config(self, repo_root: Path) -> None:
        """`vector validate` on the DEFAULT (projection-disabled) render —
        codifies runbook §4.4. -ne (no-environment) skips healthchecks /
        env-var requirements so it validates offline in CI."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vector.yaml"
            p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
            r = subprocess.run(["vector", "validate", "--no-environment", str(p)],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"vector validate failed:\n{r.stdout}\n{r.stderr}"

    @_needs_vector
    def test_vector_validate_projection_config(self, repo_root: Path) -> None:
        """`vector validate` on the projection-ENABLED render — the new
        transforms + static sinks must be valid Vector config + valid VRL."""
        with tempfile.TemporaryDirectory() as d:
            p = _render_vector_config_to_tmp(repo_root, Path(d))
            r = subprocess.run(["vector", "validate", "--no-environment", str(p)],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"vector validate failed:\n{r.stdout}\n{r.stderr}"

    @_needs_vector
    def test_vector_unit_tests_pass(self, repo_root: Path) -> None:
        """`vector test` runs helm/vector/tests/projection_tests.yaml against
        the rendered config: the AC behavior suite (negative topology-strip
        assertion, fail-closed on blank/unknown/parse-error tenant_id,
        log_event_id in both copies, operational stays 0:0)."""
        tests_file = repo_root / "helm/vector/tests/projection_tests.yaml"
        assert tests_file.exists(), "projection_tests.yaml must ship with the chart"
        with tempfile.TemporaryDirectory() as d:
            cfg_path = _render_vector_config_to_tmp(repo_root, Path(d))
            r = subprocess.run(["vector", "test", str(cfg_path), str(tests_file)],
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f"vector test failed:\n{r.stdout}\n{r.stderr}"
