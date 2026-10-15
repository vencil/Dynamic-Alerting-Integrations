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
    ]), "sha1", 100)
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
    # #2901 O1 / O2: inputs that used to escape as an exception
    (_body(deadline_ms=10 ** 400), "between 0 and"),
    (_body(deadline_ms=-10 ** 400), "between 0 and"),
    (_body(deadline_ms=-1), "between 0 and"),
    (_body(deadline_ms=proto.MAX_DEADLINE_MS + 1), "between 0 and"),
    (_body(deadline_ms=False), "deadline_ms"),
    (b"[" * 200000, "nested too deeply"),
    (_body(writes=[{"path": f"p{i}", "delete": True} for i in range(101)]), "at most 100"),
    (_body(writes=[{"path": "a", "content_b64": "QQ"}]), "not base64"),       # unpadded
    (_body(writes=[{"path": "a", "content_b64": "QQ==\n"}]), "not base64"),
])
def test_malformed_requests(body, needle):
    with pytest.raises(proto.RequestError, match=needle):
        proto.parse_validate_request(body, "sha1", 100)


def test_sha256_repo_takes_64_char_commits():
    assert proto.parse_validate_request(_body(base_commit=C256), "sha256", 100).base_commit == C256
    with pytest.raises(proto.RequestError):
        proto.parse_validate_request(_body(base_commit=C1), "sha256", 100)


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
            max_cache_transfer_bytes=1, warm_ref="", warm_seconds=1.0,
            max_connections=40, max_inflight_body_bytes=1 << 20, max_writes=100)
        super().__init__(settings, "sha1")
        self.delay = delay
        self.judged: list[str] = []
        #: when set (inside the running loop), a judgement runs until the test
        #: sets it — "still running" is then a fact, not a sleep's guess
        self.gate: asyncio.Event | None = None

    async def _judge(self, ticket):
        self.judged.append(ticket.request.base_commit)
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(self.delay)
        return gs.Response(200, {"result": "pass", "rc": 0})


async def _settle() -> None:
    """Let every ready task run up to its next real wait."""
    for _ in range(20):
        await asyncio.sleep(0)


def _req(commit: str, deadline_ms: int) -> proto.ValidateRequest:
    return proto.ValidateRequest(commit, (), deadline_ms)


def _doc(resp) -> dict:
    return json.loads(resp.body)


def test_a_request_that_expires_in_the_queue_is_answered_and_removed():
    async def scenario():
        svc = _FakeService(delay=0)
        svc.loop = asyncio.get_running_loop()
        svc.gate = asyncio.Event()  # the first runs until released
        first = asyncio.create_task(svc.submit(_req("1" * 40, 25000), svc.loop.time()))
        await _settle()
        second = asyncio.create_task(svc.submit(_req("2" * 40, 5200), svc.loop.time()))
        third = asyncio.create_task(svc.submit(_req("3" * 40, 25000), svc.loop.time()))
        r2 = await second
        assert _doc(r2)["reason"] == "deadline"
        assert not first.done()           # answered without waiting for it
        assert len(svc.waiting) == 1      # only the third still waits
        svc.gate.set()
        r1, r3 = await first, await third
        assert _doc(r1)["result"] == _doc(r3)["result"] == "pass"
        assert svc.judged == ["1" * 40, "3" * 40]  # the expired one never ran
        assert svc.metrics.rejections["deadline"] == 1
    asyncio.run(scenario())


