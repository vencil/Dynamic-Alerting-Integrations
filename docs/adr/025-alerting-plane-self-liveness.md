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
updated_at: 2026-06-17
---

# ADR-025: 告警平面自我存活性 — 讓告警系統能偵測自己的死亡

> **Language / 語言：** **中文 (Current)** | [English](./025-alerting-plane-self-liveness.en.md)

## 狀態

✅ **Accepted**（決策 2026-06-14 經 PR [#836](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/836) 接受）。

本 ADR 記錄一個決策：為平台的告警平面（Prometheus + Alertmanager）加上「自己死掉會被外部察覺」的存活心跳，並劃清「高可用與大規模儲存由 operator 負責」的責任邊界。目前進度見下方〈實作現況〉；operator 設定與靜音/抑制禁區見 [Operator 指南](../integration/alerting-plane-self-liveness.md)。

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
  labels: { severity: none }   # 非真正嚴重度；用意是讓它不落入人類頻道（詳見下方「靜音與抑制免疫」）
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
      group_by: [alertname]     # 強制獨立聚合，不繼承頂層 group_by（本平台 root 為 [alertname,tenant]）致心跳頻率錯亂
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m       # 外部 TTL 要比這更長，見下方緩衝說明
      continue: false           # 絕不落到其他 receiver 或人類頻道
receivers:
  - name: watchdog-heartbeat
    webhook_configs:
      # 心跳 URL 嵌 token/UUID＝機密，禁明文寫進 ConfigMap（會踩 secret-scan）。
      # 用 url_file 指向掛載的 Secret；Secret 未填＝停用、僅留盲點紀錄。
      - url_file: /etc/alertmanager/secrets/watchdog-heartbeat-url
      # ⚠️ 需要多通道冗餘（雙外部監測）時，在此處加多個 webhook_configs，
      #    絕不可在上方路由樹拆成多條 route——會被 continue: false 攔斷。
```

**關鍵是「反過來想」**：外部服務不是「收到訊號就告警」，而是「**預期每幾分鐘要收到一次；一旦沒收到，才呼叫人**」。這樣不論是 Prometheus 死、Alertmanager 死、還是訊號送不出去（防火牆 / 憑證問題），心跳一停，外部就會察覺。

**為什麼一定要「外部」**：監控系統不能監控自己——心跳的接收端必須在一個**不會跟平台一起死**的地方，這是唯一能真正兜底的位置。

**路由必須置頂**：Alertmanager 由上往下評估路由。Watchdog 這條**必須放在 `routes` 陣列的第一條**，否則可能被前面某條範圍較廣、且 `continue: false` 的路由（例如某個 severity 攔截或既有的租戶告警專線）先吞掉，導致心跳永遠送不出去。

**靜音與抑制免疫**：訊號就算評估正常、也進了置頂路由，送出前仍可能被攔下——攔下就收不到心跳、反而誤報「平台死亡」。兩個攔截點：

- **全域 Silence**：重大故障時 SRE 常下 `.*` 萬用靜音壓告警海嘯，會一併壓掉 Watchdog。
- **`inhibit_rules`**：例如 `ClusterDown` 觸發時抑制所有常規告警。

關鍵認知：Alertmanager **沒有「抑制免疫」這種原語**。`severity: none` 只是讓 Watchdog 不落入既有 severity-targeted 的抑制，**不是萬用免疫**——一條未來的廣域 inhibit（`target_matchers` 命中 Watchdog 任一標籤）仍會壓掉它。所以免疫得靠**設計約束 + 機械把關**：

- 任何 `inhibit_rules` 的 `target_matchers` **不得** match `alertname="Watchdog"`（用 lint / config-review 驗，比依賴標籤約定可靠）。
- operator 手冊**嚴禁**對 `alertname="Watchdog"` 施加 Silence；全域萬用靜音（`.*`）**必須顯式排除** Watchdog。

**心跳頻率與逾時要留緩衝**：外部監測的逾時門檻（TTL）必須**比 `repeat_interval` 長**，吸收網路延遲**與極端負載下的規則評估滯後（rule evaluation lag）**——資源被擠兌時 Prometheus 雖活著（pod 沒死），其規則評估迴圈會嚴重落後、心跳因而錯後。例如 `repeat_interval: 3m` → 外部 TTL 設 **5m**；這 2 分鐘緩衝是為了防引擎內部排程飢餓，不只是吸收幾秒的網路抖動。這條容錯契約要寫進 operator 手冊。

**URL 是設定開關，不是硬性依賴**：operator 把自己的心跳 URL 放進掛載的 Secret（`url_file`）即生效；Secret 留空（placeholder）則明確記錄為已知盲點。

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

## 實作現況

引擎死亡的盲點已補，且所有延後項的**設計就緒半**都已完成；唯一還沒上線的是「規則評估正確性」的**常駐**端到端保證（見〈之後再說〉）。

- **Watchdog + 外部 dead-man's-switch（D1）** — 已實作（[#838](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/838)）。
- **CI 規則靜態檢查（pint）** — 採用 OSS `pint`、hard-gate `alerts/template`，攔截「聚合砍掉 template 用到的 label → 告警永遠靜默」這個本 repo 燒過多次的類別（[#843](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/843)）。
- **後端相容性 — PromQL / value parity** — 每 PR 由 `tests/rulepacks/test_vm_alert_parity.py`（全 fixture 過 `vmalert-tool unittest` = 生產 MetricsQL 引擎）守，把「儲存後端中立」變成可驗證 CI 事實；`test_vm_backend_parity.py` 退役成 on-demand 的「vmalert-tool == 真 vmsingle」等價 anchor（原 docker-VM job 已併入 gate A，[#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947)）。
- **合成探測對接面** — 帶 `component="synthetic-probe"` 的告警保證落 `synthetic-receiver` 且 `continue:false`，讓客戶用自己現有的探測器零風險驗端到端投遞（見 [合成探測對接](../integration/synthetic-probe-interop.md)）。
- **runtime canary 租戶** — **設計就緒**（完整設計 + CI promtool 範例見 [Runtime Canary 設計](../design/runtime-canary.md)）；管線在正式環境已串通，**仍延後的是常駐部署**（詳見〈之後再說〉）。

## 之後再說（各有明確觸發條件）

> **取捨的軸**：本平台要取代或整合「已經在用成熟監控產品」的客戶。這些能力（心跳/Watchdog、高可用、合成探測）業界本來就有，所以該做到哪、門檻是**被取代的既有產品設下的標準**，不是我們內部的成熟度。據此每項拆兩半：**評估階段就要拿得出的可信設計 + 一個能跑的範例（便宜，值得先做）** vs **真正營運才需要的常駐元件（昂貴，等明確觸發條件再做）**；客戶**已經有**的能力一律走**對接、不重造**。

| 項目 | 一句話 | 觸發條件 |
|---|---|---|
| **Canary 租戶（runtime）** — **設計就緒** | 常駐假租戶 + 必觸發 dead-man's-switch（`CustomAlertPipelineCanaryDown`），抓 Watchdog 看不到的 exporter / 編譯管線靜默死亡（完整設計、壞租戶隔離兩層說明與範例見 [Runtime Canary 設計](../design/runtime-canary.md)） | **常駐部署**延後：重大規則編譯重構 / 多租戶路由大改前先佈署當安全網，或首個正式環境「告警評估悄悄失敗」事件後佈署防再犯 |
| **端到端合成探測（平台自建探針）** | interop 對接面（sinkhole route）**已落地**（見下）；仍 defer 的是「**平台主動發**一條合成告警走完 Prometheus→Alertmanager→外部」的自建探針——多半 interop 即足夠、未必要做 | 心跳 + canary 上線後出現「規則評估悄悄失敗」事件 |
| **後端相容性 — staleness / 時間語意** | 驗規則在客戶後端上的**時間相關**語意（staleness marker、gap 上的 `absence`、`predict_linear` 外插）正確——需真實時間軸 gap，非 dense fixture 測得出 | 首個客戶整合到自有後端 |

> **兩個被否決的子方案（記此免重走）**：
> - **後端相容性不採用 `promql-compliance-tester`**：它跑的是固定通用 PromQL 題庫、需約 1 小時 scrape 資料、且不能離線，對不上我們的需求——我們要驗的是**自己編譯產出**的 idiom（`and on()` / `group_left` / `max by` …）。改用一個薄 harness、復用既有 promtool golden 當參考即可（見 [backend-compat-baseline.md](../internal/backend-compat-baseline.md)）。
> - **canary 不做 CI-gate 變體**：曾評估把合成 `absence` fixture 餵進「編譯器 + promtool + amtool」當 CI 閘門，但它與既有 absence 測試、Go 標籤契約、路由 orchestration 測試重疊約九成，且 CI 沒有 exporter、得自行合成 series——反而把要驗的端到端**砍小**成 CI fixture 自身的性質。保留的是**常駐 runtime canary**（抓 exporter / 編譯管線的靜默死亡，[#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731) 類盲點），見上表與 [Runtime Canary 設計](../design/runtime-canary.md)。

## 範圍邊界

| 這份 ADR 管 | 不在這份 |
|---|---|
| 監控平面（Prometheus + Alertmanager + 到外部心跳的路由）自我存活 | 租戶側存活（租戶用 `absence` 顧自己指標，見 value-form cookbook） |
| 平台 operator 視角 | 資料平面高可用（已有設計） |

## 後果

- **正面**：用「一條規則 + 一條路由 + 一個路由測試」、零新增元件，補上「告警系統自己死掉沒人知道」的盲點；與「儲存後端中立」定位一致，不和客戶後端打架。
- **負面**：
    - 外部心跳是 operator 要自備的依賴（斷網退路＝被動探測）。
    - 心跳只能證明「引擎還活著」，不能證明「規則評估正確」（→ 留給 runtime canary）。
    - 單副本示範部署下，真出事仍需人工復原（高可用是 operator 的責任）。

## 相關

- value-form cookbook 收尾：[#832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/832)——租戶側存活性所在，與本 ADR 不同平面。
- 資料平面高可用設計：[高可用性設計](../design/high-availability.md)（互補）。
- 既有的隔離式告警路由（Alertmanager 設定中的租戶自訂告警專線）可作為 Watchdog 路由範本。
