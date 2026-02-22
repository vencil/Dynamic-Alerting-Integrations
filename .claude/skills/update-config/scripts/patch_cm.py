#!/usr/bin/env python3
import subprocess
import yaml
import sys
import json

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error executing: {cmd}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()

def main(tenant, metric_key, value):
    # 1. 獲取現有 ConfigMap
    cm_json = run_cmd("kubectl get configmap threshold-config -n monitoring -o json")
    cm_data = json.loads(cm_json)
    
    config_yaml_str = cm_data.get("data", {}).get("config.yaml", "")
    if not config_yaml_str:
        print("Error: config.yaml not found in ConfigMap.", file=sys.stderr)
        sys.exit(1)

    # 2. 解析 YAML
    config = yaml.safe_load(config_yaml_str)
    
    if "tenants" not in config:
        config["tenants"] = {}
    if tenant not in config["tenants"]:
        config["tenants"][tenant] = {}

    # 3. 更新特定值 (支援三態: custom value / "default" 刪除 key / "disable")
    if str(value).lower() == "default":
        # 刪除 key → 恢復使用 defaults 區塊的值
        config["tenants"][tenant].pop(metric_key, None)
        # 若該 tenant 已無任何自訂值，移除整個 tenant 區塊
        if not config["tenants"][tenant]:
            del config["tenants"][tenant]
    else:
        config["tenants"][tenant][metric_key] = str(value)

    # 4. 轉換回 YAML
    updated_yaml_str = yaml.dump(config, sort_keys=False)

    # 5. 準備 Patch JSON
    patch_data = {
        "data": {
            "config.yaml": updated_yaml_str
        }
    }
    
    # 6. 執行 Patch
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp:
        json.dump(patch_data, temp)
        temp_path = temp.name

    print(f"Patching ConfigMap for {tenant}: {metric_key} = {value}...")
    run_cmd(f"kubectl patch configmap threshold-config -n monitoring --type merge --patch-file {temp_path}")
    os.remove(temp_path)
    print("Success! Exporter will reload within its interval.")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: patch_cm.py <tenant> <metric_key> <value>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])