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

import json
import os
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
# Set to "1" by the CI job that installs these (ci.yml python-tests-run ``env:``).
# When set, a missing binary is a FAILURE, not a skip — see the two guards below.
_REQUIRE_VECTOR = os.environ.get("VIBE_REQUIRE_VECTOR") == "1"
_REQUIRE_HELM = os.environ.get("VIBE_REQUIRE_HELM") == "1"


def test_vector_present_when_required() -> None:
    """Fail-closed guard against silent disarmament (mirrors the #908
    ``test_mtail_present_when_required`` precedent).

    ``TestVectorValidateAndTest`` is the ONLY place the shipped VRL is executed
    rather than string-matched — tenant fail-closed routing, the ADR-028 §D3 PII
    drop, and the #1293 origin-spoof truth table are all proven there and nowhere
    else. Those tests are ``skipif``-gated on the binary, so if the CI ``Install
    Vector`` step is deleted or breaks, they SKIP, the job stays green, and the
    behaviour gate becomes a no-op that still reports success. Jobs that
    legitimately run pytest without vector (validate.yaml) don't set the flag, so
    they keep skipping."""
    if _REQUIRE_VECTOR:
        assert _HAS_VECTOR, (
            "VIBE_REQUIRE_VECTOR=1 but `vector` is not on PATH — the CI 'Install "
            "Vector' step (ci.yml python-tests-run job) is missing or broke; the "
            "VRL behaviour gate would silently skip. Restore the install step."
        )


