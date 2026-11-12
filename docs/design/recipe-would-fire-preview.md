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
> 本文的設計已實作：服務在 `components/recipe-preview/`。portal 原本經 da-portal 的反向代理（`portal.recipePreviewUrl`）呼叫它；那條路已隨 [ADR-027 §5](../adr/027-tenant-api-caller-identity.md) 移除。現在它是 tenant-api pod 裡的容器（tenant-api chart 的 `recipePreview`，預設開啟），portal 呼叫 `POST /api/v1/tenants/{id}/recipe-preview`，tenant-api 授權後經 pod 內 Unix socket 轉給它；獨立的 `helm/recipe-preview` chart 已刪除。映像隨 `tools/v*` 建置，下一個 tools release 才有第一個映像，在那之前 try-local 是唯一能跑的地方。本文聚焦兩個決策：**後端用獨立服務**、**合成輸入怎麼餵**。
> - **已定案**：後端形態（獨立 Python 服務、先上 try-local）、API 契約、生產護欄。
> - **首版範圍**：threshold 類 recipe（`>` `>=` `<` `<=` `==`）+ absence（缺口偵測——合成序列「不發該指標」即缺口）。
> - **延後**：其餘 recipe 型別、歷史回測（各項觸發條件見第 9 節）。正式環境部署原本也在這裡，已隨 #1040 落地。

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

**決策**：預覽後端是一支**獨立的 Python 服務**，內含編譯器 + `promtool`，先放進 try-local（本機 docker-compose 試用環境）。當初正式環境部署延後；後來已隨 #1040 落地，形態是 tenant-api pod 裡的容器（見本文開頭）。

為什麼是獨立服務，而不是塞進現有的 tenant-api：

| 方案 | 好處 | 代價 |
|---|---|---|
| **A. 獨立 Python 服務（採用）** | 同為 Python，可**直接呼叫編譯器**（見 5.3），零跨語言重寫；`promtool` 不必塞進正式環境的核心 image；萬一評估卡住，影響只侷限在這支服務 | 當初：要自己的 nginx 路由 + 認證、正式環境是一條新部署 → 先只上 try-local。現在：不需要自己的路由與認證——它是 tenant-api pod 裡的容器，tenant-api 授權後經 pod 內 Unix socket 呼叫（#1040） |
| B. 擴充現有 tenant-api（Go 呼叫外部程式） | 重用 tenant-api 既有的路由 + 認證；正式環境一次到位 | image 變肥；把「評估」耦進「寫入」的關鍵路徑；長駐服務裡反覆開子程序有併發風險 |

選 A 一句話：平台一向把正式環境保持精簡、把故障影響侷限化，而預覽近期的使用者就在本機試用階段——沒必要為此把評估耦進正式環境的寫入路徑。（A／B 完整比較見 [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) 討論串。）

## 4. API 契約

portal 呼叫的是 tenant-api 的端點（[ADR-027 §5](../adr/027-tenant-api-caller-identity.md)）；租戶在路徑上：

```http
POST /api/v1/tenants/{id}/recipe-preview
```

**Request**

```json
{
  "recipe":   { "...": "ADR-024 recipe 物件（同 portal 表單產出）" },
  "scenario": { "value": 1500 }
}
```

tenant-api 授權後，經 pod 內 Unix socket 把 `{recipe, tenant, scenario, deadline_ms}` 交給預覽服務（`POST /v1/preview`；`deadline_ms` 是這次請求剩下的時間）。

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

授權在 tenant-api：路由的讀取權限檢查（對路徑上的租戶，含組織範圍）通過才問預覽服務，否則回 403——否則「評自己的 recipe」會退化成跨租戶的查詢面。預覽服務在 tenant-api 的 pod 裡、只在共用 volume 的 Unix socket 上服務（只回應同 uid 的程序），**不讀任何身分、不再自己查授權**（[ADR-027 §5](../adr/027-tenant-api-caller-identity.md)）。try-local 走同一拓撲，身分由 tenant-api 的 dev-bypass 注入（[ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md)）。`recipe` / `scenario` 是使用者輸入：服務要先做格式驗證（或捕捉編譯器拋出的設定錯誤），**驗過才編譯**（見 5.2）：body 型別不對（`recipe`／`scenario` 不是 object）回 400；內容缺欄位或編譯失敗回 `state: error`。

## 5. 後端如何算出狀態

分三步：**①怎麼把測試情境變成 `promtool` 看得懂的輸入、②怎麼用 `promtool` 判定觸發、③怎麼拿到編譯器算出的規則**。

### 5.1 合成輸入：要餵的是「規則實際比對的那組序列」，不是一個數字

