#!/usr/bin/env python3
"""offboard_tenant.py — 安全的 Tenant 下架工具。

執行 Tenant 下架前的 Pre-check，確認無外部依賴後安全移除。

用法:
  # 預檢模式 (預設): 只檢查不刪除
  python3 offboard_tenant.py db-a

  # 執行下架
  python3 offboard_tenant.py db-a --execute

  # 指定 conf.d 目錄
  python3 offboard_tenant.py db-a --config-dir /path/to/conf.d --execute

  # 一併檢查 tenant-api state store 的引用（群組、views、federation 子集）
  kubectl -n tenant-api get configmap tenant-api-state -o yaml > state.yaml
  python3 offboard_tenant.py db-a --state state.yaml --execute

Pre-check 項目:
  1. 確認 tenant config 檔案存在
  2. 掃描所有其他 tenant 是否有引用此 tenant
  3. 列出此 tenant 的所有已設定指標
  4. 檢查是否有 custom_ 前綴的規則引用此 tenant
  5. tenant-api state store 的引用（--state）：群組成員、views 的 filters、
     federation 子集；AccountID 配發只列為資訊（永不回收，ADR-021）。
     工具不寫 state store，只印出要呼叫的 tenant-api API。沒給 --state 時
     標為「未判定」，--execute 需另加 --skip-state-check 才執行。
"""