def test_helm_present_when_required() -> None:
    """Same fail-closed guard for helm. 13 test files gate on
    ``shutil.which("helm")``; every rendered-manifest assertion in them
    (values.schema.json rejection, the NetworkPolicy consumer set, the egress
    allowlist, and every render test in this file) skips silently without it, so
    a broken ``azure/setup-helm`` step would fail OPEN across all of them."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            "VIBE_REQUIRE_HELM=1 but `helm` is not on PATH — the CI 'Set up Helm' "
            "step (ci.yml python-tests-run job) is missing or broke; every "
            "helm-gated render assertion would silently skip. Restore the step."
        )

# Fixture projection: two tenants, ids in the registry's reserved-floor range
# (>=1000, the account package's FirstTenantAccountID). Matches the routes the
# tests.yaml references (t_1000 / t_1001).
_PROJECTION_SETS = {
    "tenantProjections[0].tenantId": "tenant-alpha",
    "tenantProjections[0].accountId": "1000",
    "tenantProjections[1].tenantId": "tenant-beta",
    "tenantProjections[1].accountId": "1001",
    # #908 PR-2: the projection gate's init-container needs the conf.d registry
    # ConfigMap; the chart `required`s it whenever tenantProjections is set, so
    # every projection-enabled render must provide it.
    "projectionGate.registry.configMapName": "test-registry",
}


def _render(chart_dir: Path, *, sets: dict[str, str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir), "-n", "monitoring"]
    for k, v in (sets or {}).items():
        cmd += ["--set", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _vector_yaml(docs: list[dict]) -> dict:
    """Reconstruct the FULL Vector config from the #908 PR-2 config-dir split:
    deep-merge `vector.yaml` (the base: sources / demux / tenant TRANSFORMS / 0:0
    sink / additionalSinks / prometheus) with the `30-tenant-routing.yaml` fragment
    (tenant SINKS only) — exactly as Vector merges the --config-dir at runtime. The
    fragment key is absent when tenantProjections is empty. Every assertion below
    runs against this reconstructed full config, so the split is transparent to the
    invariants it pins."""
    cm = [d for d in docs
          if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
    cfg = yaml.safe_load(cm["data"]["vector.yaml"])
    fragment = cm["data"].get("30-tenant-routing.yaml")
    if fragment:
        for section, items in (yaml.safe_load(fragment) or {}).items():
            # Top-level Vector sections (sinks:) merge key-by-key, mirroring
            # config-dir merge semantics — base sinks + the fragment's tenant sinks.
            cfg.setdefault(section, {}).update(items)
    return cfg


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
        #
        # ⛔ CONTRACT CHANGE (ADR-028 D1 / #1234), not a test dodge: the 0:0
        # store now has TWO ingest paths, and the exact-list form is kept
        # deliberately so that dropping either one goes red. `demux` alone was
        # the bug — tenant-api's revocation events never reached the store, so
        # the reconciler's evidence query returned zero rows forever and the
        # un-revoke tamper-evidence control was a silent no-op. `federation_
        # evidence` is that second path. Order is asserted too: the rendered
        # template writes demux first, and a reorder would mean someone
        # restructured the sink block without reading this.
        assert cfg["sinks"]["victorialogs"]["inputs"] == ["demux", "federation_evidence"]

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
    def test_tenant_sinks_drop_newest_no_hol_blocking(self, repo_root: Path) -> None:
        """Fan-out resilience (#539 §2 / Gemini #894): each tenant sink MUST
        buffer with when_full=drop_newest so VictoriaLogs backpressure on ONE
        tenant's partition drops LOCALLY instead of head-of-line-blocking the
        shared demux → 0:0 write + every other tenant."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        for aid in (1000, 1001):
            buf = cfg["sinks"][f"vl_tenant_{aid}"].get("buffer", {})
            assert buf.get("when_full") == "drop_newest", (
                f"vl_tenant_{aid} must not block the shared pipeline on backpressure"
            )

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
        assert "truncate(" in vrl, "unbounded query field is capped (per-line / RAM bound, Gemini #894)"
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
            "projectionGate.registry.configMapName": "test-registry",
        })
        assert r.returncode != 0, "duplicate accountId must fail render"
        assert "duplicate accountId" in r.stderr

    @_needs_helm
    def test_duplicate_tenantid_fails_render(self, repo_root: Path) -> None:
        """A duplicate tenantId would mis-route a tenant to a foreign AccountID."""
        r = _render_result(repo_root / "helm/vector", sets={
            "tenantProjections[0].tenantId": "tenant-alpha", "tenantProjections[0].accountId": "1000",
            "tenantProjections[1].tenantId": "tenant-alpha", "tenantProjections[1].accountId": "1001",
            "projectionGate.registry.configMapName": "test-registry",
        })
        assert r.returncode != 0, "duplicate tenantId must fail render"
        assert "duplicate tenantId" in r.stderr

    @_needs_helm
    def test_noninteger_accountid_fails_schema(self, repo_root: Path) -> None:
        """values.schema.json constrains accountId to integer — a quoted/typo'd
        id (which would render an invalid VRL identifier → silent empty
        partition) must fail at helm template, not silently."""
        r = _render_result(repo_root / "helm/vector",
                           sets={"tenantProjections[0].tenantId": "tenant-alpha",
                                 "projectionGate.registry.configMapName": "test-registry"},
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
# Federation revocation EVIDENCE channel (ADR-028 D1 / #1234)
#
# The reconciler's evidence query returned zero rows forever because the Vector
# source only tailed federation-gateway — tenant-api's `federation_token_revoked`
# events never reached VictoriaLogs at all. ADR-028 §D3 assumed they did. These
# tests pin the pipeline shape that closes that gap; the BEHAVIOUR (parse/merge,
# PII drop, no tenant-partition reachability) is pinned by `vector test` in
# helm/vector/tests/projection_tests.yaml.
# ──────────────────────────────────────────────────────────────────────────────

_EVIDENCE_EVENTS = ("federation_token_revoked", "federation_revocation_channel_heartbeat")


class TestFederationEvidenceChannel:
    @_needs_helm
    def test_source_selector_covers_both_producers(self, repo_root: Path) -> None:
        """The single kubernetes_logs source must tail BOTH the gateway and the
        evidence producer. A second `kubernetes_logs` source was rejected: two
        sources sharing one `data_dir` have undocumented checkpoint behaviour and
        overlapping selectors double-ingest. If tenant-api falls out of this
        selector the whole ADR-028 detection plane is DOA again — silently."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        sources = cfg["sources"]
        assert list(sources) == ["kubernetes_logs"], "still exactly one primary source"
        sel = sources["kubernetes_logs"]["extra_label_selector"]
        assert "federation-gateway" in sel, "gateway must stay in scope (#539)"
        assert "tenant-api" in sel, "evidence producer must be in scope (ADR-028 / #1234)"

    @_needs_helm
    def test_split_happens_before_demux_and_keys_on_pristine_kubernetes(
        self, repo_root: Path
    ) -> None:
        """⛔ The split must sit BETWEEN the source and demux, and must key on
        `.kubernetes.*`. demux deep-merges the log payload over the event root,
        so a routing decision made downstream of that merge could be FORGED by a
        log producer. Reading pristine, kubelet-supplied pod metadata is what
        makes the origin decision untrusted-input-proof.

        The overwrite hazard itself is pinned behaviourally by the (F1)/(F2)
        cases in helm/vector/tests/projection_tests.yaml. ⚠️ This docstring used
        to assert that as already-existing fact ("projection_tests.yaml already
        pins the overwrite hazard") — it was not: no fixture forged a
        `kubernetes` key until #1294. The same unfounded claim sat in the
        configmap comment above origin_split; both are corrected together
        because a claim repeated in two places fails in two places."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        split = cfg["transforms"]["origin_split"]
        assert split["type"] == "route"
        assert split["inputs"] == ["kubernetes_logs"], "the split consumes the source directly"
        for name, cond in split["route"].items():
            assert cond.startswith(".kubernetes."), (
                f"origin_split.{name} keys on {cond!r} — it MUST read the pristine "
                ".kubernetes.* metadata, never a payload-controlled field"
            )
        # demux is fed by the gateway branch, NOT the raw source.
        assert cfg["transforms"]["demux"]["inputs"][0] == "origin_split.gateway", (
            "demux must consume the gateway branch — the 'demux only ever sees "
            "gateway-origin rows' precondition is the structural support for "
            "tenant_project's fail-closed guarantee, and it is preserved, not removed"
        )

    @_needs_helm
    def test_route_conditions_are_mutually_exclusive_positive_matches(
        self, repo_root: Path
    ) -> None:
        """Two independent properties, and it matters which one does which job.

        MUTUAL EXCLUSIVITY is what makes a plain `route` (not `exclusive_route`)
        safe: `route` copies an event to EVERY matching output, so two branches
        that could both match would double-write a row into 0:0. Same field,
        two distinct literal constants ⇒ never both.

        NON-exhaustiveness is what makes the PII gate fail in the safe
        direction. An earlier draft wrote the gateway side as the negation
        (`!= evidenceValue`), which is exhaustive: every row the source ever
        collected that was not exactly the evidence producer fell through into
        demux and on to the 30-day 0:0 store — for a mislabelled tenant-api pod
        that means `caller` (operator email) and `remote` (client IP), exactly
        what ADR-028 §D3 exists to exclude. With two positive matches an
        unrecognised row lands in `origin_split._unmatched`, which nothing
        consumes, so the failure is a DROP. Reverting either branch to a
        negation must fail here."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        routes = cfg["transforms"]["origin_split"]["route"]
        assert set(routes) == {"evidence", "gateway"}
        for name, cond in routes.items():
            assert "!=" not in cond, (
                f"{name!r} branch uses a negation ({cond!r}) — that makes the split "
                "exhaustive, so an unrecognised row is forwarded into demux/0:0 "
                "instead of dropped. Both branches must be positive matches."
            )
        lhs_ev, _, rhs_ev = routes["evidence"].partition(" == ")
        lhs_gw, _, rhs_gw = routes["gateway"].partition(" == ")
        assert rhs_ev and rhs_gw, "both conditions must be simple `==` comparisons"
        assert lhs_ev == lhs_gw, "both branches must test the SAME field"
        assert rhs_ev != rhs_gw, (
            "the two branches must test DISTINCT values — identical values would "
            "send every row down both outputs (route fans out to all matches)"
        )

    @_needs_helm
    def test_evidence_transform_is_failclosed(self, repo_root: Path) -> None:
        """Same fail-closed trio as tenant_project. The PII allowlist below is
        implemented with `abort`, so `drop_on_abort` is what actually enforces
        it — pinned explicitly rather than relying on the default."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector"))
        ev = cfg["transforms"]["federation_evidence"]
        assert ev["type"] == "remap"
        assert ev["inputs"] == ["origin_split.evidence"]
        assert ev["drop_on_abort"] is True, "the PII allowlist is built on abort"
        assert ev["drop_on_error"] is True
        assert ev["reroute_dropped"] is False

    @_needs_helm
    def test_evidence_vrl_parses_and_merges_the_payload(self, repo_root: Path) -> None:
        """⛔ THE fatal-omission guard. The reconciler filters on the FIELD
        `event:"federation_token_revoked"` and reads top-level `token_id` /
        `expires_at`. Ship the line without `parse_json(.message)` + deep merge
        and VictoriaLogs stores one opaque `message` string: the field query
        matches nothing, the reconciler still returns zero rows, and the channel
        is a no-op that LOOKS wired up. This was the fatal omission in the first
        draft of the design, so it gets its own test."""
        vrl = _vector_yaml(_render(repo_root / "helm/vector"))["transforms"][
            "federation_evidence"
        ]["source"]
        assert "parse_json(.message)" in vrl
        assert "merge(., object!(parsed), deep: true)" in vrl

    @_needs_helm
    def test_evidence_vrl_drops_everything_but_the_two_events(self, repo_root: Path) -> None:
        """ADR-028 §D3 PII minimisation. tenant-api's request middleware logs
        `caller` (the operator's EMAIL) and `remote` (client IP) on EVERY
        request; the whole pod stderr must NOT enter a 30-day audit store.

        Pins the WHOLE drop condition, operands AND joiner, as one exact string:
        `ev != A && ev != B`. Asserting only the operands left a mutation hole
        wide enough to disable the channel — see the comment on `expected_cond`
        below."""
        vrl = _vector_yaml(_render(repo_root / "helm/vector"))["transforms"][
            "federation_evidence"
        ]["source"]
        # ⛔ CODE lines only. The VRL carries long comments that quote `&&`, the
        # event names and `del()` while explaining WHY the shape is what it is, so
        # a substring scan over the whole block matches the prose and never fails
        # on a real regression (that already bit the `del()` assertion in the
        # sibling test below).
        code_lines = [ln.strip() for ln in vrl.splitlines()
                      if not ln.lstrip().startswith("#")]
        for event in _EVIDENCE_EVENTS:
            assert any(f'ev != "{event}"' in ln for ln in code_lines), (
                f"{event} must be allowlisted (in CODE, not only in a comment)"
            )
        # ⛔ Pin the JOINER too, not just the operands. The condition is a
        # conjunction of negations, so it is false the moment a row matches
        # EITHER event → only non-evidence rows abort. Flip `&&` to `||` and it
        # becomes true for EVERY row (no single `ev` can equal both names) → every
        # row aborts → the evidence channel is permanently empty, the reconciler
        # is back to zero rows forever, and the #1234 control is a silent no-op
        # again. Before this assertion that mutation passed the entire suite: the
        # other render tests only look for the operands, and the `vector test`
        # cases that would catch it are BEHAVIOURAL — they run against the
        # rendered config, so they go red only if this file's render assertions
        # are not the ones guarding the joiner. Exact-string, so a reordering or
        # a third silently-added operand also has to come through review here.
        expected_cond = ("if " + " && ".join(f'ev != "{e}"' for e in _EVIDENCE_EVENTS)
                         + " {")
        assert expected_cond in code_lines, (
            f"the PII drop condition must be exactly {expected_cond!r} — a `&&`-joined "
            "conjunction of `!=` comparisons in this order. `||` inverts it into "
            "drop-everything (silently empty channel); got: "
            f"{[ln for ln in code_lines if ln.startswith('if ev')]!r}"
        )
        assert vrl.count("abort") >= 2, "parse-failure AND non-evidence rows must both abort"
        # Allowlist, not denylist: there must be no `del(.caller)`-style scrub
        # (which would be fail-OPEN — a future PII field would ride through).
        assert "del(.caller)" not in vrl and "del(.remote)" not in vrl, (
            "PII must be excluded by dropping the ROW (allowlist), not by scrubbing "
            "named fields (a denylist is fail-open for any future field)"
        )

    @_needs_helm
    def test_evidence_vrl_promotes_from_premerge_locals(self, repo_root: Path) -> None:
        """Stream-field promotion must read the locals captured BEFORE the deep
        merge, so a payload field cannot forge `app` / `k8s_namespace` /
        `pod_name` / `pod_node` / `log_type` on a chain-of-custody row."""
        vrl = _vector_yaml(_render(repo_root / "helm/vector"))["transforms"][
            "federation_evidence"
        ]["source"]
        merge_at = vrl.index("merge(., object!(parsed)")
        for local in ("k8s_app", "k8s_ns", "k8s_pod", "k8s_node"):
            assert vrl.index(f"{local} = ") < merge_at, (
                f"{local} must be captured BEFORE the merge (pristine .kubernetes.*)"
            )
        assert ".app = k8s_app" in vrl
        assert ".k8s_namespace = k8s_ns" in vrl
        assert ".pod_name = k8s_pod" in vrl
        assert ".pod_node = k8s_node" in vrl
        # log_type is platform-owned, assigned after the merge (payload can't win).
        assert vrl.index('.log_type = "federation_evidence"') > merge_at
        # Same unconditional correlation id demux stamps — README / runbook /
        # catalogue all claim EVERY row has one.
        assert ".log_event_id = uuid_v7()" in vrl
        # ⛔ NOT `del(.kubernetes)`. The field allowlist rebuilds the event from
        # `kept` and then REPLACES it wholesale, so `.kubernetes` — and every
        # other unlisted key, including ones nobody remembered to name — is
        # structurally absent rather than deleted one remembered name at a time.
        # A `del()` denylist would be fail-open here (the same reasoning
        # tenant_project spells out). Pin the replacement, and pin that it is the
        # LAST thing the transform does, so nothing can be re-added after it.
        assert ". = kept" in vrl, (
            "the evidence row must be rebuilt from the keep-list and replaced "
            "wholesale — a del()-per-field denylist is fail-open"
        )
        assert vrl.rstrip().endswith(". = kept"), (
            "`. = kept` must be the final statement; any assignment after it "
            "would re-admit a field the allowlist just excluded"
        )
        # Comment lines mention `del()` when explaining WHY it is not used, so
        # this has to look at code only — a substring scan over the whole VRL
        # would match the prose and never fail on a real regression.
        code = "\n".join(
            ln for ln in vrl.splitlines() if not ln.lstrip().startswith("#")
        )
        assert "del(" not in code, (
            "no del() in the evidence path — exclusion is by allowlist rebuild, "
            "and a stray del() would signal a drift back to denylist thinking"
        )

    @_needs_helm
    def test_evidence_never_reaches_the_tenant_projection(self, repo_root: Path) -> None:
        """Issue #1234 acceptance criterion 3, pinned at render level (the
        behavioural proof is the `no_outputs_from` in projection_tests.yaml):
        no tenant-facing transform or sink may consume the evidence stream."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets=_PROJECTION_SETS))
        assert cfg["transforms"]["tenant_project"]["inputs"] == ["demux"], (
            "tenant_project must fork off demux ONLY — the evidence stream is "
            "platform-only (0:0) and must be unreachable from any tenant partition"
        )
        for name, sink in cfg["sinks"].items():
            if name.startswith("vl_tenant_"):
                assert "federation_evidence" not in sink["inputs"], (
                    f"{name} must not consume the evidence stream"
                )

    @_needs_helm
    def test_evidence_channel_survives_reuse_values_upgrade(self, repo_root: Path) -> None:
        """`helm upgrade --reuse-values` from a pre-#1234 release carries no
        `evidenceChannel:` map. The naive `.Values.evidenceChannel.podLabelValue`
        form nil-pointer-panics the render — the same trap the `audit:` block hit
        on the 0.3.x → 0.4.0 path. Render with the whole block deleted."""
        cfg = _vector_yaml(_render(repo_root / "helm/vector", sets={"evidenceChannel": "null"}))
        routes = cfg["transforms"]["origin_split"]["route"]
        assert "tenant-api" in routes["evidence"], "must fall back to the shipped default"
        vrl = cfg["transforms"]["federation_evidence"]["source"]
        for event in _EVIDENCE_EVENTS:
            assert f'ev != "{event}"' in vrl
        # #1288: the namespace binding must also survive the nil path — an
        # upgrader carrying no evidenceChannel map is exactly who would
        # otherwise silently keep the label-only (unbound) split.
        assert '.kubernetes.pod_namespace == "tenant-api"' in routes["evidence"], (
            "the producer binding must default ON through --reuse-values, not "
            "quietly degrade to label-only matching"
        )
        assert '.kubernetes.pod_namespace == "monitoring"' in routes["gateway"]

    @_needs_helm
    def test_producer_namespace_binding_is_on_by_default(self, repo_root: Path) -> None:
        """#1288: both origin-split branches must require the producer's
        namespace, not just its pod label. A pod declares its own labels; it
        cannot choose the namespace it runs in, so this conjunct is the only
        self-assignment-proof half of the split."""
        routes = _vector_yaml(_render(repo_root / "helm/vector"))["transforms"]["origin_split"]["route"]
        assert '.kubernetes.pod_namespace == "tenant-api"' in routes["evidence"]
        assert '.kubernetes.pod_namespace == "monitoring"' in routes["gateway"]

    @_needs_helm
    def test_empty_namespace_disables_binding_without_reverting_to_default(
        self, repo_root: Path
    ) -> None:
        """#1288 escape hatch. `""` must DISABLE the binding, not fall back to
        the shipped default — the trap a bare `default "tenant-api" $ec.X` walks
        into, because Go templates treat "" as falsy. The template distinguishes
        absent (take the default) from explicitly empty (disabled) via hasKey;
        this test is what stops that from being 'simplified' back."""
        routes = _vector_yaml(_render(
            repo_root / "helm/vector",
            sets={"evidenceChannel.podNamespace": ""},
        ))["transforms"]["origin_split"]["route"]
        assert "pod_namespace" not in routes["evidence"], (
            'explicitly empty podNamespace must disable the binding, not silently '
            'restore the default'
        )
        # the other branch is independent
        assert '.kubernetes.pod_namespace == "monitoring"' in routes["gateway"]

    def test_namespace_defaults_have_exactly_one_definition(self, repo_root: Path) -> None:
        """#1288: the producer-namespace defaults and the absent-vs-empty rule
        live ONLY in _helpers.tpl. Two templates consume them — configmap.yaml
        renders the route conditions, NOTES.txt prints them back to the operator
        after install — so a second copy would let what we ENFORCE drift from
        what we TELL the operator we enforce, and only one of the two would be
        covered by the render tests above. This is the hand-copied-contract
        drift class, so pin it at the source: both callers must `include`, and
        neither may hardcode the default.

        No binary needed — deliberately, so it runs even where helm is absent.
        """
        tpl = repo_root / "helm/vector/templates"
        helpers = (tpl / "_helpers.tpl").read_text(encoding="utf-8")
        for name, default in (("vector.evidenceNamespace", '"tenant-api"'),
                              ("vector.gatewayNamespace", '"monitoring"')):
            assert f'define "{name}"' in helpers, f"{name} must be defined in _helpers.tpl"
            assert default in helpers, f"{name}'s default must live in _helpers.tpl"

        for fname in ("configmap.yaml", "NOTES.txt"):
            src = (tpl / fname).read_text(encoding="utf-8")
            assert 'include "vector.evidenceNamespace"' in src, (
                f"{fname} must take the evidence namespace from the shared helper"
            )
            assert 'include "vector.gatewayNamespace"' in src, (
                f"{fname} must take the gateway namespace from the shared helper"
            )
            # The giveaway for a reintroduced copy: the hasKey fallback rebuilt
            # locally. The helper is the only place that pattern belongs.
            for key in ("podNamespace", "gatewayNamespace"):
                assert f'hasKey $ec "{key}"' not in src, (
                    f"{fname} re-implements the {key} fallback — that is the second "
                    f"copy this test exists to prevent; call the helper instead"
                )

    @_needs_helm
    def test_namespace_values_reject_malformed_input(self, repo_root: Path) -> None:
        """#1288: a malformed namespace matches no pod at all, which looks
        exactly like a correctly-configured-but-quiet branch — the silent-
        failure shape this change exists to remove. The schema pattern is
        DNS-1123-label-or-empty so a typo fails at render instead.
        `""` must still be accepted: it is the documented disable switch."""
        for value, should_render in (
            ("tenant-api", True),
            ("", True),                        # the disable switch
            ("Tenant-API", False),             # uppercase is not a valid ns
            ("tenant api", False),             # whitespace
            ("ns-trailing-", False),           # DNS-1123 forbids a trailing dash
            ("app.kubernetes.io/name=x", False),   # a selector, not a namespace
        ):
            r = _render_result(repo_root / "helm/vector",
                               string_sets={"evidenceChannel.podNamespace": value})
            if should_render:
                assert r.returncode == 0, f"{value!r} must be accepted: {r.stderr!r}"
            else:
                assert r.returncode != 0, (
                    f"{value!r} is not a valid namespace and must be rejected at "
                    f"render, not silently match nothing"
                )

    @_needs_helm
    def test_evidence_events_membership_is_enforced(self, repo_root: Path) -> None:
        """#1288 gap 2. The pre-existing guard only rejected an EMPTY list. A
        values override that merely DROPS the revocation event renders a valid
        pipeline where every revocation row aborts while the heartbeat still
        flows — `channel_up` 1, `tamper_suspected` 0, permanently green with no
        evidence, and no attacker needed. Both members are load-bearing."""
        for dropped, remaining in (
            ("federation_token_revoked", "federation_revocation_channel_heartbeat"),
            ("federation_revocation_channel_heartbeat", "federation_token_revoked"),
        ):
            r = _render_result(repo_root / "helm/vector",
                               sets={"evidenceChannel.events": "{%s}" % remaining})
            assert r.returncode != 0, f"dropping {dropped} must fail render"
            assert dropped in r.stderr, (
                f"the render error must name the missing member; got: {r.stderr!r}"
            )

    @_needs_helm
    def test_evidence_keepfields_membership_is_enforced(self, repo_root: Path) -> None:
        """#1288 gap 2, field half. Each of the three is read by name on the
        reconcile path (event=LogsQL filter, token_id=identity of a suspected
        row, expires_at=the expiry-vs-tamper discriminator), so narrowing past
        them empties the detection plane silently."""
        for dropped, remaining in (
            ("token_id", "event,expires_at"),
            ("expires_at", "event,token_id"),
            ("event", "token_id,expires_at"),
        ):
            r = _render_result(repo_root / "helm/vector",
                               sets={"evidenceChannel.keepFields": "{%s}" % remaining})
            assert r.returncode != 0, f"dropping {dropped} must fail render"
            assert dropped in r.stderr

    @_needs_helm
    def test_empty_event_allowlist_fails_render(self, repo_root: Path) -> None:
        """An operator emptying `evidenceChannel.events` is trying to disable the
        PII filter. It must fail LOUDLY at render, not render a broken VRL
        condition (or, worse, one that admits every tenant-api line into the
        audit store). Uses a values file because `--set k=null` deletes the key
        in Helm rather than setting it empty."""
        with tempfile.TemporaryDirectory() as d:
            vf = Path(d) / "empty-events.yaml"
            vf.write_text("evidenceChannel:\n  events: []\n", encoding="utf-8")
            r = subprocess.run(
                ["helm", "template", "t", str(repo_root / "helm/vector"),
                 "-n", "monitoring", "-f", str(vf)],
                capture_output=True, text=True, timeout=30)
        assert r.returncode != 0, "an empty evidence allowlist must fail render"
        assert "evidenceChannel.events must list at least one" in r.stderr

    @_needs_helm
    def test_misspelled_evidence_channel_key_fails_schema(self, repo_root: Path) -> None:
        """values.schema.json must constrain `evidenceChannel` with
        additionalProperties:false. A MISSPELLED key is the failure mode no other
        guard sees: `podLabelvalue: tenant-api` leaves the real `podLabelValue`
        unset, the template silently falls back to its shipped default, and the
        operator's intended routing never takes effect — no {{ fail }}, no
        `vector validate` error, no behavioural test. (A misspelled VALUE cannot
        be caught by any schema; it surfaces as an empty channel, which is why
        the runbook §8.7 note on the unmonitored discard path exists.)

        This test is also what stops the whole evidenceChannel schema block from
        being deleted silently: without it, `additionalProperties` reverts to the
        JSON-Schema default of true and this render starts passing."""
        r = _render_result(repo_root / "helm/vector",
                           sets={"evidenceChannel.podLabelvalue": "tenant-api"})
        assert r.returncode != 0, (
            "a misspelled evidenceChannel key must be rejected by values.schema.json "
            "(additionalProperties: false), not silently ignored"
        )
        assert "podLabelvalue" in r.stderr, (
            f"schema error must name the offending key; got: {r.stderr!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Projection gate wiring (#908 PR-2) — init-container + config-dir + drift seam
# ──────────────────────────────────────────────────────────────────────────────

class TestProjectionGateWiring:
    @_needs_helm
    def test_init_container_present_and_wired_when_projections_set(self, repo_root: Path) -> None:
        """The fail-closed gate runs as a Vector init-container: it reads the
        registry + projections (read-only), WRITES the config-dir Vector loads, and
        honours the degrade/enforce toggle. Vector then loads --config-dir from the
        gate-populated emptyDir, not the ConfigMap directly."""
        docs = _render(repo_root / "helm/vector", sets=_PROJECTION_SETS)
        spec = [d for d in docs if d.get("kind") == "DaemonSet"][0]["spec"]["template"]["spec"]
        inits = {c["name"]: c for c in spec.get("initContainers", [])}
        assert "projection-gate" in inits, "gate init-container must run when projections are set"
        gate = inits["projection-gate"]
        assert gate["args"][:2] == ["--registry", "/conf.d/_account_registry.yaml"]
        assert "/staging/tenant-projections.json" in gate["args"]
        assert "--config-dir" in gate["args"]
        mounts = {m["name"]: m for m in gate["volumeMounts"]}
        assert mounts["registry"].get("readOnly") is True, "registry is read-only (gate never writes it)"
        assert mounts["staging"].get("readOnly") is True
        assert mounts["config"].get("readOnly", False) is False, "gate must WRITE the config-dir emptyDir"
        vector = [c for c in spec["containers"] if c["name"] == "vector"][0]
        assert "--config-dir" in vector["args"], "Vector loads the gate-populated config-dir"
        vols = {v["name"]: v for v in spec["volumes"]}
        assert "emptyDir" in vols["config"], "Vector's config-dir is the init-populated emptyDir"
        assert vols["staging"]["configMap"]["name"].endswith("-config")
        assert vols["registry"]["configMap"]["name"], "registry ConfigMap must be wired"

    @_needs_helm
    def test_no_gate_overhead_when_projections_empty(self, repo_root: Path) -> None:
        """Default (no projections): no init-container, Vector mounts the ConfigMap
        directly and loads the single base config — zero gate overhead, byte-for-byte
        the prior single-tenant deployment."""
        docs = _render(repo_root / "helm/vector")
        spec = [d for d in docs if d.get("kind") == "DaemonSet"][0]["spec"]["template"]["spec"]
        assert not any(c["name"] == "projection-gate" for c in spec.get("initContainers", []))
        vector = [c for c in spec["containers"] if c["name"] == "vector"][0]
        assert "--config" in vector["args"] and "/etc/vector/vector.yaml" in vector["args"]
        vols = {v["name"]: v for v in spec["volumes"]}
        assert "configMap" in vols["config"], "default mounts the ConfigMap directly"

    @_needs_helm
    def test_gate_input_matches_rendered_sinks_and_map(self, repo_root: Path) -> None:
        """DRIFT-SEAM closure: the gate validates `tenant-projections.json`, but
        Vector ROUTES on the rendered account_map (tenant_project VRL) + the
        vl_tenant_<id> sinks. All three derive from .Values.tenantProjections — pin
        them in lockstep so a template bug can't let the gate bless a set that
        diverges from what Vector actually writes (gate validates X, Vector writes Y)."""
        docs = _render(repo_root / "helm/vector", sets=_PROJECTION_SETS)
        cm = [d for d in docs if d.get("kind") == "ConfigMap"
              and "vector-config" in d["metadata"]["name"]][0]
        gate_pairs = {(p["tenantId"], p["accountId"])
                      for p in json.loads(cm["data"]["tenant-projections.json"])}
        assert gate_pairs, "fixture must project at least one tenant"
        cfg = _vector_yaml(docs)
        vrl = cfg["transforms"]["tenant_project"]["source"]
        for tid, aid in gate_pairs:
            assert f'"{tid}": {aid}' in vrl, f"account_map must carry {tid}->{aid} (no drift, no hash)"
            sink = cfg["sinks"][f"vl_tenant_{aid}"]
            assert sink["request"]["headers"]["AccountID"] == str(aid), "sink header pinned to the gate's accountId"
            assert sink["inputs"] == [f"tenant_route.t_{aid}"], "sink consumes its own route"
        rendered = {int(n.rsplit("_", 1)[1]) for n in cfg["sinks"] if n.startswith("vl_tenant_")}
        assert rendered == {a for _, a in gate_pairs}, (
            "rendered tenant sinks must be EXACTLY the gate-validated accountIds — no "
            "orphan partition the gate never checked, none missing it expects"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Projection-gate verdict-metric observability (#908 PR-3a) — init --metrics-file,
# metrics-exposer sidecar, headless scrape Service
# ──────────────────────────────────────────────────────────────────────────────

class TestProjectionGateMetricsWiring:
    @staticmethod
    def _daemonset(repo_root: Path, *, sets: dict[str, str] | None = None) -> dict:
        docs = _render(repo_root / "helm/vector", sets=sets)
        return [d for d in docs if d.get("kind") == "DaemonSet"][0]["spec"]["template"]["spec"]

    @_needs_helm
    def test_init_writes_verdict_to_shared_emptydir(self, repo_root: Path) -> None:
        """The gate init-container must write its verdict to the shared gate-metrics
        emptyDir (`--metrics-file`), mounted READ-WRITE — that file is the input the
        exposer sidecar re-serves. Without this wiring the verdict metric never exists."""
        spec = self._daemonset(repo_root, sets=_PROJECTION_SETS)
        gate = {c["name"]: c for c in spec["initContainers"]}["projection-gate"]
        args = gate["args"]
        assert "--metrics-file" in args, "gate must be told where to write the verdict"
        mf = args[args.index("--metrics-file") + 1]
        assert mf == "/gate-metrics/gate.prom"
        mounts = {m["name"]: m for m in gate["volumeMounts"]}
        assert "gate-metrics" in mounts, "init must mount the shared verdict volume"
        assert mounts["gate-metrics"]["mountPath"] == "/gate-metrics"
        # The init WRITES here (not readOnly) — distinct from its read-only registry/staging.
        assert mounts["gate-metrics"].get("readOnly", False) is False
        # And the destination dir is a pod-local emptyDir (absent-on-pod-death semantics).
        vols = {v["name"]: v for v in spec["volumes"]}
        assert "emptyDir" in vols["gate-metrics"], "verdict volume must be pod-local emptyDir"

    @_needs_helm
    def test_metrics_exposer_sidecar_present_and_readonly(self, repo_root: Path) -> None:
        """A long-lived sidecar re-serves the verdict file over HTTP. It runs
        serve_metrics.py (NOT the validator — it never re-evaluates), mounts the
        shared emptyDir READ-ONLY, exposes a named port, and has resources set
        (kube-linter L4). It lives beside Vector so the series goes absent on pod death."""
        spec = self._daemonset(repo_root, sets=_PROJECTION_SETS)
        sidecars = {c["name"]: c for c in spec["containers"]}
        assert "projection-gate-metrics" in sidecars, "metrics-exposer sidecar must run when projections are set"
        sc = sidecars["projection-gate-metrics"]
        # Command override → the exposer, not the validator entrypoint.
        assert sc["command"] == ["python3", "/usr/local/bin/serve_metrics.py"]
        assert "--metrics-file" in sc["args"] and "/gate-metrics/gate.prom" in sc["args"]
        # Same image as the init-container (one image, two roles).
        assert sc["image"].startswith("ghcr.io/vencil/vector-projection-gate")
        mounts = {m["name"]: m for m in sc["volumeMounts"]}
        assert mounts["gate-metrics"].get("readOnly") is True, "exposer only READS the verdict"
        port = {p["name"]: p for p in sc["ports"]}["gate-metrics"]
        assert port["containerPort"] == 9599
        assert sc["resources"]["requests"]["cpu"], "sidecar must set resources (kube-linter L4)"

    @_needs_helm
    def test_scrape_service_headless_and_annotated(self, repo_root: Path) -> None:
        """The verdict is scraped via a dedicated headless Service (annotation-based
        SD, like vector-metrics) — gated on tenantProjections, NOT metrics.enabled, so
        the gate observability does not require turning on Vector self-telemetry.
        Headless so per-node pods aren't collapsed to one address."""
        docs = _render(repo_root / "helm/vector", sets=_PROJECTION_SETS)
        svcs = [d for d in docs if d.get("kind") == "Service"
                and d["metadata"]["name"].endswith("-projection-gate-metrics")]
        assert len(svcs) == 1, "projection-gate metrics Service must render when projections are set"
        svc = svcs[0]
        ann = svc["metadata"]["annotations"]
        assert ann["prometheus.io/scrape"] == "true"
        assert ann["prometheus.io/port"] == "9599"
        assert svc["spec"]["clusterIP"] == "None", "headless — do not collapse N nodes to one address"
        assert svc["spec"]["ports"][0]["targetPort"] == "gate-metrics"

    @_needs_helm
    def test_no_metrics_overhead_when_projections_empty(self, repo_root: Path) -> None:
        """Default (no projections): no exposer sidecar, no gate-metrics volume, no
        scrape Service — zero observability overhead, matching the no-gate default."""
        spec = self._daemonset(repo_root)
        assert not any(c["name"] == "projection-gate-metrics" for c in spec["containers"])
        assert not any(v["name"] == "gate-metrics" for v in spec["volumes"])
        docs = _render(repo_root / "helm/vector")
        assert not any(d.get("kind") == "Service"
                       and d["metadata"]["name"].endswith("-projection-gate-metrics") for d in docs)

    @_needs_helm
    def test_scrape_service_independent_of_metrics_enabled(self, repo_root: Path) -> None:
        """The gate scrape Service must render even with Vector's own metrics.enabled
        OFF (the default) — a degrade can happen with self-telemetry off, so its
        observability must not be coupled to it."""
        docs = _render(repo_root / "helm/vector",
                       sets={**_PROJECTION_SETS, "metrics.enabled": "false"})
        assert any(d.get("kind") == "Service"
                   and d["metadata"]["name"].endswith("-projection-gate-metrics") for d in docs), (
            "gate scrape Service must not depend on metrics.enabled"
        )
        # And the Vector self-telemetry Service is indeed absent in this config.
        assert not any(d.get("kind") == "Service"
                       and d["metadata"]["name"].endswith("-metrics")
                       and not d["metadata"]["name"].endswith("-projection-gate-metrics")
                       for d in docs)

    @_needs_helm
    def test_gate_containers_are_least_privilege_non_root(self, repo_root: Path) -> None:
        """#908 PR-3 m3: the gate init-container + exposer sidecar run LEAST-PRIVILEGE
        (non-root, drop ALL caps, NO DAC_READ_SEARCH) — they only read a ConfigMap
        registry + the emptyDirs. The Vector container is the ONLY one that stays root
        with DAC_READ_SEARCH (it reads root-owned /var/log host files). Guards the
        security split: a regression that reused the root context for the gate, or
        dropped root from Vector, both fail here."""
        spec = self._daemonset(repo_root, sets=_PROJECTION_SETS)
        gate = {c["name"]: c for c in spec["initContainers"]}["projection-gate"]
        sidecar = {c["name"]: c for c in spec["containers"]}["projection-gate-metrics"]
        for name, c in (("init", gate), ("sidecar", sidecar)):
            sc = c["securityContext"]
            assert sc["runAsNonRoot"] is True, f"{name} must be non-root"
            assert sc.get("runAsUser") == 65532, f"{name} must run as the image's 65532 uid"
            assert sc.get("runAsGroup") == 65532, f"{name} must pin the gid too"
            assert sc["capabilities"].get("drop") == ["ALL"], f"{name} must drop ALL caps"
            assert "add" not in sc["capabilities"], f"{name} must NOT add DAC_READ_SEARCH (only Vector needs it)"
            assert sc["readOnlyRootFilesystem"] is True
            # kube-linter L2 cannot see these containers under the DEFAULT values
            # render (tenantProjections empty → the whole block is un-rendered), so
            # THIS test + the values-projection.yaml lint variant are the enforced
            # guards — pin the privilege-escalation + seccomp knobs explicitly
            # (independent-review finding: deleting them passed every other gate).
            assert sc["allowPrivilegeEscalation"] is False, f"{name} must forbid privilege escalation"
            assert sc["seccompProfile"]["type"] == "RuntimeDefault", f"{name} must pin seccomp"
        # The Vector container is the deliberate exception — root + DAC_READ_SEARCH.
        vector = {c["name"]: c for c in spec["containers"]}["vector"]
        vsc = vector["securityContext"]
        assert vsc.get("runAsUser") == 0, "Vector must stay root to read /var/log"
        assert "DAC_READ_SEARCH" in vsc["capabilities"].get("add", []), "Vector keeps DAC_READ_SEARCH"


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
    def test_vector_validate_config_dir_merge(self, repo_root: Path) -> None:
        """`vector validate --config-dir` over the ACTUAL two files Vector loads at
        runtime (#908 PR-2): the base + the tenant fragment, merged by VECTOR ITSELF
        — not the Python-modelled `_vector_yaml` merge the render tests use. Catches
        a fragment-split bug (e.g. a sink in the fragment whose `tenant_route` input
        moved, or a section that fails to merge across files) that single-file
        validation cannot see. The gate writes the staging `vector.yaml` into the
        config-dir as `00-base.yaml` (its `_BASE_NAME`), so mirror that filename;
        `tenant-projections.json` is NEVER placed here (the gate reads it from
        /staging only)."""
        docs = _render(repo_root / "helm/vector", sets=_PROJECTION_SETS)
        cm = [d for d in docs if d.get("kind") == "ConfigMap"
              and "vector-config" in d["metadata"]["name"]][0]
        with tempfile.TemporaryDirectory() as d:
            confdir = Path(d)
            (confdir / "00-base.yaml").write_text(cm["data"]["vector.yaml"], encoding="utf-8")
            (confdir / "30-tenant-routing.yaml").write_text(
                cm["data"]["30-tenant-routing.yaml"], encoding="utf-8")
            r = subprocess.run(
                ["vector", "validate", "--no-environment", "--config-dir", str(confdir)],
                capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"vector validate --config-dir failed:\n{r.stdout}\n{r.stderr}"

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
