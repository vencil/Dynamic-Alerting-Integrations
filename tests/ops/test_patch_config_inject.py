#!/usr/bin/env python3
"""patch-config apply：寫入→驗收→回滾→回答路徑上每個函式的故障注入（#1950 盲審第 5 輪 V1）。

每個函式在第 n 次被呼叫時（或從第 n 次起每次）丟 RuntimeError／MemoryError／
KeyboardInterrupt，在「驗收通過」「驗收失敗」「驗收失敗且回滾也失敗」三種情境下各跑一次。唯一的斷言是：結束碼與
envelope 說的話，和 ConfigMap 最後的實況一致（見 `_judge`）。
"""
import contextlib
import io
import json
from unittest import mock

import pytest

import patch_config as pc  # noqa: E402
from _patch_config_fake import FakeCluster  # noqa: E402

FAST = ["--poll-interval", "0.001", "--reload-timeout", "0.05"]
_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
SCENARIOS = {
    # name: (ConfigMap, key, value, bytes that only the new version has)
    "applied": ({"_defaults.yaml": _DEFAULTS,
                 "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: '70'\n",
                 "t-b.yaml": "tenants:\n  t-b:\n    mysql_connections: '60'\n"},
                "t-a.yaml", "65", "'65'"),
    "verify-fail": ({"config.yaml": ("tenants:\n  t-a: &s\n    mysql_connections: '50'\n"
                                     "  t-b: *s\n")},
                    "config.yaml", "60", "'60'"),
}
# verify fails, and so does the rollback patch (the ConfigMap keeps the new bytes)
SCENARIOS["rollback-fail"] = SCENARIOS["verify-fail"]
FAKE_KW = {"rollback-fail": {"fail_patch": {1}}}
# Every function between the write and the answer (module functions, and the
# methods of the two objects apply drives).
MODULE_FNS = ["_kubectl_patch", "_read_key", "parse_metrics", "verify_pod",
              "_conclude", "_send_rollback",
              "_wait_rolled_back", "_observed_fallback",
              "_say", "_emit", "_apply_envelope", "format_json_report",
              "_status_exit_code", "_outcome", "_answer"]
EXPORTER_FNS = ["wait_moved", "snapshot", "last_reload", "get"]
SIGNAL_FNS = ["cause", "ignore", "checkpoint"]
OUTPUT_FNS = {"_say", "_emit", "_apply_envelope", "format_json_report",
              "_answer"}
TARGETS = ([("module", f) for f in MODULE_FNS] + [("Exporter", f) for f in EXPORTER_FNS]
           + [("_Signals", f) for f in SIGNAL_FNS])
EXCS = {"RuntimeError": RuntimeError, "MemoryError": MemoryError,
        "KeyboardInterrupt": KeyboardInterrupt}


@pytest.fixture(autouse=True)
def _signals_reset():
    yield
    pc.SIGNALS.reset()


def _inject(where, name, nth, exc_type, sticky):
    owner = {"module": pc, "Exporter": pc.Exporter, "_Signals": pc._Signals}[where]
    real = getattr(owner, name, None)
    if real is None:
        pytest.skip(f"{name} does not exist in this version")
    calls = {"n": 0}

    def wrapper(*a, **k):
        calls["n"] += 1
        if calls["n"] == nth or (sticky and calls["n"] > nth):
            raise exc_type(f"injected into {name} (call {nth})")
        return real(*a, **k)
    return mock.patch.object(owner, name, wrapper)


def _judge(scenario, cluster, outcome, out, target):
    """None if rc / envelope agree with the ConfigMap, else why not."""
    _, key, _, marker = SCENARIOS[scenario]
    is_new = marker in (cluster.data.get(key) or "")
    if outcome == "KeyboardInterrupt":
        return None if not cluster.patches else "KeyboardInterrupt escaped after a write"
    if not isinstance(outcome, int):
        return f"{outcome} escaped main()"
    code = outcome
    if code == 0 and not is_new:
        return "rc 0 but the ConfigMap holds the old bytes"
    if code in (1, 2, 3, 4, 6) and is_new:
        return f"rc {code} but the ConfigMap holds the new bytes"
    if code == 5 and not is_new:
        return "rc 5 but the ConfigMap holds the old bytes"
    try:
        doc = json.loads(out)
    except ValueError:
        return None if target in OUTPUT_FNS else f"no envelope (stdout {out[:60]!r})"
    if doc.get("status") == "caller_error":
        return None if code == 2 else f"caller_error envelope with rc {code}"
    if doc.get("exit_code") != code:
        return f"envelope exit_code {doc.get('exit_code')} != rc {code}"
    if doc.get("written") is None:
        return None if code == 7 else "written null outside state-unknown"
    if doc.get("written") is not is_new:
        return f"envelope written={doc.get('written')} but ConfigMap new={is_new}"
    return None


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("exc_name", EXCS)
@pytest.mark.parametrize("nth", [1, 2, 3])
@pytest.mark.parametrize("sticky", [False, True], ids=["once", "from-then-on"])
@pytest.mark.parametrize("target", TARGETS, ids=[f"{w}.{n}" for w, n in TARGETS])
def test_the_answer_follows_the_configmap(scenario, exc_name, nth, sticky, target):
    data, key, value, _ = SCENARIOS[scenario]
    cluster = FakeCluster(dict(data), **FAKE_KW.get(scenario, {}))
    out = io.StringIO()
    argv = ["patch_config.py", "--json", "t-a", "mysql_connections", value, *FAST]
    with mock.patch("patch_config.run_cmd", side_effect=cluster), \
            mock.patch("sys.argv", argv), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
            _inject(*target, nth, EXCS[exc_name], sticky):
        try:
            pc.main()
            outcome = 0
        except SystemExit as exc:
            outcome = exc.code
        except KeyboardInterrupt:
            outcome = "KeyboardInterrupt"
        except BaseException as exc:  # noqa: BLE001 — recorded, then judged
            outcome = type(exc).__name__
    why = _judge(scenario, cluster, outcome, out.getvalue(), target[1])
    assert why is None, f"{why} (patches sent: {len(cluster.patches)})"
