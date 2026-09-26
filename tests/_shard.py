"""Deterministic test sharding for CI (`pytest --shard=K/N`).

CI splits the required pytest leg into N parallel jobs; each job passes
``--shard=K/N`` and keeps only the tests this module assigns to shard K.

⛔ Why a hash of the nodeid and not a durations file: a committed timing file
is one more file every PR would touch, which is the merge-conflict shape the
split exists to get away from. The suite is tens of thousands of tests with no
single test dominating, so a hash spreads wall time evenly enough.

⛔ Why ``zlib.crc32`` and not ``hash()``: ``hash(str)`` is salted per process
(PYTHONHASHSEED), so two xdist workers — or two CI jobs — would disagree on
which shard a test belongs to, and a test could run twice or never. The
function below is pure in the nodeid: every job that collects the same tree
computes the same partition, so the N shards cover the collection exactly once.
"""
from __future__ import annotations

import re
import zlib

_SPEC_RE = re.compile(r"^([1-9][0-9]*)/([1-9][0-9]*)$")


def parse_shard_spec(spec: str) -> tuple[int, int]:
    """Parse ``"K/N"`` (1-based) into ``(K, N)``; raise ValueError otherwise."""
    m = _SPEC_RE.match(spec.strip())
    if not m:
        raise ValueError(f"--shard expects K/N with 1 <= K <= N, got {spec!r}")
    k, n = int(m.group(1)), int(m.group(2))
    if k > n:
        raise ValueError(f"--shard expects K/N with 1 <= K <= N, got {spec!r}")
    return k, n


def shard_of(nodeid: str, total: int) -> int:
    """Return the 1-based shard that owns ``nodeid`` out of ``total``."""
    return zlib.crc32(nodeid.encode("utf-8")) % total + 1
