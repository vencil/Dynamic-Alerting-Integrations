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
updated_at: 2026-10-23
---

# ADR-036: conf.d 只解析一次：YAML 由 Go 讀，路由與 policy 由產生器判

> **Language / 語言：** **中文 (Current)** | [English](./036-single-parser-effective-config.en.md)

## 狀態

✅ **Accepted**（2026-10-09 起草；以下各次修訂都經 owner 核可或由 owner 決定）。

- **2026-10-11 修訂**：YAML 語法層也只留一個解析器，凡是用來判定設定內容的讀取都讀 Go 解析出的 JSON；寫入後整棵樹通過產生器檢查才寫入；不輸出整理好的設定 JSON；移除 `--policy-unavailable-open`；git 為必要。
- **2026-10-11 再修訂**：訂定 `parse-tree` 的輸出外層、封閉的「讀不了」清單與固定語料（§2）、產生器容器不可用時的錯誤碼（§5）、da-guard「未判定」的 exit code（§9）。
- **2026-10-12 修訂**：產生器容器跨請求快取解析結果，每次判定在有記憶體與時間上限的獨立程序裡跑，並訂定逾時與「拒收該筆」和「容器不可用」的分界（§3）；批次改為全有全無，在暫存的 git worktree 裡判定與 commit（§4）。
- **2026-10-14 修訂**：解析快取的 key 改用檔案內容的 SHA-256，不再用 git 的 blob SHA（§3）。
- **2026-10-15 修訂**：從 git 物件取檔時，不是一般檔的項目（symlink、gitlink 等）一律判為 `not_regular_file`（§3）。
- **2026-10-15 修訂**：批次 patch 與 custom-alerts 的合併改以合併後的不變式把關：編輯以外的部分，合併結果的節點寫出來要與原檔逐一相同（§4）。
- **2026-10-16 修訂**：「未判定」的範圍擴大到樹上有只由產生器判的 `_` 鍵（§9），配合 [ADR-038](./038-unread-keys-judged-by-readers.md)。
- **2026-10-20 修訂**：產生器已改讀 `parse-tree` 的結果，§2、§9 刪去描述它先前以 PyYAML 讀取的句子；決策不變。
- **2026-10-20 再修訂**：Go 端也只留一個判定與一個寫者，conf.d 以外的 YAML 以它的消費者為準，刪除遷移稽核指令；細節見 §2、§3、§8、§9、§11、§12 與 §7 的順序。
- **2026-10-21 修訂**：tenant-api 的寫入與它自己讀的管理設定改經共用判定（§7 步驟 7），§2 改寫為現況，並寫明 RBAC 設定不論放在哪裡都經判定；決策不變。
- **2026-10-21 再修訂**：刪除 vendored yaml.v3 的「非特定 tag」patch（§7 步驟 8、§11）；§7 說明它排在步驟 8 的段落改寫為現況；決策不變。
- **2026-10-21 第三次修訂**：`parse-tree` 的每一種模式都輸出同一份逐路徑索引，每個路徑是不是設定檔由 Go 判定，格式改為 `da-guard.parse-tree/v2`（§2）；§7 步驟 9 改寫；§3「快取的去留」寫明量測的是一次完整判定，包含 served-values。
- **2026-10-22 修訂**：vendored yaml.v3 的基底升級為上游 `go.yaml.in/yaml/v3`（版本見 §11），unhashable merge key 修正改由上游提供（§4 的例子、§7、§11、「已知限制與風險」）；決策不變。
- **2026-10-23 修訂**：§3「快取的去留」記下結果：保留快取，冷啟動改以 `parse-tree` 的批次單檔模式整批讀沒命中的檔；後果段的「快取的去留未定」改為「快取保留」。

## 摘要

**問題**：同一份 conf.d（租戶設定目錄）被兩個 YAML 解析器各讀一次：Go 端（exporter、tenant-api、da-guard）用 yaml.v3，Python 端（路由產生器與各工具）用 PyYAML。兩者對同一份檔常讀出不同結果，雙方都寫了大量程式去模仿對方，修了多輪仍對不齊。產生器改讀 Go 的結果之後，Go 端自己仍有不只一個「讀不了」的判定，寫 conf.d 的 Python 工具仍以 PyYAML 讀寫，conf.d 以外的 YAML 也由 PyYAML 模仿別的讀者（見「問題」）。

**決定**：

1. **YAML 只由 Go 解析。** da-guard 新增一個指令，把整棵 conf.d 讀成一份 JSON：選了哪些檔、每份檔的內容、哪些檔讀不了。Python 端（包含路由產生器）判定設定內容時不再自己解析 conf.d 的 YAML，改讀這份 JSON。
2. **Go 端也只有一個判定。** exporter、tenant-api、da-guard 載入 conf.d 時，都先經產生這份 JSON 的同一個套件判定哪些檔讀得了；它判為讀不了的檔，exporter 不服務（§2）。哪些檔名是租戶檔、租戶 id 是什麼，也只在 Go 有一份實作。
3. **寫 conf.d 也只有一個寫者。** 改寫既有檔與產生新檔都由 da-guard 完成，Python 工具只送「要改什麼」（§2）。
4. **語意各只有一個實作。** 路由、domain policy、租戶清單的語意只由產生器（Python）判定；閾值與 exporter 送出的值只由 exporter（Go）判定。
5. tenant-api 的 pod 加一個容器跑產生器，每一筆會改變檔案內容的寫入都先問它。tenant-api 不再自己判定路由與 policy。
6. **conf.d 以外的 YAML，以它的消費者為準。** 檔案有 Go 或其他程式在讀、照它行事時，Python 端以那個消費者的讀法取得內容，不自己用 PyYAML 讀（§12）。
7. **在 main 上依序合併，每一步合併後 main 都可以發布。** 順序見 §7：tenant-api 先改問產生器容器，da-guard 接著不再判定路由與 policy，然後刪除 Go 端的模仿程式，產生器與 Python 工具再改讀 JSON，所以不論哪一個 commit 被發布，都不會出現「產生器已用 yaml.v3 讀、Go 端仍模仿 PyYAML」的版本；之後 Go 端的讀取者改經共用的判定、寫入改由 Go、conf.d 以外的 YAML 改以消費者為準。不設新舊並行期，也不設切換開關。

**對使用者與維運的影響**（都是 breaking change）：

- 依舊版 YAML 規則成立的寫法，意思可能改變，部分檔案會變成讀不了。例如路由裡的 `send_resolved: yes`，產生器過去讀成布林 `true`，改由 yaml.v3 解析後是字串 `"yes"`。
- exporter 不再服務 `parse-tree` 判為讀不了的檔（§2）。今天 exporter 照讀、之後整份不服務的寫法包括一個檔裡有多份文件、`!!binary` 這類白名單外的型別標記、檔案中段的 BOM 等。升級前在 CI 以新版 da-guard 驗自己的 conf.d：新版回 exit 3 的檔，就是新版 exporter 不服務的檔（§8）。
- 寫 conf.d 的工具改由 yaml.v3 輸出：空行可能被壓掉、行尾註解的對齊與引號樣式改為 yaml.v3 的寫法；註解比現在保留得多（§2）。
- 讀 conf.d 的 Python 工具需要 da-guard。da-tools 映像已內附；本機的 pre-commit 會以 Go 現場建置。
- tenant-api 多一個容器（da-tools 映像）。容器或部署端 policy 檔不可用時，所有會改變檔案內容的寫入回 503（包括只改閾值的），沒有「不可用時照樣放行」的開關。
- 寫入後的整棵樹必須通過產生器的檢查才寫入。樹上任何地方有會擋下的問題，經 tenant-api 的寫入都會被拒，直到修好（§4）。
- 批次寫入改為全有全無，在 tenant-api 改問產生器容器之前單獨發布（§4）。
- tenant-api 必須搭配 git，不再支援沒有 git 的部署。
- 過去產生器只因 PyYAML 的限制而整份拒收的檔，現在照 yaml.v3 讀出內容（§2）。
- 只跑 da-guard 的 CI 不再涵蓋路由與 policy，要另外加一步 `da-tools generate-routes --validate --strict`。

**不可協商的驗收條件**：

- 任何讀取端都不得比產生器寬鬆：不能讀出產生器丟掉的 policy，也不能漏掉產生器會執行的 policy。tenant-api 的寫入判準就是產生器自己的結果（§4），所以這一條對 tenant-api 由定義保證。
- `parse-tree` 的「讀不了」是封閉清單（§2）：只列各讀取端可能讀出不同意思的寫法，清單外一律照 yaml.v3 讀，包括過去產生器只因 PyYAML 的限制而拒收的檔。
- 同一份檔，每個 Go 讀取端判定「讀不了」的結果都相同：它們用的是同一個套件、同一份規則（§2）。

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

### 產生器改讀 Go 的結果之後還剩的落差

產生器與讀 conf.d 的 Python 工具改讀 `parse-tree` 的 JSON 之後，同樣的問題仍以下面幾種形狀存在：

- **Go 端自己有不只一個判定。** `parse-tree` 的「讀不了」清單只有產生器在用。exporter 每次載入時，同一份檔以 yaml.v3 解碼進有型別的結構好幾次，不經這份清單；tenant-api 另有自己的形狀判定（例如允許只有 `---` 的尾端空文件）。2026-10-20 在 main 上實測 9 種寫法，`parse-tree` 全部判為讀不了：
  - exporter 照讀的 7 種：一個檔裡有多份文件（第二份文件裡的租戶被靜默丟掉）、`!!binary`、`!!set`、檔案中段的 BOM、null 鍵（`~:`）、非特定標記 `!`、只有 `---` 的尾端文件。
  - 另 2 種兩邊都拒收或報錯：重複鍵（兩邊都拒收）、非純量鍵（exporter 報解碼錯誤而不是 panic，靠的是 vendored yaml.v3 的 unhashable merge key 修正，見 §11）。

  所以同一份檔可能 exporter 照讀、產生器拒收。
- **檔名規則在 Go 有 4 份實作**（哪些檔名是租戶檔、檔名怎麼對應租戶 id），Python 端還有一份名稱述詞，靠對照表維持一致。
- **寫 conf.d 的 Python 工具是第三個讀者。** 它們以 PyYAML 讀原檔、改、再寫回；為了讓它讀得像 yaml.v3，累積了約 27 輪補丁，而且改寫會丟註解。以 repo 內 66 份 conf.d 檔做讀入再寫出的樣本（2026-10-20）：經 yaml.v3 的節點樹，53 份與原檔相同，其餘的差異只在空行與行尾註解對齊；經 PyYAML，25 份與原檔相同，19 份的註解全部丟失。
- **conf.d 以外的 YAML，權威讀者不是 yaml.v3。** Kubernetes manifest 的讀者是 kubectl（以 sigs.k8s.io/yaml 解碼，採 YAML 1.1 的規則），它把 `name: no` 讀成布林 `false`，`parse-tree` 讀成字串 `"no"`；Alertmanager 以 yaml.v2 讀設定。Python 端用 PyYAML 讀這些檔，又是在模仿另一個讀者。repo 已有先例：`da-crdecode` 直接問 sigs.k8s.io/yaml，而不是在 Python 裡模仿它。
- **遷移稽核比對的「今天」是從沒發布過的讀法。** 它以 PyYAML 加上模仿 yaml.v3 的那層 loader 當作「今天」，但已發布的 da-tools（`tools/v2.9.0`）沒有那層 loader。客戶升級前真正要知道的是「新版拒收什麼、輸出差在哪裡」。

## 決定

### 1. 誰解析什麼

| 層次 | 唯一的實作 | 其他工具怎麼取得 |
|---|---|---|
| YAML 語法：選哪些檔、每份檔的內容、哪些檔讀不了 | Go 的 `parse-tree` 套件（yaml.v3） | Python 讀 da-guard 輸出的「解析結果 JSON」；exporter、tenant-api、da-guard 在程序內呼叫同一個套件 |
| conf.d 的檔名規則（哪些檔名是租戶檔、對應哪個租戶 id） | Go（一份實作） | Python 讀 `parse-tree` 的檔案清單 |
| 寫 conf.d：改寫既有檔、產生新檔 | Go（da-guard 的 `write`） | Python 工具送「要改什麼」 |
| 路由（`_routing_defaults`、`routing_profiles`、租戶的 `_routing`）、domain policy、租戶清單的語意 | 路由產生器（Python） | 讀產生器輸出的 finding（產生器回報的每一筆問題，見 §4）與狀態端點（§5） |
| 閾值與 exporter 實際提供的值 | exporter（Go） | 不變：讀 `da-guard served-values`／`effective` 的 JSON |
| conf.d 以外、有其他程式在讀的 YAML | 那個消費者 | 以消費者的讀法取得（§12） |

「閾值」那一列是現成的先例：閾值早就是「一個實作，其他工具讀它的輸出」。本 ADR 把同樣的做法套到 YAML 語法層、檔名規則、寫入與路由、policy 語意。

### 2. 解析結果 JSON

da-guard 新增子命令 `parse-tree`，輸入一個 conf.d 目錄，輸出一份 JSON。另有單檔模式，用於寫入前的讀回確認（見本節「寫 conf.d 檔」）；只列檔模式（`--list-only`），不讀任何檔的內容；名稱模式（`--list-only --names`），只依名稱分類不在磁碟上的檔名（例如 ConfigMap 的鍵）。四種模式輸出同一種文件。

**每個路徑一筆**：文件的 `entries` 對走訪記下的每個路徑各有一筆（走訪對一個路徑只留一筆紀錄：檔名像設定檔、但列不出內容的目錄是 `unlistable_dir`，不會另外再記成 `dir`；走訪會走進去的每個目錄都有一筆，讀取端不必自己列目錄），整棵樹模式與只列檔模式由同一段程式產生，所以兩者對同一棵樹的每一筆逐欄相同，差別只在讀內容才發現得到的問題。每一筆寫出全部欄位，沒有值的是 `null`：

