"""Tests for operator_check.py — Prometheus Operator CRD deployment verifier.

Audit flagged this as a 0% covered tool. Every kubectl / HTTP call is
monkeypatched so no real cluster / Prometheus is contacted.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import operator_check as oc  # noqa: E402
import _lib_prometheus  # noqa: E402  (#3133: the targets are read by the lib)
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


def _make_args(**overrides) -> SimpleNamespace:
    """Build a minimal argparse-shaped Namespace for OperatorChecker."""
    defaults = dict(
        namespace="monitoring",
        rule_packs_dir="rule-packs",
        prometheus=None,
        kubeconfig=None,
        json=False,
        ci=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_ALL_CRDS = (oc._CRD_RULES, oc._CRD_MONITORS, oc._CRD_AM_CONFIGS)


def _packs(directory):
    """What `main` hands the checker: the rule packs --rule-packs-dir selects."""
    import _lib_consumer_read
    return _lib_consumer_read.rule_pack_files(directory)


def _stub_cluster(monkeypatch, crds=_ALL_CRDS, lists=None, fail=None):
    """Replace `kubectl_list` (the shared reader, #3207) with an in-memory
    cluster: `crds` names the installed CRDs, `lists` maps kind → items,
    `fail` maps kind → the KubectlError message its read raises. Returns
    the list of (kind, kwargs) calls."""
    lists, fail, calls = dict(lists or {}), dict(fail or {}), []

    def fake(kind, **kw):
        calls.append((kind, kw))
        if kind in fail:
            raise oc.KubectlError(fail[kind])
        return lists.get(kind, [])

    def fake_get(kind, name, **kw):
        assert kind == "crd", kind
        calls.append(("crd", {"name": name, **kw}))
        if "crd" in fail:
            raise oc.KubectlError(fail["crd"])
        return {"metadata": {"name": name}} if name in crds else None

    monkeypatch.setattr(oc, "kubectl_list", fake)
    monkeypatch.setattr(oc, "kubectl_get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# i18n — pure helper
# ---------------------------------------------------------------------------
class TestI18n:
    def test_known_key_en(self):
        assert oc.i18n("title", "en") == oc.STRINGS["en"]["title"]

    def test_known_key_zh(self):
        assert oc.i18n("title", "zh") == oc.STRINGS["zh"]["title"]

    def test_unknown_lang_falls_back_to_en(self):
        assert oc.i18n("title", "fr") == oc.STRINGS["en"]["title"]

    def test_unknown_key_returns_key_as_fallback(self):
        assert oc.i18n("nonexistent_key", "en") == "nonexistent_key"


# ---------------------------------------------------------------------------
# CheckResult — data class
# ---------------------------------------------------------------------------
class TestCheckResult:
    def test_to_dict_shape(self):
        r = oc.CheckResult("Test", "pass", "ok")
        assert r.to_dict() == {
            "check": "Test",
            "status": "pass",
            "detail": "ok",
            "caller_error": False,  # #452/#737: additive caller-error marker
        }

    def test_default_detail_empty(self):
        r = oc.CheckResult("Test", "fail")
        assert r.detail == ""
        assert r.to_dict()["detail"] == ""


# ---------------------------------------------------------------------------
# OperatorChecker.run_kubectl — subprocess wrapper
# ---------------------------------------------------------------------------
class TestClusterReads:
    """#3207: every read is `kubectl_list` — a read with no answer is a caller
    error (rc 2), never "not found"; a missing CRD is an answer."""

    def test_kubeconfig_namespace_and_selector_reach_the_reader(self, monkeypatch):
        calls = _stub_cluster(monkeypatch)
        oc.OperatorChecker(_make_args(kubeconfig="/path/kc", namespace="ns1")).check_servicemonitor_status()
        assert ("servicemonitor", {"kubeconfig": "/path/kc", "namespace": "ns1",
                                   "selector": oc._PART_OF}) in calls

    def test_each_crd_is_read_once_by_name_even_when_it_fails(self, monkeypatch):
        """A CRD is a `get` by name (needs only `get`, review of #3207), once
        per CRD, a failure raised again rather than re-asked."""
        calls = _stub_cluster(monkeypatch, fail={"crd": "unreachable"})
        checker = oc.OperatorChecker(_make_args(kubeconfig="/kc"))
        for check in (checker.check_operator_detection, checker.check_prometheus_rule_status,
                      checker.check_servicemonitor_status, checker.check_alertmanager_config):
            check()
        assert calls == [("crd", {"name": n, "kubeconfig": "/kc"})
                         for n in (oc._CRD_RULES, oc._CRD_MONITORS, oc._CRD_AM_CONFIGS)]

    @pytest.mark.parametrize("failing", ["crd", "prometheusrule", "servicemonitor", "alertmanagerconfig"])
    def test_a_read_with_no_answer_is_a_caller_error(self, monkeypatch, failing):
        _stub_cluster(monkeypatch, fail={failing: f"`kubectl get {failing}` failed (exit 1): Forbidden"})
        checker = oc.OperatorChecker(_make_args(ci=False))
        checker.run_all_checks()
        hit = [c for c in checker.checks if c.caller_error]
        assert hit and all(c.status == "fail" and "Forbidden" in c.detail for c in hit), checker.checks
        assert checker.exit_code() == 2


class TestCheckOperatorDetection:
    def test_crd_found_passes(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_operator_detection()
        assert result.status == "pass" and not result.caller_error

    def test_crd_missing_is_a_finding(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=())
        result = oc.OperatorChecker(_make_args()).check_operator_detection()
        assert result.status == "fail" and not result.caller_error
        assert oc._CRD_RULES in result.detail


# ---------------------------------------------------------------------------
# check_prometheus_rule_status
# ---------------------------------------------------------------------------
class TestCheckPrometheusRuleStatus:
    def test_loaded_equals_expected_passes(self, monkeypatch, tmp_path):
        # 2 rule packs on disk; the cluster lists 2 → pass.
        (tmp_path / "rule-pack-database.yaml").write_text("x", encoding="utf-8")
        (tmp_path / "rule-pack-mongodb.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}, {"x": 2}]})
        result = oc.OperatorChecker(_make_args(), rule_packs=_packs(tmp_path)).check_prometheus_rule_status()
        assert result.status == "pass"
        assert "2/2" in result.detail

    def test_partial_load_warns(self, monkeypatch, tmp_path):
        for i in range(3):
            (tmp_path / f"rule-pack-{i}.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}]})
        result = oc.OperatorChecker(_make_args(), rule_packs=_packs(tmp_path)).check_prometheus_rule_status()
        assert result.status == "warn"
        assert "1/3" in result.detail

    def test_nothing_loaded_fails(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args(), rule_packs=_packs(tmp_path)).check_prometheus_rule_status()
        assert result.status == "fail" and not result.caller_error

    def test_crd_not_installed_is_a_finding_and_is_not_listed(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        calls = _stub_cluster(monkeypatch, crds=(), fail={"prometheusrule": "must not be read"})
        result = oc.OperatorChecker(_make_args(), rule_packs=_packs(tmp_path)).check_prometheus_rule_status()
        assert result.status == "fail" and not result.caller_error
        assert oc._CRD_RULES in result.detail and [k for k, _ in calls] == ["crd"]
        assert "prometheusrule" not in [k for k, _ in calls]

    def test_missing_default_rule_packs_dir_is_not_compared_and_says_so(self, monkeypatch):
        # #3462: the default --rule-packs-dir nobody gave, nothing there →
        # main passes no rule packs; the count is not compared and the row
        # says so (it was a bare "1", the path never named). One the caller
        # GAVE, or an empty one, is refused by main() before the checker runs.
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}]})
        result = oc.OperatorChecker(_make_args(), None).check_prometheus_rule_status()
        assert result.status == "warn"
        assert result.detail.startswith("1 (the default rule-packs is not there")


# ---------------------------------------------------------------------------
# check_servicemonitor_status
# ---------------------------------------------------------------------------
class TestCheckServiceMonitorStatus:
    def test_found_passes(self, monkeypatch):
        _stub_cluster(monkeypatch, lists={"servicemonitor": [{"x": 1}]})
        assert oc.OperatorChecker(_make_args()).check_servicemonitor_status().status == "pass"

    def test_empty_fails(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail" and not result.caller_error

    def test_crd_not_installed_is_a_finding(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=(oc._CRD_RULES,))
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail" and not result.caller_error and oc._CRD_MONITORS in result.detail


# ---------------------------------------------------------------------------
# check_alertmanager_config
# ---------------------------------------------------------------------------
class TestCheckAlertmanagerConfig:
    """#3572: operator-generate emits no AlertmanagerConfig any more, so one
    carrying its part-of label is a leftover (warn); none, or no CRD, passes.
    The detail wording is pinned in test_operator_routing_single_renderer.py."""

    def test_leftovers_warn(self, monkeypatch):
        _stub_cluster(monkeypatch, lists={"alertmanagerconfig": [{"x": 1}, {"x": 2}, {"x": 3}]})
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "warn"
        assert result.detail.startswith("3 ")

    def test_none_passes(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "pass"

    def test_crd_not_installed_passes_and_says_so(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=(oc._CRD_RULES, oc._CRD_MONITORS))
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "pass" and oc._CRD_AM_CONFIGS in result.detail


# ---------------------------------------------------------------------------
# check_mount_list (#1560)
# ---------------------------------------------------------------------------
def _mount_list(*names, root="/etc/alertmanager/secrets", other=()):
    return {"schema": oc.MOUNT_LIST_SCHEMA, "mount_root": root,
            "mounts": [{"name": n, "keys": ["k"]} for n in names],
            "other_files": list(other)}


def _am(name, *mounted):
    return {"metadata": {"name": name}, "spec": {"secrets": list(mounted)}}


def _stub_alertmanagers(monkeypatch, crs, *, crd=True, fail=None):
    """An in-memory cluster with *crs* as the Alertmanager CRs. Returns the
    calls; a read of a Secret fails the test (the check never needs one)."""
    calls = []

    def fake_list(kind, **kw):
        calls.append((kind, kw))
        assert kind == "alertmanager", kind
        if fail:
            raise oc.KubectlError(fail)
        return crs

    def fake_get(kind, name, **kw):
        calls.append((kind, {"name": name, **kw}))
        assert kind in ("crd", "alertmanager"), kind
        if fail:
            raise oc.KubectlError(fail)
        if kind == "crd":
            return {"metadata": {"name": name}} if crd and name == oc._CRD_AMS else None
        return next((c for c in crs if c["metadata"]["name"] == name), None)

    monkeypatch.setattr(oc, "kubectl_list", fake_list)
    monkeypatch.setattr(oc, "kubectl_get", fake_get)
    return calls


def _mounts(monkeypatch, mount_list, crs, **kw):
    args = kw.pop("args", {})
    _stub_alertmanagers(monkeypatch, crs, **kw)
    return oc.OperatorChecker(_make_args(**args), mount_list).check_mount_list()


class TestCheckSecretMounts:
    """#1560: the generate-routes mount list against spec.secrets. Only the
    Alertmanager CR is read — never a Secret."""

    def test_complete_passes_and_names_the_cr(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a", "corp-alerting"),
                    [_am("main", "da-t-a", "corp-alerting", "tls-certs")])
        assert r.status == "pass", r.detail
        assert "main" in r.detail and not r.caller_error

    def test_a_listed_secret_missing_from_spec_secrets_fails(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a", "da-t-b"), [_am("main", "da-t-a")])
        assert r.status == "fail" and not r.caller_error
        assert "da-t-b" in r.detail and "da-t-a" not in r.detail.split("lacks")[1]

    def test_an_extra_da_secret_warns(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [_am("main", "da-t-a", "da-t-gone")])
        assert r.status == "warn"
        assert "da-t-gone" in r.detail

    def test_an_extra_secret_of_another_name_is_not_reported(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [_am("main", "da-t-a", "tls-certs")])
        assert r.status == "pass"
        assert "tls-certs" not in r.detail

    def test_a_missing_one_outranks_an_extra_one(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [_am("main", "da-t-x")])
        assert r.status == "fail"
        assert "da-t-a" in r.detail and "da-t-x" in r.detail

    def test_every_cr_is_judged_and_one_lacking_fails(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"),
                    [_am("am-one", "da-t-a"), _am("am-two")])
        assert r.status == "fail"
        assert "am-one" in r.detail and "am-two" in r.detail

    def test_a_lacking_cr_among_several_points_at_the_alertmanager_flag(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"),
                    [_am("am-one", "da-t-a"), _am("am-two")])
        assert r.status == "fail" and "--alertmanager <name>" in r.detail

    def test_one_lacking_cr_alone_is_not_pointed_at_the_flag(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [_am("am-one")])
        assert r.status == "fail" and "--alertmanager" not in r.detail

    def test_the_hint_is_written_in_both_languages(self):
        assert all("--alertmanager <" in oc.STRINGS[lang]["mounts_pick_one"]
                   for lang in ("en", "zh"))

    def test_alertmanager_flag_judges_that_cr_only(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"),
                    [_am("am-one", "da-t-a"), _am("am-two")],
                    args={"alertmanager": "am-one"})
        assert r.status == "pass", r.detail
        assert "am-two" not in r.detail

    def test_alertmanager_flag_naming_no_cr_fails(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [_am("am-one", "da-t-a")],
                    args={"alertmanager": "absent"})
        assert r.status == "fail" and "absent" in r.detail

    @pytest.mark.parametrize("crd", [True, False])
    def test_alertmanager_flag_naming_no_cr_fails_with_an_empty_list(self, monkeypatch, crd):
        r = _mounts(monkeypatch, _mount_list(), [_am("am-one")], crd=crd,
                    args={"alertmanager": "absent"})
        assert r.status == "fail" and "absent" in r.detail

    @pytest.mark.parametrize("crd", [True, False])
    def test_no_cr_with_a_non_empty_list_fails(self, monkeypatch, crd):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [], crd=crd)
        assert r.status == "fail" and "monitoring" in r.detail

    def test_an_empty_list_with_no_cr_passes(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list(), [])
        assert r.status == "pass"

    def test_another_mount_root_fails(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a", root="/mnt/s"), [_am("main", "da-t-a")])
        assert r.status == "fail"
        assert "/mnt/s" in r.detail and oc.DEFAULT_MOUNT_ROOT in r.detail

    @pytest.mark.parametrize("root", ["/etc/alertmanager/secrets/", "/etc/alertmanager//secrets",
                                      "/etc/alertmanager/./secrets"])
    def test_another_spelling_of_the_default_root_passes(self, monkeypatch, root):
        r = _mounts(monkeypatch, _mount_list("da-t-a", root=root), [_am("main", "da-t-a")])
        assert r.status == "pass", r.detail

    def test_other_files_are_named_as_not_judged(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a", other=["/etc/ssl/ca.pem"]),
                    [_am("main", "da-t-a")])
        assert r.status == "pass" and "other_files" in r.detail

    def test_no_flag_is_skip_with_how_to_make_the_list(self, monkeypatch):
        calls = _stub_alertmanagers(monkeypatch, [_am("main")])
        r = oc.OperatorChecker(_make_args(), None).check_mount_list()
        assert r.status == "skip"
        assert "--secret-mounts-out" in r.detail and "--config-dir" in r.detail
        assert calls == []  # nothing asked of the cluster

    def test_kubectl_not_answering_is_a_caller_error(self, monkeypatch):
        r = _mounts(monkeypatch, _mount_list("da-t-a"), [], fail="unreachable")
        assert r.status == "fail" and r.caller_error
        assert "unreachable" in r.detail

    def test_no_secret_is_ever_read(self, monkeypatch):
        calls = _stub_alertmanagers(monkeypatch, [_am("main", "da-t-a")])
        oc.OperatorChecker(_make_args(), _mount_list("da-t-a")).check_mount_list()
        assert {kind for kind, _ in calls} <= {"crd", "alertmanager"}


class TestMountListFlag:
    """#1560: --secret-mounts is read before the cluster is asked."""

    def _write(self, tmp_path, doc) -> str:
        p = tmp_path / "mounts.json"
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
        return str(p)

    @staticmethod
    def _args(path):
        """``--secret-mounts <path>`` through the tool's own parser (#3797)."""
        return oc.build_parser("en").parse_args(["--secret-mounts", path])

    def test_a_valid_list_loads(self, tmp_path):
        doc = _mount_list("da-t-a")
        assert oc.load_mount_list(self._args(self._write(tmp_path, doc))) == doc

    @pytest.mark.parametrize("doc", [
        "{not json",
        [],
        {"schema": "other/v1", "mount_root": "/x", "mounts": [], "other_files": []},
        {"schema": "da-tools.secret-mounts/v1", "mount_root": 1, "mounts": [], "other_files": []},
        {"schema": "da-tools.secret-mounts/v1", "mount_root": "/x", "mounts": [{"name": "a"}],
         "other_files": []},
        {"schema": "da-tools.secret-mounts/v1", "mount_root": "/x", "mounts": [],
         "other_files": "x"},
    ])
    def test_an_unusable_list_is_a_caller_error(self, tmp_path, doc):
        with pytest.raises(oc.CallerError, match="--secret-mounts"):
            oc.load_mount_list(self._args(self._write(tmp_path, doc)))

    def test_unusable_list_through_the_cli_is_rc_2_before_any_check(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, _OPERATOR_CHECK, "--secret-mounts",
             self._write(tmp_path, "{not json"), "--kubeconfig", str(tmp_path / "none")],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=tmp_path)
        assert proc.returncode == 2, (proc.stdout, proc.stderr)
        assert "--secret-mounts" in proc.stderr and "Traceback" not in proc.stderr

    def test_alertmanager_without_secret_mounts_is_a_caller_error(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, _OPERATOR_CHECK, "--alertmanager", "main"],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=tmp_path)
        assert proc.returncode == 2, (proc.stdout, proc.stderr)
        assert "--alertmanager" in proc.stderr and "Traceback" not in proc.stderr


