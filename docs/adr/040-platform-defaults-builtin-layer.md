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

**決策摘要**：平台出貨即啟用的閾值預設值，改由讀 conf.d 的程式自己帶著（下稱「內建層」），排在根目錄 `_defaults.yaml` 之下、當作合併鏈的最底層。客戶的樹不再需要、`da-tools init` 也不再寫入這些值的複製品：沒寫的鍵拿平台值，寫了數字就是覆寫，寫 `"disable"` 就是退出。這是破壞性變更，隨 v3.0 出貨：省略一個鍵的意思，從「不宣告」變成「用平台值」。

## 狀態

🟡 **Proposed**（2026-11-14）。尚未實作；本文描述「現況」的句子都在 main 上量過，描述「決定」的句子是實作後要成立的行為。owner 核可後昇格 Accepted。

2026-11-14 修訂（owner 裁決）：全平台退出改為在根層寫 `"disable"`，不再借用 `optional_overrides:`（決策 3）；init 對 `operator` 鍵的寫法移出本決策，另案處理（決策 7）。

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
- `da-tools validate-config` 有一列 `unsupplied_defaults`，點名「平台預設啟用、樹裡卻沒供給」的鍵，建議把鍵加進 `defaults:`，刻意不啟用就列進 `optional_overrides:`。它只在有人執行這支工具時看得到，而且前一個建議是再做一份複製品。

業界的做法一致：Prometheus 的 monitoring mixins 讓使用者用 `_config+::` 只覆寫要改的欄位，Helm 讓使用者只寫與 chart `values.yaml` 的差異，kube-prometheus-stack 的預設規則隨 chart 升級、使用者以 `defaultRules.disabled` 逐條關閉。共同點是使用者只放覆寫與退出，預設值跟著出貨物走。

## 決策

### 1. 內建層：讀取端自己帶著預設啟用鍵的值

讀取端內建一張表：每個預設啟用鍵與它的平台值。這張表由登錄表生成、編譯進程式，不在執行期讀檔。

一條規則決定它的所有行為：

> **內建層給的值，效果等同把同一個數字寫在根層 `defaults:`，差別只有來源標示。**

根層自己給了數字的鍵用根層的數字；其餘的預設啟用鍵一律由內建層給值。根層的 `optional_overrides:` 不影響內建層（見決策 3）。

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

內建層對每個租戶都生效，不看這個部署有沒有裝對應的規則包。多一列不等於多一條告警：告警要響，還需要規則包已部署、指標存在。Helm 路徑今天就是這樣：chart 把 7 個鍵送給每個租戶。

### 2. 合併順序與來源標示

套用順序變成：**內建層** → 根層 → 往下每一層子目錄 → profile 與根平台檔的 `tenants:` → 租戶檔。後套用的覆蓋先套用的。

- 最終設定（`da-guard effective`、tenant-api 的 `/effective`）會顯示內建層給的鍵，`key_sources` 標 `layer: builtin`，沒有 `file`。
- `merged_hash`（最終設定的雜湊，ADR-017 決策 7）涵蓋內建層的值。平台改了某個內建值，沒覆寫它的租戶的雜湊跟著變，這正是它要回答的問題。
- 根層對每一個預設啟用鍵都寫了與平台相同的數字的樹，升級前後每個租戶的最終設定與 `merged_hash` 都不變。這是驗收項。只寫了其中幾個鍵的樹，沒寫的鍵會進入最終設定，雜湊跟著變。

### 3. 三種寫法：沿用、覆寫、退出

| 想要的結果 | 寫法 | 寫在哪 |
|:--|:--|:--|
| 用平台值，並跟著平台之後的校準 | 不寫這個鍵 | — |
| 整個平台用自己的數字（也等於釘住，不跟平台變動） | `<鍵>: 70` | 根層 `defaults:` |
| 整個平台不要這個預設值 | `<鍵>: "disable"` | 根層 `defaults:` |
| 某一層子目錄或某個租戶不要 | `<鍵>: "disable"` | 子目錄 `_defaults.yaml` 或租戶檔 |
| 某個租戶用自己的數字 | `<鍵>: "70"` | 租戶檔 |

「關掉」在每一層都是同一個寫法。根層的 `"disable"` 與子目錄 `_defaults.yaml` 寫 `"disable"` 的現行效果相同，只是往上一層：

