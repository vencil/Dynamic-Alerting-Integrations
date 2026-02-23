# Skill: verify-alert

## Description
精確查詢 Prometheus 針對特定 Tenant 與 Alert Name 的即時警報狀態。
大幅減少使用 bash script 中的 `curl` 與 `grep` 所造成的誤判與 Token 浪費。

## Usage
當修改了 ConfigMap 或觸發了故障後，用來驗證 Alert 是否成功觸發或解除。
**注意**: 需確保 `localhost:9090` (Prometheus) port-forward 已開啟。

## Interface
```bash
python3 scripts/tools/check_alert.py <alert_name> <tenant>
# 範例: python3 check_alert.py MariaDBHighConnections db-a