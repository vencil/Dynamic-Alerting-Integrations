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
updated_at: 2026-11-24
---

# ADR-037: config-diff 改為比較渲染結果

> **Language / 語言：** **中文 (Current)** | [English](./037-config-diff-compares-rendered-output.en.md)

## 狀態

✅ **Accepted**（2026-10-10 核可）。

- 2026-10-10：決定 4 補上渲染失敗的處理表（base 側的設定壞了不擋 PR）。
- 2026-10-11：決定 1 補上沒有傳 `--at` 時用哪個時間點；決定 4 補上 JSON 的分組方式。
- 2026-10-13：決定 3 補上由 CI 檢查負責的載體，以及兩支渲染程式都不讀的鍵怎麼列。
- 2026-10-14：決定 3 改由 `da-guard parse-tree` 逐鍵說出讀取位置，啟用「完整涵蓋、渲染沒有差異只附註」。
- 2026-10-17：決定 3 的延後載體表更新：`_` 檔頂層的 `_custom_alerts` 沒有讀取端。
- 2026-10-22：平台 CI 的另一份影響範圍報告退役，「這個變更影響誰」只由 config-diff 回答。
- 2026-11-24 修訂：改寫為現行行為。決策不變。更正兩處行為描述：`domain_policies` 寫在 `_domain_policy.yaml` 以外的檔時結束碼是 2（產生器拒收整棵樹）；symlink 迴圈在兩側都算。

## 決策

**`da-tools config-diff` 不自己讀 YAML。** 它在 base（PR 要合進去的分支，通常是 main）與 PR 兩側各「渲染」一次 conf.d，再比較兩邊的渲染結果。「渲染」是指跑那兩支真正把 conf.d 變成線上設定的程式：

- `da-guard served-values`：exporter 對每個租戶實際送出的值，是 exporter 解析程式的離線版本。
- 路由產生器 `generate_alertmanager_routes.py --output-configmap`：完整的 Alertmanager 設定。

這兩支正是 [ADR-036](./036-single-parser-effective-config.md) 指定的唯一語意實作（閾值由 exporter、路由由產生器），所以這裡不會出現第三種讀法。報告因此能說出平台層的變更（例如 `_defaults.yaml`）影響了哪些租戶。

### 範例：改平台預設的 `group_wait`

輸入：在 repo 的範例 conf.d（`components/threshold-exporter/config/conf.d`）上，把 `_defaults.yaml` 裡 `_routing_defaults` 的 `group_wait` 從 `30s` 改成 `99s`：

```yaml {.quote src="components/threshold-exporter/config/conf.d/_defaults.yaml"}
_routing_defaults:
  # ...
  group_by: ["alertname", "tenant"]
  group_wait: "30s"
  group_interval: "5m"
  repeat_interval: "4h"
```

輸出（`config-diff --old-dir <base> --new-dir <pr> --at 2026-10-10T00:00:00Z`，結束碼 1；節錄 Markdown 報告）：

```text
## Platform Settings
...
- `_defaults.yaml` `_routing_defaults` (modified)
  - the renders that read it changed; which of those changes come from this key is not told apart

## Routing Changes
...
- 2 tenants: `db-a`, `es-prod` — 2 routes (changed), e.g. `tenant="db-a"`
  - `group_wait`: `30s` → `1m39s`
```

這棵樹有 5 個租戶。db-b 有自己的 `_routing`，mongo-prod 與 redis-prod 綁了 routing profile，不受影響、不出現在結果裡；db-a 與 es-prod 繼承 `_routing_defaults`，被列出。路由面的值是 Alertmanager 載入設定後的讀法，所以 `99s` 印成它的寫法 `1m39s`；只是寫法不同的時長（`30s` 與 `0m30s`）不算變更。

另一個輸入：在同一個 `_defaults.yaml` 加上 `defaults:` 區塊、寫 `container_memory: 999`（範例樹原本沒有 `defaults:`，這個鍵用平台內建值 85）。輸出（結束碼 1；節錄）：

```text
## Threshold Changes

| Metric | Before | After | Change | Affected Alerts | Tenants |
|--------|--------|-------|--------|-----------------|---------|
| container_memory | 85 | 999 | looser | PodContainerHighMemory, PodContainerHighMemoryCritical | 4 tenants: `db-a`, `es-prod`, `mongo-prod`, `redis-prod` |
```