- 每個租戶都繼承它，整族關閉（基底、`<鍵>_critical`、維度鍵，[ADR-038](038-unread-keys-judged-by-readers.md) 決定 8）。
- 子目錄、profile 或租戶自己給了值，那個範圍的整族就回來，包括租戶寫的 `<鍵>_critical`。
- 租戶檔裡的 `<鍵>_critical` 或維度鍵被繼承來的 `disable` 關掉時，da-guard 報 `disable_cascade` 警告，不擋。

根層的 `"disable"` 比子目錄嚴格，有四個限制：

- **只收 `disable` 這個字。** 子目錄今天把 `false`、`off` 也讀成停用且不報任何訊息；根層收這些別名的話，`helm --set …=false` 會靜靜關掉全樹的一族。別名在根層是讀不了的值。
- **不收排程與 `expires:`。** 限時關閉寫在子目錄或租戶檔。
- **只對內建層給值的鍵有效。** 其他鍵在根層沒有東西可關，照舊是讀不了的值。
- **根層不得同時給同一族的值。** 根層寫了 `<鍵>: "disable"` 又給 `<鍵>_critical` 或 `<鍵>{…}` 的預設值，da-guard 報錯、結束碼 1。

根層讀不了的值照舊以 `root_default_refused` 報錯、結束碼 1，該鍵落到內建值：一個被拒收的寫法不會讓告警少掉。

**`optional_overrides:` 的意思不變**：宣告「平台認得這個鍵、但不給值」，租戶自己寫了才送出。它不是退出的寫法，把預設啟用鍵列在裡面不影響內建值。根層沒給數字卻列了預設啟用鍵時，da-guard 報錯、結束碼 1，訊息指向 `"disable"`：`validate-config` 現有的提示教過用列名來關掉，照舊讀法升級會把那些告警打開。

### 4. 根層寫 `null` 的效果改變

ADR-017 決策 4 的原則不變：閾值寫 `null` 等於這一層沒寫。改變的是「沒寫」之後落到哪裡：

| 根層 `defaults:` 寫 `鍵: null` | 現況 | 本決策之後 |
|:--|:--|:--|
| 預設啟用鍵 | 根層沒宣告：不送這一列，租戶給的值由 `root_default_null_undeclared` 指名 | 落到內建層：送出平台值 |
| 其他鍵 | 同上 | 不變 |

不讓 `null` 代表退出的理由與 ADR-017 相同：`鍵:` 後面留白解析出來就是 `null`，打到一半忘了填值不該靜靜關掉一條告警。

Helm 使用者要特別注意：Helm 把設成 `null` 的值從合併結果刪掉，所以 `--set thresholdConfig.defaults.pg_connections=null` 今天的效果是「ConfigMap 裡沒有這個鍵、不送這一列」，本決策之後是「落到內建層、送出平台值」。chart 看不到被刪掉的鍵，無法代為轉換；要關掉就改成 `--set thresholdConfig.defaults.pg_connections=disable`。

### 5. 內建表怎麼產生、各元件怎麼對齊

- 內建表由 `check_threshold_registry.py --regen` 從登錄表生成為 Go 原始碼，與現有的退役鍵表同一套機制；登錄表改了而沒有重新生成時，`--ci` 檢查失敗。
- exporter、da-guard、tenant-api 從同一份原始碼編譯進這張表。

**對齊的判準是內建表的摘要，不是版號。** exporter、da-tools（內含 da-guard）、tenant-api 各有自己的發布線與版號，規則包沒有自己的發布線，所以「同一版」沒有可以比對的東西。每個元件都公開它編譯進去的內建表摘要（決策 8），規則包帶生成它的那份登錄表的摘要；摘要相同就是對齊。

元件不對齊時會發生什麼，依方向而定：

| 落差 | 結果 | 會不會自己被發現 |
|:--|:--|:--|
| 規則包的登錄表比 exporter 新 | 新的預設啟用鍵沒有內建值，告警不響 | 不會。這是本決策要消除的失效方式，只剩這一個成因 |
| exporter 比規則包新 | 新增的鍵多出沒有規則讀的列；重新校準過的值由舊規則照讀 | 不會 |
| CI 的 da-guard 或 tenant-api 比部署中的 exporter 新 | 靠內建層的樹或寫入在檢查端通過，exporter 卻不送那些列 | 不會。[ADR-036](036-single-parser-effective-config.md) §6 承認的版本落差是「任一邊拒收就進不去」，這一種是放行，比它嚴重 |
| 樹裡有根層 `"disable"`，exporter 還是舊版 | 舊版把它當成讀不了的值：基底列不送，但不連帶整族，租戶寫的 `<鍵>_critical` 與根層給的維度預設值照送 | 不會。多的是告警，不是少 |

