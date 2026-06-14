---
title: "ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡"
tags: [adr, alerting, observability, prometheus, alertmanager, gitops]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-025
tracking_kind: adr
status: proposed
domain: observability
created_at: 2026-06-14
updated_at: 2026-06-14
---

# ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡

> **Language / 語言：** **中文 (Current)** | [English](./025-alerting-plane-self-liveness.en.md)

## 狀態

🔵 **Proposed**（草案）。本 ADR 記錄一個決策：為平台的告警平面（Prometheus + Alertmanager）加上「自己死掉會被外部察覺」的存活心跳，並劃清「高可用與大規模儲存由 operator 負責」的責任邊界。實作尚未進行。

## 摘要

平台出貨的 Prometheus 與 Alertmanager 都是單一實例，而所有平台告警都由這個 Prometheus 評估——它一旦掛掉，所有告警會**靜默停止**，沒有人會收到通知。本 ADR 加一條送到平台**外部**的存活心跳來補這個盲點；高可用與大規模儲存則維持交由 operator 的監控後端負責。

## 問題

監控系統有個經典難題：**它無法監控自己**。

- 平台的 Prometheus 與 Alertmanager 各只跑一個副本。
- 平台十多條維運告警（包含「指標來源元件掛了」這類）**全由這同一個 Prometheus 評估**。
- 所以當 Prometheus（或 Alertmanager）本身死掉，這些告警**不會觸發**——畫面一片安靜、看似沒事，實則整條告警鏈已停擺。

平台**已有**高可用設計，但範圍是**資料平面**（產生指標的元件跑兩副本、用 `max` 去重）；**監控平面自己不在其中**。本 ADR 補的正是監控平面這一塊。

這也跟租戶端的存活性是**兩回事**：租戶能用 `absence` 規則顧自己的指標，但租戶**無法**修好死掉的平台 Prometheus，也不該為此被呼叫。

## 決策

### D1：存活心跳送到外部監測點

加一條**永遠在觸發**的告警（這種模式業界慣稱 *Watchdog*），透過一條**獨立且置於最高優先級**的路由，固定把訊號送到一個**平台外部**的心跳監測服務。

```yaml
# 規則：表達式永遠成立 → 永遠處於 firing
- alert: Watchdog
  expr: vector(1)
  labels: { severity: none }
  annotations:
    summary: "告警管線心跳——這條一旦停了，代表 Prometheus 已死"
```

```yaml
# Alertmanager 路由：給 Watchdog 一條獨立、零聚合、固定頻率的專線
route:
  routes:
    # ⚠️ 必須是 routes 的「第一條」（最高優先級），見下方說明
    - matchers: [ alertname="Watchdog" ]
      receiver: watchdog-heartbeat
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m       # 外部 TTL 要比這更長，見下方緩衝說明
      continue: false           # 絕不落到其他 receiver 或人類頻道
receivers:
  - name: watchdog-heartbeat
    webhook_configs:
      - url: <operator 提供的外部心跳 URL；留空＝停用、僅留盲點紀錄>
```

**關鍵是「反過來想」**：外部服務不是「收到訊號就告警」，而是「**預期每幾分鐘要收到一次；一旦沒收到，才呼叫人**」。這樣不論是 Prometheus 死、Alertmanager 死、還是訊號送不出去（防火牆 / 憑證問題），心跳一停，外部就會察覺。

**為什麼一定要「外部」**：監控系統不能監控自己——心跳的接收端必須在一個**不會跟平台一起死**的地方，這是唯一能真正兜底的位置。

**路由必須置頂**：Alertmanager 由上往下評估路由。Watchdog 這條**必須放在 `routes` 陣列的第一條**，否則可能被前面某條範圍較廣、且 `continue: false` 的路由（例如某個 severity 攔截或既有的租戶告警專線）先吞掉，導致心跳永遠送不出去。

**心跳頻率與逾時要留緩衝**：外部監測的逾時門檻（TTL）必須**比 `repeat_interval` 長**，吸收網路與評估延遲——否則一次幾秒的抖動就會誤報。例如 `repeat_interval: 3m` → 外部 TTL 設 **5m**（含約 2 分鐘緩衝）。這條容錯契約要寫進 operator 手冊。

