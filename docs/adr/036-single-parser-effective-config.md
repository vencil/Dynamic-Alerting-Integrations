---
title: "ADR-036: conf.d 的路由與 policy 只由產生器解析一次，Go 讀取端吃它的輸出"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-036
tracking_kind: adr
status: proposed
domain: platform
created_at: 2026-10-09
updated_at: 2026-10-09
---

# ADR-036: conf.d 的路由與 policy 只由產生器解析一次，Go 讀取端吃它的輸出

> **Language / 語言：** **中文 (Current)** | [English](./036-single-parser-effective-config.en.md)

## 狀態

🟡 **Proposed**（2026-10-09 起草）。

- 方向（D3）與各待解問題已由 owner 在 [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) 與 hub [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486) 拍板。
- 設計提案已經過一輪外部對抗審查：兩個不同模型，一個偏工程面、一個偏 parser differential 與安全面。被打穿的點都已改進本文。
- ADR 文本本身也再走了一輪外部審查（不同模型）。核實後的意見已併入，包括批次寫入的重新序列化問題（owner 已拍板改由 sidecar 合併）。
- 待 owner 核可本文。

## 摘要

**問題**：同一份 conf.d 由兩種 YAML 解析器各讀一次——Python 的路由產生器（純 Python PyYAML，權威）與 Go 的 da-guard、tenant-api（vendored yaml.v3）。規格要求 Go 的判定等於產生器，於是 Go 端長出一大段手寫的 PyYAML 模擬碼。這條路不收斂：[#2763](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2763) 修了四輪，每輪都引入新的 regression；main 上至今仍量得到 Go 讀得比產生器寬的形狀。

**決定**：

1. 每一種語意只有一個解析者。**路由、domain policy、租戶列舉歸產生器**；閾值歸 exporter。
2. tenant-api 的 pod 加一個 da-tools sidecar，由**真正的產生器程式**輸出有效設定 JSON，並提供寫入驗證端點。tenant-api 不再自己解析路由與 policy 的 YAML。
3. 寫入驗證是**絕對判定**：在候選樹上跑產生器，結果綁定到 `(path, sha256, base_rev)`，在 writer lock 下核對後才 commit。
4. 過渡期先取 Go 與產生器判定的**聯集**（任一方拒收就拒收），sidecar 不可用時寫入回 503；觸發條件滿足後刪除 Go 端的模擬碼。

**驗收條件（不可協商）**：任何讀取端都不得比產生器寬——不得讀出產生器丟掉的 policy，也不得漏掉產生器執行的 policy。P1 起對寫入與讀取都成立；P0 只對寫入成立（見「過渡」）。把關方式見「驗收的機械化」。

## 問題

### 為什麼 Go 端模擬 PyYAML 不收斂

- 每一輪修正都在 Go 裡手寫 PyYAML 的另一條建構路徑：`construct_mapping` 跳過 null key、`construct_yaml_omap`、`raw_text_sequences`、`flatten_mapping`。每補一條，旁邊就再露出一條；建構順序本身也會決定 PyYAML 的判決。
- 先前試過的替代方向失敗原因都一樣：在 yaml.v3 node 上判斷 PyYAML 會不會接受、由兩個 parser 各自判定語言子集、用位元組規則擋 directive／TAB、改寫 directive 字面。
- 差分 fuzz 能找到分歧，但證明不了等價。

### main 上量得到的寬分歧（`d8525326`，2026-10-09）

| 形狀 | 產生器 | da-guard | tenant-api |
|---|---|---|---|
| 600 層巢狀 | 丟檔 | 讀出 policy | 讀出 policy |
| `!!set` 放在 `domain_policies`／domain／`constraints` | 不執行 | 照讀 | 照讀 |
| merge 值裡的 mapping 帶 collection key | 丟檔 | 拒收 | 讀出 policy |
| UTF-8 BOM 跨過 byte 512、放在註解裡 | 執行 policy | 靜默讀不到 policy | 靜默讀不到 policy |
| 租戶 `_routing` 的 `!!merge q:`（[#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)） | merge 進來的 receiver 違反 policy，rc 1 | 當成普通鍵，rc 0 | [未驗] |
| 租戶 `_routing` 裡被蓋掉的 merge 來源有不可建構值（[#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)） | 拒讀該檔，rc 1 | rc 0 | [未驗] |

完整清單與嚴的分歧見 [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)。

### 範圍不只 policy 檔

tenant-api 判 `require_critical_escalation` 等規則時，用的是租戶合併後的路由（`_routing_defaults`、`routing_profiles`、租戶 `_routing`），這幾層也都由 Go 解析。租戶列舉、重複租戶與層級判定（exporter 的 `ScanDirTree` 與 `declaredTenantIDs`）同樣是兩套 parser 各算一次。

## 決定

### 1. 語意的擁有者

| 語意 | 唯一解析者 | 其他讀取端怎麼拿 |
|---|---|---|
| 路由（`_routing_defaults`、`routing_profiles`、租戶 `_routing`）、domain policy、租戶列舉與目錄對應 | 路由產生器（Python） | 讀產生器輸出的有效設定 JSON |
| 閾值、served values | exporter（Go） | 已經是這樣：Python 工具經由 `da-guard served-values`／`effective` 的 JSON 讀（`_lib_tenant_values`） |

閾值那一列是反方向的現成先例，本 ADR 不改它。

### 2. 架構

- tenant-api pod 加一個 **da-tools sidecar**，跑現成的 da-tools 映像（內含產生器）。
- 與 tenant-api 之間以 **Unix domain socket** 溝通，socket 放在共用的 emptyDir。不開 TCP 埠：只有掛載了該 volume、且符合 socket 檔案權限的程序能連線；沒有掛載該 volume 的同 pod 容器與網路上的對端都碰不到。
- sidecar 讀樹的方式：依指定的 commit，以 `git ls-tree -r` 加 `git cat-file --batch` **物化**到私有目錄。
  - 不讀共用工作目錄：PR 模式會在 `/conf.d` 就地 `git checkout -f` 切分支。
  - 不用 `git archive`：它會套用 `.gitattributes` 的 `export-ignore` 等屬性。本 repo 沒有這種標記，但客戶的 conf.d repo 若把 `_domain_policy.yaml` 標成 `export-ignore`，匯出時它會直接消失，sidecar 因而讀得比客戶 CI 寬。
- 物化後的樹以 `base_rev` 快取；解析結果以「檔案內容 hash ＋產生器版本」為 key 逐檔快取，不快取跨檔推導的結果。
- sidecar 的防護：限制請求大小、巢狀深度與執行時間；記憶體上限 128–256Mi；根檔案系統唯讀，暫存走 tmpfs。

### 3. 有效設定 JSON（讀取用）

- 內容：每份 policy 檔是否可用；各 domain 的 tenants、forbidden／allowed receiver types、`require_critical_escalation`；各租戶合併後的路由；租戶與檔案、目錄的對應。
- 外層欄位：`schema`（主版號不符就整份判不可用）、`min_reader`、`generator_version`、`tree_rev`。
- 編碼採 I-JSON（RFC 7493）的約束：
  - Python 端 `ensure_ascii=False, allow_nan=False, sort_keys=True`，再以 UTF-8 編碼；孤立 surrogate 會在編碼時失敗，該次產生失敗。
  - 不採 `ensure_ascii=True`：Go 的 `encoding/json` 會把 `\ud800` 跳脫靜默換成 U+FFFD，等於把分歧搬進 Go。
  - Go 端用嚴格解碼：拒絕重複 key（token-stream 檢查，不新增依賴）、無效 UTF-8、未知欄位；數值用 `json.Number`，時長與閾值一律以字串輸出。
  - 以 temp 檔加 rename 原子寫入。
- 遇到不認得的限制種類，該 policy 判不可用，不得忽略（比照 X.509 critical extension）。
- **重新產生的時機**：tenant-api 每次 commit 之後通知 sidecar；另外每個輪詢週期比對 HEAD，有變就重產。
- last-good **只服務讀取**，而且：
  - 以檔案為單位，與今天 policy watcher 的逐檔 last-good 相同。只保留新 `tree_rev` 裡仍然存在的檔案；新樹已刪除的 policy 檔不得以 last-good 延續。
  - 帶 `tree_rev` 與產生時間；距最後一次成功產生超過上限就判不可用並告警。

### 4. 寫入驗證

- **判準是絕對的**，與今天的 `judgePutBody` 相同。sidecar 在「base 樹替換掉這次寫入的檔」的候選樹上跑產生器，有以下任一情況就拒收：
  - 任何 blocking finding 的主體是這次寫入的租戶或檔案；
  - 候選樹出現 base 沒有的樹層級失敗（檔案讀不了、重複租戶、路由樹錯誤等）。
- 不比對 finding 字串的差集：已經違規的租戶重寫時會產生同一行字串而被放行；解析失敗印的是 `FAIL:` 而不是 `ERROR:`，用 prefix 根本比不到。
- **前置工作**：產生器先輸出結構化 finding（`policy`、`tenant`、`file`、`kind`、是否 blocking），上述判準才不必靠字串。
- **樹層級失敗也以結構化的 `kind`＋`file` 比對**，不比對文字。「base 沒有、候選樹才有」是對這組結構化欄位的集合比較；租戶主體的 finding 則是絕對判定，兩者不可互相替代。
- **base 本身已經有樹層級失敗時**：允許「這次寫入的主體乾淨、也沒新增樹層級失敗」的寫入，不讓一個壞掉的鄰居檔擋下所有人。
- **送驗的門檻是「會不會產生新的檔案位元組」**，不是 Go 對檔案內容的判讀。凡會改寫檔案位元組的寫入都送 sidecar 驗；`tenantBlockOnDisk` 讀不了檔時「當成空、只判 patch」的 fail-open 刪除。
- **綁定與競態**：
  - sidecar 回傳它判過的 `(path, sha256(bytes), base_rev)`；writer 在 lock 下核對三者，不一致就重驗。
  - 送驗的是**最終要寫入的那串 bytes**，writer 寫入的必須與它逐位元組相同。PUT 本來就是原文。
  - **批次 patch 的合併移到 sidecar**（owner 2026-10-09 拍板）：目前 `mergePatchYAML` 以 yaml.v3 讀出既有檔再重新輸出，會把 Go 的讀法寫進檔案（例如 `!!merge q:` 被改寫成普通鍵 `q:`），即使該操作沒碰路由。改由 sidecar 以產生器的讀法合併並回傳結果 bytes，tenant-api 只負責寫入。P0 期間仍由 Go 合併，sidecar 驗合併後的 bytes（能擋下違規結果，擋不住無違規的語意改寫）；P1 起改由 sidecar 合併。
  - PR 模式的驗證放進 `WritePRChecked` 的 fresh-base 閉包。
  - direct 模式不 push，`base_rev` 就是本機 HEAD；在 lock 下確認 HEAD 仍等於 `base_rev`。
- **批次寫入**：一次請求帶整批檔案，驗最終狀態與每個前綴狀態；lock 持有到全部 commit 完成。批次的操作數要有上限，前綴驗證共用同一份物化樹與解析快取。
- **失敗一律拒收**：例外、rc 不為 0、輸出被截斷或沒有明確的 OK 標記加回傳的 hash，都當成拒收。
- **回給 API 用戶的錯誤**只含主體是這次寫入租戶的 finding，不回其他租戶的違規內容。
- P0 的 Go 判定就是聯集裡的那一半（見「過渡」）；P1 起不保留 Go 預檢，避免 yaml.v3 解析失敗而拒收客戶 CI 接受的寫入。

### 5. 版本對齊

- 可以保證的不變式是「**tenant-api 不比 sidecar 內的產生器寬**」。「等於客戶 CI 用的那一版」做不到：客戶 CI 的 da-tools 版本獨立於 tenant-api。
- 收斂版本落差的做法：
  - tenant-api chart 預設釘一個 da-tools 映像版本，CI 對這一對跑契約測試。
  - `da-tools init` 產生的設定改成釘 tag，不再用 `latest`。
  - 新增：policy 檔可選擇宣告 `generator_min_version`（目前 repo 內沒有這個鍵）；sidecar 比它舊就拒收寫入。這個鍵只由產生器讀，Go 不為它解析 policy 檔。
  - tenant-api 以 metric 曝露 `generator_version`。
  - 加一個測試：產生器能輸出的每一種限制種類，都必須在 Go 的已知清單裡。

### 6. 過渡

| 階段 | 寫入判定 | sidecar 不可用時 | 讀取 |
|---|---|---|---|
| P0 | Go 與產生器的**聯集**：任一方拒收就拒收 | 涉及路由或 policy 的寫入回 503；不退回只用 Go | 現有 Go 讀法照舊，另記錄不一致 |
| P1 | 只信產生器；Go 模擬碼（`shapeWork` 等）刪除 | 寫入回 503 | 讀有效設定 JSON |

- P0 不退回只用 Go：sidecar 是否可用可以被請求方影響（大量昂貴請求），而 Go 單獨判定有已知的寬分歧，退回等於讓攻擊者選擇較寬的判定者。業界對照：Kyverno 預設 `failurePolicy: Fail`；Gatekeeper 預設 Ignore，在被當成安全控制時有被繞過的風險。
- P0 → P1 的觸發條件：一段 shadow 期內，每一筆判定不一致都有穩定的原因碼，並列在明確的「已解釋」清單裡，沒有未解釋的不一致。shadow 期長度待定。
- 回退旗標 `--policy-source=go|union|generator`。P0 之後 `go` 不是預設值，選用時大聲記錄。
- 既有的 `--policy-unavailable-open` 維持原意（policy 檔不可用時放行），**不**涵蓋 sidecar 不可用；sidecar 不可用沒有放行開關。
- **P0 期間的讀取是驗收條件的明示例外**：GET 與 policy watcher 仍用 Go 讀法，#2759 列的已知分歧在讀取側持續存在，直到 P1。寫入在 P0 就必須滿足驗收條件。

### 7. da-guard

da-guard 與產生器在同一個 da-tools 映像裡。路由與 policy 的判定改向產生器取，不再自己解析這部分 YAML；閾值相關的讀取（exporter 語意）不變。具體介面見「待決問題」。

## 驗收的機械化

- **fuzz**：隨機產生 conf.d 樹 → 跑產生器得到有效設定 JSON 與結構化 finding → 同一棵樹給 Go 讀取端 → 兩個方向都斷言：Go 執行的 policy 與租戶集合**等於** JSON 的（多讀與漏讀都算失敗），且 JSON 解碼只有「完整成功」或「整份拒收」兩種結果。比較器本身不解析 YAML。
  - P1 起，這個等式對寫入與讀取都要成立。
  - P0 只斷言寫入側：聯集判定拒收所有產生器拒收的寫入。讀取側的已知分歧是「過渡」一節列出的明示例外，不納入斷言，直到 P1。
- **凍結回歸語料**：#2759、#2700、#2713、#2674 的形狀，連同現有的 `merge_key_policy_corpus.json`，做成固定案例。
- 在 CI 對 chart 釘的那一對映像跑，不是臨時手動跑。

## 成本（實測，2026-10-09）

| 項目 | 量測 |
|---|---|
| 產生器整體，1000 租戶 | 1.72 秒、38 MB；其中解析 1.06 秒，每個檔被解析兩次 |
| 單檔 SafeLoader | 0.47 ms |
| Python 冷啟動 | 0.11 秒、25 MB |
| `ls-tree`＋`cat-file` 物化 1000 檔 | 0.21 秒、12 MB |
| 映像大小（壓縮後，amd64） | da-tools v2.9.0 36.9 MB；tenant-api v2.7.0 14.0 MB |

單筆寫入在快取命中時，主要成本是單檔解析加檢查。檢查加渲染約 0.6 秒，這是推估，沒有單獨量。

## 業界做法

- 只解析一次，下游吃標準形式：Kubernetes 的 `sigs.k8s.io/yaml` 先轉 JSON；Envoy 先轉 protobuf；OPA／conftest 判 `terraform show -json` 而不是 HCL 原文。
- 解析工具以 sidecar 形式放在使用者的 pod 裡：Argo CD 的 Config Management Plugin（repo-server 的 sidecar）、OPA 的 sidecar 部署、Envoy Gateway 以 Unix socket 連 OPA sidecar。
- 介面格式的約束：I-JSON（RFC 7493）。
- LangSec：處理前先完整辨識。

## 考慮過但否決

| 方向 | 否決理由 |
|---|---|
| 繼續在 Go 端模擬 PyYAML | 不收斂，見「問題」 |
| D1：commit 進 git 的 policy 鎖檔 | 只涵蓋 policy，路由仍由 Go 解析；忘了重產時冷啟動 503、暖機時照舊版把關 |
| D2：三方共用自訂文法 | YAML 1.1 的 scalar 型別會撞上真實租戶 id（`no`、`0777`）；breaking change |
| O2：產生器改用 libyaml | 只對齊 scanner 層，regression 都在 constructor 層 |
| Go 忠實移植 PyYAML（約四、五千行） | 備案，只在 sidecar 的成本被否決時才考慮 |
| 寫入 API 只收 JSON | breaking change；Go 仍要輸出 YAML 給 PyYAML 讀，等於換方向的雙 parser |
| 比對 finding 字串的差集 | 比今天的絕對判定寬，見「寫入驗證」 |
| 以 `git archive` 匯出樹 | 套用 `export-ignore`，可能讓 policy 檔消失 |
| P0 時 sidecar 掛掉退回只用 Go | 攻擊者可選擇較寬的判定者 |
| 寫入時使用 last-good | 版本落差時舊 policy 被無限期沿用 |
| `ensure_ascii=True` | 分歧搬進 Go 的 `encoding/json` |

## 待決問題

1. **da-guard 的介面**：da-guard 是直接呼叫產生器的 Python 模組，還是讀一份由產生器先產出的 JSON（例如 `--effective-json`）？後者讓 da-guard 在沒有 Python 的環境（CI 自建 binary）也能跑。
2. **P0 shadow 期的長度**與「已解釋不一致」清單的審核方式。
3. **tenant-api 的 GET 端點**：讀閾值的部分屬 exporter 語意、不在本 ADR 範圍；讀路由的部分改讀 JSON 的時程隨 P1。
4. **P1 刪除範圍**：vendored yaml.v3 的三段 patch 中，只服務 policy 路徑的（`SpacesOnly`、nonspecific-tag）是否一併移除；`uniquekeys` 是否仍有 exporter 使用者。

**已決定、在此記錄的取捨**：
- Unix socket 的授權邊界是「掛載了該 volume、且符合 socket 檔案權限的程序」，也就是 tenant-api 與 sidecar 這兩個容器裡的程序都能呼叫驗證端點，視為可接受。沒有掛載該 volume 的容器不能呼叫。
- sidecar 只以 `cat-file`／`ls-tree` 依 rev 讀 git 物件，不碰 index 與工作目錄，不與 PR 模式的 `checkout -f` 搶 `index.lock`。

## 實作切分

1. 產生器輸出結構化 finding（前置）。
2. sidecar：有效設定 JSON 與寫入驗證端點；Helm chart 加容器、socket 與資源設定。
3. tenant-api：寫入路徑呼叫 sidecar（P0 聯集）、讀取改吃 JSON。
4. da-guard 改向產生器取路由與 policy 的判定。
5. P1：刪除 Go 端的 PyYAML 模擬碼，於 main 重測 [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)、[#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)、[#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)、[#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674) 後關票。

## 相關

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)（設計與外部審查紀錄）、hub [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)
- [ADR-023 tenant-api 寫入平面：單一寫者不變式](./023-write-plane-single-writer-invariant.md)
