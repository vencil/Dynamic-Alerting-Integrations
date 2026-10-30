"""Contract tests for `check_md_yaml_drift.py` (the schema-conformance pass).

Synthetic fixtures under ``tmp_path`` — see the sibling
test_check_md_yaml_fences.py for why these do not assert against the live tree.

What these pin is the part that was broken for the gate's whole life: it sent
EVERY block to tenant-config.schema.json, so `_defaults.yaml`-shaped examples
failed with ``'tenants' is a required property`` — a verdict about the wrong
contract — and 38/38 blocks "failed" while nothing was actually validated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _run(root: Path, env: dict[str, str] | None = None, check: str = "schema"):
    return subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(SCRIPT), "--ci", "--repo-root", str(root),
         "--check", check],
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


@pytest.mark.parametrize("body, kind", [
    ("```yaml\ndefaults:\n  a: 1\n---\ndefaults:\n  b: 2\n```\n", "multiple_documents"),
    ("```yaml\ntenants:\n  t1: {a: '1'}\ntenants:\n  t2: {a: '2'}\n```\n", "duplicate_key"),
    ("```yaml\ntenants:\n  t1: !!binary YQ==\n```\n", "tag_not_allowed"),
])
def test_an_unreadable_confd_example_fails(tmp_path: Path, body: str, kind: str) -> None:
    """ADR-036 step 11 (#3206): an example is read as conf.d is read (da-guard
    parse-tree). One the exporter refuses whole is a violation — a reader who
    copies it gets a file that is not served. Before, PyYAML read it
    leniently (each document, the last of a repeated key) and judged that.

    #3416: the refusal is the inventory's error, reported by `--check
    fences`; the schema pass counts the unit as not judged here."""
    root = _mkrepo(tmp_path, "bad.md", body)
    r = _run(root, check="fences")
    assert r.returncode == EXIT_VIOLATION, r.stdout + r.stderr
    assert "[FENCE] docs/bad.md:2" in r.stdout and f"({kind}:" in r.stdout, r.stdout
    s = _run(root)
    assert "Units not judged here:       1" in s.stdout, s.stdout


def test_a_declared_unreadable_example_passes(tmp_path: Path) -> None:
    """Control: a deliberate counter-example declares the refusal it shows."""
    body = ("```yaml {.confd .expect-refused=multiple_documents}\n"
            "defaults:\n  a: 1\n---\ndefaults:\n  b: 2\n```\n")
    root = _mkrepo(tmp_path, "marked.md", body)
    for check in ("schema", "fences"):
        r = _run(root, check=check)
        assert r.returncode == EXIT_OK, check + r.stdout + r.stderr


def test_an_unreadable_block_that_is_no_confd_example_is_declared(tmp_path: Path) -> None:
    """`---`-separated k8s manifests (and a frontmatter example) are no
    conf.d; parse-tree refusing them as one conf.d file says nothing about
    them. #3416: so the author says what they are — undeclared, the fence
    check asks for that; declared, both pass."""
    k8s = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n---\n"
           "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: b}\n```\n")
    front = "---\ntitle: x\nlang: zh\n---\n```\n"
    root = _mkrepo(tmp_path, "k8s.md", "```yaml\n" + k8s + "```yaml\n" + front)
    r = _run(root, check="fences")
    assert r.returncode == EXIT_VIOLATION, r.stdout + r.stderr
    assert r.stdout.count("[FENCE]") == 2, r.stdout
    (root / "docs" / "k8s.md").write_text(
        "```yaml {.k8s}\n" + k8s
        + '```yaml {.illustrative reason="a frontmatter example"}\n' + front,
        encoding="utf-8")
    for check in ("schema", "fences"):
        r = _run(root, check=check)
        assert r.returncode == EXIT_OK, check + r.stdout + r.stderr


