#!/usr/bin/env python3
"""patch-config apply 的寫後驗收與回滾（#1950 路線 E）——fake kubectl 單元測試。

每格都經 `main()` 跑，斷言結束碼、ConfigMap 最後的位元組與送出的 patch 數，
不只看訊息。真 exporter 的那一半在 `test_patch_config_exporter.py`。
"""
import contextlib
import json
import os
import re
import signal
from pathlib import Path
from unittest import mock

import pytest
import yaml

import patch_config as pc  # noqa: E402
from _patch_config_fake import FakeCluster, render_thresholds  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _signals_reset():
    """apply keeps its SIGINT/SIGTERM handlers to the end of the process;
    in-process, hand the next test the caller's handlers back (the seam)."""
    was = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    pc.SIGNALS.reset()
    assert {s: signal.getsignal(s) for s in was} == was
FAST = ["--poll-interval", "0.001", "--reload-timeout", "0.05"]
DEFAULTS = "defaults:\n  mysql_connections: 80\n"
T_A = "tenants:\n  t-a:\n    mysql_connections: '70'\n"
T_B = "tenants:\n  t-b:\n    mysql_connections: '60'\n"


def _run(cluster, *argv):
    """Exit code of `patch_config.py <argv>` run against `cluster`."""
    with mock.patch("patch_config.run_cmd", side_effect=cluster), \
            mock.patch("sys.argv", ["patch_config.py", *argv, *FAST]):
        try:
            pc.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code


def _multi():
    return {"_defaults.yaml": DEFAULTS, "t-a.yaml": T_A, "t-b.yaml": T_B}


def _with(extra):
    """render_thresholds plus the lines `extra(data)` returns."""
    return lambda data, pod=None: render_thresholds(data) + extra(data)


