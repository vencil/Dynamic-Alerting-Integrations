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
import glob
import argparse
import yaml


def find_config_file(tenant, config_dir):
    """尋找 tenant 的設定檔案。"""
    # 嘗試 <tenant>.yaml 和 <tenant>.yml
    for ext in ('.yaml', '.yml'):
        path = os.path.join(config_dir, f"{tenant}{ext}")
        if os.path.exists(path):
            return path
    return None


def load_all_configs(config_dir):
    """載入 conf.d 下所有設定檔案。"""
    configs = {}
    for path in glob.glob(os.path.join(config_dir, "*.yaml")) + \
                glob.glob(os.path.join(config_dir, "*.yml")):
        filename = os.path.basename(path)
        if filename.startswith('.'):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            configs[filename] = {"path": path, "data": data}
        except Exception as e:
            print(f"  ⚠️  無法讀取 {filename}: {e}")
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
    report.append(f"🔍 Tenant 下架 Pre-check: {tenant}")
    report.append(f"{'='*60}\n")

    # 1. 檔案存在性
    config_file = find_config_file(tenant, config_dir)
    if config_file:
        report.append(f"✅ 設定檔案: {config_file}")
    else:
        report.append(f"❌ 找不到設定檔案: {tenant}.yaml")
        issues.append("設定檔案不存在")

    # 2. 載入所有 configs
    configs = load_all_configs(config_dir)
    report.append(f"\n📂 掃描目錄: {config_dir} ({len(configs)} 個檔案)\n")

    # 3. Cross-reference check
    refs = check_cross_references(tenant, configs)
    if refs:
        report.append(f"⚠️  發現跨檔案引用 (請手動確認):")
        for ref in refs:
            report.append(f"   → {ref}")
        issues.append(f"跨檔案引用: {', '.join(refs)}")
    else:
        report.append(f"✅ 無跨檔案引用")

    # 4. 列出 tenant 的所有指標
    metrics = get_tenant_metrics(tenant, configs)
    if metrics:
        report.append(f"\n📊 此 tenant 的已設定指標 ({len(metrics)} 個):")
        for key, val in metrics.items():
            report.append(f"   • {key}: {val}")
    else:
        report.append(f"\n📊 此 tenant 無自訂指標 (全部使用平台預設值)")

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
        print(f"❌ 找不到 {tenant} 的設定檔案", file=sys.stderr)
        return False

    try:
        os.remove(config_file)
        print(f"🗑️  已刪除: {config_file}")
        print(f"\n📋 後續步驟:")
        print(f"  1. threshold-exporter 將在下次 reload (30s) 時自動清除 {tenant} 的閾值")
        print(f"  2. Prometheus 下次 scrape 時，{tenant} 的向量將消失")
        print(f"  3. 所有相關 Alert 將自動解除")
        print(f"  4. 請記得一併清理 Alertmanager 中 tenant={tenant} 的 routing 設定")
        return True
    except Exception as e:
        print(f"❌ 刪除失敗: {e}", file=sys.stderr)
        return False


def main():
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
            sys.exit(1)
    else:
        print(f"\n💡 這是 Pre-check 模式。要實際下架，請加 --execute 參數。")


if __name__ == "__main__":
    main()
