#!/usr/bin/env python3
"""patch-config apply 以 exporter 的 /api/v1/config/identity 驗收（#2069、#2132）。

寫後驗收改問「pod 服務的是不是本次寫入產生的那一版整份位元組」：由 PATCH 回應的
ConfigMap 算出 exporter 會回報的 config_hash，輪詢到相符為止；parse 失敗改看寫入後
的 parse_failed 集合，不再看計數器差值。fake kubectl 的 identity 由它自己照 exporter
規則算（`_patch_config_fake.fake_config_hash`），不呼叫 patch_config——Python 那份
規則對錯由 `tests/shared/config_identity_golden.json` 與 Go 端共同釘住。
"""
import json
import signal
from pathlib import Path
from unittest import mock

import pytest

import patch_config as pc  # noqa: E402
from _patch_config_fake import FakeCluster, NOT_FOUND, render_thresholds  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "shared" / "config_identity_golden.json"
FAST = ["--poll-interval", "0.001", "--reload-timeout", "0.05"]
DEFAULTS = "defaults:\n  mysql_connections: 80\n"
T_A = "tenants:\n  t-a:\n    mysql_connections: '70'\n"
T_B = "tenants:\n  t-b:\n    mysql_connections: '60'\n"


@pytest.fixture(autouse=True)
def _signals_reset():
    was = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    pc.SIGNALS.reset()
    assert {s: signal.getsignal(s) for s in was} == was


def _multi(**extra):
    return {"_defaults.yaml": DEFAULTS, "t-a.yaml": T_A, "t-b.yaml": T_B, **extra}


def _run(cluster, *argv):
    """(exit code, --json envelope) of `patch_config.py --json <argv>`."""
    out = []
    with mock.patch("patch_config.run_cmd", side_effect=cluster), \
            mock.patch("sys.argv", ["patch_config.py", "--json", *argv, *FAST]), \
            mock.patch("patch_config._say",
                       side_effect=lambda t, out_=False, **k: out.append(t)
                       if (out_ or k.get("out")) else None):
        try:
            pc.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    docs = [json.loads(t) for t in out if t.startswith("{")]
    assert len(docs) == 1, out
    return code, docs[0]


def _first_call_after(cluster, n_patches):
    seen = 0
    for i, c in enumerate(cluster.calls):
        if seen >= n_patches:
            return i
        if c[:2] == ["kubectl", "patch"]:
            seen += 1
    return len(cluster.calls)


class TestCrossLanguageGolden:
    """同一份 {key: 位元組}：Go 端 config_identity_test.go 斷言 exporter 回報這個值，
    這裡斷言 patch-config 算出同一個值。"""

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_directory_hash(self):
        files = pc.configmap_files({"data": self.golden["data"]})
        assert pc.config_hash(files) == self.golden["expected_directory_hash"]

    def test_the_kept_keys_are_the_exporters(self):
        kept = sorted(k for k in self.golden["data"] if pc.exporter_reads(k))
        assert kept == self.golden["expected_scanned_keys"]
        assert len(kept) < len(self.golden["data"])  # it still excludes something

    def test_single_file_hash(self):
        files = pc.configmap_files({"data": self.golden["data"]})
        assert self.golden["single_file_key"] == pc.LEGACY_KEY
        assert pc.config_hash(files, "single-file") == \
            self.golden["expected_single_file_hash"]

    def test_binary_data_is_decoded_like_kubelet_does(self):
        files = pc.configmap_files({"data": {"a.yaml": "x: 1\n"},
                                    "binaryData": {"b.yaml": "eDogMgo="}})
        assert files == {"a.yaml": b"x: 1\n", "b.yaml": b"x: 2\n"}


class TestWrittenFiles:
    def test_taken_from_the_patch_response(self):
        obj = {"metadata": {"resourceVersion": "9"},
               "data": {"t-a.yaml": "new", "t-z.yaml": "theirs"}}
        assert pc.written_files(obj, {"data": {"t-a.yaml": "old"}}, "t-a.yaml",
                                "new") == {"t-a.yaml": b"new", "t-z.yaml": b"theirs"}

    def test_derived_when_the_response_has_no_data(self):
        cm = {"data": {"t-a.yaml": "old", "t-b.yaml": "b"}}
        assert pc.written_files({"metadata": {}}, cm, "t-a.yaml", "new") == {
            "t-a.yaml": b"new", "t-b.yaml": b"b"}