db-b 自己把 `container_memory` 設成 90，所以不受影響。兩個範例由 `tests/ops/test_render_diff.py` 與 `tests/ops/test_config_diff.py` 的 `TestAcceptance` 在同一棵範例樹上重現。

以下是決定 1 到 6。

### 1. 比較什麼

| 面向 | 渲染來源 | 比較的內容 |
|---|---|---|
| exporter 送出的值 | `da-guard served-values --at <時間點> --schedules --disable-effects` | 每個租戶的值、severity、state filter 的 severity、租戶的 routing、時間限定的模式（time box）、整天的排程、沒送出或被丟掉的鍵 |
| 路由 | 路由產生器 `--output-configmap` 產出的完整 Alertmanager 設定，以 Alertmanager 自己的載入方式讀回 | route 樹的每個節點、receiver 的定義、inhibit rules |

- 兩側在同一次執行中用**同一版**工具渲染，所以差異只反映設定的差異，不會混進工具改版造成的輸出變化。
- served-values 的 `--at` 兩側用同一個時間點，並印在報告上（`Rendered at:`）。兩側各取各的執行時間的話，連只改註解都會變成差異。config-diff 的 `--at` 選填：沒有傳時，在執行當下取一次、兩側共用。平台 CI 傳 base commit 的時間，所以重跑時答案不變；`da-tools init` 產生的 CI 在映像支援 `--at` 時也這樣傳。`--schedules` 讓當天（UTC）整天的排程也納入比較，不只看單一時間點。
- route 節點的身分是「從根到它的 matcher 路徑」。matcher 是 Alertmanager 用來決定告警走哪條路的條件，例如 `tenant="db-a"`。比較的欄位是 `receiver`、`group_by`、`group_wait`、`group_interval`、`repeat_interval`、`continue`。用 matcher 當身分，不依賴產生器替 receiver 取的名字。inhibit rules 當成不計順序的集合比較。
- receiver 定義與 served-values 的 routing 裡的憑證欄位（webhook URL、token、密碼）只報「有變」，前後值都遮罩，因為報告會貼成 PR 留言。同一個值出現在不是憑證的欄位時也遮：YAML 的 anchor／alias 或手抄都會把它放到那裡，而渲染結果分不出來；JSON 報告的 `masked_by_value` 記遮了幾處。

### 2. 差異歸給誰

- route 節點的 matcher 路徑上有 `tenant="X"`，就歸給租戶 X。
- 沒有租戶 matcher 的節點（例如 `_routing_enforced` 產生的平台通道），歸為「平台級」。
- receiver 定義有變，就歸給所有指向這個 receiver 的 route 所屬的租戶。多個租戶共用同一個 receiver 時，每個租戶各算一筆。
- inhibit rule 歸給它的 `tenant="X"` matcher 所指的租戶。severity 去重複的規則不指名租戶；它只有排除清單（`tenant!~"a|b"`）有變時，歸給清單增減的每個租戶，其餘歸為平台級。
- exporter 值的差異按 served-values 的租戶歸屬。

### 3. 沒比對到的要說出來

渲染結果只涵蓋上表那些設定。以下把 `_` 開頭檔案裡的每一個頂層鍵（`defaults`、`_routing_defaults`、`state_filters` 等）稱為一個「載體」：它承載一類平台設定。config-diff 對兩側每個 `_` 開頭的檔（任何深度），逐一比較每個載體的值（只比較是否相等，不解讀意義），再對照一張「載體 → 由哪個面向涵蓋、涵蓋得完不完整」的表（`scripts/tools/ops/config_diff.py` 的 `RENDERED_CARRIERS`）：

| 載體 | 面向 | 涵蓋 | 部分涵蓋的原因 |
|---|---|---|---|
| `defaults` | exporter 值 | 完整 | |
| `tenants` | exporter 值、路由 | 完整 | |
| `profiles` | exporter 值 | 完整 | |
| `_routing_enforced` | 路由 | 完整 | |
| `routing_profiles` | 路由 | 完整 | |
| `optional_overrides` | exporter 值 | 部分 | 清單本身不會送出 |
| `dimensions` | exporter 值 | 部分 | 宣告本身不會送出 |
| `state_filters` | exporter 值 | 部分 | `reasons` 沒有渲染程式讀 |
| `_routing_defaults` | 路由 | 部分 | 其中的 `routes` 不會被套用 |

