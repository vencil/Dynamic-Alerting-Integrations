"""test_helm_chart_log_aggregation.py — #539 chart invariants

Pins behaviors of the three #539 charts (helm/victorialogs, helm/vector,
helm/chargeback-aggregator) that future contributors could silently break
when editing values / templates / VRL. Each test maps to a specific
design decision documented in:

  - #539 issue body (§2 hard rule, §3 schema, §7 non-goals)
  - docs/internal/platform-log-aggregation-runbook.md
  - PR #565 description (red-team T2-2 + T4-4 patches)

Render-required tests invoke `helm template`; gated on `helm` being on
PATH (matches the openssl-gating pattern in test_federation_keygen.py).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


def _render(chart_dir: Path, *, sets: dict[str, str] | None = None,
            values_file: Path | None = None) -> list[dict]:
    """helm template the chart, return list of parsed manifests."""
    cmd = ["helm", "template", "test-release", str(chart_dir), "-n", "monitoring"]
    for k, v in (sets or {}).items():
        cmd += ["--set", f"{k}={v}"]
    if values_file is not None:
        cmd += ["-f", str(values_file)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _render_failing(chart_dir: Path, sets: dict[str, str]) -> subprocess.CompletedProcess:
    """helm template expected to fail; return CompletedProcess so caller asserts stderr."""
    cmd = ["helm", "template", "test-release", str(chart_dir), "-n", "monitoring"]
    for k, v in sets.items():
        cmd += ["--set", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)


# ──────────────────────────────────────────────────────────────────────────────
# helm/victorialogs
# ──────────────────────────────────────────────────────────────────────────────

class TestVictoriaLogs:
    """victorialogs chart invariants."""

    def test_chart_yaml_valid(self, repo_root: Path) -> None:
        chart = yaml.safe_load((repo_root / "helm/victorialogs/Chart.yaml").read_text(encoding="utf-8"))
        assert chart["name"] == "victorialogs"
        assert chart["apiVersion"] == "v2"
        # Image tag pinned (not latest)
        values = yaml.safe_load((repo_root / "helm/victorialogs/values.yaml").read_text(encoding="utf-8"))
        assert values["image"]["tag"] and values["image"]["tag"] != "latest"

    def test_persistence_default_size_matches_runbook(self, repo_root: Path) -> None:
        """runbook §5 capacity formula expects ~30GiB default; smaller silently
        produces a too-small PVC on cluster install."""
        values = yaml.safe_load((repo_root / "helm/victorialogs/values.yaml").read_text(encoding="utf-8"))
        assert values["persistence"]["size"] == "30Gi", (
            "default size should match runbook §5 capacity formula"
        )

    @_needs_helm
    def test_networkpolicy_enabled_by_default(self, repo_root: Path) -> None:
        """Red-team T2-2: vlogs HTTP API has no built-in auth; NetworkPolicy
        is the perimeter. Disabling it by default would re-open the gap."""
        docs = _render(repo_root / "helm/victorialogs")
        np = [d for d in docs if d.get("kind") == "NetworkPolicy"]
        assert len(np) == 1, "NetworkPolicy must render by default"

    @_needs_helm
    def test_networkpolicy_locks_to_three_consumers(self, repo_root: Path) -> None:
        """Red-team T2-2 mitigation: only vector / chargeback-aggregator /
        grafana can reach :9428. If anyone deletes a row from
        allowedPodSelectors the lockdown silently loosens."""
        docs = _render(repo_root / "helm/victorialogs")
        np = [d for d in docs if d.get("kind") == "NetworkPolicy"][0]
        peers = np["spec"]["ingress"][0].get("from", [])
        labels = {tuple(sorted(p["podSelector"]["matchLabels"].items())) for p in peers if "podSelector" in p}
        expected = {
            (("app.kubernetes.io/name", "vector"),),
            (("app.kubernetes.io/name", "chargeback-aggregator"),),
            (("app", "grafana"),),
        }
        assert expected.issubset(labels), f"missing consumers: {expected - labels}"


# ──────────────────────────────────────────────────────────────────────────────
# helm/vector
# ──────────────────────────────────────────────────────────────────────────────

class TestVector:
    """vector chart invariants."""

    def test_chart_yaml_valid(self, repo_root: Path) -> None:
        chart = yaml.safe_load((repo_root / "helm/vector/Chart.yaml").read_text(encoding="utf-8"))
        assert chart["name"] == "vector"
        assert chart["apiVersion"] == "v2"

    @_needs_helm
    def test_vrl_has_rulegroup_override(self, repo_root: Path) -> None:
        """Phase 2: Prometheus rule evaluations must bucket as `platform`, not
        billed to a tenant. The VRL `ruleGroup`-presence check is what makes
        that true; removing it silently mis-bills."""
        docs = _render(repo_root / "helm/vector")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert "ruleGroup" in vrl, "VRL must check ruleGroup for platform-bucket override"
        assert "del(.tenant_id)" in vrl, "ruleGroup override must del the regex-extracted tenant"

    @_needs_helm
    def test_vrl_tenant_regex_matches_tenant_not_tenant_id(self, repo_root: Path) -> None:
        """federation-proxy injects `{tenant="X"}` (not `tenant_id`); regex MUST
        match the actual label. PR #505 / federation-label-enrichment-audit."""
        docs = _render(repo_root / "helm/vector")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert 'tenant="' in vrl, "regex must match `tenant=\"X\"` (the data-layer label)"
        assert 'tenant_id="(?P' not in vrl, "regex must NOT match tenant_id= (would never fire)"

    @_needs_helm
    def test_vrl_hoists_cost_fields_in_seconds(self, repo_root: Path) -> None:
        """Phase 2 self-review unit fix: Prometheus emits *Total*Time in
        SECONDS. Renaming to `_ms` would under-bill by 1000×."""
        docs = _render(repo_root / "helm/vector")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert ".exec_time_s" in vrl and ".eval_time_s" in vrl
        assert ".exec_time_ms" not in vrl and ".eval_time_ms" not in vrl

    @_needs_helm
    def test_buffer_block_shorthand_guard(self, repo_root: Path) -> None:
        """Phase 3 self-review: _buffer_when_full=block violates §2; must
        fail template render."""
        r = _render_failing(repo_root / "helm/vector", {
            "additionalSinks[0].name": "bad",
            "additionalSinks[0].type": "http",
            "additionalSinks[0]._buffer_when_full": "block",
        })
        assert r.returncode != 0
        assert "§2 hard rule" in r.stderr, "fail message must reference §2"

    @_needs_helm
    def test_buffer_block_full_block_guard(self, repo_root: Path) -> None:
        """Red-team T4-4: operator-supplied `buffer: {when_full: block}` MUST
        also fail (the shorthand-only guard was bypassable)."""
        r = _render_failing(repo_root / "helm/vector", {
            "additionalSinks[0].name": "bad",
            "additionalSinks[0].type": "http",
            "additionalSinks[0].buffer.type": "memory",
            "additionalSinks[0].buffer.when_full": "block",
        })
        assert r.returncode != 0
        assert "§2 hard rule" in r.stderr

    @_needs_helm
    def test_vrl_origin_filter_default_on(self, repo_root: Path) -> None:
        """#566 T2-1: VRL must reclassify federation_audit rows whose
        pod_owner doesn't match the gateway Deployment prefix as
        suspicious_audit. If someone removes the filter the spoof
        detection silently disappears."""
        docs = _render(repo_root / "helm/vector")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert "suspicious_audit" in vrl
        assert "starts_with(owner" in vrl
        assert '"federation-gateway-"' in vrl

    @_needs_helm
    def test_vrl_origin_filter_disabled_with_empty_prefix(self, repo_root: Path) -> None:
        """Empty audit.gatewayPodOwnerPrefix disables the check (escape hatch
        for non-default Deployment names). Must skip the VRL clause entirely
        — leaving a no-op `if starts_with("", ...)` would be a foot-gun."""
        docs = _render(repo_root / "helm/vector", sets={"audit.gatewayPodOwnerPrefix": ""})
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert "suspicious_audit" not in vrl

    @_needs_helm
    def test_vrl_origin_filter_nil_audit_block_safe(self, repo_root: Path) -> None:
        """`helm upgrade --reuse-values` from 0.3.x → 0.4.0 doesn't carry the
        new `audit:` map; without nil-safe templating the render panics with
        `nil pointer evaluating interface{}.gatewayPodOwnerPrefix`. Exercise
        the upgrade path by deleting the whole audit block."""
        docs = _render(repo_root / "helm/vector", sets={"audit": "null"})
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        # Render must succeed; the clause is correctly absent (treated as empty).
        vrl = yaml.safe_load(cm["data"]["vector.yaml"])["transforms"]["demux"]["source"]
        assert "suspicious_audit" not in vrl

    @_needs_helm
    def test_additional_sink_buffer_memory_default(self, repo_root: Path) -> None:
        """#566 B-1: default `memory` buffer (current Phase 3 behavior) — no
        regression on existing deployments."""
        docs = _render(repo_root / "helm/vector", sets={
            "additionalSinks[0].name": "t",
            "additionalSinks[0].type": "http",
            "additionalSinks[0].uri": "http://x",
        })
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        sink = yaml.safe_load(cm["data"]["vector.yaml"])["sinks"]["t"]
        buf = sink["buffer"]
        assert buf["type"] == "memory"
        assert buf["when_full"] == "drop_newest"
        assert buf["max_events"] == 10000
        assert "max_size" not in buf, "memory buffer must not get max_size (Vector rejects)"

    @_needs_helm
    def test_additional_sink_buffer_disk_mode(self, repo_root: Path) -> None:
        """#566 B-1: `_buffer_type: disk` renders disk buffer with `max_size`
        (bytes) instead of `max_events`. Vector rejects the wrong knob, so
        the chart must switch field names — getting this wrong = sink fails
        to start with a config-validation error."""
        docs = _render(repo_root / "helm/vector", sets={
            "additionalSinks[0].name": "t",
            "additionalSinks[0].type": "http",
            "additionalSinks[0].uri": "http://x",
            "additionalSinks[0]._buffer_type": "disk",
        })
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        buf = yaml.safe_load(cm["data"]["vector.yaml"])["sinks"]["t"]["buffer"]
        assert buf["type"] == "disk"
        assert buf["when_full"] == "drop_newest"  # §2 stays default even with disk
        assert "max_size" in buf
        assert "max_events" not in buf, "disk buffer must not get max_events (Vector rejects)"

    @_needs_helm
    def test_additional_sink_buffer_disk_max_size_renders_as_integer(self, repo_root: Path) -> None:
        """#566 B-1 runtime catch: helm/sprig round-trips large numbers
        through float, so `max_size: 268435488` rendered as `2.68435488e+08`
        — Vector's BufferConfig rejects with an unhelpful "untagged enum"
        error. The `| int64` cast forces integer rendering. Without it,
        every operator who sets a disk buffer size between 2^28 and 2^53
        hits a cryptic config-parse failure."""
        docs = _render(repo_root / "helm/vector", sets={
            "additionalSinks[0].name": "t",
            "additionalSinks[0].type": "http",
            "additionalSinks[0]._buffer_type": "disk",
            "additionalSinks[0]._buffer_max_size": "268435488",  # 256 MiB + 32 bytes — the smallest accepted size
        })
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "vector-config" in d["metadata"]["name"]][0]
        # parse-then-introspect: PyYAML would silently coerce the float
        # form back to a number; the bug is in the STRING form Vector sees
        # before parsing. So inspect the raw text.
        raw = cm["data"]["vector.yaml"]
        # Find the max_size line within the t sink block
        for line in raw.splitlines():
            if "max_size:" in line:
                value_part = line.split("max_size:", 1)[1].strip()
                assert value_part == "268435488", (
                    f"max_size must render as bare integer, got {value_part!r} "
                    "(float scientific notation breaks Vector's BufferConfig parser)"
                )
                break
        else:
            raise AssertionError("max_size line not found in rendered vector.yaml")

    @_needs_helm
    def test_additional_sink_buffer_unknown_type_fails(self, repo_root: Path) -> None:
        """Anything other than memory/disk must fail template render —
        otherwise Vector starts but the buffer config is wrong."""
        r = _render_failing(repo_root / "helm/vector", {
            "additionalSinks[0].name": "t",
            "additionalSinks[0].type": "http",
            "additionalSinks[0]._buffer_type": "ramdisk",
        })
        assert r.returncode != 0
        assert "must be" in r.stderr and "memory" in r.stderr

    @_needs_helm
    def test_image_digest_knob(self, repo_root: Path) -> None:
        """#566 T5: image.digest empty → `repo:tag`; digest set →
        `repo:tag@sha256:...`. Templating the @-form wrong silently
        ships a colon-without-digest and kubelet falls back to tag pulls
        — defeating the pinning."""
        # Empty default → no @
        docs = _render(repo_root / "helm/vector")
        ds = [d for d in docs if d.get("kind") == "DaemonSet"][0]
        img = ds["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "@" not in img, "default empty digest must NOT add @"
        # Set digest → @sha256: appended
        docs = _render(repo_root / "helm/vector", sets={
            "image.digest": "sha256:0123456789abcdef",
        })
        ds = [d for d in docs if d.get("kind") == "DaemonSet"][0]
        img = ds["spec"]["template"]["spec"]["containers"][0]["image"]
        assert img.endswith("@sha256:0123456789abcdef")

    @_needs_helm
    def test_extra_env_renders_into_daemonset(self, repo_root: Path) -> None:
        """Phase 3 self-review: SIEM creds need to flow via extraEnv. If the
        knob doesn't render, the documented example silently breaks (token
        stays the literal string `${SPLUNK_TOKEN}`)."""
        docs = _render(repo_root / "helm/vector", sets={
            "extraEnv[0].name": "SPLUNK_TOKEN",
            "extraEnv[0].value": "smoke-test-token",
        })
        ds = [d for d in docs if d.get("kind") == "DaemonSet"][0]
        env = ds["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env}
        assert "SPLUNK_TOKEN" in names


# ──────────────────────────────────────────────────────────────────────────────
# helm/chargeback-aggregator
# ──────────────────────────────────────────────────────────────────────────────

class TestChargebackAggregator:
    """chargeback-aggregator chart invariants."""

    def test_chart_yaml_valid(self, repo_root: Path) -> None:
        chart = yaml.safe_load((repo_root / "helm/chargeback-aggregator/Chart.yaml").read_text(encoding="utf-8"))
        assert chart["name"] == "chargeback-aggregator"
        assert chart["apiVersion"] == "v2"

    def test_pvc_keeps_history_on_uninstall(self, repo_root: Path) -> None:
        """Phase 2 self-review: PVC `helm.sh/resource-policy: keep` prevents
        accidental uninstall from wiping 90d audit trail."""
        pvc_text = (repo_root / "helm/chargeback-aggregator/templates/pvc.yaml").read_text(encoding="utf-8")
        assert "helm.sh/resource-policy: keep" in pvc_text

    @_needs_helm
    def test_aggregate_script_compiles(self, repo_root: Path) -> None:
        """The Python aggregator is embedded in a ConfigMap (147 lines).
        py_compile catches syntax breaks introduced when editing it
        in-place — easy to miss without local Python tooling."""
        import py_compile
        import tempfile
        docs = _render(repo_root / "helm/chargeback-aggregator")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "script" in d["metadata"]["name"]][0]
        script = cm["data"]["aggregate.py"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        py_compile.compile(path, doraise=True)

    @_needs_helm
    def test_aggregate_queries_both_legacy_and_current_unit_field(self, repo_root: Path) -> None:
        """Phase 2 NaN-coalesce fix: during chart-upgrade transition window
        VictoriaLogs has rows under both `exec_time_ms` (legacy 0.1.0) and
        `exec_time_s` (>=0.1.1). The script MUST query both — querying only
        one returns NaN for stream slots that have just the other."""
        docs = _render(repo_root / "helm/chargeback-aggregator")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "script" in d["metadata"]["name"]][0]
        script = cm["data"]["aggregate.py"]
        assert "exec_time_s" in script and "exec_time_ms_legacy" in script
