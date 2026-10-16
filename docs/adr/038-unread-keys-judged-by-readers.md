---
title: "ADR-038: 沒有讀取端讀的鍵以實測判定，保留鍵由一份登錄表宣告"
tags: [adr, conf.d, validation, da-guard, routing, schema]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-038
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-10-16
updated_at: 2026-10-16
---

# ADR-038: 沒有讀取端讀的鍵以實測判定，保留鍵由一份登錄表宣告

> **Language / 語言：** **中文 (Current)** | [English](./038-unread-keys-judged-by-readers.en.md)

## 狀態

✅ **Accepted**（2026-10-16 起草並由 owner 核可）。

2026-10-16 修訂（實作登錄表時依實測修正）：讀取端多一類 `policy_engine`；層多一層子目錄 `_defaults.yaml`；探針改為雙向，涵蓋所有比對得到輸出的讀取端（決定 2、3）。決定 8 的成本說明更新。

## 摘要

**問題**：租戶設定裡拼錯的鍵大多默默失效。路由產生器 `--validate --strict` 對它們只給警告；da-guard（exporter 隨附的設定檢查工具）完全不報；validate-config 只擋得到 `_` 開頭的拼錯，閾值鍵的拼錯一樣只有警告。寫成 `disable` 想關掉的告警，也可能其實沒關掉。今天判斷「這個鍵合不合法」的是幾處各自維護的鍵名規則，彼此對不齊。

**讀取端**指會讀租戶設定的程式：exporter（把閾值變成 Prometheus 指標）、路由產生器（產生 Alertmanager 設定）、自訂告警編譯器（把 `_custom_alerts` 編成告警規則）。

**決定**：

1. 一個鍵有沒有用，**看讀取端實際做了什麼**，不從鍵名預測：exporter 的部分看它實際送出的值，產生器的部分看產生器自己的輸出。
2. 量不到的事實（每個 `_` 開頭的保留鍵歸哪個讀取端、可以寫在哪一層、哪些鍵已退役）寫在**一份登錄表**（JSON）。Python、Go 的保留鍵常數與兩份 JSON schema 的頂層宣告都由它產生。
3. 「沒有任何讀取端讀」的判決由**一個共用函式**計算，validate-config、產生器 `--validate` 與 tenant-api 的寫入檢查都呼叫它，不各自實作。
4. `disable` 沒關到任何東西、或只關到一部分時要報出來；並提供一個**讓使用者自己檢查與測試**的輸出。

本 ADR 只決定怎麼判、怎麼報。各閘門（validate-config、產生器 `--validate`、da-guard、tenant-api 的寫入檢查）何時改成「判到就擋」由 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016) 依序推進，屆時是 breaking change。

## 背景

### 名詞

- **保留鍵**：`_` 開頭、由平台定義意義的鍵，例如 `_silent_mode`、`_routing`、`_state_maintenance`。其他鍵是閾值鍵。
- **維度鍵**：只對某個 label 值生效的閾值，寫成 `metric{label="value"}`，例如 `redis_queue_length{queue="tasks"}`；去掉大括號的部分叫 base。
- **`_critical` 鍵**：`X_critical` 是閾值 `X` 的 critical 等級門檻。
- **profile**：平台定義的一組共用設定，租戶用 `_profile: <名稱>` 套用。
- **根平台檔**：conf.d 根目錄下 `_` 開頭的檔，例如所有租戶繼承的 `_defaults.yaml`。
- **served-values**：`da-guard served-values` 指令，輸出 exporter 對每個租戶實際送出的值，以及寫了卻沒送出的鍵（`unserved`）與原因。

### 一個例子

根 `_defaults.yaml` 與一個租戶檔：

<!-- md-yaml-drift: ignore — 故意拼錯的範例：拼錯的鍵正是本節要說明的 -->
```yaml
# _defaults.yaml
defaults:
  mysql_connections: 80
  mysql_slave_lag: 30
  redis_memory: 70

# db-a.yaml
tenants:
  db-a:
    mysql_conections: "70"        # 拼錯
    mysql_slave_lagg: disable     # 拼錯，原意是關掉 mysql_slave_lag
    _routng: disable              # 拼錯，原意是停用路由
    redis_memory: disable         # 想關掉 redis 記憶體告警
    redis_memory_critical: "90"
    _namespaces: [prod-ns]        # 給工具看的標註
```