# ---------------------------------------------------------------------------
# check_target_health
# ---------------------------------------------------------------------------
class TestCheckTargetHealth:
    def test_no_prometheus_url_skips(self):
        result = oc.OperatorChecker(_make_args()).check_target_health()
        assert result.status == "skip"

    def test_api_error_is_a_caller_error(self, monkeypatch):
        """#3133 (owner 2026-10-23): --prometheus was given, so targets that
        cannot be read are a caller error (exit 2) — it used to be a warn."""
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: (None, "connection refused"))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert (result.status, result.caller_error) == ("fail", True)
        assert "connection refused" in result.detail

    def test_no_targets_warns(self, monkeypatch):
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": []}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_no_exporter_targets_warns(self, monkeypatch):
        # All targets exist but none have "threshold" or "exporter" in job.
        targets = [
            {"labels": {"job": "kubernetes-nodes"}, "health": "up"},
            {"labels": {"job": "prometheus"}, "health": "up"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"
        assert "no targets" in result.detail

    def test_all_exporters_healthy_passes(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "UP"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "pass"

    def test_unhealthy_exporter_fails(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "down"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "fail"

    def test_target_with_missing_labels_treated_as_no_match(self, monkeypatch):
        # Targets present but each has empty labels — `.get("job", "")`
        # returns "" so neither "threshold" nor "exporter" matches → no
        # exporter targets → warn.
        targets = [{"labels": {}, "health": "up"}]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_malformed_response_is_a_caller_error(self, monkeypatch):
        """#3133: a body that is not a target list is not an answer — it used
        to degrade to the "no targets found" warn (PR #291 kept it from crashing)."""
        for malformed in (None, "not a dict", ["list-not-dict"], 42,
                          {"status": "success", "data": {}},
                          {"status": "success", "data": {"activeTargets": [{"labels": {"job": 1}}]}}):
            monkeypatch.setattr(_lib_prometheus, "http_get_json",
                                lambda *a, _body=malformed, **kw: (_body, None))
            checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
            result = checker.check_target_health()
            assert (result.status, result.caller_error) == ("fail", True), malformed


# ---------------------------------------------------------------------------
# run_all_checks + reports + exit_code
# ---------------------------------------------------------------------------
class TestRunAllChecks:
    def test_appends_six_checks(self, monkeypatch):
        # Stub each individual check_* to return a known PASS result.
        for fn_name in (
            "check_operator_detection",
            "check_prometheus_rule_status",
            "check_servicemonitor_status",
            "check_alertmanager_config",
            "check_mount_list",
            "check_target_health",
        ):
            monkeypatch.setattr(
                oc.OperatorChecker, fn_name,
                lambda self, _name=fn_name: oc.CheckResult(_name, "pass"),
            )
        checker = oc.OperatorChecker(_make_args())
        checker.run_all_checks()
        assert len(checker.checks) == 6
        assert all(c.status == "pass" for c in checker.checks)


class TestReports:
    def _make_filled(self):
        c = oc.OperatorChecker(_make_args())
        c.checks = [
            oc.CheckResult("Operator", "pass", "found"),
            oc.CheckResult("Rules", "warn", "1/2"),
            oc.CheckResult("Targets", "fail", "broken"),
            oc.CheckResult("Skipped", "skip", "no url"),
        ]
        return c

    def test_human_report_mentions_each_check_and_summary(self, capsys):
        self._make_filled().print_human_report()
        out = capsys.readouterr().out
        for name in ("Operator", "Rules", "Targets"):
            assert name in out
        # Status symbols for each tier appear.
        for sym in ("✓", "⚠", "✗", "—"):
            assert sym in out

    def test_json_report_has_summary_counts(self, capsys):
        payload = self._make_filled().json_document()
        assert payload["summary"]["pass"] == 1
        assert payload["summary"]["warn"] == 1
        assert payload["summary"]["fail"] == 1
        assert payload["summary"]["total"] == 4
        assert len(payload["checks"]) == 4


class TestExitCode:
    def test_non_ci_always_returns_zero_even_with_failures(self):
        c = oc.OperatorChecker(_make_args(ci=False))
        c.checks = [oc.CheckResult("X", "fail")]
        assert c.exit_code() == 0

    def test_ci_with_no_failures_returns_zero(self):
        c = oc.OperatorChecker(_make_args(ci=True))
        c.checks = [oc.CheckResult("X", "pass"), oc.CheckResult("Y", "warn")]
        assert c.exit_code() == 0

    def test_ci_with_failure_returns_one(self):
        c = oc.OperatorChecker(_make_args(ci=True))
        c.checks = [oc.CheckResult("X", "fail"), oc.CheckResult("Y", "pass")]
        assert c.exit_code() == 1


# ---------------------------------------------------------------------------
# main — CLI entry
# ---------------------------------------------------------------------------
class TestMain:
    def test_default_run_exits_zero(self, monkeypatch, cli_argv):
        # Stub everything so main() doesn't actually contact a cluster.
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: 0)
        cli_argv("operator_check.py")
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 0

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_json_flag_picks_json_reporter(self, monkeypatch, cli_argv, code):
        """#3617: rc 2 under --json is a partial answer, so the code-2 run
        holds one answered check beside the unanswered one (a caller-error
        status with no unanswered check is refused) — the document is
        still the JSON reporter's."""
        called = {"json": False, "human": False}
        checks = ([oc.CheckResult("answered", "pass"),
                   oc.CheckResult("unanswered", "fail", "kubectl timed out",
                                  caller_error=True)] if code == 2 else [])
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks",
                            lambda self: self.checks.extend(checks))
        monkeypatch.setattr(
            oc.OperatorChecker, "json_document",
            lambda self: called.__setitem__("json", True) or {},
        )
        monkeypatch.setattr(
            oc.OperatorChecker, "print_human_report",
            lambda self: called.__setitem__("human", True),
        )
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: code)
        cli_argv("operator_check.py", "--json")
        assert emit_result(oc.main()) == code  # #3216: run()'s step — the rc is the verdict
        assert called["json"] is True
        assert called["human"] is False

    def test_ci_flag_propagates_to_exit_code(self, monkeypatch, cli_argv):
        # When --ci is set and exit_code returns 1, main() exits 1.
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: 1)
        cli_argv("operator_check.py", "--ci")
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# #3137 — `--config-dir` was removed (it was declared but never read). Every
# spelling of it is now an argparse caller error, and removing it changed
# what the `--c` abbreviation resolves to.
# ---------------------------------------------------------------------------
_OPERATOR_CHECK = os.path.join(_TOOLS_DIR, "operator_check.py")


