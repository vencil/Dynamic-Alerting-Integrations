---
title: "ADR-018: Profile-as-Directory-Default"
tags: [adr, profile-builder, conf-d, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-018
tracking_kind: adr
status: accepted
domain: tools
created_at: 2026-04-27
updated_at: 2026-11-28
---
# ADR-018: Profile-as-Directory-Default

> **Language / 語言：** **中文 (Current)** | [English](./018-profile-as-directory-default.en.md)

## 狀態

🟢 **Accepted**（v2.8.0，2026-04-27）。與 [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)（目錄分層）、[ADR-017](017-defaults-yaml-inheritance-dual-hash.md)（`_defaults.yaml` 的繼承語意）為一組。

2026-11-22 修訂：最初實作這條規則的 Profile Builder（把相似告警規則分群、替每群寫出 `_defaults.yaml` 與租戶覆寫檔的 Go library）已從程式碼刪除，它從未接到任何 CLI（[#4228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4228)）。規則本身仍然有效。

2026-11-24 修訂：改寫為現行行為。決策不變。

2026-11-28 修訂：閾值鍵的多餘覆寫改為反事實判定（刪掉這行後 exporter 送出的不變才算多餘），不再比較兩個值（[#4330](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4330)）。

## 決策

**一群租戶共用的閾值寫在它們共同上層目錄的 `_defaults.yaml`；租戶檔只寫「與它繼承到的值不同」的鍵。**

1. **共通值放 `_defaults.yaml` 的 `defaults:` 底下。** 這個值沿目錄鏈往下繼承到每個租戶（繼承語意見 ADR-017）。
2. **與繼承值相同就不寫。** 租戶的值等於它繼承到的值時，租戶檔不寫這個鍵；只有真的不同才寫。這樣租戶檔裡的每一行都是一個刻意的偏離，審查的人不必猜它是要釘住這個值、還是忘了刪的舊行。
3. **每個租戶仍有自己的租戶檔。** 租戶是由租戶檔宣告的：沒有檔，就沒有這個租戶。沒有任何鍵要覆寫的租戶寫成空 mapping（下例的 `db-c`）。
4. **default 取這群租戶的中位數，不取平均。** 一個離群的租戶不會把 default 拉偏。這是給整理 `conf.d/` 的人的準則，目前沒有工具替你算，也沒有檢查守它。

### 範例

三個租戶的閾值分別是 80、95、80，中位數 80 寫進 `_defaults.yaml`。`db-b` 只寫它不同的那個值，`db-c` 什麼都不覆寫；`db-a` 多寫了一行與 default 相同的值，照本規則應刪掉：

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connection_usage_percent: 80

# conf.d/db-a.yaml
tenants:
  db-a:
    mysql_connection_usage_percent: "80"

# conf.d/db-b.yaml
tenants:
  db-b:
    mysql_connection_usage_percent: "95"

# conf.d/db-c.yaml
tenants:
  db-c: {}
```

`da-guard served-values --config-dir conf.d` 對 `mysql_connection_usage_percent` 給 `db-a` 80、`db-b` 95、`db-c` 80：`db-c` 從 `_defaults.yaml` 繼承到 80，`db-a` 那一行刪不刪都是 80。租戶檔的閾值依 schema 寫成字串，exporter 讀成數字。

### 誰在守這條規則

da-guard 的 `redundant_override` 檢查：租戶檔裡某個鍵的值，與租戶刪掉它之後會拿到的值相同時，報一筆 warning。比對的對象是這個租戶自己的繼承結果：目錄鏈上每一層的 `_defaults.yaml`，以及租戶綁定的 `_profile`（profile 給了值，就以 profile 的值為準）。

閾值鍵的判定是一個反事實：在租戶自己的檔案裡刪掉這一行、重建這個租戶之後，exporter 送出的閾值列（值、severity、label）從現在起都完全不變，才算多餘：一天中每個時段排程的切點都比，租戶值或繼承值帶 `expires:` 的，到期之後也比。比的不是兩個值像不像，而是 exporter 自己建構與解析的結果，所以 YAML 型別與寫法（`80`、`"80"`、`"80.0"`、`"80:warning"`）、時段排程、子目錄與 profile 的繼承，都照 exporter 的讀法決定；`"80:critical"` 送出的 severity 不同，不算。另有 finding 負責的行也不報：值被 exporter 拒收的（`value_not_served`），完全沒人讀的鍵（例如維度 label 沒在 `dimensions:` 宣告；由 `da-guard key-verdict` 判為 unread error，tenant-api 寫入時用同一個判定），以及寫了 `disable` 卻沒有東西可關的。對上例跑 `da-guard --config-dir conf.d` 指出 `db-a`（結束碼 0；Summary 與掃描清單省略）：

```text
### Warnings (informational)

| Tenant | Field | Kind | Message |
|--------|-------|------|---------|
| db-a | mysql_connection_usage_percent | redundant_override | tenant "db-a" overrides "mysql_connection_usage_percent" with the same value as the new defaults; remove the override and rely on inheritance |
```

- warning 不影響結束碼（0）；加 `--warn-as-error` 時結束碼 1。由 `cmd/da-guard/main_test.go` 的 `TestRun_RedundantOverride_SurfacesAsWarning` 與 `TestRun_RedundantOverride_WithWarnAsError_ExitsOne` 守住。
- 不同子目錄的租戶各自比對自己那條鏈：`TestRun_RedundantOverride_HonorsCascadingDefaults`。
- 報出與否，與「刪掉這行後 `da-guard served-values` 的值與 severity 是否不變」逐一對照：`cmd/da-guard/redundant_override_read_test.go` 的 `TestGuard_RedundantOverrideFiresExactlyWhenServedIsUnchanged`；各種容易判錯的形狀（根目錄缺 `defaults:` 包裝、base 被關掉的 `_critical`、沒宣告的維度 label、含 `.` 的 regex 維度鍵、時段排程）見 `cmd/da-guard/redundant_counterfactual_test.go` 的 `TestGuard_RedundantOverrideIsTheCounterfactual`。
- 綁了 `_profile` 的租戶比對 profile 的值：`cmd/da-guard/profile_guard_test.go` 的 `TestGuard_RedundantOverrideFollowsTheProfile`。

## 背景

一群結構相似的租戶落到 `conf.d/` 時，有三種寫法：

1. **每個租戶一份完整的租戶檔**：N 個租戶就把同樣的鍵重複寫 N 次。改一個共通值要改 N 個檔，而且看不出哪些值是刻意不同的。
2. **全部寫進 `_defaults.yaml`，租戶檔不寫閾值**：失去逐租戶微調的能力。
3. **共通值寫在 `_defaults.yaml`，只有不同的租戶寫覆寫**：ADR-017 的合併規則本來就支援這種稀疏覆寫（租戶檔只列部分鍵，其餘從上層補齊）。缺的是規則：default 取什麼值、哪些租戶算「真的不同」。沒有明確的規則，每個經手人會各自詮釋。

本 ADR 選第 3 種並補上規則。它是跨元件的約定：產生 `conf.d/` 的工具、審查 PR 的人、da-guard 的多餘覆寫檢查都照同一條邊界判斷；客戶看到的 `conf.d/` 形狀也由它決定。

## 後果與已知限制

- **多餘的覆寫只是警告，不擋 merge。** 執行時它無害，只是讓租戶檔變長、讓意圖變模糊。要擋就在 CI 加 `--warn-as-error`。
- **`_` 開頭的保留鍵與它底下的值仍比對 YAML 的值與型別。** 這些鍵不經閾值列送出，由 route generator、metadata 等各自的讀者讀取，反事實還沒涵蓋它們；da-guard 只在值與型別都相同時才報（[#4331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4331)）。
- **保留鍵只比對單一值。** 保留鍵的 mapping 會攤平到每個葉節點逐一比對；list 不比對，內容相同也不報（`internal/guard/guard_test.go` 的 `TestCheckRedundantOverrides_SkipsStructuredValues`）。
- **子目錄 `_defaults.yaml` 不生效的鍵不判。** 子目錄 `_defaults.yaml` 裡 `_` 開頭的鍵（例如 `_silent_mode`），exporter 不套用（ADR-017 決策 4），租戶自己那一行才是生效的值；這種覆寫不算多餘，da-guard 也不報（`cmd/da-guard/subtree_reserved_test.go` 的 `TestGuard_RedundantOverrideSkipsRefusedSubtreeKey`）。
- **租戶只能覆寫有宣告的閾值。** 租戶檔寫了一個 `_defaults.yaml` 沒宣告的閾值鍵，`/metrics` 不送它；da-guard 主報告不報，`da-guard key-verdict` 與 tenant-api 的寫入判定則把它判為 unread error。哪些算已宣告（根目錄、內建層、`optional_overrides:`）見 ADR-017 決策 3、4、11。
- **要關掉繼承來的閾值，寫 `"disable"`，不要寫 `null`。** 租戶檔寫 `null` 等於這一層沒寫，仍拿到上層的值（ADR-017 決策 4）；而且 `check_confd_schema.py` 會擋下租戶檔裡寫成 `null` 的閾值。
- **共通值放在子目錄時，標準部署送不到線上。** 客戶部署用的扁平組裝只帶 conf.d 頂層的檔，子目錄的 `_defaults.yaml` 到不了線上的 exporter（ADR-016「後果與已知限制」）。在那個問題解決前，共通值要放在根目錄 `_defaults.yaml` 才會在線上生效。
- **沒有工具從規則集自動產生這種形狀。** 導入客戶規則目前用逐租戶的 `da-tools migrate`；批次導入尚未出貨，追蹤在 [#4226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4226)。

## 考慮過的替代方案

- **每個租戶寫完整的租戶檔**：見「背景」第 1 種。重複，而且刻意的偏離淹沒在重複的值裡。
- **只用 `_defaults.yaml`**：見「背景」第 2 種。無法逐租戶微調。
- **default 取平均**：一個離群值就會把 default 拉偏，讓原本與多數一致的租戶也得寫覆寫。

## 相關

- [ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略](016-conf-d-directory-hierarchy-mixed-mode.md)
- [ADR-017: `_defaults.yaml` 多層繼承與雙雜湊熱重載](017-defaults-yaml-inheritance-dual-hash.md)
- [#4211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4211) — Profile Builder 退役的討論
- 實作位置：`components/threshold-exporter/app/internal/guard/redundant.go`（`redundant_override` 檢查）