今天的結果（在 main 上實跑）：

| 工具 | 結果 |
|---|---|
| exporter 實際送出（served-values） | `mysql_connections: 80`、`mysql_slave_lag: 30`、`redis_memory_critical: 90`；沒有送出的：`mysql_conections`、`mysql_slave_lagg`、`_routng`、`redis_memory`、`_namespaces` |
| 產生器 `--validate --strict` | rc 0，三行警告：`unknown reserved key '_routng' (typo?)`、`unknown key 'mysql_conections' not in defaults`、`unknown key 'mysql_slave_lagg' not in defaults` |
| validate-config | rc 1：schema 擋下 `_routng`；兩個閾值鍵的拼錯只列警告 |
| da-guard | rc 0，沒有任何 finding |

validate-config 擋下了 `_routng`，但其餘幾件事都沒有人擋：

- 租戶以為門檻是 70，實際生效的是預設的 80。
- 租戶以為關掉了 `mysql_slave_lag`，它照樣以 30 送出。
- 租戶以為關掉了 redis 記憶體告警，但 `redis_memory_critical` 仍然送出 90，critical 告警照常會響。

### 為什麼不再補鍵名規則

「哪些鍵合法」今天由幾處各自判斷：產生器的保留鍵檢查、exporter 載入時的檢查、tenant-api 的寫入檢查，以及 `_state_`／`_routing` 的前綴規則。保留鍵清單本身也有三份：Python 常數、Go 常數與 JSON schema，靠比對測試保持一致。逐一補齊做不到，原因是同一個鍵合不合法，取決於讀取端怎麼讀，而不是它叫什麼名字：

- **維度鍵**：`es_heap_usage_percent{node="es-hot-01"}` 的 base 不在 defaults 裡，產生器的鍵名檢查判它不合法；exporter 載入時的鍵名檢查也判它不合法，但只寫一行 log，照樣送出它。主 conf.d 上產生器有 17 條這類警告，其中 15 條的鍵 exporter 實際有送出（另外 2 條的值是 `disable`）。這 15 個鍵有送出，所以不是「沒人讀」；它們的問題是沒有任何告警規則讀（見決定 10）。
- **`_` 開頭但沒有登記的鍵**：根 defaults 宣告了 `_myth`，租戶寫 `_myth: "7"`，exporter 照樣送出 7；JSON schema 卻拒收 `_myth`。
- **profile 帶來的錯字**：exporter 算在租戶頭上，產生器不檢查。
- **`_state_` 前綴**：任何拼法都算合法。

所以直接把現有的警告改成擋下，主 conf.d 會當場出現 15 條誤報。

## 決定

### 1. 判決以實測為準

一個鍵的讀取端只來自實測：

- **exporter**：served-values 有沒有送出該鍵，或有沒有因為它而改變送出的值。`disable` 讓本來會送出的一列不送出，也算 exporter 讀了它；`disable` 沒有改變任何送出的值，則是 `disable_noop`（見決定 8）。exporter 實測送出的鍵判為有人讀，不論登錄表怎麼寫。
- **產生器**：產生器自己的輸出（它讀到並採用的鍵）。
- **自訂告警編譯器**：它讀的 `_custom_alerts`。

不另寫一份「產生器會讀哪些鍵」的程式邏輯給別的讀取端模仿。某個值在產生器的語意下是否有效（例如 `_routing_profile` 指向的 profile 存不存在），仍只由產生器判，與 [ADR-036](./036-single-parser-effective-config.md) 一致。

### 2. 保留鍵登錄表

量不到的事實寫在一份 JSON 登錄表，放在 exporter 的 Go module 內（`components/threshold-exporter/app/pkg/config/`），內容限於頂層 `_` 命名空間：

