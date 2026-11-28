---
title: "ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡"
tags: [adr, alerting, observability, prometheus, alertmanager, gitops]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-025
tracking_kind: adr
status: accepted
domain: observability
created_at: 2026-06-14
updated_at: 2026-11-24
---

# ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡

> **Language / 語言：** **中文 (Current)** | [English](./025-alerting-plane-self-liveness.en.md)

## 狀態

✅ **Accepted**（2026-06-14）。

2026-11-24 修訂：改寫為現行行為。決策不變。

Operator 要做的設定、TTL 怎麼抓、靜音與抑制的禁區，見 [Operator 指南](../integration/alerting-plane-self-liveness.md)。

## 決策

平台出貨的 Prometheus 與 Alertmanager 各只跑一個副本，平台自己的告警全部由這個 Prometheus 評估。它（或 Alertmanager）一死，所有告警安靜地停止，沒有人會收到通知。為此決定三件事：

1. **存活心跳送到平台外部。** 一條永遠在觸發的告警 `Watchdog`，經一條獨立、排在最前面的路由，固定頻率送到平台外部的心跳監測服務。外部服務「該收到卻沒收到」時才呼叫人。
2. **斷網環境由叢集外部輪詢健康端點。** 送不出心跳的環境，改由叢集外部的監測系統定期探測 Prometheus 與 Alertmanager 的健康端點。
3. **高可用與大規模儲存交給 operator。** 平台不把多副本 Prometheus／Alertmanager 或大規模時序儲存做進產品。

### 範例：心跳的規則、路由與 receiver

規則：表達式永遠成立，所以告警永遠處於 firing。

```yaml {.quote src="rule-packs/rule-pack-platform.yaml"}
      - alert: Watchdog
        expr: vector(1)
        labels:
          severity: none
        annotations:
          summary: "Alerting pipeline heartbeat — if this stops reaching the external monitor, Prometheus/Alertmanager is down"
```

Alertmanager 設定：`Watchdog` 是 `routes` 的第一條，有自己的分組與頻率，`continue: false` 讓它不再往下比對；receiver 從掛載的 Secret 讀心跳 URL。

```yaml {.quote src="k8s/03-monitoring/configmap-alertmanager.yaml"}
route:
  group_by: ["alertname", "tenant"]
# ...
  routes:
# ...
    - matchers:
        - alertname="Watchdog"
        - da_migrated=""
      group_by: ["alertname"]
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m
      receiver: "watchdog-heartbeat"
      continue: false
# ...
receivers:
# ...
  - name: "watchdog-heartbeat"
# ...
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/watchdog-heartbeat/watchdog-heartbeat-url
        send_resolved: false
```

結果：`promtool test rules tests/rulepacks/platform-watchdog_test.yaml` 斷言 `Watchdog` 在第 0 分鐘與第 30 分鐘都在觸發、標籤只有 `severity: none`，不需要任何輸入序列。Alertmanager 收到後立刻送出第一次（`group_wait: 0s`），之後每 3 分鐘重送一次（`repeat_interval: 3m`）。外部監測設 5 分鐘逾時：每 3 分鐘收到一次就保持安靜；Prometheus 死、Alertmanager 死、或訊號出不了叢集（防火牆、憑證），5 分鐘內收不到，外部就呼叫人。

### 1. 存活心跳送到平台外部

這種「永遠觸發、停了才代表出事」的告警，業界稱為 *Watchdog*；接收端反過來判斷的監測方式稱為 dead-man's switch（收不到訊號才觸發的開關）。

**接收端一定要在平台外部。** 監控系統無法監控自己：心跳的接收端若和 Prometheus 在同一個叢集或網路，它們會一起死。只有不會跟平台一起死的地方，才能在平台整體失聯時發出警報。

**路由必須是第一條。** Alertmanager 由上往下比對路由。若前面有一條範圍較廣、`continue: false` 的路由（例如租戶自訂告警的隔離路由），它會先吃掉 `Watchdog`，心跳就永遠送不出去。路由產生器（`da-tools generate-routes`）每次重新產生設定時，都把 `Watchdog` 這條放回 `routes` 的第一條；路由的分組用 `[alertname]`，不繼承 root 的 `[alertname, tenant]`，心跳頻率才不會被分組打亂。