Prometheus 規則比對的是「序列」（series，帶標籤的時間序列），不是單一數字。所以預覽要餵的不是一個值，而是規則評估時實際會用到的那組序列。以 threshold 為例（已對照 `tests/dx/fixtures/custom_alerts_promtool/threshold.yaml` 驗證），最小可觸發只需三條（下表 `@` 後的數字表示「整段時間都固定為這個值」）：

| 序列 | 內容 | 來源 |
|---|---|---|
| 觀測指標 `@` 測試值 | 使用者填的測試值 | `scenario.value` |
| `user_threshold @ 閾值`（帶 recipe 識別字 + severity + name + mode） | 規則要比對的閾值與標籤 | 識別字直接取自編譯器（見 5.3），不另外推算 |
| `tenant_metadata_info @ 1` | 給告警的 runbook／owner annotation 查詢用（規則不 join 它，#3483） | 固定值 |

> 預覽的合成輸入**一律附上** `tenant_metadata_info`，所以預覽看到的是帶 runbook／owner 的完整告警。（真實環境若租戶缺這個，告警仍會觸發、只是這些 annotation 為空，那由執行期處理，與預覽無關。）

**關鍵分界——只有序列的「形狀」才跟 recipe 型別有關**：

- **threshold 類**：**平的常數序列**（值固定不動）。這正是 threshold 類預覽便宜的原因：一個固定值就夠，不需要斜率或趨勢。
- **absence**：缺口型——合成序列**不發該指標**即「缺口」（規則 `count_over_time(metric[window])` 抓不到樣本 → `unless` 觸發），故與 threshold 同樣可便宜預覽 → **已支援**（eval 跨過 window + `for:`）。
- **rate / ratio / forecast / p99_latency / slo_burn_rate**：需要型別專屬的形狀（rate 要斜率、ratio 要分子分母、forecast 要趨勢）→ 延後。

> 預覽回答的是「在這個值／場景下會不會觸發」，不是重新驗證規則本身的正確性（後者由既有 CI 測試保證）。所以單一測試值對 threshold 類已足夠；多副本、趨勢等多序列場景留待未來。

> **預覽答的範圍（前端對使用者明示）**：因為餵的是合成、固定的序列，預覽回答的是「**這條 recipe 的閾值邏輯在這個測試值會不會越線**」，**不是**「在你環境會不會真的發出通知」——它不模擬真實數據的走勢與雜訊、`for:` 隨時間的計時，也不含 Alertmanager 的靜默／路由。這條界線由 would-fire 面板的常駐註記告知使用者，避免「邏輯會觸發」被讀成「我的告警會響」。

### 5.2 評估機制：用反證法讓 `promtool` 告訴你會不會觸發

`promtool test rules` 是個**斷言**工具——你給它「預期觸發哪些告警」，它幫你比對；但它不會主動「回報誰觸發」，而預覽正是不知道答案。所以反過來用：同一份合成輸入寫成**兩組斷言相反的測試**，看哪一組不成立。

- `fires` 組：斷言這條告警**有**觸發（`ALERTS{alertname=…, alertstate="firing"}` 存在）。
- `quiet` 組：斷言它**沒有**觸發，同時斷言服務自己加進規則檔的一條紀錄規則（`da_preview_loaded`）存在——證明規則檔確實有被載入、有被評估。

結果一律從 `promtool test rules --junit` 的報告讀（每組一個 testcase），不讀主控台文字（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)）：

| `fires` 組 | `quiet` 組 | 判定 |
|---|---|---|
| 成立 | 不成立 | `firing` |
| 不成立 | 成立 | `inactive` |
| 其他（兩組都不成立、都成立、報告有 `<error>`、組數不對、結束碼與報告不一致、沒有報告） | | `error`（**絕不可當成觸發或沒觸發**） |

為什麼要兩組：`promtool` 把組內的**任何**錯誤——輸入序列讀不了、規則載入失敗、規則評估出錯——都記成和「斷言不成立」一模一樣的 `<failure>`。只有一組時，「沒跑成」會被讀成「觸發了」。兩組斷言相反：真的評估過，恰好一組不成立；沒能評估，兩組都不成立。規則檔沒被載入時 `promtool` 不報錯（它只是沒有規則可跑），這由 `quiet` 組的載入證明擋下。

三個必守的細節（不照做就會算錯）：

