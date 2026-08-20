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

FRONTMATTER = b"---\n"


def provenance(source_rel):
    """The single line that separates an adapter from its source."""
    return (f"<!-- GENERATED from {source_rel} — edit that file, then run "
            f"`make agent-adapters`. Do not edit this copy. -->\n").encode("utf-8")


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
    line = provenance(source_rel)
    if not raw.startswith(FRONTMATTER):
        return line + raw
    end = raw.find(b"\n---\n", len(FRONTMATTER) - 1)
    if end == -1:
        # An opening fence with no closing one: not frontmatter, leave it alone.
        return line + raw
    cut = end + len(b"\n---\n")
    return raw[:cut] + line + raw[cut:]


def iter_ssot(root):
    """(source_rel, dest_rel) for every file under one SSOT dir, sorted."""
    src_abs = os.path.join(REPO_ROOT, root)
    out = []
    for dirpath, _dirs, files in os.walk(src_abs):
        for name in sorted(files):
            abs_path = os.path.join(dirpath, name)
            out.append(os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/"))
    return sorted(out)


def read_frontmatter_field(raw, field):
    """Pull one scalar field out of leading YAML frontmatter, or None.

    A hand-rolled two-key reader rather than a YAML dependency: the index needs
    exactly `name` and `description`, both of which are single-line scalars by
    the skill-file convention, and the pre-commit hook this feeds runs in an
    environment where adding a parser dependency buys nothing.
    """
    if not raw.startswith(FRONTMATTER):
        return None
    end = raw.find(b"\n---\n", len(FRONTMATTER) - 1)
    if end == -1:
        return None
    block = raw[len(FRONTMATTER):end + 1].decode("utf-8", "replace")
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

    removed = write_outputs(plan)
    print(f"✅ Wrote {len(plan)} adapter file(s) from the agents/ SSOT")
    for item in removed:
        print(f"   removed stale adapter: {item}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
