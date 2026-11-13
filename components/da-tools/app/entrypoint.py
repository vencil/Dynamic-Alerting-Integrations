#!/usr/bin/env python3
"""da-tools CLI dispatcher: route a subcommand to its tool script."""
import importlib.util
import os
import sys
import traceback


def _build_help_text(lang):
    """Build help text in the specified language.

    Args:
        lang: 'zh' (Chinese) or 'en' (English)

    Returns:
        Help text string
    """
    if lang == 'zh':
        return """da-tools — 動態告警 CLI 工具包

統一的可攜式驗證和遷移工具入口。
為平台工程師和 SRE 設計，無需克隆完整倉庫即可驗證整合。

用法:
    da-tools <command> [options]

命令 (Prometheus API — 可攜式):
    check-alert       查詢租戶的告警觸發狀態
    diagnose          租戶健康檢查 (配置 + 指標 + 告警狀態)
    batch-diagnose    多租戶並行健康報告 (轉換後)
    baseline          觀察指標並建議閾值
    validate          比較舊規則 vs 新規則 (影子監控)
    backtest          根據歷史資料回測閾值變更
    cutover           一鍵影子監控轉換 (§7.1 所有步驟)
    blind-spot        掃描叢集目標並查找未監控實例
    alert-quality     警報品質評估 (震盪/閒置/延遲分析)
    alert-correlate   告警關聯分析 (時間窗口聚類 + 根因推斷)

命令 (配置生成 — 讀取租戶 YAML):
    generate-routes   租戶 YAML → route/receiver/inhibit 合併進 Alertmanager 設定
    patch-config      修補 threshold-config ConfigMap (含 --diff 預覽)
    analyze-gaps      Rule Pack 間隙分析 (用於自訂規則)

命令 (文件系統 — 離線):
    migrate           轉換遺留 Prometheus 規則為動態格式
    scaffold          互動式生成租戶配置
    offboard          租戶配置預檢和移除
    deprecate         下架指標：從設定中刪除其 key
    lint              針對治理禁止列表驗證自訂規則
    onboard           分析現有 Alertmanager/Prometheus 配置以供遷移
    validate-config   一站式配置驗證 (YAML + schema + routing + policy)
    config-diff       GitOps PR 審查的目錄級配置差異
    drift-detect      跨叢集配置漂移偵測 (目錄級 SHA-256 比對)
    evaluate-policy   Policy-as-Code 策略評估 (宣告式 DSL)
    opa-evaluate      OPA Rego 策略評估橋接 (OPA 整合)
    cardinality-forecast  基數預測 (線性回歸趨勢分析)
    test-notification 多通道通知連通性測試 (驗證 receiver 可達性)
    threshold-recommend 閾值推薦引擎 (基於歷史 P50/P95/P99)
    threshold-govern  閾值治理報告 — 推薦→過濾→列出該調的腐敗閾值（唯讀，不寫入；#656）
    explain-route     路由合併管線除錯器 (四層展開 + 設定檔擴展)
    byo-check         BYO Alertmanager 整合前檢 (端點 + 配置驗證)
    federation-check  Prometheus Federation 健康檢查
    grafana-import    Grafana Dashboard JSON 匯入
    shadow-verify     Shadow Monitoring 雙軌比對驗證
    discover-mappings 自動發現 1:N 實例-租戶映射 (掃描 exporter /metrics)
    init              在客戶 repo 初始化 Dynamic Alerting 整合骨架 (CI/CD + conf.d + Kustomize)
    config-history    配置快照與歷史追蹤 (snapshot / log / diff / show)
    gitops-check      conf.d 本機結構檢查 (local)
    state-reconcile   遷移狀態目錄聲明式一致化 (.da/state/ schema 驗證 + .da/manifest.json 重建)
    rule-pack-diff    Rule Pack 兩版本機械比對 (added / removed / breaking label schema)
    silencer-drift-check  AM silence 對 v2 rule pack 漂移偵測 (offline，吃 amtool silence query -o json)
    explain-keys      逐鍵列出來源、讀取端、判決與 disable 的作用；--assert 對照預期結果 (ADR-038 決定 9)
    generator-service 產生器容器的服務：經 Unix socket 判定寫入後的 conf.d 能否通過 generate-routes --validate --strict (ADR-036 §3)

命令 (Operator-Native — CRD 產生與驗證):
    operator-generate 產出 PrometheusRule / ServiceMonitor CRD YAML（Alertmanager 路由改用 generate-routes → configSecret）
    operator-check    驗證 Operator CRD 部署狀態 (5 項檢查 + 診斷報告)
    runtime-audit     Git rule-packs ↔ Prometheus runtime 唯讀對帳 (#747；MISSING/UNHEALTHY/ORPHAN)
    migrate-to-operator ConfigMap 格式遷移至 Operator 原生 CRD (含遷移清單與預檢)
    migrate-credentials 找出 conf.d 裡明文的 receiver 憑證：改寫成 Secret 參照、產出 Secret 清單與待輪替清單 (#1560，只報告)

命令 (Federation — 多叢集):
    rule-pack-split   Rule Pack 分層拆分 (edge Part 1 + central Part 2+3)
    fed-key           產生 / 輪替 federation JWT 簽章金鑰 (ADR-020 IV-2l)

命令 (Guard — Dangling Defaults Guard, v2.8.0):
    guard             驗證 conf.d/ 樹是否安全 (schema + cardinality；路由交給 generate-routes)
                      子命令: defaults-impact

命令 (Tenant Verify — rollback verification, v2.8.0):
    tenant-verify     列印 tenant 的 effective config + merged_hash；
                      `--expect-merged-hash` 與快照比對 (rollback checklist)
                      `--all --json` 拍 pre-base snapshot 給 rollback 後 diff

命令 (Batch PR — Migration Batch PR Pipeline, v2.8.0):
    batch-pr          開出/更新 tenant chunk PRs，或於 Base merge 後 rebase。
                      子命令: apply / refresh / refresh-source

命令 (Parser — PromRule parser, v2.8.0):
    parser            解析 PrometheusRule YAML，輸出 JSON ParseResult；
                      可選 strict-PromQL 相容性檢查 (anti-vendor-lock-in)。
                      子命令: import / allowlist

全域環境變數:
    PROMETHEUS_URL    預設 Prometheus 端點 (--prometheus 的後備)
    DA_LANG           設定 CLI 語言 (zh/en，優先於 LC_ALL/LANG)"""
    else:
        return """da-tools — Dynamic Alerting CLI Toolkit

Unified entrypoint for portable verification and migration tools.
Designed for Platform Engineers and SREs to validate integrations
without cloning the full repository.

Usage:
    da-tools <command> [options]

Commands (Prometheus API — portable):
    check-alert       Query alert firing status for a tenant
    diagnose          Tenant health check (config + metric + alert status)
    batch-diagnose    Multi-tenant parallel health report (post-cutover)
    baseline          Observe metrics and recommend thresholds
    validate          Compare old vs new recording rules (Shadow Monitoring)
    backtest          Backtest threshold changes against historical data
    cutover           One-command Shadow Monitoring cutover (§7.1 all steps)
    blind-spot        Scan cluster targets and find unmonitored instances
    alert-quality     Alert quality scoring (noise/stale/latency)
    alert-correlate   Alert correlation analysis (time-window clustering + root cause)

Commands (Config Generation — reads tenant YAML):
    generate-routes   Tenant YAML → route/receiver/inhibit merged into Alertmanager config
    patch-config      Patch threshold-config ConfigMap (with --diff preview)
    analyze-gaps      Rule Pack gap analysis for custom rules

Commands (File System — offline):
    migrate           Convert legacy Prometheus rules to dynamic format
    scaffold          Generate tenant configuration interactively
    offboard          Pre-check and remove a tenant configuration
    deprecate         Deprecate metrics: delete their keys from the config
    lint              Validate custom rules against governance deny-list
    onboard           Analyze existing Alertmanager/Prometheus configs for migration
    validate-config   One-stop config validation (YAML + schema + routing + policy)
    config-diff       Directory-level config diff for GitOps PR review
    drift-detect      Cross-cluster config drift detection (directory-level SHA-256)
    evaluate-policy   Policy-as-Code evaluation (declarative DSL)
    opa-evaluate      OPA Rego policy evaluation bridge (OPA integration)
    cardinality-forecast  Cardinality forecasting (linear regression trend)
    test-notification Multi-channel notification connectivity testing
    threshold-recommend Threshold recommendation engine (historical P50/P95/P99)
    threshold-govern  Threshold governance report — recommend→gate→list rotten thresholds (read-only; #656)
    explain-route     Routing merge pipeline debugger (four-layer expansion + profile)
    byo-check         BYO Alertmanager pre-integration check (endpoint + config)
    federation-check  Prometheus Federation health check
    grafana-import    Grafana Dashboard JSON import
    shadow-verify     Shadow Monitoring dual-rail comparison
    discover-mappings Auto-discover 1:N instance-tenant mappings (scrape exporter /metrics)
    init              Bootstrap Dynamic Alerting integration in your repo (CI/CD + conf.d + Kustomize)
    config-history    Config snapshot & history tracker (snapshot / log / diff / show)
    gitops-check      Local conf.d structure check (local)
    state-reconcile   Migration state directory declarative reconciliation (.da/state/ schema + .da/manifest.json rebuild)
    rule-pack-diff    Mechanical diff between two Rule Pack versions (added / removed / breaking label schema)
    silencer-drift-check  AM silence drift audit against v2 rule pack (offline, eats amtool silence query -o json)
    explain-keys      Per key: source, readers, verdict, what a disable does; --assert checks it (ADR-038 decision 9)
    generator-service Generator container service: judge a conf.d plus writes with generate-routes --validate --strict over a Unix socket (ADR-036 §3)

Commands (Operator-Native — CRD generation & validation):
    operator-generate Generate PrometheusRule / ServiceMonitor CRD YAML (Alertmanager routing: generate-routes → configSecret)
    operator-check    Validate Operator CRD deployment status (5 checks + diagnostic report)
    runtime-audit     Read-only Git rule-packs ↔ Prometheus runtime reconciliation (#747; MISSING/UNHEALTHY/ORPHAN)
    migrate-to-operator Migrate ConfigMap format to Operator native CRD (migration checklist + pre-check)
    migrate-credentials Find receiver credentials written out in conf.d: rewrite as Secret references, Secret manifests, rotation list (#1560, report only)

Commands (Federation — multi-cluster):
    rule-pack-split   Rule Pack stratification (edge Part 1 + central Parts 2+3)
    fed-key           Generate / rotate the federation JWT signing keypair (ADR-020 IV-2l)

Commands (Guard — Dangling Defaults Guard, v2.8.0):
    guard             Validate a conf.d/ tree (schema + cardinality; routing: generate-routes)
                      Subcommands: defaults-impact

Commands (Tenant Verify — rollback verification, v2.8.0):
    tenant-verify     Print tenant effective config + merged_hash;
                      `--expect-merged-hash` compares against a snapshot
                      (rollback checklist item 6).
                      `--all --json` snapshots pre-base state for diffing
                      after rollback.

Commands (Batch PR — Migration Batch PR Pipeline, v2.8.0):
    batch-pr          Open/update tenant chunk PRs, or rebase after Base merges.
                      Subcommands: apply / refresh / refresh-source

Commands (Parser — PromRule parser, v2.8.0):
    parser            Parse PrometheusRule YAML to JSON ParseResult;
                      optional strict-PromQL portability check
                      (anti-vendor-lock-in). Subcommands: import / allowlist

Global environment variables:
    PROMETHEUS_URL    Default Prometheus endpoint (fallback for --prometheus)
    DA_LANG           Set CLI language (zh/en, takes precedence over LC_ALL/LANG)"""


