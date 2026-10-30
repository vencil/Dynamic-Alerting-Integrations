"""#2901（ADR-036 §3、§5、§6）：產生器容器的服務程式，真的起服務、走 Unix
socket 打請求。

- 判定：寫入後的整棵樹以服務判出的 rc 與 findings，與 CLI
  ``generate-routes --validate --strict`` 對同一棵樹跑出的相同（每個 findings
  fixture 各一筆，含會擋下的）。
- 拒收該筆（HTTP 200、``result: rejected``）：期限（判定中到期時子孫程序一併
  結束）、排隊中到期、佇列滿、記憶體上限、沒有完整結果、git 讀兩次都失敗。
- 容器不可用（503）：找不到 amtool、產生器比 policy 要求的舊、部署端 policy
  檔不可用；每次判定都重查。
- git 讀取失敗重試一次；只實體化一般檔（100644／100755），symlink、gitlink
  等其他 mode 一律判 not_regular_file（#2981）。
- 快取：同一棵樹只改一個檔時，只解析那個檔。
- fork 時常駐程序只有一個執行緒；socket 權限 0600；HTTP 大小與格式錯誤。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

import _gsvc_child  # noqa: E402
import _gsvc_proto  # noqa: E402
import _lib_parse_cache as pc  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="fork + pidfd + /proc: Linux only")

FIXTURES = sorted(cases.FIXTURES)
EXTRA = cases._tenant("zz-extra")


def _split(fixture: str) -> tuple[dict, str]:
    """(base files, the file sent as a write) for *fixture*."""
    files = dict(cases.FIXTURES[fixture])
    last = sorted(files)[-1]
    del files[last]
    return files, last


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gsvc")
    bin_dir = tmp / "bin"
    h.stub(bin_dir, "amtool", h.ACCEPTING_AMTOOL)
    repo = h.init_repo(tmp / "repo")
    commits = {}
    for fx in FIXTURES:
        base, _write = _split(fx)
        commits[fx] = h.commit_tree(repo, base)
        commits[fx + "+extra"] = h.commit_tree(
            repo, {**cases.FIXTURES[fx], "zz-extra.yaml": EXTRA})
    fork_log = tmp / "forks.log"
    prelude = (
        "import threading\n"
        "_real_fork = os.fork\n"
        "def _logged_fork():\n"
        f"    with open({str(fork_log)!r}, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(f'{threading.active_count()}\\n')\n"
        "    return _real_fork()\n"
        "os.fork = _logged_fork\n")
    svc = h.Service(tmp / "svc", repo, bin_dir=bin_dir, prelude=prelude)
    yield {"tmp": tmp, "bin": bin_dir, "repo": repo, "commits": commits,
           "svc": svc, "fork_log": fork_log}
    svc.stop()


def _jobs_empty(svc, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not list((svc.work / "jobs").iterdir()):
            return True
        time.sleep(0.05)
    return False


def _until(pred, what: str, timeout: float = 60.0) -> None:
    """Wait for a state instead of sleeping for it: a fixed sleep is a bet on
    the host's speed (#3015)."""
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"never reached: {what}")
        time.sleep(0.05)


def _norm(doc: dict, *dirs: Path) -> tuple:
    text = json.dumps(doc["findings"], sort_keys=True)
    for d in dirs:
        text = text.replace(str(d), "<DIR>")
    return doc["exit_code"], text


# ── verdicts equal the CLI's ─────────────────────────────────────────────

@pytest.mark.parametrize("fixture", FIXTURES)
def test_verdict_equals_the_cli_on_the_written_tree(world, tmp_path, fixture):
    svc = world["svc"]
    _base, write = _split(fixture)
    status, doc = svc.validate(world["commits"][fixture], [
        {"path": write, "content_b64": h.b64(cases.FIXTURES[fixture][write])}])
    assert status == 200, doc
    tree = h.write_tree(tmp_path / "conf.d", cases.FIXTURES[fixture])
    rc, cli = h.run_cli(tree, world["bin"], tmp_path / "f.json")
    assert doc["rc"] == rc
    assert doc["result"] == ("pass" if rc == 0 else "findings")
    # ADR-038 §5: the CLI's `--validate` also prints the shared key verdict
    # (`key_*`, `blocks: never`); the container does not run it inside the
    # generator — it judges the keys itself, in `key_verdict`.
    assert not [r for r in doc["findings"]["findings"] if r["kind"].startswith("key_")]
    cli = dict(cli, findings=[r for r in cli["findings"] if not r["kind"].startswith("key_")])
    assert _norm(doc["findings"], Path(doc["findings"]["config_dir"])) \
        == _norm(cli, tree)
    assert doc["validated"] == {
        "base_commit": world["commits"][fixture], "policy_sha256": None,
        "files": [{"path": write, "sha256": hashlib.sha256(
            cases.FIXTURES[fixture][write].encode()).hexdigest()}]}


def test_the_fixtures_include_both_verdicts(world, tmp_path):
    rcs = set()
    for fixture in FIXTURES:
        tree = h.write_tree(tmp_path / fixture, cases.FIXTURES[fixture])
        rcs.add(h.run_cli(tree, world["bin"], tmp_path / f"{fixture}.json")[0] == 0)
    assert rcs == {True, False}


@pytest.mark.parametrize("fixture", ["clean", "policy", "duplicate"])
def test_a_delete_is_applied(world, tmp_path, fixture):
    svc = world["svc"]
    status, doc = svc.validate(world["commits"][fixture + "+extra"],
                               [{"path": "zz-extra.yaml", "delete": True}])
    assert status == 200
    tree = h.write_tree(tmp_path / "conf.d", cases.FIXTURES[fixture])
    rc, cli = h.run_cli(tree, world["bin"], tmp_path / "f.json")
    assert (doc["rc"], _norm(doc["findings"], Path(doc["findings"]["config_dir"]))) \
        == (rc, _norm(cli, tree))
    assert doc["validated"]["files"] == [{"path": "zz-extra.yaml", "deleted": True}]


def test_a_write_cannot_go_through_a_symlink_or_file(world, tmp_path):
    repo = h.init_repo(tmp_path / "r")
    (repo / "conf.d").mkdir()
    (repo / "conf.d" / "alpha.yaml").write_text(cases._tenant("alpha"), encoding="utf-8")
    symlink_or_skip("/tmp", repo / "conf.d" / "out")
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        name = f"gsvc-escape-{uuid.uuid4().hex}"
        for path in (f"out/{name}", f"alpha.yaml/{name}"):
            status, doc = svc.validate(commit, [{"path": path, "content_b64": h.b64("x")}])
            assert status == 400 and "not a directory" in doc["message"], doc
        assert not os.path.exists(f"/tmp/{name}")


# ── the parse cache ──────────────────────────────────────────────────────

def test_changing_one_file_parses_only_that_file(world, tmp_path, monkeypatch):
    svc = world["svc"]
    commit = world["commits"]["clean+extra"]
    svc.validate(commit)  # base tree cached (and pinned)
    body = EXTRA + f"# {uuid.uuid4().hex}\n"  # content nothing has parsed yet
    status, doc = svc.validate(commit, [{"path": "zz-extra.yaml",
                                         "content_b64": h.b64(body)}])
    assert status == 200 and doc["result"] == "pass", doc
    # how many modes read that file: count it in-process on the same tree
    tree = h.write_tree(tmp_path / "conf.d",
                        {**cases.FIXTURES["clean"], "zz-extra.yaml": body})
    monkeypatch.setenv("PATH", os.devnull)
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        with pytest.raises(SystemExit):
            _gsvc_child.gar.main(["--config-dir", str(tree), "--validate",
                                  "--strict", "--findings-json",
                                  str(tmp_path / "f.json")])
    finally:
        pc.install(None)
    blob = pc.content_key("zz-extra.yaml", body.encode())
    modes = {m for (b, m) in cache.new if b == blob}
    assert len(modes) >= 2
    assert doc["cache"]["parses"] == len(modes)
    assert doc["cache"]["stored"] == len(modes)


