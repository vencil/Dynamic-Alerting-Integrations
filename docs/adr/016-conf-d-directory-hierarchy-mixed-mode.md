---
title: "ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-016
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-04-18
updated_at: 2026-11-22
---
# ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略

> **Language / 語言：** **中文 (Current)** | [English](./016-conf-d-directory-hierarchy-mixed-mode.en.md)

## 狀態

✅ **Accepted**（v2.7.0，2026-04-19）。與 [ADR-017](017-defaults-yaml-inheritance-dual-hash.md)（`_defaults.yaml` 的繼承語意）為一組。

2026-09-28 修訂：路由面改跟閾值面走同一套目錄階層，不再只讀根目錄。

2026-10-16 修訂：路由面只剩路由生成器一個讀者，da-guard 不再有自己的路由層。

2026-11-22 修訂：改寫為現行行為；標明「由目錄路徑推斷 `_metadata` 預設值」尚未實作（見「後果與已知限制」）。

## 決策

**`conf.d/` 同時接受平面與分層兩種布局，不強制遷移。** 租戶設定可以全部放在根目錄（平面），也可以放進任意深度的子目錄；兩種可以混在同一棵樹裡。

```
conf.d/
├── legacy-tenant-a.yaml          ← 平面（向下相容）
├── _defaults.yaml                ← 全域 defaults（可選）
├── finance/                      ← domain 層
│   ├── _defaults.yaml            ← domain 層 defaults
│   └── us-east/                  ← region 層
│       └── prod/                 ← environment 層
│           ├── _defaults.yaml
│           └── fin-db-001.yaml
└── logistics/
    └── ap-northeast/
        └── prod/
            └── log-db-001.yaml
```