class TestApplyVerified:
    def test_success_writes_once_and_exits_0(self, capsys):
        c = FakeCluster(_multi())
        assert _run(c, "t-a", "mysql_connections", "65") == 0
        assert len(c.patches) == 1
        assert yaml.safe_load(c.data["t-a.yaml"])["tenants"]["t-a"] == {
            "mysql_connections": "65"}
        assert "Verified on 1 exporter pod" in capsys.readouterr().out

    def test_every_pod_is_verified(self):
        c = FakeCluster(_multi(), pods=("p0", "p1"))
        assert _run(c, "t-a", "mysql_connections", "65") == 0
        raw = [x[3] for x in c.calls if x[:3] == ["kubectl", "get", "--raw"]]
        assert {r.split("/pods/")[1].split(":")[0] for r in raw} == {"p0", "p1"}
        assert all(":8080/proxy/" in r for r in raw)  # port name → number

    def test_same_bytes_is_a_noop_without_exporter(self, capsys):
        c = FakeCluster(_multi())
        same = {"data": {"t-a.yaml": T_A}}
        with mock.patch("patch_config.build_patch", return_value=same):
            assert _run(c, "t-a", "mysql_connections", "65") == 0
        assert c.patches == []
        assert not any(x[:3] == ["kubectl", "get", "pods"] for x in c.calls)
        assert "No-op" in capsys.readouterr().err

    def test_other_tenant_change_rolls_back_with_1(self, capsys):
        # #1950 列 9：legacy config.yaml 用 anchor 共用區塊，改 t-a 連帶 t-b。
        old = ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
               "  t-b: *s\n")
        c = FakeCluster({"config.yaml": old})
        assert _run(c, "t-a", "mysql_connections", "60") == pc.EXIT_VERIFY_FAILED
        assert c.data["config.yaml"] == old          # 回到舊位元組
        assert len(c.patches) == 2
        err = capsys.readouterr().err
        assert 'tenant="t-b"' in err and "another tenant" in err

    def test_new_parse_failure_rolls_back(self):
        c = FakeCluster(_multi(), render=_with(
            lambda d: "" if "65" not in d["t-a.yaml"] else
            'da_config_parse_failure_total{file_basename="t-a.yaml"} 1\n'))
        assert _run(c, "t-a", "mysql_connections", "65") == pc.EXIT_VERIFY_FAILED
        assert c.data["t-a.yaml"] == T_A

    def test_rise_on_the_patched_key_fails_even_if_already_failing(self):
        state = {"n": 3}

        def extra(d):
            state["n"] += 1
            return f'da_config_parse_failure_total{{file_basename="t-a.yaml"}} {state["n"]}\n'
        c = FakeCluster(_multi(), render=_with(extra))
        assert _run(c, "t-a", "mysql_connections", "65") == pc.EXIT_VERIFY_FAILED

    def test_rise_on_an_already_broken_other_file_only_warns(self, capsys):
        state = {"n": 3}

        def extra(d):
            state["n"] += 1
            return f'da_config_parse_failure_total{{file_basename="broken.yaml"}} {state["n"]}\n'
        c = FakeCluster(_multi(), render=_with(extra))
        assert _run(c, "t-a", "mysql_connections", "65") == 0
        assert len(c.patches) == 1
        assert "WARNING" in capsys.readouterr().err

    def test_timeout_rolls_back_with_3(self):
        c = FakeCluster(_multi(), reload=lambda n: False)
        assert _run(c, "t-a", "mysql_connections", "65") == pc.EXIT_RELOAD_TIMEOUT
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_unreachable_before_the_write_writes_nothing(self):
        c = FakeCluster(_multi(), fail_raw=lambda pod, path, n: True)
        assert _run(c, "t-a", "mysql_connections", "65") == \
            pc.EXIT_EXPORTER_UNREACHABLE
        assert c.patches == []

    def test_no_pod_is_unreachable(self):
        c = FakeCluster(_multi(), no_pods=True)
        assert _run(c, "t-a", "mysql_connections", "65") == \
            pc.EXIT_EXPORTER_UNREACHABLE
        assert c.patches == []

    def test_unreachable_after_the_write_rolls_back_with_4(self):
        c = FakeCluster(_multi(), fail_raw=lambda pod, path, n: n == 1)
        assert _run(c, "t-a", "mysql_connections", "65") == \
            pc.EXIT_EXPORTER_UNREACHABLE
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_rollback_failure_is_5(self, capsys):
        old = ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
               "  t-b: *s\n")
        c = FakeCluster({"config.yaml": old}, fail_patch={1})
        assert _run(c, "t-a", "mysql_connections", "60") == pc.EXIT_ROLLBACK_FAILED
        assert "still holds the new bytes" in capsys.readouterr().err

    def test_rollback_of_a_created_key_removes_it(self):
        c = FakeCluster({"_defaults.yaml": DEFAULTS, "t-b.yaml": T_B},
                        reload=lambda n: False)
        assert _run(c, "t-new", "mysql_connections", "65") == pc.EXIT_RELOAD_TIMEOUT
        assert c.patches[1] == {"data": {"t-new.yaml": None}}
        assert "t-new.yaml" not in c.data

    def test_rollback_waits_for_the_pods_that_reloaded(self):
        old = ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
               "  t-b: *s\n")
        c = FakeCluster({"config.yaml": old})
        _run(c, "t-a", "mysql_connections", "60")
        assert c.gen["exporter-0"] == 2  # patch + rollback both reloaded
        assert render_thresholds(c.loaded["exporter-0"]) == \
            render_thresholds({"config.yaml": old})


class TestVerifyPod:
    """`verify_pod` 的判準本身（exporter 行為推得的上限，見其 docstring）。"""

    @staticmethod
    def _snap(lines):
        return ("t",) + pc.parse_metrics("\n".join(lines))

    def _problems(self, before, after, key="mysql_connections", tenant="t-a"):
        return pc.verify_pod("p", self._snap(before), self._snap(after),
                             tenant, key, "t-a.yaml")[0]

    def test_four_changes_of_the_target_pass_five_fail(self):
        before = [f'user_threshold{{metric="m{i}",tenant="t-a"}} 1' for i in range(5)]
        four = before[:1] + [s.replace(" 1", " 2") for s in before[1:]]
        five = [s.replace(" 1", " 2") for s in before]
        assert self._problems(before, four) == []
        assert self._problems(before, five)

    def test_a_new_tenant_may_add_any_number(self):
        after = [f'user_threshold{{metric="m{i}",tenant="t-a"}} 1' for i in range(9)]
        assert self._problems([], after) == []

    def test_underscore_key_own_tenant_is_listed_not_judged(self):
        base = ['user_threshold{metric="m",tenant="t-a"} 1']
        more = base + [f'user_silent_mode{{target_severity="{s}",tenant="t-a"}} 1'
                       for s in ("warning", "critical")]
        many = [f'user_threshold{{metric="m{i}",tenant="t-a"}} 1' for i in range(9)]
        assert self._problems(base, more, key="_silent_mode") == []
        assert self._problems(more, base, key="_silent_mode") == []
        assert self._problems(many, [], key="_profile") == []
        _, _, target = pc.verify_pod("p", self._snap(more), self._snap(base),
                                     "t-a", "_silent_mode", "t-a.yaml")
        assert {(t["before"], t["after"]) for t in target} == {("1", None)}
        assert len(target) == 2

    def test_underscore_key_still_fails_on_another_tenant(self):
        before = ['user_threshold{metric="m",tenant="t-b"} 1']
        assert self._problems(before, [], key="_silent_mode")

    def test_threshold_key_may_remove_its_series(self):
        base = ['user_threshold{metric="m",tenant="t-a"} 1']
        assert self._problems(base, []) == []

    def test_any_change_of_another_tenant_fails(self):
        before = ['user_threshold{metric="m",tenant="t-b"} 1']
        assert self._problems(before, [])
        assert self._problems([], before)

    def test_label_values_with_escapes_are_read(self):
        series, _ = pc.parse_metrics(
            'user_threshold{a="x\\"y,z}",tenant="t-a"} 5\n# HELP user_x h\n')
        assert series == {("user_threshold",
                           (("a", 'x\\"y,z}'), ("tenant", "t-a"))): "5"}