def test_cache_metrics_and_pinning(world):
    svc = world["svc"]
    svc.validate(world["commits"]["clean+extra"])
    m = svc.metrics()
    assert m['da_generator_cache_bytes{pinned="true"}'] > 0
    assert m["da_generator_cache_entries"] > 0
    assert m["da_generator_judgement_peak_rss_bytes"] > 0
    assert m["da_generator_judgement_memory_limit_bytes"] == 192 * 1024 * 1024


# ── one thread at fork, cleanup ──────────────────────────────────────────

def test_the_service_forks_with_one_thread(world):
    world["svc"].validate(world["commits"]["clean"])
    counts = world["fork_log"].read_text(encoding="utf-8").split()
    assert counts and set(counts) == {"1"}


def test_job_directories_are_removed(world):
    world["svc"].validate(world["commits"]["clean"])
    assert _jobs_empty(world["svc"])


def test_socket_is_0600(world):
    mode = os.stat(world["svc"].socket).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600


# ── HTTP ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("request_bytes,status", [
    # Content-Length present too: refused for the Transfer-Encoding itself
    (b"POST /v1/validate HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n0\r\n\r\n", 411),
    (b"POST /v1/validate HTTP/1.1\r\nHost: g\r\n\r\n", 411),
    (b"POST /v1/validate HTTP/1.1\r\nContent-Length: abc\r\n\r\n", 400),
    (b"POST /v1/validate HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx", 400),
    (b"POST /v1/validate HTTP/1.1\r\nContent-Length: 999999999\r\n\r\n", 413),
    (b"POST /v1/validate HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc", 400),
    (b"GET /v1/validate HTTP/1.1\r\n\r\n", 405),
    (b"POST /metrics HTTP/1.1\r\nContent-Length: 0\r\n\r\n", 405),
    (b"GET /nope HTTP/1.1\r\n\r\n", 404),
    (b"GET /v1/status?x=1 HTTP/1.1\r\n\r\n", 404),
    (b"BROKEN\r\n\r\n", 400),
    (b"GET /v1/status HTTP/1.1\r\nX-Big: " + b"a" * 20000 + b"\r\n\r\n", 431),
])
def test_http_errors(world, request_bytes, status):
    got, headers, _body = world["svc"].raw(request_bytes)
    assert got == status
    assert headers["connection"] == "close"


def test_bad_request_body_is_400(world):
    status, doc = world["svc"].validate("main")
    assert status == 400 and doc["error"] == "BAD_REQUEST"


# ── deadline ─────────────────────────────────────────────────────────────

# PATH holds only the stub directory: sleep is called by its full path.
SLEEP = shutil.which("sleep") or "/bin/sleep"
_SLOW_AMTOOL = f"""#!/bin/sh
echo $$ > "$GSVC_PID_DIR/amtool.pid"
{SLEEP} 60 &
echo $! > "$GSVC_PID_DIR/grandchild.pid"
wait
"""


# One amtool call per judgement (check-config): call n records ``started-n``
# and blocks until the test creates ``release-n`` (or ``release-all``). A
# judgement held here ends when the TEST says, not when a sleep runs out.
# Builtins and full paths only: PATH holds just the stub directory.
_GATED_AMTOOL = f"""#!/bin/sh
d="$GSVC_GATE_DIR"
n=0
while [ -e "$d/started-$n" ]; do n=$((n+1)); done
: > "$d/started-$n"
while [ ! -e "$d/release-$n" ] && [ ! -e "$d/release-all" ]; do {SLEEP} 0.05; done
exit 0
"""


def _gate(tmp_path: Path) -> tuple[Path, Path]:
    """(bin directory with the gated amtool, the gate directory)."""
    bin_dir, gate = tmp_path / "bin", tmp_path / "gate"
    h.stub(bin_dir, "amtool", _GATED_AMTOOL)
    gate.mkdir()
    return bin_dir, gate


def test_deadline_ends_the_judgement_and_its_descendants(tmp_path, world):
    """The deadline (8 s here) is what ends a judgement stuck in amtool (60 s),
    with amtool's own child. Asserted only what the deadline guarantees: not
    answered before it, answered long before amtool would end, nothing left.
    The 8 s is room to REACH amtool on a slow host (the pid files say so)."""
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", _SLOW_AMTOOL)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   env={"GSVC_PID_DIR": str(pid_dir)}) as svc:
        t0 = time.monotonic()
        status, doc = svc.validate(world["commits"]["clean+extra"], deadline_ms=13000)
        took = time.monotonic() - t0
        assert status == 200
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        # the clock starts after t0, so it cannot have ended sooner; and the
        # answer did not wait for amtool's 60 s
        assert 8.0 <= took < 50, took
        assert (pid_dir / "grandchild.pid").exists(), \
            "the judgement never reached amtool within its 8 s: nothing was tested"
        pids = [int((pid_dir / n).read_text().strip())
                for n in ("amtool.pid", "grandchild.pid")]
        end = time.monotonic() + 5
        while any(h.alive(p) for p in pids) and time.monotonic() < end:
            time.sleep(0.05)
        assert not any(h.alive(p) for p in pids), pids
        # the warming run the new base schedules (E2) has its own directory
        # until it ends; then nothing is left
        assert _jobs_empty(svc)
        assert svc.metrics()['da_generator_rejections_total{reason="deadline"}'] == 1


def test_queue_expiry_and_busy(tmp_path, world):
    """A queued request whose deadline passes is answered then, without
    waiting for the running one; a full queue answers busy. The running
    judgement is held in amtool until the test releases it, so "the first is
    still running" is a fact, not a sleep's guess."""
    bin_dir, gate = _gate(tmp_path)
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   env={"GSVC_GATE_DIR": str(gate)},
                   args=("--max-queue", "1")) as svc, \
            ThreadPoolExecutor(4) as pool:
        first = pool.submit(svc.validate, commit)
        _until(lambda: (gate / "started-0").exists(), "the first judgement in amtool")
        expiring = pool.submit(svc.validate, commit, None, 10000)  # 5 s
        _until(lambda: svc.metrics()["da_generator_queue_depth"] == 1, "one queued")
        status, busy = svc.validate(commit)  # queue (1) is full
        assert status == 200 and (busy["result"], busy["reason"]) == ("rejected", "busy")
        status, exp = expiring.result()
        assert (exp["result"], exp["reason"]) == ("rejected", "deadline")
        assert not first.done()  # answered without waiting for the first
        (gate / "release-all").touch()
        assert first.result()[1]["result"] == "pass"
        # the next one is judged as usual
        assert svc.validate(commit)[1]["result"] == "pass"


# ── memory, incomplete ───────────────────────────────────────────────────

def test_memory_limit_rejects_that_write_only(tmp_path, world):
    """ADR-036 step 4 (#2906): the file is read by `da-guard parse-tree`,
    which inherits the judgement's RLIMIT_DATA; the Go runtime's out of
    memory is the judgement's memory limit too. 192 MiB (the default) runs
    da-guard on the base tree; 64 MiB would not even start the Go runtime."""
    big = "tenants:\n  big:\n    x: [" + ",".join(["0"] * 400000) + "]\n"
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--memory-limit-bytes", str(192 * 1024 * 1024))) as svc:
        status, doc = svc.validate(commit, [{"path": "big.yaml",
                                             "content_b64": h.b64(big)}])
        assert status == 200 and (doc["result"], doc.get("reason")) == ("rejected", "memory"), doc
        assert svc.validate(commit)[1]["result"] == "pass"
        m = svc.metrics()
        assert m['da_generator_rejections_total{reason="memory"}'] == 1
        assert m["da_generator_judgement_memory_limit_bytes"] == 192 * 1024 * 1024


