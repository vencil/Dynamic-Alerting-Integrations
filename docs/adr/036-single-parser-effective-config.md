---
title: "ADR-036: conf.d 只解析一次：YAML 由 Go 讀，路由與 policy 由產生器判"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-036
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-10-09
updated_at: 2026-10-11
---

# ADR-036: conf.d 只解析一次：YAML 由 Go 讀，路由與 policy 由產生器判

> **Language / 語言：** **中文 (Current)** | [English](./036-single-parser-effective-config.en.md)

## 狀態

✅ **Accepted**（2026-10-09 起草並由 owner 核可）。

- **2026-10-11 修訂**：YAML 語法層也只留一個解析器。原版讓路由產生器（Python）自己讀 YAML，Go 端再讀一次，兩個 YAML 解析器的差異仍要逐條對齊；修訂後所有工具讀的都是 Go 解析出的同一份 JSON，產生器只負責路由與 policy 的語意。方向由 owner 在 [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858) 核可，本文經 PR 審閱。
- 設計與原版本文各經過一輪外部對抗審查（由不同模型擔任）；修訂另經一輪內部對抗審查（換模型）。

## 摘要

**問題**：同一份 conf.d 被兩個 YAML 解析器各讀一次：Go 端（exporter、tenant-api、da-guard）用 yaml.v3，Python 端（路由產生器與各工具）用 PyYAML。兩者對同一份檔常讀出不同結果，雙方都寫了大量程式去模仿對方，修了多輪仍對不齊。

**決定**：

1. **YAML 只由 Go 解析。** da-guard 新增一個指令，把整棵 conf.d 讀成一份 JSON：選了哪些檔、每份檔的內容（鍵一律是原文，純量同時帶原文與型別）、哪些檔讀不了。Python 端（包含路由產生器）不再自己解析 conf.d 的 YAML，改讀這份 JSON。
2. **語意各只有一個實作。** 路由、domain policy、租戶清單的語意只由產生器（Python）判定；閾值與 exporter 送出的值只由 exporter（Go）判定。
3. tenant-api 的 pod 加一個容器跑產生器，負責驗證每一筆涉及路由或 policy 的寫入。tenant-api 不再自己判定這些語意。
4. 分兩階段切換：第一階段新舊並行、任一邊拒收就拒收；條件達成後進入第二階段，刪掉 Go 端模仿產生器語意的程式。
5. da-guard 不再判斷路由與 policy，改由產生器判斷。

**對使用者與維運的影響**（都是 breaking change）：

- 依舊版 YAML 規則成立的寫法，意思可能改變。例如路由裡的 `send_resolved: yes`，產生器過去讀成布林 `true`，改由 yaml.v3 解析後是字串 `"yes"`。提供一次性的遷移稽核指令列出所有受影響的值。
- 讀 conf.d 的 Python 工具需要 da-guard。da-tools 映像已內附；本機的 pre-commit 會以 Go 現場建置。
- tenant-api 多一個容器（da-tools 映像）；容器無法使用時，涉及路由或 policy 的寫入回 503，不會放行。
- 只跑 da-guard 的 CI 不再涵蓋路由與 policy，要另外加一步 `da-tools generate-routes --validate --strict`。

**不可協商的驗收條件**：任何讀取端都不得比產生器寬鬆。也就是說，不能讀出產生器丟掉的 policy，也不能漏掉產生器會執行的 policy。第二階段起對寫入與讀取都成立；第一階段只保證寫入。

## 問題

### 一個例子

YAML 有一種「把另一個 mapping 併進來」的寫法，常見的是 `<<:`。PyYAML 把任何標上 `!!merge` 的鍵都當成這種合併；yaml.v3 只認字面上的 `<<`。下面這份租戶檔就踩在這個落差上：

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

| 寫法 | PyYAML（產生器） | yaml.v3（Go 端） |
|---|---|---|
| 租戶 `_routing` 用 `!!merge` 標記的鍵 | 合併進來 | 普通的鍵 |
| `send_resolved: yes`、`on`、`off` | 布林 | 字串 |
| `a: "x\/y"`（引號字串裡的 `\/`） | `x/y` | 整份讀不了 |
| `a:` 後面接 TAB 再接值 | 整份讀不了 | 正常讀出 |
| policy 檔巢狀 600 層 | 整份不可用 | 讀出並執行 policy |
| UTF-8 BOM 剛好跨過檔案第 512 byte，而且在註解裡 | 照常執行 policy | 靜默地讀不到任何 policy |