1. **深度不限，命名是建議。** 建議用 `{domain}/{region}/{env}/` 三層，與 `_metadata` 的欄位對齊；但任何深度都會被掃描。`_defaults.yaml` 的繼承也沒有深度上限：從根目錄到租戶所在目錄，每一層的 `_defaults.yaml` 都進繼承鏈（語意見 ADR-017）。
2. **哪些檔案算數。** 掃描遞迴走訪整棵樹，略過隱藏目錄。檔名轉成小寫後以 `.yaml`／`.yml` 結尾才讀；以 `_` 開頭的是平台檔（`_defaults.yaml` 等），不是租戶設定、不產生 metric。
3. **閾值面與路由面走同一套階層。** threshold-exporter 吐出的 metric、`/effective`，與路由生成器產生的 Alertmanager 路由，讀的是同一棵樹。路由設定沿目錄鏈逐層繼承，語意記在 [ADR-017 決策 9](017-defaults-yaml-inheritance-dual-hash.md#9-路由設定沿目錄逐層繼承)。整棵樹都讀之後，子目錄有設定檔本身不是錯誤；會擋下的是：子目錄檔案裡出現 `_routing_enforced`（rc 2，它只認根目錄）、同一個租戶 id 在多個檔案宣告（rc 1），以及 ADR-017 決策 9 列出的其餘錯誤。
4. **遷移是可選的。** `migrate-conf-d` 依每個租戶的 `_metadata.domain/region/environment` 規劃搬移，預設只列出計畫（`--dry-run`），`--apply` 才搬。缺 `_metadata` 的檔案不動，列出來讓人決定。

### 範例：規劃一次遷移

輸入是一棵平面的 `conf.d/`，一個租戶有 `_metadata`、一個沒有：

```yaml
# fin-db-001.yaml
tenants:
  fin-db-001:
    _metadata:
      domain: finance
      region: us-east
      environment: prod
    mysql_connection_usage_percent: "70"
```

```yaml
# legacy-db.yaml
tenants:
  legacy-db:
    mysql_connection_usage_percent: "75"
```

`python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d --dry-run` 的輸出（需要 da-guard：`$DA_GUARD_BINARY` 或 `$PATH` 上的 `da-guard`，沒有就結束碼 2；結束碼 0，不搬任何檔；第一行的路徑省略）：

```text
   Migratable:        1
   Skip (no metadata): 1
   Skip (nested):      0
   Skip (system):      0
   Skip (unplaceable): 0

📋 Dry-run — `da-guard write --dry-run` accepts these moves:

  fin-db-001.yaml -> finance/us-east/prod/fin-db-001.yaml

Run with --apply to move them. (1 files will be moved)

⚠️  1 file(s) skipped (missing _metadata). Decide manually:
    legacy-db.yaml
```

`--apply` 把整份計畫當成一批交給 da-guard 判定：任何一筆搬不成（目的地沒有讀者會讀、已存在、搬完會讓整棵樹變差），一個檔都不搬、結束碼 2。樹在 git 工作目錄裡時，每個來源都要是 git 追蹤的檔案，搬完後 index 的效果與 `git mv` 相同。`--dry-run` 不改任何檔，可以放進 PR 檢查。

## 背景

平面布局把所有租戶 YAML 放在同一個 `conf.d/`。租戶到兩百個以上時：

1. **難找**：查某個 domain／region 的租戶只能靠 grep。
2. **影響範圍看不出來**：改一份 defaults 會影響多少租戶，從目錄結構看不出來，PR 審查與 CI 都難判斷。
3. **metadata 重複**：每個租戶都要手填 `_metadata.domain/region/environment`，與目錄結構表達的是同一件事。

分層布局解決前兩點：目錄就是範圍，一份 `_defaults.yaml` 只影響它底下的租戶。第三點見「後果與已知限制」。

## 後果與已知限制

- **由目錄路徑推斷 `_metadata` 預設值，尚未實作。** 原決策是：租戶缺 `_metadata.domain` 等欄位時，由所在目錄補上（第 1 層是 domain、第 2 層 region、第 3 層 environment），明確寫出的 `_metadata` 優先。現行行為是 threshold-exporter 的 effective metadata 只讀 `_metadata`，目錄路徑不參與；`check_path_metadata_consistency.py` 只在目錄的第一段與 `domain`、或某一段是已知環境名而與 `environment` 不一致時警告，不擋 commit。所以分層之後，`_metadata` 仍要手填。
- **metric 不帶路徑。** 目錄深度不影響 metric label，`tenant` 仍是唯一的租戶鍵；搬目錄不會改變任何 series。
- **子目錄 `_defaults.yaml` 裡的路由鍵不被讀。** 子目錄 `_defaults.yaml` 的 `defaults:` 區塊裡寫 `_routing*`，沒有任何元件消費；路由生成器會點名它（`routing_in_unread_location`），`--validate` 時擋下。

### 支援面邊界：不是每個讀者都遞迴

| 讀者 | 分層 `conf.d/` |
|:--|:--|
| threshold-exporter（吐出的 metric 與 `/effective`） | ✅ 遞迴，完整繼承 |
| `validate_config.py` | ✅ 遞迴 |
| 路由生成器（`generate_alertmanager_routes.py`） | ✅ 讀整棵樹的租戶與路由層 |
| da-guard | 不判路由：樹上有路由內容時列為未判定（結束碼 4），交給路由生成器（[ADR-036](036-single-parser-effective-config.md) §9） |
| tenant-api | 只服務根目錄的租戶檔，不自己讀路由層；寫入由產生器對整棵樹判定（[ADR-036](036-single-parser-effective-config.md) §4） |
| 其餘 Python 工具 | 平面，但會列出被略過的檔案並指回本節 |

「平面就要出聲」由 `tests/shared/test_confd_enumeration_contract.py` 強制：一支新工具平面讀取設定目錄、又不列出略過的檔案，就會被擋下。成員是從工具實際的呼叫推導出來的，不是名單。共用的列舉層在 `scripts/tools/_lib_confd.py`。

## 考慮過的替代方案

- **強制遷移到分層**：破壞向下相容，所有既有使用者升級時都得一次重整 `conf.d/`；對十幾個租戶的小部署是不必要的負擔。
- **只支援平面**：解決不了兩百個租戶以上的可讀性與影響範圍問題。
- **用外部索引（資料庫／JSON）取代目錄結構**：偏離 config-as-code，增加部署元件；本平台的原則是檔案系統就是設定的唯一來源。

## 相關

- [ADR-017: `_defaults.yaml` 繼承語意 + dual-hash hot-reload](017-defaults-yaml-inheritance-dual-hash.md)
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.md)
- [ADR-036](036-single-parser-effective-config.md) — 單一解析器與 effective config
- [多 domain 的 conf.d 布局](../scenarios/multi-domain-conf-layout.md)
- [Benchmark Playbook §Synthetic Fixture Generation](../internal/benchmark-playbook.md#synthetic-fixture-generation-速率對照) — 平面與分層的效能對照