_KILL = "os.kill(os.getpid(), __import__('signal').SIGKILL)"


@pytest.mark.parametrize("stage,action", [
    ("before_result", "os._exit(0)"),  # exit 0, nothing sent
    ("before_result", _KILL),          # killed by a signal, nothing sent
    ("after_result", "os._exit(0)"),   # exit 0, the result but no end frame
    ("after_end", _KILL),              # every frame sent, then killed
])
def test_no_complete_result_is_incomplete(tmp_path, world, stage, action):
    prelude = ("import _gsvc_child\n"
               f"_gsvc_child.TEST_HOOK = lambda s: {action} if s == {stage!r} else None\n")
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   prelude=prelude) as svc:
        status, doc = svc.validate(world["commits"]["clean"])
        assert status == 200 and (doc["result"], doc["reason"]) == ("rejected", "incomplete")


_FAKE_GENERATOR = """
import json, _gsvc_child
def _fake(argv, **_kw):
    path = argv[argv.index("--findings-json") + 1]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({{"exit_code": {doc_rc}, "findings": []}}, fh)
    raise SystemExit({rc})
_gsvc_child.gar.main = _fake
"""


@pytest.mark.parametrize("doc_rc,rc,want", [
    (0, 1, "incomplete"),   # the document does not belong to this exit
    (1, 1, "findings"),     # control
])
def test_a_findings_document_that_disagrees_with_the_exit_is_incomplete(
        tmp_path, world, doc_rc, rc, want):
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   prelude=_FAKE_GENERATOR.format(doc_rc=doc_rc, rc=rc)) as svc:
        status, doc = svc.validate(world["commits"]["clean"])
        got = doc["reason"] if doc["result"] == "rejected" else doc["result"]
        assert status == 200 and got == want, doc


# ── git ──────────────────────────────────────────────────────────────────

_FLAKY_GIT = """#!/bin/sh
if [ "$3" = "ls-tree" ]; then
  if [ "$GSVC_GIT_MODE" = always ] || [ ! -e "$GSVC_GIT_STATE" ]; then
    : > "$GSVC_GIT_STATE"
    echo "fatal: injected failure" >&2
    exit 128
  fi
fi
exec {git} "$@"
"""


@pytest.mark.parametrize("mode,want", [("once", "pass"), ("always", "git_read")])
def test_git_read_is_retried_once(tmp_path, world, mode, want):
    git = h.stub(tmp_path / "gitbin", "git", _FLAKY_GIT.format(git=h.GIT))
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--git", str(git)),
                   env={"GSVC_GIT_MODE": mode,
                        "GSVC_GIT_STATE": str(tmp_path / "failed-once")}) as svc:
        status, doc = svc.validate(world["commits"]["clean"])
        assert status == 200
        got = doc["result"] if doc["result"] != "rejected" else doc["reason"]
        assert got == want, doc
        assert "git read attempt 1 failed" in doc["stderr_tail"]
        if mode == "always":
            assert "git read attempt 2 failed" in doc["stderr_tail"]


def test_a_da_guard_failing_on_names_is_not_a_git_read(tmp_path):
    """#3206: which component of a not-regular git entry is hidden is asked
    of da-guard's names mode. When that run fails, the reply says so — an
    incomplete judgement naming da-guard — not "reading the base commit from
    git failed" (the git read succeeded)."""
    import stat
    real = provide_da_guard_path()
    wrapper = tmp_path / "da-guard-no-names"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = "--names" ] && { echo "names: boom" >&2; exit 1; }; done\n'
        f'exec "{real}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    repo = h.init_repo(tmp_path / "r")
    d = repo / "conf.d"
    d.mkdir(parents=True)
    (d / "alpha.yaml").write_text(cases._tenant("alpha"), encoding="utf-8")
    symlink_or_skip("alpha.yaml", d / "link.yml")
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", h.ACCEPTING_AMTOOL)
    with h.Service(tmp_path / "s", repo, bin_dir=bin_dir,
                   env={"DA_GUARD_BINARY": str(wrapper)}) as svc:
        status, doc = svc.validate(commit)
    assert status == 200 and doc["result"] == "rejected", doc
    assert doc["reason"] == "incomplete", doc
    assert "da-guard parse-tree could not classify" in doc["message"], doc
    assert "git" not in doc["message"].split("da-guard")[0], doc
    # da-guard's own words reach the reply, not only the prefix.
    assert "boom" in doc["message"], doc


def provide_da_guard_path() -> str:
    real = os.environ.get("DA_GUARD_BINARY")
    if not real:
        pytest.skip("needs $DA_GUARD_BINARY (the session build sets it)")
    return real


def test_tree_entries_are_written_by_mode(tmp_path):
    """100644 / 100755 files are written; a 120000 symlink and a 160000
    gitlink are not — they come back as not regular files (#2981), except a
    hidden one, which no reader reads."""
    repo = h.init_repo(tmp_path / "r")
    d = repo / "conf.d"
    (d / "sub").mkdir(parents=True)
    (d / "alpha.yaml").write_text(cases._tenant("alpha"), encoding="utf-8")
    (d / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (d / "run.sh").chmod(0o755)
    symlink_or_skip("alpha.yaml", d / "link.yml")
    symlink_or_skip("../../nowhere", d / "sub" / "dangling.yaml")
    symlink_or_skip("alpha.yaml", d / ".hidden.yaml")
    h.run_git(repo, "add", "-A")
    other = h.run_git(repo, "hash-object", "-t", "commit", "--stdin", "-w",
                      stdin=b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
                            b"author a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nx\n")
    h.run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{other},conf.d/vendor")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job = _gsvc_child.ChildJob(
        request=_gsvc_proto.ValidateRequest(commit, (), 25000),
        job_dir=str(job_dir), repo=str(repo), config_subdir="conf.d", git=h.GIT,
        object_format="sha1", memory_limit=0, policy_copy=None,
        policy_sha256=None, store_entries={}, max_transfer=0)
    tree = _gsvc_child.read_tree(job)
    modes = {rel: mode for mode, _o, rel, _c in tree}
    assert modes == {"alpha.yaml": "100644", "run.sh": "100755",
                     "link.yml": "120000", "sub/dangling.yaml": "120000",
                     ".hidden.yaml": "120000", "vendor": "160000"}
    root = job_dir / "conf.d"
    irregular = _gsvc_child.materialise(str(root), tree)
    assert irregular == {"link.yml": "120000", "sub/dangling.yaml": "120000",
                         "vendor": "160000"}
    assert (root / "alpha.yaml").read_text(encoding="utf-8") == cases._tenant("alpha")
    assert (root / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\n"
    for rel in ("link.yml", "sub/dangling.yaml", ".hidden.yaml", "vendor"):
        assert not os.path.lexists(root / rel), rel
    # read_tree's object ids are git's own blob ids (pinning uses content
    # SHA-256, computed from these contents)
    listed = [line.split()[:3] for line in
              h.run_git(repo, "ls-tree", "-r", commit, "--", "conf.d").splitlines()]
    assert {o for m, o, _r, _c in tree if m != "160000"} == {
        oid for _mode, kind, oid in listed if kind == "blob"}


def _unreadable(doc: dict) -> dict:
    return {f["file"]: f["message"] for f in doc["findings"]["findings"]
            if f["kind"] == "tenant_file_unreadable"}


def test_a_symlink_or_gitlink_entry_is_not_a_regular_file(tmp_path, world):
    """#2981 (ADR-036 §3): read from git objects, a symlink (whatever its
    link text — here one that would parse as a mapping, and one to a real
    file) and a gitlink are judged not_regular_file, blocking, with the fix;
    a filesystem read of the same tree follows the link and passes."""
    repo = h.init_repo(tmp_path / "r")
    files = dict(cases.FIXTURES["clean"])
    files["zz-target.txt"] = cases._tenant("db-z")  # not config-named: no reader reads it
    commit_files = h.write_tree(repo / "conf.d", files)
    symlink_or_skip("db-x: a", commit_files / "zz-maplike.yaml")
    symlink_or_skip("zz-target.txt", commit_files / "zz-real.yaml")
    symlink_or_skip("_defaults.yaml", commit_files / ".zz-hidden.yaml")
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "update-index", "--add", "--cacheinfo",
              f"160000,{'1' * 40},conf.d/vendor")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        status, doc = svc.validate(commit)
        assert status == 200 and doc["result"] == "findings" and doc["rc"] == 1, doc
        got = _unreadable(doc)
        assert set(got) == {"zz-maplike.yaml", "zz-real.yaml", "vendor"}, got
        for rel, mode, what in (("zz-maplike.yaml", "120000", "a symlink"),
                                ("zz-real.yaml", "120000", "a symlink"),
                                ("vendor", "160000", "a gitlink (submodule)")):
            assert (f"is not a regular file (not_regular_file): git tree mode {mode}, {what}"
                    in got[rel]), got[rel]
            assert f"commit {rel} as a regular file" in got[rel], got[rel]
        # tenant-api writes a regular file over the link: that path is no
        # longer judged as one; a write below a link is refused.
        status, doc = svc.validate(commit, [
            {"path": "zz-maplike.yaml", "content_b64": h.b64(cases._tenant("db-y"))}])
        assert status == 200 and set(_unreadable(doc)) == {"zz-real.yaml", "vendor"}, doc
        status, doc = svc.validate(commit, [{"path": "zz-real.yaml/x.yaml",
                                             "content_b64": h.b64("x")}])
        assert status == 400 and "not a directory" in doc["message"], doc
    # The CLI over a real directory follows the file symlinks (a ConfigMap
    # mount is made of them): the same tree without the link texts passes.
    tree = h.write_tree(tmp_path / "cli" / "conf.d", files)
    symlink_or_skip("zz-target.txt", tree / "zz-real.yaml")
    rc, cli = h.run_cli(tree, world["bin"], tmp_path / "f.json")
    assert rc == 0 and not [f for f in cli["findings"] if f["kind"] == "tenant_file_unreadable"], cli