**URL 是設定開關，不是硬性依賴**：operator 填上自己的心跳服務即生效；留空則明確記錄為已知盲點。

### D2：斷網環境改用「被動健康檢查」

完全斷網的環境（如金融內網、產線邊緣）送不出心跳。退路是**反向操作**：由**叢集外部**的上層網管系統**定期主動探測**平台的健康檢查端點（Prometheus 與 Alertmanager 都內建 `/-/ready`、`/-/healthy`）。

⚠️ **這不是 Kubernetes 的 readiness / liveness probe**。Kubernetes 內建探測失敗只會重啟 Pod 或把它移出服務——那是**叢集內部**行為、**沒有對外告警能力**，斷網或整個節點死亡時幫不上忙。這裡指的是**叢集外部**的監測系統主動輪詢，才能在平台整體失聯時發出警報。

### D3：高可用與大規模儲存：交給 operator，不自己做

平台**不**把「Prometheus / Alertmanager 多副本」或「大規模時序儲存」做進產品。理由：

- 平台一貫定位是**只負責規則與授權、儲存後端保持中立**——同一套規則在任何相容後端上都能跑。
- 正式環境的 operator 會自帶高可用監控棧（本案目標客戶本來就在跑大規模時序資料庫）。
- 出貨的範例部署維持單副本，是明確的「示範用」姿態。

若未來平台真要提供高可用範例，計費遙測（目前尚未建置）必須一開始就設計成「多副本不重複計數」。但這屬於 operator 的儲存層職責，不是這條心跳要解的事。

## 不採用的方案

- **和平台同生共死的自託管心跳**：若心跳監測跟 Prometheus 在同一叢集 / 同一網路，它們會一起死——等於沒有。要自託管，就必須放在**真正獨立**的另一個叢集或機器。
- **拿既有日誌管線當心跳來源**：平台的日誌儲存只是儲存、本身不評估告警，而且那條日誌串流在預設部署裡根本沒開。拿它當心跳是「看起來重用、其實要新蓋一套」，且仍兜不回外部錨點。

## 之後再說（各有明確觸發條件）

| 項目 | 一句話 | 觸發條件 |
|---|---|---|
| **Canary 租戶** | 一個常駐假租戶 + 必觸發告警，端到端驗證「規則編譯 + 路由」整條沒被改壞（不只是引擎還活著） | **下次重大的規則編譯邏輯重構、或多租戶路由規則大改時，先行佈署當安全網**；不必等事故發生 |
| **規則靜態檢查** | CI 引入成熟的開源規則 linter，攔截低效 / 危險查詢 | 租戶自寫查詢的複雜度開始造成後端負載 |
| **端到端合成探測** | 從外部送一條測試告警，驗證它真的走完 Prometheus→Alertmanager→外部 | 心跳 + canary 上線後，出現「規則評估悄悄失敗」事件 |
| **後端相容性測試** | 驗證規則在客戶的大規模後端上正確評估（含資料過期時序差異） | 首個客戶整合到自有後端 |

## 範圍邊界

| 這份 ADR 管 | 不在這份 |
|---|---|
| 監控平面（Prometheus + Alertmanager + 到外部心跳的路由）自我存活 | 租戶側存活（租戶用 `absence` 顧自己指標，見 value-form cookbook） |
| 平台 operator 視角 | 資料平面高可用（已有設計） |

## 後果

- **正面**：用「一條規則 + 一條路由 + 一個路由測試」、零新增元件，補上「告警系統自己死掉沒人知道」的盲點；與「儲存後端中立」定位一致，不和客戶後端打架。
- **負面**：外部心跳是 operator 要自備的依賴（斷網退路＝被動探測）；心跳只能證明「引擎還活著」、不能證明「規則評估正確」（→ 留給 Canary 租戶）；單副本示範部署下，真出事仍需人工復原（高可用是 operator 的責任）。

## 相關

- value-form cookbook 收尾：[#832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/832)——租戶側存活性所在，與本 ADR 不同平面。
- 資料平面高可用設計：[高可用性設計](../design/high-availability.md)（互補）。
- 既有的隔離式告警路由（Alertmanager 設定中的租戶自訂告警專線）可作為 Watchdog 路由範本。
