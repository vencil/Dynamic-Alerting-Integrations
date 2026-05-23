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
    def test_aggregate_writes_sha256_sidecar(self, repo_root: Path) -> None:
        """#566 T2-4: every chargeback CSV gets a `.sha256` sidecar in
        `sha256sum -c` format so finance can spot-verify. If anyone
        removes the hash write, tamper detection silently degrades."""
        docs = _render(repo_root / "helm/chargeback-aggregator")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "script" in d["metadata"]["name"]][0]
        script = cm["data"]["aggregate.py"]
        assert "hashlib.sha256" in script
        assert ".csv.sha256" in script
        # sha256sum format: "<hash>  <filename>" (two spaces, basename only)
        assert '{csv_sha}  {out_path.name}' in script

    @_needs_helm
    def test_aggregate_appends_to_manifest(self, repo_root: Path) -> None:
        """#566 T2-4 layer 2: append-only manifest.jsonl persists every
        run's hash + metadata. Must be `a` (append) not `w` (write) —
        getting this wrong = every run wipes prior history, defeating
        the tamper-detection point."""
        docs = _render(repo_root / "helm/chargeback-aggregator")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "script" in d["metadata"]["name"]][0]
        script = cm["data"]["aggregate.py"]
        assert "manifest.jsonl" in script
        assert 'manifest_path.open("a"' in script, "manifest MUST be append-only"
        # Retention prune logic must skip the manifest
        assert "Manifest is NEVER pruned" in script

    @_needs_helm
    def test_aggregate_prunes_sha256_sidecar_with_csv(self, repo_root: Path) -> None:
        """When the CSV is pruned past retention, the .sha256 sidecar
        MUST also be deleted — orphan hashes referring to deleted CSVs
        are misleading evidence."""
        docs = _render(repo_root / "helm/chargeback-aggregator")
        cm = [d for d in docs if d.get("kind") == "ConfigMap" and "script" in d["metadata"]["name"]][0]
        script = cm["data"]["aggregate.py"]
        assert "sidecar = f.with_suffix" in script
        assert "sidecar.unlink()" in script

    @_needs_helm
    def test_chargeback_extra_env_renders(self, repo_root: Path) -> None:
        """#566 Q5: chargeback CronJob accepts extraEnv (same shape as
        helm/vector). Without this, operators with multi-tenant
        VictoriaLogs can't wire account creds via Secret refs."""
        docs = _render(repo_root / "helm/chargeback-aggregator", sets={
            "extraEnv[0].name": "VICTORIALOGS_ACCOUNT_KEY",
            "extraEnv[0].value": "k",
        })
        cj = [d for d in docs if d.get("kind") == "CronJob"][0]
        env = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env}
        assert "VICTORIALOGS_ACCOUNT_KEY" in names

    @_needs_helm
    def test_victorialogs_extra_env_renders(self, repo_root: Path) -> None:
        """#566 Q5: victorialogs accepts extraEnv too — needed for
        future `-httpAuthKey` Secret-ref wiring without forking chart."""
        docs = _render(repo_root / "helm/victorialogs", sets={
            "extraEnv[0].name": "HTTP_AUTH_KEY",
            "extraEnv[0].value": "k",
        })
        dep = [d for d in docs if d.get("kind") == "Deployment"][0]
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = {e["name"] for e in env}
        assert "HTTP_AUTH_KEY" in names

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


# ──────────────────────────────────────────────────────────────────────────────
# #566 batch D — log-egress policy gate (check_log_egress_policy.py)
# ──────────────────────────────────────────────────────────────────────────────

def _egress_lint(repo_root: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    """Run the egress gate; return CompletedProcess (caller asserts rc/stdout)."""
    cmd = ["python3", str(repo_root / "scripts/tools/lint/check_log_egress_policy.py")] + extra_args
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60, cwd=repo_root)


