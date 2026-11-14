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
updated_at: 2026-11-14
---

# ADR-038: 沒有讀取端讀的鍵以實測判定，保留鍵由一份登錄表宣告

> **Language / 語言：** **中文 (Current)** | [English](./038-unread-keys-judged-by-readers.en.md)

## 狀態

✅ **Accepted**（2026-10-16 起草並由 owner 核可）。

2026-10-16 修訂（實作登錄表時依實測修正）：讀取端多一類 `policy_engine`；層多一層子目錄 `_defaults.yaml`；探針改為雙向，涵蓋所有比對得到輸出的讀取端（決定 2、3）。決定 8 的成本說明更新。

2026-10-17 修訂：`_` 開頭的鍵都不再產生閾值列，登錄表沒有的 `_` 鍵 exporter 完全不讀，與 JSON schema 一致（決定 2）；判決因此不再有 `platform_violation`，這類鍵判為 `unread`（決定 4、6）。

2026-11-05 修訂：閾值鍵的 `disable` 連帶關掉同一族的 `_critical` 與維度鍵，不分同族鍵來自哪一層；被連帶關掉的鍵判為有讀。「只關到一部分」的警告因此不再存在，一併移除；租戶自己檔裡的同族鍵被連帶關掉時報 `disable_cascade` 警告；只在 `dimensions:` 出現的 base 也算一族（決定 8）。

2026-10-17 修訂：自訂告警編譯器改照 exporter 的讀法編譯，兩者讀 `_custom_alerts` 的層相同（決定 2）。

2026-10-21 修訂：產生器那半以登錄表的宣告為準，由雙向探針保證與程式一致，不在每次判定時另外量產生器的輸出（決定 1、5）；Go 的寫入檢查只留值與結構的檢查，鍵名交給共用判決（決定 2）；profile 裡的 `_` 鍵依登錄表的 profile 層直接判、不經租戶（決定 4）；產生器容器接上共用判決前，served-values 先能只算指定的租戶，記憶體與時間預算照 ADR-036 §3（決定 5、11）。

2026-10-22 修訂：登錄表多記每個鍵的值的形狀、由 exporter 的哪個判斷式判值、批次操作可否移除這個鍵（決定 2）；exporter 讀的鍵，值在寫入關卡由 exporter 自己的解析程式判（決定 1）；新增決定 12，規定磁碟上既有的錯值怎麼處理。

2026-10-23 修訂：只算一筆寫入的租戶的模式由 served-values 擴及 effective，判決也只判那些租戶（決定 5）。

2026-10-24 修訂：產生器容器裡的判決有自己的那一份期限，判不完回「判決未完成」與原因，並記錄量到的成本（決定 5）。

2026-10-28 修訂：tenant-api 把判決附在寫入回應的 `warnings`，並訂定哪些 finding 給誰看（決定 5）。

2026-10-28 修訂：產生器 `--validate` 也報出共用判決；產生器容器執行產生器時不讓它再判一次（決定 5）。

2026-10-30 修訂：判決併入產生器容器的同一次判定，不另有期限、不讓位，取代 2026-10-24 的「判決有自己的那一份期限」；tenant-api 改成判到就擋之後的規則，取代 2026-10-28 的「判決只放進 `warnings`」：判決未完成時寫入不收，不設降級路徑。寫入檢查只對被寫的租戶做反事實重算（決定 2、5、8、11）。

2026-10-31 修訂：讀取租戶設定（GET）不再回報「這個鍵有沒有人讀」，也不即時判，取代「GET 的鍵名回報改用共用判決」；exporter 載入時只記它自己讀不到哪些鍵、不帶嚴重度。Go、Python、portal 的鍵名判斷刪除，`version` 標籤與改名提示經 da-guard 交給 CI 照印；維度鍵要它的每個 label 都在根 `_defaults.yaml` 的 `dimensions:` 為該 base 宣告過才送出（決定 11）。

2026-11-01 修訂：維度鍵改以 `dimensions:` 宣告的 label 判定是否送出，取代「base 已宣告即送出」；出貨清單由門檻登錄表生成（決定 11）。

2026-11-03 修訂：沒有讀取端讀的鍵一律 error，不分寫在租戶檔、profile、子目錄 `_defaults.yaml` 或根平台檔的 `tenants:`，例外只留只因 `disable_noop` 而未讀（warning）與 `_state_<filter>` 指向未宣告的 filter（notice）；profile 的每個鍵（含閾值鍵）逐 profile 直接判；等級與誰被擋分開，寫入閘門擋不擋只看 finding 所在的檔是不是這次寫入的檔（決定 4、5、6）。

2026-11-04 修訂：判決改由 da-guard 計算（`da-guard key-verdict`），取代「放在 Python 的共用函式」；需要判決的工具都讀它的輸出、不自己判（決定 5）。

2026-11-04 修訂：各閘門判到就擋，決定 4 的等級生效，擋不擋由判決的 `blocks` 欄位決定；整棵樹的 CI 也擋判不了的閾值鍵；寫入閘門比對新舊的單位跟著 finding 的單位走；profile 閾值鍵逐 profile 直接判延到 #3395（決定 4、6、7）。

2026-11-04 修訂：沒有租戶綁定的 profile 上的閾值鍵不判，報 `profile_not_judged` notice，取代「profile 的每個鍵都逐 profile 直接判」；tenant-api 的預覽走寫入同一套判定；tenant-api 與產生器容器擋不擋讀判決的 `blocks`（決定 4、5、6）。

2026-11-05 修訂：`dimensions:` 的 base 不能是 `<key>_critical`，這種宣告不宣告任何東西，`<key>_critical{…}` 一律沒有列（決定 11）。

2026-11-07 修訂：讀了但值讀不懂的保留鍵進共用判決（`value_unread`），在寫它的檔與層判，不在合併後的值上判：租戶檔 error，profile 與根平台檔 `tenants:` notice；`_metadata` 的子鍵與 `_severity_dedup` 也各有讀取端的值檢查。tenant-api 不因這類 finding 另外拒收，沿用決定 12 的寫入檢查（決定 10、12）。

2026-11-07 修訂：送出了但沒有告警比較的鍵由 validate-config 的 `no_alert` 列以 `metric_observed_map.yaml` 判、只報不擋；範例樹的 3 個 `_critical` 鍵是規則包缺 critical 層，不是索引沒處理後綴（決定 10）。

2026-11-12 修訂：沒經過 CI 就進了 exporter 所讀 ConfigMap 的樹，由 threshold-exporter chart 可選的 CronJob 跑 `validate-config` 判，失敗時告警（後果與已知限制）。

2026-11-13 修訂：閾值 registry 的 `retired_keys:` 登記的退役閾值鍵，不論寫在哪一層、有沒有讀取端讀，一律判 `unread`、原因 `retired`、等級 error，finding 帶點名替代鍵的提示；tenant-api 的 batch `unset` 可以刪它（決定 4）。

2026-11-14 修訂：保留鍵寫成排程（時窗或 `expires:`）進共用判決（`schedule_unread`），在寫它的檔與層判，等級同 `value_unread`（決定 10）。

## 摘要

**問題**：租戶設定裡拼錯的鍵大多默默失效。路由產生器 `--validate --strict` 對它們只給警告；da-guard（exporter 隨附的設定檢查工具）完全不報；validate-config 只擋得到 `_` 開頭的拼錯，閾值鍵的拼錯一樣只有警告。寫成 `disable` 想關掉的告警，也可能其實沒關掉。今天判斷「這個鍵合不合法」的是幾處各自維護的鍵名規則，彼此對不齊。

**讀取端**指會讀租戶設定的程式：exporter（把閾值變成 Prometheus 指標）、路由產生器（產生 Alertmanager 設定）、自訂告警編譯器（把 `_custom_alerts` 編成告警規則）。

**決定**：

1. 一個鍵有沒有用，**看讀取端實際做了什麼**，不從鍵名預測：exporter 的部分看它實際送出的值，產生器的部分看登錄表的宣告，而宣告由探針對照產生器的實際輸出。
2. 量不到的事實（每個 `_` 開頭的保留鍵歸哪個讀取端、可以寫在哪一層、哪些鍵已退役）寫在**一份登錄表**（JSON）。Python、Go 的保留鍵常數與兩份 JSON schema 的頂層宣告都由它產生。
3. 「沒有任何讀取端讀」的判決由**一個共用函式**計算，validate-config、產生器 `--validate` 與 tenant-api 的寫入檢查都呼叫它，不各自實作。
4. `disable` 沒關到任何東西時要報出來；並提供一個**讓使用者自己檢查與測試**的輸出。

本 ADR 決定怎麼判、怎麼報，以及 tenant-api 的寫入檢查改成擋下時依什麼規則（決定 5）。各閘門（validate-config、產生器 `--validate`、da-guard、tenant-api 的寫入檢查）何時改成「判到就擋」由 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016) 依序推進，屆時是 breaking change。

## 背景

### 名詞

- **保留鍵**：`_` 開頭、由平台定義意義的鍵，例如 `_silent_mode`、`_routing`、`_state_maintenance`。其他鍵是閾值鍵。
- **維度鍵**：只對某個 label 值生效的閾值，寫成 `metric{label="value"}`，例如 `redis_connected_clients{instance="redis-orders:6379"}`；去掉大括號的部分叫 base。
- **`_critical` 鍵**：`X_critical` 是閾值 `X` 的 critical 等級門檻。
- **profile**：平台定義的一組共用設定，租戶用 `_profile: <名稱>` 套用。
- **根平台檔**：conf.d 根目錄下 `_` 開頭的檔，例如所有租戶繼承的 `_defaults.yaml`。
- **served-values**：`da-guard served-values` 指令，輸出 exporter 對每個租戶實際送出的值，以及寫了卻沒送出的鍵（`unserved`）與原因。

