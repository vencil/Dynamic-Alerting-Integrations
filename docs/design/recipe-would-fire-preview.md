---
title: "Recipe 預覽設計 — 填完當場確認告警會不會觸發"
tags: [architecture, alerting, custom-alerts, recipe, would-fire, preview, design]
audience: [platform-engineer, domain-expert, sre]
version: v2.9.0
lang: zh
parent: architecture-and-design.md
---
# Recipe 預覽設計

> **Language / 語言：** **中文 (Current)** | [English](./recipe-would-fire-preview.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)
>
> **相關**：[ADR-024 自訂告警](../adr/024-version-aware-threshold-via-dimensional-label.md)，追蹤 issue [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657)。recipe 是租戶在 portal 自助定義的告警規則；本文設計的是「填完當場確認它會不會觸發」的預覽。
>
> 本文是**設計就緒**產出（設計與契約定案、尚未實作），聚焦兩個決策：**後端用獨立服務**、**合成輸入怎麼餵**。
> - **已定案**：後端形態（獨立 Python 服務、先上 try-local）、API 契約、生產護欄。
> - **首版範圍**：threshold 類 recipe（`>` `>=` `<` `<=` `==`）+ absence（缺口偵測——合成序列「不發該指標」即缺口）。
> - **延後**：正式環境部署、其餘時間相依型 recipe（rate / ratio / forecast / p99）、歷史回測（各項觸發條件見第 9 節）。

## 1. 要解決的問題

租戶／領域專家在 portal 的表單填完一條 recipe → 直接寫入 → git commit，全程不碰 PromQL。**唯一還得跨出這個畫面的一步是「驗證」**：填完後，得另外去 Grafana 看 `ALERTS`、或先把 recipe 設成靜默模式觀察一陣，才知道它會不會如預期觸發。

本設計把這個驗證收回**同一個表單**（以下稱「預覽」）：填完當場按一下，就看到「會觸發／不會觸發」，不用離開畫面。

## 2. 核心原則：重用既有的評估引擎，絕不另寫一份

平台已經有一條把 recipe 編譯成 Prometheus 規則、再用 `promtool` 驗證的管線。預覽**只呼叫這條既有管線**，不在前端或別處另寫一份比較邏輯。

| 規則類 | 權威評估引擎 | 狀態 |
|---|---|---|
| 扁平閾值 | `backtest_threshold.py` | 已建；本次補上「遇 recipe 會明確提示、不再靜默略過」 |
| 自訂告警 recipe | 編譯器 `compile_custom_alerts.py` + `promtool` | 引擎與測試夾具皆已建；本設計把它接成預覽 |

**為什麼不抄捷徑？** threshold 看起來「不就是 `值 {運算子} 閾值`」，很想在前端用 JavaScript 比一下就好——但編譯器的真實語意還包含這四件事：

- 版本不相符時的閾值降級（先找精確版本、找不到才退回預設）；
- 維護期抑制；
- `==` 的「多副本任一匹配」；
- 附掛 runbook／owner 的標籤映射（`group_left`）。

前端另抄一份，一定會在這些情況算錯，而**預覽算錯比沒有預覽更糟**（給人錯誤的信心）。所以連最簡單的 threshold 也走真正的編譯器 + `promtool`。（「一個規則類只有一個權威引擎」這條原則，源自過去跨語言重寫造成規則漂移的教訓。）

## 3. 預覽後端：一支獨立的 Python 服務（先上 try-local）

**決策**：預覽後端是一支**獨立的 Python 服務**，內含編譯器 + `promtool`，先放進 try-local（本機 docker-compose 試用環境）；正式環境部署延後。（定案的是「形態與契約」，服務本身在實作階段才寫。）

為什麼是獨立服務，而不是塞進現有的 tenant-api：

| 方案 | 好處 | 代價 |
|---|---|---|
| **A. 獨立 Python 服務（採用）** | 同為 Python，可**直接呼叫編譯器**（見 5.3），零跨語言重寫；`promtool` 不必塞進正式環境的核心 image；萬一評估卡住，影響只侷限在這支服務 | 要自己的 nginx 路由 + 認證；正式環境是一條新部署 → 因此先只上 try-local |
| B. 擴充現有 tenant-api（Go 呼叫外部程式） | 重用 tenant-api 既有的路由 + 認證；正式環境一次到位 | image 變肥；把「評估」耦進「寫入」的關鍵路徑；長駐服務裡反覆開子程序有併發風險 |