def detect_cli_lang():
    """Detect CLI language from LANG/LC_ALL/DA_LANG environment variable.

    Returns 'zh' or 'en'. Default: 'en'.
    """
    for var in ('DA_LANG', 'LC_ALL', 'LANG'):
        val = os.environ.get(var, '')
        if val.startswith('zh'):
            return 'zh'
        if val.startswith('en'):
            return 'en'
    return 'en'


# realpath, not abspath: the image puts a /usr/local/bin/da-tools SYMLINK to
# this file on PATH, and every GitLab job `da-tools init --ci gitlab` emits
# clears the image ENTRYPOINT and calls `da-tools <cmd>` through it. abspath
# keeps the link's directory, where no tool lives, so every such job exited 2
# (issue 1444; measured on a real runner).
TOOLS_DIR = os.path.dirname(os.path.realpath(__file__))
_LANG = detect_cli_lang()

# Exit code for "the dispatcher itself could not do its job": unknown
# subcommand, or COMMAND_MAP points at a script the image does not ship.
# Mirrors scripts/tools/_lib_exitcodes.EXIT_CALLER_ERROR (the 0/1/2 SSOT)
# as a literal on purpose: entrypoint.py is contractually zero-import from
# _lib_* (see _configure_std_utf8) — a raw local-dev run has only this dir
# on sys.path, so importing the SSOT would break the escape hatch itself.
# Never 1: that is EXIT_VIOLATION ("tool ran and found something"), and a
# mistyped subcommand / an image whose tag moved past the subcommand must
# not be indistinguishable from a finding with an empty report (#1406).
EXIT_CALLER_ERROR = 2


