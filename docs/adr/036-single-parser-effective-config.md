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
updated_at: 2026-11-24
---

# ADR-036: conf.d 只解析一次：YAML 由 Go 讀，路由與 policy 由產生器判

> **Language / 語言：** **中文 (Current)** | [English](./036-single-parser-effective-config.en.md)

## 狀態

✅ **Accepted**（2026-10-09）。**已實作**：§7 的各步都已合併進 main。

修訂（內容都已併入括號所列的段落，被後來取代的做法不再列出）：

- 2026-10-11：Python 判定設定內容時一律讀 Go 解析出的 JSON；寫入後整棵樹通過產生器才寫；拿掉「policy 不可用時放行」的開關；tenant-api 必須搭配 git；訂定 `parse-tree` 的輸出、封閉的「讀不了」清單與 da-guard 的「未判定」結束碼（§2、§5、§9）。
- 2026-10-12、14、15：產生器容器跨請求快取解析結果（key 是檔案內容的 SHA-256），每次判定在有記憶體與時間上限的子程序裡跑；批次全有全無；從 git 取檔時不是一般檔的項目判為讀不了；批次 patch 以合併後的不變式把關（§3、§4）。
- 2026-10-16、30：「未判定」也涵蓋只由產生器判的 `_` 鍵；容器沒有回鍵的判決算不可用（§3、§9，配合 [ADR-038](./038-unread-keys-judged-by-readers.md)）。
- 2026-10-20、21：Go 端也只留一個判定與一個寫者（exporter、tenant-api、da-guard 共用同一個判定套件，寫 conf.d 只經 `da-guard write`）；`parse-tree` 每種模式輸出同一份逐路徑索引（`da-guard.parse-tree/v2`）（§2、§7、§11）。
- 2026-10-22、23：vendored yaml.v3 的基底改為上游 `go.yaml.in/yaml/v3`；量測後保留解析快取，冷啟動改為批次讀取（§3、§11）。
- 2026-10-28 至 11-01：conf.d 以外的 YAML 以它的消費者為準：Kubernetes manifest 與 Helm values 經 `da-crdecode`，Alertmanager 設定、Prometheus 規則檔與 `prometheus.yml` 經 `da-read`，tenant-api 的管理設定經它自己的 `state show`／`config check`（§12）。
- 2026-10-30：遷移稽核指令刪除，升級前檢查寫進漸進遷移 playbook（§8）。
- 2026-11-13 至 15：判定要跑的外部程式（da-guard、da-read、da-crdecode）有沒有跑成由判定本身回答，沒跑成回 503 `tool_fault`；就緒只看三支程式內建的自檢；容器的回應只帶封閉的代碼與結構化欄位（§3、§5）。
- 2026-11-15：記下平台預設閾值內建在讀取端之後的版本落差（§6）。
- 2026-11-21：Python 讀 YAML 只有一種讀法，就是規格的嚴格讀法，客戶檔案也不例外（§12）。
- 2026-11-22：文件範例多一種 fence 屬性 `{.quote src="…"}`（§7 步驟 11）。
- 2026-11-24：改寫為現行行為。決策不變。

## 決策

**同一份 conf.d（租戶設定目錄）只由一個 YAML 解析器讀：Go 的 yaml.v3。** 其他程式要知道檔裡寫了什麼，讀 Go 輸出的 JSON，不自己解析。語意也各只有一個實作：路由、domain policy、租戶清單由路由產生器（Python）判，閾值與 exporter 實際送出的值由 exporter（Go）判。

1. **YAML 只由 Go 解析。** `da-guard parse-tree` 把整棵 conf.d 讀成一份 JSON：每個路徑是什麼、誰讀它、讀不讀得了、讀得了的檔內容是什麼。Python 工具（包括路由產生器）讀這份 JSON（§2）。
2. **Go 端只有一個判定。** exporter、tenant-api、da-guard 載入 conf.d 時都先經產生這份 JSON 的同一個套件；它判為讀不了的檔，三者都拒收（§2）。檔名規則也只在 Go 有一份。
3. **寫 conf.d 只有一個寫者。** 改寫既有檔與產生新檔都由 `da-guard write` 完成，Python 工具只送「要改什麼」（§2）。
4. **tenant-api 每一筆改變檔案內容的寫入都先問產生器。** tenant-api 的 pod 多一個產生器容器；寫入後的整棵樹通過產生器的 `--validate --strict` 才寫入（§3、§4）。產生器容器或部署端 policy 檔不可用時回 503，沒有放行開關（§5）。
5. **conf.d 以外的 YAML：消費者的讀取器能呼叫時以它為準，否則照 YAML 規格嚴格讀**（§12）。

範例。下面這份租戶檔，鍵 `010` 與值 `yes` 都沒加引號：

```yaml {.confd .expect-invalid}
# t010.yaml
tenants:
  010:
    _routing:
      receiver:
        type: slack
        api_url: {secret: {key: slack-api-url}}
        send_resolved: yes
      group_wait: 30s
```

`da-guard parse-tree --config-dir conf.d` 輸出的 `files`（節錄）：

```json
{"t010.yaml": {"tenants": {"010": {"_routing": {
  "group_wait": {"text": "30s", "tag": "!!str", "value": "30s"},
  "receiver": {
    "api_url": {"secret": {"key": {"text": "slack-api-url", "tag": "!!str", "value": "slack-api-url"}}},
    "send_resolved": {"text": "yes", "tag": "!!str", "value": "yes"},
    "type": {"text": "slack", "tag": "!!str", "value": "slack"}}}}}}}
```

租戶 id 是字串 `"010"`，`send_resolved` 是字串 `"yes"`（PyYAML 會讀成八進位整數 8 與布林 `True`）。產生器、exporter、tenant-api 看到的都是這一份。

### 1. 誰解析什麼

| 層次 | 唯一的實作 | 其他工具怎麼取得 |
|---|---|---|
| YAML 語法：選哪些檔、每份檔的內容、哪些檔讀不了 | Go 的 `parse-tree` 套件（yaml.v3） | Python 讀 `da-guard parse-tree` 的 JSON；exporter、tenant-api、da-guard 在程序內呼叫同一個套件 |
| conf.d 的檔名規則（哪些檔名是租戶檔、對應哪個租戶 id） | Go（一份實作） | Python 讀 `parse-tree` 的逐路徑索引 |
| 寫 conf.d：改寫既有檔、產生新檔 | Go（`da-guard write`） | Python 工具送「要改什麼」 |
| 路由（`_routing_defaults`、`routing_profiles`、租戶的 `_routing`）、domain policy、租戶清單的語意 | 路由產生器（Python） | 讀產生器輸出的 finding（它回報的每一筆問題）與產生器容器的狀態端點（§5） |
| 閾值與 exporter 實際送出的值 | exporter（Go） | 讀 `da-guard served-values`／`effective` 的 JSON |
| conf.d 以外、有其他程式在讀的 YAML | 那個消費者 | 它的讀取器能呼叫時以它的讀法取得；否則照規格嚴格讀（§12） |

閾值那一列早就是「一個實作，其他工具讀它的輸出」；本 ADR 把同樣的做法套到語法層、檔名規則、寫入與路由、policy 語意。

### 2. 解析結果 JSON

`da-guard parse-tree` 有四種模式，輸出同一種文件：

| 模式 | 指令 | 用途 |
|---|---|---|
| 整棵樹（`tree`） | `parse-tree --config-dir <dir>` | 每個路徑的索引，加上每份讀得了的檔的內容 |
| 單檔（`file`） | `--path <rel> --input <file\|->`；批次為 `--inputs` | 寫入前判一份還沒寫上磁碟的文字 |
| 只列檔（`list`） | `--list-only --config-dir <dir>` | 只要檔案清單、不讀內容 |
| 名稱（`names`） | `--list-only --names <file\|->` | 只依名稱分類不在磁碟上的檔名（例如 ConfigMap 的鍵） |

