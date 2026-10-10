---
title: "ADR-037: config-diff 改為比較渲染結果"
tags: [adr, config-diff, blast-radius, gitops, routing, ci]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-037
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-10-10
updated_at: 2026-10-10
---

# ADR-037: config-diff 改為比較渲染結果

> **Language / 語言：** **中文 (Current)** | [English](./037-config-diff-compares-rendered-output.en.md)

## 狀態

✅ **Accepted**（2026-10-10 起草，2026-10-10 由 owner 核可）。決策內容由 owner 在 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) 拍板。

## 摘要

**問題**：`da-tools config-diff` 是 PR review 用的「這個變更影響誰」報告。它只比對每個頂層租戶檔自己寫的值；`_` 開頭的平台檔（例如所有租戶繼承的 `_defaults.yaml`、可以替個別租戶補設定的根目錄 `_platform.yaml`、定義共用路由組合的 `_routing_profiles.yaml`）與子目錄裡的檔，它整份不比對，只能把檔名列為「沒有比對」。可是平台層的變更恰好影響面最大：改一個預設的告警接收端，所有沒有自己設定路由的租戶都會跟著變。

**決定**：config-diff 不再自己讀 YAML。它在 base 與 PR 兩側各「渲染」一次 conf.d，再比較兩邊的渲染結果。「渲染」是指跑那兩支真正把 conf.d 變成線上設定的程式：

- `da-guard served-values`：exporter 實際送出的值。
- 路由產生器 `generate_alertmanager_routes.py --output-configmap`：完整的 Alertmanager 設定。

這兩支正是 [ADR-036](./036-single-parser-effective-config.md) 指定的唯一解析者，所以這裡不會出現第三種讀法。

## 背景

### 一個例子

平台把預設路由的 `group_wait` 從 30s 改成 99s（`_defaults.yaml` 的 `_routing_defaults`）。實際影響：沒有自己 `_routing`、也沒有綁 routing profile 的租戶，告警的等待時間都變了。

目前的 config-diff 對這個變更只能說「`_defaults.yaml` 有變，但沒有比對」，說不出影響了誰。平台 CI 另有一支 blast-radius 報告，它讀的是每個租戶合併後的設定。那份合併結果看得到 `defaults:` 裡的值，但依 [ADR-017](./017-defaults-yaml-inheritance-dual-hash.md) 不含與 `defaults:` 同層的鍵（`_routing_defaults` 就是其中之一），所以這個變更它也看不到。

### 兩支渲染程式各看得到什麼

在 repo 的 `components/threshold-exporter/config/conf.d` 上，每次只改一處，比較兩側渲染結果有沒有差異（2026-10-10 實測）：

下表的 served-values 指 `da-guard served-values`：它輸出 exporter 對每個租戶實際送出的值，是 exporter 解析程式的離線版本。

| 變更 | served-values | 路由產生器 |
|---|---|---|
| `_routing_defaults` 的接收端或 `group_wait` | 不變 | 變 |
| `_routing_enforced`（平台強制的額外通道）打開 | 不變 | 變 |
| `defaults:` 裡的閾值 | 變（只有沒覆寫該鍵的租戶） | 不變 |
| `state_filters` 的 severity | **不變** | **不變** |
| 只改註解 | 不變 | 不變 |

最後一列是對照組，代表比較方法不會被格式變動誤導。`state_filters` 那一列是 served-values 的缺口：exporter 的 `/metrics` 會送出 `user_state_filter{tenant,filter,severity}`，但 served-values 的 JSON 只寫「這個 filter 開或關」，沒有 severity。這個缺口在本決策裡補上（決定 5）。

兩支程式合起來，才涵蓋平台層大部分的設定；任何一支單獨都不夠。

## 決定

### 1. 比較什麼

| 面向 | 渲染來源 | 比較的內容 |
|---|---|---|
| exporter 送出的值 | `da-guard served-values --at <base commit 時間> --schedules` | 每個租戶的值、severity、被丟掉或沒送出的鍵、state filter 的 severity |
| 路由 | 路由產生器 `--output-configmap` 取出的完整 Alertmanager 設定 | route 樹的每個節點、receiver 的定義、inhibit rules |

- 兩側用**同一版**工具渲染（這次 CI 跑的那一版），所以差異只反映設定的差異，不會混進工具改版造成的輸出形狀變化。
- served-values 的 `--at` 固定為 base commit 的時間，並印在報告上。不固定的話，輸出裡的時間戳每次不同，連只改註解都會變成差異。`--schedules` 讓排程生效的時段也納入比較，不只看單一時間點。
- route 節點的身分是「從根到它的 matcher 路徑」，比較的欄位是 receiver、`group_by`、`group_wait`、`group_interval`、`repeat_interval`、`continue`。matcher 是 Alertmanager 用來決定告警走哪條路的條件，例如 `tenant="db-a"`。用 matcher 當身分，不依賴產生器替 receiver 取的名字。
- receiver 定義裡的憑證欄位（webhook URL、token、密碼）比較時只報「有變」，前後值都遮罩，因為報告會貼成 PR 留言。