每個有變的載體，在報告的「Platform Settings」段列成一行，依下面的規則決定狀態與結束碼：

- 有變、有面向涵蓋、而且那個面向出現差異：列為 `Rendered`，差異照常出現在後面各段。
- 有變、表上標為**完整涵蓋**、讀取端就在這裡讀它，但渲染結果沒有差異：這是真的沒有效果，例如改了一個所有租戶都自己覆寫的預設值。列在 `Changed, no effect on the renders that read it` 之下，不影響結束碼。
- 有變、表上標為**部分涵蓋**，而渲染結果沒有差異：列出並寫明可能沒有效果、也可能落在比較看不到的部分，結束碼 1。寧可多列一行，不說「沒有變更」。
- 有變、有面向涵蓋這個載體，但它寫在那個面向不讀的位置：列為 `Not read where it is written`，結束碼 1。
- 有變、但沒有面向涵蓋（包括表上沒有的新鍵）：列為 `Not compared`，結束碼 1。

這張表靠 `tests/ops/test_config_diff_carriers.py` 維持：每個載體在範例 conf.d 上改一次，斷言表上宣稱涵蓋它的面向真的出現差異；標為完整涵蓋的載體，再對它的每個子鍵各改一次；表上每一列都要有案例。渲染程式改版而失去某個載體或子鍵時，這個測試會先紅。

目前沒有涵蓋、延後處理的載體與觸發條件：

| 載體 | 延後理由 | 什麼時候做 |
|---|---|---|
| 平台層的 `_custom_alerts`（`_` 檔頂層的 recipe） | 沒有讀取端：exporter 不送出，custom-alert 編譯器照 exporter 的讀法編譯、也不編它（見 [ADR-024](024-version-aware-threshold-via-dimensional-label.md) §3） | 有讀取端讀它時 |
| `instance_tenant_mapping` | 由 tenant mapping 產生器讀，不是兩支渲染程式 | 該產生器有穩定的輸出格式 |
| `max_metrics_per_tenant` | served-values 不輸出上限值本身 | 有人需要時 |

**由 CI 檢查負責的載體。** `domain_policies` 由路由產生器的 `--validate --strict` 擋下違規；`_policies` 由 `validate-config` 檢查，`severity: error` 的規則違反、或規則寫法讀不了時失敗。寫在檢查會讀的位置時（`domain_policies` 在 `_domain_policy.yaml`，`_policies` 在根目錄的預設檔）只列為提示，不影響結束碼。`_policies` 寫在其他位置時列為 `Not read where it is written`，結束碼 1，免得報告暗示一條沒有人讀的規則有被執行。`domain_policies` 寫在 `_domain_policy.yaml` 以外的檔時，路由產生器拒收整棵樹，所以 PR 側的路由面「沒有計算」、結束碼 2，原因點名該檔。需要看「新增了哪些違規」時，讀產生器的 `--findings-json`。

**兩支渲染程式都不讀的鍵。** 表上沒有的鍵（例如 `_tenant_orgs.yaml` 的 `tenant_orgs`、`_rbac.yaml` 的 `groups`）列為 `Not compared`，結束碼 1。報告只寫 config-diff 確定的事：exporter 與路由產生器都不讀這個鍵；在 tenant-api 相關的檔裡另附一句誰可能讀它，因為讀不讀取決於 tenant-api 怎麼部署，例如 `_rbac.yaml` 只有在 tenant-api 的 `--rbac` 指到它時才從這裡讀。同一個檔裡渲染程式會讀的鍵（例如根目錄任何 `_` 檔裡的 `tenants:`）照常比較。沒有渲染程式讀的鍵，非字串的值以原文比較：`007` 與 `7` 在 YAML 1.2 是同一個整數，但以字串讀它的程式看到的是兩個值，而這裡沒有渲染結果可以佐證。