**外層**：`schema` 是 `"da-guard.parse-tree/v2"`，另帶 `da_guard_version`、`mode`、`defaults_carriers`（每個目錄採用的那一份 `_defaults` 檔）與 `entries`；整棵樹與單檔模式另帶 `files`、`rendered_by` 與 `config_hash`。讀取端遇到其他 `schema` 值，整份當成不可用。新增欄位不換 `schema`，改變讀取端依賴的形狀才換，與 `served-values`／`effective` 的慣例相同。

**每個路徑一筆**：`entries` 對 exporter 的走訪記下的每個路徑各有一筆，每筆寫出全部欄位，沒有值的是 `null`。整棵樹與只列檔模式由同一段程式產生，差別只在讀內容才發現得到的問題。

- `path`、`depth`、`class`（`tenant`、`defaults`、`platform`、`directory`）、`tenant_named`（檔名帶的租戶 id）、`defaults_document`、`config_extension`。
- `readers`：誰讀它（`exporter`、`generator`）；沒有人讀時是空清單，`not_read` 寫原因（例如 `unselected_defaults_carrier`：同一目錄兩種拼法並存時落選的那份）。
- `role`：這個路徑是什麼，讀取端照讀、不自己組合。`config` 是設定檔，讀不了也算，此時 `problem` 寫原因；`dir` 是走訪會走進去的目錄；`not_a_file` 是 exporter 碰不到一般檔的地方（指向目錄的 symlink、FIFO、懸空或迴圈的連結）；`unlistable_dir` 是列不出內容的目錄，代表這份文件不完整。只有上面列的情況是 `not_a_file`，其餘任何失敗（權限不足、EIO）一律是 `config`，免得一份 stat 失敗的租戶檔從清單上消失。
- `problem`：讀它會失敗的原因，`{kind, message}`。
- `verdict`：不帶 `--scope` 的 da-guard 驗證會怎麼列這個路徑：`unservable`（列在結束碼 3：exporter 無法服務）、`tree_refused`（列在結束碼 4：產生器因它拒收整棵樹）或 `null`。讀取端要在驗證列出的每個路徑上失敗時讀這一欄。它只回答單一路徑：每筆都是 `null` 不等於驗證通過（例如同一租戶在兩個檔宣告）。

範例：一棵樹有 `t1.yaml`、`_defaults.yaml` 與 `_defaults.yml`、語法錯的 `team/t2.yaml`，以及指向不存在檔案的 symlink `gone.yaml`。`da-guard parse-tree --list-only --config-dir conf.d` 輸出（節錄三筆）：

```json
{"path": "_defaults.yml", "depth": 0, "class": "defaults", "tenant_named": null, "defaults_document": true, "config_extension": true, "readers": [], "not_read": "unselected_defaults_carrier", "role": "config", "problem": null, "verdict": null}
{"path": "gone.yaml", "depth": 0, "class": "tenant", "tenant_named": "gone", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "not_a_file", "problem": {"kind": "read_failure", "message": "the path cannot be statted (stat_error)"}, "verdict": "unservable"}
{"path": "team/t2.yaml", "depth": 1, "class": "tenant", "tenant_named": "t2", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "config", "problem": null, "verdict": null}
```

整棵樹模式給出同樣的三筆，只有 `team/t2.yaml` 多了 `"problem": {"kind": "syntax_error", "message": "line 2: yaml.v3 cannot parse the text into a document"}`，`verdict` 為 `"unservable"`。

**每份檔的內容**：輸出 yaml.v3 解碼後的結構。

- 頂層必須是 mapping；空檔是空 mapping。
- 別名（`*x` 指回 `&x` 標記的內容）與未加引號的合併鍵 `<<`（把另一個 mapping 併進來）都已展開；加了引號的 `"<<"` 是普通的鍵。讀取端不必自己展開。
- mapping 的鍵一律輸出原文字串（`010` 就是 `"010"`，與 exporter 讀租戶 id 的方式相同）。
- 純量輸出原文、yaml.v3 判定的型別與換算後的值，例如 `{"text": "0777", "tag": "!!int", "value": "511"}`。布林與 null 的值是 JSON 的 `true`／`false`／`null`；整數與浮點數以十進位字串輸出，避免精度在兩種語言間不一致；時間與無限大只有原文與型別。
- 語意層只依型別與換算後的值判斷，不採用 yaml.v3 解碼進 Go 有型別欄位時的寬鬆轉換（例如字串 `yes` 解碼進布林欄位得到 `true`）。

**讀不了的檔**：`problem.kind` 是一份封閉清單，共 13 種；`files` 裡沒有它的內容。清單上只放各讀取端可能讀出不同意思的寫法：

| `kind` | 情況 |
|---|---|
| `read_failure`、`not_regular_file`、`invalid_utf8`、`syntax_error` | 讀取失敗、不是一般檔、不是合法 UTF-8、語法錯 |
| `top_level_not_mapping` | 頂層不是 mapping（空檔不算） |
| `decode_error` | yaml.v3 解碼會報錯的一切情況，例如別名展開超過上限、`!!int abc`、`<<: 1` |
| `multiple_documents` | 一個檔裡有多份文件（以 `---` 分隔）。yaml.v3 只讀第一份，照抄等於讓 `---` 之後的內容靜默失效 |
| `duplicate_key` | 同一個 mapping 裡有重複的鍵（以展開別名後的鍵原文比對） |
| `null_key`、`non_scalar_key` | 鍵是 null（`~:`、`null:`、空鍵），或鍵不是純量（`? [a]`） |
| `tag_not_allowed` | 型別標記不在白名單。純量只收 `!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!timestamp`；`!!binary`、`!!set`、`!!merge`、`!foo` 等都算讀不了 |
| `non_specific_tag` | YAML 的非特定標記 `!`（`! "x"`、`! "<<"`）：當成合併鍵或字面上的鍵都可能靜默地多出或少掉 policy |
| `bom_not_at_start` | 檔案開頭以外出現 BOM（U+FEFF，一種標示編碼的不可見字元）：它可能讓鍵或值帶上看不見的字元 |

清單以外的一律照 yaml.v3 讀，包括同一個錨點名稱定義兩次、不存在的日期 `2001-13-45`、冒號後接 TAB 這類 PyYAML 拒收、yaml.v3 讀得了的寫法。為什麼不拒收 PyYAML 拒收的一切，見「考慮過的替代方案」。

範例。一個檔裡有兩份文件：

```yaml {.confd .expect-refused=multiple_documents}
# t1.yaml
tenants:
  t1:
    mysql_connection_usage_percent: "80"
---
tenants:
  t2:
    mysql_connection_usage_percent: "90"
```

`parse-tree` 判為 `multiple_documents`（「a second document starts at line 4; only one document per file is read」），`verdict` 為 `unservable`；exporter 不服務 `t1`、`t2`；`da-guard --config-dir conf.d` 以結束碼 3 點名 `t1.yaml`。修法是把第二份文件拆成另一個檔。

**Go 端的讀取者也照這份判定**：判定是 exporter、tenant-api、da-guard 都引用的 Go 套件（`pkg/parsetree`）。三者載入時先經它，再各自把讀得了的檔解碼成自己的結構；所以產生器、exporter、da-guard、tenant-api 拒收的是同一批檔。