### 一個例子

根 `_defaults.yaml` 與一個租戶檔：

```yaml
# _defaults.yaml
defaults:
  mysql_connections: 80
  mysql_slave_lag: 30
  redis_memory: 70
```

```yaml {.confd .expect-invalid}
# db-a.yaml
tenants:
  db-a:
    mysql_conections: "70"        # 拼錯
    mysql_slave_lagg: disable     # 拼錯，原意是關掉 mysql_slave_lag
    _routng: disable              # 拼錯，原意是停用路由
    redis_memory: disable         # 關掉這個告警
    redis_memory_critical: "90"
    _namespaces: [prod-ns]        # 給工具看的標註
```

起草時（2026-10-16）在 main 上實跑的結果：

| 工具 | 結果 |
|---|---|
| exporter 實際送出（served-values） | `mysql_connections: 80`、`mysql_slave_lag: 30`、`redis_memory_critical: 90`；沒有送出的：`mysql_conections`、`mysql_slave_lagg`、`_routng`、`redis_memory`、`_namespaces` |
| 產生器 `--validate --strict` | rc 0，三行警告：`unknown reserved key '_routng' (typo?)`、`unknown key 'mysql_conections' not in defaults`、`unknown key 'mysql_slave_lagg' not in defaults` |
| validate-config | rc 1：schema 擋下 `_routng`；兩個閾值鍵的拼錯只列警告 |
| da-guard | rc 0，沒有任何 finding |

validate-config 擋下了 `_routng`，但其餘幾件事都沒有人擋：

- 租戶以為門檻是 70，實際生效的是預設的 80。
- 租戶以為關掉了 `mysql_slave_lag`，它照樣以 30 送出。
- `redis_memory_critical` 照樣送出 90：租戶寫了 `redis_memory: disable` 以為整個告警關了，critical 告警卻照常會響；同一條從子目錄 `_defaults.yaml` 或 profile 繼承來時也一樣。決定 8 改成連帶關閉。

### 為什麼不再補鍵名規則

「哪些鍵合法」今天由幾處各自判斷：產生器的保留鍵檢查、exporter 載入時的檢查、tenant-api 的寫入檢查，以及 `_state_`／`_routing` 的前綴規則。保留鍵清單本身也有三份：Python 常數、Go 常數與 JSON schema，靠比對測試保持一致。逐一補齊做不到，原因是同一個鍵合不合法，取決於讀取端怎麼讀，而不是它叫什麼名字：

- **維度鍵**：`es_heap_usage_percent{node="es-hot-01"}` 的 base 不在 defaults 裡，產生器的鍵名檢查判它不合法；exporter 載入時的鍵名檢查也判它不合法，但只寫一行 log，照樣送出它。主 conf.d 上產生器有 17 條這類警告，其中 15 條的鍵 exporter 實際有送出（另外 2 條的值是 `disable`）。這 15 個鍵有送出，所以不是「沒人讀」；它們的問題是沒有任何告警規則讀（見決定 10）。
- **`_` 開頭但沒有登記的鍵**：JSON schema 拒收 `_myth`；產生器 `--validate` 只在根 defaults 沒宣告它時警告；exporter 則在根 defaults 宣告了它時，把租戶寫的 `_myth: "7"` 當成閾值送出 7。三者對同一個鍵的結論不同；決定 2 讓 exporter 改以登錄表為準。
- **profile 帶來的錯字**：exporter 算在租戶頭上，產生器不檢查。
- **`_state_` 前綴**：任何拼法都算合法。

所以直接把現有的警告改成擋下，主 conf.d 會當場出現 15 條誤報。

## 決定

### 1. 判決以實測為準

一個鍵的讀取端只來自實測（產生器那半的實測是決定 3 的探針）：

- **exporter**：served-values 有沒有送出該鍵，或有沒有因為它而改變送出的值。`disable` 讓本來會送出的一列不送出，也算 exporter 讀了它；`disable` 沒有改變任何送出的值，則是 `disable_noop`（見決定 8）。exporter 實測送出的鍵判為有人讀，不論登錄表怎麼寫。
- **產生器**：登錄表宣告的、產生器在各層讀的保留鍵。產生器在租戶層逐鍵讀的只有固定幾個 `_` 鍵（決定 3 的探針與「考慮過但否決」），所以這一半不在每次判定時重跑產生器，而是由雙向探針保證登錄表與產生器的程式一致；探針不一致時 CI 紅。
- **自訂告警編譯器**：它讀的 `_custom_alerts`。

不另寫一份「產生器會讀哪些鍵」的程式邏輯給別的讀取端模仿；登錄表只是宣告，由探針兩個方向對照產生器的實際輸出。某個值在產生器的語意下是否有效（例如 `_routing_profile` 指向的 profile 存不存在），仍只由產生器判，與 [ADR-036](./036-single-parser-effective-config.md) 一致。

值也照同一個原則判。exporter 讀的鍵，值合不合法在寫入關卡由 exporter 自己的解析程式判：寫入檢查呼叫 exporter 讀這個值時用的同一段程式，不另寫一份詞表或規則。產生器讀的鍵，值仍只由產生器判，寫入關卡不複製產生器的規則。

### 2. 保留鍵登錄表

量不到的事實寫在一份 JSON 登錄表，放在 exporter 的 Go module 內（`components/threshold-exporter/app/pkg/config/`），內容限於頂層 `_` 命名空間：

- 每個保留鍵與前綴（`_silent_mode`、`_routing`、`_state_` …）屬於哪個**讀取端類別**：`exporter`、`generator`、`custom_alert_compiler`、`policy_engine`（Policy-as-Code，讀根 `_defaults.yaml` 的 `_policies`）、`annotation`（只給人或工具看的標註，例如 `_namespaces`）。一個鍵可以屬於多個讀取端。
- 每個讀取端**在哪些層讀這個鍵**：租戶檔、根平台檔的 `tenants:`、profile、子目錄 `_defaults.yaml` 的 `defaults:`、根平台檔的頂層。同一個鍵，各讀取端讀的層可以不同：例如登錄表宣告子目錄 `_defaults.yaml` 的 `defaults:` 裡的 `_severity_dedup` 由 exporter 讀、產生器不讀。
- **已退役的鍵**與給使用者的提示文字。
- 說明文字（給 schema 的 description 與 explain 輸出）。
- 每個鍵的**值**：
  - 形狀：`scalar`（純量）、`mapping`（對映）、`list`（清單），或 `scalar_or_mapping`（兩種寫法都讀，例如 `_silent_mode: warning` 與 `_silent_mode: {target: warning}`）。租戶設定裡讀得到的鍵一定要寫；只在根平台檔頂層讀的鍵可以省略。
  - `check`：由 exporter 的哪個判斷式判值。只有 exporter 讀的鍵能寫；判斷式在 Go 裡以同一個名稱綁定，Go 測試要求登錄表寫到的每個名稱都有綁定、每個綁定都有鍵用到。
  - `refers_to`：值指向哪一層的項目，例如 `_profile` 的值是 profile 層裡的一個 profile 名稱。
- **批次操作可否移除**（`batch.unset`）：tenant-api 的批次操作能不能 unset 這個鍵。已退役的鍵一律可以移除、不必另寫：沒有讀取端讀它，移除不改變任何輸出；而它留在檔裡時，寫入檢查會擋下該租戶的其他寫入，批次必須能把它拿掉。決定租戶範圍的鍵（`_metadata`，以及下一段推得的鍵）不能設成可移除，產生常數時會拒絕這樣的登錄表。

tenant-api 從這些欄位推出兩份清單，不再各自手寫：形狀是 `mapping` 或 `list` 的鍵，批次 patch 的字串值設不了；`refers_to` 指向的層若是 exporter 讀 `_metadata` 的層，該鍵就會改變租戶的 `_metadata`，也就是租戶的環境與領域，屬於決定租戶範圍的鍵（目前只有 `_profile`）。

今天的三份保留鍵清單改由登錄表產生，CI 重新產生後比對差異，有差異就紅：

| 今天 | 之後 |
|---|---|
| Python 的保留鍵常數 | 由登錄表產生 |
| Go 的保留鍵常數 | 由登錄表產生 |
| `tenant-config.schema.json` 與 `platform-defaults.schema.json` 的頂層宣告 | 由登錄表產生 |
| 產生器、exporter、tenant-api、validate-config 各自的鍵名判斷 | 由決定 4 的共用判決取代 |

**值與結構的檢查不在此列，留在 Go**。共用判決只回答「有沒有讀取端讀這個鍵」，不判斷值對不對。下面幾種鍵都有 exporter 讀，判決是 `read`，但今天會擋下 tenant-api 寫入的是 Go 的 `ValidateTenantKeys`，所以它保留這些檢查、只刪掉判斷鍵名的那一段：

| 情況 | 判決 | Go 寫入檢查 |
|---|---|---|
| `_profile` 指向不存在的 profile | `read` | 擋 |
| `expires:` 不是 RFC3339 時間 | `read` | 擋 |
| `expires:` 加在只宣告於 `optional_overrides`、沒有平台預設值的鍵上 | `read` | 擋 |
| `expires:` 加在 `_critical` 鍵、維度鍵或保留鍵上（不會生效、也不會回復） | `read` | 擋 |

