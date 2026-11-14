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
- **預設啟用鍵**：登錄表裡 `arming: default` 的鍵。目前 11 個：`container_cpu`、`container_cpu_throttle`、`container_memory`、`pg_connections`、`pg_replication_lag`、`mysql_connection_usage_percent`、`mysql_replication_lag`、`es_disk_usage_percent`、`es_heap_usage_percent`、`oracle_tablespace_used_percent`、`rabbitmq_node_mem_percent`。
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
- init 選全部規則包時，根層 `defaults:` 寫入 39 個鍵，其中只有 11 個是預設啟用鍵，另外 28 個是登錄表標為 `operator` 的鍵。只選 `mariadb,kubernetes` 時寫入 5 個。
- init 產生的租戶檔也帶複製品：第一個規則包的基底值被寫成租戶覆寫（例如 `mysql_connection_usage_percent: "80"`，與平台值相同）。
- `da-tools validate-config` 有一列 `unsupplied_defaults`，點名「平台預設啟用、樹裡卻沒供給」的鍵，建議把鍵加進 `defaults:`，刻意不啟用就列進 `optional_overrides:`。它只在有人執行這支工具時看得到，而且前一個建議是再做一份複製品。

業界的做法一致：Prometheus 的 monitoring mixins 讓使用者用 `_config+::` 只覆寫要改的欄位，Helm 讓使用者只寫與 chart `values.yaml` 的差異，kube-prometheus-stack 的預設規則隨 chart 升級、使用者以 `defaultRules.disabled` 逐條關閉。共同點是使用者只放覆寫與退出，預設值跟著出貨物走。

## 決策

### 1. 內建層：讀取端自己帶著預設啟用鍵的值

讀取端內建一張表：每個預設啟用鍵與它的平台值。這張表由登錄表生成、編譯進程式，不在執行期讀檔。

兩條規則決定它的所有行為。

**什麼時候給值**：一個預設啟用鍵，根層既沒有在 `defaults:` 給數字、也沒有列進 `optional_overrides:` 時，內建層才給值。優先序是：根層的數字 → 根層 `optional_overrides:` 的列名（內建層不給值）→ 內建值。一個鍵同時有根層數字又列名時，數字照送；這是現行行為，不變。

**給了之後**：

> **內建層給的值，效果等同把同一個數字寫在根層 `defaults:`，差別只有來源標示。**

所以凡是「根層 `defaults:` 有值」的既有規則，內建層給值的鍵一律適用，不另立規則：

- 子目錄 `_defaults.yaml` 與租戶檔可以覆寫它、可以寫 `"disable"` 關掉它（[ADR-017](017-defaults-yaml-inheritance-dual-hash.md) 決策 2、4）。
- 租戶寫的 `<鍵>_critical` 會送出 critical 列，因為基底有值（ADR-017 決策 3）。
- 租戶寫這個鍵不再被判為沒有讀取端讀（[ADR-038](038-unread-keys-judged-by-readers.md) 決定 4）。
- `"disable"` 關掉一整族、過期後失效的規則照舊（ADR-038 決定 8）。

內建層只收預設啟用鍵。`arming: operator` 的鍵與所有 `_critical` 鍵平台不主張值，不進內建層。

根層檔的狀態決定內建層給不給：

| 根層 `_defaults.yaml` | 內建層 |
|:--|:--|
| 不存在，或存在而 `defaults:` 是空的 | 給值。這是合法的樹，鍵判決不再因為沒有根層檔而回「無從判斷」（修訂 ADR-038 決定 7） |
| 讀不了（例如 YAML 語法錯） | 不給值，維持現行行為：整棵樹不送任何閾值列，da-guard 結束碼 3。根層讀不了時，寫在裡面的覆寫與退出也讀不到；這時送出平台值，等於替客戶打開他退出的告警 |

內建層對每個租戶都生效，不看這個部署有沒有裝對應的規則包。多一列不等於多一條告警：告警要響，還需要規則包已部署、指標存在。Helm 路徑今天就是這樣：chart 把 11 個鍵送給每個租戶。

### 2. 合併順序與來源標示

套用順序變成：**內建層** → 根層 → 往下每一層子目錄 → profile 與根平台檔的 `tenants:` → 租戶檔。後套用的覆蓋先套用的。