四種都不會報錯，所以要有主動的偵測與一道閘門：

- 偵測：摘要比對的 meta 告警（決策 8）。
- 閘門：遷移指令在刪除複製品之前，要求使用者給出部署中 exporter 的摘要（從它的指標讀），與指令自己的不同就拒絕（決策 7）。刪除複製品是唯一會讓「檢查端較新」變成告警消失的動作。

**改動內建表是 BREAKING。** 改值、新增或移除預設啟用鍵，都會在客戶的 repo 沒有任何 diff 的情況下改變告警行為。依據與 Kubernetes API 的慣例相同：相容的變更不得改變預設值的語意。機制：生成的內建表有差異的變更，必須帶一個標為 Breaking 的 changelog 片段，由登錄表的 `--ci` 檢查擋。

### 6. Helm chart

- **chart 照舊把生成的預設值寫進它渲染的 ConfigMap。** `thresholdConfig.defaults` 不改成空的。chart 的值本來就隨 chart 升級，沒有複製品過期的問題；改成空的反而讓「新 chart 配上被釘住的舊 exporter 映像」這個組合少掉全部預設列而沒有任何錯誤。chart 渲染的數字在決策 8 的狀態裡算 `overridden`。
- 退出不需要新的值：`--set thresholdConfig.defaults.<鍵>=disable` 把 chart 渲染的數字換成 `"disable"`，就是決策 3 的根層寫法。`=false` 這類別名會被拒收並報錯，告警維持開啟。
- 設了 `thresholdConfig.existingConfigMap` 的部署不經 chart 的值，直接適用決策 1 到 4。

### 7. `da-tools init` 與既有的樹

**新專案**：