def _t(zh, en):
    """Pick the language variant of a message based on the detected CLI lang.

    Reads the module-level _LANG at call time (not bound as a default arg),
    so tests can monkeypatch entrypoint._LANG to exercise the zh path. Both
    arguments are evaluated eagerly by the caller — only pass side-effect-free
    strings (today every call site interpolates a plain command/script name).
    """
    return zh if _LANG == 'zh' else en

# Local-dev fallback search paths (relative to repo root).
# Docker image (build.sh) assembles all scripts flat into TOOLS_DIR,
# so the first lookup hits. Local dev (running entrypoint.py from
# components/da-tools/app/ in the checkout) needs to find scripts at
# their canonical source locations.
_LOCAL_SOURCE_DIRS = (
    "scripts/tools/ops",
    "scripts/tools/dx",
    "scripts/tools",
)


# Where this file sits in a checkout. Its root is the directory holding it.
_APP_PARTS = ("components", "da-tools", "app")


def _find_repo_root(start_dir):
    """The root of the tree this file sits in, by structure (#2852).

    In a checkout `start_dir` is `<root>/components/da-tools/app`, so the root
    is three levels up — in a clone, a worktree, a `git archive` tarball and a
    vendored copy alike, `.git` or not. Anywhere else (the image's flat
    `/opt/da-tools`) there is no repo tree: returns None.

    ⛔ Not "the nearest ancestor with a `.git`": a copy with none of its own,
    nested in another project, then ran THAT project's
    `scripts/tools/ops/<tool>.py`. Same intent as
    `_lib_compat.find_project_root` (this module imports no `_lib_*`), but
    keyed on this file's own position: that one looks for the innermost
    enclosing `scripts/tools`, which this file is never under.
    """
    cur = os.path.abspath(start_dir)
    parts = []
    for _ in _APP_PARTS:
        cur, name = os.path.split(cur)
        parts.append(name)
    if tuple(reversed(parts)) != _APP_PARTS:
        return None
    return cur


