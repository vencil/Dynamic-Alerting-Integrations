#!/usr/bin/env python3
"""patch-config apply 在真信號下的行為（#1950 盲審第 2 輪 X1／X3／X6）。

patch_config.py 以子行程（自己的 session）跑，所以 `os.kill`／`os.killpg` 打不到
pytest。PATH 上的 fake `kubectl` 把 ConfigMap 放在檔案裡，每次 patch 讓
`Last reload` 前進；`delay_<op>` 檔讓第 N 次某種呼叫睡一段時間，測試在 log 看到
那次睡眠開始後才送信號——信號因此落在指定的那一步。
"""
import json
import os
import random
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops" / "patch_config.py"
OLD = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
       "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: '70'\n",
       "t-b.yaml": "tenants:\n  t-b:\n    mysql_connections: '60'\n"}
# #1950 列 9：改 t-a 連帶改 t-b ⇒ 驗收失敗、回滾（不靠信號觸發的回滾）。
ANCHOR = {"config.yaml": ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
                          "  t-b: *s\n")}

FAKE_KUBECTL = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    import yaml
    S, a = os.environ["FAKE_S"], sys.argv[1:]
    def log(m):
        with open(os.path.join(S, "log"), "a", encoding="utf-8") as f:
            f.write(m + "\\n")
    def delay(op):
        p = os.path.join(S, "delay_" + op)
        if not os.path.exists(p):
            return
        secs, nth = open(p, encoding="utf-8").read().split()
        cnt = os.path.join(S, "cnt_" + op)
        c = int(open(cnt, encoding="utf-8").read()) + 1 if os.path.exists(cnt) else 1
        open(cnt, "w", encoding="utf-8").write(str(c))
        if str(c) in nth.split(","):
            log(f"sleep {op} {c}")
            time.sleep(float(secs))
    def cm():
        return json.load(open(os.path.join(S, "cm.json"), encoding="utf-8"))
    def gen():
        return int(open(os.path.join(S, "gen"), encoding="utf-8").read())
    def rv():
        p = os.path.join(S, "rv")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else "1"
    if a[:2] == ["get", "configmap"]:
        print(json.dumps({"metadata": {"resourceVersion": rv()}, "data": cm()}))
    elif a[:1] == ["patch"]:
        body = json.load(open(a[a.index("--patch-file") + 1], encoding="utf-8"))
        patch = body["data"]
        delay("patch_before")
        if body.get("metadata", {}).get("resourceVersion") not in (None, rv()):
            sys.exit("Error from server (Conflict): the object has been modified")
        d = cm()
        for k, v in patch.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        json.dump(d, open(os.path.join(S, "cm.json"), "w", encoding="utf-8"))
        new_rv = str(int(rv()) + 1)
        open(os.path.join(S, "rv"), "w", encoding="utf-8").write(new_rv)
        if not os.path.exists(os.path.join(S, "freeze")):  # freeze: never reloads
            g = gen() + 1
            open(os.path.join(S, "gen"), "w", encoding="utf-8").write(str(g))
        log("patch-landed")
        delay("patch_after")
        print(json.dumps({"metadata": {"resourceVersion": new_rv}, "data": d}))
    elif a[:2] == ["get", "pods"]:
        print(json.dumps({"items": [{"metadata": {"name": "exporter-0"},
              "status": {"phase": "Running"},
              "spec": {"containers": [{"ports": [{"name": "http", "containerPort": 8080}]}]}}]}))
    elif a[:2] == ["get", "--raw"]:
        path = a[2].split("/proxy/", 1)[1]
        delay("raw")
        if path == "api/v1/config":
            print(f"Last reload:   g{gen()}")
        else:
            for k, v in sorted(cm().items()):
                if k.startswith("_"):
                    continue
                for t, b in ((yaml.safe_load(v) or {}).get("tenants") or {}).items():
                    for m, val in (b or {}).items():
                        print(f'user_threshold{{metric="{m}",tenant="{t}"}} {val}')
    else:
        sys.exit(f"fake kubectl: unexpected {a}")
