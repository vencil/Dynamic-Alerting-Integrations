---
title: "編輯器 Schema 驗證設定 (conf.d Tenant YAML)"
tags: [schema, editor, conf.d, authoring, internal]
audience: [maintainers, ai-agent, contributors]
version: v2.9.0
lang: zh
---

# 編輯器 Schema 驗證設定（conf.d Tenant YAML）

把 [`docs/schemas/tenant-config.schema.json`](../schemas/tenant-config.schema.json)（draft-07）接到「打字當下」的編輯器，讓直接編輯 `conf.d/` 下 YAML（`.yaml` / `.yml` 兩種拼法、不分大小寫）的人（平台工程師、領域專家、走 raw GitOps PR 的租戶）在存檔/部署前就拿到 inline 驗證 + autocomplete + hover 說明。對應 issue [#658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/658)「Cash out tenant-config schema」。

> **為什麼 schema 的綠勾值得信任**：schema 的 reserved-key 集合與 Go (`pkg/config/types.go`) / Python (`scripts/tools/_lib_constants.py`) 兩個 runtime validator 由 **3-way drift gate** 守住（`scripts/tools/dx/sync_schema.py` + `tests/dx/test_sync_schema.py` + `tests/shared/test_reserved_key_py_go_parity.py`，跑在 CI pytest）。schema 一漂移，CI 就紅 → 編輯器不會對著合法 config 說謊。

## 適用範圍（哪些檔吃 tenant schema）

檔案集合與 CI 閘門 `check_confd_schema.py` **同一套分類**（#1815）：副檔名 `.yaml` / `.yml` 兩種拼法、**不分大小寫**（`.YAML` / `.Yml` 也算），扁平與巢狀皆同。

- ✅ **租戶檔**（檔名開頭不是 `_` 也不是 `.`）：`conf.d/<id>.yaml`、`conf.d/<id>.yml`、`conf.d/**/<id>.yaml`（含 `examples/`）、`try-local/seed/conf.d/<id>.yaml`。
- ❌ **平台檔 `_*`** 從 tenant schema **刻意排除**（它們不是 `required: [tenants]` 結構，硬套會對合法檔亮紅勾）。其中 **檔名以 `_defaults` 開頭者（不分大小寫，含 `_defaults-multidb.yml`）改接專屬的 `platform-defaults.schema.json`**（頂層 key 守門，見下節）；其餘 `_*`（`_profiles` / `_routing_profiles` / `_rbac` / `_domain_policy` / `_instance_mapping`）各有自家 shape/validator，仍不接 schema。
- ❌ **隱藏檔 `.*`**：CI 與 exporter 都跳過，編輯器也不接。

排除靠 glob 的字元類 `[^_.]`（檔名第一字非底線、非點），而 `*([^/])`（extglob：零或多個非 `/` 字元）把比對鎖在檔名內。兩者都是**量出來的必要條件**，不是風格：

- yaml-language-server 1.24 用 picomatch 的 `bash` 模式比對，該模式下裸 `*` **會跨 `/`**——寫成 `[^_]*.yaml` 會讓 `conf.d/sub/_other.yaml` 也吃到 tenant schema、巢狀 `_defaults.yaml` 同時吃到兩份 schema。
- 開頭的字元類**不受** picomatch 的 dotfile 保護，所以 `.` 必須寫進 `[^_.]`。

**已知差異（不是等價，照實寫）**：

- **yaml-language-server ≤ 1.23**（vscode-yaml ≤ 1.23）的比對器會把 `^` 與 `(` 當字面字元跳脫 → 這組 glob 在舊版**一個檔都不綁**（沒有驗證，而不是錯誤驗證）。升級擴充套件即可；舊寫法 `[^_]` 在舊版反而把 tenant schema 綁到 `_*` 檔上。
- **conf.d 底下點開頭的子目錄**：CI 的 walker 每個目錄都進（exporter 本身跳過點目錄）。1.24（bash 模式）只在點目錄是 conf.d 下**第一層**時綁：`conf.d/.x/t.yaml` 綁、`conf.d/a/.x/t.yaml` 不綁；下一版（`next`，拿掉 bash 模式）兩者都不綁。
- **workspace 本身位在點目錄底下**（例如 `.claude/worktrees/…` 裡的 checkout）：兩種模式開頭的 `**` 都跨不過點目錄 → 整棵樹一個檔都不綁。
- 對等關係由 `tests/lint/test_editor_schema_glob_parity.py` **推導**：讀 devcontainer.json 的 glob、呼叫 CI 自己的 `validate_dir` 分類，兩種比對模式各自逐一比對、各自釘住上述差異；模型與真 picomatch 4.0.5（vscode-yaml 1.24.0 內附版本）逐格交叉比對，CI 的 Python Tests 會安裝這個版本。

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
           "**/conf.d/**/[^_.]*([^/]).[yY][aA][mM][lL]",
           "**/conf.d/**/[^_.]*([^/]).[yY][mM][lL]"
       ],
       "./docs/schemas/platform-defaults.schema.json": [
           "**/conf.d/**/_[dD][eE][fF][aA][uU][lL][tT][sS]*([^/]).[yY][aA][mM][lL]",
           "**/conf.d/**/_[dD][eE][fF][aA][uU][lL][tT][sS]*([^/]).[yY][mM][lL]"
       ]
   }
   ```

   （權威版本是 devcontainer.json 那份，此處是複本；兩者不同時以 devcontainer.json 為準。`**/conf.d/**/` 同時涵蓋扁平與巢狀；`[yY][aA][mM][lL]` 這種逐字元類是為了與 CI 一樣不分大小寫。各段的必要性見上方〈適用範圍〉。）

裝好後開任一租戶檔，打錯 key（例如把 `tenants` 拼成 `tenant`、或 `_metadata` 下放 `db_typ`）會即時紅波浪線；`_severity_dedup` 之類 enum 欄位會跳 autocomplete。

## 非 VS Code 編輯器

底層都是 `yaml-language-server`（與 VS Code 同一引擎），所以行為一致；差別只在「怎麼告訴它 schema 對映」。

### 方法 A：file modeline（最可攜，跨所有 yaml-language-server client）

在單一檔頂端加一行 magic comment，任何支援 `yaml-language-server` 的編輯器（VS Code / Neovim / JetBrains LSP）都會吃。**用絕對 https URL，不要用相對路徑**：

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/vencil/Dynamic-Alerting-Integrations/main/docs/schemas/tenant-config.schema.json
tenants:
  my-tenant:
    mysql_connections: "90"
```