def _resolve_script_path(script_name):
    """Resolve `script_name` to an existing file path, with local-dev fallback.

    Docker image path: TOOLS_DIR/script_name (assembled flat by build.sh).
    Local dev fallback: search _LOCAL_SOURCE_DIRS under the repo root, in
    the declared order. If the same basename exists in multiple source
    dirs (unlikely by project convention but not impossible), the
    first match wins — `ops/` is the most populous canonical dir, so
    it's first, then `dx/`, then `scripts/tools/` root.

    Defensively basenames `script_name` before joining so a hypothetical
    future caller passing a path-with-separator value can't escape the
    intended search roots. Today all values come from the hardcoded
    `COMMAND_MAP` so script_name is always a bare filename, but the
    guard keeps the helper safe for re-use.

    Returns (resolved_path or None, searched_paths list).
    """
    script_name = os.path.basename(script_name)
    searched = []
    primary = os.path.join(TOOLS_DIR, script_name)
    searched.append(primary)
    if os.path.isfile(primary):
        return primary, searched

    repo_root = _find_repo_root(TOOLS_DIR)
    if repo_root is None:
        return None, searched

    for subdir in _LOCAL_SOURCE_DIRS:
        # _LOCAL_SOURCE_DIRS holds POSIX-separated literals ("scripts/tools/ops").
        # os.path.join keeps the embedded "/" verbatim, so on Windows the result
        # is a mixed-separator path (...\repo\scripts/tools/ops\script.py). It
        # still resolves (Windows accepts "/"), but leaks an un-normalized path
        # into the return value and the "Searched paths" diagnostic. normpath
        # collapses to the native separator here and is a no-op on POSIX.
        candidate = os.path.normpath(os.path.join(repo_root, subdir, script_name))
        searched.append(candidate)
        if os.path.isfile(candidate):
            return candidate, searched

    return None, searched