### 2. 差異歸給誰

- route 節點的 matcher 路徑上有 `tenant="X"`，就歸給租戶 X。
- 沒有租戶 matcher 的節點（例如 `_routing_enforced` 產生的平台通道），歸為「平台級」。
- receiver 定義有變，就歸給所有指向這個 receiver 的 route 所屬的租戶。多個租戶共用同一個 receiver 時，每個租戶各算一筆。
- exporter 值的差異按 served-values 的租戶歸屬。

### 3. 沒比對到的要說出來

渲染結果只涵蓋上表那些設定。以下把 `_` 開頭檔案裡的每一個頂層鍵（`defaults`、`_routing_defaults`、`state_filters` 等）稱為一個「載體」：它承載一類平台設定。對兩側每個 `_` 開頭的檔，比較每個載體的值（只比較值是否相等，不解讀意義），再對照一張「載體 → 由哪個面向涵蓋、涵蓋得完不完整」的表：

- 有變、而且有面向涵蓋：照常比較渲染結果。
- 有變、但沒有面向涵蓋：在報告裡具名列為「沒有比對」，結束碼 1。
- 有變、表上標為**完整涵蓋**，但渲染結果沒有差異：這是真的沒有效果，例如改了一個所有租戶都自己覆寫的預設值。報告印一行「值變了，但渲染結果沒有差異」，不影響結束碼。
- 有變、表上標為**部分涵蓋**（已知渲染程式看不到其中一部分），而渲染結果沒有差異：列出並寫明「可能沒有效果，也可能落在比較看不到的部分」，結束碼 1。這一條防的正是 `state_filters` 補上 severity 之前那種缺口：寧可多列一行，不說「沒有變更」。
- 表上沒有的新鍵：一律當成沒有涵蓋。

這張表靠 CI 的自我測試維持：每個載體在範例 conf.d 上改一次，斷言表上宣稱涵蓋它的面向真的出現差異；標為完整涵蓋的載體，還要對它的每個子鍵各改一次。渲染程式改版而失去某個載體或子鍵時，這個測試會先紅。

目前沒有涵蓋、延後處理的載體與觸發條件：

| 載體 | 延後理由 | 什麼時候做 |
|---|---|---|
| 平台層的 `_custom_alerts`（`_` 檔頂層的 recipe） | 由 custom-alert 編譯器讀，不在兩支渲染程式裡 | 編譯器有可比較的輸出，或第一次出現漏報 |
| `instance_tenant_mapping` | 由 tenant mapping 產生器讀 | 該產生器有穩定的輸出格式 |
| `max_metrics_per_tenant` | served-values 不輸出上限值本身 | 有人需要時 |
| `domain_policies` | 路由產生器的 `--validate --strict` 已在 CI 擋下違規 | 需要看「新增了哪些違規」時，讀產生器的 `--findings-json` |

`domain_policies` 有其他檢查把關，所以只列為提示，不影響結束碼。

### 4. 結束碼與報告

結束碼的意義不變：0 沒有變更；1 有變更，或有變更但沒比對到；2 渲染失敗或輸入讀不到，報告寫明「沒有計算」，不得退化成「沒有變更」。

報告依「同一個變更」分組：一筆平台變更列出受影響的租戶數與前幾個租戶名，不逐一展開上千行；整份報告仍受 PR 留言的長度上限約束。JSON 輸出改用新的結構，頂層帶 `schema` 版本，讀取端遇到不認得的版本就拒收。

### 5. served-values 補上 state filter 的 severity

served-values 每個租戶加一個與 `values` 同層的欄位，記錄每個開啟中的 state filter 的 severity；頂層加 `schema` 版本，讀取端改為嚴格檢查。

不改 `values` 裡 `_state_<filter>` 的型別（目前是 true／false）：有讀取端以「等於 true」判斷維護模式，改型別會讓它悄悄失效。也不放進既有的 `severities` 欄位：多個讀取端把那個欄位的鍵當成「有輸出的閾值鍵」清單。

### 6. 上線方式

