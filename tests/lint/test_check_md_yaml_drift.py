"""Contract tests for `check_md_yaml_drift.py` (the schema-conformance pass).

Synthetic fixtures under ``tmp_path`` — see the sibling
test_check_md_yaml_fences.py for why these do not assert against the live tree.

What these pin is the part that was broken for the gate's whole life: it sent
EVERY block to tenant-config.schema.json, so `_defaults.yaml`-shaped examples
failed with ``'tenants' is a required property`` — a verdict about the wrong
contract — and 38/38 blocks "failed" while nothing was actually validated.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "tools" / "lint" / "check_md_yaml_drift.py"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_CALLER_ERROR = 2


def _mkrepo(tmp_path: Path, name: str, body: str) -> Path:
    """A minimal repo root: docs/<name> plus the two real schemas."""
    (tmp_path / "docs" / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / name).write_text(body, encoding="utf-8")
    for schema in ("tenant-config.schema.json", "platform-defaults.schema.json"):
        src = REPO / "docs" / "schemas" / schema
        (tmp_path / "docs" / "schemas" / schema).write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _run(root: Path, env: dict[str, str] | None = None):
    return subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(SCRIPT), "--ci", "--repo-root", str(root)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )


def test_platform_block_is_judged_by_platform_schema(tmp_path: Path) -> None:
    """A `_defaults.yaml` shape must NOT be told it is missing `tenants:`."""
    body = "```yaml\ndefaults:\n  mysql_connections: 80\nstate_filters:\n  m:\n    severity: info\n```\n"
    r = _run(_mkrepo(tmp_path, "p.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "'tenants' is a required property" not in r.stdout


def test_typo_in_platform_key_is_caught(tmp_path: Path) -> None:
    """Class the Go loader is structurally blind to: it decodes non-strictly,
    so an unknown top-level key is invisible there and only the schema sees it."""
    body = "```yaml\ndefaults:\n  a: 1\nstate_flters:\n  m:\n    severity: info\n```\n"
    r = _run(_mkrepo(tmp_path, "typo.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "state_flters" in r.stdout


def test_tenant_fragment_is_wrapped_and_validated(tmp_path: Path) -> None:
    """A bare tenant body has no `tenants:` wrapper; wrapping it in a synthetic
    tenant is what lets the real per-tenant subschema judge it."""
    body = "```yaml\n_routing:\n  receiver_type: slack\n  webhook_url: https://x\n```\n"
    r = _run(_mkrepo(tmp_path, "frag.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "receiver_type" in r.stdout
    assert "(fragment)" in r.stdout


def test_valid_tenant_fragment_passes(tmp_path: Path) -> None:
    body = ("```yaml\n_routing:\n  receiver:\n    type: slack\n"
            "    api_url: https://hooks.slack.com/services/x\n```\n")
    r = _run(_mkrepo(tmp_path, "ok.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_composite_fence_is_split_per_documented_file(tmp_path: Path) -> None:
    """Two files in one fence: judged whole this is not even valid YAML (a
    repeated top-level key), which says nothing about the documented config."""
    body = (
        "```yaml\n"
        "# L0 _defaults.yaml\n"
        "defaults:\n  a: 1\n"
        "\n"
        "# L1 finance/_defaults.yaml\n"
        "defaults:\n  b: 2\n"
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "composite.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "Config units checked:        2" in r.stdout


def test_multi_document_block_validates_each_document(tmp_path: Path) -> None:
    body = "```yaml\ndefaults:\n  a: 1\n---\ndefaults:\n  b: 2\n```\n"
    r = _run(_mkrepo(tmp_path, "multi.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "Config units checked:        2" in r.stdout


def test_missing_jsonschema_fails_closed(tmp_path: Path) -> None:
    """⛔ The regression that made this gate meaningless: the script used to do
    `except ImportError: jsonschema = None  # Graceful degradation`, and the
    hook shipped without the dependency — so it validated nothing, quietly, and
    still reported a run. Absence must be an error, never a pass."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "jsonschema.py").write_text(
        'raise ImportError("simulated missing jsonschema")\n', encoding="utf-8")
    body = "```yaml\ndefaults:\n  a: 1\n```\n"
    r = _run(_mkrepo(tmp_path, "x.md", body), env={"PYTHONPATH": str(stub)})
    assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
    assert "jsonschema not installed" in r.stderr


def test_unparseable_block_is_left_to_the_fence_check(tmp_path: Path) -> None:
    """Fence hygiene is a separate hook; reporting it here too would let a
    mislabelled directory tree fail the schema gate for an unrelated reason."""
    body = "```yaml\nconf.d/\n├── a.yaml\n```\n"
    r = _run(_mkrepo(tmp_path, "tree.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