def test_an_irregular_entry_occupies_its_path_for_writes(tmp_path, world):
    """#2981 (ADR-036 §3): an entry that is not a regular file is still the
    tree entry at its path. Writes see it in request order: below it is 400
    (however deep, a directory symlink too) until a write or delete of that
    path takes it out; the directory holding it is a directory, so writing
    a file there is 400 as for a regular file. Any name is judged (a
    README.md too), unless the path is hidden."""
    repo = h.init_repo(tmp_path / "r")
    files = dict(cases.FIXTURES["clean"])
    files["zz-target.txt"] = "not config\n"
    files["zz-dir/zz-note.txt"] = "not config\n"
    commit_files = h.write_tree(repo / "conf.d", files)
    symlink_or_skip("zz-dir", commit_files / "zz-dirlink")
    symlink_or_skip("nowhere", commit_files / "zz-a")
    symlink_or_skip("zz-target.txt", commit_files / "README.md")
    (commit_files / "zz-d").mkdir()
    symlink_or_skip("../zz-target.txt", commit_files / "zz-d" / "s.yaml")
    (commit_files / ".x").mkdir()
    symlink_or_skip("../zz-target.txt", commit_files / ".x" / "l.yaml")
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    tenant = {"content_b64": h.b64(cases._tenant("db-q"))}
    links = {"zz-dirlink", "zz-a", "README.md", "zz-d/s.yaml"}
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        status, doc = svc.validate(commit)
        assert status == 200 and set(_unreadable(doc)) == links, doc
        for writes, why in (
                ([{"path": "zz-dirlink/x.yaml", **tenant}], "not a directory"),
                ([{"path": "zz-a/b/c/d.yaml", **tenant}], "not a directory"),
                ([{"path": "zz-a/x.yaml", **tenant}, {"path": "zz-a", "delete": True}],
                 "not a directory"),
                ([{"path": "zz-d", **tenant}], "is a directory")):
            status, doc = svc.validate(commit, writes)
            assert status == 400 and why in doc["message"], (writes, doc)
        status, doc = svc.validate(commit, [{"path": "zz-a", "delete": True},
                                            {"path": "zz-a/x.yaml", **tenant}])
        assert status == 200 and set(_unreadable(doc)) == links - {"zz-a"}, doc
        # Writing a file over it does not make it a directory: below it is
        # still a 400 — only deleting it does.
        status, doc = svc.validate(commit, [{"path": "zz-a", **tenant},
                                            {"path": "zz-a/x.yaml", **tenant}])
        assert status == 400 and "not a directory" in doc["message"], doc
        # A hidden path is no tree entry any reader reads: not judged, and
        # nothing stops a write below it.
        status, doc = svc.validate(commit, [{"path": ".x/l.yaml/y.yaml", **tenant}])
        assert status == 200 and set(_unreadable(doc)) == links, doc


def test_an_irregular_policy_file_is_an_unusable_policy(tmp_path, world):
    """#2981 (ADR-036 §3): an irregular entry is classified as any other
    file that cannot be read — by its name. A `_domain_policy.yaml` symlink
    is domain_policy_unusable (not tenant_file_unreadable), and it refuses
    the write all the same."""
    repo = h.init_repo(tmp_path / "r")
    files = dict(cases.FIXTURES["clean"])
    files["zz-target.txt"] = "not config\n"
    commit_files = h.write_tree(repo / "conf.d", files)
    symlink_or_skip("zz-target.txt", commit_files / "_domain_policy.yaml")
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        status, doc = svc.validate(commit)
    assert status == 200 and doc["result"] == "findings" and doc["rc"] == 1, doc
    found = doc["findings"]["findings"]
    assert not _unreadable(doc), doc
    policy = [f for f in found if f["kind"] == "domain_policy_unusable"]
    assert len(policy) == 1, found
    assert "is not a regular file (not_regular_file): git tree mode 120000" \
        in json.dumps(policy[0]), policy


def test_sha256_repository(tmp_path, world):
    repo = h.init_repo(tmp_path / "r", fmt="sha256")
    commit = h.commit_tree(repo, cases.FIXTURES["clean"])
    assert len(commit) == 64
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        status, doc = svc.validate(commit)
        assert status == 200 and doc["result"] == "pass", doc
        status, doc = svc.validate(commit[:40])
        assert status == 400


# ── 503: container / policy unavailable ──────────────────────────────────

def test_amtool_missing_is_503(tmp_path, world):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    with h.Service(tmp_path / "s", world["repo"], bin_dir=empty) as svc:
        status, doc = svc.validate(world["commits"]["clean"])
        assert status == 503
        assert (doc["error"], doc["reason"]) == ("VALIDATOR_UNAVAILABLE", "amtool_missing")
        _s, st = svc.call("GET", "/v1/status")
        assert st["amtool_available"] is False and st["ready"] is False


@pytest.mark.parametrize("how", ["missing", "not_executable"])
def test_da_guard_missing_is_503(tmp_path, world, how):
    """ADR-036 §3 (2026-10-30): without a da-guard the judgement process can
    run, no key verdict (ADR-038 §5) can be given — the container is
    unavailable, checked before every judgement like amtool."""
    target = tmp_path / "da-guard"
    if how == "not_executable":
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o644)
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   env={"DA_GUARD_BINARY": str(target)}) as svc:
        status, doc = svc.validate(world["commits"]["clean"])
        assert status == 503, doc
        assert (doc["error"], doc["reason"]) == ("VALIDATOR_UNAVAILABLE", "da_guard_missing")
        _s, st = svc.call("GET", "/v1/status")
        assert st["da_guard_available"] is False and st["ready"] is False
        m = svc.metrics()
        assert m['da_generator_unavailable_total{reason="da_guard_missing"}'] == 1


