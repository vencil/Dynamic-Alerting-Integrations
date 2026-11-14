#!/usr/bin/env python3
"""Verify Prometheus Operator CRD deployment status.

Usage:
    da-tools operator-check [--namespace NS] [--prometheus URL]
                            [--secret-mounts FILE [--alertmanager NAME]] [--json] [--ci]

Checks: (1) Operator CRD, (2) PrometheusRule count, (3) ServiceMonitor,
(4) leftover AlertmanagerConfigs, (5) Secret mounts (opt), (6) Target Health (opt)

Check 4 (#3572): operator-generate no longer emits AlertmanagerConfig —
routing lives in the configSecret `generate-routes` renders. An
AlertmanagerConfig carrying this tool's part-of label is a leftover of an
older operator-generate that the Prometheus Operator routes ahead of the
platform routes with `continue: true`: it warns, naming how many to delete.
None, or the CRD not installed (nothing can be left over), passes.

Check 5 (#1560): the Secrets the configSecret reads against the Alertmanager
CR's ``spec.secrets``. ``--secret-mounts`` is the list ``generate-routes
--secret-mounts-out`` wrote from that same config; without it the row is
SKIP (not judged, never a pass). The Prometheus Operator mounts each
``spec.secrets`` entry at ``/etc/alertmanager/secrets/<name>/``:

* a Secret the list names that ``spec.secrets`` does not → FAIL: every
  notification through a receiver reading it fails when it is sent
  (Alertmanager cannot open the ``*_file``), and that notification is lost;
* a list written with another ``--secret-mount-root`` → FAIL: the files are
  not where the Operator mounts them;
* a ``da-`` Secret (the tenant-own prefix) in ``spec.secrets`` the list does
  not name → WARN: nothing reads it, and if it does not exist the Pod cannot
  start (the Operator's Secret volumes are not optional). Any other name may
  be the deployment's own (TLS, a sidecar) and is not reported.

Every Alertmanager CR in ``--namespace`` is judged and named in the detail;
``--alertmanager NAME`` judges that one only. No CR (or the CRD not
installed) with a non-empty list → FAIL: nothing mounts them. Only the CR is
read — Secrets are never read (no ``get`` on secrets is needed), so whether a
listed key exists inside its Secret is not judged.

Every cluster read goes through ``_lib_kubectl`` (``kubectl_get`` /
``kubectl_list``, #3207): a
read kubectl does not answer (cluster unreachable, forbidden, kubectl
missing or timed out) is a caller error — the row is marked so and the
exit code is 2 — never "not found". Whether a CRD is installed is a `get`
of that CRD by name (an answer: absent is a finding, not a failed read; it
needs only `get` on customresourcedefinitions).
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import detect_cli_lang, url_type  # noqa: E402
import _lib_input  # noqa: E402  (#3462: what --rule-packs-dir selects)
from _lib_prometheus import QueryError, prometheus_targets  # noqa: E402  (#3133)
from _lib_kubectl import KubectlError, kubectl_get, kubectl_list  # noqa: E402  (#3207)
import _lib_consumer_read  # noqa: E402  (#3426: rule_pack_files, the one declaration)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, die_caller_error,
    partial_answer)
# #1560: the mount-list document generate-routes writes, and the Operator's
# mount root (the generator's default) — one definition, the generator's.
from _grar_merge import (  # noqa: E402
    DEFAULT_MOUNT_ROOT, MOUNT_LIST_SCHEMA, OWN_NAME_PREFIX)

# The CRDs of the kinds the checks list (#3207): whether each is installed
# is a `kubectl_get` of the CRD by name — an answer, unlike a `kubectl get`
# of a kind the server does not have, which exits 1 like an unreachable
# cluster does.
_CRD_RULES = "prometheusrules.monitoring.coreos.com"
_CRD_MONITORS = "servicemonitors.monitoring.coreos.com"
_CRD_AM_CONFIGS = "alertmanagerconfigs.monitoring.coreos.com"
_CRD_AMS = "alertmanagers.monitoring.coreos.com"
#: #1560: the Alertmanager CR field listing the Secrets the Operator mounts.
_SPEC_MOUNTED = "secrets"
_PART_OF = "app.kubernetes.io/part-of=dynamic-alerting"


# i18n strings
STRINGS = {
    "en": {
        "title": "Dynamic Alerting — Operator Check Report",
        "check_operator": "Operator Detection",
        "check_rules": "PrometheusRule",
        "check_monitor": "ServiceMonitor",
        "check_alertconfig": "AlertmanagerConfig",
        "check_targets": "Target Health",
        "pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP", "result": "Result",
        "operator_found": "Prometheus Operator detected",
        "rules_loaded": "rules loaded",
        "rules_not_compared": ("the default {dir} is not there: the count is not compared "
                               "(give --rule-packs-dir)"),
        "monitor_found": "ServiceMonitor found",
        "monitor_missing": "ServiceMonitor not found",
        "alertconfig_none": "no leftover AlertmanagerConfigs (routing lives in the configSecret rendered by generate-routes)",
        "alertconfig_leftover": (
            "leftover AlertmanagerConfigs from an older operator-generate: the Prometheus "
            "Operator routes them ahead of the platform routes with continue: true — delete "
            "them (kubectl delete alertmanagerconfig -n {ns} -l {sel}); routing lives in "
            "the configSecret rendered by generate-routes"
        ),
        "targets_healthy": "targets healthy",
        "targets_unhealthy": "targets unhealthy",
        "targets_skip": "no Prometheus URL",
        "error_api": "API call failed",
        "check_mounts": "Secret mounts",
        "mounts_skip": (
            "not judged: no --secret-mounts. Write the list with `da-tools generate-routes "
            "--config-dir <tenant-config-dir> --output-alertmanager --base-config <alertmanager.yml> "
            "-o <out.yml> --secret-mounts-out secret-mounts.json` and pass "
            "--secret-mounts secret-mounts.json"
        ),
        "mounts_none_needed": "the config reads no Secret under the mount root",
        "mounts_ok": "{name}: spec.secrets mounts all {n} Secret(s) the config reads",
        "mounts_missing": (
            "{name}: spec.secrets lacks {names} — notifications through the receivers "
            "reading them fail when sent (the *_file cannot be opened) and are lost; "
            "add them to spec.secrets"
        ),
        "mounts_extra": (
            "{name}: spec.secrets lists {names}, which the config does not read — if such "
            "a Secret does not exist the Pod cannot start; remove it unless it is meant"
        ),
        "mounts_root": (
            "the list was written with --secret-mount-root {root}, but the Prometheus "
            "Operator mounts spec.secrets under {expected}: Alertmanager will not find "
            "the files — regenerate without --secret-mount-root"
        ),
        "mounts_no_cr": (
            "no Alertmanager CR in namespace {ns}: nothing mounts the {n} Secret(s) "
            "the config reads"
        ),
        "mounts_cr_missing": "Alertmanager CR {ns}/{name} not found",
        "mounts_pick_one": (
            "every Alertmanager CR in {ns} is compared; if only one serves this "
            "configSecret, pass --alertmanager <name> to compare that one"
        ),
        "mounts_other": "{n} *_file path(s) outside {root} not judged (other_files)",
        "mounts_unusable": "--secret-mounts {path}: not a generate-routes --secret-mounts-out list ({why})",
        "h_desc": "Verify Prometheus Operator CRD deployment status",
        "h_namespace": "Kubernetes namespace (default: monitoring)",
        "h_rule_packs_dir": ("Directory with rule pack YAML files (default: rule-packs; not "
                             "there: the PrometheusRule count is not compared)"),
        "h_prometheus": "Prometheus API URL for target health check (optional)",
        "h_mount_list": (
            "The list `generate-routes --secret-mounts-out` wrote for the "
            "configSecret: compared with the Alertmanager CR's spec.secrets "
            "(#1560). Without it that check is SKIP (not judged)"
        ),
        "h_alertmanager": (
            "With --secret-mounts: judge this Alertmanager CR only "
            "(default: every one in --namespace)"
        ),
        "h_kubeconfig": "Path to kubeconfig file (optional)",
        "h_json": "Output results as JSON",
        "h_ci": "Exit non-zero if any check fails",
    },
    "zh": {
        "title": "Dynamic Alerting — Operator 檢查報告",
        "check_operator": "Operator 偵測", "check_rules": "PrometheusRule",
        "check_monitor": "ServiceMonitor", "check_alertconfig": "AlertmanagerConfig",
        "check_targets": "Target 健檢",
        "pass": "通過", "warn": "警告", "fail": "失敗", "skip": "跳過", "result": "結果",
        "operator_found": "Prometheus Operator 已偵測到",
        "rules_loaded": "已加載規則",
        "rules_not_compared": "預設的 {dir} 不存在：未比對數量（請給 --rule-packs-dir）",
        "monitor_found": "ServiceMonitor 已找到",
        "monitor_missing": "ServiceMonitor 未找到",
        "alertconfig_none": "沒有殘留的 AlertmanagerConfig（路由由 generate-routes 產出的 configSecret 承載）",
        "alertconfig_leftover": (
            "個舊版 operator-generate 留下的 AlertmanagerConfig：Prometheus Operator 會把它們"
            "排在平台路由前面並帶 continue: true——請刪除"
            "（kubectl delete alertmanagerconfig -n {ns} -l {sel}）；路由由 generate-routes "
            "產出的 configSecret 承載"
        ),
        "targets_healthy": "Target 健康",
        "targets_unhealthy": "Target 不健康",
        "targets_skip": "未提供 Prometheus URL",
        "error_api": "API 調用失敗",
        "check_mounts": "Secret 掛載",
        "mounts_skip": (
            "未判定：沒有 --secret-mounts。先以 `da-tools generate-routes "
            "--config-dir <tenant-config-dir> --output-alertmanager --base-config <alertmanager.yml> "
            "-o <out.yml> --secret-mounts-out secret-mounts.json` 產出清單，再帶 "
            "--secret-mounts secret-mounts.json"
        ),
        "mounts_none_needed": "設定沒有讀 mount root 下的任何 Secret",
        "mounts_ok": "{name}：spec.secrets 已掛上設定讀的全部 {n} 個 Secret",
        "mounts_missing": (
            "{name}：spec.secrets 少了 {names}——經讀取它們的 receiver 送出的通知會在送出時"
            "失敗（*_file 開不了）而遺失；請加進 spec.secrets"
        ),
        "mounts_extra": (
            "{name}：spec.secrets 列了 {names}，設定沒有讀它——那個 Secret 不存在時 Pod 起不來；"
            "不是刻意要掛的就移除"
        ),
        "mounts_root": (
            "清單是以 --secret-mount-root {root} 產出的，但 Prometheus Operator 把 "
            "spec.secrets 掛在 {expected} 下：Alertmanager 找不到那些檔案——請不帶 "
            "--secret-mount-root 重新產出"
        ),
        "mounts_no_cr": "namespace {ns} 裡沒有 Alertmanager CR：設定讀的 {n} 個 Secret 沒有任何東西掛載",
        "mounts_cr_missing": "找不到 Alertmanager CR {ns}/{name}",
        "mounts_pick_one": (
            "{ns} 裡每個 Alertmanager CR 都會比對；只有其中一個使用這份 configSecret 時，"
            "帶 --alertmanager <名稱> 只比對那一個"
        ),
        "mounts_other": "{n} 個 *_file 路徑在 {root} 之外，未判定（other_files）",
        "mounts_unusable": "--secret-mounts {path}：不是 generate-routes --secret-mounts-out 產出的清單（{why}）",
        "h_desc": "驗證 Prometheus Operator 的 CRD 部署狀態",
        "h_namespace": "Kubernetes namespace（預設：monitoring）",
        "h_rule_packs_dir": "Rule Pack YAML 所在目錄（預設：rule-packs；不存在時不比對 PrometheusRule 數量）",
        "h_prometheus": "Prometheus API URL，給了才檢查 target 健康（選用）",
        "h_mount_list": (
            "`generate-routes --secret-mounts-out` 為 configSecret 寫出的清單："
            "與 Alertmanager CR 的 spec.secrets 比對（#1560）。不給則該項 SKIP（未判定）"
        ),
        "h_alertmanager": (
            "與 --secret-mounts 同用：只比對這個 Alertmanager CR"
            "（預設：--namespace 裡每一個）"
        ),
        "h_kubeconfig": "kubeconfig 路徑（選用）",
        "h_json": "以 JSON 輸出結果",
        "h_ci": "任一檢查失敗時以非 0 結束",
    }
}


def i18n(key: str, lang: str = "en") -> str:
    """Get i18n string."""
    return STRINGS.get(lang, STRINGS["en"]).get(key, key)


def _mount_list_problem(doc: object) -> str | None:
    """Why *doc* is not a ``generate-routes --secret-mounts-out`` document
    (``MOUNT_LIST_SCHEMA``), or None."""
    if not isinstance(doc, dict):
        return "not a JSON object"
    if doc.get("schema") != MOUNT_LIST_SCHEMA:
        return f"schema is not {MOUNT_LIST_SCHEMA}"
    if not isinstance(doc.get("mount_root"), str):
        return "mount_root is not a string"
    entries = doc.get("mounts")
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]
            and isinstance(e.get("keys"), list)
            and all(isinstance(k, str) for k in e["keys"]) for e in entries):
        return "mounts is not a list of {name, keys}"
    other = doc.get("other_files")
    if not isinstance(other, list) or not all(isinstance(x, str) for x in other):
        return "other_files is not a list of strings"
    return None


#: --secret-mounts as a declared input reads it (#3797): a JSON object.
MOUNT_LIST_DOCUMENT = _lib_input.Document("json", dict)


def load_mount_list(args: argparse.Namespace, lang: str = "en") -> "dict | None":
    """The ``--secret-mounts`` document (#1560); None when not given. The
    file is judged and read once by its declaration (`_lib_input.document`:
    the path, strict UTF-8, JSON, an object — a CallerError naming the flag,
    #3797); a document that is not that list is a `CallerError` too (rc 2):
    a list that cannot be read is not judged as an empty one."""
    doc = _lib_input.document(args, "secret_mounts", lang=lang)
    if doc is None:
        return None
    path = args.secret_mounts
    why = _mount_list_problem(doc)
    if why is not None:
        raise CallerError(i18n("mounts_unusable", lang).format(path=path, why=why),
                          "mount_list_unusable")
    return doc


class CheckResult:
    """Single check result."""
    def __init__(self, name: str, status: str, detail: str = "",
                 caller_error: bool = False):
        self.name = name
        self.status = status  # "pass", "warn", "fail", "skip"
        self.detail = detail
        # #452/#737, #3207: True when a "fail" was caused by a read that got
        # no answer (kubectl: KubectlError; Prometheus: QueryError), not a
        # finding.
        self.caller_error = caller_error

    def to_dict(self) -> dict:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "caller_error": self.caller_error,
        }


class OperatorChecker:
    """Prometheus Operator checker. *rule_packs*: the rule packs
    ``--rule-packs-dir`` selected (``main``, ``_lib_input.selected``), the
    expected PrometheusRule count; None when the default nobody gave is not
    there — the count is not compared, and the row says so (#3462)."""
    def __init__(self, args, mount_list: dict | None = None, *,
                 rule_packs: "list[Path] | None" = None):
        self.args = args
        #: #1560: the --secret-mounts document (None: not given).
        self.mount_list = mount_list
        self.rule_packs = rule_packs
        self.lang = detect_cli_lang()
        self.checks: list[CheckResult] = []
        self._crds: dict[str, bool | KubectlError] = {}

    def _list(self, kind: str, **kw) -> list[dict]:
        return kubectl_list(kind, kubeconfig=self.args.kubeconfig, **kw)

    def _installed(self, crd: str) -> bool:
        """Whether *crd* is installed, read once per CRD; KubectlError when
        kubectl does not answer (raised again, not re-asked)."""
        if crd not in self._crds:
            try:
                self._crds[crd] = kubectl_get("crd", crd, kubeconfig=self.args.kubeconfig) is not None
            except KubectlError as exc:
                self._crds[crd] = exc
        got = self._crds[crd]
        if isinstance(got, KubectlError):
            raise got
        return got

    def _unanswered(self, key: str, exc: KubectlError) -> CheckResult:
        return CheckResult(i18n(key, self.lang), "fail", str(exc), caller_error=True)

    def check_operator_detection(self) -> CheckResult:
        """Check 1: Operator CRD exists."""
        try:
            found = self._installed(_CRD_RULES)
        except KubectlError as exc:
            return self._unanswered("check_operator", exc)
        return CheckResult(i18n("check_operator", self.lang), "pass" if found else "fail",
                           i18n("operator_found", self.lang) if found else f"CRD {_CRD_RULES} not installed")

    def check_prometheus_rule_status(self) -> CheckResult:
        """Check 2: PrometheusRule resources loaded."""
        try:
            installed = self._installed(_CRD_RULES)
            loaded = len(self._list("prometheusrule", namespace=self.args.namespace,
                                    selector=_PART_OF)) if installed else 0
        except KubectlError as exc:
            return self._unanswered("check_rules", exc)

        # The packs operator-generate emits: every declared pack, the
        # platform pack included since #1595 D1b (as the rules operator mode
        # can run) — what --rule-packs-dir selected (never none: `main`).
        if self.rule_packs is None:
            status = "warn" if loaded > 0 else "fail"
            detail = f"{loaded} ({i18n('rules_not_compared', self.lang).format(dir=self.args.rule_packs_dir)})"
        else:
            expected = len(self.rule_packs)
            status = "pass" if loaded == expected > 0 else ("warn" if loaded > 0 else "fail")
            detail = f"{loaded}/{expected}"
        if not installed:
            detail += f" (CRD {_CRD_RULES} not installed)"
        return CheckResult(i18n("check_rules", self.lang), status, detail)

    def check_servicemonitor_status(self) -> CheckResult:
        """Check 3: ServiceMonitor for threshold-exporter."""
        try:
            installed = self._installed(_CRD_MONITORS)
            found = installed and bool(self._list("servicemonitor", namespace=self.args.namespace,
                                                  selector=_PART_OF))
        except KubectlError as exc:
            return self._unanswered("check_monitor", exc)
        detail = i18n("monitor_found", self.lang) if found else i18n("monitor_missing", self.lang)
        if not installed:
            detail += f" (CRD {_CRD_MONITORS} not installed)"
        return CheckResult(i18n("check_monitor", self.lang), "pass" if found else "fail", detail)

    def check_alertmanager_config(self) -> CheckResult:
        """Check 4: leftover AlertmanagerConfigs (#3572).

        operator-generate no longer emits AlertmanagerConfig; one carrying
        its part-of label is left over from an older run and is routed ahead
        of the platform routes with `continue: true` → warn. None → pass; the
        CRD not installed → pass (nothing can be left over)."""
        try:
            installed = self._installed(_CRD_AM_CONFIGS)
            count = len(self._list("alertmanagerconfig", namespace=self.args.namespace,
                                   selector=_PART_OF)) if installed else 0
        except KubectlError as exc:
            return self._unanswered("check_alertconfig", exc)
        if count > 0:
            leftover = i18n("alertconfig_leftover", self.lang).format(
                ns=self.args.namespace, sel=_PART_OF)
            return CheckResult(i18n("check_alertconfig", self.lang), "warn", f"{count} {leftover}")
        detail = (i18n("alertconfig_none", self.lang) if installed
                  else f"CRD {_CRD_AM_CONFIGS} not installed")
        return CheckResult(i18n("check_alertconfig", self.lang), "pass", detail)

    def _alertmanager_crs(self) -> list[dict]:
        """The Alertmanager CRs check 5 judges: ``--alertmanager``'s alone
        (a FAIL row names it when absent), else every one in the namespace
        (none when the CRD is not installed)."""
        if not self._installed(_CRD_AMS):
            return []
        name = getattr(self.args, "alertmanager", None)
        if name is not None:
            got = kubectl_get("alertmanager", name, namespace=self.args.namespace,
                              kubeconfig=self.args.kubeconfig)
            return [got] if got is not None else []
        return self._list("alertmanager", namespace=self.args.namespace)

    def check_mount_list(self) -> CheckResult:
        """Check 5 (#1560): ``--secret-mounts`` against each Alertmanager CR's
        ``spec.secrets`` (the rules are in the module docstring)."""
        title = i18n("check_mounts", self.lang)
        if self.mount_list is None:
            return CheckResult(title, "skip", i18n("mounts_skip", self.lang))
        needed = {e["name"] for e in self.mount_list["mounts"]}
        root = self.mount_list["mount_root"]
        notes: list[str] = []
        status = "pass"
        # posixpath.normpath: `/etc/alertmanager//secrets/` or
        # `/etc/alertmanager/./secrets` is the same directory inside the
        # container (CodeRabbit on #3615); the render only strips the
        # trailing slash, so a spelling like that must not read as another root.
        if needed and posixpath.normpath(root) != DEFAULT_MOUNT_ROOT:
            status = "fail"
            notes.append(i18n("mounts_root", self.lang).format(
                root=root, expected=DEFAULT_MOUNT_ROOT))
        try:
            crs = self._alertmanager_crs()
        except KubectlError as exc:
            return self._unanswered("check_mounts", exc)
        name = getattr(self.args, "alertmanager", None)
        if not crs and name is not None:
            # The CR the caller named is not there: nothing was compared,
            # whatever the list holds.
            status = "fail"
            notes.append(i18n("mounts_cr_missing", self.lang).format(
                ns=self.args.namespace, name=name))
        elif not crs and needed:
            status = "fail"
            notes.append(i18n("mounts_no_cr", self.lang).format(ns=self.args.namespace,
                                                                n=len(needed)))
        for cr in crs:
            cr_name = (cr.get("metadata") or {}).get("name", "?")
            listed = (cr.get("spec") or {}).get(_SPEC_MOUNTED) or []
            mounted = {x for x in listed if isinstance(x, str)}
            missing = sorted(needed - mounted)
            extra = sorted(x for x in mounted - needed if x.startswith(OWN_NAME_PREFIX))
            if missing:
                status = "fail"
                notes.append(i18n("mounts_missing", self.lang).format(
                    name=cr_name, names=", ".join(missing)))
            if extra:
                status = "warn" if status == "pass" else status
                notes.append(i18n("mounts_extra", self.lang).format(
                    name=cr_name, names=", ".join(extra)))
            if not missing and not extra and needed:
                notes.append(i18n("mounts_ok", self.lang).format(name=cr_name, n=len(needed)))
        if not needed and not notes:
            notes.append(i18n("mounts_none_needed", self.lang))
        if len(crs) > 1 and name is None and any(
                needed - {x for x in ((c.get("spec") or {}).get(_SPEC_MOUNTED) or [])
                          if isinstance(x, str)} for c in crs):
            notes.append(i18n("mounts_pick_one", self.lang).format(ns=self.args.namespace))
        if self.mount_list["other_files"]:
            notes.append(i18n("mounts_other", self.lang).format(
                n=len(self.mount_list["other_files"]), root=root))
        return CheckResult(title, status, "; ".join(notes))

    def check_target_health(self) -> CheckResult:
        """Check 6: Prometheus target health (optional)."""
        if not self.args.prometheus:
            return CheckResult(i18n("check_targets", self.lang), "skip", i18n("targets_skip", self.lang))

        # #3133 (owner 2026-10-23): --prometheus is given, so targets that
        # cannot be read are a caller error (exit 2), not a warn; the lib
        # checks the answer's shape (a body that is not a target list used to
        # read as "no targets found").
        try:
            targets = prometheus_targets(self.args.prometheus, timeout=10)
        except QueryError as exc:
            return CheckResult(i18n("check_targets", self.lang), "fail",
                               f"{i18n('error_api', self.lang)}: {str(exc)[:60]}",
                               caller_error=True)
        exporter_targets = [t for t in targets if any(
            kw in t.get("labels", {}).get("job", "").lower() for kw in ["threshold", "exporter"]
        )]

        if not exporter_targets:
            return CheckResult(i18n("check_targets", self.lang), "warn", "no targets found")

        unhealthy = [t for t in exporter_targets if t.get("health", "").lower() != "up"]
        status = "fail" if unhealthy else "pass"
        detail = i18n("targets_healthy", self.lang) if not unhealthy else i18n("targets_unhealthy", self.lang)
        return CheckResult(i18n("check_targets", self.lang), status, f"{len(exporter_targets)} {detail}")

    def run_all_checks(self) -> None:
        """Run all checks sequentially."""
        self.checks.append(self.check_operator_detection())
        self.checks.append(self.check_prometheus_rule_status())
        self.checks.append(self.check_servicemonitor_status())
        self.checks.append(self.check_alertmanager_config())
        self.checks.append(self.check_mount_list())
        self.checks.append(self.check_target_health())

    def print_human_report(self) -> None:
        """Print human-readable report."""
        title = i18n("title", self.lang)
        print(f"\n╔══════════════════════════════════════════════════╗")
        print(f"║  {title:<48} ║")
        print(f"╠══════════════════════════════════════════════════╣")

        for check in self.checks:
            status_str = {
                "pass": "✓",
                "warn": "⚠",
                "fail": "✗",
                "skip": "—",
            }.get(check.status, "?")

            status_label = {
                "pass": i18n("pass", self.lang),
                "warn": i18n("warn", self.lang),
                "fail": i18n("fail", self.lang),
                "skip": i18n("skip", self.lang),
            }.get(check.status, "?")

            line = f"║ {status_str} {status_label:<6} {check.name:<22} "
            if check.detail:
                line += f"({check.detail})"
            # Pad to 50 chars
            line = line.ljust(50) + "║"
            print(line)

        print(f"╠══════════════════════════════════════════════════╣")

        # Summary
        pass_count = sum(1 for c in self.checks if c.status == "pass")
        warn_count = sum(1 for c in self.checks if c.status == "warn")
        fail_count = sum(1 for c in self.checks if c.status == "fail")

        summary = (
            f"{i18n('result', self.lang)}: {pass_count} {i18n('pass', self.lang)}, "
            f"{warn_count} {i18n('warn', self.lang)}, {fail_count} {i18n('fail', self.lang)}"
        )
        line = f"║ {summary:<48} ║"
        print(line)
        print(f"╚══════════════════════════════════════════════════╝\n")

    def json_document(self) -> dict:
        """The --json document (run() encodes it, #3216)."""
        pass_count = sum(1 for c in self.checks if c.status == "pass")
        warn_count = sum(1 for c in self.checks if c.status == "warn")
        fail_count = sum(1 for c in self.checks if c.status == "fail")

        report = {
            "checks": [c.to_dict() for c in self.checks],
            "summary": {
                "pass": pass_count,
                "warn": warn_count,
                "fail": fail_count,
                "total": len(self.checks),
            },
        }
        return report

    def exit_code(self) -> int:
        """Return exit code based on checks.

        #452/#737, #3207: a read that got no answer (kubectl unreachable,
        forbidden, missing or timed out; Prometheus not answering) maps to
        EXIT_CALLER_ERROR (2) even outside --ci, since the tool genuinely
        could not do its job. Genuine "fail" findings only gate
        the run in --ci mode (EXIT_VIOLATION).
        """
        caller_error = any(
            c.status == "fail" and c.caller_error for c in self.checks
        )
        if caller_error:
            return EXIT_CALLER_ERROR
        if not self.args.ci:
            return EXIT_OK
        fail_count = sum(1 for c in self.checks if c.status == "fail")
        return EXIT_VIOLATION if fail_count > 0 else EXIT_OK


def build_parser(lang: str) -> argparse.ArgumentParser:
    """The command line, help in *lang*."""
    parser = argparse.ArgumentParser(
        description=i18n("h_desc", lang),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--namespace", default="monitoring", help=i18n("h_namespace", lang))
    # #3462: the expected PrometheusRule count is what this selects (the
    # rule-pack declaration, RULE_PACKS). None selected is a caller error
    # (it was an expected count of 0 and a WARN that never named the path);
    # the default nobody gave, not there, is no count to compare.
    _lib_input.add_input(
        parser, "--rule-packs-dir", select=_lib_consumer_read.RULE_PACKS,
        empty=_lib_input.EMPTY_REFUSED, default="rule-packs",
        absent_default=_lib_input.ABSENT_EMPTY, help=i18n("h_rule_packs_dir", lang))
    parser.add_argument("--prometheus", type=url_type(empty="refuse"),  # #3528
                        help=i18n("h_prometheus", lang))
    _lib_input.add_input(parser, "--secret-mounts", decode=MOUNT_LIST_DOCUMENT,  # #3797
                         metavar="FILE", help=i18n("h_mount_list", lang))
    parser.add_argument("--alertmanager", default=None, metavar="NAME",
                        help=i18n("h_alertmanager", lang))
    parser.add_argument("--kubeconfig", help=i18n("h_kubeconfig", lang))
    parser.add_argument("--json", action="store_true", help=i18n("h_json", lang))
    parser.add_argument("--ci", action="store_true", help=i18n("h_ci", lang))
    return parser


def main():
    try_utf8_stdout()
    # dev-rules §9 Layer 3 (CodeRabbit on #3615): help follows DA_LANG too.
    lang = detect_cli_lang()
    args = build_parser(lang).parse_args()
    # #3392 / #3462: --rule-packs-dir is judged before the cluster is asked
    # (`_lib_input`): given — or the default, when something is there — and
    # not a directory, unlistable, or holding no rule pack, rc 2 naming it.
    # It was an expected count of 0 and a WARN that never named the path,
    # rc 0. #3426: an entry the rule-pack declaration does not cover is rc 2
    # too, naming it.
    try:
        rule_packs = _lib_input.selected(args, "rule_packs_dir", lang=detect_cli_lang())
    except _lib_consumer_read.ConsumerReadError as exc:
        die_caller_error(f"ERROR: --rule-packs-dir: {_lib_consumer_read.error_text(exc)}")

    # #1560: an unusable list is a caller error before the cluster is asked;
    # --alertmanager only selects what --secret-mounts is compared with.
    if args.alertmanager is not None and args.secret_mounts is None:
        raise CallerError("--alertmanager is read only with --secret-mounts (it picks the "
                          "Alertmanager CR the list is compared with): add --secret-mounts "
                          "<file> or drop --alertmanager", "flag_not_read")
    mount_list = load_mount_list(args, detect_cli_lang())
    # rule_packs is [] only for the default with nothing there: `empty` is refused.
    checker = OperatorChecker(args, mount_list, rule_packs=rule_packs or None)
    checker.run_all_checks()

    if args.json:
        # #3617: a read that got no answer is rc 2; the document is a partial
        # answer naming those checks — or, when no check was answered, none
        # at all (the causes on stderr, stdout empty).
        return partial_answer(  # #3216: run() encodes it
            checker.json_document(), checker.exit_code(),
            unanswered=[f"{c.name}: {c.detail}" for c in checker.checks
                        if c.status == "fail" and c.caller_error],
            of=len(checker.checks))
    checker.print_human_report()

    sys.exit(checker.exit_code())


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
