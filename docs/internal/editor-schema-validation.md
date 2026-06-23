---
title: "編輯器 Schema 驗證設定 (conf.d Tenant YAML)"
tags: [schema, editor, conf.d, authoring, internal]
audience: [maintainers, ai-agent, contributors]
version: v2.9.0
lang: zh
---

# 編輯器 Schema 驗證設定（conf.d Tenant YAML）

把 [`docs/schemas/tenant-config.schema.json`](../schemas/tenant-config.schema.json)（draft-07）接到「打字當下」的編輯器，讓直接編輯 `conf.d/*.yaml` 的人（平台工程師、領域專家、走 raw GitOps PR 的租戶）在存檔/部署前就拿到 inline 驗證 + autocomplete + hover 說明。對應 issue [#658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/658)「Cash out tenant-config schema」。

> **為什麼 schema 的綠勾值得信任**：schema 的 reserved-key 集合與 Go (`pkg/config/types.go`) / Python (`scripts/tools/_lib_constants.py`) 兩個 runtime validator 由 **3-way drift gate** 守住（`scripts/tools/dx/sync_schema.py` + `tests/dx/test_sync_schema.py` + `tests/shared/test_reserved_key_py_go_parity.py`，跑在 CI pytest）。schema 一漂移，CI 就紅 → 編輯器不會對著合法 config 說謊。

## 適用範圍（哪些檔吃 tenant schema）

- ✅ **租戶檔**：`conf.d/<id>.yaml`、`conf.d/**/<id>.yaml`（含 `examples/`）、`try-local/seed/conf.d/<id>.yaml`。
- ❌ **平台檔 `_*.yaml`** 從 tenant schema **刻意排除**（它們不是 `required: [tenants]` 結構，硬套會對合法檔亮紅勾）。其中 **`_defaults*.yaml` 改接專屬的 `platform-defaults.schema.json`**（頂層 key 守門，見下節）；其餘 `_*`（`_profiles` / `_routing_profiles` / `_rbac` / `_domain_policy` / `_instance_mapping`）各有自家 shape/validator，仍不接 schema。

排除是用 glob 的字元類 `[^_]`（檔名第一字非底線）達成。

## VS Code（零設定，已 codified）

> ⚠️ **為什麼不是 commit `.vscode/settings.json`**：repo 的 `.gitignore` 有 `.vscode/*`（只放行 `!.vscode/extensions.json`）——`.vscode/settings.json` 是**開發者本機檔**（也被起手式拿去寫 VS Code Git 開關），commit 不進去。所以 schema 對映改由**兩個可 commit 的入口**遞送：

### Dev Container（零設定，已 codified）

[`.devcontainer/devcontainer.json`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.devcontainer/devcontainer.json) 的 `customizations.vscode` 同時帶：

- `extensions`：`redhat.vscode-yaml`（container 自動安裝）。
- `settings.yaml.schemas`：把 tenant schema 對映到 conf.d 租戶檔（如下）。

在 dev container 裡開 VS Code 即生效，無需任何手動步驟。

### 本機（非 container）VS Code

1. [`.vscode/extensions.json`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.vscode/extensions.json)（此檔**有**被 `!.vscode/extensions.json` 放行 commit）會在你開啟 workspace 時**推薦安裝** `redhat.vscode-yaml`。
2. schema 對映請加進你**自己的**（gitignored）`.vscode/settings.json` 或 user settings——或用下節〈方法 A：file modeline〉（免設定、跨編輯器）：

   ```jsonc
   "yaml.schemas": {
       "./docs/schemas/tenant-config.schema.json": [
           "**/conf.d/[^_]*.yaml",
           "**/conf.d/**/[^_]*.yaml"
       ]
   }
   ```

   （`**/` 前綴讓 glob 不論 yaml-language-server 是「比對完整 workspace 相對路徑」或「比對路徑後綴」都會命中；`[^_]` 字元類排除底線平台檔。）

裝好後開任一租戶檔，打錯 key（例如把 `tenants` 拼成 `tenant`、或 `_metadata` 下放 `db_typ`）會即時紅波浪線；`_severity_dedup` 之類 enum 欄位會跳 autocomplete。

## 非 VS Code 編輯器

底層都是 `yaml-language-server`（與 VS Code 同一引擎），所以行為一致；差別只在「怎麼告訴它 schema 對映」。

### 方法 A：file modeline（最可攜，跨所有 yaml-language-server client）

在單一檔頂端加一行 magic comment，任何支援 `yaml-language-server` 的編輯器（VS Code / Neovim / JetBrains LSP）都會吃。**用絕對 https URL，不要用相對路徑**：

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/vencil/Dynamic-Alerting-Integrations/main/docs/schemas/tenant-config.schema.json
tenants:
  my-tenant:
    mysql_connections: 90
```

> ⚠️ **相對 modeline 會腐敗**：`$schema=` 的相對路徑是相對「該 YAML 檔自身位置」。若有人把帶 `../../docs/...` modeline 的檔**複製到別的目錄深度**（例如 `conf.d/asia/db-c.yaml`），相對層數就錯了 → 該檔**靜默失去驗證**（沒紅線 ≠ 正確）。絕對 https URL 與**位置無關**、複製到哪都對。（注意 `$schema=/docs/...` 的前導斜線在多數 language server 是**檔案系統絕對路徑**、非 workspace-root，不可靠。）

代價：會寫進檔案內容；`conf.d` 含 generated / 客戶檔時不建議全面鋪。**主路徑仍以 dev-container/workspace 的 glob 設定為準**（`**/conf.d/[^_]*.yaml` 與檔案位置無關、不會腐敗），modeline 當「某一檔臨時想要強驗」的逃生門。

### 方法 B：Neovim（coc.nvim + coc-yaml）

`:CocConfig` 加：

```jsonc
"yaml.schemas": {
    "./docs/schemas/tenant-config.schema.json": ["**/conf.d/[^_]*.yaml", "**/conf.d/**/[^_]*.yaml"]
}
```

（內建 LSP + `nvim-lspconfig` 走 `yamlls` 的 `settings.yaml.schemas`，鍵值同上。）

### 方法 C：JetBrains（IntelliJ / GoLand / PyCharm）

JetBrains 內建 YAML schema 支援（不需 yaml-language-server）：
*Settings → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings* → 新增，Schema file 指 `docs/schemas/tenant-config.schema.json`，File path pattern 加 `conf.d/*.yaml`（JetBrains 的 pattern 不支援 `[^_]` 字元類排除 → 平台檔 `_*.yaml` 請逐檔在 *JSON schema* 下拉選 "No mapping"，或忽略其紅勾）。

## 平台檔 `_defaults*.yaml` — 頂層 key 守門

**`_defaults*.yaml` 接 [`platform-defaults.schema.json`](../schemas/platform-defaults.schema.json)**（#658 fast-follow）。`_defaults.yaml` 由領域專家撰寫、爆炸半徑最大（影響該目錄下全部租戶），一個頂層 key typo（`state_flters` / `defalts`）會讓整塊平台預設**被 YAML 解析器靜默忽略**。

此 schema 是**最小守門**：

- **頂層 key 嚴格**（`additionalProperties:false`）→ 擋上述非前綴類 typo。`^_state_` / `^_routing` patternProperties 放行 prefix-class（同 tenant validator 的寬鬆 prefix 模型 → prefix **內部** typo 如 `_routing_defualts` **不在守備**）。
- **巢狀值刻意 loose**（`defaults` / `state_filters` 下的 metric / filter 名是動態的、不建模）。
- 頂層 properties 同時鏡像 Go `ThresholdConfig`（`tenants` / `profiles` / `max_metrics_per_tenant` 也放行——loader 從任何檔讀它們）。
- **CI（`check_confd_schema.py`）與編輯器（devcontainer `yaml.schemas`）用同一 schema → 跨 surface 一致**。

**仍 fast-follow（defer-with-trigger）**：(1) `_defaults` 的 **full 巢狀結構** schema（metric / filter 名動態，須真建模）——trigger＝收到 value-level「對合法亮紅 / 對錯放行」回報；(2) 其餘 `_*`（`_profiles` / `_routing_profiles` / `_rbac` / `_domain_policy` / `_instance_mapping`）的專屬 schema——形狀各異，`_routing_profiles` 等已有自家 validator（`check_routing_profiles.py`）。

## 相關

- Schema 本體：[`docs/schemas/tenant-config.schema.json`](../schemas/tenant-config.schema.json)（檔內 `$comment` 也附 VS Code snippet）。
- CI 驗證：`scripts/tools/lint/check_confd_schema.py`（conf.d 租戶檔 × schema，pre-commit `confd-schema-check` + CI Lint）。
- 3-way drift gate：`scripts/tools/dx/sync_schema.py`、`tests/dx/test_sync_schema.py`、`tests/shared/test_reserved_key_py_go_parity.py`。