- `path`、`depth`（根目錄到它之間的目錄層數）、`class`（名稱決定的分類：`tenant`、`defaults`、`platform`，或 `directory`；單檔模式與名稱模式另有 `hidden`、`not_config_name`，走訪不會列出這兩類）、`tenant_named`（檔名帶的租戶 id；exporter 服務的租戶仍取自檔內 `tenants:` 的鍵）、`defaults_document`（檔名以 `_defaults` 開頭的平台預設文件，以 platform-defaults schema 驗證）、`config_extension`（名稱帶設定檔副檔名 `.yaml`／`.yml`，不分大小寫；隱藏的名稱、名稱像設定檔的目錄也照名稱回答）。
- `readers`：誰讀它（`exporter`、`generator`）；沒有讀取端讀時是空清單，`not_read` 寫原因。名稱模式沒有 `readers`，`not_read` 只會是 `unselected_defaults_carrier`：給定的名稱當成同一個目錄，選法與走訪相同，落選的載體標這個原因、選中的寫在 `defaults_carriers` 的 `.`。
- `role`：這個路徑是什麼，由 Go 判定，讀取端照讀、不自己組合：
  - `config`：設定檔。讀得了或讀不了都算；讀不了時 `problem` 寫原因，讀取端讀它時明確失敗。
  - `dir`：走訪會走進去、也列得出內容的目錄。檔名像設定檔的目錄（例如 `x.yaml/`）也是 `dir`——exporter 照樣走進去讀它底下的檔——另帶 `problem`（`not_regular_file`）說明它的名稱像檔案。
  - `not_a_file`：exporter 的走訪在這裡碰不到一般檔，也不會走進去：指向目錄的 symlink、FIFO 等特殊檔，以及 stat 找不到東西（懸空連結、迴圈連結、超過 40 層的連結鏈）。
  - `unlistable_dir`：列不出內容的目錄（根目錄為 `.`），它底下的檔都沒看到，所以這份文件不完整。
  - 判定是白名單：只有上面列的情況是 `not_a_file`，其餘任何失敗（權限不足、EIO、名稱過長、之後新增的原因）一律是 `config`。反方向的預設會讓一份 stat 被權限擋下的租戶檔只留一行 WARN 就從清單上消失。
- `problem`：讀它會失敗的原因，`{kind, message}`（見下方「讀不了的檔」）。
- `verdict`：da-guard 的驗證（不帶 `--scope`）會怎麼列這個路徑，由 Go 從驗證自己的清單算出：`unservable`（結束碼 3 列出：exporter 載入時丟掉或讀不到，無法服務它）、`tree_refused`（結束碼 4 列為 `unreadable_outside_run`：產生器因它拒收整棵樹）或 `null`。讀取端要在驗證列出的每個路徑上失敗時，讀這一欄，不從 `role` 與 `problem` 自己組合——exporter 解碼拒收的檔可能完全沒有 `problem`（例如 `tenants:` 底下是清單）。只列檔模式不讀內容，只知道走訪那一半：內容被拒收的檔在那裡是 `null`；單檔模式只對 `--path` 本身給完整判定，不載入整棵樹。子目錄裡未被選中、內容壞掉的 `_defaults` 載體會同時是 `unselected_defaults_carrier` 與 `unservable`。`verdict` 只回答單一路徑：每筆都是 `null` 不等於驗證通過（同一租戶兩處宣告、路由內容都不落在路徑上），要整體結論就跑驗證。

範例：一棵樹有 `t1.yaml`、`_defaults.yaml` 與 `_defaults.yml`、語法錯的 `team/t2.yaml`，以及指向不存在檔案的 `gone.yaml`。`da-guard parse-tree --list-only --config-dir conf.d` 輸出（節錄三筆，各佔一行）：

```json
{"path": "_defaults.yml", "depth": 0, "class": "defaults", "tenant_named": null, "defaults_document": true, "config_extension": true, "readers": [], "not_read": "unselected_defaults_carrier", "role": "config", "problem": null, "verdict": null}
{"path": "gone.yaml", "depth": 0, "class": "tenant", "tenant_named": "gone", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "not_a_file", "problem": {"kind": "read_failure", "message": "the path cannot be statted (stat_error)"}, "verdict": "unservable"}
{"path": "team/t2.yaml", "depth": 1, "class": "tenant", "tenant_named": "t2", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "config", "problem": null, "verdict": null}
```

整棵樹模式對同一棵樹給出同樣的三筆，只有 `team/t2.yaml` 多了 `"problem": {"kind": "syntax_error", "message": "yaml: line 2: did not find expected node content"}`，`verdict` 為 `"unservable"`，另外帶 `files` 與 `rendered_by`。`gone.yaml` 與一份權限不足、stat 失敗的檔 `problem` 相同，只有 `role` 分得出前者不是檔。

**選了哪些檔**：`readers` 標出每個讀取端實際會讀的檔。

- exporter 的檔案選擇規則（副檔名、大小寫、隱藏檔、子目錄），包含每個目錄最後採用哪一份 `_defaults` 檔。
- 產生器另外讀、exporter 不讀的檔：policy 檔與 routing profile 檔（`_domain_policy.yaml`、`_routing_profiles.yaml`、`_profiles.yaml`，包括子目錄裡的與 `.yml` 拼法）。同一份檔兩種拼法並存時，沿用產生器現行的處理。

Python 端不再有自己的一份規則。兩邊選檔的差異列進固定語料。

**檔名規則只在 Go 一處**：哪些檔名算租戶檔、檔名怎麼對應租戶 id，Go 端只留一份實作，exporter、tenant-api、da-guard 共用。只需要檔案清單、不需要內容的工具，用 `parse-tree` 只列檔、不解析內容的模式；已經拿到整棵樹文件的工具直接讀它的 `entries`，不再另外列一次檔。Python 端的名稱述詞，以及拿它比對 Go 規則的測試一併退場；名稱對照表本身保留，作為 Go 測試的 fixture。

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

**讀不了的檔**：該路徑那一筆的 `problem` 是 `{kind, message}`，`files` 裡沒有它的內容。`kind` 是固定代號，下列每一種情況各有一個（共 13 個，標在括號裡），讀取端依它分類；`message` 是給人看的說明。下列都算讀不了，而不是照 yaml.v3 讀出的結果：

- 讀取失敗（`read_failure`）、不是一般檔（`not_regular_file`）、不是合法的 UTF-8（`invalid_utf8`）、語法錯誤（`syntax_error`）。
- 頂層不是 mapping（`top_level_not_mapping`）。空檔視為空 mapping，不在此列。
- yaml.v3 解碼時會報錯的一切情況（`decode_error`），包括別名展開超過上限、顯式型別與原文對不上（`!!int abc`）、合併鍵的值不是 mapping（`<<: 1`）。PyYAML 會把其中一部分照讀（例如 `!!bool yes` 讀成 `True`），之後一律讀不了。
- 一個檔裡有多份文件（`multiple_documents`，以 `---` 分隔）。yaml.v3 只讀第一份，其餘靜默忽略；照抄等於讓 `---` 之後的 policy 失效。
- 同一個 mapping 裡有重複的鍵（`duplicate_key`），以展開別名後的鍵原文比對。yaml.v3 對「別名當鍵」造成的重複會靜默取最後一個值，所以這一條要 `parse-tree` 自己檢查。
- 鍵被判為 null（`null_key`：`~:`、`null:`、空鍵），或鍵不是純量（`non_scalar_key`：`? [a]`、`? {c: d}`）。原文輸出會讓 `~` 與 `"~"` 分不出來；yaml.v3 對同一個 mapping 裡的 `~:` 與 `null:` 也會靜默取最後一個；產生器與 exporter 過去對 null 鍵的處理也不同。
- 型別標記不在白名單內（`tag_not_allowed`）。純量只收 `!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!timestamp`；mapping 與序列只收沒有標記或標 `!!map`／`!!seq` 的。`!foo`、`!!python/...`、`!!binary`、`!!set`、`!!omap`、`!!merge` 都算讀不了：yaml.v3 會把其中幾種當成普通字串讀出，與作者或 PyYAML 的原意不同。
- YAML 的非特定標記 `!`（`non_specific_tag`，例如 `! "x"`、`! "<<"`）。它不在白名單內，任何時期都算讀不了：把 `! "<<"` 當成合併鍵或當成字面上的鍵，都可能靜默地多出或少掉 policy。`parse-tree` 從每個節點位置的原文判斷有沒有 `!`，不看節點上的標記，所以不依賴 vendored yaml.v3 的「非特定 tag」patch；patch 刪除之後，`! "x"`、`! "<<"`、`! |`、`! 1`、`! {…}`、`!<!>` 仍判為讀不了。固定語料涵蓋這些寫法。
- 檔案開頭以外的位置出現 BOM（`bom_not_at_start`；U+FEFF，一種標示編碼的不可見字元）。BOM 跨過第 512 byte 時，yaml.v3 會吃掉下一個字元（實測 `domain_policies` 讀成 `omain_policies`，整份 policy 靜默失效）；在其他位置時，可能讓鍵或值帶上看不見的字元。為了不必逐一分辨，檔案開頭以外出現一律判為讀不了。

**外層**：`schema` 為 `"da-guard.parse-tree/v2"`，另帶 `da_guard_version`、`mode`（`tree`、`file`、`list`、`names`）、`defaults_carriers`（每個目錄採用的那一份 `_defaults` 檔）與 `entries`；整棵樹模式與單檔模式另帶 `files` 與 `rendered_by`。讀取端遇到其他 `schema` 值、或一筆少了欄位，整份當成不可用，並提示 da-guard 比工具舊。新增欄位不換 `schema` 值，改變讀取端依賴的形狀才換，與 `served-values`／`effective` 的慣例相同。

**Go 端的讀取者也照這份判定**：判定哪些檔讀得了的程式是一個 Go 套件，放在 exporter、tenant-api、da-guard 都能引用的位置。三者載入 conf.d 時都先經它，再各自把讀得了的檔解碼成自己需要的結構。tenant-api 自己解碼的 YAML 也先經它：寫入的本文、整檔寫入要取代的原檔，以及它自己讀的管理設定（`_tenant_orgs.yaml`、RBAC 設定、群組、視圖、聯邦白名單與各租戶子集合、AccountID 帳本）。

- RBAC 設定不論放在 conf.d 內外都經判定：它只有 tenant-api 一個讀者（§12），依所在位置分兩種讀法，等於同一份檔有兩個 loader。
- 群組、視圖、聯邦白名單、子集合與 AccountID 帳本已存在 tenant-api 的狀態儲存裡，不在 conf.d 內；內容來自升級時從 conf.d 原樣匯入的舊檔，或 tenant-api 自己寫出的文字，同樣經判定。
- 被判為讀不了時，各讀取端照自己既有的「解析失敗」處置，不當成空檔（這些多半是授權邊界的輸入，空的 RBAC 或組織對照是另一種授權，不是「沒有設定」）：RBAC 設定與 `_tenant_orgs.yaml` 啟動失敗、熱重載時保留上一版；群組、視圖與白名單啟動時記 WARN 並從空設定開始、熱重載時保留上一版；改寫群組或視圖時回 500 `CONFIG_DECODE`，不以讀不了的紀錄為底重建；AccountID 帳本照讀不了的帳本處置（匯入時啟動失敗，配號與啟動檢查時回錯誤）。
- 所以判定之前的解碼結果，沒有任何 Go 讀取端拿來當設定用。判定之前仍有的解碼只用來拒收或分類（例如判斷一份租戶檔是否只宣告一個租戶、要不要對呼叫端隱藏原文），結果只會讓請求被拒或內容被隱藏，不會放行。

- 被判為讀不了的檔，exporter 沿用今天同類檔語法錯誤時的處置，log 與 metric 另外帶上 `kind`：
  - 租戶檔：這份檔宣告的租戶不服務，同一棵樹的其他租戶不受影響；記一行 WARN log，`da_config_parse_failure_total` 加一。
  - `_defaults` 鏈裡的檔與其他 `_` 開頭的平台檔：整份略過，記一行 ERROR log（「entire block dropped」），`da_config_parse_failure_total` 加一；合併 defaults 鏈時把它當成空檔，`da-guard effective` 在 `chain_parse_failed` 列出它。依賴它的租戶仍照常載入，只是少了這份檔提供的預設值：只靠這份檔的預設值才送出的閾值不再送出。
  - 實測（2026-10-21，head 上的 da-guard）：根目錄 `_defaults.yaml` 語法錯時，`served-values` 的 `parse_failed` 列出它，租戶 `t1` 仍在，但原本由它提供預設值的 `mysql_connections` 不再送出，`effective` 的 `chain_parse_failed` 是 `["_defaults.yaml"]`；租戶檔 `t2.yaml` 語法錯時，`t2` 不在 `tenants` 裡，`t1` 照常。兩種情況 da-guard 都回 exit 3。
- tenant-api 的讀取面（`/effective`、租戶列表等經 `pkg/config` 讀 conf.d 的端點）與 exporter 同一步改經這份判定。tenant-api 的寫入沒有自己的形狀規則：本文被判為讀不了時回 400，與其他本文驗證失敗相同，錯誤文字寫出 `kind` 與行號，不引用 `parse-tree` 的說明（說明會引用被拒的鍵或值，而批次 patch 驗證的是可能由多個租戶共用的整份檔）。整檔寫入要取代的原檔被判為讀不了時，視同無法解析的原檔：須有全租戶的寫入權限才能取代。
- da-guard 對這些檔回 exit 3（§9）。
- 所以同一份檔不會出現「exporter 照讀、產生器拒收」：產生器、exporter、da-guard、tenant-api 拒收的是同一批檔。

這是 breaking change：前面「問題」一節實測的 9 種寫法裡，exporter 今天照讀 7 種，之後這些檔整份不服務。範例。輸入：

```yaml
# t1.yaml
tenants:
  t1:
    mysql_connections: "80"
---
tenants:
  t2:
    mysql_connections: "90"
```

- 修訂時（2026-10-21）的 main：`parse-tree` 把這份檔判為 `multiple_documents`（「a second document starts at line 4; only one document per file is read」），exporter 照讀第一份文件，服務 `t1`，`t2` 被靜默丟掉（`da-guard served-values` 的 `tenants` 只有 `t1`）；單獨執行的 da-guard 回 exit 4，把這份檔列為產生器才判的證據。
- 之後：exporter 不服務 `t1`、`t2`，並以 metric 與 log 回報這份檔與 `multiple_documents`；da-guard 回 exit 3。修法是把第二份文件拆成另一個檔。