def test_a_full_queue_answers_busy():
    async def scenario():
        svc = _FakeService(delay=0, max_queue=1)
        svc.loop = asyncio.get_running_loop()
        svc.gate = asyncio.Event()
        now = svc.loop.time
        running = asyncio.create_task(svc.submit(_req("1" * 40, 25000), now()))
        await _settle()
        waiting = asyncio.create_task(svc.submit(_req("2" * 40, 25000), now()))
        await _settle()
        assert len(svc.waiting) == 1
        busy = await svc.submit(_req("3" * 40, 25000), now())
        assert _doc(busy)["result"] == "rejected"
        assert _doc(busy)["reason"] == "busy" and busy.status == 200
        svc.gate.set()
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
    receipt, so a request received 'earlier' expires sooner. Received 10 s
    ago with a 10.3 s budget it expires 0.3 s from now; counted from now it
    would take 10.3 s — the bound sits far between the two."""
    async def scenario():
        svc = _FakeService(delay=0)
        svc.loop = asyncio.get_running_loop()
        svc.gate = asyncio.Event()  # the running one never ends on its own
        first = asyncio.create_task(svc.submit(_req("1" * 40, 25000), svc.loop.time()))
        await _settle()
        t0 = svc.loop.time()
        resp = await svc.submit(_req("2" * 40, 15300), t0 - 10.0)  # 10.3 s budget
        assert _doc(resp)["reason"] == "deadline"
        assert svc.loop.time() - t0 < 5.0
        svc.gate.set()
        await first
    asyncio.run(scenario())


# ── E1: incremental frames ───────────────────────────────────────────────

def test_frames_fed_byte_by_byte_come_out_as_they_complete():
    raw = (proto.frame(b"B", json.dumps(["b1", "b2"]).encode())
           + proto.entry_frame(("b1", "m"), b"v1")
           + proto.frame(b"R", b'{"rc": 0}') + proto.frame(b"Z", b""))
    reader = proto.FrameReader(max_result=1000, max_entries=1000)
    events = []
    for i in range(len(raw)):
        events += reader.feed(raw[i:i + 1])
    assert events == [("B", ["b1", "b2"]), ("E", (("b1", "m"), b"v1")),
                      ("R", {"rc": 0}), ("Z", None)]
    assert reader.ended and reader.result == {"rc": 0}


def test_an_entry_arrives_before_the_process_ends():
    """The entry is usable as soon as its frame is complete — no end frame
    and no result needed (a killed process's parses are kept)."""
    reader = proto.FrameReader(max_result=1000, max_entries=1000)
    piece = proto.entry_frame(("b1", "m"), b"value")
    assert reader.feed(piece[:-1]) == []
    assert reader.feed(piece[-1:]) == [("E", (("b1", "m"), b"value"))]
    assert reader.result is None and not reader.ended


def test_an_over_limit_frame_is_skipped_without_being_buffered():
    reader = proto.FrameReader(max_result=10, max_entries=100)
    big = proto.entry_frame(("b", "m"), b"x" * 5000)
    after = proto.entry_frame(("c", "m"), b"y")
    events = []
    for i in range(0, len(big), 100):
        events += reader.feed(big[i:i + 100])
        assert len(reader._buf) < 200  # never holds the big payload
    events += reader.feed(after)
    assert events == [("E", (("c", "m"), b"y"))]
    assert reader.dropped_entries == 1


def test_coverage_of_the_pinned_tree():
    store = proto.CacheStore(unpinned_limit=100)
    assert store.coverage() == 0.0
    store.set_pinned({"a", "b", "c", "d"})
    store.add(("a", "m1"), b"x")
    store.add(("a", "m2"), b"x")
    store.add(("b", "m1"), b"x")
    store.add(("z", "m1"), b"x")  # not in the tree
    assert store.coverage() == 0.5


# ── E1 / E2 rules of the long-running process (no fork) ──────────────────

class _Evil:
    def __reduce__(self):
        import os
        return (os.system, ("true",))


def test_a_streamed_entry_must_read_back_under_the_restricted_unpickler():
    import pickle
    svc = _FakeService(delay=0)
    assert svc._admit(("b", "m"), pickle.dumps({"ok": 1}, protocol=5)) is True
    assert svc._admit(("c", "m"), pickle.dumps(_Evil(), protocol=5)) is False
    assert svc._admit(("d", "m"), b"not a pickle") is False
    assert list(svc.store.entries) == [("b", "m")]


def test_the_pin_follows_requests_and_a_warm_commit_only_before_one():
    svc = _FakeService(delay=0)
    svc._pin(["w1"], "W" * 40, request=False)       # warming before any request
    assert svc.pinned_commit == "W" * 40 and svc.store.pinned == {"w1"}
    svc._pin(["r1"], "R" * 40, request=True)        # a request moves it
    assert svc.pinned_commit == "R" * 40 and svc.store.pinned == {"r1"}
    svc._pin(["w2"], "W" * 40, request=False)       # another commit's warming does not
    assert svc.pinned_commit == "R" * 40 and svc.store.pinned == {"r1"}
    svc._pin(["r1", "r2"], "R" * 40, request=False)  # warming the pinned commit does
    assert svc.store.pinned == {"r1", "r2"}


class _WarmingFake(_FakeService):
    """_FakeService whose warming run is a sleep (no fork)."""

    def __init__(self, delay: float, warm_for: float) -> None:
        super().__init__(delay)
        self.warm_for = warm_for
        self.order: list[str] = []

    async def _judge(self, ticket):
        self.order.append("request")
        return await super()._judge(ticket)

    async def _warm(self, commit):
        self.order.append("warm")
        try:
            await asyncio.sleep(self.warm_for)
        finally:
            self.warming = None
            self._start_next()


def test_waiting_requests_go_before_a_pending_warming_run():
    async def scenario():
        svc = _WarmingFake(delay=0, warm_for=0.01)
        svc.loop = asyncio.get_running_loop()
        svc.gate = asyncio.Event()  # the first runs until both are in place
        now = svc.loop.time
        first = asyncio.create_task(svc.submit(_req("1" * 40, 25000), now()))
        await _settle()
        svc.pending_warm = "9" * 40
        second = asyncio.create_task(svc.submit(_req("2" * 40, 25000), now()))
        await _settle()
        svc.gate.set()
        await first
        await second
        await asyncio.sleep(0.05)
        assert svc.order == ["request", "request", "warm"]
    asyncio.run(scenario())



def test_base64_is_checked_but_not_decoded_by_the_service():
    text = base64.b64encode(b"x" * 1000).decode()
    req = proto.parse_validate_request(_body(writes=[
        {"path": "a", "content_b64": text}]), "sha1", 100)
    assert req.writes[0].content_b64 == text  # kept as text, decoded where used
    assert req.writes[0].content == b"x" * 1000


@pytest.mark.parametrize("n,ok", [(100, True), (101, False)])
def test_writes_limit_boundary(n, ok):
    body = _body(writes=[{"path": f"p{i}", "delete": True} for i in range(n)])
    if ok:
        assert len(proto.parse_validate_request(body, "sha1", 100).writes) == n
    else:
        with pytest.raises(proto.RequestError):
            proto.parse_validate_request(body, "sha1", 100)


def test_a_warming_run_scheduled_but_not_started_is_preempted():
    """#2901 R2 (the blind review's race): the warming task is created but
    has not run when a request arrives — the request must not wait for it."""
    async def scenario():
        svc = _WarmingFake(delay=0.05, warm_for=5.0)
        svc.loop = asyncio.get_running_loop()
        svc._warm = gs.Service._warm.__get__(svc)  # the real _warm …
        spawned = []

        async def fake_spawn(job, deadline_at, *, request):
            spawned.append(request)
            await asyncio.sleep(60.0)
        svc._spawn = fake_spawn                     # … with no fork
        svc.pending_warm = "9" * 40
        svc._start_next()                           # task created, not stepped
        t0 = svc.loop.time()
        resp = await svc.submit(_req("1" * 40, 9000), svc.loop.time())
        assert _doc(resp)["result"] == "pass"
        assert svc.loop.time() - t0 < 30.0  # vs the warm run's 60 s
        assert spawned == []                        # the warm run never forked
        assert svc.metrics.warm_runs["preempted"] == 1
    asyncio.run(scenario())


def test_checking_a_large_content_b64_does_not_balloon_memory():
    """#2901 R1: a grouped base64 regex measured +113 MiB on a 4 MB string;
    the check must stay flat (run in a fresh interpreter: ru_maxrss only
    ever grows)."""
    import subprocess
    import sys
    code = (
        "import base64, json, resource, sys\n"
        "import _gsvc_proto as p\n"
        "body = json.dumps({'base_commit': 'a' * 40, 'deadline_ms': 1, 'writes': [\n"
        "    {'path': 'x', 'content_b64': base64.b64encode(b'a' * 3_000_000).decode()}]}).encode()\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "p.parse_validate_request(body, 'sha1', 10)\n"
        "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, check=True,
                         env={**__import__("os").environ,
                              "PYTHONPATH": str(__import__("pathlib").Path(proto.__file__).parent)})
    assert int(out.stdout.strip()) < 20 * 1024, out.stdout  # KiB


def test_out_of_descriptors_when_forking_is_busy_not_503(tmp_path, monkeypatch):
    """#2901 R1: EMFILE at fork time rejects that write (busy); it does not
    make the container unavailable."""
    import errno
    work = tmp_path / "work"
    gs._prepare_work_dir(str(work))

    class _Real(_FakeService):
        _judge = gs.Service._judge

    svc = _Real(delay=0)
    svc.s.work_dir = str(work)
    monkeypatch.setattr(gs.shutil, "which", lambda name: "/bin/true")

    async def no_fds(job, deadline_at, *, request):
        raise OSError(errno.EMFILE, "Too many open files")

    svc._spawn = no_fds

    async def scenario():
        svc.loop = asyncio.get_running_loop()
        resp = await svc.submit(_req("1" * 40, 25000), svc.loop.time())
        assert resp.status == 200 and _doc(resp)["reason"] == "busy"
    asyncio.run(scenario())



@pytest.mark.parametrize("deadline", [0, proto.MAX_DEADLINE_MS])
def test_deadline_bounds_are_accepted(deadline):
    assert proto.parse_validate_request(_body(deadline_ms=deadline), "sha1",
                                        100).deadline_ms == deadline


def test_accept_errors_are_counted_and_logged_at_most_once_a_minute(capsys):
    """#2901 N2: EMFILE on accept is a metric plus a rate-limited line, not
    a traceback each time; anything else still reaches the default handler."""
    import errno

    class _Loop:
        t = 1000.0
        defaults: list = []

        def time(self):
            return self.t

        def default_exception_handler(self, context):
            self.defaults.append(context)

    svc = _FakeService(delay=0)
    loop = _Loop()
    ctx = {"message": "socket.accept() out of system resource",
           "exception": OSError(errno.EMFILE, "Too many open files")}
    for _ in range(5):
        svc.loop_exception(loop, ctx)
    loop.t += gs.ACCEPT_ERROR_LOG_INTERVAL_SECONDS - 1
    svc.loop_exception(loop, ctx)
    first = capsys.readouterr().err
    assert first.count("accept() failed with EMFILE") == 1
    assert "Traceback" not in first
    loop.t += 1
    svc.loop_exception(loop, ctx)
    svc.loop_exception(loop, {"message": "socket.accept() out of system resource",
                              "exception": OSError(errno.ENFILE, "x")})
    later = capsys.readouterr().err
    assert later.count("EMFILE") == 1 and later.count("ENFILE") == 1
    assert svc.metrics.accept_errors == {"EMFILE": 7, "ENFILE": 1}
    text = svc._metrics().text
    assert 'da_generator_accept_errors_total{errno="EMFILE"} 7' in text
    svc.loop_exception(loop, {"message": "something else", "exception": ValueError()})
    assert len(loop.defaults) == 1


@pytest.mark.parametrize("peer", ["same", "other", "unreadable"])
def test_only_a_peer_with_this_uid_is_served(peer):
    """#2901: SO_PEERCRED is read from the real socket; a different (or
    unreadable) uid is closed unanswered and counted. The uid is injected
    through the _peer_uid seam — the test does not switch uid."""
    import os
    import tempfile
    sdir = tempfile.mkdtemp(prefix="gsvcu", dir="/tmp")
    path = os.path.join(sdir, "s.sock")
    svc = _FakeService(delay=0)
    seen = []
    real = svc._peer_uid

    def peer_uid(sock):
        uid = real(sock)
        seen.append(uid)
        return {"same": uid, "other": uid + 1, "unreadable": None}[peer]

    svc._peer_uid = peer_uid

    async def scenario():
        svc.loop = asyncio.get_running_loop()
        server = await asyncio.start_unix_server(svc.handle, path=path)
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b"GET /v1/status HTTP/1.1\r\n\r\n")
            await writer.drain()
            try:
                data = await asyncio.wait_for(reader.read(), 10)
            except ConnectionResetError:
                data = b""  # closed with our request unread: no answer either way
            writer.close()
            return data
        finally:
            server.close()

    try:
        data = asyncio.run(scenario())
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)
    assert seen == [os.getuid()]  # the real SO_PEERCRED read
    if peer == "same":
        assert data.startswith(b"HTTP/1.1 200") and svc.metrics.peer_rejected == 0
    else:
        assert data == b"" and svc.metrics.peer_rejected == 1
        assert "da_generator_peer_rejected_total 1" in svc._metrics().text