- 最終設定（`da-guard effective`、tenant-api 的 `/effective`）會顯示內建層給的鍵，`key_sources` 標 `layer: builtin`，沒有 `file`。
- `merged_hash`（最終設定的雜湊，ADR-017 決策 7）涵蓋內建層的值。平台改了某個內建值，沒覆寫它的租戶的雜湊跟著變，這正是它要回答的問題。
- 根層對每一個預設啟用鍵都寫了與平台相同的數字的樹，升級前後每個租戶的最終設定與 `merged_hash` 都不變。這是驗收項。只寫了其中幾個鍵的樹，沒寫的鍵會進入最終設定，雜湊跟著變。

### 3. 三種寫法：沿用、覆寫、退出

| 想要的結果 | 寫法 | 寫在哪 |
|:--|:--|:--|
| 用平台值，並跟著平台之後的校準 | 不寫這個鍵 | — |
| 整個平台用自己的數字（也等於釘住，不跟平台變動） | `defaults:` 寫數字 | 根層 |
| 整個平台不要這個預設值 | 鍵名列進 `optional_overrides:` | 根層 |
| 某一層子目錄或某個租戶不要 | `<鍵>: "disable"` | 子目錄 `_defaults.yaml` 或租戶檔 |
| 某個租戶用自己的數字 | `<鍵>: "70"` | 租戶檔 |

退出沿用 `optional_overrides:`，不新增語法。這個欄位原本的意思就是「平台認得這個鍵、但不主張它的值」，對預設啟用鍵寫下它，意思不變：內建層不給值，租戶自己寫了才送出。`validate-config` 現有的提示已經這樣教。

全平台退出一個鍵之後，它就照 `optional_overrides:` 鍵的現行規則走，有三件事和「有平台值的鍵」不同：

- 租戶可以自己寫基底值把它開回來，但 `<鍵>_critical` 不送（critical 列要基底在根層或內建層有值），da-guard 報 `no_row`、結束碼 1。init 會在租戶檔寫入 `_critical` 建議值，所以退出一個鍵之前，先刪掉各租戶檔裡它的 `_critical`。
- profile 給它的值不套用。
- 子目錄 `_defaults.yaml` 給它的值會送出。

只想讓部分租戶不要、其餘保有 critical 層，就不要全平台退出，改在那些租戶或子目錄寫 `"disable"`。

根層 `defaults:` 仍然只收數字。在根層寫 `"disable"` 照舊是讀不了的值，da-guard 以 `root_default_refused` 報錯、結束碼 1；這時該鍵落到內建層的值，不會因為一個被拒收的寫法而少掉告警。錯誤訊息裡「exporter 不送這個鍵的預設列」那一句跟著改。

### 4. 根層寫 `null` 的效果改變

ADR-017 決策 4 的原則不變：閾值寫 `null` 等於這一層沒寫。改變的是「沒寫」之後落到哪裡：

| 根層 `defaults:` 寫 `鍵: null` | 現況 | 本決策之後 |
|:--|:--|:--|
| 預設啟用鍵 | 根層沒宣告：不送這一列，租戶給的值由 `root_default_null_undeclared` 指名 | 落到內建層：送出平台值 |
| 其他鍵 | 同上 | 不變 |

不讓 `null` 代表退出的理由與 ADR-017 相同：`鍵:` 後面留白解析出來就是 `null`，打到一半忘了填值不該靜靜關掉一條告警。

Helm 使用者要特別注意：Helm 把設成 `null` 的值從合併結果刪掉，所以 `--set thresholdConfig.defaults.pg_connections=null` 今天的效果是「ConfigMap 裡沒有這個鍵、不送這一列」，本決策之後是「落到內建層、送出平台值」。chart 看不到被刪掉的鍵，無法代為轉換；替代寫法見決策 6。

### 5. 內建表怎麼產生、各元件怎麼對齊

- 內建表由 `check_threshold_registry.py --regen` 從登錄表生成為 Go 原始碼，與現有的退役鍵表同一套機制；登錄表改了而沒有重新生成時，`--ci` 檢查失敗。
- exporter、da-guard、tenant-api 從同一份原始碼編譯進這張表。

**對齊的判準是內建表的摘要，不是版號。** exporter、da-tools（內含 da-guard）、tenant-api 各有自己的發布線與版號，規則包沒有自己的發布線，所以「同一版」沒有可以比對的東西。每個元件都公開它編譯進去的內建表摘要（決策 8），規則包帶生成它的那份登錄表的摘要；摘要相同就是對齊。

摘要不同時會發生什麼，依方向而定：

