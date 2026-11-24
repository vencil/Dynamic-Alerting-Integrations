---
title: "ADR-035: tenant id 合法字元集的單一來源"
tags: [adr, config, multi-tenant, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-035
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-10-03
updated_at: 2026-11-24
---

# ADR-035: tenant id 合法字元集的單一來源

> **Language / 語言：** **中文 (Current)** | [English](./035-tenant-id-single-source.en.md)

## 狀態

✅ **Accepted**（2026-10-03，owner 核可）。

2026-11-05 修訂：`operator_generate.py` 與 `migrate_to_operator.py` 不再讀租戶（[ADR-008](008-operator-native-integration-path.md)），從讀者清單移除；63 字元上限不變。

2026-11-24 修訂：改寫為現行行為；決定改用編號 1–4。

## 決策

1. **規則是 DNS-1123 label**：小寫英數與 `-`，長度 1–63，首尾是英數（pattern 見第 1 節）。
2. **規則只寫一次**，在 tenant-config schema 的 `definitions.tenantId`。Python 在執行時讀它；Go 與 portal 讀從它產生的 JSON 副本。
3. **產生器遇到不合法的 id，任何模式都非零退出、不輸出設定。**
4. **exporter 照常載入，只印 WARN。**

### 範例：一個不合法的 id

```yaml {.confd .expect-invalid}
# conf.d/Team_A.yaml
tenants:
  Team_A:
    mysql_connection_usage_percent: "70"
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/a"
```

`generate_alertmanager_routes.py --config-dir conf.d --output-alertmanager --dry-run` 的輸出（結束碼 1；`--validate` 相同）：

```text
Config files: 1 read, 0 skipped
FAIL: 1 invalid tenant id(s) — nothing was generated, written or applied; rename the tenant(s):
  Team_A.yaml: tenant id 'Team_A' is not a valid tenant id (Tenant id (ADR-035): a DNS-1123 label — 1-63 characters of lowercase letters, digits and '-', starting and ending with a letter or digit.) — no route, receiver or inhibit rule is rendered for it
```

括號裡的那句是 schema 的 `definitions.tenantId.description`，產生器、da-guard 與 tenant-api 的錯誤訊息都引用它，不各自描述規則。改名為 `team-a` 後才會產生設定。

## 背景

決定之前，repo 裡的 tenant id 規則有四份、互不一致：DNS-1123 label（operator 產生器、`init_project.py` 各手寫一份）、portal 少了 63 字元上限的 DNS-1123、`alert_quality.py` 的 `^[a-zA-Z0-9_-]+$`，以及產生器、da-guard、tenant-api 寫入路徑共用的過渡規則（只擋空字串與 `^[A-Za-z0-9_-]+$` 以外的字元）。exporter、tenant-api 的讀取路徑與 `scaffold_tenant.py` 幾乎不檢查，JSON schema 對 `tenants` 的 key 也沒有限制。

選 DNS-1123 的原因：

- **id 會進到 Kubernetes 物件名稱與 label。** 物件名稱只接受小寫英數、`-`、`.`；用過渡規則的話，同一個 id 在 config-driven 模式下合法、放進 K8s 物件卻不合法。
- **它是過渡規則的子集**，只往嚴的方向收，與 Kubernetes namespace 的命名一致。
- **repo 內實際使用的 id 都符合。** 起草時，git 追蹤的 YAML 裡 `tenants:` 下共有 33 個不同的 id，全部符合 DNS-1123，最長 21 字元。客戶端實際用了什麼量不到，見「後果與已知限制」。

## 決定的細節

### 1. 規則是 DNS-1123 label

```text
^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$
```

- 含大寫、底線或超過 63 字元的 id 必須改名。改名後 metric 的 `tenant` label 值會變，歷史資料不連續。
- 全數字的 id（例如 `123`）合法，但 YAML 沒加引號時會被解析成整數 key，schema 檢查會以「不是 string」擋下，所以要加引號。
- 63 字元上限不扣前綴：id 本身就用在檔名、ConfigMap key 與 label 值。

### 2. 規則只寫一次，在 tenant-config schema

- **規則本體**：`docs/schemas/tenant-config.schema.json` 的 `definitions.tenantId`（`type: string` 與 `pattern`），`properties.tenants` 以 `propertyNames` 引用它，所以 schema 檢查會檢查每個租戶 key。不另寫 `maxLength`：pattern 已限制長度，重複寫只會讓違規時出現兩條錯誤。`description` 是給人看的一句規則，會跟著副本一起出貨。
- **Python**：`_lib_validation.is_valid_tenant_id` 在執行時讀這個 pattern；da-tools image 出貨這份 schema。讀不到 schema 時 fail-closed。`alert_quality.py`、`init_project.py`、`scaffold_tenant.py` 都呼叫它，不再各寫 regex。
- **Go 與 portal**：Go binary 在執行時讀不到 repo 的 `docs/`，所以用產生的副本。`scripts/tools/dx/gen_tenant_id_json.py` 從 schema 抽出 `definitions.tenantId`，寫成 `components/threshold-exporter/app/pkg/tenantid/tenant_id.json`（Go 以 `go:embed` 讀，`pkg/tenantid` 的 `Valid` 供 exporter、da-guard 與 tenant-api 寫入路徑呼叫）與 portal 的 `_common/data/tenant-id.json`。pre-commit 的 `tenant-id-json-check` 要求兩份副本都等於重新產生的結果。
- **pattern 方言**：只能用 ECMA-262、Python `re`、Go RE2 三者語意一致的子集（字元類、`{m,n}`、`^`、`$`），不得用 lookaround、反向參照或 Unicode 類別。比對一律是完整比對：Python 用 `re.fullmatch`（因為 `$` 會吃掉結尾的 `\n`），Go 以 `^…$` 錨定。

### 3. 產生器在任何模式都非零退出

產生器、da-guard、tenant-api 寫入路徑、schema、portal、`scaffold_tenant`、`init_project` 同時用 DNS-1123 判定。產生器遇到不合法的 id，不論一般產生、`--dry-run`、`--apply`、`--output-configmap`、`--validate` 還是 `--strict`，都非零退出、不輸出設定。

產生器對其他問題的慣例是「印 WARN、跳過該項，產生照樣成功，只有 `--validate` 失敗」。這裡是例外：跳過一條 route 不會丟告警，跳過一個租戶會。它的告警會落到 root route 的 receiver，而 repo 出貨的預設 root receiver 是空的，等於靜默丟棄。產生器另外會在 root receiver 沒有任何 integration 時印 WARN。

tenant-api 的新規則套用到所有寫入路徑：強制點在 `gitops.guardTenantID`，涵蓋 PUT、批次寫入、group 批次寫入、custom-alerts、federation subset 與 dry-run；handler 的 `ValidateWritableTenantID` 用來提早回 400。讀取路徑不套用：規則不放進 `confd.IsAddressableTenantID`，因為讀取路徑也呼叫它。tenant-api 沒有刪除或改名租戶的端點，改名一律在 git 裡做，所以新規則不會擋住改名。

### 4. exporter 照常載入、印 WARN

exporter 不拒收不合法 id 的租戶，每次 reload 時對每個不合法 id 印一次 WARN，引用規則的 `description`。不新增 metric，也不新增告警。把關集中在 CI 與寫入端；一個 id 不合法，不會讓該租戶的 threshold metric 消失。

## 後果與已知限制

- **用了大寫或底線 id 的部署，換新 image 後 CI 與部署會轉紅。** 產生器不輸出新設定，線上的 Alertmanager 設定維持舊版，告警走舊設定、不會丟失；tenant-api 拒絕寫入該租戶；exporter 照常輸出 metric 並印 WARN。只要有一個 id 不合法，所有租戶的設定更新都會卡住，直到改名。`da-tools validate-config` 會列出所有不合法 id。
- **改名的代價**：metric 的 `tenant` label 值改變，歷史不連續；Alertmanager 的 silence 與 inhibit 要跟著改。
- **schema 路徑對結尾 `\n` 的判定不一致。** JSON Schema 的 `pattern` 是搜尋語意：Python jsonschema 會放過結尾帶 `\n` 的 key，編輯器（ECMAScript 的 `$`）會擋下。產生器、da-guard 與 tenant-api 寫入驗證都擋下解碼後的這類 key，所以接受這個差異；tenant-api 的讀取驗證較寬。parity matrix 的 `tenant_ids` 表以直接跑 jsonschema 的案例釘住它。
- **有人改了 schema 卻沒重新產生副本**，Go、portal 與 Python 的判定會不一致：由 `tenant-id-json-check` 擋下。
- **schema 檔不在 da-tools image 內**時，Python 端 fail-closed、`generate-routes` 停止：由 `check_build_completeness` 守住出貨清單。

## 考慮過的替代方案

| 方案 | 不採用的理由 |
|---|---|
| 維持過渡規則 `^[A-Za-z0-9_-]+$` 並加長度上限 | 放不進 K8s 物件名稱，仍要另一份 DNS-1123 規則 |
| 兩層規則：核心用過渡規則，進 K8s 時另外要求 DNS-1123 | 同一個 id 在不同模式下合法性不同，要維護兩層 |
| 寬鬆規則（例如 Mimir／Loki 允許 `!-_.*'()`、長度 150） | 同樣放不進 K8s 物件名稱；`.`、`*` 會出現在 receiver 名稱、檔名與 URL |
| 分段上線：先 WARN，下一個 minor 版本才阻擋 | owner 選擇直接阻擋，讓規則從第一天起在所有讀者上一致 |
| 直接阻擋，但只在 `--validate` 時失敗 | 不加 `--validate` 的部署路徑會靜默丟掉該租戶的告警 |
| 兩種語言各寫一份，只靠 parity 測試釘住；或 Go 手抄常數、測試讀 schema 比對 | 規則文字不止一份，portal 沒有 Go 測試可用、會留下第三份 |
| exporter 跳過不合法 id 的租戶，另加 counter 與告警 | 需要新機制；該租戶的 metric 會消失 |
| exporter 拒收整份檔案 | 同一檔案裡的其他租戶會一起被丟棄 |
| portal 不納入，只標示為已知不一致 | JS 那份規則會繼續漂移 |

## 相關

- [ADR-008](008-operator-native-integration-path.md) —— operator 路徑的取消
- [#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655) —— 本決定的討論與拍板
- `scripts/tools/dx/gen_recipe_contract.py` —— 同樣「產生副本供 Go 與 portal 讀、搭配漂移守衛」的前例