class TestExporterAccessDefaults:
    """預設值取自 chart（D-05 pin：具名對象等於某值）。"""

    @staticmethod
    def _render(name):
        text = (REPO / "helm/threshold-exporter/templates" / name).read_text(
            encoding="utf-8")
        kept = [ln for ln in text.splitlines() if not ln.strip().startswith("{{")]
        return yaml.safe_load(re.sub(r"\{\{.*?\}\}", "X", "\n".join(kept)))

    def test_namespace_selector_and_port_name_are_the_charts(self):
        dep = self._render("deployment.yaml")
        svc = self._render("service.yaml")
        labels = dep["spec"]["template"]["metadata"]["labels"]
        key, _, value = pc.EXPORTER_SELECTOR.partition("=")
        assert labels.get(key) == value
        assert dep["metadata"]["namespace"] == pc.NAMESPACE == svc["metadata"]["namespace"]
        ports = [p["name"] for c in dep["spec"]["template"]["spec"]["containers"]
                 for p in c.get("ports", [])]
        assert pc.EXPORTER_PORT in ports

    def test_flags_default_to_them(self):
        args = pc.build_parser().parse_args(["t", "k", "v"])
        assert (args.exporter_namespace, args.exporter_selector,
                args.exporter_port, args.reload_timeout) == (
            pc.NAMESPACE, pc.EXPORTER_SELECTOR, pc.EXPORTER_PORT, 120)

    @pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "x"])
    def test_non_positive_timeout_is_a_caller_error(self, bad):
        with pytest.raises(SystemExit) as exc:
            pc.build_parser().parse_args(["t", "k", "v", "--reload-timeout", bad])
        assert exc.value.code == 2


def test_diff_does_not_touch_the_exporter():
    c = FakeCluster(_multi())
    with mock.patch("patch_config.run_cmd", side_effect=c), \
            mock.patch("sys.argv", ["patch_config.py", "--diff", "--json",
                                    "t-a", "mysql_connections", "65"]):
        pc.main()
    assert [x[:3] for x in c.calls] == [["kubectl", "get", "configmap"]]




SILENT = "tenants:\n  t-a:\n    mysql_connections: '70'\n    _silent_mode: warning\n"


