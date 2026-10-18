---
title: "故障排查與邊界情況"
tags: [troubleshooting, operations]
audience: [platform-engineer, sre, tenant]
version: v2.9.0
lang: zh
---
# 故障排查與邊界情況

> **Language / 語言：** | **中文（當前）** | [English](./troubleshooting.en.md)

> 相關文件：[Architecture](architecture-and-design.md) · [HA Design](architecture-and-design.md#4-高可用性設計-high-availability)
>
> **遷移期間的 troubleshooting**（dual-write、shadow、cutover、decommission 等場景）→ 走 [Migration Troubleshooting Checklist](integration/troubleshooting-checklist.md)；本頁聚焦平台**穩態運行**的故障排查。

**依症狀 / 角色快速定位：**

| 症狀 | 適用角色 | 跳到 |
|------|---------|------|
| ConfigMap 改了但 exporter 還是舊值 | Platform / SRE | [SHA-256 熱重載延遲](#sha-256-熱重新加載延遲) |
| 我的告警該響卻沒響 | Tenant（診斷需 Platform / SRE 協助） | [空向量警報不觸發](#空向量警報不觸發) |
| 多副本 / 多租戶數值被重複計算 | Platform / SRE | [雙租戶抓取重複計數](#雙租戶抓取重複計數) |
| 平台自監控告警在 Prometheus 有 firing，卻沒人收到 | Platform / SRE | [平台告警沒有收到通知](#平台告警沒有收到通知) |
| Operator 環境規則沒載入 | Platform Engineer | [Operator 環境常見問題](#prometheus-operator-環境常見問題) |
| 經 tenant-api 的寫入都回 409 `TENANT_TREE_UNREADABLE` 或 500 `TENANT_STORE_FAULT` | Platform Engineer | [tenant-api 寫入全部被拒](#tenant-api-寫入全部被拒tenant_tree_unreadabletenant_store_fault) |
| 經 tenant-api 寫某個租戶回 409 `TENANT_FILE_DIRTY`（批次 422） | Platform Engineer | [tenant-api 寫入回 TENANT_FILE_DIRTY](#tenant-api-寫入回-tenant_file_dirty) |
| 經 tenant-api 的寫入回 422 `GENERATOR_FINDINGS`、429／422 `GENERATOR_REJECTED` 或 503 `VALIDATOR_UNAVAILABLE`／`POLICY_UNAVAILABLE` | Platform Engineer | [tenant-api 寫入被產生器容器拒收](#tenant-api-寫入被產生器容器拒收generator_findingsvalidator_unavailable) |

## SHA-256 熱重新加載延遲

> 👤 **適用：** Platform Engineer / SRE（需 `kubectl` 存取叢集）

**情景：** ConfigMap 更新後，threshold-exporter 仍顯示舊值

```bash
# 診斷
$ kubectl get configmap -n monitoring configmap-defaults -o jsonpath='{.metadata.generation}'
5

$ kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
2026-02-26T10:15:32Z SHA256: abc123... (old)
2026-02-26T10:20:45Z SHA256: def456... (updated after 5min)
```

**原因：** Kubernetes 至多每 60 秒同步一次 ConfigMap 掛載

⚠️ **超過一分鐘仍是舊值、且之後永遠不會更新**：舊版 exporter 對 symlink 掛載的 conf.d（ConfigMap volume 的 `key -> ..data/key` 就是這種）判斷「檔案沒變」時只看 symlink 自己的 mtime，`..data` 換版後永遠判成未變（[#1969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1969)）。受影響（依各版程式碼判讀）：v2.1.0–v2.7.x 的所有目錄模式；v2.8.0–v2.9.1 中 conf.d 沒有任何 `_defaults.yaml`／`_defaults.yml` 的樹（扁平模式）；v2.0.x 不受影響。修正版本起同時比對 symlink 與其目標的 stat。在受影響版本上只能用下方第 1 項重啟。⚠️ 修正只涵蓋扁平 key 佈局：ConfigMap `items` 帶巢狀 path（如 `team-a/x.yaml`）時，kubelet 只建頂層目錄 symlink `team-a -> ..data/team-a`，exporter 不跟進目錄 symlink，那些檔案根本不會被載入——這是另一個問題，見 [#1972](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1972)。含 #1972 的版本起這件事不再無聲：exporter 每次掃描都會對每個這種目錄 symlink 印一行 `WARN: <路徑> is a symlink to a directory and is not followed; config files under it are NOT loaded`，`validate_config` 的 `yaml_syntax` 也會點名它並 FAIL（rc 1）。檔案仍然不會被載入；要讓它們生效，把 `items[].path` 改成不含 `/` 的扁平 key。名稱本身像設定檔的目錄 symlink（例如指向目錄的 `t.YAML`）不走這行，印的是既有的 `WARN: cannot read <路徑>: ... is a directory`。指向 conf.d 內、且 walker 本來就會掃到的真實目錄的 symlink（路徑不經過 `.` 開頭的目錄）不會印 WARN：那些檔案已經透過真實路徑載入。

⚠️ **所有租戶的修改都不生效，log 每個 tick 出現 `duplicate tenant ID "<id>": defined in both <檔 A> and <檔 B>`**：同一租戶 id 在兩個檔宣告會讓每次掃描失敗，整棵樹停在最後一版正確設定——不只那個租戶，任何租戶的修改都不套用；`/ready` 仍回 200、`da_config_reload_trigger_total` 不動（[#2452](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2452)）。有無 `_defaults.yaml` 都會發生，log 都是每 tick 一行 `WARN: cannot check config <設定目錄>: …`：有 `_defaults.yaml` 時後面接 `hierarchical scan: duplicate tenant ID …`，沒有時（扁平模式）直接接 `duplicate tenant ID …`。重複宣告出現時若已有排定、尚未執行的 debounced reload，它自己的掃描也會失敗一次，log 多一行 `ERROR: scan failed: duplicate tenant ID …`，兩種模式字樣相同（[#2587](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2587) 之前的版本寫成 `ERROR: hierarchical scan failed: …`，扁平模式也是這樣寫，不代表樹裡有 `_defaults.yaml`）。含 #2452 的版本起，每次失敗會累加 `da_config_scan_failures_total{reason="duplicate_tenant"}`；設定超過 5 分鐘無法掃描時平台告警 `ConfigScanFailing` 觸發（設定目錄不見則是 `reason="walk_error"`；設定目錄列不出內容是 `root_unreadable`、裡面沒有任何可用的設定檔是 `empty_tree`，[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)）。⚠️ `--reload-interval` 大於約 180s 時，單次失敗的 tick 也可能觸發它：前後兩次成功掃描最多相隔約兩個間隔，那時設定已真的超過 5 分鐘沒有成功掃描。逐 tick 模擬（scrape 30s）實測：180s 以下單次失敗不觸發，190s 部分情況觸發，200s–420s 都觸發。處理：刪掉其中一處宣告；下一個 tick 就會重新載入，期間累積的修改一併生效，不需重啟，告警也在這個成功的 tick 被抓到後解除。

⚠️ **某些租戶（或全部租戶從 `_defaults.yaml` 繼承的閾值）從 `/metrics` 消失，其他租戶照常更新**：多半是檔案或子目錄讀不到（權限、懸空的 symlink、設定檔名的 FIFO／socket／裝置檔），或 `_defaults.yaml` 壞掉；只有某個租戶消失時，也可能是**它的租戶檔**解析失敗（語法錯、租戶 body 不是 mapping、泛型解碼失敗，如顯式 tag 與內容不符的純量 `!!int abc`、未知頂層 key（`x-anchors:` 之類）內的重複 key——[#3063](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3063) 起整份拒收，同檔其他租戶一併消失；`_` 開頭的平台檔不套用，見 [#3131](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3131)）——該檔宣告的租戶一律從 `/metrics` 移除（除非另一個可解析的檔也宣告它），log 每次掃描印 `WARN: skip unparseable file "<路徑>"`、`da_config_parse_failure_total{file_basename="<檔名>"}` 上升（告警 `ConfigParseFailure`）。[#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980) 之前，沒有 `_defaults.yaml` 的扁平樹在熱重載時會保留它最後的正確值，重啟後才消失。讀不到的檔不算掃描失敗，其餘的樹照常套用，所以 `ConfigScanFailing` 不會響；log 每個 tick 有 `WARN: cannot read …`／`WARN: cannot stat …`／`WARN: walk error at …`／`WARN: … is a special file (not a regular file) and is not read`（[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939) 之前，設定檔名的 FIFO 會讓掃描永遠卡住，reload 與 `da-guard` 各子命令都不會結束）。含 [#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592) 的版本起有兩個狀態型 gauge：`da_config_unreadable_files{reason}`（讀不到的檔／子目錄數，告警 `ConfigFilesUnreadable`，critical）與 `da_config_defaults_unusable{reason}`（解析失敗或讀不到的 `_defaults.yaml` 數，告警 `ConfigDefaultsUnusable`）；兩者在問題持續期間維持非 0，修好後下一個 tick 回 0。處理：修正檔案權限／symlink 或 YAML，不需重啟。

**解決方案：**
1. 強制重新啟動：`kubectl rollout restart deployment/threshold-exporter`
2. 或等待掛載同步（典型 < 1分鐘）——**只在 exporter 已含 [#1969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1969) 修正時有效**；在上方列出的受影響版本上等待永遠不會生效，只能用第 1 項重啟

## 空向量警報不觸發

> 👤 **適用：** Tenant（症狀：我的告警沒響）；下方診斷指令需 Platform / SRE 的 PromQL / 叢集存取

**情景：** Redis 沒有部署匯出器，但 Redis 警報規則仍在評估

```promql
# 問題：
redis_memory_usage_percent{job="redis-exporter"} >= on(tenant) group_left
  user_threshold{metric="redis_memory_usage_percent", severity="warning"}

# 右側為空向量 (user_threshold 無 Redis 資料)
# group_left 匹配失敗 → 警報不觸發 ✓ 預期行為
```

**驗證（非問題）：**
```bash
$ kubectl exec -it prometheus-0 -c prometheus -- \
  promtool query instant 'count(redis_memory_usage_percent)'
0  # 無 Redis 指標 ✓
```

## 雙租戶抓取重複計數

> 👤 **適用：** Platform Engineer / SRE（scrape 設定與 recording rule）

**情景：** Prometheus 從兩個 threshold-exporter 副本抓取，user_threshold 值翻倍

```
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-1)
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-2)
# ↓ sum by(tenant) 會產生 60 （錯誤！）
```

**修正：** 確保所有閾值規則使用 `max by(tenant)`

```yaml
- record: tenant:alert_threshold:slave_lag
  expr: |
    max by(tenant)  # ✓ 不是 sum
      user_threshold{metric="slave_lag"}
```

**閾值 vs 資料——聚合方式的差異：**

此問題僅涉及 **threshold（閾值）recording rules**。閾值本質上是一個設定值（例如「連線上限 100」），無論幾個 exporter 副本回報，數值都相同，因此 `max by(tenant)` 是語義上唯一正確的聚合方式——不存在需要 `sum` 的場景。平台在兩層保證這一點：

1. **Platform Rule Packs**：所有 threshold recording rules 固定使用 `max by(tenant)`
2. **`migrate_rule.py` AST 引擎**：產出的 threshold recording rule 也固定為 `max by(tenant)`，使用者無法覆寫

另一方面，**data（資料）recording rules** 的聚合方式依語義而異。例如 `mysql_threads_connected`（當前連線數）每個副本觀察到的是同一個值，用 `max`；但 `rate(requests_total)`（每秒請求量）若來自不同來源，可能需要 `sum`。Data recording rules 的聚合策略可透過 metric dictionary 指定，不受本節 threshold 聚合約束的影響。

## 平台告警沒有收到通知

> 👤 **適用：** Platform Engineer / SRE

**情景：** Prometheus `/alerts` 看得到平台自監控告警（`ThresholdExporterAbsent`、`TenantApiSingleWriterBreach`、`FederationAuditPipelineSilent` 之類）在 Firing，Alertmanager UI 也看得到，但**沒有任何人收到通知**。

**原因（多半不是故障，是沒接）：** 平台自監控 rule pack 出貨預設**不投遞**。`Watchdog` 走 index-0 心跳專線，其餘**全部**落在 root 的 `default` receiver——那是一個**沒有 notifier** 的 receiver，所以告警到此為止。這是刻意的出貨姿態（不預設把告警送到我們不知道的目的地）。

**診斷：**

```bash
# 1. 確認告警在 Alertmanager 手上（不是 Prometheus 沒送出去）
kubectl exec -n monitoring deploy/alertmanager -- \
  amtool alert --alertmanager.url=http://localhost:9093

# 2. 問 Alertmanager 這則告警會被路由到哪個 receiver
kubectl exec -n monitoring deploy/alertmanager -- \
  amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml \
  alertname=ThresholdExporterAbsent severity=critical alert_source=platform
# 回 `default` ⇒ 沒接（正常出貨狀態）；回 `platform-enforced` ⇒ 已接上
```

**解法：** 用平台級 enforced route 接上——`conf.d/_defaults.yaml` 的 `_routing_enforced` 加 `match: ['alert_source="platform"']`（除 `Watchdog` 外全部帶此 label）。完整範例、`tenant=""` 兜底的陷阱、以及「一條 enforced route 無法同時涵蓋 critical 與 platform」等限制，見 [BYO Alertmanager 整合指南 §11](integration/byo-alertmanager-integration.md#11-平台自監控告警的投遞)。

**接上之後仍沒收到？** 依序排除：

1. `da-tools validate-config --config-dir conf.d/ --policy .github/custom-rule-policy.yaml` 是否有 `_routing_enforced` 被 WARN + skip（receiver 設定錯誤或 webhook domain 不在 allowlist 都會**靜默跳過**該 route）。⚠️ **`--policy` 不可省**：domain allowlist 檢查在 `_grar_routes.py` 是 `if allowed_domains:` 守著的，沒給 policy 就等於沒有 allowlist、驗不出「domain 被擋掉」這個成因（裸指令會顯示一切正常）。
2. 產生設定後是否真的 reload 了 Alertmanager（見 [§Step 6](integration/byo-alertmanager-integration.md#step-6-reload-alertmanager)）。
3. 是否誤用了模式 B（receiver 含 `{{tenant}}`）——per-tenant enforced route 帶 `tenant="<name>"` matcher，而平台告警除後面點名的 3 條外**都沒有 tenant、結構上收不到**。⚠️ 反過來要注意這 3 條（`TenantMetricsOverLimit` / `FederationRejectionRateAnomaly` / `FederationGatewayBackendErrors`）**帶 tenant、會被送進租戶通道**——症狀是「租戶抱怨收到看不懂的平台告警」而不是「我沒收到」，兩種相反的失敗都指向同一個成因。詳見 [BYO 指南 §11](integration/byo-alertmanager-integration.md#11-平台自監控告警的投遞)。

⚠️ **注意，這不是「沒收到」而是「收太多」的反向情境**：若你在某條 route 寫了 `tenant=~".*"` 想表達「所有租戶」，Alertmanager 會把**平台告警一起吃進租戶通道**（label 不存在等同空值）。改寫 `tenant!=""` 只解決一半——有 3 條平台告警帶 `tenant` label（`TenantMetricsOverLimit` / `FederationRejectionRateAnomaly` / `FederationGatewayBackendErrors`，後兩條的 label 由 expr `sum by (tenant)` 在觸發時才產生）。「只要租戶告警」要寫 `tenant!=""` **加上** `alert_source=""`。

**相關：** `Watchdog` 心跳本身沒送出去是另一個症狀，走 [告警平面自我存活性 §⑤ 排障](integration/alerting-plane-self-liveness.md)。

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## Prometheus Operator 環境常見問題

> 👤 **適用：** Platform Engineer（Prometheus Operator 部署環境）

**情景：** 使用 Prometheus Operator（kube-prometheus-stack）時 PrometheusRule 不生效

**診斷**：
```bash
# 檢查 PrometheusRule 是否已載入
kubectl get prometheusrules -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting

# 檢查 Prometheus 是否 reject rule
kubectl logs -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus | grep "rule"

# 確認 ruleSelector 匹配
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'
```

**常見原因與修正**：

1. **ruleSelector label 不匹配**
   - 原因：PrometheusRule 缺少 Prometheus CRD 所要求的 label
   - 診斷：比對 `kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'` 輸出與 PrometheusRule labels
   - 修正：確保 PrometheusRule 同時包含 `prometheus: kube-prometheus` 和 `release: kube-prometheus-stack`
   ```bash
   # 使用 operator-generate 自動產出正確 label（預設即帶上述兩個；Helm release 名稱不同時用 --selector-label 覆寫）
   da-tools operator-generate --config-dir conf.d/ --output-dir ./crds/
   da-tools operator-generate --config-dir conf.d/ --selector-label release=<你的 release> --output-dir ./crds/
   # 或手動 patch 現有 CRD
   kubectl label prometheusrule <name> -n monitoring release=kube-prometheus-stack prometheus=kube-prometheus
   ```

2. **namespace 不在 Prometheus 監控範圍**
   - 原因：Prometheus CRD 的 `ruleNamespaceSelector` 未包含目標 namespace
   - 診斷：`kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleNamespaceSelector}'`
   - 修正：擴展 namespace selector 或將 PrometheusRule 部署至已納入監控的 namespace
   ```bash
   # 方案 A：將 CRD 部署到 monitoring namespace
   da-tools operator-generate --config-dir conf.d/ --namespace monitoring --output-dir ./crds/
   # 方案 B：修改 Prometheus CRD 的 ruleNamespaceSelector 納入目標 namespace
   kubectl edit prometheus -n monitoring kube-prometheus-stack-prometheus
   # 在 spec.ruleNamespaceSelector.matchLabels 加入目標 namespace label
   ```

3. **CRD API 版本不匹配**
   - 原因：叢集安裝的 Operator 版本與產出的 CRD apiVersion 不一致
   - 診斷：`kubectl api-versions | grep monitoring.coreos.com`
   - 修正：
   ```bash
   # 指定與叢集相符的 AlertmanagerConfig API 版本（v1alpha1 / v1beta1，預設 v1beta1）
   da-tools operator-generate --config-dir conf.d/ --api-version v1alpha1 --output-dir ./crds/
   ```

**Rollback 程序**（從 Operator 退回 ConfigMap 模式）：
```bash
# 1. 停止 Operator 管理：刪除 PrometheusRule / AlertmanagerConfig CRD
kubectl delete prometheusrule -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
# 2. 恢復 ConfigMap 模式：Helm upgrade 切換 rules.mode
helm upgrade threshold-exporter ./helm/threshold-exporter --set rules.mode=configmap
# 3. 驗證 ConfigMap rules 已生效
kubectl get configmap -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
da-tools validate-config --config-dir ./conf.d/
```

> 詳見：[Operator Prometheus 整合](integration/operator-prometheus-integration.md) · [Operator Alertmanager 整合](integration/operator-alertmanager-integration.md) · [Operator GitOps 部署](integration/operator-gitops-deployment.md)

## tenant-api 寫入全部被拒（`TENANT_TREE_UNREADABLE`／`TENANT_STORE_FAULT`）

> 👤 **適用：** Platform Engineer / 呼叫 tenant-api 的工具維護者

**症狀**：經 tenant-api 的寫入都被拒，回應碼是下列其中之一（[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939)）：

- 409 `TENANT_TREE_UNREADABLE`（批次 422、逐筆 `code: TENANT_TREE_UNREADABLE`）
- 500 `TENANT_STORE_FAULT`（批次整批 500）；tenant-api 的 log 有 `conf.d holds unreadable entries`，平台告警 `TenantApiStoreUnreadable` 或 `TenantApiStoreScanStale`
- 409 `TENANT_CONFIG_NOT_LOADABLE`，`config_error` 為 `unreadable` 或 `not_regular_file`

只有某個租戶的部分更新（批次 patch、`PUT .../custom-alerts`）被拒、`config_error` 為 `not_round_trippable` 時，不是這一節的問題：重新輸出那個檔會連帶改到沒有編輯的內容，見 [Tenant API 硬化 §3.9](api/tenant-api-hardening.md#39-每筆寫入先問產生器容器2903)。

與 exporter 的 `ConfigFilesUnreadable`（見 [SHA-256 熱重載延遲](#sha-256-熱重新加載延遲)）不同，這裡指的是 tenant-api 掛載的那份 conf.d。

原因與修法見 [Tenant API 硬化 §3.8](api/tenant-api-hardening.md#38-confd-有讀不了的項目時寫入整個拒收2939)。

## tenant-api 寫入回 TENANT_FILE_DIRTY

> 👤 **適用：** Platform Engineer / 呼叫 tenant-api 的工具維護者

**症狀**：直寫模式下寫某個租戶回 409 `TENANT_FILE_DIRTY`（批次 422、該筆 `code: TENANT_FILE_DIRTY`），重整重試仍一樣（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）。

**原因**：tenant-api 掛載的 repo 裡，這個租戶檔與 HEAD 不一致——有人在 tenant-api 之外改了沒 commit、`git add` 了沒 commit、刪了、放了未追蹤的同名檔，或在 index 標了 `skip-worktree`／`assume-unchanged`；經 symlink 抵達的租戶檔，沿路的連結或 repo 內的目標被改也算。

**處置**：在該 repo 跑 `git status -- <租戶檔>`（標記用 `git ls-files -v -- <租戶檔>` 看），把修改 commit 或丟棄後重試。細節見 [Tenant API 硬化 §3.10](api/tenant-api-hardening.md#310-租戶檔與-head-不一致時直寫拒收1535)。

## tenant-api 寫入被產生器容器拒收（`GENERATOR_FINDINGS`／`VALIDATOR_UNAVAILABLE`）

> 👤 **適用：** Platform Engineer / 呼叫 tenant-api 的工具維護者

**症狀**：會改變 conf.d 內容的寫入被拒（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)），什麼都沒寫入：

- 422 `GENERATOR_FINDINGS`：寫入後的整棵 conf.d 過不了 `generate-routes --validate --strict`。看回應的 `findings[]`：`related: true` 的是這筆寫入造成或碰到的；`pre_existing: true` 的是寫入前就有的。樹上別處已有會擋的問題時，所有寫入都會被拒——問題在某個租戶檔、修好它就能讓整棵樹通過時可以經 tenant-api 修，其他情況（平台檔、子目錄的檔、多個租戶同時違規）走 git 修。要在本機重現，用容器那一版 da-tools（版本在 `da_generator_build_info`）帶同一份部署端 policy 檔跑 `da-tools generate-routes --validate --strict`。
- 429 `GENERATOR_REJECTED`（`reason` 為 `busy`、`deadline`、`git_read`、`mismatch`、`no_time`）：這次沒判出結果，照 `Retry-After` 重送。持續出現看 `tenant_api_generator_rejections_total` 與產生器容器的 log。
- 422 `GENERATOR_REJECTED`：`too_large` 拆小批次重送；`memory` 看告警 `GeneratorJudgementMemoryNearLimit`，一起調高產生器的 `--memory-limit-bytes` 與容器的記憶體上限；`normalized` 是 `.gitattributes`（eol、text、filter）會把送來的內容改掉再 commit——照 git 存的樣子送（例如 `eol=lf` 就用 LF），或改屬性。
- 503 `VALIDATOR_UNAVAILABLE`／`POLICY_UNAVAILABLE`：`tenant_api_policy_available` 為 0、告警 `TenantApiPolicyUnavailable`。依 `reason` 處理：`unreachable` 看產生器容器是否在跑（映像要含 `generator-service`，chart 預設的 `tools/v2.9.0` 沒有）、與 tenant-api 是否同 uid；`amtool_missing`、`generator_too_old` 換映像；`policy_unusable`／`policy_unreadable` 修部署端 policy 的 ConfigMap。`GENERATOR_REJECTED` 與這些 503 的回應都只帶 `reason`（不認得的顯示 `unknown`），細節（路徑、例外內容、原本的 reason）在 tenant-api 的 log（`write refused: the generator …`、批次為 `batch op refused: …`）。

各回應碼的完整說明見 [Tenant API 硬化 §3.9](api/tenant-api-hardening.md#39-每筆寫入先問產生器容器2903)。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Troubleshooting and Edge Cases"] | ⭐⭐⭐ |
| ["Grafana Dashboard 導覽"](./grafana-dashboards.md) | ⭐⭐⭐ |
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.md) | ⭐⭐ |
| ["性能基準 (Performance Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](integration/byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](integration/byo-prometheus-integration.md) | ⭐⭐ |
| ["Operator Prometheus 整合"](integration/prometheus-operator-integration.md) | ⭐⭐ |
| ["Operator Alertmanager 整合"](integration/operator-alertmanager-integration.md) | ⭐⭐ |
| ["Operator GitOps 部署"](integration/operator-gitops-deployment.md) | ⭐⭐ |
| ["驗證場景與平台行為"](scenarios/verified-scenarios.md) | ⭐⭐ |