def test_policy_is_read_for_every_judgement(tmp_path, world):
    policy = tmp_path / "policy.yaml"
    policy.write_text("allowed_domains: []\n", encoding="utf-8")
    commit = world["commits"]["clean"]
    own = cases.REPO.joinpath("components/da-tools/app/VERSION").read_text(
        encoding="utf-8").strip()
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--policy", str(policy))) as svc:
        def check(status_want, error=None, reason=None):
            status, doc = svc.validate(commit)
            assert status == status_want, doc
            if error:
                assert (doc["error"], doc["reason"]) == (error, reason), doc
            return doc

        doc = check(200)
        assert doc["validated"]["policy_sha256"] == hashlib.sha256(
            policy.read_bytes()).hexdigest()

        policy.write_text("allowed_domains: 5\n", encoding="utf-8")
        doc = check(503, "POLICY_UNAVAILABLE", "policy_unusable")
        assert str(policy) in doc["message"]
        _s, st = svc.call("GET", "/v1/status")
        assert st["policy"]["available"] is False and st["ready"] is False

        # #3276 G1: a --policy with nothing there is the same refusal, named.
        policy.unlink()
        doc = check(503, "POLICY_UNAVAILABLE", "policy_unusable")
        assert str(policy) in doc["message"]

        major = int(own.split(".")[0])
        policy.write_text(f'allowed_domains: []\nmin_generator_version: "{major + 1}.0.0"\n',
                          encoding="utf-8")
        doc = check(503, "VALIDATOR_UNAVAILABLE", "generator_too_old")
        assert "image tag" in doc["message"]
        _s, st = svc.call("GET", "/v1/status")
        assert st["policy"]["available"] is True and st["ready"] is False
        assert st["policy"]["min_generator_version"] == f"{major + 1}.0.0"

        policy.write_text("allowed_domains: []\nmin_generator_version: 3\n",
                          encoding="utf-8")
        check(503, "POLICY_UNAVAILABLE", "policy_unusable")

        policy.unlink()
        check(503, "POLICY_UNAVAILABLE", "policy_unusable")

        policy.write_text(f'allowed_domains: []\nmin_generator_version: "{own}"\n',
                          encoding="utf-8")
        doc = check(200)
        assert doc["result"] == "pass"
        _s, st = svc.call("GET", "/v1/status")
        assert st["ready"] is True and st["generator_version"] == own
        assert st["policy"]["sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
        m = svc.metrics()
        assert m['da_generator_unavailable_total{reason="policy_unusable"}'] == 4
        assert m['da_generator_unavailable_total{reason="generator_too_old"}'] == 1


# ── socket path ──────────────────────────────────────────────────────────

def test_an_old_socket_is_replaced_and_a_file_is_refused(tmp_path, world):
    sdir = h.short_dir()
    try:
        old = sdir / "old.sock"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(old))
        s.close()  # leaves the socket file behind, as a crash would
        (tmp_path / "a").mkdir()
        with h.Service(tmp_path / "a", world["repo"], bin_dir=world["bin"],
                       socket_path=old) as svc:
            assert svc.call("GET", "/v1/status")[0] == 200
        plain = sdir / "plain"
        plain.write_text("keep me", encoding="utf-8")
        (tmp_path / "b").mkdir()
        svc = h.Service(tmp_path / "b", world["repo"], bin_dir=world["bin"],
                        socket_path=plain, wait=False)
        assert svc.proc.wait(60) == 2
        assert plain.read_text(encoding="utf-8") == "keep me"
        assert "not a socket" in svc.log.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(sdir, ignore_errors=True)


def test_status_reports_versions(world):
    status, st = world["svc"].call("GET", "/v1/status")
    assert status == 200
    assert set(st) == {"policy", "generator_version", "generator_too_old",
                       "da_guard_version", "amtool_available", "da_guard_available", "ready",
                       "cache_base_coverage_ratio", "warm"}
    assert st["policy"] == {"configured": False, "available": True, "sha256": None,
                            "error": None, "min_generator_version": None}
    assert st["ready"] is True and st["da_guard_available"] is True
    m = world["svc"].metrics()
    info = [k for k in m if k.startswith("da_generator_build_info")]
    assert info and f'generator_version="{st["generator_version"]}"' in info[0]


def test_help_names_every_default(tmp_path):
    out = subprocess.run([sys.executable, str(h.OPS / "generator_service.py"), "--help"],
                         capture_output=True, text=True, encoding="utf-8",
                         check=True, timeout=120).stdout
    for flag in ("--max-judgement-seconds", "--deadline-reserve-seconds",
                 "--memory-limit-bytes", "--cache-max-bytes", "--max-queue",
                 "--max-body-bytes"):
        assert flag in out
    assert out.count("(default:") >= 6


# ── the limits are honoured (each flag changes the answer) ───────────────

def test_judgement_seconds_and_body_limits(tmp_path, world):
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", f"#!/bin/sh\n{SLEEP} 3\n")
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   args=("--max-judgement-seconds", "1",
                         "--max-body-bytes", "300")) as svc:
        # amtool alone takes 3 s: a deadline answer means the 1 s limit
        # applied (without it, the harness's 600 s would let this pass)
        status, doc = svc.validate(commit)
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        status, doc = svc.validate(commit, [{"path": "x.yaml",
                                             "content_b64": h.b64("a" * 300)}])
        assert status == 413


def test_result_and_transfer_limits(tmp_path, world):
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "a", world["repo"], bin_dir=world["bin"],
                   args=("--max-cache-transfer-bytes", "0")) as svc:
        status, doc = svc.validate(commit)
        assert doc["result"] == "pass" and doc["cache"]["stored"] > 0
        assert svc.metrics()["da_generator_cache_entries"] == 0  # all dropped
        status, doc = svc.validate(commit)
        assert doc["cache"]["hits"] == 0  # nothing came back to reuse
    with h.Service(tmp_path / "b", world["repo"], bin_dir=world["bin"],
                   args=("--max-result-bytes", "50")) as svc:
        status, doc = svc.validate(commit)
        assert (doc["result"], doc["reason"]) == ("rejected", "incomplete")


# ── E1: cache entries stream back; E2: warming, pre-emptible ─────────────

def _many_tenants(n: int) -> dict[str, str]:
    files = {"_defaults.yaml": cases.FIXTURES["clean"]["_defaults.yaml"]}
    for i in range(n):
        files[f"tn-{i:02d}.yaml"] = cases._tenant(f"tn-{i:02d}")
    return files


def _slow_parse(log: Path, delay: float, *, slow: str = "judgement",
                warm_exits: bool = False) -> str:
    """Launcher prelude: in the forked processes of kind *slow*
    (``judgement`` or ``warm``) every real parse is logged and sleeps
    *delay*, so that kind takes a known, long time on any host; the other
    kind parses at full speed, unlogged — so a warming run starting right
    after a response cannot add to a judgement's count. *warm_exits* makes
    every warming run end at once, having parsed nothing."""
    return (
        "import time, _lib_parse_cache as _pc, _gsvc_child as _gc\n"
        # ADR-036 step 4: a miss is read through parse-tree and stored once
        # converted — the store is where one file's read ends.
        "_orig_store = _pc.ParseCache.store\n"
        "_SLOW = [False]\n"
        "def _slow(self, key, value):\n"
        "    if _SLOW[0]:\n"
        f"        with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
        "            fh.write('p\\n')\n"
        f"        time.sleep({delay})\n"
        "    return _orig_store(self, key, value)\n"
        "_pc.ParseCache.store = _slow\n"
        "_orig_run_child = _gc.run_child\n"
        "def _run_child(job, fd):\n"
        f"    if job.warm and {warm_exits!r}:\n"
        "        os._exit(1)\n"
        f"    _SLOW[0] = job.warm == {slow == 'warm'!r}\n"
        "    return _orig_run_child(job, fd)\n"
        "_gc.run_child = _run_child\n")


