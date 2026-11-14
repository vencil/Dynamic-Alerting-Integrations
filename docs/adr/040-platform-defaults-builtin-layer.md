---
title: "ADR-040: 平台預設值內建在讀取端，客戶的 _defaults.yaml 只放覆寫與退出"
tags: [adr, conf.d, defaults, threshold-registry, upgrade, v3.0.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-040
tracking_kind: adr
status: proposed
domain: exporter
created_at: 2026-11-14
updated_at: 2026-11-14
---

# ADR-040: 平台預設值內建在讀取端，客戶的 _defaults.yaml 只放覆寫與退出

> **Language / 語言：** **中文 (Current)** | [English](./040-platform-defaults-builtin-layer.en.md)

**決策摘要**：平台出貨即啟用的閾值預設值，改由讀 conf.d 的程式自己帶著（下稱「內建層」），排在根目錄 `_defaults.yaml` 之下、當作合併鏈的最底層。客戶的樹不再需要、`da-tools init` 也不再寫入這些值的複製品：沒寫的鍵拿平台值，寫了數字就是覆寫，列進 `optional_overrides:` 就是退出。這是破壞性變更，隨 v3.0 出貨：省略一個鍵的意思，從「不宣告」變成「用平台值」。

## 狀態

🟡 **Proposed**（2026-11-14）。尚未實作；本文描述「現況」的句子都在 main 上量過，描述「決定」的句子是實作後要成立的行為。owner 核可後昇格 Accepted，其中兩項需要 owner 明示選擇，見「待 owner 決定」。

## 名詞

- **閾值登錄表**：`rule-packs/threshold-registry.yaml`，平台所有閾值鍵的唯一來源。每個鍵記它屬於哪個規則包、平台的值，以及 `arming`：`default` 表示平台出貨即啟用，`operator` 表示平台不主張值、要部署方自己校準。
- **預設啟用鍵**：登錄表裡 `arming: default` 的鍵。目前 7 個：`container_cpu`、`container_cpu_throttle`、`container_memory`、`pg_connections`、`pg_replication_lag`、`mysql_connection_usage_percent`、`mysql_replication_lag`。
- **讀取端**：讀 conf.d 並算出每個租戶設定的程式：threshold-exporter、da-guard（conf.d 檢查工具）、tenant-api。三者編譯進同一套 Go 程式庫，`da-tools validate-config` 經 da-guard 取得答案。
- **根層**：conf.d 根目錄的 `_defaults.yaml`。它的 `defaults:` 給數字，`optional_overrides:` 是一份鍵名清單，宣告「平台認得這個鍵、但不主張它的值」，租戶自己寫了才送出。
- **列**：exporter 在 `/metrics` 為某個租戶的某個閾值送出的一條 `user_threshold` series。`da-guard served-values` 印出同一份答案。

## 背景

`da-tools init` 建立新專案時，把登錄表的值連數字寫進客戶的 `conf.d/_defaults.yaml`。這份檔案之後歸客戶所有，平台再也改不到它，於是：

- **新的預設啟用鍵進不來。** 平台把連線數閾值從絕對值 `mysql_connections` 換成百分比 `mysql_connection_usage_percent` 之後，既有客戶的樹裡沒有新鍵，對應的告警不會響，也沒有任何錯誤訊息。
- **平台重新校準的值進不來。** 客戶檔裡的數字是 init 當天的值。
- **兩條安裝路徑今天就不一致。** Helm chart 的 `values.yaml` 隨 chart 升級，客戶只寫差異；init 在 Helm 這一側也刻意不寫預設值，因為一份複製品會蓋掉上游之後的校準。conf.d 這一側沒有跟進。

量到的現況：

- 沒有任何讀取端拿登錄表的值當底層。根層沒寫 `pg_connections`、租戶寫了值，da-guard 的鍵判決是 `unread`／`error`／`no_row`，`/metrics` 不送這一列。
- init 選全部規則包時，根層 `defaults:` 寫入 39 個鍵，其中只有 7 個是預設啟用鍵，另外 32 個是登錄表標為 `operator` 的鍵。只選 `mariadb,kubernetes` 時寫入 5 個。
- init 產生的租戶檔也帶複製品：第一個規則包的基底值被寫成租戶覆寫（例如 `mysql_connection_usage_percent: "80"`，與平台值相同）。
- `da-tools validate-config` 有一列 `unsupplied_defaults`，點名「平台預設啟用、樹裡卻沒供給」的鍵，建議把鍵加進 `defaults:`。它只在有人執行這支工具時看得到，而且它的建議是再做一份複製品。

業界的做法一致：Prometheus 的 monitoring mixins 讓使用者用 `_config+::` 只覆寫要改的欄位，Helm 讓使用者只寫與 chart `values.yaml` 的差異，kube-prometheus-stack 的預設規則隨 chart 升級、使用者以 `defaultRules.disabled` 逐條關閉。共同點是使用者只放覆寫與退出，預設值跟著出貨物走。

## 決策

### 1. 內建層：讀取端自己帶著預設啟用鍵的值

讀取端內建一張表：每個預設啟用鍵與它的平台值。這張表由登錄表生成、編譯進程式，不在執行期讀檔。

一條規則決定它的所有行為：

> **內建層給的值，效果等同把同一個數字寫在根層 `defaults:`，差別只有來源標示。**

所以凡是「根層 `defaults:` 有值」的既有規則，內建層給值的鍵一律適用，不另立規則：

- 子目錄 `_defaults.yaml` 與租戶檔可以覆寫它、可以寫 `"disable"` 關掉它（[ADR-017](017-defaults-yaml-inheritance-dual-hash.md) 決策 2、4）。
- 租戶寫的 `<鍵>_critical` 會送出 critical 列，因為基底有值（ADR-017 決策 3）。
- 租戶寫這個鍵不再被判為沒有讀取端讀（[ADR-038](038-unread-keys-judged-by-readers.md) 決定 4）。
- `"disable"` 關掉一整族、過期後失效的規則照舊（ADR-038 決定 8）。

內建層只收預設啟用鍵。`arming: operator` 的鍵與所有 `_critical` 鍵平台不主張值，不進內建層。

內建層對每個租戶都生效，不看這個部署有沒有裝對應的規則包。多一列不等於多一條告警：告警要響，還需要規則包已部署、指標存在。Helm 路徑今天就是這樣：chart 把 7 個鍵送給每個租戶。

### 2. 合併順序與來源標示

套用順序變成：**內建層** → 根層 → 往下每一層子目錄 → profile 與根平台檔的 `tenants:` → 租戶檔。後套用的覆蓋先套用的。

- 最終設定（`da-guard effective`、tenant-api 的 `/effective`）會顯示內建層給的鍵，`key_sources` 標 `layer: builtin`，沒有 `file`。
- `merged_hash`（最終設定的雜湊，ADR-017 決策 7）涵蓋內建層的值。平台改了某個內建值，沒覆寫它的租戶的雜湊跟著變，這正是它要回答的問題。
- 仍然帶著複製品、值與平台相同的樹，升級前後每個租戶的最終設定與 `merged_hash` 都不變。這是驗收項。

### 3. 三種寫法：沿用、覆寫、退出

| 想要的結果 | 寫法 | 寫在哪 |
|:--|:--|:--|
| 用平台值，並跟著平台之後的校準 | 不寫這個鍵 | — |
| 整個平台用自己的數字（也等於釘住，不跟平台變動） | `defaults:` 寫數字 | 根層 |
| 整個平台不要這個預設值 | 鍵名列進 `optional_overrides:` | 根層 |
| 某一層子目錄或某個租戶不要 | `<鍵>: "disable"` | 子目錄 `_defaults.yaml` 或租戶檔 |
| 某個租戶用自己的數字 | `<鍵>: "70"` | 租戶檔 |

退出沿用 `optional_overrides:`，不新增語法。這個欄位原本的意思就是「平台認得這個鍵、但不主張它的值」，對預設啟用鍵寫下它，意思不變：內建層不給值，租戶自己寫了才送出。

根層 `defaults:` 仍然只收數字。在根層寫 `"disable"` 照舊是讀不了的值，da-guard 以 `root_default_refused` 報錯、結束碼 1；這時該鍵落到內建層的值，不會因為一個被拒收的寫法而少掉告警。錯誤訊息改為指向 `optional_overrides:`。

### 4. 根層寫 `null` 的效果改變

ADR-017 決策 4 的原則不變：閾值寫 `null` 等於這一層沒寫。改變的是「沒寫」之後落到哪裡：

| 根層 `defaults:` 寫 `鍵: null` | 現況 | 本決策之後 |
|:--|:--|:--|
| 預設啟用鍵 | 根層沒宣告：不送這一列，租戶給的值由 `root_default_null_undeclared` 指名 | 落到內建層：送出平台值 |
| 其他鍵 | 同上 | 不變 |

不讓 `null` 代表退出的理由與 ADR-017 相同：`鍵:` 後面留白解析出來就是 `null`，打到一半忘了填值不該靜靜關掉一條告警。

Helm 使用者要特別注意：Helm 把設成 `null` 的值從合併結果刪掉，所以 `--set thresholdConfig.defaults.pg_connections=null` 今天的效果是「ConfigMap 裡沒有這個鍵、不送這一列」，本決策之後是「落到內建層、送出平台值」。chart 看不到被刪掉的鍵，無法代為轉換；替代寫法見決策 6。

### 5. 內建表怎麼產生、版本怎麼對齊

- 內建表由 `check_threshold_registry.py --regen` 從登錄表生成為 Go 原始碼，與現有的退役鍵表同一套機制；登錄表改了而沒有重新生成時，`--ci` 檢查失敗。
- exporter、da-guard、tenant-api 編譯進同一份表，同一版的三者答案相同。
- **threshold-exporter 與規則包以同一個平台版本出貨、一起升級。** 表在 exporter 裡，讀這些值的告警規則在規則包裡；兩者版本不同時：
  - 規則包較新：新的預設啟用鍵沒有內建值，對應的告警不響。這正是本決策要消除的失效方式，只剩版本落差這一個成因，由決策 8 的執行期訊號偵測。
  - exporter 較新：多出沒有規則讀的列，無害。
- da-tools 映像（CI 用的 da-guard）與部署中的 exporter 版本不同時，兩邊對「內建層給什麼」的答案可能不同。這類落差 [ADR-036](036-single-parser-effective-config.md) §6 已經承認；本決策讓它多影響一件事，所以 da-guard 的輸出與 exporter 的指標都帶內建表的版本摘要，供人對照。
- **改動內建值、新增或移除預設啟用鍵，一律是 BREAKING 的 changelog 條目。** 它會在客戶的 repo 沒有任何 diff 的情況下改變告警行為。依據與 Kubernetes API 的慣例相同：相容的變更不得改變預設值的語意。

### 6. Helm chart

- `thresholdConfig.defaults` 預設改為空，只放使用者的覆寫；`values.yaml` 以生成的註解列出內建值，供查閱。使用者原本覆寫的值照樣勝出。
- chart 新增一個值 `thresholdConfig.defaultsOptOut`（鍵名清單），渲染時併進 `optional_overrides:`。Helm 對清單是整個取代、不合併，讓使用者直接覆寫 `thresholdConfig.optional_overrides` 等於要他複製 chart 生成的整份清單；另給一個只放退出項的清單可以避免這件事。conf.d 的語法仍然只有 `optional_overrides:` 一種。
- 設了 `thresholdConfig.existingConfigMap` 的部署不經 chart 的值，直接適用決策 1 到 4。

### 7. `da-tools init` 與既有的樹

**新專案**：

- 根層 `defaults:` 不寫預設啟用鍵，改以註解列出內建值與決策 3 的三種寫法。
- 租戶檔不寫與上層相同的值。
- `operator` 鍵的處置見「待 owner 決定」。

**既有的樹**：升級本身不改任何檔案。帶著複製品的樹繼續照自己寫的數字運作，那些數字從此算是覆寫。

- da-guard 對根層每個「值與內建值相同」的預設啟用鍵報一則提示（不影響結束碼），說明刪掉它就會跟著平台之後的校準。
- 另提供一支遷移指令，預設只預覽（dry-run），列出三件事：它會刪掉哪些鍵、升級後哪些租戶會多出哪些列、根層哪些 `null` 要改寫成 `optional_overrides:`。寫入經 `da-guard write`。
  - 只刪「值與內建值相同的預設啟用鍵」。值不同的保留（那是客戶的覆寫）；`operator` 鍵即使值與登錄表相同也保留（它沒有內建值，刪掉就是關告警）。
  - init 產生的檔案在 `.da-init.yaml` 留有 sha256。檔案與紀錄相符（沒被手改過）時可以直接刪；不相符時要使用者明示同意，因為分不出一個與平台相同的數字是複製品還是刻意釘住的值。
  - 租戶檔裡與上層相同的值同樣處理。

不在升級時自動刪除：與平台值相同的數字可能是刻意釘住的，刪掉之後它會跟著平台變動，這個選擇應該由客戶做。

### 8. 升級訊號：讓客戶看得到哪些告警被打開

三個訊號，答案都來自讀取端自己，不由工具另外讀登錄表推算：

- **`da-tools validate-config`**：現有的 `unsupplied_defaults` 列由 `platform_defaults` 列取代。它逐一列出預設啟用鍵的狀態：`builtin`（樹裡沒寫、由內建層供給，附平台值與受影響的租戶數）、`overridden`（根層給了值；與平台值相同時註明）、`opted_out`（列在 `optional_overrides:`），並印出內建表的版本摘要。這一列是說明，不是警告：樹裡沒寫某個預設啟用鍵是正常狀態。原本「把鍵加進 `defaults:`」的建議移除。在內建層上線之前，現有的列與建議對現行行為是正確的，兩者在同一個版本一起替換。
- **exporter 的執行期指標**：
  - 每個預設啟用鍵一條狀態指標（`builtin`／`overridden`／`opted_out`），不帶租戶標籤。
  - 一條帶內建表版本摘要的資訊指標。規則包帶同一個摘要；兩者不一致持續一段時間時，平台規則包的一條 meta 告警觸發。原先構想的「未供給的預設鍵」指標在本決策之後只剩版本落差一個成因，所以改以版本比對表達。
  - 退役鍵的指標不受本決策影響。
- **升級前的比對**：沿用 ADR-036 §8 的做法。以新版 da-guard 驗自己的樹，再以新舊兩版各跑一次 `da-guard served-values`，比對多出的列；作法寫進發布說明與漸進遷移 playbook。

### 9. 升級順序

1. 以新版 da-guard 驗 conf.d，並比對新舊兩版的 `served-values`，確認多出的列。
2. 不想要的預設啟用鍵：整個平台不要就列進 `optional_overrides:`（Helm：`thresholdConfig.defaultsOptOut`），個別租戶不要就寫 `"disable"`。根層與 Helm 的 `null` 一併改寫。
3. 升級讀取端與規則包到同一個平台版本：threshold-exporter、規則包、tenant-api、da-tools。
4. **全部升級完之後**才執行遷移指令、刪掉複製品。

第 4 步不能提前：舊版讀取端沒有內建層，讀到一棵已經刪掉複製品的樹，那些鍵就沒有值，告警不響也沒有錯誤。同理，要回退到舊版，先把複製品放回去。同一份 conf.d 供給多個叢集時，等所有叢集都升級完再刪。

### 範例

`conf.d/_defaults.yaml`：

```yaml
defaults:
  redis_connected_clients: 1000
optional_overrides:
  - "container_memory"
```

`conf.d/t1.yaml`：

```yaml
tenants:
  t1:
    pg_connections: "70"
    pg_replication_lag_critical: "60"
    container_cpu: "disable"
```

**現況**（`da-guard served-values --config-dir conf.d`）：`t1` 只有一列。

```
redis_connected_clients      1000  warning
```

租戶寫的三個鍵都不送：`pg_connections` 與 `pg_replication_lag_critical` 的原因是 `no_row`（根層沒有宣告基底），`container_cpu` 是 `disabled`。

**本決策之後**（預期輸出，尚未實作；數字取自把內建值寫進根層 `defaults:` 的同一棵樹，依決策 1 的等價規則兩者相同）：

```
container_cpu_throttle           25  warning    ← 內建層
mysql_connection_usage_percent   80  warning    ← 內建層
mysql_replication_lag            30  warning    ← 內建層
pg_connections                   70  warning    ← 租戶覆寫
pg_replication_lag               30  warning    ← 內建層
pg_replication_lag_critical      60  critical   ← 基底有內建值，租戶的 critical 送出
redis_connected_clients        1000  warning    ← 根層
```

`container_cpu` 被租戶的 `"disable"` 關掉；`container_memory` 列在 `optional_overrides:`，內建層不給值。

## 後果與已知限制

- **省略一個鍵的意思變了。** 既有的樹升級後，每個租戶最多多出 7 條基底列，另加「租戶寫了 `_critical`、根層原本沒有基底」的 critical 列；沒有列會消失。在本 repo 的樹上量到的幅度：

  | 樹 | 租戶數 | 列數 前 → 後 |
  |:--|:--|:--|
  | repo 的開發範例樹（`components/threshold-exporter/config/conf.d`） | 5 | 95 → 95 |
  | `try-local/seed/conf.d` | 2 | 9 → 19 |
  | init，選 `mariadb,kubernetes` | 1 | 10 → 12 |
  | init，選全部規則包 | 2 | 98 → 98 |
  | init，只選 `redis` | 2 | 8 → 22 |
  | 上方範例 | 1 | 1 → 7 |

- **平台改值會在客戶沒有 diff 的情況下改變告警。** 這是本決策的目的，也是它的代價。護欄是決策 5 的 BREAKING 條目與決策 8 的升級前比對；不接受的客戶把數字寫進根層，就釘住了。
- **exporter 與規則包的版本要一起動。** 版本落差由 meta 告警偵測，不是由設計消除。
- **回退與混合版本有靜默失效的窗口**（決策 9 第 4 步）。舊版讀取端無從得知樹已經改成只放覆寫，這一段只能靠升級順序避免。
- **每個租戶的列數上限。** 每個租戶多出的列計入 `max_metrics_per_tenant`（預設 500）。接近上限的租戶在升級前比對時由 da-guard 既有的列數檢查指出。
- **不涵蓋的複製品。** 根層的 `optional_overrides:` 與 `dimensions:` 兩份清單、init 寫進租戶檔的 `_critical` 建議值，仍然是 init 當天的複製品。前兩者過期時的失效方式是 da-guard 報錯，不是靜默，所以不在本決策處理；再議的條件是實際升級中出現「新宣告的鍵在客戶的樹上被拒收」的回報。
- **沒有量到的事**：多出的列實際會變成幾條告警（取決於客戶的指標與已部署的規則包）、多少客戶用了 Helm 的 `null` 或手改過預設值、大量租戶下多出的列對 exporter 的成本。

## 考慮過的替代方案

- **維持複製，只靠檢查點名**：訊號只給有跑檢查的部署，而且每次平台新增預設啟用鍵，每個客戶都要手動補一次。檢查留下來當升級訊號，不當解法。
- **讓 init 重跑來更新**：`init --force` 會重寫兩個檔、丟掉所有手改。
- **登錄表隨規則包出貨、exporter 在執行期讀檔**：值與讀它的規則永遠同版，版本落差的問題消失。但 CI 裡的 da-guard 與 tenant-api 手上沒有那個檔，三個讀取端的答案會不同，違反 ADR-036「同一棵樹只有一個答案」；exporter 也多出「檔不在、檔讀不了」兩種狀態。再議的條件：版本落差的 meta 告警在實際升級中反覆觸發，或出現無法讓 exporter 與規則包同步升級的客戶。
- **只在組裝 ConfigMap 時注入預設值**：走 `existingConfigMap` 的部署與 tenant-api 的 Git 工作目錄拿不到，讀取端之間不一致。
- **讓根層 `"disable"` 合法**：要推翻根層只收數字的規則；`disable` 關一整族的連帶會從租戶擴大到全樹。
- **新增一份明示清單（例如 `disabled_defaults:`）**：與 kube-prometheus-stack 最像、最好搜尋，但要新增一個保留鍵、改 schema 與登錄表，而 `optional_overrides:` 已經表達同一件事。
- **讓 `null` 代表退出**：理由見決策 4。
- **內建層只對「有部署該規則包」的租戶生效**：exporter 不知道部署了哪些規則包，只能從樹的內容推論；推論有已知的誤判，可以用在提示，不能用來決定送不送值。
- **升級時自動刪除複製品**：理由見決策 7。

## 待 owner 決定

1. **退出的寫法**（決策 3、4）。建議如本文：根層列進 `optional_overrides:`、租戶層寫 `"disable"`，根層 `null` 落到內建層。採用它等於接受決策 4 的行為改變。
2. **init 對 32 個 `operator` 鍵的處置。** 選全部規則包時，init 今天把它們連值寫進根層，conf.d 路徑因此啟用 39 個鍵、Helm 路徑 7 個。
   - **維持寫入（建議）**：這些鍵平台沒有預設值，客戶檔裡的數字不會相對於平台值過期，不屬於本決策要解決的問題；維持也避免在同一個版本再疊一個行為改變。註解改為明寫「這些是你的值，平台不出貨」。代價：兩條路徑的不一致留著，登錄表記有實測反例的值（例如 `oracle_sessions_active`）繼續被 init 啟用。
   - **改成不啟用**：鍵名列進 `optional_overrides:`，參考值寫成註解。兩條路徑一致，但新專案少 32 個預設啟用的鍵；只選 `redis` 的專案在自行校準之前沒有任何 Redis 閾值告警。
   - 再議的條件：這些鍵改成比值形式或通過驗證、升為預設啟用鍵時，它們自然進入內建層。

## 實作順序

每一步都要有「把修法還原就失敗」的驗收。

| 步 | 內容 | 驗收 |
|:--|:--|:--|
| 1 | 本 ADR 核可 | — |
| 2 | 執行期訊號與 `platform_defaults` 列的規格依決策 8 更新 | — |
| 3 | 讀取端的內建層：生成表、合併鏈最底層、`key_sources`、鍵判決 | 沒有根層 `defaults:` 的樹送出 7 列、來源為 `builtin`；拿掉內建層就少 7 列；登錄表改了沒重新生成時檢查失敗。對照：租戶覆寫勝出、列進 `optional_overrides:` 沒有列、租戶 `"disable"` 沒有列、critical 送出 |
| 4 | Helm chart | `helm template` 的輸出比對；使用者覆寫仍勝出；`defaultsOptOut` 的鍵沒有列 |
| 5 | init | 產物的 `served-values` 中，預設啟用鍵的列與改動前相同 |
| 6 | 遷移指令與 da-guard 的提示 | 值不同的鍵保留；值相同的 `operator` 鍵保留 |
| 7 | 重新生成範例樹與文件，寫發布說明 | — |

## 相關

- [ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載](017-defaults-yaml-inheritance-dual-hash.md)：本決策在它的合併鏈底下加一層，並改變決策 4 中根層 `null` 的效果。
- [ADR-038: 沒有讀取端讀的鍵以實測判定](038-unread-keys-judged-by-readers.md)：租戶寫預設啟用鍵不再判為沒有讀取端讀；`"disable"` 的規則不變。
- [ADR-036: conf.d 只解析一次](036-single-parser-effective-config.md)：版本落差（§6）與升級前比對（§8）的做法。
- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](001-severity-dedup-via-inhibit.md)：它產生的抑制規則形狀正在修訂（[#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)），同樣會在客戶沒改設定的情況下改變行為。兩者同版出貨時，決策 9 第 1 步的升級前比對一併涵蓋，客戶只遷移一次。
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)：本決策的討論與實作追蹤。
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831)：執行期訊號（決策 8）。
