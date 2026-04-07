#!/usr/bin/env python3
"""deprecate_rule.py — 規則/指標下架工具。

安全地將指定的 metric key 從平台中淘汰，三步自動化:
  Step 1: 在 _defaults.yaml 中設定該 metric 為 "disable"
  Step 2: 掃描所有 conf.d/*.yaml，移除殘留的 metric key
  Step 3: 產出下架報告 (含需手動處理的 ConfigMap 清理指引)

用法:
  # 預覽模式 (預設)
  python3 deprecate_rule.py mysql_slave_lag

  # 執行下架 (修改檔案)
  python3 deprecate_rule.py mysql_slave_lag --execute

  # 指定 conf.d 目錄
  python3 deprecate_rule.py mysql_slave_lag --config-dir /path/to/conf.d --execute

  # 同時處理多個 metric
  python3 deprecate_rule.py mysql_slave_lag mysql_innodb_buffer_pool --execute

注意:
  此工具處理 conf.d/ 層面的設定清理。Prometheus ConfigMap 中的
  Recording Rule / Alert Rule 需在下個 Release Cycle 手動移除。
"""

import sys
import os
import glob
import argparse
import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file as _lib_load_yaml  # noqa: E402
from _lib_python import write_text_secure  # noqa: E402


def load_yaml_file(path):
    """安全載入 YAML 檔案，with warning on parse error.

    Delegates to ``_lib_python.load_yaml_file`` for the common path.
    Catches ``yaml.YAMLError`` on corrupt files (which the lib version
    intentionally does not swallow) so the deprecation workflow can
    continue with a warning instead of aborting.
    """
    try:
        result = _lib_load_yaml(path, default={})
        if result is None:
            return {}
        return result
    except (OSError, yaml.YAMLError) as e:
        print(f"  ⚠️  無法讀取 {path}: {e}")
        return None


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案。"""
    content = ""
    if header_comment:
        content += header_comment
    content += yaml.safe_dump(data, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
    write_text_secure(path, content)


def scan_for_metric(metric_key, config_dir):
    """掃描 conf.d/ 中所有引用指定 metric 的檔案。

    回傳: list of {filename, path, section, occurrences}
    """
    findings = []
    pattern_keys = [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]

    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml")) +
                       glob.glob(os.path.join(config_dir, "*.yml"))):
        filename = os.path.basename(path)
        if filename.startswith('.'):
            continue

        data = load_yaml_file(path)
        if data is None:
            continue

        occurrences = []

        # Check defaults section
        defaults = data.get("defaults", {})
        for pk in pattern_keys:
            if pk in defaults:
                occurrences.append(("defaults", pk, defaults[pk]))

        # Check tenants section
        tenants = data.get("tenants", {})
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            for pk in pattern_keys:
                if pk in tenant_config:
                    occurrences.append((f"tenants.{tenant_name}", pk, tenant_config[pk]))
            # Also check dimensional keys like "metric{label="value"}"
            for key, val in tenant_config.items():
                if metric_key in key and key not in pattern_keys:
                    occurrences.append((f"tenants.{tenant_name}", key, val))

        if occurrences:
            findings.append({
                "filename": filename,
                "path": path,
                "occurrences": occurrences,
            })

    return findings


def disable_in_defaults(metric_key, config_dir, execute=False):
    """在 _defaults.yaml 中將 metric 設為 "disable"。"""
    defaults_path = os.path.join(config_dir, "_defaults.yaml")
    if not os.path.exists(defaults_path):
        return False, "_defaults.yaml 不存在"

    data = load_yaml_file(defaults_path)
    if data is None:
        return False, "無法讀取 _defaults.yaml"

    defaults = data.get("defaults", {})
    current_val = defaults.get(metric_key)

    if current_val == "disable":
        return True, f"已經是 disable 狀態"

    if execute:
        if "defaults" not in data:
            data["defaults"] = {}
        data["defaults"][metric_key] = "disable"

        # Read original file to preserve header comment
        header = ""
        with open(defaults_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#'):
                    header += line
                else:
                    break

        save_yaml_file(defaults_path, data, header)
        return True, f"已將 {metric_key} 設為 disable (原值: {current_val})"
    else:
        return True, f"將把 {metric_key} 從 {current_val} 改為 disable"


def remove_from_tenants(metric_key, config_dir, execute=False):
    """從所有 tenant 設定中移除殘留的 metric key。"""
    removed = []
    pattern_keys = [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]

    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml")) +
                       glob.glob(os.path.join(config_dir, "*.yml"))):
        filename = os.path.basename(path)
        if filename.startswith('_') or filename.startswith('.'):
            continue  # Skip _defaults.yaml

        data = load_yaml_file(path)
        if data is None:
            continue

        tenants = data.get("tenants", {})
        modified = False
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            keys_to_remove = []
            for key in tenant_config:
                if key in pattern_keys or (metric_key in key and '{' in key):
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                val = tenant_config[key]
                removed.append((filename, tenant_name, key, val))
                if execute:
                    del tenant_config[key]
                    modified = True

        if modified and execute:
            header = ""
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#'):
                        header += line
                    else:
                        break
            save_yaml_file(path, data, header)

    return removed


def main():
    """CLI entry point: 規則/指標下架工具。."""
    parser = argparse.ArgumentParser(
        description="規則/指標下架工具 — 三步安全淘汰 metric key"
    )
    parser.add_argument("metrics", nargs="+",
                        help="要下架的 metric key (例如 mysql_slave_lag)")
    parser.add_argument("--config-dir",
                        default="components/threshold-exporter/config/conf.d",
                        help="conf.d 目錄路徑")
    parser.add_argument("--execute", action="store_true",
                        help="實際執行下架 (預設只預覽)")

    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {args.config_dir}", file=sys.stderr)
        sys.exit(1)

    mode = "執行" if args.execute else "預覽"

    print(f"{'='*60}")
    print(f"🗑️  規則下架工具 — {mode}模式")
    print(f"{'='*60}\n")
    print(f"目標 Metrics: {', '.join(args.metrics)}")
    print(f"Config 目錄: {args.config_dir}\n")

    for metric in args.metrics:
        print(f"\n{'─'*40}")
        print(f"📌 Processing: {metric}")
        print(f"{'─'*40}\n")

        # Step 1: 掃描
        findings = scan_for_metric(metric, args.config_dir)
        if findings:
            print(f"  📂 發現 {sum(len(f['occurrences']) for f in findings)} 處引用:")
            for f in findings:
                for section, key, val in f["occurrences"]:
                    print(f"     • {f['filename']} → [{section}] {key}: {val}")
        else:
            print(f"  ✅ 未發現任何引用")

        # Step 2: 在 defaults 中設為 disable
        print(f"\n  Step 1: _defaults.yaml")
        ok, msg = disable_in_defaults(metric, args.config_dir, execute=args.execute)
        icon = "✅" if ok else "❌"
        print(f"  {icon} {msg}")

        # Step 3: 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir, execute=args.execute)
        if removed:
            for filename, tenant, key, val in removed:
                action = "已移除" if args.execute else "將移除"
                print(f"  🗑️  {action}: {filename} → {tenant}.{key} (值: {val})")
        else:
            print(f"  ✅ 無需清理 tenant configs")

        # Step 4: ConfigMap 指引
        print(f"\n  Step 3: Prometheus ConfigMap (手動)")
        print(f"  📋 下一個 Release Cycle 請手動移除:")
        print(f"     • Recording Rule: tenant:{metric}:* 或 tenant:custom_{metric}:*")
        print(f"     • Alert Rule: 引用上述 Recording Rule 的 Alert")
        print(f"     • Threshold Rule: tenant:alert_threshold:{metric}")

    # 總結
    print(f"\n{'='*60}")
    if args.execute:
        print("✅ 下架完成！threshold-exporter 將在下次 reload 時生效。")
        print("📋 請在下個 Release Cycle 清理 Prometheus ConfigMap 中的對應規則。")
    else:
        print("💡 這是預覽模式。要實際執行，請加 --execute 參數。")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
