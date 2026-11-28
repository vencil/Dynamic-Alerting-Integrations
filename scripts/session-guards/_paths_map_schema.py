"""The schema of `.agents/paths-map.json`: one validator, two readers.

`paths_map.py` (the PreToolUse hook that reads the map) and
`scripts/tools/dx/gen_agent_adapters.py` (which projects it into the other
vendors' files) both judge the map with these functions, so they cannot
disagree about what a well-formed map is.

A library, not a hook: it imports nothing beyond the standard library and
declares no exit protocol. The hook declares the hook protocol for its own
process; the generator loads this file instead of the hook so that its exit
codes stay a CLI tool's (#3981).
"""
from __future__ import annotations

import re


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
        if any("," in p for p in paths):
            # Copilot's `applyTo` is one comma-joined string; a comma inside a
            # glob would split it into globs the map never declared.
            raise MapError(f"{where} ({eid}): a glob may not contain a comma")
        _check_read_and_note(e, f"{where} ({eid})")
        entries.append(e)
    validate_commands(data)
    return entries


def _check_read_and_note(e: dict, where: str) -> None:
    reads = e.get("read")
    if not isinstance(reads, list) or not all(
            isinstance(r, dict) and isinstance(r.get("file"), str) and r["file"]
            and (r.get("section") is None or isinstance(r["section"], str))
            for r in reads):
        raise MapError(f"{where}: `read` must be a list of {{file, section?}}")
    note = e.get("note")
    if not isinstance(note, str) or not note.strip():
        raise MapError(f"{where}: `note` must be a non-empty string")


def validate_commands(data: dict) -> list[dict]:
    """The optional top-level `commands` list: guidance keyed on a Bash command
    (a regex searched in the whole command string) instead of on a path.

    Claude-only: Cursor and Copilot have no command trigger, so
    gen_agent_adapters.py projects `entries` and never reads this list.
    Every command carries `examples` that its own pattern must match — a regex
    typo would otherwise leave an entry that never fires and says nothing.
    """
    cmds = data.get("commands", [])
    if not isinstance(cmds, list):
        raise MapError("`commands` must be a list")
    seen: set[str] = set()
    out: list[dict] = []
    for i, c in enumerate(cmds):
        where = f"commands[{i}]"
        if not isinstance(c, dict):
            raise MapError(f"{where}: must be an object")
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid):
            raise MapError(f"{where}: `id` must be a lowercase-hyphen slug")
        if cid in seen:
            raise MapError(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        pat = c.get("pattern")
        if not isinstance(pat, str) or not pat.strip():
            raise MapError(f"{where} ({cid}): `pattern` must be a non-empty regex string")
        try:
            rx = re.compile(pat)
        except re.error as exc:
            raise MapError(f"{where} ({cid}): `pattern` is not a valid regex: {exc}") from exc
        examples = c.get("examples")
        if not isinstance(examples, list) or not examples or not all(
                isinstance(x, str) and x for x in examples):
            raise MapError(f"{where} ({cid}): `examples` must be a non-empty list of commands")
        dead = [x for x in examples if not rx.search(x)]
        if dead:
            raise MapError(f"{where} ({cid}): `pattern` does not match its example {dead[0]!r}")
        _check_read_and_note(c, f"{where} ({cid})")
        out.append(c)
    return out
