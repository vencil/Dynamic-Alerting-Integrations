"""The declared-quote guard (scripts/tools/lint/check_doc_quotes.py, #1558).

What these pin, one rule each:

* a segment EQUALS a run of whole source lines (not `in`): a fabricated
  line, a clause cut off the end of a line, `→` written `->`, a full-width
  punctuation mark written half-width, a deleted middle line are all red;
  only common indentation and trailing whitespace are normalised;
* an elision line (indentation, optional `#` `//` `--` `;`, then `...` /
  `…`) cuts the block; segments are in order, do not overlap, and a
  mid-block elision skips at least one source line; a leading / trailing
  elision line, when written, stands for a line too; an inline `…` is no
  elision;
* `anchor` occurs exactly once; the search starts at its line;
* `src` is a repo-relative regular file inside the repository and outside
  its ignored directories;
* the `.quote` vocabulary is closed and is the inventory's
  (`parse_declaration`); on a YAML fence it excludes every consumer, and the
  inventory lists the block as an exemption;
* `foo.md` / `foo.en.md` carry the same `.quote` count and `src` multiset;
* the guard runs no subprocess, a missing renderer is rc 2, and the live
  docs' declared quotes pass; ci.yml's Lint job names the hook.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO / "scripts" / "tools", REPO / "scripts" / "tools" / "lint", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import _lib_doc_yaml_inventory as inv  # noqa: E402
import check_doc_quotes as guard  # noqa: E402

SRC = """\
pack:
  id: demo
signatures:
  - description: "自測用：磁碟爬升，持續兩小時"
    source: seed
    normal_level: 42
    fault_level: 96
    note: "a → b；c"
    metric: disk_used
    time_axis:
      dropout: "every:10"