class TestLogEgressPolicy:
    """#566 T4-1/T4-2 — egress allowlist + env-override gate."""

    @_needs_helm
    def test_default_charts_pass(self, repo_root: Path) -> None:
        """Default chart values (empty additionalSinks, only the chart's own
        fieldRef VECTOR_SELF_* env) must pass — no false positive on the
        legitimate downward-API env."""
        r = _egress_lint(repo_root, ["--ci"])
        assert r.returncode == 0, f"default charts should pass:\n{r.stdout}\n{r.stderr}"

    @_needs_helm
    def test_malicious_sink_url_blocked(self, repo_root: Path) -> None:
        """A sink pointing at a non-allowlisted host is the T4-1 exfil
        primitive — must be an ERROR."""
        r = _egress_lint(repo_root, [
            "--chart", "helm/vector",
            "--set", "additionalSinks[0].name=exfil",
            "--set", "additionalSinks[0].type=http",
            "--set", "additionalSinks[0].uri=http://attacker.evil.com/x",
            "--set", "additionalSinks[0].inputs[0]=demux",
            "--ci",
        ])
        assert r.returncode == 1
        # Assert on the violation MESSAGE, not a hostname-substring-in-text
        # check (the latter is CodeQL's "incomplete URL substring
        # sanitization" antipattern — a false positive in a test, but
        # cleaner to avoid the shape entirely).
        assert "egress to non-allowlisted host" in r.stdout

    @_needs_helm
    def test_allowlisted_sink_passes(self, repo_root: Path) -> None:
        """An explicitly allowlisted SIEM host passes — the gate permits
        reviewed egress."""
        r = _egress_lint(repo_root, [
            "--chart", "helm/vector",
            "--set", "additionalSinks[0].name=splunk",
            "--set", "additionalSinks[0].type=http",
            "--set", "additionalSinks[0].uri=https://splunk.example.com:8088/x",
            "--set", "additionalSinks[0].inputs[0]=demux",
            "--allow-host", "splunk.example.com",
            "--ci",
        ])
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    @_needs_helm
    def test_reserved_env_literal_override_blocked(self, repo_root: Path) -> None:
        """Overriding a VECTOR_* reserved var with a literal value hijacks
        the pipeline — T4-2. Must be ERROR. (The chart's own fieldRef form
        of the same vars must NOT trip — covered by test_default_charts_pass.)"""
        r = _egress_lint(repo_root, [
            "--chart", "helm/vector",
            "--set", "extraEnv[0].name=VECTOR_SELF_NODE_NAME",
            "--set", "extraEnv[0].value=attacker-controlled",
            "--ci",
        ])
        assert r.returncode == 1
        assert "VECTOR_SELF_NODE_NAME" in r.stdout

    @_needs_helm
    def test_sensitive_env_literal_blocked(self, repo_root: Path) -> None:
        """A sensitive-named env via literal value (hardcoded secret /
        attacker-substitutable credential) is ERROR; the valueFrom path is
        the supported way (not asserted here — needs a Secret to exist)."""
        r = _egress_lint(repo_root, [
            "--chart", "helm/vector",
            "--set", "extraEnv[0].name=SPLUNK_TOKEN",
            "--set", "extraEnv[0].value=hardcoded-secret",
            "--ci",
        ])
        assert r.returncode == 1
        assert "SPLUNK_TOKEN" in r.stdout

    def test_host_extraction_helper(self, repo_root: Path) -> None:
        """Unit-test the host parser directly (no helm needed) — the
        allowlist decision hinges on it parsing host out of url/host:port."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "check_log_egress_policy",
            repo_root / "scripts/tools/lint/check_log_egress_policy.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve cls.__module__
        # via sys.modules (else AttributeError on NoneType.__dict__).
        sys.modules["check_log_egress_policy"] = mod
        spec.loader.exec_module(mod)
        assert mod._host_of("https://splunk.example.com:8088/x") == "splunk.example.com"
        assert mod._host_of("http://victorialogs.monitoring.svc:9428/insert") == "victorialogs.monitoring.svc"
        assert mod._host_of("syslog.security.svc:6514") == "syslog.security.svc"
        # Verify the userinfo segment is stripped from the parsed host.
        # Assemble the URI from parts so secret scanners don't flag a
        # literal credential-bearing URI token in source.
        cred_uri = "https://" + "u" + ":" + "p" + "@evil.com/x"
        assert mod._host_of(cred_uri) == "evil.com"
        assert mod._host_of("") is None
        # allowlist glob match
        assert mod._host_allowed("victorialogs.monitoring.svc", mod.DEFAULT_ALLOWED_HOST_GLOBS)
        assert not mod._host_allowed("attacker.evil.com", mod.DEFAULT_ALLOWED_HOST_GLOBS)


# ──────────────────────────────────────────────────────────────────────────────
# #566 batch D — in-process unit tests for the egress lint internals.
# These call lint_chart()/_iter_* directly with synthetic manifests (no helm,
# no subprocess) so coverage.py actually measures the rule logic — the
# subprocess-based tests above exercise behavior but don't count toward coverage.
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def egress_mod(repo_root: Path):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "check_log_egress_policy",
        repo_root / "scripts/tools/lint/check_log_egress_policy.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_log_egress_policy"] = mod
    spec.loader.exec_module(mod)
    return mod


def _vector_configmap(sinks: dict) -> dict:
    """Build a synthetic Vector ConfigMap manifest with an embedded
    vector.yaml carrying the given sinks dict."""
    return {
        "kind": "ConfigMap",
        "metadata": {"name": "vector-config"},
        "data": {"vector.yaml": yaml.safe_dump({"sinks": sinks})},
    }


def _workload(kind: str, env: list[dict]) -> dict:
    """Build a synthetic workload manifest (Deployment/DaemonSet/CronJob)
    with one container carrying the given env list."""
    container = {"name": "vector", "env": env}
    if kind == "CronJob":
        return {"kind": kind, "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [container]}}}}}}
    return {"kind": kind, "spec": {"template": {"spec": {"containers": [container]}}}}


class TestEgressLintInternals:
    """In-process coverage of check_log_egress_policy.lint_chart + helpers."""

    def test_allowlisted_sink_no_violation(self, egress_mod, repo_root: Path) -> None:
        cm = _vector_configmap({
            "victorialogs": {"type": "elasticsearch",
                             "endpoints": ["http://victorialogs.monitoring.svc:9428/insert/elasticsearch/"]},
        })
        vios = egress_mod.lint_chart(Path("helm/vector"), [cm], list(egress_mod.DEFAULT_ALLOWED_HOST_GLOBS))
        assert vios == []

    def test_nonallowlisted_sink_endpoint_violation(self, egress_mod) -> None:
        cm = _vector_configmap({
            "exfil": {"type": "http", "endpoints": ["http://attacker.example.net/x"]},
        })
        vios = egress_mod.lint_chart(Path("helm/vector"), [cm], list(egress_mod.DEFAULT_ALLOWED_HOST_GLOBS))
        assert len(vios) == 1
        assert vios[0].level == "ERROR"
        assert "non-allowlisted" in vios[0].message

    def test_sink_uri_scalar_form_checked(self, egress_mod) -> None:
        """http sinks use `uri` (scalar) not `endpoints` (list) — both paths
        must be inspected."""
        cm = _vector_configmap({
            "exfil": {"type": "http", "uri": "https://bad.example.org:9000/ingest"},
        })
        vios = egress_mod.lint_chart(Path("helm/vector"), [cm], list(egress_mod.DEFAULT_ALLOWED_HOST_GLOBS))
        assert any("bad.example.org" in v.message for v in vios)

    def test_allow_host_extends_allowlist(self, egress_mod) -> None:
        cm = _vector_configmap({"siem": {"type": "http", "uri": "https://splunk.example.com:8088/x"}})
        globs = list(egress_mod.DEFAULT_ALLOWED_HOST_GLOBS) + ["splunk.example.com"]
        assert egress_mod.lint_chart(Path("helm/vector"), [cm], globs) == []

    def test_reserved_env_fieldref_allowed(self, egress_mod) -> None:
        """The chart's own VECTOR_SELF_* via fieldRef must NOT be flagged."""
        m = _workload("DaemonSet", [
            {"name": "VECTOR_SELF_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        ])
        assert egress_mod.lint_chart(Path("helm/vector"), [m], []) == []

    def test_reserved_env_literal_violation(self, egress_mod) -> None:
        m = _workload("DaemonSet", [{"name": "VECTOR_SELF_NODE_NAME", "value": "pwned"}])
        vios = egress_mod.lint_chart(Path("helm/vector"), [m], [])
        assert len(vios) == 1 and "reserved" in vios[0].message.lower()

    def test_reserved_env_secretref_also_violation(self, egress_mod) -> None:
        """A reserved var sourced from a Secret (not fieldRef) is still an
        override — only fieldRef is the legitimate form."""
        m = _workload("DaemonSet", [
            {"name": "VECTOR_SECRET_THING", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        ])
        vios = egress_mod.lint_chart(Path("helm/vector"), [m], [])
        assert len(vios) == 1

    def test_sensitive_env_literal_violation(self, egress_mod) -> None:
        m = _workload("CronJob", [{"name": "SPLUNK_TOKEN", "value": "hardcoded"}])
        vios = egress_mod.lint_chart(Path("helm/chargeback-aggregator"), [m], [])
        assert len(vios) == 1 and "valueFrom" in vios[0].message

    def test_sensitive_env_valuefrom_allowed(self, egress_mod) -> None:
        m = _workload("Deployment", [
            {"name": "SPLUNK_TOKEN", "valueFrom": {"secretKeyRef": {"name": "splunk", "key": "token"}}},
        ])
        assert egress_mod.lint_chart(Path("helm/vector"), [m], []) == []

    def test_unparseable_embedded_vector_yaml_is_error(self, egress_mod) -> None:
        cm = {"kind": "ConfigMap", "metadata": {"name": "vector-config"},
              "data": {"vector.yaml": "sinks: [unclosed"}}
        vios = egress_mod.lint_chart(Path("helm/vector"), [cm], [])
        assert any("unparseable" in v.message for v in vios)

    def test_iter_container_envs_covers_all_kinds(self, egress_mod) -> None:
        for kind in ("Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"):
            m = _workload(kind, [{"name": "X", "value": "1"}])
            envs = list(egress_mod._iter_container_envs(m))
            assert envs and envs[0][1]["name"] == "X", f"{kind} env not iterated"
        # unknown kind yields nothing
        assert list(egress_mod._iter_container_envs({"kind": "Service", "spec": {}})) == []

    def test_main_skips_nonexistent_chart_and_exits_clean(self, egress_mod, capsys) -> None:
        """main() with a chart dir that has no Chart.yaml renders nothing,
        reports no violations, exits 0 — covers the arg-parse + skip +
        clean-exit path without needing helm."""
        egress_mod.main(["--chart", "/nonexistent-chart-dir"])
        out = capsys.readouterr().out
        assert "no violations" in out

    def test_main_json_output_path(self, egress_mod, capsys, monkeypatch) -> None:
        """--json path: stub render_chart so no helm is needed, feed a bad
        sink, assert JSON violations are emitted + SystemExit(1)."""
        bad_cm = _vector_configmap({"exfil": {"type": "http", "uri": "http://attacker.example.net/x"}})
        monkeypatch.setattr(egress_mod, "render_chart", lambda *a, **k: [bad_cm])
        # Point at a real chart dir so the Chart.yaml existence check passes.
        with pytest.raises(SystemExit) as exc:
            egress_mod.main(["--chart", "helm/vector", "--json"])
        assert exc.value.code == 1
        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        assert any("non-allowlisted" in v["message"] for v in payload)
