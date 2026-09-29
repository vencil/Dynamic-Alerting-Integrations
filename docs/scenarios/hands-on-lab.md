---
title: "動手實驗：從零到生產告警"
tags: [scenario, hands-on, lab, adoption, tutorial]
audience: [platform-engineer, tenant]
version: v2.9.0
lang: zh
---

# 動手實驗：從零到生產告警

> **Language / 語言：** **中文 (Current)** | [English](./hands-on-lab.en.md)

> **v2.9.0** | 預計時間：30–45 分鐘 | 前置需求：已安裝 Docker
>
> 相關文件：[GitOps CI/CD 整合指南](gitops-ci-integration.md) · [Tenant 生命週期](tenant-lifecycle.md) · [CLI 參考](../cli-reference.md)

> 💡 **想先 1 分鐘看產品跑起來、而不是動手敲 CLI？** → [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md)（推薦首站：瀏覽器看 da-portal UI + 真實告警紅燈，不需 K8s；`⏱️ <1 min · 🟢 只需 Docker`）。**本實驗**聚焦**動手跑 da-tools CLI 工作流**（配置 / 路由 / blast radius；`⏱️ 30–45 min · 🟡 中度 (CLI)`）—— 兩者互補、深度不同，不是擇一。

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
  --user $(id -u):$(id -g) \
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

預期輸出：

```
./.da-init.yaml
./.github/workflows/dynamic-alerting.yaml
./.pre-commit-config.da.yaml
./conf.d/_defaults.yaml
./conf.d/prod-kafka.yaml
./conf.d/prod-mariadb.yaml
./conf.d/prod-oracle.yaml
./conf.d/prod-redis.yaml
./conf.d/staging-pg.yaml
./kustomize/base/README.md
./kustomize/base/kustomization.yaml
./kustomize/overlays/dev/kustomization.yaml
./kustomize/overlays/prod/kustomization.yaml
```

打開任一個 `conf.d/<租戶名稱>.yaml`：除了開頭的註解，內容是 `tenants:` 底下一層 `<租戶名稱>:`，閾值與 `_routing` 都寫在第二層底下。練習 2 會沿用這個外框。

## 練習 2：配置 Tenant 閾值

把每個租戶檔換成下面的內容（開頭的註解可留可刪）。每一段都保留 init 產生的 `tenants:` → `<租戶名稱>:` 兩層外框：少了這兩層，驗證會找不到任何租戶，卻照樣顯示通過（見練習 3 的提醒）。

**conf.d/prod-mariadb.yaml** — 電商資料庫：

```yaml
tenants:
  prod-mariadb:
    mysql_connections: "150"
    mysql_connections_critical: "200"
    mysql_threads_running: "40"    # threads_running 飽和（併發執行緒數，NOT host CPU%）；平台預設 30
    container_cpu: "75"
    container_memory: "80"

    _routing:
      receiver:
        type: slack
        api_url: https://hooks.slack.com/services/T00/B00/xxx
      group_by: [alertname, severity]
      group_wait: "30s"
      repeat_interval: "4h"

    _metadata:
      owner: ecommerce-team
      tier: production
```

**conf.d/prod-redis.yaml** — 會話快取（使用 routing profile）：

```yaml
tenants:
  prod-redis:
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

**conf.d/prod-kafka.yaml** — 事件管道（PagerDuty）：

```yaml
tenants:
  prod-kafka:
    kafka_consumer_lag: "50000"
    kafka_consumer_lag_critical: "200000"
    kafka_broker_count: "3"
    kafka_active_controllers: "1"
    kafka_under_replicated_partitions: "0"
    jvm_gc_pause: "0.8"
    jvm_memory: "85"

    _routing:
      receiver:
        type: pagerduty
        service_key: "<your-pagerduty-service-key>"
      group_by: [alertname, topic]
      group_wait: "1m"
      repeat_interval: "12h"
```

**conf.d/staging-pg.yaml** — 預備環境 + 維護窗口：

```yaml
tenants:
  staging-pg:
    pg_connections: "100"
    pg_replication_lag: "60"

    _state_maintenance:
      expires: "2099-03-20T06:00:00Z"

    _silent_mode:
      target: warning        # 必填 —— 要靜音哪些嚴重度（warning | critical | all | disable）
      expires: "2099-03-18T12:00:00Z"

    _routing:
      receiver:
        type: email
        to: ["dba-oncall@example.com"]
        smarthost: "smtp.example.com:587"
        from: "alerting@example.com"
      group_wait: "5m"
      repeat_interval: "24h"
```

**conf.d/prod-oracle.yaml** — 金融資料庫（使用 routing profile，受 finance domain policy 約束）：

```yaml
tenants:
  prod-oracle:
    oracle_sessions_active: "100"
    oracle_sessions_active_critical: "150"
    oracle_tablespace_used_percent: "75"
    oracle_tablespace_used_percent_critical: "85"

    _routing_profile: domain-finance-tier1

    _metadata:
      owner: finance-dba-team
      domain: finance
      tags: [sox-compliant]