> ⚠️ **相對 modeline 會腐敗**：`$schema=` 的相對路徑是相對「該 YAML 檔自身位置」。若有人把帶 `../../docs/...` modeline 的檔**複製到別的目錄深度**（例如 `conf.d/asia/db-c.yaml`），相對層數就錯了 → 該檔**靜默失去驗證**（沒紅線 ≠ 正確）。絕對 https URL 與**位置無關**、複製到哪都對。（注意 `$schema=/docs/...` 的前導斜線在多數 language server 是**檔案系統絕對路徑**、非 workspace-root，不可靠。）

代價：會寫進檔案內容；`conf.d` 含 generated / 客戶檔時不建議全面鋪。**主路徑仍以 dev-container/workspace 的 glob 設定為準**（glob 與檔案位置無關、不會腐敗），modeline 當「某一檔臨時想要強驗」的逃生門。

### 方法 B：Neovim（coc.nvim + coc-yaml）

`:CocConfig` 加：

與上節 VS Code 同一個 `"yaml.schemas"` 區塊（不在此重抄，避免第三份複本）。

（內建 LSP + `nvim-lspconfig` 走 `yamlls` 的 `settings.yaml.schemas`，鍵值同上。⚠️ 生效與否取決於該 client 內附的 yaml-language-server 版本，≤ 1.23 不綁任何檔，見〈適用範圍〉的已知差異。）

### 方法 C：JetBrains（IntelliJ / GoLand / PyCharm）

JetBrains 內建 YAML schema 支援（不需 yaml-language-server）：
*Settings → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings* → 新增，Schema file 指 `docs/schemas/tenant-config.schema.json`，File path pattern 加 `conf.d/*.yaml` 與 `conf.d/*.yml`（JetBrains 的 pattern 不支援 `[^_]` 字元類排除 → 平台檔 `_*.yaml` 請逐檔在 *JSON schema* 下拉選 "No mapping"，或忽略其紅勾）。

## 平台檔 `_defaults*.yaml` — 頂層 key 守門

**`_defaults*.yaml` 接 [`platform-defaults.schema.json`](../schemas/platform-defaults.schema.json)**（#658 fast-follow）。`_defaults.yaml` 由領域專家撰寫、爆炸半徑最大（影響該目錄下全部租戶），一個頂層 key typo（`state_flters` / `defalts`）會讓整塊平台預設**被 YAML 解析器靜默忽略**。

此 schema 是**最小守門**：

- **頂層 key 嚴格**（`additionalProperties:false`）→ 擋上述非前綴類 typo。`^_state_` / `^_routing` patternProperties 放行 prefix-class（同 tenant validator 的寬鬆 prefix 模型 → prefix **內部** typo 如 `_routing_defualts` **不在守備**）。
- **巢狀值刻意 loose**（`defaults` / `state_filters` 下的 metric / filter 名是動態的、不建模）。
- 頂層 properties 同時鏡像 Go `ThresholdConfig`（`tenants` / `profiles` / `max_metrics_per_tenant` 也放行——loader 從任何檔讀它們）。
- **CI（`check_confd_schema.py`）與編輯器（devcontainer `yaml.schemas`）用同一 schema、同一組檔案**（兩種副檔名拼法、不分大小寫；例外見〈適用範圍〉的已知差異）。

**仍 fast-follow（defer-with-trigger）**：(1) `_defaults` 的 **full 巢狀結構** schema（metric / filter 名動態，須真建模）——trigger＝收到 value-level「對合法亮紅 / 對錯放行」回報；(2) 其餘 `_*`（`_profiles` / `_routing_profiles` / `_rbac` / `_domain_policy` / `_instance_mapping`）的專屬 schema——形狀各異，`_routing_profiles` 等已有自家 validator（`check_routing_profiles.py`）。

## 相關

- Schema 本體：[`docs/schemas/tenant-config.schema.json`](../schemas/tenant-config.schema.json)（檔內 `$comment` 也附 VS Code snippet）。
- CI 驗證：`scripts/tools/lint/check_confd_schema.py`（conf.d 租戶檔 × schema，pre-commit `confd-schema-check` + CI Lint）。
- 3-way drift gate：`scripts/tools/dx/sync_schema.py`、`tests/dx/test_sync_schema.py`、`tests/shared/test_reserved_key_py_go_parity.py`。
