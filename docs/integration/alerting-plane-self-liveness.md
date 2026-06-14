---
title: "告警平面自我存活性 — Watchdog + 外部 Dead-Man's-Switch（Operator 指南）"
tags: [operator, alerting, observability, prometheus, alertmanager, watchdog]
audience: [platform-engineer]
version: v2.9.0
lang: zh
---

# 告警平面自我存活性 — Watchdog + 外部 Dead-Man's-Switch（Operator 指南）

> **Language / 語言：** **中文 (Current)** | [English](./alerting-plane-self-liveness.en.md)

> **受眾**：**部署並維運本平台的 Operator / SRE**。本文是讓「Prometheus / Alertmanager 自己死掉也能被察覺」的設定與維運合約——你需要備妥一個平台**外部**的監測點、完成設定、並遵守靜音/抑制禁區。
>
> 設計依據見 [ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)（D1 心跳 / D2 斷網被動探測 / D3 HA 邊界）。租戶告警的 receiver / Secret / 抑制規則設定見 [Operator Alertmanager 整合指南](operator-alertmanager-integration.md)。

## 它解什麼

平台出貨的 Prometheus 與 Alertmanager 都是單副本，而**所有平台告警都由這一個 Prometheus 評估**——它一旦掛掉，告警會**靜默停止**，沒有人會收到通知。

解法：一條**永遠在 firing** 的 `Watchdog` 告警（`expr: vector(1)`），透過一條**置頂、零聚合、固定頻率**的 Alertmanager 路由，把心跳固定送到一個平台**外部**的 dead-man's-switch（DMS）。**關鍵是反過來想**：外部服務不是「收到才告警」，而是「**預期每幾分鐘收到一次；一旦沒收到，才呼叫人**」。如此一來，不論 Prometheus 死、Alertmanager 死、還是心跳送不出去（防火牆 / 憑證），心跳一停，平台外部就會察覺。

| 元件 | 位置 |
|---|---|
| `Watchdog` 規則 + `AlertmanagerWebhookNotificationsFailing` 內部互補告警 | `k8s/03-monitoring/configmap-rules-platform.yaml` |
| 置頂 route（`routes[0]`）+ `watchdog-heartbeat` receiver（`url_file`） | `k8s/03-monitoring/configmap-alertmanager.yaml`（路由由平台的 `generate_alertmanager_routes.py` 於每次重生時重新注入 index 0，撐過 route-REPLACE） |
| 外部 DMS URL（內嵌 token，**機密**） | `k8s/03-monitoring/secret-watchdog-heartbeat.yaml` → 掛載到 `/etc/alertmanager/secrets/watchdog-heartbeat-url` |

## ① 設定外部心跳（必做）

1. 在平台**外部**（不會跟本叢集一起死的地方）準備一個 DMS / heartbeat 監測（如 Healthchecks.io、Better Stack、PagerDuty heartbeat、或自架在獨立 VM / 叢集的監測）。取得它的 ingest URL（通常內嵌一個 token / UUID）。
2. **禁止把 URL 明文寫進 ConfigMap**（URL 內嵌的 token 會踩 secret-scan 並洩漏）。改覆寫掛載的 Secret：

   ```bash
   kubectl create secret generic watchdog-heartbeat \
     --from-literal=watchdog-heartbeat-url="https://<dms-host>/api/heartbeat/<token>" \
     -n monitoring --dry-run=client -o yaml | kubectl apply -f -
   ```

   receiver 用 `webhook_configs[].url_file` 指向這個 Secret 檔；檔案**在送出時才讀**，故日後輪換 URL **不需 reload** Alertmanager。
   > 註：`kubectl apply` 更新 Secret 後，Kubernetes 把新值同步進 Pod volume 檔案約有 **1～2 分鐘物理延遲**（kubelet sync period + kubelet cache）。這段期間心跳仍送往**舊** URL 屬正常，**請稍候生效，勿誤判**為設定失敗。
3. **留空＝已知盲點**：若 Secret 維持 placeholder，心跳送不出去，`AlertmanagerWebhookNotificationsFailing` 會 firing 當作「請設定我」的提醒（demo / lab 預設即此狀態）。

## ② 外部 TTL 怎麼抓（容錯契約）

外部監測的逾時門檻（TTL）**必須比路由的 `repeat_interval`（3m）長**，且要同時吸收：

- **網路抖動**；
- **極端負載下的規則評估滯後**（Prometheus pod 沒死，但評估迴圈嚴重落後 → 心跳錯後）；
- **Prometheus 重啟冷啟動 ~60s 空窗**（重啟後第一拍心跳會延遲）。

建議：**外部 TTL = 5m**（= `repeat_interval` 3m + 2m 緩衝），並在外部 DMS 開「**首發心跳寬限期**（grace period）」以免每次平台重啟誤報。若你調整了 `repeat_interval`，外部 TTL 要等比例放大。