- 根層 `defaults:` 不寫預設啟用鍵，改以註解列出內建值與決策 3 的三種寫法。
- 租戶檔不寫與上層相同的值。
- `operator` 鍵不在本決策的範圍，init 對它們的寫法不變。平台對這些鍵沒有預設值，客戶檔裡的數字不會相對於平台值過期。讓「哪些鍵預設啟用」只由登錄表決定是另一件事，它有自己的前置條件，見 [#3924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3924)。

**既有的樹**：升級本身不改任何檔案。帶著複製品的樹繼續照自己寫的數字運作，那些數字從此算是覆寫。

- da-guard 對根層每個「值與內建值相同」的預設啟用鍵報一則提示（不影響結束碼），說明刪掉它就會跟著平台之後的校準。
- 另提供一支遷移指令，預設只預覽（dry-run），列出三件事：它會刪掉哪些鍵、升級後哪些租戶會多出哪些列、哪些今天靠根層 `null` 或 `optional_overrides:` 列名關掉的預設啟用鍵要改寫成 `"disable"`。寫入經 `da-guard write`。
  - 只刪「值與內建值相同的預設啟用鍵」。值不同的保留（那是客戶的覆寫）；`operator` 鍵即使值與登錄表相同也保留（它沒有內建值，刪掉就是關告警）。
  - 寫入之前要求使用者給出部署中 exporter 的內建表摘要，與指令自己的不同就拒絕（決策 5）。同一份 conf.d 供給多個叢集時，每個叢集都要給。
  - init 產生的檔案在 `.da-init.yaml` 留有 sha256。檔案與紀錄相符（沒被手改過）時可以直接刪；不相符時要使用者明示同意，因為分不出一個與平台相同的數字是複製品還是刻意釘住的值。
  - 租戶檔裡與上層相同的值同樣處理。

不在升級時自動刪除：與平台值相同的數字可能是刻意釘住的，刪掉之後它會跟著平台變動，這個選擇應該由客戶做。

### 8. 升級訊號：讓客戶看得到哪些告警被打開

三個訊號，答案都來自讀取端自己，不由工具另外讀登錄表推算：

- **`da-tools validate-config`**：現有的 `unsupplied_defaults` 列由 `platform_defaults` 列取代。它逐一列出預設啟用鍵的狀態：`builtin`（樹裡沒寫、由內建層供給，附平台值與受影響的租戶數）、`overridden`（根層給了值；與平台值相同時註明）、`opted_out`（根層寫了 `"disable"`），並印出內建表的摘要。這一列是說明，不是警告：樹裡沒寫某個預設啟用鍵是正常狀態。原本「把鍵加進 `defaults:`，或列進 `optional_overrides:`」的建議移除。在內建層上線之前，現有的列與建議對現行行為是正確的，兩者在同一個版本一起替換。
- **執行期指標**：
  - exporter 為每個預設啟用鍵送一條狀態指標（`builtin`／`overridden`／`opted_out`），不帶租戶標籤。
  - exporter 與 tenant-api 各送一條帶內建表摘要的資訊指標；規則包帶生成它的登錄表摘要。平台規則包的一條 meta 告警在摘要不一致持續一段時間時觸發。這條告警的規格有三個要求：exporter 沒有送出摘要指標（舊版）也算不一致；告警文字寫出哪一邊是哪個摘要，因為哪一邊較新決定了後果（決策 5 的表）；它隨平台規則包出貨，threshold-exporter chart 不出貨規則包，沒有部署平台規則包的客戶沒有這條告警。
  - 原先構想的「未供給的預設鍵」指標在本決策之後只剩摘要落差一個成因，所以改以摘要比對表達。規劃中的退役鍵指標不受本決策影響。
- **升級前的比對**：沿用 ADR-036 §8 的做法。以新版 da-guard 驗自己的樹，再以新舊兩版各跑一次 `da-guard served-values`，比對多出的列；作法寫進發布說明與漸進遷移 playbook。

### 9. 升級順序

1. 以新版 da-guard 驗 conf.d，並比對新舊兩版的 `served-values`，確認多出的列。
2. CI 換成新版 da-tools，並在同一個變更裡處理不想要的預設啟用鍵：整個平台不要就在根層寫 `"disable"`（Helm：`--set thresholdConfig.defaults.<鍵>=disable`），個別租戶不要就在租戶檔寫 `"disable"`。今天靠根層 `null`、Helm 的 `null` 或 `optional_overrides:` 列名關掉的鍵一併改寫。
3. 升級 threshold-exporter、規則包、tenant-api，直到它們與 da-tools 的內建表摘要相同。
4. **全部升級完之後**才執行遷移指令、刪掉複製品。

第 2 步排在第 3 步之前：根層的 `"disable"` 在舊版 exporter 上一樣不送基底列（決策 5 的表），所以先改樹不會打開告警；反過來先升級 exporter，不想要的告警會在改樹之前先響。舊版 da-guard 會拒收根層的 `"disable"`，所以樹與 CI 的 da-tools 要在同一個變更裡換。

第 4 步不能提前：舊版讀取端沒有內建層，讀到一棵已經刪掉複製品的樹，那些鍵就沒有值，告警不響也沒有錯誤。遷移指令會比對摘要（決策 7），手動刪除則沒有東西擋。同理，要回退到舊版，先把複製品放回去。同一份 conf.d 供給多個叢集時，等所有叢集都升級完再刪。

### 範例

`conf.d/_defaults.yaml`：

```yaml
defaults:
  redis_connected_clients: 1000
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

**本決策之後**（預期輸出，尚未實作；數字取自一棵等價的樹：把內建值寫進根層 `defaults:`）：

```
container_cpu_throttle           25  warning    ← 內建層
container_memory                 85  warning    ← 內建層
mysql_connection_usage_percent   80  warning    ← 內建層
mysql_replication_lag            30  warning    ← 內建層
pg_connections                   70  warning    ← 租戶覆寫
pg_replication_lag               30  warning    ← 內建層
pg_replication_lag_critical      60  critical   ← 基底有內建值，租戶的 critical 送出
redis_connected_clients        1000  warning    ← 根層
```

`container_cpu` 被租戶的 `"disable"` 關掉。

**全平台退出**：在上面的根層 `defaults:` 加一行 `container_memory: "disable"`，`container_memory` 那一列消失，其餘七列不變（同樣是預期輸出；取自等價的樹，把這一行寫在包住所有租戶的一層子目錄）。同一行在今天的讀取端是讀不了的值，da-guard 以 `root_default_refused` 報錯、結束碼 1。

## 後果與已知限制

- **省略一個鍵的意思變了。** 既有的樹升級後，每個租戶最多多出 7 條基底列，另加因為基底現在有值而送出的列：租戶寫的 `_critical`，以及下一條說的過期 `disable`。在本 repo 的樹上量到的幅度：

  | 樹 | 租戶數 | 列數 前 → 後 |
  |:--|:--|:--|
  | repo 的開發範例樹（`components/threshold-exporter/config/conf.d`） | 5 | 95 → 95 |
  | `try-local/seed/conf.d` | 2 | 9 → 19 |
  | init，選 `mariadb,kubernetes` | 1 | 10 → 12 |
  | init，選全部規則包 | 2 | 98 → 98 |
  | init，只選 `redis` | 2 | 8 → 22 |
  | 上方範例（未退出） | 1 | 1 → 8 |

- **過期的 `disable` 開始失效。** 根層沒有值的鍵，`"disable"` 的 `expires:` 現行不看，等於永久關閉（ADR-038 決定 8）。內建層給值之後，這些鍵比照根層有值的鍵：`disable` 過期即失效，基底列與它的維度列一起回來。
- **全平台退出會讓租戶寫的同族鍵安靜地失效。** 根層寫了 `"disable"` 之後，租戶檔裡既有的 `<鍵>_critical` 與維度鍵不再送出，da-guard 只報 `disable_cascade` 警告、不擋。關掉整族正是退出的意思，但 init 寫進每個租戶檔的 `_critical` 建議值會因此全部失效，要自己看警告。
- **平台改值會在客戶沒有 diff 的情況下改變告警。** 這是本決策的目的，也是它的代價。護欄是決策 5 的 BREAKING 條目與決策 8 的升級前比對；不接受的客戶把數字寫進根層，就釘住了。
- **元件之間的落差只能偵測，不能消除。** 決策 5 表裡的四種落差都不會報錯；meta 告警要有部署平台規則包才有，遷移指令的閘門只擋它自己的刪除。
- **回退與混合版本有靜默失效的窗口**（決策 9 第 4 步）。舊版讀取端無從得知樹已經改成只放覆寫，這一段只能靠升級順序避免。
- **到達列數上限的租戶會少掉自己寫的列。** 多出的列計入 `max_metrics_per_tenant`（預設 500）。超過上限時 exporter 截斷，先被截掉的是租戶寫的維度列，不是新增的基底列。da-guard 既有的列數檢查以 `cardinality_exceeded` 指名這些租戶、結束碼 1，所以升級前比對（決策 9 第 1 步）會看到；沒做比對就升級的部署，只會在 exporter 的日誌看到截斷訊息。
- **不涵蓋的複製品。** 根層的 `optional_overrides:` 與 `dimensions:` 兩份清單、init 寫進租戶檔的 `_critical` 建議值、init 寫的 `operator` 鍵，仍然是 init 當天的複製品。前兩份過期時，租戶寫的鍵沒有列、da-guard 報錯，不是靜默，所以不在本決策處理。`operator` 鍵見決策 7。
- **沒有量到的事**：多出的列實際會變成幾條告警（取決於客戶的指標與已部署的規則包）、多少客戶用了 Helm 的 `null` 或手改過預設值、大量租戶下多出的列對 exporter 的成本、根層 `"disable"` 讓「這個 `disable` 有沒有作用」的重算（ADR-038 決定 8）多花多少時間。

## 考慮過的替代方案

- **維持複製，只靠檢查點名**：訊號只給有跑檢查的部署，而且每次平台新增預設啟用鍵，每個客戶都要手動補一次。檢查留下來當升級訊號，不當解法。
- **讓 init 重跑來更新**：`init --force` 會重寫它產生的每一個檔、丟掉所有手改。
- **登錄表隨規則包出貨、exporter 在執行期讀檔**：值與讀它的規則永遠同版，版本落差的問題消失。但 CI 裡的 da-guard 與 tenant-api 手上沒有那個檔，三個讀取端的答案會不同，違反 ADR-036「同一棵樹只有一個答案」；exporter 也多出「檔不在、檔讀不了」兩種狀態。再議的條件：版本落差的 meta 告警在實際升級中反覆觸發，或出現無法讓 exporter 與規則包同步升級的客戶。
- **只在組裝 ConfigMap 時注入預設值**：走 `existingConfigMap` 的部署與 tenant-api 的 Git 工作目錄拿不到，讀取端之間不一致。
- **用 `optional_overrides:` 的列名當退出**：不新增語法，但借用了「宣告但不給值」的欄位，有兩個問題。退出之後的鍵照那個欄位的規則走：租戶把基底開回來時 `<鍵>_critical` 不送、profile 給的值不套用。而且 init 把這份清單複製進客戶的樹，日後平台把清單上的鍵升為預設啟用時，既有的樹會把它讀成已退出，新的預設值進不來，也沒有錯誤。
- **新增一份只放退出的清單（例如 `disabled_defaults:`）**：與 kube-prometheus-stack 的 `defaultRules.disabled` 最像。但它的效果要定義成「每個租戶都繼承 `disable`」才沒有上一項的問題，那就只是同一件事的第二種寫法，還多一個頂層鍵。在同一個鍵上寫「關」也有先例：ESLint 的 `"off"`、kube-prometheus-stack 的 `defaultRules.rules.<組>: false`。
- **把登錄表的宣告清單與維度宣告也內建**：init 就不必複製 `optional_overrides:` 與 `dimensions:`。但客戶今天靠自己的 `dimensions:` 收窄可用的維度、靠 `optional_overrides:` 限制租戶能寫的鍵，內建之後與客戶清單的合併規則沒有不犧牲其中一邊的答案；也有多支工具直接從樹讀這兩段。
- **Helm chart 的 `thresholdConfig.defaults` 改成空的**：理由見決策 6。
- **讓 `null` 代表退出**：理由見決策 4。
- **內建層只對「有部署該規則包」的租戶生效**：exporter 不知道部署了哪些規則包，只能從樹的內容推論；推論有已知的誤判，可以用在提示，不能用來決定送不送值。
- **升級時自動刪除複製品**：理由見決策 7。

## 實作順序

每一步都要有「把修法還原就失敗」的驗收。

| 步 | 內容 | 驗收 |
|:--|:--|:--|
| 1 | 本 ADR 核可 | — |
| 2 | 執行期訊號與 `platform_defaults` 列的規格依決策 8 更新 | — |
| 3 | 讀取端的內建層：生成表、合併鏈最底層、`key_sources`、鍵判決 | 沒有根層檔的樹、與根層 `defaults:` 是空的樹，都送出 7 列、來源為 `builtin`；拿掉內建層就少 7 列；登錄表改了沒重新生成時檢查失敗；內建表有差異而沒有 Breaking 片段時檢查失敗。對照：租戶覆寫勝出、租戶 `"disable"` 沒有列、critical 送出、根層讀不了時沒有列。根層 `"disable"`：每個租戶整族沒有列，租戶給值後整族回來；`false` 被拒收、列照送；預設啟用鍵只列名而沒有數字時 da-guard 結束碼 1 |
| 4 | Helm chart | `helm template` 的輸出與改動前相同；`--set thresholdConfig.defaults.<鍵>=disable` 之後該鍵沒有列 |
| 5 | init | 產物的 `served-values` 中，預設啟用鍵的列與改動前相同 |
| 6 | 遷移指令與 da-guard 的提示 | 值不同的鍵保留；值相同的 `operator` 鍵保留；摘要不同時拒絕寫入；根層 `null` 與列名改寫成 `"disable"` 後，前後送出的列相同 |
| 7 | 重新生成範例樹與文件，寫發布說明 | — |

## 相關

- [ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載](017-defaults-yaml-inheritance-dual-hash.md)：本決策在它的合併鏈底下加一層。對預設啟用鍵，它改變三句話的結果：決策 4 的根層 `null`（改為落到內建值）、決策 4 的根層 `"disable"`（對內建層給值的鍵改為合法，關掉整族），以及決策 3「子目錄只能改根目錄已宣告的閾值」（內建層給值的鍵算已宣告）。
- [ADR-038: 沒有讀取端讀的鍵以實測判定](038-unread-keys-judged-by-readers.md)：租戶寫預設啟用鍵不再判為沒有讀取端讀（決定 4）；沒有根層檔的樹不再是「無從判斷」（決定 7）；`"disable"` 的規則不變，但適用「根層有值」那一支的鍵變多，根層也成為可以寫它的一層（決定 8）。
- [ADR-036: conf.d 只解析一次](036-single-parser-effective-config.md)：升級前比對（§8）的做法；§6 的版本落差在本決策多出一種放行的方向（決策 5）。
- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](001-severity-dedup-via-inhibit.md)：它產生的抑制規則形狀正在修訂（[#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)），同樣會在客戶沒改設定的情況下改變行為。兩者同版出貨時，決策 9 第 1 步的升級前比對一併涵蓋，客戶只遷移一次。
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)：本決策的討論與實作追蹤。
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831)：執行期訊號（決策 8）。
- [#3924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3924)：`operator` 鍵的啟用政策收斂到登錄表（決策 7）。
