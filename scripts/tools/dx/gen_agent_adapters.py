#!/usr/bin/env python3
"""Generate the per-vendor agent adapters from the neutral agents/ SSOT (TRK-361).

    python3 scripts/tools/dx/gen_agent_adapters.py --generate
    python3 scripts/tools/dx/gen_agent_adapters.py --check     # drift gate

WHAT THIS IS FOR
================
The project's agent instructions used to live only under `.claude/`, which made
them unusable by any other coding agent. They now live in a vendor-neutral tree:

    agents/skills/<name>/SKILL.md   workflow skills (+ references/**)
    agents/roles/<name>.md          subagent role prompts

and this script projects that tree into whatever each vendor actually reads:

    .claude/skills/<name>/**        Claude Code only discovers skills HERE
    .claude/agents/<name>.md        Claude Code only discovers subagents HERE
    AGENTS.md                       the Linux Foundation AAIF neutral standard,
                                    read natively by Codex, Cursor, Copilot,
                                    Gemini CLI and Grok

WHY COPIES AND NOT SYMLINKS
===========================
Symlinks are not an option here, and that is measured rather than assumed: this
repo's own test suite records three tests erroring on a Windows host because tar
could not create a symlink (PR #1457), and the Windows escape hatch
(`make win-commit`) is a supported path. Copies plus a drift gate is the same
shape the repo already uses for every other generated artifact (tool-map,
verify_diff_map, README counts).

The cost is honest: every skill edit touches two files, and ~40KB of skill text
exists twice in git. The gate below is what keeps the second copy from becoming
a second source of truth.

WHY THE GENERATOR IS DELIBERATELY DUMB
======================================
An adapter is the SSOT file byte-for-byte, plus ONE inserted provenance line.
No reformatting, no frontmatter rewriting, no template expansion. Two reasons:

  * a transforming generator is itself a defect surface, and it would be the
    only unreviewed thing standing between the SSOT and what the agent reads;
  * `--check` can then be an exact byte comparison. A generator that reflows
    text needs a fuzzy comparison, and a fuzzy drift gate is one that some real
    drift slips through.

Relative links survive the projection for free: `agents/skills/<n>/SKILL.md` and
`.claude/skills/<n>/SKILL.md` sit at the same depth, so every `../../../docs/...`
in the corpus resolves identically from both. That was verified over all 29 such
links before the move, and `tests/dx/test_gen_agent_adapters.py` pins it.

EXIT CODES (scripts/tools/_lib_exitcodes.py)
============================================
  0  --generate wrote successfully, or --check found no drift
  1  --check found drift (adapters stale, extra, or missing) -- run --generate
  2  cannot do the job: SSOT tree missing, unreadable, or malformed
"""
from __future__ import annotations

import argparse
import os
import stat
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR,
    EXIT_OK,
    EXIT_VIOLATION,
)

REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", ".."))

SSOT_SKILLS = "agents/skills"
SSOT_ROLES = "agents/roles"

OUT_SKILLS = ".claude/skills"
OUT_ROLES = ".claude/agents"
OUT_ENTRY = "AGENTS.md"

# AGENTS.md is BOTH source and output: its prose is hand-written and its skill
# index is machine-maintained between these two markers. It is not a projected
# copy like the files under .claude/, and deliberately so -- it must sit at the
# repo root for the vendors that read it, and a source file one directory down
# would need every `docs/...` link rewritten on projection. That rewrite is
# exactly the kind of clever generator this design refuses: measured, an
# `agents/AGENTS.base.md` carrying root-relative links reported 9 broken links
# the moment the corpus was brought under the doc-link gate. Same depth, no
# rewrite, no magic.
INDEX_BEGIN = "<!-- BEGIN GENERATED SKILL INDEX -->"
INDEX_END = "<!-- END GENERATED SKILL INDEX -->"

FRONTMATTER_FENCE = b"---"

