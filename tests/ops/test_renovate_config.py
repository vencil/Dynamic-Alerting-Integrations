"""#902 L3 — guard the Renovate config against silent no-op + incomplete coverage.

Renovate cannot run in this PR's own CI (it is owner-activated via
`.github/workflows/renovate.yaml` + a RENOVATE_TOKEN PAT). So this test is the
offline safety net: it parses `renovate.json`, applies each customManager's regex to
the ACTUAL repo files, and asserts that every one of the 14 #902 L2-pinned
third-party images is matched with a sane (depName, tag, digest) — and that the
scan-matrix refs and the deploy refs resolve to the SAME depName set (so a Renovate
bump updates both in one PR and the drift-guard stays green).

A custom-manager regex that silently matches NOTHING is the classic failure mode; it
would go unnoticed until an owner runs Renovate weeks later. Here it fails loud, in
the normal Python Tests lane.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RENOVATE_JSON = REPO / "renovate.json"

# Directories never worth walking when resolving managerFilePatterns.
_SKIP_DIRS = {".git", "node_modules", ".claude", "site", "__pycache__", ".mypy_cache", ".pytest_cache"}

# The 14 third-party images #902 L2 pinned. depName == the registry/repo path carried
# in the chart values, the k8s manifests, and the scan matrix. Pin a NEW third-party
# image -> add it here AND ensure a customManager matches it (this set is the SSOT the
# coverage test enforces against the config).
EXPECTED_DEPNAMES = {
    "envoyproxy/envoy",
    "quay.io/oauth2-proxy/oauth2-proxy",
    "quay.io/prometheuscommunity/prom-label-proxy",
    "timberio/vector",
    "victoriametrics/victoria-logs",
    "python",
    "mariadb",
    "prom/mysqld-exporter",
    "grafana/grafana",
    "prom/prometheus",
    "prom/alertmanager",
    "registry.k8s.io/kube-state-metrics/kube-state-metrics",
    "ghcr.io/jimmidyson/configmap-reload",
    "alpine/git",
}


def _load_config() -> dict:
    return json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))


def _py_regex(renovate_pattern: str) -> re.Pattern:
    # Renovate uses RE2 named groups `(?<name>...)`; Python's re wants `(?P<name>...)`.
    # The constructs used in this config (named groups, [\s\S], non-greedy, [ \t]) are
    # otherwise RE2/Python-compatible, so the translation is faithful for validation.
    translated = re.sub(r"\(\?<([a-zA-Z][a-zA-Z0-9]*)>", r"(?P<\1>", renovate_pattern)
    return re.compile(translated)


def _manager_file_patterns(mgr: dict) -> list[str]:
    return mgr.get("managerFilePatterns", mgr.get("fileMatch", []))


def _files_for_manager(mgr: dict) -> list[Path]:
    """Resolve a manager's regex-form (`/.../`) file patterns to actual repo files."""
    regexes = []
    for p in _manager_file_patterns(mgr):
        assert p.startswith("/") and p.endswith("/"), f"expected regex-form pattern, got {p!r}"
        regexes.append(re.compile(p[1:-1]))
    out: list[Path] = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            rel = (Path(root) / fn).relative_to(REPO).as_posix()
            if any(rx.search(rel) for rx in regexes):
                out.append(Path(root) / fn)
    return out


def _extract(mgr: dict) -> list[dict]:
    """Apply a customManager's matchStrings to its files; return captured deps."""
    regexes = [_py_regex(s) for s in mgr["matchStrings"]]
    found: list[dict] = []
    for f in _files_for_manager(mgr):
        text = f.read_text(encoding="utf-8")
        for rx in regexes:
            for m in rx.finditer(text):
                found.append({"file": f.relative_to(REPO).as_posix(), **m.groupdict()})
    return found


def _matrix_manager(cfg: dict) -> dict:
    return next(m for m in cfg["customManagers"]
                if "nightly-image-scan" in "".join(_manager_file_patterns(m)))


def test_renovate_json_is_strict_json_and_well_formed():
    cfg = _load_config()
    # Scope MUST stay images-only — a wider enabledManagers would let Renovate open
    # broken tool-SHA / lang-dep PRs (#902 L3 Category B/C are deliberately out).
    assert cfg["enabledManagers"] == ["custom.regex"]
    assert cfg.get("pinDigests") is True
    assert len(cfg["customManagers"]) == 3
    assert cfg.get("packageRules"), "expected grouping + major-approval rules"


def test_every_custom_manager_matches_something():
    """The classic custom-manager failure is a regex that matches NOTHING. Assert each
    manager extracts >=1 dep, each with a non-empty tag and a well-formed sha256."""
    cfg = _load_config()
    for mgr in cfg["customManagers"]:
        deps = _extract(mgr)
        assert deps, f"customManager matched nothing: {mgr.get('description', '')[:70]}"
        for d in deps:
            assert d.get("depName"), f"empty depName in {d['file']}"
            assert d.get("currentValue"), f"empty currentValue ({d.get('depName')} in {d['file']})"
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", d.get("currentDigest", "")), \
                f"bad digest for {d.get('depName')} in {d['file']}: {d.get('currentDigest')!r}"


def test_coverage_is_complete_and_exact():
    """Every #902-pinned third-party image is covered — and nothing extra (a stray
    match would mean Renovate touches an unintended ref)."""
    cfg = _load_config()
    seen = {d["depName"] for mgr in cfg["customManagers"] for d in _extract(mgr)}
    assert seen == EXPECTED_DEPNAMES, (
        f"\n  missing (pinned but Renovate won't bump): {sorted(EXPECTED_DEPNAMES - seen)}"
        f"\n  unexpected (Renovate would touch):       {sorted(seen - EXPECTED_DEPNAMES)}"
    )


def test_scan_matrix_and_deploy_refs_share_depnames():
    """Grouping invariant: the scan-matrix refs and the deploy refs (helm + k8s) must
    resolve to the SAME depName set, so a Renovate bump updates both in one PR and the
    drift-guard (matrix == deploy refs) stays green."""
    cfg = _load_config()
    matrix_mgr = _matrix_manager(cfg)
    matrix_names = {d["depName"] for d in _extract(matrix_mgr)}
    deploy_names = {d["depName"] for m in cfg["customManagers"] if m is not matrix_mgr
                    for d in _extract(m)}
    assert matrix_names == EXPECTED_DEPNAMES, f"matrix missing: {sorted(EXPECTED_DEPNAMES - matrix_names)}"
    assert deploy_names == EXPECTED_DEPNAMES, f"deploy missing: {sorted(EXPECTED_DEPNAMES - deploy_names)}"


def test_renovate_config_validator_if_available():
    """If the official validator is installed, the config must pass its schema check.
    Skipped where renovate isn't available (e.g. the Python Tests CI lane has no node)."""
    validator = shutil.which("renovate-config-validator")
    if not validator:
        pytest.skip("renovate-config-validator not installed (node/renovate absent)")
    res = subprocess.run([validator, str(RENOVATE_JSON)],
                         capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, f"renovate-config-validator failed:\n{res.stdout}\n{res.stderr}"