class TestUnderscoreKeysAndJson:
    """方案 C（owner 拍板）：`_` key 只驗 reload／parse failure／其他租戶；apply `--json`。"""

    def test_silent_mode_disable_is_applied_and_listed(self, capsys):
        c = FakeCluster({"_defaults.yaml": DEFAULTS, "t-a.yaml": SILENT,
                         "t-b.yaml": T_B})
        assert _run(c, "t-a", "_silent_mode", "disable") == 0
        assert len(c.patches) == 1
        err = capsys.readouterr().err
        assert 'user_silent_mode{target_severity="warning",tenant="t-a"}: 1 -> (absent)' in err

    def test_underscore_key_that_moves_another_tenant_rolls_back(self):
        old = ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
               "    _silent_mode: warning\n  t-b: *s\n")
        c = FakeCluster({"config.yaml": old})
        assert _run(c, "t-a", "_silent_mode", "disable") == pc.EXIT_VERIFY_FAILED
        assert c.data["config.yaml"] == old and len(c.patches) == 2

    def test_json_success_envelope(self, capsys):
        c = FakeCluster({"_defaults.yaml": DEFAULTS, "t-a.yaml": SILENT,
                         "t-b.yaml": T_B})
        assert _run(c, "--json", "t-a", "_silent_mode", "disable") == 0
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["exit_code"], doc["written"],
                doc["rolled_back"]) == ("applied", 0, True, False)
        assert doc["pods"]["exporter-0"]["target_changes"] == [{
            "series": 'user_silent_mode{target_severity="warning",tenant="t-a"}',
            "before": "1", "after": None}]

    def test_json_rollback_envelope(self, capsys):
        old = ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
               "  t-b: *s\n")
        c = FakeCluster({"config.yaml": old})
        assert _run(c, "--json", "t-a", "mysql_connections", "60") == 1
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["exit_code"], doc["written"],
                doc["rolled_back"]) == ("verify-failed-rolled-back", 1, False, True)
        assert any('tenant="t-b"' in p
                   for p in doc["pods"]["exporter-0"]["problems"])

    @pytest.mark.parametrize("cluster,status,code,written", [
        (lambda: FakeCluster(_multi(), no_pods=True), "unreachable", 4, False),
        (lambda: FakeCluster(_multi(), reload=lambda n: False), "timeout", 3, False),
        (lambda: FakeCluster({"config.yaml": "tenants:\n  t-a: &s\n    m: '5'\n"
                                             "  t-b: *s\n"}, fail_patch={1}),
         "rollback-failed", 5, True),
    ], ids=["unreachable", "timeout", "rollback-failed"])
    def test_json_every_failure_is_one_envelope(self, capsys, cluster, status,
                                                code, written):
        key = "m" if status == "rollback-failed" else "mysql_connections"
        assert _run(cluster(), "--json", "t-a", key, "6") == code
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["exit_code"], doc["written"]) == (
            status, code, written)
        assert doc["message"]

    def test_json_noop_envelope(self, capsys):
        c = FakeCluster(_multi())
        assert _run(c, "--json", "t-a", "mysql_connections", "70") == 0
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["changed"], doc["pods"]) == ("no-op", False, {})