import sys
import os
import re
import argparse
from pathlib import Path
import yaml

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION  # noqa: E402
from _lib_confd import (  # noqa: E402
    config_stem,
    has_yaml_extension,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import strict_load_exporter_keys  # noqa: E402  (#2123 duplicate key = YAML error; #2216 tenant id as text)
from _lib_io import exit_on_yaml_file_error  # noqa: E402  (#1654 unreadable YAML → rc 2, named)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import LEGACY_STATE_FILES  # noqa: E402  (#2883)
from _lib_tenant_state import (  # noqa: E402  (#2883 groups / views / subsets live in tenant-api's state store)
    StateInputError,
    load_state,
    subset_key,
)


def find_config_file(tenant, config_dir):
    """尋找 tenant 的設定檔案。"""
    # #1588: the carrier is found by comparing STEMS, not by pasting an
    # extension onto the tenant id. `alpha.YAML` exists on disk but
    # `base / "alpha.yaml"` does not, and the caller reads that miss as
    # "this tenant has no config" — measured: offboarding pre-check
    # reported ✅ 通過 for a tenant whose entire config it could not see.
    #
    # ⛔ The comparison is exact, not case-folded: `config_stem` preserves
    # the original case (`Upper.YAML` -> `Upper`), and folding here would
    # let `--tenant alpha` offboard a DIFFERENT tenant named `Alpha`.
    base = Path(config_dir)
    # ⚠️ FLAT, and this now says so. The exporter reads the tree
    # recursively, so a tenant whose carrier lives in a subdirectory is
    # invisible here — and this is the OFFBOARD pre-check, where
    # "no config found" reads as "safe to remove". `warn_nested`
    # prints at most once per directory and nothing at all on a flat
    # tree, so the common case is unchanged.
    warn_nested(base)
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return None
    # #1607: `is_file()` here is the THIRD axis, and this is the worst place
    # for it to be silent — a DIRECTORY named `<tenant>.yaml/` makes this
    # return None and the pre-check print "❌ 找不到設定檔案", which reads as
    # "nothing to remove". ⛔ The naming is done ONCE in `load_all_configs`,
    # which always runs a few lines later in the same pre-check and does not
    # run twice; this function is called twice per invocation (pre-check and
    # execute), so reporting here would say everything twice.
    for entry in entries:
        if entry.is_file() and config_stem(entry.name) == tenant:
            return str(entry)
    return None


#: Keys whose list value is a list of tenant ids (#2216).
_TENANT_ID_LISTS = ("exclude_tenants", "tenants", "members")


def load_all_configs(config_dir, unreadable=None):
    """載入 conf.d 下所有設定檔案。

    unreadable: optional list; the name of every YAML file that could not be
    read (I/O error, YAML error incl. a duplicate key) is appended to it, so
    the caller can count it instead of only seeing the ⚠️ line (#2179).
    """
    configs = {}
    base = Path(config_dir)
    # #1911: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(base, tool="offboard_tenant")
    entries = sorted(base.iterdir())
    # #1607: the single naming point for what `is_file()` drops in this tool
    # — see `find_config_file`. Same channel as the unreadable-file warning
    # below, because it is the same fact for the operator: this entry looks
    # like configuration and was NOT taken into account by the pre-check.
    for bad in unusable_config_entries(entries):
        print(f"  ⚠️  略過 {safe_label(bad.name)}: {unusable_reason(bad)}")
    yaml_paths = [
        p for p in entries
        if p.is_file() and has_yaml_extension(p.name)
    ]
    for entry in yaml_paths:
        filename = entry.name
        if filename.startswith('.'):
            continue
        if filename in LEGACY_STATE_FILES:
            # #2883: tenant-api keeps these records in its state store since
            # #2856; it imports this copy at startup only while the store has
            # no such record. check_state_references judges it against --state.
            print(f"  ⚠️  略過 {safe_label(filename)}: tenant-api 只在 state store 沒有這筆紀錄時"
                  "於開機匯入它（引用改由 --state 判定）")
            continue
        try:
            with open(entry, 'r', encoding='utf-8') as f:
                # Strict (#2123): a key written twice in one mapping is a
                # YAMLError, so the file is named ⚠️ unreadable like a syntax
                # error instead of reporting PyYAML's last value.
                # #2216: tenant ids are the keys' source text, as the
                # exporter reads them — `010:` is tenant "010", not 8.
                # The lists that name tenants by VALUE are read as text too
                # (`exclude_tenants:` in a policy, `tenants:` in a domain
                # policy, `members:` in `_groups.yaml`), so a reference
                # written `[010]` is found by check_cross_references.
                data = strict_load_exporter_keys(
                    f, raw_text_sequences=_TENANT_ID_LISTS) or {}
            configs[filename] = {"path": str(entry), "data": data}
        except (OSError, yaml.YAMLError) as e:
            print(f"  ⚠️  無法讀取 {safe_label(filename)}: {safe_label(e)}")
            if unreadable is not None:
                unreadable.append(filename)
    return configs


def check_cross_references(tenant, configs):
    """檢查其他設定檔中是否有引用此 tenant。"""
    references = []
    for filename, info in configs.items():
        if filename.startswith(f"{tenant}."):
            continue  # 跳過自己
        # 搜尋檔案內容中是否出現 tenant 名稱
        content = yaml.dump(info["data"], default_flow_style=False)
        if tenant in content:
            references.append(filename)
    return references


def get_tenant_metrics(tenant, configs):
    """取得 tenant 的所有已設定指標。"""
    for filename, info in configs.items():
        if filename.startswith(f"{tenant}."):
            tenants = info["data"].get("tenants", {})
            return tenants.get(tenant, {})
    return {}


def _legacy_refs(tenant, state, config_dir):
    """conf.d 殘留、而 state store 沒有對應紀錄的舊檔中對 tenant 的引用。

    tenant-api 每次開機在 state store 沒有該筆紀錄時，會從 conf.d 匯入
    `_groups.yaml`／`_views.yaml`／`_federation_policy.yaml` 與
    `_federation/<id>.yaml`（statemigrate.Import），所以這些引用在下次
    重啟後就會出現在 state store。
    """
    refs = []
    if config_dir is None:
        return refs
    base = Path(config_dir)
    for name in LEGACY_STATE_FILES:
        path = base / name
        if name == "_account_registry.yaml" or name in state.records or not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = strict_load_exporter_keys(f, raw_text_sequences=_TENANT_ID_LISTS) or {}
        if tenant in yaml.dump(data, default_flow_style=False):
            refs.append((f"conf.d 的 {safe_label(name)}（state store 沒有這筆紀錄，tenant-api 下次開機會匯入）",
                         f"刪除 {safe_label(str(path))}，或先讓 tenant-api 匯入後再以 API 清除"))
    if subset_key(tenant) not in state.records:
        for ext in (".yaml", ".yml"):
            path = base / "_federation" / f"{tenant}{ext}"
            if path.is_file():
                refs.append((f"conf.d 的 _federation/{safe_label(path.name)}（tenant-api 下次開機會匯入為子集紀錄）",
                             f"刪除 {safe_label(str(path))}"))
    return refs


def check_state_references(tenant, state, config_dir=None):
    """tenant-api state store 中對 tenant 的引用（#2883）。

    回傳 (references, info)：references 是 (說明, 要呼叫的 API) 的清單，
    info 是只供參考的行（AccountID 配發）。config_dir 給定時，另列 conf.d
    殘留舊檔中 tenant-api 下次開機會匯入的引用。
    """
    refs = _legacy_refs(tenant, state, config_dir)
    for gid, g in sorted(state.groups().items()):
        members = [str(m) for m in (g.get("members") or [])]
        if tenant in members:
            refs.append((f"群組 {safe_label(gid)} 的成員",
                         f"PUT /api/v1/groups/{safe_label(gid)}（members 移除 {safe_label(tenant)}）"))
    for vid, v in sorted(state.views().items()):
        filters = v.get("filters") or {}
        if not isinstance(filters, dict):
            continue
        keys = sorted(str(k) for k, val in filters.items() if str(val) == tenant)
        if keys:
            refs.append((f"view {safe_label(vid)} 的 filters（{safe_label(', '.join(keys))}）",
                         f"PUT 或 DELETE /api/v1/views/{safe_label(vid)}"))
    key = subset_key(tenant)
    if key in state.records:
        refs.append((f"federation 子集紀錄 {safe_label(key)}",
                     "tenant-api 沒有刪除子集的 API：照 docs/internal/tenant-offboarding-runbook.md 步驟 2 "
                     "以 kubectl patch 移除 ConfigMap 的這個 key"))
    info = []
    account = state.account_id(tenant)
    if account is not None:
        info.append(f"AccountID 配發 {safe_label(account)}：永不回收（ADR-021），不需要也不可以清")
    return refs, info


def run_precheck(tenant, config_dir, state=None):
    """執行完整 Pre-check，回傳 (can_proceed, report_lines)。

    state: _lib_tenant_state.TenantState，或 None（未提供 --state：state
    store 的引用標為未判定）。
    """
    report = []
    issues = []

    report.append(f"{'='*60}")
    report.append(f"🔍 Tenant 下架 Pre-check: {safe_label(tenant)}")
    report.append(f"{'='*60}\n")

    # 1. 檔案存在性
    config_file = find_config_file(tenant, config_dir)
    if config_file:
        report.append(f"✅ 設定檔案: {safe_label(config_file)}")
    else:
        report.append(f"❌ 找不到設定檔案: {safe_label(tenant)}.yaml")
        issues.append("設定檔案不存在")

    # 2. 載入所有 configs
    unreadable = []
    configs = load_all_configs(config_dir, unreadable)
    report.append(f"\n📂 掃描目錄: {safe_label(config_dir)} "
                  f"({len(configs)} 個檔案)\n")
    # #2179: a file that cannot be read is a blocker, not a footnote. The
    # cross-reference check and the verdict below cannot see into it, so
    # without this line it read as "✅ 無跨檔案引用" + "✅ Pre-check 通過" and
    # --execute deleted the tenant file.
    if unreadable:
        report.append("❌ 無法讀取的設定檔 (修好後重跑 Pre-check):")
        for name in unreadable:
            report.append(f"   → {safe_label(name)}")
        issues.append(f"無法讀取的設定檔: {safe_label(', '.join(unreadable))}")

    # 3. Cross-reference check
    refs = check_cross_references(tenant, configs)
    if refs:
        report.append(f"⚠️  發現跨檔案引用 (請手動確認):")
        for ref in refs:
            report.append(f"   → {safe_label(ref)}")
        issues.append(f"跨檔案引用: {safe_label(', '.join(refs))}")
    else:
        report.append(f"✅ 無跨檔案引用")

    # 3b. tenant-api state store 的引用（#2883）
    if state is None:
        report.append("⚠️  state store 引用：未判定（未提供 --state；群組、views、"
                      "federation 子集存在 tenant-api 的 state store，不在 conf.d）")
    else:
        state_refs, state_info = check_state_references(tenant, state, config_dir)
        if state_refs:
            report.append(f"⚠️  state store 引用 ({safe_label(state.source)})，"
                          "工具不寫 state store，請在下架後呼叫 tenant-api 清除:")
            for what, how in state_refs:
                report.append(f"   → {what}: {how}")
            issues.append(f"跨檔案引用 (state store): {len(state_refs)} 處")
        else:
            report.append(f"✅ 無 state store 引用 ({safe_label(state.source)})")
        for line in state_info:
            report.append(f"   ℹ️  {line}")

    # 4. 列出 tenant 的所有指標
    metrics = get_tenant_metrics(tenant, configs)
    if metrics:
        report.append(f"\n📊 此 tenant 的已設定指標 ({len(metrics)} 個):")
        for key, val in metrics.items():
            report.append(f"   • {safe_label(key)}: {safe_label(val)}")
    else:
        # ⛔ 不寫「全部使用平台預設值」（#1321）：`_defaults.yaml` 的
        # `optional_overrides:` 宣告層只有 key 名、沒有值，那些 key 沒被租戶設定
        # 就是**沒有值＝靜默**，不是「沿用平台預設」。下架 pre-check 的讀者正是要
        # 判斷「拿掉這個 tenant 會失去什麼」的人，講反了會低估。
        report.append("\n📊 此 tenant 未設定任何指標覆寫 "
                      "(有平台預設值的 key 沿用預設；平台只宣告、不主張值的 key 維持靜默)")

    # 5. 最終判定
    report.append(f"\n{'='*60}")
    can_proceed = all(i.startswith("跨檔案引用") for i in issues)

    if not issues:
        report.append("✅ Pre-check 通過！可安全下架。")
    elif can_proceed:
        report.append("⚠️  Pre-check 有警告，但可手動確認後繼續。")
    else:
        report.append("❌ Pre-check 失敗，無法下架。")
    report.append(f"{'='*60}")

    return can_proceed, report


def execute_offboard(tenant, config_dir):
    """執行下架: 刪除 tenant 設定檔案。"""
    config_file = find_config_file(tenant, config_dir)
    if not config_file:
        print(f"❌ 找不到 {safe_label(tenant)} 的設定檔案", file=sys.stderr)
        return False

    try:
        os.remove(config_file)
        print(f"🗑️  已刪除: {safe_label(config_file)}")
        print(f"\n📋 後續步驟:")
        print(f"  1. threshold-exporter 將在下次 reload (30s) 時自動清除 "
              f"{safe_label(tenant)} 的閾值")
        print(f"  2. Prometheus 下次 scrape 時，{safe_label(tenant)} 的向量將消失")
        print(f"  3. 所有相關 Alert 將自動解除")
        print(f"  4. 請記得一併清理 Alertmanager 中 "
              f"tenant={safe_label(tenant)} 的 routing 設定")
        return True
    except (ValueError, TypeError, IndexError) as e:
        print(f"❌ 刪除失敗: {safe_label(e)}", file=sys.stderr)
        return False


@exit_on_yaml_file_error  # #2883: an unreadable --state record → rc 2, named
def main():
    """CLI entry point: 安全的 Tenant 下架工具。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="安全的 Tenant 下架工具 — Pre-check + 安全移除"
    )
    parser.add_argument("tenant", help="要下架的 tenant 名稱 (例如 db-a)")
    parser.add_argument("--config-dir",
                        default="components/threshold-exporter/config/conf.d",
                        help="conf.d 目錄路徑")
    parser.add_argument("--execute", action="store_true",
                        help="實際執行下架 (預設只做 Pre-check)")
    parser.add_argument("--state",
                        help="tenant-api state store 的副本：state 目錄，或 "
                             "`kubectl get configmap tenant-api-state -o yaml|json` 存下的檔")
    parser.add_argument("--skip-state-check", action="store_true",
                        help="沒有 --state 時仍允許 --execute（state store 引用不檢查）")

    args = parser.parse_args()

    state = None
    if args.state:
        try:
            state = load_state(args.state)
        except StateInputError as e:
            print(f"ERROR: {safe_label(e)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    can_proceed, report = run_precheck(args.tenant, args.config_dir, state)

    for line in report:
        print(line)

    if args.execute:
        if can_proceed and state is None and not args.skip_state_check:
            print("\n❌ 未提供 --state，無法確認 tenant-api state store 的引用。"
                  "加 --state，或明確加 --skip-state-check 後再執行下架。")
            sys.exit(EXIT_CALLER_ERROR)
        if can_proceed:
            print(f"\n⚡ 正在執行下架...\n")
            execute_offboard(args.tenant, args.config_dir)
        else:
            print(f"\n❌ Pre-check 未通過，無法執行下架。")
            sys.exit(EXIT_VIOLATION)
    else:
        print(f"\n💡 這是 Pre-check 模式。要實際下架，請加 --execute 參數。")
        if not can_proceed:
            # #2179: a failed pre-check is a failed run, with or without
            # --execute (it used to print "❌ Pre-check 失敗" and exit 0).
            sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
