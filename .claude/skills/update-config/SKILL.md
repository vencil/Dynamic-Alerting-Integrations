# Skill: update-config

## Description
安全且局部地動態修改 `monitoring/threshold-config` ConfigMap。
腳本會先獲取現有配置，僅更新指定的 `tenant` 與 `metric`，避免覆蓋其他設定。
修改後 Exporter 會在 reload-interval (15s~30s) 內自動生效。

## Usage
當需要動態調整閾值、停用監控或為了 Scenario D 設定優先級時使用。

## Interface
```bash
python3 scripts/tools/patch_config.py <tenant> <metric_key> <value>
# 範例: python3 scripts/tools/patch_config.py db-a mysql_connections 5
# 範例: python3 scripts/tools/patch_config.py db-b _state_container_crashloop disable