| 落差 | 結果 | 會不會自己被發現 |
|:--|:--|:--|
| 規則包的登錄表比 exporter 新 | 新的預設啟用鍵沒有內建值，告警不響 | 不會。這是本決策要消除的失效方式，只剩這一個成因 |
| exporter 比規則包新 | 新增的鍵多出沒有規則讀的列；重新校準過的值由舊規則照讀 | 不會 |
| CI 的 da-guard 或 tenant-api 比部署中的 exporter 新 | 靠內建層的樹或寫入在檢查端通過，exporter 卻不送那些列 | 不會。[ADR-036](036-single-parser-effective-config.md) §6 承認的版本落差是「任一邊拒收就進不去」，這一種是放行，比它嚴重 |

三種都不會報錯，所以要有主動的偵測與一道閘門：

- 偵測：摘要比對的 meta 告警（決策 8）。
- 閘門：遷移指令在刪除複製品之前，要求使用者給出部署中 exporter 的摘要（從它的指標讀），與指令自己的不同就拒絕（決策 7）。刪除複製品是唯一會讓「檢查端較新」變成告警消失的動作。

**改動內建表是 BREAKING。** 改值、新增或移除預設啟用鍵，都會在客戶的 repo 沒有任何 diff 的情況下改變告警行為。依據與 Kubernetes API 的慣例相同：相容的變更不得改變預設值的語意。機制：生成的內建表有差異的變更，必須帶一個標為 Breaking 的 changelog 片段，由登錄表的 `--ci` 檢查擋。

**把原本列在 `optional_overrides:` 的鍵升為預設啟用**要另外處理：init 把這份清單複製進客戶的樹，既有的樹會把新升格的鍵讀成「已退出」，新的預設值進不來，也沒有錯誤。內建表因此為每個鍵記下它從哪個內建表版本起預設啟用，`platform_defaults` 列對「列為退出、而且是後來才升格」的鍵特別標出，發布說明逐鍵列出。這只能讓它看得見，分不出客戶是刻意退出還是留著舊清單；這是沿用 `optional_overrides:` 當退出寫法的代價（見「待 owner 決定」第 1 項）。

### 6. Helm chart

- **chart 照舊把生成的預設值寫進它渲染的 ConfigMap。** `thresholdConfig.defaults` 不改成空的。chart 的值本來就隨 chart 升級，沒有複製品過期的問題；改成空的反而讓「新 chart 配上被釘住的舊 exporter 映像」這個組合少掉全部預設列而沒有任何錯誤。chart 渲染的值在決策 8 的狀態裡算 `overridden`。
- chart 新增一個值 `thresholdConfig.defaultsOptOut`（鍵名清單）。列在裡面的鍵，chart 不渲染它的預設值，並把它併進 `optional_overrides:`。Helm 對清單是整個取代、不合併，讓使用者直接覆寫 `thresholdConfig.optional_overrides` 等於要他複製 chart 生成的整份清單；另給一個只放退出項的清單可以避免這件事。conf.d 的語法仍然只有 `optional_overrides:` 一種。
- 設了 `thresholdConfig.existingConfigMap` 的部署不經 chart 的值，直接適用決策 1 到 4。

### 7. `da-tools init` 與既有的樹

**新專案**：

- 根層 `defaults:` 不寫預設啟用鍵，改以註解列出內建值與決策 3 的三種寫法。
- 租戶檔不寫與上層相同的值。
- `operator` 鍵的處置見「待 owner 決定」。

**既有的樹**：升級本身不改任何檔案。帶著複製品的樹繼續照自己寫的數字運作，那些數字從此算是覆寫。

- da-guard 對根層每個「值與內建值相同」的預設啟用鍵報一則提示（不影響結束碼），說明刪掉它就會跟著平台之後的校準。
- 另提供一支遷移指令，預設只預覽（dry-run），列出三件事：它會刪掉哪些鍵、升級後哪些租戶會多出哪些列、根層哪些 `null` 要改寫成 `optional_overrides:`。寫入經 `da-guard write`。
  - 只刪「值與內建值相同的預設啟用鍵」。值不同的保留（那是客戶的覆寫）；`operator` 鍵即使值與登錄表相同也保留（它沒有內建值，刪掉就是關告警）；同時列在 `optional_overrides:` 的鍵也保留並指名（刪掉數字，它就變成退出）。
  - 寫入之前要求使用者給出部署中 exporter 的內建表摘要，與指令自己的不同就拒絕（決策 5）。同一份 conf.d 供給多個叢集時，每個叢集都要給。
  - init 產生的檔案在 `.da-init.yaml` 留有 sha256。檔案與紀錄相符（沒被手改過）時可以直接刪；不相符時要使用者明示同意，因為分不出一個與平台相同的數字是複製品還是刻意釘住的值。
  - 租戶檔裡與上層相同的值同樣處理。