''')

needs_posix = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")


class Run:
    def __init__(self, tmp_path, old, delays, argv=("t-a", "mysql_connections", "65"),
                 merged_pipe=False, extra=("--reload-timeout", "3"), freeze=False,
                 close=()):
        self.dir, self.old = tmp_path / "s", old
        self.dir.mkdir()
        (self.dir / "cm.json").write_text(json.dumps(old), encoding="utf-8")
        (self.dir / "gen").write_text("0", encoding="utf-8")
        for op, spec in delays.items():
            (self.dir / f"delay_{op}").write_text(spec, encoding="utf-8")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "kubectl").write_text(FAKE_KUBECTL, encoding="utf-8")
        (bindir / "kubectl").chmod(0o755)
        env = dict(os.environ, FAKE_S=str(self.dir),
                   PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        if freeze:
            (self.dir / "freeze").write_text("", encoding="utf-8")
        cmd = [sys.executable, str(TOOL), "--json", *argv, "--poll-interval",
               "0.05", *extra]
        if close:  # `>&-` / `2>&-`: the tool starts with those fds closed
            cmd = ["sh", "-c", 'exec "$@" ' + " ".join(f"{fd}>&-" for fd in close),
                   "sh", *cmd]
        if merged_pipe:  # `… 2>&1 | head -n 2`
            rfd, wfd = os.pipe()
            self.proc = subprocess.Popen(cmd, env=env, stdout=wfd, stderr=wfd,
                                         start_new_session=True)
            os.close(wfd)
            self.pipe = os.fdopen(rfd, encoding="utf-8")
        else:
            self.proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)

    def wait_log(self, marker, timeout=20):
        deadline = time.monotonic() + timeout
        log = self.dir / "log"
        while time.monotonic() < deadline:
            if log.exists() and marker in log.read_text(encoding="utf-8"):
                return
            time.sleep(0.005)
        raise AssertionError(f"marker {marker!r} never logged")

    def signal(self, sig, n=1, group=False):
        for _ in range(n):
            (os.killpg if group else os.kill)(self.proc.pid, sig)

    def head_then_close(self, n=2):
        lines = [self.pipe.readline() for _ in range(n)]
        self.pipe.close()  # every later write by the tool hits EPIPE
        rc = self.proc.wait(timeout=30)
        cm = json.loads((self.dir / "cm.json").read_text(encoding="utf-8"))
        return rc, lines, cm

    def finish(self):
        out, err = self.proc.communicate(timeout=60)
        cm = json.loads((self.dir / "cm.json").read_text(encoding="utf-8"))
        return self.proc.returncode, out, err, cm


def _one_doc(out):
    return json.loads(out)  # raises unless stdout is exactly one JSON document


SIGS = pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM],
                               ids=["INT", "TERM"])


@needs_posix
@SIGS
def test_two_signals_back_to_back_during_verification_roll_back(tmp_path, sig):
    r = Run(tmp_path, OLD, {"raw": "2 3"})  # 3rd raw = the first wait poll
    r.wait_log("sleep raw 3")
    r.signal(sig, n=2)
    rc, out, err, cm = r.finish()
    doc = _one_doc(out)
    assert (rc, doc["status"], doc["exit_code"]) == (6, "interrupted-rolled-back", 6), err
    assert cm == r.old


@needs_posix
@SIGS
def test_a_burst_of_signals_is_absorbed(tmp_path, sig):
    r = Run(tmp_path, OLD, {"raw": "2 3"})
    r.wait_log("sleep raw 3")
    r.signal(sig, n=500)
    rc, out, err, cm = r.finish()
    assert (rc, _one_doc(out)["status"]) == (6, "interrupted-rolled-back"), err
    assert cm == r.old and "Rolled back." in err


@needs_posix
@SIGS
def test_a_signal_during_the_rollback_does_not_stop_it(tmp_path, sig):
    r = Run(tmp_path, ANCHOR, {"patch_before": "2 2"},
            argv=("t-a", "mysql_connections", "60"))
    r.wait_log("sleep patch_before 2")  # the rollback patch is in flight
    # To the whole group: without SIG_IGN the rollback's kubectl dies with it.
    r.signal(sig, n=3, group=True)
    rc, out, err, cm = r.finish()
    assert (rc, _one_doc(out)["status"]) == (1, "verify-failed-rolled-back"), err
    assert cm == r.old


@needs_posix
@SIGS
def test_a_signal_while_the_patch_call_lands_rolls_back(tmp_path, sig):
    r = Run(tmp_path, OLD, {"patch_after": "2 1"})
    r.wait_log("sleep patch_after 1")  # landed, kubectl not yet returned
    r.signal(sig)
    rc, out, err, cm = r.finish()
    assert (rc, _one_doc(out)["status"]) == (6, "interrupted-rolled-back"), err
    assert cm == r.old


@needs_posix
@SIGS
def test_a_signal_that_kills_the_patch_before_it_lands_writes_nothing(tmp_path, sig):
    # Ctrl-C on a terminal reaches the whole process group, kubectl included.
    r = Run(tmp_path, OLD, {"patch_before": "2 1"})
    r.wait_log("sleep patch_before 1")
    r.signal(sig, group=True)
    rc, out, err, cm = r.finish()
    assert rc == -sig, (rc, err)  # ends as the signal would
    assert out == "" and cm == r.old


@needs_posix
def test_kubectl_killed_by_the_same_ctrl_c_is_not_unreachable(tmp_path):
    r = Run(tmp_path, OLD, {"raw": "2 3"})
    r.wait_log("sleep raw 3")
    r.signal(signal.SIGINT, group=True)  # kills the in-flight `get --raw` too
    rc, out, err, cm = r.finish()
    assert (rc, _one_doc(out)["status"]) == (6, "interrupted-rolled-back"), err
    assert cm == r.old


@needs_posix
@pytest.mark.parametrize("old,argv,rc_want,cm_is_new", [
    (OLD, ("t-a", "mysql_connections", "65"), 0, True),
    (ANCHOR, ("t-a", "mysql_connections", "60"), 1, False),
], ids=["verified", "rolled-back"])
def test_a_closed_output_pipe_changes_nothing(tmp_path, old, argv, rc_want, cm_is_new):
    """Y1: `… 2>&1 | head -n 2` — output is best effort; verification and
    rollback still run and the exit code still says what the ConfigMap holds."""
    r = Run(tmp_path, old, {}, argv=argv, merged_pipe=True)
    rc, lines, cm = r.head_then_close()
    assert rc == rc_want, lines
    assert (cm != r.old) is cm_is_new


@needs_posix
def test_a_signal_just_after_success_does_not_change_the_answer(tmp_path):
    """Y5: SIGTERM 0–30ms after apply has said "Success!" — including during
    interpreter shutdown, which resets Python handlers to SIG_DFL — must not
    change the exit code the envelope already gave."""
    rng = random.Random(1950)
    for i in range(20):
        sub = tmp_path / str(i)
        sub.mkdir()
        r = Run(sub, OLD, {})
        for line in r.proc.stderr:
            if line.startswith("Success!"):
                time.sleep(rng.uniform(0, 0.03))
                try:
                    os.kill(r.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break
        rc, out, err, cm = r.finish()
        assert rc == _one_doc(out)["exit_code"] == 0, (i, rc, err[-300:])


@needs_posix
@pytest.mark.parametrize("close", [(1,), (2,), (1, 2)], ids=["1>&-", "2>&-", "both"])
@pytest.mark.parametrize("old,argv,rc_want,cm_is_new", [
    (OLD, ("t-a", "mysql_connections", "65"), 0, True),
    (ANCHOR, ("t-a", "mysql_connections", "60"), 1, False),
], ids=["verified", "rolled-back"])
def test_w1_closed_fds_change_nothing(tmp_path, close, old, argv, rc_want, cm_is_new):
    r = Run(tmp_path, old, {}, argv=argv, close=close)
    rc, out, err, cm = r.finish()
    assert rc == rc_want, err[-400:]
    assert (cm != r.old) is cm_is_new


@needs_posix
def test_w1_a_broken_stdout_alone_keeps_the_exit_code(tmp_path):
    """stdout 是讀端已關的 pipe（envelope 寫不出去）：rc 仍是實況的 0，
    不會是直譯器收尾 flush 失敗的 120。"""
    rfd, wfd = os.pipe()
    os.close(rfd)
    r = Run(tmp_path, OLD, {})
    r.proc.kill()
    r.proc.wait()
    (r.dir / "cm.json").write_text(json.dumps(OLD), encoding="utf-8")
    env = dict(os.environ, FAKE_S=str(r.dir),
               PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    p = subprocess.run([sys.executable, str(TOOL), "--json", "t-a",
                        "mysql_connections", "65", "--poll-interval", "0.05"],
                       env=env, stdout=wfd, stderr=subprocess.DEVNULL, timeout=60)
    os.close(wfd)
    assert p.returncode == 0
    assert json.loads((r.dir / "cm.json").read_text(encoding="utf-8")) != OLD


@needs_posix
@pytest.mark.parametrize("case", ["no-op", "unreachable-before-write"])
def test_w2_a_signal_just_after_an_early_answer_does_not_change_it(tmp_path, case):
    """W2：no-op 與寫入前的 unreachable 也一樣——envelope 印出後的 SIGTERM
    不得讓 rc 與 envelope 的 exit_code 不符。"""
    rng = random.Random(4)
    for i in range(20):
        sub = tmp_path / str(i)
        sub.mkdir()
        if case == "no-op":
            r = Run(sub, OLD, {}, argv=("t-a", "mysql_connections", "70"))
        else:
            r = Run(sub, OLD, {"raw": "0 99"},
                    argv=("t-a", "mysql_connections", "65", "--exporter-port", "nope"))
        want = "No-op" if case == "no-op" else "ERROR:"
        for line in r.proc.stderr:
            if line.startswith(want):
                time.sleep(rng.uniform(0, 0.03))
                try:
                    os.kill(r.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break
        rc, out, err, cm = r.finish()
        assert cm == r.old
        if out:  # answered: the signal must not change the stated code
            assert rc == _one_doc(out)["exit_code"], (i, rc, err[-300:])
        else:  # the signal came before the answer: nothing written, ended by it
            assert rc < 0, (i, rc, err[-300:])


@needs_posix
@SIGS
def test_m7_a_signal_during_the_wait_ends_well_before_the_timeout(tmp_path, sig):
    """--help 的延遲上限：等待期間的信號在一個 --poll-interval／一次 kubectl
    呼叫內生效，不會等到 --reload-timeout。"""
    r = Run(tmp_path, OLD, {}, extra=("--reload-timeout", "30"), freeze=True)
    r.wait_log("patch-landed")
    time.sleep(0.3)
    t0 = time.monotonic()
    r.signal(sig)
    rc, out, err, cm = r.finish()
    assert time.monotonic() - t0 < 5, err[-300:]
    assert (rc, _one_doc(out)["status"]) == (6, "interrupted-rolled-back")
    assert cm == r.old


# Runs the tool the way a coverage / atexit hook would see it: something writes
# to stdout AFTER main() has answered. With stdout's reader gone, that write
# (or the interpreter's exit flush of it) is what turns the exit code into 120.
_AFTER_MAIN = """
import atexit, runpy, sys
flag, tool = sys.argv[1], sys.argv[2]
sys.argv = [tool] + sys.argv[3:]
def late():
    try:
        sys.stdout.write("written after main\\n")
        sys.stdout.flush()
        result = "ok"
    except BaseException as exc:
        result = repr(exc)
    with open(flag, "w", encoding="utf-8") as fh:
        fh.write(result)
atexit.register(late)
runpy.run_path(tool, run_name="__main__")
"""


@needs_posix
@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["json", "text"])
def test_a_broken_stdout_cannot_fail_what_runs_after_main(tmp_path, json_flag):
    """A：stdout 的讀端一開始就關了。main 結束時把壞掉的 fd 導到 /dev/null，
    之後的寫入（atexit、直譯器收尾的 flush）不會失敗，結束碼維持實況。"""
    r = Run(tmp_path, OLD, {})
    r.proc.kill()
    r.proc.wait()
    (r.dir / "cm.json").write_text(json.dumps(OLD), encoding="utf-8")
    (r.dir / "rv").unlink(missing_ok=True)
    env = dict(os.environ, FAKE_S=str(r.dir),
               PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    flag = tmp_path / "late"
    for i in range(5):
        (r.dir / "cm.json").write_text(json.dumps(OLD), encoding="utf-8")
        (r.dir / "rv").unlink(missing_ok=True)
        rfd, wfd = os.pipe()
        os.close(rfd)
        p = subprocess.run([sys.executable, "-c", _AFTER_MAIN, str(flag), str(TOOL),
                            *json_flag, "t-a", "mysql_connections", "65",
                            "--poll-interval", "0.05"],
                           env=env, stdout=wfd, stderr=subprocess.DEVNULL, timeout=60)
        os.close(wfd)
        assert p.returncode == 0, (i, p.returncode)
        assert flag.read_text(encoding="utf-8") == "ok", i
