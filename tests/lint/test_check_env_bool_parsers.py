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


# --- main() / bypass / scope plumbing -------------------------------------
# The pure-function tests above cover the parse contract. These cover the
# wiring around it — most importantly the PR-body bypass tag, which
# lint-policy.md §4 requires of every (b) class lint and which nothing else
# exercises: if it broke, the only symptom would be a lint that cannot be
# overridden, discovered by whoever needed the override.

VIOLATION_LINE = '\tcase "true", "1", "yes", "on":'


def _stub_diff(monkeypatch, lines):
    """Feed find_violations a fixed set of added lines (no git needed)."""
    monkeypatch.setattr(guard, "get_diff_added_lines", lambda path, base: lines)
    monkeypatch.setattr(guard, "resolve_diff_base", lambda *a, **k: "origin/main")


def test_find_violations_reports_scope_hits(monkeypatch):
    _stub_diff(monkeypatch, [(10, VIOLATION_LINE)])
    out = guard.find_violations(["components/x/main.go"], "origin/main")
    assert len(out) == 1
    rel, line_no, content, reason = out[0]
    assert (rel, line_no) == ("components/x/main.go", 10)
    assert "hand-rolled truthy parser" in reason


def test_find_violations_skips_out_of_scope_files(monkeypatch):
    _stub_diff(monkeypatch, [(10, VIOLATION_LINE)])
    assert guard.find_violations(["components/x/main_test.go"], "origin/main") == []
    assert guard.find_violations(["docs/adr/034.md"], "origin/main") == []


def test_read_pr_body_prefers_file_then_env(monkeypatch, tmp_path):
    f = tmp_path / "body.md"
    f.write_text("from file", encoding="utf-8")
    assert guard._read_pr_body(str(f)) == "from file"

    monkeypatch.setenv("PR_BODY", "from env")
    assert guard._read_pr_body(None) == "from env"

    monkeypatch.delenv("PR_BODY", raising=False)
    assert guard._read_pr_body(None) is None


def test_main_exits_violation_on_hit(monkeypatch):
    _stub_diff(monkeypatch, [(10, VIOLATION_LINE)])
    monkeypatch.delenv("PR_BODY", raising=False)
    monkeypatch.setattr("sys.argv", ["prog", "--ci", "components/x/main.go"])
    assert guard.main() == guard.EXIT_VIOLATION


def test_main_bypass_tag_downgrades_to_ok(monkeypatch):
    """lint-policy.md §4: a (b) class lint must be overridable from the PR body."""
    _stub_diff(monkeypatch, [(10, VIOLATION_LINE)])
    monkeypatch.setenv(
        "PR_BODY",
        f"some prose\n\nbypass-lint: {guard.LINT_NAME}\nreason: re-exec sentinel, not config\n",
    )
    monkeypatch.setattr("sys.argv", ["prog", "--ci", "components/x/main.go"])
    assert guard.main() == guard.EXIT_OK


def test_main_bypass_for_a_different_lint_does_not_apply(monkeypatch):
    """The tag names one lint; it must not silently cover this one too."""
    _stub_diff(monkeypatch, [(10, VIOLATION_LINE)])
    monkeypatch.setenv(
        "PR_BODY", "bypass-lint: some-other-lint\nreason: unrelated\n"
    )
    monkeypatch.setattr("sys.argv", ["prog", "--ci", "components/x/main.go"])
    assert guard.main() == guard.EXIT_VIOLATION


def test_main_without_files_is_a_noop(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--ci"])
    assert guard.main() == guard.EXIT_OK


def test_main_clean_files_exit_ok(monkeypatch):
    _stub_diff(monkeypatch, [(10, "\tv, err := strconv.ParseBool(trimmed)")])
    monkeypatch.setattr("sys.argv", ["prog", "--ci", "components/x/main.go"])
    assert guard.main() == guard.EXIT_OK


def test_read_pr_body_survives_an_unreadable_file(monkeypatch, tmp_path, capsys):
    """A missing --pr-body-file must warn, not crash: losing the bypass channel
    should never take the lint itself down with it."""
    monkeypatch.delenv("PR_BODY", raising=False)
    assert guard._read_pr_body(str(tmp_path / "nope.md")) is None
    assert "cannot read" in capsys.readouterr().err
