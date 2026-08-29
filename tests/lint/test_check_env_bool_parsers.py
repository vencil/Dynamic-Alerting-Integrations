"""Self-test for check_env_bool_parsers.py (ADR-034 mechanical enforcement).

The anti-noop witness is deliberate: this lint's baseline is ZERO matches in
the repo (#1624 removed the only one), so a bug that made it match nothing
would be invisible. `test_flags_the_real_pre_1624_envbool` feeds it the actual
pre-#1624 source of `envBool` — the defect the rule exists for — and demands a
hit.
"""

from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_env_bool_parsers as guard  # noqa: E402

# Verbatim from components/tenant-api/cmd/server/main.go before #1624.
PRE_1624_ENVBOOL = '''\
func envBool(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "true", "1", "yes", "on":
		return true
	}
	return false
}
'''


def _hits(src: str) -> list[str]:
    return [ln for ln in src.splitlines() if guard.line_violation(ln)]


def test_flags_the_real_pre_1624_envbool():
    """Anti-noop witness: the actual defect must be caught."""
    hits = _hits(PRE_1624_ENVBOOL)
    assert hits, (
        "the pre-#1624 envBool source was NOT flagged — this lint would have "
        "been a no-op against the very defect ADR-034 was written for"
    )
    assert any('case "true", "1", "yes", "on":' in h for h in hits)


def test_flags_case_arm_with_boolish_literals():
    assert guard.line_violation('\tcase "true", "1":')
    assert guard.line_violation('    case "yes", "on", "enabled":')


def test_flags_direct_env_comparison():
    assert guard.line_violation('\tif strings.ToLower(os.Getenv("X")) == "true" {')
    assert guard.line_violation('\tenabled := os.Getenv("X") != "false"')


def test_allows_legitimate_enum_switch():
    """--write-mode's own switch is the shape ADR-034 wants, not the one it bans."""
    assert guard.line_violation('\tcase "pr", "pr-github":') is None
    assert guard.line_violation('\tcase "pr-gitlab":') is None


def test_allows_the_fixed_shape():
    for line in (
        "\tv, err := strconv.ParseBool(trimmed)",
        '\traw := strings.TrimSpace(os.Getenv(key))',
        "\tif trimmed == \"\" {",
    ):
        assert guard.line_violation(line) is None, line


def test_single_boolish_literal_is_not_enough():
    """One literal cannot distinguish a truthy parser from an ordinary enum.

    Deliberate precision/recall trade-off: requiring two or more keeps a plain
    `case "true":` over some non-config string from turning red.
    """
    assert guard.line_violation('\tcase "true":') is None


def test_ignores_comments():
    assert guard.line_violation('\t// case "true", "1", "yes", "on": old shape') is None


def test_scope_excludes_tests_and_non_go():
    assert guard.is_in_scope("components/tenant-api/cmd/server/main.go")
    assert not guard.is_in_scope("components/tenant-api/cmd/server/envbool_test.go")
    assert not guard.is_in_scope("docs/adr/034-legal-value-as-fallback.md")


def test_test_file_sentinel_would_be_out_of_scope():
    """The sentinel in envbool_test.go matches the pattern but is out of scope.

    Recorded so that widening the scope to `_test.go` cannot happen without
    this expectation being restated.
    """
    sentinel = '\tif os.Getenv("ENVBOOL_CRASHER") == "1" {'
    assert guard.line_violation(sentinel) is not None
    assert not guard.is_in_scope("components/tenant-api/cmd/server/envbool_test.go")
