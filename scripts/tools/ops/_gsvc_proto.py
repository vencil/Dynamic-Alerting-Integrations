"""Request checks, result frames and the parse-cache store of generator_service.

``generator_service`` (#2901, ADR-036 §3): the validate request's checks, the
frames the judgement process sends back, and the store of parse results.

Standard library only. Nothing here parses conf.d content: the request
checks read the JSON envelope (paths, commit id, deadline) and check that
each ``content_b64`` is base64, and the store keeps the judgement processes'
pickled parse results as opaque bytes.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Optional

#: Bytes of the generator's stderr a response carries (the end of it).
STDERR_TAIL_BYTES = 64 * 1024

#: The reasons a judgement can be refused for — "reject this one write", never
#: "the container is unusable" (ADR-036 §3).
REJECT_REASONS = ("deadline", "memory", "incomplete", "git_read", "busy")

#: Why the container answers 503 VALIDATOR_UNAVAILABLE.
UNAVAILABLE_REASONS = ("amtool_missing", "generator_too_old", "internal_error")

_COMMIT_LEN = {"sha1": 40, "sha256": 64}
_HEX = re.compile(r"[0-9a-f]+")


class RequestError(ValueError):
    """The request body is not a well-formed validate request (HTTP 400)."""


@dataclass(frozen=True)
class Write:
    path: str
    content: Optional[bytes]  # None: delete

    @property
    def delete(self) -> bool:
        return self.content is None


@dataclass(frozen=True)
class ValidateRequest:
    base_commit: str
    writes: tuple[Write, ...]
    deadline_ms: int


def path_problem(path: object) -> Optional[str]:
    """Why *path* is not a conf.d-relative POSIX file path, or None."""
    if not isinstance(path, str):
        return "path must be a string"
    if path == "":
        return "path is empty"
    if "\x00" in path:
        return "path contains NUL"
    if "\\" in path:
        return "path contains a backslash"
    if path.startswith("/"):
        return "path is absolute"
    for seg in path.split("/"):
        if seg == "":
            return "path has an empty segment"
        if seg in (".", ".."):
            return f"path has a '{seg}' segment"
    return None


def parse_validate_request(body: bytes, object_format: str) -> ValidateRequest:
    """The validate request in *body*, or RequestError naming what is wrong."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RequestError(f"body is not JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise RequestError("body must be a JSON object")
    unknown = sorted(set(doc) - {"base_commit", "writes", "deadline_ms"})
    if unknown:
        raise RequestError(f"unknown field(s): {', '.join(unknown)}")
    for key in ("base_commit", "writes", "deadline_ms"):
        if key not in doc:
            raise RequestError(f"missing field: {key}")

    commit = doc["base_commit"]
    want = _COMMIT_LEN[object_format]
    if not (isinstance(commit, str) and len(commit) == want
            and _HEX.fullmatch(commit)):
        raise RequestError(
            f"base_commit must be a full {want}-character lowercase hex "
            f"commit id (this repository uses {object_format}); ref names "
            f"and abbreviations are not accepted")

    deadline = doc["deadline_ms"]
    if isinstance(deadline, bool) or not isinstance(deadline, int):
        raise RequestError("deadline_ms must be an integer (milliseconds)")

    raw_writes = doc["writes"]
    if not isinstance(raw_writes, list):
        raise RequestError("writes must be an array")
    writes: list[Write] = []
    seen: set[str] = set()
    for i, w in enumerate(raw_writes):
        where = f"writes[{i}]"
        if not isinstance(w, dict):
            raise RequestError(f"{where} must be an object")
        problem = path_problem(w.get("path"))
        if problem is not None:
            raise RequestError(f"{where}: {problem}")
        path = w["path"]
        if path in seen:
            raise RequestError(f"{where}: path {path!r} appears more than once")
        seen.add(path)
        keys = set(w)
        if keys == {"path", "content_b64"}:
            text = w["content_b64"]
            if not isinstance(text, str):
                raise RequestError(f"{where}: content_b64 must be a string")
            try:
                content = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                raise RequestError(f"{where}: content_b64 is not base64") from None
            writes.append(Write(path, content))
        elif keys == {"path", "delete"}:
            if w["delete"] is not True:
                raise RequestError(f"{where}: delete must be true")
            writes.append(Write(path, None))
        else:
            raise RequestError(
                f"{where} must be {{path, content_b64}} or {{path, delete: true}}")
    return ValidateRequest(commit, tuple(writes), deadline)