1. 先在平台 repo 的 CI 以**只留言、不擋 merge** 的方式運作，掛在 `guard-defaults-impact` 旁邊。那支 workflow 已經在 `_` 開頭的檔變更時觸發、從 PR 的原始碼建 da-guard、用 merge-base 取得 base 側。
2. 改為必要檢查的條件：至少 20 個觸發的 PR 或 6 週（取較晚者），期間誤報 0、事後發現的漏報 0，1000 個租戶的樹在 5 分鐘內跑完。
3. 客戶端：`da-tools init` 產生的 CI 本來就呼叫 `config-diff`，tools 發版後自動改用新行為。

## 範例

輸入：在 repo 的 conf.d 上，把 `_defaults.yaml` 的 `_routing_defaults.group_wait` 從 `30s` 改成 `99s`。

路由面的比較結果：

```
route db-a -> {'group_wait': ('30s', '99s')}
route es-prod -> {'group_wait': ('30s', '99s')}
```

這個樹裡有 5 個租戶。db-b 有自己的 `_routing`，mongo-prod 與 redis-prod 綁 routing profile，所以不受影響、不出現在結果裡；db-a 與 es-prod 繼承 `_routing_defaults`，被列出。

另一個輸入：把 `defaults:` 裡的 `container_memory` 從 `85` 改成 `999`。exporter 值的比較結果：

```
{'db-a': (85, 999), 'es-prod': (85, 999), 'mongo-prod': (85, 999), 'redis-prod': (85, 999)}
```

db-b 自己把 `container_memory` 設成 90，所以不受影響。

（以上是比較程式的原始輸出，報告的排版另行設計。）

## 後果與已知限制

- **breaking change**：報告的排版與 JSON 結構都會改；解析舊 JSON 的客戶腳本要跟著改。
- **一律需要 da-guard**：da-tools 映像內建；在 repo 裡直接跑要先 `make da-guard-build`。
- **變慢**：1000 個租戶的樹，每一側渲染約 7.5 秒（產生器約 6.7 秒、served-values 約 0.8 秒），路由比較約 2.4 秒（2026-10-10 實測）。
- **現有報告的內容不得退步**：租戶閾值的 tighter／looser 分類、設定變更、custom alert 的比較，要改在 served-values 的值上重做。
- **exporter 不送出的鍵**（例如 `_namespaces`）：served-values 把它列在「沒有送出」的清單裡，變更仍比得到，但報告只能說「設定變了」，不能說「告警行為變了」。
- **matcher 完全相同的兄弟節點**：只能依出現順序對應。順序改變時，可能報成一刪一增。
- **平台的 blast-radius 報告**（`blast_radius.py`）與本決策重疊，之後另行評估是否退役。

## 考慮過但否決

| 做法 | 否決理由 |
|---|---|
| 把 `defaults:` 以外的鍵併進合併後設定與 `merged_hash`（每個租戶合併後設定的雜湊值） | `merged_hash` 一動，exporter 就重新載入；`_routing_defaults` 這類 exporter 根本不用的鍵放進去，會重現 ADR-017 要避免的大量重新載入。既有的 `merged_hash` 快照也會全部作廢 |
| 另開新工具比較平台層，保留 config-diff 現有的比對 | reviewer 會同時看到兩三份報告，對同一件事可能說法相反；客戶端還要另外接一個新的 CI 步驟 |
| 保留 config-diff 自己讀 YAML，再加一段平台層比較 | 會出現第三種讀法，違反 ADR-036 |
| 用 amtool 拿每個租戶的 label 去問「告警會送到哪」，當作主要偵測 | 實測漏掉只改 `group_wait` 的變更（receiver 名稱與定義都沒變）；以 alertname 或正規表示式分流的路由，探針沒帶到那個值就看不到。保留作為解釋用途 |
| 直接比較產生器的 `-o` 輸出，或等 ADR-036 的整理後 JSON | `-o` 的輸出形狀會隨產生器改版變動，也沒有測試釘住它的形狀；ADR-036 的 JSON 尚未落地。Alertmanager 的設定格式由上游定義，比較穩定 |
| 把 `_state_<filter>` 改成帶 severity 的物件，或放進 `severities` | 會讓現有讀取端悄悄失效（見決定 5） |
| 起一個 exporter 去抓 `/metrics` | CI 要起容器，成本高；served-values 是同一套解析程式的離線版本 |

## 相關

- [ADR-017](./017-defaults-yaml-inheritance-dual-hash.md)：conf.d 的目錄繼承與合併
- [ADR-036](./036-single-parser-effective-config.md)：路由與 domain policy 只由產生器解析
- 議題：[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)（平台層變更不出現在報告）、[#1420](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1420)（`_defaults.yaml` 變更被報成沒有變更）、[#2120](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2120)（根目錄平台檔的 `_profile` 變更）