# Map subcommand names to script filenames
COMMAND_MAP = {
    # Group A: Prometheus API only (portable)
    "check-alert": "check_alert.py",
    "baseline": "baseline_discovery.py",
    "diagnose": "diagnose.py",
    "validate": "validate_migration.py",
    "batch-diagnose": "batch_diagnose.py",
    "backtest": "backtest_threshold.py",
    "cutover": "cutover_tenant.py",
    "blind-spot": "blind_spot_discovery.py",
    "alert-quality": "alert_quality.py",
    "alert-correlate": "alert_correlate.py",
    # Group B: Prometheus + file system (config generation)
    "generate-routes": "generate_alertmanager_routes.py",
    "drift-detect": "drift_detect.py",
    # Group C: File system only (offline)
    "migrate": "migrate_rule.py",
    "scaffold": "scaffold_tenant.py",
    "offboard": "offboard_tenant.py",
    "deprecate": "deprecate_rule.py",
    "lint": "lint_custom_rules.py",
    "onboard": "onboard_platform.py",
    "validate-config": "validate_config.py",
    "analyze-gaps": "analyze_rule_pack_gaps.py",
    "patch-config": "patch_config.py",
    "config-diff": "config_diff.py",
    "evaluate-policy": "policy_engine.py",
    "cardinality-forecast": "cardinality_forecasting.py",
    "test-notification": "notification_tester.py",
    "threshold-recommend": "threshold_recommend.py",
    # #656 — governance gate over threshold-recommend: gate on rot magnitude +
    # confidence and report the rotten thresholds. Read-only: the write path
    # (--apply, PRs via tenant-api) was removed by ADR-027 §4.
    "threshold-govern": "threshold_govern.py",
    "explain-route": "explain_route.py",
    "byo-check": "byo_check.py",
    "federation-check": "federation_check.py",
    "grafana-import": "grafana_import.py",
    "shadow-verify": "shadow_verify.py",
    "discover-mappings": "discover_instance_mappings.py",
    "init": "init_project.py",
    "config-history": "config_history.py",
    "gitops-check": "gitops_check.py",
    # v2.8.0 — Migration State reconciliation (issue #405 Category A)
    # Replaces manual jq workflow for schema_version drift + manifest drift.
    "state-reconcile": "state_reconcile.py",
    # v2.8.0 — Rule Pack version diff (issue #405 Category D)
    # Mechanical comparison of two Rule Pack YAML versions for upgrade audits.
    "rule-pack-diff": "rule_pack_diff.py",
    # v2.8.0 — Alertmanager silence drift auditor (issue #405 Category B)
    # Offline check: silences JSON dump + rule pack source → list orphan silences.
    "silencer-drift-check": "silencer_drift_check.py",
    # #3016 — ADR-038 decision 9: per tenant and key, where it comes from,
    # who reads it, the shared verdict and what a `disable` does; --assert.
    "explain-keys": "explain_keys.py",
    # #2901 — ADR-036 §3 generator container: a long-running service that
    # judges conf.d writes for tenant-api over a Unix socket.
    "generator-service": "generator_service.py",
    # Group D: Operator-native (CRD generation + validation)
    "operator-generate": "operator_generate.py",
    "operator-check": "operator_check.py",
    # #747 — read-only Git rule-packs ↔ Prometheus runtime reconciliation.
    # Detect-only (the runtime leg #711/#714 PR-gates don't cover); rejects
    # self-healing / a standing controller. See docs/custom-rule-governance.md §7.1.
    "runtime-audit": "runtime_audit.py",
    "migrate-to-operator": "migrate_to_operator.py",
    # #1560 ⑤-a — receiver credentials written out in conf.d → Secret
    # references + Secret manifests + rotation list (report only).
    "migrate-credentials": "migrate_credentials.py",
    # Group E: Federation (multi-cluster)
    "rule-pack-split": "generate_rule_pack_split.py",
    # Tenant federation (ADR-020 IV-2l) — RS256 signing-key generation /
    # rotation; see scripts/tools/ops/federation_keygen.py.
    "fed-key": "federation_keygen.py",
    # Group F: Policy (OPA/Rego)
    "opa-evaluate": "policy_opa_bridge.py",
    # Group G: Guard (v2.8.0 Phase .c C-12 — Dangling Defaults Guard)
    # Wraps the da-guard Go binary; see scripts/tools/ops/guard_dispatch.py.
    "guard": "guard_dispatch.py",
    # Group H: Tenant verify (v2.8.0 Phase .b Track A A5)
    # B-4 Emergency Rollback Procedures verification primitive.
    # See docs/scenarios/incremental-migration-playbook.md §Emergency
    # Rollback Procedures, checklist item 6.
    "tenant-verify": "tenant_verify.py",
    # Group I: Batch PR pipeline (v2.8.0 Phase .c C-10 PR-5)
    # Wraps the da-batchpr Go binary; see scripts/tools/ops/batchpr_dispatch.py.
    "batch-pr": "batchpr_dispatch.py",
    # Group J: PromRule parser (v2.8.0 Phase .c C-8 PR-2)
    # Wraps the da-parser Go binary; see scripts/tools/ops/parser_dispatch.py.
    "parser": "parser_dispatch.py",
}

