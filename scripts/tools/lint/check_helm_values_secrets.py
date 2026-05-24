#!/usr/bin/env python3
"""check_helm_values_secrets.py — Container/k8s IaC SAST, Layer 3.

Epic #448 / TRK-313. A Vibe wrapper (NO open-source engine — the YAML-shape
check below has no kube-linter/trivy equivalent). Catches *hardcoded literal*
secrets in Helm values + Secret templates: a key whose name looks like a
secret (`password` / `token` / `apiKey` / `secret` / `clientSecret` / …) set
to a non-empty literal string.

Complements #445 trufflehog (L1/L2): trufflehog flags HIGH-ENTROPY values;
this lint flags the YAML *shape* regardless of entropy (a low-entropy literal
like `password: hunter2` is missed by entropy detectors but caught here). The
two do not double-fire on the same line — this lint ships at 0 (every current
match is whitelisted), so it only fires on a NEWLY hardcoded literal.

Lint class: (b) per docs/internal/lint-policy.md (negative pattern + false-
positive escape allowlist). Default scan scope: **diff-only** — only lines
ADDED in the current PR's diff are checked. --full-scan for periodic audit.

Scope: helm/*/values*.yaml + helm/values*.yaml + helm/*/templates/*.yaml
(EVERY template, not just secret*.yaml — the most common leak is a hardcoded
secret misplaced in a ConfigMap, which people guard far less than a Secret)
+ k8s/**/*.yaml (raw manifests — the secret-shape check is manifest-agnostic;
the Layer-4 kube-linter pass [TRK-314] has no hardcoded-Secret-value check, and
trufflehog [#445] misses low-entropy literals like `admin`, so raw k8s/ Secrets
would otherwise be unscanned for hardcoded literals entirely).

A candidate line `KEY: VALUE` is a VIOLATION when:
  - KEY (case-insensitive) ENDS WITH a secret word AND is not a ref/flag key
    (createSecret / secretName / existingSecret / secretRef / secretKeyRef);
    endswith (not contains) so `passwordPolicy` / `tokenTTL` aren't flagged.
  - VALUE is a non-empty literal that is NOT whitelisted.
Whitelisted VALUEs (legitimate, not a hardcoded secret):
  - empty (`""` / `''` / nothing)            — must-be-set marker
  - `${VAR}`                                  — env interpolation
  - `{{ .Values.* }}` / any `{{ ... }}`       — Helm template reference
  - placeholders: `<...>`, REPLACE_WITH/REPLACEME/CHANGEME/CHANGE_ME/TODO/
    PLACEHOLDER/YOUR_* (deploy-time fill-in markers)
  - boolean (true/false) / numeric / Go-duration (4h) — config, not secrets
  - `valueFrom` / `secretKeyRef`              — k8s indirection, not a literal
  - `*anchor` / `&anchor`                     — YAML alias / anchor reference

Known limitations (accepted residual risk — line-based KEY:VALUE scan, no
YAML AST, to stay fast + comment-preserving):
  - Block scalars (`key: |` / `key: >`) and list items (`- "literal"`) are NOT
    scanned — a hardcoded secret inside those is left to #445 trufflehog
    (high-entropy capture). A bare `key:` with a block/anchor below is treated
    as a non-assignment and skipped.

Usage:
    python3 scripts/tools/lint/check_helm_values_secrets.py [--ci]
    python3 scripts/tools/lint/check_helm_values_secrets.py --full-scan [--ci]

Exit codes:
    0  no hardcoded secrets (or bypass matched)
    1  findings (with --ci)
    2  diff base ref missing (fix CI fetch-depth / base ref)

Bypass (lint-policy.md §4): add to PR description:
    bypass-lint: helm-values-secrets
    reason: <>=30 words explaining why this is legitimate>
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BYPASS_NAME = "helm-values-secrets"
SKIP_DIR_PARTS = {".claude", ".git", "node_modules", ".venv", "venv"}

# Secret-word fragments matched (case-insensitive) inside a key name.
SECRET_WORDS = (
    "password", "passwd", "token", "apikey", "api_key",
    "secret", "clientsecret", "client_secret",
    "accesskey", "access_key", "privatekey", "private_key",
)
# Keys that contain a secret word but are references / config flags, NOT
# literal-secret holders. Normalised (lowercased, '-' -> '_') substring match.
KEY_ALLOWLIST = (
    "createsecret", "secretname", "existingsecret", "secretref",
    "secretkeyref", "usesecret", "enablesecret", "tokenname",
    # references to the *key within* a Secret (value is a key-name/filename,
    # not a literal secret); real high-entropy values here are caught by #445
    # trufflehog. `tokenttl` etc. are durations, handled by _DURATION too.
    "secretkey", "tokenttl", "tokenlifetime",
)

# Value-level whitelist regexes (a matching VALUE is NOT a hardcoded secret).
_ENV_INTERP = re.compile(r"\$\{[^}]+\}")
_HELM_TPL = re.compile(r"\{\{.*\}\}")
_PLACEHOLDER = re.compile(
    r"(?i)("
    r"<[^>]*>"                            # <changeme> angle-bracket marker
    r"|\breplace[_ -]?with"               # REPLACE_WITH_X / "replace with" (may continue, e.g. _32_BYTE)
    r"|\breplace[_ -]?me\b|\breplaceme\b" # replace-me / replaceme (\b so "replacement" isn't matched)
    r"|\bchange[_ -]?me\b|\bchangeme\b"   # change_me / changeme
    r"|\bplaceholder\b"
    r"|\byour[_-]\w"                      # your_key / your-secret
    r"|\b(todo|fixme)\b"
    r"|\bexample\b"
    r"|x{4,}"                            # xxxx... filler
    r")"
    # NB: bare "replace" (no with/me) intentionally NOT whitelisted — a real
    # value like `replaced_secret_value` must still be flagged (self-review).
)
_BOOL = re.compile(r"(?i)^(true|false|yes|no|on|off)$")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
# Go/k8s durations (4h / 30s / 1500ms / 4h30m) — a *TTL/*timeout config, not a secret.
_DURATION = re.compile(r"(?i)^\d+(\.\d+)?(ns|us|ms|s|m|h|d|w|y)([0-9.]+(ns|us|ms|s|m|h))*$")
_K8S_REF = re.compile(r"(?i)^(valueFrom|secretKeyRef|configMapKeyRef|fieldRef)\b")
# YAML alias (`*anchor`) or bare anchor (`&anchor`) — a reference to a value
# defined elsewhere, not a literal here. `&anchor "literal"` (anchor WITH an
# inline value) does NOT match this (trailing value) and is still scanned.
_YAML_REF = re.compile(r"^[*&][A-Za-z0-9_.\-]+$")

_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*:\s*(.*?)\s*$")


def _norm_key(key: str) -> str:
    return key.lower().replace("-", "_")


def key_is_secret(key: str) -> bool:
    nk = _norm_key(key)
    if any(allow in nk for allow in KEY_ALLOWLIST):
        return False
    # ENDS WITH a secret word, not merely CONTAINS one: a literal-secret holder
    # is `password` / `rootPassword` / `OAUTH_CLIENT_SECRET`, whereas
    # `passwordPolicy` / `passwordMinLength` / `tokenTTL` / `secretRotation` are
    # CONFIG *about* a secret — endswith avoids those false-positives.
    # (trufflehog #445 still backstops any high-entropy value in odd keys.)
    return any(nk.endswith(word) for word in SECRET_WORDS)


def _strip_value(raw: str) -> str:
    """Strip surrounding quotes + a trailing inline comment from a YAML value."""
    v = raw.strip()
    if not v:
        return ""
    # Quoted: take the quoted span, ignore trailing comment.
    if v[0] in ("'", '"'):
        q = v[0]
        end = v.find(q, 1)
        if end != -1:
            return v[1:end]
        return v[1:]
    # Unquoted: drop a trailing " # comment" (space-hash; '#' mid-token kept).
    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return v


def value_is_whitelisted(value: str) -> bool:
    v = _strip_value(value)
    if v == "":
        return True
    if _ENV_INTERP.search(v) or _HELM_TPL.search(v):
        return True
    if _PLACEHOLDER.search(v):
        return True
    if _BOOL.match(v) or _NUMERIC.match(v) or _DURATION.match(v):
        return True
    if _K8S_REF.match(v):
        return True
    if _YAML_REF.match(v):  # YAML alias `*anchor` / bare anchor `&anchor` — a reference, not a literal
        return True
    return False


def scan_line(line: str) -> tuple[str, str] | None:
    """Return (key, value) if the line is a hardcoded-secret violation, else None."""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return None  # comment line
    m = _LINE.match(line)
    if not m:
        return None
    key, value = m.group(1), m.group(2)
    if not key_is_secret(key):
        return None
    # A bare `KEY:` (value continues on following lines, e.g. a block/map) is
    # not a literal-secret assignment.
    if value.strip() == "" or value.strip() in ("|", ">", "|-", ">-"):
        return None
    if value_is_whitelisted(value):
        return None
    return (key, _strip_value(value))


# ---------------------------------------------------------------------------
# Scope + scanning
# ---------------------------------------------------------------------------
def find_scope_files() -> list[Path]:
    out: list[Path] = []
    # Scope = every chart values file (incl. tier variants), top-level value
    # overlays (helm/values-*.yaml), every rendered-source template — NOT just
    # secret*.yaml — AND every raw k8s/ manifest. A key named like a secret must
    # not appear as a literal in ANY manifest; the most common leak is a
    # hardcoded value misplaced in a ConfigMap (people guard `Secret` but not
    # `ConfigMap`). The k8s/**/*.yaml arm closes the raw-manifest gap: L4
    # kube-linter has no hardcoded-Secret-value check and trufflehog (#445)
    # misses low-entropy literals, so raw Secrets would otherwise be unscanned.
    # The positive whitelist (${VAR} / {{ .Values }} / placeholder / ref /
    # YAML-alias) keeps this broad scope false-positive-free.
    for pattern in ("helm/*/values*.yaml", "helm/values*.yaml",
                    "helm/*/templates/*.yaml", "k8s/**/*.yaml"):
        for p in REPO_ROOT.glob(pattern):
            if not p.is_file():
                continue
            if any(part in SKIP_DIR_PARTS for part in p.relative_to(REPO_ROOT).parts):
                continue
            out.append(p)
    return sorted(set(out))


def scan_file_full(path: Path) -> list[tuple[int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        hit = scan_line(line)
        if hit:
            out.append((i, hit[0], hit[1]))
    return out


def scan_file_diff(path: Path, base: str) -> list[tuple[int, str, str]]:
    try:
        added = get_diff_added_lines(path, base)
    except subprocess.CalledProcessError:
        return scan_file_full(path)
    out: list[tuple[int, str, str]] = []
    for line_no, line in added:
        hit = scan_line(line)
        if hit:
            out.append((line_no, hit[0], hit[1]))
    return out


def _read_pr_body(pr_body_file: str | None) -> str | None:
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}",
                  file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true",
                        help="Exit non-zero on any finding")
    parser.add_argument("--full-scan", action="store_true",
                        help="Scan full file content (default: diff-only)")
    parser.add_argument("--diff-base", default=None,
                        help="Override diff base (default: $LINT_DIFF_BASE / origin/main)")
    parser.add_argument("--pr-body-file", default=None,
                        help="Path to PR body file for bypass-tag check")
    args = parser.parse_args()

    files = find_scope_files()
    if args.full_scan:
        scan_mode = "full-file"
        scanner = scan_file_full
    else:
        try:
            base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        scan_mode = f"diff vs {base}"
        scanner = lambda fp: scan_file_diff(fp, base)  # noqa: E731

    findings: list[tuple[str, int, str, str]] = []
    for fp in files:
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for line_no, key, val in scanner(fp):
            findings.append((rel, line_no, key, val))

    for rel, line_no, key, val in findings:
        shown = (val[:24] + "…") if len(val) > 24 else val
        print(f"  {rel}:{line_no}: hardcoded secret-shape `{key}: {shown}`")

    total = len(findings)
    if total == 0:
        print(
            f"OK no hardcoded secret-shape in {len(files)} file(s) "
            f"(mode={scan_mode}).\n"
            f"   Helm values/secret 無硬編字面 secret（key 名像 secret 但值為空/"
            f"`${{VAR}}`/`{{{{ .Values }}}}`/placeholder/ref 皆放行）。"
        )
        return 0

    pr_body = _read_pr_body(args.pr_body_file)
    if parse_bypass_tag(pr_body, BYPASS_NAME):
        print(f"\n⚠️  BYPASSED via PR body — {total} finding(s) author-acknowledged.")
        return 0

    print(
        f"\nFAIL {total} hardcoded secret-shape finding(s) (mode={scan_mode}).\n"
        f"   硬編字面 secret 不可進 repo。改用：`${{ENV_VAR}}` 插值 / "
        f"`{{{{ .Values.x }}}}` template ref / `valueFrom.secretKeyRef` / "
        f"留空 `\"\"`（install 時提供）/ `<changeme>` placeholder。\n"
        f"   合法例外（如固定的測試假值）請於 PR description 加：\n"
        f"     bypass-lint: {BYPASS_NAME}\n"
        f"     reason: <>=30 words>\n"
        f"   詳見 docs/internal/lint-policy.md §4 與 epic #448 / TRK-313。"
    )
    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