**心跳不能被靜音或抑制擋掉。** 訊號就算評估正常、也進了第一條路由，送出前仍可能被 Alertmanager 擋下；擋下就等於外部收不到心跳，反而誤報「平台已死」。擋下的地方有兩個：

- **Silence（靜音）**：重大故障時常下萬用靜音壓告警海嘯，會一起壓掉 `Watchdog`。
- **`inhibit_rules`（抑制規則）**：例如某條叢集掛掉的告警觸發時，抑制所有常規告警。

Alertmanager 沒有「免於抑制」的設定。`severity: none` 只讓 `Watchdog` 不落入以 severity 為目標的抑制，一條目標更廣的抑制仍會壓掉它。所以改用兩道約束：

- **抑制規則由路由產生器檢查。** 路由產生器組出最終設定時，檢查每一條 `inhibit_rules` 的 `target_matchers`：若全部比對得上 `Watchdog` 的標籤（`alertname="Watchdog"`、`severity="none"`），就拒絕輸出。例如一條 `target_matchers: ['severity=~".*"']` 的規則會得到：

  ```text
  ADR-025 invariant violated: inhibit rule(s) would suppress the always-firing Watchdog heartbeat (inhibit_rules[0] target=['severity=~".*"']). No inhibit_rules target_matchers may match alertname="Watchdog" — the heartbeat must always reach the external dead-man's-switch. Remove or narrow the rule (see the alerting-plane self-liveness runbook).
  ```

- **靜音由 operator 規範約束。** 不得對 `alertname="Watchdog"` 下 Silence；萬用靜音要明確排除 `Watchdog`。這一條沒有機械檢查。

**外部逾時要比重送間隔長。** 外部逾時必須比 `repeat_interval` 長，吸收網路延遲，也吸收極端負載下的規則評估落後：資源被擠壓時 Prometheus 雖然還活著，規則評估迴圈會落後，心跳因而延遲。`repeat_interval: 3m` 時建議外部逾時 5 分鐘，並在外部設首次心跳的寬限期，避免平台重啟時誤報。

**心跳 URL 是設定，不是硬性依賴。** URL 內嵌 token，屬於機密，不寫進 ConfigMap；receiver 以 `url_file` 讀掛載的 Secret，送出時才讀檔，輪換 URL 不必重載 Alertmanager。出貨的 Secret 是佔位值：此時心跳送不出去，`AlertmanagerWebhookNotificationsFailing` 會觸發，提示 operator 去設定。路由產生器也會檢查 `watchdog-heartbeat` receiver 有沒有接上任何通知設定：沒接時印出警告；以 operator 自己的 base 設定組裝、又帶 `--strict` 時，改為錯誤並以結束碼 1 結束。要同時送到兩個外部監測（冗餘），在 `watchdog-heartbeat` 這個 receiver 裡加多個 `webhook_configs`，不要拆成多條路由：路由產生器每次都移除所有 `Watchdog` 路由、只放回一條，而手寫設定裡第二條路由會被第一條的 `continue: false` 擋住。

### 2. 斷網環境：由叢集外部輪詢健康端點

完全斷網的環境（例如金融內網、產線邊緣）送不出心跳。退路是反方向：由叢集外部的上層網管系統，定期探測 Prometheus 與 Alertmanager 的 `/-/ready`、`/-/healthy`。

這不是 Kubernetes 的 readiness／liveness probe。Kubernetes 的探測失敗只會重啟 Pod 或把它移出服務，那是叢集內部的動作，不會對外發告警，在斷網或整個節點死掉時也幫不上忙。這裡指的是叢集外部的監測系統主動輪詢，平台整體失聯時才發得出警報。

### 3. 高可用與大規模儲存交給 operator

平台不把多副本 Prometheus／Alertmanager 或大規模時序儲存做進產品，理由：

- 平台的定位是只負責規則與授權、對儲存後端保持中立：同一套規則在任何相容的後端上都能跑。
- 正式環境的 operator 預期自帶高可用的監控棧。
- 出貨的範例部署維持單副本，是示範用的姿態。

### 其餘平台告警怎麼送到人

上面的路由只處理 `Watchdog`。其餘平台自我監控告警走既有機制，不需要新元件：

