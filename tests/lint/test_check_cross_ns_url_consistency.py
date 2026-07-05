"""Tests for check_cross_ns_url_consistency.py — #1004 cross-ns URL lint.

Pinned contracts
----------------
1. **R1 FQDN**: `<svc>.<ns>.svc[.cluster.local]` with a non-canonical ns is
   flagged, context-free; the canonical FQDN never is.
2. **R2 bare name**: `http(s)://<svc>[:port]` / `<svc>:<port>` is flagged
   outside the service's canonical namespace (unknown context = conservative
   violation) and allowed inside it.
3. **Word boundaries**: object names (`tenant-api-netpol`), exact bare names
   without scheme/port (`tenant-api` label values), public hosts
   (`tenant-api.example.com`) and image refs (`repo/tenant-api:123`) are NOT
   hosts — never flagged.
4. **Parse, not grep**: YAML comments containing `tenant-api:8080` never fire.
5. **Compose mode**: R1 only — bare names are legitimate on compose's single
   network; wrong-ns FQDN aliases still fire.
6. **Exemptions**: a registered (path, substring) hit downgrades to INFO.
7. **Live dogfood**: the real tree has 0 violations and exactly 1 exempt INFO
   (the compose legacy alias) — guards a regression of this very PR.
8. **Config DATA is external + fail-closed** (#1004 Option D): the governed
   map / chart contexts / exemptions load from an external YAML; a missing,
   malformed, or schema-invalid config is a caller-error (exit 2), NEVER a
   silent pass. The config file itself is outside every scan glob (self-scan
   guard).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_cross_ns_url_consistency as lint  # noqa: E402

_LINT_PY = Path(lint.__file__).resolve()


@pytest.fixture
def restore_lint_config():
    """Snapshot + restore the module-level config globals so a test that calls
    _apply_config (which rebinds them) can't leak state into sibling tests."""
    snap = (lint.GOVERNED_SERVICES, lint.CHART_CONTEXT_NS, lint.EXEMPTIONS,
            lint._SVC_ALT, lint._FQDN_RE, lint._BARE_SCHEME_RE,
            lint._BARE_HOSTPORT_RE)
    yield
    (lint.GOVERNED_SERVICES, lint.CHART_CONTEXT_NS, lint.EXEMPTIONS,
     lint._SVC_ALT, lint._FQDN_RE, lint._BARE_SCHEME_RE,
     lint._BARE_HOSTPORT_RE) = snap


_VALID_CONFIG = textwrap.dedent("""\
    governed_services:
      tenant-api: tenant-api
      recipe-preview: monitoring
    chart_context_ns:
      tenant-api: tenant-api
      da-portal: monitoring
      mariadb-instance: null
    exemptions:
      - path: try-local/docker-compose.yaml
        substring: tenant-api.monitoring.svc.cluster.local
        rationale: legacy compose network alias
        exit_condition: "remove when PORTAL_TAG is bumped past #1004"
""")


# ── R1: wrong-namespace FQDN (context-free) ─────────────────────────────────
def test_r1_wrong_ns_fqdn_flagged():
    out = lint.scan_scalar(
        "http://tenant-api.monitoring.svc.cluster.local:8080", "monitoring")
    assert len(out) == 1 and "WRONG namespace `monitoring`" in out[0][1]
    assert "tenant-api.tenant-api.svc.cluster.local" in out[0][1]


def test_r1_wrong_ns_short_svc_form_flagged():
    out = lint.scan_scalar("recipe-preview.default.svc:8082", None)
    assert len(out) == 1 and "WRONG namespace `default`" in out[0][1]


def test_r1_canonical_fqdn_clean():
    assert lint.scan_scalar(
        "http://tenant-api.tenant-api.svc.cluster.local:8080", None) == []
    assert lint.scan_scalar(
        "http://recipe-preview.monitoring.svc.cluster.local:8082", None) == []


def test_r1_fires_regardless_of_context():
    # A wrong-ns FQDN is wrong everywhere — even when the caller sits in the
    # namespace the FQDN names.
    out = lint.scan_scalar(
        "tenant-api.monitoring.svc.cluster.local", "monitoring")
    assert len(out) == 1


