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

- **2026-10-11 修訂（提議中，待 PR 核可）**：
  - YAML 語法層也只留一個解析器：凡是用來判定設定內容的讀取，都讀 Go 解析出的同一份 JSON（例外兩項：§8 的遷移稽核指令兩個解析器都讀；§2 的寫 conf.d 工具讀原檔仍用 PyYAML，寫入前再經 `parse-tree` 核對），產生器只負責路由與 policy 的語意。方向由 owner 在 [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858) 核可。
  - 併入 owner 於 2026-10-10 與 2026-10-11 拍板的切換設計與寫入判準（[#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)）：不分兩階段，一步切換；寫入後整棵樹通過產生器檢查才寫入；不輸出整理好的設定 JSON；移除「policy 不可用時照樣放行」的開關（`--policy-unavailable-open`）；git 為必要。
- 設計與修訂都經過對抗審查（由不同模型擔任）。

## 摘要

**問題**：同一份 conf.d（租戶設定目錄）被兩個 YAML 解析器各讀一次：Go 端（exporter、tenant-api、da-guard）用 yaml.v3，Python 端（路由產生器與各工具）用 PyYAML。兩者對同一份檔常讀出不同結果，雙方都寫了大量程式去模仿對方，修了多輪仍對不齊。

**決定**：

1. **YAML 只由 Go 解析。** da-guard 新增一個指令，把整棵 conf.d 讀成一份 JSON：選了哪些檔、每份檔的內容、哪些檔讀不了。Python 端（包含路由產生器）判定設定內容時不再自己解析 conf.d 的 YAML，改讀這份 JSON；例外只有 §8 的遷移稽核指令與 §2 的寫入工具。
2. **語意各只有一個實作。** 路由、domain policy、租戶清單的語意只由產生器（Python）判定；閾值與 exporter 送出的值只由 exporter（Go）判定。
3. tenant-api 的 pod 加一個容器跑產生器，每一筆會改變檔案內容的寫入都先問它。tenant-api 不再自己判定路由與 policy。
4. **一步切換。** 產生器改讀 JSON、tenant-api 改問產生器容器、da-guard 不再判定路由與 policy，三者在同一個版本發布；Go 端與 Python 端互相模仿的程式在同一個版本刪除。不設新舊並行期。

**對使用者與維運的影響**（都是 breaking change）：

- 依舊版 YAML 規則成立的寫法，意思可能改變，部分檔案會變成讀不了。例如路由裡的 `send_resolved: yes`，產生器過去讀成布林 `true`，改由 yaml.v3 解析後是字串 `"yes"`。提供一次性的遷移稽核指令，列出所有受影響的值與檔案。
- 讀 conf.d 的 Python 工具需要 da-guard。da-tools 映像已內附；本機的 pre-commit 會以 Go 現場建置。
- tenant-api 多一個容器（da-tools 映像）。容器或部署端 policy 檔不可用時，所有會改變檔案內容的寫入回 503（包括只改閾值的），沒有「不可用時照樣放行」的開關。
- 寫入後的整棵樹必須通過產生器的檢查才寫入。樹上任何地方有會擋下的問題，經 tenant-api 的寫入都會被拒，直到修好（§4）。
- tenant-api 必須搭配 git，不再支援沒有 git 的部署。
- 只跑 da-guard 的 CI 不再涵蓋路由與 policy，要另外加一步 `da-tools generate-routes --validate --strict`。

**不可協商的驗收條件**：

- 任何讀取端都不得比產生器寬鬆：不能讀出產生器丟掉的 policy，也不能漏掉產生器會執行的 policy。tenant-api 的寫入判準就是產生器自己的結果（§4），所以這一條對 tenant-api 由定義保證。
- 改讀 JSON 後的產生器，不得比現在的產生器寬鬆：今天產生器在 `--strict` 下拒收的檔，`parse-tree` 也要判為讀不了，例外逐筆列出理由（§2）。

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

對齊的做法，是讓一邊在自己的語言裡重寫另一邊解析器的每一條處理路徑：Go 端有一個專門模仿 PyYAML 的套件，vendored（複製進 repo 並自行修改）的 yaml.v3 也打了兩段為了模仿 PyYAML 而加的 patch；Python 端則有多個模仿 yaml.v3 的 loader（例如把 mapping 的鍵照原文讀成字串）。每補一條，旁邊就露出另一條，處理順序本身也會影響結果。用隨機輸入同時餵兩邊、比對結果，找得到落差，但證明不了兩邊相等。

只把「語意」收斂到一處並不夠。路由與 policy 的判定若只在產生器做，但產生器仍用 PyYAML 讀 YAML、Go 端仍用 yaml.v3 讀，上表前四列這類語法層的落差照樣存在，只是換成「產生器容器與 tenant-api 的判定不同」。

也試過另一條路：把 conf.d 限縮到「兩個解析器必然讀出相同結果」的寫法。實測不可行，上表第三、四列的值都已加上引號、也沒用到任何進階功能，兩邊仍然讀出不同結果。這種「相同的子集」只能抽樣驗證，無法定義。

問題的根源是「兩個解析器各讀一次，又要求結果一模一樣」。唯一不需要證明兩邊相等的做法，是只留一個解析器。

## 決定

### 1. 誰解析什麼

| 層次 | 唯一的實作 | 其他工具怎麼取得 |
|---|---|---|
| YAML 語法：選哪些檔、每份檔的內容 | Go（da-guard，yaml.v3） | 讀 da-guard 輸出的「解析結果 JSON」 |
| 路由（`_routing_defaults`、`routing_profiles`、租戶的 `_routing`）、domain policy、租戶清單的語意 | 路由產生器（Python） | 讀產生器輸出的 finding（產生器回報的每一筆問題，見 §4）與狀態端點（§5） |
| 閾值與 exporter 實際提供的值 | exporter（Go） | 不變：讀 `da-guard served-values`／`effective` 的 JSON |

第三列是現成的先例：閾值早就是「一個實作，其他工具讀它的輸出」。本 ADR 把同樣的做法套到 YAML 語法層與路由、policy 語意。

### 2. 解析結果 JSON

da-guard 新增子命令 `parse-tree`，輸入一個 conf.d 目錄，輸出一份 JSON。另有單檔模式，給改寫檔案的工具在寫入前確認（見本節末）。

**選了哪些檔**：輸出每個讀取端實際會讀的檔，並標出誰讀它。

- exporter 的檔案選擇規則（副檔名、大小寫、隱藏檔、子目錄），包含每個目錄最後採用哪一份 `_defaults` 檔。
- 產生器另外讀、exporter 不讀的檔：policy 檔與 routing profile 檔（`_domain_policy.yaml`、`_routing_profiles.yaml`、`_profiles.yaml`，包括子目錄裡的與 `.yml` 拼法）。同一份檔兩種拼法並存時，沿用產生器現行的處理。

Python 端不再有自己的一份規則。兩邊選檔的差異列進固定語料。

**每份檔的內容**：輸出的是 yaml.v3 **解碼後**的結構，不是節點原樣。

- 頂層必須是 mapping；空檔視為空 mapping。
- YAML 的別名（alias，`*x` 指回前面以 `&x` 標記的內容）與未加引號的合併鍵 `<<`（把另一個 mapping 併進來的寫法）都已展開；展開套用 yaml.v3 解碼時同樣的別名上限，超過上限的檔列為讀不了。加了引號的 `"<<"` 是普通的鍵，照原文輸出。讀取端不需要自己展開。
- mapping 的**鍵一律輸出原文字串**（`010` 就是 `"010"`，與 exporter 讀租戶 id 的方式相同）。
- **純量輸出原文、yaml.v3 判定的型別，以及換算後的值**：例如 `{"text": "yes", "tag": "!!str", "value": "yes"}`、`{"text": "0777", "tag": "!!int", "value": "511"}`。
  - 布林與 null 的值是 JSON 的 `true`／`false`／`null`。
  - 整數與浮點數的值以十進位字串輸出，避免精度在兩種語言間不一致。
  - 時間與無限大只有原文與型別。
  - 帶原文是因為 exporter 讀閾值時用的就是原文；帶換算後的值，是讓語意層不必再寫一份 yaml.v3 的型別判定。
- 語意層只依型別與換算後的值判斷，不得採用「yaml.v3 解碼進 Go 有型別欄位時的寬鬆轉換」（例如字串 `yes` 解碼進布林欄位會得到 `true`）。
- 輸出必須是合法的 UTF-8。

**讀不了的檔**：輸出檔名與原因，不輸出內容。下列都算讀不了，而不是照 yaml.v3 讀出的結果：

- 語法錯誤、讀取失敗、不是一般檔、不是合法的 UTF-8。
- yaml.v3 解碼時會報錯的一切情況，例如顯式型別與原文對不上（`!!int abc`）、合併鍵的值不是 mapping（`<<: 1`）。PyYAML 會把其中一部分照讀（例如 `!!bool yes` 讀成 `True`），之後一律讀不了。
- 一個檔裡有多份文件（`---` 分隔）。yaml.v3 只讀第一份，其餘靜默忽略；照抄等於讓 `---` 之後的 policy 失效。
- 同一個 mapping 裡有重複的鍵，以展開別名後的鍵原文比對。yaml.v3 對「別名當鍵」造成的重複會靜默取最後一個值，所以這一條要 `parse-tree` 自己檢查。
- 鍵被判為 null（`~:`、`null:`、空鍵），或鍵不是純量（`? [a]`、`? {c: d}`）。原文輸出會讓 `~` 與 `"~"` 分不出來；yaml.v3 對同一個 mapping 裡的 `~:` 與 `null:` 也會靜默取最後一個；產生器與 exporter 過去對 null 鍵的處理也不同。
- 型別標記不在白名單內。純量只收 `!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!timestamp`；mapping 與序列只收沒有標記或標 `!!map`／`!!seq` 的。`!foo`、`!!python/...`、`!!binary`、`!!set`、`!!omap`、`!!merge` 都算讀不了；yaml.v3 會把其中一部分讀成字串，照抄會讓產生器過去拒收的寫法變成可用。
- 檔案開頭以外的位置出現 BOM（U+FEFF，一種標示編碼的不可見字元）。BOM 跨過第 512 byte 時，yaml.v3 會吃掉下一個字元（實測 `domain_policies` 讀成 `omain_policies`，整份 policy 靜默失效）；在其他位置時，可能讓鍵或值帶上看不見的字元。為了不必逐一分辨，檔案開頭以外出現一律判為讀不了。

外層帶格式版本與 da-guard 版本。讀取端遇到不認得的格式版本，整份當成不可用。

這些「讀不了」是 `parse-tree` 自己的規則。exporter 讀閾值的方式不變，所以同一份檔可能 exporter 照讀、產生器拒收；這時產生器與 CI 會擋下，不會放行。

**不得比現在寬鬆**：固定語料收錄「今天的產生器在 `--strict` 下拒收」的檔，`parse-tree` 必須判為讀不了；不判的，逐筆列出理由並經審查。已知的例外是 yaml.v3 本來就讀得正確、只是 PyYAML 拒收的寫法：冒號後接 TAB、以 TAB 縮排的 policy 檔、巢狀很深（例如 600 層）的檔。這些檔之後讀得到，產生器照內容執行 policy，不會因此放寬。

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

輸出（節錄；以 yaml.v3 的節點逐一轉成 JSON 實測，`value` 欄位依上述規則補上）：

```json
{
  "t010.yaml": {
    "tenants": {
      "010": {
        "_routing": {
          "group_wait": {"text": "30s", "tag": "!!str", "value": "30s"},
          "receiver": {
            "api_url": {"text": "https://hooks.slack.com/x", "tag": "!!str", "value": "https://hooks.slack.com/x"},
            "send_resolved": {"text": "yes", "tag": "!!str", "value": "yes"},
            "type": {"text": "slack", "tag": "!!str", "value": "slack"}
          }
        }
      }
    }
  }
}
```

現在的產生器以一個模仿 yaml.v3 的 loader 讀鍵，`010` 已經讀成 `"010"`；但 `send_resolved: yes` 仍讀成布林 `True`。改讀 JSON 後是字串 `"yes"`，所有 Python 工具看到的都是 Go 端讀到的那一份，那個 loader 也一併刪除。

**Python 端怎麼取得**：沿用現有的「Python 呼叫 Go binary、讀 JSON」機制（da-tools 已用這個方式讀 da-guard 的 `served-values`／`effective`）。共用的 YAML 讀取與檔案選擇函式庫改為讀這份 JSON；各工具不再直接呼叫 YAML 解析器讀 conf.d。產生器每個用到值的地方都改用 JSON 的型別與值，不只 `send_resolved`（例如 `_severity_dedup: off` 過去讀成 `False`，現在是字串 `"off"`）。

**寫 conf.d 檔的 Python 工具是明示例外**，包括改寫既有檔的（例如 `patch_config`、`deprecate_rule`）與產生新檔的（例如 `scaffold_tenant`、`init_project`、`onboard_platform`、`migrate_rule`）。它們用 PyYAML 讀原檔、產生要寫出的文字，這兩件事用的 PyYAML loader 不在刪除範圍（§11）。本 ADR 不改這個做法，只加限制：

- 要改什麼、改不改，一律依 JSON 判斷；PyYAML 只用來產生文字。
- 產生文字後、寫入前，以 `parse-tree` 的單檔模式讀這段文字，並帶上它要寫入的路徑：
  - 預期內容是「原檔的解析結果套上這次的修改」；新檔則是工具要寫的內容。
  - 整份檔的鍵、結構、每個純量的原文、型別與值都要與預期相同，否則不寫入。
  - 原檔讀不了、或寫入路徑不會被任何讀取端讀到，也不寫入。
- 已知後果：檔裡有 PyYAML 輸出時會改寫的寫法（例如未加引號的 `1e3`、`0o17`、`0x1F`，yaml.v3 會讀成數字），整份檔都會被拒寫，要先手動改成加引號的寫法。這是安全的方向。

日後若改由 Go 提供改寫指令，這個例外就可以拿掉。

**範圍**：只涵蓋 conf.d。rule pack、Kubernetes manifest 等其他 YAML 不在本 ADR 範圍內。portal 的瀏覽器端工具（以 js-yaml 解析的設定比對、路由追蹤、YAML 驗證、playground、告警預覽，以及設定檢查器裡手寫的逐行解析）只做預覽，不在範圍內：它們的結果不作為任何寫入或 CI 的放行依據，畫面上註明以 da-tools 的結果為準。

### 3. 運作方式（tenant-api）

**pod 加一個產生器容器，tenant-api 每次寫入前都問它。**

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api（Go）              │ ───▶ │ 產生器容器（da-tools 映像）   │
│ - 收到寫入請求                │ 驗證 │ - 依指定的 commit 取出檔案    │
│ - 依驗證結果寫入或拒收        │ ◀─── │ - da-guard parse-tree 解析    │
│ - 讀狀態端點產生 metric       │ 結果 │ - 產生器依解析結果判定語意    │
└──────────────────────────────┘      └──────────────────────────────┘
          兩者只透過共用目錄裡的 Unix socket 溝通
```

- **映像**：用現成的 da-tools 映像，裡面已有產生器與 da-guard。服務程式只用 Python 標準庫。
- **溝通**：HTTP/1.1，走 Unix socket，不開網路埠。socket 放在 pod 內的共用暫存目錄（emptyDir）。只有掛了這個目錄、而且符合 socket 檔案權限的程序能連線。chart 現有給人員身分請求用的 human socket 也是這樣做。
- **執行**：常駐程序負責從 git 取檔，取出的檔案依 commit 快取。每個請求開一個子程序判完；批次的驗證在同一個子程序內做；解析結果只在單一請求內重用。
- **逾時**：判定超時就殺掉子程序，子程序啟動的程式也一併殺掉；該筆寫入拒收。
- **amtool 必須存在**：amtool 是 Alertmanager 的設定檢查工具。產生器的驗證模式找不到它時只印提醒並通過，產生器容器不接受這種情況：每次判定前先確認 amtool 存在，缺了就算容器不可用。否則產生器會以結束碼 0、零筆 finding 結束，看起來像通過。
- **何時改成在常駐程序裡解析**：看寫入鎖持有時間的 p95（95% 的寫入不超過的值）。p95 超過寫入逾時的三分之一就改：常駐程序 fork 子程序，加跨請求的解析快取。寫入逾時指 HTTP server 的寫入逾時（`--write-timeout`，預設 30 秒），另有固定 30 秒的請求逾時 middleware，兩者都不是寫入鎖的逾時。

**取檔方式：產生器容器只從 git 物件取檔，不讀工作目錄。**

- 唯讀掛載 tenant-api 用的同一個 repo，依指定的 commit 取出檔案（`git ls-tree` 加 `git cat-file`）。
- 不讀工作目錄：PR 模式（寫入時開 PR，而不是直接 commit）會就地切換分支，直接讀可能讀到切換一半的狀態。
- 不用 `git archive`：它會套用 `.gitattributes` 的匯出規則，客戶若把 policy 檔標成不匯出，產生器就看不到。
- **兩個容器必須用同一個 uid**（程序執行時的 Linux 使用者編號）：git 會拒絕讀別的 uid 擁有的 repo（實測：不同 uid 以 `-C` 讀，報 dubious ownership）。chart 加測試釘住。
- **repo 關閉 git 的自動垃圾回收（`gc.auto=0`）**。實測：同 uid 下一邊 15 輪 commit 加 `repack -a -d` 加 `gc --prune=now`，另一邊讀固定 commit 1376 次，0 失敗；唯讀掛載可讀。
- **讀取失敗**：重試一次，再失敗就拒收。
- **git 為必要**：不再支援沒有 git 的部署。

### 4. 寫入怎麼驗證

**寫入後，產生器檢查整棵樹；全部通過才寫入。**

- **判準**：產生器容器把要寫的檔換成新內容，對寫入後的整棵樹以 `--validate --strict` 執行。結束碼是 0 就寫入，其他一律拒收。
  - 用的是同一個 base commit、同一份部署端 policy 檔、容器裡的那一版產生器，而且 amtool 必須存在。
  - 判準就是產生器自己的結果，所以 tenant-api 不會比它容器裡的產生器寬鬆。
- **哪些寫入要送驗**：凡是會改變檔案內容的寫入都送驗。是否送驗不由 Go 判斷「有沒有改到路由」。
- **錯誤回應**：列出產生器回報的 finding（產生器回報的每一筆問題）。與被寫入檔有關的排在前面，並標出哪些在寫入前就存在、來源是平台預設、routing profile 還是租戶自己。這只影響訊息，不影響判定。
- **批次**：一次請求、一次 commit、全有全無。整批套用後整棵樹通過才寫入，否則整批拒收；回應逐筆列出相關的 finding。中間狀態不驗，也不會進 repo。同步與非同步批次規則相同；非同步批次在執行時才判定。前置：tenant-api 的 git 寫入支援多檔一次 commit。
- **批次 patch 的合併**：仍由 Go 讀出檔案、套用 patch、再寫回；合併後的內容一定送驗。
  - 要寫的檔在 base 裡本身讀不了，合併前就拒收。否則 Go 重新輸出時可能吃掉 `---` 之後的文件或重複的鍵，讓合併後的內容反而讀得了，等於靜默改寫。
  - 擋不到的是「沒有違規，但意思被 Go 改掉」的改寫，由離線差分測試把關：一邊是 Go 合併後交給產生器讀，另一邊是產生器讀 base 再套同一個 patch，兩邊結果要相同，同一個檔裡的其他租戶也要比。
- **驗的和寫的必須是同一份**：容器回傳它驗過的「檔案路徑、內容 hash、base commit、部署端 policy 檔的內容 hash」。tenant-api 在寫入鎖之下核對；部署端 policy 檔由 tenant-api 唯讀掛載同一份，自己算 hash 來比。不一致就重驗一次，仍不一致就拒收；寫入的位元組必須和驗過的一模一樣。
- **PR 模式驗的是分支起點**：驗的是 PR 分支所在的 base commit 加上這筆寫入。PR 合併進主幹後的結果不在 tenant-api 的判定範圍，由客戶 CI 或 merge queue 把關。
- **任何異常都拒收**：容器出錯、逾時、輸出不完整，都當成拒收。

**代價（breaking change）**：

- 樹上任何地方有會擋下的問題，所有經 tenant-api 的寫入都會被拒，直到修好為止。
- 修復路徑：
  - 問題在某個租戶檔、而且修好它就能讓整棵樹通過時，可以經 tenant-api 修。
  - 其他情況走 git，例如平台檔、子目錄的檔、多個租戶同時違規（平台收緊 policy 後常見）。PR 模式下合併後才出現的問題也屬此類。
  - 客戶 CI 綠、tenant-api 卻判整棵樹紅時：用容器那一版產生器（版本由 §6 的 metric 讀）與部署端 policy 檔在本機重跑，找出 finding，再走 git 修。
- 為什麼可以接受：客戶 CI 用同一版 da-tools、帶同一份部署端 policy 檔時，會讓整棵樹變紅的改動進不了主幹；整棵樹紅的時候，客戶 CI 本來就紅。這等於「主幹永遠保持綠燈」，與 merge queue（合併前先在最新主幹上跑完測試的佇列）的做法相同。兩個條件缺一個，就可能出現客戶 CI 綠、tenant-api 整棵樹紅，走上面最後一條修復路徑。

### 5. policy 檔與 policy 狀態

**部署端的 policy 檔掛進產生器容器；policy 檔不可用就拒收寫入。**

- **部署端的 policy 檔**（`--policy`）：檔裡的 `allowed_domains` 限制 webhook 網址的網域。chart 加一個可選值把這份檔掛進產生器容器，判定時一併帶上；tenant-api 容器也唯讀掛載同一份，用來核對 hash（§4）。理由：這是防 SSRF（服務被拿來替攻擊者對內網發請求）的控制，要放在租戶 repo 之外；有寫入權限的人不該能改掉它。`da-tools init` 產生的客戶 CI 模板目前沒帶 `--policy`；模板改為可選擇帶入部署端 policy 檔，讓客戶 CI 與 tenant-api 判的是同一份。
- **狀態端點**：產生器容器回報部署端 policy 檔是否可用，tenant-api 讀它來產生 `tenant_api_policy_available`。這個 metric 只反映部署端 policy 檔與容器；repo 裡的 policy 檔有錯不會讓它變 0，那種情況寫入以 finding 拒收。這個 metric 有告警在讀，維持原名；目前沒有 label。
  - 讀不到狀態端點（容器不可達）時，metric 為 0，告警照樣會亮。
  - 它的說明文字把「沿用上一份可用內容」算成可用，切換後不再沿用舊內容，說明文字屆時要改。
- **沒有放行開關**：部署端 policy 檔或產生器容器不可用時，會改變檔案內容的寫入一律拒收（包括只改閾值的），回 503。
  - 503 只用在這兩種情況。repo 裡的 policy 檔（平台預設、routing profile）有錯，是產生器回報的 finding，照 §4 拒收，不回 503。
  - 部署端 policy 檔不可用時錯誤碼是 `POLICY_UNAVAILABLE`；容器不可用時的錯誤碼在實作時訂定。
  - 現行 tenant-api 的範圍較小：只在直接寫入模式、policy 檔存在卻沒有任何一份可用內容時才回；單筆 PUT 一律回；同步批次只要有一筆碰到路由，整個請求回 503；非同步批次在執行時逐筆再判。新規則不分模式。
  - `--policy-unavailable-open` 與計數器 `tenant_api_policy_unavailable_open_total` 移除。告警 `TenantApiPolicyUnavailable` 保留（看的是可用與否的 metric），但它的說明文字提到放行開關與計數器，要一併改。
- **不輸出整理好的設定 JSON**：目前沒有程式需要讀它。

### 6. 版本

客戶的 CI 用哪一版 da-tools，與 tenant-api 無關，所以「和客戶 CI 完全一致」做不到。能保證的是：**tenant-api 不比它容器裡的產生器寬鬆**。da-guard 與產生器在同一個 da-tools 映像裡，兩者的版本一起決定。

兩邊版本不同時，任一邊拒收，設定都進不了 Alertmanager：
- 客戶 CI 的版本比較嚴格時，tenant-api 接受的寫入會在 CI 被擋下。結果是晚一步擋下，不會放行。
- tenant-api 容器裡的版本比較嚴格時，tenant-api 會拒收 CI 原本會接受的寫入。

兩種情況都不會放行違規的設定，代價是兩邊的判定不一致。縮小版本落差的做法：

- tenant-api 的 chart 預設固定一個 da-tools 版本，CI 對這一組版本跑相容性測試。
- `da-tools init` 產生的設定改成固定版本，不再用 `latest`。
- policy 檔可以宣告「至少需要哪一版產生器」，容器比它舊就拒收所有會改變檔案內容的寫入；修法是維運者調高 chart 設定的 da-tools 版本。這是新增的鍵，只由產生器讀取；最晚在切換那一版加上，否則較舊的產生器不認得它。
- tenant-api 以 metric 公開產生器與 da-guard 的版本。

### 7. 一步切換

**產生器改讀 JSON、tenant-api 改問產生器容器、da-guard 改為「未判定」，三者在同一個版本發布。** 不設新舊並行期，也不維護差異清單（逐筆記錄 Go 與產生器判定不同的設定檔）。

這樣做是因為任何一個版本裡，只要還有 Go 端在模仿舊的 PyYAML 讀法、而產生器已改用 yaml.v3 讀法，語法層的落差就只是換了方向；同時發布，就沒有這種版本。

**切換前要先完成的事**：

- `da-tools init` 產生的設定固定 da-tools 版本。
- 把不是解析的檢查（例如遮罩憑證的佔位字串回寫防護、租戶 id 檢查、判斷租戶是否停用的 `IsDisabled`）搬出要刪除的套件。
- 只有 Go 有的 da-guard 檢查先搬到產生器（§9）。
- tenant-api 的 git 寫入支援多檔一次 commit（§4）。
- `parse-tree` 與它的單檔模式；遷移稽核指令。這兩項可以先發布。Python 共用函式庫與各工具（包括產生器，它直接使用共用函式庫）在切換那一版一起改讀 JSON，寫 conf.d 的工具也在那一版加上寫入前確認；先切共用函式庫會讓產生器一半讀 JSON、一半用 PyYAML。

**切換那一版同時做的事**：

- 刪除 Go 端模仿 PyYAML 與模仿產生器語意的程式（§11）。
- 刪除扁平鍵 `_routing_receiver_type`（舊版指定 receiver 種類的單層寫法），寫入它改為回 400。
- 刪除差異清單與 `make pre-tag` 的到期檢查。

**容器不可用時不退回 Go**：Go 端判定已刪除；而且能送寫入請求的使用者可以影響容器是否可用（例如連續送出大量需要整棵樹重算的請求），退回較寬鬆的判定等於讓請求方自己挑判定者。

客戶的 conf.d 無法取得，切換前的驗證只涵蓋 owner 的樹。遷移稽核指令（§8）讓客戶在自己的樹上先看到影響。

### 8. 遷移：讀法會改變的值

YAML 改由 yaml.v3 解析後，產生器與 Python 工具看到的值，凡是兩個解析器讀法不同的，都會改變。最常見的是未加引號的 `yes`／`no`／`on`／`off`（舊讀法是布林，新讀法是字串）；`!!merge` 標記的鍵（舊讀法是合併）之後整份檔讀不了。

- da-tools 提供一次性的遷移稽核指令：在客戶的樹上用兩個解析器各讀一次，列出三類結果：
  - 讀法不同的值：所在檔案、行號、建議的寫法（例如布林改寫成 `true`／`false`）。
  - 讀法不同的鍵。
  - 整份會變成讀不了的檔（例如引號字串裡的 `\/`、多份文件、null 鍵、白名單外的型別標記如 `!!merge`、檔中的 BOM）。

  這個指令與 §2 的寫入工具是本 ADR 僅有還會用 PyYAML 讀寫 conf.d 的地方；稽核指令只列差異，不做判定。
- 發布說明列出受影響的寫法與遷移步驟。
- exporter 本來就用 yaml.v3，但刪掉 vendored yaml.v3 的「非特定 tag」patch 後，它讀閾值與自訂告警的方式有兩處改變，都列在遷移稽核裡：
  - 以 `!` 標記的引號字串 `! "<<"` 不再被當成合併鍵，回到 yaml.v3 原本的讀法（字面上的鍵）。
  - 自訂告警 SLO 的 `objective: ! "99.9"` 過去因型別標記是 `!` 而被拒收，之後型別是 `!!str`，改為接受。這一處是變寬鬆。

### 9. da-guard

da-guard 新增 `parse-tree`，不再判斷路由與 policy，交給產生器的 `--validate --strict`。閾值相關的檢查不變。

- **單獨執行 da-guard 時**（例如使用沒有 Python 的獨立 binary）：
  - 報告（文字與 JSON）明示路由與 policy「未判定」，並回一個專用的 exit code。這樣「沒判」和「判過、沒問題」可以分得開。exit code 的數值在實作時訂定，寫進 CLI 文件。
  - 遷移方式：在 CI 原本跑 da-guard 的地方，加一步 `da-tools generate-routes --validate --strict`。
  - `--required-fields _routing.*` 會直接報錯，不會悄悄失效。
- **刪掉 Go 端檢查之前**，先把只有 Go 有的檢查搬到產生器，加進兩邊的對照表，並證明拿掉後測試會紅。「只有 Go 有」的清單由程式計算，不手動維護。原本只是警告、搬過去後改成錯誤的檢查，要在 changelog 註明「變嚴格」。
- `guard-defaults-impact.yml`（PR 改到預設值時檢查影響範圍的 CI）加跑產生器。

### 10. tenant-api 的讀取端點

- 目前沒有任何 GET 端點回傳產生器渲染的路由，因此沒有東西要遷移。
- 等到有人需要渲染後的路由，才新增 `GET /tenants/{id}/routing`，內容由產生器容器產生。
- `/effective` 回應裡的 `_routing` 是 exporter 合併出來的原始鍵，不是渲染後的路由。API 說明會註明這一點。

### 11. 刪除哪些程式

依「還有沒有人在用」決定。CI 已有檢查：列出相關套件的使用者，和宣告表不一致就紅。

| 項目 | 何時 | 處理 |
|---|---|---|
| Python 端模仿 yaml.v3 讀法的 loader、Python 端的檔案選擇規則 | 切換那一版 | 刪除；寫 conf.d 工具讀原檔與產生文字用的 loader 不在此列（§2） |
| Go 端模仿 PyYAML 讀法的程式：模仿 PyYAML 解碼的套件、模仿 PyYAML 型別判定的函式、policy 解碼器打開的「只認空格」選項 | 切換那一版（tenant-api 與 da-guard 在同一版停用） | 刪除 |
| Go 端模仿產生器語意的程式（Go 版的路由與 policy 判定） | 同上 | 刪除 |
| vendored yaml.v3 裡為模仿 PyYAML 加的兩段 patch：「非特定 tag」（YAML 的 `!` 標記，讓引號字串照未加引號的規則判型別）、「只認空格」（拒收 TAB 縮排） | 同上 | 刪除；「非特定 tag」會改變 exporter 的兩處讀法（§8），寫成 breaking change |
| vendored yaml.v3 裡 exporter 也用到的效能修改（大型 mapping 的重複鍵檢查改用 hash 表） | — | 保留 |
| 差異清單、它的測試與 `make pre-tag` 的到期檢查 | 同上 | 刪除 |
| 與解析無關的小工具（例如租戶 id 檢查） | 切換之前 | 移到別的套件 |

CI 的宣告表記錄每個套件與 patch 何時可刪；做切換的那支 PR 一併更新它。

重複鍵、多份文件、白名單外的型別標記：今天由產生器的 loader 與 Go 端模仿套件擋下（部分 Python 工具刻意只讀第一份文件）。刪除它們之前，`parse-tree` 要先把這些判為讀不了（§2），並由固定語料確認。

## 失敗時會怎樣

| 情況 | 行為 |
|---|---|
| 寫入後整棵樹的產生器結束碼不是 0 | 拒收；錯誤回應列出 finding，標出寫入前就存在的與來源 |
| 產生器容器掛掉、逾時，或找不到 amtool | 所有會改變檔案內容的寫入回 503；`tenant_api_policy_available` 為 0 |
| 部署端 policy 檔不可用 | 所有會改變檔案內容的寫入回 503，錯誤碼 `POLICY_UNAVAILABLE` |
| repo 裡的 policy 檔有錯 | 產生器回報 finding，照第一列拒收，不回 503 |
| `parse-tree` 讀不了某份檔 | 該檔列在「讀不了的檔」，產生器的檢查不會通過，寫入拒收 |
| 寫 conf.d 的 Python 工具產生的文字，`parse-tree` 讀出來與預期不同 | 不寫入，並說明哪個值不同 |
| 讀取端遇到不認得的格式版本 | 整份當成不可用 |
| 容器裡的產生器比 policy 要求的舊 | 所有會改變檔案內容的寫入拒收，直到維運者調高 chart 的 da-tools 版本 |
| 本機沒有 da-guard、也沒有 Go 可建置 | 讀 conf.d 的 Python 工具與 pre-commit hook 失敗並說明原因，不會退回自己解析。受影響的 hook 與沒有 Go 的 CI job 由實作 PR 以程式列出，CI job 改用 da-tools 映像或加裝 Go |

## 怎麼驗收

- **沒有第二個 YAML 解析器**：CI 檢查列出 Python 端所有 YAML loader 的呼叫點，只允許透過解析結果 JSON；例外只有遷移稽核指令與 §2 的寫入工具，並且寫死在檢查的白名單裡。
- **不比現在寬鬆**：固定語料收錄今天產生器在 `--strict` 下拒收的檔，`parse-tree` 必須判為讀不了；例外逐筆列出理由（§2）。
- **固定語料**另外包括現有的 `merge_key_policy_corpus.json`，加上以下各票的範例：
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)：policy 檔的剩餘落差清單。
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)：租戶檔的 `!!merge` 鍵。
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)：被覆蓋的合併來源裡有無法解析的值。
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674)：自我參照的 YAML 別名。
- **批次 patch 合併的離線差分測試**（§4）。
- **da-guard 的「未判定」**：exit code 與報告內容有測試。
- **產生器容器的拒收條件**：找不到 amtool、驗過與要寫的內容重驗一次仍不一致、產生器比 policy 要求的版本舊，三者各有測試，結果都是拒收。
- 在 CI 裡，以 chart 固定的那一組版本執行。

## 成本

語意層（產生器，2026-10-09 實測）：

| 項目 | 量測 |
|---|---|
| 產生器處理 1000 個租戶 | 1.72 秒、38 MB；其中解析佔 1.06 秒，而且每個檔被解析兩次（可用快取省下） |
| 解析單一檔案 | 0.47 ms |
| Python 啟動 | 0.11 秒、25 MB |
| 從 git 取出 1000 個檔 | 0.21 秒、12 MB |
| 映像大小（壓縮後，amd64） | da-tools 36.9 MB；tenant-api 14.0 MB |

產生器容器（2026-10-10 實測，1000 個租戶的樹）：每個請求開新程序約 3.1 秒；同一程序加上解析快取約 1.5～1.8 秒。

語法層（`parse-tree`，2026-10-11 實測；1000 份各含一個租戶的檔，每份約 20 行、含 `_routing`、`_metadata` 與排程閾值）：

| 項目 | 量測 |
|---|---|
| yaml.v3 逐份解析成節點並轉成 JSON（以實驗程式代替尚未實作的 `parse-tree`；只輸出原文與型別，沒有 `value`、也沒有展開合併） | 約 0.11 秒，累計配置 29 MB，輸出 795 KB |
| Python 讀這份 JSON | 0.024 秒 |
| 對照：PyYAML 讀同樣 1000 份檔 | 1.22 秒 |

改讀 JSON 後，Python 端的解析成本大約降到原本的十分之一以下；加上 `value` 與展開後，輸出會略大。

## 考慮過但否決

正文已說明理由的方案（繼續在兩端互相模仿、用 `git archive` 取檔、容器掛掉時退回 Go）不再重列。

| 方案 | 否決理由 |
|---|---|
| 限縮 conf.d 到「兩個解析器必然讀出相同結果」的寫法 | 實測不可行：引號字串裡的 `\/`、冒號後接 TAB，兩邊在值都已加引號、沒用進階功能時仍讀出不同結果。這種子集只能抽樣驗證，無法定義 |
| 寫入時只看屬於這次寫入的 finding（逐筆歸屬） | 產生器回報 finding 的方式不是為了判斷責任而設計：它的驗證依序短路，前面有錯，後面的組裝與 amtool 就不跑；執行失敗的 finding 只在沒有其他 finding 時才補上；平台檔與跨租戶的 finding 沒有一致的主體。兩版歸屬規則都被實跑的反例打穿：鄰居的違規遮掉被寫入檔的 amtool 拒收；`_routing_defaults.yaml` 寫壞，平台強制路由整個被丟，那筆 finding 卻不屬於任何一筆寫入 |
| 只比對寫入前後「新增了哪些錯誤訊息」 | 已經違規的租戶改寫設定時，會產生和 base 一模一樣的那行訊息，比對差異會把它放行 |
| 租戶本來就違規時，寫入前後的渲染結果相同就不擋 | 會漏：實測在 `group_by` 加一個空值，產生器多報一筆擋下的 finding，渲染結果卻完全不變。要補就得比對 finding 有沒有新增，也就是上一列。Kubernetes CRD validation ratcheting 從 1.28 alpha 到 1.33 GA 花了五個版本，累積了一張只增不減的不適用清單，也說明這類例外難以收斂 |
| 批次驗兩次：先剔除違規的那幾筆，再驗剩下的組合 | 依賴逐筆歸屬（上上列）；改為整批全有全無 |
| 產生器拒收別名、合併鍵與顯式型別標記 | 理由是縮小兩個解析器的落差；只留一個解析器後已沒有落差可縮，卻會要求客戶改寫既有的設定檔 |
| 分兩階段切換（新舊並行、比對差異、條件達成才刪 Go 端） | 產品採用者少，可以接受 breaking change；並行期要維護兩份判定與差異清單，而 Go 端判定已知較寬鬆，留著它沒有好處 |
| 輸出整理好的設定 JSON 給 tenant-api 讀 | 目前沒有程式需要讀它；policy 是否可用改由狀態端點回報 |
| YAML 改由 Python 統一解析，Go 端讀 Python 輸出的 JSON | exporter 是執行期元件，要在 Go 裡獨立運作；它依賴 Python 會讓每個 exporter pod 都要帶 Python 執行環境 |
| 把整理好的 policy 另存一份檔案 commit 進 git | 只涵蓋 policy，路由仍由兩邊各自解析。忘了重產時，tenant-api 剛啟動會因讀不到而擋下所有寫入；已在運作的則會沿用舊版 policy |
| 三方共用一套自訂的設定語法 | YAML 舊版規則會把 `no`、`0777` 這類真實租戶 id 讀成布林值或數字；而且要所有客戶改寫全部設定檔 |
| 產生器改用 C 實作的解析器 | 只對齊了把文字切成記號的那一層；落差也出在把記號組成資料的那一層 |
| 把 Python 的路由與 policy 語意完整移植到 Go | 約四、五千行，等於再造一份語意實作；只在加容器的成本無法接受時才考慮 |
| 寫入 API 只收 JSON | 只解決 tenant-api 的寫入；客戶手寫的 YAML 在 CI 與 exporter 仍要被解析 |
| da-guard 自己呼叫產生器 | 執行時要有 Python；沒有 Python 的獨立 binary 就不能用 |

## 實作順序

1. 已完成：產生器輸出結構化的 finding。
2. 已完成：CI 檢查（刪除範圍的使用者比對、差異清單與它的雙向測試）。差異清單在第 6 步刪除。
3. 切換前的準備：§7 列的前置工作；產生器容器（驗證端點、狀態端點）；Helm chart 加容器、socket、policy 檔掛載與資源設定。
4. 切換：產生器改讀解析結果 JSON；tenant-api 寫入改問產生器容器；`--policy-unavailable-open` 移除。
5. da-guard：加上「未判定」狀態與 exit code；`guard-defaults-impact.yml` 加跑產生器。
6. 刪除：Go 端與 Python 端的模仿程式、vendored yaml.v3 的兩段 patch、扁平鍵 `_routing_receiver_type`、差異清單與 `make pre-tag` 的到期檢查；在 main 上重測 #2759 等固定語料後關票。

第 4 至 6 步在同一個版本發布（§7）。

## 附錄：實作對照

給實作者的程式碼位置，讀決策時可略過。

| 本文用語 | 程式碼 |
|---|---|
| Python 呼叫 Go binary、讀 JSON 的現有機制 | `scripts/tools/_lib_tenant_values.py`（`served-values`／`effective`） |
| Python 端的 YAML 讀取與檔案選擇函式庫（改讀 `parse-tree`） | `scripts/tools/_lib_io.py`、`_lib_confd.py`、`_lib_yaml_keys.py` |
| Python 端模仿 yaml.v3 的 loader | `_lib_yaml_keys.ExporterKeyLoader`、`_lib_io` 的 `StrictExporterKeyLoader` 系列、`describe_tenant` 的 `_GoKeyLoader` |
| 寫 conf.d 的工具（PyYAML 產生文字；保留） | `_lib_io` 的 `StrictRewriteLoader`、`strict_load_for_rewrite`／`load_yaml_file_for_rewrite` 與 `_lib_yaml_keys.dump_for_rewrite` 的呼叫端（例如 `patch_config`、`deprecate_rule`、`da_assembler`）；以 `yaml.safe_dump` 產生新檔的 `scaffold_tenant` 等 |
| 產生器另外讀的 policy 檔名 | `_grar_parse.py` 的 `_POLICY_FILENAMES`；exporter 不讀巢狀 `_` 檔的說明在 `flat_build.go` |
| portal 只做預覽的解析 | js-yaml 經 `tools/portal/src/interactive/tools/_common/validation/yaml-parser.js`；手寫的逐行解析 `tools/portal/src/interactive/tools/config-lint/lint.js` |
| 今天擋下重複鍵、多份文件的程式（刪除前由 `parse-tree` 接手） | Python 的 `RejectDuplicateKeys`；Go 的 `pyyamlcompat.FindDuplicateKey`、`routingpolicy` 的 `firstDocument`／`errMoreDocuments`、da-guard 的 `pyyaml_duplicates.go` |
| Go 端模仿 PyYAML 讀法的程式 | `pkg/pyyamlcompat`；`pkg/routingpolicy` 的 `pyResolve` 與打開 `SpacesOnly` 的 policy 解碼器 |
| Go 端模仿產生器語意的程式 | `pkg/routingpolicy` 的 `shapeWork`、`ParseDomainPolicies`、`UnmarshalPolicy` |
| vendored yaml.v3 的修改 | `third_party/yaml.v3`：只認空格（刪除）、非特定 tag（刪除）、大型 mapping 的重複鍵檢查（exporter 使用，保留）；釘值測試 `tests/ops/test_vendored_yaml_v3.py` 同一支 PR 更新 |
| 非特定 tag 影響的 exporter 讀法 | 合併鍵判斷；`custom_alert.go` 的 `validateSloObjective` |
| tenant-api 現在的寫入判定 | `judgePutBody`；PR 模式在 `WritePRChecked` 的閉包內重判 |
| 批次 patch 由 Go 重新輸出 | `mergePatchYAML` |
| 「讀不了的檔當成空」的舊行為（將刪除） | `tenantBlockOnDisk` |
| 刪除範圍的使用者比對（CI 檢查） | 宣告表 `tests/shared/phase2_deletion_scope.json`；比對它的測試 `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| 只有 Go 有的 da-guard 檢查 | `internal/guard/types.go` 減去 `tests/shared/routing_policy_parity_matrix.json` |
| 差異清單（第 6 步刪除） | `tests/shared/reader_divergence_catalog.yaml`；Go 端判讀快照 `tests/shared/merge_key_go_verdicts.json`；`tests/shared/test_reader_divergence_catalog.py`；`make pre-tag` 的 `reader-divergence-expiry` |

## 參考

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)：語意層的設計討論、量測與外部審查紀錄
- [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)：切換設計與寫入判準的拍板紀錄、反例與量測
- [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858)：語法層只留一個解析器的追蹤票
- [ADR-023 tenant-api 寫入平面：單一寫者不變式](./023-write-plane-single-writer-invariant.md)
- [ADR-037 config-diff 改為比較渲染結果](./037-config-diff-compares-rendered-output.md)
- 業界做法：
  - 只解析一次、驗證交給權威端：[Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/)。
  - 把解析工具放進使用者的 pod：[Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/)。
  - 驗證端失效時拒絕而不放行：[Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/)。