選 A 一句話：平台一向把正式環境保持精簡、把故障影響侷限化，而預覽近期的使用者就在本機試用階段——沒必要為此把評估耦進正式環境的寫入路徑。（A／B 完整比較見 [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) 討論串。）

## 4. API 契約

預覽服務只暴露一個 API 端點（portal 經 nginx 轉發）：

```http
POST /preview
```

**Request**

```json
{
  "recipe":   { "...": "ADR-024 recipe 物件（同 portal 表單產出）" },
  "tenant":   "shop-a",
  "scenario": { "value": 1500 }
}
```

- 首版的 `scenario` 是單一測試值（threshold 類不需要時間序列）。
- 契約欄位向前相容：**未來**支援時間相依型時，`scenario` 可擴成「期間／趨勢」描述，甚至**逐維度的陣列**（例如每顆 PVC 一個值 `[{pvc, value}, …]`，用來示範「大碟掩蓋小碟」這類多副本場景）——這些都是後話，首版不做。

**Response**（只回狀態，不回「誰會被通知」）

```json
{
  "alertname": "Custom_threshold__order_queue_depth__gt__w5m__for1m",
  "supported": true,
  "states": [
    { "severity": "warning", "mode": "page", "state": "firing", "reason": "value 1500 > threshold 1000" }
  ],
  "warnings": []
}
```

- **三種互斥結果**：`supported: false`（此 recipe 型尚未支援，不會嘗試編譯）；`state: error`（型有支援但編譯／評估失敗或逾時）；`state: firing | inactive`（正常評估結果）。
- `for:`（recipe 設定的「需持續幾分鐘才觸發」）以說明文字呈現，不另設即時的「待定」狀態。維護期抑制、多副本等場景屬未來擴充。
- **不回「誰會被通知」**——通知路由屬於另一個元件，首版不承諾。

### 4.1 認證與租戶隔離

預覽服務**沿用 portal 的認證**：try-local 走 dev-bypass（[ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md) 四層防線），正式環境經 oauth2-proxy（與 tenant-api 同樣式）。服務**必須驗證 request 的 `tenant` 屬於登入者可存取的租戶**，否則回 403——否則「評自己的 recipe」會退化成跨租戶的查詢面。`recipe` / `scenario` 是使用者輸入：服務要先做格式驗證（或捕捉編譯器拋出的設定錯誤）→ 失敗即回 `state: error`，**驗過才編譯**（見 5.2）。

## 5. 後端如何算出狀態

分三步：**①怎麼把測試情境變成 `promtool` 看得懂的輸入、②怎麼用 `promtool` 判定觸發、③怎麼拿到編譯器算出的規則**。

### 5.1 合成輸入：要餵的是「規則實際比對的那組序列」，不是一個數字

Prometheus 規則比對的是「序列」（series，帶標籤的時間序列），不是單一數字。所以預覽要餵的不是一個值，而是規則評估時實際會用到的那組序列。以 threshold 為例（已對照 `tests/dx/fixtures/custom_alerts_promtool/threshold.yaml` 驗證），最小可觸發只需三條（下表 `@` 後的數字表示「整段時間都固定為這個值」）：

| 序列 | 內容 | 來源 |
|---|---|---|
| 觀測指標 `@` 測試值 | 使用者填的測試值 | `scenario.value` |
| `user_threshold @ 閾值`（帶 recipe 識別字 + severity + name + mode） | 規則要比對的閾值與標籤 | 識別字直接取自編譯器（見 5.3），不另外推算 |
| `tenant_metadata_info @ 1` | 給規則 `group_left` 帶入 runbook／owner 用 | 固定值 |

> 預覽的合成輸入**一律附上** `tenant_metadata_info`，所以預覽看到的是帶 runbook／owner 的完整告警。（真實環境若租戶缺這個，告警仍會觸發、只是這些標籤為空，那由執行期處理，與預覽無關。）