class TestAfterTheWriteAborts:
    """盲審 V1–V4：寫入之後的中斷、意外例外、patch 回報失敗但其實已套用。"""

    @staticmethod
    def _interrupt_on_first_raw_after_write(c, exc):
        orig = c.__call__

        def side(cmd):
            if cmd[:3] == ["kubectl", "get", "--raw"] and c.patches:
                raise exc
            return orig(cmd)
        return side

    def _json(self, c, side, *argv, capsys):
        with mock.patch("patch_config.run_cmd", side_effect=side), \
                mock.patch("sys.argv", ["patch_config.py", "--json", *argv, *FAST]):
            try:
                pc.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except BaseException as exc:  # noqa: BLE001 — must not escape main
                pytest.fail(f"{type(exc).__name__} escaped main(); "
                            f"ConfigMap left at {c.data.get('t-a.yaml')!r}")
        return code, json.loads(capsys.readouterr().out)

    def test_v1_a_signal_seen_at_a_checkpoint_rolls_back(self, capsys):
        """同一路徑的行程內版本（真信號在 test_patch_config_signals.py）：
        handler 只記 flag，寫入後的下一個 checkpoint 才走回滾。"""
        c = FakeCluster(_multi())
        orig = c.__call__

        def side(cmd):
            if cmd[:3] == ["kubectl", "get", "--raw"] and c.patches:
                pc.SIGNALS.pending = signal.SIGTERM  # what the handler records
            return orig(cmd)
        code, doc = self._json(c, side, "t-a", "mysql_connections", "65",
                               capsys=capsys)
        assert code == pc.EXIT_ABORTED_ROLLED_BACK
        assert (doc["status"], doc["exit_code"], doc["rolled_back"],
                doc["written"]) == ("interrupted-rolled-back", 6, True, False)
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_v1_interrupt_before_the_write_writes_nothing(self):
        c = FakeCluster(_multi())
        orig = c.__call__

        def side(cmd):
            if cmd[:3] == ["kubectl", "get", "--raw"]:
                raise KeyboardInterrupt
            return orig(cmd)
        with mock.patch("patch_config.run_cmd", side_effect=side), \
                mock.patch("sys.argv", ["patch_config.py", "t-a",
                                        "mysql_connections", "65", *FAST]):
            with pytest.raises(KeyboardInterrupt):
                pc.main()
        assert c.patches == []

    def test_v2_only_running_pods_not_being_deleted_are_verified(self):
        c = FakeCluster(_multi(), not_serving={
            "p-pending": ("Pending", None), "p-failed": ("Failed", None),
            "p-gone": ("Running", "2026-01-01T00:00:00Z")},
            fail_raw=lambda pod, path, n: pod != "exporter-0")
        assert _run(c, "t-a", "mysql_connections", "65") == 0
        raw = {x[3].split("/pods/")[1].split(":")[0]
               for x in c.calls if x[:3] == ["kubectl", "get", "--raw"]}
        assert raw == {"exporter-0"}

    def test_v2_no_running_pod_is_unreachable(self):
        c = FakeCluster(_multi(), pods=(), not_serving={"p": ("Pending", None)})
        assert _run(c, "t-a", "mysql_connections", "65") == \
            pc.EXIT_EXPORTER_UNREACHABLE
        assert c.patches == []

    def test_v3_unexpected_error_after_the_write_is_6_not_2(self, capsys):
        c = FakeCluster(_multi())
        with mock.patch("patch_config.verify_pod", side_effect=RuntimeError("bug")):
            code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert code == pc.EXIT_ABORTED_ROLLED_BACK
        assert (doc["status"], doc["rolled_back"]) == ("error-rolled-back", True)
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_v3_unexpected_error_with_failed_rollback_is_5(self):
        c = FakeCluster(_multi(), fail_patch={1})
        with mock.patch("patch_config.verify_pod", side_effect=RuntimeError("bug")):
            assert _run(c, "t-a", "mysql_connections", "65") == \
                pc.EXIT_ROLLBACK_FAILED

    def test_v4_failed_patch_that_landed_is_rolled_back(self, capsys):
        c = FakeCluster(_multi(), fail_patch={0}, patch_lands=True)
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["rolled_back"]) == (
            6, "error-rolled-back", True)
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_v4_failed_patch_that_did_not_land_is_2(self, capsys):
        c = FakeCluster(_multi(), fail_patch={0})
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["reason"]) == (
            2, "caller_error", "kubectl_failed")
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 1

    def test_v4_failed_patch_and_unreadable_configmap_is_state_unknown(self, capsys):
        c = FakeCluster(_multi(), fail_patch={0}, patch_lands=True,
                        fail_get_cm_after=1)
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (
            pc.EXIT_STATE_UNKNOWN, "state-unknown", None)
        assert len(c.patches) == 1  # no blind rollback


