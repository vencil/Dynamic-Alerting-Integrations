#!/usr/bin/env python3
"""PreToolUse hook (matcher `Edit|Write|MultiEdit|Bash`): path-triggered guidance.

Why
---
Nested `CLAUDE.md` files only fire when a file under them is *Read*; a Bash
command that edits the same file triggers nothing (TRK-377 measured it). This
hook closes that gap for Claude Code and, more importantly, reads the same
`.agents/paths-map.json` that `gen_agent_adapters.py` projects into Cursor's
`paths:` frontmatter and Copilot's `applyTo` — one map, three vendors.

What it does
------------
1. Collect candidate paths from the payload: `tool_input.file_path` for the
   editing tools, and every token of a Bash command that exists on disk
   (relative to the payload's `cwd`, or absolute). Existence is the filter
   that keeps `origin/main` and `--tb=short` out; a path that a command is
   about to *create* is therefore invisible here (Write covers the common
   case).
2. Make each candidate repo-relative. The repo root of a path is the nearest
   ancestor holding `.git` (a directory for the main checkout, a file for a
   worktree), so an edit inside `vibe-wt/impl-x/helm/...` matches `helm/**`
   exactly like one inside the main checkout does.
3. Match against every entry's globs; each entry is injected **once per
   session** as `additionalContext` (marker in the harness scratchpad dir),
   so the map costs tokens once, not on every tool call.

Failure policy: never blocks. An unreadable map is reported once per session
through `additionalContext` (fail-loud, like run-hooks.sh does for a missing
interpreter) — a silent skip here is the #824 failure shape.

Manual use
----------
    bash scripts/session-guards/run-hooks.sh paths_map.py --match helm/values.yaml CHANGELOG.md
    bash scripts/session-guards/run-hooks.sh paths_map.py --validate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as gl  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
try:
    from _lib_compat import try_utf8_stdout
except Exception:  # pragma: no cover — standalone fallback, never block
    def try_utf8_stdout() -> None:  # type: ignore
        return None

TAG = "paths-map"
MAP_REL = ".agents/paths-map.json"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
COMMAND_TOOLS = ("Bash",)
LOAD_ERROR_ID = "__load_error__"


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

class MapError(ValueError):
    """The map file is unreadable or violates its schema."""


def validate_map(data: object) -> list[dict]:
    """Return the entry list, or raise MapError naming the first violation."""
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise MapError("top level must be an object with an `entries` list")
    seen: set[str] = set()
    entries: list[dict] = []
    for i, e in enumerate(data["entries"]):
        where = f"entries[{i}]"
        if not isinstance(e, dict):
            raise MapError(f"{where}: must be an object")
        eid = e.get("id")
        if not isinstance(eid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", eid):
            raise MapError(f"{where}: `id` must be a lowercase-hyphen slug")
        if eid in seen:
            raise MapError(f"{where}: duplicate id {eid!r}")
        seen.add(eid)
        paths = e.get("paths")
        if not isinstance(paths, list) or not paths or not all(
                isinstance(p, str) and p and not p.startswith("/") for p in paths):
            raise MapError(f"{where} ({eid}): `paths` must be a non-empty list of relative globs")
        reads = e.get("read")
        if not isinstance(reads, list) or not all(
                isinstance(r, dict) and isinstance(r.get("file"), str) and r["file"]
                and (r.get("section") is None or isinstance(r["section"], str))
                for r in reads):
            raise MapError(f"{where} ({eid}): `read` must be a list of {{file, section?}}")
        note = e.get("note")
        if not isinstance(note, str) or not note.strip():
            raise MapError(f"{where} ({eid}): `note` must be a non-empty string")
        entries.append(e)
    return entries


def load_map(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise MapError(f"cannot read {path}: {exc}") from exc
    return validate_map(data)


# ---------------------------------------------------------------------------
# Glob matching (repo-relative POSIX paths)
# ---------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    """`**` spans directories, `*` / `?` stay inside one segment.

    `a/**/b` also matches `a/b` (zero directories), the gitignore / Cursor /
    Copilot reading of `**`, so the three projections agree on what a glob
    covers.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(rel_path: str, patterns: list[str]) -> str | None:
    """The first pattern that matches, or None."""
    for pat in patterns:
        if glob_to_regex(pat).match(rel_path):
            return pat
    return None


# ---------------------------------------------------------------------------
# Candidate paths
# ---------------------------------------------------------------------------

def _norm(p: str) -> str:
    return p.replace("\\", "/")


def repo_root_of(abs_path: Path) -> Path | None:
    """Nearest ancestor that holds `.git` (dir or worktree file), else None."""
    start = abs_path if abs_path.is_dir() else abs_path.parent
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    return None


def to_repo_relative(token: str, cwd: str | None) -> str | None:
    """Repo-relative POSIX path for an existing file/dir token, else None."""
    raw = _norm(token.strip().strip("'\""))
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        if not cwd:
            return None
        p = Path(cwd) / raw
    try:
        if not p.exists():
            return None
        p = p.resolve()
    except OSError:
        return None
    root = repo_root_of(p)
    if root is None:
        return None
    try:
        return _norm(str(p.relative_to(root)))
    except ValueError:
        return None


