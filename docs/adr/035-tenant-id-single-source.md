---
title: "ADR-035: tenant id 合法字元集的單一來源"
tags: [adr, config, multi-tenant, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-035
tracking_kind: adr
status: proposed
domain: platform
created_at: 2026-10-03
updated_at: 2026-10-03
---

# ADR-035: tenant id 合法字元集的單一來源

> **Language / 語言：** **中文 (Current)** | [English](./035-tenant-id-single-source.en.md)

## 狀態

🟡 **Proposed**（2026-10-03 起草）。

- 決策內容已由 owner 在 [#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655) 拍板，其中兩題是外審之後再拍板的。
- 外審由不同模型以對抗方式進行，結論已併入本文。
- 本文尚待 owner 核可。

## 摘要

**問題**

- tenant id 的合法字元集在 repo 內有四份互不一致的規則，分散在 Python、JS 與 #2633 的過渡規則。
- exporter、tenant-api 讀取路徑與 `scaffold_tenant` 則幾乎不檢查。

**決定**

1. 最終規則採 **DNS-1123 label**。
2. 規則**只寫一次**，放在 tenant-config schema。Python 在 runtime 讀取；Go 與 portal 讀取由 schema 產生的同一份 JSON 副本。
3. **直接全面阻擋**：所有產生模式遇到不合法 id 一律非零退出，不輸出設定。
4. exporter 照常載入，只印 WARN。

本 ADR 只定規則，實作另開 PR。

## 問題

### 現況（main `ecae7115`）

| 位置 | 規則 |
|---|---|
| `operator_generate.py:54`、`migrate_to_operator.py:49` | DNS-1123 label（`fullmatch`） |
| portal `operator-setup-wizard/utils/generators.js:26` | DNS-1123 形狀，但**沒有 63 字元上限** |
| `alert_quality.py:65` | `^[a-zA-Z0-9_-]+$` |
| 產生器、da-guard、tenant-api 寫入路徑（#2633 過渡規則；`_lib_validation.py:98`、`routingpolicy/tenantid.go`） | 擋下空字串，以及不符 `^[A-Za-z0-9_-]+$` 者 |
| tenant-api 讀取路徑 `ValidateTenantID`（`handler/sanitize.go:14`） | 只擋路徑分隔符、`..`、非 base name、保留檔名 |
| `scaffold_tenant.py --tenant` | 不驗證，id 直接當成 key 與檔名 |
| exporter（`config_file.go` 的 `ParseTenantFile`） | 只要求合法 UTF-8，否則整份檔案拒收 |
| JSON Schema（`check_confd_schema`） | `tenants` 的 key 沒有限制 |

### 為什麼不能停在過渡規則

- **operator 模式需要 DNS-1123。** operator 模式會把 id 放進 K8s 物件名稱（`da-tenant-{id}`、`da-{id}-{receiver_type}`）。物件名稱要符合 DNS subdomain 規則：小寫英數、`-`、`.`，不能有大寫或底線。所以 operator 模式另外需要一份 DNS-1123 規則，結果同一個 id 在 config-driven 模式下合法、在 operator 模式下不合法。
- **多份規則會一直繁殖。** #2341 原本問的就是「該以哪一份為準」。

### 量測

- repo 內 git 追蹤的 `*.yaml`／`*.yml` 中，`tenants:` key 共有 33 個不同的 id，全部符合 DNS-1123，最長 21 字元。
- 另有 3 個 key 來自 CRD schema 結構（`k8s/crd/thresholdconfig-crd.yaml`），不是 tenant id。
- 客戶端實際使用的 id 量不到，這是本決定最大的未知數，見「失敗模式」。

## 決定

### D1：最終規則為 DNS-1123 label

```text
^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$
```

小寫英數與 `-`，長度 1–63，首尾必須是英數。

- **換到的**
  - operator 模式與 config-driven 模式共用同一條規則。
  - 新規則是過渡規則的子集，只往嚴的方向收，符合 #2633「只收緊、不反悔」的設計。
  - 與 K8s namespace 命名一致。
- **犧牲的**
  - 含大寫、底線或長度超過 63 的 id 必須改名。
  - 改名後 metric 的 `tenant` label 值會變，歷史資料不連續。
- **全數字的 id**（例如 `123`）在 DNS-1123 下合法，但 YAML 未加引號時會被解析成整數 key，`check_confd_schema` 會以「不是 string」擋下。因此這類 id 必須加引號，遷移文件要寫明。

### D2：規則寫一次，放在 tenant-config schema

**規則本體**

- 在 `docs/schemas/tenant-config.schema.json` 新增 `definitions.tenantId`（`type: string`、`pattern`）。
- 在 `properties.tenants` 加上 `propertyNames: {"$ref": "#/definitions/tenantId"}`。
- schema 是 draft-07，支援 `propertyNames`，所以 `check_confd_schema` 不需改程式就會開始檢查。外審已用 schema 副本實測：`Team_A` 回 rc 1；repo 內 conf.d 回 rc 0。
- 不另外寫 `maxLength`。pattern 已限制長度，重複寫只會讓違規時出現兩條錯誤。

**Python**

- `_lib_validation.is_valid_tenant_id` 在 runtime 讀這個 pattern，讀法沿用 `_find_tenant_schema()`：在 da-tools image 內讀扁平佈局，在 repo 內往上找專案根目錄。
- build.sh 的 `REPO_DATA_FILES` 已經出貨這份 schema，`check_build_completeness` 也把它列為必要檔案。
- 讀不到 schema 時 fail-closed，作法比照 #2180 的 receiver URL pattern。
- 以下三支工具刪除各自的 regex 或不驗證的寫法，改呼叫這個函式：`operator_generate.py`、`migrate_to_operator.py`、`alert_quality.py`；`scaffold_tenant.py` 原本不驗證，也改為呼叫。

**Go 與 portal**

Go binary 在 runtime 讀不到 repo 的 `docs/`，所以採「產生副本＋漂移守衛」，作法與 `recipe-status.json` 相同：

- 用一支產生腳本，從 schema 抽出 `definitions.tenantId`，寫出兩份 JSON：
  - `components/threshold-exporter/app/pkg/tenantid/tenant_id.json`
  - portal 的對應資料檔
- Go 以 `go:embed` 讀取，`routingpolicy.IsValidTenantID` 改用它。tenant-api 經由既有的 `replace github.com/vencil/threshold-exporter => ../threshold-exporter/app` 引用。tenant-api 的 Dockerfile 會整包複製該 module，`.dockerignore` 沒有排除 `*.json`。
- portal 的 `validateTenantName` 改讀它的資料檔，刪掉自己的 regex。
- pre-commit 加一支漂移檢查：兩份副本都必須等於重新產生的結果。

**為什麼不採 receiverspec 模式**

receiverspec 模式是 Go 端手抄常數，再用 `TestSpecs_MatchSchema` 讀 schema 比對，見 `pkg/receiverspec/spec.go`。這個做法省掉產生腳本，但有兩個問題：

- 規則文字在 Go 端仍然手抄一份，不符合「資料檔、兩種語言都讀」的決定。
- portal 沒有 Go 測試可用，仍會留下第三份。

**pattern 方言**

- pattern 只能使用 ECMA-262、Python `re`、Go RE2 三者語意一致的子集：字元類、`{m,n}`、`^`、`$`，不得使用 lookaround、反向參照或 Unicode 類別。
- 外審實測：本規則在三者對 `abc`、`abc\n`、`é` 的判定一致。
- 比對語意依路徑不同：
  - `is_valid_tenant_id`：用 `re.fullmatch`，因為 Python 的 `$` 會吃掉結尾的 `\n`。
  - Go：以 `^…$` 錨定，RE2 的 `$` 不吃 `\n`。
  - JSON Schema 的 `pattern` 是 **search** 語意，所以 `check_confd_schema` 與編輯器會放過結尾帶 `\n` 的 key（實測 jsonschema 4.26：`{"abc\n": 1}` 回 0 個錯誤）。
- 結尾帶 `\n` 只會出現在 YAML block-scalar 形式的 key。這類 key 會由產生器、da-guard、tenant-api 擋下，所以接受 schema 路徑的這個差異。
- parity matrix 的 `tenant_ids` 表要補一個直接跑 jsonschema 的案例，把這個差異釘住。

- **換到的**：規則文字只有一份，四個讀者都從它取得規則：產生器（Python）、da-guard 與 tenant-api（Go）、portal、schema checker。
- **犧牲的**
  - 多一支產生腳本與一個漂移守衛。
  - Python 在 runtime 依賴這份 schema 檔。這個依賴早已存在（#2180）。

### D3：直接全面阻擋，所有產生模式非零退出

新規則上線後，所有讀者同時改用 DNS-1123 判定：產生器、da-guard、tenant-api 寫入路徑、schema、portal、`scaffold_tenant`。

產生器遇到不合法 id 時，**不論哪種模式都非零退出、不輸出設定**，包括一般產生、`--dry-run`、`--apply`、`--output-configmap`、`--validate`、`--strict`。

**這是第 2 波 1A 規則的例外。** 1A 規則是「印 WARN、跳過該項，render 照樣 rc 0，只有 `--validate` 回 rc 1」。例外的理由見「失敗模式」第一列：跳過一個租戶會讓它的告警靜默消失，而跳過一條 route 不會造成這種後果。

- **換到的**
  - 不會部署出「少了某個租戶」的 Alertmanager 設定。部署工作會轉紅，線上舊的設定維持不動。
  - #2633 的過渡規則 id（空字串、含空格）也一併改為非零退出，不再被靜默跳過。
- **犧牲的**
  - 只要有一個 id 不合法，所有租戶的設定更新都會卡住，直到改名為止。
  - 使用大寫或底線 id 的客戶，換新 image 後 CI 與部署都會轉紅。

### D4：exporter 照常載入、印 WARN

exporter 不拒收不合法 id 的租戶，每次 reload 時對每個不合法 id 印一次 WARN。不新增 metric，也不新增告警。

- **換到的**：exporter 是 runtime 元件，把關集中在 CI 與寫入端；不會因為一個 id 不合法，就讓該租戶的 threshold metric 消失。
- **犧牲的**：在 D3 卡住部署的期間，exporter 仍會輸出不合法 id 的 metric，而線上的 Alertmanager 設定還是舊的。這段期間的告警走舊設定，所以不會丟失。

## 失敗模式與護欄

| 情境 | 結果 | 護欄 |
|---|---|---|
| 客戶已使用 `Team_A` 這類 id，換上新 image | 產生器在所有模式都非零退出，不輸出設定；tenant-api 拒絕寫入該租戶；exporter 照常輸出 metric 並印 WARN；線上的 Alertmanager 設定維持舊版，告警不會丟失 | release note 與 changelog 標為 **breaking** 並附改名步驟；`da-tools validate-config` 會列出所有不合法 id |
| **若 D3 只在 `--validate` 時阻擋**（本 ADR 已否決） | 產生器跳過該租戶、render rc 0，部署出少了該租戶的設定；告警落到 root route 的 receiver，而 repo 出貨的 `default` 是空 receiver（`k8s/03-monitoring/configmap-alertmanager.yaml:107`、`_grar_render.py` 的內建 base），等於靜默丟棄 | 這正是 D3 改成「所有模式非零退出」的原因 |
| 改名 | metric 的 `tenant` label 值改變，歷史不連續；Alertmanager 的 silence 與 inhibit 要跟著改；全數字的 id 要加引號 | 寫入遷移文件 |
| schema 檔不在 da-tools image 內 | Python 端 fail-closed，`generate-routes` 停止 | build.sh 的 `REPO_DATA_FILES` 已出貨該檔，`check_build_completeness` 會守住 |
| 有人改了 schema 但沒重新產生副本 | Go、portal 與 Python 的判定不一致 | pre-commit 漂移守衛 |

## 考慮過但否決的方案

| 方案 | 否決理由 |
|---|---|
| 維持過渡規則 `^[A-Za-z0-9_-]+$` 並加長度上限 | operator 模式仍要另一份 DNS-1123 規則 |
| 兩層規則：核心用過渡規則，operator 模式另外要求 DNS-1123 | 同一個 id 在不同模式下合法性不同，要維護兩層 |
| 寬鬆規則，例如 Mimir／Loki 允許 `!-_.*'()`、長度 150 | 同樣放不進 K8s 物件名稱；`.`、`*` 會出現在 receiver 名稱、檔名與 URL 中 |
| 分段上線：先 WARN，下一個 minor 版本才阻擋 | owner 選擇直接阻擋，讓規則從第一天起在所有讀者上一致 |
| 直接阻擋，但只在 `--validate` 時失敗（沿用 1A） | 不加 `--validate` 的部署路徑會靜默丟掉該租戶的告警（見失敗模式第二列） |
| 兩種語言各寫一份、只靠 parity 釘住；或採 receiverspec 模式 | 規則文字不止一份，portal 也無法納入 |
| exporter 跳過不合法 id 的租戶，並新增 counter 與告警 | 需要新機制；fail-closed 會讓該租戶的 metric 消失 |
| exporter 拒收整份檔案（沿用 parse failure） | 同一檔案中的其他租戶會一起被丟棄 |
| portal 不納入，只標示為已知不一致 | JS 那份規則會繼續漂移，而且它現在就少了 63 字元上限 |

## 已結案的問題

- **63 字元上限要不要扣掉前綴。** 不用扣。外審 grep 確認：operator 模式寫進 K8s label 的是**裸 id**（`metadata.labels: {"tenant": id}`，見 `operator_generate.py:461-466`、`migrate_to_operator.py:356-361`）。帶前綴的只有物件名稱（上限 253）與 Alertmanager receiver 名稱（沒有 63 的限制）。

## 待決問題

1. **tenant-api 的讀取與刪除路徑要不要套用新規則。** 傾向不套用：讀取與刪除已存在但不合法的租戶，是改名遷移時必要的動作。批次寫入與 `ValidateWritableTenantID` 是否會擋住改名流程的某一步，實作時要追蹤。
2. **root receiver 為空時要不要 WARN**（範圍外，但相關）。產生器偵測到 base config 的 root receiver 沒有任何 integration 時，可以印 WARN，提醒未被路由的告警會被靜默丟棄。這不屬於 tenant id 規則，另開票處理。

## 實作時要同步修改的地方

- `docs/cli-reference{,.en}.md` 的 `invalid_tenant_id` 一列，目前寫「大寫可用」。
- `docs/integration/gitops-deployment*.md`、`byo-alertmanager-integration.md`、`tenant-federation.md` 中提到大寫 id 的段落，逐一確認。
- `_grar_parse.py` 與 `_grar_validate.invalid_tenant_id_text` 的 WARN 文字，目前寫「letters, digits, '_' and '-' only」。
- `tests/shared/routing_policy_parity_matrix.json` 的 `tenant_ids` 表：把 `UPPER`、`Mixed_Case-1`、`_x`、`-x`、`x_`、64 字元等案例移到 invalid，並更新說明文字。
- `components/tenant-api/internal/handler/tenant_id_write_test.go`、`tests/ops/test_tenant_name_rfc1123.py`、`pkg/routingpolicy/parity_test.go` 等測試。

## 參考

- [#2341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2341)、[#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655)、[#2633](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2633)（過渡規則）
- [#2180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2180)（pattern 寫在 schema、Python 在 runtime 讀取的前例）
- `scripts/tools/dx/gen_recipe_status_json.py`（產生副本並同時供 Go 與 portal 使用、搭配漂移守衛的前例）