# ── R2: bare name cross-ns ──────────────────────────────────────────────────
def test_r2_bare_scheme_url_cross_ns_flagged():
    out = lint.scan_scalar("http://tenant-api:8080", "monitoring")
    assert len(out) == 1 and "bare host `tenant-api`" in out[0][1]


def test_r2_bare_scheme_url_unknown_context_flagged():
    # Conservative: no namespace context → violation.
    out = lint.scan_scalar("https://tenant-api:8080/api/v1/", None)
    assert len(out) == 1 and "context ns=unknown" in out[0][1]


def test_r2_bare_name_in_canonical_ns_allowed():
    assert lint.scan_scalar("http://tenant-api:8080", "tenant-api") == []
    assert lint.scan_scalar("recipe-preview:8082", "monitoring") == []


def test_r2_hostport_form_flagged():
    out = lint.scan_scalar("tenant-api:8080", "monitoring")
    assert len(out) == 1 and "bare host" in out[0][1]


def test_r2_scheme_url_not_double_counted():
    # R2a (scheme) owns `http://tenant-api:8080`; R2b (host:port) must not
    # also fire on the same span.
    out = lint.scan_scalar("http://tenant-api:8080", None)
    assert len(out) == 1


# ── word-boundary negatives (module docstring: names are not hosts) ─────────
def test_object_names_and_labels_not_flagged():
    assert lint.scan_scalar("tenant-api", None) == []           # label value
    assert lint.scan_scalar("tenant-api-netpol", None) == []    # object name
    assert lint.scan_scalar("my-tenant-api", None) == []


def test_public_host_not_flagged():
    # `.example.com` is not a `.svc` form and not a bare host.
    assert lint.scan_scalar(
        "http://tenant-api.example.com/oauth2/callback", None) == []


def test_image_ref_not_flagged():
    assert lint.scan_scalar("da-try-local/tenant-api:8080", None) == []


def test_other_host_prefix_not_flagged():
    # `foo-tenant-api` / `foo.tenant-api.…` are different hosts.
    assert lint.scan_scalar("foo-tenant-api.monitoring.svc", None) == []
    assert lint.scan_scalar("http://foo-tenant-api:8080", None) == []


# ── compose mode: R1 only ───────────────────────────────────────────────────
def test_compose_mode_ignores_bare_names():
    assert lint.scan_scalar("http://tenant-api:8080", None,
                            apply_bare=False) == []


def test_compose_mode_still_catches_wrong_fqdn():
    out = lint.scan_scalar("tenant-api.monitoring.svc.cluster.local", None,
                           apply_bare=False)
    assert len(out) == 1


# ── synthetic repo end-to-end ───────────────────────────────────────────────
_CLEAN_PORTAL_URL = "http://tenant-api.tenant-api.svc.cluster.local:8080"


def _write_repo(tmp_path, *, portal_url=_CLEAN_PORTAL_URL,
                compose_alias="tenant-api.tenant-api.svc.cluster.local",
                k8s_ns="monitoring", k8s_url=_CLEAN_PORTAL_URL,
                nginx_upstream=_CLEAN_PORTAL_URL):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "helm" / "da-portal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "helm" / "da-portal" / "values.yaml").write_text(
        f'global:\n  tenantApiUrl: "{portal_url}"\n', encoding="utf-8")
    (tmp_path / "k8s" / "03-monitoring").mkdir(parents=True, exist_ok=True)
    (tmp_path / "k8s" / "03-monitoring" / "cronjob.yaml").write_text(
        "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n"
        f"  name: govern\n  namespace: {k8s_ns}\n"
        "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
        "          containers:\n            - name: c\n              args:\n"
        f"                - {k8s_url}\n"
        "# comment only, must never fire: tenant-api:8080\n",
        encoding="utf-8")
    (tmp_path / "components" / "da-portal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "components" / "da-portal" / "nginx.conf").write_text(
        "server {\n    # proxy to tenant-api (name in prose is fine)\n"
        f"    location /api/v1/ {{ proxy_pass {nginx_upstream}/api/v1/; }}\n"
        "}\n", encoding="utf-8")
    (tmp_path / "try-local").mkdir(exist_ok=True)
    (tmp_path / "try-local" / "docker-compose.yaml").write_text(
        "services:\n  tenant-api:\n    image: da-try-local/tenant-api:source\n"
        "    environment:\n"
        "      PREVIEW_TENANT_API_URL: http://tenant-api:8080\n"  # bare = fine in compose
        "    networks:\n      default:\n        aliases:\n"
        f"          - {compose_alias}\n", encoding="utf-8")