class TestRollbackAndEnvelopeEdges:
    """盲審第 2 輪 X2／X5／X6（新舊皆非 → 7）。"""

    def test_x2_rollback_failing_with_any_exception_is_5(self, capsys):
        c = FakeCluster(_multi(), reload=lambda n: False)
        real, calls = pc.tempfile.NamedTemporaryFile, {"n": 0}

        def ntf(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:  # the rollback's patch file
                raise OSError(28, "No space left on device")
            return real(*a, **k)
        with mock.patch("patch_config.tempfile.NamedTemporaryFile", side_effect=ntf):
            code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (
            pc.EXIT_ROLLBACK_FAILED, "rollback-failed", True)
        assert "65" in c.data["t-a.yaml"]  # it really is still written

    def test_x5_caller_error_envelope_says_nothing_written(self, capsys):
        c = FakeCluster(_multi(), fail_patch={0})
        assert _run(c, "--json", "t-a", "mysql_connections", "65") == 2
        doc = json.loads(capsys.readouterr().out)
        assert (doc["status"], doc["written"]) == ("caller_error", False)

    def test_x6_failed_patch_leaving_neither_version_is_7(self, capsys):
        c = FakeCluster(_multi())
        orig = c.__call__

        def side(cmd):
            if cmd[:2] == ["kubectl", "patch"] and not c.patches:
                orig(cmd)
                c.data["t-a.yaml"] = "tenants:\n  t-a:\n    mysql_connections: '1'\n"
                raise pc.KubectlError("timed out")
            return orig(cmd)
        with mock.patch("patch_config.run_cmd", side_effect=side), \
                mock.patch("sys.argv", ["patch_config.py", "--json", "t-a",
                                        "mysql_connections", "65", *FAST]):
            with pytest.raises(SystemExit) as exc:
                pc.main()
        doc = json.loads(capsys.readouterr().out)
        assert (exc.value.code, doc["status"], doc["written"]) == (
            pc.EXIT_STATE_UNKNOWN, "state-unknown", None)
        assert "neither" in doc["message"] and len(c.patches) == 1


class TestTheGuardDecidesFromTheConfigMap:
    """盲審第 3 輪 Y1／Y2／Y5：寫入後任何例外都由重讀的實況決定結束碼。"""

    def test_keyboard_interrupt_after_the_write_is_rolled_back(self, capsys):
        c = FakeCluster(_multi())
        orig = c.__call__

        def side(cmd):
            if cmd[:3] == ["kubectl", "get", "--raw"] and c.patches:
                raise KeyboardInterrupt
            return orig(cmd)
        with mock.patch("patch_config.run_cmd", side_effect=side), \
                mock.patch("sys.argv", ["patch_config.py", "--json", "t-a",
                                        "mysql_connections", "65", *FAST]):
            try:
                pc.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except BaseException as exc:  # noqa: BLE001 — must not escape main
                pytest.fail(f"{type(exc).__name__} escaped main(); "
                            f"ConfigMap left at {c.data['t-a.yaml']!r}")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"]) == (6, "interrupted-rolled-back")
        assert c.data["t-a.yaml"] == T_A

    def test_an_error_while_waiting_after_the_rollback_keeps_the_rollback(self, capsys):
        """rb_wait_exc：回滾後等待 reload 時讀到壞的 /metrics（ValueError）。"""
        n = {"i": 0}

        def render(data, pod=None):
            n["i"] += 1
            base = render_thresholds(data, pod)
            if n["i"] == 2:  # after the write: another tenant moved
                return base.replace('tenant="t-b"} 60', 'tenant="t-b"} 61')
            if n["i"] >= 3:  # the rollback wait: not a float
                return base + 'da_config_parse_failure_total{file_basename="x"} 1,0\n'
            return base
        c = FakeCluster(_multi(), render=render)
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["rolled_back"]) == (
            1, "verify-failed-rolled-back", True)
        assert c.data["t-a.yaml"] == T_A

    def test_a_failing_temp_file_removal_masks_nothing(self, capsys):
        """rm_exc：os.remove 丟 PermissionError 不得蓋掉 patch 的結果。"""
        real, n = os.remove, {"i": 0}

        def rm(path):
            n["i"] += 1
            real(path)
            if n["i"] == 1:
                raise PermissionError(13, "Permission denied", path)
        c = FakeCluster(_multi())
        with mock.patch("patch_config.os.remove", side_effect=rm):
            code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (0, "applied", True)

    def test_the_rollback_patch_is_sent_before_anything_is_printed(self):
        events = []
        c = FakeCluster(_multi(), reload=lambda n: False)
        orig = c.__call__

        def side(cmd):
            if cmd[:2] == ["kubectl", "patch"]:
                events.append("patch")
            return orig(cmd)
        real_say = pc._say

        def say(text, out=False):
            if "rolling back" in text:
                events.append("say")
            real_say(text, out)
        with mock.patch("patch_config._say", side_effect=say):
            with mock.patch("patch_config.run_cmd", side_effect=side), \
                    mock.patch("sys.argv", ["patch_config.py", "t-a",
                                            "mysql_connections", "65", *FAST]):
                with pytest.raises(SystemExit):
                    pc.main()
        assert events == ["patch", "patch", "say"]

    def test_a_broken_stderr_does_not_stop_the_rollback(self, capsys):
        c = FakeCluster(_multi(), reload=lambda n: False)

        class Broken:
            def write(self, _):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

            def fileno(self):
                raise OSError("no fd")
        with mock.patch("sys.stderr", Broken()):
            code = _run(c, "--json", "t-a", "mysql_connections", "65")
        assert code == pc.EXIT_RELOAD_TIMEOUT
        assert c.data["t-a.yaml"] == T_A and len(c.patches) == 2

    def test_the_cli_keeps_its_handlers_after_answering(self):
        """Y5／Y6：CLI 路徑不還原 handler（還原後才到的信號會讓 rc 與 envelope
        不符）；行程內要還原只能走 SIGNALS.reset()。"""
        c = FakeCluster(_multi())
        assert _run(c, "t-a", "mysql_connections", "65") == 0
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        pc.SIGNALS.reset()
        assert signal.getsignal(signal.SIGTERM) != signal.SIG_IGN