不在升級時自動刪除：與平台值相同的數字可能是刻意釘住的，刪掉之後它會跟著平台變動，這個選擇應該由客戶做。

### 8. 升級訊號：讓客戶看得到哪些告警被打開

三個訊號，答案都來自讀取端自己，不由工具另外讀登錄表推算：

- **`da-tools validate-config`**：現有的 `unsupplied_defaults` 列由 `platform_defaults` 列取代。它逐一列出預設啟用鍵的狀態：`builtin`（樹裡沒寫、由內建層供給，附平台值與受影響的租戶數）、`overridden`（根層給了值；與平台值相同時註明）、`opted_out`（列在 `optional_overrides:`），並印出內建表的版本摘要。這一列是說明，不是警告：樹裡沒寫某個預設啟用鍵是正常狀態。原本「把鍵加進 `defaults:`」的建議移除。在內建層上線之前，現有的列與建議對現行行為是正確的，兩者在同一個版本一起替換。
- **執行期指標**：
  - exporter 為每個預設啟用鍵送一條狀態指標（`builtin`／`overridden`／`opted_out`），不帶租戶標籤。同時有根層數字又列在 `optional_overrides:` 的鍵算 `overridden`。
  - exporter 與 tenant-api 各送一條帶內建表摘要的資訊指標；規則包帶生成它的登錄表摘要。平台規則包的一條 meta 告警在摘要不一致持續一段時間時觸發。這條告警的規格有三個要求：exporter 沒有送出摘要指標（舊版）也算不一致；告警文字寫出哪一邊是哪個摘要，因為哪一邊較新決定了後果（決策 5 的表）；它隨平台規則包出貨，threshold-exporter chart 不出貨規則包，沒有部署平台規則包的客戶沒有這條告警。
  - 原先構想的「未供給的預設鍵」指標在本決策之後只剩摘要落差一個成因，所以改以摘要比對表達。規劃中的退役鍵指標不受本決策影響。
- **升級前的比對**：沿用 ADR-036 §8 的做法。以新版 da-guard 驗自己的樹，再以新舊兩版各跑一次 `da-guard served-values`，比對多出的列；作法寫進發布說明與漸進遷移 playbook。

### 9. 升級順序

1. 以新版 da-guard 驗 conf.d，並比對新舊兩版的 `served-values`，確認多出的列。
2. 不想要的預設啟用鍵：整個平台不要就列進 `optional_overrides:`（Helm：`thresholdConfig.defaultsOptOut`），個別租戶不要就寫 `"disable"`。根層與 Helm 的 `null` 一併改寫。
3. 升級 threshold-exporter、規則包、tenant-api、da-tools，直到四者的內建表摘要相同。
4. **全部升級完之後**才執行遷移指令、刪掉複製品。

第 4 步不能提前：舊版讀取端沒有內建層，讀到一棵已經刪掉複製品的樹，那些鍵就沒有值，告警不響也沒有錯誤。遷移指令會比對摘要（決策 7），手動刪除則沒有東西擋。同理，要回退到舊版，先把複製品放回去。同一份 conf.d 供給多個叢集時，等所有叢集都升級完再刪。

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

**本決策之後**（預期輸出，尚未實作；數字取自同一棵樹，把內建層依決策 1 會給的值寫進根層 `defaults:`，依等價規則兩者相同）：

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

- **省略一個鍵的意思變了。** 既有的樹升級後，每個租戶最多多出 11 條基底列，另加因為基底現在有值而送出的列：租戶寫的 `_critical`，以及下一條說的過期 `disable`。在本 repo 的樹上量到的幅度：

  | 樹 | 租戶數 | 列數 前 → 後 |
  |:--|:--|:--|
  | repo 的開發範例樹（`components/threshold-exporter/config/conf.d`） | 5 | 95 → 95 |
  | `try-local/seed/conf.d` | 2 | 9 → 19 |
  | init，選 `mariadb,kubernetes` | 1 | 10 → 12 |
  | init，選全部規則包 | 2 | 98 → 98 |
  | init，只選 `redis` | 2 | 8 → 22 |
  | 上方範例 | 1 | 1 → 11 |