- 每個保留鍵與前綴（`_silent_mode`、`_routing`、`_state_` …）屬於哪個**讀取端類別**：`exporter`、`generator`、`custom_alert_compiler`、`policy_engine`（Policy-as-Code，讀根 `_defaults.yaml` 的 `_policies`）、`annotation`（只給人或工具看的標註，例如 `_namespaces`）。一個鍵可以屬於多個讀取端。
- 每個讀取端**在哪些層讀這個鍵**：租戶檔、根平台檔的 `tenants:`、profile、子目錄 `_defaults.yaml` 的 `defaults:`、根平台檔的頂層。同一個鍵，各讀取端讀的層可以不同：實測 `_custom_alerts` 寫在 profile 裡，exporter 讀、編譯器不讀。
- **已退役的鍵**與給使用者的提示文字。
- 說明文字（給 schema 的 description 與 explain 輸出）。

今天的三份保留鍵清單改由登錄表產生，CI 重新產生後比對差異，有差異就紅：

| 今天 | 之後 |
|---|---|
| Python 的保留鍵常數 | 由登錄表產生 |
| Go 的保留鍵常數 | 由登錄表產生 |
| `tenant-config.schema.json` 與 `platform-defaults.schema.json` 的頂層宣告 | 由登錄表產生 |
| 產生器、exporter、tenant-api 各自的鍵名判斷 | 由決定 4 的共用判決取代 |

執行時各工具讀的是產生出來的常數，不在執行時讀這份 JSON，所以 da-tools 映像不需要多帶檔案。登錄表放在 Go module 內，是為了讓 Go 端日後若要直接嵌入，三個 Go 程式（exporter、da-guard、tenant-api）都不必再複製一份。

**範圍只到頂層鍵名**。子鍵表維持「由讀取它的程式碼為準，schema 與其他端以測試釘住」：

| 子鍵 | 以誰為準 |
|---|---|
| `_routing` 的子鍵、receiver 型別、`_routing_enforced` 的欄位 | 產生器的程式碼 |
| `_metadata` 的 9 個欄位 | Go 的 `TenantMetadata` 結構 |
| `_custom_alerts` 條目的欄位 | 自訂告警編譯器 |

這條分界讓讀者知道某個清單該去哪裡改：頂層保留鍵改登錄表，子鍵改讀取它的那支程式。

