#!/usr/bin/env python3
"""migrate-to-operator — Migrate ConfigMap-based rules to Operator CRD format.

Reads existing Prometheus ConfigMap rules (from configmaps/ or live cluster export)
and produces equivalent PrometheusRule CRDs along with a step-by-step migration
checklist. It reads no conf.d.

⛔ No Alertmanager routing (#3572): `generate-routes` is the one Alertmanager
route renderer. AlertmanagerConfig CRDs cannot carry it — the Prometheus
Operator puts their routes first with `continue: true` and a namespace
matcher, so the platform routes (Watchdog, custom isolation, synthetic probe,
sentinel) could never come first. The flags that only fed AlertmanagerConfig
(`--config-dir`, `--receiver-template`, `--secret-name`, `--secret-key`) are
refused (rc 2) with how to render alertmanager.yaml and hand it to the
Alertmanager as its configSecret (`_lib_constants.OPERATOR_ROUTING_REMEDY_*`);
the checklist carries the same steps.

The --source-dir files are what their readers say (#3426): a manifest kubectl
reads (da-crdecode; a `kind: List` is read as its items), or else a plain rule
file (one mapping declaring `groups`, or — when no YAML reading reads it —
one Prometheus' rule loader reads `groups` from). Any other file — broken
YAML, a top-level list, a document with no `kind`, a list item kubectl would
not apply — exits 2 before anything is written, every such file named with
its reader's reason.

Usage:
    da-tools migrate-to-operator --source-dir configmaps/   # stdout; nothing written
    da-tools migrate-to-operator --source-dir configmaps/ --output-dir migration-output/   # writing is opt-in (#1582)
    da-tools migrate-to-operator --source-dir configmaps/ --dry-run
    da-tools migrate-to-operator --source-dir configmaps/ --checklist-only
    da-tools migrate-to-operator --source-dir configmaps/ --json
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

try:
    import yaml
except ImportError:
    yaml = None

from _lib_python import detect_cli_lang, i18n_text  # noqa: E402
from _lib_io import OutputWriteError, ensure_dir, write_text_secure  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, die_caller_error, encode_json)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_yaml import _dict_to_yaml, write_yaml_crd  # noqa: E402
from _lib_confd import warn_nested  # noqa: E402
import _lib_parse_cache  # noqa: E402  (parse_tree_error_line)
import _lib_parse_tree  # noqa: E402
from _lib_parse_tree import ParseTreeError  # noqa: E402
from _lib_paths import supplied  # noqa: E402  (#3392)
from _lib_constants import (  # noqa: E402  (#3572)
    DOCS_SITE_BASE, OPERATOR_ROUTING_REMEDY_EN, OPERATOR_ROUTING_REMEDY_ZH)
import _lib_input  # noqa: E402  (#3446: --source-dir is a directory da-guard lists)

# #3572: the flags that only fed AlertmanagerConfig, as (flag, dest). Still
# parsed — hidden (`help=argparse.SUPPRESS`), `default=None`, no type /
# choices — so that giving one, with any value, is refused naming it with the
# remedy rather than as a bare "unrecognized arguments": the caller learns
# where routing went. They are declared one literal `add_argument` each in
# the parser builder (the conf.d population walks read flags statically).
_ROUTING_FLAGS = (
    ("--config-dir", "config_dir"),
    ("--receiver-template", "receiver_template"),
    ("--secret-name", "secret_name"),
    ("--secret-key", "secret_key"),
)

_HELP = {
    "zh": {
        "desc": (
            "將 ConfigMap 式規則遷移至 Operator CRD 格式（PrometheusRule）。"
            "Alertmanager 路由不在此產生：用 generate-routes 產出 alertmanager.yaml，"
            "交給 Alertmanager 當 configSecret（#3572）"
        ),
        "source_dir": "ConfigMap YAML 檔案來源目錄",
        "output_dir": "把 CRD 與 checklist 寫進這個目錄。不給、或帶 --dry-run / --checklist-only，就改印到 stdout 而不寫任何檔案",
        "namespace": "目標命名空間（預設 monitoring）",
        "dry_run": "列印輸出而不寫入檔案",
        "checklist_only": "僅生成遷移檢核清單（不生成 CRD）",
        "json": "以 JSON 格式輸出結果報告",
    },
    "en": {
        "desc": (
            "Migrate ConfigMap-based Prometheus rules to Operator CRD format (PrometheusRule). "
            "Alertmanager routing is not generated here: render alertmanager.yaml with "
            "generate-routes and give it to the Alertmanager as its configSecret (#3572)"
        ),
        "source_dir": "Source directory with ConfigMap YAML files",
        "output_dir": "Write CRDs and the checklist into this directory. Omit it, or pass --dry-run / --checklist-only, and everything goes to stdout instead; nothing is written",
        "namespace": "Target namespace (default monitoring)",
        "dry_run": "Print output instead of writing files",
        "checklist_only": "Only generate migration checklist (skip CRD generation)",
        "json": "Output results as JSON report",
    },
}

_LANG = detect_cli_lang()

# ─────────────────────────────────────────────────────────────────────────────
# Migration-specific functions
# ─────────────────────────────────────────────────────────────────────────────


def list_source_dir(source_dir: Path) -> "_lib_parse_tree.Listing":
    """Which files *source_dir* holds, as da-guard's walk lists them
    (`parse-tree --list-only`, #3206) — no Python listing of its own.

    ⛔ List mode, not a conf.d reader: these are ConfigMap dumps and rule
    files (a multi-document manifest is ordinary here), not files the
    exporter serves, so the exporter's judgement of their content — and the
    conf.d refusal built on it — does not apply. A listed file that is
    neither a manifest kubectl reads nor a plain rule file stops the run in
    `parse_configmap_rules` (`SourceFilesUnreadable`, #3426), naming it.

    Nothing at the top level is dropped silently: one stderr line names
    each entry this tool will not read, with Go's reason — the names the
    walk does not list that Go classes `hidden` (names mode), and the listed
    entries whose role is no readable file (`Entry.flagged`: a dangling
    link, a FIFO, a link to a directory, a directory it cannot list, a
    directory with a file's name). A name Go classes `not_config_name`
    (`notes.txt`) was never a rule file and is not named. Raises
    ParseTreeError (no usable da-guard).

    ⛔ #3446: a *source_dir* da-guard cannot list (its root entry is role
    `unlistable_dir`) raises CallerError (`dir_not_accessible`, rc 2,
    `_lib_input.require_listed`): it was a WARNING and a migration made from
    no source file, rc 0."""
    listing = _lib_parse_tree.list_tree(source_dir)
    _lib_input.require_listed("--source-dir", source_dir, listing.entry("."), lang=_LANG)
    warn_nested(source_dir, tool="migrate_to_operator", listing=listing)
    try:
        # Only for the hidden names da-guard's listing leaves out (#3446:
        # through the one listing, `_lib_input._list`).
        names = _lib_input._list(source_dir)
    except OSError:
        names = []  # list_tree read it; a race is not worth a second error
    top = [e for e in listing.entries if e.depth == 0]
    listed = {e.path for e in top}
    unlisted = sorted(n for n in names if n not in listed)
    classes = _lib_parse_tree.classify_names(unlisted) if unlisted else {}
    unread = [(n, "hidden") for n in unlisted if classes[n].cls == "hidden"]
    unread += [(e.path, e.problem[1] if e.problem else e.role) for e in top if e.flagged]
    if unread:
        shown = "; ".join(f"{safe_label(n)} ({safe_label(why)})" for n, why in sorted(unread))
        print(i18n_text(
            f"WARNING: --source-dir 有 {len(unread)} 個頂層項目不會被讀取: {shown}",
            f"WARNING: --source-dir: {len(unread)} top-level entries not read: {shown}",
        ), file=sys.stderr)
    return listing


def source_files(source_dir: Path, listing: "_lib_parse_tree.Listing") -> List[Path]:
    """⛔ The files of *source_dir* a migration reads: the top-level config
    files of its da-guard *listing* (its `list_source_dir`). What is nested
    is not read, and `warn_nested` names it. `parse_configmap_rules` reads
    these, and `main` refuses a --source-dir with none (#3462)."""
    warn_nested(source_dir, tool="migrate_to_operator", listing=listing)
    return [source_dir / e.path for e in listing.config_files() if e.depth == 0]


class SourceFilesUnreadable(Exception):
    """Files of --source-dir that are neither a Kubernetes manifest kubectl
    reads nor a plain rule file (#3426). ``files`` is ``[(name, reason)]``,
    every such file (the ones the rule loader judged last); ``str()`` names
    them all. The run
    stops on it (rc 2) before any file is written: a migration that left a
    source file out would read as a complete one."""

    def __init__(self, files: "list[tuple[str, str]]") -> None:
        self.files = list(files)
        shown = "; ".join(f"{safe_label(n)}: {safe_label(why)}" for n, why in self.files)
        super().__init__(i18n_text(
            f"--source-dir 有 {len(self.files)} 個檔既不是 kubectl 讀得了的 manifest、"
            f"也不是純 rule file，遷移中止、不寫任何檔: {shown}",
            f"--source-dir: {len(self.files)} file(s) are neither a manifest kubectl "
            f"reads nor a plain rule file; nothing is written: {shown}"))


def parse_configmap_rules(source_dir: Path,
                          listing: "Optional[_lib_parse_tree.Listing]" = None) -> List[dict]:
    """Parse ConfigMap YAML files and extract rule groups.

    Scans source_dir for YAML files containing Prometheus rules in ConfigMap format
    (i.e., with a 'data:' key containing nested YAML with 'groups:' and 'rules:').

    Args:
        source_dir: Directory containing ConfigMap YAML files
        listing: its `list_source_dir` (taken here when None)

    Returns:
        List of dicts: {name, file, rule_groups, cm_data_key, already_crd}.
        ``already_crd`` is True for a PrometheusRule's ``spec`` (#3426): it
        is already the CRD this tool writes, so it is not converted — the
        callers name it on stderr and in the checklist (never a silent skip,
        never an error).

    Raises:
        ParseTreeError: da-guard cannot list *source_dir* (not there, say) —
            #3721: no `is_dir` of its own here; that second look, made after
            `_lib_input.selected` judged the directory, turned a directory
            removed in between into an `issues` WARNING and a migration made
            from no source file, rc 0
        SourceFilesUnreadable: a file that is neither a manifest kubectl
            reads nor a plain rule file (every one named, with the reader's
            reason; #3426)
        _lib_consumer_read.ConsumerReadError: a rule file Prometheus' rule
            loader refuses (RuleFilesRefused, naming each), or no
            da-read / da-crdecode — each stops the run at rc 2 in `main`
    """
    # #3206 (ADR-036 §12) / #3426: which rule files a file holds is
    # `_lib_consumer_read.rule_files_in`'s answer — the one classifier: the
    # manifest kubectl reads (a `kind: List`, which `kubectl get … -o yaml`
    # writes, is its items; a ConfigMap's data values that set out to be rule
    # files), else a plain rule file (its YAML reading, or — when none reads
    # it — Prometheus' rule loader, declares `groups`). Any other file stops
    # the run (`SourceFilesUnreadable`, every one named with its readers'
    # reason), and so does a rule file the rule loader refuses
    # (`RuleFilesRefused`): no CRD set is generated with a source left out.
    # Top level only; `warn_nested` names what that leaves out.
    if listing is None:
        listing = list_source_dir(source_dir)
    paths = source_files(source_dir, listing)
    try:
        found = _lib_consumer_read.rule_files_in(paths)
    except _lib_consumer_read.RuleFileSourcesRefused as exc:
        raise SourceFilesUnreadable([(Path(p).name, why) for p, why in exc.files]) from exc
    rule_files = _lib_consumer_read.require_read(
        [rf for path in paths for rf in found[str(path)]])
    results = []
    for rf in rule_files:
        path = Path(rf.manifest)
        results.append({
            "name": rf.object_name if rf.kind is not None else path.stem,
            "file": path.name,
            "cm_data_key": rf.key if rf.kind == "ConfigMap" else None,
            "rule_groups": rf.groups,
            "already_crd": rf.kind == "PrometheusRule",
        })
    return results


def already_crd_notice(item: dict) -> str:
    """The line that names a PrometheusRule found in --source-dir (#3426):
    it is already a CRD, so nothing is migrated from it."""
    return i18n_text(
        f"{item['file']}: PrometheusRule {item['name']} 已是 PrometheusRule CRD，不需遷移（未轉換）",
        f"{item['file']}: PrometheusRule {item['name']} is already a PrometheusRule CRD; "
        "nothing to migrate (not converted)")


def convert_rules_to_crd(
    rule_groups: list,
    pack_name: str,
    namespace: str,
) -> dict:
    """Convert ConfigMap-format rule groups into PrometheusRule CRD.

    Args:
        rule_groups: List of rule group dicts
        pack_name: Name for the rule pack
        namespace: Target Kubernetes namespace

    Returns:
        PrometheusRule CRD dict
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            # #3471: the shipped PrometheusRule naming, the declaration's
            # (`OPERATOR_RULES`): the same name operator-generate gives its copy.
            "name": _lib_consumer_read.OPERATOR_RULES.file_stem(pack_name),
            "namespace": namespace,
            # #3427: the selector labels operator-generate puts on its copy,
            # from the one definition — a Prometheus whose ruleSelector
            # matches `release` loaded none of the CRDs this used to emit.
            "labels": {
                "app.kubernetes.io/part-of": "dynamic-alerting",
                **_lib_consumer_read.PROMETHEUS_RULE_SELECTOR_LABELS,
                "migrated-from": "configmap",
            },
        },
        "spec": {
            "groups": rule_groups,
        },
    }


def analyze_migration(source_dir: Path,
                      source_listing: "Optional[_lib_parse_tree.Listing]" = None) -> dict:
    """Analyze migration scope and identify potential issues.

    Args:
        source_dir: Directory with ConfigMap YAML files
        source_listing: its `list_source_dir` (taken here when None)

    Returns:
        Analysis dict with counts and issues
    """
    analysis = {
        "configmap_files": 0,
        "already_crd": [],
        "rule_groups": 0,
        "estimated_crds": 0,
        "issues": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    found = parse_configmap_rules(source_dir, source_listing)
    cm_rules = [item for item in found if not item["already_crd"]]
    analysis["already_crd"] = [{"file": i["file"], "name": i["name"]}
                               for i in found if i["already_crd"]]
    analysis["configmap_files"] = len(cm_rules)
    for item in cm_rules:
        analysis["rule_groups"] += len(item.get("rule_groups", []))
    # #3426: a rule file the rule loader refuses, an unreadable source file
    # and a missing da-read / da-crdecode are no `issues` entry beside a
    # partial count: they reach `main` (`_sources_or_exit`), rc 2. #3721: a
    # source directory gone since it was judged is no `issues` entry either
    # (it was "Source directory error", a migration from no file, rc 0).

    analysis["estimated_crds"] = analysis["configmap_files"]

    return analysis


def _output_dir_arg(value: str) -> Path:
    """`--output-dir` 的 argparse type：拒絕空字串／純空白。

    ⛔ 不能靠 `type=Path` 之後在 main() 補判：`Path("")` 就是 `Path(".")`，
    到那時「使用者傳了空字串」與「使用者傳了 `.`」已經無法區分（實測：補判版本
    對 `--output-dir ""` 回 rc=0 並照樣寫檔）。判定必須發生在還看得見原始字串的
    這一層。
    ⚠️ 為什麼值得判：#1582 之後「要寫檔就必須打這個參數」，於是
    `--output-dir "$OUT"` 在 OUT 未設時會傳空字串、產物無聲落在呼叫目錄——
    命中面從零變成全部。`.` 仍然合法（明確要求寫進目前目錄）。
    """
    if not value.strip():
        raise argparse.ArgumentTypeError(i18n_text(
            "不能是空字串；要寫進目前目錄請明確傳 --output-dir .",
            "must not be empty; pass --output-dir . to write into the current directory",
        ))
    return Path(value)

# #3572: where the checklist sends the reader for Alertmanager routing under
# the Operator — a published page (#1447: the reader's repository has no docs/).
_ROUTING_DOC_URL = f"{DOCS_SITE_BASE}integration/operator-alertmanager-integration/"


def build_migration_checklist(
    source_dir: Path,
    output_dir: Optional[Path],
    result: dict,
) -> str:
    """Generate a markdown-formatted migration checklist.

    Routing is no CRD this tool writes (#3572): its phase renders
    alertmanager.yaml with generate-routes and hands it to the Alertmanager
    as its configSecret.

    Args:
        source_dir: Source ConfigMap directory
        output_dir: Output CRD directory, or ``None`` when no ``--output-dir``
            was given (#1582 — writing is opt-in)
        result: Migration result dict

    Returns:
        Markdown-formatted checklist string
    """
    # #1582：checklist 把輸出目錄嵌進 13 條 `kubectl apply -f {output_dir}/...`。
    # 沒有 --output-dir 時就沒有真實路徑。⛔ 不要在這裡填回預設值——那會教使用者
    # 對一個從來沒被建立的目錄跑 kubectl，而 kubectl 的錯誤訊息不會提到是我們印錯的。
    if output_dir is None:
        output_dir = "<--output-dir>"
    timestamp = datetime.now(timezone.utc).isoformat()
    total = len(result.get("prometheus_rules", []))
    # #3426: PrometheusRules found in --source-dir are named here — they are
    # already the CRD, so nothing was converted from them.
    already = result.get("already_crd", [])
    zh_already = en_already = ""
    if already:
        rows = "".join(f"- `{a['file']}`: PrometheusRule `{a['name']}`\n" for a in already)
        zh_already = ("\n## 已是 PrometheusRule CRD（不需遷移）\n\n"
                      "以下來源已是 PrometheusRule CRD，未轉換、不在輸出 CRD 數內：\n\n" + rows)
        en_already = ("\n## Already PrometheusRule CRDs (nothing to migrate)\n\n"
                      "These sources are already PrometheusRule CRDs; they were not "
                      "converted and are not in the output CRD count:\n\n" + rows)

    zh_checklist = f"""# 遷移檢核清單

生成時間: {timestamp}

## 概覽

- **ConfigMap 檔案數**: {result.get('configmap_files', 0)}
- **Rule 群組數**: {result.get('rule_group_count', 0)}
- **輸出 CRD 數**: {total} PrometheusRules
- **來源目錄**: {source_dir}
- **輸出目錄**: {output_dir}
{zh_already}
## 遷移步驟

### Phase 1: 前置驗證

- [ ] 備份現有 ConfigMap（`kubectl get configmap -n monitoring -o yaml > configmap-backup.yaml`）
- [ ] 備份現有 Prometheus 配置（`kubectl get prometheus -n monitoring -o yaml > prometheus-backup.yaml`）
- [ ] 驗證新 CRD 檔案數正確（應為 {total} 個）
- [ ] 檢查 CRD 檔案中是否有語法錯誤（`kubectl apply -f {output_dir}/ --dry-run=client`）

### Phase 2: 套用新 CRD

- [ ] 套用 PrometheusRule CRD (`kubectl apply -f {output_dir}/*-rule-pack-*.yaml`)
- [ ] 等待 Prometheus reconcile（約 30-60 秒）
- [ ] 驗證 Prometheus 載入新 Rule（`kubectl logs -n monitoring -l app=prometheus -c prometheus | grep "loaded groups"`)

### Phase 2b: Alertmanager 路由（configSecret，不是 AlertmanagerConfig CRD）

Alertmanager 路由不以 AlertmanagerConfig CRD 產生（#3572）：Prometheus Operator 會把它們的路由排在最前面、
強制 `continue: true` 並加上 namespace matcher，平台路由（Watchdog、custom 隔離、synthetic probe、sentinel）
因此無法排在第一。詳見 {_ROUTING_DOC_URL}

- [ ] 用路由產生器產出 alertmanager.yaml：
  ```
  da-tools generate-routes --config-dir conf.d --output-alertmanager --base-config <你的 alertmanager.yaml> -o alertmanager.yaml
  ```
- [ ] 建立／更新 Secret：
  ```
  kubectl create secret generic <secret> -n monitoring --from-file=alertmanager.yaml --dry-run=client -o yaml | kubectl apply -f -
  ```
- [ ] 把 Alertmanager 的 configSecret 設為 `<secret>`（kube-prometheus-stack：`alertmanager.alertmanagerSpec.configSecret`）
- [ ] 若叢集裡還有舊版 operator-generate 產生的 AlertmanagerConfig（`kubectl get alertmanagerconfig -A -l app.kubernetes.io/part-of=dynamic-alerting`），將它們刪除（`da-tools operator-check` 會以警告列出）

### Phase 3: 驗證規則

- [ ] 確認 Prometheus 成功載入所有 Rule Pack
  ```
  kubectl port-forward -n monitoring svc/prometheus 9090:9090
  # 訪問 http://localhost:9090/rules
  ```
- [ ] 檢查是否有失敗的 Rule（Rules 頁面應該沒有紅色警告）
- [ ] 驗證告警正常觸發（檢查 Active 或 Pending 的 alert）

### Phase 4: 切換 Helm 配置

- [ ] 更新 Helm values: `rules.mode: operator`（需要修改 values.yaml）
- [ ] 執行 Helm upgrade
- [ ] 驗證 Prometheus Pod 未發生 crash（`kubectl get pods -n monitoring`）

### Phase 5: 清理舊資源

- [ ] 確認所有規則都已成功遷移
- [ ] 刪除舊 ConfigMap: `kubectl delete configmap -n monitoring <configmap-names>`
- [ ] 刪除舊 Projected Volume 設定（如適用）
- [ ] 驗證 Prometheus 仍正常運作

### Phase 6: 後置驗證

- [ ] 檢查告警是否繼續正常運作
- [ ] 檢查 Alertmanager route 配置是否正確
- [ ] 運行告警品質檢查（`da-tools alert-quality --config-dir conf.d/`）
- [ ] 記錄遷移完成時間與結果

## 回滾計畫

如需回滾：

1. 刪除新 CRD:
   ```
   kubectl delete -f {output_dir}/
   ```

2. 恢復舊配置:
   ```
   kubectl apply -f configmap-backup.yaml
   kubectl apply -f prometheus-backup.yaml
   ```

3. 重啟 Prometheus Pod（強制 ConfigMap reload）:
   ```
   kubectl rollout restart deployment -n monitoring prometheus-operator
   ```

## 注意事項

- 遷移過程中，Prometheus 不會中斷（配置滾動更新）
- 新 CRD 被標記為 `migrated-from: configmap`，便於追蹤
- Alertmanager 路由只由 generate-routes 產出的 configSecret 承載；不要另外套用 AlertmanagerConfig CRD
- 租戶 RBAC 需在遷移後手動驗證

"""

    en_checklist = f"""# Migration Checklist

Generated: {timestamp}

## Overview

- **ConfigMap Files**: {result.get('configmap_files', 0)}
- **Rule Groups**: {result.get('rule_group_count', 0)}
- **Output CRDs**: {total} PrometheusRules
- **Source Directory**: {source_dir}
- **Output Directory**: {output_dir}
{en_already}
## Migration Steps

### Phase 1: Pre-Migration Validation

- [ ] Back up existing ConfigMaps (`kubectl get configmap -n monitoring -o yaml > configmap-backup.yaml`)
- [ ] Back up existing Prometheus config (`kubectl get prometheus -n monitoring -o yaml > prometheus-backup.yaml`)
- [ ] Verify CRD file count is correct (should be {total} files)
- [ ] Check for syntax errors in CRD files (`kubectl apply -f {output_dir}/ --dry-run=client`)

### Phase 2: Apply New CRDs

- [ ] Apply PrometheusRule CRDs (`kubectl apply -f {output_dir}/*-rule-pack-*.yaml`)
- [ ] Wait for Prometheus reconciliation (approximately 30-60 seconds)
- [ ] Verify Prometheus loaded new Rules (`kubectl logs -n monitoring -l app=prometheus -c prometheus | grep "loaded groups"`)

### Phase 2b: Alertmanager Routing (configSecret, not AlertmanagerConfig CRDs)

Alertmanager routing is not generated as AlertmanagerConfig CRDs (#3572): the Prometheus Operator puts their
routes first with `continue: true` and a namespace matcher, so the platform routes (Watchdog, custom isolation,
synthetic probe, sentinel) could not come first. See {_ROUTING_DOC_URL}

- [ ] Render alertmanager.yaml with the route generator:
  ```
  da-tools generate-routes --config-dir conf.d --output-alertmanager --base-config <your alertmanager.yaml> -o alertmanager.yaml
  ```
- [ ] Create / update the Secret:
  ```
  kubectl create secret generic <secret> -n monitoring --from-file=alertmanager.yaml --dry-run=client -o yaml | kubectl apply -f -
  ```
- [ ] Set the Alertmanager's configSecret to `<secret>` (kube-prometheus-stack: `alertmanager.alertmanagerSpec.configSecret`)
- [ ] Delete any AlertmanagerConfigs an older operator-generate left in the cluster (`kubectl get alertmanagerconfig -A -l app.kubernetes.io/part-of=dynamic-alerting`; `da-tools operator-check` warns about them)

### Phase 3: Verify Rules

- [ ] Confirm Prometheus successfully loaded all Rule Packs
  ```
  kubectl port-forward -n monitoring svc/prometheus 9090:9090
  # Visit http://localhost:9090/rules
  ```
- [ ] Check for failed Rules (Rules page should have no red warnings)
- [ ] Verify alerts trigger normally (check Active or Pending alerts)

### Phase 4: Switch Helm Configuration

- [ ] Update Helm values: `rules.mode: operator` (requires modifying values.yaml)
- [ ] Execute Helm upgrade
- [ ] Verify Prometheus Pod did not crash (`kubectl get pods -n monitoring`)

### Phase 5: Clean Up Old Resources

- [ ] Confirm all rules successfully migrated
- [ ] Delete old ConfigMaps: `kubectl delete configmap -n monitoring <configmap-names>`
- [ ] Delete old Projected Volume settings (if applicable)
- [ ] Verify Prometheus still works normally

### Phase 6: Post-Migration Verification

- [ ] Check if alerts continue to work normally
- [ ] Verify Alertmanager route configuration is correct
- [ ] Run alert quality checks (`da-tools alert-quality --config-dir conf.d/`)
- [ ] Record migration completion time and results

## Rollback Plan

If rollback is needed:

1. Delete new CRDs:
   ```
   kubectl delete -f {output_dir}/
   ```

2. Restore old configuration:
   ```
   kubectl apply -f configmap-backup.yaml
   kubectl apply -f prometheus-backup.yaml
   ```

3. Restart Prometheus Pod (force ConfigMap reload):
   ```
   kubectl rollout restart deployment -n monitoring prometheus-operator
   ```

## Important Notes

- Prometheus will not be interrupted during migration (rolling configuration update)
- New CRDs are marked with `migrated-from: configmap` for tracking
- Alertmanager routing lives only in the configSecret generate-routes renders; do not apply AlertmanagerConfig CRDs beside it
- Tenant RBAC must be manually verified post-migration

"""

    return zh_checklist if _LANG == "zh" else en_checklist


def generate_migration(
    source_dir: Path,
    output_dir: Optional[Path],
    namespace: str,
    source_listing: "Optional[_lib_parse_tree.Listing]" = None,
) -> dict:
    """Main orchestration: parse → convert → write CRDs.

    Args:
        source_dir: Source ConfigMap directory
        output_dir: Output CRD directory
        namespace: Target Kubernetes namespace
        source_listing: its `list_source_dir` (taken here when None)

    Returns:
        Result dict with generated CRDs and metadata
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "prometheus_rules": [],
        "configmap_files": 0,
        "already_crd": [],
        "rule_group_count": 0,
        "errors": [],
    }

    # Parse ConfigMap rules
    found = parse_configmap_rules(source_dir, source_listing)
    cm_rules = [item for item in found if not item["already_crd"]]
    result["already_crd"] = [{"file": i["file"], "name": i["name"]}
                             for i in found if i["already_crd"]]
    result["configmap_files"] = len(cm_rules)

    for cm_item in cm_rules:
        try:
            rule_groups = cm_item.get("rule_groups", [])
            result["rule_group_count"] += len(rule_groups)
            rule_pack_name = cm_item["name"].replace(
                _lib_consumer_read.RULE_PACKS.prefix, "").lower()

            crd = convert_rules_to_crd(rule_groups, rule_pack_name, namespace)
            result["prometheus_rules"].append({
                "name": crd["metadata"]["name"],
                "file": cm_item["file"],
                "crd": crd,
            })
        except Exception as exc:
            result["errors"].append(
                i18n_text(
                    f"轉換 {cm_item['file']} 失敗: {exc}",
                    f"Failed to convert {cm_item['file']}: {exc}",
                )
            )
    # ConsumerReadError (a refused rule file, no da-read / da-crdecode)
    # reaches `main`: rc 2 before any write (#3426); so does a source
    # directory gone since it was judged (#3721: no `is_dir` to catch).

    return result


def plan_stdout(
    *,
    checklist_only: bool,
    dry_run: bool,
    json_mode: bool,
    result: dict,
    checklist: str,
    namespace: str,
    source_dir: Path,
) -> List[str]:
    """Pure decision table for the (checklist_only × dry_run × json) matrix.

    Returns the exact sequence of text chunks main() must emit to stdout, in
    order; each chunk carries its own newlines, so the caller's only job is
    ``print(chunk, end="")``.  Zero I/O happens here — the whole 8-combo
    output matrix is decided in this one function, so any future regression
    in mode precedence shows up at the unit level instead of only in
    subprocess gates.

    This matrix bit twice in one PR cycle (#1112 / CodeRabbit); both
    historical fixes are explicit branches below:

    * **bug 1** — ``--checklist-only`` MUST win over ``--dry-run``.  The old
      guard ``if checklist_only and not dry_run:`` let the combination fall
      through to the dry-run branch and serve an (empty) CRD preview with NO
      ``checklist`` field at all — valid JSON, so the stdout-contract gate
      stayed green while the caller silently got the wrong payload.
      ``--checklist-only`` already implies "write nothing", so the narrower
      no-op flag must not override the one that selects the payload.

    * **bug 2** — in ``--checklist-only --dry-run --json`` the summary used
      to be suppressed by the "dry-run + json already printed its one
      document" rule, leaving stdout EMPTY while ``--json`` was asked for.
      The checklist envelope IS that run's one document, so the suppression
      only applies to the pure dry-run preview (see the summary condition).

    Args:
        checklist_only: ``--checklist-only`` flag
        dry_run: ``--dry-run`` flag
        json_mode: ``--json`` flag
        result: migration result dict (from generate_migration(), or the
            analysis dict with forced-empty CRD lists in checklist-only mode)
        checklist: pre-rendered migration checklist markdown
        namespace: target namespace (dry-run JSON preview metadata)
        source_dir: resolved source dir (dry-run JSON preview metadata)

    Returns:
        List of text chunks to write to stdout, in order.
    """
    docs: List[str] = []

    # ── Mode payload ────────────────────────────────────────────────────
    # bug 1 (#1112, CodeRabbit): `--checklist-only` outranks `--dry-run` —
    # this `if/elif` ORDER is the fix. Do not reorder.
    if checklist_only:
        if not json_mode:
            docs.append(checklist + "\n")
        # JSON mode: the checklist rides INSIDE the summary envelope below —
        # stdout must stay exactly ONE JSON document.
    elif dry_run:
        if json_mode:
            output_obj = {
                "metadata": {
                    "timestamp": result.get("timestamp"),
                    "namespace": namespace,
                    "source_dir": str(source_dir),
                    "configmap_files": result.get("configmap_files", 0),
                    "rule_groups": result.get("rule_group_count", 0),
                },
                "prometheus_rules": [item["crd"] for item in result.get("prometheus_rules", [])],
                # #3426: PrometheusRules of --source-dir, named, not converted
                "already_crd": list(result.get("already_crd", [])),
                "errors": result.get("errors", []),
            }
            docs.append(encode_json(output_obj) + "\n")
        else:
            docs.append("# MIGRATION CHECKLIST\n")
            docs.append(checklist + "\n")
            docs.append("\n# CRD PREVIEW\n\n")
            all_crds = [item["crd"] for item in result["prometheus_rules"]]
            if yaml:
                for i, crd in enumerate(all_crds):
                    if i > 0:
                        docs.append("---\n")
                    docs.append(yaml.dump(crd, default_flow_style=False, allow_unicode=True))
            else:
                for crd in all_crds:
                    docs.append(_dict_to_yaml(crd) + "\n")
                    docs.append("---\n")
    # else: write mode — CRDs + checklist go to files and progress lines go
    # to stderr (both are main()'s job); stdout carries only the JSON summary
    # below (text mode: nothing).

    # ── Summary document ────────────────────────────────────────────────
    # The human-readable "✓ ..." summary goes to stderr and stays in main();
    # only the JSON summary is a stdout document.
    #
    # bug 2 (#1112, CodeRabbit): in dry-run JSON the CRD-preview above is
    # already the ONE document stdout may carry — EXCEPT when checklist_only
    # also holds: then no preview was emitted and the checklist envelope IS
    # this run's one document, so suppressing it would leave stdout empty
    # while `--json` was asked for. Hence `or checklist_only`.
    if json_mode and (not dry_run or checklist_only):
        summary = {
            "configmap_files": result.get("configmap_files", 0),
            "rule_groups": result.get("rule_group_count", 0),
            "prometheus_rules": len(result["prometheus_rules"]),
            "total_crds": len(result["prometheus_rules"]),
            # #3426: PrometheusRules of --source-dir, named, not converted
            "already_crd": list(result.get("already_crd", [])),
        }
        if checklist_only:
            # #1112: stdout must be exactly ONE JSON document. The checklist
            # is the whole point of --checklist-only, so it is carried INSIDE
            # the summary document (as a string) rather than dumped as raw
            # Markdown next to it — no information is lost from stdout, and
            # the summary keys stay exactly where a consumer already expects
            # them.
            summary["status"] = "checklist_only"
            summary["checklist"] = checklist
        docs.append(encode_json(summary) + "\n")

    return docs


def _sources_or_exit(fn, *args, **kwargs):
    """*fn*'s result; a --source-dir that cannot be answered exits 2.

    ⛔ Fail-closed: a source file no reader reads, a rule file the rule
    loader refuses, or no usable da-guard / da-read / da-crdecode leaves the
    CRD set unknown — an `issues` line next to a partial set would read as a
    migration plan.
    """
    try:
        return fn(*args, **kwargs)
    except SourceFilesUnreadable as exc:
        # #3426: a source file no reader reads stops the run before anything
        # is written, every such file named.
        die_caller_error(f"ERROR: {exc}", reason="source_files_unreadable")
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3426: a rule file Prometheus' rule loader refuses, or no
        # da-read / da-crdecode — no answer about the sources, so no CRD set:
        # the run stops before anything is written.
        die_caller_error(f"ERROR: --source-dir rule files: {_lib_consumer_read.error_text(exc)}",
                         reason="rule_files_unreadable")
    except ParseTreeError as exc:
        # No usable `da-guard parse-tree --list-only`: --source-dir was not listed.
        die_caller_error(_lib_parse_cache.parse_tree_error_line(exc),
                         reason="source_dir_unlisted")


def refuse_routing_flags(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """#3572: a flag that only fed AlertmanagerConfig is a caller error (rc 2,
    nothing on stdout, nothing written) that names it and says how routing is
    rendered now. A flag counts when it was given (`supplied`), whatever its
    value: an explicit `--config-dir conf.d` (the old default) is refused like
    any other."""
    given = [flag for flag, dest in _ROUTING_FLAGS if supplied(args, parser, dest)]
    if given:
        die_caller_error(
            f"ERROR: {', '.join(given)}: "
            f"{i18n_text(OPERATOR_ROUTING_REMEDY_ZH, OPERATOR_ROUTING_REMEDY_EN)}",
            reason="routing_not_generated")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=i18n_text(_HELP["zh"]["desc"], _HELP["en"]["desc"]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _lib_input.add_input(
        parser, "--source-dir", select=_lib_input.DA_GUARD_LISTS,
        empty=_lib_input.EMPTY_REFUSED, required=True,
        help=i18n_text(_HELP["zh"]["source_dir"], _HELP["en"]["source_dir"]),
    )
    parser.add_argument(
        "--output-dir",
        type=_output_dir_arg,
        default=None,   # #1582：不給就不寫檔（寫入是 opt-in），見 main() 的寫入閘
        help=i18n_text(_HELP["zh"]["output_dir"], _HELP["en"]["output_dir"]),
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help=i18n_text(_HELP["zh"]["namespace"], _HELP["en"]["namespace"]),
    )
    # #3572: hidden and refused (`refuse_routing_flags`) — see _ROUTING_FLAGS.
    parser.add_argument("--config-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--receiver-template", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--secret-name", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--secret-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=i18n_text(_HELP["zh"]["dry_run"], _HELP["en"]["dry_run"]),
    )
    parser.add_argument(
        "--checklist-only",
        action="store_true",
        help=i18n_text(
            _HELP["zh"]["checklist_only"],
            _HELP["en"]["checklist_only"],
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_text(_HELP["zh"]["json"], _HELP["en"]["json"]),
    )

    args = parser.parse_args()
    refuse_routing_flags(args, parser)
    # #3392: a directory the caller gave that is not there or not a
    # directory is a caller error, named before anything is analysed or
    # written — it was an `issues` WARNING and a checklist / CRD set made from
    # nothing, rc 0. `selected` judges it (#3721: once — a `classify_path`
    # of its own here was a second look at the same path).
    source_dir = _lib_input.selected(args, "source_dir", lang=_LANG)[0].resolve()
    # #1582：沒給 --output-dir 就沒有寫入目標。⛔ 不要在這裡塞回一個預設值。
    output_dir = args.output_dir.resolve() if args.output_dir is not None else None

    # #1582：理由與 operator_generate.main() 同一段（stdout 升為預設產物通道 ⇒
    # 必須與寫檔通道等價：UTF-8 且 LF）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError, OSError):
        pass
    # ⛔ 一個真相來源。「沒有寫入目標」與 --dry-run 在**輸出面完全同義**，所以摺成
    # 同一個布林，而不是給 plan_stdout() 加第四個維度——那張 8 組合矩陣已經被
    # characterization 釘住（#1112 兩個歷史 bug 的修法就住在裡面）。分開判會讓
    # 「不寫檔」與「印什麼」各有一份判定，正是本票在修的那種形狀。
    no_write = args.dry_run or output_dir is None

    # Analyze migration
    print(
        i18n_text(
            "正在分析遷移範圍...",
            "Analyzing migration scope...",
        ),
        file=sys.stderr,
    )
    # One listing of --source-dir for analysis and generation (#3206).
    source_listing = _sources_or_exit(list_source_dir, source_dir)
    # #3462: a --source-dir whose top level holds no file da-guard lists as
    # a config file is a caller error (rc 2, nothing written) — it was a
    # checklist and a CRD set made from no source, rc 0.
    _lib_input.require_selection(
        args, "source_dir", source_files(source_dir, source_listing),
        wanted=i18n_text("da-guard 列為頂層 config 檔的檔案",
                         "a top-level file da-guard lists as a config file"),
        lang=_LANG)
    # #3724: the judgement of --source-dir is done (not a conf.d, so not
    # `declared_trees`'): every later look at it is a reading.
    _lib_input.judged("--source-dir", source_dir)
    analysis = _sources_or_exit(analyze_migration, source_dir, source_listing)

    if analysis["issues"]:
        for issue in analysis["issues"]:
            print(f"WARNING: {safe_label(issue)}", file=sys.stderr)
    # #3426: a PrometheusRule in --source-dir is named, never passed over in
    # silence; it is no error (the rc is not touched).
    for item in analysis.get("already_crd", []):
        print(f"NOTE: {safe_label(already_crd_notice(item))}", file=sys.stderr)

    # Generate migration if not checklist-only
    if args.checklist_only:
        result = analysis
        result["prometheus_rules"] = []
        # analyze_migration() counts rule groups under "rule_groups", but the
        # checklist/summary consumers read generate_migration()'s key
        # "rule_group_count" — without this bridge the count is always 0.
        result["rule_group_count"] = result.get("rule_groups", 0)
    else:
        print(
            i18n_text(
                "正在生成 CRD...",
                "Generating CRDs...",
            ),
            file=sys.stderr,
        )
        result = _sources_or_exit(
            generate_migration,
            source_dir,
            output_dir,
            args.namespace,
            source_listing=source_listing,
        )

    # The checklist is rendered once here (pure string) and reused by both the
    # write-mode file and whatever stdout documents plan_stdout() selects.
    checklist = build_migration_checklist(source_dir, output_dir, result)

    # Write mode: the only branch with filesystem side effects. Progress lines
    # go to stderr (existing convention); stdout is decided below.
    if not args.checklist_only and not no_write:
        # #1641: every path below descends from --output-dir. The shared
        # write_yaml_crd keeps the RAISING form (it is a library reached by
        # several tools), so the one handler for the whole block lives here:
        # an unusable --output-dir is rc=2 + one line naming the flag, not a
        # traceback at rc=1 (which reads as "your migration has a finding").
        try:
            ensure_dir(output_dir, flag="--output-dir")

            # Write CRD files
            for item in result["prometheus_rules"]:
                crd = item["crd"]
                name = crd["metadata"]["name"]
                output_path = output_dir / f"{name}.yaml"
                write_yaml_crd(output_path, crd, gitops=False, flag="--output-dir")
                print(f"Generated: {safe_label(output_path)}", file=sys.stderr)

            # Write checklist
            checklist_path = output_dir / "MIGRATION-CHECKLIST.md"
            write_text_secure(str(checklist_path), checklist, flag="--output-dir")
            print(f"Generated: {safe_label(checklist_path)}", file=sys.stderr)
        except OutputWriteError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    # stdout: the whole 8-combo (checklist_only × dry_run × json) matrix is
    # decided in plan_stdout() — main() only prints. See plan_stdout() for the
    # two historical matrix bugs (#1112) whose fixes live there as explicit
    # branches.
    for doc in plan_stdout(
        checklist_only=args.checklist_only,
        dry_run=no_write,
        json_mode=args.json,
        result=result,
        checklist=checklist,
        namespace=args.namespace,
        source_dir=source_dir,
    ):
        print(doc, end="")

    # Human-readable summary → stderr, in every non-JSON mode (the JSON
    # summary, when one is due, is a stdout document owned by plan_stdout()).
    if not args.json:
        pr_count = len(result["prometheus_rules"])
        print(
            i18n_text(
                f"\n✓ 遷移分析完成: {result.get('configmap_files', 0)} 個 ConfigMap → "
                f"{pr_count} 個 PrometheusRule CRD（Alertmanager 路由見 checklist："
                f"generate-routes + configSecret）",
                f"\n✓ Migration analysis complete: {result.get('configmap_files', 0)} ConfigMaps → "
                f"{pr_count} PrometheusRule CRDs (Alertmanager routing: see the checklist — "
                f"generate-routes + configSecret)",
            ),
            file=sys.stderr,
        )


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
