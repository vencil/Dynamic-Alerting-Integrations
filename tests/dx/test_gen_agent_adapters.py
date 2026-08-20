"""Tests for scripts/tools/dx/gen_agent_adapters.py (TRK-361).

The generator is the only thing standing between the neutral `agents/` SSOT and
what each vendor's agent actually reads. Two properties carry the whole design
and both are pinned here rather than argued:

  * an adapter is its source byte-for-byte plus ONE provenance line -- if that
    stops being true, `--check` stops being an exact comparison and the drift
    gate quietly weakens into a fuzzy one;
  * `agents/skills/<n>/SKILL.md` and `.claude/skills/<n>/SKILL.md` sit at the
    same depth, so every `../../../docs/...` link in the corpus resolves from
    both. That equivalence is why the migration touched zero links, and a future
    relocation of either tree would break it silently.

Coverage:
  - project(): frontmatter present / absent / unterminated fence; CRLF and a
    missing trailing newline preserved; round-trip is source + exactly one line
  - read_frontmatter_field(): present / missing field / no frontmatter
  - build_entry(): markers required, index replaced, previous index discarded,
    and AGENTS.md is its own source rather than a projected copy
  - planned_outputs(): missing SSOT dir raises FileNotFoundError
  - diff_against_disk(): clean / missing / stale / extra
  - write_outputs(): writes, removes stale, prunes emptied dirs
  - main(): --generate, --check clean, --check drifted, missing SSOT, mode is
    required and mutually exclusive
  - repo invariants: real adapters in sync, every SSOT relative link resolves
    from BOTH trees, every adapter carries its provenance line
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx",
))
import gen_agent_adapters as gaa  # noqa: E402


FM = b"---\nname: x\ndescription: d\n---\n"


# ============================================================
# project — the one transformation
# ============================================================


def test_provenance_lands_after_frontmatter():
    out = gaa.project(FM + b"body\n", "agents/skills/x/SKILL.md")
    assert out.startswith(FM), "frontmatter must still start at byte 0"
    rest = out[len(FM):]
    assert rest.startswith(gaa.provenance("agents/skills/x/SKILL.md"))
    assert rest.endswith(b"body\n")


def test_provenance_lands_on_top_when_there_is_no_frontmatter():
    out = gaa.project(b"# just a doc\n", "agents/skills/x/references/d.md")
    assert out.startswith(gaa.provenance("agents/skills/x/references/d.md"))
    assert out.endswith(b"# just a doc\n")


def test_an_unterminated_fence_is_not_treated_as_frontmatter():
    """`---` with no closing fence is horizontal-rule content, not metadata."""
    out = gaa.project(b"---\nnot closed\n", "agents/skills/x/SKILL.md")
    assert out.startswith(gaa.provenance("agents/skills/x/SKILL.md"))


def test_crlf_bytes_survive_untouched():
    """A Windows-committed file must not be reflowed (#1363)."""
    raw = b"---\r\nname: x\r\n---\r\nbody\r\n"
    out = gaa.project(raw, "agents/skills/x/SKILL.md")
    assert b"\r\n" in out
    assert out.count(b"\n") == raw.count(b"\n") + 1  # exactly one line added


def test_a_missing_trailing_newline_is_preserved():
    out = gaa.project(FM + b"no trailing newline", "agents/skills/x/SKILL.md")
    assert out.endswith(b"no trailing newline")


def test_projection_adds_exactly_one_line_and_changes_nothing_else():
    src = FM + b"line one\nline two\n"
    out = gaa.project(src, "agents/skills/x/SKILL.md")
    marker = gaa.provenance("agents/skills/x/SKILL.md")
    stripped = b"".join(
        l + b"\n" for l in out.split(b"\n")[:-1] if l + b"\n" != marker)
    assert stripped == src


def test_projection_is_not_idempotent_and_that_is_the_point():
    """Re-projecting an adapter would stack lines -- generation reads the SSOT.

    Pinned because an 'optimisation' that projected the adapter in place would
    pass a casual eyeball and corrupt the file on the second run.
    """
    once = gaa.project(FM + b"b\n", "agents/skills/x/SKILL.md")
    twice = gaa.project(once, "agents/skills/x/SKILL.md")
    assert twice.count(gaa.provenance("agents/skills/x/SKILL.md")) == 2


# ============================================================
# read_frontmatter_field
# ============================================================


@pytest.mark.parametrize("field,expected", [
    ("name", "x"), ("description", "d"), ("model", None),
])
def test_read_frontmatter_field(field, expected):
    assert gaa.read_frontmatter_field(FM, field) == expected


def test_read_frontmatter_field_without_frontmatter():
    assert gaa.read_frontmatter_field(b"# doc\n", "name") is None


def test_read_frontmatter_field_with_unterminated_fence():
    assert gaa.read_frontmatter_field(b"---\nname: x\n", "name") is None


# ============================================================
# build_entry — the neutral entry point
# ============================================================


def _fake_ssot(tmp_path, monkeypatch, skills=("alpha",), base_text=None):
    for name in skills:
        d = tmp_path / gaa.SSOT_SKILLS / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(
            f"---\nname: {name}\ndescription: does {name}\n---\nbody\n".encode())
    (tmp_path / gaa.SSOT_ROLES).mkdir(parents=True)
    (tmp_path / gaa.SSOT_ROLES / "r.md").write_bytes(b"---\nname: r\n---\nrole\n")
    if base_text is None:
        base_text = f"# Head\n\n{gaa.INDEX_BEGIN}\nstale\n{gaa.INDEX_END}\n\ntail\n"
    (tmp_path / gaa.OUT_ENTRY).write_text(base_text, encoding="utf-8")
    monkeypatch.setattr(gaa, "REPO_ROOT", str(tmp_path))
    return tmp_path


def test_build_entry_replaces_the_index_and_keeps_the_prose(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch, skills=("alpha", "beta"))
    text = gaa.build_entry().decode("utf-8")
    assert "# Head" in text and "tail" in text
    assert "stale" not in text
    assert "[`alpha`]" in text and "[`beta`]" in text
    assert "does alpha" in text
    assert text.index("[`alpha`]") < text.index("[`beta`]"), "index is sorted"


def test_build_entry_requires_both_markers(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch, base_text="# Head\nno markers\n")
    with pytest.raises(ValueError):
        gaa.build_entry()


def test_build_entry_is_its_own_source_and_takes_no_provenance_line(tmp_path,
                                                                     monkeypatch):
    """AGENTS.md is hand-written prose, not a projected copy.

    It must sit at the repo root for the vendors that read it. A source file one
    directory down would need every `docs/...` link rewritten on projection --
    measured: an `agents/AGENTS.base.md` with root-relative links produced 9
    broken links the moment the corpus came under the doc-link gate.
    """
    _fake_ssot(tmp_path, monkeypatch)
    out = gaa.build_entry()
    assert not out.startswith(b"<!-- ")
    assert out.startswith(b"# Head")


def test_a_skill_dir_without_a_skill_md_is_skipped(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    (tmp_path / gaa.SSOT_SKILLS / "empty").mkdir()
    assert "[`empty`]" not in gaa.build_entry().decode("utf-8")


# ============================================================
# planned_outputs / diff_against_disk / write_outputs
# ============================================================


def test_planned_outputs_raises_when_the_ssot_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(gaa, "REPO_ROOT", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        gaa.planned_outputs()


def test_generate_then_check_is_clean(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    assert gaa.diff_against_disk(gaa.planned_outputs()) == ([], [], [])


def test_check_reports_a_hand_edited_adapter_as_stale(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    (tmp_path / gaa.OUT_SKILLS / "alpha" / "SKILL.md").write_bytes(b"edited\n")
    _missing, stale, _extra = gaa.diff_against_disk(gaa.planned_outputs())
    assert stale == [f"{gaa.OUT_SKILLS}/alpha/SKILL.md"]


def test_check_reports_a_deleted_adapter_as_missing(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    (tmp_path / gaa.OUT_SKILLS / "alpha" / "SKILL.md").unlink()
    missing, _stale, _extra = gaa.diff_against_disk(gaa.planned_outputs())
    assert missing == [f"{gaa.OUT_SKILLS}/alpha/SKILL.md"]


def test_check_reports_an_adapter_with_no_source_as_extra(tmp_path, monkeypatch):
    """A skill deleted from the SSOT must not keep being discovered by a vendor."""
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    ghost = tmp_path / gaa.OUT_SKILLS / "ghost"
    ghost.mkdir()
    (ghost / "SKILL.md").write_bytes(b"x\n")
    _missing, _stale, extra = gaa.diff_against_disk(gaa.planned_outputs())
    assert extra == [f"{gaa.OUT_SKILLS}/ghost/SKILL.md"]


def test_write_outputs_removes_stale_files_and_prunes_their_dirs(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    ghost = tmp_path / gaa.OUT_SKILLS / "ghost"
    ghost.mkdir()
    (ghost / "SKILL.md").write_bytes(b"x\n")
    removed = gaa.write_outputs(gaa.planned_outputs())
    assert removed == [f"{gaa.OUT_SKILLS}/ghost/SKILL.md"]
    assert not ghost.exists(), "an emptied dir is still discovered by the vendor"


def test_references_subdirs_are_projected_too(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    refs = tmp_path / gaa.SSOT_SKILLS / "alpha" / "references"
    refs.mkdir()
    (refs / "derivation.md").write_bytes(b"# why\n")
    gaa.write_outputs(gaa.planned_outputs())
    out = tmp_path / gaa.OUT_SKILLS / "alpha" / "references" / "derivation.md"
    assert out.is_file()
    assert out.read_bytes().endswith(b"# why\n")


# ============================================================
# main — exit-code contract
# ============================================================


def test_main_generate_then_check(tmp_path, monkeypatch, capsys):
    _fake_ssot(tmp_path, monkeypatch)
    assert gaa.main(["--generate"]) == gaa.EXIT_OK
    assert gaa.main(["--check"]) == gaa.EXIT_OK
    assert "in sync" in capsys.readouterr().out


def test_main_check_exits_violation_on_drift(tmp_path, monkeypatch, capsys):
    _fake_ssot(tmp_path, monkeypatch)
    gaa.main(["--generate"])
    (tmp_path / gaa.OUT_SKILLS / "alpha" / "SKILL.md").write_bytes(b"edited\n")
    assert gaa.main(["--check"]) == gaa.EXIT_VIOLATION
    assert "drifted" in capsys.readouterr().err


def test_main_exits_caller_error_without_an_ssot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gaa, "REPO_ROOT", str(tmp_path))
    assert gaa.main(["--check"]) == gaa.EXIT_CALLER_ERROR
    assert "SSOT path missing" in capsys.readouterr().err


def test_main_exits_caller_error_when_the_entry_has_no_markers(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch, base_text="# no markers\n")
    assert gaa.main(["--check"]) == gaa.EXIT_CALLER_ERROR


@pytest.mark.parametrize("argv", [[], ["--generate", "--check"]])
def test_main_requires_exactly_one_mode(argv):
    with pytest.raises(SystemExit) as excinfo:
        gaa.main(argv)
    assert excinfo.value.code == 2


# ============================================================
# repo invariants — the real tree, not a fixture
# ============================================================


def test_the_checked_in_adapters_are_in_sync():
    assert gaa.main(["--check"]) == gaa.EXIT_OK


def test_every_projected_adapter_carries_its_provenance_line():
    """Every `.claude/**` file says where it came from -- and AGENTS.md does not.

    The exception is asserted rather than skipped: AGENTS.md is hand-edited
    prose, so a provenance line telling a reader "do not edit this copy" would
    be actively wrong there. If it ever gains one, this test should fail and the
    author should have to justify the change.
    """
    plan = gaa.planned_outputs()
    projected = {k: v for k, v in plan.items() if k != gaa.OUT_ENTRY}
    assert len(projected) == len(plan) - 1
    for dest_rel, data in projected.items():
        source_rel = gaa.source_of(dest_rel)
        assert gaa.provenance(source_rel) in data, dest_rel
        # The load-bearing half of the marker is that it NAMES its source.
        assert source_rel.encode("utf-8") in data, dest_rel
    # AGENTS.md is its own source, so it takes no provenance line. It DOES
    # legitimately contain the word GENERATED, in its index markers — assert
    # the derived marker's absence, not a substring that also appears there.
    assert not plan[gaa.OUT_ENTRY].startswith(gaa.provenance(gaa.OUT_ENTRY))


def test_every_ssot_relative_link_resolves_from_both_trees():
    """The depth equivalence the whole migration rests on.

    `agents/skills/<n>/` and `.claude/skills/<n>/` are both three levels below
    the repo root, so a `../../../docs/...` link is correct from either. Moving
    one tree to a different depth would break every such link in the corpus at
    once, and nothing else in CI would notice.
    """
    pattern = re.compile(r'\]\((\.\./[^)#\s]+)')
    checked = 0
    for source_rel in gaa.iter_ssot(gaa.SSOT_SKILLS) + gaa.iter_ssot(gaa.SSOT_ROLES):
        if not source_rel.endswith(".md"):
            continue
        with open(os.path.join(gaa.REPO_ROOT, source_rel), encoding="utf-8") as fh:
            text = fh.read()
        adapter_rel = (gaa.OUT_SKILLS + source_rel[len(gaa.SSOT_SKILLS):]
                       if source_rel.startswith(gaa.SSOT_SKILLS)
                       else gaa.OUT_ROLES + source_rel[len(gaa.SSOT_ROLES):])
        for link in pattern.findall(text):
            checked += 1
            for base in (source_rel, adapter_rel):
                target = os.path.normpath(os.path.join(
                    gaa.REPO_ROOT, os.path.dirname(base), link))
                assert os.path.exists(target), f"{base} -> {link}"
    assert checked >= 29, f"only {checked} relative links seen; corpus shrank?"


# ============================================================
# provenance comment syntax per file type (CodeRabbit #1481 C1)
# ============================================================


@pytest.mark.parametrize("source,expected_open", [
    ("agents/skills/a/SKILL.md", b"<!-- "),
    ("agents/skills/a/notes.markdown", b"<!-- "),
    ("agents/skills/a/w.js", b"// "),
    ("agents/skills/a/w.mjs", b"// "),
    ("agents/skills/a/w.ts", b"// "),
    ("agents/skills/a/h.py", b"# "),
    ("agents/skills/a/h.sh", b"# "),
    ("agents/skills/a/c.yaml", b"# "),
])
def test_provenance_uses_the_adapter_language_comment(source, expected_open):
    assert gaa.provenance(source).startswith(expected_open)


def test_provenance_is_empty_for_an_unknown_suffix():
    """Silence is recoverable; a syntactically invalid marker is not.

    The drift gate still owns such a file byte-for-byte, so the only thing a
    missing marker costs is the in-file hint.
    """
    assert gaa.provenance("agents/skills/a/data.bin") == b""


def test_an_html_comment_never_reaches_a_javascript_adapter():
    """The defect this replaced: `<!-- -->` in a .js file.

    It parsed only because Annex B keeps legacy HTML-comment syntax alive in
    sloppy scripts, and it is a hard parse error the moment anything reads the
    file as an ES module.
    """
    out = gaa.project(b"const a = 1;\n", "agents/skills/a/w.js")
    assert b"<!--" not in out
    assert out.startswith(b"// ")
    assert out.endswith(b"const a = 1;\n")


def test_javascript_projection_keeps_the_body_byte_identical():
    body = b"export const meta = {};\nreturn { ok: true };\n"
    out = gaa.project(body, "agents/skills/a/w.js")
    assert out.split(b"\n", 1)[1] == body


# ============================================================
# CRLF frontmatter (CodeRabbit #1481 C6)
# ============================================================


CRLF_FM = b"---\r\nname: x\r\ndescription: d\r\n---\r\n"


def test_crlf_frontmatter_is_recognised_and_stays_at_byte_zero():
    """An unrecognised CRLF fence would push the frontmatter off the top.

    That is not cosmetic: every vendor stops seeing the skill's name and
    description, so the skill silently stops being routable.
    """
    out = gaa.project(CRLF_FM + b"body\r\n", "agents/skills/a/SKILL.md")
    assert out.startswith(b"---\r\n")
    assert out[:len(CRLF_FM)] == CRLF_FM


def test_crlf_provenance_is_inserted_after_the_closing_fence():
    out = gaa.project(CRLF_FM + b"body\r\n", "agents/skills/a/SKILL.md")
    after = out[len(CRLF_FM):]
    assert after.startswith(b"<!-- ")
    assert after.split(b"\r\n", 1)[1] == b"body\r\n"


def test_crlf_provenance_terminates_with_crlf_not_lf():
    """Matching the file's own ending keeps the adapter byte-comparable.

    An LF-terminated line inside a CRLF file leaves one odd line that every
    later diff carries.
    """
    out = gaa.project(CRLF_FM + b"body\r\n", "agents/skills/a/SKILL.md")
    marker_line = out[len(CRLF_FM):].split(b"\r\n", 1)[0]
    assert not marker_line.endswith(b"\r")
    assert out[len(CRLF_FM) + len(marker_line):].startswith(b"\r\n")


def test_read_frontmatter_field_handles_crlf():
    assert gaa.read_frontmatter_field(CRLF_FM + b"body\r\n", "description") == "d"
    assert gaa.read_frontmatter_field(CRLF_FM + b"body\r\n", "name") == "x"


def test_lf_and_crlf_frontmatter_agree_on_the_field_values():
    assert (gaa.read_frontmatter_field(CRLF_FM, "name")
            == gaa.read_frontmatter_field(FM, "name").replace("x", "x"))


# ============================================================
# symlink and path-escape hardening (CodeRabbit #1481 C5)
# ============================================================


def _symlinks_work(tmp_path):
    try:
        (tmp_path / "_probe").symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "_probe").unlink()
    return True


def test_a_symlinked_ssot_file_is_refused(tmp_path, monkeypatch):
    """`make agent-adapters` runs on whatever branch is checked out.

    A symlinked SSOT file is a way to pull content from outside the worktree
    into a committed artefact that reviewers read as repo content.
    """
    _fake_ssot(tmp_path, monkeypatch)
    if not _symlinks_work(tmp_path):
        pytest.skip("this filesystem cannot create symlinks")
    outside = tmp_path.parent / "outside.md"
    outside.write_bytes(b"---\nname: evil\n---\n")
    (tmp_path / gaa.SSOT_SKILLS / "alpha" / "extra.md").symlink_to(outside)
    with pytest.raises(gaa.UnsafePath):
        gaa.planned_outputs()


def test_a_symlinked_ssot_directory_is_refused(tmp_path, monkeypatch):
    _fake_ssot(tmp_path, monkeypatch)
    if not _symlinks_work(tmp_path):
        pytest.skip("this filesystem cannot create symlinks")
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (tmp_path / gaa.SSOT_SKILLS / "linked").symlink_to(outside)
    with pytest.raises(gaa.UnsafePath):
        gaa.planned_outputs()


def test_writing_through_a_symlinked_target_is_refused(tmp_path, monkeypatch):
    """`open(..., "wb")` follows a symlink and would overwrite outside the tree."""
    _fake_ssot(tmp_path, monkeypatch)
    if not _symlinks_work(tmp_path):
        pytest.skip("this filesystem cannot create symlinks")
    plan = gaa.planned_outputs()
    target = tmp_path / gaa.OUT_SKILLS / "alpha" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path.parent / "victim.md"
    outside.write_bytes(b"original\n")
    target.symlink_to(outside)
    with pytest.raises(gaa.UnsafePath):
        gaa.write_outputs(plan)
    assert outside.read_bytes() == b"original\n"


def test_a_clean_tree_passes_the_target_check(tmp_path, monkeypatch):
    """The guard must not fire on the ordinary case."""
    _fake_ssot(tmp_path, monkeypatch)
    gaa.write_outputs(gaa.planned_outputs())
    assert gaa.diff_against_disk(gaa.planned_outputs()) == ([], [], [])


def test_main_reports_an_unsafe_path_as_caller_error(tmp_path, monkeypatch, capsys):
    _fake_ssot(tmp_path, monkeypatch)
    if not _symlinks_work(tmp_path):
        pytest.skip("this filesystem cannot create symlinks")
    outside = tmp_path.parent / "outside2.md"
    outside.write_bytes(b"x\n")
    (tmp_path / gaa.SSOT_SKILLS / "alpha" / "extra.md").symlink_to(outside)
    assert gaa.main(["--check"]) == gaa.EXIT_CALLER_ERROR
    assert "refusing" in capsys.readouterr().err