def test_check_repo_clean(tmp_path):
    _write_repo(tmp_path)
    violations, infos, n_files = lint.check_repo(tmp_path)
    assert violations == [] and infos == []
    assert n_files == 4


def test_check_repo_catches_helm_values_drift(tmp_path):
    # The exact past drift: a chart hardcoding the wrong-ns FQDN.
    _write_repo(tmp_path,
                portal_url="http://tenant-api.monitoring.svc.cluster.local:8080")
    violations, _, _ = lint.check_repo(tmp_path)
    assert len(violations) == 1
    assert "helm/da-portal/values.yaml:2" in violations[0]
    assert "global.tenantApiUrl" in violations[0]


def test_check_repo_catches_bare_url_cross_ns(tmp_path):
    # The other past drift: bare http://tenant-api:8080 from monitoring ns.
    _write_repo(tmp_path, k8s_url="http://tenant-api:8080")
    violations, _, _ = lint.check_repo(tmp_path)
    assert len(violations) == 1 and "bare host `tenant-api`" in violations[0]


def test_check_repo_bare_url_in_canonical_ns_allowed(tmp_path):
    _write_repo(tmp_path, k8s_ns="tenant-api",
                k8s_url="http://tenant-api:8080")
    violations, _, _ = lint.check_repo(tmp_path)
    assert violations == []


def test_check_repo_nginx_drift(tmp_path):
    _write_repo(tmp_path, nginx_upstream="http://tenant-api:8080")
    violations, _, _ = lint.check_repo(tmp_path)
    assert len(violations) == 1
    assert "components/da-portal/nginx.conf:3" in violations[0]


def test_check_repo_yaml_comment_never_fires(tmp_path):
    # `tenant-api:8080` lives in a k8s YAML comment in every synthetic repo;
    # the clean tree proves comments are invisible to the scan.
    _write_repo(tmp_path)
    violations, infos, _ = lint.check_repo(tmp_path)
    assert violations == [] and infos == []


def test_exemption_downgrades_to_info(tmp_path):
    # The registered compose legacy alias hits R1 but reports as INFO.
    _write_repo(tmp_path,
                compose_alias="tenant-api.monitoring.svc.cluster.local")
    violations, infos, _ = lint.check_repo(tmp_path)
    assert violations == []
    assert len(infos) == 1 and "exempt" in infos[0]
    assert "exit condition" in infos[0]


def test_unexempted_compose_fqdn_drift_flagged(tmp_path):
    # A wrong-ns FQDN in compose that is NOT the registered alias stays a
    # violation (the exemption is substring-scoped, not file-wide).
    _write_repo(tmp_path,
                compose_alias="recipe-preview.default.svc.cluster.local")
    violations, infos, _ = lint.check_repo(tmp_path)
    assert len(violations) == 1 and infos == []


# ── main() exit codes against a synthetic repo ──────────────────────────────
def test_main_exit_codes(tmp_path, monkeypatch):
    _write_repo(tmp_path, k8s_url="http://tenant-api:8080")
    monkeypatch.setattr(lint, "_THIS_DIR",
                        str(tmp_path / "scripts" / "tools" / "lint"))
    (tmp_path / "scripts" / "tools" / "lint").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv",
                        ["check_cross_ns_url_consistency.py", "--ci"])
    assert lint.main() == 1

    _write_repo(tmp_path)  # overwrite back to clean
    assert lint.main() == 0

    # without --ci a violating tree reports but exits 0 (report mode)
    _write_repo(tmp_path, k8s_url="http://tenant-api:8080")
    monkeypatch.setattr(sys, "argv", ["check_cross_ns_url_consistency.py"])
    assert lint.main() == 0


def test_main_missing_fixed_target_is_caller_error(tmp_path, monkeypatch):
    _write_repo(tmp_path)
    (tmp_path / "components" / "da-portal" / "nginx.conf").unlink()
    monkeypatch.setattr(lint, "_THIS_DIR",
                        str(tmp_path / "scripts" / "tools" / "lint"))
    (tmp_path / "scripts" / "tools" / "lint").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv",
                        ["check_cross_ns_url_consistency.py", "--ci"])
    assert lint.main() == 2