## ③ 靜音 / 抑制禁區（⛔ 嚴格遵守）

Alertmanager **沒有「inhibition 免疫」原語**；`severity: none` 只是讓 Watchdog 不落入既有 severity-targeted 抑制，**不是萬用免疫**。真正的保證來自兩道把關：

- **抑制端（機械強制，平台已內建）**：任何 `inhibit_rules` 的 `target_matchers` **不得** match `alertname="Watchdog"`（含反向匹配，如 `severity!="critical"` 也會命中 `severity: none` 的 Watchdog）。此不變式由平台的 `generate_alertmanager_routes.py` 在兩條輸出路徑（GitOps 組裝 / `--apply` 合併）對 base + generated 的**完整合併集** **fail-closed 驗證**，違反即拒絕產出。
  > ⚠️ **不要**新增「`source = Watchdog` → 抑制其他告警」這類規則：Watchdog 永遠 firing 且無 `equal:`，那會永久壓掉**所有**非 Watchdog 告警（ADR-025 已明確否決）。
- **靜音端（無法機械強制 → 靠紀律）**：
  - ⛔ **嚴禁**對 `alertname="Watchdog"` 下 Silence。
  - ⛔ 重大故障時若要下**全域萬用靜音**（`.*` / `alertname=~".*"` 壓告警海嘯），**必須顯式排除** Watchdog（多加一條 matcher `alertname!="Watchdog"`）。否則外部 DMS 會在你最需要它時誤報「平台死亡」，引發次生混亂。

## ④ 斷網環境（被動健康檢查）

完全斷網的環境（金融內網 / 產線邊緣）送不出心跳。退路是**反向操作**：由**叢集外部**的上層網管系統**定期主動輪詢** Prometheus 與 Alertmanager 的健康端點（兩者都內建 `/-/ready`、`/-/healthy`）。

⚠️ 這**不是** Kubernetes 的 readiness / liveness probe——K8s 內建探測失敗只會重啟 Pod 或把它移出服務，那是**叢集內部**行為、**沒有對外告警能力**，斷網或整節點死亡時幫不上忙。這裡指的是**叢集外部**的監測系統主動 pull。

## ⑤ 排障

| 症狀 | 可能原因 / 處置 |
|---|---|
| `AlertmanagerWebhookNotificationsFailing` firing | webhook egress 壞：Secret 沒填（仍是 placeholder）/ URL 無效 / token 過期 / egress 防火牆擋。先查 Secret 內容與到 DMS 的網路。demo 環境屬預期（請設定 Secret）。 |
| 外部 DMS 報「未收到心跳」但平台看似正常 | 先看 `AlertmanagerWebhookNotificationsFailing`：**有** firing ⇒ 是**心跳管路壞**（egress），不是平台死；**沒** firing ⇒ 可能 Prometheus / Alertmanager 真的死了（到叢集外 pull `/-/ready` 確認），或外部 TTL 設太短（見 ②）。 |
| 重生 ConfigMap 後 Watchdog 不在 `routes[0]` | 不應發生——`generate_alertmanager_routes.py` 強制注入 index 0。若有人手改了 base ConfigMap 的 route 順序，重跑一次 generator 即校正。 |

> **已知限制（Day-2 雷達）**：`AlertmanagerWebhookNotificationsFailing` 監聽 `integration="webhook"` 的**全域**失敗數，而 Alertmanager 此 metric 預設**無 `receiver` label**——故**任一**租戶自訂 webhook receiver 壞掉（URL / 憑證）也會觸發這條 platform 告警。MVP 階段刻意接受（fail-safe 勝過漏報），故設為 `warning`、`for: 15m` 以濾掉瞬斷。**觸發條件**：當租戶 webhook 數量成長到誤報變吵時，改用 Vector 解析 Alertmanager log 萃出 `receiver="watchdog-heartbeat"` 的精準失敗，取代這個全域 metric。

## ⑥ 驗證（staging 手動）

1. 確認 `Watchdog` 在 Prometheus `/alerts` 恆為 Firing；確認外部 DMS 正常收到心跳。
2. 停掉 Prometheus：`kubectl scale deploy/prometheus --replicas=0 -n monitoring`。
3. 等到超過外部 TTL（~5m）→ 外部 DMS 應發出「心跳停止」告警。
4. 還原（`--replicas=1`），確認心跳恢復、DMS 告警 resolve。

> 端到端合成探測（automated E2E：從外部送一條測試告警，驗證整條 Prometheus → Alertmanager → 外部）屬 ADR-025 **defer-with-trigger**；本文的手動驗證為 interim。

## 相關

- [ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)（設計決策）
- [Operator Alertmanager 整合指南](operator-alertmanager-integration.md)（租戶告警 receiver / Secret / 抑制規則）
- [高可用性設計](../design/high-availability.md)（資料平面 HA，互補、不同平面）