def _lines(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


def _wait_metric(svc, name: str, at_least: float, timeout: float = 60.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = svc.metrics()
        if m.get(name, 0) >= at_least:
            return m
        time.sleep(0.1)
    raise AssertionError(f"{name} never reached {at_least}: {svc.metrics()}")


def test_killed_judgements_keep_their_parses_until_one_passes(tmp_path, world):
    """E1: the deadline is shorter than a cold judgement. Each killed attempt
    hands back what it finished parsing, so the cache grows with every
    attempt; nothing already cached is parsed again; and one attempt passes,
    parsing exactly what was still missing.

    ⛔ "each attempt parses fewer than the one before" is NOT a property of
    E1: a killed attempt's last parses may still be in the pipe when it is
    killed, so the next attempt parses them again and then gets further —
    its count can be larger (CI saw [21, 22, 20, 0]). The bound that holds is
    against what is cached, not against the previous attempt's count.

    A cold judgement is 61 parses × 0.15 s ≈ 9 s of sleeps against a 3 s
    limit, so the first attempt is killed on any host. What remains timed is
    only that a 3 s attempt gets past its fixed start (fork, git, the tree)
    and into parsing; the attempt count is bounded by (1), not by a guess at
    the host's speed."""
    repo = h.init_repo(tmp_path / "r")
    commit = h.commit_tree(repo, _many_tenants(30))
    log = tmp_path / "parses.log"
    # the warming run the new base schedules ends at once, parsing nothing
    # (E2 is tested on its own below)
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   prelude=_slow_parse(log, 0.15, warm_exits=True),
                   args=("--max-judgement-seconds", "3")) as svc:
        counts, results, cached_before = [], [], []
        for _attempt in range(80):  # (1) bounds it by the 61 keys
            cached_before.append(svc.metrics()["da_generator_cache_entries"])
            before = _lines(log)
            status, doc = svc.validate(commit)
            assert status == 200, doc
            counts.append(_lines(log) - before)
            results.append(doc["result"] if doc["result"] != "rejected"
                           else doc["reason"])
            if doc["result"] == "rejected":
                assert doc["cache"]["received"] > 0, doc  # kept, not lost
                after = svc.metrics()["da_generator_cache_entries"]
                if after == cached_before[-1]:
                    cached_before.append(after)  # no progress: (1) fails on it
                    break
            else:
                break
        total = svc.metrics()["da_generator_cache_entries"]
    assert cached_before[0] == 0, cached_before
    # (1) every killed attempt handed something back: the cache before each
    # attempt grows strictly up to the one that passes
    assert all(a < b for a, b in zip(cached_before, cached_before[1:])), \
        (cached_before, counts, results)
    assert results[0] == "deadline" and results[-1] == "pass", results
    assert set(results[:-1]) == {"deadline"}, results
    # (2) what is cached is not parsed again
    assert all(n <= total - c for n, c in zip(counts, cached_before)), \
        (counts, cached_before, total)
    # (3) the passing attempt parsed exactly what was still missing (0 when
    # nothing was), and handed all of it back
    assert counts[-1] == total - cached_before[-1], (counts, cached_before, total)


def test_startup_warming_leaves_only_the_changed_file_to_parse(tmp_path, world):
    """E2: --warm-ref (HEAD) is warmed at start; the first request then
    parses only the file it changes."""
    repo = h.init_repo(tmp_path / "r")
    files = _many_tenants(10)
    commit = h.commit_tree(repo, files)
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   args=("--warm-ref", "HEAD")) as svc:
        m = _wait_metric(svc, 'da_generator_warm_runs_total{outcome="completed"}', 1)
        assert m["da_generator_cache_base_coverage_ratio"] == 1.0
        _s, st = svc.call("GET", "/v1/status")
        assert st["warm"]["commit"] == commit and st["cache_base_coverage_ratio"] == 1.0
        assert st["warm"]["pinned_commit"] == commit
        body = files["tn-03.yaml"] + f"# {uuid.uuid4().hex}\n"
        status, doc = svc.validate(commit, [{"path": "tn-03.yaml",
                                             "content_b64": h.b64(body)}])
        assert status == 200 and doc["result"] == "pass", doc
        # a tenant file is read in two modes (routing read + declarations)
        assert doc["cache"]["parses"] == 2, doc["cache"]
        assert doc["cache"]["hits"] > 0
        # the request's base is the warmed (pinned) one: nothing more to warm
        time.sleep(0.5)
        m = svc.metrics()
        assert sum(v for k, v in m.items()
                   if k.startswith("da_generator_warm_runs_total")) == 1


def test_a_request_preempts_warming(tmp_path, world):
    """E2: a request arriving during a warming run does not wait for it:
    the warming run is ended (preempted), what it parsed stays cached.
    Only the warming run is slowed (61 parses × 0.5 s): after 4 of them it
    has ≥ 28 s of sleeps left, so a request that waited for it would show a
    queue wait of that order; the judgement itself parses at full speed."""
    repo = h.init_repo(tmp_path / "r")
    commit = h.commit_tree(repo, _many_tenants(30))
    log = tmp_path / "parses.log"
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   prelude=_slow_parse(log, 0.5, slow="warm"),
                   args=("--warm-ref", "HEAD", "--warm-seconds", "300")) as svc:
        _until(lambda: _lines(log) >= 4, "warming has parsed (3 handed back)")
        status, doc = svc.validate(commit)
        assert status == 200 and doc["result"] == "pass", doc
        assert doc["queue_wait_ms"] < 15000, doc["queue_wait_ms"]  # vs ≥ 28 s left
        assert doc["cache"]["hits"] > 0  # the preempted run's parses were kept
        m = svc.metrics()
        assert m['da_generator_warm_runs_total{outcome="preempted"}'] == 1
        assert m['da_generator_warm_runs_total{outcome="completed"}'] == 0


def test_an_unresolvable_warm_ref_is_logged_and_counted(tmp_path, world):
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--warm-ref", "no-such-ref")) as svc:
        m = svc.metrics()
        assert m['da_generator_warm_runs_total{outcome="failed"}'] == 1
        _s, st = svc.call("GET", "/v1/status")
        assert st["ready"] is True and "no-such-ref" in st["warm"]["error"]
        assert svc.validate(world["commits"]["clean"])[1]["result"] == "pass"
    assert "does not resolve" in svc.log.read_text(encoding="utf-8")


def test_pinning_follows_the_latest_request_base(tmp_path, world):
    """E2: the warm commit is pinned before any request; afterwards the pin
    follows the latest request's base, and a warming run of another commit
    does not move it."""
    repo = h.init_repo(tmp_path / "r")
    first = h.commit_tree(repo, _many_tenants(3))
    second = h.commit_tree(repo, _many_tenants(4))  # HEAD
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   args=("--warm-ref", "HEAD")) as svc:
        _wait_metric(svc, 'da_generator_warm_runs_total{outcome="completed"}', 1)
        assert svc.call("GET", "/v1/status")[1]["warm"]["pinned_commit"] == second
        svc.validate(first)  # a new base: pinned now, and warmed after
        _wait_metric(svc, 'da_generator_warm_runs_total{outcome="completed"}', 2)
        _s, st = svc.call("GET", "/v1/status")
        assert st["warm"]["pinned_commit"] == first
        assert st["cache_base_coverage_ratio"] == 1.0


