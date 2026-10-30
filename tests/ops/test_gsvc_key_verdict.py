"""ADR-038 decision 5：產生器容器在產生器放行的寫入之後，對寫入涉及的租戶跑共用判決。

- 判決與對同一棵樹直接呼叫 `judge_tree(--tenant)` 相同，附在回應的
  `key_verdict`，結果（pass）不因判決改變；產生器擋下的寫入不跑判決。
- 判決與產生器共用同一次判定的期限，沒有自己的期限：剩的時間不夠就不開始
  （no_time），期限到了就停（deadline），記憶體不足（memory）、da-guard 失敗
  （error）、沒讀到根平台檔（undecidable）都回「未完成」與原因。
- 受影響的租戶是被寫的檔寫入前後宣告的租戶；不被選取的 `_` 檔不宣告租戶。
- 有另一筆寫入在排隊時，判決不讓位：照樣判完。
- error 級的 finding 一律送回；上限只略去 warning 與 notice。
- 判定程序裡的 served-values／effective 帶 GOTRACEBACK=crash，Go 的記憶體用盡是 memory。
- 判決期間的解析不經管線送回：訊號打斷時管線上不會留下半個 frame。
- metric 依結果計數。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import base64
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402

import _gsvc_child as child  # noqa: E402
import _gsvc_proto as proto  # noqa: E402
import _lib_key_verdict as kv  # noqa: E402
import _lib_parse_cache as pc  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="fork + pidfd + /proc: Linux only")

DEFAULTS = "defaults:\n  mysql_connections: 80\n"
TYPO = "tenants:\n  t-a:\n    mysql_conections: \"50\"\n"  # misspelt: no reader reads it


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gsvc-kv")
    bin_dir = tmp / "bin"
    h.stub(bin_dir, "amtool", h.ACCEPTING_AMTOOL)
    repo = h.init_repo(tmp / "repo")
    base = h.commit_tree(repo, {"_defaults.yaml": DEFAULTS,
                                "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"50\"\n",
                                "t-b.yaml": "tenants:\n  t-b:\n    mysql_connections: \"60\"\n"})
    svc = h.Service(tmp / "svc", repo, bin_dir=bin_dir)
    yield {"tmp": tmp, "bin": bin_dir, "repo": repo, "base": base, "svc": svc}
    svc.stop()


def _findings(verdict) -> list[dict]:
    return [{"severity": f.severity, "kind": f.kind, "key": f.key, "file": f.file,
             "tenants": list(f.tenants), "reason": f.reason} for f in verdict.findings]


def test_a_passed_write_carries_the_verdict_of_its_tenants(world, tmp_path):
    status, doc = world["svc"].validate(world["base"], [{"path": "t-a.yaml", "content_b64": h.b64(TYPO)}])
    assert status == 200, doc
    assert doc["result"] == "pass", doc  # the verdict never makes it anything else
    got = doc["key_verdict"]
    assert got["status"] == "complete", got
    assert got["tenants"] == 1
    tree = h.write_tree(tmp_path / "conf.d", {"_defaults.yaml": DEFAULTS, "t-a.yaml": TYPO,
                                             "t-b.yaml": "tenants:\n  t-b:\n    mysql_connections: \"60\"\n"})
    want = _findings(kv.judge_tree(tree, tenants=["t-a"]))
    assert got["findings"] == want
    assert any(f["key"] == "mysql_conections" for f in want), want  # the case judges something
    assert got["truncated"] == 0


def test_a_write_the_generator_refuses_gets_no_verdict(world, tmp_path):
    for fixture, files in sorted(cases.FIXTURES.items()):
        rc, _ = h.run_cli(h.write_tree(tmp_path / fixture, files), world["bin"], tmp_path / f"{fixture}.json")
        if rc == 0:
            continue
        repo_base = h.commit_tree(world["repo"], {})
        writes = [{"path": p, "content_b64": h.b64(c)} for p, c in sorted(files.items())]
        status, doc = world["svc"].validate(repo_base, writes)
        assert status == 200 and doc["result"] == "findings", doc
        assert "key_verdict" not in doc
        return
    pytest.fail("no findings fixture: the case tests nothing")


def test_the_metric_counts_verdicts_by_outcome(world):
    before = world["svc"].metrics().get('da_generator_key_verdicts_total{outcome="complete"}', 0)
    status, doc = world["svc"].validate(world["base"], [{"path": "t-b.yaml", "content_b64": h.b64(
        "tenants:\n  t-b:\n    mysql_connections: \"61\"\n")}])
    assert status == 200 and doc["key_verdict"]["status"] == "complete", doc
    after = world["svc"].metrics()
    assert after['da_generator_key_verdicts_total{outcome="complete"}'] == before + 1
    assert after["da_generator_key_verdict_seconds"] > 0
    # ADR-038 §5: the judgement's run time over its time budget (deadline_ms,
    # capped): a share of it, never 0 once a judgement ran.
    assert 0 < after["da_generator_judgement_deadline_ratio"] < 1


# ── the judgement deadline, in the judgement process ─────────────────────

def _job(tmp_path: Path, deadline_in: float, writes=()) -> child.ChildJob:
    req = proto.ValidateRequest("a" * 40, tuple(writes), 20000)
    return child.ChildJob(request=req, job_dir=str(tmp_path), repo=str(tmp_path), config_subdir="",
                          git="git", object_format="sha1", memory_limit=0, policy_copy=None,
                          policy_sha256=None, store_entries={}, max_transfer=0,
                          deadline_at=time.monotonic() + deadline_in if deadline_in else 0.0)


def _run(monkeypatch, tmp_path, deadline_in: float, judge, cache=None):
    monkeypatch.setattr(child, "written_scope", lambda *_a: ([], ["t-a"]))
    monkeypatch.setattr(kv, "judge_tree", judge)
    return child.key_verdict(_job(tmp_path, deadline_in), str(tmp_path), {}, cache or pc.ParseCache())


def test_too_little_time_left_is_not_started(monkeypatch, tmp_path):
    def judge(*_a, **_k):
        raise AssertionError("started with no time for it")
    for deadline_in in (0.0, child.VERDICT_RESERVE_SECONDS + child.VERDICT_MIN_SECONDS - 0.1):
        got = _run(monkeypatch, tmp_path, deadline_in, judge)
        assert (got["status"], got["reason"]) == ("not_completed", "no_time"), got


def test_the_deadline_running_out_stops_it_and_reports_it(monkeypatch, tmp_path):
    def slow(*_a, **_k):
        time.sleep(30)
    start = time.monotonic()
    got = _run(monkeypatch, tmp_path, child.VERDICT_RESERVE_SECONDS + 1.0, slow)
    assert (got["status"], got["reason"]) == ("not_completed", "deadline"), got
    assert time.monotonic() - start < 5, "the verdict ran past the judgement deadline"


def test_an_undecidable_tree_is_not_completed(monkeypatch, tmp_path):
    """ADR-038 §5/§7: no root platform file read — the tenants' threshold keys
    cannot be judged, and a retry changes nothing: not a complete verdict."""
    got = _run(monkeypatch, tmp_path, 15.0,
               lambda *_a, **_k: SimpleNamespace(findings=[], undecidable=True))
    assert (got["status"], got["reason"]) == ("not_completed", "undecidable"), got
    assert "root platform file" in got["message"]


def _finding(severity: str, n: int) -> kv.Finding:
    return kv.Finding(severity, "unread", f"k{n}", "t-a.yaml", ("t-a",), "unregistered")


@pytest.mark.parametrize("errors,others,sent_errors,sent_others", [
    (child.VERDICT_MAX_FINDINGS + 200, 5, child.VERDICT_MAX_FINDINGS + 200, 0),
    (10, child.VERDICT_MAX_FINDINGS + 500, 10, child.VERDICT_MAX_FINDINGS - 10),
    (3, 4, 3, 4),
])
def test_error_findings_are_never_truncated(monkeypatch, tmp_path, errors, others,
                                            sent_errors, sent_others):
    """ADR-038 §5: the write is refused on error findings, so every one is
    sent; the limit drops warnings and notices only, counted in truncated."""
    found = ([_finding(kv.WARNING, i) for i in range(others)]
             + [_finding(kv.ERROR, i) for i in range(errors)])
    got = _run(monkeypatch, tmp_path, 15.0,
               lambda *_a, **_k: SimpleNamespace(findings=found, undecidable=False))
    assert got["status"] == "complete", got
    sev = [f["severity"] for f in got["findings"]]
    assert sev.count(kv.ERROR) == sent_errors
    assert sev.count(kv.WARNING) == sent_others
    assert got["truncated"] == others - sent_others


def _gotraceback_da_guard(tmp_path: Path) -> tuple[Path, Path]:
    seen = tmp_path / "gotraceback"
    fake = tmp_path / "fake-da-guard"
    fake.write_text("#!/bin/sh\nprintf '%s' \"${GOTRACEBACK-unset}\" > "
                    f"'{seen}'\nkill -ABRT $$\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake, seen


@pytest.mark.parametrize("limited,want", [(True, "crash"), (False, "unset")])
def test_served_values_runs_with_gotraceback_crash_under_the_memory_limit(
        monkeypatch, tmp_path, limited, want):
    """The judgement's served-values and effective (the verdict's da-guard
    runs) take parse-tree's environment: under the memory limit the Go
    runtime's own out-of-memory ends them by a signal, which the verdict
    reports as memory — not exit 2, reported as error."""
    import _lib_parse_tree as lpt
    monkeypatch.setattr(lpt, "_MEMORY_LIMITED", limited)
    fake, seen = _gotraceback_da_guard(tmp_path)
    missing = tmp_path / "no-such-conf.d"  # not a directory: no parse-tree run first
    for load in (tv.load_served_tree, tv.load_effective_tree):
        seen.unlink(missing_ok=True)
        with pytest.raises(tv.DaGuardError) as e:
            load(str(missing), binary=str(fake), timeout=30)
        assert e.value.returncode == -signal.SIGABRT
        assert seen.read_text(encoding="utf-8") == want


def test_the_verdicts_parses_are_not_streamed(monkeypatch, tmp_path):
    sent = []
    cache = pc.ParseCache(on_store=lambda k, v: sent.append(k))

    def storing(*_a, **_k):
        pc.active().store(("k" * 64, "mode"), {"tenants": {}})
        return SimpleNamespace(findings=[], undecidable=False)
    _run(monkeypatch, tmp_path, 15.0, storing, cache)
    assert sent == [], "a verdict entry went down the pipe"
    assert cache.on_store is not None, "the generator's streaming is not restored"


@pytest.mark.parametrize("exc,reason", [
    (MemoryError("cannot allocate"), "memory"),
    (SystemExit(3), "error"),
    (KeyboardInterrupt(), "error"),
    (tv.ServedValuesError("da-guard served-values exited -6", -6, ""), "memory"),
    (tv.EffectiveError("da-guard effective exited 2", 2, ""), "error"),
    (ValueError("anything else"), "error"),
])
def test_a_verdict_that_fails_is_not_completed_never_raised(monkeypatch, tmp_path, exc, reason):
    def fail(*_a, **_k):
        raise exc
    got = _run(monkeypatch, tmp_path, 15.0, fail)
    assert (got["status"], got["reason"]) == ("not_completed", reason), got
    assert got["message"]


# ── which tenants a write touches ────────────────────────────────────────

def _write(path: str, text: str | None) -> proto.Write:
    return proto.Write(path, None if text is None else base64.b64encode(text.encode()).decode())


def _scope(tmp_path, files: dict, base: dict, writes: list):
    tree = h.write_tree(tmp_path / "conf.d", files)
    pc.install(pc.ParseCache())
    try:
        return child.written_scope(_job(tmp_path, 0.0, writes), str(tree), base,
                                   pc.ConfdReader(str(tree)))
    finally:
        pc.install(None)


def test_the_tenants_are_those_declared_before_and_after(tmp_path):
    got = _scope(tmp_path, {
        "_defaults.yaml": DEFAULTS,
        "x.yaml": "tenants:\n  t-b:\n    mysql_connections: \"5\"\n",   # after: t-b
        "_groups.yaml": "groups: {}\n",
        # a root platform file's tenants: block is those tenants' platform default
        "_platform.yaml": "tenants:\n  t-d:\n    mysql_connections: \"9\"\n"},
        {"x.yaml": b"tenants:\n  t-a:\n    mysql_connections: \"5\"\n",  # before: t-a
         "gone.yaml": b"tenants:\n  t-c:\n    mysql_connections: \"5\"\n"},
        [_write("x.yaml", "unused"), _write("gone.yaml", None),
         _write("_groups.yaml", "groups: {}\n"), _write("_platform.yaml", "unused")])
    assert got == ([], ["t-a", "t-b", "t-c", "t-d"])


@pytest.mark.parametrize("files,base,write", [
    # a defaults file below the root reaches every tenant below it
    ({"_defaults.yaml": DEFAULTS, "sub/_defaults.yaml": DEFAULTS, "sub/t-a.yaml": TYPO},
     {}, ("sub/_defaults.yaml", "unused")),
    # a root platform file with profiles reaches every tenant bound to one
    ({"_defaults.yaml": DEFAULTS, "_profiles.yaml": "profiles:\n  gold:\n    mysql_connections: 9\n"},
     {}, ("_profiles.yaml", "unused")),
    # ... and so does one that had profiles before the write
    ({"_defaults.yaml": DEFAULTS, "_profiles.yaml": "profiles: {}\n"},
     {"_profiles.yaml": b"profiles:\n  gold:\n    mysql_connections: 9\n"}, ("_profiles.yaml", "unused")),
    # a deleted defaults file, by its name
    ({"_defaults.yaml": DEFAULTS, "sub/t-a.yaml": TYPO},
     {"sub/_defaults.yaml": DEFAULTS.encode()}, ("sub/_defaults.yaml", None)),
])
def test_a_platform_write_is_named_not_judged(tmp_path, files, base, write):
    platform, _tenants = _scope(tmp_path, files, base, [_write(*write)])
    assert platform == [write[0]]


def test_a_platform_write_is_not_completed_in_the_service(world):
    status, doc = world["svc"].validate(world["base"], [{"path": "sub/_defaults.yaml", "content_b64": h.b64(DEFAULTS)}])
    assert status == 200 and doc["result"] == "pass", doc
    assert (doc["key_verdict"]["status"], doc["key_verdict"]["reason"]) == ("not_completed", "platform_file")


def test_a_groups_write_judges_no_tenant(world):
    status, doc = world["svc"].validate(world["base"], [{"path": "_groups.yaml", "content_b64": h.b64("groups: {}\n")}])
    assert status == 200 and doc["result"] == "pass", doc
    assert doc["key_verdict"] == {**doc["key_verdict"], "status": "complete", "tenants": 0, "findings": []}


def test_a_hidden_file_declares_nothing(tmp_path):
    got = _scope(tmp_path, {"_defaults.yaml": DEFAULTS},
                 {".hidden/t-g.yaml": b"tenants:\n  t-ghost:\n    mysql_connections: \"5\"\n"},
                 [_write(".hidden/t-g.yaml", None)])
    assert got == ([], [])


def test_a_waiting_write_does_not_stop_the_verdict(world, tmp_path):
    """ADR-038 §5: the verdict does not yield to a waiting write — it
    completes, and the waiting write is judged after it."""
    marker = tmp_path / "in-verdict"
    prelude = (
        "import _lib_key_verdict as _k, os as _o, time as _t\n"
        "_real = _k.judge_tree\n"
        "def _slow(*a, **k):\n"  # the first verdict waits a little; later ones do not
        f"    if not _o.path.exists({str(marker)!r}):\n"
        f"        open({str(marker)!r}, 'w').close()\n"
        "        _t.sleep(3)\n"
        "    return _real(*a, **k)\n"
        "_k.judge_tree = _slow\n")
    with h.Service(tmp_path / "svc", world["repo"], bin_dir=world["bin"], prelude=prelude) as svc:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(svc.validate, world["base"], [{"path": "t-a.yaml", "content_b64": h.b64(TYPO)}])
            end = time.monotonic() + 60
            while not marker.exists():
                assert time.monotonic() < end, "the verdict never started"
                time.sleep(0.05)
            second = pool.submit(svc.validate, world["base"], [{"path": "t-b.yaml", "content_b64": h.b64(
                "tenants:\n  t-b:\n    mysql_connections: \"62\"\n")}])
            status, doc = first.result(timeout=120)
            assert status == 200 and doc["result"] == "pass", doc
            assert doc["key_verdict"]["status"] == "complete", doc["key_verdict"]
            assert any(f["key"] == "mysql_conections" for f in doc["key_verdict"]["findings"])
            status2, doc2 = second.result(timeout=120)
            assert status2 == 200 and doc2["result"] == "pass", doc2
            assert doc2["key_verdict"]["status"] == "complete", doc2["key_verdict"]