policy 檔的完整清單見 [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)。

### 為什麼補不完

對齊的做法，是讓一邊在自己的語言裡重寫另一邊解析器的每一條處理路徑：Go 端有一個專門模仿 PyYAML 的套件，vendored 的 yaml.v3 也打了兩段只為了模仿 PyYAML 的 patch；Python 端則有多個模仿 yaml.v3 的 loader（例如把 mapping 的鍵照原文讀成字串）。每補一條，旁邊就露出另一條，處理順序本身也會影響結果。差分 fuzz 找得到落差，但證明不了兩邊相等。

原版本文只處理了「語意」這一層：路由與 policy 的判定只在產生器做。但產生器自己仍用 PyYAML 讀 YAML，Go 端仍用 yaml.v3 讀，所以上表前四列這類語法層的落差照樣存在，只是換成「產生器容器與 tenant-api 的判定不同」。

也試過另一條路：把 conf.d 限縮到「兩個解析器必然讀出相同結果」的寫法。實測不可行，上表第三、四列的值都已加上引號、也沒用到任何進階功能，兩邊仍然讀出不同結果。這種「相同的子集」只能抽樣驗證，無法定義。

問題的根源是「兩個解析器各讀一次，又要求結果一模一樣」。唯一不需要證明兩邊相等的做法，是只留一個解析器。

## 決定

### 1. 誰解析什麼

| 層次 | 唯一的實作 | 其他工具怎麼取得 |
|---|---|---|
| YAML 語法：選哪些檔、每份檔的內容 | Go（da-guard，yaml.v3） | 讀 da-guard 輸出的「解析結果 JSON」 |
| 路由（`_routing_defaults`、`routing_profiles`、租戶的 `_routing`）、domain policy、租戶清單的語意 | 路由產生器（Python） | 讀產生器輸出的「整理好的設定 JSON」與 finding |
| 閾值與 exporter 實際提供的值 | exporter（Go） | 不變：讀 `da-guard served-values`／`effective` 的 JSON |

第三列是現成的先例：閾值早就是「一個實作，其他工具讀它的輸出」。本 ADR 把同樣的做法套到 YAML 語法層與路由、policy 語意。

### 2. 解析結果 JSON

da-guard 新增子命令 `parse-tree`，輸入一個 conf.d 目錄，輸出一份 JSON：

- **選了哪些檔**：照 exporter 自己的檔案選擇規則（副檔名、大小寫、隱藏檔、子目錄）。Python 端不再有自己的一份規則。
- **每份檔的內容**：mapping 的**鍵一律輸出原文字串**（`010` 就是 `"010"`，與 exporter 讀租戶 id 的方式相同）；**純量同時輸出原文與 yaml.v3 判定的型別**，例如 `{"text": "yes", "tag": "!!str"}`。帶原文是因為 exporter 讀閾值時用的就是原文；帶型別是給需要布林或數字的語意層用。時間、無限大這類 JSON 無法直接表示的值，也只以原文加型別表示，不會失真。
- **讀不了的檔**：檔名、原因（語法錯誤、讀取失敗、不是一般檔），不輸出內容。
- 外層帶格式版本與 da-guard 版本。讀取端遇到不認得的格式版本，整份當成不可用。

範例。輸入：

```yaml
# t010.yaml
tenants:
  010:
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/x"
        send_resolved: yes
      group_wait: 30s
```

輸出（節錄；以 yaml.v3 的節點逐一轉成 JSON 實測）：

```json
{
  "t010.yaml": {
    "tenants": {
      "010": {
        "_routing": {
          "group_wait": {"text": "30s", "tag": "!!str"},
          "receiver": {
            "api_url": {"text": "https://hooks.slack.com/x", "tag": "!!str"},
            "send_resolved": {"text": "yes", "tag": "!!str"},
            "type": {"text": "slack", "tag": "!!str"}
          }
        }
      }
    }
  }
}
```

同一份檔用 PyYAML 讀，得到的是 `{'tenants': {8: {'_routing': {'receiver': {'type': 'slack', 'api_url': 'https://hooks.slack.com/x', 'send_resolved': True}, 'group_wait': '30s'}}}}`：租戶 id 變成整數 8，`yes` 變成布林。改讀 JSON 後，所有 Python 工具看到的都是 Go 端讀到的那一份。