def judgement_seconds(deadline_ms: int, max_seconds: float,
                      reserve_seconds: float) -> float:
    """min(*max_seconds*, *deadline_ms* − *reserve_seconds*), in seconds:
    the time the container may spend on one request, queueing included
    (ADR-036 §3). ≤ 0 means refuse at once."""
    return min(max_seconds, deadline_ms / 1000.0 - reserve_seconds)


# ── result frames (judgement process → long-running process, one pipe) ────
#
# frame := type (1 byte) | length (4 bytes, big-endian) | payload
#   b"R"  the result, JSON
#   b"E"  one parse-cache entry: key length (2 bytes) | key JSON | value bytes
#   b"Z"  end (length 0)

_HEAD = struct.Struct(">cI")


def frame(kind: bytes, payload: bytes) -> bytes:
    return _HEAD.pack(kind, len(payload)) + payload


def entry_frame(key: tuple[str, str], value: bytes) -> bytes:
    k = json.dumps(list(key)).encode("utf-8")
    return frame(b"E", struct.pack(">H", len(k)) + k + value)


@dataclass
class Decoded:
    result: Optional[dict] = None
    entries: list[tuple[tuple[str, str], bytes]] = field(default_factory=list)
    ended: bool = False
    dropped_entries: int = 0


def decode_frames(buf: bytes, *, max_result: int, max_entries: int) -> Decoded:
    """The frames in *buf*. A result larger than *max_result* bytes is not
    decoded; entries past *max_entries* bytes in total are dropped (counted,
    never an error). A truncated last frame is ignored."""
    out = Decoded()
    pos, used = 0, 0
    while pos + _HEAD.size <= len(buf):
        kind, length = _HEAD.unpack_from(buf, pos)
        start, end = pos + _HEAD.size, pos + _HEAD.size + length
        if end > len(buf):
            break
        payload = buf[start:end]
        pos = end
        if kind == b"R" and out.result is None:
            if length > max_result:
                continue
            try:
                doc = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(doc, dict):
                out.result = doc
        elif kind == b"E":
            if used + length > max_entries or length < 2:
                out.dropped_entries += 1
                continue
            (klen,) = struct.unpack_from(">H", payload, 0)
            try:
                key = json.loads(payload[2:2 + klen].decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                out.dropped_entries += 1
                continue
            if not (isinstance(key, list) and len(key) == 2
                    and all(isinstance(k, str) for k in key)):
                out.dropped_entries += 1
                continue
            used += length
            out.entries.append(((key[0], key[1]), payload[2 + klen:]))
        elif kind == b"Z":
            out.ended = True
    return out


# ── the parse-cache store of the long-running process ─────────────────────

class CacheStore:
    """Pickled parse results by ``(blob id, mode)``, most recently used last.

    Every entry whose blob is in the latest base tree (:meth:`set_pinned`) is
    kept; the others together may use at most *unpinned_limit* bytes, the
    least recently used going first (ADR-036 §3 淘汰).
    """

    def __init__(self, unpinned_limit: int) -> None:
        self.unpinned_limit = unpinned_limit
        self.entries: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()
        self.pinned: frozenset[str] = frozenset()

    def add(self, key: tuple[str, str], value: bytes) -> None:
        self.entries[key] = value
        self.entries.move_to_end(key)

    def touch(self, keys: Iterable[tuple[str, str]]) -> None:
        for key in keys:
            if key in self.entries:
                self.entries.move_to_end(key)

    def set_pinned(self, blob_ids: Iterable[str]) -> None:
        self.pinned = frozenset(blob_ids)

    def sizes(self) -> tuple[int, int]:
        """(pinned bytes, unpinned bytes)."""
        pinned = unpinned = 0
        for (blob, _mode), value in self.entries.items():
            if blob in self.pinned:
                pinned += len(value)
            else:
                unpinned += len(value)
        return pinned, unpinned

    def evict(self) -> int:
        """Drop least recently used unpinned entries until the unpinned
        total fits the limit; returns how many were dropped."""
        _pinned, unpinned = self.sizes()
        dropped = 0
        if unpinned <= self.unpinned_limit:
            return 0
        for key in list(self.entries):
            if unpinned <= self.unpinned_limit:
                break
            if key[0] in self.pinned:
                continue
            unpinned -= len(self.entries.pop(key))
            dropped += 1
        return dropped