def test_only_the_proxied_404_counts_as_an_older_exporter():
    assert pc._http_404(pc.KubectlError(NOT_FOUND))
    assert not pc._http_404(pc.KubectlError(
        'Error from server (NotFound): pods "exporter-0" not found'))
    assert not pc._http_404(pc.KubectlError("connection refused"))


class TestIdentityVerdicts:
    def test_success_waits_for_the_written_hash(self):
        c = FakeCluster(_multi())
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"]) == (0, "applied")
        assert doc["pods"]["exporter-0"]["identity"] == "checked"
        assert "'65'" in c.data["t-a.yaml"] and len(c.patches) == 1

    def test_a_reload_of_someone_elses_earlier_bytes_is_not_ours(self, capsys):
        """(a) 另一個寫者在本工具讀 ConfigMap 前寫了 t-b（comment 變動，series 不變），
        pod 此刻才載入那一版；本次寫入的位元組始終沒到。舊驗法看到 `Last reload`
        前進就放行（rc 0）；identity 的 config_hash 對不上 ⇒ 逾時、回滾。"""
        def run(identity):
            data = _multi(**{"t-b.yaml": "# edited by someone else\n" + T_B})
            c = FakeCluster(data, identity=identity, reload=lambda n: False,
                            concurrent=lambda n, cl: n == 0 and cl.deliver(cl.data))
            c.loaded = {p: _multi() for p in c.pods}  # not yet delivered
            return c, _run(c, "t-a", "mysql_connections", "65")
        c, (code, doc) = run(identity=True)
        assert (code, doc["status"], doc["rolled_back"]) == (
            pc.EXIT_RELOAD_TIMEOUT, "timeout", True)
        assert c.data["t-a.yaml"] == T_A
        assert "reports config_hash" in doc["message"]
        want = pc.config_hash(pc.configmap_files(
            {"data": dict(c.data, **{"t-a.yaml": c.patches[0]["data"]["t-a.yaml"]})}))
        assert want in doc["message"]
        # The residual this closes: the older check accepts it.
        _, (old_code, _) = run(identity=False)
        assert old_code == 0

    def test_fixing_a_key_the_exporter_rejected_passes(self):
        """(b) #2132：t-a.yaml 寫入前已在 parse_failed（exporter 拒收；本工具可讀），
        計數器每次讀都在漲；寫入可 parse 的內容（重新序列化丟掉了讓它被拒的那行）
        ⇒ 成功、不回滾。舊驗法（計數器）在同一情境誤判失敗。"""
        def rejects(key, text):
            return "exporter-rejects" in text
        broken = "# exporter-rejects\n" + T_A
        state = {"n": 0}

        def render(data, pod=None):
            state["n"] += 1
            return render_thresholds(data) + (
                f'da_config_parse_failure_total{{file_basename="t-a.yaml"}} '
                f'{state["n"]}\n')
        c = FakeCluster(_multi(**{"t-a.yaml": broken}), rejects=rejects,
                        render=render)
        assert c.identity_doc("exporter-0")["parse_failed"] == ["t-a.yaml"]
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"], doc["rolled_back"]) == (0, "applied", False)
        assert c.identity_doc("exporter-0")["parse_failed"] == []

        old = FakeCluster(_multi(**{"t-a.yaml": broken}), rejects=rejects,
                          render=render, identity=False)
        assert _run(old, "t-a", "mysql_connections", "65")[0] == pc.EXIT_VERIFY_FAILED

    def test_our_bytes_that_do_not_parse_roll_back(self):
        """(c) hash 相符（pod 確實載入了本次位元組），但被 patch 的 key 在
        parse_failed ⇒ 失敗、回滾。"""
        c = FakeCluster(_multi(), rejects=lambda k, t: k == "t-a.yaml" and "'65'" in t)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"], doc["rolled_back"]) == (
            pc.EXIT_VERIFY_FAILED, "verify-failed-rolled-back", True)
        assert c.data["t-a.yaml"] == T_A
        assert "could not parse the written key t-a.yaml" in doc["message"]

    def test_another_key_newly_failing_fails_and_one_already_failing_warns(self):
        # t-c 寫入前就壞 ⇒ 只警告；t-d 寫入後才壞（別人的變更一起被載入）⇒ 失敗。
        bad = "# exporter-rejects\n"
        c = FakeCluster(_multi(**{"t-c.yaml": bad + "tenants: {}\n"}),
                        rejects=lambda k, t: "exporter-rejects" in t)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert code == 0
        assert doc["pods"]["exporter-0"]["warnings"] == [
            "exporter-0: t-c.yaml does not parse (parse_failed; it already did "
            "not before the write)."]

        def rejects(key, text):
            return "exporter-rejects" in text
        c = FakeCluster(_multi(**{"t-d.yaml": "tenants: {}\n"}), rejects=rejects,
                        concurrent=lambda n, cl: n == 0 and cl.data.update(
                            {"t-d.yaml": bad + "tenants: {}\n"}))
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"]) == (pc.EXIT_VERIFY_FAILED,
                                          "verify-failed-rolled-back")
        assert "t-d.yaml no longer parses after the write" in doc["message"]

    def test_a_single_file_pod_expects_the_hash_of_config_yaml(self):
        old = "tenants:\n  t-a:\n    mysql_connections: '50'\n"
        c = FakeCluster({"config.yaml": old}, mode="single-file")
        code, doc = _run(c, "t-a", "mysql_connections", "60")
        assert (code, doc["status"]) == (0, "applied")
        assert c.identity_doc("exporter-0")["mode"] == "single-file"

    def test_an_unknown_schema_is_unreachable_before_the_write(self):
        c = FakeCluster(_multi())
        real = c.identity_doc
        c.identity_doc = lambda pod: dict(real(pod), schema=2)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"], doc["written"]) == (
            pc.EXIT_EXPORTER_UNREACHABLE, "unreachable", False)
        assert c.patches == []