```

prod-redis 與 prod-oracle 引用的兩個 routing profile，以及約束 prod-oracle 的 domain policy，是平台層級的設定，各有專屬檔名，不寫在租戶檔裡。在 `conf.d/` 新增這兩個檔：

**conf.d/_routing_profiles.yaml** — 可被多個租戶引用的具名路由設定：

```yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: slack
      api_url: https://hooks.slack.com/services/T00/B00/sre-apac
    group_by: [tenant, alertname, severity]
    group_wait: "30s"
    repeat_interval: "4h"

  domain-finance-tier1:
    receiver:
      type: pagerduty
      service_key: "<your-finance-pagerduty-key>"
    group_by: [tenant, alertname, severity]
    group_wait: "30s"
    repeat_interval: "1h"
```

**conf.d/_domain_policy.yaml** — 業務領域的合規約束（練習 8 會用到）：

```yaml
domain_policies:
  finance:
    description: "金融資料庫的通知合規要求"
    tenants: [prod-oracle]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

domain policy 用 `tenants:` 清單指名受約束的租戶。只有檔名是 `_domain_policy.yaml` 時這個區塊才會被讀；在租戶檔裡寫 `_domain_policy: finance` 不會套用任何政策，驗證只會報 `unknown reserved key '_domain_policy'`。

## 練習 3：驗證所有配置

```bash
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  validate-config --config-dir /data/conf.d
```

預期輸出（照練習 1、2 做完之後的實測結果）：

```
============================================================
  validate-config — Unified Validation Report
============================================================

[PASS] yaml_syntax
       8 files parsed successfully

[PASS] yaml_quoting
       7 files checked: no unquoted value in a string field is read as a non-string

[PASS] schema
       No schema warnings

[PASS] routes
       5 routes, 5 receivers, 5 inhibit_rules
       amtool check-config: the generated config (assembled on the built-in default base, never on a --base-config) accepted by Alertmanager's parser (/usr/local/bin/amtool)

[PASS] profiles
       5 tenants scanned, 0 profile refs, 0 profiles defined

[PASS] policy_dsl
       No _policies defined — skipped

[PASS] tenant_uniqueness
       5 tenant(s), each declared in exactly one file

[PASS] root_defaults
       _defaults.yaml: 25 key(s) under `defaults:`, every value a number threshold-exporter decodes, no `_routing*` key

------------------------------------------------------------
  Total: 8 checks | 8 pass | 0 warn | 0 fail
------------------------------------------------------------
  Result: PASS
```

結束碼是 `0`：只有任一檢查項為 `fail` 時才會非零（`1`），WARN 不會讓它失敗，所以 CI 直接呼叫即可，不需要另加旗標。

- `schema` 這一列也檢查 `_routing_profile` 的引用與 domain policy：少了 `_routing_profiles.yaml`，這裡會出現 `_routing_profile references unknown profile`；少了 `_domain_policy.yaml`，finance 的約束就不存在，不會有任何提示。
- `profiles` 這一列管的是閾值 profile（`_profiles.yaml` 與租戶檔的 `_profile`），不是 routing profile，所以練習 2 做完它仍是 `0 profiles defined`。

⚠️ 如果 `tenant_uniqueness` 顯示 `0 tenant(s)`、`routes` 顯示 `0 routes`，代表你把練習 2 的片段貼成檔案時漏掉了 `tenants:` 與 `<租戶名稱>:` 兩層外框。這時每一項都是 PASS，但其實什麼都沒驗到。

**檢查點**：你能解釋為什麼 `group_wait: "2s"` 會在 `routes` 出現 WARN，而且實際生效的是 5s 嗎？（提示：guardrail 範圍是 5s–5m，低於下限的值會被夾到 5s，只警告、不失敗）

## Exercise 4: Generate Alertmanager Routes

```bash
mkdir -p .output

# 產出檔案：不要同時給 --validate——它在用到 -o 之前就結束，工具會直接拒絕（結束碼 2）
docker run --rm \
  --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  -v $(pwd)/.output:/data/output \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d \
  -o /data/output/alertmanager-routes.yaml

# 驗證另外跑一次（只讀、不寫檔）
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/da-tools:latest \
  generate-routes --config-dir /data/conf.d --validate
```

預期輸出（第一次執行）：