# Provenance has to be a comment in the ADAPTER's own language, not markdown's.
# `<!-- ... -->` inside a .js file only parses because Annex B keeps legacy
# HTML-comment syntax alive in sloppy scripts; it is a hard parse error the
# moment anything reads the file as an ES module. One comment style per suffix,
# and an unknown suffix gets no line at all rather than a guessed one -- the
# drift gate still owns those files, so a missing marker costs nothing while a
# wrong marker corrupts the artefact.
COMMENT_STYLES = {
    ".md": ("<!-- ", " -->"),
    ".markdown": ("<!-- ", " -->"),
    ".js": ("// ", ""),
    ".mjs": ("// ", ""),
    ".cjs": ("// ", ""),
    ".ts": ("// ", ""),
    ".py": ("# ", ""),
    ".sh": ("# ", ""),
    ".yaml": ("# ", ""),
    ".yml": ("# ", ""),
}


def provenance(source_rel, eol=b"\n"):
    """The single line that separates an adapter from its source, or b"" .

    Returns empty bytes for a suffix with no known comment syntax: silence is
    recoverable, a syntactically invalid marker is not.
    """
    style = COMMENT_STYLES.get(os.path.splitext(source_rel)[1].lower())
    if style is None:
        return b""
    open_tag, close_tag = style
    text = (f"{open_tag}此檔為產生物，來源 {source_rel} —— 請改那份 SSOT，"
            f"再跑 `make agent-adapters`；不要直接編輯這份複本。{close_tag}")
    return text.encode("utf-8") + eol


def project(raw, source_rel):
    """SSOT bytes -> adapter bytes: identical, plus one provenance line.

    Inserted AFTER the YAML frontmatter when there is one, because frontmatter
    must start at byte 0 or the vendor stops recognising the file. Files without
    frontmatter (references/**) take the line at the top.

    Operates on bytes end to end. Decoding to str would let Python's universal
    newlines rewrite CRLF into LF and silently reflow a file that a Windows host
    committed -- the failure mode #1363 shipped ("only changed one number, but
    touched every line").
    """
    span = frontmatter_span(raw)
    if span is None:
        line = provenance(source_rel, source_eol(raw))
        return line + raw
    _body_start, cut, eol = span
    return raw[:cut] + provenance(source_rel, eol) + raw[cut:]


def source_eol(raw):
    """The line ending this file already uses -- CRLF only if the first one is.

    A Windows host writes CRLF, and appending an LF-terminated line into a CRLF
    file leaves one odd line that every subsequent diff carries. Cheap to match,
    and it keeps the adapter byte-comparable with what a regeneration on either
    host produces.
    """
    first_lf = raw.find(b"\n")
    if first_lf > 0 and raw[first_lf - 1:first_lf] == b"\r":
        return b"\r\n"
    return b"\n"


def frontmatter_span(raw):
    """(body_start, insert_at, eol) for leading YAML frontmatter, else None.

    Recognises both `---\n` and `---\r\n` fences. Getting this wrong is not a
    cosmetic miss: an unrecognised CRLF fence sends the provenance line to byte
    0, which pushes the frontmatter off the top of the file and makes every
    vendor stop seeing the skill's name and description at all.
    """
    eol = source_eol(raw)
    opening = FRONTMATTER_FENCE + eol
    if not raw.startswith(opening):
        return None
    closing = eol + FRONTMATTER_FENCE + eol
    end = raw.find(closing, len(opening) - len(eol))
    if end == -1:
        # An opening fence with no closing one: not frontmatter, leave it alone.
        return None
    return len(opening), end + len(closing), eol


def source_of(dest_rel):
    """Inverse of the projection: adapter path -> the SSOT path it came from.

    One table drives both directions, so a new adapter root cannot be added to
    the forward map and forgotten in the reverse one.
    """
    for ssot_root, out_root in ((SSOT_SKILLS, OUT_SKILLS), (SSOT_ROLES, OUT_ROLES)):
        if dest_rel == out_root or dest_rel.startswith(out_root + "/"):
            return ssot_root + dest_rel[len(out_root):]
    raise KeyError(f"{dest_rel} is not a projected adapter path")


class UnsafePath(Exception):
    """An SSOT source or adapter target that this generator refuses to touch."""