# $PROMETHEUS_URL is the tool's business, not the dispatcher's (#3528): a
# tool whose `--prometheus` may be left out declares it with
# `_lib_io.add_prometheus_arg` (the action carries `env_fallback =
# "PROMETHEUS_URL"`) and reads the env through `_lib_io.prometheus_url` where
# it queries, judging it there. The dispatcher appends nothing: it used to
# append `--prometheus $PROMETHEUS_URL`, which made argparse refuse an
# unusable env on every run — paths that never query Prometheus included.


# Usage examples shown after the help text. These command lines are
# language-agnostic (no translatable prose), so they live in a single
# source of truth rather than being duplicated per language — the old
# zh/en branches held byte-identical copies, an unguarded drift risk.
# NOTE: keep this a tuple of plain strings (not a triple-quoted block):
# check_cli_coverage.py regex-parses triple-quoted blocks in this file
# for command coverage, and a """...""" here would put these examples in
# its scope. The 2-space indent is re-added at print time.
_USAGE_EXAMPLES = (
    "da-tools check-alert MariaDBHighConnections db-a --prometheus http://prometheus:9090",
    "da-tools baseline --tenant db-a --prometheus http://prometheus:9090",
    "da-tools validate --mapping prefix-mapping.yaml --prometheus http://prometheus:9090",
    "da-tools migrate legacy-rules.yml --dry-run --triage",
    "da-tools scaffold --tenant db-c --db mariadb,redis --non-interactive",
    "da-tools lint /path/to/custom-rules/ --ci",
    "da-tools onboard --alertmanager-config alertmanager.yaml --tenant-label organization",
    "da-tools validate-config --config-dir conf.d/",
    "da-tools init --ci both --tenants db-a,db-b --rule-packs mariadb,redis",
)

# Per-language labels/descriptions for the usage footer. Values are
# tuples (examples_label, env_label, prometheus_url_line, da_lang_line).
_USAGE_LABELS = {
    "zh": ("範例:", "環境變數:",
           "  PROMETHEUS_URL   Prometheus 預設端點 (未指定 --prometheus 時使用)",
           "  DA_LANG          設定 CLI 語言 (zh/en)"),
    "en": ("Examples:", "Environment:",
           "  PROMETHEUS_URL   Default Prometheus endpoint (used when --prometheus is omitted)",
           "  DA_LANG          Set CLI language (zh/en)"),
}


def print_usage():
    """Print help message in detected language."""
    try:
        print(_build_help_text(_LANG))
        print()
        examples_label, env_label, env_prom, env_lang = \
            _USAGE_LABELS["zh"] if _LANG == "zh" else _USAGE_LABELS["en"]
        print(examples_label)
        for example in _USAGE_EXAMPLES:
            print(f"  {example}")
        print()
        print(env_label)
        print(env_prom)
        print(env_lang)
    except OSError as exc:
        _exit_stdout_failed(exc)
    _exit_flushed(0)