def _never_check(self):
    raise AssertionError("a rejected argv must not reach the checks")


class TestConfigDirRemoved:
    @pytest.mark.parametrize("argv, unrecognized", [
        (["--config-dir", "X"], "--config-dir X"),
        (["--config-dir=X"], "--config-dir=X"),
        # Prefixes that used to abbreviate --config-dir match nothing now.
        (["--co", "X"], "--co X"),
        (["--config", "X"], "--config X"),
    ])
    def test_every_spelling_is_an_argparse_caller_error(
        self, monkeypatch, cli_argv, capsys, argv, unrecognized,
    ):
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", _never_check)
        cli_argv("operator_check.py", *argv)
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert f"unrecognized arguments: {unrecognized}" in err

    def test_json_mode_rejects_before_any_envelope(self, tmp_path):
        # Through the real entry point: argparse refuses before --json arms
        # the crash envelope, so stdout stays empty (no JSON on stdout).
        proc = subprocess.run(
            [sys.executable, _OPERATOR_CHECK, "--json", "--config-dir", "X"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60, cwd=tmp_path,
        )
        assert proc.returncode == 2, proc.stderr
        assert proc.stdout == ""
        assert "unrecognized arguments: --config-dir X" in proc.stderr

    @pytest.mark.parametrize("argv, rc", [
        (["--c"], 1),   # --c is now an unambiguous prefix of --ci
        ([], 0),        # control: same failing check without --ci
    ])
    def test_c_abbreviation_now_resolves_to_ci(
        self, monkeypatch, cli_argv, argv, rc,
    ):
        def one_failure(self):
            self.checks = [oc.CheckResult("X", "fail")]
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", one_failure)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        cli_argv("operator_check.py", *argv)
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == rc