**關鍵分界——只有序列的「形狀」才跟 recipe 型別有關**：

- **threshold 類**：**平的常數序列**（值固定不動）。這正是 threshold 類預覽便宜的原因：一個固定值就夠，不需要斜率或趨勢。
- **absence**：缺口型——合成序列**不發該指標**即「缺口」（規則 `count_over_time(metric[window])` 抓不到樣本 → `unless` 觸發），故與 threshold 同樣可便宜預覽 → **已支援**（eval 跨過 window + `for:`）。
- **rate / ratio / forecast / p99**：需要型別專屬的形狀（rate 要斜率、ratio 要分子分母、forecast 要趨勢）→ 延後。

> 預覽回答的是「在這個值／場景下會不會觸發」，不是重新驗證規則本身的正確性（後者由既有 CI 測試保證）。所以單一測試值對 threshold 類已足夠；多副本、趨勢等多序列場景留待未來。

> **預覽答的範圍（前端對使用者明示）**：因為餵的是合成、固定的序列，預覽回答的是「**這條 recipe 的閾值邏輯在這個測試值會不會越線**」，**不是**「在你環境會不會真的發出通知」——它不模擬真實數據的走勢與雜訊、`for:` 隨時間的計時，也不含 Alertmanager 的靜默／路由。這條界線由 would-fire 面板的常駐註記告知使用者，避免「邏輯會觸發」被讀成「我的告警會響」。

### 5.2 評估機制：用反證法讓 `promtool` 告訴你會不會觸發

`promtool test rules` 是個**斷言**工具——你給它「預期觸發哪些告警」，它幫你比對；但它不會主動「回報誰觸發」，而預覽正是不知道答案。所以反過來用：餵合成輸入 + **斷言「不會觸發任何告警」**（`exp_alerts: []`），再看 `promtool` 的反應——**它沒意見就代表沒觸發、它抗議就代表有觸發**。

| `promtool` 結果 | 判定 |
|---|---|
| 成功（returncode 0） | 沒有任何告警觸發 → `inactive` |
| 失敗，且輸出含 `FAILED:` + 非空的 `got:`（實際觸發的告警） | 有告警觸發 → `firing`；該區塊直接帶出標籤與說明 |
| 失敗，但**沒有**上述匹配字樣 | 編譯／語法錯、逾時、被系統終止等 → `error`（**絕不可當成觸發**） |

兩個必守的細節（不照做就會算錯）：

1. **評估時間點必須大於 recipe 的 `for:` 視窗。** 告警在滿足 `for:` 之前處於「待定」（pending，尚未真正觸發），而「斷言不會觸發」**不會**把待定當成違反 → `promtool` 會回成功 → 預覽誤判成「不會觸發」（明明已越過閾值）。所以合成測試的評估時間要**嚴格大於 `for:`**（例如 `for: 30m` → 評估設 35m），序列也要長到跨過 `for:`。
2. **returncode 非 0 不等於「觸發」。** 記憶體不足被系統終止（OOM）、找不到 `promtool`、合成測試檔語法錯，都會讓 returncode 非 0。若盲目把「非 0」當成觸發，就會把基礎設施錯誤誤報成 `firing`。所以判定觸發**必須同時**看到失敗簽章（`FAILED:` + `got:`），否則一律當 `error` 並噴出真正的錯誤。

為了讓「編譯失敗」永遠不會被誤標成「觸發」，分三層把關：① 編譯器對壞 recipe 直接拋例外 → `error`；② 編出的規則先過 `promtool check rules`（語法）→ 失敗即 `error`；③ 語法過了，才跑上面的反證斷言。

> 本機已實測（`promtool` 3.12.0）：值 1500 > 閾值 1000 → 失敗、輸出帶完整告警；值 500 → 成功。整套用既有工具即可，不需要另起一個 Prometheus 實例。

### 5.3 單一 recipe 編譯 + 直接重用編譯器

服務是 Python，所以**直接呼叫編譯器**：把表單那一條 recipe 寫進一份暫存設定，請編譯器產生規則和它算出的 recipe 識別字。識別字是編譯器自己算的、不是另外用正則或在 Go 重推——這就是「直接重用、零跨語言重寫」在 Python 服務下自然成立的原因。單一 recipe 在隔離的暫存設定裡編出的規則，正是「你宣告這條 recipe 會長這樣」，恰好是預覽要的。