**「讀不了」是封閉清單**：只有上面列的 13 種情況算讀不了。它們是仍在的讀取端可能讀出不同意思的寫法；這些讀取端是 `parse-tree` 的判定本身、Go 端各讀取者在判定之後以 yaml.v3 解碼進有型別結構的那一步、寫入用的 Go 編輯指令（見本節「寫 conf.d 檔」），以及 portal 以 js-yaml 做的預覽（不作為放行依據）。清單以外的一律照 yaml.v3 讀，包括過去產生器只因 PyYAML 的限制而拒收的檔，例如：

- 同一個錨點名稱定義兩次（`a: &x 1` 與 `b: &x 2`）
- 不存在的日期（`2001-13-45`）
- flow 寫法裡以冒號結尾的值（`{b: 1:}`）
- 雙引號字串裡的 `\'`
- block 純量的標頭後面緊接 `#`（`|#c`）
- 以 TAB 分隔（`a:` 後接 TAB）

這些檔過去被產生器整份拒收，現在照 yaml.v3 讀出內容。

為什麼不讓 `parse-tree` 拒收過去產生器拒收的一切：那等於在 Go 裡重寫 PyYAML 的每一項檢查，正是本 ADR 對兩個解析器否決的做法；用兩邊同時讀隨機輸入來比對，也一再找到新的「只有 PyYAML 拒收」的類別。其中風險最高的一例是 flow mapping 裡的 `<<:`（例如租戶 `_routing` 寫成 `{<<:, receiver: {...}}`）：PyYAML 整份拒收，yaml.v3 讀成字面上的鍵 `"<<:"`（值是 null）。照 yaml.v3 的讀法，沒有任何東西被合併進來，產生器報「'<<:' is not read by the route generator」並以結束碼 1 擋下這份檔；出錯的方向是擋下，不是放行。

固定語料是 `tests/shared/parse_tree_corpus.json`，做法比照現有的 `merge_key_policy_corpus.json`：由一支 Python 測試產生，並記錄產生時的 PyYAML 版本。每一列存檔案原文、PyYAML 讀法下產生器的判定（凍結的紀錄，現在的產生器已算不出來），以及 `parse-tree` 應有的分類（讀不了並帶哪個 `kind`，或照常讀出）；測試檢查的是這個分類，封閉清單裡的每一種 `kind` 至少有一列。Go 讀取端改經共用判定之後，同一份語料也用來確認每個 Go 讀取端拒收的檔與 `parse-tree` 相同（「怎麼驗收」）。

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

輸出（節錄，省略外層欄位；以 yaml.v3 的節點逐一轉成 JSON 實測，`value` 欄位依上述規則補上）：

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

產生器讀這份 JSON，所以 `010` 是 `"010"`、`send_resolved: yes` 是字串 `"yes"`（PyYAML 會讀成布林 `True`）：所有 Python 工具看到的都是 Go 端讀到的那一份。

**Python 端怎麼取得**：沿用現有的「Python 呼叫 Go binary、讀 JSON」機制（da-tools 已用這個方式讀 da-guard 的 `served-values`／`effective`）。共用的 YAML 讀取與檔案選擇函式庫改為讀這份 JSON；各工具不再直接呼叫 YAML 解析器讀 conf.d。產生器每個用到值的地方都改用 JSON 的型別與值，不只 `send_resolved`（例如 `_severity_dedup: off` 過去讀成 `False`，現在是字串 `"off"`）。

**寫 conf.d 檔：只有 Go 一個寫者。** 改寫既有檔的工具（例如 `patch_config`、`deprecate_rule`）與產生新檔的工具（例如 `scaffold_tenant`、`init_project`、`onboard_platform`、`migrate_rule`）只決定「要改什麼」，交給 da-guard 寫：

- `da-guard write` 改寫既有檔，操作有三種：set（把某個路徑的鍵設成一個值）、unset（刪掉某個路徑的鍵）、ensure-map（確保某個路徑是 mapping，不存在就建立）。編輯在 yaml.v3 的節點樹上做，只換掉被編輯的節點，其餘節點與註解照原樣寫回。
- `da-guard write` 也以呼叫端給的有序節點產生新檔，鍵的順序照呼叫端給的。註解的位置與 yaml.v3 的註解模型一致：mapping 的鍵與序列的項目可以帶上方註解與行尾註解，檔首與檔尾（最後一個節點之後）也可以各帶一段註解；註解是版面、不是內容，讀回時不比對。
- `da-guard write` 也能把既有檔逐位元組搬到新路徑，來源之後就不存在。
- `da-guard write` 也能整份取代一個檔：寫出的文字就是呼叫端給的節點與註解，舊檔的節點與註解一律不留，也不拿舊檔當基準，所以 `parse-tree` 讀不了的舊檔也可以取代（取代就是修好它；本程序讀不到其位元組的檔仍拒，因為就位前要比對它）；權限與擁有者沿用舊檔。給整份重新產生的檔使用。
- `da-guard write` 也能刪除既有檔；路徑上沒有檔時什麼都不做。刪除的判定與其他操作相同：拿掉它之後的樹不能變壞（例如刪掉被選中的 `_defaults`，讓同目錄另一種拼法被選中卻讀不進去，就整批不寫）。
- 改寫、新建、取代、搬移、刪除可以放在同一次呼叫，整批一起判定。寫到一半失敗時，能還原的（新建、搬移、刪除）先做、失敗時還原；以 rename 蓋過原檔的（改寫、取代）無法還原，回報為已寫入。
- 這些編輯原語是一個共用的 Go 套件，從 tenant-api 合併 custom-alerts 與批次 patch 的節點編輯抽出；§4 的合併不變式（編輯以外的節點寫出來要與原檔相同）同樣適用。
- 寫入後讀回確認在 Go 內完成：一次呼叫可以帶多個檔，整批只判一次——把每個檔要寫的文字放在各自的路徑上走訪整棵樹一次，以 `parse-tree` 判那棵樹，每個檔的內容必須等於「原檔的解析結果套上這次的修改」（新檔則是呼叫端給的內容），任何一個不符就一個檔都不寫。同一批的檔會互相影響（例如同一目錄裡哪一個 `_defaults` 被選中），所以要整批判，不能逐檔對著磁碟判。一次寫入可以修好樹、不能讓樹變壞：呼叫後的樹若多出呼叫前沒有的問題（別的檔失去讀取端、任何路徑新出現驗證會點名的狀態、da-guard 驗證新出現的錯誤，例如同一租戶在兩個檔宣告），整批不寫；別處原本就有的問題不擋無關的寫入。原檔讀不了、或寫入路徑不會被任何讀取端讀到，也不寫入。
- 要改什麼、改不改，Python 工具依解析結果 JSON 判斷；它們不再以 PyYAML 讀原檔或產生文字，以 PyYAML 改寫的那一層與它依賴的模仿 yaml.v3 的 loader 一併刪除（§11）。

後果：寫出的格式改變。yaml.v3 重新輸出時，空行可能被壓掉，行尾註解的對齊被正規化，被寫出的值的引號樣式是 yaml.v3 的寫法；但註解比以 PyYAML 改寫時保留得多（樣本見「問題」一節）。檔裡有未加引號的 `1e3`、`0x1F` 這類寫法時，也不必先手動改成加引號的寫法才能寫入：編輯以外的節點照原樣寫回（§4 的不變式）。

**範圍**：本節只涵蓋 conf.d；其他 YAML 見 §12。portal 的瀏覽器端工具（以 js-yaml 解析的設定比對、路由追蹤、YAML 驗證、playground、告警預覽，以及設定檢查器裡手寫的逐行解析）只做預覽，不在範圍內：它們的結果不作為任何寫入或 CI 的放行依據，畫面上註明以 da-tools 的結果為準。

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

容器裡的結構，先講結論：一個常駐程序（一直在跑、跨請求保留狀態的服務程序）負責取檔並快取解析結果，讓每次寫入不必重新解析整棵樹（這個快取的去留依量測決定，見下方「快取的去留」）；每次判定交給一個新的子程序（由常駐程序複製出來、判完一次就結束的程序），在記憶體上限與判定期限內跑完，同一時間只跑一個。子程序出事只拒收那一筆寫入；常駐程序出事才算整個容器不可用。細節如下。

- **映像**：用現成的 da-tools 映像，裡面已有產生器與 da-guard。服務程式只用 Python 標準庫。
- **溝通**：HTTP/1.1，走 Unix socket，不開網路埠。socket 放在 pod 內的共用暫存目錄（emptyDir）。只有掛了這個目錄、而且符合 socket 檔案權限的程序能連線。chart 現有給人員身分請求用的 human socket 也是這樣做。
- **執行**：常駐程序負責從 git 取檔，並跨請求快取每份檔的解析結果，每次寫入只重新解析改到的檔。
  - 這個快取是在產生器以 PyYAML 讀檔、每次從頭解析整棵樹會超過 20 秒判定期限時採用的（「成本」）；產生器改讀 `parse-tree` 之後是否保留，見下方「快取的去留」。
  - **判定在子程序裡做**：每個請求由常駐程序 fork 一個子程序判定。fork 是作業系統複製程序的方式，複製出來的子程序一開始就擁有與常駐程序相同的記憶體內容，所以直接繼承當下的快取。批次的驗證在同一個子程序內做。
  - **一次只跑一個判定**：常駐程序同一時間只讓一個子程序判定，其餘請求排隊；排隊時間算在判定期限內（見下方「期限」）。
  - 改到的檔也在子程序裡解析，同樣受記憶體上限與判定期限約束；解析成功的結果交回常駐程序加入快取。常駐程序本身不解析寫入請求帶來的內容。
- **快取**：內容位址快取，也就是以內容本身算出的 hash 當 key 的快取。
  - key 是（檔案內容的 SHA-256，解析模式）。SHA-256 依檔案位元組算出，同樣的內容一定得到同一個值，已 commit 與還沒 commit 的內容算法相同。不用 git 的 blob SHA：SHA-1 的 repo 裡 blob SHA 可以被刻意造出碰撞，兩份不同內容共用一個 key，一份被拒的內容的解析結果就會被拿去判另一份，等於繞過判定。解析模式指用哪個解析器、輸出什麼形態（現在是 `parse-tree` 對單一檔案的輸出）。同一份內容用不同解析器讀出的結果不同，所以解析模式也放進 key。
  - 為什麼不會讀到過期內容：key 由內容本身算出，內容改了 SHA 就跟著變，舊的結果不會被新內容取用；不需要依時間或 commit 判斷快取是否失效。同一個程序裡解析器版本不變，所以同一個 key 的解析結果也不變。
  - 只存解碼後的內容結構。選哪些檔、哪些檔讀不了這類依檔案路徑而定的判定不進快取，每次判定都重算。
  - 子程序交回常駐程序的解析結果，必須是產生器修改之前的樣子（交出深拷貝或序列化後的結果）。子程序對繼承來的快取所做的修改，隨子程序結束而消失，不影響下一次判定。
  - 部署端 policy 檔不在 git 裡，不進這個快取，每次判定都重新讀。
  - **淘汰**：最新 base commit 的樹裡出現的 blob 一律保留。不在那棵樹裡的（例如被拒收的寫入帶來的內容）總量上限 32 MiB，以子程序交回的序列化結果的位元組數計，用完先丟最久沒用到的（LRU，least recently used）。只加不刪的快取會讓常駐程序的記憶體一直成長。
- **快取的去留**：快取存的是 Go（`parse-tree`）的輸出，不是第二個讀者，所以它的去留是效能決定，與「只有一個解析器」無關。兩個方向的量測（2026-10-20 的樣本）互相拉扯：
  - 冷啟動（啟動時或遇到新的 base commit 後第一次填滿快取）：單檔模式每次都要走訪整棵樹來判定選檔，所以沒命中的檔若各跑一次單檔模式，時間隨檔案數的平方成長（O(N²)）。因此沒命中的檔整批交給 `parse-tree` 的批次單檔模式（`--inputs`）：每批只走訪整棵樹一次，每份檔得到的那一行與單獨跑單檔模式逐位元組相同，快取的 key、內容與判定結果都不變。
  - 撤掉快取、每次判定都跑整棵樹模式時，5000 個租戶在子程序 192 MiB 的預算下只剩不到 8% 的餘裕，主因之一是 `parse-tree` 把整份 JSON 輸出先緩衝在記憶體裡。
  - 決定方式：先讓 `parse-tree` 串流輸出（邊產生邊寫出，不先在記憶體裡組完整份），再在含 amtool、形狀接近真實的 5000 個租戶的樹上，以子程序 `RLIMIT_DATA` 192 MiB、容器 1 CPU 量測一次完整判定。
  - 「一次完整判定」是產生器容器處理一筆寫入時要跑的全部程式：整棵樹模式的 `parse-tree`、產生器、amtool，以及 [ADR-038](./038-unread-keys-judged-by-readers.md) 推進順序第 3 項把共用判決接上產生器容器之後要跑的 served-values。撤快取前的量測必須包含 served-values；只量其中一部分，餘裕會被高估。
  - 在那之前，served-values 要先提供只算受影響租戶的方式（目前它每次算整棵樹的每個租戶），量測以那個方式為準。
  - 量測時，記憶體上限與判定期限取自 Helm chart 與程式本身，不另寫一份數字；上限或期限改了，量測跟著改。
  - 撤快取的條件，兩者都要成立：子程序與它啟動的每一支程式，峰值記憶體各自不超過 144 MiB（192 MiB 的 75%）；判定時間不超過 10 秒（20 秒判定期限的一半，另一半留給排隊與租戶成長）。成立就撤快取：容器與 CLI 走同一條整棵樹模式的路徑，本節的快取、淘汰規則與快取大小的 metric 一併移除。
  - 不成立就保留快取，只修冷啟動 O(N²) 的成因。
  - **結果**：條件不成立（量測工具 `scripts/tools/dx/measure_judgement_budget.py`，數字見 [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)），快取保留；冷啟動改走上面的批次單檔模式，批次呼叫的次數不隨檔案數增加，時間隨檔案數線性成長。冷啟動前後的量測見 [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)。