# ── live dogfood ────────────────────────────────────────────────────────────
def test_live_repo_is_consistent():
    repo = Path(__file__).resolve().parents[2]
    violations, infos, _ = lint.check_repo(repo)
    assert violations == []
    # exactly the one registered exemption: the compose legacy alias
    assert len(infos) == 1
    assert "tenant-api.monitoring.svc" in infos[0]


# ── #1004 Option D: config DATA loaded from external file, fail-closed ───────
def _write_config(tmp_path, text) -> Path:
    p = tmp_path / "cross_ns_url_lint.config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_module_constants_loaded_from_real_config():
    # The module-level constants are the real repo config, loaded at import —
    # so every legacy test above (which references lint.GOVERNED_SERVICES etc.)
    # keeps working without any per-test config wiring.
    assert lint.GOVERNED_SERVICES == {
        "tenant-api": "tenant-api", "recipe-preview": "monitoring"}
    assert lint.CHART_CONTEXT_NS["mariadb-instance"] is None
    assert lint.CHART_CONTEXT_NS["da-portal"] == "monitoring"
    assert len(lint.CHART_CONTEXT_NS) == 11
    key = (lint._COMPOSE, "tenant-api.monitoring.svc.cluster.local")
    assert key in lint.EXEMPTIONS
    rationale, exit_cond = lint.EXEMPTIONS[key]
    assert "legacy compose network alias" in rationale
    assert exit_cond  # non-empty exit condition preserved


def test_load_config_valid_returns_expected_structures(tmp_path):
    cfg = _write_config(tmp_path, _VALID_CONFIG)
    gov, charts, exemptions = lint.load_config(str(cfg))
    assert gov == {"tenant-api": "tenant-api", "recipe-preview": "monitoring"}
    assert charts == {"tenant-api": "tenant-api", "da-portal": "monitoring",
                      "mariadb-instance": None}
    key = ("try-local/docker-compose.yaml",
           "tenant-api.monitoring.svc.cluster.local")
    assert exemptions[key] == (
        "legacy compose network alias",
        "remove when PORTAL_TAG is bumped past #1004")


def test_load_config_missing_file_fails_closed(tmp_path):
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(tmp_path / "does-not-exist.yaml"))


def test_load_config_malformed_yaml_fails_closed(tmp_path):
    cfg = _write_config(tmp_path, "governed_services: [unterminated\n")
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(cfg))


def test_load_config_missing_top_level_key_fails_closed(tmp_path):
    cfg = _write_config(tmp_path,
                        "governed_services: {tenant-api: tenant-api}\n"
                        "chart_context_ns: {}\n")  # no `exemptions`
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(cfg))


def test_load_config_empty_governed_services_fails_closed(tmp_path):
    cfg = _write_config(tmp_path,
                        "governed_services: {}\n"
                        "chart_context_ns: {}\nexemptions: []\n")
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(cfg))


def test_load_config_exemption_missing_exit_condition_rejected(tmp_path):
    # HARD REQ #3: every exemption MUST carry a non-empty exit_condition.
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        governed_services: {tenant-api: tenant-api}
        chart_context_ns: {tenant-api: tenant-api}
        exemptions:
          - path: try-local/docker-compose.yaml
            substring: tenant-api.monitoring.svc.cluster.local
            rationale: some reason
        """))  # no exit_condition
    with pytest.raises(lint.ConfigError) as exc:
        lint.load_config(str(cfg))
    assert "exit_condition" in str(exc.value)


def test_load_config_exemption_blank_exit_condition_rejected(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        governed_services: {tenant-api: tenant-api}
        chart_context_ns: {tenant-api: tenant-api}
        exemptions:
          - path: p
            substring: s
            rationale: r
            exit_condition: "   "
        """))
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(cfg))


def test_load_config_bad_chart_ns_type_fails_closed(tmp_path):
    cfg = _write_config(tmp_path,
                        "governed_services: {tenant-api: tenant-api}\n"
                        "chart_context_ns: {da-portal: 123}\n"
                        "exemptions: []\n")
    with pytest.raises(lint.ConfigError):
        lint.load_config(str(cfg))


