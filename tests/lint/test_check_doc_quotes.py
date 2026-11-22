"""The declared-quote guard (scripts/tools/lint/check_doc_quotes.py, #1558).

What these pin, one rule each:

* a segment EQUALS a run of whole source lines (not `in`), with ONE
  indentation offset P for the whole block (s == P + d, no tab expansion):
  a fabricated line, a clause cut off, `→` written `->`, a full-width mark
  written half-width, a deleted middle line, a line re-nested under another,
  a tab turned into spaces are all red;
* an elision line is exactly `# ...` / `// ...` / `-- ...` / `; ...` (or
  `…`) once indentation is off: no note after it, no bare `...`; a source
  holding such a line makes an elided quote of it red (ambiguous);
  segments are in order, do not overlap, a mid-block elision skips at least
  one source line; a written leading / trailing elision stands for a line;
* placement is greedy per P and runs in bounded time;
* `anchor` occurs exactly once; the search starts at its line;
* `src` is a repo-relative regular file, not `.md`, no symlink on its path;
* the `.quote` vocabulary is closed and is the inventory's; a fence that
  looks like a quote declaration but renders as none is an error;
* `foo.md` / `foo.en.md` carry the same `.quote` count and `src` multiset;
* the guard runs no subprocess, a missing renderer is rc 2, the live docs'
  declared quotes pass; ci.yml's Lint job names the hook.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO / "scripts" / "tools", REPO / "scripts" / "tools" / "lint", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import _lib_doc_yaml_inventory as inv  # noqa: E402
import check_doc_quotes as guard  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

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


def _match(block: str, start: int = 0, src=SRC):
    return guard.match_quote(block.split("\n"), src, start)


# ── equality with one indentation offset ─────────────────────────────────────

@pytest.mark.parametrize("block", [
    "pack:\n  id: demo",
    "normal_level: 42\nfault_level: 96",                     # lifted to the margin: P = 4 spaces
    "  normal_level: 42\n  fault_level: 96",                 # P = 2 spaces
    "    normal_level: 42   \n    fault_level: 96\t",        # trailing whitespace
    'note: "a → b；c"',
    "- description: \"自測用：磁碟爬升，持續兩小時\"\n  source: seed",
    "time_axis:\n  dropout: \"every:10\"",
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
    ("time_axis:\ndropout: \"every:10\"", "a child moved to its parent's level"),
    ("pack:\n# ...\n  signatures:", "a top-level key re-nested across an elision (B1)"),
    ("      normal_level: 42", "the doc line indented deeper than the source"),
])
def test_any_other_difference_is_red(block, shape):
    assert _match(block) is not None, shape


def test_a_tab_is_not_spaces():
    src = ["all:", "\tgo build", "\tgo test"]
    assert guard.match_quote(["\tgo build", "\tgo test"], src) is None
    assert guard.match_quote(["    go build", "    go test"], src) is not None
    assert guard.match_quote(["all:", "# ...", "go test"], src) is not None   # recipe at column 0


def test_a_blank_line_matches_only_a_blank_line():
    src = ["a: 1", "", "b: 2", "c: 3"]
    assert guard.match_quote(["a: 1", "", "b: 2"], src) is None
    assert guard.match_quote(["a: 1", "   ", "b: 2"], src) is None   # trailing whitespace off
    assert guard.match_quote(["b: 2", "", "c: 3"], src) is not None


# ── elisions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", [
    "# ...", "// ...", "-- ...", "; ...", "# …", "// …", "-- …", "; …",   # the eight, literally
    "    # ...", "# ...   ",
])
def test_a_marked_elision_skips_lines(marker):
    assert _match(f"source: seed\n{marker}\nfault_level: 96") is None


@pytest.mark.parametrize("line", [
    "# ... b: 999", "...", "…", "#...", "# ....", "  ...defaults,", "// ... more", "# …note",
])
def test_anything_else_is_no_elision_and_must_match(line):
    """S1 / S2: a note after the marker is unchecked text; a bare `...` is a
    source line (spread, stub, YAML document end)."""
    assert not guard.is_elision(line)
    assert _match(f"source: seed\n{line}\nfault_level: 96") is not None


def test_a_bare_ellipsis_in_the_source_is_quoted_like_any_line():
    src = ["const cfg = {", "  ...defaults,", "  ...env,", "  port: 9090,", "};"]
    assert guard.match_quote(list(src), src) is None
    assert guard.match_quote(["const cfg = {", "  ...productionOverrides,", "  port: 9090,"],
                             src) is not None


def test_the_markers_are_exactly_eight():
    assert guard.ELISION_MARKERS == {"# ...", "// ...", "-- ...", "; ...",
                                     "# …", "// …", "-- …", "; …"}


def test_a_source_holding_a_marker_line_makes_an_elided_quote_ambiguous():
    src = ["a: 1", "b: 2", "# ...", "c: 3", "d: 4"]
    off, msg = guard.match_quote(["a: 1", "# ...", "d: 4"], src)
    assert "ambiguous elision marker: source line 3" in msg, msg
    assert "this file can only be quoted without elisions" in msg, msg
    assert guard.match_quote(["a: 1", "b: 2"], src) is None   # no elision: nothing ambiguous


@pytest.mark.parametrize("block", [["# ...", "b: 2"], ["b: 2", "# ..."]])
def test_a_lone_leading_or_trailing_elision_is_ambiguous_too(block):
    src = ["a: 1", "b: 2", "// ...", "c: 3"]
    off, msg = guard.match_quote(block, src)
    assert "ambiguous" in msg, msg


def test_a_blank_block_line_does_not_stand_for_a_source_line():
    src = ["a: 1", "x: 9", "b: 2"]
    assert guard.match_quote(["a: 1", "", "b: 2"], src) is not None


def test_a_segment_of_blank_lines_only_is_red():
    off, msg = guard.match_quote(["a: 1", "# ...", "", "# ...", "b: 2"], ["a: 1", "", "", "x", "b: 2"])
    assert "blank lines only" in msg, msg


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
    off, msg = _match("# ...\npack:")
    assert "leading elision" in msg, msg
    assert _match("# ...\n  id: demo") is None


def test_a_written_trailing_elision_stands_for_a_line():
    off, msg = _match('dropout: "every:10"\n# ...')
    assert "trailing elision" in msg, msg
    assert _match("time_axis:\n# ...") is None


@pytest.mark.parametrize("block", ["# ...", "", "# ...\n# ...", "a: 1\n# ...\n# ...\nb: 2",
                                   "a: 1\n# ...\n\n# ...\nb: 2"])
def test_a_block_with_nothing_to_compare_or_two_adjacent_elisions_is_red(block):
    assert _match(block) is not None


def test_the_line_of_two_adjacent_elisions_is_the_first_of_them():
    """N2: the message points at one line, the first elision of the pair."""
    off, msg = guard.match_quote(["a: 1", "# ...", "# ...", "b: 2"], ["a: 1", "x", "b: 2"])
    assert off == 1 and "two elision lines" in msg


def test_placement_takes_the_earliest_place_with_the_same_offset():
    src = ["x: 1", "y: 2", "x: 1", "z: 3", "w: 4"]
    assert guard.match_quote(["x: 1", "# ...", "w: 4"], src) is None
    assert guard.match_quote(["y: 2", "# ...", "z: 3"], src) is None
    assert guard.match_quote(["x: 1", "z: 3"], src) is None


def test_a_later_first_segment_place_with_another_offset_is_tried():
    src = ["a:", "  k: 1", "b:", "  c:", "    k: 1", "    m: 2"]
    # `k: 1` first fits at line 2 with P = 2 spaces, where `m: 2` does not
    # follow; at line 5 P is 4 spaces and it does.
    assert guard.match_quote(["k: 1", "m: 2"], src) is None
    assert guard.match_quote(["k: 1", "m: 3"], src) is not None


def test_placement_runs_in_bounded_time():
    """S3: 200 lines of `}` and a block of five segments that cannot all be
    placed — an unbounded backtracking search takes minutes here."""
    src = ["}"] * 200
    block = ["}", "# ...", "}", "# ...", "}", "# ...", "}", "# ...", "{"]
    result: list = []
    # a daemon thread, so a search that never ends fails this test instead of
    # hanging the run
    worker = threading.Thread(target=lambda: result.append(guard.match_quote(block, src)),
                              daemon=True)
    t0 = time.monotonic()
    worker.start()
    worker.join(timeout=1.0)
    assert result, f"match_quote did not return within 1s ({time.monotonic() - t0:.1f}s)"
    assert result[0] is not None


def test_the_message_names_the_segment_and_the_closest_source_line():
    off, msg = _match("source: seed\n# ...\nfault_level: 9")
    assert off == 2
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


def test_trailing_whitespace_in_the_source_is_off(tmp_path):
    root = _root(tmp_path, {"src.yaml": "a: 1   \nb: 2\t\n"})
    assert not _quote(root, "a: 1\nb: 2").errors


def test_a_final_newline_adds_no_source_line(tmp_path):
    """A trailing elision after the source's last line stands for nothing,
    whether or not the file ends with a newline."""
    root = _root(tmp_path, {"src.yaml": "a: 1\nb: 2\n"})
    assert any("trailing elision" in e for e in _quote(root, "b: 2\n# ...").errors)
    assert not _quote(root, "a: 1\n# ...").errors


@pytest.mark.parametrize("src, needle", [
    ("/etc/passwd", "relative to the repository root"),
    ("a\\b.yaml", "relative to the repository root"),
    ("C:x.yaml", "relative to the repository root"),
    ("../x.yaml", "plain repo path"),
    ("a/./b.yaml", "plain repo path"),
    ("a//b.yaml", "plain repo path"),
    ("missing.yaml", "does not exist"),
    ("docs", "does not exist"),
    ("docs/x.md", "Markdown page"),
    ("docs/x.en.MD", "Markdown page"),
    ("a/b.markdown", "Markdown page"),
    ("a/b.MDX", "Markdown page"),
])
def test_src_must_be_a_regular_non_markdown_file_in_the_repository(tmp_path, src, needle):
    root = _root(tmp_path, {"docs/x.md": "a: 1\n", "docs/x.en.MD": "a: 1\n",
                            "a/b.markdown": "a: 1\n", "a/b.MDX": "a: 1\n"})
    q = _quote(root, "a: 1", src=src)
    assert any(needle in e for e in q.errors), q.errors


@pytest.mark.parametrize("link", ["l.yaml", "d/x.yaml"])
def test_no_symlink_on_the_path(tmp_path, link):
    root = _root(tmp_path, {".build/gen.yaml": "a: 1\n", ".build/x.yaml": "a: 1\n"})
    symlink_or_skip(root / ".build" / "gen.yaml", root / "l.yaml")
    symlink_or_skip(root / ".build", root / "d")
    assert any("is a symlink" in e for e in _quote(root, "a: 1", src=link).errors)


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
    ('{.quote src="a" .expect-refused=x}', "takes no expectation"),
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
        '> ```yaml {.quote src="src/demo.yaml"}\n> time_axis:\n>   dropout: "every:10"\n> ```\n')})
    rc, out = _run(root, capsys, "--report")
    assert rc == 0, out
    assert out.count("PASS") == 3 and "3 declared .quote block(s), 0 error(s)" in out, out


@pytest.mark.parametrize("container", ["    ", "> "])
def test_the_container_prefix_is_off_but_the_lines_own_indentation_is_kept(
        tmp_path, capsys, container):
    """S5: a list item's / blockquote's prefix comes off the file's lines;
    what is left must still equal the source with one offset."""
    bad = f'{container}```yaml {{.quote src="src/demo.yaml"}}\n{container}time_axis:\n' \
          f'{container}dropout: "every:10"\n{container}```\n'
    root = _root(tmp_path, {**SOURCE, "docs/x.md": "- item\n\n" + bad})
    rc, out = _run(root, capsys)
    assert rc == 1 and "'dropout: \"every:10\"' != source line 11" in out, out


def test_a_tab_indented_fence_with_space_indented_lines_and_a_blank_line(tmp_path, capsys):
    """N1: the container prefix is compared with tabs expanded; the rest as written."""
    src = {"src/s.yaml": "a:\n  b: 1\n\n  c: 2\n"}
    doc = '- item\n\n\t```yaml {.quote src="src/s.yaml"}\n    a:\n      b: 1\n\n      c: 2\n\t```\n'
    rc, out = _run(_root(tmp_path, {**src, "docs/x.md": doc}), capsys, "--report")
    assert rc == 0 and "1 declared .quote block(s)" in out, out


def test_a_lone_blockquote_marker_line_is_a_blank_line(tmp_path, capsys):
    src = {"src/s.yaml": "a: 1\n\nb: 2\n"}
    doc = '> ```yaml {.quote src="src/s.yaml"}\n> a: 1\n>\n> b: 2\n> ```\n'
    rc, out = _run(_root(tmp_path, {**src, "docs/x.md": doc}), capsys, "--report")
    assert rc == 0 and "1 declared .quote block(s)" in out, out
    bad = '> ```yaml {.quote src="src/s.yaml"}\n> a: 1\n>\n> c: 2\n> ```\n'
    assert _run(_root(tmp_path, {**src, "docs/x.md": bad}), capsys)[0] == 1


def test_a_tab_in_the_doc_is_compared_as_written(tmp_path, capsys):
    root = _root(tmp_path, {"src/Makefile": "all:\n\tgo build\n",
                            "docs/x.md": '```make {.quote src="src/Makefile"}\nall:\n\tgo build\n```\n'})
    assert _run(root, capsys)[0] == 0


def test_a_drifted_quote_names_the_doc_line_src_and_segment(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        "Text.\n\n"
        '```yaml {.quote src="src/demo.yaml"}\n'
        "source: seed\n# ...\nfault_level: 9\n```\n")})
    rc, out = _run(root, capsys)
    assert rc == 1
    assert "docs/x.md:3: src='src/demo.yaml': (line 6) segment 2" in out, out


def test_an_undeclared_block_is_not_looked_at(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": "```yaml\nfabricated: 1\n```\n"})
    rc, out = _run(root, capsys)
    assert rc == 0 and "0 declared .quote block(s)" in out, out


def test_a_vocabulary_error_on_a_non_yaml_fence_is_reported(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": '```text {src="src/demo.yaml"}\npack:\n```\n'})
    rc, out = _run(root, capsys)
    assert rc == 1 and "is only for .quote" in out, out


def test_a_quote_class_with_a_value_is_read_as_a_quote_fence(tmp_path, capsys):
    """S5: `{.quote=x}` is the vocabulary's error (it reaches parse_declaration),
    not only the look-alike scan's."""
    root = _root(tmp_path, {**SOURCE, "docs/x.md": "```text {.quote=x}\npack:\n```\n"})
    rc, out = _run(root, capsys)
    assert rc == 1 and "unknown attribute '.quote=x'" in out, out


