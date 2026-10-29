"""#2968：kubectl 逾時是環境錯誤（rc 2），batch 的期限傳到 kubectl 與 Prometheus。

kubectl 卡住時，單跑 `diagnose` 曾經等滿 120 秒再以 traceback 結束（rc 1）；
`batch-diagnose --timeout` 到期後放下租戶，但它啟動的 kubectl 留成孤兒行程
（ppid 1），直到自己的 120 秒上限。

假 kubectl 一律是 `exec sleep <marker>`：它的 PID 就是 sleep 本身。用 `sh` 包一層
再 sleep 會留下 sleep 孫程序，subprocess 殺不到，量到的是測試自己造的孤兒。
"""

import io
import json
import os
import random
import shutil
import signal
import subprocess
import threading
import time

import pytest

import _lib_exitcodes
import _lib_kubectl
from _kubectl_fake import kubectl_run  # noqa: E402  (#3349)
import batch_diagnose as bd
import diagnose

_MARIADB = ([{"metric": {"tenant": "t1", "db_type": "mariadb"}, "value": [0, "1"]}], None)


def _prom_mariadb_up(_url, query):
    """Prometheus answers at once: a MariaDB tenant whose exporter is up, so
    the only thing that can stall is kubectl."""
    if query.startswith("tenant_expected_exporter"):
        return _MARIADB
    if query.startswith("mysql_up"):
        return [{"metric": {}, "value": [0, "1"]}], None
    return [], None


def _alive(marker):
    """PIDs of live processes whose command line is exactly `sleep <marker>`."""
    out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True,
                         encoding="utf-8", timeout=10, check=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if args.strip() == f"sleep {marker}":
            pids.append(int(pid))
    return pids