def test_apply_config_rebinds_regexes(tmp_path, restore_lint_config):
    # A config with a DIFFERENT governed set must reshape the matchers so the
    # pure functions pick it up (proves the regexes are built post-load).
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        governed_services: {only-svc: only-ns}
        chart_context_ns: {only-svc: only-ns}
        exemptions: []
        """))
    lint._apply_config(str(cfg))
    # `only-svc` is now governed; `tenant-api` no longer is.
    assert lint.scan_scalar("http://only-svc:8080", "monitoring") != []
    assert lint.scan_scalar("http://tenant-api:8080", "monitoring") == []


def test_main_missing_config_is_caller_error(tmp_path, monkeypatch):
    # HARD REQ #1: point main() at a missing config → exit 2, never 0.
    _write_repo(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "check_cross_ns_url_consistency.py", "--ci",
        "--config", str(tmp_path / "absent.yaml")])
    assert lint.main() == 2


def test_main_malformed_config_is_caller_error(tmp_path, monkeypatch):
    _write_repo(tmp_path)
    bad = _write_config(tmp_path, "governed_services: [unterminated\n")
    monkeypatch.setattr(sys, "argv", [
        "check_cross_ns_url_consistency.py", "--ci", "--config", str(bad)])
    assert lint.main() == 2


def test_main_missing_config_subprocess_exits_2(tmp_path):
    # End-to-end: the real script binary, bad --config → exit 2 (fail-closed).
    result = subprocess.run(
        [sys.executable, str(_LINT_PY), "--ci",
         "--config", str(tmp_path / "absent.yaml")],
        capture_output=True, timeout=15)
    assert result.returncode == 2
    assert b"config data file" in result.stderr


# ── #1004 Option D: unmapped chart still → None → conservative violation ─────
def test_unmapped_chart_still_conservative_violation(tmp_path):
    # HARD REQ #2: a chart absent from chart_context_ns resolves to None →
    # a bare governed name in its values fires R2 (same as the old .get()).
    (tmp_path / ".git").mkdir(exist_ok=True)
    unmapped = tmp_path / "helm" / "brand-new-chart"
    unmapped.mkdir(parents=True)
    (unmapped / "values.yaml").write_text(
        'api:\n  url: "http://tenant-api:8080"\n', encoding="utf-8")
    assert "brand-new-chart" not in lint.CHART_CONTEXT_NS  # genuinely unmapped
    findings = lint.scan_helm_values(
        unmapped / "values.yaml", "helm/brand-new-chart/values.yaml",
        chart="brand-new-chart")
    assert len(findings) == 1
    assert "context ns=unknown" in findings[0][5]


# ── #1004 Option D: config file must NOT be self-scanned ─────────────────────
def test_config_file_is_not_a_scan_target():
    # HARD REQ #4: the config lives under scripts/tools/lint/ — outside every
    # scan glob and both fixed targets — so despite carrying the governed
    # strings + the exemption FQDN it is never among the lint's scan targets.
    repo = Path(__file__).resolve().parents[2]
    cfg = Path(lint.DEFAULT_CONFIG_PATH).resolve()
    cfg_rel = cfg.relative_to(repo).as_posix()

    scanned: set[str] = set()
    for pattern in ("helm/*/values*.yaml", "helm/*/values*.yml",
                    "k8s/**/*.yaml", "k8s/**/*.yml"):
        for p in repo.glob(pattern):
            if p.is_file():
                scanned.add(p.resolve().relative_to(repo).as_posix())
    scanned.update(lint._FIXED_TARGETS)

    assert cfg_rel not in scanned, (
        f"config file {cfg_rel} is inside a scan target set — it would "
        f"self-flag the governed strings it declares")
    # And its own governed strings really are present in the file (so the
    # guard above is meaningful, not vacuous).
    assert "tenant-api.monitoring.svc.cluster.local" in cfg.read_text(
        encoding="utf-8")


def test_config_file_not_flagged_in_live_dogfood():
    # The live dogfood already proves 0 violations despite the config carrying
    # tenant-api / recipe-preview / the exemption FQDN; assert the config path
    # never appears in any finding location.
    repo = Path(__file__).resolve().parents[2]
    cfg_rel = Path(lint.DEFAULT_CONFIG_PATH).resolve().relative_to(
        repo).as_posix()
    violations, infos, _ = lint.check_repo(repo)
    assert all(cfg_rel not in v for v in violations)
    assert all(cfg_rel not in i for i in infos)