def iter_ssot(root):
    """Every file under one SSOT dir, repo-relative and sorted.

    Symlinks are refused rather than followed. `make agent-adapters` gets run
    on whatever branch happens to be checked out, so a symlinked SSOT file is a
    way to pull content from outside the worktree into a committed artefact
    that reviewers read as repo content. Refusing is safe because the SSOT has
    no legitimate symlink today, and this repo cannot rely on symlinks anyway
    (they do not survive a Windows host -- PR #1457).
    """
    src_abs = os.path.join(REPO_ROOT, root)
    out = []
    for dirpath, dirs, files in os.walk(src_abs):
        for name in sorted(dirs):
            if os.path.islink(os.path.join(dirpath, name)):
                raise UnsafePath(
                    f"{os.path.relpath(os.path.join(dirpath, name), REPO_ROOT)} "
                    "is a symlinked directory; the SSOT must be real files")
        for name in sorted(files):
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path):
                raise UnsafePath(
                    f"{os.path.relpath(abs_path, REPO_ROOT)} is a symlink; "
                    "the SSOT must be real files")
            out.append(os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/"))
    return sorted(out)


def assert_writable_target(dest_rel):
    """Refuse an adapter path that escapes REPO_ROOT or is an existing symlink.

    Two distinct escapes, both cheap to close: a `..` segment in the projected
    path, and an existing symlink at the destination that `open(..., "wb")`
    would happily follow out of the tree.
    """
    abs_path = os.path.join(REPO_ROOT, dest_rel)
    resolved_root = os.path.realpath(REPO_ROOT)
    if os.path.commonpath([os.path.realpath(os.path.dirname(abs_path)),
                           resolved_root]) != resolved_root:
        raise UnsafePath(f"{dest_rel} resolves outside the repository")
    if os.path.islink(abs_path):
        raise UnsafePath(f"{dest_rel} is a symlink; refusing to write through it")
    return abs_path


def read_frontmatter_field(raw, field):
    """Pull one scalar field out of leading YAML frontmatter, or None.

    A hand-rolled two-key reader rather than a YAML dependency: the index needs
    exactly `name` and `description`, both of which are single-line scalars by
    the skill-file convention, and the pre-commit hook this feeds runs in an
    environment where adding a parser dependency buys nothing.
    """
    span = frontmatter_span(raw)
    if span is None:
        return None
    body_start, insert_at, eol = span
    block = raw[body_start:insert_at - len(eol) - len(FRONTMATTER_FENCE)].decode(
        "utf-8", "replace")
    prefix = field + ":"
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def planned_outputs():
    """Every adapter file this generator owns -> its bytes.

    Raises FileNotFoundError when the SSOT tree is absent (caller error).
    """
    plan = {}
    for root, out_root in ((SSOT_SKILLS, OUT_SKILLS), (SSOT_ROLES, OUT_ROLES)):
        src_abs = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(src_abs):
            raise FileNotFoundError(root)
        for source_rel in iter_ssot(root):
            dest_rel = out_root + source_rel[len(root):]
            with open(os.path.join(REPO_ROOT, source_rel), "rb") as fh:
                plan[dest_rel] = project(fh.read(), source_rel)
    plan[OUT_ENTRY] = build_entry()
    return plan


def skill_index_rows():
    """One markdown row per skill, derived from the SSOT frontmatter."""
    rows = []
    src_abs = os.path.join(REPO_ROOT, SSOT_SKILLS)
    for name in sorted(os.listdir(src_abs)):
        skill_md = os.path.join(src_abs, name, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        with open(skill_md, "rb") as fh:
            raw = fh.read()
        desc = read_frontmatter_field(raw, "description") or ""
        rows.append(f"| [`{name}`]({SSOT_SKILLS}/{name}/SKILL.md) | {desc} |")
    return rows


def build_entry():
    """AGENTS.md's own prose with the skill index refreshed between the markers.

    Reads the file it also writes. Editing the prose is the supported workflow;
    only the marked block is machine-owned, so `--check` fails exactly when a
    skill was added, removed, or re-described without refreshing the index.
    """
    base_abs = os.path.join(REPO_ROOT, OUT_ENTRY)
    if not os.path.isfile(base_abs):
        raise FileNotFoundError(OUT_ENTRY)
    with open(base_abs, "rb") as fh:
        text = fh.read().decode("utf-8")
    if INDEX_BEGIN not in text or INDEX_END not in text:
        raise ValueError(
            f"{OUT_ENTRY} must contain both {INDEX_BEGIN} and {INDEX_END}")
    head, rest = text.split(INDEX_BEGIN, 1)
    _stale, tail = rest.split(INDEX_END, 1)
    block = "\n".join([
        INDEX_BEGIN,
        "",
        "| Skill | When it applies |",
        "|---|---|",
        *skill_index_rows(),
        "",
        INDEX_END,
    ])
    return (head + block + tail).encode("utf-8")


def existing_outputs():
    """Adapter files currently on disk, so stale ones can be reported/removed."""
    found = set()
    for out_root in (OUT_SKILLS, OUT_ROLES):
        abs_root = os.path.join(REPO_ROOT, out_root)
        for dirpath, _dirs, files in os.walk(abs_root):
            for name in files:
                abs_path = os.path.join(dirpath, name)
                found.add(os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/"))
    if os.path.isfile(os.path.join(REPO_ROOT, OUT_ENTRY)):
        found.add(OUT_ENTRY)
    return found


def diff_against_disk(plan):
    """(missing, stale, extra) — the three ways an adapter tree can drift."""
    on_disk = existing_outputs()
    missing, stale = [], []
    for dest_rel, want in sorted(plan.items()):
        abs_path = os.path.join(REPO_ROOT, dest_rel)
        if not os.path.isfile(abs_path):
            missing.append(dest_rel)
            continue
        with open(abs_path, "rb") as fh:
            if fh.read() != want:
                stale.append(dest_rel)
    extra = sorted(on_disk - set(plan))
    return missing, stale, extra


def write_outputs(plan):
    for dest_rel, data in sorted(plan.items()):
        abs_path = os.path.join(REPO_ROOT, dest_rel)
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        abs_path = assert_writable_target(dest_rel)
        with open(abs_path, "wb") as fh:
            fh.write(data)
        # Generated artefacts ship 0644, the same mode every sibling generator
        # sets (dev-rules §5 SAST; gated by tests/shared/test_sast.py). Without
        # it the adapter inherits whatever umask the regenerating host had, and
        # the mode difference shows up as repo churn on the next machine.
        os.chmod(abs_path,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    removed = []
    for dest_rel in sorted(existing_outputs() - set(plan)):
        os.remove(os.path.join(REPO_ROOT, dest_rel))
        removed.append(dest_rel)
    # An emptied skill dir left behind would still be discovered by the vendor.
    for out_root in (OUT_SKILLS, OUT_ROLES):
        for dirpath, dirs, files in os.walk(os.path.join(REPO_ROOT, out_root),
                                            topdown=False):
            if not files and not dirs and dirpath != os.path.join(REPO_ROOT, out_root):
                os.rmdir(dirpath)
    return removed


def build_parser():
    parser = argparse.ArgumentParser(
        description=("Project the neutral agents/ SSOT into the per-vendor "
                     "adapter files (.claude/**), and refresh the AGENTS.md "
                     "skill index."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", action="store_true",
                      help="write the adapters and delete stale ones")
    mode.add_argument("--check", action="store_true",
                      help="report drift without writing (exit 1 on drift)")
    return parser


def main(argv=None):
    try_utf8_stdout()
    args = build_parser().parse_args(argv)

    try:
        plan = planned_outputs()
    except FileNotFoundError as exc:
        print(f"ERROR: SSOT path missing: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except UnsafePath as exc:
        print(f"ERROR: refusing to run — {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except (ValueError, OSError) as exc:
        print(f"ERROR: cannot build the adapter plan: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.check:
        missing, stale, extra = diff_against_disk(plan)
        if not (missing or stale or extra):
            print(f"✅ agent adapters in sync ({len(plan)} files from "
                  f"{SSOT_SKILLS}/, {SSOT_ROLES}/, and the {OUT_ENTRY} index)")
            return EXIT_OK
        for label, items in (("missing", missing), ("stale", stale),
                             ("extra (no SSOT source)", extra)):
            for item in items:
                print(f"  {label}: {item}", file=sys.stderr)
        print("❌ agent adapters drifted — run `make agent-adapters` "
              "(edit the agents/ SSOT, never the adapter)", file=sys.stderr)
        return EXIT_VIOLATION

    try:
        removed = write_outputs(plan)
    except UnsafePath as exc:
        print(f"ERROR: refusing to write — {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    print(f"✅ Wrote {len(plan)} adapter file(s) from the agents/ SSOT")
    for item in removed:
        print(f"   removed stale adapter: {item}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