```
NOTICE: routing fragment mode; generated output was NOT validated by Alertmanager (a fragment is not a complete Alertmanager config — it has no root receiver, which amtool check-config rejects on its own). Use --output-configmap to validate the merged config when amtool is on PATH.
Config files: 8 read, 0 skipped
Found 5 tenant(s) with routing config: prod-kafka, prod-mariadb, prod-oracle, prod-redis, staging-pg
Found 5 tenant(s) for severity dedup: prod-kafka, prod-mariadb, prod-oracle, prod-redis, staging-pg
Written to /data/output/alertmanager-routes.yaml (5 routes, 5 receivers, 5 inhibit rules)
```

第二次執行（`--validate`）：

```
Config files: 8 read, 0 skipped
Found 5 tenant(s) with routing config: prod-kafka, prod-mariadb, prod-oracle, prod-redis, staging-pg
Found 5 tenant(s) for severity dedup: prod-kafka, prod-mariadb, prod-oracle, prod-redis, staging-pg
Validation: 5 route(s), 5 receiver(s), 5 inhibit rule(s)
OK: all configs valid
amtool check-config: the generated config (assembled on the built-in default base, never on a --base-config) accepted by Alertmanager's parser (/usr/local/bin/amtool)
```

`NOTICE` 與 `amtool check-config` 這兩行印在 stderr，其餘在 stdout。

每個租戶在產出檔 `.output/alertmanager-routes.yaml` 的 `route.routes` 底下各有一段（`matchers: tenant="…"`），`receivers` 與 `inhibit_rules` 各 5 筆。對照練習 2 的設定：

| 租戶 | receiver | group_wait | repeat_interval | 來自 |
|---|---|---|---|---|
| prod-kafka | pagerduty | 1m | 12h | 租戶 `_routing` |
| prod-mariadb | slack | 30s | 4h | 租戶 `_routing` |
| prod-oracle | pagerduty | 30s | 1h | profile `domain-finance-tier1` |
| prod-redis | slack | 30s | 4h | profile `team-sre-apac` |
| staging-pg | email | 5m | 24h | 租戶 `_routing` |

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
4. **Platform enforced**（`_defaults.yaml` 的 `_routing_enforced`）→ （未設定：init 產生的 `_defaults.yaml` 沒有這一層，輸出顯示 `(empty)`。平台團隊設定後，它會在所有租戶路由之前插入一條 `continue: true` 的平台路由，例如讓 NOC 一律收到副本）

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

`staging-pg.yaml` 同時設了兩種運營狀態，效果不一樣：

| 狀態 | 告警觸發 | 記錄進 TSDB | 送出通知 | 由誰擋下 |
|---|---|---|---|---|
| `_silent_mode`（本例 `target: warning`） | ✅ | ✅ | ❌（只擋 warning） | Alertmanager inhibit |
| `_state_maintenance` | ❌ | ❌ | ❌ | Prometheus（rule pack 的 `unless`） |

兩者都帶 `expires`，到期自動恢復正常。行為矩陣的完整說明見 [Config-Driven 設計 §2.7](../design/config-driven.md)。

這兩種狀態在告警執行期間才生效：`validate-config`、`explain-route`、`generate-routes` 的輸出都不會因為它們改變。要看到它們，讓 threshold-exporter 讀這個 `conf.d/`，看它輸出的旗標 metric：

```bash
docker run --rm -d --name da-lab-exporter \
  -p 8080:8080 \
  -v $(pwd)/conf.d:/data/conf.d:ro \
  ghcr.io/vencil/threshold-exporter:latest \
  --config-dir /data/conf.d

curl -s localhost:8080/metrics | grep -E '^user_(state_filter\{filter="maintenance"|silent_mode)'
```

預期輸出：

```
user_silent_mode{target_severity="warning",tenant="staging-pg"} 1
user_state_filter{filter="maintenance",severity="info",tenant="staging-pg"} 1
```

接著刪掉 `staging-pg.yaml` 裡整個 `_state_maintenance` 區塊（兩行），等約 30 秒（exporter 預設每 30 秒重新載入），再跑一次同一個 `curl`：`maintenance` 那一行消失，靜音那一行還在。rule pack 以 `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)` 讀這個 metric，所以 staging-pg 的告警會重新觸發；warning 的通知仍被靜音擋著，critical 則會送出。

做完後停掉 exporter：`docker stop da-lab-exporter`（`--rm` 會一併刪除容器）。

## 練習 8：Domain Policy 測試

試著把 prod-oracle 的路由改成 Slack：在 `conf.d/prod-oracle.yaml` 的 `prod-oracle:` 底下（和 `_metadata` 同一層）加上

```yaml
_routing:
  receiver:
    type: slack
    api_url: https://hooks.slack.com/services/xxx
```

重跑驗證 — 應該會看到 domain policy 警告：`finance` domain 禁止使用 `slack`。加上 `--strict` 則警告升級為 ERROR 並以非零 exit code 失敗（CI 即以 `--validate --strict` 把這類違規擋在 merge 之前）。

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

**文件版本：** v2.9.0
**維護者：** Platform Engineering Team