值的檢查之後也走這條路：登錄表 `check` 指名的判斷式由 Go 寫入檢查呼叫（決定 1）。例如 `_silent_mode: purple`，exporter 讀了它（判決是 `read`），只是不認得這個值、什麼都不靜音；擋下它的是這個值檢查，不是鍵名判決。磁碟上已經有的錯值怎麼處理見決定 12。

鍵名那一段指的是：登錄表沒有的 `_` 鍵、登錄表有但不在租戶層讀的 `_` 鍵（例如租戶檔裡的 `_policies`、`_routing_defaults`）、`_routing` 前綴下產生器不讀的鍵、平台預設與 `optional_overrides` 都沒有的閾值鍵（維度鍵見下一段）、base 沒有平台預設值的 `_critical` 鍵。這幾種沒有讀取端讀，共用判決判為 `unread`，改由它判，同一件事只有一個地方說了算。維度鍵 `version` 標籤值的檢查屬於值的檢查，留在 Go；舊鍵名改名的提示是遷移說明、不擋寫入，也留在 Go。

base 不在平台預設、也不在 `optional_overrides` 的維度鍵，Go 今天擋，但 exporter 照樣送出它，判決是 `read`。2026-10-31 修訂：exporter 改為不送出這種鍵（決定 11），判決讀成 `unread`，所以 Go 鍵名段刪除後 tenant-api 依判決照樣擋下，入口沒有因此放寬。

tenant-api 那條入口的 Go 鍵名段，要等 tenant-api 自己改成依共用判決擋下的同一步或之後才刪，不因 validate-config 等其他入口先擋就提前刪。判決未完成時寫入不收（決定 5），所以刪除之後，這條入口只在一處比今天寬鬆，而且是刻意的：只因 `disable_noop` 而未讀的鍵（決定 4 只報 warning；例如 `mysql_slave_lagg: disable`，今天 Go 會擋）。

執行時各工具讀的是產生出來的常數，不在執行時讀這份 JSON，所以 da-tools 映像不需要多帶檔案。登錄表放在 Go module 內，是為了讓 Go 端日後若要直接嵌入，三個 Go 程式（exporter、da-guard、tenant-api）都不必再複製一份。

**範圍只到頂層鍵名與值的形狀**。值接受哪些字（例如 `_silent_mode` 認得哪些模式）與子鍵表都以讀取它的程式為準，不進登錄表：`_routing` 與 `_metadata` 的子鍵由程式碼定義，schema 與其他端以測試釘住；`_custom_alerts` 條目的欄位寫在 Go 的 recipe 契約檔，schema 由它產生：

| 子鍵 | 以誰為準 |
|---|---|
| `_routing` 的子鍵、receiver 型別、`_routing_enforced` 的欄位 | 產生器的程式碼 |
| `_metadata` 的 9 個欄位 | Go 的 `TenantMetadata` 結構 |
| `_custom_alerts` 條目的欄位 | recipe 契約檔（[ADR-024](./024-version-aware-threshold-via-dimensional-label.md) §9） |

這條分界讓讀者知道某個清單該去哪裡改：頂層保留鍵改登錄表，子鍵改讀取它的那支程式。