- 除 `Watchdog` 外，平台自我監控告警都帶 `alert_source: platform`。平台告警多半沒有 tenant 標籤可以依附，這個標籤讓 operator 能用正向條件選到它們。
- `Watchdog` 刻意不帶這個標籤：它已有第一條路由，多一個標籤會讓心跳被拉進第二條投遞路徑。這兩個方向都由 `tests/ops/test_generate_routes_orchestration.py` 的 `TestPlatformAlertSourceContract` 檢查。
- 出貨預設不投遞：這些告警落在 root 的 `default` receiver，它沒有任何通知設定。平台不預設把告警送到不知道的地方，與心跳 URL 由 operator 設定是同一個姿態。
- operator 要收，用 `_routing_enforced`（平台級的強制路由）比對 `alert_source="platform"`。設定範例與限制見 [BYO Alertmanager 整合指南](../integration/byo-alertmanager-integration.md)第 11 節〈平台自監控告警的投遞〉。

## 背景

監控系統有個老問題：它無法監控自己。

- 平台的 Prometheus 與 Alertmanager 各只跑一個副本。
- 平台自我監控的告警（包括「產生指標的元件掛了」這類）全部由這同一個 Prometheus 評估。
- 所以 Prometheus 或 Alertmanager 本身死掉時，這些告警不會觸發。畫面一片安靜，看起來沒事，其實整條告警鏈已經停了。

平台已有的高可用設計只涵蓋資料平面：產生指標的 threshold-exporter 跑兩個副本，規則用 `max by(tenant)` 去重（見[高可用性設計](../design/high-availability.md)）。監控平面自己不在其中，這份 ADR 補的就是這一塊。

這也和租戶端的存活性是兩回事。租戶可以用 `absence` 規則顧自己的指標（見[自訂規則治理](../custom-rule-governance.md)），但租戶修不好死掉的平台 Prometheus，也不該為此被呼叫。

| 這份 ADR 管 | 不在這份 |
|---|---|
| 監控平面（Prometheus、Alertmanager、到外部心跳的路由）自己的存活 | 租戶端的存活（租戶以 `absence` 規則顧自己的指標） |
| 平台 operator 的視角 | 資料平面的高可用（已有設計） |

## 後果與已知限制

- **一條規則加一條路由，不新增元件**，就補上「告警系統自己死掉沒人知道」的盲點；和「儲存後端中立」的定位一致，不和客戶的後端衝突。
- **外部心跳是 operator 要自備的依賴。** Secret 還是佔位值時，平台沒有心跳保護。斷網環境的退路是外部輪詢健康端點。
- **`AlertmanagerWebhookNotificationsFailing` 看的是所有 webhook 的失敗數。** 它的表達式只以 `integration="webhook"` 篩選、不分 receiver，所以任一租戶的 webhook receiver 壞掉也會觸發它，不只是心跳。它設為 `warning`、`for: 15m`。
- **心跳只證明引擎還活著，不證明規則評估正確。** 引擎活著、但租戶自訂告警的編譯管線或 exporter 悄悄斷掉，`Watchdog` 看不到；這要靠 runtime canary（見下方〈還沒做〉）。
- **抑制規則的檢查只涵蓋路由產生器組出的設定。** 繞過路由產生器、直接手改 `alertmanager.yml` 的抑制規則不受檢查；Silence 完全沒有機械檢查，只靠 operator 規範。
- **單副本的範例部署出事時要人工復原。** 高可用是 operator 的責任。

配套的檢查：

- **規則靜態檢查（pint）**：CI 以 pint 的 `alerts/template` 檢查規則來源，攔「聚合砍掉了告警模板用到的標籤，告警因此永遠不觸發」這類錯誤；`Watchdog` 所在的平台 rule pack 也在範圍內。設定在 `.pint.hcl`。
- **後端相容性**：`tests/rulepacks/test_vm_alert_parity.py` 在每個 PR 把所有 promtool 測試資料跑過 `vmalert-tool unittest`（VictoriaMetrics 的 MetricsQL 引擎）；未登記在 `tests/rulepacks/vm_deviation_catalog.yaml` 的差異會讓檢查失敗。`tests/rulepacks/test_vm_backend_parity.py` 在 VictoriaMetrics 版本釘選變更時，對真實的 vmsingle 驗證兩者等價；`tests/rulepacks/test_vm_replay_staleness.py` 每晚以 `vmalert -replay` 量測資料中斷（staleness）時的時序差異。
- **合成探測的對接面**：帶 `component="synthetic-probe"` 的告警一定落到 `synthetic-receiver`、`continue: false`，客戶可以用自己現有的探測器驗證端到端投遞，測試告警不會呼叫到人（見[合成探測對接](../integration/synthetic-probe-interop.md)）。