def test_a_judgement_does_not_hold_other_clients_connections(tmp_path, world):
    """A forked process must not keep the service's client sockets open: C,
    queued when B is forked, expires while B runs — its answer must end
    (EOF) then, not when B ends. A and B are held in amtool until released."""
    bin_dir, gate = _gate(tmp_path)
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   env={"GSVC_GATE_DIR": str(gate)}) as svc, \
            ThreadPoolExecutor(4) as pool:
        a = pool.submit(svc.validate, commit)
        _until(lambda: (gate / "started-0").exists(), "A in amtool")
        b = pool.submit(svc.validate, commit)
        _until(lambda: svc.metrics()["da_generator_queue_depth"] == 1, "B queued")
        # 10 s: room for A to end and B to be forked while C still waits
        c = pool.submit(svc.validate, commit, None, 15000)
        _until(lambda: svc.metrics()["da_generator_queue_depth"] == 2, "C queued")
        (gate / "release-0").touch()  # A ends; B is forked with C queued
        _until(lambda: (gate / "started-1").exists(), "B in amtool")
        assert not c.done(), "C ended before B was forked: nothing was tested"
        status, doc = c.result()
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        assert not b.done()  # C's answer ended while B still runs
        (gate / "release-1").touch()
        assert a.result()[1]["result"] == b.result()[1]["result"] == "pass"


# ── round 2: admission, malformed input, zombies, shutdown ───────────────

def _hwm_kib(pid: int) -> int:
    with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmHWM"):
                return int(line.split()[1])
    raise AssertionError("no VmHWM")


def _post_head(length: int) -> bytes:
    return (f"POST /v1/validate HTTP/1.1\r\nHost: g\r\n"
            f"Content-Length: {length}\r\n\r\n").encode()


def test_body_bytes_are_admitted_before_they_are_read(tmp_path, world):
    """#2901 R1 (the blind review's exp_mem): many clients each sending most
    of a large body do not grow the service's memory with their number —
    past the in-flight budget they are answered busy without being read —
    and a legitimate request gets a verdict or busy, never a 503."""
    commit = world["commits"]["clean"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"]) as svc:
        pid = svc.proc.pid
        base = _hwm_kib(pid)
        length = 3 * 1024 * 1024
        socks, answered = [], 0
        for _ in range(20):
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(svc.socket))
            s.settimeout(10)
            s.sendall(_post_head(length))
            socks.append(s)
        chunk = b"x" * (256 * 1024)
        for s in socks:
            try:
                for _ in range(10):  # 2.5 of 3 MiB
                    s.sendall(chunk)
            except OSError:
                pass  # answered busy and closed after its linger: nothing kept
        time.sleep(0.5)
        grew = _hwm_kib(pid) - base
        status, doc = svc.validate(commit)
        assert status == 200 and doc["result"] in ("pass", "rejected"), doc
        if doc["result"] == "rejected":
            assert doc["reason"] == "busy"
        for s in socks:
            s.close()
        assert grew < 24 * 1024, grew    # not 20 × 2.5 MiB
        _inflight_released(svc)
        assert svc.validate(commit)[1]["result"] == "pass"
        # one fits the budget, the rest are answered busy unread
        assert svc.metrics()['da_generator_rejections_total{reason="busy"}'] >= 15


def test_connection_slots_answer_busy_and_never_503(tmp_path, world):
    """#2901 R1 (the blind review's exp_fd): idle connections past the slot
    limit are answered busy and closed; a legitimate request is never 503."""
    commit = world["commits"]["clean"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   prelude="import resource; resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n",
                   args=("--max-connections", "20")) as svc:
        idle = []
        for _ in range(56):
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(svc.socket))
            idle.append(s)
        time.sleep(0.5)
        # every slot is held (the readiness probe's slot may just have been
        # freed, so the legitimate request may get it): busy or a verdict,
        # never 503 / EMFILE
        status, doc = svc.validate(commit, timeout=30)
        assert status == 200 and (doc["result"] == "pass"
                                  or (doc["result"], doc["reason"]) == ("rejected", "busy")), doc
        for s in idle:
            s.close()
        _wait_metric(svc, 'da_generator_rejections_total{reason="busy"}', 56 - 20)
        # only the metrics request itself is still open
        _until(lambda: svc.metrics()["da_generator_connections"] == 1,
               "the idle connections' slots given back")
        status, doc = svc.validate(commit)
        assert (status, doc["result"]) == (200, "pass"), doc


def test_a_too_low_descriptor_limit_refuses_to_start(tmp_path, world):
    svc = h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                    prelude="import resource; resource.setrlimit(resource.RLIMIT_NOFILE, (40, 40))\n",
                    wait=False)
    assert svc.proc.wait(60) == 2
    assert "RLIMIT_NOFILE" in svc.log.read_text(encoding="utf-8")
    svc.stop()


@pytest.mark.parametrize("raw_request", [
    "POST /v1/validate HTTP/1.1\r\nContent-Length: ²\r\n\r\n".encode("latin-1"),
    b"POST /v1/validate HTTP/1.1\r\nContent-Length: +5\r\n\r\nxxxxx",
], ids=["superscript-digit", "signed"])
def test_a_non_ascii_or_signed_content_length_is_400(world, raw_request):
    status, _h, _b = world["svc"].raw(raw_request)
    assert status == 400


@pytest.mark.parametrize("body", [
    b'{"base_commit": "' + b"a" * 40 + b'", "writes": [], "deadline_ms": 1' + b"0" * 400 + b"}",
    b'{"base_commit": "' + b"a" * 40 + b'", "writes": [], "deadline_ms": -1' + b"0" * 400 + b"}",
    b'{"base_commit": "' + b"a" * 40 + b'", "writes": [], "deadline_ms": -5}',
    b'{"base_commit": "' + b"a" * 40 + b'", "writes": [], "deadline_ms": true}',
    b"[" * 200000,
], ids=["deadline-10e400", "deadline-minus-10e400", "deadline-negative", "deadline-bool",
        "json-depth"])
def test_inputs_that_used_to_end_the_connection_are_400(world, body):
    """#2901 O1: no empty answer, no traceback."""
    status, _h, data = world["svc"].raw(_post_head(len(body)) + body)
    assert status == 400 and json.loads(data)["error"] == "BAD_REQUEST"
    assert "Traceback" not in world["svc"].log.read_text(encoding="utf-8")


def test_two_content_lengths_are_400_even_if_the_first_fits(world):
    """#2901 R7 (N14): a valid body under the first of two Content-Length
    headers is still refused."""
    body = json.dumps({"base_commit": world["commits"]["clean"], "writes": [],
                       "deadline_ms": 25000}).encode()
    raw = (f"POST /v1/validate HTTP/1.1\r\nContent-Length: {len(body)}\r\n"
           f"Content-Length: {len(body) + 7}\r\n\r\n").encode() + body
    status, _h, _b = world["svc"].raw(raw)
    assert status == 400


def test_no_zombies_are_left_after_a_deadline(tmp_path, world):
    """#2901 R3: the service is a child subreaper, so what a killed judgement
    leaves (amtool, its sleep) is re-parented to it and collected."""
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", f"#!/bin/sh\n{SLEEP} 60 &\nwait\n")
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir) as svc:
        for _ in range(3):
            # 6 s: room to reach amtool on a slow host; the deadline is what
            # ends it (amtool's sleep is 60 s)
            status, doc = svc.validate(world["commits"]["clean+extra"], deadline_ms=11000)
            assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        end = time.monotonic() + 10
        while h.zombies_of(svc.proc.pid) and time.monotonic() < end:
            time.sleep(0.1)
        assert h.zombies_of(svc.proc.pid) == []
        assert svc.metrics()["da_generator_orphans_reaped_total"] >= 6


def _outcome_of(pending) -> str:
    """What a submitted ``svc.validate`` came to, for a failure message."""
    if not pending.done():
        return "still running"
    try:
        return repr(pending.result())
    except Exception as exc:  # noqa: BLE001 — reported, not handled
        return f"raised {exc!r}"


