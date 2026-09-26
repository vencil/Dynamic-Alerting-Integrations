"""changelog.d/ fragments and the [Unreleased] freeze (#2102).

In-flight changelog entries moved out of CHANGELOG.md into one file per
change, because every PR appending to the same few blocks of one file made
any two parallel PRs conflict. These tests pin:

* the fragment contract (`generate_changelog.lint_fragment`): front matter
  keys and values, exactly one entry, the per-entry cap;
* assembly order (`assemble_fragments`): section, then topic, then created;
* the freeze (`lint_unreleased_frozen`): [Unreleased] may shrink or be
  corrected, never gain an entry;
* the CLI and the live tree: every fragment checked in is clean.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import generate_changelog as gc
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_changelog.py"


def _fragment(section="Added", topic="ci", issues="[2102]",
              created="2026-09-26T15:40:00+08:00", body="- **headline**: what changed.\n",
              extra_meta=""):
    return (f"---\nsection: {section}\ntopic: {topic}\nissues: {issues}\n"
            f"created: {created}\n{extra_meta}---\n{body}")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ── lint_fragment ────────────────────────────────────────────────────

def test_a_wellformed_fragment_is_clean(tmp_path):
    assert gc.lint_fragment(_write(tmp_path, "a.md", _fragment())) == []


def test_continuation_lines_and_a_quoted_timestamp_are_accepted(tmp_path):
    body = "- **headline**: first line\n  - a sub-bullet\n  continued.\n\n"
    text = _fragment(created='"2026-09-26T15:40:00+00:00"', body=body)
    assert gc.lint_fragment(_write(tmp_path, "a.md", text)) == []


@pytest.mark.parametrize("text, needle", [
    ("- **no front matter**: x\n", "no front matter"),
    (_fragment(section="Improved"), "section 'Improved'"),
    (_fragment(topic="Exporter Stuff"), "not kebab-case"),
    (_fragment(issues="2102"), "issues must be a list"),
    (_fragment(issues="[true]"), "issues must be a list"),
    (_fragment(issues="[0]"), "issues must be a list"),
    (_fragment(created="2026-09-26T15:40:00"), "UTC offset"),
    (_fragment(created="yesterday"), "UTC offset"),
    (_fragment(extra_meta="owner: me\n"), "unknown key(s) owner"),
    ("---\nsection: Added\ntopic: ci\ncreated: 2026-09-26T15:40:00+08:00\n---\n- **x**: y\n",
     "lacks issues"),
    ("---\n: [unbalanced\n---\n- **x**: y\n", "not valid YAML"),
    ("---\n- a list\n---\n- **x**: y\n", "not a mapping"),
])
def test_each_front_matter_defect_is_named(tmp_path, text, needle):
    problems = gc.lint_fragment(_write(tmp_path, "a.md", text))
    assert any(needle in p for p in problems), problems


@pytest.mark.parametrize("body, needle", [
    ("", "found 0"),
    ("prose, no bullet\n", "found 0"),
    ("- **one**: a\n- **two**: b\n", "found 2"),
    ("- **one**: a\n\nstray paragraph\n", "text outside the entry"),
    ("intro line\n- **one**: a\n", "text outside the entry"),
])
def test_the_body_must_be_exactly_one_entry(tmp_path, body, needle):
    problems = gc.lint_fragment(_write(tmp_path, "a.md", _fragment(body=body)))
    assert any(needle in p for p in problems), problems


def test_the_entry_cap_uses_the_changelog_ruler(tmp_path):
    head = "- **h**: "
    at = head + "x" * (gc.ENTRY_CAP - len(head))
    assert gc.lint_fragment(_write(tmp_path, "at.md", _fragment(body=at + "\n"))) == []
    over = _write(tmp_path, "over.md", _fragment(body=at + "x\n"))
    assert any("over the 1000-char cap" in p for p in gc.lint_fragment(over))
    assert gc.lint_fragment(over, cap=0) == []


# ── assemble_fragments ───────────────────────────────────────────────

def test_assembly_orders_by_section_then_topic_then_created(tmp_path):
    paths = [
        _write(tmp_path, "z-fix.md", _fragment(section="Fixed", topic="aa",
                                               body="- **fix**: f\n")),
        _write(tmp_path, "b-late.md", _fragment(topic="exporter", created="2026-09-02T00:00:00+00:00",
                                                body="- **exporter later**: revised\n")),
        _write(tmp_path, "a-early.md", _fragment(topic="exporter", created="2026-09-01T00:00:00+00:00",
                                                 body="- **exporter first**: original\n")),
        _write(tmp_path, "c-ci.md", _fragment(topic="ci", issues="[7]", body="- **ci**: c\n")),
    ]
    markdown, problems, notes = gc.assemble_fragments(paths)
    assert problems == []
    order = [ln for ln in markdown.splitlines() if ln.startswith(("### ", "<!-- topic", "- "))]
    assert order == [
        "### Added",
        "<!-- topic: ci -->", "- **ci**: c",
        "<!-- topic: exporter -->", "- **exporter first**: original", "- **exporter later**: revised",
        "### Fixed",
        "<!-- topic: aa -->", "- **fix**: f",
    ]
    # #2102 appears in three fragments: the assembler says so, it does not fail.
    assert len(notes) == 1 and "#2102 has 3 fragments" in notes[0]


def test_assembly_keeps_mixed_offsets_in_real_time_order(tmp_path):
    """09:00+08:00 is 01:00Z, which is earlier than 02:00Z."""
    paths = [
        _write(tmp_path, "a.md", _fragment(created="2026-09-01T02:00:00+00:00", body="- **utc**: u\n")),
        _write(tmp_path, "b.md", _fragment(created="2026-09-01T09:00:00+08:00", body="- **tpe**: t\n")),
    ]
    markdown, _, _ = gc.assemble_fragments(paths)
    assert markdown.index("**tpe**") < markdown.index("**utc**")


def test_a_broken_fragment_is_left_out_and_named(tmp_path):
    good = _write(tmp_path, "good.md", _fragment(body="- **good**: g\n"))
    bad = _write(tmp_path, "bad.md", _fragment(section="Nope", body="- **bad**: b\n"))
    markdown, problems, _ = gc.assemble_fragments([good, bad])
    assert "**good**" in markdown and "**bad**" not in markdown
    assert len(problems) == 1 and "bad.md" in problems[0]


def test_nothing_to_assemble_is_empty(tmp_path):
    assert gc.assemble_fragments([]) == ("", [], [])


# ── lint_unreleased_frozen ───────────────────────────────────────────

def _unreleased(*entries: str) -> str:
    return "## [Unreleased]\n\n### Changed\n" + "\n".join(entries) + "\n"


def test_a_new_unreleased_entry_is_refused_and_points_at_the_fragments():
    base = _unreleased("- **old**: o")
    head = _unreleased("- **old**: o", "- **new**: n")
    issues = gc.lint_unreleased_frozen(head, base, base_label="origin/main")
    assert len(issues) == 1
    assert "gained 1 entry over origin/main" in issues[0] and "changelog.d/" in issues[0]


def test_shrinking_or_correcting_unreleased_is_allowed():
    base = _unreleased("- **a**: a", "- **b**: b")
    assert gc.lint_unreleased_frozen(_unreleased("- **a**: a"), base) == []
    assert gc.lint_unreleased_frozen(_unreleased("- **a**: fixed typo", "- **b**: b"), base) == []
    assert gc.lint_unreleased_frozen(base, base) == []


def test_the_freeze_is_skipped_without_a_section_on_either_side():
    head = _unreleased("- **a**: a")
    assert gc.lint_unreleased_frozen(head, None) == []
    assert gc.lint_unreleased_frozen(head, "# Changelog\n") == []
    assert gc.lint_unreleased_frozen("## [v1.0.0] (2026-01-01)\n### Added\n- x\n", head) == []


# ── CLI ──────────────────────────────────────────────────────────────

def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def test_cli_fragments_exit_codes(tmp_path):
    good = _write(tmp_path, "good.md", _fragment())
    bad = _write(tmp_path, "bad.md", _fragment(topic="Bad Topic"))
    assert _run("--fragments", str(good)).returncode == EXIT_OK
    proc = _run("--fragments", str(good), str(bad))
    assert proc.returncode == EXIT_VIOLATION and "bad.md" in proc.stdout
    assert _run("--fragments", str(tmp_path / "missing.md")).returncode == EXIT_CALLER_ERROR


def test_cli_skips_the_readme_it_is_handed(tmp_path):
    """pre-commit passes changed files; the README is documentation, not a
    fragment, even when it is the only file in the commit."""
    readme = _write(tmp_path, "README.md", "# not a fragment\n")
    proc = _run("--fragments", str(readme))
    assert proc.returncode == EXIT_OK, proc.stdout + proc.stderr


def test_cli_assemble_prints_markdown_and_fails_on_a_broken_fragment(tmp_path):
    good = _write(tmp_path, "good.md", _fragment(body="- **good**: g\n"))
    proc = _run("--assemble", "--fragments", str(good))
    assert proc.returncode == EXIT_OK and "- **good**: g" in proc.stdout
    bad = _write(tmp_path, "bad.md", _fragment(section="Nope"))
    proc = _run("--assemble", "--fragments", str(good), str(bad))
    assert proc.returncode == EXIT_VIOLATION and "bad.md" in proc.stderr


# ── Live tree ────────────────────────────────────────────────────────

def test_every_checked_in_fragment_is_clean():
    paths = gc.fragment_paths(REPO_ROOT)
    problems = [f"{p.name}: {i}" for p in paths for i in gc.lint_fragment(p)]
    assert problems == []


def test_the_readme_documents_every_section_the_linter_accepts():
    readme = (REPO_ROOT / gc.FRAGMENT_DIR / gc.FRAGMENT_README).read_text(encoding="utf-8")
    missing = [s for s in gc.FRAGMENT_SECTIONS if f"`{s}`" not in readme]
    assert missing == []
