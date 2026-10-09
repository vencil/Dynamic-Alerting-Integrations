---
title: "ADR-036: 路由與 domain policy 設定只由產生器解析一次"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-036
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-10-09
updated_at: 2026-10-09
---

# ADR-036: 路由與 domain policy 設定只由產生器解析一次

> **Language / 語言：** **中文 (Current)** | [English](./036-single-parser-effective-config.en.md)

## 狀態

✅ **Accepted**（2026-10-09 起草，2026-10-09 由 owner 核可）。

- 決策內容已由 owner 在 [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) 與 [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486) 拍板。
- 設計與本文各經過一輪外部對抗審查（由不同模型擔任），核實後的意見已併入。
- owner 已核可本文。

## 摘要

**問題**：同一份 conf.d 會被讀兩次：路由產生器（Python）讀一次，da-guard 與 tenant-api（兩者都是 Go，以下合稱「Go 端」）再用另一個 YAML 解析器讀一次。兩邊的讀法有落差，Go 端有時會放行產生器會擋下的設定。Go 端一直靠手寫程式模擬 Python 的讀法來對齊，修了多輪仍然對不齊。

**決定**：

1. 路由、domain policy 與租戶清單只由**產生器**解析；閾值仍由 exporter 解析。
2. tenant-api 的 pod 加一個容器，跑真正的產生器程式。它輸出整理好的設定（JSON），並負責驗證每一筆寫入。tenant-api 不再自己解析這些 YAML。
3. 分兩階段切換：
   - 第一階段，新舊兩邊並行，任一邊拒收就拒收。
   - 兩邊的差異都查清、Go 端不再有任何比產生器寬鬆的地方，而且線上並行跑滿至少 14 天、有實際比對量之後，進入第二階段，刪掉 Go 端的模擬程式。完整條件見「何時進入第二階段」。
4. da-guard 不再判斷路由與 policy，改由產生器的 `--validate --strict` 判斷。

**對使用者與維運的影響**：
- tenant-api 會多一個容器（da-tools 映像，壓縮後約 37 MB）。
- 這個容器無法使用時，涉及路由或 policy 的寫入會回 503，不會放行。
- 只跑 da-guard 的 CI 從此不涵蓋路由與 policy，要另外加一步 `da-tools generate-routes --validate --strict`。這是 breaking change。

**不可協商的驗收條件**：任何讀取端都不得比產生器寬鬆。也就是說，不能讀出產生器丟掉的 policy，也不能漏掉產生器會執行的 policy。
- 第二階段起，對寫入與讀取都成立。
- 第一階段只保證寫入；讀取仍有已知落差，直到第二階段（見「分兩階段切換」）。

## 問題

### 一個例子

YAML 有一種「把另一個 mapping 併進來」的寫法，常見的是 `<<:`。產生器使用的 Python 解析器（PyYAML）把任何標上 `!!merge` 的鍵都當成這種合併；Go 端的解析器只認字面上的 `<<`。下面這份租戶檔就踩在這個落差上：

```yaml
# t1.yaml
tenants:
  t1:
    _routing:
      !!merge q: {receiver: {type: slack, api_url: "https://hooks.slack.com/x"}}
```

- **產生器**照 merge 處理，t1 的 receiver 是 `slack`。如果 domain policy 禁止 slack，產生器會報錯，exit code 為 1。
- **da-guard** 把 `q` 當成普通的鍵，receiver 改取平台預設值。它判定設定合規，exit code 為 0。

同一份檔，兩個工具給出相反的結論，而放行的是 Go 這一邊。

### 不只一個例子

2026-10-09 在 main 上量到的情況：

| 寫法 | 產生器 | Go 讀取端 |
|---|---|---|
| 租戶 `_routing` 用 `!!merge` 標記的鍵 | merge 進來的 receiver 違反 policy，擋下 | 當成普通鍵，放行 |
| policy 檔巢狀 600 層 | 整份不可用 | 讀出並執行 policy |
| UTF-8 BOM 剛好跨過檔案第 512 byte，而且在註解裡 | 照常執行 policy | 靜默地讀不到任何 policy |

完整清單見 [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)。

### 為什麼補不完