class _Raises:
    """A stream whose every write fails with `exc`."""

    def __init__(self, exc):
        self.exc = exc

    def write(self, _):
        raise self.exc

    def flush(self):
        raise self.exc


_ANCHOR_CM = {"config.yaml": ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
                              "  t-b: *s\n")}
# (cluster, value, expected rc, ConfigMap holds the new bytes at the end)
_OUTCOMES = {
    "ok": (lambda: FakeCluster(_multi()), "65", 0, True),
    "verify-fail": (lambda: FakeCluster(dict(_ANCHOR_CM)), "60", 1, False),
    "rollback-fail": (lambda: FakeCluster(_multi(), reload=lambda n: False,
                                          fail_patch={1}), "65", 5, True),
}
_STREAMS = {
    "stdout-None": {"stdout": None},
    "stderr-None": {"stderr": None},
    "both-None": {"stdout": None, "stderr": None},
    "raises-RuntimeError": {"stdout": _Raises(RuntimeError("boom")),
                            "stderr": _Raises(RuntimeError("boom"))},
    "EPIPE": {"stdout": _Raises(BrokenPipeError(32, "Broken pipe")),
              "stderr": _Raises(BrokenPipeError(32, "Broken pipe"))},
}


@pytest.mark.parametrize("outcome", _OUTCOMES)
@pytest.mark.parametrize("as_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("streams", _STREAMS)
def test_w1_output_streams_never_change_the_outcome(outcome, as_json, streams):
    """W1：輸出的任何毛病（fd 關閉 ⇒ None、寫入丟任何例外、EPIPE）都不改變
    apply 做了什麼，也不改變結束碼。"""
    make, value, rc_want, new_want = _OUTCOMES[outcome]
    c = make()
    key = "config.yaml" if outcome == "verify-fail" else "t-a.yaml"
    old = c.data[key]
    argv = ["patch_config.py", *(["--json"] if as_json else []), "t-a",
            "mysql_connections", value, *FAST]
    patches = [mock.patch("patch_config.run_cmd", side_effect=c),
               mock.patch("sys.argv", argv)]
    patches += [mock.patch(f"sys.{name}", stream)
                for name, stream in _STREAMS[streams].items()]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            pc.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    assert code == rc_want
    assert (c.data[key] != old) is new_want


class TestEnvelopeFieldsFollowTheConfigMap:
    """M21／M4b：state-unknown 的 written 是 null；回滾呼叫丟例外但其實已套用
    ⇒ 是已回滾（1／3／4／6），不是 5。"""

    @pytest.mark.parametrize("kw", [
        dict(reload=lambda n: False, fail_patch={1}, fail_get_cm_after=2),
        dict(fail_patch={0}, patch_lands=True, fail_get_cm_after=1),
    ], ids=["rollback-fails-reread-fails", "write-fails-landed-reread-fails"])
    def test_m21_state_unknown_says_written_null(self, capsys, kw):
        c = FakeCluster(_multi(), **kw)
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (7, "state-unknown", None)

    def test_m4b_rollback_that_raised_but_landed_is_rolled_back(self, capsys):
        c = FakeCluster(_multi(), reload=lambda n: False, fail_patch={1},
                        patch_lands=True)
        code = _run(c, "--json", "t-a", "mysql_connections", "65")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"], doc["rolled_back"]) == (
            pc.EXIT_RELOAD_TIMEOUT, "timeout", False, True)
        assert c.data["t-a.yaml"] == T_A


def test_m28_a_failed_write_that_landed_and_a_failed_rollback_says_written(capsys):
    """V4／M28：寫入呼叫失敗但其實已套用、回滾又沒套用 ⇒ 5，envelope 的
    written 是 true（由觀察到的 ConfigMap 決定，不是由走到哪一段決定）。"""
    c = FakeCluster(_multi(), fail_patch={0, 1}, patch_lands={0})
    code = _run(c, "--json", "t-a", "mysql_connections", "65")
    doc = json.loads(capsys.readouterr().out)
    assert (code, doc["status"], doc["written"], doc["rolled_back"]) == (
        pc.EXIT_ROLLBACK_FAILED, "rollback-failed", True, False)
    assert "'65'" in c.data["t-a.yaml"]


