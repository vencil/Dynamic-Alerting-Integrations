---
title: "Runbook：告警平面自我存活性（Watchdog + 外部 dead-man's-switch）"
tags: [runbook, alerting, observability, prometheus, alertmanager]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
status: active
domain: observability
created_at: 2026-06-14
updated_at: 2026-06-14
---

# Runbook：告警平面自我存活性（Watchdog + 外部 dead-man's-switch）

> 設計依據：[ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)（D1 心跳 / D2 斷網被動探測 / D3 HA 邊界）。
> 本 runbook 是 **operator 合約**：要讓「Prometheus / Alertmanager 自己死掉」被外部察覺，operator 必須完成設定、且**遵守靜音/抑制禁區**。

## 它解什麼

平台 Prometheus 與 Alertmanager 都是單副本，所有平台告警都由這一個 Prometheus 評估——它一旦掛掉，告警會**靜默停止**。`Watchdog`（`expr: vector(1)`，永遠 firing）經一條**置頂、零聚合、固定頻率**的路由，把心跳送到平台**外部**的 dead-man's-switch（DMS）。**外部服務的邏輯是「沒收到才告警」**：心跳一停（Prometheus 死 / Alertmanager 死 / egress 壞），外部就察覺。

| 元件 | 位置 |
|---|---|
| `Watchdog` 規則 + `AlertmanagerWebhookNotificationsFailing` 內部互補告警 | `k8s/03-monitoring/configmap-rules-platform.yaml` |
| 置頂 route（`routes[0]`）+ `watchdog-heartbeat` receiver（`url_file`） | `k8s/03-monitoring/configmap-alertmanager.yaml`（由 `generate_alertmanager_routes.py` 於每次 regen 重新注入 index 0，撐過 route-REPLACE） |
| 外部 DMS URL（含 token，**機密**） | `k8s/03-monitoring/secret-watchdog-heartbeat.yaml` → 掛載到 `/etc/alertmanager/secrets/watchdog-heartbeat-url` |

## ① 設定外部心跳（必做）

1. 在平台**外部**（不會跟本叢集一起死的地方）準備一個 DMS / heartbeat 監測（如 Healthchecks.io、Better Stack、PagerDuty heartbeat、或自架在獨立 VM/叢集的監測）。取得它的 ingest URL（通常嵌一個 token/UUID）。
2. **禁止把 URL 明文寫進 ConfigMap**（會踩 secret-scan L1/L2 並洩漏）。改覆寫 Secret：

   ```bash
   kubectl create secret generic watchdog-heartbeat \
     --from-literal=watchdog-heartbeat-url="https://<dms-host>/api/heartbeat/<token>" \
     -n monitoring --dry-run=client -o yaml | kubectl apply -f -
   ```

   `webhook_configs[].url_file` 在送出時才讀檔，故輪換 URL **不需 reload**。
3. **留空＝已知盲點**：若不填，心跳送不出去、`AlertmanagerWebhookNotificationsFailing` 會 firing 當「請設定我」的提醒（demo / lab 預設就是這狀態）。

## ② 外部 TTL 怎麼抓（容錯契約）

外部監測的逾時門檻（TTL）**必須比 route 的 `repeat_interval`（3m）長**，且要同時吸收：

- **網路抖動**；
- **極端負載下的規則評估滯後**（Prometheus pod 沒死但評估迴圈落後）；
- **Prometheus 重啟冷啟動 ~60s 空窗**（重啟後第一拍心跳會延遲）。

建議：**外部 TTL = 5m**（= 3m + 2m 緩衝），並在外部 DMS 開「**首發心跳寬限期**（grace period）」以免每次平台重啟誤報。若調 `repeat_interval`，外部 TTL 要等比例放大。

## ③ 靜音 / 抑制禁區（⛔ 嚴格遵守）

Alertmanager **沒有「inhibition 免疫」原語**；`severity: none` 只是讓 Watchdog 不落入既有 severity-targeted 抑制，**不是萬用免疫**。保證來自兩道把關：

- **抑制端（機械強制）**：任何 `inhibit_rules` 的 `target_matchers` **不得** match `alertname="Watchdog"`。此不變式由 `generate_alertmanager_routes.py` 在兩條 render path（`assemble_configmap` / `--apply` merge）**fail-closed 驗證**（base + generated 合併後的完整集合），違反即拒絕產出。**不要**新增 CodeRabbit 曾建議的 `source=Watchdog → target!=Watchdog` 規則——Watchdog 永遠 firing 且無 `equal:`，那會永久壓掉**所有**非 Watchdog 告警（ADR-025 已否決）。
- **靜音端（無法機械強制 → 靠紀律）**：
  - ⛔ **嚴禁**對 `alertname="Watchdog"` 下 Silence。
  - ⛔ 重大故障時若要下**全域萬用靜音**（`.*` / `alertname=~".*"` 壓告警海嘯），**必須顯式排除** Watchdog，例如多加一條 matcher `alertname!="Watchdog"`。否則外部 DMS 會在你最需要它的時候誤報「平台死亡」。

## ④ 斷網環境（D2 被動探測）

完全斷網（金融內網 / 產線邊緣）送不出心跳。退路是**反向操作**：由**叢集外部**的上層網管系統**定期主動輪詢** Prometheus 與 Alertmanager 的健康端點（兩者都內建 `/-/ready`、`/-/healthy`）。

⚠️ 這**不是** Kubernetes 的 readiness / liveness probe——K8s 內建探測失敗只會重啟 Pod 或移出服務，是**叢集內部**行為、**沒有對外告警能力**，斷網或整節點死亡時幫不上忙。必須是**叢集外部**的監測主動 pull。

## ⑤ 排障

| 症狀 | 可能原因 / 處置 |
|---|---|
| `AlertmanagerWebhookNotificationsFailing` firing | webhook egress 壞：Secret 沒填（仍是 placeholder）/ URL 無效 / token 過期 / egress 防火牆擋。先查 Secret 內容與到 DMS 的網路。demo 環境屬預期（請設定 Secret）。 |
| 外部 DMS 報「未收到心跳」但平台看似正常 | 先看 `AlertmanagerWebhookNotificationsFailing`：有 firing ⇒ 是**心跳管路壞**（egress），不是平台死；沒 firing ⇒ 可能 Prometheus/Alertmanager 真的死了（去叢集外 pull `/-/ready` 確認），或外部 TTL 設太短（見 ②）。 |
| regen 後 Watchdog route 不在 `routes[0]` | 不應發生——`generate_alertmanager_routes.py` 強制注入 index 0。若手改了 base ConfigMap 的 route 順序，跑一次 generator 重新產出即可校正。 |

## ⑥ 驗證（staging 手動）

1. 確認 `Watchdog` 在 Prometheus `/alerts` 恆為 Firing；確認外部 DMS 正常收到心跳。
2. 停掉 Prometheus（`kubectl scale deploy/prometheus --replicas=0 -n monitoring`）。
3. 等到超過外部 TTL（~5m）→ 外部 DMS 應發出「心跳停止」告警。
4. 還原（`--replicas=1`），確認心跳恢復、DMS 告警 resolve。

> 端到端合成探測（automated E2E：從外部送測試告警驗證整條 Prometheus→Alertmanager→外部）屬 ADR-025 **defer-with-trigger**，本 runbook 的手動驗證為 interim。

## 相關

- [ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)
- [Operator Alertmanager 整合指南](../integration/operator-alertmanager-integration.md)
- 資料平面高可用（互補，不同平面）：[高可用性設計](../design/high-availability.md)
