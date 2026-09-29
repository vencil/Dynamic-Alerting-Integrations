"""Tests for scripts/tools/lint/check_image_caveats.py.

The gate's job is to make "the published image still behaves the old way"
caveats expire at release instead of by memory. The controls that matter most
are at the bottom: on the REAL tree the gate is green today, and it turns red
on every marker the moment the da-tools version moves past v2.9.0 — which is
what `bump_docs.py --tools` does at release.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# ⛔ One literal path string, so scripts/tools/dx/verify_diff.py maps a change
# to the gate onto this module.
_GATE = _REPO_ROOT / "scripts/tools/lint/check_image_caveats.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools"))

import check_image_caveats as gate  # noqa: E402

MARK = "<!-- image-caveat: v2.9.0 -->"
CUR = (2, 9, 0)


def _tree(tmp_path: Path, files: dict[str, str], version: str = "2.9.0") -> Path:
    root = tmp_path / "repo"
    (root / "components/da-tools/app").mkdir(parents=True)
    (root / "components/da-tools/app/VERSION").write_text(
        version + "\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [v2.9.0] — t\n", encoding="utf-8")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _kinds(root: Path, current=CUR) -> list[tuple[str, int, str]]:
    return [(f["file"], f["line"], f["kind"]) for f in gate.scan(root, current)]


# ── markers ────────────────────────────────────────────────────────────────

def test_a_current_marker_is_clean(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"The v2.9.0 image is old. {MARK}\n"})
    assert _kinds(root) == []


def test_a_marker_older_than_version_is_stale(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"The v2.9.0 image is old. {MARK}\n"})
    assert _kinds(root, (2, 10, 0)) == [("docs/a.md", 1, "stale")]


@pytest.mark.parametrize("body", ["v2.11.0", "latest", "", "2.9", "vNEXT"])
def test_a_future_or_unparseable_marker_is_malformed(tmp_path, body) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"x <!-- image-caveat: {body} -->\n"})
    assert _kinds(root) == [("docs/a.md", 1, "malformed")]


def test_a_marker_may_carry_a_note_after_the_version(tmp_path) -> None:
    root = _tree(tmp_path, {
        "docs/a.md": "x <!-- image-caveat: v2.9.0 — the comment above -->\n"})
    assert _kinds(root) == []
    assert _kinds(root, (2, 10, 0)) == [("docs/a.md", 1, "stale")]


# ── unmarked phrasing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "The v2.9.0 image still has the old behavior.",
    "⚠️ v2.9.0 映像還沒有這套結束碼。",
    "你手上這顆映像不接受這個旗標。",
    "on the image you have that raises AttributeError",
])
def test_the_caveat_phrasing_without_a_marker_is_reported(tmp_path, line) -> None:
    root = _tree(tmp_path, {"docs/a.md": line + "\n"})
    assert _kinds(root) == [("docs/a.md", 1, "unmarked")]


def test_a_marker_anywhere_in_the_paragraph_covers_it(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md":
                            f"First line {MARK}\nThe v2.9.0 image is old.\n"})
    assert _kinds(root) == []


def test_a_marker_in_another_paragraph_does_not(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md":
                            f"First {MARK}\n\nThe v2.9.0 image is old.\n"})
    assert _kinds(root) == [("docs/a.md", 3, "unmarked")]


def test_each_table_row_needs_its_own_marker(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": (
        "| a | b |\n|---|---|\n"
        f"| x | The v2.9.0 image is old. {MARK} |\n"
        "| y | The v2.9.0 image is old. |\n")})
    assert _kinds(root) == [("docs/a.md", 4, "unmarked")]


def test_a_fenced_caveat_takes_its_marker_right_after_the_fence(tmp_path) -> None:
    body = "```bash\n# the v2.9.0 image prints this on stdout\n```\n"
    ok = _tree(tmp_path / "ok", {"docs/a.md": body + MARK + "\n"})
    assert _kinds(ok) == []
    # A blank line between the fence and the marker detaches it.
    far = _tree(tmp_path / "far", {"docs/a.md": body + "\n" + MARK + "\n"})
    assert _kinds(far) == [("docs/a.md", 2, "unmarked")]


def test_a_marker_quoted_in_backticks_is_documentation_not_a_marker(tmp_path) -> None:
    """doc-template.md shows the convention as `<!-- image-caveat: v2.9.0 -->`.
    Counted as a marker, that example would go stale at the next release."""
    root = _tree(tmp_path, {"docs/a.md": f"Write `{MARK}` next to the caveat.\n"})
    assert _kinds(root, (2, 10, 0)) == []
    # ...and it does not cover a caveat in the same paragraph either.
    root2 = _tree(tmp_path / "b", {"docs/a.md":
                                   f"The v2.9.0 image is old. `{MARK}`\n"})
    assert _kinds(root2) == [("docs/a.md", 1, "unmarked")]


def test_a_phrase_quoted_in_backticks_is_not_a_caveat(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md":
                            "Phrasings such as `the image you have` are checked.\n"})
    assert _kinds(root) == []


def test_changelogs_are_not_scanned(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/CHANGELOG.md": "The v2.8.0 image did X.\n",
                            "CHANGELOG.md": "The v2.8.0 image did X.\n"})
    assert _kinds(root) == []


def test_the_root_readmes_and_components_are_scanned(tmp_path) -> None:
    root = _tree(tmp_path, {"README.en.md": "The v2.9.0 image is old.\n",
                            "components/x/README.md": "The v2.9.0 image is old.\n"})
    assert sorted(_kinds(root)) == [("README.en.md", 1, "unmarked"),
                                    ("components/x/README.md", 1, "unmarked")]


# ── caller errors ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", [None, "", "2.9", "v2.9.0", "latest"])
def test_an_unusable_version_file_is_not_read_as_a_version(tmp_path, version) -> None:
    root = _tree(tmp_path, {"docs/a.md": "x\n"})
    vf = root / "components/da-tools/app/VERSION"
    if version is None:
        vf.unlink()
    else:
        vf.write_text(version + "\n", encoding="utf-8")
    assert gate.read_tools_version(root) is None


def test_main_exits_2_without_a_version(tmp_path, monkeypatch, capsys) -> None:
    root = _tree(tmp_path, {"docs/a.md": "x\n"})
    (root / "components/da-tools/app/VERSION").unlink()
    monkeypatch.setattr(gate, "REPO_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["check_image_caveats.py"])
    assert gate.main() == 2
    assert "VERSION" in capsys.readouterr().err


def test_main_exits_1_on_a_finding_and_0_when_clean(tmp_path, monkeypatch) -> None:
    root = _tree(tmp_path, {"docs/a.md": "The v2.9.0 image is old.\n"})
    monkeypatch.setattr(gate, "REPO_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["check_image_caveats.py"])
    assert gate.main() == 1
    (root / "docs/a.md").write_text(f"The v2.9.0 image is old. {MARK}\n",
                                    encoding="utf-8")
    assert gate.main() == 0


# ── the real tree ──────────────────────────────────────────────────────────

def test_the_real_tree_is_clean_today() -> None:
    p = subprocess.run(
        [sys.executable, str(_GATE)], cwd=_REPO_ROOT, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert p.returncode == 0, p.stderr


def test_every_real_marker_expires_when_the_version_moves() -> None:
    """Positive control: the release bump is what makes the markers fire.

    Every marker in the tree names the current image, so pretending the next
    version is current must report each of them as stale, and nothing else.
    The expected count is derived from the tree, not written down.
    """
    current = gate.read_tools_version(_REPO_ROOT)
    assert current is not None
    # Counted the way the gate reads them: a marker quoted in backticks
    # (doc-template.md documents the convention that way) is not one.
    markers = sum(
        len(gate.MARKER_RE.findall(gate._CODE_SPAN_RE.sub("", line)))
        for p in gate.iter_markdown(_REPO_ROOT)
        for line in p.read_text(encoding="utf-8").splitlines()
    )
    assert markers > 0, "no markers in the tree; this control measures nothing"
    bumped = (current[0], current[1] + 1, 0)
    findings = gate.scan(_REPO_ROOT, bumped)
    assert {f["kind"] for f in findings} == {"stale"}, findings
    assert len(findings) == markers


# ── since markers（本輪決策 R2）─────────────────────────────────────────────
#
# "v2.10.0 起…" statements name the release a behaviour ships in. They are true
# only if that version is actually released — v3.0.0 was cut instead of
# v2.10.0, and nothing tied those lines to a version. The released set is the
# CHANGELOG's `## [vX.Y.Z]` headings: the release wrap-up adds the new one
# before `make pre-tag`, so at tag time the version being cut is in it.

SINCE = "<!-- since: v3.0.0 -->"
CHANGELOG = "## [Unreleased]\n\n## [v2.9.0] — t\n\n## [v2.8.1] — t\n\n## [v2.8.0] — t\n"


def _since(root: Path, pre_tag: bool = False, changelog: str = CHANGELOG):
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    released = gate.read_released_versions(root)
    assert released is not None
    return [(f["file"], f["line"], f["kind"])
            for f in gate.scan_since(root, released, pre_tag=pre_tag)]


def test_released_versions_are_the_changelog_headings(tmp_path) -> None:
    root = _tree(tmp_path, {})
    (root / "CHANGELOG.md").write_text(
        CHANGELOG + "## [2.7.0] — no v, not a heading bump_docs writes\n",
        encoding="utf-8")
    assert gate.read_released_versions(root) == {(2, 9, 0), (2, 8, 1), (2, 8, 0)}


@pytest.mark.parametrize("text", [None, "", "## [Unreleased]\n"])
def test_no_changelog_headings_is_not_an_empty_released_set(tmp_path, text) -> None:
    root = _tree(tmp_path, {})
    if text is None:
        (root / "CHANGELOG.md").unlink()
    else:
        (root / "CHANGELOG.md").write_text(text, encoding="utf-8")
    assert gate.read_released_versions(root) is None


def test_an_unreleased_since_marker_is_clean_during_development(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"From v3.0.0 this exits 2. {SINCE}\n"})
    assert _since(root) == []


def test_an_unreleased_since_marker_fails_the_pre_tag_gate(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"From v3.0.0 this exits 2. {SINCE}\n"})
    assert _since(root, pre_tag=True) == [("docs/a.md", 1, "since-unreleased")]


def test_the_release_heading_clears_the_pre_tag_gate(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"From v3.0.0 this exits 2. {SINCE}\n"})
    assert _since(root, pre_tag=True,
                  changelog="## [v3.0.0] — t\n\n" + CHANGELOG) == []


def test_a_skipped_version_is_never_released(tmp_path) -> None:
    """The case this exists for: docs said v2.10.0, the release was v3.0.0."""
    root = _tree(tmp_path, {
        "docs/a.md": "From v2.10.0 this exits 2. <!-- since: v2.10.0 -->\n"})
    released = "## [v3.0.0] — t\n\n" + CHANGELOG
    assert _since(root, changelog=released) == [
        ("docs/a.md", 1, "since-never-released")]
    assert _since(root, pre_tag=True, changelog=released) == [
        ("docs/a.md", 1, "since-never-released")]


@pytest.mark.parametrize("body", ["latest", "", "3.0", "vNEXT"])
def test_an_unparseable_since_marker_is_malformed(tmp_path, body) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"x <!-- since: {body} -->\n"})
    assert _since(root) == [("docs/a.md", 1, "since-malformed")]


@pytest.mark.parametrize("line", [
    "⚠️ **v3.0.0 起這一格的失敗模式改了**",
    "它的 `-o` 自 v3.0.0 起把寫入失敗攔成結束碼 2",
    "--validate 在 v3.0.0 之前會印 OK",
    "（v3.0.0 前只印 WARN、結束碼 0）",
    "⚠️ **上列是 v3.0.0 的契約**",
    "**From v3.0.0 the pair is refused outright**",
    "before v3.0.0 the shape exits 0",
    "The failure mode changed in v3.0.0.",
    "blocking since v3.0.0",
    "**This row is the v3.0.0 contract**",
    "retired in v3.0.0",
    "v2.9.0 delivered + v3.0.0+ exploration",
])
def test_since_phrasing_about_an_unreleased_version_needs_a_marker(tmp_path, line) -> None:
    root = _tree(tmp_path, {"docs/a.md": line + "\n"})
    assert _since(root) == [("docs/a.md", 1, "since-unmarked")]
    root2 = _tree(tmp_path / "m", {"docs/a.md": f"{line} {SINCE}\n"})
    assert _since(root2) == []


@pytest.mark.parametrize("line", [
    "v2.8.0 起 conf.d 支援階層式目錄",          # released: history, not a promise
    "Since v2.9.0 the image ships amtool.",
    "`v3.0.0 起` 是這類句型的寫法",             # quoted, not said
    "image: prom/prometheus:v2.55.0",           # a third-party version, no phrasing
    "對 v3.0.0 / v4.0.0 等 major bump 用同 skeleton",
])
def test_other_version_mentions_are_not_since_statements(tmp_path, line) -> None:
    root = _tree(tmp_path, {"docs/a.md": line + "\n"})
    assert _since(root) == []


def test_a_since_marker_anywhere_in_the_paragraph_covers_it(tmp_path) -> None:
    root = _tree(tmp_path, {
        "docs/a.md": f"From v3.0.0 the pair is refused.\nexit 2. {SINCE}\n\n"
                     "Before v3.0.0 it exited 0.\n"})
    assert _since(root) == [("docs/a.md", 4, "since-unmarked")]


def test_an_image_caveat_marker_is_not_a_since_marker(tmp_path) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"From v3.0.0 this exits 2. {MARK}\n"})
    assert _since(root) == [("docs/a.md", 1, "since-unmarked")]


def test_main_exits_2_without_changelog_headings(tmp_path, monkeypatch, capsys) -> None:
    root = _tree(tmp_path, {"docs/a.md": "x\n"})
    (root / "CHANGELOG.md").unlink()
    monkeypatch.setattr(gate, "REPO_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["check_image_caveats.py"])
    assert gate.main() == 2
    assert "CHANGELOG" in capsys.readouterr().err


def test_main_pre_tag_flag_turns_an_unreleased_marker_red(tmp_path, monkeypatch) -> None:
    root = _tree(tmp_path, {"docs/a.md": f"From v3.0.0 this exits 2. {SINCE}\n"})
    (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["check_image_caveats.py"])
    assert gate.main() == 0
    monkeypatch.setattr(sys, "argv", ["check_image_caveats.py", "--pre-tag"])
    assert gate.main() == 1


def _real_since_markers() -> list[tuple[str, tuple[int, int, int]]]:
    out = []
    for p in gate.iter_markdown(_REPO_ROOT):
        for line in p.read_text(encoding="utf-8").splitlines():
            for m in gate.SINCE_MARKER_RE.finditer(gate._CODE_SPAN_RE.sub("", line)):
                vm = gate._MARKER_VERSION_RE.match(m.group("body").strip())
                assert vm, (p, line)
                out.append((p.as_posix(), tuple(int(g) for g in vm.groups())))
    return out


def test_every_real_since_marker_is_held_by_the_pre_tag_gate() -> None:
    """Positive control on the real tree, derived rather than written down.

    Every since marker names a version that is either released (clean) or not
    yet (red under --pre-tag until the wrap-up adds its CHANGELOG heading).
    And if the release were cut under a DIFFERENT number — the v2.10.0 → v3.0.0
    case — every unreleased marker turns never-released, in both modes.
    """
    released = gate.read_released_versions(_REPO_ROOT)
    assert released is not None
    markers = _real_since_markers()
    assert markers, "no since markers in the tree; this control measures nothing"
    pending = [v for _, v in markers if v not in released]
    findings = gate.scan_since(_REPO_ROOT, released, pre_tag=True)
    assert {f["kind"] for f in findings} <= {"since-unreleased"}, findings
    assert len(findings) == len(pending)
    if pending:
        skipped_to = (max(pending)[0] + 1, 0, 0)
        cut = gate.scan_since(_REPO_ROOT, released | {skipped_to}, pre_tag=False)
        assert {f["kind"] for f in cut} == {"since-never-released"}, cut
        assert len(cut) == len(pending)