## 6. 生產護欄

`promtool` 每次評估會開一個約 1 秒的子程序；預覽服務每個請求都會開一個，所以需要：

1. **併發上限**——限制同時開的數量，滿了排隊／拒絕。
2. **單一請求逾時**——`promtool` 逾時即終止、回 `error`。
3. **速率限制**——每租戶限流，避免被當成攻擊面。
4. **互動設計**——因為約 1 秒延遲，不做即時連發；用手動「執行預覽」按鈕 + 載入中狀態。
5. **`promtool` 版本鎖**——上面的 returncode／輸出格式是跟版本綁的契約（實測基準 3.12.0）；服務 image 要鎖版本、啟動時記錄版本。

## 7. 誠實標示尚未支援的型別

首版支援 threshold 與 absence 類。其餘型別（rate / ratio / forecast / p99）**不可靜默**——portal 對未支援型別要明確顯示「此型別的預覽即將推出」，而不是裝作能算或留白。

理由：若使用者**存得進**一條 ratio recipe 卻**看不到**預覽，他會以為「存了就對」——這是錯誤的信心。所以「閉環」是**逐型別宣告**的，不會因為支援了 threshold 就宣稱全部完成。

機制：服務硬編支援清單（目前 `{threshold, absence}`）；不在清單內就直接回 `supported: false` + 說明、**不嘗試編譯**——所以未支援型別永遠不會被誤標成 `firing` 或 `error`。

## 8. 分階段交付

| 階段 | 範圍 | 狀態 |
|---|---|---|
| **設計（本文）** | 後端形態 + 契約定案 + 護欄 + 合成輸入設計；扁平工具補 recipe 提示 | 本 PR |
| **首版實作** | threshold 類：獨立 Python 服務（try-local）+ 合成序列產生器 + portal 表單渲染 + 逐型別放行 | 下一步 |
| **absence 型** | 缺口型：合成序列不發指標即缺口（與 threshold 同樣便宜） | ✅ 本次（PR-B） |
| **其餘時間相依型** | rate／ratio／forecast／p99：型別專屬序列產生器 + 場景模型 + 逐型別開放 | 延後（見第 9 節） |

## 9. 延後項目（每項都有觸發條件，不是模糊的 TODO）

| 延後項 | 觸發條件 |
|---|---|
| 正式環境部署預覽服務 | 真正的正式客戶在 portal 寫 recipe 且需要預覽（本機試用不夠時）。屆時重評部署形態 |
| 其餘時間相依型（rate／ratio／forecast／p99） | 領域專家／租戶實際需要這些型別的預覽 |
| 歷史回測（「過去 24h 我的真實資料觸發過幾次」） | recipe 的 recording rule 落地、`for:` 語意就緒 |
| rule-pack 影響矩陣 CI（改 rule-pack 前評估對全租戶的影響） | rule-pack 變更造成預期外的全租戶告警漂移，或需要合併前的影響評估。注意：預覽用合成輸入，**不需要**這條的快照資料 |
| 「誰會被通知」歸因 | 有消費者真的需要（屬通知路由元件） |
| operator 遷移回測 | operator 解除延後時 |

## 10. 為什麼說「安全」

預覽只評**使用者自己的** recipe + **合成**輸入，所以：**不碰其他租戶的資料、不拉歷史、不打正式環境的 Prometheus**。使用者就是領域專家（寫 recipe）和租戶（自己的 recipe）；跨租戶的防線在 4.1。

## 相關文件

- [ADR-024：版本感知閾值 + 自訂告警](../adr/024-version-aware-threshold-via-dimensional-label.md) — recipe 引擎本身；本設計是它的「填完即確認」最後一哩。
- [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.md) — 正式環境核心 image 不打包 `promtool` 的決定，也是預覽走獨立服務的理由之一。
- [Runtime Canary 設計](./runtime-canary.md) — 同樣是「設計就緒、部署延後」的姊妹設計。
- `scripts/tools/ops/backtest_threshold.py` — 扁平閾值的評估引擎；本次補上遇 recipe 的明確提示。