- **期限**：一個請求只有一個總期限，從 tenant-api 收到請求起算 30 秒，也就是 HTTP server 的寫入逾時（`--write-timeout`，預設 30 秒；另有固定 30 秒的請求逾時 middleware）。等寫入鎖、呼叫產生器容器、重驗、commit 都在這 30 秒之內。
  - tenant-api 每次呼叫產生器容器都帶上剩餘時間。容器的判定期限取「20 秒」與「剩餘時間減 5 秒」兩者較小的那個；那 5 秒留給 tenant-api 收到結果後核對與 commit。做法類似 gRPC 的 deadline propagation（期限傳遞）：呼叫端把自己剩下的期限傳給下游，下游不會做到呼叫端已經放棄之後。
  - 判定期限從常駐程序收到請求時起算，排隊時間也算在內。到期時不論那筆請求還在排隊或已在判定，都由常駐程序回應「拒收該筆」；已在判定的，子程序一併結束（見下一點）。剩餘時間以「還剩多久」傳遞，不依賴兩個容器的時鐘一致；判定期限比 tenant-api 的剩餘時間早 5 秒到，這 5 秒涵蓋回應的傳遞，以及上一點所說的核對與 commit。所以只要常駐程序本身正常，tenant-api 就會在自己的期限內收到這一筆的結果，不會因為等不到而把容器當成不可用。
  - 剩餘時間不夠再重驗一次（§4「驗的和寫的必須是同一份」）時，tenant-api 直接回可重試的拒收，與「拒收該筆」同類，不回 503。
  - 子程序以 `setsid` 自成一個 process group（程序群組：它和它啟動的所有程式共用一個群組編號）。判定期限到期時對整組送 `killpg`，amtool、da-guard 等子孫程序一併結束，不留殘餘。
- **資源**：分兩層設限。
  - 容器的 request 與 limit 相同：記憶體 512Mi、CPU 1。
  - 每個判定子程序另設資料段上限（`RLIMIT_DATA`，192Mi）。`RLIMIT_DATA` 是 Linux 對單一程序的記憶體上限之一；Linux 4.7 起，它計算程序私有、可寫的記憶體，也就是實際能寫入資料的記憶體，超過時配置記憶體會失敗。子程序 fork 時繼承的快取也算在內，所以快取的大小算在這 192Mi 之內。
  - 這個上限會被子程序啟動的程式繼承：amtool、da-guard 各自有同樣 192Mi 的上限。實測在這個上限下，產生器加 amtool 跑完 5000 個租戶的樹仍通過，`da-guard parse-tree`（與映像相同，以 `CGO_ENABLED=0` 建置）讀同一棵樹也通過；Python 一次要 500Mi 會得到 MemoryError；Go 程式持續配置記憶體，到上限時以 out of memory 結束。
  - 為什麼總和放得進 512Mi：一次只跑一個判定，最壞情況是子程序與它啟動的一支 Go 程式同時用滿上限，再加常駐程序，約 192＋192＋80，約 464Mi，小於 512Mi。實測 5000 個租戶時產生器峰值約 117 MiB，amtool 檢查產生出的 2.3 MB 設定約 104 MiB，都在 192Mi 之內。常駐程序的 80Mi 是估計值：Python 程序加上 5000 份最新樹的解析結果實測約 36 MiB，加上被拒內容的快取上限 32 MiB，其餘是 git 等開銷。
  - **容量上限**：最新樹的快取不設上限，隨租戶數成長；子程序 fork 時繼承它，所以它算在子程序的 192Mi 之內。快取加上一次判定的峰值超過 192Mi 後，每一筆判定都會被拒收，那時要一起調高子程序上限與容器的 limit。為了在那之前看到，常駐程序公開兩個 metric：快取大小、單次判定的峰值記憶體；接近子程序上限時發 warning 告警，門檻由實作決定。
  - 為什麼不用位址空間上限（`RLIMIT_AS`）：amtool 與 da-guard 都是 Go 程式，Go 執行期一啟動就預留大量虛擬位址（只保留位址、還沒有可寫的記憶體，不算進 `RLIMIT_DATA`）。實測 amtool 在 `RLIMIT_AS` 低於 768Mi（384Mi～700Mi 都試過）時無法啟動，遠超過子程序的記憶體預算。
  - `parse-tree` 整棵樹的輸出總量上限，與上方「快取的去留」的量測一併訂定。
- **拒收該筆與容器不可用的分界**：常駐程序有回應，而且回應指明這一筆的結果（通過、有 finding，或因判定期限到期、超過記憶體上限、輸出不完整而拒收），才算「拒收該筆」。容器不可用只有這三種情況：常駐程序出錯或沒有回應、找不到 amtool（見下方「amtool 必須存在」）、容器裡的產生器比 policy 要求的版本舊（§6）；三者都回 503（§5）。
  - 拒收該筆：HTTP status 不是 503，錯誤碼也與 `VALIDATOR_UNAVAILABLE` 不同，名稱由實作決定；因期限、記憶體或輸出不完整而拒收的，計入 metric。容器照常可用；如果把這些算成容器不可用，一筆寫入就能讓之後所有寫入都回 503。
  - 容器不可用：`VALIDATOR_UNAVAILABLE`，回 503（§5）。
- **amtool 必須存在**：amtool 是 Alertmanager 的設定檢查工具。產生器的驗證模式找不到它時只印提醒並通過，產生器容器不接受這種情況：每次判定前先確認 amtool 存在，缺了就算容器不可用。否則產生器會以結束碼 0、零筆 finding 結束，看起來像通過。

**取檔方式：產生器容器只從 git 物件取檔，不讀工作目錄。**

- 唯讀掛載 tenant-api 用的同一個 repo，依指定的 commit 取出檔案（`git ls-tree` 加 `git cat-file`）。
- 不讀工作目錄：PR 模式（寫入時開 PR，而不是直接 commit）會就地切換分支，直接讀可能讀到切換一半的狀態。
- 不用 `git archive`：它會套用 `.gitattributes` 的匯出規則，客戶若把 policy 檔標成不匯出，產生器就看不到。
- **只取一般檔**：tree 裡 mode 是 100644 或 100755 的項目才寫進判定用的樹。其他項目一律判為 `not_regular_file`（§2 的「讀不了」清單裡那一項），包括 symlink（mode 120000）與 gitlink（mode 160000，也就是子模組）；這是會擋下寫入的 finding。
  - 為什麼：從 git 物件讀到的 symlink 只是連結文字，不是它指向的檔；gitlink 沒有內容。照連結文字判，結果取決於文字碰巧長什麼樣，例如文字剛好讀得出一個 mapping。
  - 例子：`t1.yaml` 在 repo 裡是指向 `real.yaml` 的 symlink。寫入被拒，finding 是 `tenant_file_unreadable`，原因寫「is not a regular file (not_regular_file): git tree mode 120000, a symlink」，修法是把它換成內容相同的一般檔。tenant-api 寫這個路徑時本來就會寫成一般檔，所以這筆寫入本身不受影響。
  - 分類與其他讀不了的檔相同，依檔名：domain policy 檔名（`_domain_policy.yaml`／`.yml`）報 `domain_policy_unusable`，其他報 `tenant_file_unreadable`；兩者都擋下寫入。
  - 例外：路徑中有一段以 `.` 開頭的項目不判，因為所有讀取端都略過隱藏項目。
  - 不看檔名：除了上面的例外，任何不是一般檔的項目都擋，包括 `README.md` 這類不是設定檔名的項目。這比從檔案系統讀的讀取端嚴：它們依檔名挑設定檔，不是設定檔名的 symlink 直接略過、不報。刻意如此：判定只依 git mode 一條規則，不必跟「哪些檔名算設定檔」的清單保持一致；修法也只有一種，換成一般檔或刪掉。
  - 這些項目仍佔著自己的路徑，寫入照請求裡的順序逐筆套用，每筆看到的是前面各筆寫完之後的樹：
    - 寫到它底下的路徑（多深都一樣，例如 `l` 是 symlink 時寫 `l/x.yaml` 或 `l/a/b.yaml`）回 400，與寫到一般檔底下相同。
    - 本次寫入覆寫或刪除了它的路徑，它就不再判 `not_regular_file`（tenant-api 在那裡寫的是一般檔，或那裡已經沒有東西）。只有刪除之後，後面的寫入才能寫到它底下：同一批先刪 `l` 再寫 `l/x.yaml` 會通過，順序反過來回 400；先把 `l` 覆寫成一般檔再寫 `l/x.yaml` 也回 400，與寫到一般檔底下相同。
    - 它所在的目錄是目錄：tree 裡有 `d/s.yaml`（symlink）時把 `d` 寫成檔，與 `d/s.yaml` 是一般檔時一樣回 400。
  - 產生器容器為了預先填滿快取所做的讀取（啟動時、以及遇到新的 base commit 之後），只讀判定用的樹裡寫在磁碟上的檔，不產生 finding，因此不涉及這些項目。
  - 從檔案系統讀的讀取端（exporter、`da-guard parse-tree` 讀目錄、客戶 CI）照舊跟隨檔案 symlink：Kubernetes ConfigMap 掛載後每個 key 都是一條 symlink，不能禁止。所以同一棵樹，客戶 CI 可能是綠的，而 tenant-api 的寫入被拒。
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
- **批次**：一次請求、一次 commit、全有全無。整批套用後整棵樹通過才寫入，否則整批拒收；回應逐筆列出相關的 finding。中間狀態不驗，也不會進 repo。同步與非同步批次規則相同；非同步批次在執行時才判定。
  - 每個批次在獨立的暫存 git worktree（同一個 repo 另外 checkout 出來的工作目錄）裡套用、判定、commit。commit 成功後，先推進分支 ref，再更新服務中的工作目錄。推進前先記下這批要動的路徑；推進到一半中斷時（例如程序崩潰），服務啟動時與下一次寫入前把這些路徑重設成分支上的內容。只重設這批路徑，不碰工作目錄裡其他還沒 commit 的編輯。
  - 保證範圍：讀者不會看到從未 commit 的內容；中途失敗或程序崩潰時，半批內容只留在暫存 worktree。更新工作目錄的那段時間，讀者可能短暫看到新舊混合的內容，但兩者都已 commit。這與 §3 產生器容器只從 git 物件取檔、不讀工作目錄是同一個原則。
  - 改動前的批次逐筆判定：違規的那幾筆標為錯誤，其餘照常寫入。改成全有全無是 breaking change，在 tenant-api 改問產生器容器之前單獨發布：讓 git 寫入支援多檔一次 commit 的前置工作（§7）就改成全有全無，那時判定仍用 Go 端現有的檢查；改問產生器容器時只把判準換成產生器，全有全無的語意不變。
- **批次 patch 的合併**：仍由 Go 讀出檔案、套用 patch、再寫回；合併後的內容一定送驗。做這件事的節點編輯抽成共用套件，da-guard 的 `write` 也用它（§2）。
  - 合併後要符合一條不變式，否則不寫入：把原檔與合併結果都解析成 yaml.v3 的節點樹，這次編輯取代的部分以外，每個節點寫出來的樣子都要相同，包括種類、style（flow 或 block、純文字或哪一種引號）、寫出的 tag（顯式 tag 的原文，或沒寫 tag）、純量的原文、anchor 名稱，以及別名所指的節點（比對內容，不只比名稱）。文件數要相同，定義的 anchor 也要相同、順序一致。字元相同，對任何讀取端意思都相同，所以這條判準與讀取端無關。被編輯的鍵另外解碼檢查：設定的鍵是新值、刪掉的鍵不在了，因為 `<<` merge key 仍可能帶進已刪掉的鍵。註解與位置不比。
    - 任何差異都以 `TENANT_CONFIG_NOT_LOADABLE` 拒收（`PUT .../custom-alerts` 回 409；批次是該筆的 code，整批 422），`config_error` 為 `not_round_trippable`，訊息附第一個差異的位置；只有在該租戶自己的 block 裡才寫出路徑，其他位置只說「另一處」，因為那可能是呼叫者讀不到的租戶。
    - 例子：`k: [1:20]` 在別的租戶 block 裡。yaml.v3 重新輸出時會寫成 `['1:20']`，PyYAML 讀原檔是整數 80、讀新檔是字串；節點的 style 從純文字變成單引號，所以拒收。同一條不變式也擋下 folded block scalar 裡縮排較深的行被改寫、別名共用的租戶 block 連帶被改、同名 anchor 讓別名改指、`<<` 帶進的值在刪掉明寫的鍵後仍在。
    - 合併寫出的值一律用雙引號（批次 patch 的值是字串；custom-alerts 由 Go 產生的 `_custom_alerts` 裡每個字串也是），不論內容，避免任何讀取端把它讀成時間、數字或布林；唯一例外是 `_custom_alerts` 裡含換行的字串，寫成 literal block（`|`，對任何讀取端也都是字串），讓 diff 看得到逐行內容——前提是那個 block 讀回來仍是原字串（開頭是換行或 tab 的就不是），否則照樣用雙引號。重新輸出前清掉 yaml.v3 自行補上的隱含 tag，否則 `<<` 會被寫成 `!!merge <<`。
    - 已知限制：只在節點樹以外的地方不同的寫法擋不到。實測的例子是 `k:` 後面緊接 TAB：PyYAML 讀不了原檔，yaml.v3 讀成空值，重新輸出後兩邊都讀得了，等於把讀不了的檔修好了。差分測試把這一例釘住。
    - 解碼失敗時回給呼叫端的文字是固定的，只說哪裡失敗、第幾行；parser 的原文只寫進 server log，因為一份檔可能有多個租戶，原文會引用別的租戶的值。
  - 擋不到的是「沒有違規，但意思被 Go 改掉」的改寫，由離線差分測試把關：一邊是 Go 合併後交給產生器讀，另一邊是產生器讀 base 再套同一個 patch，兩邊結果要相同，同一個檔裡的其他租戶也要比。
