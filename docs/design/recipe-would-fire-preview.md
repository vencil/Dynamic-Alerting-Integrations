---
title: "Recipe would-fire 預覽設計 — 自訂告警的 authoring→confidence 閉環"
tags: [architecture, alerting, custom-alerts, recipe, would-fire, preview, design]
audience: [platform-engineer, domain-expert, sre]
version: v2.9.0
lang: zh
parent: architecture-and-design.md
---
# Recipe would-fire 預覽設計

> **Language / 語言：** **中文 (Current)** | [English](./recipe-would-fire-preview.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)
>
> **相關**：[#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657)（would-fire eval spike）、[ADR-024 自訂告警](../adr/024-version-aware-threshold-via-dimensional-label.md)。本文是 #657 的 **P1 設計就緒 (design-readiness) 產出**：
> - **已鎖**：facade host（獨立 Python preview 服務、try-local 先行）、API 契約、三道護欄。
> - **提案中（本文凍結、待審）**：MVP 範圍為 threshold/equals 兩型。
> - **defer（觸發條件見 §9）**：prod 部署、時間相依型 recipe、歷史回測。

## 1. 它補的盲點：最後一個 plane-switch

[#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692) 的靈魂是 **simplicity**——domain/tenant 不切平面、不寫 PromQL。authoring 這側已經是單平面：portal recipe modal → tenant-api → git commit，全程不碰 YAML / PromQL。

唯一還跨平面的是 **confidence**。寫完一條 recipe，要**離開 modal**、去 Grafana 看 `ALERTS`、或先掛 `mode: silent` 觀察一陣，才知道它會不會如預期 fire。

本設計把 would-fire 信心收回**同一個 modal**：填完 recipe，當場看到「會 fire / 不會 fire」，**零平面切換**。這是 #692 simplicity 承諾裡的**最後一個 plane-switch**。

## 2. 設計鐵則：兩個 eval 家，絕不重寫

| 規則類 | 權威 eval 家 | 狀態 |
|---|---|---|
| flat threshold / rule-pack | `scripts/tools/ops/backtest_threshold.py` | ✅ 已建（純量 breach；本次補了對 `_custom_alerts` 的 fail-loud） |
| custom-alert recipe (ADR-024) | 編譯器 `compile_custom_alerts.py` + `promtool` | 引擎與 golden harness 皆已建；本設計把它接成 preview |

**鐵則：eval 每個規則類只有一個權威家，所有 consumer 都呼叫它，絕不在 JS / Go / Python 重寫**（[#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731) / [#719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/719) 的跨語言 drift 教訓）。**前端笨**：不在 JS 重算 Prometheus eval；後端回 state，前端只渲染。

**禁止的捷徑。** threshold 看起來「只是 `value {op} threshold`」，誘人在 JS/Python 抄一個純量比較——**不行**。編譯器的真語意還含：version-aware exact-or-fallback、maintenance 抑制、`==` 的 any-match（[#819](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/819) 修過的 silent-miss）、`group_left` enrichment。抄捷徑會在這些情況給**錯**答案——而 preview 給錯比沒 preview 更糟（false confidence）。所以**即使最簡單的 threshold 也走 compiler+promtool**。

## 3. Facade host：獨立 Python preview 服務（try-local 先行）

**決策（已鎖）**：preview 後端 = 一支**獨立的 Python 服務**，內含編譯器 + `promtool`，先進 try-local 的 docker-compose stack；prod 部署 defer。**「已鎖」指架構與契約（P1）；服務實裝在 P2、prod 部署觸發見 §9**——不是說服務已存在。

| 方案 | 換到 | 犧牲 |
|---|---|---|
| **A. 獨立 Python 服務（採用）** | Py→Py 乾淨——facade **直接 import 編譯器**（`build_pack` / `shape.recipe_id`），原生重用、零跨語言 drift（見 §5.3）；`promtool` **不進 prod 核心 image**（守 [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.md)「prod image 不打包 promtool」）；fork 的 blast-radius 隔離；三道護欄有天然落點 | 要自己的 nginx route + auth；prod = 新部署 / 新版號線 → try-local 先閉、prod defer |
| B. 擴 tenant-api（Go + `os/exec`） | 重用 tenant-api 既有 nginx upstream + oauth2 auth + exec 樣式；prod 環 day-1 就閉 | image 膨脹、把 eval 耦進 authoring **寫入路徑**、長駐 HA 服務的 subprocess 併發風險 |

選 A 的理由：本平台一向偏 **fail-isolation**、最小 data-plane image（#448）、portal **demo-by-default**；且 recipe preview 近期受眾是 onboarding / 評估（try-local）。把 eval 耦進 prod 寫入路徑，正是要避開的 blast-radius。

> 方案 A vs B 的完整對抗式評估（3-lens review）置於 #657 comment；本 repo 只留 operative 決策。

## 4. API 契約（凍結）

preview 服務暴露單一 endpoint（portal 經 nginx route 轉發）：

```
POST /preview
```

**Request**

```json
{
  "recipe":   { "...": "ADR-024 recipe object（同 portal recipe builder 產出）" },
  "tenant":   "shop-a",
  "scenario": { "value": 1500 }
}
```

- MVP 的 `scenario` = 單一測試值（threshold/equals 不需時序）。
- P3 把 `scenario` 擴成時序模型（見 §5.1），契約欄位前向相容。

**Response（`state-only`，no route）**

```json
{
  "alertname": "Custom_threshold__order_queue_depth__gt__w5m__for1m",
  "supported": true,
  "states": [
    { "severity": "warning", "mode": "page", "state": "firing", "reason": "1500 > 1000" }
  ],
  "warnings": []
}
```

- **三種互斥結果**：`supported: false`（recipe 型尚未支援 → **不嘗試編譯**，見 §7）；`state: error`（型有支援但編譯 / eval 失敗或逾時，見 §5.2）；`state: firing | inactive`（乾淨 eval 結果）。
- `for:` 以「需持續 N 分鐘才觸發」當 context 文字呈現，**不另設 `pending` live 狀態**；`suppressed`（maintenance / silent）與多副本場景屬未來的 scenario 擴充（見 §5.1、§9）。
- **不回 route / 誰被 page**——那屬四層路由的另一元件，MVP 不承諾（§9 defer）。

### 4.1 Auth 與租戶隔離

preview 服務**繼承 portal 的 auth**：try-local 走 `--dev-bypass-auth`（[ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md) 四層防線），prod 經 oauth2-proxy（與 tenant-api 同樣式）。facade **必須驗證 request 的 `tenant` 屬於已驗證身分可存取的租戶**，否則 403——否則「評自己的 recipe」會退化成跨租戶面（這是 §10「安全」的**前提**，非自動成立）。`recipe` / `scenario` 為使用者輸入：facade 須先 schema 驗證（或捕捉 `build_pack` 拋的 `CustomAlertConfigError`）→ 失敗回 `state: error`，**驗過才編譯**（見 §5.2）。

## 5. 後端如何算出 state：合成輸入 + eval 機制

### 5.1 合成輸入：label-correct graph，不是「滑桿值展開」

要餵 `promtool` 的不是一個值，是一張 **label-correct 的依賴圖**。以 threshold 為例（已驗 `tests/dx/fixtures/custom_alerts_promtool/threshold.yaml`），最小可觸發只需 3 條序列：

| series | 內容 | 來源 |
|---|---|---|
| 觀測 metric `@` 測試值 | 使用者填的測試值 | `scenario.value` |
| `user_threshold @ 閾值`，帶 `recipe_id` slug + `severity` + `name` + `mode` | 閾值與標籤 | `recipe_id` slug 來自編譯器自身（見 §5.3），不另推 |
| `tenant_metadata_info @ 1` | enrichment | `group_left` join 用 |

> preview 的合成輸入**一律附上** `tenant_metadata_info`，故預覽到的是 enriched alert；真實執行期租戶若缺 metadata，告警仍 fire 但 runbook / owner 標籤為空——屬 ADR-024 執行期關注，與 preview 正交。

**關鍵分界**——序列的「**形狀**」才是 recipe-type 相依的：

- **threshold / equals**：**flat 常數序列**（如 `1500x48`）。`for:` 靠序列長度滿足，無斜率 / 趨勢 → **MVP 範圍**。
- **rate / ratio / forecast / absence**：需 recipe-type-aware 形狀（rate=斜率、ratio=分子+分母、forecast=趨勢+lookback、absence=缺口）→ **P3 defer**。

> Gemini 的「Time-Vector 護欄」（單一值餵不動 `for:` / `rate` / `forecast`）只打在**時間相依型**；threshold/equals 不受影響——這正是 MVP 能便宜閉環的原因。

> **preview 評的是「場景」，不是重測規則正確性。** 例如 `==` 的多副本 any-match（#819）正確性由 CI golden 保證；preview 只回答「在這個值/場景下會不會 fire」。單一測試值對 threshold/equals 的 preview 問題是**正確且足夠**的；多序列場景（多副本、趨勢）屬未來的 scenario-model 擴充。

### 5.2 Eval 機制：inverted-assert probe（已實測 promtool 2.53.2）

`promtool test rules` 是**斷言**工具（比對 `exp_alerts`），不是「回報誰 fire」的 eval 工具——而 preview 並不知道答案。解法是**反向斷言**：合成輸入 + **`exp_alerts: []`（宣稱不會 fire）**，再讀 `promtool` 結果：

| promtool 結果 | 判定 |
|---|---|
| `returncode == 0`（SUCCESS） | 沒 fire → `inactive` |
| `returncode != 0`（FAILED） | 有 fire → `firing`；mismatch 的 "got" 區塊**直接帶出實際 alert**（labels + annotations + severity） |

實測（example pack + threshold golden、`exp_alerts` 翻成 `[]`）：value 1500 > 1000 → `rc=1` 且輸出含完整 alert（`value 1500.00 crossed…` + owner/tier/runbook）；value 500 → `rc=0`。所以 **fire/no-fire 走 returncode（穩健、不靠脆弱字串解析）**；per-severity 與標籤明細從 "got" 區塊取，或對每個宣告的 severity 各跑一次 probe。**不需** throwaway Prometheus（此為外審原始疑慮，已被實測推翻）。

**錯誤 ≠ firing（fail-loud）**：`rc≠0` 必須能分辨「真 fire」與「規則沒編成 / promtool 語法錯」，否則把錯誤誤標成 firing（= §7 要避免的 false confidence）。故分三層：① `build_pack` 對 bad recipe 拋 `CustomAlertConfigError` → `state: error`；② 對編出的 pack 先跑 `promtool check rules`（語法 gate，既有 test 已用）→ 失敗 → `state: error`；③ **語法驗過後**才跑 `promtool test rules` 的 inverted-assert，此時 `rc≠0` 才穩定等於「fire」。

### 5.3 單一 recipe 編譯 + Python 原生重用

facade 是 Python，故**直接 import 編譯器**：把 modal 的單一 recipe 寫進一份 temp `conf.d`（含最小 `_defaults.yaml`）→ `compile_custom_alerts.build_pack(temp_dir)` → 取得規則與 `shape.recipe_id()` 的 slug。slug 是呼叫編譯器**同一支函式**得到的，不是 Go / 正則回推——所以「兩個 eval 家、絕不重寫」在 Python facade 下是**原生達成、零跨語言 drift**（也是選方案 A 的附帶好處）。單一 recipe 在隔離 temp tree 編出的規則，正是「若你宣告這條 recipe，會長這樣」——恰好是 preview 要的。

## 6. 三道生產護欄

`promtool` 是 ~1s 的 subprocess fork（#655 實測量級；ADR-024 §5 已明載 prod 不打包它）。preview 服務每請求 fork，故需護欄：

1. **concurrency cap**——限制同時 fork 數，滿了排隊 / 拒絕（防 fork 風暴）。
2. **per-request timeout**——`promtool` 逾時即殺、回 `state: error`（防 zombie / hang）。
3. **rate-limit**——per-tenant 限流（防被當 DoS 面）。
4. **UX 反推**——因為 ~1s fork，**不做即時滑桿連發**：手動「Run preview」按鈕 + loading state。此妥協由「不重寫 eval（嚴守 promtool）」原則反推而來。
5. **promtool 版本鎖**——inverted-assert 的 returncode / 輸出格式是**版本相依的契約**（實測基準 2.53.2）；facade image 須 pin promtool 版本、啟動時 log `promtool --version`。

## 7. 誠實的 per-type gating（避免 false confidence）

MVP 只支援 threshold/equals 的 preview。其餘型**不可靜默**——portal 對未支援型明確顯示「此 recipe 型的 would-fire 預覽即將支援」，而非裝作能算或留白。

理由：若 portal 讓使用者**存了** ratio recipe 卻**沒** preview，使用者會以為「存了就對」= false confidence（違反 fail-loud）。所以 loop-closure 是**逐型宣告**的，**不從 threshold-only 宣稱全閉環**。

**機制**：facade 硬編 `SUPPORTED_RECIPES_MVP = {threshold, equals}`；型別不在內 → 直接回 `supported: false` + warning、**不嘗試編譯**——故 unsupported 型永遠不會被誤標成 `firing` 或 `error`（與 §5.2 的錯誤路徑互斥）。

## 8. 分階段交付

| Phase | 範圍 | 狀態 |
|---|---|---|
| **P1**（本文） | facade host + 契約凍結 + 護欄 + 合成輸入設計；flat 工具補 `_custom_alerts` fail-loud | ← 本 PR |
| **P2** | threshold/equals MVP——獨立 Python preview 服務（try-local）+ flat 序列產生器 + portal modal renderer（資料源無關）+ per-type gating | next |
| **P3** | time-vector 型——recipe-type-aware 序列產生器 + scenario-model UX + 逐型翻開 gating | defer（§9） |

## 9. Defer-with-trigger（每條給觸發條件，非模糊 TODO）

| 延後項 | 觸發條件 |
|---|---|
| **prod 部署 preview 服務** | 真 prod 客戶在 portal authoring 且要 preview（try-local / onboarding 不夠用時）。屆時重評 host：獨立部署 vs 折進既有服務 |
| **P3 time-vector 型**（rate/ratio/forecast/absence） | domain/tenant 實際要這些型的 preview |
| **B2 歷史回測**（「過去 24h 我真資料 fire 幾次」） | recipe 的 recording-rule 落地 + `for:` 語意就緒 |
| **A1 rule-pack matrix-impact CI**（+ 快照 pipeline） | rule-pack 變更造成預期外的全租戶告警漂移 / SRE 要 pre-merge blast-radius。**注意**：recipe preview 用合成輸入，**不需**快照 pipeline；快照只屬 A1 |
| **route attribution**（誰被 page） | consumer 真需要（屬四層路由元件） |
| **A2 operator 遷移 PR backtest** | operator（#692）解除 defer |

## 10. 受眾 / 隔離

recipe preview = **domain expert（authoring）+ tenant（own recipe）**：評**自己的** recipe + **合成**輸入 → **安全**——無跨租戶資料、無歷史拉取、無打 live prod-Prom。與平台向的 A1 matrix（rule-pack blast-radius、跨全租戶）受眾與隔離面截然不同。

## Cross-Reference

- [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) — 本設計的 spike 與 build-split 追蹤。
- [ADR-024: 版本感知閾值 + 自訂告警](../adr/024-version-aware-threshold-via-dimensional-label.md) — recipe 引擎；本設計是其 capability B 的 confidence last-mile。
- [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.md) — 驗證雙層、prod image 不打包 promtool → 為何 preview 走獨立服務。
- [Runtime Canary 設計](./runtime-canary.md) — 同為「設計就緒、部署 defer」的姊妹設計。
- `scripts/tools/ops/backtest_threshold.py` — flat eval 家；本次已補對 `_custom_alerts` 的 fail-loud 友善訊息（#657）。
