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
        capture_output=True, text=True, encoding="utf-8", timeout=60,
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
        # `defaults:` keeps this segment SELECTED (a lone `state_flters` is not
        # a key the selector recognises, so the segment would drop out and the
        # unit count would fall back to 1); the typo is what makes it fail.
        "# L1 finance/_defaults.yaml\n"
        "defaults:\n  b: 2\n"
        "state_flters:\n  m:\n    severity: info\n"
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "composite.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "Config units checked:        2" in r.stdout
    # Also pins the fence→file line translation (`line_num + offset + 1`), which
    # is the contract the Go gate reports against: the fence opens on line 1 and
    # the second documented file starts at offset 4, so the violation must be
    # reported at line 6. Asserting only the unit count would let a regression in
    # that arithmetic — the exact divergence this PR found between the two
    # gates — stay green.
    assert "docs/composite.md:6" in r.stdout


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


def test_composite_platform_file_also_validates_its_tenants_block(tmp_path: Path) -> None:
    """⛔ Regression pin for a fail-open found by adversarial review.

    A platform file may legitimately carry `tenants:`, and routing picks the
    PLATFORM schema on the platform key. But platform-defaults.schema.json
    leaves `tenants` explicitly loose (its own title: "guarded by
    tenant-config.schema.json when in a tenant file"), so for a while every
    tenant body inside a composite example was validated by NOTHING — this
    exact input passed clean, reporting "✓ Every documented config example
    matches its schema".

    The defect below is the same class the gate exists to catch (a flat
    `receiver_type` plus an invented key), so if it ever passes again the
    routing has silently stopped holding composites to both contracts.
    """
    body = (
        "```yaml\n"
        "# conf.d/_defaults.yaml\n"
        "defaults:\n  mysql_connections: 80\n"
        "tenants:\n"
        "  example-tenant:\n"
        "    _routing:\n"
        "      receiver_type: slack\n"
        "      bogus_key: 1\n"
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "composite_tenants.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "receiver_type" in r.stdout
    assert "(platform+tenants)" in r.stdout


def test_platform_file_with_valid_tenants_block_passes(tmp_path: Path) -> None:
    """The reverse assertion: holding composites to both contracts must not
    reject a correct one. Note the asymmetry the loader itself imposes —
    `defaults:` values are unquoted numbers (map[string]float64), tenant values
    are quoted strings (ScheduledValue)."""
    body = (
        "```yaml\n"
        "# conf.d/_defaults.yaml\n"
        "defaults:\n  mysql_connections: 80\n"
        "tenants:\n"
        "  example-tenant:\n"
        '    mysql_connections: "70"\n'
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "composite_ok.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_top_level_routing_receiver_is_validated(tmp_path: Path) -> None:
    """⛔ Second fail-open found by the same adversarial sweep.

    platform-defaults.schema.json accepts `^_routing` through an EMPTY
    patternProperties schema, so a top-level `_routing_enforced:` body was
    validated by nothing — 18 of the 52 documented units, the largest group.
    This is the same flat-`receiver_type` class the tenant side already caught.
    """
    body = (
        "```yaml\n"
        "# conf.d/_defaults.yaml\n"
        "_routing_enforced:\n"
        "  enabled: true\n"
        "  receiver:\n"
        "    receiver_type: slack\n"
        "    bogus: 1\n"
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "enforced.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "_routing_enforced.receiver" in r.stdout


def test_enforced_routing_keeps_its_own_extra_fields(tmp_path: Path) -> None:
    """The reverse assertion, and the reason only the RECEIVER is cross-checked:
    `_routing_enforced` legitimately carries `enabled:` and `match:`, which
    tenant-config's `definitions.routing` forbids. Validating the whole routing
    block against that definition would reject every correct example."""
    body = (
        "```yaml\n"
        "# conf.d/_defaults.yaml\n"
        "_routing_enforced:\n"
        "  enabled: true\n"
        "  receiver:\n"
        "    type: slack\n"
        "    api_url: https://hooks.slack.com/services/x\n"
        "  match:\n"
        "    - 'severity=\"critical\"'\n"
        "```\n"
    )
    r = _run(_mkrepo(tmp_path, "enforced_ok.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_bare_tenants_block_is_selected(tmp_path: Path) -> None:
    """The widening this file's docstring describes: a plain tenant FILE (just
    `tenants:`, no marker key) is a documented config example and is judged."""
    # An unquoted number: tenant values are ScheduledValue (string | object).
    # Carries no marker key, so this block is selected ONLY by the `tenants`
    # rule — which is exactly what this test pins.
    body = "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    r = _run(_mkrepo(tmp_path, "t.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "(tenant-file)" in r.stdout


def test_ignore_marker_exempts_and_is_reported(tmp_path: Path) -> None:
    """A deliberate ❌ counter-example opts out — but never silently. The skip
    is printed with the reason the marker had to supply, so `git grep
    'md-yaml-drift: ignore'` plus the report is the full audit."""
    body = (
        "<!-- md-yaml-drift: ignore — deliberate counter-example -->\n"
        '```yaml\ntenants:\n  example-tenant:\n    bogus_key: "1"\n```\n'
    )
    r = _run(_mkrepo(tmp_path, "ig.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "Blocks exempted by marker:   1" in r.stdout
    assert "deliberate counter-example" in r.stdout
    assert "docs/ig.md:2" in r.stdout


def test_ignore_marker_requires_a_reason(tmp_path: Path) -> None:
    """⛔ A bare `ignore` must NOT exempt anything. An exemption without a stated
    reason is the silent-exclusion pattern this marker exists to avoid."""
    body = (
        "<!-- md-yaml-drift: ignore -->\n"
        "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    )
    r = _run(_mkrepo(tmp_path, "bare.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "123" in r.stdout


def test_ignore_marker_survives_multiple_blank_lines(tmp_path: Path) -> None:
    """⛔ Regression: the look-back used a two-line window, so a marker with two
    blank lines before the fence was silently lost and the block it exempted got
    reported as a violation — with nothing pointing at the marker. The scan now
    walks back to the first non-blank line."""
    body = (
        "<!-- md-yaml-drift: ignore — two blank lines below -->\n"
        "\n\n"
        "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    )
    r = _run(_mkrepo(tmp_path, "blanks.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "Blocks exempted by marker:   1" in r.stdout


def test_preceding_prose_is_not_mistaken_for_a_marker(tmp_path: Path) -> None:
    """The reverse of the above: walking back further must not start treating
    ordinary prose as an exemption."""
    body = (
        "Some prose paragraph here.\n\n"
        "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    )
    r = _run(_mkrepo(tmp_path, "prose.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout


def test_ignore_marker_only_applies_to_the_next_block(tmp_path: Path) -> None:
    """Scope check: the marker exempts ONE block, not the rest of the file."""
    body = (
        "<!-- md-yaml-drift: ignore — first only -->\n"
        "```yaml\ntenants:\n  a:\n    mysql_connections: 111\n```\n"
        "\ntext between\n\n"
        "```yaml\ntenants:\n  b:\n    mysql_connections: 222\n```\n"
    )
    r = _run(_mkrepo(tmp_path, "scope.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "222" in r.stdout
    assert "111" not in r.stdout


def test_unparseable_block_is_left_to_the_fence_check(tmp_path: Path) -> None:
    """Fence hygiene is a separate hook; reporting it here too would let a
    mislabelled directory tree fail the schema gate for an unrelated reason."""
    body = "```yaml\nconf.d/\n├── a.yaml\n```\n"
    r = _run(_mkrepo(tmp_path, "tree.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