- **驗的和寫的必須是同一份**：容器回傳它驗過的「檔案路徑、內容 hash、base commit、部署端 policy 檔的內容 hash」。tenant-api 在寫入鎖之下核對；部署端 policy 檔由 tenant-api 唯讀掛載同一份，自己算 hash 來比。不一致就重驗一次，仍不一致就拒收；寫入的位元組必須和驗過的一模一樣。
- **PR 模式驗的是分支起點**：驗的是 PR 分支所在的 base commit 加上這筆寫入。PR 合併進主幹後的結果不在 tenant-api 的判定範圍，由客戶 CI 或 merge queue 把關。
- **異常時怎麼處理**：哪些只拒收該筆、哪些算容器不可用而回 503，見 §3「拒收該筆與容器不可用的分界」。

**代價（breaking change）**：

- 樹上任何地方有會擋下的問題，所有經 tenant-api 的寫入都會被拒，直到修好為止。
- 批次全有全無：一筆不通過，整批都不寫入，修好那一筆後整批重送（§4）。
- 修復路徑：
  - 問題在某個租戶檔、而且修好它就能讓整棵樹通過時，可以經 tenant-api 修。
  - 其他情況走 git，例如平台檔、子目錄的檔、多個租戶同時違規（平台收緊 policy 後常見）。PR 模式下合併後才出現的問題也屬此類。
  - 客戶 CI 綠、tenant-api 卻判整棵樹紅時：用容器那一版產生器（版本由 §6 的 metric 讀）與部署端 policy 檔在本機重跑，找出 finding，再走 git 修。
- 為什麼可以接受：客戶 CI 用同一版 da-tools、帶同一份部署端 policy 檔時，會讓整棵樹變紅的改動進不了主幹；整棵樹紅的時候，客戶 CI 本來就紅。這等於「主幹永遠保持綠燈」，與 merge queue（合併前先在最新主幹上跑完測試的佇列）的做法相同。兩個條件缺一個，就可能出現客戶 CI 綠、tenant-api 整棵樹紅，走上面最後一條修復路徑。

### 5. policy 檔與 policy 狀態

**部署端的 policy 檔掛進產生器容器；policy 檔不可用就拒收寫入。**

