"""Tests for check_helm_values_secrets.py — Container SAST Layer 3 (#448 / TRK-313).

Pinned contracts:
1. **Key detection**: password/token/apiKey/secret/clientSecret keys flagged;
   ref/flag keys (createSecret / secretName / existingSecret / secretKeyRef /
   secretKey / tokenTTL) NOT flagged.
2. **Value whitelist**: empty / ${VAR} / {{ .Values }} / placeholder / bool /
   numeric / duration / k8s-ref => not a violation.
3. **scan_line**: hardcoded literal => (key, value); whitelisted/comment/bare
   key => None.
4. **Baseline**: a full-file scan of the repo's helm + raw-k8s scope is 0
   (ship-at-0; also the #445-non-conflict guarantee — this lint doesn't
   double-fire on the secret templates that trufflehog covers).
5. **Scope (TRK-314)**: raw k8s/**/*.yaml manifests are scanned too — the
   secret-shape check is manifest-agnostic; raw Secrets are otherwise unscanned
   for low-entropy hardcoded literals (L4 kube-linter has no such check).
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_helm_values_secrets as hvs  # noqa: E402


class TestKeyIsSecret:
    @pytest.mark.parametrize("key", [
        "password", "rootPassword", "exporterPassword", "token",
        "apiKey", "api_key", "secret", "clientSecret", "client_secret",
        "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET",
        "accessKey", "privateKey",
    ])
    def test_secret_keys(self, key):
        assert hvs.key_is_secret(key) is True

    @pytest.mark.parametrize("key", [
        "createSecret", "secretName", "existingSecret", "existingSecretName",
        "secretRef", "secretKeyRef", "secretKey", "tokenTTL", "tokenName",
        "name", "image", "port", "replicaCount", "enabled",
        # config *about* a secret (contains the word but isn't a holder) —
        # endswith() must not flag these (self-review regression guard)
        "passwordPolicy", "passwordMinLength", "secretRotationDays",
        "tokenTimeout",
    ])
    def test_non_secret_keys(self, key):
        assert hvs.key_is_secret(key) is False


class TestValueWhitelist:
    @pytest.mark.parametrize("val", [
        '""', "''", "", "${SPLUNK_TOKEN}", "${OAUTH_CLIENT_SECRET}",
        "{{ .Values.mariadb.rootPassword | quote }}", "{{ .Values.x }}",
        "REPLACE_WITH_CLIENT_SECRET", "<changeme>", "CHANGE_ME",
        "your-secret-here", "TODO", "placeholder",
        "true", "false", "12345", "4h", "30s", "1500ms", "4h30m",
        "*db_pass", "&db_pass",  # YAML alias / anchor reference (self/Gemini review)
    ])
    def test_whitelisted(self, val):
        assert hvs.value_is_whitelisted(val) is True

    @pytest.mark.parametrize("val", [
        "hunter2", "sk-abc123xyz", "mytopsecret", "ghp_realtokenhere",
        '"AKIAIOSFODNN7EXAMPLE2"',
    ])
    def test_not_whitelisted(self, val):
        # NB: "EXAMPLE" substring would whitelist; the above avoid it on purpose.
        assert hvs.value_is_whitelisted(val) is False


class TestScanLine:
    @pytest.mark.parametrize("line,key", [
        ("  password: hunter2", "password"),
        ('  apiKey: "sk-abc123xyz"', "apiKey"),
        ("  clientSecret: mytopsecret", "clientSecret"),
        ("  token: ghp_realtokenhere", "token"),
        # self-review: a real value containing "replace" (past tense) must
        # still flag — only placeholder forms (REPLACE_WITH/replaceme) are waived
        ("  clientSecret: replaced_secret_value", "clientSecret"),
        # TRK-314: the grafana raw-Secret regression — `admin-password: admin`
        # is a hardcoded weak literal (low entropy; trufflehog misses it).
        ("  admin-password: admin", "admin-password"),
    ])
    def test_violations(self, line, key):
        hit = hvs.scan_line(line)
        assert hit is not None and hit[0] == key

    @pytest.mark.parametrize("line", [
        '  password: ""',
        "  token: ${SPLUNK_TOKEN}",
        "  secret: {{ .Values.x }}",
        "  clientSecret: REPLACE_WITH_SECRET",
        "  createSecret: true",
        "  tokenTTL: 4h",
        "  secretKey: federation-signing-key.pem",
        "  secretName: tenant-federation-signing-key",
        "  passwordPolicy: strict",   # config about a secret, not a holder
        "  admin-user: admin",        # username (key ≠ secret word) — not flagged
        "  password: *db_pass",       # YAML alias reference, not a literal
        "  # password: leaked-in-comment",
        "  password:",          # bare key (block/continuation)
        "  image: nginx:1.28",  # not a secret key
    ])
    def test_non_violations(self, line):
        assert hvs.scan_line(line) is None


class TestScopeAndBaseline:
    def test_scope_includes_values_templates_and_configmaps(self):
        rels = {p.relative_to(hvs.REPO_ROOT).as_posix() for p in hvs.find_scope_files()}
        assert "helm/mariadb-instance/values.yaml" in rels
        assert any("templates/secret" in r for r in rels)
        # expanded scope (Gemini review): ALL templates incl ConfigMaps + the
        # top-level value overlays, not just secret*.yaml
        assert any("/templates/" in r and "secret" not in r for r in rels), \
            "non-secret templates (ConfigMaps etc.) must be in scope"
        assert any(r.startswith("helm/values") for r in rels), \
            "top-level helm/values-*.yaml overlays must be in scope"
        # TRK-314: raw k8s/ manifests are in scope too — secret-shape is
        # manifest-agnostic; this closes the raw-Secret gap L4 (kube-linter, no
        # hardcoded-value check) + #445 (trufflehog, low-entropy miss) leave open.
        assert "k8s/03-monitoring/secret-grafana.yaml" in rels, \
            "raw k8s/ Secret manifests must be in scope (TRK-314)"
        assert any(r.startswith("k8s/") and "/configmap" in r for r in rels), \
            "raw k8s/ ConfigMaps must be in scope (most common misplacement)"
        # worktrees excluded
        assert not any(".claude" in r for r in rels)

    def test_repo_ships_at_zero(self):
        """Full-file scan of the real helm scope must be clean (ship-at-0 +
        #445 non-conflict: no double-fire on trufflehog-covered secret files)."""
        findings = []
        for fp in hvs.find_scope_files():
            findings += hvs.scan_file_full(fp)
        assert findings == [], f"unexpected hardcoded secret-shape: {findings}"
