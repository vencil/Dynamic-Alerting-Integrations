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

Pre-check 項目:
  1. 確認 tenant config 檔案存在
  2. 掃描所有其他 tenant 是否有引用此 tenant
  3. 列出此 tenant 的所有已設定指標
  4. 檢查是否有 custom_ 前綴的規則引用此 tenant
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
from _lib_confd import config_stem, has_yaml_extension, warn_nested  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)


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
    for entry in entries:
        if entry.is_file() and config_stem(entry.name) == tenant:
            return str(entry)
    return None


def load_all_configs(config_dir):
    """載入 conf.d 下所有設定檔案。"""
    configs = {}
    base = Path(config_dir)
    # #1339: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(base, tool="offboard_tenant")
    yaml_paths = sorted(
        p for p in base.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    )
    for entry in yaml_paths:
        filename = entry.name
        if filename.startswith('.'):
            continue
        try:
            with open(entry, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            configs[filename] = {"path": str(entry), "data": data}
        except (OSError, yaml.YAMLError) as e:
            print(f"  ⚠️  無法讀取 {safe_label(filename)}: {safe_label(e)}")
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


def run_precheck(tenant, config_dir):
    """執行完整 Pre-check，回傳 (can_proceed, report_lines)。"""
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
    configs = load_all_configs(config_dir)
    report.append(f"\n📂 掃描目錄: {safe_label(config_dir)} "
                  f"({len(configs)} 個檔案)\n")

    # 3. Cross-reference check
    refs = check_cross_references(tenant, configs)
    if refs:
        report.append(f"⚠️  發現跨檔案引用 (請手動確認):")
        for ref in refs:
            report.append(f"   → {safe_label(ref)}")
        issues.append(f"跨檔案引用: {safe_label(', '.join(refs))}")
    else:
        report.append(f"✅ 無跨檔案引用")

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
    can_proceed = len(issues) == 0 or (len(issues) == 1 and "跨檔案引用" in issues[0])

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

    args = parser.parse_args()

    can_proceed, report = run_precheck(args.tenant, args.config_dir)

    for line in report:
        print(line)

    if args.execute:
        if can_proceed:
            print(f"\n⚡ 正在執行下架...\n")
            execute_offboard(args.tenant, args.config_dir)
        else:
            print(f"\n❌ Pre-check 未通過，無法執行下架。")
            sys.exit(EXIT_VIOLATION)
    else:
        print(f"\n💡 這是 Pre-check 模式。要實際下架，請加 --execute 參數。")


if __name__ == "__main__":
    main()
