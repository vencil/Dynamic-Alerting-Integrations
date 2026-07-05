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
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_cross_ns_url_consistency as lint  # noqa: E402


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