- **部署端的 policy 檔**（`--policy`）：檔裡的 `allowed_domains` 限制 webhook 網址的網域。chart 加一個可選值把這份檔掛進產生器容器，判定時一併帶上；tenant-api 容器也唯讀掛載同一份，用來核對 hash（§4）。理由：這是防 SSRF（服務被拿來替攻擊者對內網發請求）的控制，要放在租戶 repo 之外；有寫入權限的人不該能改掉它。`da-tools init` 產生的客戶 CI 模板可選擇帶入同一份檔，讓客戶 CI 與 tenant-api 判的是同一份：GitHub 讀 repository 或 organization variable `DA_TOOLS_POLICY`（值是 policy 的 YAML 內容），GitLab 讀同名、File 型的 CI 變數（值是 runner 寫出的檔案路徑）；設了就在 `validate-config` 與 `generate-routes` 帶 `--policy`，沒設只印提示、不失敗。CI 這一側不是安全控制，有 repo 寫入權限的人本來就能改 workflow；它的用處是在 PR 上提早看到 tenant-api 會給的答案。pre-commit hook 不帶，因為它只掛得進 repo 本身。設定方式見 [GitOps CI/CD 整合指南 §2.5](../scenarios/gitops-ci-integration.md#25-帶入部署端-policy-檔da_tools_policy)。
- **狀態端點**：產生器容器回報部署端 policy 檔是否可用，tenant-api 讀它來產生 `tenant_api_policy_available`。這個 metric 只反映部署端 policy 檔與容器；repo 裡的 policy 檔有錯不會讓它變 0，那種情況寫入以 finding 拒收。這個 metric 有告警在讀，維持原名；目前沒有 label。
  - 讀不到狀態端點（容器不可達）時，metric 為 0，告警照樣會亮。
  - 它的說明文字把「沿用上一份可用內容」算成可用；tenant-api 改問產生器容器後不再沿用舊內容，說明文字屆時要改。
- **沒有放行開關**：部署端 policy 檔或產生器容器不可用時，會改變檔案內容的寫入一律拒收（包括只改閾值的），回 503。
  - 503 只用在這兩種情況。repo 裡的 policy 檔（平台預設、routing profile）有錯，是產生器回報的 finding，照 §4 拒收，不回 503。
  - 部署端 policy 檔不可用時錯誤碼是 `POLICY_UNAVAILABLE`。產生器容器不可用（常駐程序出錯或沒有回應、找不到 amtool、版本比 policy 要求的舊）時錯誤碼是 `VALIDATOR_UNAVAILABLE`，是哪一種原因寫在 message 裡。判定子程序逾時或超過記憶體上限不算容器不可用（§3）。
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
- policy 檔可以宣告「至少需要哪一版產生器」，容器比它舊就拒收所有會改變檔案內容的寫入；修法是維運者調高 chart 設定的 da-tools 版本。這是新增的鍵，只從部署端的 policy 檔（`--policy`）讀，不讀租戶 repo 裡的 policy 檔；讀它的是路由產生器，以及用同一份 `--policy` 判定 receiver 的 CLI（`validate-config`、`test-notification`、`explain-route`，與產生器共用同一個讀取函式），版本比它舊時一律拒絕判定；最晚在 tenant-api 改問產生器容器的那一步（§7）加上，否則較舊的產生器不認得它。
- tenant-api 以 metric 公開產生器與 da-guard 的版本。

### 7. 在 main 上依序合併

**各步依序合併進 main。** 不設新舊並行期，不設切換開關，也不維護差異清單（逐筆記錄 Go 與產生器判定不同的設定檔）。為什麼不在同一個版本一次做完，見「考慮過但否決」。

步驟 0 到 8 已完成，每一步合併後都可以發布；「非特定 tag」patch 不在步驟 4 刪，排在步驟 8（理由見下方）。

- **步驟 0：先發布，行為不變。** `parse-tree`（含單檔模式）與它的固定語料（§2）；遷移稽核指令（§8）；把與解析無關的小工具（例如租戶 id 檢查、判斷檔案所在的目錄層級、policy 檔名判斷）搬出要刪除的套件。
- **步驟 1：tenant-api 寫入改問產生器容器。** 同一步刪除 tenant-api 裡所有 Go 端的路由與 policy 判定（§11），包括判斷 `_routing` 是否為「關閉路由」字串的模仿程式。不是路由判定的檢查保留：遮罩憑證的佔位字串回寫防護、以 yaml.v3 確認 exporter 讀得了寫入內容的檢查、閾值等保留鍵的值檢查。後兩者讓 tenant-api 不會因此放行 PyYAML 讀得了、yaml.v3 讀不了的檔（例如 `"x\/y"`）；過去由 Go 端模仿套件擋下的重複鍵，改由產生器自己的重複鍵拒收擋下。同一步也移除 `--policy-unavailable-open`，並刪除扁平鍵 `_routing_receiver_type`（舊版指定 receiver 種類的單層寫法），寫入它改為回 400。
- **步驟 2：da-guard 改報「未判定」**（§9）；`guard-defaults-impact.yml` 加跑產生器。
- **步驟 3：刪除 Go 端模仿 PyYAML 與模仿產生器語意的程式**（§11），連同差異清單與 `make pre-tag` 的到期檢查。vendored yaml.v3 的「非特定 tag」patch（讓 YAML 的 `!` 標記保留在節點上的修改）這一步保留。
- **步驟 4：產生器與 Python 工具改讀解析結果 JSON。** 刪除讀 conf.d 用的、模仿 yaml.v3 的 Python loader；寫 conf.d 的工具加上寫入前確認（這個確認在步驟 10 移進 Go，§2）。Python 共用函式庫與各工具（包括直接使用共用函式庫的產生器）在這一步一起改；先切共用函式庫會讓產生器一半讀 JSON、一半用 PyYAML。

**保證**：從步驟 3 起，沒有任何 Go 程式產生路由或 policy 的判定（finding）；exporter 保留的只有查 receiver 種類是否存在。步驟 1 到步驟 2 之間，Go（da-guard）與產生器兩個判定者並存，不比今天寬鬆。產生器到步驟 4 才改用 yaml.v3 的讀法，那時 Go 端模仿 PyYAML 的程式已經刪除；所以不論哪一個 commit 被發布，都不會出現「產生器已用 yaml.v3 讀、Go 端仍模仿 PyYAML」的版本。

**為什麼 `parse-tree` 排在步驟 0**：擋下重複鍵、多份文件、白名單外型別標記的，原本有一部分是 Go 端的模仿程式。步驟 3 刪掉它們之前，`parse-tree` 必須已經把這些判為讀不了（§11）。

**步驟 5 起：Go 端單一判定、單一寫者、conf.d 以外的 YAML。** 依序合併進 main，紀律與步驟 0 到 4 相同：每一步合併後，main 都必須可以發布。

- **步驟 5：`parse-tree` 的判定移到共用位置。** 從 da-guard 內部移到 exporter、tenant-api、da-guard 都能引用的 Go 套件（`pkg/` 下），行為不變。
- **步驟 6：exporter 與 da-guard 載入 conf.d 先經這份判定**（§2），tenant-api 經 `pkg/config` 的讀取面（`/effective`、租戶列表等）同一步跟著改。exporter 不服務讀不了的檔並以 metric 與 log 回報；da-guard 對這些檔回 exit 3，不再以 exit 4 列為產生器才判的證據（§9）。這是 breaking change，升級前的把關見 §8。
- **步驟 7：tenant-api 的寫入改用這份判定**，取代自己的形狀判定；tenant-api 自己讀的管理設定（`_tenant_orgs.yaml`、RBAC 設定、群組、視圖、聯邦白名單與子集合、AccountID 帳本）同一步改經這份判定再解碼。這些多半是授權邊界的輸入，判為讀不了時照各自現有的「解析失敗」處置，不當成空檔（§2）。
- **步驟 8：刪除「非特定 tag」patch。** 這時 exporter、da-guard、tenant-api 都已經經共用的判定（理由見下方）。
- **步驟 9：檔名規則與檔案清單收斂。** Go 端 4 份檔名規則合併為一份。`parse-tree` 的每一種模式輸出同一份逐路徑索引（`entries`），每個路徑是不是設定檔由 Go 判定（`role`），只列檔模式是同一份文件少了內容。Python 端只有一個讀這份文件的地方，讀 `role`，不再自己判斷路徑是不是設定檔；名稱述詞與拿它比對 Go 規則的測試退場，名稱對照表保留作為 Go 測試的 fixture。需要檔案清單的工具改用只列檔模式，已有整棵樹文件的工具讀它的 `entries`；Python 端仍以自己的名稱規則列出 conf.d 檔案的地方一併改經 da-guard。
- **步驟 10：編輯原語與 da-guard 的 `write`**（§2）。寫 conf.d 的 Python 工具分批改為送「要改什麼」，寫入後的讀回確認移進 Go。
- **步驟 11：刪除 Python 端以 PyYAML 改寫 conf.d 的那一層**，連同它依賴的、模仿 yaml.v3 的 loader（§11）。
- **步驟 12：conf.d 以外的 YAML 改以消費者為準，加上守住邊界的 lint**（§12），並決定 tenant-api 管理設定的取得機制。§12 的做法（AST lint 列出呼叫點，白名單逐項附理由）已先用在 conf.d 讀取端，但只用在它判得準的地方：步驟 9 刪掉 `_lib_confd` 的名稱述詞之後，以同樣的閘門守住它們不再出現：`_lib_confd` 不再定義、`scripts/` 底下也沒有任何地方引用（`tests/shared/test_confd_tools_no_python_judgement.py`）；工具是否以其他方式自己判斷 conf.d 路徑，是行為問題，由行為矩陣測試回答，不靠文字比對。
- **步驟 13：刪除遷移稽核指令**（§8），同一步在漸進遷移 playbook 補上升級前檢查。
- 可與上列平行：「快取的去留」的量測（§3）。vendored yaml.v3 的基底已升級到上游 `go.yaml.in/yaml/v3` v3.0.5（§11）。

順序的理由：步驟 6、7 要引用共用的套件，所以步驟 5 先做；步驟 6 是使用者看得到的 breaking change，先做讓它單獨發布、單獨把關；「非特定 tag」patch 要等每個 Go 讀取端都經共用的判定之後才刪（步驟 8）；步驟 11 刪除改寫層之前，所有寫 conf.d 的工具都要已經改送意圖（步驟 10）。

**為什麼「非特定 tag」patch 留到步驟 8**：這個 patch 讓 yaml.v3 把 `!` 標記保留在引號字串的節點上，exporter 判斷合併鍵時靠它把 `! "<<"` 當成合併鍵，與 PyYAML 相同。沒有它，yaml.v3 丟掉標記，`! "<<"` 是字面上的鍵 `<<`，什麼都不合併。在 exporter 經 `parse-tree` 的判定之前刪掉它，等於靜默改變 exporter 照讀的檔。步驟 6 起 exporter、步驟 7 起 tenant-api 都只解碼 `parse-tree` 判為讀得了的檔，帶 `!` 的檔到不了它們的解碼；兩者都改完之後（步驟 8）才刪，main 上就沒有任何一個 commit 讓元件以沒有這個 patch 的 yaml.v3 讀到 `!`。

**步驟 1 之前要先完成的事**：

- `da-tools init` 產生的設定固定 da-tools 版本。
- tenant-api 的 git 寫入支援多檔一次 commit，同時把批次改成全有全無，單獨發布（§4）。
- 產生器容器（驗證端點、狀態端點）；Helm chart 加容器、socket、policy 檔掛載與資源設定。
- 只有 Go 有的 da-guard 檢查搬到產生器（§9）。

**容器不可用時不退回 Go**：tenant-api 的 Go 端判定在步驟 1 已刪除；而且能送寫入請求的使用者可以影響容器是否可用（例如連續送出大量需要整棵樹重算的請求），退回較寬鬆的判定等於讓請求方自己挑判定者。

客戶的 conf.d 無法取得，每一步合併前的驗證只涵蓋 owner 的樹。客戶升級前怎麼在自己的樹上看到影響，見 §8。

### 8. 遷移：升級前怎麼知道影響

YAML 改由 yaml.v3 解析後，產生器與 Python 工具看到的值，凡是兩個解析器讀法不同的，都會改變。最常見的是未加引號的 `yes`／`no`／`on`／`off`（舊讀法是布林，新讀法是字串）；`!!merge` 標記的鍵（舊讀法是合併）之後整份檔讀不了。exporter 改經共用的判定之後，也不服務 `parse-tree` 判為讀不了的檔（§2）。

**升級前的把關**，兩件事都在客戶自己的樹上、以新版工具做：

- **新版拒收什麼**：在 CI 以新版 da-guard 驗自己的 conf.d。新版 da-guard 回 exit 3 的檔，就是新版 exporter 不服務、產生器與 tenant-api 拒收的檔（三者用同一份判定，§2、§9）；報告逐檔列出 `kind`。
- **輸出差在哪裡**：以新舊兩版的 da-tools 映像各渲染一次同一棵樹，比對渲染結果（Alertmanager 路由設定等），作法與 [ADR-037](./037-config-diff-compares-rendered-output.md) 的 config-diff 相同。
- 代價：比對看得到渲染結果哪裡變了，但不會指出是哪一項讀法改變造成的；要歸因，得回頭看那一處的原文。
- 發布說明列出受影響的寫法與遷移步驟。
- 刪除遷移稽核指令的那一步，同時在 [漸進遷移 playbook](../scenarios/incremental-migration-playbook.md) 補上這兩項升級前檢查（新版 da-guard 驗 conf.d、新舊映像渲染結果比對）。

**遷移稽核指令退場**：它在客戶的樹上以兩個解析器各讀一次、列出差異，但它當作「今天」的那一側，是 PyYAML 加上模仿 yaml.v3 的 loader，這個讀法從沒在任何發布的版本裡出現過（已發布的 `tools/v2.9.0` 沒有那層 loader）。它回答的是「一個沒發布過的讀法與新讀法差在哪裡」，不是客戶要問的「新版拒收什麼、輸出差在哪裡」；它也是 Python 端以 PyYAML 讀 conf.d 的最後一處。所以由上面兩件事取代：在刪除它的那一步之前，這個指令仍在，但不建議依賴；那一步移除它（§7）。

刪除 vendored yaml.v3 的「非特定 tag」patch 不另外改變 exporter 的讀法：刪除時 exporter 與 tenant-api 都只解碼 `parse-tree` 判為讀得了的檔，而含 `! "<<"`、`objective: ! "99.9"` 這類帶 `!` 標記的檔整份讀不了，與其他讀不了的檔一樣不服務，也會在新版 da-guard 的 exit 3 裡列出。

### 9. da-guard

da-guard 新增 `parse-tree`，不再判斷路由與 policy，交給產生器的 `--validate --strict`。閾值相關的檢查不變。

- **單獨執行 da-guard 時**（例如使用沒有 Python 的獨立 binary）：
  - 報告（文字與 JSON）明示路由與 policy「未判定」，並回 exit code 4。這樣「沒判」和「判過、沒問題」可以分得開。
  - 只在樹上有只能由產生器判定的內容時才回 4：路由、policy，或只由產生器讀、以及拼錯而只有產生器會報的 `_` 鍵（判法見 [ADR-038](./038-unread-keys-judged-by-readers.md)）。沒有這類內容的樹照常回 0。
  - 同時符合多個 exit code 時，優先順序是 3 > 1 > 4（3 是有檔讀不了，1 是有錯誤等級的 finding；3 優先於 1 與現在相同）。
  - exit 3 的範圍以 [CLI 參考的 guard 一節](../cli-reference.md#guard)為唯一定義：exporter 載入時整份丟掉的檔、合併時解碼失敗的檔（`_defaults.yaml` 鏈或租戶檔），以及讀不到的檔、子目錄或路徑。exporter 改經共用的判定之後，不服務 `parse-tree` 判為讀不了的檔（§2），所以這些檔全部以 exit 3 回報，例如檔案中段的 BOM、`!!binary` 這類型別標記、多份文件。exit 4 的未判定證據裡「`parse-tree` 判為讀不了、exporter 卻照讀」的那一類（`generator_unreadable`）不再需要，併入 exit 3：產生器與 exporter 拒收的是同一批檔。只剩本次執行的 exit 3 點不到、產生器卻會整棵拒收的路徑仍列為未判定證據（`unreadable_outside_run`）：`--scope` 以外的這類檔、檔名像設定檔的目錄或指向目錄的 symlink，以及整棵樹掃不了；定義見 CLI 參考的 guard 一節。
  - exit 4 保留給路由、policy 與只由產生器判的 `_` 鍵。它的條件仍是在預測產生器會不會判；如果真實的樹幾乎都回 4，這個條件就沒有資訊量，屆時再評估是否保留。
  - 不另設旗標讓 CI 宣告「路由已由產生器判過」：`da-tools init` 產生的客戶 CI 範本本來就跑 `generate-routes --validate --strict`，不會單獨跑 da-guard。
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

| 項目 | 隨哪件事 | 處理 |
|---|---|---|
| 與解析無關的小工具（例如租戶 id 檢查、判斷檔案所在的目錄層級） | `parse-tree` 發布時 | 移到別的套件 |
| tenant-api 裡其他的 Go 端路由與 policy 判定：寫入時對路由 patch 與 receiver 的檢查、依 policy 判定租戶的路由、把讀不了的檔當成空的舊行為 | tenant-api 改問產生器容器時 | 刪除 |
| Go 端模仿 PyYAML 讀法的程式：模仿 PyYAML 解碼的套件、模仿 PyYAML 型別判定的函式、policy 解碼器打開的「只認空格」選項 | tenant-api 與 da-guard 都不再用它之後 | 刪除 |
| Go 端模仿產生器語意的程式（Go 版的路由與 policy 判定，包括判斷 `_routing` 是否為「關閉路由」字串） | 同上 | 刪除 |
| Go 版的 receiver 規格（有哪些 receiver 種類、每種要哪些欄位） | 判定的部分：與上列同時 | 刪除判定的部分；exporter 只用它查 receiver 種類是否存在，這部分保留 |
| vendored yaml.v3 的「只認空格」patch（拒收 TAB 縮排；只有上述 policy 解碼器會打開） | 與上列同時 | 刪除 |
| 差異清單、它的測試與 `make pre-tag` 的到期檢查 | 與上列同時 | 刪除 |
| Python 端讀 conf.d 用的、模仿 yaml.v3 讀法的 loader，Python 端的檔案選擇規則 | 產生器與 Python 工具改讀 JSON 時 | 刪除 |
| da-guard 的未判定證據 `generator_unreadable` | exporter 與 da-guard 改經共用判定時 | 併入 exit 3；本次執行點不到的路徑改列 `unreadable_outside_run`（§9） |
| tenant-api 自己的 conf.d 寫入形狀判定 | tenant-api 改用共用判定時 | 改用 `parse-tree` 的判定 |
| vendored yaml.v3 的「非特定 tag」patch（把 YAML 的 `!` 標記保留在引號字串的節點上，讓 `! "<<"` 成為合併鍵） | exporter 與 tenant-api 都改經共用判定之後 | 已刪除（步驟 8）：帶 `!` 的檔到不了它們的解碼，patch 沒有作用（§7、§8） |
| vendored yaml.v3 的重複鍵檢查修改（大型 mapping 的重複鍵檢查從逐對比較的 O(n²) 改用 hash 表） | — | 保留：它防的是以大量鍵拖垮解碼的阻斷服務攻擊，不是在模仿另一個解析器 |
| vendored yaml.v3 的 unhashable merge key 修正（合併鍵旁邊有非純量的鍵時，把執行期 panic 改成解碼錯誤） | 與其他各項平行 | 已刪除 patch：vendored 的基底升級為上游 `go.yaml.in/yaml/v3` v3.0.5，上游已內建同一修正；重複鍵檢查的修改疊在新基底上。升級前以固定語料（`parse_tree_corpus.json` 與 yaml.v3 讀法的測試）比對新舊基底，讀法沒有差異 |
| Go 端 4 份檔名規則；Python 端的名稱述詞與 Go／Python 名稱規則的對照表 | 檔名規則收斂時 | Go 合併為一份；Python 端刪除，改用只列檔模式 |
| 寫 conf.d 工具以 PyYAML 讀原檔、產生文字的那一層，連同它依賴的、模仿 yaml.v3 的 loader | 寫 conf.d 的工具都改送「要改什麼」之後 | 刪除（§2） |
| 遷移稽核指令 | 升級前檢查寫進漸進遷移 playbook 時 | 刪除（§8） |

各項的順序見 §7；對應的程式碼見附錄。CI 的宣告表記錄每個套件與 patch 何時可刪；每一步刪除程式的那支 PR 一併更新它。

重複鍵、多份文件、白名單外的型別標記：原本由產生器的 loader 與 Go 端模仿套件擋下（部分 Python 工具刻意只讀第一份文件）。刪除 Go 端的檢查之前，`parse-tree` 要已經把這些判為讀不了（§2），並由固定語料確認；所以 `parse-tree` 最先發布（§7）。

### 12. conf.d 以外的 YAML：以它的消費者為準

一份 YAML 的意思，由讀它、照它行事的那個程式決定。Python 工具需要讀這類檔的內容時，問那個消費者的讀法，不自己用 PyYAML 讀；PyYAML 與 yaml.v3 都不是這些檔的權威讀者。

| 檔案 | 權威讀者 | Python 端怎麼取得 |
|---|---|---|
| Kubernetes manifest、Alertmanager 設定 | kubectl（sigs.k8s.io/yaml）、Alertmanager（yaml.v2），都採 YAML 1.1 的規則 | `da-crdecode`（以 sigs.k8s.io/yaml 解碼後輸出 JSON）通用化後解碼；要支援一個檔多份文件 |
| tenant-api 讀的管理設定（例如 RBAC、群組、視圖） | tenant-api 的 loader（先經 `parse-tree` 的判定，再以 yaml.v3 解碼，§2） | 以 tenant-api 的讀法取得；機制待定，在處理 conf.d 以外 YAML 的那一步決定（例如由 tenant-api 提供驗證子命令） |
| 文件裡的 conf.d 範例 | exporter 與產生器（經 `parse-tree`） | `parse-tree` 的整棵樹模式 |
| 沒有 Go 消費者、repo 內只有 Python 在讀的檔（GitHub workflow、pre-commit 設定、mkdocs 設定、repo 內部資料） | — | 不強求，照舊 |

範例：Kubernetes manifest 裡的 `name: no`，kubectl 讀成布林 `false`，`parse-tree` 讀成字串 `"no"`。以 yaml.v3 的讀法判斷這份 manifest，就與真正套用它的 kubectl 意思不同；所以問 sigs.k8s.io/yaml。

**邊界與怎麼守住**：檔案有 Go 消費者，Python 就不得以 PyYAML 讀它。CI 以 AST lint（解析 Python 原始碼的語法樹，找出呼叫 YAML 解析器的地方）列出每個呼叫點；呼叫點所在的檔要列在白名單裡，並逐檔附上理由（例如「只讀 GitHub workflow，沒有 Go 消費者」）。

範圍：本節的工作順序見 §7。portal 以 js-yaml 做的預覽照 §2 的範圍說明，不在此列。

## 失敗時會怎樣

| 情況 | 行為 |
|---|---|
| 寫入後整棵樹的產生器結束碼不是 0 | 拒收；錯誤回應列出 finding，標出寫入前就存在的與來源 |
| 產生器容器不可用：常駐程序出錯或沒有回應，或找不到 amtool | 所有會改變檔案內容的寫入回 503，錯誤碼 `VALIDATOR_UNAVAILABLE`；`tenant_api_policy_available` 為 0（§5） |
| 判定期限到期（含排隊中）、子程序超過記憶體上限或輸出不完整，或剩餘時間不夠重驗 | 只拒收這一筆，不回 503（分界見 §3） |
| 部署端 policy 檔不可用 | 所有會改變檔案內容的寫入回 503，錯誤碼 `POLICY_UNAVAILABLE` |
| repo 裡的 policy 檔有錯 | 產生器回報 finding，照第一列拒收，不回 503 |
| `parse-tree` 讀不了某份檔 | 該檔列在「讀不了的檔」，產生器的檢查不會通過，寫入拒收 |
| tenant-api 寫入的本文，或整檔寫入要取代的原檔，被判為讀不了 | 本文：400，錯誤文字寫出 `kind` 與行號，不寫入；原檔：視同無法解析，沒有全租戶寫入權限的呼叫端回 400 |
| tenant-api 自己讀的管理設定被判為讀不了 | 照各讀取端既有的解析失敗處置，不當成空檔（§2）：RBAC 設定與 `_tenant_orgs.yaml` 啟動失敗、熱重載保留上一版 |
| exporter 載入時，某份檔被判為讀不了（exporter 改經共用判定之後） | 與今天同類檔的語法錯誤相同（租戶檔：檔內租戶不服務；`_` 平台檔：整份略過、defaults 鏈當成空檔，§2）；metric 與 log 回報檔案與 `kind`；da-guard 對同一份檔回 exit 3 |
| da-guard `write` 寫出的文字，`parse-tree` 讀回來與預期不同 | 不寫入，並說明哪個值不同 |
| 讀取端遇到不認得的 `schema` 值 | 整份當成不可用 |
| 容器裡的產生器比 policy 要求的舊 | 所有會改變檔案內容的寫入回 503，錯誤碼 `VALIDATOR_UNAVAILABLE`，直到維運者調高 chart 的 da-tools 版本 |
| 本機沒有 da-guard、也沒有 Go 可建置 | 讀或寫 conf.d 的 Python 工具與 pre-commit hook 失敗並說明原因，不會退回自己解析或自己寫。受影響的 hook 與沒有 Go 的 CI job 由實作 PR 以程式列出，CI job 改用 da-tools 映像或加裝 Go |

## 已知限制與風險

Go 端單一判定、單一寫者與 conf.d 以外 YAML 的改動完成後消失的風險：

- 同一份檔 exporter 照讀、產生器拒收（例如多份文件時第二份文件的租戶被 exporter 靜默丟掉）。
- 寫 conf.d 的工具以 PyYAML 改寫時丟註解，以及維持它讀得像 yaml.v3 的補丁一再漏接。
- 檔名規則在 Go 與 Python 多份實作之間不一致。
- 遷移稽核以一個沒發布過的讀法當基準，讓客戶誤判升級影響。

新增或仍在的風險：

- **exporter 改為拒收的升級風險**：exporter 改經共用判定之後，今天照讀的檔可能整份不服務，檔內租戶的告警停止。客戶沒在升級前以新版 da-guard 驗過 conf.d 時，第一次發現是在升級之後。緩解只有 §8 的升級前把關與 metric、log；不設「照舊讀」的開關。
- **判定成本與它的快取**：每份檔在解碼前多一次判定。exporter 以檔案內容的 SHA-256 保留判定結果（上限 8,192 筆，滿了整份清掉並記一行 DEBUG log），同一份內容不重判；冷啟動仍要逐檔判一次（實測 1000 份租戶檔的冷掃描 109 ms，全部命中時 70 ms）。一個程序遇到的不同內容多於上限時，快取一再清空、命中率趨近 0，成本退回無快取的那個數字，判定結果不受影響。讀不了的說明文字在 `parse-tree` 產生它的地方就限制在 512 bytes 以內：過長時保留開頭（行號與被拒的內容）與結尾（例如重複鍵的「first at line M」），中間省略；所以 exporter 的 log、tenant-api、da-guard 的報告與 `parse-tree` 的 JSON 是同一份文字。
- **yaml.v3 的輸出格式改變**：寫 conf.d 的工具改由 Go 寫出後，空行、行尾註解對齊與引號樣式會變，客戶的 diff 會出現與這次修改無關的格式差異（§2）。
- **快取保留**：產生器容器繼續跨請求快取解析結果，因為撤快取的條件不成立（§3「快取的去留」）。代價是快取、淘汰規則與快取大小的 metric 都要維護；冷啟動以批次單檔模式填滿，時間隨檔案數線性成長。
- **`da-crdecode` 還不支援一個檔多份文件**：§12 的 Kubernetes manifest 常一檔多份，支援之前這類檔沒有權威讀法可問。
- **升級 yaml.v3 基底帶進的寫法改變**：基底升級到上游 v3.0.5 時，以固定語料比對新舊基底，讀法沒有差異；寫法有一處不同：以換行開頭的字串寫成 literal block（`|`）時，舊基底漏寫開頭的空行，讀回少了那個換行，新基底照實寫出。影響 tenant-api 的兩處：合併 custom alerts 時，這種字串過去寫成雙引號，現在寫成 literal block，兩者讀回是同一個字串；部分更新遇到開頭有空行的 block scalar，過去因重新輸出會改掉它而拒收（`not_round_trippable`），現在重新輸出不變，照常寫入。之後再升級上游版本，同樣先以固定語料比對（§11）。
- **渲染結果比對無法歸因**：§8 的比對指出結果哪裡變了，不指出是哪一項讀法造成的。

## 怎麼驗收

- **沒有第二個 YAML 解析器**：CI 檢查列出 Python 端所有 YAML loader 的呼叫點，只允許透過解析結果 JSON；遷移稽核指令刪除之後，conf.d 沒有例外。conf.d 以外有 Go 消費者的檔由 §12 的 AST lint 與逐檔附理由的白名單守住。
- **Go 端只有一個判定**：固定語料裡 `parse-tree` 判為讀不了的每一列，exporter 都不服務、tenant-api 都拒寫、da-guard 都回 exit 3；判為讀得了的每一列，三者都照讀。
- **單一寫者**：`write` 的每種操作（改寫、新建、取代、搬移、刪除）有測試：編輯以外的節點與註解照原樣寫回，讀回確認不符時不寫入。
- **「讀不了」是封閉清單**：固定語料 `tests/shared/parse_tree_corpus.json` 每一列帶 `parse-tree` 應有的分類，測試逐列檢查；封閉清單裡的每一種 `kind` 至少有一列（§2）。
- **固定語料**另外包括現有的 `merge_key_policy_corpus.json`，加上以下各票的範例：
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)：policy 檔的剩餘落差清單。
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)：租戶檔的 `!!merge` 鍵。
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)：被覆蓋的合併來源裡有無法解析的值。
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674)：自我參照的 YAML 別名。
- **批次 patch 合併的離線差分測試**（§4）。
- **da-guard 的「未判定」**：exit code 4 與報告內容有測試，包括沒有只能由產生器判定的內容時不回 4，以及與 3、1 同時成立時的優先順序。
- **產生器容器的拒收條件**：找不到 amtool、驗過與要寫的內容重驗一次仍不一致、產生器比 policy 要求的版本舊，三者各有測試，結果都是拒收。判定子程序超過記憶體上限、判定期限在判定中到期、在排隊中到期也各有測試：只拒收那一筆、不回 503，下一筆寫入照常判定；逾時後沒有殘留的子孫程序（amtool、da-guard 等）；剩餘時間不夠重驗時回可重試的拒收；推進到一半中斷後，這批路徑會重設成分支上的內容。
- 在 CI 裡，以 chart 固定的那一組版本執行。