## 考慮過的替代方案

- **和平台同生共死的自架心跳監測**：監測和 Prometheus 在同一個叢集或網路，會一起死，等於沒有。要自架，就得放在真正獨立的另一個叢集或機器。
- **拿既有的日誌管線當心跳來源**：日誌儲存只存資料、不評估告警，拿它當心跳等於另外蓋一套；而且它在平台內部，仍然不是外部的錨點。
- **後端相容性用 `promql-compliance-tester`**：它跑的是固定的通用 PromQL 題庫，我們要驗的是平台自己編譯出來的寫法（`and on()`、`group_left`、`max by` 等）。改用既有的 promtool 測試資料當參考，跑過 VictoriaMetrics 的引擎比對（見 [backend-compat-baseline.md](../internal/backend-compat-baseline.md)）。
- **把 canary 做成 CI 閘門**：把合成的 `absence` 測試資料餵進「編譯器 + promtool + amtool」。它和既有的 absence 測試、Go 端的標籤契約、路由測試大幅重疊；而且 CI 裡沒有 exporter，序列得自己合成，驗到的只剩測試資料本身的性質，不是端到端。保留的是常駐的 runtime canary。

## 還沒做、各自的觸發條件

| 項目 | 內容 | 觸發條件 |
|---|---|---|
| runtime canary 的常駐部署 | 一個常駐的假租戶加一條必觸發的 dead-man's switch（`CustomAlertPipelineCanaryDown`），抓 `Watchdog` 看不到的 exporter／編譯管線靜默死亡。設計與 CI 範例已完成，見 [Runtime Canary 設計](../design/runtime-canary.md) | 重大的規則編譯重構或多租戶路由大改之前先部署當安全網；或第一次正式環境發生「告警評估悄悄失敗」之後部署防再犯 |
| 平台自建的端到端合成探測 | 平台主動發一條合成告警，走完 Prometheus → Alertmanager → 外部。客戶多半用上面的對接面就夠，未必要做 | 心跳與 canary 上線後，仍發生「規則評估悄悄失敗」 |
| 後端的時間外插語意 | `predict_linear` 在客戶後端上的時間外插是否正確；需要真實時間軸，密集的測試資料驗不出 | 沒有明確的觸發條件，仍開；見 [backend-compat-baseline.md](../internal/backend-compat-baseline.md) |

取捨的軸：本平台要取代或整合已經在用成熟監控產品的客戶。心跳、高可用、合成探測這些能力業界本來就有，該做到哪，門檻是被取代的既有產品設下的標準。所以每一項先做評估階段就要拿得出來的設計與可跑的範例（便宜），常駐元件（昂貴）等觸發條件；客戶已經有的能力走對接，不重造。

## 相關

- [Operator 指南：告警平面自我存活性](../integration/alerting-plane-self-liveness.md) — 心跳設定、TTL、靜音與抑制禁區、排障
- [BYO Alertmanager 整合指南](../integration/byo-alertmanager-integration.md) — 第 11 節，平台自監控告警的投遞
- [高可用性設計](../design/high-availability.md) — 資料平面的高可用（互補）
- [Runtime Canary 設計](../design/runtime-canary.md)
- [合成探測對接](../integration/synthetic-probe-interop.md)
- 實作位置：規則在 `rule-packs/rule-pack-platform.yaml`；路由與 receiver 由 `scripts/tools/ops/_grar_routes.py` 產生，抑制規則的檢查在 `scripts/tools/ops/_grar_validate.py`；出貨的 Alertmanager 設定在 `k8s/03-monitoring/configmap-alertmanager.yaml`，佔位 Secret 在 `k8s/03-monitoring/secret-watchdog-heartbeat.yaml`