**怎麼知道寫在那裡有沒有人讀。** 值以 `da-guard parse-tree` 讀出的結果比較（ADR-036：Python 不自己解析 YAML），純量比型別與值，所以只改註解、縮排、引號或鍵的順序都不算變更。同一個鍵寫在哪個檔、哪一層，決定讀取端讀不讀它：例如 `defaults` 只在預設檔（`_defaults.yaml`）裡被讀，`tenants:` 寫在子目錄的 `_` 檔不會被讀。parse-tree 對每個 `_` 檔的每個載體輸出 `rendered_by`：改它會影響哪一份渲染結果（`exporter` 是 served-values，`generator` 是路由）。config-diff 只在那個面向真的讀這個位置時才算涵蓋。parse-tree 的輸出沒有 `rendered_by` 時（da-guard 比 config-diff 舊），config-diff 以結束碼 2 結束，不退回只看「這個檔有誰讀」的判斷，因為那會把寫在不讀位置的鍵當成有涵蓋。這份逐鍵的答案由 `tests/shared/test_parse_tree_rendered_by.py` 以實際渲染驗證：每個載體在它可能出現的每個位置各改一次，斷言出現差異的面向恰好是 parse-tree 說的那些。parse-tree 列為沒有讀取端讀的 `_` 檔（例如 `_defaults.yaml` 旁邊另一個沒被選上的 `_defaults.yml`）內容不在它的輸出裡，以位元組比較，有差異就列為 `Not read where it is written`。

範例一：在根目錄加一個 `_platform.yaml`，內容是 `defaults: {cpu: 1}`（這個檔不是預設檔，exporter 不讀它的 `defaults`），報告的「Platform Settings」段列出下面這段，結束碼 1：

```text
**:warning: Not read where it is written** (1)

- `_platform.yaml` `defaults` (added)
  - no render reads this key where it is written
```

範例二：在根目錄加一個只有 `tenants: {}` 的 `_platform.yaml`（`tenants` 標為完整涵蓋，這裡也有人讀，但空的區塊不改變任何渲染結果），列出下面這段，結束碼 0：

```text
**Changed, no effect on the renders that read it** (1)

- `_platform.yaml` `tenants` (added)
  - read here by the renders that show it in full, which show no difference: the change has no effect on them
```

### 4. 結束碼與報告

結束碼：

- **0**：沒有變更。
- **1**：有變更，或有東西沒比對到。
- **2**：沒算出結果。報告寫「not computed」，不會假裝成「沒有變更」。

渲染失敗時的原則只有一句：**PR 側失敗、或工具失敗，結束碼 2；base 側的設定壞了、或第一次匯入還沒有設定，結束碼 1。** main 上的設定已經壞了時，修好它的 PR 不該被擋。

| 情況 | 結束碼 | 報告 |
|---|---|---|
| PR 側某個面向渲染失敗 | 2 | 該面向寫「not computed」，其他面向照常比較 |
| PR 側沒有 conf.d，或裡面沒有設定檔 | 2 | 渲染程式不跑；該面向寫「not computed」 |
| base 側的設定壞了 | 1 | 寫出原因；該面向**整個**列為「not compared」 |
| base 側沒有 conf.d，或裡面沒有設定檔（第一次匯入） | 1 | 渲染程式不跑；把 base 當成空的，所有租戶都算新增 |
| base 側或 PR 側的路徑是 symlink 迴圈 | 2 | 不是「沒有 conf.d」：`ls` 看得到那個項目，只是讀不到。點名該路徑（`dir_not_accessible`），不當成第一次匯入 |
| 工具失敗（不論哪一側） | 2 | 寫「not computed」 |
| 兩側都失敗 | 2 | 照 PR 側處理 |

base 側的設定壞了時，就算它還渲染得出一部分，那一部分也不拿來比；base 側讀不了的 `_` 檔，它的載體（見決定 3）也列為「not compared」。

怎麼判斷是「設定壞了」：只認下面三種，其他一律當成工具失敗（判不清就擋）。

- `da-guard served-values` 回 3：有檔案解不開或讀不到。這時它仍會輸出一份不完整的結果，不能拿來比。
- da-guard 認得這組參數，卻回 2（而且不是程式崩潰）：例如同一個租戶在兩個檔重複宣告。
- 路由產生器拒收這棵樹：回 1，或以「routing-tree error(s)」回 2，且不是程式崩潰。

工具失敗的例子：找不到 da-guard、da-guard 比這版 config-diff 舊、輸出格式不對、逾時、被中止。

「沒有 conf.d 或沒有設定檔」要在跑渲染程式之前先看目錄判斷，不能看結束碼：`da-guard served-values` 對空目錄和不存在的目錄都回 2，路由產生器對空目錄卻回 0。

