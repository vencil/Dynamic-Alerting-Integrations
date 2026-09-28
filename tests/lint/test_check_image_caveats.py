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
    markers = sum(
        len(gate.MARKER_RE.findall(p.read_text(encoding="utf-8")))
        for p in gate.iter_markdown(_REPO_ROOT)
    )
    assert markers > 0, "no markers in the tree; this control measures nothing"
    bumped = (current[0], current[1] + 1, 0)
    findings = gate.scan(_REPO_ROOT, bumped)
    assert {f["kind"] for f in findings} == {"stale"}, findings
    assert len(findings) == markers