## 成本

本節的量測都在產生器改讀 `parse-tree` 之前，產生器當時以 PyYAML 讀檔；改讀之後的樣本見 §3「快取的去留」。

語意層（產生器，2026-10-09 實測）：

| 項目 | 量測 |
|---|---|
| 產生器處理 1000 個租戶 | 1.72 秒、38 MB；其中解析佔 1.06 秒，而且每個檔被解析兩次（可用快取省下） |
| 解析單一檔案 | 0.47 ms |
| Python 啟動 | 0.11 秒、25 MB |
| 從 git 取出 1000 個檔 | 0.21 秒、12 MB |
| 映像大小（壓縮後，amd64） | da-tools 36.9 MB；tenant-api 14.0 MB |

產生器容器（2026-10-10 實測，1000 個租戶的樹）：每個請求開新程序約 3.1 秒；同一程序加上解析快取約 1.5～1.8 秒。

寫入判定的規模（2026-10-11 實測；合成樹，每個租戶一份檔，各含 `_routing`、閾值與 `_metadata`；與上方各列用的樹不同，數字不能直接相比）：

| 項目 | 量測 |
|---|---|
| 產生器 `--validate --strict`，1000 個租戶（4 核、CPU 不設限、不含 amtool） | 約 8～9 秒 |
| 同上，5000 個租戶 | 約 42 秒 |
| 同上，1000 個租戶，單核、含 amtool | 9.4～9.8 秒 |
| 其中 PyYAML 讀檔所佔 | 約四分之三 |
| `da-guard parse-tree` 讀同一棵 1000 租戶的樹 | 不到 1 秒 |
| 5000 份檔的解析結果留在程序裡 | 常駐記憶體約多 25 MB |

這是 §3 採用常駐程序加解析快取的依據；產生器改讀 `parse-tree` 後是否保留，由 §3「快取的去留」的量測決定。

語法層（`parse-tree`，2026-10-20 實測樣本；合成樹，1000 份檔、每份一個租戶）：

| 項目 | 量測 |
|---|---|
| `da-guard parse-tree` 讀整棵樹一次 | 約 0.12 秒，輸出約 858 KB |
| Python 讀這份 JSON | 約 0.04 秒 |

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
| `parse-tree` 拒收過去產生器拒收的一切 | 要在 Go 裡重寫 PyYAML 的每一項檢查，等於又讓 Go 模仿另一個解析器；兩邊比對一再找到新的「只有 PyYAML 拒收」的類別。改為封閉清單，其餘照 yaml.v3 讀（§2） |
| 分兩階段切換（新舊並行、比對差異、條件達成才刪 Go 端） | 產品採用者少，可以接受 breaking change；並行期要維護兩份判定與差異清單，而 Go 端判定已知較寬鬆，留著它沒有好處 |
| 在常駐程序本身判定，不 fork 子程序 | 一筆過大或惡意的寫入會拖垮常駐程序，讓之後所有寫入都回 503；子程序可以各自設記憶體上限與逾時，只拒收那一筆 |
| 所有步驟在同一個版本發布 | 只能用一支涵蓋全部步驟的巨大 PR，難以審查；或改用整合分支（下一列） |
| 在整合分支上做完，再一次合併進 main | CI 只在對 main 的 PR 上執行（`ci.yml` 的觸發條件），整合分支上的每一步都沒有 CI 把關；要這樣做得先改 CI 的觸發條件 |
| 用開關在新舊判定之間切換 | 開關兩邊的程式都要維護、都要測，等於重新造出已否決的新舊並行期（上方「分兩階段切換」） |
| 輸出整理好的設定 JSON 給 tenant-api 讀 | 目前沒有程式需要讀它；policy 是否可用改由狀態端點回報 |
| YAML 改由 Python 統一解析，Go 端讀 Python 輸出的 JSON | exporter 是執行期元件，要在 Go 裡獨立運作；它依賴 Python 會讓每個 exporter pod 都要帶 Python 執行環境 |
| 把整理好的 policy 另存一份檔案 commit 進 git | 只涵蓋 policy，路由仍由兩邊各自解析。忘了重產時，tenant-api 剛啟動會因讀不到而擋下所有寫入；已在運作的則會沿用舊版 policy |
| 三方共用一套自訂的設定語法 | YAML 舊版規則會把 `no`、`0777` 這類真實租戶 id 讀成布林值或數字；而且要所有客戶改寫全部設定檔 |
| 產生器改用 C 實作的解析器 | 只對齊了把文字切成記號的那一層；落差也出在把記號組成資料的那一層 |
| 把 Python 的路由與 policy 語意完整移植到 Go | 約四、五千行，等於再造一份語意實作；只在加容器的成本無法接受時才考慮 |
| 寫入 API 只收 JSON | 只解決 tenant-api 的寫入；客戶手寫的 YAML 在 CI 與 exporter 仍要被解析 |
| da-guard 自己呼叫產生器 | 執行時要有 Python；沒有 Python 的獨立 binary 就不能用 |
| exporter 只觀測、不拒收：照舊讀 `parse-tree` 判為讀不了的檔，只加 metric 與 log | 等於保留兩個判定（exporter 讀的與產生器讀的），同一份檔仍可能 exporter 服務、產生器拒收，正是本 ADR 要消除的「各讀各的」 |
| 所有 YAML 都以 yaml.v3 讀（也就是 conf.d 以外的檔也走 `parse-tree`） | 對 Kubernetes manifest 與 Alertmanager 設定是錯的方言：它們的權威讀者採 YAML 1.1 的規則，`name: no` 在 kubectl 是 `false`，在 yaml.v3 是 `"no"` |
| 以 `import yaml` 劃邊界（凡是 import PyYAML 的檔都要列管） | 太寬：只讀 GitHub workflow 這類沒有 Go 消費者的檔也會被擋；又可被繞過：經一層包裝函式呼叫就看不到 `import yaml`。改以 AST lint 找呼叫點，並逐檔附理由（§12） |

## 實作順序