class TestOlderExporter:
    def test_404_falls_back_and_says_so(self, capsys):
        """(d) 404 ⇒ 該 pod 退回舊驗法；envelope 逐 pod 標示，stderr 提示。"""
        c = FakeCluster(_multi(), pods=("p0", "p1"),
                        identity=lambda pod: pod == "p1")
        with mock.patch("sys.argv", ["patch_config.py", "--json", "t-a",
                                     "mysql_connections", "65", *FAST]), \
                mock.patch("patch_config.run_cmd", side_effect=c):
            pc.main()
        cap = capsys.readouterr()
        doc = json.loads(cap.out)
        assert doc["status"] == "applied"
        assert {p: v["identity"] for p, v in doc["pods"].items()} == {
            "p0": "unavailable (404)", "p1": "checked"}
        assert "WARNING: p0: no api/v1/config/identity (404" in cap.err
        # p0 was waited for by `Last reload`, p1 by its identity.
        raw = [x[3] for x in c.calls if x[:3] == ["kubectl", "get", "--raw"]]
        assert any("p0:8080/proxy/api/v1/config" == r.split("/pods/")[1] for r in raw)
        assert not any("p1:8080/proxy/api/v1/config" == r.split("/pods/")[1] for r in raw)

    def test_any_other_identity_error_is_unreachable(self):
        c = FakeCluster(_multi(), fail_raw=lambda pod, path, n:
                        path == pc.IDENTITY_PATH)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"], doc["written"]) == (
            pc.EXIT_EXPORTER_UNREACHABLE, "unreachable", False)


class TestConsistentRead:
    def test_a_reload_between_the_two_identity_reads_rereads(self):
        """(e) identity → /metrics → identity 之間 pod 又 reload 了一次（同一份位元組、
        last_reload 前進）⇒ 那份 /metrics 不採用、重讀。"""
        flips = {"n": 0}

        def on_raw(pod, path, cl):
            if path == "metrics" and cl.patches and flips["n"] == 0:
                flips["n"] += 1
                cl.deliver(cl.loaded[pod], pods=[pod])
        c = FakeCluster(_multi(), on_raw=on_raw)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"]) == (0, "applied")
        metrics_after = [x for x in c.calls[_first_call_after(c, 1):]
                         if x[:3] == ["kubectl", "get", "--raw"]
                         and x[3].endswith("/proxy/metrics")]
        assert len(metrics_after) == 2  # the torn read, then the kept one

    def test_a_pod_that_never_settles_times_out(self):
        def on_raw(pod, path, cl):
            if path == "metrics" and len(cl.patches) == 1:
                cl.deliver(cl.loaded[pod], pods=[pod])
        c = FakeCluster(_multi(), on_raw=on_raw)
        code, doc = _run(c, "t-a", "mysql_connections", "65")
        assert (code, doc["status"], doc["rolled_back"]) == (
            pc.EXIT_RELOAD_TIMEOUT, "timeout", True)
        assert f"each of {pc.CONSISTENT_READ_ATTEMPTS}" in doc["message"]
        assert c.data["t-a.yaml"] == T_A