- **過期的 `disable` 開始失效。** 根層沒有值的鍵，`"disable"` 的 `expires:` 現行不看，等於永久關閉（ADR-038 決定 8）。內建層給值之後，這些鍵比照根層有值的鍵：`disable` 過期即失效，基底列與它的維度列一起回來。
- **平台改值會在客戶沒有 diff 的情況下改變告警。** 這是本決策的目的，也是它的代價。護欄是決策 5 的 BREAKING 條目與決策 8 的升級前比對；不接受的客戶把數字寫進根層，就釘住了。
- **元件之間的落差只能偵測，不能消除。** 決策 5 表裡的三種落差都不會報錯；meta 告警要有部署平台規則包才有，遷移指令的閘門只擋它自己的刪除。
- **回退與混合版本有靜默失效的窗口**（決策 9 第 4 步）。舊版讀取端無從得知樹已經改成只放覆寫，這一段只能靠升級順序避免。
- **到達列數上限的租戶會少掉自己寫的列。** 多出的列計入 `max_metrics_per_tenant`（預設 500）。超過上限時 exporter 截斷，先被截掉的是租戶寫的維度列，不是新增的基底列。da-guard 既有的列數檢查以 `cardinality_exceeded` 指名這些租戶、結束碼 1，所以升級前比對（決策 9 第 1 步）會看到；沒做比對就升級的部署，只會在 exporter 的日誌看到截斷訊息。
- **不涵蓋的複製品。** 根層的 `optional_overrides:` 與 `dimensions:` 兩份清單、init 寫進租戶檔的 `_critical` 建議值，仍然是 init 當天的複製品。`dimensions:` 過期時租戶寫的維度鍵沒有列、da-guard 報錯，不是靜默。`optional_overrides:` 過期有兩種結果：平台新宣告的鍵在客戶的樹上被拒收（報錯），以及平台把清單上的鍵升為預設啟用時被讀成退出（靜默，處理方式見決策 5）。這兩份清單與 `_critical` 建議值不在本決策處理；再議的條件是實際升級中出現前述任一種回報。
- **沒有量到的事**：多出的列實際會變成幾條告警（取決於客戶的指標與已部署的規則包）、多少客戶用了 Helm 的 `null` 或手改過預設值、大量租戶下多出的列對 exporter 的成本。

## 考慮過的替代方案

- **維持複製，只靠檢查點名**：訊號只給有跑檢查的部署，而且每次平台新增預設啟用鍵，每個客戶都要手動補一次。檢查留下來當升級訊號，不當解法。
- **讓 init 重跑來更新**：`init --force` 會重寫它產生的每一個檔、丟掉所有手改。
- **登錄表隨規則包出貨、exporter 在執行期讀檔**：值與讀它的規則永遠同版，版本落差的問題消失。但 CI 裡的 da-guard 與 tenant-api 手上沒有那個檔，三個讀取端的答案會不同，違反 ADR-036「同一棵樹只有一個答案」；exporter 也多出「檔不在、檔讀不了」兩種狀態。再議的條件：版本落差的 meta 告警在實際升級中反覆觸發，或出現無法讓 exporter 與規則包同步升級的客戶。
- **只在組裝 ConfigMap 時注入預設值**：走 `existingConfigMap` 的部署與 tenant-api 的 Git 工作目錄拿不到，讀取端之間不一致。
- **讓根層 `"disable"` 合法**：要推翻根層只收數字的規則；`disable` 關一整族的連帶會從租戶擴大到全樹。
- **新增一份只放退出的清單（例如 `disabled_defaults:`）**：與 kube-prometheus-stack 最像、最好搜尋，而且退出與「宣告但不給值」分開之後，舊的 `optional_overrides:` 複製品不會被讀成退出（決策 5 最後一段的問題消失）。代價是根層多一個頂層鍵、改 schema，既有提示教的寫法要改，今天已經靠 `optional_overrides:` 關掉預設啟用鍵的樹要遷移。這是「待 owner 決定」第 1 項的另一個選項。
- **Helm chart 的 `thresholdConfig.defaults` 改成空的**：理由見決策 6。
- **讓 `null` 代表退出**：理由見決策 4。
- **內建層只對「有部署該規則包」的租戶生效**：exporter 不知道部署了哪些規則包，只能從樹的內容推論；推論有已知的誤判，可以用在提示，不能用來決定送不送值。
- **升級時自動刪除複製品**：理由見決策 7。

## 待 owner 決定