- 已完成：產生器輸出結構化的 finding。
- 已完成：CI 檢查（刪除範圍的使用者比對、差異清單與它的雙向測試）。差異清單已在步驟 3 刪除。
- 已完成，步驟 0（先發布，行為不變）：`parse-tree` 與固定語料 `tests/shared/parse_tree_corpus.json`；遷移稽核指令；與解析無關的小工具搬家。
- 已完成，步驟 1：§7 列的前置工作；產生器容器（驗證端點、狀態端點）；Helm chart 加容器、socket、policy 檔掛載與資源設定；tenant-api 寫入改問產生器容器，同時刪除 tenant-api 的 Go 端路由與 policy 判定、扁平鍵 `_routing_receiver_type` 與 `--policy-unavailable-open`。
- 已完成，步驟 2：da-guard 加上「未判定」狀態與 exit code 4；`guard-defaults-impact.yml` 加跑產生器。
- 已完成，步驟 3：刪除了 Go 端模仿 PyYAML 與模仿產生器語意的程式、vendored yaml.v3 的「只認空格」patch、差異清單與 `make pre-tag` 的到期檢查。
- 已完成，步驟 4：產生器與 Python 工具改讀解析結果 JSON；刪除 Python 端讀 conf.d 用的模仿 loader；寫 conf.d 的工具加上寫入前確認。「非特定 tag」patch 改到步驟 8 刪除。
- 已完成，步驟 5：`parse-tree` 的判定移到 `pkg/` 下的共用套件。
- 已完成，步驟 6：exporter、da-guard 與 tenant-api 的讀取面載入 conf.d 先經它；exporter 不服務讀不了的檔並加 metric 與 log；`generator_unreadable` 併入 exit 3，本次執行點不到的路徑改列 `unreadable_outside_run`。
- 已完成，步驟 7：tenant-api 的寫入形狀判定改用它；`_tenant_orgs.yaml`、RBAC 設定、群組、視圖、聯邦白名單與子集合、AccountID 帳本的讀取也改經它。
- 已完成，步驟 8：刪除「非特定 tag」patch；exporter 判斷合併鍵不再認 `!` 標記。
- 進行中，步驟 9：已完成 Go 端檔名規則收斂為一份、`parse-tree` 只列檔模式、每種模式同一份逐路徑索引與 Go 的 `role` 判定（`da-guard.parse-tree/v2`）、原本就依賴 da-guard 的 Python 工具改讀 `role`（已有整棵樹文件的工具直接讀其 `entries`，不另列一次）；Python 的名稱規則已刪除（步驟 9b-3）：`_lib_confd` 的名稱述詞（`is_reserved_name`、`is_hidden_name`、`has_yaml_extension`、`is_defaults_name`、`select_defaults_carrier`、`config_stem` 等）連同拿它們和 Go 對照的測試一起刪掉，原本的呼叫點改讀 Go 給的資料——樹裡的路徑讀索引的 `role`、`class`、`tenant_named`、`not_read` 與 `defaults_carriers`，不在樹裡的名稱（ConfigMap 的鍵、還沒寫上磁碟的檔名）問 `parse-tree` 的名稱模式——它也回答名稱是否帶設定檔副檔名（`config_extension`，隱藏的名稱也答）、以及這些名稱放在同一個目錄時選哪一份 defaults 載體（落選的標 `not_read: unselected_defaults_carrier`），Go 測試要求名稱模式與整棵樹模式在名稱對照表的每一列都答得一樣；檔名規則對照表（`tests/shared/confd_name_classification_matrix.json`）只由 Go 的 `pkg/confdname` 測試擁有，逐格驗證並守住列的涵蓋下限。`tests/shared/test_confd_tools_no_python_judgement.py` 改為守住這些述詞不再出現。尚未完成：閘門看不到、由行為矩陣測試（`tests/shared/test_confd_behaviour_matrix.py`）守住的。矩陣用的樹來自一份目錄檔，Go 端測試要求 Go 能寫出的每一種 role 與 problem 都有一棵樹；矩陣跑其中列檔層面的樹，加上兩種內容錯誤（YAML 語法錯、一個檔裡有多份文件），其餘內容錯誤只由 Go 端測試驗。每支工具以 nobody 身分執行，判準是同一棵樹上 da-guard 驗證的結束碼：0 時工具必須結束碼 0；3 或 4 時工具必須以非 0 結束、不得 traceback 或卡住，並點名驗證報告列出的每一條路徑；樹裡有不是 UTF-8 的檔名時，工具同樣必須以非 0 結束（哪一碼依工具文件化的契約：`tenant_verify` 的 2 代表驗證不一致，所以是 1）、不得 traceback 或卡住，並以跳脫形式點名那個檔名。讀 conf.d 的工具共用一個失敗即停的出口：取得這次執行的整棵樹索引時，只要有一筆 `verdict` 不是 null，就逐條 `ERROR:` 點名那些路徑、在寫入任何東西之前以結束碼 2 結束；判定本身就是輸出的 `validate-config`、路由產生器、`check_routing_profiles`、`check_confd_schema`、`config_diff` 不經這個出口，自己點名並失敗；只讀 conf.d 一個子樹的工具（`deprecate_rule --plane subtree`）同樣經這個出口：它的索引取自 conf.d root 的整棵樹、再切出子樹那一段，因為 `parse-tree` 對子樹單獨執行時會把它當 root 判。還不符的格子會逐格列為預期失敗；目前沒有。`config_history`、`drift_detect`、`run_chaos_soak` 讀的檔一律取自這份索引，所以都需要 da-guard；`migrate_to_operator` 的 `--source-dir` 是 ConfigMap 匯出檔、不是 conf.d，它的檔取自 `parse-tree` 只列檔模式，不經這個出口。矩陣之外也還沒接上這個出口的：`batch_diagnose` 以名稱篩 ConfigMap 匯出檔，它在下一步（β2）接上。它守住的是：以 stat 或前綴自己判斷 conf.d 路徑的 `describe_tenant` 的 `--replaces`、`check_path_metadata_consistency`。`offboard_tenant` 認租戶自己的檔，也改看檔名帶的租戶 id（`tenant_named`），不再看檔名前綴。generator service 把 git 樹寫上磁碟時（`_gsvc_child`）略過隱藏路徑，改由名稱模式判定哪一段是隱藏的；路由產生器與 routing profile 的 lint 判斷平台檔裡的租戶有沒有人宣告時，改讀 `TenantScope` 的兩半，可能藏著租戶檔的項目會寫進訊息，不再把有人宣告的租戶說成 orphan。
- 進行中，步驟 10：已完成編輯原語的共用套件（`pkg/confedit`，含 §4 的不變式），tenant-api 的批次 patch 與 custom-alerts 合併已改用它；以及 da-guard 的 `write`（一次改寫、新建、搬移一批檔，讀回確認後由它自己寫檔）；改既有 conf.d 檔的三支工具（`patch_config`、`threshold_govern`、`deprecate_rule`）已改為把要改什麼送給 `da-guard write`，一次改多個檔時全部送進同一次呼叫、整批判定，全部通過才寫。產生新檔的工具中，`generate_tenant_fixture`、`onboard_platform`、`migrate_rule` 已改為把要寫的資料（帶型別的值與註解）一次全部交給 `da-guard write`、整棵樹判一次，全部通過才寫（後兩支重跑同一個輸出目錄時整份取代既有的檔）；`da_assembler` 也把 CR 渲染出的檔交給 `da-guard write`（整份取代既有的檔，CR 刪除時的刪檔也經它），長跑的 controller 遇到拒寫只記在該 CR 的狀態、不結束程序。尚未完成：其餘產生新檔或搬檔的工具（`scaffold_tenant`、`init_project`、`migrate_conf_d`）仍由 Python 產生文字、經寫入前確認後寫入。
- 步驟 11：刪除 Python 端以 PyYAML 改寫 conf.d 的那一層與它依賴的模仿 loader。
- 步驟 12：conf.d 以外的 YAML 改以消費者為準；`da-crdecode` 通用化並支援多份文件；AST lint 與白名單。
- 步驟 13：刪除遷移稽核指令；漸進遷移 playbook 補上升級前檢查。
- 可平行：`parse-tree` 串流輸出與「快取的去留」的量測。
- 已完成，可平行的一項：vendored yaml.v3 基底升級到上游 v3.0.5，unhashable merge key 的 patch 刪除。

各步依序合併進 main，每一步合併後 main 都必須可以發布（§7）。

## 附錄：實作對照

給實作者的程式碼位置，讀決策時可略過。程式碼註解裡的「ADR-036 step 2」（或「第 2 步」）指實作順序裡的 CI 檢查；「實作順序第 4 步」指 tenant-api 改問產生器容器。

| 本文用語 | 程式碼 |
|---|---|
| Python 呼叫 Go binary、讀 JSON 的現有機制 | `scripts/tools/_lib_tenant_values.py`（`served-values`／`effective`） |
| Python 端的 YAML 讀取與檔案選擇函式庫（改讀 `parse-tree`） | `scripts/tools/_lib_io.py`、`_lib_confd.py`、`_lib_yaml_keys.py` |
| Python 端模仿 yaml.v3 的 loader | `_lib_yaml_keys.ExporterKeyLoader`、`_lib_io` 的 `StrictExporterKeyLoader` 系列、`describe_tenant` 的 `_GoKeyLoader`（已刪除：describe-tenant 讀 `da-guard effective`）；`ExporterKeyLoader` 與 `StrictExporterKeyLoader` 系列仍被寫入工具的改寫層使用，隨改寫層一起刪除 |
| 寫 conf.d 的工具的 PyYAML 改寫層（退場） | `_lib_io` 的 `StrictRewriteLoader`、`strict_load_for_rewrite`／`load_yaml_file_for_rewrite` 與 `_lib_yaml_keys.dump_for_rewrite` 的呼叫端（例如 `patch_config`、`deprecate_rule`、`da_assembler`）；以 `yaml.safe_dump` 產生新檔的 `scaffold_tenant` 等；它們依賴的 `_lib_yaml_keys.ExporterKeyLoader` |
| Python 端的寫入前確認（移進 Go） | `scripts/tools/_lib_write_check.py` |
| 編輯原語（共用套件） | `pkg/confedit`：節點編輯（`Doc` 的 `Set`、`Unset`、`EnsureMap`）、值的寫法（`StringNode`、`BlockStringNode`、`DropImplicitTags`）、§4 的不變式（`Verify`）；抽自 tenant-api 的 `internal/customalerts/merge.go` 與批次 patch 的 `mergePatchYAML`，兩者現在呼叫它。da-guard 的 `write` 在 `cmd/da-guard/write.go` |
| `parse-tree` 的判定 | `pkg/parsetree` |
| Go 端的檔名規則（已合併為一份） | `pkg/confdname`。其他各份已刪除，原處改呼叫它：`pkg/config/source.go` 的副檔名判斷與 `hasHiddenSegment`；`pkg/config/tree_scan.go` 的 `IsScannedFileName`、`IsScannedPath` 與走訪內嵌的前綴判斷；tenant-api `internal/confd` 的 `TenantIDFromFile`、`isReservedName`；tenant-api `handler/errors.go` 的 `findingReadable` 自己寫的副檔名判斷；da-guard `parse_tree.go` 的 `hiddenPath` |
| `parse-tree` 只列檔、不解析內容的模式 | `da-guard parse-tree --list-only`（`cmd/da-guard/parse_tree_list.go`）；走訪是 `pkg/config` 的 `ScanDirTreeNames` |
| 逐路徑索引與 `role` 判定 | `entries` 由 `cmd/da-guard/parse_tree_list.go` 的 `walkEntries` 產生（整棵樹模式與只列檔模式共用）；判定是 `pkg/config/role.go`（`UnreadableFile.Role`、`NotFile.Role`）；Python 讀取端是 `scripts/tools/_lib_parse_tree.py`（`Entry`、`entries_of`、`list_tree`、`classify_names`） |
| Python 端的名稱述詞（已刪除）與名稱規則對照表（改由 Go 獨有） | 原本在 `scripts/tools/_lib_confd.py` 的名稱判定（例如 `is_reserved_name`、`config_stem`）與拿它們比對對照表的 Python 測試；對照表 `tests/shared/confd_name_classification_matrix.json` 留作 Go 測試的 fixture（`pkg/confdname` 的矩陣測試逐格驗證並守住涵蓋下限） |
| 以 sigs.k8s.io/yaml 解碼的先例（通用化） | `cmd/da-crdecode`（目前一個檔多份文件時拒收） |
| 遷移稽核指令（退場） | `scripts/tools/ops/migration_audit.py` |
| 產生器另外讀的 policy 檔名 | `_grar_parse.py` 的 `_POLICY_FILENAMES`；exporter 不讀巢狀 `_` 檔的說明在 `flat_build.go` |
| portal 只做預覽的解析 | js-yaml 經 `tools/portal/src/interactive/tools/_common/validation/yaml-parser.js`；手寫的逐行解析 `tools/portal/src/interactive/tools/config-lint/lint.js` |
| 與解析無關、搬出的小工具 | `IsValidTenantID`（呼叫端改用 `pkg/tenantid`）、`LevelOf` 與 `IsDomainPolicyFile`（`internal/confdlayout`）；routing profiles 的檔名是 `cmd/da-guard` 的 `routingProfilesNames` |
| 擋下重複鍵、多份文件的程式 | Python 的 `RejectDuplicateKeys`；Go 端是 `parse-tree`（`pkg/parsetree`），da-guard 的未判定證據讀它。Go 端的仿製（`pyyamlcompat.FindDuplicateKey`、`routingpolicy` 的 `firstDocument`／`errMoreDocuments`、da-guard 的 `pyyaml_duplicates.go`）已刪除 |
| Go 端模仿 PyYAML 讀法的程式（已刪除） | `pkg/pyyamlcompat`；`pkg/routingpolicy` 的 `pyResolve` 與打開 `SpacesOnly` 的 policy 解碼器 |
| Go 端模仿產生器語意的程式（已刪除） | `pkg/routingpolicy` 的 `shapeWork`、`ParseDomainPolicies`、`UnmarshalPolicy`、`IsDisabled`；da-guard 的路由檢查 `internal/guard/routing.go` 與它們的 finding kind |
| vendored yaml.v3 的修改 | `third_party/yaml.v3`：只認空格（已刪除）、非特定 tag（已刪除）、大型 mapping 的重複鍵檢查（保留）、unhashable merge key 修正（已刪除：基底是上游 `go.yaml.in/yaml/v3` v3.0.5，由上游提供）；釘值測試 `tests/ops/test_vendored_yaml_v3.py` |
| 帶 `!` 的檔到不了 exporter 的解碼 | `pkg/config/nonspecific_tag_test.go`；合併鍵的判斷 `pkg/config/config_file.go` 的 `isMergeKey` |
| Go 版的 receiver 規格 | `pkg/receiverspec`：`Known`（exporter 的路由解析用，保留）；`Check`（已刪除） |
| tenant-api 現在的寫入判定 | 產生器容器的呼叫端 `internal/generator`（`Gate`）；Go 端只剩形狀與保留鍵的檢查（下兩列） |
| tenant-api 經共用判定的入口 | `internal/confd/verdict.go` 的 `Judge`（回 `pkg/config` 的 `UnreadableFileError`）；呼叫端：`validateShape`、`eolGuardErrs`、`tenantorg`、`rbac`、`groups`、`views`、`fedpolicy`、`account` 的解析函式，以及批次的 `tenantAbsentIn` 與 `scopeBlockOnDisk` |
| 批次 patch 由 Go 重新輸出 | `mergePatchYAML`；合併後的不變式 `verifyPartialWrite`（`handler/tenant_list.go`，判定是 `confedit.Verify`）；離線差分測試 `handler/batch_patch_diff_test.go` |
| tenant-api 裡其他的 Go 端路由與 policy 判定（已刪除） | 路由 patch 檢查 `validateRoutingPatch`；receiver 檢查 `gitops` 的 `receiverViolations`（同檔的 `placeholderViolations` 是遮罩憑證回寫防護，保留）；`internal/policy` 的 `JudgeTenantBlock`／`CheckTenantRouting`；「讀不了的檔當成空」的 `tenantBlockOnDisk` |
| tenant-api 保留的非路由檢查 | `gitops/writer.go` 的 `validateShape`（先經 `parse-tree` 的判定，再以 yaml.v3 確認 exporter 讀得了）；保留鍵值的檢查（`_silent_mode`）改由寫入關卡以 exporter 自己的讀取函式判定（`pkg/config` 的 `CheckReservedValue`，[ADR-038](038-unread-keys-judged-by-readers.md) 決定 12），`handler/body_validator.go` 只剩長度上限與依保留鍵登錄表判定的 key 檢查 |
| da-guard exit 4 要更新的說明 | `cmd/da-guard/main.go` 的 exit code 註解；`scripts/tools/_lib_exitcodes.py`；`docs/cli-reference.md` |
| 刪除範圍的使用者比對（CI 檢查） | 宣告表 `tests/shared/phase2_deletion_scope.json`；比對它的測試 `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| 只有 Go 有的 da-guard 檢查（已搬到產生器） | 產生器那一側由 `tests/shared/routing_policy_parity_matrix.json` 釘住 |
| 差異清單（已刪除，下列檔案與指令已不存在） | `tests/shared/reader_divergence_catalog.yaml`；Go 端判讀快照 `tests/shared/merge_key_go_verdicts.json`；`tests/shared/test_reader_divergence_catalog.py`；`make pre-tag` 的 `reader-divergence-expiry` |

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