@pytest.mark.parametrize("fence", [
    '```yaml .quote src="src/demo.yaml"',      # no braces: no code block on the site
    '```yaml .Quote',                          # no braces, wrong case
    '```yaml src="src/demo.yaml"',             # no braces, src= alone
    '```text {.Quote src="src/demo.yaml"}',    # wrong case, with src: the vocabulary's error
    '```text {.Quote}',                        # wrong case alone
    '```text {src2="src/demo.yaml"}',          # a src… key without .quote
    '```text {.text anchor="pack:"}',          # anchor without .quote
    '```text {.quote src2="src/demo.yaml"}',   # wrong key: a vocabulary error
])
def test_a_fence_that_looks_like_a_quote_declaration_is_never_skipped(tmp_path, capsys, fence):
    """S4."""
    root = _root(tmp_path, {**SOURCE, "docs/x.md": f"T\n\n{fence}\nfabricated: 1\n```\n\nT\n"})
    rc, out = _run(root, capsys)
    assert rc == 1 and "docs/x.md:3:" in out, out


@pytest.mark.parametrize("fence", [
    '```python title="quote_utils.py"',
    '```yaml title="blockquote example"',
    "```text {.qoute}",                        # not caught: no edit distance is guessed
    '```text bogus title="my.quote file"',     # unrendered: a quoted value is no attribute
])
def test_a_title_or_an_unrelated_class_is_no_look_alike(tmp_path, capsys, fence):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": f"T\n\n{fence}\nplain: 1\n```\n"})
    rc, out = _run(root, capsys)
    assert rc == 0, out


def test_src_inside_an_html_block_is_content(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        '```html\n<script src="a.js"></script>\n<img src="b.png">\n```\n')})
    assert _run(root, capsys)[0] == 0


def test_a_leftover_declaration_in_an_html_comment_is_reported(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        'T\n\n<!--\n```yaml {.quote src="src/demo.yaml"}\nfabricated: 1\n```\n-->\n')})
    rc, out = _run(root, capsys)
    assert rc == 1 and "docs/x.md:4:" in out, out


def test_a_look_alike_inside_a_rendered_code_block_is_content(tmp_path, capsys):
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        '````markdown\n```yaml {.quote src="src/demo.yaml"}\nexample: 1\n```\n````\n')})
    rc, out = _run(root, capsys)
    assert rc == 0, out


def test_a_quote_whose_text_is_also_in_an_example_cannot_be_placed(tmp_path, capsys):
    """S5 / N3: the inventory cannot tell which copy the site renders; that is
    an error of locate_blocks that must reach the report."""
    root = _root(tmp_path, {**SOURCE, "docs/x.md": (
        '````markdown\n```yaml {.quote src="src/demo.yaml"}\npack:\n```\n````\n\n'
        '```yaml {.quote src="src/demo.yaml"}\npack:\n```\n')})
    rc, out = _run(root, capsys)
    assert rc == 1 and "at 2 places" in out, out


def _pair(zh_srcs, en_srcs) -> "dict[str, str]":
    def page(srcs):
        return "".join(f'```yaml {{.quote src="{s}"}}\npack:\n```\n\nText {i}.\n\n'
                       for i, s in enumerate(srcs))
    return {**SOURCE, "src/other.yaml": "pack:\n",
            "docs/p.md": "# P\n\n" + page(zh_srcs), "docs/p.en.md": "# P\n\nMore.\n\n" + page(en_srcs)}


@pytest.mark.parametrize("zh, en, ok, line", [
    (["src/demo.yaml"], ["src/demo.yaml"], True, None),
    ([], [], True, None),
    (["src/demo.yaml"], [], False, 3),
    ([], ["src/demo.yaml"], False, 1),
    (["src/demo.yaml", "src/demo.yaml"], ["src/demo.yaml"], False, 3),
    (["src/demo.yaml"], ["src/other.yaml"], False, 3),
])
def test_a_page_and_its_translation_quote_the_same_sources(tmp_path, capsys, zh, en, ok, line):
    rc, out = _run(_root(tmp_path, _pair(zh, en)), capsys)
    assert (rc == 0) is ok, out
    if not ok:
        # N1: on the Chinese page, at its own first quote (line 1 if none)
        assert f"docs/p.md:{line}: docs/p.md has" in out, out


def test_the_guard_runs_no_subprocess(tmp_path, capsys, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("check_doc_quotes ran a subprocess")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)
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
    assert "FAIL" not in out and out.count("PASS") >= 11, out


def test_ci_lint_job_runs_the_hook():
    from _ci_lint_wiring import assert_ci_lint_job_runs_hook
    assert_ci_lint_job_runs_hook(
        "doc-quote-check", guards="declared quotes vs their source files (#1558)")