**Python 端怎麼取得**：沿用現有的「Python 呼叫 Go binary、讀 JSON」機制（da-tools 已用這個方式呼叫 da-guard 的 `served-values`／`effective`）。共用的 YAML 讀取與檔案選擇函式庫改為讀這份 JSON；各工具不再直接呼叫 YAML 解析器讀 conf.d。會改寫檔案的工具可以保留自己的文字層編輯（為了保留註解與排版），但判斷依據一律來自 JSON，改寫後的結果也要再經 `parse-tree` 讀一次確認。

**範圍**：只涵蓋 conf.d。rule pack、Kubernetes manifest 等其他 YAML 不在本 ADR 範圍內。

### 3. 運作方式（tenant-api）

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api（Go）              │ ───▶ │ 產生器容器（da-tools 映像）   │
│ - 收到寫入請求                │ 驗證 │ - 依指定的 commit 取出檔案    │
│ - 依驗證結果寫入或拒收        │ ◀─── │ - da-guard parse-tree 解析    │
│ - 讀取時讀整理好的 JSON       │ 結果 │ - 產生器依解析結果判定語意    │
└──────────────────────────────┘      └──────────────────────────────┘
          兩者只透過共用目錄裡的 Unix socket 溝通
```

- 產生器容器使用現成的 da-tools 映像，裡面已有產生器與 da-guard。
- **溝通方式**：不開網路埠，只用一個放在共用目錄（emptyDir）的 Unix socket。只有掛載了該目錄、且符合 socket 檔案權限的程序能連線。
- **取檔方式**：一律依指定的 commit，從 git 物件取出檔案（`git ls-tree` 加 `git cat-file`），不讀共用的工作目錄。原因有二：
  - tenant-api 的 PR 模式（寫入時開 PR，而不是直接 commit）會在工作目錄裡就地切換分支，直接讀會讀到切換中的狀態。
  - `git archive` 會套用 `.gitattributes` 的匯出規則。客戶的 repo 若把 policy 檔標成不匯出，產生器就會看不到它。
- **快取**：取出的檔案依 commit 快取；解析結果依「檔案內容的 hash 加 da-guard 版本」逐檔快取；語意判定依「解析結果的 hash 加產生器版本」快取。

### 4. 寫入怎麼驗證

**判準是絕對的**，和現在 tenant-api 的做法相同。容器把 base 樹裡要寫的那個檔換成新內容，解析後再跑產生器。以下任一情況就拒收：

- 有任何違規，主體是這次寫入的租戶或檔案。
- 出現 base 原本沒有的整棵樹層級錯誤，例如某個檔讀不了、租戶重複。

**為什麼不比對「新增了哪些錯誤訊息」**：已經違規的租戶改寫設定時，會產生和 base 一模一樣的那行訊息，比對差異會把它放行。另外，檔案讀不了時產生器印的是另一種格式的訊息，用字串比對會漏掉。所以判準依據產生器輸出的結構化 finding：每筆問題帶 policy、租戶、檔案、種類、是否擋下。

**其他規則**：

- **base 本身已有錯誤時**：只要這次寫入的主體沒有問題、也沒有新增整棵樹層級的錯誤，就允許寫入。不讓一個壞掉的鄰居檔擋下所有人。
- **哪些寫入要送驗**：凡是會改變檔案內容的寫入都送驗。是否送驗不由 Go 判斷「有沒有改到路由」。
- **驗的和寫的必須是同一份**：容器回傳它驗過的「檔案路徑、內容 hash、base commit」。tenant-api 在寫入鎖之下核對三者，不一致就重驗；寫入的位元組必須和驗過的一模一樣。
- **批次寫入**：一次請求帶整批檔案，同時驗最終狀態和每一個中間狀態。目前批次 patch 由 Go 讀出檔案再重新輸出；因為 YAML 也只由 Go 解析，重新輸出不會再把「另一個解析器的讀法」寫進檔案，但仍可能改變排版與註解，所以合併後的內容一定送驗。第二階段起，批次合併改由產生器容器執行。
- **任何異常都拒收**：容器出錯、逾時、輸出不完整，都當成拒收。
- **錯誤回應**：只回這次寫入主體的 finding，不回其他租戶的違規內容。

### 5. 整理好的設定（JSON）

產生器輸出的語意結果，內容包括：每份 policy 檔是否可用、各 domain 的租戶與限制、各租戶合併後的路由、租戶與檔案的對應。

| 規則 | 理由 |
|---|---|
| 外層帶格式版本、最低讀取端版本、產生器版本、da-guard 版本、對應的 commit | 讀取端能判斷自己看不看得懂、讀的是不是最新 |
| 看不懂的限制種類，該 policy 一律當成不可用 | 忽略看不懂的限制，等於少擋 |
| 不允許重複的鍵、無效的 UTF-8、未知欄位 | 避免 JSON 這一層再出現兩種讀法 |
| 時長與閾值一律以字串輸出 | 避免數字精度在兩種語言間不一致 |
| 先寫暫存檔再改名 | 讀取端不會讀到寫一半的檔 |

讀取時若最新一次產生失敗，可以沿用上一份成功的結果，但有三個限制：
- 只用在讀取，寫入永遠即時驗證。
- 以檔案為單位沿用；新版已刪除的 policy 檔不得沿用。
- 超過時限就當成不可用並告警。

### 6. 版本

客戶的 CI 用哪一版 da-tools，與 tenant-api 無關，所以「和客戶 CI 完全一致」做不到。能保證的是：**tenant-api 不比它容器裡的產生器寬鬆**。da-guard 與產生器在同一個 da-tools 映像裡，兩者的版本一起決定。

兩邊版本不同時，任一邊拒收，設定都進不了 Alertmanager：
- 客戶 CI 的版本比較嚴格時，tenant-api 接受的寫入會在 CI 被擋下。結果是晚一步擋下，不會放行。
- tenant-api 容器裡的版本比較嚴格時，tenant-api 會拒收 CI 原本會接受的寫入。

兩種情況都不會放行違規的設定，代價是兩邊的判定不一致。縮小版本落差的做法：

- tenant-api 的 chart 預設固定一個 da-tools 版本，CI 對這一組版本跑相容性測試。
- `da-tools init` 產生的設定改成固定版本，不再用 `latest`。
- policy 檔可以宣告「至少需要哪一版產生器」，容器比它舊就拒收寫入。這是新增的鍵，只由產生器讀取。
- tenant-api 以 metric 公開產生器與 da-guard 的版本。

### 7. 分兩階段切換

| | 寫入怎麼判 | 產生器容器掛掉時 | 讀取 |
|---|---|---|---|
| **第一階段（並行）** | Go 與產生器任一邊拒收就拒收 | 涉及路由或 policy 的寫入回 503 | 沿用 Go 的讀法，同時記錄兩邊的差異 |
| **第二階段（只用產生器）** | 只看產生器；刪掉 Go 端模仿產生器的程式 | 寫入回 503 | 讀整理好的 JSON |

- **容器掛掉時不退回只用 Go**：
  - 能送寫入請求的使用者，可以影響容器是否可用，例如連續送出大量需要整棵樹重算的請求。
  - Go 單獨判定有已知的寬鬆漏洞。
  - 退回只用 Go，等於讓請求方自己挑選比較寬鬆的判定者。
- **第一階段的讀取是驗收條件的明示例外**：讀取仍有已知落差，直到第二階段。寫入從第一階段起就必須符合驗收條件。
- tenant-api 現有一個放行開關 `--policy-unavailable-open`：policy 檔讀不了時，仍允許寫入。它維持原意，不涵蓋容器掛掉的情況；容器掛掉沒有放行開關。
- `tenant_api_policy_available`（「policy 是否可用」的 metric）有告警在讀，維持原名與原 label，代表兩邊合併後的結果。兩邊各自的數值另開新 metric。
- **產生器改讀解析結果 JSON 要在第一階段之前完成。** 這樣第一階段比對的只剩語意層差異，語法層的差異（上表那類）在比對開始前就已消失。

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
- **Go 比產生器寬鬆的差異一律擋住第二階段。**
- Go 比產生器嚴格的差異，要在追蹤票留下「以產生器為準」的明確簽核。進入第二階段後，這些寫入會改成被接受。
- 清單的任何修改都經 PR。每筆記錄寫死精確列數，所以任何增減都會出現在 PR 的 diff 裡，由審查者看見；目前沒有機制強制 owner 核准（main 的分支保護不要求 review，CODEOWNERS 只會通知）。
- 產生器改讀解析結果 JSON 後，清單裡屬於語法層的記錄（例如 `!!merge` 鍵、BOM、巢狀深度）應該全部消失；沒有消失的，代表還有讀取端沒改讀 JSON。

客戶的 conf.d 無法取得，上面的條件只涵蓋 owner 的樹。另外提供一個 `da-tools` 指令，讓客戶在自己的樹上跑同一套比對。

### 8. 遷移：讀法會改變的值

YAML 改由 yaml.v3 解析後，產生器與 Python 工具看到的值，凡是兩個解析器讀法不同的，都會改變。最常見的是未加引號的 `yes`／`no`／`on`／`off`（舊讀法是布林，新讀法是字串）與 `!!merge` 標記的鍵（舊讀法是合併，新讀法是普通的鍵）。

- da-tools 提供一次性的遷移稽核指令：在客戶的樹上用兩個解析器各讀一次，列出每個讀法不同的值、所在檔案與行號、建議的寫法（例如布林改寫成 `true`／`false`）。這個指令是本 ADR 唯一還會用 PyYAML 讀 conf.d 的地方，只用來列差異，不做判定。
- 發布說明列出受影響的寫法與遷移步驟。
- exporter 本來就用 yaml.v3，閾值的讀法不變。

### 9. da-guard

da-guard 新增 `parse-tree`，不再判斷路由與 policy，交給產生器的 `--validate --strict`。閾值相關的檢查不變。

- **單獨執行 da-guard 時**（例如使用沒有 Python 的獨立 binary）：
  - 報告（文字與 JSON）明示路由與 policy「未判定」，並回一個專用的 exit code。這樣「沒判」和「判過、沒問題」可以分得開。exit code 的數值在實作時訂定，寫進 CLI 文件。
  - 遷移方式：在 CI 原本跑 da-guard 的地方，加一步 `da-tools generate-routes --validate --strict`。
  - `--required-fields _routing.*` 會直接報錯，不會悄悄失效。
- **刪掉 Go 端檢查之前**，先把只有 Go 有的檢查搬到產生器，加進兩邊的對照表，並證明拿掉後測試會紅。「只有 Go 有」的清單由程式計算，不手動維護。原本只是警告、搬過去後改成錯誤的檢查，要在 changelog 註明「變嚴格」。
- `guard-defaults-impact.yml` 加跑產生器。
- 第一階段保留 da-guard 的 Go 檢查，CI 兩邊都跑；第二階段刪除。

### 10. tenant-api 的讀取端點

- 目前沒有任何 GET 端點回傳產生器渲染的路由，因此沒有東西要遷移。
- 等到有人需要渲染後的路由，才新增 `GET /tenants/{id}/routing`，內容一律來自整理好的 JSON。
- `/effective` 回應裡的 `_routing` 是 exporter 合併出來的原始鍵，不是渲染後的路由。API 說明會註明這一點。

### 11. 刪除哪些程式

依「還有沒有人在用」決定。CI 已有檢查：列出相關套件的使用者，和宣告表不一致就紅。

| 項目 | 何時 | 處理 |
|---|---|---|
| Python 端模仿 yaml.v3 讀法的 loader、Python 端的檔案選擇規則 | 所有讀 conf.d 的 Python 工具改讀解析結果 JSON 之後 | 刪除 |
| portal 設定檢查器裡手寫的逐行 YAML 解析 | 同上；改呼叫 tenant-api 的驗證端點 | 刪除 |
| vendored yaml.v3 裡只為模仿 PyYAML 加的兩段 patch（非特定 tag、只認空格） | 產生器改讀解析結果 JSON 之後 | 刪除；寫成 breaking change |
| vendored yaml.v3 裡 exporter 也用到的效能修改（線性判重） | — | 保留 |
| Go 端模仿產生器語意的程式（模仿 PyYAML 解碼的套件、Go 版的路由與 policy 判定） | 第二階段 | 刪除 |
| 與解析無關的小工具（例如租戶 id 檢查） | — | 保留或移到別的套件 |

已驗證：用一份含重複鍵的租戶檔實測，產生器回報 `DuplicateKeyError` 並拒收。所以刪掉 Go 端的重複鍵檢查不會變寬鬆。

## 失敗時會怎樣

| 情況 | 行為 |
|---|---|
| 產生器容器掛掉或逾時 | 涉及路由或 policy 的寫入回 503；讀取沿用上一份成功結果，直到超過時限 |
| `parse-tree` 讀不了某份檔 | 該檔列在「讀不了的檔」；寫入的主體是它就拒收，否則照「base 本身已有錯誤」處理 |
| 產生整理好的 JSON 失敗（例如 policy 檔裡有無效字元） | 保留上一份成功結果供讀取；寫入照常即時驗證 |
| 讀取端遇到不認得的格式版本 | 整份當成不可用 |
| 容器裡的產生器比 policy 要求的舊 | 拒收寫入 |
| 本機沒有 da-guard、也沒有 Go 可建置 | 讀 conf.d 的 Python 工具與 pre-commit hook 失敗並說明原因，不會退回自己解析 |
| base 樹已有錯誤 | 主體乾淨的寫入照常通過 |

## 怎麼驗收

- **沒有第二個 YAML 解析器**：CI 檢查列出 Python 端所有讀 conf.d 的呼叫點，只允許透過解析結果 JSON（遷移稽核指令除外）。
- **隨機語料比對**：隨機產生 conf.d → `parse-tree` → 產生器輸出整理好的 JSON 與 finding → 同一份解析結果交給 Go 讀取端 → 斷言兩邊執行的 policy 與租戶**完全相同**，多讀、漏讀都算失敗。JSON 解析只能「完整成功」或「整份拒收」。比對程式本身不解析 YAML。
  - 第二階段起，寫入與讀取都要相同。
  - 第一階段只斷言寫入：並行判定一定擋下產生器會擋的寫入。
- **固定語料**：現有的 `merge_key_policy_corpus.json`，加上以下各票的範例：
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)：policy 檔的剩餘落差清單。
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)：租戶檔的 `!!merge` 鍵。
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)：被覆蓋的合併來源裡有無法解析的值。
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674)：自我參照的 YAML 別名。
- 在 CI 裡，以 chart 固定的那一組版本執行。

## 成本

原版實測（2026-10-09）：

| 項目 | 量測 |
|---|---|
| 產生器處理 1000 個租戶 | 1.72 秒、38 MB；其中解析佔 1.06 秒，而且每個檔被解析兩次（可用快取省下） |
| 解析單一檔案 | 0.47 ms |
| Python 啟動 | 0.11 秒、25 MB |
| 從 git 取出 1000 個檔 | 0.21 秒、12 MB |
| 映像大小（壓縮後，amd64） | da-tools 36.9 MB；tenant-api 14.0 MB |

修訂實測（2026-10-11，1000 份各含一個租戶的檔，每份約 20 行、含 `_routing`、`_metadata` 與排程閾值）：

| 項目 | 量測 |
|---|---|
| yaml.v3 逐份解析成節點並轉成上述格式的 JSON（以實驗程式代替尚未實作的 `parse-tree`） | 約 0.11 秒，累計配置 29 MB，輸出 795 KB |
| Python 讀這份 JSON | 0.024 秒 |
| 對照：PyYAML 讀同樣 1000 份檔 | 1.22 秒 |

改讀 JSON 後，Python 端的解析成本大約降到原本的十分之一以下。

## 考慮過但否決

正文已說明理由的方案（繼續在兩端互相模仿、比對錯誤訊息差異、用 `git archive` 取檔、容器掛掉時退回 Go、寫入沿用上一份結果）不再重列。

| 方案 | 否決理由 |
|---|---|
| 限縮 conf.d 到「兩個解析器必然讀出相同結果」的寫法 | 實測不可行：引號字串裡的 `\/`、冒號後接 TAB，兩邊在值都已加引號、沒用進階功能時仍讀出不同結果。這種子集只能抽樣驗證，無法定義 |
| YAML 改由 Python 統一解析，Go 端讀 Python 輸出的 JSON | exporter 是執行期元件，要在 Go 裡獨立運作；它依賴 Python 會讓每個 exporter pod 都要帶 Python 執行環境 |
| 把整理好的 policy 另存一份檔案 commit 進 git | 只涵蓋 policy，路由仍由兩邊各自解析。忘了重產時，tenant-api 剛啟動會因讀不到而擋下所有寫入；已在運作的則會沿用舊版 policy |
| 三方共用一套自訂的設定語法 | YAML 舊版規則會把 `no`、`0777` 這類真實租戶 id 讀成布林值或數字；而且要所有客戶改寫全部設定檔 |
| 產生器改用 C 實作的解析器 | 只對齊了把文字切成記號的那一層；落差也出在把記號組成資料的那一層 |
| 把 Python 的路由與 policy 語意完整移植到 Go | 約四、五千行，等於再造一份語意實作；只在加容器的成本無法接受時才考慮 |
| 寫入 API 只收 JSON | 只解決 tenant-api 的寫入；客戶手寫的 YAML 在 CI 與 exporter 仍要被解析 |
| da-guard 自己呼叫產生器 | 執行時要有 Python；沒有 Python 的獨立 binary 就不能用 |

## 實作順序

已完成：產生器輸出結構化的 finding；CI 檢查（刪除範圍的使用者比對、差異清單與它的雙向測試）。

接下來：

1. da-guard `parse-tree`；Python 共用函式庫改讀它；各 Python 工具與 pre-commit hook 改走它；遷移稽核指令。
2. 產生器改讀解析結果 JSON；刪除 Python 端模仿 yaml.v3 的 loader 與 vendored yaml.v3 的兩段模仿 patch。
3. 產生器容器：輸出整理好的 JSON、提供驗證端點；Helm chart 加容器、socket 與資源設定。
4. tenant-api：寫入改走容器驗證（第一階段並行），讀取改讀整理好的 JSON。
5. da-guard：搬移只有 Go 有的檢查、加上「未判定」狀態與 exit code；`guard-defaults-impact.yml` 加跑產生器。
6. 第二階段：刪除 Go 端模仿產生器語意的程式，在 main 上重測 #2759 等固定語料後關票。

## 附錄：實作對照

給實作者的程式碼位置，讀決策時可略過。

| 本文用語 | 程式碼 |
|---|---|
| Python 呼叫 Go binary、讀 JSON 的現有機制 | `scripts/tools/_lib_godispatch.py`、`scripts/tools/_lib_tenant_values.py`（`served-values`／`effective`） |
| Python 端的 YAML 讀取與檔案選擇函式庫（改讀 `parse-tree`） | `scripts/tools/_lib_io.py`、`_lib_confd.py`、`_lib_yaml_keys.py` |
| Python 端模仿 yaml.v3 的 loader | `_lib_yaml_keys.ExporterKeyLoader`、`_lib_io` 的 `StrictExporterKeyLoader` 系列、`describe_tenant` 的 `_GoKeyLoader` |
| portal 手寫的逐行解析 | `tools/portal/src/interactive/tools/config-lint/lint.js` |
| Go 端模仿產生器語意的程式 | `pkg/routingpolicy` 的 `shapeWork`、`pyResolve`、`ParseDomainPolicies`、`UnmarshalPolicy`；`pkg/pyyamlcompat` |
| vendored yaml.v3 的修改 | `third_party/yaml.v3`：只認空格（只給 policy，刪除）、非特定 tag（刪除）、線性判重（exporter 使用，保留）；釘值測試 `tests/ops/test_vendored_yaml_v3.py` 同一支 PR 更新 |
| tenant-api 現在的寫入判定 | `judgePutBody`；PR 模式在 `WritePRChecked` 的閉包內重判 |
| 批次 patch 由 Go 重新輸出 | `mergePatchYAML` |
| 「讀不了的檔當成空」的舊行為（將刪除） | `tenantBlockOnDisk` |
| 刪除範圍的使用者比對（CI 檢查） | 宣告表 `tests/shared/phase2_deletion_scope.json`；比對它的測試 `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| 只有 Go 有的 da-guard 檢查 | `internal/guard/types.go` 減去 `tests/shared/routing_policy_parity_matrix.json` |
| 差異清單 | `tests/shared/reader_divergence_catalog.yaml`（policy 層與路由層）；Go 端判讀快照 `tests/shared/merge_key_go_verdicts.json`；方向計算、雙向比對、到期檢查與第二階段是否就緒在 `tests/shared/test_reader_divergence_catalog.py`，`make pre-tag` 經 `reader-divergence-expiry` 以嚴格到期執行 |

## 參考

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)：原版的設計討論、量測與外部審查紀錄；[#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858)：修訂的追蹤票
- [ADR-023 tenant-api 寫入平面：單一寫者不變式](./023-write-plane-single-writer-invariant.md)
- 業界做法：
  - 只解析一次、驗證交給權威端：[Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/)。
  - 把解析工具放進使用者的 pod：[Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/)。
  - 新舊並行比對、依條件切換：[GitHub Scientist](https://github.blog/developer-skills/application-development/scientist/)。
  - 驗證端失效時拒絕而不放行：[Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/)。