def test_shutdown_ends_the_running_judgement(tmp_path, world):
    """#2901 R7 (N19): SIGTERM while judging ends the judgement's whole group."""
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", _SLOW_AMTOOL)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    svc = h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                    env={"GSVC_PID_DIR": str(pid_dir)})
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(svc.validate, world["commits"]["clean"])
        end = time.monotonic() + 30
        while not (pid_dir / "grandchild.pid").exists() and not pending.done() \
                and time.monotonic() < end:
            time.sleep(0.05)
        if not (pid_dir / "grandchild.pid").exists():
            # #3174: a judgement that never reached amtool (e.g. rejected
            # before it) says why here, instead of a FileNotFoundError below.
            svc.stop()
            pytest.fail("amtool's child never started; the judgement: "
                        + _outcome_of(pending) + "; service log: "
                        + svc.log.read_text(encoding="utf-8", errors="replace")[-2000:])
        pids = [int((pid_dir / n).read_text().strip())
                for n in ("amtool.pid", "grandchild.pid")]
        svc.stop()
        with pytest.raises(Exception):
            pending.result()
    end = time.monotonic() + 5
    while any(h.alive(p) and p not in h.zombies_of(1) for p in pids) \
            and time.monotonic() < end:
        time.sleep(0.05)
    still = [p for p in pids if h.alive(p) and p not in h.zombies_of(1)]
    assert still == [], still


def _hold_body(svc, length: int) -> socket.socket:
    """A request admitted with *length* reserved whose body never completes."""
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(svc.socket))
    s.sendall(_post_head(length) + b"{")
    _until(lambda: svc.metrics()["da_generator_inflight_body_bytes"] >= length,
           f"{length} body bytes reserved")
    return s


def _inflight_released(svc) -> None:
    _until(lambda: svc.metrics()["da_generator_inflight_body_bytes"] == 0,
           "every reserved body byte given back")


def test_an_inflight_budget_below_the_largest_body_refuses_to_start(tmp_path, world):
    """#2901 N5: a request between the two limits could never be admitted."""
    svc = h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                    args=("--max-body-bytes", "1000",
                          "--max-inflight-body-bytes", "999"), wait=False)
    assert svc.proc.wait(60) == 2
    assert "--max-inflight-body-bytes" in svc.log.read_text(encoding="utf-8")
    svc.stop()


def test_inflight_budget_and_writes_limit_flags(tmp_path, world):
    """Each admission flag changes the answer it bounds."""
    commit = world["commits"]["clean"]
    two = [{"path": "x.yaml", "delete": True}, {"path": "y.yaml", "delete": True}]
    with h.Service(tmp_path / "a", world["repo"], bin_dir=world["bin"],
                   args=("--max-body-bytes", "300",
                         "--max-inflight-body-bytes", "300")) as svc:
        held = _hold_body(svc, 250)  # admitted, its body never completes
        status, doc = svc.validate(commit)  # ~90 bytes: 250 + 90 > 300
        assert status == 200 and (doc["result"], doc["reason"]) == ("rejected", "busy")
        held.close()
        _inflight_released(svc)
        assert svc.validate(commit)[1]["result"] == "pass"
    with h.Service(tmp_path / "c", world["repo"], bin_dir=world["bin"],
                   args=("--max-body-bytes", "200",
                         "--max-inflight-body-bytes", "200")) as svc:
        # each request's bytes are given back when it is answered: five in a
        # row fit a budget that holds two at once
        for _ in range(5):
            assert svc.validate(commit)[1]["result"] == "pass"
        assert svc.metrics()["da_generator_inflight_body_bytes"] == 0
    with h.Service(tmp_path / "b", world["repo"], bin_dir=world["bin"],
                   args=("--max-writes", "1")) as svc:
        status, doc = svc.validate(commit, two)
        assert status == 400 and "at most 1" in doc["message"]
        assert svc.validate(commit, two[:1])[1]["result"] == "pass"


def test_reading_the_body_counts_against_the_deadline(tmp_path, world):
    """#2901 O6: the clock starts when the header has been read."""
    body = json.dumps({"base_commit": world["commits"]["clean"], "writes": [],
                       "deadline_ms": 25000}).encode()
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--max-judgement-seconds", "1.5")) as svc:
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(svc.socket))
        s.settimeout(30)
        s.sendall(_post_head(len(body)))
        time.sleep(4.0)  # far longer than the whole 1.5 s budget
        s.sendall(body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        doc = json.loads(data.split(b"\r\n\r\n", 1)[1])
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")


def test_a_busy_answer_reaches_a_client_still_sending_its_body(tmp_path, world):
    """#2901 R1: answered busy before its body is read, the connection is
    half-closed and drained, so the client reads the answer rather than a
    reset."""
    body = b"x" * (2 * 1024 * 1024)
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--max-body-bytes", str(3 * 1024 * 1024),
                         "--max-inflight-body-bytes", str(3 * 1024 * 1024))) as svc:
        held = _hold_body(svc, 2 * 1024 * 1024)  # 2 of the 3 MiB reserved
        status, _h, data = svc.raw(_post_head(len(body)) + body)
        held.close()
        doc = json.loads(data)
        assert status == 200 and (doc["result"], doc["reason"]) == ("rejected", "busy")


# ── --config-subdir spellings (CodeRabbit on #3015) ─────────────────────

def _verdict(doc: dict) -> tuple:
    return (doc["result"], doc["rc"],
            _norm(doc["findings"], Path(doc["findings"]["config_dir"])))


def _root_repo(path: Path) -> tuple[Path, dict[str, str]]:
    """The fixtures again, with conf.d at the repository's root."""
    repo = h.init_repo(path)
    commits = {}
    for fx in FIXTURES:
        for child in repo.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        h.write_tree(repo, _split(fx)[0])
        h.run_git(repo, "add", "-A")
        h.run_git(repo, "commit", "-q", "--allow-empty", "-m", fx)
        commits[fx] = h.run_git(repo, "rev-parse", "HEAD")
    return repo, commits


def test_config_subdir_spellings_judge_the_same_tree(tmp_path, world):
    """`./conf.d` judges like `conf.d`, and `.` like `""` (the root): before
    the start-up normalisation `./conf.d` matched no ls-tree path, so every
    write came back as findings on an empty tree."""
    root_repo, root_commits = _root_repo(tmp_path / "rootrepo")
    with h.Service(tmp_path / "dot", world["repo"], bin_dir=world["bin"],
                   subdir="./conf.d") as dotted, \
         h.Service(tmp_path / "r1", root_repo, bin_dir=world["bin"],
                   subdir=".") as dot_root, \
         h.Service(tmp_path / "r2", root_repo, bin_dir=world["bin"],
                   subdir="") as empty_root:
        results = set()
        for fx in FIXTURES:
            _base, write = _split(fx)
            writes = [{"path": write,
                       "content_b64": h.b64(cases.FIXTURES[fx][write])}]
            plain = _verdict(world["svc"].validate(world["commits"][fx], writes)[1])
            assert _verdict(dotted.validate(world["commits"][fx], writes)[1]) == plain, fx
            at_root = _verdict(dot_root.validate(root_commits[fx], writes)[1])
            assert at_root == _verdict(empty_root.validate(root_commits[fx], writes)[1]), fx
            assert at_root == plain, fx
            results.add(plain[0])
    assert results == {"pass", "findings"}  # both verdicts were compared


@pytest.mark.parametrize("subdir", ["/abs", "../x"])
def test_a_config_subdir_outside_the_repository_refuses_to_start(tmp_path, world, subdir):
    svc = h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                    subdir=subdir, wait=False)
    assert svc.proc.wait(60) == 2
    assert "--config-subdir" in svc.log.read_text(encoding="utf-8")
    svc.stop()