1. **退出的寫法**（決策 3、4）。兩個選項都讓根層 `null` 落到內建層，採用任一個都等於接受決策 4 的行為改變。
   - **沿用 `optional_overrides:`（本文的寫法，建議）**：不新增語法，與現有提示一致。代價：日後把清單上的鍵升為預設啟用時，既有的樹靜默地讀成退出，只能靠 `platform_defaults` 列與發布說明讓它看得見。
   - **新增只放退出的清單**：沒有上述的靜默情況，代價見「考慮過的替代方案」。若預期會把 `optional_overrides:` 層的鍵升為預設啟用，這個選項比較好。
2. **init 對 28 個 `operator` 鍵的處置。** 選全部規則包時，init 今天把它們連值寫進根層，conf.d 路徑因此啟用 39 個鍵、Helm 路徑 11 個。
   - **維持寫入（建議）**：這些鍵平台沒有預設值，客戶檔裡的數字不會相對於平台值過期，不屬於本決策要解決的問題；維持也避免在同一個版本再疊一個行為改變。日後其中的鍵升為預設啟用時，客戶寫的數字是看得見的覆寫；改成列名的話，升格時會遇到第 1 項說的靜默退出。註解改為明寫「這些是你的值，平台不出貨」。代價：兩條路徑的不一致留著，登錄表記有實測反例的值（例如 `oracle_sessions_active`）繼續被 init 啟用。
   - **改成不啟用**：鍵名列進 `optional_overrides:`，參考值寫成註解。兩條路徑一致，但新專案少 28 個預設啟用的鍵；只選 `redis` 的專案在自行校準之前沒有任何 Redis 閾值告警。
   - 再議的條件：這些鍵改成比值形式或通過驗證、升為預設啟用鍵時，它們自然進入內建層。

## 實作順序

每一步都要有「把修法還原就失敗」的驗收。

| 步 | 內容 | 驗收 |
|:--|:--|:--|
| 1 | 本 ADR 核可 | — |
| 2 | 執行期訊號與 `platform_defaults` 列的規格依決策 8 更新 | — |
| 3 | 讀取端的內建層：生成表、合併鏈最底層、`key_sources`、鍵判決 | 沒有根層檔的樹、與根層 `defaults:` 是空的樹，都送出 11 列、來源為 `builtin`；拿掉內建層就少 11 列；登錄表改了沒重新生成時檢查失敗；內建表有差異而沒有 Breaking 片段時檢查失敗。對照：租戶覆寫勝出、列進 `optional_overrides:` 沒有列、同時有根層數字又列名的鍵照送、租戶 `"disable"` 沒有列、critical 送出、根層讀不了時沒有列 |
| 4 | Helm chart 的 `defaultsOptOut` | `helm template` 的輸出比對；沒設時輸出與改動前相同；列在裡面的鍵沒有列 |
| 5 | init | 產物的 `served-values` 中，預設啟用鍵的列與改動前相同 |
| 6 | 遷移指令與 da-guard 的提示 | 值不同的鍵保留；值相同的 `operator` 鍵保留；同時列名的鍵保留；摘要不同時拒絕寫入 |
| 7 | 重新生成範例樹與文件，寫發布說明 | — |

## 相關

- [ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載](017-defaults-yaml-inheritance-dual-hash.md)：本決策在它的合併鏈底下加一層。對預設啟用鍵，它改變三句話的結果：決策 4 的根層 `null` 與根層 `"disable"`（都改為落到內建值），以及決策 3「子目錄只能改根目錄已宣告的閾值」（內建層給值的鍵算已宣告）。
- [ADR-038: 沒有讀取端讀的鍵以實測判定](038-unread-keys-judged-by-readers.md)：租戶寫預設啟用鍵不再判為沒有讀取端讀（決定 4）；沒有根層檔的樹不再是「無從判斷」（決定 7）；`"disable"` 的規則不變，但適用「根層有值」那一支的鍵變多（決定 8）。
- [ADR-036: conf.d 只解析一次](036-single-parser-effective-config.md)：升級前比對（§8）的做法；§6 的版本落差在本決策多出一種放行的方向（決策 5）。
- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](001-severity-dedup-via-inhibit.md)：它產生的抑制規則形狀正在修訂（[#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)），同樣會在客戶沒改設定的情況下改變行為。兩者同版出貨時，決策 9 第 1 步的升級前比對一併涵蓋，客戶只遷移一次。
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)：本決策的討論與實作追蹤。
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831)：執行期訊號（決策 8）。