class TestRound6Pins:
    """盲審第 6 輪 U4／U5：這幾條行為各有一支撤掉就會紅的測試。"""

    @staticmethod
    def _main(c, *argv, side=None):
        with mock.patch("patch_config.run_cmd", side_effect=side or c), \
                mock.patch("sys.argv", ["patch_config.py", "--json", *argv, *FAST]):
            try:
                pc.main()
                return 0
            except SystemExit as exc:
                return exc.code

    def test_m2_the_fallback_really_rolls_back(self, capsys):
        """_conclude 自己失敗時，備援要真的把舊位元組送回去（不只是說出 5）。"""
        c = FakeCluster(dict(_ANCHOR_CM))
        old = c.data["config.yaml"]
        with mock.patch("patch_config._conclude", side_effect=RuntimeError("bug")):
            code = self._main(c, "t-a", "mysql_connections", "60")
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (6, "error-rolled-back", False)
        assert c.data["config.yaml"] == old

    @pytest.mark.parametrize("fail_patch,patch_lands", [
        ({0}, {0}),   # the write call fails but lands; its re-read is garbage
        ({1}, False),  # the rollback fails; its re-read is garbage
    ], ids=["after-a-failed-write", "after-a-failed-rollback"])
    def test_m5_a_re_read_that_is_not_json_is_state_unknown(self, capsys,
                                                             fail_patch, patch_lands):
        c = FakeCluster(dict(_ANCHOR_CM), fail_patch=fail_patch,
                        patch_lands=patch_lands)
        orig = c.__call__

        def side(cmd):
            if cmd[:3] == ["kubectl", "get", "configmap"] and c.patches:
                return "<html>502 Bad Gateway</html>"
            return orig(cmd)
        code = self._main(c, "t-a", "mysql_connections", "60", side=side)
        doc = json.loads(capsys.readouterr().out)
        assert (code, doc["status"], doc["written"]) == (7, "state-unknown", None)

    def test_m18_an_error_before_the_write_sends_no_patch(self, capsys):
        """寫入前的錯誤走到備援時，備援不得送出任何 patch。"""
        c = FakeCluster(_multi())
        with mock.patch("patch_config.detect_mode", side_effect=RuntimeError("x")), \
                mock.patch("patch_config._conclude", side_effect=RuntimeError("bug")):
            code = self._main(c, "t-a", "mysql_connections", "65")
        assert code == 2 and c.patches == []
        assert json.loads(capsys.readouterr().out)["written"] is False

    @staticmethod
    def _fail_on_success(text, out=False):
        if text.startswith("Success!"):
            raise RuntimeError("cannot print")

    def test_m16_failing_to_print_success_keeps_a_verified_write(self, capsys):
        c = FakeCluster(_multi())
        with mock.patch("patch_config._say", side_effect=self._fail_on_success):
            code = self._main(c, "t-a", "mysql_connections", "65")
        assert code == 0 and len(c.patches) == 1 and "'65'" in c.data["t-a.yaml"]

    def test_m17_the_fallback_keeps_a_verified_write(self, capsys):
        c = FakeCluster(_multi())
        with mock.patch("patch_config._say", side_effect=self._fail_on_success), \
                mock.patch("patch_config._conclude", side_effect=RuntimeError("bug")):
            code = self._main(c, "t-a", "mysql_connections", "65")
        assert code == 0 and len(c.patches) == 1 and "'65'" in c.data["t-a.yaml"]


@pytest.mark.parametrize("doc", ["docs/cli-reference.md", "docs/cli-reference.en.md"])
def test_every_exit_code_is_in_the_cli_reference_table(doc):
    """apply 的結束碼在執行時才定（`sys.exit(code)`），check_cli_contract 的 V4
    以 AST 讀不到它們——這裡改從程式的 EXIT_* 常數推導，對 cli-reference 的
    patch-config 結束碼表做集合相等。"""
    codes = {v for k, v in vars(pc).items()
             if k.startswith("EXIT_") and isinstance(v, int)}
    text = (REPO / doc).read_text(encoding="utf-8")
    section = text.split("#### patch-config", 1)[1].split("\n---", 1)[0]
    rows = {int(m) for m in re.findall(r"^\| `(\d+)` \|", section, re.M)}
    assert rows == codes, (sorted(rows), sorted(codes))
