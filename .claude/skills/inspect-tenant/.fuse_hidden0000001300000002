# Skill: inspect-tenant

## Purpose
全面檢查指定 tenant (db-a, db-b 等) 的健康狀態，包含：
- K8s Pod 狀態
- MariaDB 日誌
- Exporter 狀態
- Prometheus Metrics 可用性

## Usage
當使用者詢問類似問題時，自動執行此 skill：
- "db-a 怎麼了？"
- "檢查 db-b 的狀態"
- "為什麼 db-a 的 alert 在 firing？"
- "tenant X 是否正常？"

## Execution

```bash
# 1. 執行檢查腳本
.claude/skills/inspect-tenant/scripts/inspect.sh <tenant-name>

# 2. 解析輸出
# 腳本會返回 JSON 格式：
{
  "tenant": "db-a",
  "pod_status": "Running",
  "db_healthy": true,
  "exporter_healthy": true,
  "metrics": {
    "mysql_up": 1,
    "uptime": 3600,
    "threads_connected": 5
  },
  "recent_errors": []
}

# 3. 根據結果給出建議
- 如果 pod_status != Running → 檢查 kubectl describe pod
- 如果 db_healthy = false → 檢查 MariaDB logs
- 如果 exporter_healthy = false → 檢查 exporter logs
- 如果 recent_errors 非空 → 分析錯誤訊息
```

## Implementation

參見 `scripts/inspect.sh`

## Expected Output Format

成功案例：
```
✓ Tenant: db-a
✓ Pod Status: Running
✓ Database: Healthy
✓ Exporter: Up (mysql_up=1)
✓ Metrics: uptime=3600s, connections=5
✓ No recent errors
```

異常案例：
```
✗ Tenant: db-a
✗ Pod Status: CrashLoopBackOff
✗ Recent errors:
  - [ERROR] InnoDB: Cannot allocate memory
  - [ERROR] Plugin 'InnoDB' init function returned error
→ Recommendation: Check resource limits (memory/CPU)
```