def test_a_uid_at_or_above_2_31_is_read_unsigned():
    """#2901 O-a: struct ucred's uid is unsigned; a user-namespace uid such as
    3000000000 must read as itself (read signed, every peer was refused)."""
    import struct
    raw = struct.pack("iII", 4321, 3_000_000_000, 3_000_000_001)
    assert gs.ucred_uid(raw) == 3_000_000_000
    raw = struct.pack("iII", -1, 0, 0)
    assert gs.ucred_uid(raw) == 0


@pytest.mark.parametrize("err", ["EAGAIN", "ENOMEM", "EMFILE"])
def test_a_fork_that_fails_for_load_is_busy_and_leaks_no_descriptor(tmp_path, monkeypatch, err):
    """#2901: fork failing with EAGAIN / ENOMEM (like EMFILE) rejects that
    write as busy — not 503 — and the result pipe is closed."""
    import errno
    import os
    work = tmp_path / "work"
    gs._prepare_work_dir(str(work))

    class _Real(_FakeService):
        _judge = gs.Service._judge
        _spawn = gs.Service._spawn

        @staticmethod
        def _fork():
            code = getattr(errno, err)
            raise OSError(code, os.strerror(code))

    svc = _Real(delay=0)
    svc.s.work_dir = str(work)
    monkeypatch.setattr(gs.shutil, "which", lambda name: "/bin/true")
    before = len(os.listdir("/proc/self/fd"))

    async def scenario():
        svc.loop = asyncio.get_running_loop()
        resp = await svc.submit(_req("1" * 40, 25000), svc.loop.time())
        assert resp.status == 200, resp.doc
        assert _doc(resp)["reason"] == "busy" and err in _doc(resp)["message"]
    asyncio.run(scenario())
    assert len(os.listdir("/proc/self/fd")) <= before


# ── --config-subdir normalisation (CodeRabbit on #3015) ─────────────────

@pytest.mark.parametrize("given,expected", [
    (".", ""), ("", ""), ("./", ""), ("./conf.d", "conf.d"), ("conf.d/", "conf.d"),
    ("conf.d", "conf.d"), ("a//b/./c/", "a/b/c"),
])
def test_config_subdir_is_normalised_to_what_ls_tree_prints(given, expected):
    assert gs._normalize_config_subdir(given) == expected


@pytest.mark.parametrize("given", ["/abs", "/", "../x", "conf.d/../x", ".."])
def test_config_subdir_outside_the_repository_is_refused(given):
    with pytest.raises(RuntimeError, match="--config-subdir"):
        gs._normalize_config_subdir(given)