1. **評估時間點必須大於 recipe 的 `for:` 視窗。** 告警在滿足 `for:` 之前處於「待定」（pending，尚未真正觸發），兩組都只看 `alertstate="firing"` → 時間點太早會被判成 `inactive`（明明已越過閾值）。所以合成測試的評估時間要**嚴格大於 `for:`**（例如 `for: 30m` → 評估設 35m），序列也要長到跨過 `for:`。
2. **測試值只收有限的 JSON 數字。** 字串（連 `"1500"` 也是）、布林、`NaN`、`Infinity` 在呼叫 `promtool` 前就判 `error`；寫進序列時用與閾值相同的正規化格式。`promtool` 的序列語法與 Python 的 `float()` 不同：`"1500\n"` 會被讀成只有一個樣本，到評估時已過期，若照收會誤判成 `inactive`。
3. **兩組的名稱與判定表是同一份定義**（`_recipe_preview._FIRES`／`_QUIET`／`_VERDICT`），規則檔名也只寫一次，寫測試的一方與讀報告的一方不會各說各話。

為了讓「編譯失敗」永遠不會被誤標成「觸發」，分三層把關：① exporter 拒收的 recipe（沒有送出的門檻列）→ `error`，附 exporter 的原因；② 編出的規則先過 `promtool check rules`（看結束碼）→ 失敗即 `error`；③ 語法過了，才跑上面的兩組斷言。

> 本機已實測（promtool 3.15.1）：值 1500 > 閾值 1000 → `fires` 成立、`quiet` 不成立；值 500 → 反過來；輸入讀不了或規則評估出錯 → 兩組都不成立。整套用既有工具即可，不需要另起一個 Prometheus 實例。

### 5.3 單一 recipe 編譯 + 直接重用編譯器

服務把表單那一條 recipe 寫成一棵只有一個租戶的暫存 conf.d，交給部署時用的同一個 da-guard：`served-values` 給出 exporter 的判定與它會送出的 `user_threshold` 列（標籤、severity、值），`compile-custom-alerts` 給出規則與 recipe 識別字（[#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229)）。服務映像因此帶 da-guard。合成序列的閾值列就是 exporter 送出的那一列，`for:` 取自編好的規則——不另外用正則或在 Python 重推，所以 exporter 拒收的 recipe 不會被預覽成會觸發。單獨一條 recipe 編出的規則，正是「你宣告這條 recipe 會長這樣」，恰好是預覽要的。

## 6. 生產護欄

`promtool` 每次評估會開一個約 1 秒的子程序；預覽服務每個請求都會開一個，所以需要：

1. **併發上限**——限制同時開的數量，滿了排隊；排隊逾時（`PREVIEW_QUEUE_TIMEOUT`）回 503。
2. **單一請求逾時**——`promtool` 逾時即終止、回 `error`。
3. **速率限制**——每租戶限流，避免被當成攻擊面。
4. **互動設計**——因為約 1 秒延遲，不做即時連發；用手動「執行預覽」按鈕 + 載入中狀態。
5. **`promtool` 版本鎖**——上面的 returncode／輸出格式是跟版本綁的契約（marker 由 `ci.yml` 的 `Preview would-fire e2e` 一步對當下 pin 的版本每次重驗）；服務 image 要鎖版本、啟動時記錄版本。

## 7. 誠實標示尚未支援的型別

首版支援 threshold 與 absence 類。其餘型別**不可靜默**——portal 對未支援型別要明確顯示「此型別的預覽即將推出」，而不是裝作能算或留白。

理由：若使用者**存得進**一條 ratio recipe 卻**看不到**預覽，他會以為「存了就對」——這是錯誤的信心。所以「閉環」是**逐型別宣告**的，不會因為支援了 threshold 就宣稱全部完成。

機制：服務硬編支援清單（`scripts/tools/dx/_recipe_preview.py` 的 `SUPPORTED_RECIPES_MVP`）；不在清單內就直接回 `supported: false` + 說明、**不嘗試編譯**——所以未支援型別永遠不會被誤標成 `firing` 或 `error`。

## 8. 分階段交付

| 階段 | 範圍 | 狀態 |
|---|---|---|
| **設計（本文）** | 後端形態 + 契約定案 + 護欄 + 合成輸入設計；扁平工具補 recipe 提示 | ✅ |
| **首版實作** | threshold 類：獨立 Python 服務（try-local）+ 合成序列產生器 + portal 表單渲染 + 逐型別放行 | ✅ |
| **absence 型** | 缺口型：合成序列不發指標即缺口（與 threshold 同樣便宜） | ✅ |
| **其餘型別** | `SUPPORTED_RECIPES_MVP` 以外的 recipe：型別專屬序列產生器 + 場景模型 + 逐型別開放 | 延後（見第 9 節） |

## 9. 延後項目（每項都有觸發條件，不是模糊的 TODO）

| 延後項 | 觸發條件 |
|---|---|
| 其餘 recipe 型別 | 領域專家／租戶實際需要這些型別的預覽 |
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