**`_` 命名空間以登錄表為準**：登錄表的 `_` 鍵沒有一個是閾值，所以 `_` 開頭的鍵在任何一層、任何形狀（base、`_critical`、維度鍵）都不產生閾值列，根 defaults 宣告了也一樣；登錄表沒有的 `_` 鍵則沒有任何讀取端讀，與 JSON schema 一致。例如根 defaults 寫 `_myth: 5`、租戶寫 `_myth: "7"`，exporter 不為 `_myth`、`_myth_critical`、`_myth{x="a"}` 產生任何列；served-values 把它列進 `unserved`，原因是 `unregistered`，`da-guard effective` 的 `not_served` 也標 `unregistered`；tenant-api 的寫入依判決拒收；`expires:` 也不再為它發出過期事件。根 defaults 寫成數字的登錄表鍵（例如 `_namespaces: 5`）同樣不再成為閾值，判決照決定 4 判為 `unread`。這是 breaking change：依賴根 defaults 宣告這種鍵的部署，升級後少掉那些列；遷移方式是改用登錄表有的鍵，或拿掉 `_` 前綴讓它成為一般閾值鍵（[#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)）。

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
| `unread` | 沒有任何讀取端讀它；登錄表沒有的 `_` 鍵一律是（原因 `unregistered`），已退役的 `_` 鍵也是（原因 `retired`）；閾值 registry `retired_keys:` 登記的退役閾值鍵也是（原因 `retired`），即使 exporter 仍為它送出一列——部署方根 `defaults:` 寫的也算；finding 的 `hint` 點名替代鍵 | error，不論寫在租戶檔、profile、子目錄 `_defaults.yaml` 還是根平台檔的 `tenants:`；來自租戶檔以外的檔時指名那個檔。例外兩個：只因 `disable_noop` 而未讀：warning（見決定 8）；`_state_<filter>` 指向平台沒有宣告的 filter：notice（見決定 7） |
| `undecidable` | 沒有讀到任何根平台檔，租戶寫的閾值鍵在 exporter 那半無從判斷；保留鍵照常判（見決定 7） | 判決輸出回 rc 2 |

同一個錯字來自平台檔時只報一次（依檔案與鍵去重），並列出受影響的租戶，不會變成每個租戶各一筆。

profile 裡的鍵在該 profile 檔報，`_` 鍵與閾值鍵判法不同：

- `_` 鍵逐 profile 直接判，不經租戶：依登錄表的 profile 層，沒有任何讀取端在 profile 這一層讀的鍵判為 `unread`，不論有沒有租戶綁定，列出綁定它的租戶（可以一個都沒有）。例如寫在 profile 裡的 `_profile` 永遠被租戶那一層蓋掉，只有這樣判才報得出來。
- 閾值鍵經綁定它的租戶判：exporter 會不會為它建閾值列，是在租戶上實測出來的（served-values），沒有不經租戶就能呼叫的判定。profile 寫了只在 `optional_overrides` 宣告、沒有平台值的鍵時，exporter 不填入（#1189），effective 以 `profile_not_filled` 指名它；判決把它判為 `unread`、等級 error，掛在 profile 檔，列出綁定的租戶。沒有租戶綁定的 profile 上的閾值鍵**不判**，每個鍵報一筆 `notice`（種類 `profile_not_judged`），不擋，只把「沒判」和「判了沒事」分開；這筆只在判整棵樹時出現（只判部分租戶時無從得知 profile 有沒有人綁）。
- Policy-as-Code 讀的是租戶看到的設定，所以只有真的有租戶拿到這個值時，規則指名它才算有人讀。

不逐 profile 判閾值鍵的理由：要判就得模擬一個綁定該 profile 的租戶，而兩輪實測找出多處與真實載入不同的地方（`optional_overrides` 與別名雙拼的填入、維度鍵原寫法、同名 profile 跨根檔逐鍵合併、policy 讀取端、只判部分租戶）；另一條路是從 exporter 的建列程式抽出「這個鍵會不會出列」的純函式，但建列判斷散在多處，抽出後要先證明與實測等價，成本高。設定系統的慣例也是用到才驗（Helm 只驗渲染到的 template、Kustomize 只驗被引用的 base）。代價見決定 6：沒有租戶綁的 profile 上的錯鍵，等到有租戶綁定時才判到。

例：根 `_defaults.yaml` 沒有宣告 `redis_evicted_keys`，租戶 `t1` 寫了 `_profile: gold`：

```yaml
# _profiles.yaml
profiles:
  gold:
    redis_evicted_keys: "11"   # t1 綁 gold
  silver:
    redis_evictd: "5"          # 拼錯，而且沒有租戶綁 silver
```

`redis_evicted_keys` 判為 `unread`、等級 error，報在 `_profiles.yaml`，列出 `t1`；`redis_evictd` 不判，報一筆 `profile_not_judged` notice，租戶清單是空的。誰被擋見決定 6。

`disable_noop` 只報 warning、拼錯的閾值鍵報 error，理由是：一個沒關到東西的 `disable` 不會讓任何值錯誤生效，也可能是刻意的保險寫法（例如預設日後才會加上的鍵）；它的風險在於原意要關的東西沒關到，所以除了 warning，這次變更新引入的還會出現在 config-diff 的 PR 留言（見決定 8）。

等級只描述缺陷，不決定誰被擋；一道寫入閘門擋不擋，看的是 finding 所在的檔（決定 6）。

上表的等級隨各閘門改成「判到就擋」一起生效（[#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)，breaking change）：整棵樹的 CI 有 error、或有判不了的閾值鍵就擋（決定 6、7）。擋不擋只在判決裡定一次：每筆 finding 帶 `blocks`（等級是 error 時為 true），判決文件頂層的 `blocks` 表示整棵樹該不該擋；各閘門讀這兩個欄位，不自己依等級另判。

沒有租戶綁定的 profile 上的閾值鍵不判，報 `profile_not_judged` notice（上文，[#3395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3395)）。

### 5. 計算放在哪

「沒有任何讀取端讀」需要把 exporter 與產生器兩邊的結果併起來，由 da-guard 計算，指令是 `da-guard key-verdict`；需要判決的工具都讀它輸出的 JSON，不自己判：

- exporter 那半走 exporter 自己的載入，與 served-values 同一條路徑。
- 它併上登錄表宣告的其他讀取端（見決定 1），以及根 `_defaults.yaml` 的 policy 規則點名的鍵。
- validate-config、產生器 `--validate`、`explain-keys` 與 tenant-api 的產生器容器（tenant-api 每次寫入前呼叫的產生器，見 [ADR-036](./036-single-parser-effective-config.md) §3）都呼叫它。產生器 `--validate` 把判到的每一筆印成一行（與 validate-config 的行相同），判決會擋的（`blocks`）讓結束碼 1（決定 4 末段）；被拒收的樹不判。容器在產生器放行之後、在同一次判定裡判（下一點），所以它執行產生器時，產生器不再判一次。
- 產生器容器每次寫入只需要被寫入的租戶的判決，所以 `da-guard key-verdict`（連同它底下的 served-values 與 effective）都要能只算指定的租戶（`--tenant`；平台檔照常整份讀，每個要求的租戶得到的與整棵樹時相同，樹裡沒有的列在 `tenants_absent`）。判決也只判這些租戶，沒要求的租戶不出現，不會被讀成「沒有讀取端讀」。/metrics 能不能收集不屬於單一租戶，子集裡只就要求的租戶判（別的租戶收集失敗看不到）。profile 鍵依決定 4 逐 profile 直接判，所以有沒有 finding 與要求了哪些租戶無關，只是 finding 列出的綁定租戶只含被要求的租戶。profile 裡的 `_` 鍵今天已經這樣判；閾值鍵在改成逐 profile 判之前經由綁定的租戶判，子集裡看不到沒被要求、但綁同一個 profile 的租戶，所以可能出現整棵樹不會給的 finding。判一筆寫入時，呼叫端要把被寫檔案寫入前後宣告的租戶都列進去。
- 容器裡的判決只在產生器放行的寫入之後跑，與產生器**共用同一次判定的期限**（ADR-036 §3 的期限以該節為準，不在此重述），不另有自己的期限，也不因有寫入在排隊而讓位。時間或記憶體用完、da-guard 失敗，或結果是 `undecidable`，都回「判決未完成」與原因。容器裡的反事實重算只算被寫的租戶（決定 8）；error 級的 finding 不截斷，回應裡顯示行數的上限只管顯示。寫到 defaults 檔，或寫到前後版本帶 `profiles:` 的根平台檔時不判：這種檔的鍵會影響它沒有列名的租戶，只有整棵樹的判決判得到，同樣回「判決未完成」並指名那個檔。判決約佔一次判定的三成，一次判定的時間隨租戶數成長；以 1 CPU 為例，約 2500 個租戶時就接近 ADR-036 §3 所說的期限一半（示意值，依主機而異）。實際的界線看一個 metric：一次判定耗時與判定期限的比值，名稱由實作決定。比值超過 0.5 時告警，留一半給排隊與租戶成長（與 ADR-036 §3 撤快取條件用的是同一個比例），到 1 時寫入就會因判不完而被拒。各結果的次數與最近一次的耗時也是 metric。
- tenant-api 改成判到就擋之後（[#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)）：
  - 被寫入的檔裡、屬於寫入對象租戶、判決說會擋（`blocks`）的 finding，讓寫入不收，回 400，與今天鍵名檢查擋下時相同，回應列出這些 finding。這筆寫入沒有碰到、但已經在該檔裡的這類 error 也算，與今天 Go 的鍵名檢查相同；決定 12 的作法只用於新加的值檢查。其他檔上的 error 不擋這筆寫入（決定 6）。
  - 判決未完成時寫入不收。依 ADR-036 §3 的分界，這是「拒收該筆」，不是容器不可用：時間不夠的回可重試的錯誤，其他原因回不可重試的錯誤並說明原因。`undecidable` 是沒讀到任何根平台檔，重試也不會改變，回應指名缺少根平台檔；今天 Go 對這種樹的閾值鍵也會擋。
  - 擋不擋讀判決的 `blocks`，tenant-api 與容器都不依等級另判；容器原樣轉送每筆 finding 的 `blocks`，判不了的閾值鍵讀判決的 `undecidable_blocks`。finding 不帶 `blocks`（映像早於 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)）與沒有回判決同樣是容器太舊，回 503。
  - 找不到或無法執行 da-guard，或容器沒有回判決（映像早於這一步），依 ADR-036 §3 是容器不可用，回 503；與找不到 da-read 相同，每次判定前先確認 da-guard，所以長期缺 da-guard 會出現在容器可用性的告警上，不會變成一筆筆的拒收。
  - 其餘的 finding 放進寫入回應的 `warnings`。

  在那之前，判決只放進 `warnings`，不擋寫入；判決未完成或容器沒有回判決時，回應明說沒有判決，不留成看起來像「沒找到問題」。

  誰看得到什麼，沿用寫入回應既有的揭露規則：被寫入的檔案裡、屬於這筆寫入對象租戶的 finding，寫入者一律看得到；同一檔案裡其他租戶的 finding，只在呼叫者能讀那個租戶時顯示；平台檔、profile，或這筆寫入沒有動到的檔案上的 finding，只給能看整棵樹的呼叫者，其他人只看到一行「有你看不到的 finding」；伺服器日誌只記這些 finding 的數量與種類，不記內容，因為內容會引用租戶寫的值。判決未完成時只顯示原因，不轉述容器的訊息，因為那段訊息可能帶有平台檔的路徑或程式的 stderr。直接寫入的批次裡，租戶的 finding 掛在該租戶第一個有變更的操作上，整筆寫入共用的那幾行只掛一次，因為非同步批次的任務只保留逐操作的結果；只報不擋時，判決未完成則每個有變更的操作都寫，因為只看得到部分結果的讀者不能把其他操作讀成「沒找到問題」。PR 模式的回應只有一份清單，每行自己寫出租戶名。清單有行數上限，超過時先留寫入對象租戶自己的行。判決看的是寫入之後的樹，不與寫入之前比較，所以列出的問題可能早就存在；內容沒有改變的寫入不附判決。
- tenant-api 的預覽（`POST /tenants/{id}/validate`，direct mode）走寫入同一套判定：PUT 的拒收步驟通過後，問產生器與判決，拒收的理由與文字與 PUT 相同（[#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)）。產生器一次只判一筆，預覽不能把寫入擠出佇列，所以預覽不拿寫入鎖、同時只判一筆，其餘直接回「沒判」與原因，不排隊（預覽仍佔佇列裡的一個位置，剛好排在它後面的寫入最多多等一次判定）；預覽被拒不計入寫入的拒收計數，有產生器 finding 時也不再判一次 base。PUT 會回 429 或 503 的情況（忙、時間不夠、容器不可用），預覽回 `judged: false` 與原因，`valid` 只反映前面的步驟；chart 對「容器不可用時讀取照常」的承諾因此不受影響。PR mode 的本地樹可能落後 base，預覽照舊只做 body 預檢。
- 判決未完成時**不設降級路徑**：既不照收，也不退回另一份鍵名判斷。照收會讓今天會被擋的拼錯鍵進 repo；退回 Go 的鍵名判斷，等於永久保留第二份判斷（決定 2）。代價是 da-guard 壞了，tenant-api 的寫入就不收。但產生器容器本來就是寫入的必要條件，產生器壞了寫入也不收；判決併進同一次判定，增加的是它的工作量，不是多一個依賴。

判決只用三種事實：exporter 實測的讀取與原因、登錄表對其他讀取端的宣告、policy 規則點名的鍵。產生器負責的鍵依登錄表的宣告判，不另外量產生器的輸出（決定 1）。policy 規則有沒有效由 Policy-as-Code 自己判：它拒收的規則從來不執行，所以 Python 的呼叫端（validate-config、產生器 `--validate`、`explain-keys`、產生器容器）先用它載入規則，拒收的樹不判，不拿無效的規則把鍵算成有人讀。已知限制：`da-guard write` 的寫入檢查不經 Policy-as-Code，規則讀得到 `target` 就算，所以無效的規則仍會讓它點名的鍵算成有人讀；這種樹在 validate-config 的 `policy_dsl` 列是 FAIL。

例如租戶檔寫了拼錯的鍵：

```yaml
# _defaults.yaml
defaults:
  mysql_connections: 80
# db-a.yaml
tenants:
  db-a:
    mysql_conections: "70"
```

`da-guard key-verdict --config-dir conf.d` 對這個鍵輸出的那一筆與 finding：

```json
{"key": "mysql_conections", "layer": "tenant", "file": "db-a.yaml", "read_by": [],
 "verdict": "unread", "severity": "error", "reason": "no_row", "disable": null,
 "value": "70", "served": null}
{"severity": "error", "kind": "unread", "key": "mysql_conections", "file": "db-a.yaml",
 "tenants": ["db-a"], "reason": "no_row", "blocks": true}
```

這帶來一個連動修訂：[ADR-036](./036-single-parser-effective-config.md) §9 規定單獨執行的 da-guard 對它判不了的內容回 exit 4（未判定）。原本觸發條件只有「樹上有路由或 policy 內容」，現在擴大為「樹上有只有產生器能判值的內容」，例如租戶寫了 `_routing_profile`，它指向的 profile 存不存在只有產生器能判。鍵名本身不需要產生器：共用判決以 da-guard 與登錄表就判得出來（決定 1）。否則單獨跑 da-guard 的人會把「沒判」讀成「判過、沒問題」。exit 4 本身要等 ADR-036 §7 的步驟 2（da-guard 加上「未判定」）完成後才存在，這個擴大範圍也從那時起生效。

### 6. 平台側的問題不算在租戶頭上

決定 4 的等級只描述缺陷。一道寫入閘門擋不擋一筆寫入，只看 finding 所在的檔是不是這次寫入的檔，與等級來自哪一層無關：

- **tenant-api**：error 只在 finding 所在的檔就是這次寫入的檔、而且屬於寫入對象租戶時才擋（決定 5）。所以 profile、子目錄 `_defaults.yaml` 或其他平台檔上的 error，不擋只改了租戶檔的寫入；寫到 defaults 檔或帶 `profiles:` 的根平台檔時判決不判（決定 5）。今天已經這樣判。
- **經 `da-guard write` 寫檔的工具**（改租戶檔、profile 檔、子目錄 `_defaults.yaml` 或根平台檔）：這次寫入讓被寫的檔新出現 error 時被擋；寫入前就有的 error 不擋。與 tenant-api 不同（它連被寫的檔裡原本就有的 error 也擋，決定 5），這裡只擋新出現的；這個差異是刻意的：整棵樹的 CI 擋住任何 error，main 上平常沒有既有的 error，tenant-api 擋既有的是讓租戶自己的檔先修好，`da-guard write` 常一次改多個平台檔，擋既有的會讓與之無關的修改都過不了。新舊的比對單位跟著 finding 的單位走：鍵寫在租戶自己那一筆（租戶檔，或根平台檔 `tenants:` 下它那一筆）時以「租戶＋鍵」比對，所以把租戶搬到別的檔不算新出現，同檔另一個租戶新寫錯則算；其他 finding（鍵來自 profile、子目錄 `_defaults.yaml` 等繼承的檔，或沒有列出租戶）以「檔案＋鍵」比對，寫入前同一個檔與鍵已有 error 就不算新的，所以解除或改綁 profile 不會讓它原有的錯變成新的，profile 新寫錯的鍵也不會因為租戶檔裡已有同一個錯而放行。已知限制：同一個根平台檔裡 profile 與 `tenants:` 下的租戶寫了同一個錯鍵時，兩者是同一筆 finding，其中一邊新寫進同一個錯不算新出現。這條規則只在 `pkg/keyverdict` 定一次（`NewBlocks`）。判不了的閾值鍵寫入時不擋，交給整棵樹的 CI 擋。
- **整棵樹的 CI**（validate-config、產生器 `--validate`）：判到 error、有判不了的閾值鍵、或判決沒有給出時就擋，不分 finding 在哪個檔；讀判決文件的 `blocks`。

平台側的問題不算在正在修改的租戶頭上，由上面的檔案範圍保證，不靠降低平台側 finding 的等級。

例：根 `_defaults.yaml` 沒有宣告 `redis_evicted_keys`，租戶 `t1` 綁 profile `gold`（「之後」是各閘門改成判到就擋之後）：

| 寫法 | 改成判到就擋之前 | 之後 |
|---|---|---|
| profile `gold` 寫 `redis_evicted_keys: "11"` | 判決報 notice，掛在 `_profiles.yaml`；da-guard 沒有 finding | error，掛在 `_profiles.yaml`，列出 `t1`。讓這筆 error 新出現的 `_profiles.yaml` 寫入被擋，整棵樹的 CI 擋；`t1` 自己經 tenant-api 的寫入照收 |
| 子目錄 `_defaults.yaml` 寫 `redis_evicted_keys: 100` | 判決報 notice；da-guard 報 error（子目錄的值送不到 exporter），兩者等級不一致 | 兩者都是 error，掛在子目錄 `_defaults.yaml`。改這個檔的寫入與整棵樹的 CI 照上一列；子目錄下租戶自己的寫入照收 |
| 沒有租戶綁的 profile 寫錯閾值鍵（決定 4 例子裡的 `silver`） | 沒有任何 finding | 不判，報 `profile_not_judged` notice，不擋（決定 4，[#3395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3395)）。有租戶綁定之後判為 error、掛在 profile 檔，只有整棵樹的 CI 擋：綁定那筆寫入只改租戶檔，tenant-api 與 `da-guard write` 都不擋，只給 warning |

### 7. 無從判斷的情況

exporter 那半能不能判，取決於**有沒有讀到任何根平台檔**，而不是 conf.d 裡有沒有某個檔：

- **沒讀到任何根平台檔**（例如只放了單一團隊目錄的分片樹）：租戶寫的閾值鍵標 `undecidable`。保留鍵照常判。整棵樹的 CI 擋下，因為沒判的鍵不能當成判過、沒問題；要驗，就驗組好的整棵樹。tenant-api 對判不了的寫入同樣不收。
- **有讀到根平台檔，但沒有宣告任何閾值**（例如只有 profiles、只有子目錄的 `_defaults.yaml`）：這是可以判定的錯誤，照現有的精確 finding 報（例如「子目錄的 defaults 送不到 exporter」），不降級成無從判斷。
- **只有路由設定的樹**：沒有閾值鍵，也就沒有判不了的鍵，rc 0。

拼錯的閾值鍵與「平台沒宣告、租戶有意省略」得到的原因相同，判決不嘗試區分兩者。

`_state_<filter>` 指向平台沒有宣告的 filter，或 filter 宣告在子目錄（不會被讀）時，原因在平台檔。這類先報成平台側的 notice，指名缺的宣告；等到「平台刪掉 filter 時不會讓既有租戶懸空」的守衛到位，才考慮升級。

### 8. `disable` 關掉什麼、有沒有作用

閾值鍵寫 `disable`，關掉的是**一整族**：base 鍵本身、`<key>_critical`，以及維度鍵 `<key>{…}`（租戶寫的，和根目錄 `defaults:` 給的維度預設值），不論同族鍵寫在哪一層（租戶檔、子目錄 `_defaults.yaml`、profile、根平台檔 `tenants:`）。排程式的 `disable` 只在它那段時間連帶。base 的 `disable` 過期（`expires:`）後是否還算數，跟 base 自己的規則走：根目錄 `defaults:` 有值的鍵過期即失效，其他（`optional_overrides:` 宣告的鍵、只在 `dimensions:` 出現的鍵）不看 `expires:`。只在 `dimensions:` 出現的鍵也是一族的 base：子目錄 `_defaults.yaml` 寫它的 `disable` 會傳到租戶，同樣連帶；子目錄給它其他值則沒有讀取端讀，照舊報 `subtree_default_undeliverable`。`disable:critical` 這類帶等級的寫法不是停用，不連帶。

只想留 critical 那一級，寫 `X: "90:critical"`，並刪掉 `X_critical`：它送出的 series 與舊的 `X: disable` 加 `X_critical: 90` 相同，兩者並存則會送出同一條 series 兩次，整份 `/metrics` 失敗。只想停 critical 那一級，寫 `X_critical: disable`。

被連帶關掉的同族鍵，exporter 讀了它才決定不送，所以判為有讀（`read_by` 為 exporter）。它寫在租戶自己的檔裡、且在一天中某個時段被連帶關掉時，報 `disable_cascade`（warning，不擋），並寫出是哪個 base：舊語意下它會照送。被關掉的是 `<key>_critical` 時，上面的遷移寫法適用；是 `<key>{…}` 時沒有替代寫法（見後果與已知限制），`X: "90:critical"` 只會另加一條不分維度的 critical 列；繼承來的同族鍵被關掉正是 `disable` 要的，不報。判定一樣是反事實重算：拿掉 base 的 `disable` 後才有列的同族鍵，就是被它連帶關掉的鍵。`/effective` 的 `not_served` 與 served-values 的 `keys`、逐時段 `schedules` 以原因 `base_disabled` 標出。這個原因不是 `value_not_served`：不送正是 `disable` 要的。同族鍵的值本身讀不懂時，照樣報 `value_unparsed_dropped`，不被 `base_disabled` 蓋掉。

`disable` 有沒有作用，以反事實重算判定：

| 報告 | 什麼情況 | 怎麼判 |
|---|---|---|
| `disable_noop` | 拿掉這個 `disable`，送出的值完全不變 | 反事實重算：假設這個 `disable` 不存在，再算一次，看結果有沒有變；重算在 Go 內進行 |
| `disable_cascade` | 租戶自己檔裡寫的 `<key>_critical`／`<key>{…}`，在某個時段被 base 的 `disable` 連帶關掉 | 同一次反事實重算：拿掉 base 的 `disable` 後才有列的同族鍵 |

- 保留鍵（`_routing: disable`、`_silent_mode: disable` …）沒有豁免，照決定 4 判。
- `disable_noop` 常見的原因是鍵拼錯：上面例子的 `mysql_slave_lagg: disable` 沒關到任何東西，真正的 `mysql_slave_lag` 照樣送出。
- 反事實重算要看整天的時段（含排程值），不只看某一個時間點，否則時窗內與時窗外的判斷會相反。重算時另外檢查：base 在某時段是 `disable`，那個時段同族就不得有列；有的話 da-guard 以內部錯誤結束，不當成判定。這條檢查抓的是 exporter 某個讀同族鍵的地方漏了連帶。
- 成本：一個租戶的閾值列只取決於它自己的設定與平台的設定，所以每個租戶只在自己的排程切點重算，每次只建入同一輪的租戶；第 k 輪把所有租戶各自的第 k 個 `disable` 一起拿掉，輪數等於單一租戶最多有幾個 `disable`，與租戶數無關。
- 寫在 profile 或平台檔的 `disable` 影響所有選用它的租戶，報在那個檔上。
- `disable_noop` 與 `disable_cascade` 在整棵樹報警告。這次變更**新引入**的 `disable_noop`，另外出現在 config-diff 的 PR 留言（被連帶關掉的同族鍵在 config-diff 的閾值變更裡標為停用），因為 reviewer 實際會看的是那裡（[ADR-037](./037-config-diff-compares-rendered-output.md)）。
- tenant-api 的寫入檢查只對被寫的租戶重算：輪數取決於那個租戶有幾個 `disable`，每輪只重建那個租戶，比起載入整棵樹，成本可以忽略。整棵樹的重算只在 explain、config-diff 與 CI 中進行：它的每一輪都要重建所有該輪的租戶，輪數又取決於設定內容，放不進寫入檢查的時間上限（[ADR-036](./036-single-parser-effective-config.md) §3）。

不採用「租戶檔寫的同族鍵保留、繼承來的才連帶關掉」：exporter 算閾值時分不出同族鍵來自哪一層，profile 與子目錄 defaults 的值在那之前已併進租戶的設定；要分得出來，得逐鍵記錄來源，那是先前因效能退化否決過的做法。業界的對照是 kube-prometheus-stack 的 `defaultRules.disabled.<告警名>`：以告警名為單位關閉，同名的各嚴重度一起關。

### 9. 讓使用者自己檢查

新增 `explain` 輸出：對指定租戶，逐鍵列出

- 來源層與檔案；
- 哪些讀取端讀它，沒讀的原因；
- 有效值；
- `disable` 的作用：`effective`（有關掉東西）、`noop`（對應 `disable_noop`）。

輸出是一份帶版本號的 JSON（另有給人讀的表格），由現有的 `da-guard effective` 來源資訊、served-values 的原因欄位與反事實結果組成，不另造第三種格式。`--assert <檔案>` 拿輸出與一份預期結果比對，不同就回非 0，可以放進客戶自己的 CI。這和 `amtool config routes test`（給一組 label，回答會送到哪個 receiver）、`promtool test rules` 的用法相同。

文件會說明：工具會盡量把問題報出來，但值是否符合預期，使用者要用 explain 自己檢查與測試。

### 10. 其他一併處理的情況

| 情況 | 處理 | 等級 |
|---|---|---|
| 送出了但沒有任何告警比較它（例如根 defaults 的錯字、規則包沒有 critical 層時的 `<key>_critical`） | 讀取端是出貨的告警規則。以 `metric_observed_map.yaml` 判斷：它記每個閾值鍵被哪條告警比較（含經 recording rule 間接比較；規則包有 critical 層的，`<key>_critical` 各自一筆），並由 CI 守住與規則包一致。validate-config 的 `no_alert` 列報出 exporter 有送、但這份對照查不到的鍵，每個鍵（正規拼法、不含維度）一行，列出寫它的檔與租戶；以 `"95:critical"` 寫成 critical 的值，查的是 `<key>_critical`；對照延後判的鍵（version-aware 的 `container_*`）不報。客戶自己的 Prometheus 規則不在判斷範圍 | 永遠只是建議（WARN，不影響 exit code） |
| 整份沒有讀取端讀的檔（租戶區沒有 `tenants:` 的檔、子目錄沒被選上的 `_defaults` 檔） | 沿用 served-values 現有的 `skipped`／`unread_keys` | notice：新建、還沒寫 `tenants:` 的檔不能凍結寫入 |
| `_metadata` 不認得的子鍵 | 以 Go 的 `TenantMetadata` 為準，判決報 `value_unread`；schema 的 `_metadata` 欄位表以 Go 測試與它雙向釘住 | 租戶檔：error；平台檔與 profile：notice |
| 讀了但值無效（`_silent_mode: garbage`、`_severity_dedup: maybe`、`_profile` 指到不存在的 profile） | 由讀取它的程式判：登錄表 `value.check` 把鍵綁到 exporter 的讀取函式，判決報 `value_unread`。兩個讀取端都讀的 `_severity_dedup`，產生器的接受字由測試釘成與 exporter 一致。只有產生器讀的 `_routing_profile` 由產生器自己擋 | 租戶檔：error；平台檔與 profile：notice |
| 保留鍵寫成排程（`overrides:` 時窗，或 `{default: …, expires: …}` 形式的 `expires:`／`reason:`） | 讀取端只讀 `default`，時窗與到期時間都不生效。判決報 `schedule_unread`，判在寫它的檔與層，規則與 `value_unread` 相同；涵蓋那一層有任何讀取端讀的鍵，不只 exporter 讀的（產生器讀的 `_routing_profile`、annotation 讀的 `_namespaces` 也只讀 `default`）；那一層沒有讀取端讀的鍵（例如 profile 裡的 `_severity_dedup`）只報那個鍵本身沒人讀。寫入閘門以寫下的原樣比對新舊，挪動時窗算新的；tenant-api 不因它另外拒收，沿用決定 12 的寫入檢查 | 租戶檔：error；平台檔與 profile：notice |

值在寫它的那一份檔、那一層判，不在合併後的值上判。合併後的租戶設定已經帶著 profile 與根平台檔 `tenants:` 填進來的值；在那上面判，profile 裡一個錯字會算到每個選用它的租戶頭上，擋下它們與這個值無關的寫入，而它們沒辦法在自己的檔裡修。例如 profile `gold` 寫了 `_silent_mode: purple`，選用它的 `ta`、`tb` 只得到一筆掛在 profile 檔上的 notice，列出這兩個租戶：

```
_defaults.yaml: notice: value_unread `_silent_mode` (unknown silent mode "purple" (valid: warning, critical, all, disable)) — tenant(s) ta, tb
```

對 repo 的範例 conf.d 實測，`no_alert` 報 3 個鍵：`mongodb_connections_current_critical`、`mongodb_replication_lag_critical`、`redis_memory_used_bytes_critical`。mongodb 與 redis 規則包只比較 `severity="warning"` 的門檻，沒有 critical 層，所以這幾個 critical 門檻送出了也沒有告警用到（[#3658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3658)）。它們列在測試裡一份帶 issue 的已知清單，清單只准縮小：範例樹多出一個就紅，修好一個沒從清單拿掉也紅。背景一節的維度鍵如今不再送出（決定 11），由判決報 unread，不在這裡。這項檢查問「送出的值有沒有告警比較」，現有的「告警所需的閾值是否送得到」檢查問「告警要的值有沒有送出」，兩者方向相反、互相補足。

```
[WARN] no_alert
       no_alert `mongodb_connections_current_critical` — served, but no shipped alert compares it — file(s) examples/mongo-prod.yaml — tenant(s) mongo-prod
```

### 11. 推進順序

每一項都可以單獨發布：

1. 登錄表、產生出來的常數與 schema、黑箱探針。行為不變。
2. da-guard 輸出 `read_by` 與原因、反事實 `disable_noop`、explain 與 `--assert`；共用函式接上 validate-config，只報不擋。
3. 產生器 `--validate` 與產生器容器接上共用函式，da-guard 的 exit 4 擴大範圍。需要等 [ADR-036](./036-single-parser-effective-config.md) §7 的步驟 2（da-guard 加上「未判定」）先完成；接上容器前，served-values 先能只算指定的租戶並量過記憶體（決定 5）。
4. 決定 10 的各項，以及 `_state_` 的平台側 notice。

閘門改成「判到就擋」不在本 ADR，依 [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016) 推進。各處手寫的鍵名判斷，與同一入口的閘門改成擋下同一步或之後才刪；判決未完成時寫入不收（決定 5），所以除了決定 2 所列兩處刻意的放寬，任何一個中間版本都不比之前寬鬆。tenant-api 這條入口依序是：產生器容器先改成在同一次判定的期限內判、不讓位、只對被寫的租戶做反事實重算、error 不截斷，這時仍只報不擋；tenant-api 再在同一版改成依判決擋下，並刪掉 Go 的鍵名段與 handler 對 `_` 鍵的預檢。同一版內，其餘各處的鍵名判斷也拿掉：

- 讀取租戶設定（GET）不再回報「這個鍵有沒有人讀」，也不即時判。即時判要佔產生器容器的判定佇列，而讀權限的使用者也能觸發；要看用 `da-tools explain-keys` 或 CI 的 validate-config，寫入時則由判決擋下。這和 Kubernetes 的 warning 只出現在寫入回應裡同型。git 管理的租戶不經 API 寫入，它的這類問題只有 CI 會報。
- exporter 載入時，改記它自己讀不到的鍵（沒有任何一列讀的鍵、對這種鍵寫的 `disable`、登錄表沒有讀取端的 `_` 鍵），不帶嚴重度：這是讀取端在原地的事實，嚴重度由判決決定（決定 4）。沒有跑 CI 的部署仍有這個訊號。exporter 另外兩則它自己讀取時的訊息照舊：profile 裡的路由層鍵不套用，以及 `_critical` 找不到 base 的預設值。
- 產生器自己的鍵名警告與 Go、Python 兩份鍵名判斷，改成只用共用判決後刪除；`version` 標籤與改名提示這類值的檢查由 Go 一處判，經 da-guard served-values 每個租戶的 `key_checks` 交給 validate-config 與產生器 `--validate` 照它的措辭印出。portal 的鍵名判斷同時刪除。原本比對兩份手寫判斷的成員矩陣保留，改成驗證共用判決。
- 維度鍵（`<base>{…}`）只有在它的每個 label 都在根 `_defaults.yaml` 的 `dimensions:` 為該 base 宣告過時，exporter 才送出；不論它寫在租戶檔、profile、子目錄還是根 `defaults:`。比對的是那一列送出的 label 名稱：regex 選擇器送出 `<label>_re`（`version=~"v3.*"` 送出 `version_re`），要宣告的是 `version_re`，宣告 `version` 不涵蓋它。base 不能是 `<key>_critical`（新舊拼法皆然）：`<key>_critical` 是 `<key>` 的 critical 層的名字，不是 base，所以這種宣告不宣告任何東西（根 `defaults:` 只給 `<key>_critical`、沒給 `<key>` 時它會被當成自己的 base 送出，那本身就是 da-guard 報 `root_defaults_critical_key` 的設定錯誤；它的維度版同樣不送，刻意比那個 warning 嚴），`<key>_critical{…}` 沒有列，da-guard 對根檔報 `root_dimensions_critical_base`（error）；每個維度的 critical 門檻寫成 `<key>{label="v"}: "N:critical"`。其他維度鍵沒有列，判決讀成 `unread`（`no_row`）；兩個例外同樣沒有列：值寫 `disable` 的依決定 8 報 `disable_noop`，Policy-as-Code 規則指名的讀成 `read`（`policy_engine` 讀它）。理由是出貨的規則包以 `max by(…)` 合成門檻，只保留它分組的 label（多數只有 `tenant`）；不在分組裡的 label 無法成為獨立維度，值會併進同一組的門檻，寫了也不會依維度生效。出貨的 `dimensions:` 來自門檻登錄表（`rule-packs/threshold-registry.yaml`）每個 key 的 `dimensions`：`--regen` 由它重生出貨的宣告與 `scaffold_tenant.RULE_PACKS`，`da-tools init` 與 `scaffold_tenant` 讀的是後者；測試兩個方向對照登錄表與規則包的分組，目前是 `container_cpu`、`container_memory` 的 `version`。自訂規則若依某個 label 讀，平台在 `dimensions:` 加上該 base 與 label。例：

  ```yaml
  # 根 _defaults.yaml
  dimensions:
    container_cpu: [version]
  ```

  ```yaml
  # 租戶檔
  tenants:
    t-a:
      container_cpu{version="v2"}: "60"     # 送出
      container_cpu{pod="a"}: "70"          # 不送出：pod 沒宣告，判決 unread
      container_cpu{version=~"v3.*"}: "95"  # 不送出：送出的是 version_re，沒宣告
      container_cpu{version="v4"}: "90:critical"     # 送出，severity=critical
      container_cpu_critical{version="v3"}: "90"     # 不送出：_critical 層不帶維度，判決 unread
  ```
- 平台檔與 profile 層的 notice 不再含「沒有人讀」的鍵，由判決報（決定 4）。

### 12. 磁碟上既有的錯值

寫入關卡加上一條值的檢查時，repo 裡可能已經有不符的值。若照樣擋，租戶改任何其他鍵都會被這個沒人動過的舊值擋下。做法借用 Kubernetes 的 CRD validation ratcheting（KEP-4008：更新物件時，新加的驗證規則只套在這次改到的欄位，沒改的舊值照樣放行）：

- **以讀取端解析的結果判「沒變」**，不比原始文字。加不加引號、對映的鍵序、註解不同，或讀取端本來就不分大小寫的大小寫差異，只要 exporter 解出的結果相同就算沒變。例如 `Purple` 改成 `purple`，exporter 讀起來一樣，算沒變；`purple` 改成 `orange` 算改了。
- **沒變的錯值降為 notice，寫入照收**；新寫的、或改過的錯值照擋。
- **讀不到寫入前的檔案就嚴格判**：新租戶、檔案不存在或無法解析時沒有比較的基準，一律照擋。
- **只適用新加的值檢查**。既有的檢查（例如 `_profile` 指向不存在的 profile、排程值的 `expires:` 不是 RFC3339 時間）不因此放寬。
- **作用在檔案裡的每一個租戶**：一個檔可以有多個租戶，寫其中一個時，其他租戶沒動過的錯值同樣只是 notice。
- 讀取租戶設定（GET）時，以磁碟上的內容自己當基準，既有的錯值以 notice 回報。
- 共用判決的 `value_unread` 在 tenant-api 不另外拒收：它只看寫入後的樹，分不出值有沒有被這次寫入改過；租戶層的值由寫入檢查照上面的規則擋，判決的那一筆只當成提示回報。validate-config 照判決擋整棵樹，`da-guard write` 只擋新出現的。

這和產生器關卡對寫入前就有的問題的處理不同。產生器關卡把它標成 `pre_existing`（寫入前就有），但照樣擋。差別在失效方向與影響範圍：`_silent_mode` 的錯值讓 exporter 什麼都不靜音，告警照常送出，失效在安全的那一側，影響也只有這一個鍵；照擋卻會讓租戶在修好它之前什麼都不能改。產生器擋的問題有些會讓整棵 conf.d 拒絕產出（例如 `_severity_dedup` 寫了產生器不認得的值），放行的代價高得多。

## 範例

上面背景一節的例子，判決輸出（explain 的人讀版，節錄；等級是各閘門改成「判到就擋」之後的等級）：

```
db-a
  key                     source        read_by     verdict  disable
  mysql_conections        db-a.yaml     —           unread   —        ← error：沒有讀取端讀
  mysql_slave_lagg        db-a.yaml     —           unread   noop     ← warning：沒關到任何東西，mysql_slave_lag 仍送出 30
  _routng                 db-a.yaml     —           unread   —        ← error
  redis_memory            db-a.yaml     exporter    read     effective
  redis_memory_critical   db-a.yaml     exporter    read     —        ← warning：跟著 redis_memory 關掉（disable_cascade），不送
  _namespaces             db-a.yaml     annotation  read     —
```

輸入與今天的實測輸出見「背景」；這張表是本 ADR 規定的判決，各欄位依決定 1、4、8、9 產生。

值的檢查與決定 12：exporter 不認得 `_silent_mode: purple`（它只記一行 `WARN: unknown silent mode "purple" for tenant=…`（節錄）、什麼都不靜音）。寫入關卡接上值的檢查之後，本 ADR 規定 tenant-api 對它的結果如下：

| 寫入 | 磁碟上原本 | 結果 |
|---|---|---|
| PUT 租戶設定，含 `_silent_mode: purple` | 沒有這個鍵，或是 `warning` | 拒絕（400） |
| PUT 租戶設定，只改了別的鍵 | `_silent_mode: purple` | 寫入，回應附 notice |
| PUT 租戶設定，`_silent_mode` 改成 `orange` | `_silent_mode: purple` | 拒絕（400） |
| 批次 patch `_silent_mode: purple` | 沒有這個鍵 | 該筆操作失敗，不寫入 |
| 批次 patch 別的鍵 | `_silent_mode: purple` | 寫入，該筆結果附 notice |
| GET 租戶設定 | `_silent_mode: purple` | 回應附 notice |

## 後果與已知限制

- **保留鍵清單只剩一個來源**：新增保留鍵只改登錄表，再重新產生；各讀取端各自的鍵名判斷改由共用判決取代。
- **登錄表沒有的 `_` 鍵不再產生閾值列**：exporter 與 JSON schema 對這種鍵的結論一致。靠根 defaults 宣告這種鍵送出閾值的部署，升級後少掉那些列（遷移方式見決定 2）。根 defaults 宣告了這種鍵時，共用判決把租戶寫的同名鍵判為 `unread`（原因 `unregistered`），validate-config 與產生器 `--validate` 都報出。
- **鍵名判決只需要 da-guard 與登錄表**：產生器那半取登錄表的宣告，不跑產生器。值在產生器語意下是否有效（例如 `_routing_profile` 指向的 profile）仍只有產生器能判；只有 da-guard 的環境，這部分回 exit 4（未判定）。
- **寫入的判定變慢，單一產生器容器支援的租戶數變少**：判決併進產生器容器的一次判定，約佔三成時間；以 1 CPU 為例，約 2500 個租戶時一次判定就接近期限的一半（示意值，依主機而異）。樹再大，或有寫入在排隊時，寫入可能因判決判不完而被拒：時間不夠的可重試，其他原因不可重試（決定 5）。界線看決定 5 的 metric；拿回容量的方向是讓 served-values 與 effective 在同一次 da-guard 裡算完、讓判決與產生器並行，以及加速產生器本身。
- **判決壞了，寫入就不收**：容器裡找不到或無法執行 da-guard 時，與找不到 da-read 一樣是容器不可用，寫入回 503；da-guard 在個別寫入上判不完時，拒收那一筆（決定 5）。
- **升級時平台側的這類鍵一次轉紅**：各閘門改成判到就擋的那一版，既有樹裡 profile、子目錄 `_defaults.yaml`、根平台檔 `tenants:` 上沒人讀的鍵，以及登錄表沒有或已退役的 `_` 鍵，從 notice 變成 error，整棵樹的 CI 一次轉紅。今天 validate-config 的 `unread_keys` 以 notice 列出的平台側鍵，就是那時會轉紅的大部分；沒有租戶綁定的 profile 上的閾值鍵今天不列，要到逐 profile 判接上之後才報出。
- **平台刪掉根層宣告，錯掛在讀不到的那一層**：根 `_defaults.yaml` 拿掉某個閾值的宣告後，profile 與子目錄 `_defaults.yaml` 裡同名的值變成沒人讀，error 掛在那些 profile／子目錄檔，而不是被改的根檔；改根檔的寫入閘門不因此擋下，由整棵樹的 CI 報出。
- **退役的 `_` 鍵沒有緩衝期**：寫在平台側的已退役鍵直接從 notice 變成 error，中間沒有一段 warning 的過渡；遷移靠登錄表為它寫的提示文字，所以判決的 `retired` finding 要帶上這段提示。
- **寫入時只因 `disable_noop` 而未讀的鍵照收**：決定 4 讓它只報 warning，所以拼錯的鍵寫成 `disable` 時寫入照收、只附 warning，而今天 Go 會擋。
- **direct 模式沒有整棵樹的判決；沒經過 CI 就進了線上的樹，要另外開啟 CronJob 才判**：寫入時只判被寫的租戶。平台檔改動讓既有租戶的鍵變成沒人讀這類不屬於單筆寫入的情況，PR 模式由 CI 判；direct 模式（寫入只留在 pod 裡、沒有 PR）目前沒有對應的整棵樹判決，這些寫入也不會進到 exporter 讀的 ConfigMap。在 tenant-api 之外改動、沒經過 CI 就進了那份 ConfigMap 的樹（直接 commit 到 git、ConfigMap 同步），由 threshold-exporter chart 的 `configValidation` 判：開啟後，一個 CronJob 定期在那份 ConfigMap 上跑 CI 用的同一支 `validate-config`，有 FAIL 時 Job 失敗、由平台規則 `CronJobLastRunFailed` 告警，細節在 Job 的 log（[#3353](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3353)）。它預設關閉；判到的範圍是映像裡那一版 `validate-config` 的範圍；告警只看 `monitoring` namespace；一次改動最多要等一個排程週期才被判到。
- **沒有租戶綁的 profile 上的錯鍵進得了線上**：這種鍵不判，只報 notice（決定 4）；有租戶綁定之後，只有整棵樹的 CI 擋得到，direct 模式下綁定那筆寫入只給 warning。
- **被租戶檔蓋掉的平台鍵不另報**：根平台檔 `tenants:` 下的鍵被租戶檔同名鍵蓋掉時，判決只判租戶那份（鍵名相同，拼錯就報在租戶檔）；平台那份的值不生效。例外：租戶寫 `typo: disable` 蓋掉平台的 `typo: 70` 時報的是 `disable_noop` warning，不是 error。
- **錯字與有意省略不區分**：平台沒有宣告的閾值鍵，判決不知道是租戶拼錯還是平台漏宣告，兩者報同一個原因。
- **規則讀取以 repo 內的規則包為準**：客戶自己的規則讀了某個閾值，除非宣告為外部讀取端，否則會被報成「沒有規則讀」。這一項因此永遠只是建議。
- **schema 比讀取端窄**：JSON schema 只是給編輯器的提示，判值的是讀取端。exporter 讀 `_silent_mode` 時不分大小寫、會去掉前後空白，也認得 `disabled`、`off`、`false` 等停用詞；schema 的列舉沒有這些寫法。YAML 未加引號的 `false`、`off` 在部分解析器裡是布林值，字串列舉表達不了。所以 schema 標錯的值不一定是錯的，以讀取端為準。
- **沒變的錯值會一直留著**：決定 12 讓沒改過的錯值只報 notice，它會留在檔裡直到有人改這個鍵；期間它照樣什麼都不靜音。
- **探針不涵蓋兩類**：標註類的鍵（`_namespaces`），以及讀了但輸出不變的鍵，靠登錄表的明確標註。
- **反事實重算的成本**：整棵樹時，重算的輪數等於單一租戶最多有幾個 `disable`，每輪是一次 served-values，所以不進入寫入檢查；寫入檢查只對被寫的租戶重算（決定 8）。
- **`disable` 連帶關閉少了一種寫法**：「base 關掉、只對某個維度告警」（`X: disable` 加 `X{db="a"}: 5`）沒有替代寫法；要這麼做，得把 base 的門檻設成不會觸發的值。從舊語意升級時，`X: disable` 加 `X_critical: 90` 不再送出 critical；`X_critical` 寫在租戶自己檔裡時報 `disable_cascade` warning，寫在 profile 或子目錄時不報（決定 8 的遷移寫法）。根目錄 `defaults:` 自己給了 `X_critical` 的值時，它是一個獨立的閾值鍵（exporter 以 `metric="…_critical"` 的 warning 列送出），不屬於 `X` 這一族，`X: disable` 不連帶關掉它；這種寫法另有 `root_defaults_critical_key` 警告。`disable_cascade` 有兩種情況報不出來：拿掉 base 的 `disable` 之後那個時段整份 `/metrics` 無法產生（例如兩個維度鍵送出同一條 series），該時段沒有證據；同一個 base 的兩種 #1231 拼法都寫 `disable` 時，拿掉其中一個，另一個仍關著整族。
- **實測中發現、另案處理的問題**：
  - 產生器讀了 `_metadata` 卻沒有使用（[#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043)）。
  - `_severity_dedup` 只讀租戶檔，其他保留鍵會繼承 profile；需要確認是否刻意（[#3044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3044)）。

## 考慮過但否決

- **在判定工具裡放一份「產生器讀哪些鍵」的表，再用測試追產生器的實作**：表的擁有者（Go 的判定工具）和被描述的程式（Python 產生器）不同，而測試只從表出發、檢查表上寫的鍵，程式多讀了一個表上沒有的鍵時沒有人發現，這份表就是漂移點。業界的對照是 Spring Boot 手寫的 `additional-spring-configuration-metadata.json`，同樣靠人記得同步。採用的登錄表不同在兩點：它是所有讀取端共用的一份宣告，不屬於任何一個判定工具；探針對登錄表上每個鍵、每一層兩個方向都比對（登錄表說讀的必須改變輸出，沒說讀的不能改變輸出，決定 3），程式在某一層多讀或少讀一個登錄表上的鍵都會讓 CI 紅；登錄表沒有的 `_` 鍵依決定 2 本來就不得有讀取端讀。
- **各讀取端在執行時回報自己讀了哪些鍵，再取聯集**：業界找不到「任一讀取端讀了就合法」的聯集判決先例，主流是按前綴或子樹劃分歸屬（OpenTelemetry Collector 各元件驗自己的子樹、Helm subchart 與 `global`）。實作上，Python 端資料一經複製（`copy.copy`、`json.dumps`）就失去追蹤，平台檔合併後也丟了來源；Go 的 map 讀取無法攔截，要在每個解析階段逐一埋點。實測產生器在租戶層逐鍵讀的只有 `_routing`、`_routing_profile`、`_severity_dedup`、`_metadata` 四個，不隨設定而變，執行時追蹤換不到額外資訊。這個想法改用在決定 3 的黑箱探針。
- **只在一個點記錄「下層有值」來判 `disable` 有沒有作用**：exporter 在閾值相關的地方讀 `disable` 有五處（base、宣告型閾值、`_critical`、維度鍵、state filter），而且 base 階段是逐一走過 defaults，走得到的 `disable` 本來就有下層值，沒作用的那些反而走不到。所以改用反事實重算。
- **「conf.d 裡有根 `_defaults.yaml`」就算可以判**：實測根 `_defaults.yaml` 存在但沒有 `defaults:`、只有 `_routing_defaults`、或 `defaults: {}` 時，都沒有任何閾值送出；而只有 `optional_overrides` 的樹沒有 defaults 也送得出值。所以改看「有沒有讀到根平台檔」，再交給現有的逐鍵原因。
- **平台側沒人讀的鍵維持 notice**：同一類缺陷（平台寫了值，綁定的租戶永遠拿不到）會因層與檢查不同而等級不同，例如子目錄的值在判決是 notice、在 da-guard 是 error；寫錯的 profile 也永遠不會讓任何閘門變紅。不擋無關租戶寫入這件事，已由決定 6 的檔案範圍保證，不需要靠低等級。業界對照：`kubectl --validate=strict` 遇到未知欄位直接失敗；Terraform 對 tfvars 裡未宣告的變數只警告，前提是共用的 tfvars 可能被別的設定使用，而本案的 profile 只在這棵樹裡被讀，沒有這個前提。
- **只把閾值鍵升成 error，`_` 鍵在平台側維持 notice**：多出一條「鍵的種類 × 層」的例外規則，讀者要記、各實作要各自對齊，容易漂移。這個方案省下的是退役 `_` 鍵的緩衝期，但登錄表已有改名提示可以讓判決照印（見後果與已知限制），breaking 可以接受。
- **讓 schema 本身當登錄表**（在 JSON Schema 加自訂欄位標讀取端）：兩個 Go module 都沒有 JSON Schema 函式庫，Go 端要用它就得新增依賴或手抄一份；而且 schema 今天是被程式碼「釘住」的一方，改由它驅動程式會把現有方向倒過來。

## 實作位置

- 登錄表：`components/threshold-exporter/app/pkg/config/reserved_keys.json`；`scripts/tools/dx/sync_schema.py --update` 產生、`--check` 比對（CI 由 `tests/dx/test_sync_schema.py` 執行）。
- 值的形狀、判斷式綁定、批次可移除的鍵與決定租戶範圍的鍵的查詢：`components/threshold-exporter/app/pkg/config/reserved_values.go`。
- 探針：`tests/shared/test_reserved_key_probes.py`。
- 判決：`components/threshold-exporter/app/pkg/keyverdict`；`da-guard key-verdict`（`cmd/da-guard/key_verdict.go`）讀樹並輸出。Python 工具經 `scripts/tools/_lib_key_verdict.py` 讀它的輸出。
- exporter 實測：`da-guard served-values`（`components/threshold-exporter/app/cmd/da-guard/served_values.go`）。
- 規則讀取索引：`scripts/tools/ops/_threshold_alerts.py`、`rule-packs/threshold-registry.yaml`。

## 相關

- [ADR-036](./036-single-parser-effective-config.md)：YAML 只由 Go 解析，路由語意只由產生器判；本 ADR 修訂其 §9 的 exit 4 範圍，並在 §3 容器不可用的情況加上「找不到或無法執行 da-guard」與「容器沒有回判決」。
- [ADR-037](./037-config-diff-compares-rendered-output.md)：config-diff 比較渲染結果；`disable` 新引入的問題出現在它的 PR 留言。
- [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)：拼錯的鍵改由讀取端實報判定，以及之後改成擋下的步驟。