**登錄表和實測不一致時**：以實測為準。例如 `_myth` 沒有登記，但根 defaults 宣告了它、exporter 也送出了。此時判為「平台檔違反 `_` 命名空間規則」，報在平台檔上，不算租戶拼錯（見決定 6）。長期會讓 exporter 不再送出沒有登記的 `_` 鍵，使三方一致；那是 breaking change，見 [#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)。

### 3. 防止登錄表與程式漂移

用黑箱探針：依登錄表逐鍵、逐層產生一份最小設定，分別跑 served-values、產生器、自訂告警編譯器與 Policy-as-Code，看輸出有沒有因為這個鍵而改變。

- 登錄表說「某讀取端在這一層讀這個鍵」→ 該讀取端的輸出必須改變。這能抓到「登錄了，但程式已經不讀」。
- 登錄表沒說某讀取端在這一層讀 → 該讀取端的輸出不能改變。這能抓到「程式讀了，登錄表沒寫」；少寫一層，聯集判決就會把有人讀的鍵判成沒人讀。
- 探針看不出「標註」與「沒人讀」的差別，也看不出「讀了但輸出不變」（例如產生器讀了 `_metadata` 卻沒用到）。這兩類在登錄表明確標註，不靠探針驗證。
- Policy-as-Code 除了按名稱讀 `_policies`，還會讀部署端規則以 `target` 指名的任何鍵。這類讀取由規則決定，登錄表列不出來；探針的樹裡沒有指向該鍵的規則，所以對 Policy-as-Code 只驗它不會自己去讀鍵。共用判決因此要把規則指名的鍵算成有人讀。

探針只看輸出，不看程式內部怎麼讀，所以讀取端改寫載入方式時不需要重做。

### 4. 判決與等級

共用函式對每個租戶的每個鍵給出：

- `read_by`：哪些讀取端讀它（可以為空）。
- `source`：它寫在哪一層、哪個檔。
- `verdict`：

| verdict | 意義 | 等級 |
|---|---|---|
| `read` | 至少一個讀取端讀它；登記為 `annotation` 的鍵也算 | — |
| `unread` | 沒有任何讀取端讀它 | 寫在租戶檔：error；來自平台檔或 profile：notice，並指名檔案；只因 `disable_noop` 而未讀：warning（見決定 8） |
| `undecidable` | 沒有讀到任何根平台檔，租戶寫的閾值鍵在 exporter 那半無從判斷；保留鍵照常判（見決定 7） | 判決輸出回 rc 2 |
| `platform_violation` | 沒有登記的 `_` 鍵被 exporter 送出 | 報在平台檔；不歸給租戶的變更，也不擋無關租戶的寫入 |

同一個錯字來自平台檔時只報一次（依檔案與鍵去重），並列出受影響的租戶，不會變成每個租戶各一筆。

`disable_noop` 只報 warning、拼錯的閾值鍵報 error，理由是：一個沒關到東西的 `disable` 不會讓任何值錯誤生效，也可能是刻意的保險寫法（例如預設日後才會加上的鍵）；它的風險在於原意要關的東西沒關到，所以除了 warning，這次變更新引入的還會出現在 config-diff 的 PR 留言（見決定 8）。`disable_partial` 不改變 verdict（鍵有被讀），另以 warning 報。

上表的等級是各閘門改成「判到就擋」之後的等級。在那之前，這些 finding 只報不擋：validate-config 一律列為警告，結束碼不變；`undecidable` 的 rc 2 只出現在判決輸出（決定 9 的 explain）。

### 5. 計算放在哪

「沒有任何讀取端讀」需要把 exporter 與產生器兩邊的結果併起來，放在 Python 的共用函式：

- 它呼叫 served-values 取得 exporter 那半（validate-config 今天已經這樣呼叫）。
- 它併上產生器自己的讀取結果。
- validate-config 先接上；產生器 `--validate` 與 tenant-api 的產生器容器（tenant-api 每次寫入前呼叫的產生器，見 [ADR-036](./036-single-parser-effective-config.md) §3）之後接上。
- da-guard 不可用或失敗時，只報一筆 notice「判決未完成」，**不得**當成拒收寫入。否則 da-guard 一出問題，tenant-api 的所有寫入都會被擋。

da-guard 本身只輸出事實：exporter 實測的 `read_by` 與原因，以及登錄表對非 exporter 鍵的宣告。它對產生器負責的鍵不下判決。

這帶來一個連動修訂：[ADR-036](./036-single-parser-effective-config.md) §9 規定單獨執行的 da-guard 對它判不了的內容回 exit 4（未判定）。原本觸發條件只有「樹上有路由或 policy 內容」，現在擴大為「樹上有只有產生器能判的內容」，包括租戶寫了只由產生器讀、或拼錯而只有產生器會警告的 `_` 鍵。否則單獨跑 da-guard 的人會把「沒判」讀成「判過、沒問題」。exit 4 本身要等 ADR-036 §7 的步驟 2（da-guard 加上「未判定」）完成後才存在，這個擴大範圍也從那時起生效。

### 6. 平台側的問題不算在租戶頭上

決定 4 的 `platform_violation` 與「來自平台檔的 `unread`」每次判定都會出現，不歸給正在修改的租戶。tenant-api 的寫入檢查不因平台檔既有的這類問題，擋下無關租戶的寫入。

### 7. 無從判斷的情況

exporter 那半能不能判，取決於**有沒有讀到任何根平台檔**，而不是 conf.d 裡有沒有某個檔：

- **沒讀到任何根平台檔**（例如客戶 CI 只檢出租戶 repo）：租戶寫的閾值鍵標 `undecidable`。保留鍵照常判。
- **有讀到根平台檔，但沒有宣告任何閾值**（例如只有 profiles、只有子目錄的 `_defaults.yaml`）：這是可以判定的錯誤，照現有的精確 finding 報（例如「子目錄的 defaults 送不到 exporter」），不降級成無從判斷。
- **只有路由設定的樹**：rc 0。

拼錯的閾值鍵與「平台沒宣告、租戶有意省略」得到的原因相同，判決不嘗試區分兩者。

`_state_<filter>` 指向平台沒有宣告的 filter，或 filter 宣告在子目錄（不會被讀）時，原因在平台檔。這類先報成平台側的 notice，指名缺的宣告；等到「平台刪掉 filter 時不會讓既有租戶懸空」的守衛到位，才考慮升級。

### 8. `disable` 有沒有作用

| 報告 | 什麼情況 | 怎麼判 |
|---|---|---|
| `disable_partial` | base 鍵關掉了，但同一族的 `_critical` 或維度鍵仍在送出 | 一次 served-values 比對同族的鍵，不需要重算 |
| `disable_noop` | 拿掉這個 `disable`，送出的值完全不變 | 反事實重算：假設這個 `disable` 不存在，再算一次，看結果有沒有變；重算在 Go 內進行 |

- 保留鍵（`_routing: disable`、`_silent_mode: disable` …）沒有豁免，照決定 4 判。
- `disable_noop` 常見的原因是鍵拼錯：上面例子的 `mysql_slave_lagg: disable` 沒關到任何東西，真正的 `mysql_slave_lag` 照樣送出。
- 反事實重算要看整天的時段（含排程值），不只看某一個時間點，否則時窗內與時窗外的判斷會相反。
- 成本：一個租戶的閾值列只取決於它自己的設定與平台的設定，所以每個租戶只在自己的排程切點重算，每次只建入同一輪的租戶；第 k 輪把所有租戶各自的第 k 個 `disable` 一起拿掉，輪數等於單一租戶最多有幾個 `disable`，與租戶數無關。
- 寫在 profile 或平台檔的 `disable` 影響所有選用它的租戶，報在那個檔上。
- 兩者在整棵樹報警告。這次變更**新引入**的，另外出現在 config-diff 的 PR 留言，因為 reviewer 實際會看的是那裡（[ADR-037](./037-config-diff-compares-rendered-output.md)）。
- 反事實重算只在 explain、config-diff 與 CI 中進行，不放進 tenant-api 的寫入檢查：寫入檢查有時間上限（[ADR-036](./036-single-parser-effective-config.md) §3），而重算的輪數取決於設定內容，不固定。

`disable` 只關 base 鍵、不連帶關掉 `_critical` 與維度鍵，這是現行語意。本 ADR 只把它報出來；要不要改成連帶關閉是 breaking change，見 [#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)。

### 9. 讓使用者自己檢查

新增 `explain` 輸出：對指定租戶，逐鍵列出

- 來源層與檔案；
- 哪些讀取端讀它，沒讀的原因；
- 有效值；
- `disable` 的作用：`effective`（有關掉東西）、`partial`（對應 `disable_partial`）、`noop`（對應 `disable_noop`）。

輸出是一份帶版本號的 JSON（另有給人讀的表格），由現有的 `da-guard effective` 來源資訊、served-values 的原因欄位與反事實結果組成，不另造第三種格式。`--assert <檔案>` 拿輸出與一份預期結果比對，不同就回非 0，可以放進客戶自己的 CI。這和 `amtool config routes test`（給一組 label，回答會送到哪個 receiver）、`promtool test rules` 的用法相同。

文件會說明：工具會盡量把問題報出來，但值是否符合預期，使用者要用 explain 自己檢查與測試。

### 10. 其他一併處理的情況

| 情況 | 處理 | 等級 |
|---|---|---|
| 送出了但沒有任何規則讀它（例如根 defaults 的錯字、維度鍵的 base 拼錯） | 讀取端是部署中的告警規則。以 repo 內規則包的索引判斷，涵蓋經 recording rule 間接讀、直接查 `user_threshold` 的規則，以及 `_critical` 鍵；可以宣告「外部讀取端」（客戶自己的 Prometheus 規則） | 永遠只是建議 |
| 整份沒有讀取端讀的檔（租戶區沒有 `tenants:` 的檔、子目錄沒被選上的 `_defaults` 檔） | 沿用 served-values 現有的 `skipped`／`unread_keys` | notice：新建、還沒寫 `tenants:` 的檔不能凍結寫入 |
| `_metadata` 不認得的子鍵 | 以 Go 的 `TenantMetadata` 為準；今天只有 validate-config 的 schema 檢查會報 | 租戶檔：error；平台檔與 profile：notice |
| 讀了但值無效（`_silent_mode: garbage`、`_severity_dedup: maybe`、`_routing_profile: 5`） | 今天產生器與 da-guard 都不報，validate-config 只擋得到其中兩個（`_severity_dedup: maybe` 全無提示）。只有一個讀取端的鍵，由它驗；兩個讀取端都讀的 `_severity_dedup`，以一份共用案例表把兩邊的接受值釘成一致 | error |

用現有的規則索引對主 conf.d 實測，有 18 個送出了但查不到規則讀的鍵。其中 3 個是 `_critical` 鍵，它們的 base 有告警讀，只是索引還沒有處理 `_critical` 後綴，實作時要補上；其餘 15 個是維度鍵，所屬的 base 沒有任何規則讀；它們正是背景一節那 15 條鍵名警告的鍵。後者不刪也不放寬檢查，而是列進一份帶 issue 連結的已知清單，清單只准縮小。這項檢查問「送出的值有沒有規則讀」，現有的「告警所需的閾值是否送得到」檢查問「規則要的值有沒有送出」，兩者方向相反、互相補足。

### 11. 推進順序

每一項都可以單獨發布：

1. 登錄表、產生出來的常數與 schema、黑箱探針。行為不變。
2. da-guard 輸出 `read_by` 與原因、`disable_partial`、反事實 `disable_noop`、explain 與 `--assert`；共用函式接上 validate-config，只報不擋。
3. 產生器 `--validate` 與產生器容器接上共用函式，da-guard 的 exit 4 擴大範圍。需要等 [ADR-036](./036-single-parser-effective-config.md) §7 的步驟 2（da-guard 加上「未判定」）先完成。
4. 決定 10 的各項，以及 `_state_` 的平台側 notice。

閘門改成「判到就擋」不在本 ADR，依 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016) 推進。

## 範例

上面背景一節的例子，判決輸出（explain 的人讀版，節錄；等級是各閘門改成「判到就擋」之後的等級）：

```
db-a
  key                     source        read_by     verdict  disable
  mysql_conections        db-a.yaml     —           unread   —        ← error：沒有讀取端讀
  mysql_slave_lagg        db-a.yaml     —           unread   noop     ← warning：沒關到任何東西，mysql_slave_lag 仍送出 30
  _routng                 db-a.yaml     —           unread   —        ← error
  redis_memory            db-a.yaml     exporter    read     partial  ← warning：redis_memory_critical 仍送出 90
  redis_memory_critical   db-a.yaml     exporter    read     —
  _namespaces             db-a.yaml     annotation  read     —
```

輸入與今天的實測輸出見「背景」；這張表是本 ADR 規定的判決，各欄位依決定 1、4、8、9 產生。

## 後果與已知限制

- **保留鍵清單只剩一個來源**：新增保留鍵只改登錄表，再重新產生；各讀取端各自的鍵名判斷改由共用判決取代。
- **判決需要兩支程式**：完整的「沒有讀取端讀」需要 da-guard 與產生器都能跑。只有 da-guard 的環境得到 exporter 那半，產生器那半回 exit 4（未判定）。
- **錯字與有意省略不區分**：平台沒有宣告的閾值鍵，判決不知道是租戶拼錯還是平台漏宣告，兩者報同一個原因。
- **規則讀取以 repo 內的規則包為準**：客戶自己的規則讀了某個閾值，除非宣告為外部讀取端，否則會被報成「沒有規則讀」。這一項因此永遠只是建議。
- **探針不涵蓋兩類**：標註類的鍵（`_namespaces`），以及讀了但輸出不變的鍵，靠登錄表的明確標註。
- **反事實重算的成本**：重算的輪數等於單一租戶最多有幾個 `disable`，每輪是一次 served-values。它不進入寫入檢查。
- **實測中發現、另案處理的問題**：
  - 自訂告警編譯器與 exporter 對 `_custom_alerts` 的來源規則不同：編譯器收子目錄非選中的 `_` 檔裡的租戶，exporter 不讀（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）。
  - 產生器讀了 `_metadata` 卻沒有使用（[#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043)）。
  - `_severity_dedup` 只讀租戶檔，其他保留鍵會繼承 profile；需要確認是否刻意（[#3044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3044)）。
  - exporter 送出沒有登記的 `_` 鍵，與 schema 不一致（見決定 2，[#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)）。
  - `disable` 不連帶關掉 `_critical` 與維度鍵（見決定 8，[#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)）。

## 考慮過但否決

- **在判定工具裡放一份「產生器讀哪些鍵」的表，再用測試追產生器的實作**：表的擁有者（Go 的判定工具）和被描述的程式（Python 產生器）不同，這份表就是漂移點。業界的對照是 Spring Boot 手寫的 `additional-spring-configuration-metadata.json`，同樣靠人記得同步。
- **各讀取端在執行時回報自己讀了哪些鍵，再取聯集**：業界找不到「任一讀取端讀了就合法」的聯集判決先例，主流是按前綴或子樹劃分歸屬（OpenTelemetry Collector 各元件驗自己的子樹、Helm subchart 與 `global`）。實作上，Python 端資料一經複製（`copy.copy`、`json.dumps`）就失去追蹤，平台檔合併後也丟了來源；Go 的 map 讀取無法攔截，要在每個解析階段逐一埋點。實測產生器在租戶層逐鍵讀的只有 `_routing`、`_routing_profile`、`_severity_dedup`、`_metadata` 四個，不隨設定而變，執行時追蹤換不到額外資訊。這個想法改用在決定 3 的黑箱探針。
- **只在一個點記錄「下層有值」來判 `disable` 有沒有作用**：exporter 在閾值相關的地方讀 `disable` 有五處（base、宣告型閾值、`_critical`、維度鍵、state filter），而且 base 階段是逐一走過 defaults，走得到的 `disable` 本來就有下層值，沒作用的那些反而走不到。所以改用反事實重算。
- **「conf.d 裡有根 `_defaults.yaml`」就算可以判**：實測根 `_defaults.yaml` 存在但沒有 `defaults:`、只有 `_routing_defaults`、或 `defaults: {}` 時，都沒有任何閾值送出；而只有 `optional_overrides` 的樹沒有 defaults 也送得出值。所以改看「有沒有讀到根平台檔」，再交給現有的逐鍵原因。
- **讓 schema 本身當登錄表**（在 JSON Schema 加自訂欄位標讀取端）：兩個 Go module 都沒有 JSON Schema 函式庫，Go 端要用它就得新增依賴或手抄一份；而且 schema 今天是被程式碼「釘住」的一方，改由它驅動程式會把現有方向倒過來。

## 實作位置

- 登錄表：`components/threshold-exporter/app/pkg/config/reserved_keys.json`；`scripts/tools/dx/sync_schema.py --update` 產生、`--check` 比對（CI 由 `tests/dx/test_sync_schema.py` 執行）。
- 探針：`tests/shared/test_reserved_key_probes.py`。
- 共用函式：`scripts/tools/` 下的新模組，由 validate-config、產生器與產生器容器呼叫。
- exporter 實測：`da-guard served-values`（`components/threshold-exporter/app/cmd/da-guard/served_values.go`）。
- 規則讀取索引：`scripts/tools/ops/_threshold_alerts.py`、`rule-packs/threshold-registry.yaml`。

## 相關

- [ADR-036](./036-single-parser-effective-config.md)：YAML 只由 Go 解析，路由語意只由產生器判；本 ADR 修訂其 §9 的 exit 4 範圍。
- [ADR-037](./037-config-diff-compares-rendered-output.md)：config-diff 比較渲染結果；`disable` 新引入的問題出現在它的 PR 留言。
- [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)：拼錯的鍵改由讀取端實報判定，以及之後改成擋下的步驟。