def _exit_stdout_failed(exc):
    """stdout failed (EPIPE, ENOSPC, `>&-`'s EBADF): one stderr line and
    EXIT_CALLER_ERROR, never 0 — the exit _lib_exitcodes gives a tool
    (#3124, #3558). stdout's fd goes to the null device first, so what it
    still buffers cannot fail the interpreter's own last flush (rc 120)."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, sys.stdout.fileno())
        os.close(null)
    except Exception:  # noqa: BLE001 — best effort
        pass
    try:
        print(f"ERROR: cannot write to stdout: {exc.strerror or exc}",
              file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — the exit code still says it
        pass
    sys.exit(EXIT_CALLER_ERROR)


def _exit_flushed(code):
    """Exit with *code* once stdout took all of it (gnulib close_stdout)."""
    try:
        sys.stdout.flush()
    except OSError as exc:
        _exit_stdout_failed(exc)
    sys.exit(code)


def run_tool(script_name, args):
    """Load and execute a tool script by rewriting sys.argv."""
    script_path, searched = _resolve_script_path(script_name)

    if script_path is None:
        print(_t(f"錯誤: 找不到工具腳本 {script_name}",
                 f"Error: Tool script not found: {script_name}"), file=sys.stderr)
        print(_t("已搜尋以下路徑：", "Searched paths:"), file=sys.stderr)
        for path in searched:
            print(f"  {path}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Rewrite sys.argv so argparse in each tool sees correct arguments
    sys.argv = [script_name] + args

    # Load and execute the script as __main__
    spec = importlib.util.spec_from_file_location("__main__", script_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — last line of defence (#3090)
        # SystemExit / KeyboardInterrupt are not Exception: a tool's own
        # sys.exit(N) still passes through unchanged (#1406), and Ctrl-C
        # keeps the interpreter's handling.
        _exit_on_tool_crash(exc)


# Mirrors scripts/tools/_lib_exitcodes.crash_message (zero-import, see
# EXIT_CALLER_ERROR above); tests/shared/test_entrypoint.py pins the two.
_CRASH_MESSAGE_MAX = 500


def _crash_message(exc):
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — a broken __str__ must not hide the crash
        text = "(unprintable message)"
    text = " ".join(text.split()) or "(no message)"
    if len(text) > _CRASH_MESSAGE_MAX:
        text = text[:_CRASH_MESSAGE_MAX] + "..."
    return f"ERROR: unexpected internal error ({type(exc).__name__}): {text}"


# Mirrors scripts/tools/_lib_exitcodes.crash_exit_code_from_env (#3092; zero-
# import, as above): test suites set it so a crash cannot pass for a named
# rc 2. tests/shared/test_entrypoint.py pins the two together.
TEST_CRASH_EXIT_CODE_ENV = "DA_TEST_CRASH_EXIT_CODE"


def _crash_exit_code_from_env():
    raw = os.environ.get(TEST_CRASH_EXIT_CODE_ENV)
    if raw is None or not (raw.isascii() and raw.isdigit()):
        return None
    code = int(raw)
    return code if 3 <= code <= 255 else None


def _exit_on_tool_crash(exc):
    """A tool raised an uncaught Exception: report it and exit, never rc 1.

    The tool's crash code wins: if it loaded _lib_exitcodes (looked up in
    sys.modules, not imported — this module stays zero-import), that
    module's exit_on_crash reports and exits with whatever the tool set via
    set_crash_exit_code (none does since #3393). Otherwise print the same
    traceback + ERROR line here and exit EXIT_CALLER_ERROR — or, under a
    test suite, the code DA_TEST_CRASH_EXIT_CODE names (#3092).
    """
    lib = sys.modules.get("_lib_exitcodes")
    handler = getattr(lib, "exit_on_crash", None)
    if callable(handler):
        handler(exc)
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    print(_crash_message(exc), file=sys.stderr, flush=True)
    override = _crash_exit_code_from_env()
    sys.exit(EXIT_CALLER_ERROR if override is None else override)


def _print_version(tools_dir=None):
    """Print the da-tools version (or a dev fallback) and exit.

    tools_dir defaults to the module-level TOOLS_DIR but is resolved in the
    body (not bound as a default arg) so tests can pass a temp dir to cover
    the missing-VERSION dev fallback without monkeypatching a frozen global.
    """
    tools_dir = tools_dir or TOOLS_DIR
    version_file = os.path.join(tools_dir, "VERSION")
    if os.path.isfile(version_file):
        with open(version_file, encoding="utf-8") as f:
            text = f"da-tools {f.read().strip()}"
    else:
        text = "da-tools (dev)"
    try:
        print(text)
    except OSError as exc:
        _exit_stdout_failed(exc)
    _exit_flushed(0)


# Mirrors scripts/tools/_lib_exitcodes.sanitize_std_fds byte for byte (zero-
# import, as above; #3558); tests/shared/test_entrypoint.py pins the two.
def sanitize_std_fds():
    """Give each closed fd 0, 1, 2 the null device, read-only (#3558).

    As git's ``sanitize_stdfds`` and Rust's std: checked in order, so the
    null device opens at the very fd it fills (``dup2`` if not). Read-only
    for all three: reading fd 0 gets EOF, writing fd 1 or 2 fails (EBADF)
    instead of vanishing or landing in a file a later ``open()`` was handed.
    A ``sys.stdin`` the interpreter left None (fd 0 was closed at start) is
    pointed at fd 0, so ``<&-`` reads as empty input, as ``</dev/null`` does
    (#3602). A ``sys.stdout`` left None (fd 1 was closed at start) is
    pointed at fd 1, so a write fails as any stdout failure does; a None
    ``sys.stderr`` at a writable null device, so a tool's stderr line is
    neither printed on stdout (``print(file=None)``) nor a failure of its own.
    Best effort: an fd that cannot be filled is left as it was.

    ⚠️ components/da-tools/app/entrypoint.py holds this same function, byte
    for byte (it may not import _lib_*): tests/shared/test_entrypoint.py.
    """
    import os
    import sys
    filled = set()
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
            continue
        except OSError:
            pass
        try:
            null = os.open(os.devnull, os.O_RDONLY)
            if null == fd:
                os.set_inheritable(fd, True)
            else:
                os.dup2(null, fd)
                os.close(null)
            filled.add(fd)
        except OSError:
            pass
    if sys.stdin is None and 0 in filled:
        sys.stdin = sys.__stdin__ = open(  # noqa: SIM115
            0, "r", encoding="utf-8", errors="surrogateescape", closefd=False)
    if sys.stdout is None and 1 in filled:
        sys.stdout = sys.__stdout__ = open(  # noqa: SIM115
            1, "w", encoding="utf-8", errors="backslashreplace", newline="\n",
            closefd=False)
    if sys.stderr is None:
        sys.stderr = sys.__stderr__ = open(  # noqa: SIM115
            os.devnull, "w", encoding="utf-8", errors="backslashreplace", newline="\n")


def _configure_std_utf8():
    """Best-effort: make stdout/stderr tolerate non-ASCII on legacy Windows
    consoles so output doesn't crash or garble.

    The help text carries non-ASCII even in English (em dash —, arrows ↔ →,
    section sign §) and the error messages are bilingual Chinese. On a cp950 /
    cp1252 console sys.stdout defaults to errors='strict', so printing any of
    these raises UnicodeEncodeError and kills the command (e.g. `da-tools
    --help`). stderr defaults to backslashreplace (non-fatal) but renders zh
    errors as unreadable \\uXXXX escapes off a CJK locale — so reconfigure
    both streams to UTF-8.

    Inline + stdlib-only on purpose: entrypoint.py is contractually
    zero-import from _lib_*, and a raw local-dev run (`py
    components/da-tools/app/entrypoint.py`) puts only the app dir on sys.path
    — scripts/tools/_lib_compat is NOT importable there. (Same shim shape as
    scripts/tools/lint/check_cli_coverage.py.) Idempotent; a no-op under
    pytest capture, whose stream wrappers lack .reconfigure.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def main():
    sanitize_std_fds()  # before anything writes or opens: `>&-` is then a failing write
    _configure_std_utf8()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_usage()

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "--version":
        _print_version()

    # Note: -h/--help/help is fully handled by the guard at the top of main()
    # (print_usage exits there), so no second help check is needed here.

    if command not in COMMAND_MAP:
        commands = ', '.join(sorted(COMMAND_MAP.keys()))
        print(_t(f"錯誤: 未知命令 '{command}'",
                 f"Error: Unknown command '{command}'"), file=sys.stderr)
        print(_t(f"可用命令: {commands}",
                 f"Available commands: {commands}"), file=sys.stderr)
        print(_t("執行 'da-tools --help' 以查看用法。",
                 "Run 'da-tools --help' for usage."), file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    run_tool(COMMAND_MAP[command], args)


if __name__ == "__main__":
    main()
