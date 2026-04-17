---
title: "動手實驗：從零到生產告警"
tags: [scenario, hands-on, lab, adoption, tutorial]
audience: [platform-engineer, tenant]
version: v2.7.0
lang: zh
---

# 動手實驗：從零到生產告警

> **v2.7.0** | 預計時間：30–45 分鐘 | 前置需求：已安裝 Docker
>
> 相關文件：[GitOps CI/CD 整合指南](gitops-ci-integration.md) · [Tenant 生命週期](tenant-lifecycle.md) · [CLI 參考](../cli-reference.md)

## 實驗概覽

本實驗帶你走過 Dynamic Alerting 的完整旅程，使用 5 個真實場景 tenant。完成後你將掌握：

- 使用 `da-tools init` 快速建立完整監控配置目錄
- 為 MariaDB、Redis、Kafka、JVM、PostgreSQL、Oracle、DB2、Kubernetes 共 8 種規則包配置閾值
- 理解四層路由合併機制（ADR-007）
- 測試三態運營（Normal / Silent / Maintenance）
- 產生帶驗證的 Alertmanager 路由
- 分析配置變更的影響範圍（blast radius）

## 實驗環境

所有練習透過 Docker 使用 `da-tools` — 配置驗證和路由產生步驟不需要 Kubernetes 叢集。

```bash
# 拉取 da-tools image（一次性）
docker pull ghcr.io/vencil/da-tools:latest

# 建立工作目錄
mkdir -p ~/da-lab && cd ~/da-lab
```

## Exercise 1: Bootstrap with da-tools init

可使用 CI/CD 導入精靈或直接執行 `da-tools init`：

```bash
docker run --rm -it \
  -v $(pwd):/workspace -w /workspace \
  ghcr.io/vencil/da-tools:latest \
  init \
  --ci github \
  --deploy kustomize \
  --tenants prod-mariadb,prod-redis,prod-kafka,staging-pg,prod-oracle \
  --rule-packs mariadb,redis,kafka,jvm,postgresql,oracle,db2,kubernetes \
  --non-interactive
```

確認產生的結構：

```bash
find . -type f | sort
```

預期輸出（縮排省略）：

```
.da-init.yaml
.github/workflows/dynamic-alerting.yaml
.pre-commit-config.da.yaml
conf.d/_defaults.yaml
conf.d/prod-mariadb.yaml
conf.d/prod-redis.yaml
conf.d/prod-kafka.yaml
conf.d/staging-pg.yaml
conf.d/prod-oracle.yaml
kustomize/base/kustomization.yaml
```

## 練習 2：配置 Tenant 閾值

編輯每個 tenant 檔案設定真實閾值。

**prod-mariadb.yaml** — 電商資料庫：

```yaml
mysql_connections: "150"
mysql_connections_critical: "200"
mysql_cpu: "75"
container_cpu: "75"
container_memory: "80"

_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/T00/B00/xxx
  group_by: [alertname, severity]
  group_wait: "30s"
  repeat_interval: "4h"

_metadata:
  owner: ecommerce-team
  tier: production
```

**prod-redis.yaml** — 會話快取（使用 routing profile）：

```yaml
redis_memory_used_bytes: "3221225472"
redis_memory_used_bytes_critical: "4294967296"
redis_connected_clients: "3000"
container_cpu: "70"
container_memory: "80"

_routing_profile: team-sre-apac

_metadata:
  owner: sre-apac
  tier: production
```

**prod-kafka.yaml** — 事件管道（PagerDuty）：

```yaml
kafka_consumer_lag: "50000"
kafka_consumer_lag_critical: "200000"
kafka_broker_count: "3"
kafka_active_controllers: "1"
kafka_under_replicated_partitions: "0"
jvm_gc_pause: "0.8"
jvm_memory: "85"

_routing:
  receiver_type: pagerduty
  group_by: [alertname, topic]
  group_wait: "1m"
  repeat_interval: "12h"
```

**staging-pg.yaml** — 預備環境 + 維護窗口：

```yaml
pg_connections: "100"
pg_replication_lag: "60"

_state_maintenance:
  expires: "2026-03-20T06:00:00Z"

_silent_mode:
  expires: "2026-03-18T12:00:00Z"

_routing:
  receiver_type: email
  group_wait: "5m"
  repeat_interval: "24h"
```

**prod-oracle.yaml** — 金融資料庫 + domain policy：