def test_values_are_read_as_conf_d_reads_them(tmp_path: Path) -> None:
    """ADR-036 step 11 (#3206): `send_resolved: yes` is the string "yes" to
    yaml.v3 (the exporter, and the route generator through parse-tree), so a
    documented receiver with it does not meet the schema's boolean. PyYAML
    read True, and this gate passed an example the platform reads otherwise."""
    body = ("```yaml\ntenants:\n  t1:\n    _routing:\n      receiver:\n"
            "        type: slack\n        api_url: https://hooks.slack.com/x\n"
            "        send_resolved: yes\n```\n")
    r = _run(_mkrepo(tmp_path, "yes.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout + r.stderr
    assert "'send_resolved': 'yes'" in r.stdout, r.stdout


def test_no_da_guard_fails_closed(tmp_path: Path) -> None:
    """Without da-guard nothing is read, and that is an error, never a pass."""
    body = "```yaml\ndefaults:\n  a: 1\n```\n"
    r = _run(_mkrepo(tmp_path, "x.md", body),
             env={"DA_GUARD_BINARY": str(tmp_path / "no-such-da-guard")})
    assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
    assert "da-guard parse-tree" in r.stderr, r.stderr


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
    validated by nothing. This is the same flat-`receiver_type` class the tenant
    side already caught.
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


def test_illustrative_exempts_and_is_reported(tmp_path: Path) -> None:
    """A block no gate should judge opts out — but never silently. The skip
    is printed with the reason the attribute had to supply, so a search for
    `.illustrative` plus the report is the full audit."""
    body = ('```yaml {.illustrative reason="shows a shape on purpose"}\n'
            'tenants:\n  example-tenant:\n    bogus_key: "1"\n```\n')
    r = _run(_mkrepo(tmp_path, "ig.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "Units exempted (.illustrative): 1" in r.stdout
    assert "docs/ig.md:2 — shows a shape on purpose" in r.stdout


def test_expect_invalid_holds_a_counter_example_to_being_invalid(tmp_path: Path) -> None:
    """`{.confd .expect-invalid}` (rustdoc's compile_fail): the schema must
    find the block wrong. Wrong: passes, its violations are not reported.
    Right after all: that is the violation (the block is no counter-example)."""
    bad = '```yaml {.confd .expect-invalid}\ntenants:\n  t1:\n    mysql_connections: 123\n```\n'
    r = _run(_mkrepo(tmp_path, "ce.md", bad))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "docs/ce.md:2 — 1 violation(s) on purpose" in r.stdout, r.stdout
    ok = '```yaml {.confd .expect-invalid}\ntenants:\n  t1:\n    mysql_connections: "123"\n```\n'
    (tmp_path / "docs" / "ce.md").write_text(ok, encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "[SCHEMA] docs/ce.md:2 (expect-invalid)" in r.stdout, r.stdout


def test_retired_marker_is_an_error_naming_the_attribute(tmp_path: Path) -> None:
    """⛔ The retired comment marker exempts nothing any more: the fence check
    names it and the attribute to write instead, and the block below it is
    judged as any other."""
    body = (
        "<!-- md-yaml-drift: ignore — old style -->\n"
        "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    )
    root = _mkrepo(tmp_path, "old.md", body)
    r = _run(root, check="fences")
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "[FENCE] docs/old.md:1" in r.stdout and ".illustrative" in r.stdout, r.stdout
    r = _run(root)
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "123" in r.stdout


def test_preceding_prose_is_no_declaration(tmp_path: Path) -> None:
    """Only the fence's own attribute list declares it: prose above is prose."""
    body = (
        "Some prose paragraph here (illustrative only).\n\n"
        "```yaml\ntenants:\n  example-tenant:\n    mysql_connections: 123\n```\n"
    )
    r = _run(_mkrepo(tmp_path, "prose.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout


def test_illustrative_applies_to_its_own_fence_only(tmp_path: Path) -> None:
    """Scope check: the attribute exempts ONE fence, not the rest of the file."""
    body = (
        '```yaml {.illustrative reason="first only"}\n'
        "tenants:\n  a:\n    mysql_connections: 111\n```\n"
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
    body = "```yaml\nconf.d/\n├── a.yaml\n  : broken\n```\n"
    root = _mkrepo(tmp_path, "tree.md", body)
    r = _run(root)
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    # ...and the fence check, which reports every inventory error, does.
    assert _run(root, check="fences").returncode == EXIT_VIOLATION


def _break_tenant_schema_id(root: Path) -> None:
    """#2232: the platform schema's cross-file `$ref` resolves by the tenant
    schema's `$id`; without it the `$ref` is unresolvable."""
    path = root / "docs" / "schemas" / "tenant-config.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema.pop("$id")
    path.write_text(json.dumps(schema), encoding="utf-8")


_ROUTING_DEFAULTS_BLOCK = ("```yaml\n_routing_defaults:\n  receiver:\n    type: webhook\n"
                           "    url: \"https://a.example.com/h\"\n```\n")
_UNRESOLVED_REF = "tenant-config.schema.json#/definitions/routingDefaults"


def test_unresolvable_cross_file_ref_is_caller_error(tmp_path: Path) -> None:
    """#2232: used to be a traceback at rc 1."""
    root = _mkrepo(tmp_path, "rd.md", _ROUTING_DEFAULTS_BLOCK)
    _break_tenant_schema_id(root)
    r = _run(root)
    assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert _UNRESOLVED_REF in r.stderr, r.stderr


def test_unresolvable_ref_fails_even_when_no_block_reaches_it(tmp_path: Path) -> None:
    """#2232: used to pass at rc 0 — no block walked into the `$ref`."""
    root = _mkrepo(tmp_path, "t.md", "```yaml\ntenants:\n  t1:\n    _severity_dedup: \"enable\"\n```\n")
    _break_tenant_schema_id(root)
    r = _run(root)
    assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
    assert _UNRESOLVED_REF in r.stderr, r.stderr


def test_routing_defaults_block_passes_with_repo_schemas(tmp_path: Path) -> None:
    r = _run(_mkrepo(tmp_path, "rd.md", _ROUTING_DEFAULTS_BLOCK))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