Go 端的對齊做法，是把 Python 解析器的每一條處理路徑在 Go 裡重寫一次：merge 怎麼展開、空的鍵怎麼處理、有序 mapping 怎麼建。每補一條，旁邊就露出另一條。處理順序本身也會影響結果。[#2763](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2763) 修了四輪，每一輪都帶出新的落差。差分 fuzz 找得到落差，但證明不了兩邊相等。

問題的根源不在某一條規則，而在「兩個解析器各讀一次，又要求結果一模一樣」。

## 決定

### 1. 誰解析什麼

| 設定 | 唯一的解析者 | 其他工具怎麼取得 |
|---|---|---|
| 路由（`_routing_defaults`、`routing_profiles`、租戶的 `_routing`）、domain policy、租戶清單 | 路由產生器（Python） | 讀產生器輸出的 JSON |
| 閾值與 exporter 實際提供的值 | exporter（Go） | 不變：Python 工具本來就透過 `da-guard served-values`／`effective` 的 JSON 讀取 |

第二列是現成的先例：閾值早就是「一個解析者，其他工具讀它的輸出」。本 ADR 把同樣的做法套到路由與 policy。

### 2. 運作方式

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api（Go）              │ ───▶ │ 產生器容器（Python）          │
│ - 收到寫入請求                │ 驗證 │ - 依指定的 commit 取出檔案    │
│ - 依驗證結果寫入或拒收        │ ◀─── │ - 用真正的產生器判定          │
│ - 讀取時讀整理好的 JSON       │ 結果 │ - 輸出整理好的設定 JSON       │
└──────────────────────────────┘      └──────────────────────────────┘
          兩者只透過共用目錄裡的 Unix socket 溝通
```

- 產生器容器使用現成的 da-tools 映像，裡面已有產生器。
- **溝通方式**：不開網路埠，只用一個放在共用目錄（emptyDir）的 Unix socket。只有掛載了該目錄、且符合 socket 檔案權限的程序能連線。
- **取檔方式**：一律依指定的 commit，從 git 物件取出檔案（`git ls-tree` 加 `git cat-file`），不讀共用的工作目錄。原因有二：
  - tenant-api 的 PR 模式（寫入時開 PR，而不是直接 commit）會在工作目錄裡就地切換分支，直接讀會讀到切換中的狀態。
  - `git archive` 會套用 `.gitattributes` 的匯出規則。客戶的 repo 若把 policy 檔標成不匯出，產生器就會看不到它。
- **快取**：取出的檔案依 commit 快取；解析結果依「檔案內容的 hash 加產生器版本」逐檔快取。

### 3. 寫入怎麼驗證

**判準是絕對的**，和現在 tenant-api 的做法相同。容器把 base 樹裡要寫的那個檔換成新內容，再跑產生器。以下任一情況就拒收：

- 有任何違規，主體是這次寫入的租戶或檔案。
- 出現 base 原本沒有的整棵樹層級錯誤，例如某個檔讀不了、租戶重複。

**為什麼不比對「新增了哪些錯誤訊息」**：已經違規的租戶改寫設定時，會產生和 base 一模一樣的那行訊息，比對差異會把它放行。另外，檔案讀不了時產生器印的是另一種格式的訊息，用字串比對會漏掉。

因此**第一步先讓產生器輸出結構化的 finding**。finding 指產生器回報的單筆問題，欄位有 policy、租戶、檔案、種類、是否擋下。有了它，判準就不必靠比對字串。

**其他規則**：

- **base 本身已有錯誤時**：只要這次寫入的主體沒有問題、也沒有新增整棵樹層級的錯誤，就允許寫入。不讓一個壞掉的鄰居檔擋下所有人。
- **哪些寫入要送驗**：凡是會改變檔案內容的寫入都送驗。是否送驗不由 Go 判斷「有沒有改到路由」。
- **驗的和寫的必須是同一份**：容器回傳它驗過的「檔案路徑、內容 hash、base commit」。tenant-api 在寫入鎖之下核對三者，不一致就重驗；寫入的位元組必須和驗過的一模一樣。
- **批次寫入**：一次請求帶整批檔案，同時驗最終狀態和每一個中間狀態。
  - 目前批次 patch 由 Go 讀出檔案再重新輸出，Go 的讀法會被寫進檔案。例如上面的 `!!merge q:` 會被改寫成普通鍵 `q:`。
  - 第一階段仍由 Go 合併，但合併後的內容一定送驗，所以違規的結果會被擋下。擋不住的是「沒有違規、但意思被悄悄改掉」的改寫，這是第一階段的另一個明示例外。
  - 第二階段起，批次合併改由產生器容器執行。
- **任何異常都拒收**：容器出錯、逾時、輸出不完整，都當成拒收。
- **錯誤回應**：只回這次寫入主體的 finding，不回其他租戶的違規內容。

### 4. 整理好的設定（JSON）

內容包括：每份 policy 檔是否可用、各 domain 的租戶與限制、各租戶合併後的路由、租戶與檔案的對應。

| 規則 | 理由 |
|---|---|
| 外層帶格式版本、最低讀取端版本、產生器版本、對應的 commit | 讀取端能判斷自己看不看得懂、讀的是不是最新 |
| 看不懂的限制種類，該 policy 一律當成不可用 | 忽略看不懂的限制，等於少擋 |
| 不允許重複的鍵、無效的 UTF-8、未知欄位 | 避免 JSON 這一層再出現兩種讀法 |
| 時長與閾值一律以字串輸出 | 避免數字精度在兩種語言間不一致 |
| 先寫暫存檔再改名 | 讀取端不會讀到寫一半的檔 |

讀取時若最新一次產生失敗，可以沿用上一份成功的結果，但有三個限制：
- 只用在讀取，寫入永遠即時驗證。
- 以檔案為單位沿用；新版已刪除的 policy 檔不得沿用。
- 超過時限就當成不可用並告警。

### 5. 版本

客戶的 CI 用哪一版 da-tools，與 tenant-api 無關，所以「和客戶 CI 完全一致」做不到。能保證的是：**tenant-api 不比它容器裡的產生器寬鬆**。

兩邊版本不同時，任一邊拒收，設定都進不了 Alertmanager：
- 客戶 CI 的產生器比較嚴格時，tenant-api 接受的寫入會在 CI 被擋下。結果是晚一步擋下，不會放行。
- tenant-api 容器裡的產生器比較嚴格時，tenant-api 會拒收 CI 原本會接受的寫入。

兩種情況都不會放行違規的設定，代價是兩邊的判定不一致。縮小版本落差的做法：

- tenant-api 的 chart 預設固定一個 da-tools 版本，CI 對這一組版本跑相容性測試。
- `da-tools init` 產生的設定改成固定版本，不再用 `latest`。
- policy 檔可以宣告「至少需要哪一版產生器」，容器比它舊就拒收寫入。這是新增的鍵，只由產生器讀取。
- tenant-api 以 metric 公開產生器版本。

### 6. 分兩階段切換

| | 寫入怎麼判 | 產生器容器掛掉時 | 讀取 |
|---|---|---|---|
| **第一階段（並行）** | Go 與產生器任一邊拒收就拒收 | 涉及路由或 policy 的寫入回 503 | 沿用 Go 的讀法，同時記錄兩邊的差異 |
| **第二階段（只用產生器）** | 只看產生器；刪掉 Go 端的模擬程式 | 寫入回 503 | 讀整理好的 JSON |

- **容器掛掉時不退回只用 Go**：
  - 能送寫入請求的使用者，可以影響容器是否可用，例如連續送出大量需要整棵樹重算的請求。
  - Go 單獨判定有已知的寬鬆漏洞。
  - 退回只用 Go，等於讓請求方自己挑選比較寬鬆的判定者。
- **第一階段的讀取是驗收條件的明示例外**：讀取仍有 #2759 列的落差，直到第二階段。寫入從第一階段起就必須符合驗收條件。
- tenant-api 現有一個放行開關 `--policy-unavailable-open`：policy 檔讀不了時，仍允許寫入。它維持原意，不涵蓋容器掛掉的情況；容器掛掉沒有放行開關。
- `tenant_api_policy_available`（「policy 是否可用」的 metric）有告警在讀，維持原名與原 label，代表兩邊合併後的結果。兩邊各自的數值另開新 metric。

**何時進入第二階段**：看條件，不看天數。三條都成立才進入：

1. CI 中，固定語料加隨機語料的雙向比對（扣掉差異清單已列的項目）連續 14 天全綠。隨機語料的種子每天更換，失敗時記錄種子。
2. 線上並行至少 14 天，期間跨過一次 da-tools 的版本發布（`tools/v*` 標籤），而且有實際比對量：
   - 比對次數達到下限。具體數字在實作 PR 中訂定，並寫回本文。
   - 每天送一筆合成的測試寫入，走完整流程。
   - 只看天數的話，零流量也會成立，所以兩者都要。
3. 每一筆差異都列在**差異清單**裡（見下）。

**差異清單**沿用 repo 既有的 `tests/rulepacks/vm_deviation_catalog.yaml` 格式：

- 每筆記錄：範例檔、原因、方向、處置、追蹤票、到期日。
- **方向由測試計算**：比較兩邊的判定後得出，再與清單上的值比對，標錯就紅。不靠人手寫。
- 雙向把關：沒列進清單的差異會紅；已列入、但實際已消失的差異也會紅。
- **Go 比產生器寬鬆的差異一律擋住第二階段**。#2759 目前列的寬鬆類都屬於這種，全部修好之前不能進第二階段。
- Go 比產生器嚴格的差異，要在追蹤票留下「以產生器為準」的明確簽核。進入第二階段後，這些寫入會改成被接受。
- 清單的任何修改都經 PR，由 owner 核准。

客戶的 conf.d 無法取得，上面的條件只涵蓋 owner 的樹。另外提供一個 `da-tools` 指令，讓客戶在自己的樹上跑同一套比對。

### 7. da-guard

da-guard 不再判斷路由與 policy，交給產生器的 `--validate --strict`。閾值相關的檢查不變。

- **單獨執行 da-guard 時**（例如使用沒有 Python 的獨立 binary）：
  - 報告（文字與 JSON）明示路由與 policy「未判定」，並回一個專用的 exit code。這樣「沒判」和「判過、沒問題」可以分得開。exit code 的數值在實作時訂定，寫進 CLI 文件。
  - 遷移方式：在 CI 原本跑 da-guard 的地方，加一步 `da-tools generate-routes --validate --strict`。
  - `--required-fields _routing.*` 會直接報錯，不會悄悄失效。
- **刪掉 Go 端檢查之前**，先把只有 Go 有的檢查搬到產生器，加進兩邊的對照表，並證明拿掉後測試會紅。「只有 Go 有」的清單由程式計算，不手動維護。原本只是警告、搬過去後改成錯誤的檢查，要在 changelog 註明「變嚴格」。
- `guard-defaults-impact.yml` 加跑產生器。
- 第一階段保留 da-guard 的 Go 檢查，CI 兩邊都跑；第二階段刪除。

### 8. tenant-api 的讀取端點

- 目前沒有任何 GET 端點回傳產生器渲染的路由，因此沒有東西要遷移。
- 等到有人需要渲染後的路由，才新增 `GET /tenants/{id}/routing`，內容一律來自整理好的 JSON。
- `/effective` 回應裡的 `_routing` 是 exporter 合併出來的原始鍵，不是渲染後的路由。API 說明會註明這一點。

### 9. 第二階段刪除哪些程式

依「還有沒有人在用」決定。**現在就**加一個 CI 檢查：列出相關套件的使用者，和下表不一致就紅。

| 項目 | 處理 |
|---|---|
| Go 端模擬 Python 讀法的程式 | 刪除 |
| vendored YAML 解析器裡只為 policy 讀取加的修改 | 跟著刪除 |
| vendored YAML 解析器裡 exporter 也用到的效能修改 | 保留 |
| 與解析無關的小工具（例如租戶 id 檢查） | 保留或移到別的套件 |

已驗證：用一份含重複鍵的租戶檔實測，產生器回報 `DuplicateKeyError` 並拒收。所以刪掉 Go 端的重複鍵檢查不會變寬鬆。

## 失敗時會怎樣

| 情況 | 行為 |
|---|---|
| 產生器容器掛掉或逾時 | 涉及路由或 policy 的寫入回 503；讀取沿用上一份成功結果，直到超過時限 |
| 產生 JSON 失敗（例如 policy 檔裡有無效字元） | 保留上一份成功結果供讀取；寫入照常即時驗證 |
| 讀取端遇到不認得的格式版本 | 整份當成不可用 |
| 容器裡的產生器比 policy 要求的舊 | 拒收寫入 |
| base 樹已有錯誤 | 主體乾淨的寫入照常通過 |

## 怎麼驗收

- **隨機語料比對**：隨機產生 conf.d → 產生器輸出 JSON 與 finding → 同一份樹交給 Go 讀取端 → 斷言兩邊執行的 policy 與租戶**完全相同**，多讀、漏讀都算失敗。JSON 解析只能「完整成功」或「整份拒收」。比對程式本身不解析 YAML。
  - 第二階段起，寫入與讀取都要相同。
  - 第一階段只斷言寫入：並行判定一定擋下產生器會擋的寫入。
- **固定語料**：加上現有的 `merge_key_policy_corpus.json`，以及以下各票的範例：
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)：policy 檔的剩餘落差清單。
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)：租戶檔的 `!!merge` 鍵。
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)：被覆蓋的合併來源裡有無法解析的值。
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674)：自我參照的 YAML 別名。
- 在 CI 裡，以 chart 固定的那一組版本執行。

## 成本（2026-10-09 實測）

| 項目 | 量測 |
|---|---|
| 產生器處理 1000 個租戶 | 1.72 秒、38 MB；其中解析佔 1.06 秒，而且每個檔被解析兩次（可用快取省下） |
| 解析單一檔案 | 0.47 ms |
| Python 啟動 | 0.11 秒、25 MB |
| 從 git 取出 1000 個檔 | 0.21 秒、12 MB |
| 映像大小（壓縮後，amd64） | da-tools 36.9 MB；tenant-api 14.0 MB |

快取命中時，一筆寫入的主要成本是解析單一檔案加上檢查。檢查加輸出約 0.6 秒，這是推估值，沒有單獨量測。

## 考慮過但否決

正文已說明理由的方案（繼續在 Go 端模擬、比對錯誤訊息差異、用 `git archive` 取檔、容器掛掉時退回 Go、寫入沿用上一份結果）不再重列。

| 方案 | 否決理由 |
|---|---|
| 把整理好的 policy 另存一份檔案 commit 進 git | 只涵蓋 policy，路由仍由 Go 解析。忘了重產時，tenant-api 剛啟動會因讀不到而擋下所有寫入；已在運作的則會沿用舊版 policy |
| 三方共用一套自訂的設定語法 | YAML 舊版規則會把 `no`、`0777` 這類真實租戶 id 讀成布林值或數字；而且是 breaking change |
| 產生器改用 C 實作的解析器 | 只對齊了把文字切成記號的那一層；落差都出在把記號組成資料的那一層 |
| 把 Python 解析器完整移植到 Go（約四、五千行） | 只在加容器的成本無法接受時才考慮 |
| 寫入 API 只收 JSON | breaking change；Go 仍要把 JSON 轉成 YAML 給產生器讀，只是方向反過來 |
| da-guard 自己呼叫產生器 | 執行時要有 Python；沒有 Python 的獨立 binary 就不能用 |

## 實作順序

1. 產生器輸出結構化的 finding。
2. CI 檢查：第二階段刪除範圍的使用者比對，以及差異清單與它的雙向測試。
3. 產生器容器：輸出 JSON、提供驗證端點；Helm chart 加容器、socket 與資源設定。
4. tenant-api：寫入改走容器驗證（第一階段並行），讀取改讀 JSON。
5. da-guard：搬移只有 Go 有的檢查、加上「未判定」狀態與 exit code；`guard-defaults-impact.yml` 加跑產生器。
6. 第二階段：刪除 Go 端的模擬程式，在 main 上重測 #2759、#2700、#2713、#2674 後關票。

## 附錄：實作對照

給實作者的程式碼位置，讀決策時可略過。

| 本文用語 | 程式碼 |
|---|---|
| Go 端模擬 Python 讀法的程式 | `pkg/routingpolicy` 的 `shapeWork`、`pyResolve`、`ParseDomainPolicies`、`UnmarshalPolicy` |
| tenant-api 現在的寫入判定 | `judgePutBody`；PR 模式在 `WritePRChecked` 的閉包內重判 |
| 批次 patch 由 Go 重新輸出 | `mergePatchYAML` |
| 「讀不了的檔當成空」的舊行為（將刪除） | `tenantBlockOnDisk` |
| vendored YAML 解析器的修改 | `third_party/yaml.v3`：`SpacesOnly`（只給 policy，刪除）、nonspecific-tag（沒有使用者時刪除）、`uniquekeys`（exporter 使用，保留）；釘值測試 `tests/ops/test_vendored_yaml_v3.py` 同一支 PR 更新 |
| 只有 Go 有的 da-guard 檢查 | `internal/guard/types.go` 減去 `tests/shared/routing_policy_parity_matrix.json` |
| 現有的「已知較嚴」標記（併入差異清單） | `merge_key_policy_corpus.json` 的 `known: stricter` |

## 參考

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)：設計討論、量測與外部審查紀錄；[#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)：追蹤票
- [ADR-023 tenant-api 寫入平面：單一寫者不變式](./023-write-plane-single-writer-invariant.md)
- 業界做法：
  - 只解析一次、驗證交給權威端：[Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/)。
  - 把解析工具放進使用者的 pod：[Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/)。
  - 新舊並行比對、依條件切換：[GitHub Scientist](https://github.blog/developer-skills/application-development/scientist/)。
  - 驗證端失效時拒絕而不放行：[Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/)。