```yaml
oracle_sessions_active: "100"
oracle_sessions_active_critical: "150"
oracle_tablespace_used_percent: "75"
oracle_tablespace_used_percent_critical: "85"

_routing_profile: domain-finance-tier1
_domain_policy: finance

_metadata:
  owner: finance-dba-team
  domain: finance
  compliance: SOX
```

## 練習 3：驗證所有配置

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  validate-config --config-dir /data/conf.d --ci
```

預期輸出：

```
[PASS] prod-mariadb: 5 keys, routing OK
[PASS] prod-redis:   5 keys, routing OK (profile: team-sre-apac)
[PASS] prod-kafka:   7 keys, routing OK
[PASS] staging-pg:   2 keys, routing OK, maintenance window active
[PASS] prod-oracle:  4 keys, routing OK (profile: domain-finance-tier1)

✅ All 5 tenants passed validation.
```

若出現警告，檢查 key 名稱和 timing guardrails。

**檢查點**：你能解釋為什麼 `group_wait: "2s"` 會驗證失敗嗎？（提示：guardrail 最小值是 5s）

## Exercise 4: Generate Alertmanager Routes

```bash
mkdir -p .output

docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/.output:/data/output \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d \
  -o /data/output/alertmanager-routes.yaml --validate
```

預期輸出摘要：

```
Generated routes for 5 tenants:
  prod-mariadb  → slack     (group_wait: 30s, repeat: 4h)
  prod-redis    → slack     (profile: team-sre-apac)
  prod-kafka    → pagerduty (group_wait: 1m, repeat: 12h)
  staging-pg    → email     (group_wait: 5m, repeat: 24h)
  prod-oracle   → pagerduty (profile: domain-finance-tier1)
  + 5 inhibit rules (severity dedup)
Written: /data/output/alertmanager-routes.yaml
```

每個 tenant 都有獨立的路由區塊，包含 receiver、group_by、timing 參數和 severity dedup 的 inhibit rules。

**檢查點**：找到 `inhibit_rules` 區段。它如何防止 critical 和 warning 的重複通知？

## 練習 5：路由追蹤

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  explain-route --tenant prod-redis --config-dir /data/conf.d
```

顯示 prod-redis 的四層合併過程：
1. **平台預設** → webhook, 30s group_wait
2. **Routing profile** `team-sre-apac` → 覆蓋為 slack, 30s wait, 4h repeat
3. **Tenant _routing** → （未設定，使用 profile）
4. **Platform enforced** → NOC 副本

**檢查點**：prod-redis 最終 resolve 的 receiver_type 是什麼？哪一層設定的？

## 練習 6：影響範圍分析

模擬降低電商 MySQL 閾值：

```bash
cp -r conf.d conf.d.new
sed -i 's/mysql_connections: "150"/mysql_connections: "120"/' conf.d.new/prod-mariadb.yaml

docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/conf.d.new:/data/conf.d.new:ro \
  ghcr.io/vencil/da-tools:latest \
  config-diff --old-dir /data/conf.d --new-dir /data/conf.d.new
```

Diff 精確顯示哪個 tenant、哪些 metric 受影響 — 這就是 CI 中會作為 PR comment 貼出的內容。

## 練習 7：三態運營

檢查 `staging-pg.yaml`：

- **`_state_maintenance`**：告警仍然評估但路由到維護處理。`expires` 時間戳代表該狀態在到期後自動恢復。
- **`_silent_mode`**：告警完全抑制 — 不發送通知。同樣有 `expires` 安全機制。

試著移除 `_state_maintenance` 再跑驗證 — 你會看到 tenant 恢復正常路由。

## 練習 8：Domain Policy 測試

試著把 prod-oracle 的路由改成 Slack：

```yaml
_routing:
  receiver_type: slack
  webhook_url: https://hooks.slack.com/services/xxx
```

重跑驗證 — 應該會看到 domain policy 警告：`finance` domain 禁止使用 `slack`。

這就是 Policy-as-Code 的執行效果。

## 清理

```bash
cd ~ && rm -rf ~/da-lab
```

## 下一步

- **部署到真實叢集**：參照 [GitOps CI/CD 整合指南](gitops-ci-integration.md) 設定完整管線
- **探索互動工具**：在瀏覽器開啟 Self-Service Portal 做視覺化驗證
- **執行展演腳本**：`make demo-showcase` 自動跑完所有練習
- **深入了解**：閱讀 [架構與設計](../architecture-and-design.md) 文件了解完整平台概念

---

**文件版本：** v2.2.0 — 2026-03-17
**維護者：** Platform Engineering Team