- exporter 遇到讀不了的租戶檔：這份檔宣告的租戶不服務，同一棵樹的其他租戶不受影響，記 WARN log、`da_config_parse_failure_total` 加一。遇到讀不了的 `_defaults` 或其他 `_` 平台檔：整份略過，記一行 ERROR（「entire block dropped」），合併 defaults 鏈時當成空檔，`da-guard effective` 在 `chain_parse_failed` 列出它；只靠這份檔的預設值才送出的閾值不再送出。
- tenant-api 寫入的本文被判為讀不了時回 400，錯誤文字寫出 `kind`。整檔寫入要取代的原檔讀不了時，視同無法解析的原檔，要有全租戶的寫入權限才能取代。
- tenant-api 自己讀的管理設定（`_tenant_orgs.yaml`、RBAC 設定、群組、視圖、聯邦白名單與子集合、AccountID 帳本）也先經判定。讀不了時照該讀取端既有的解析失敗處置，不當成空檔：這些多半是授權邊界的輸入，空的 RBAC 是另一種授權，不是「沒有設定」。例如 RBAC 設定熱重載失敗時保留上一版。
- da-guard 對這些檔回結束碼 3（§9）。

**Python 端怎麼取得**：Python 呼叫 Go binary、讀 JSON（`scripts/tools/_lib_parse_tree.py`，與讀 `served-values`／`effective` 的機制相同）。讀 conf.d 的工具取得整棵樹的索引時，只要有一筆 `verdict` 不是 null，就逐條以 `ERROR:` 點名那些路徑，在寫任何東西之前以結束碼 2 結束。產生器每個用到值的地方都用 JSON 的型別與值。

**寫 conf.d 檔：只有 Go 一個寫者。** Python 工具決定「要改什麼」，交給 `da-guard write`：

- 一次呼叫是一批，可以混合改寫既有檔（set、unset、ensure-map）、新建、整份取代、搬移、從樹外複製、刪除；整批只判一次，全部通過才寫。
- 改寫在 yaml.v3 的節點樹上做，只換掉被編輯的節點，其餘節點與註解照原樣寫回（§4 的合併不變式同樣適用）。整份取代不讀舊檔，所以也能修好 `parse-tree` 讀不了的檔。
- 判定的是寫入後的整棵樹：每個檔讀回的內容必須等於預期，而且不能讓樹變壞——別的檔失去讀取端、任何路徑新出現驗證會點名的狀態、da-guard 驗證新出現錯誤，都整批不寫；別處原本就有的問題不擋無關的寫入。
- 刪除可以帶前提「這個檔恰好宣告這些租戶」，不符就整批不寫。

後果：寫出的格式改為 yaml.v3 的寫法，空行可能被壓掉，行尾註解的對齊與被寫出的值的引號樣式會變；但註解比以 PyYAML 改寫時保留得多。

**範圍**：本節只涵蓋 conf.d，其他 YAML 見 §12。portal 的瀏覽器端工具以 js-yaml 解析，只做預覽，不作為任何寫入或 CI 的放行依據。

### 3. 運作方式（tenant-api）

**tenant-api 的 pod 多一個產生器容器（da-tools 映像，`da-tools generator-service`），tenant-api 每次寫入前都問它。**

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

容器裡一個常駐程序負責從 git 取檔、跨請求快取每份檔的解析結果；每次判定由它 fork 一個子程序（作業系統複製出來的程序，一開始就有常駐程序當下的記憶體，所以直接繼承快取），在記憶體上限與判定期限內跑完，同一時間只跑一個。子程序出事只拒收那一筆；常駐程序出事才算容器不可用。

