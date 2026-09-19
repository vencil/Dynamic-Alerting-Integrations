"""Contract tests for `check_md_yaml_drift.py --check crd`.

Synthetic like its two siblings: each case builds its own tiny ``docs/`` tree
AND its own ``docs/schemas/crd/`` under ``tmp_path``, so the tests pin the
CHECKER rather than the current state of the repo's documentation.

The two properties worth defending here are design decisions that were reached
by measurement, not taste, and both fail SILENTLY if they regress:

* a missing schema must be a VIOLATION, never a skip
  (``test_unknown_crd_fails_closed``);
* validation runs against the whole schema, so a required field nested several
  levels down is caught (``test_nested_required_violation_is_caught``) — a
  top-level-only pass reported 0 violations on inputs that had 4.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "tools" / "lint" / "check_md_yaml_drift.py"
)

EXIT_OK = 0
EXIT_VIOLATION = 1

# A miniature stand-in for an ArgoCD Application CRD: `spec.project` required,
# plus one nested required field to exercise the depth property.
TOY_SCHEMA = {
    "type": "object",
    "required": ["spec"],
    "properties": {
        "spec": {
            "type": "object",
            "required": ["project"],
            "properties": {
                "project": {"type": "string"},
                "receivers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "apiURL": {
                                "type": "object",
                                "required": ["name", "key"],
                                "properties": {"name": {"type": "string"},
                                               "key": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    },
}


def _run(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(SCRIPT),
         "--check", "crd", "--ci", "--repo-root", str(repo_root)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def _tree(tmp_path: Path, body: str, *, with_schema: bool = True) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "page.md").write_text(body, encoding="utf-8")
    if with_schema:
        crd = docs / "schemas" / "crd" / "example.io"
        crd.mkdir(parents=True, exist_ok=True)
        (crd / "Widget_v1.json").write_text(json.dumps(TOY_SCHEMA), encoding="utf-8")
    return tmp_path


def _block(spec: str) -> str:
    return f"# t\n\n```yaml\napiVersion: example.io/v1\nkind: Widget\nspec:\n{spec}```\n"


def test_valid_object_passes(tmp_path: Path) -> None:
    r = _run(_tree(tmp_path, _block("  project: default\n")))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_missing_required_field_fails(tmp_path: Path) -> None:
    """The #1353 shape: a block a reader would copy, that the API server rejects."""
    r = _run(_tree(tmp_path, _block("  replicas: 1\n")))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "'project' is a required property" in r.stdout


def test_nested_required_violation_is_caught(tmp_path: Path) -> None:
    """Depth property. A top-level-`required`-only check passes this input;
    the four AlertmanagerConfig defects in docs/ lived exactly this deep."""
    body = _block("  project: default\n"
                  "  receivers:\n"
                  "    - apiURL:\n"
                  "        secret:\n"
                  "          name: s\n"
                  "          key: k\n")
    r = _run(_tree(tmp_path, body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "spec/receivers/0/apiURL" in r.stdout


def test_unknown_crd_fails_closed(tmp_path: Path) -> None:
    """⛔ The core design property. Both the stripped prometheus-operator CRD
    variant and datreeio's CRDs-catalog lack AlertmanagerConfig v1beta1, so a
    skip-on-missing rule would have reported a clean run while validating none
    of the six blocks that declare it."""
    r = _run(_tree(tmp_path, _block("  project: default\n"), with_schema=False))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "no vendored schema" in r.stdout
    assert "SOURCES.yaml" in r.stdout


def test_non_cluster_group_is_out_of_scope(tmp_path: Path) -> None:
    """An apiserver config FILE is never `kubectl apply`ed, so the failure this
    gate speaks for cannot happen to it — and it must not fail closed either."""
    body = ("# t\n\n```yaml\napiVersion: audit.k8s.io/v1\nkind: Policy\n"
            "rules:\n  - level: Metadata\n```\n")
    r = _run(_tree(tmp_path, body, with_schema=False))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "not a cluster object" in r.stdout


def test_builtin_kind_is_out_of_scope_and_counted(tmp_path: Path) -> None:
    body = ("# t\n\n```yaml\napiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n```\n")
    r = _run(_tree(tmp_path, body, with_schema=False))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "built-in, out of scope" in r.stdout


def test_ignore_marker_exempts_and_is_reported(tmp_path: Path) -> None:
    """Exemptions are never silent — the reason the marker had to supply is
    printed, so a gate that dropped an input cannot read as full coverage."""
    body = ("# t\n\n<!-- md-yaml-drift: ignore — counter-example on purpose -->\n"
            "```yaml\napiVersion: example.io/v1\nkind: Widget\nspec:\n  replicas: 1\n```\n")
    r = _run(_tree(tmp_path, body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "counter-example on purpose" in r.stdout


def test_unparseable_block_is_left_to_the_fences_pass(tmp_path: Path) -> None:
    """Duplicating fence hygiene here would let a mislabelled directory tree
    fail the CRD gate for a reason that has nothing to do with CRDs."""
    body = "# t\n\n```yaml\nconf.d/\n├── a.yaml\n  : broken\n```\n"
    r = _run(_tree(tmp_path, body, with_schema=False))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