""".split("\n")[:-1]


def _match(block: str, start: int = 0):
    return guard.match_quote(block.split("\n"), SRC, start)


# ── equality, not containment ────────────────────────────────────────────────

@pytest.mark.parametrize("block", [
    "pack:\n  id: demo",
    # the doc lifts a nested run to the left margin: common indentation is off
    "normal_level: 42\nfault_level: 96",
    "    normal_level: 42   \n    fault_level: 96\t",     # trailing whitespace
    'note: "a → b；c"',
])
def test_a_run_of_whole_source_lines_matches(block):
    assert _match(block) is None


@pytest.mark.parametrize("block, shape", [
    ("normal_level: 42\nfabricated: 1\nfault_level: 96", "a fabricated line"),
    ("normal_level: 42\nfault_level: 9", "a suffix dropped"),
    ("rmal_level: 42\nfault_level: 96", "a prefix dropped"),
    ('note: "a -> b；c"', "→ written ->"),
    ('note: "a → b;c"', "full-width ； written ;"),
    ('description: "自測用：磁碟爬升,持續兩小時"', "full-width ， written ,"),
    ("source: seed\nfault_level: 96", "a middle line deleted, elision not marked"),
    ("normal_level:  42", "whitespace inside a line squeezed / widened"),
    ("normal_level: 42\n  fault_level: 96", "relative indentation changed"),
])
def test_any_other_difference_is_red(block, shape):
    assert _match(block) is not None, shape


# ── elisions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", [
    "# ...", "    # ... the rest likewise", "...", "…", "// ...", "-- …", "; ...", "#...",
])
def test_a_marked_elision_skips_lines(marker):
    assert _match(f"source: seed\n{marker}\nfault_level: 96") is None


def test_a_mid_elision_must_skip_at_least_one_line():
    assert _match("source: seed\n# ...\nnormal_level: 42") is not None


def test_segments_are_in_order():
    assert _match("fault_level: 96\n# ...\nsource: seed") is not None


def test_segments_do_not_overlap():
    assert _match("source: seed\nnormal_level: 42\n# ...\nnormal_level: 42") is not None


def test_an_inline_elision_is_no_elision():
    assert _match("source: seed … fault_level: 96") is not None


def test_leading_and_trailing_elisions_are_implicit():
    assert _match("normal_level: 42") is None


def test_a_written_leading_elision_stands_for_a_line():
    assert _match("# ...\npack:") is not None
    assert _match("# ...\n  id: demo") is None


def test_a_written_trailing_elision_stands_for_a_line():
    assert _match('dropout: "every:10"\n# ...') is not None
    assert _match("time_axis:\n# ...") is None


@pytest.mark.parametrize("block", ["# ...", "", "# ...\n# ...", "a: 1\n# ...\n# ...\nb: 2"])
def test_a_block_with_nothing_to_compare_or_two_adjacent_elisions_is_red(block):
    assert _match(block) is not None


def test_placement_backtracks_past_an_early_match():
    src = ["x: 1", "y: 2", "x: 1", "z: 3", "w: 4"]
    # `x: 1` first fits at line 1, but `w: 4` then needs the elision to skip
    # line 4 — it does, so either placement works; and `z: 3` only fits after
    # the SECOND `x: 1` when the elision must skip one line.
    assert guard.match_quote(["x: 1", "# ...", "w: 4"], src) is None
    assert guard.match_quote(["y: 2", "# ...", "z: 3"], src) is None
    assert guard.match_quote(["x: 1", "z: 3"], src) is None


def test_the_message_names_the_segment_and_the_closest_source_line():
    idx, off, msg = _match("source: seed\n# ...\nfault_level: 9")
    assert (idx, off) == (1, 2)
    assert "segment 2" in msg and "source line 7" in msg and "'fault_level: 96'" in msg, msg


# ── anchor, src ─────────────────────────────────────────────────────────────

def _root(tmp_path: Path, files: "dict[str, str]") -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _quote(root: Path, body: str, src: str = "src.yaml", anchor=None) -> guard.Quote:
    q = guard.Quote("docs/x.md", 1, body.split("\n"), src=src, anchor=anchor)
    guard.judge(root, q)
    return q


@pytest.mark.parametrize("anchor, ok, needle", [
    ("time_axis:", True, ""),
    ("normal", True, ""),
    ("missing", False, "occurs 0 time(s)"),
    ("level:", False, "occurs 2 time(s)"),
])
def test_the_anchor_occurs_exactly_once(tmp_path, anchor, ok, needle):
    root = _root(tmp_path, {"src.yaml": "\n".join(SRC) + "\n"})
    q = _quote(root, "fault_level: 96" if anchor != "time_axis:" else "time_axis:", anchor=anchor)
    assert (not q.errors) is ok, q.errors
    assert needle in " ".join(q.errors)


def test_the_search_starts_at_the_anchor_line(tmp_path):
    root = _root(tmp_path, {"src.yaml": "\n".join(SRC) + "\n"})
    assert _quote(root, "source: seed", anchor="metric:").errors


@pytest.mark.parametrize("src, needle", [
    ("/etc/passwd", "relative to the repository root"),
    ("a\\b.yaml", "relative to the repository root"),
    ("../x.yaml", "plain repo path"),
    ("a/./b.yaml", "plain repo path"),
    ("missing.yaml", "does not exist"),
    ("docs", "does not exist"),
    (".build/x.yaml", "repository ignores"),
    ("tools/node_modules/x.yaml", "repository ignores"),
])
def test_src_must_be_a_tracked_looking_file_in_the_repository(tmp_path, src, needle):
    root = _root(tmp_path, {"docs/x.md": "", ".build/x.yaml": "a: 1\n",
                            "tools/node_modules/x.yaml": "a: 1\n"})
    q = _quote(root, "a: 1", src=src)
    assert any(needle in e for e in q.errors), q.errors


def test_src_may_not_resolve_outside_the_repository(tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("a: 1\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "link.yaml").symlink_to(outside)
    assert any("outside the repository" in e for e in _quote(root, "a: 1", src="link.yaml").errors)


def test_a_source_that_is_not_utf8_is_red(tmp_path):
    (tmp_path / "bin.yaml").write_bytes(b"\xff\xfe a: 1\n")
    assert any("not UTF-8" in e for e in _quote(tmp_path, "a: 1", src="bin.yaml").errors)


# ── vocabulary (the inventory's) ────────────────────────────────────────────

_RENDERER: "list[inv.Renderer]" = []


def _declaration(info: str, lang: str = "yaml") -> "inv.Declaration":
    if not _RENDERER:
        _RENDERER.append(inv.Renderer(REPO))
    block, = _RENDERER[0].code_blocks(f"```{lang} {info}\na: 1\n```\n")
    return inv.parse_declaration(block, info)


@pytest.mark.parametrize("info, quote", [
    ('{.quote src="a/b.yaml"}', ("a/b.yaml", None)),
    ('{.quote src="a/b.yaml" anchor="k:"}', ("a/b.yaml", "k:")),
    ('{ .quote anchor="k:" src="a/b.yaml" }', ("a/b.yaml", "k:")),
])
def test_every_quote_form_is_read(info, quote):
    d = _declaration(info)
    assert d.errors == [] and d.quote == quote and d.consumer is None, d


@pytest.mark.parametrize("info, needle", [
    ("{.quote}", "needs a non-empty src"),
    ('{.quote src=""}', "needs a non-empty src"),
    ('{.quote src="a" anchor=""}', "anchor=\"\" is empty"),
    ('{.quote src="a" .confd}', "takes no other attribute"),
    ('{.quote src="a" .illustrative reason="r"}', "takes no other attribute"),
    ('{.quote src="a" reason="r"}', "takes no other attribute"),
    ('{.quote src="a" .expect-invalid}', "takes no expectation"),
    ('{.quote src="a" owner="me"}', "unknown attribute owner="),
    ('{.quote=x src="a"}', "unknown attribute '.quote=x'"),
    ('{src="a"}', "is only for .quote"),
    ('{.confd anchor="k"}', "is only for .quote"),
])
def test_anything_else_is_an_error(info, needle):
    d = _declaration(info)
    assert any(needle in e for e in d.errors), d.errors


def test_the_inventory_lists_a_yaml_quote_as_an_exemption_and_reads_nothing(tmp_path):
    root = _root(tmp_path, {"docs/x.md": '```yaml {.quote src="s.yaml"}\na: [1\n```\n'})
    u, = inv.build(root).units
    assert (u.errors, u.read, u.consumer) == ([], False, None)
    assert u.exemption and "s.yaml" in u.exemption


# ── end to end ──────────────────────────────────────────────────────────────

def _run(root: Path, capsys, *extra: str) -> "tuple[int, str]":
    rc = guard.main(["--repo-root", str(root), *extra])
    return rc, capsys.readouterr().out


SOURCE = {"src/demo.yaml": "\n".join(SRC) + "\n"}


def test_a_matching_quote_in_any_language_and_container_passes(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        "Intro.\n\n"
        '```text {.quote src="src/demo.yaml" anchor="source:"}\n'
        "source: seed\n# ...\nfault_level: 96\n```\n\n"
        "- item\n\n"
        '    ```yaml {.quote src="src/demo.yaml"}\n    pack:\n      id: demo\n    ```\n\n'
        '> ```yaml {.quote src="src/demo.yaml"}\n> metric: disk_used\n> ```\n')})
    rc, out = _run(root, capsys, "--report")
    assert rc == 0, out
    assert out.count("PASS") == 3 and "3 declared .quote block(s), 0 error(s)" in out, out


def test_a_drifted_quote_names_the_doc_line_src_and_segment(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        "Text.\n\n"
        '```yaml {.quote src="src/demo.yaml"}\n'
        "source: seed\n# ...\nfault_level: 9\n```\n")})
    rc, out = _run(root, capsys)
    assert rc == 1
    assert "docs/x.md:3: src='src/demo.yaml': (block line 6) segment 2" in out, out


def test_an_undeclared_block_is_not_looked_at(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": "```yaml\nfabricated: 1\n```\n"})
    rc, out = _run(root, capsys)
    assert rc == 0 and "0 declared .quote block(s)" in out, out


def test_a_vocabulary_error_on_a_non_yaml_fence_is_reported(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": '```text {src="src/demo.yaml"}\npack:\n```\n'})
    rc, out = _run(root, capsys)
    assert rc == 1 and "is only for .quote" in out, out


def _pair(zh_srcs, en_srcs) -> "dict[str, str]":
    def page(srcs):
        return "".join(f'```yaml {{.quote src="{s}"}}\npack:\n```\n\nText {i}.\n\n'
                       for i, s in enumerate(srcs))
    return {**SOURCE, "src/other.yaml": "pack:\n",
            "docs/p.md": page(zh_srcs), "docs/p.en.md": page(en_srcs)}


@pytest.mark.parametrize("zh, en, ok", [
    (["src/demo.yaml"], ["src/demo.yaml"], True),
    ([], [], True),
    (["src/demo.yaml"], [], False),
    (["src/demo.yaml", "src/demo.yaml"], ["src/demo.yaml"], False),
    (["src/demo.yaml"], ["src/other.yaml"], False),
])
def test_a_page_and_its_translation_quote_the_same_sources(tmp_path, capsys, zh, en, ok):
    rc, out = _run(_root(tmp_path, _pair(zh, en)), capsys)
    assert (rc == 0) is ok, out
    if not ok:
        assert "docs/p.md and docs/p.en.md" in out or "docs/p.en.md has" in out, out


def test_the_guard_runs_no_subprocess(tmp_path, capsys, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("check_doc_quotes ran a subprocess")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    root = _root(tmp_path, {**SOURCE, "docs/x.md": '```yaml {.quote src="src/demo.yaml"}\npack:\n```\n'})
    assert _run(root, capsys)[0] == 0


def test_a_missing_renderer_is_rc_2_not_a_skip(tmp_path, capsys, monkeypatch):
    def missing(*a, **k):
        raise inv.RendererError("the site's renderer is not installed (No module named 'markdown')")
    monkeypatch.setattr(inv, "Renderer", missing)
    root = _root(tmp_path, {"docs/x.md": "x\n"})
    assert guard.main(["--repo-root", str(root)]) == 2
    assert "not installed" in capsys.readouterr().err


def test_the_live_docs_declared_quotes_match(capsys):
    rc, out = _run(REPO, capsys, "--report")
    assert rc == 0, out
    assert "FAIL" not in out and out.count("PASS") >= 5, out


def test_ci_lint_job_runs_the_hook():
    from _ci_lint_wiring import assert_ci_lint_job_runs_hook
    assert_ci_lint_job_runs_hook(
        "doc-quote-check", guards="declared quotes vs their source files (#1558)")
