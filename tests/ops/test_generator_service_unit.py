"""#2901（ADR-036 §3）：`generator_service` 不需要起服務就能驗的部分。

期限計算、請求格式（路徑、commit id、base64）、結果 frame 的上限、快取的
LRU 加 pinned 淘汰，以及排隊：到期移出、佇列滿回 busy（以不 fork 的假判定
驅動同一個 `Service`）。
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import _gsvc_proto as proto  # noqa: E402
import generator_service as gs  # noqa: E402

C1 = "a" * 40
C256 = "b" * 64


def _body(**kw) -> bytes:
    doc = {"base_commit": C1, "writes": [], "deadline_ms": 25000}
    doc.update(kw)
    return json.dumps(doc).encode()


# ── deadline ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("deadline_ms,want", [
    (30000, 20.0),   # capped by the maximum
    (25000, 20.0),
    (24000, 19.0),   # caller's remaining time minus the reserve
    (5001, 0.001),
    (5000, 0.0),     # nothing left: refused
    (-1, -5.001),
])
def test_judgement_seconds(deadline_ms, want):
    assert proto.judgement_seconds(deadline_ms, 20.0, 5.0) == pytest.approx(want)


# ── request format ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path,problem", [
    ("alpha.yaml", None),
    ("team/alpha.yaml", None),
    ("_defaults.yaml", None),
    ("", "empty"),
    ("/etc/passwd", "absolute"),
    ("../x.yaml", "'..'"),
    ("a/../x.yaml", "'..'"),
    ("a//x.yaml", "empty segment"),
    ("a/", "empty segment"),
    ("./x.yaml", "'.'"),
    ("a\\x.yaml", "backslash"),
    ("a\x00.yaml", "NUL"),
    (5, "string"),
])
def test_path_problem(path, problem):
    got = proto.path_problem(path)
    if problem is None:
        assert got is None
    else:
        assert got is not None and problem in got


def test_a_valid_request():
    content = b"tenants: {alpha: {}}\n"
    req = proto.parse_validate_request(_body(writes=[
        {"path": "alpha.yaml", "content_b64": base64.b64encode(content).decode()},
        {"path": "beta.yaml", "delete": True},
    ]), "sha1")
    assert req.base_commit == C1 and req.deadline_ms == 25000
    assert req.writes[0].content == content and not req.writes[0].delete
    assert req.writes[1].delete and req.writes[1].content is None


@pytest.mark.parametrize("body,needle", [
    (b"not json", "not JSON"),
    (b"\xff", "not JSON"),
    (b"[]", "object"),
    (_body(extra=1), "unknown field"),
    (json.dumps({"writes": [], "deadline_ms": 1}).encode(), "missing field: base_commit"),
    (_body(base_commit="main"), "base_commit"),
    (_body(base_commit="A" * 40), "base_commit"),       # uppercase
    (_body(base_commit="a" * 39), "base_commit"),       # abbreviated
    (_body(base_commit=C256), "base_commit"),           # other object format
    (_body(deadline_ms="25000"), "deadline_ms"),
    (_body(deadline_ms=2.5), "deadline_ms"),
    (_body(deadline_ms=True), "deadline_ms"),
    (_body(writes={}), "array"),
    (_body(writes=["x"]), "object"),
    (_body(writes=[{"path": "../a", "delete": True}]), "'..'"),
    (_body(writes=[{"path": "a", "delete": True}, {"path": "a", "delete": True}]),
     "more than once"),
    (_body(writes=[{"path": "a", "delete": False}]), "delete must be true"),
    (_body(writes=[{"path": "a", "content_b64": "!!"}]), "not base64"),
    (_body(writes=[{"path": "a", "content_b64": 5}]), "string"),
    (_body(writes=[{"path": "a", "content_b64": "", "delete": True}]), "must be"),
    (_body(writes=[{"path": "a"}]), "must be"),
])
def test_malformed_requests(body, needle):
    with pytest.raises(proto.RequestError, match=needle):
        proto.parse_validate_request(body, "sha1")


def test_sha256_repo_takes_64_char_commits():
    assert proto.parse_validate_request(_body(base_commit=C256), "sha256").base_commit == C256
    with pytest.raises(proto.RequestError):
        proto.parse_validate_request(_body(base_commit=C1), "sha256")


# ── result frames ────────────────────────────────────────────────────────

def _stream(result: dict, entries, end=True) -> bytes:
    out = proto.frame(b"R", json.dumps(result).encode())
    for key, value in entries:
        out += proto.entry_frame(key, value)
    return out + (proto.frame(b"Z", b"") if end else b"")


def test_frames_round_trip():
    entries = [(("b1", "m"), b"x" * 10), (("b2", "m"), b"y" * 20)]
    got = proto.decode_frames(_stream({"rc": 0}, entries), max_result=1000,
                              max_entries=1000)
    assert got.result == {"rc": 0} and got.entries == entries and got.ended


def test_entries_past_the_cap_are_dropped_not_fatal():
    entries = [((f"b{i}", "m"), b"x" * 100) for i in range(5)]
    got = proto.decode_frames(_stream({"rc": 0}, entries), max_result=1000,
                              max_entries=250)
    assert len(got.entries) == 2 and got.dropped_entries == 3
    assert got.result == {"rc": 0} and got.ended


def test_an_oversized_result_is_not_decoded():
    got = proto.decode_frames(_stream({"rc": 0, "pad": "x" * 500}, []),
                              max_result=100, max_entries=0)
    assert got.result is None


def test_a_truncated_stream_has_no_end():
    raw = _stream({"rc": 0}, [(("b", "m"), b"v" * 50)])
    got = proto.decode_frames(raw[:-10], max_result=1000, max_entries=1000)
    assert got.result == {"rc": 0} and not got.ended and got.entries == []


# ── cache store: LRU + pinned ────────────────────────────────────────────

def test_unpinned_entries_are_evicted_least_recently_used_first():
    store = proto.CacheStore(unpinned_limit=250)
    for name in ("a", "b", "c"):
        store.add((name, "m"), b"x" * 100)
    store.touch([("a", "m")])          # a is now the most recent
    assert store.evict() == 1
    assert list(store.entries) == [("c", "m"), ("a", "m")]  # b went


def test_pinned_entries_are_kept_and_not_counted():
    store = proto.CacheStore(unpinned_limit=100)
    store.add(("p1", "m"), b"x" * 1000)
    store.add(("p1", "other"), b"x" * 1000)
    store.add(("u1", "m"), b"x" * 80)
    store.add(("u2", "m"), b"x" * 80)
    store.set_pinned({"p1"})
    assert store.sizes() == (2000, 160)
    assert store.evict() == 1
    assert set(store.entries) == {("p1", "m"), ("p1", "other"), ("u2", "m")}
    assert store.sizes() == (2000, 80)


def test_unpinning_a_tree_makes_its_entries_evictable():
    store = proto.CacheStore(unpinned_limit=100)
    store.add(("old", "m"), b"x" * 1000)
    store.set_pinned({"old"})
    assert store.evict() == 0
    store.set_pinned({"new"})          # the latest base no longer has it
    assert store.evict() == 1 and not store.entries


def test_one_entry_larger_than_the_limit_does_not_stay():
    store = proto.CacheStore(unpinned_limit=10)
    store.add(("u", "m"), b"x" * 11)
    store.evict()
    assert not store.entries


# ── queue (no fork: the judgement is faked) ──────────────────────────────

class _FakeService(gs.Service):
    def __init__(self, delay: float, max_queue: int = 32,
                 max_seconds: float = 20.0) -> None:
        settings = gs.Settings(
            socket="", repo="", config_subdir="conf.d", work_dir="", policy=None,
            git="git", da_guard_binary="/nonexistent",
            max_judgement_seconds=max_seconds, deadline_reserve_seconds=5.0,
            memory_limit_bytes=1, cache_max_bytes=1, max_queue=max_queue,
            max_body_bytes=1, read_timeout_seconds=1.0, max_result_bytes=1,
            max_cache_transfer_bytes=1)
        super().__init__(settings, "sha1")
        self.delay = delay
        self.judged: list[str] = []

    async def _judge(self, ticket):
        self.judged.append(ticket.request.base_commit)
        await asyncio.sleep(self.delay)
        return gs.Response(200, {"result": "pass", "rc": 0})


def _req(commit: str, deadline_ms: int) -> proto.ValidateRequest:
    return proto.ValidateRequest(commit, (), deadline_ms)


def _doc(resp) -> dict:
    return json.loads(resp.body)


def test_a_request_that_expires_in_the_queue_is_answered_and_removed():
    async def scenario():
        svc = _FakeService(delay=0.6)
        svc.loop = asyncio.get_running_loop()
        t0 = svc.loop.time()
        first = asyncio.create_task(svc.submit(_req("1" * 40, 25000), t0))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(svc.submit(_req("2" * 40, 5200), svc.loop.time()))
        third = asyncio.create_task(svc.submit(_req("3" * 40, 25000), svc.loop.time()))
        r2 = await second
        when = svc.loop.time() - t0
        assert _doc(r2)["reason"] == "deadline" and when < 0.5
        assert len(svc.waiting) == 1      # only the third still waits
        r1, r3 = await first, await third
        assert _doc(r1)["result"] == _doc(r3)["result"] == "pass"
        assert svc.judged == ["1" * 40, "3" * 40]  # the expired one never ran
        assert svc.metrics.rejections["deadline"] == 1
    asyncio.run(scenario())


def test_a_full_queue_answers_busy():
    async def scenario():
        svc = _FakeService(delay=0.3, max_queue=1)
        svc.loop = asyncio.get_running_loop()
        now = svc.loop.time
        running = asyncio.create_task(svc.submit(_req("1" * 40, 25000), now()))
        await asyncio.sleep(0.05)
        waiting = asyncio.create_task(svc.submit(_req("2" * 40, 25000), now()))
        await asyncio.sleep(0.05)
        busy = await svc.submit(_req("3" * 40, 25000), now())
        assert _doc(busy)["result"] == "rejected"
        assert _doc(busy)["reason"] == "busy" and busy.status == 200
        assert _doc(await running)["result"] == _doc(await waiting)["result"] == "pass"
        assert svc.metrics.rejections["busy"] == 1
    asyncio.run(scenario())


def test_no_time_left_is_refused_before_queueing():
    async def scenario():
        svc = _FakeService(delay=0)
        svc.loop = asyncio.get_running_loop()
        resp = await svc.submit(_req("1" * 40, 5000), svc.loop.time())
        assert _doc(resp)["reason"] == "deadline" and svc.judged == []
    asyncio.run(scenario())


def test_queue_time_counts_against_the_budget():
    """Received at t, budget min(20, deadline−5): the clock started at
    receipt, so a request received 'earlier' expires sooner."""
    async def scenario():
        svc = _FakeService(delay=1.0)
        svc.loop = asyncio.get_running_loop()
        asyncio.create_task(svc.submit(_req("1" * 40, 25000), svc.loop.time()))
        await asyncio.sleep(0.05)
        t0 = svc.loop.time()
        resp = await svc.submit(_req("2" * 40, 5800), t0 - 0.5)  # 0.8 s budget
        assert _doc(resp)["reason"] == "deadline"
        assert svc.loop.time() - t0 < 0.5
    asyncio.run(scenario())