def bash_tokens(command: str) -> list[str]:
    """Tokens that could be paths: shell words, plus the value side of `k=v`."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    out: list[str] = []
    for w in words:
        out.append(w)
        if "=" in w:
            out.append(w.split("=", 1)[1])
    return out


def candidate_paths(payload: dict) -> list[str]:
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    cwd = payload.get("cwd") or None
    cands: list[str] = []
    if tool in EDIT_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if isinstance(fp, str) and fp:
            rel = to_repo_relative(fp, cwd)
            if rel is None:
                # A file about to be created: fall back to the path relative
                # to cwd's repo, without requiring existence.
                rel = _relative_without_existence(fp, cwd)
            if rel:
                cands.append(rel)
    elif tool in COMMAND_TOOLS:
        cmd = tool_input.get("command") or ""
        if isinstance(cmd, str):
            for tok in bash_tokens(cmd):
                rel = to_repo_relative(tok, cwd)
                if rel:
                    cands.append(rel)
    # de-dup, keep order
    seen: set[str] = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def _relative_without_existence(fp: str, cwd: str | None) -> str | None:
    p = Path(_norm(fp))
    if not p.is_absolute():
        if not cwd:
            return _norm(str(p))
        p = Path(cwd) / p
    root = repo_root_of(p.parent) if p.parent.exists() else None
    if root is None:
        return None
    try:
        return _norm(str(p.resolve().relative_to(root.resolve())))
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Rendering + once-per-session
# ---------------------------------------------------------------------------

def render_hit(entry: dict, rel_path: str, pattern: str) -> str:
    reads = "；".join(
        f"{r['file']} §{r['section']}" if r.get("section") else r["file"]
        for r in entry.get("read") or []
    )
    lines = [f"[{TAG}] `{rel_path}` 命中 `{pattern}`（{entry['id']}）"]
    if reads:
        lines.append(f"  先讀：{reads}")
    lines.append(f"  約束：{entry['note']}")
    return "\n".join(lines)


def marker_path(payload: dict) -> Path:
    sid = str(payload.get("session_id") or "nosession")
    return gl.state_dir(payload) / f"vibe-paths-map.{gl.sid_digest(sid)}.json"


def load_fired(marker: Path) -> set[str]:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return set(x for x in data if isinstance(x, str)) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_fired(marker: Path, fired: set[str]) -> None:
    try:
        marker.write_text(json.dumps(sorted(fired)), encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[{TAG}] warning: could not write marker {marker}: {exc}", file=sys.stderr)


def select_hits(entries: list[dict], paths: list[str], fired: set[str]) -> list[tuple[dict, str, str]]:
    """(entry, path, pattern) for every not-yet-fired entry some path matches."""
    hits: list[tuple[dict, str, str]] = []
    for entry in entries:
        if entry["id"] in fired:
            continue
        for rel in paths:
            pat = matches(rel, entry["paths"])
            if pat:
                hits.append((entry, rel, pat))
                break
    return hits


def emit_context(text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": text}},
                     ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_hook(payload: dict, map_path: Path) -> int:
    marker = marker_path(payload)
    fired = load_fired(marker)
    try:
        entries = load_map(map_path)
    except MapError as exc:
        if LOAD_ERROR_ID not in fired:
            fired.add(LOAD_ERROR_ID)
            save_fired(marker, fired)
            emit_context(f"[{TAG}] WARNING: {MAP_REL} unreadable — path-triggered guidance is "
                         f"OFF for this session ({exc}). Fix the map; the pre-commit gate "
                         f"`session-guard-liveness-check` validates it.")
        print(f"[{TAG}] warning: {exc}", file=sys.stderr)
        return 0
    paths = candidate_paths(payload)
    if not paths:
        return 0
    hits = select_hits(entries, paths, fired)
    if not hits:
        return 0
    for entry, _rel, _pat in hits:
        fired.add(entry["id"])
    save_fired(marker, fired)
    emit_context("\n".join(render_hit(e, rel, pat) for e, rel, pat in hits))
    return 0


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Path-triggered guidance hook.")
    parser.add_argument("--map", default=None, help=f"map file (default <repo>/{MAP_REL})")
    parser.add_argument("--validate", action="store_true", help="validate the map and exit (rc 1 on error)")
    parser.add_argument("--match", nargs="*", default=None,
                        help="print the entries these repo-relative paths hit, then exit")
    args = parser.parse_args(argv)

    map_path = Path(args.map) if args.map else gl.repo_root_from(__file__) / MAP_REL

    if args.validate or args.match is not None:
        try:
            entries = load_map(map_path)
        except MapError as exc:
            print(f"[{TAG}] INVALID: {exc}", file=sys.stderr)
            return 1
        if args.validate:
            print(f"[{TAG}] ok: {len(entries)} entries in {map_path}")
            return 0
        for rel in args.match:
            hit = select_hits(entries, [_norm(rel)], set())
            for entry, path, pat in hit:
                print(render_hit(entry, path, pat))
            if not hit:
                print(f"[{TAG}] `{rel}`: no entry")
        return 0

    payload = gl.read_payload()
    if payload is None:
        return 0
    try:
        return run_hook(payload, map_path)
    except Exception as exc:  # noqa: BLE001 — never block on a hook bug
        print(f"[{TAG}] warning: hook crashed ({exc}); allowing", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