@pytest.fixture
def stuck_kubectl(tmp_path, monkeypatch):
    """A kubectl that never answers, first on PATH; yields its sleep marker.

    Teardown kills whatever is left by explicit PID, so a red run of the
    orphan assertions below does not leak a process into the next test.
    """
    sh = shutil.which("sh")
    if os.name != "posix" or not sh or not shutil.which("ps") or not shutil.which("sleep"):
        pytest.skip("needs a POSIX sh, ps and sleep for the stuck-kubectl stand-in")
    marker = f"{random.randint(100000, 999999)}.{os.getpid()}"
    exe = tmp_path / "kubectl"
    exe.write_text(f"#!{sh}\nexec sleep {marker}\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(diagnose, "query_prometheus", _prom_mariadb_up)
    yield marker
    for pid in _alive(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ── 單跑 diagnose ──────────────────────────────────────────────────


class TestKubectlBudget:
    """The timeout each kubectl read gets (`kubectl_budget`, #3349: was inside
    run_cmd) and what the `_lib_kubectl` runner makes of running out of it."""

    @staticmethod
    def _record(monkeypatch):
        """kubectl answers at once with an empty List; records its kwargs."""
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, '{"items": []}', "")
        monkeypatch.setattr(_lib_kubectl.subprocess, "run", kubectl_run(fake))
        return seen

    def test_timeout_expired_is_an_environment_error(self, monkeypatch):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        monkeypatch.setattr(_lib_kubectl.subprocess, "run", stuck)
        with pytest.raises(_lib_kubectl.KubectlError, match=r"no answer within 120s"):
            diagnose.pod_phase("t1")

    def test_not_a_file_not_found_error(self):
        """batch_diagnose's OSError branch reports rc 1; a FileNotFoundError
        subclass would slip into it unnamed. It must be named explicitly."""
        assert not issubclass(diagnose.CommandTimeoutError, FileNotFoundError)

    def test_no_deadline_keeps_the_120s_cap(self, monkeypatch):
        seen = self._record(monkeypatch)
        assert diagnose.kubectl_budget() == diagnose.KUBECTL_TIMEOUT_S == 120
        diagnose.pod_phase("t1")
        assert seen["timeout"] == 120

    def test_deadline_shortens_the_timeout_by_the_grace(self, monkeypatch):
        seen = self._record(monkeypatch)
        with diagnose.deadline(time.monotonic() + 5, grace=0.5):
            diagnose.pod_phase("t1")
        assert 4.0 < seen["timeout"] <= 4.5

    def test_the_cap_still_bounds_a_far_deadline(self, monkeypatch):
        seen = self._record(monkeypatch)
        with diagnose.deadline(time.monotonic() + 10_000, grace=0.5):
            diagnose.pod_phase("t1")
        assert seen["timeout"] == diagnose.KUBECTL_TIMEOUT_S

    def test_past_the_deadline_starts_nothing(self, monkeypatch):
        monkeypatch.setattr(_lib_kubectl.subprocess, "run",
                            lambda *a, **kw: pytest.fail("a process was started"))
        with diagnose.deadline(time.monotonic(), grace=0.5):
            with pytest.raises(diagnose.CommandTimeoutError, match="not run"):
                diagnose.pod_phase("t1")
            with pytest.raises(diagnose.CommandTimeoutError, match="not run"):
                diagnose.mariadb_logs("t1")

    def test_deadline_is_reset_on_exit(self):
        with diagnose.deadline(time.monotonic() + 5, grace=0.5):
            assert diagnose._DEADLINE.get() is not None
        assert diagnose._DEADLINE.get() is None


class TestQueryPrometheusDeadline:
    @staticmethod
    def _record(monkeypatch):
        seen = []

        def fake(url, promql, **kw):
            seen.append(kw)
            return []
        monkeypatch.setattr(diagnose, "prometheus_instant", fake)
        return seen

    def test_no_deadline_keeps_its_own_default(self, monkeypatch):
        seen = self._record(monkeypatch)
        assert diagnose.query_prometheus("http://prom", "up") == ([], None)
        assert seen == [{}]

    def test_timeout_is_at_most_the_time_left(self, monkeypatch):
        """The time left, with NO grace taken off: a grace of 1s would put the
        timeout at or below 2, so the lower bound tells the two apart."""
        seen = self._record(monkeypatch)
        with diagnose.deadline(time.monotonic() + 3, grace=1.0):
            diagnose.query_prometheus("http://prom", "up")
        assert 2.5 < seen[0]["timeout"] <= 3

    def test_the_cap_still_bounds_a_far_deadline(self, monkeypatch):
        seen = self._record(monkeypatch)
        with diagnose.deadline(time.monotonic() + 10_000, grace=0.5):
            diagnose.query_prometheus("http://prom", "up")
        assert seen[0]["timeout"] == diagnose.PROMETHEUS_TIMEOUT_S

    def test_past_the_deadline_sends_nothing(self, monkeypatch):
        seen = self._record(monkeypatch)
        with diagnose.deadline(time.monotonic() - 1, grace=0.5):
            results, err = diagnose.query_prometheus("http://prom", "up")
        assert seen == []
        assert results is None and "deadline" in err


def _run_cli():
    """`diagnose.main()` the way the CLI enters it — through `run()`, the one
    exit path, which turns a raised `KubectlError` into rc 2, one `ERROR:`
    line and, armed for --json (diagnose's default), one envelope. That
    envelope goes straight to fd 1, so the caller reads it with `capfd`."""
    with pytest.raises(SystemExit) as exc:
        _lib_exitcodes.run(diagnose.main)
    return exc.value.code


def _is_the_kubectl_error_envelope(out):
    """stdout is the caller-error envelope (reason kubectl_error), not a
    health document."""
    doc = json.loads(out)
    assert doc["reason"] == "kubectl_error", doc
    assert "issues" not in doc and "tenant" not in doc, doc
    return doc


class TestSingleDiagnoseCli:
    def test_stuck_kubectl_exits_2_with_one_line(self, monkeypatch, capfd, cli_argv):
        monkeypatch.setattr(diagnose, "query_prometheus", _prom_mariadb_up)

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        monkeypatch.setattr(_lib_kubectl.subprocess, "run", stuck)
        cli_argv("diagnose", "t1", "--prometheus", "http://prom:9090")
        assert _run_cli() == 2
        captured = capfd.readouterr()
        _is_the_kubectl_error_envelope(captured.out)  # no health document
        lines = captured.err.splitlines()
        assert len(lines) == 1 and "no answer within 120s" in lines[0], captured.err

    def test_a_real_stuck_kubectl_is_killed_and_exits_2(self, stuck_kubectl, monkeypatch,
                                                         capfd, cli_argv):
        monkeypatch.setattr(diagnose, "KUBECTL_TIMEOUT_S", 1)   # 120s in production
        cli_argv("diagnose", "t1", "--prometheus", "http://prom:9090")
        start = time.monotonic()
        assert _run_cli() == 2
        assert time.monotonic() - start < 10
        captured = capfd.readouterr()
        assert "Traceback" not in captured.err
        assert "no answer within 1s" in captured.err, captured.err
        _is_the_kubectl_error_envelope(captured.out)
        assert _alive(stuck_kubectl) == []

    def test_a_logs_timeout_keeps_the_finding(self, monkeypatch, capsys, cli_argv):
        """`kubectl logs` only runs once a finding exists; its timeout must not
        turn that finding (rc 1) into an environment error (rc 2)."""
        monkeypatch.setattr(diagnose, "query_prometheus", _prom_mariadb_up)

        def kubectl(cmd, **kw):
            if cmd[1] == "logs":
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            pods = {"items": [{"status": {"phase": "CrashLoopBackOff"}}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(pods), "")
        monkeypatch.setattr(_lib_kubectl.subprocess, "run", kubectl_run(kubectl))
        cli_argv("diagnose", "t1", "--prometheus", "http://prom:9090")
        with pytest.raises(SystemExit) as exc:
            diagnose.main()
        assert exc.value.code == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["issues"] == ["Pod status is CrashLoopBackOff"]
        assert doc["recent_logs"] == []
        assert "no answer within 120s" in doc["recent_logs_error"]

    @pytest.mark.parametrize("failure", [
        _lib_kubectl.KubectlError("`kubectl logs deploy/mariadb` in namespace 't1': "
                                  "kubectl is not on PATH"),
        _lib_kubectl.KubectlError("`kubectl logs deploy/mariadb` in namespace 't1' failed "
                                  "(exit 1): Error from server (Forbidden): pods \"x\" is "
                                  "forbidden"),
        diagnose.CommandTimeoutError("kubectl not run: the deadline has passed"),
    ], ids=["not-on-path", "forbidden", "past-the-deadline"])
    def test_any_logs_failure_is_recorded_and_keeps_the_finding(self, monkeypatch, failure):
        """#3349: not only the timeout — every logs read that got no answer is
        recorded in `recent_logs_error` beside the finding (rc 1), never raised
        over it. A missing kubectl here used to reach the CLI (rc 2)."""
        monkeypatch.setattr(diagnose, "query_prometheus", _prom_mariadb_up)
        monkeypatch.setattr(diagnose, "pod_phase", lambda tenant: "CrashLoopBackOff")

        def logs(tenant):
            raise failure
        monkeypatch.setattr(diagnose, "mariadb_logs", logs)
        doc = diagnose.check("t1", "http://prom:9090", out=io.StringIO())
        assert doc["issues"] == ["Pod status is CrashLoopBackOff"]
        assert doc["recent_logs"] == []
        assert doc["recent_logs_error"] == str(failure)
        assert diagnose.exit_code(doc) == 1


# ── batch-diagnose ─────────────────────────────────────────────────


class TestBatchDeadline:
    def test_the_deadline_is_set_inside_the_tenant_thread(self, monkeypatch):
        """A new thread does not inherit the caller's context, so a deadline set
        around the thread instead of in it would silently never apply."""
        seen = {}

        def check(tenant, prom_url, *, out):
            seen["at"] = diagnose._DEADLINE.get()
            seen["thread"] = threading.current_thread().name
            out.write(json.dumps({"tenant": tenant, "status": "healthy"}))
        monkeypatch.setattr(bd, "diagnose_check", check)
        before = time.monotonic()
        bd.run_diagnose_for_tenant("t1", "http://prom", timeout_sec=7)
        assert seen["thread"] == "diagnose-t1"
        at, grace = seen["at"]
        assert before + 7 <= at <= time.monotonic() + 7
        assert grace == diagnose.DEADLINE_GRACE_S

    def test_grace_shrinks_for_a_short_timeout(self, monkeypatch):
        """A --timeout at or below the 0.5s grace used to leave kubectl no
        time at all: "not run: the deadline has passed" before anything ran."""
        seen = {}

        def check(tenant, prom_url, *, out):
            seen["at"] = diagnose._DEADLINE.get()
            out.write(json.dumps({"tenant": tenant, "status": "healthy"}))
        monkeypatch.setattr(bd, "diagnose_check", check)
        bd.run_diagnose_for_tenant("t1", "http://prom", timeout_sec=0.3)
        assert seen["at"][1] == pytest.approx(0.3 / 4)

    def test_kubectl_is_killed_before_the_join_gives_up(self, stuck_kubectl):
        result = bd.run_diagnose_for_tenant("t1", "http://prom", timeout_sec=1.5)
        assert result["_environment_error"] is True
        # kubectl's own budget ran out (the runner killed it), not the join's.
        assert "kubectl get pods" in result["issues"][0], result
        assert "no answer within" in result["issues"][0], result
        assert _alive(stuck_kubectl) == []

    def test_a_timeout_below_the_grace_still_runs_kubectl(self, stuck_kubectl):
        """0.5s: the shortest --timeout that still leaves kubectl some time on
        an oversubscribed CPU, and the longest at which a fixed 0.5s grace
        would leave it none ("not run: the deadline has passed") — so the
        min(0.5, timeout/4) branch is what this measures (grace 0.125s)."""
        result = bd.run_diagnose_for_tenant("t1", "http://prom", timeout_sec=0.5)
        assert result["_environment_error"] is True
        assert "no answer within" in result["issues"][0], result   # it ran
        assert "not run" not in result["issues"][0], result
        assert _alive(stuck_kubectl) == []

    def test_cli_leaves_no_orphan_and_exits_2(self, stuck_kubectl, cli_argv, capsys):
        cli_argv("batch_diagnose", "--tenants", "t1,t2", "--timeout", "1",
                 "--prometheus", "http://prom:9090", "--json")
        assert bd.main() == 2
        assert _alive(stuck_kubectl) == []
        doc = json.loads(capsys.readouterr().out)
        for tenant in ("t1", "t2"):
            issue = doc["tenants"][tenant]["issues"][0]
            # The kubectl timeout, not the join's "timed out after 1s": the
            # child gave up first and the run is still rc 2.
            assert "kubectl get pods" in issue and "no answer within" in issue, doc