報告依「同一個變更」分組：一筆變更列出受影響的租戶數與前 5 個租戶名，其餘寫成「(+N more)」，不逐一展開上千行；整份報告仍受 PR 留言的長度上限約束。JSON 輸出（`--format json`）頂層帶 `schema`（目前為 `da-tools.config-diff/v1`），讀取端遇到不認得的版本就拒收。JSON 裡同樣一個變更一筆，帶它影響的租戶清單，所以 Markdown 的排版之後再改時，JSON 不必跟著改。

### 5. served-values 補上 state filter 的 severity

served-values 每個租戶有一個與 `values` 同層的欄位 `state_filters`，記錄每個開啟中的 state filter 的 severity，與 exporter 的 `/metrics` 送出的 `user_state_filter{tenant,filter,severity}` 一一對應；關閉的 filter 不列。輸出頂層帶 `schema`（目前為 `da-guard.served-values/v6`），Python 讀取端遇到其他值即拒收。

不改 `values` 裡 `_state_<filter>` 的型別（仍是 true／false）：有讀取端以「等於 true」判斷維護模式，改型別會讓它悄悄失效。也不放進既有的 `severities` 欄位：多個讀取端把那個欄位的鍵當成「有輸出的閾值鍵」清單。

### 6. 上線方式

1. 先在平台 repo 的 CI 以**只留言、不擋 merge** 的方式運作：`.github/workflows/config-diff-advisory.yml`，在範例 conf.d 或 config-diff 相關程式變更時觸發，從 PR 的原始碼建 da-guard，用 merge-base 取得 base 側，結束碼 2 只發警告。它列在 `.github/required-checks.yaml` 的 `advisory:` 之下，不是必要檢查。
2. 改為必要檢查的條件（全部成立）：至少 20 個觸發的 PR 或 6 週（取較晚者），期間誤報 0、事後發現的漏報 0，1000 個租戶的樹在 5 分鐘內比較完。條件與記錄方式寫在該 workflow 的檔頭。目前還在第 1 步。
3. 客戶端：`da-tools init` 產生的 CI 本來就呼叫 `config-diff`，換上帶新行為的 da-tools 映像就自動改用。

## 背景

`da-tools config-diff` 是 PR review 用的「這個變更影響誰」報告。只比對每個頂層租戶檔自己寫的值的作法，看不到 `_` 開頭的平台檔（例如所有租戶繼承的 `_defaults.yaml`、可以替個別租戶補設定的根目錄 `_platform.yaml`、定義共用路由組合的 `_routing_profiles.yaml`）與子目錄裡的檔，只能把檔名列為「沒有比對」。可是平台層的變更恰好影響面最大：改一個預設的告警接收端，所有沒有自己設定路由的租戶都會跟著變。

以每個租戶合併後的設定為基礎也不夠。依 [ADR-017](./017-defaults-yaml-inheritance-dual-hash.md)，合併結果看得到 `defaults:` 裡的值，但不含與 `defaults:` 同層的鍵，`_routing_defaults` 就是其中之一，所以上面範例的 `group_wait` 變更它看不到。

兩支渲染程式各看得到不同的東西。在範例 conf.d 上每次只改一處，比較兩側渲染結果：

| 變更 | served-values | 路由產生器 |
|---|---|---|
| `_routing_defaults` 的 `group_wait` | 不變 | 變 |
| `_routing_enforced`（平台強制的額外通道）打開 | 不變 | 變（新增平台級 route 與 receiver） |
| `defaults:` 裡的閾值 | 變（只有沒覆寫該鍵的租戶） | 不變 |
| `state_filters` 的 severity | 變（`state_filters` 欄位，決定 5） | 不變 |
| 只改註解 | 不變 | 不變 |

最後一列是對照組，代表比較方法不會被格式變動誤導。每一列都由 `tests/ops/test_render_diff.py` 的案例重現。兩支程式合起來，才涵蓋平台層大部分的設定；任何一支單獨都不夠。

## 後果與已知限制