- **溝通**：HTTP/1.1 走 Unix socket，不開網路埠；socket 放在 pod 內的共用暫存目錄（emptyDir）。
- **快取**：key 是（檔案內容的 SHA-256，解析模式）。內容一改 key 就變，所以不會讀到過期內容。不用 git 的 blob SHA：SHA-1 可以被刻意造出碰撞，讓一份被拒內容的解析結果拿去判另一份。最新 base commit 樹裡的內容一律保留；其他內容（例如被拒的寫入帶來的）總量上限 32 MiB，用完先丟最久沒用到的。快取只存內容，選檔與讀不讀得了每次判定都重算；部署端 policy 檔不在 git 裡，每次都重新讀。
- **快取的去留**：快取存的是 Go 的輸出，不是第二個讀者，所以它的去留是效能決定。撤快取的條件是：在 5000 個租戶的樹上，一次完整判定（`parse-tree`、產生器、`da-read alertmanager`，以及鍵的判決要跑的 served-values、effective、`key-verdict`）裡每支程式的峰值記憶體不超過子程序上限的 75%，而且時間不超過判定期限的一半。量測（`scripts/tools/dx/measure_judgement_budget.py`，數字見 [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）不成立，所以保留快取。冷啟動時沒命中的檔整批交給 `parse-tree --inputs`，每批只走訪整棵樹一次，時間隨檔案數線性成長。量測時整棵樹模式的輸出還沒有串流；之後要重新評估撤快取，先做串流再照同樣條件量。
- **期限**：一個請求的總期限是 tenant-api 的 HTTP 寫入逾時（`--write-timeout`，預設 30 秒）；等寫入鎖、問產生器、重驗、commit 都在其中。tenant-api 每次呼叫都帶上剩餘時間，容器的判定期限取「20 秒」與「剩餘時間減 5 秒」較小者，排隊時間也算在內；那 5 秒留給回應傳遞與 tenant-api 的核對與 commit。到期時由常駐程序回應「拒收該筆」。子程序以 `setsid` 自成一個 process group，到期時整組以 `killpg` 結束，不留殘餘的 da-read、da-guard。
- **資源**：容器的 request 與 limit 相同，記憶體 512Mi、CPU 1。每個判定子程序另設 `RLIMIT_DATA` 192 MiB（Linux 對單一程序可寫記憶體的上限；fork 繼承來的快取也算在內），它啟動的 Go 程式各自繼承同樣的上限。一次只跑一個判定，最壞情況約是 192＋192＋常駐程序約 80，小於 512。不用位址空間上限（`RLIMIT_AS`）：Go 執行期一啟動就預留大量虛擬位址，上限設低了程式啟動不了。

```yaml {.quote src="helm/tenant-api/values.yaml"}
  # ADR-036 §3: request = limit (512Mi, 1 CPU). The memory budget is the
  # resident service (~80Mi) plus one judgement process and one Go program it
  # starts (192Mi RLIMIT_DATA each). The render fails when requests and limits
  # differ; raise both together (and the service's --memory-limit-bytes) when
  # the parse cache outgrows the judgement budget.
  resources:
    requests:
      cpu: "1"
      memory: 512Mi
    limits:
      cpu: "1"
      memory: 512Mi
```

**拒收該筆與容器不可用的分界**：常駐程序有回應、而且回應指明這一筆的結果，就是「拒收該筆」，容器照常可用；把這些算成容器不可用，一筆寫入就能讓之後所有寫入都回 503。

| 情況 | tenant-api 回什麼 |
|---|---|
| 產生器有擋下的 finding | 422 `GENERATOR_FINDINGS` |
| 子程序超過記憶體上限、輸出不完整、這次寫入產生的檔太大、git 屬性會讓 commit 的位元組與判定的不同 | 422 `GENERATOR_REJECTED`（reason `memory`、`incomplete`、`too_large`、`normalized` 等），重送同一筆也不會過 |
| 判定期限到期、容器忙碌、git 讀取失敗、重驗後仍不一致、剩餘時間不夠判定或不夠重驗 | 429 `GENERATOR_BUSY`（reason `deadline`、`busy`、`git_read`、`mismatch`、`no_time`），可稍後重送 |
| 常駐程序沒有回應或回應讀不懂、判定要跑的程式沒跑成、產生器比 policy 要求的版本舊、容器自己出錯 | 503 `VALIDATOR_UNAVAILABLE`（reason `unreachable`、`bad_response`、`tool_fault`、`generator_too_old`、`internal_error`） |
| 部署端 policy 檔不可用 | 503 `POLICY_UNAVAILABLE`（§5） |

容器的回應只帶封閉的代碼與結構化欄位，不帶自由文字；細節寫在容器自己的 log。

**判定要跑的程式必須跑成**：一次判定會跑三支 Go 程式：da-guard、da-read（Alertmanager 與 Prometheus 的設定載入器，§12）、da-crdecode（照 kubectl 的讀法讀 manifest）。其中任何一支沒跑成，那筆寫入就沒有結論，回 503 `tool_fault`，不論產生器少了它得出什麼結果。

- 「沒跑成」是程式本身出問題：找不到、無法執行、逾時、被訊號結束、結束碼或輸出不在契約內。程式對輸入的回答不算：da-guard 判某個檔讀不了、da-read 拒收一份設定，照常是那筆寫入的結果。
- da-read 與 da-crdecode 的拒收和用法錯一樣以結束碼 2 結束；分辨靠標準輸出：只有拒收會印出固定的被拒文件 `{"answer":"refused"}`，理由寫在 stderr。結束碼 2 而沒有這份文件就是沒跑成。所以 Python 工具與這兩支程式必須同版。
- 因判定的記憶體上限而由 Go 執行期自己結束的程式不算故障：那筆寫入以 reason `memory` 拒收。
- **就緒**：容器只在 policy 可用、產生器不比 policy 要求的舊、三支程式內建的自檢都通過、而且沒有還沒清除的工具故障時才回報就緒（`GET /v1/status` 的 `ready`）。自檢是每支程式自己的指令（`da-guard self-check`、`da-read self-check`、`da-crdecode --self-check`），以打包在 binary 裡的樣本把每個子指令跑一次，通過時印 `{"self_check":"passed"}`、結束碼 0。判定裡一出現工具故障，容器立刻回報未就緒並跑一輪自檢，只有通過的自檢清除故障。
- 已知限制：自檢對每個子指令只跑一種輸入，判定實際走的旗標更多，所以自檢通過之後判定仍可能遇到工具故障；這時那筆請求回 503、容器回到未就緒，等下一輪自檢。

**取檔方式：產生器容器只從 git 物件取檔，不讀工作目錄。**

- 唯讀掛載 tenant-api 用的同一個 repo，以 `git ls-tree` 與 `git cat-file` 依指定的 commit 取檔。不讀工作目錄：PR 模式會就地切換分支，可能讀到切換一半的狀態。不用 `git archive`：它會套用 `.gitattributes` 的匯出規則，客戶把 policy 檔標成不匯出時產生器就看不到。
- 只取一般檔（mode 100644、100755）。symlink、gitlink（子模組）等其他項目判為 `not_regular_file`，擋下寫入：從 git 物件讀到的 symlink 只是連結文字，照它判結果取決於文字碰巧長什麼樣。隱藏路徑（某一段以 `.` 開頭）不判。這比從檔案系統讀的讀取端嚴：exporter 與客戶 CI 跟隨 symlink（ConfigMap 掛載後每個 key 都是 symlink），所以同一棵樹可能客戶 CI 綠、tenant-api 的寫入被拒；修法是換成內容相同的一般檔。
- 兩個容器以同一個 uid 執行：git 拒絕讀別的 uid 擁有的 repo。repo 關閉自動垃圾回收（`gc.auto=0`），避免一邊 commit 一邊讀時物件被清掉。
- git 為必要：不支援沒有 git 的部署。

### 4. 寫入怎麼驗證

**寫入後，產生器檢查整棵樹；全部通過才寫入。**

- **判準**：產生器容器把要寫的檔換成新內容，對寫入後的整棵樹以 `--validate --strict` 執行；結束碼 0 才寫入。用的是同一個 base commit、同一份部署端 policy 檔、容器裡那一版產生器。判準就是產生器自己的結果，所以 tenant-api 不會比它容器裡的產生器寬鬆。
- **哪些寫入要送驗**：凡是會改變檔案內容的寫入都送驗，包括只改閾值的；不由 Go 判斷「有沒有改到路由」。
- **錯誤回應**：列出產生器回報的 finding，與被寫入檔有關的排在前面，並標出哪些在寫入前就存在。這只影響訊息，不影響判定。
- **批次**：一次請求、一次 commit、全有全無。整批套用後整棵樹通過才寫入，否則整批拒收；同步與非同步批次規則相同。每個批次在獨立的暫存 git worktree 裡套用、判定、commit，再推進分支 ref、更新服務中的工作目錄；推進到一半中斷時，下次啟動或寫入前把這批路徑重設成分支上的內容。讀者不會看到從未 commit 的內容。
- **批次 patch 的合併**：由 Go 讀出檔案、套用 patch、再寫回，合併後的內容一定送驗。合併要符合一條不變式，否則以 `TENANT_CONFIG_NOT_LOADABLE`（`config_error` 為 `not_round_trippable`）拒收：把原檔與合併結果都解析成 yaml.v3 的節點樹，這次編輯取代的部分以外，每個節點寫出來都要相同（種類、style、tag、純量原文、anchor 與別名所指的節點）。字元相同，對任何讀取端意思都相同，所以這條判準與讀取端無關。合併寫出的值一律用雙引號，避免任何讀取端把它讀成時間、數字或布林。
- **驗的和寫的必須是同一份**：容器回傳它驗過的檔案路徑、內容 hash、base commit 與部署端 policy 檔的 hash；tenant-api 在寫入鎖之下核對。不一致就重驗一次，仍不一致就拒收（429 `mismatch`）。寫入的位元組必須和驗過的一模一樣。
- **PR 模式驗的是分支起點**：驗的是 PR 分支所在的 base commit 加上這筆寫入。PR 合併進主幹後的結果不在 tenant-api 的判定範圍，由客戶 CI 或 merge queue 把關。

**代價**：樹上任何地方有會擋下的問題，所有經 tenant-api 的寫入都會被拒，直到修好。問題在某個租戶檔、修好它就能讓整棵樹通過時，可以經 tenant-api 修；其他情況（平台檔、多個租戶同時違規）走 git。客戶 CI 用同一版 da-tools、帶同一份部署端 policy 檔時，會讓整棵樹變紅的改動進不了主幹，等於主幹永遠保持綠燈。

### 5. policy 檔與 policy 狀態

**部署端的 policy 檔掛進產生器容器；policy 檔不可用就拒收寫入。**

- **部署端的 policy 檔**（`--policy`）：裡面的 `allowed_domains` 限制 webhook 網址的網域，用來防 SSRF（服務被拿來替攻擊者對內網發請求），所以放在租戶 repo 之外，有 repo 寫入權限的人改不掉。Helm chart 的 `generator.policy` 把它唯讀掛進產生器容器與 tenant-api 容器（tenant-api 用它核對 hash，§4）。`da-tools init` 產生的客戶 CI 可以帶入同一份檔（GitHub 的 variable 或 GitLab 的 File 型 CI 變數 `DA_TOOLS_POLICY`），讓 CI 在 PR 上提早看到 tenant-api 會給的答案；這一側不是安全控制。設定方式見 [GitOps CI/CD 整合指南 §2.5](../scenarios/gitops-ci-integration.md#25-帶入部署端-policy-檔da_tools_policy)。
- **狀態端點**：產生器容器回報部署端 policy 檔是否可用，tenant-api 讀它產生 `tenant_api_policy_available`；容器不可達時這個 metric 為 0。告警 `TenantApiPolicyUnavailable` 讀它。repo 裡的 policy 檔有錯不會讓它變 0，那是產生器的 finding，照 §4 拒收。
- **沒有放行開關**：部署端 policy 檔或產生器容器不可用時，會改變檔案內容的寫入一律回 503（`POLICY_UNAVAILABLE` 或 `VALIDATOR_UNAVAILABLE`，§3）。能送寫入請求的使用者可以影響容器是否可用，退回較寬鬆的判定等於讓請求方自己挑判定者。
- **不輸出整理好的設定 JSON**：沒有程式需要讀它。

### 6. 版本

客戶的 CI 用哪一版 da-tools，與 tenant-api 無關，所以「和客戶 CI 完全一致」做不到。能保證的是：**tenant-api 不比它容器裡的產生器寬鬆**。da-guard 與產生器在同一個 da-tools 映像裡，版本一起決定。

兩邊版本不同時，任一邊拒收，設定都進不了 Alertmanager：客戶 CI 較嚴時，tenant-api 接受的寫入會在 CI 被擋下；容器較嚴時，tenant-api 會拒收 CI 原本會接受的寫入。兩者都不放行違規的設定，代價是判定不一致。

這條不涵蓋平台預設啟用的閾值：它們的值編譯在讀取端裡（[ADR-017](017-defaults-yaml-inheritance-dual-hash.md) 決策 11），檢查端（CI 的 da-guard、tenant-api）比部署中的 exporter 新時，一棵靠內建值的樹在檢查端通過，exporter 卻不送那些列。這是放行；兩邊是否對齊，看兩者內建表的摘要是否相同。

縮小版本落差的做法：

- tenant-api 的 chart 固定一個 da-tools 版本（`generator.image.tag`）；`da-tools init` 產生的設定也固定版本，不用 `latest`。
- 部署端 policy 檔可以宣告 `min_generator_version: "X.Y.Z"`：容器裡的產生器比它舊時，所有會改變檔案內容的寫入回 503（reason `generator_too_old`），修法是調高 chart 的 da-tools 版本。用同一份 `--policy` 判定 receiver 的 CLI（`validate-config` 等）比它舊時也拒絕判定。這個鍵只從部署端的 policy 檔讀。
- tenant-api 的 `/metrics` 轉出產生器容器的 metric，其中 `da_generator_build_info` 帶產生器與 da-guard 的版本。

### 7. 在 main 上依序合併

**各步依序合併進 main，每一步合併後 main 都可以發布。** 不設新舊並行期、不設切換開關。順序的原則：Go 端模仿 PyYAML 的程式先刪，產生器才改用 yaml.v3 的讀法，所以不論發布哪一個 commit，都不會出現「產生器已用 yaml.v3 讀、Go 端仍模仿 PyYAML」的版本。步驟的編號被程式碼註解引用，所以保留。

| 步驟 | 內容 |
|---|---|
| 0 | `parse-tree` 與它的固定語料 `tests/shared/parse_tree_corpus.json` 先發布，行為不變；與解析無關的小工具搬出要刪的套件。重複鍵、多份文件、白名單外的型別標記原本有一部分由 Go 端的模仿程式擋下，刪它們之前 `parse-tree` 必須已經判為讀不了 |
| 1 | tenant-api 寫入改問產生器容器；刪除 tenant-api 裡 Go 端的路由與 policy 判定、扁平鍵 `_routing_receiver_type`（寫入改回 400）與放行開關。前置：`da-tools init` 固定版本、git 寫入支援多檔一次 commit 並把批次改成全有全無、產生器容器與 chart 設定 |
| 2 | da-guard 不再判路由與 policy，改報「未判定」（結束碼 4，§9）；`guard-defaults-impact.yml` 加跑產生器 |
| 3 | 刪除 Go 端模仿 PyYAML 與模仿產生器語意的程式（§11）。從這一步起，沒有任何 Go 程式產生路由或 policy 的 finding |
| 4 | 產生器與 Python 工具改讀解析結果 JSON，刪除讀 conf.d 用的、模仿 yaml.v3 的 Python loader |
| 5 | `parse-tree` 的判定移到 `pkg/` 下的共用套件 |
| 6 | exporter、da-guard 與 tenant-api 的讀取面載入 conf.d 先經這份判定；exporter 不服務讀不了的檔。使用者看得到的 breaking change，升級前的把關見 §8 |
| 7 | tenant-api 的寫入與它自己讀的管理設定改經這份判定 |
| 8 | 刪除 vendored yaml.v3 的「非特定 tag」patch（它讓 `! "<<"` 被當成合併鍵）。要等步驟 6、7 之後：那時帶 `!` 的檔已到不了任何元件的解碼，刪 patch 不會靜默改變任何元件照讀的檔 |
| 9 | Go 端的檔名規則合併為一份（`pkg/confdname`）；`parse-tree` 每種模式輸出同一份逐路徑索引，Python 讀 `role`，不再自己判斷路徑是不是設定檔 |
| 10 | 編輯原語的共用套件（`pkg/confedit`）與 `da-guard write`；所有寫 conf.d 的 Python 工具改為只送「要改什麼」 |
| 11 | 刪除 Python 端以 PyYAML 改寫 conf.d 的那一層。文件裡的 yaml 範例由單一清冊（`scripts/tools/_lib_doc_yaml_inventory.py`）判定，作者在 fence 上以屬性宣告讀者，例如 `{.confd}`、`{.k8s}`、`{.alertmanager}`；刻意讀不了的寫 `{.confd .expect-refused=<kind>}`，故意寫錯的寫 `{.confd .expect-invalid}`，不給任何檢查判的寫 `{.illustrative reason="…"}`，repo 內檔案的節錄寫 `{.quote src="…"}`（由引用守衛逐行比對來源）。寫法見 [文件模板](../internal/doc-template.md) |
| 12 | conf.d 以外的 YAML 改以消費者為準，加上守住邊界的 lint（§12） |
| 13 | 刪除遷移稽核指令與模仿 yaml.v3 的 Python loader；漸進遷移 playbook 補上升級前檢查（§8） |

與上列平行完成的：「快取的去留」的量測（§3）；vendored yaml.v3 的基底升級到上游 v3.0.5（§11）。未做：`parse-tree` 整棵樹模式的串流輸出，只在重新評估撤快取時才需要。

客戶的 conf.d 拿不到，每一步合併前的驗證只涵蓋 repo 內的樹；客戶在自己的樹上看影響的方式見 §8。

### 8. 遷移：升級前怎麼知道影響

改由 yaml.v3 解析後，凡是兩個解析器讀法不同的值都會改變。最常見的是未加引號的 `yes`／`no`／`on`／`off`（以前是布林，現在是字串）；`!!merge` 標記的鍵之後整份檔讀不了。exporter 也不服務 `parse-tree` 判為讀不了的檔（§2）。

升級前在客戶自己的樹上、以新版工具做兩件事，做法寫在[漸進遷移 playbook](../scenarios/incremental-migration-playbook.md) 的「升級平台版本前的檢查」：

- **新版拒收什麼**：以新版 da-guard 驗自己的 conf.d。回結束碼 3 的檔，就是新版 exporter 不服務、產生器與 tenant-api 拒收的檔（三者用同一份判定），報告逐檔列出。
- **輸出差在哪裡**：以新舊兩版的 da-tools 映像各渲染一次同一棵樹，比對渲染結果，作法與 [ADR-037](./037-config-diff-compares-rendered-output.md) 的 config-diff 相同。它指出結果哪裡變了，不指出是哪一項讀法造成的。

遷移稽核指令已刪除：它當作「今天」的那一側是 PyYAML 加上模仿 yaml.v3 的 loader，這個讀法從沒出現在任何發布的版本裡，回答不了客戶要問的「新版拒收什麼、輸出差在哪裡」。

### 9. da-guard

da-guard 有 `parse-tree` 與 `write`，不再判斷路由與 policy，交給產生器的 `--validate --strict`。閾值相關的檢查不變。

- 樹上有只能由產生器判的內容（路由、policy，或只由產生器讀的 `_` 鍵，判法見 [ADR-038](./038-unread-keys-judged-by-readers.md)）時，報告明示「未判定」並回結束碼 4，讓「沒判」和「判過、沒問題」分得開；沒有這類內容的樹照常回 0。
- 同時符合多個結束碼時，優先順序是 3 > 1 > 4（3 是有檔讀不了，1 是有錯誤等級的 finding）。
- 結束碼 3 的範圍以 [CLI 參考的 guard 一節](../cli-reference.md#guard)為唯一定義，包括 `parse-tree` 判為讀不了的每一份檔。結束碼 4 的證據裡，另列本次執行點不到、產生器卻會整棵拒收的路徑（`unreadable_outside_run`）：`--scope` 以外的讀不了的檔、名稱像設定檔的目錄等。
- `--required-fields _routing.*` 直接以結束碼 2 報錯，不會悄悄失效。
- 不另設旗標讓 CI 宣告「路由已判過」：`da-tools init` 產生的客戶 CI 本來就跑 `generate-routes --validate --strict`。只跑 da-guard 的 CI 要加這一步。

範例：只有一個帶 `_routing` 的租戶檔、沒有其他問題的樹，`da-guard --config-dir conf.d` 結束碼 4，stderr 最後一行是：

```text
da-guard: routing and domain policy not judged: 1 item(s) only the route generator judges — run `da-tools generate-routes --config-dir conf.d --validate --strict` — da-guard no longer judges routing or domain policy (ADR-036 §9) (exit 4)
```

### 10. tenant-api 的讀取端點

- 沒有任何 GET 端點回傳產生器渲染的路由。等到有人需要，才新增 `GET /tenants/{id}/routing`，內容由產生器容器產生。
- `/effective` 回應裡的 `_routing` 是 exporter 合併出來的原始鍵，不是渲染後的路由。

### 11. 刪除哪些程式

依「還有沒有人在用」決定。CI 以宣告表（`tests/shared/phase2_deletion_scope.json`）列出相關套件的使用者，和宣告不一致就紅。

| 項目 | 處理 |
|---|---|
| Go 端模仿 PyYAML 讀法的程式（`pkg/pyyamlcompat`、policy 解碼器的「只認空格」選項） | 已刪除 |
| Go 端模仿產生器語意的程式（Go 版的路由與 policy 判定、da-guard 的路由檢查） | 已刪除 |
| tenant-api 裡的 Go 端路由與 policy 判定、把讀不了的檔當成空的舊行為 | 已刪除 |
| Go 版的 receiver 規格 | 判定的部分已刪除；exporter 只用它查 receiver 種類是否存在，這部分保留 |
| Go 與 Python 對讀法不同之處的差異清單 | 已刪除 |
| Python 端模仿 yaml.v3 的 loader、檔案選擇規則、名稱述詞、以 PyYAML 改寫 conf.d 的那一層 | 已刪除，改讀 `parse-tree` 或送 `da-guard write` |
| Go 端多份檔名規則 | 合併為 `pkg/confdname` |
| vendored yaml.v3 的「只認空格」與「非特定 tag」patch | 已刪除 |
| vendored yaml.v3 的 unhashable merge key 修正 | 基底升級為上游 `go.yaml.in/yaml/v3` v3.0.5，上游已內建同一修正，patch 刪除 |
| vendored yaml.v3 的重複鍵檢查修改（大型 mapping 的判重從逐對比較改為 hash 表） | 保留：它防的是以大量鍵拖垮解碼的阻斷服務攻擊，不是在模仿另一個解析器 |
| 遷移稽核指令 | 已刪除（§8） |

### 12. conf.d 以外的 YAML：能問消費者就問，否則照規格嚴格讀

一份 YAML 的意思由讀它、照它行事的程式決定。那個消費者的讀取器能被呼叫時，Python 工具問它；PyYAML 與 yaml.v3 都不是這些檔的權威讀者。沒有讀取器可呼叫的檔，照 YAML 規格以嚴格讀法讀，不模仿消費者比規格寬鬆的地方。

| 檔案 | 權威讀者 | Python 端怎麼取得 |
|---|---|---|
| Kubernetes manifest | kubectl（sigs.k8s.io/yaml，採 YAML 1.1 的規則） | `da-crdecode`（manifest 讀法） |
| Helm values 檔 | Helm（sigs.k8s.io/yaml） | `da-crdecode --any-shape` |
| Alertmanager 設定 | Alertmanager 自己的設定載入函式（上游 `config.Load`） | `da-read alertmanager` |
| Prometheus 規則檔 | Prometheus 的載入函式（上游 `rulefmt.Parse`） | `da-read prometheus-rules` |
| Prometheus 設定（`prometheus.yml`） | Prometheus（上游 `config.Load`） | `da-read prometheus` |
| tenant-api 的狀態紀錄（群組、視圖、聯邦白名單與子集合、AccountID 帳本） | tenant-api 每種紀錄的解析函式 | `tenant-api state show` |
| tenant-api 讀的 RBAC 設定與 `_tenant_orgs.yaml` | 同上 | `tenant-api config check` |
| 文件裡的 conf.d 範例 | exporter 與產生器 | `parse-tree`（經 §7 步驟 11 的清冊） |
| 沒有 Go 消費者的檔（GitHub workflow、pre-commit 設定、mkdocs 設定、repo 內部資料） | — | 嚴格讀法（見下方第二條邊界） |

範例：Alertmanager 設定裡的 `match: {tenant: 010}`，`da-read alertmanager` 讀出字串 `"010"`（Alertmanager 以 yaml.v2 解碼進字串欄位，取原文）；同一段文字以 Kubernetes 的讀法是數字 8（YAML 1.1 把開頭是 0 的數字當八進位）。所以先看檔案由誰讀。

**`da-read`**：呼叫消費者自己的載入函式，印出載入後的設定，只留原文寫了的鍵（沒寫的鍵、補上的預設值不印）。載入函式拒收、或原文寫了的鍵在載入結果裡對不上時拒收並點名路徑。`da-read` 是獨立的 Go 模組；它編進的 Prometheus 版本與部署的 Prometheus 映像相同，由版本釘值測試守住。`da-read prometheus` 與 Prometheus 一樣匯入上游的 `discovery/install`，註冊所有內建的服務探索設定型別，否則 `kubernetes_sd_configs` 這類欄位會被拒收。vmalert 也讀同一批規則檔，但權威讀法以 rulefmt 為準；vmalert 讀不讀得了交給既有的 vmalert-tool 測試（`tests/rulepacks/test_vm_alert_parity.py`）。

**`da-crdecode` 的兩種讀法**：同一個轉換（sigs.k8s.io/yaml，照 `kubectl apply -f` 切文件），差在對文件形狀的要求。

- **manifest（預設）**：每份文件必須是有 `kind` 的物件，`apiVersion` 與 `metadata` 必須能解碼成 kubectl 讀它們用的型別（Kubernetes apimachinery 的 `TypeMeta` 與 `ObjectMeta`），判準直接呼叫同一個函式庫，所以接受與拒收都和 kubectl 相同。list（有頂層 `items` 的文件）照 kubectl 換成它的 items；item 的 `kind` 與 `apiVersion` 都沒寫時從 list 繼承（`ConfigMapList` 的 item 是 `v1` 的 `ConfigMap`）。比 kubectl 嚴的地方：item 也做型別檢查（kubectl 讀檔時不做，但這樣的 item 套用時會被拒收或被改掉）；list 在 `items` 旁邊帶自己的內容時拒收（kubectl 會不出聲地丟掉它）；任何一份被拒收時整個檔都不輸出。
- **`--any-shape`**：讀不是 manifest 的 YAML（Helm values、單獨的 rule file、ConfigMap 裡的字串值），只轉換、不要求形狀。

輸出帶 `reading` 欄寫明是哪一種讀法，另帶每份文件的 `differs_from_text`（Kubernetes 讀法與原文不同的鍵與字串）。Python 端拿到的 `reading` 不是它要的那一種時當成讀不到。

```text
$ da-crdecode list.yaml          # kind: List，兩個 ConfigMap
{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"}},{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"b"}}],"differs_from_text":[[],[]],"reading":"manifest"}
$ da-crdecode cmlist.yaml        # kind: ConfigMapList，item 只寫了 metadata 與 data
{"documents":[{"metadata":{"name":"a"},"data":{"k":"v"},"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]],"reading":"manifest"}
$ da-crdecode values.yaml        # replicaCount: 1（結束碼 2；stderr 是第一行，stdout 是被拒文件）
da-crdecode: values.yaml: it is not a Kubernetes object: it has no `kind` (kubectl: Object 'Kind' is missing)
{"answer":"refused"}
$ da-crdecode --any-shape values.yaml
{"documents":[{"replicaCount":1}],"differs_from_text":[[]],"reading":"any-shape"}
```

**`tenant-api state show` 與 `config check`**：tenant-api 自己的子命令，呼叫 server 用的同一組解析函式（先經 `parse-tree` 的判定，再解碼，最後是該紀錄自己的檢查）。`state show` 讀 server 設定的 state store，或以 `--from` 讀備份，輸出 `tenant-api.state-show/v1`：每筆紀錄恰有 `record`（解碼結果）或 `problem` 其中一個。`config check --kind rbac|tenant-orgs` 判一份 RBAC 設定或 `_tenant_orgs.yaml`，輸出 `tenant-api.config-check/v1`。Python 工具（例如 `offboard_tenant` 的 `--state`、`check_admin_config_schema`）讀這些 JSON；工具用到的紀錄帶 `problem` 時算未判定，不當成空。

**第一條邊界：有 Go 消費者的檔，Python 不得以 PyYAML 讀。** AST lint（`tests/shared/test_yaml_reader_boundary.py`，解析 Python 語法樹找出呼叫 YAML 解析器的地方）列出每個呼叫點；呼叫點要列在白名單（`tests/shared/_yaml_reader_baseline.py`），逐項附理由，白名單只收沒有 Go 消費者的讀者。有 Go 消費者但還沒遷移的呼叫點列在另一份只減不增的 baseline，現在是空的。

**第二條邊界：Python 讀 YAML 只有一種讀法，就是規格的讀法——同一個 mapping 裡的鍵必須唯一。** 讀法只有 `_lib_yaml_strict` 一處，重複的鍵即拒收；PyYAML 預設會不出聲地取後者。`tests/shared/test_yaml_strict_only.py` 掃 repo 裡每個被追蹤的 Python 檔與嵌在其他檔裡的 Python，在 `_lib_yaml_strict` 以外引用 PyYAML 或 ruamel.yaml 的讀取入口就失敗；沒有例外清單。測試不掃：測試讀的是自己寫的 fixture，嚴格讀法自己的測試也需要寬鬆讀法當對照組。客戶的檔案也不例外：即使它的消費者取後者（例如 GitLab 讀 `.gitlab-ci.yml`），重複鍵也是回報給客戶修的錯。

```yaml {.illustrative reason="刻意示範重複鍵：init 的 .da-init.yaml，嚴格讀法拒收它"}
# .da-init.yaml（init 記錄自己寫過哪些檔）
files:
  ci/a.yml: h1
files:
  ci/b.yml: h2
```

以 PyYAML 預設的讀法，第一段 `files:` 消失，init 會把它寫過的 `ci/a.yml` 當成不是自己的；嚴格讀法拒收這份檔，init 點名重複的 `files`、什麼都不刪。

## 背景

同一份 conf.d 曾被兩個 YAML 解析器各讀一次：Go 端（exporter、tenant-api、da-guard）用 yaml.v3，Python 端（路由產生器與各工具）用 PyYAML。兩者對同一份檔常讀出不同結果。例如租戶 `_routing` 裡用 `!!merge` 標記的鍵：

```yaml {.confd .expect-refused=tag_not_allowed}
# t1.yaml
tenants:
  t1:
    _routing:
      !!merge q: {receiver: {type: slack, api_url: {secret: {key: slack-api-url}}}}
```

PyYAML 把它當成合併，t1 的 receiver 是 `slack`，domain policy 禁止 slack 時產生器報錯；yaml.v3 把 `q` 當成普通的鍵，receiver 取平台預設值，da-guard 判定合規。同一份檔，兩個工具給出相反的結論，而放行的是 Go 這一邊。現在 `parse-tree` 判這份檔為 `tag_not_allowed`，所有讀取端都拒收它。

同類的落差還有：

| 寫法 | PyYAML | yaml.v3 |
|---|---|---|
| `send_resolved: yes`、`on`、`off` | 布林 | 字串 |
| `a: "x\/y"`（引號字串裡的 `\/`） | `x/y` | 整份讀不了 |
| `a:` 後面接 TAB 再接值（`a:<TAB>"v"`） | 整份讀不了 | 正常讀出 |
| 一個檔裡有多份文件 | `safe_load` 整份讀不了 | 只讀第一份，其餘靜默忽略 |

對齊的做法是讓一邊在自己的語言裡重寫另一邊解析器的每一條處理路徑：Go 端曾有專門模仿 PyYAML 的套件與 vendored yaml.v3 的 patch，Python 端有多個模仿 yaml.v3 的 loader。每補一條，旁邊就露出另一條；用隨機輸入同時餵兩邊比對，找得到落差，但證明不了兩邊相等。限縮 conf.d 到「兩邊必然讀出相同結果」的寫法也不可行：上表第二、三列的值都已加上引號、沒用任何進階功能，兩邊仍讀出不同結果。

只把語意收斂到產生器也不夠：產生器仍用 PyYAML 讀、Go 端仍用 yaml.v3 讀，語法層的落差照樣存在。Go 端自己也有不只一個判定（exporter 以 yaml.v3 直接解碼，照讀多份文件、`!!binary`、檔案中段的 BOM 這類產生器拒收的檔），寫 conf.d 的 Python 工具以 PyYAML 讀寫、會丟註解，conf.d 以外的 YAML 又由 PyYAML 模仿 kubectl、Alertmanager 這些別的讀者。

問題的根源是「兩個解析器各讀一次，又要求結果一模一樣」。唯一不需要證明兩邊相等的做法，是只留一個解析器。

## 後果與已知限制

**對使用者與維運的影響（都是 breaking change）**：

- 依舊版 YAML 規則成立的寫法，意思可能改變，部分檔案會變成讀不了（§8）。
- exporter 不服務 `parse-tree` 判為讀不了的檔：一個檔裡有多份文件、`!!binary` 這類白名單外的型別標記、檔案中段的 BOM 等，以前 exporter 照讀，現在檔內租戶的告警停止。緩解只有 §8 的升級前把關與 metric、log，不設「照舊讀」的開關。
- 寫 conf.d 的工具改由 yaml.v3 輸出，客戶的 diff 會出現與這次修改無關的格式差異（§2）。
- 讀 conf.d 的 Python 工具需要 da-guard：da-tools 映像已內附；工具依序找 `$DA_GUARD_BINARY` 與 `PATH`；本機的 pre-commit hook 沒設 `DA_GUARD_BINARY` 時以 Go 建出 `.build/da-guard`。找不到也建不了時工具失敗並說明原因，不會退回自己解析。
- tenant-api 多一個容器；容器或部署端 policy 檔不可用時，所有會改變檔案內容的寫入回 503（§5）。tenant-api 必須搭配 git。
- 樹上任何地方有會擋下的問題，經 tenant-api 的寫入都被拒，直到修好；批次全有全無（§4）。
- 只跑 da-guard 的 CI 不再涵蓋路由與 policy，要另外加一步 `da-tools generate-routes --validate --strict`（§9）。

**不可協商的驗收條件**：

- 任何讀取端都不得比產生器寬鬆。tenant-api 的寫入判準就是產生器自己的結果（§4），所以這一條對 tenant-api 由定義保證。
- 「讀不了」是封閉清單：固定語料 `tests/shared/parse_tree_corpus.json` 每一列帶 `parse-tree` 應有的分類，清單裡的每一種 `kind` 至少有一列。
- 同一份檔，每個 Go 讀取端判定「讀不了」的結果都相同：它們呼叫同一個套件。

**已知限制**：

- **判定成本**：每份檔在解碼前多一次判定。exporter 以檔案內容的 SHA-256 保留判定結果，上限 8,192 筆，滿了整份清掉；一個程序遇到的不同內容多於上限時，快取一再清空，成本退回無快取的數字，判定結果不受影響。
- **快取保留**：產生器容器的快取、淘汰規則與快取大小的 metric 都要維護（§3）。
- **映像體積**：`da-read prometheus` 為了讀得了用 `kubernetes_sd_configs` 的 `prometheus.yml`，註冊全部服務探索，連帶編進各家雲端 SDK 與 Kubernetes client。2026-11-24 在 commit `d77a3ecb4` 上以出貨用的建置（`scripts/ops/go_build.sh`，linux/amd64）量得：

  | `da-read` | 未壓縮／`gzip -9` 後 |
  |---|---|
  | 現在的 `da-read` | 81.1 MB／23.4 MB |
  | 同一份原始碼拿掉 `discovery/install` 的匯入 | 20.0 MB／7.5 MB |

  換來的是 `prometheus.yml` 的讀法與 Prometheus 相同。
- **取得 state 需要 tenant-api 本身**：線上要能 `kubectl exec` 進 tenant-api 的 pod，離線讀備份要能以 docker 跑部署中的 tenant-api 映像。兩者都沒有的站點取不到，`offboard_tenant` 只能加 `--skip-state-check`。有站點回報這種情境時，才另建一支呼叫同一組解析函式的 binary 打包進 da-tools。
- **升級 yaml.v3 基底**：之後再升級上游版本，先以固定語料比對新舊基底的讀法。
- **從 git 取檔比檔案系統嚴**：同一棵樹可能客戶 CI 綠、tenant-api 因 symlink 拒寫（§3）。

## 考慮過的替代方案

| 方案 | 為什麼沒選 |
|---|---|
| 繼續在兩端互相模仿 | 每補一條旁邊就露出另一條，比對找得到落差、證明不了相等（「背景」） |
| 限縮 conf.d 到兩個解析器必然讀出相同結果的寫法 | 值都已加引號、沒用進階功能時兩邊仍讀出不同結果；這種子集只能抽樣，無法定義 |
| YAML 改由 Python 統一解析，Go 讀 Python 輸出的 JSON | exporter 是執行期元件，依賴 Python 會讓每個 exporter pod 都要帶 Python 執行環境 |
| `parse-tree` 拒收 PyYAML 拒收的一切 | 等於在 Go 裡重寫 PyYAML 的每一項檢查，又讓 Go 模仿另一個解析器；兩邊比對一再找到新的「只有 PyYAML 拒收」的類別。改為封閉清單，其餘照 yaml.v3 讀 |
| 產生器拒收別名、合併鍵與顯式型別標記 | 理由是縮小兩個解析器的落差；只留一個解析器後已沒有落差可縮，卻會要求客戶改寫既有的設定檔 |
| exporter 只觀測、不拒收讀不了的檔 | 等於保留兩個判定，同一份檔仍可能 exporter 服務、產生器拒收 |
| 寫入時只看屬於這次寫入的 finding（逐筆歸屬） | 產生器的驗證依序短路，前面有錯後面的檢查就不跑；實跑的反例中，鄰居的違規遮掉了被寫入檔的問題，平台檔寫壞產生的 finding 不屬於任何一筆寫入 |
| 只比對寫入前後新增了哪些錯誤訊息 | 已經違規的租戶改寫設定時，會產生和 base 一模一樣的訊息，比對差異會放行 |
| 寫入前後的渲染結果相同就不擋 | 會漏：在 `group_by` 加一個空值，產生器多報一筆擋下的 finding，渲染結果卻不變 |
| 在常駐程序本身判定，不 fork 子程序 | 一筆過大或惡意的寫入會拖垮常駐程序，讓之後所有寫入都回 503 |
| 容器不可用時退回 Go 的判定，或設放行開關 | 能送寫入的人可以影響容器是否可用，等於讓請求方自己挑判定者 |
| 就緒只看最近一次判定有沒有工具故障 | 第一次判定之前一律回報就緒，程式根本不存在也一樣；跑完全程的判定可能因內容很早被拒而沒走到出問題的程式 |
| 分兩階段切換（新舊並行、比對差異）或用開關切換 | 要維護兩份判定與差異清單，而 Go 端判定已知較寬鬆，留著它沒有好處 |
| 在整合分支上做完再一次合併 | CI 只由對 main 的 PR 與 main 上的 push 觸發，整合分支上的每一步都沒有 CI 把關 |
| 所有 YAML 都以 yaml.v3 讀 | 對 Kubernetes manifest 是錯的方言：kubectl 採 YAML 1.1 的規則，`name: no` 在 kubectl 是 `false`，在 yaml.v3 是 `"no"` |
| 以 `import yaml` 劃邊界 | 太寬（只讀 GitHub workflow 的檔也被擋），又可被包裝函式繞過；改以 AST lint 找呼叫點 |
| Python 讀 `parse-tree` 的結果，自己換算出 tenant-api 的狀態紀錄 | 要在 Python 重寫一份依欄位型別的換算與每種紀錄的拒收規則（`members: abc` 在 tenant-api 整筆拒收，`parse-tree` 判為讀得了），兩份讀法會各自演變 |
| 把整理好的 policy 另存一份檔案 commit 進 git | 只涵蓋 policy，路由仍由兩邊各自解析；忘了重產時會沿用舊版 policy |
| 把 Python 的路由與 policy 語意完整移植到 Go | 等於再造一份語意實作；只在加容器的成本無法接受時才考慮 |
| 寫入 API 只收 JSON | 只解決 tenant-api 的寫入；客戶手寫的 YAML 在 CI 與 exporter 仍要被解析 |

## 實作位置

| 本文用語 | 程式碼 |
|---|---|
| `parse-tree` 的判定 | `components/threshold-exporter/app/pkg/parsetree`；da-guard 子命令 `cmd/da-guard/parse_tree.go`、`parse_tree_list.go` |
| Go 端的檔名規則 | `pkg/confdname` |
| 編輯原語與合併不變式 | `pkg/confedit`（`Verify`）；`da-guard write` 在 `cmd/da-guard/write.go` |
| Python 讀 `parse-tree` | `scripts/tools/_lib_parse_tree.py`；送寫入 `scripts/tools/_lib_confedit.py` |
| 產生器容器 | `scripts/tools/ops/generator_service.py`、`_gsvc_child.py`；tenant-api 的呼叫端 `components/tenant-api/internal/generator` |
| tenant-api 經共用判定的入口 | `internal/confd/verdict.go` 的 `Judge` |
| tenant-api 的 `state show`／`config check` | `components/tenant-api/cmd/server/state_cmd.go`、`config_cmd.go`；Python 讀取端 `scripts/tools/_lib_tenant_state.py` |
| `da-crdecode`、`da-read` | `components/threshold-exporter/app/cmd/da-crdecode`、`components/da-read` |
| vendored yaml.v3 | `components/threshold-exporter/app/third_party/yaml.v3`（說明在同層 `README.md`） |
| 刪除範圍的宣告表 | `tests/shared/phase2_deletion_scope.json` |

## 相關

- [ADR-023 tenant-api 寫入平面：單一寫者不變式](./023-write-plane-single-writer-invariant.md)
- [ADR-037 config-diff 改為比較渲染結果](./037-config-diff-compares-rendered-output.md)
- [ADR-038 沒有讀取端讀的鍵以實測判定](./038-unread-keys-judged-by-readers.md)
- 設計討論與量測：[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)（語意層）、[#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)（切換與寫入判準）、[#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858)（語法層）
- 業界做法：只解析一次、驗證交給權威端，見 [Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/)；把解析工具放進使用者的 pod，見 [Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/)；驗證端失效時拒絕而不放行，見 [Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/)。
