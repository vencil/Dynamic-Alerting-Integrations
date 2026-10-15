"""#2901（ADR-036 §3、§5、§6）：產生器容器的服務程式，真的起服務、走 Unix
socket 打請求。

- 判定：寫入後的整棵樹以服務判出的 rc 與 findings，與 CLI
  ``generate-routes --validate --strict`` 對同一棵樹跑出的相同（每個 findings
  fixture 各一筆，含會擋下的）。
- 拒收該筆（HTTP 200、``result: rejected``）：期限（判定中到期時子孫程序一併
  結束）、排隊中到期、佇列滿、記憶體上限、沒有完整結果、git 讀兩次都失敗。
- 容器不可用（503）：找不到 amtool、產生器比 policy 要求的舊、部署端 policy
  檔不可用；每次判定都重查。
- git 讀取失敗重試一次；symlink 與 gitlink 依 tree entry mode 實體化。
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
    blob = pc.content_key(body.encode())
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


def test_deadline_ends_the_judgement_and_its_descendants(tmp_path, world):
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", _SLOW_AMTOOL)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   env={"GSVC_PID_DIR": str(pid_dir)}) as svc:
        t0 = time.monotonic()
        status, doc = svc.validate(world["commits"]["clean+extra"], deadline_ms=7000)
        took = time.monotonic() - t0
        assert status == 200
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        assert 1.5 <= took < 10
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
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", f"#!/bin/sh\n{SLEEP} 3\n")
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir,
                   args=("--max-queue", "1")) as svc, \
            ThreadPoolExecutor(4) as pool:
        first = pool.submit(svc.validate, commit)
        time.sleep(1.0)  # running
        t0 = time.monotonic()
        expiring = pool.submit(svc.validate, commit, None, 6500)  # 1.5 s
        time.sleep(0.2)
        status, busy = svc.validate(commit)  # queue (1) is full
        assert status == 200 and (busy["result"], busy["reason"]) == ("rejected", "busy")
        status, exp = expiring.result()
        assert (exp["result"], exp["reason"]) == ("rejected", "deadline")
        assert time.monotonic() - t0 < 2.5  # did not wait for the first
        assert first.result()[1]["result"] == "pass"
        # the next one is judged as usual
        assert svc.validate(commit)[1]["result"] == "pass"


# ── memory, incomplete ───────────────────────────────────────────────────

def test_memory_limit_rejects_that_write_only(tmp_path, world):
    big = "tenants:\n  big:\n    x: [" + ",".join(["0"] * 400000) + "]\n"
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=world["bin"],
                   args=("--memory-limit-bytes", str(64 * 1024 * 1024))) as svc:
        status, doc = svc.validate(commit, [{"path": "big.yaml",
                                             "content_b64": h.b64(big)}])
        assert status == 200 and (doc["result"], doc["reason"]) == ("rejected", "memory"), doc
        assert svc.validate(commit)[1]["result"] == "pass"
        m = svc.metrics()
        assert m['da_generator_rejections_total{reason="memory"}'] == 1
        assert m["da_generator_judgement_memory_limit_bytes"] == 64 * 1024 * 1024


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
def _fake(argv):
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


def test_tree_entries_are_written_by_mode(tmp_path):
    """100644 / 100755 files, a 120000 symlink with its target text as
    stored, a 160000 gitlink as an empty directory."""
    repo = h.init_repo(tmp_path / "r")
    d = repo / "conf.d"
    (d / "sub").mkdir(parents=True)
    (d / "alpha.yaml").write_text(cases._tenant("alpha"), encoding="utf-8")
    (d / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (d / "run.sh").chmod(0o755)
    symlink_or_skip("alpha.yaml", d / "link.yml")
    symlink_or_skip("../../nowhere", d / "sub" / "dangling.yaml")
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
                     "vendor": "160000"}
    root = job_dir / "conf.d"
    _gsvc_child.materialise(str(root), tree)
    assert (root / "alpha.yaml").read_text(encoding="utf-8") == cases._tenant("alpha")
    assert (root / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert os.readlink(root / "link.yml") == "alpha.yaml"
    assert os.readlink(root / "sub" / "dangling.yaml") == "../../nowhere"
    assert (root / "vendor").is_dir() and not os.listdir(root / "vendor")
    # read_tree's object ids are git's own blob ids (pinning uses content
    # SHA-256, computed from these contents)
    listed = [line.split()[:3] for line in
              h.run_git(repo, "ls-tree", "-r", commit, "--", "conf.d").splitlines()]
    assert {o for m, o, _r, _c in tree if m != "160000"} == {
        oid for _mode, kind, oid in listed if kind == "blob"}


def test_a_symlink_and_gitlink_tree_judges_like_the_cli(tmp_path, world):
    repo = h.init_repo(tmp_path / "r")
    files = dict(cases.FIXTURES["clean"])
    commit_files = h.write_tree(repo / "conf.d", files)
    symlink_or_skip("_defaults.yaml", commit_files / "_defaults.yml")  # 2 carriers
    h.run_git(repo, "add", "-A")
    h.run_git(repo, "update-index", "--add", "--cacheinfo",
              f"160000,{'1' * 40},conf.d/vendor")
    h.run_git(repo, "commit", "-qm", "x")
    commit = h.run_git(repo, "rev-parse", "HEAD")
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"]) as svc:
        status, doc = svc.validate(commit)
    tree = h.write_tree(tmp_path / "cli" / "conf.d", files)
    symlink_or_skip("_defaults.yaml", tree / "_defaults.yml")
    (tree / "vendor").mkdir()
    rc, cli = h.run_cli(tree, world["bin"], tmp_path / "f.json")
    assert status == 200
    assert (doc["rc"], _norm(doc["findings"], Path(doc["findings"]["config_dir"]))) \
        == (rc, _norm(cli, tree))


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
        assert m['da_generator_unavailable_total{reason="policy_unusable"}'] == 3
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
                       "da_guard_version", "amtool_available", "ready",
                       "cache_base_coverage_ratio", "warm"}
    assert st["policy"] == {"configured": False, "available": True, "sha256": None,
                            "error": None, "min_generator_version": None}
    assert st["ready"] is True
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
        t0 = time.monotonic()
        status, doc = svc.validate(commit)  # deadline_ms 25000: 1 s, not 20
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        assert time.monotonic() - t0 < 2.5
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


def _slow_parse(log: Path, delay: float) -> str:
    """Launcher prelude: every real parse sleeps *delay* and is logged, so
    a cold judgement takes a known, long time on any host."""
    return (
        "import time, _lib_parse_cache as _pc\n"
        "_orig_parse = _pc._parse_file\n"
        "def _slow(path, mode):\n"
        f"    with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('p\\n')\n"
        f"    time.sleep({delay})\n"
        "    return _orig_parse(path, mode)\n"
        "_pc._parse_file = _slow\n")


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
    still leaves its finished parses in the cache, so the next attempt
    parses fewer, and one of them passes."""
    repo = h.init_repo(tmp_path / "r")
    commit = h.commit_tree(repo, _many_tenants(30))
    log = tmp_path / "parses.log"
    # the warming run the new base schedules gets no time to help (E2 is
    # tested on its own below)
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   prelude=_slow_parse(log, 0.05),
                   args=("--max-judgement-seconds", "1.2",
                         "--warm-seconds", "0.01")) as svc:
        counts, results, entries = [], [], []
        for _attempt in range(12):
            before = _lines(log)
            status, doc = svc.validate(commit)
            assert status == 200, doc
            counts.append(_lines(log) - before)
            results.append(doc["result"] if doc["result"] != "rejected"
                           else doc["reason"])
            if len(results) == 1:
                # the first (new) base schedules one warming run; let its
                # 0.01 s limit end it before counting again
                _wait_metric(svc, 'da_generator_warm_runs_total{outcome="deadline"}', 1)
            entries.append(svc.metrics()["da_generator_cache_entries"])
            if doc["result"] == "rejected":
                assert doc["cache"]["received"] > 0, doc  # kept, not lost
            else:
                break
    assert results[0] == "deadline" and results[-1] == "pass", results
    assert set(results[:-1]) == {"deadline"}, results
    assert all(a >= b for a, b in zip(counts, counts[1:])), counts
    assert counts[-1] < counts[0], counts
    # every killed attempt added to the cache, and nothing was lost
    killed = entries[:-1]
    assert killed[0] > 0 and all(a < b for a, b in zip(killed, killed[1:])), entries
    assert entries[-1] >= killed[-1], entries


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
    the warming run is ended (preempted), what it parsed stays cached."""
    repo = h.init_repo(tmp_path / "r")
    commit = h.commit_tree(repo, _many_tenants(30))
    log = tmp_path / "parses.log"
    with h.Service(tmp_path / "s", repo, bin_dir=world["bin"],
                   prelude=_slow_parse(log, 0.1),
                   args=("--warm-ref", "HEAD", "--warm-seconds", "60")) as svc:
        end = time.monotonic() + 30
        while _lines(log) < 5 and time.monotonic() < end:  # warming is parsing
            time.sleep(0.05)
        assert _lines(log) >= 5
        t0 = time.monotonic()
        status, doc = svc.validate(commit)
        assert status == 200 and doc["result"] == "pass", doc
        assert doc["queue_wait_ms"] < 1000, doc["queue_wait_ms"]  # vs a 60 s warm limit
        assert doc["cache"]["hits"] > 0  # the preempted run's parses were kept
        assert time.monotonic() - t0 < 20
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
    (EOF) then, not when B ends."""
    bin_dir = tmp_path / "bin"
    h.stub(bin_dir, "amtool", f"#!/bin/sh\n{SLEEP} 3\n")
    commit = world["commits"]["clean+extra"]
    with h.Service(tmp_path / "s", world["repo"], bin_dir=bin_dir) as svc, \
            ThreadPoolExecutor(4) as pool:
        a = pool.submit(svc.validate, commit)
        time.sleep(0.3)
        b = pool.submit(svc.validate, commit)
        time.sleep(0.3)
        t0 = time.monotonic()
        c = pool.submit(svc.validate, commit, None, 9000)  # 4 s: ends during B
        status, doc = c.result()
        took = time.monotonic() - t0
        assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        assert took < 5.0, took  # B runs until about 6 s
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
        time.sleep(1.5)
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
        time.sleep(0.5)
        assert svc.metrics()['da_generator_rejections_total{reason="busy"}'] >= 56 - 20
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
            status, doc = svc.validate(world["commits"]["clean+extra"], deadline_ms=6500)
            assert (doc["result"], doc["reason"]) == ("rejected", "deadline")
        end = time.monotonic() + 10
        while h.zombies_of(svc.proc.pid) and time.monotonic() < end:
            time.sleep(0.1)
        assert h.zombies_of(svc.proc.pid) == []
        assert svc.metrics()["da_generator_orphans_reaped_total"] >= 6


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
        while not (pid_dir / "grandchild.pid").exists() and time.monotonic() < end:
            time.sleep(0.05)
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
    time.sleep(0.3)
    return s


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
        status, doc = svc.validate(commit)  # ~85 bytes: 250 + 85 > 300
        assert status == 200 and (doc["result"], doc["reason"]) == ("rejected", "busy")
        held.close()
        time.sleep(0.3)
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
        time.sleep(1.8)  # longer than the whole budget
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