- **報告與 JSON 結構和舊版不同**：解析舊版 config-diff JSON 的客戶腳本要改讀新結構（見決定 4）。
- **一律需要 da-guard**，路由面另外需要 da-read 與 da-crdecode（路由產生器以它們按 Alertmanager 與 kubectl 的讀法讀回輸出）：da-tools 映像內建三者；在 repo 裡直接跑要先 `make da-guard-build`、`make da-read-build`、`make da-crdecode-build`。
- **v2.9.0 映像的 config-diff 仍是舊行為**：只比對每個租戶檔自己寫的值、不接受 `--at`；`da-tools init` 產生的 CI 會先探測映像支不支援 `--at` 才傳 <!-- image-caveat: v2.9.0 -->。
- **變慢**：1000 個租戶的樹（`scripts/tools/dx/generate_tenant_fixture.py -n 1000` 產生，再在 `_defaults.yaml` 末尾加上 `_routing_defaults: {receiver: {type: webhook, url: "http://example.invalid/hook"}, group_wait: "30s"}`），改一個 `group_wait` 的整次比較約 19 秒（4 核機器，2026-11-24 量測）。
- **既有報告的內容保留**：租戶閾值的 tighter／looser 分類、設定變更、custom alert 的比較，都改從 served-values 的值算出（`tests/ops/test_config_diff_served.py`）。
- **exporter 不送出的鍵**（例如 `_namespaces`）：served-values 把它列在 `unserved` 之下，變更仍會列出，但沒有一份渲染結果能說出它對告警行為的效果。
- **matcher 完全相同的兄弟節點**：只能依出現順序對應。順序改變時，可能報成一刪一增。
- **放寬或刪除規則看不出來**：`domain_policies`、`_policies` 寫在檢查會讀的位置時只列為提示，放寬或刪除一條規則不會被 CI 擋，報告也不會以結束碼標出；`severity: warning` 的 `_policies` 規則違反時，CI 也不會失敗。
- **閾值 profile 裡寫的路由層鍵**（`_routing`、`_severity_dedup` 等）：兩個讀取端都不套用，報告把這種變更列為沒有效果（`no_effect`）；為什麼見 [`routing_in_unread_location`](../cli-reference.md#guard)。
- **平台 CI 沒有另一份影響範圍報告**：「這個變更影響誰」只由 config-diff 回答。

## 考慮過的替代方案

| 做法 | 否決理由 |
|---|---|
| 把 `defaults:` 以外的鍵併進合併後設定與 `merged_hash`（每個租戶合併後設定的雜湊值） | `merged_hash` 一動，exporter 就重新載入；`_routing_defaults` 這類 exporter 根本不用的鍵放進去，會重現 ADR-017 要避免的大量重新載入。既有的 `merged_hash` 快照也會全部作廢 |
| 另開新工具比較平台層，保留 config-diff 自己比對 YAML | reviewer 會同時看到兩三份報告，對同一件事可能說法相反；客戶端還要另外接一個新的 CI 步驟 |
| 保留 config-diff 自己讀 YAML，再加一段平台層比較 | 會出現第三種讀法，違反 ADR-036 |
| 用 amtool 拿每個租戶的 label 去問「告警會送到哪」，當作主要偵測 | 只改 `group_wait` 時 receiver 名稱與定義都沒變，問路的結果相同，看不出差異；以 alertname 或正規表示式分流的路由，探針沒帶到那個值就看不到。保留作為解釋用途 |
| 比較產生器自己的輸出格式，或一份另外整理的設定 JSON | 產生器的輸出格式是它自己的；Alertmanager 的設定格式由上游定義，比較穩定。ADR-036 也決定不輸出整理好的設定 JSON |
| 把 `_state_<filter>` 改成帶 severity 的物件，或放進 `severities` | 會讓現有讀取端悄悄失效（見決定 5） |
| 起一個 exporter 去抓 `/metrics` | CI 要起容器，成本高；served-values 是同一套解析程式的離線版本 |

## 相關

- [ADR-017](./017-defaults-yaml-inheritance-dual-hash.md)：conf.d 的目錄繼承與合併
- [ADR-036](./036-single-parser-effective-config.md)：YAML 只由 Go 解析；路由與 domain policy 只由產生器判
- 實作位置：`scripts/tools/ops/config_diff.py`（報告與載體表）、`scripts/tools/ops/_render_diff.py`（渲染與比較）、`components/threshold-exporter/app/cmd/da-guard/served_values.go`
- 議題：[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)（平台層變更不出現在報告；也是 advisory 階段記錄誤報與漏報的地方）
