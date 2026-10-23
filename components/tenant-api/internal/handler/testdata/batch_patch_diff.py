"""The generator's side of batch_patch_diff_test.go (#2903, ADR-036 §4).

Reads a JSON list of cases on stdin — {base, merged, tenant, patch, unset}
(merged is null when tenant-api refused the merge) — and writes one result
per case to stdout as JSON:

  {"base_readable": bool, "base_error": str,
   "merged_readable": bool, "merged_error": str,
   "equal": bool, "want": str, "got": str}

Both files are read exactly as the route generator reads a conf.d file:
`da-guard parse-tree` (ADR-036 step 4), converted by _grar_parse.ROUTING_READ.
da-guard comes from $DA_GUARD_BINARY (batch_patch_diff_test.go builds it) or
$PATH. `want` is the
base as the generator reads it with the same patch laid over
tenants.<tenant> (each patch value a string; each unset key removed; a
tenant the base lacks is added); `got` is the merged file as the generator
reads it. `equal` compares the whole document — every tenant of the file
and every other key — not only the patched block.

argv[1] is the repository root.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

ROOT = sys.argv[1]
sys.path.insert(0, os.path.join(ROOT, "scripts", "tools"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "tools", "ops"))
from _grar_parse import ROUTING_READ  # noqa: E402
from _lib_parse_cache import ConfdReader, judging  # noqa: E402
from _lib_parse_tree import FileUnreadable  # noqa: E402


def read(text: str, where: str) -> tuple[bool, object, str]:
    path = os.path.join(where, "t.yaml")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        # A new reader per file: the tree holds only t.yaml, rewritten each time.
        # As the route generator reads it — a judge (#3206): a file it cannot
        # read is its finding (FileUnreadable), not the shared conf.d refusal.
        with judging():
            data = ConfdReader(where).read(path, ROUTING_READ)
    except FileUnreadable as e:  # the generator cannot read it (no da-guard: raises)
        return False, None, f"{e.kind}: {' '.join(e.message.split())}"
    return True, data, ""


def canon(v: object, path: tuple = ()) -> object:
    """A comparable, printable form: mapping order and object identity do
    not count, a value's type does (1 and "1" and True differ). A container
    that holds itself (an alias cycle) is written as how far up it is."""
    if isinstance(v, (dict, list, tuple)):
        if id(v) in path:
            return ["cycle", len(path) - path.index(id(v))]
        path = path + (id(v),)
    if isinstance(v, dict):
        return ["map", sorted([canon(k, path), canon(x, path)] for k, x in v.items())]
    if isinstance(v, (list, tuple)):
        return ["seq", [canon(x, path) for x in v]]
    if isinstance(v, float) and math.isnan(v):
        return ["float", "nan"]
    return [type(v).__name__, repr(v)]


def apply(base: object, tenant: str, patch: dict, unset: list) -> object:
    doc = dict(base) if isinstance(base, dict) else {}
    tenants = doc.get("tenants")
    tenants = dict(tenants) if isinstance(tenants, dict) else {}
    block = tenants.get(tenant)
    block = dict(block) if isinstance(block, dict) else {}
    block.update(patch)
    for k in unset:
        block.pop(k, None)
    tenants[tenant] = block
    doc["tenants"] = tenants
    return doc


def main() -> None:
    cases = json.load(sys.stdin)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for c in cases:
            ok_b, base, err_b = read(c["base"], tmp)
            r = {"base_readable": ok_b, "base_error": err_b, "merged_readable": False,
                 "merged_error": "", "equal": False, "want": "", "got": ""}
            if c["merged"] is not None:
                ok_m, merged, err_m = read(c["merged"], tmp)
                r["merged_readable"], r["merged_error"] = ok_m, err_m
                if ok_b and ok_m:
                    want = canon(apply(base, c["tenant"], c["patch"], c["unset"]))
                    got = canon(merged)
                    r["equal"] = want == got
                    r["want"], r["got"] = json.dumps(want), json.dumps(got)
            out.append(r)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
