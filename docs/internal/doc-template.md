---
title: "文件模板規範"
tags: [internal, dx]
audience: [all]
version: v2.9.0
lang: zh
---

# 文件模板規範

> **受眾**：所有貢獻者 (All Contributors)
> **版本**：v2.3.0
> **相關文件**：[文件導航地圖](doc-map.md) · [開發規範](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)

本文件定義 Dynamic Alerting 平台所有 Markdown 文檔的標準結構與撰寫規範。遵循本模板能確保文件風格一致、可維護性高、易於自動化檢查。

---

## 1. Frontmatter（必須）

所有文件必須以 YAML frontmatter 開頭，包含以下必須欄位：

| 欄位 | 說明 | 範例 | 狀態 |
|------|------|------|------|
| `title` | 文件標題（英文或中文） | `"da-tools CLI Reference"` | 必須 |
| `lang` | 文件語言 | `zh` （繁體中文）或 `en` （英文）| 必須 |
| `tags` | 文件分類標籤（陣列） | `[cli, reference, da-tools]` | 必須 |
| `audience` | 目標受眾（陣列） | `[platform-engineer, sre, tenant]` | 必須 |
| `version` | 文件版本（與 release 版本同步） | `v2.3.0` | 強烈建議 |

**Frontmatter 範例**：

```yaml {.illustrative reason="Markdown 文件的 frontmatter（含前後兩條 --- 分隔線），由 mkdocs 讀，不是 YAML 設定檔"}
---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.9.0
lang: zh
---
```

**受眾標籤定義**：

| 標籤 | 說明 |
|------|------|
| `platform-engineer` | 平台工程師、SRE、DevOps |
| `tenant` | 租戶業務系統負責人 |
| `domain-expert` | 領域專家（DBA、Infrastructure lead） |
| `all` | 適用所有人 |

---

## 2. 文件結構順序

按以下順序組織內容：

### 2.1 第一行：H1 標題

```markdown
# 文件標題
```

標題應清晰反映文件內容。

### 2.2 語言切換行（雙語文件可選）

如果支援多語言，在 H1 之後加上語言指示：

```markdown
> **Language / 語言：** | **中文（當前）** | [English](./filename-en.md)
```

或簡化版本：

```markdown
> **Language / 語言：** | **中文（當前）**
```

### 2.3 上下文區塊（適用對象、版本、相關文件）

在標題下方提供快速上下文：

```markdown
> **受眾**：Platform Engineers、SREs
> **版本**：v2.3.0
>
> **相關文件**：[Architecture](architecture-and-design.md) · [Troubleshooting](./troubleshooting.md)
```

或以表格形式（較為簡潔）：

```markdown
> **v2.9.0** | 適用對象：Platform Engineer、SRE
>
> 相關文件：[Architecture](./architecture-and-design.md) · [Troubleshooting](./troubleshooting.md)
```

### 2.4 簡介段落

一或兩句話說明文件用途，幫助讀者決定是否繼續閱讀。

**範例**：

```markdown
本文檔介紹 Dynamic Alerting 平台提供的運維工具與 Prometheus API 集成。
涵蓋常見的診斷、驗證和配置管理任務。
```

### 2.5 目錄（可選，長文件建議）

對於超過 500 行的文件，在簡介後加入「目錄」section：

```markdown
## 目錄

1. [快速開始](#快速開始)
2. [核心概念](#核心概念)
3. [API 參考](#api-參考)
```

### 2.6 主要內容

使用 H2（`##`）作為主要 section，H3（`###`）作為子 section。避免超過四層標題深度（H4 以下）。

**最佳實踐**：

- 每個 section 開頭加上一句話說明該 section 的目的
- 使用表格、代碼塊、清單等視覺化信息
- 提供實際範例（代碼、配置、命令行輸出）

### 2.7 常見問題（FAQ，如適用）

如果文件涉及複雜概念或常見誤解，加入 FAQ section：

```markdown
## 常見問題

### Q1：為什麼...？
A：...

### Q2：如何...？
A：...
```

### 2.8 相關資源（建議）

**建議**以「相關資源」section 結尾。⚠️ 這條沒有任何機制強制：原本負責的檢查工具 `check_doc_template.py` 從來沒有接上閘門，產生器 `inject_related_docs.py` 則會覆寫人工撰寫的段落，兩者都已退役（[#1984](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1984)）。

此 section 使用表格格式，列舉相關的文檔、工具、API 等資源。

**格式（繁體中文）**：

```markdown
## 相關資源

| 資源 | 說明 |
|------|------|
| [Title 1](link1) | Brief description or relevance |
| [Title 2](link2) | Brief description or relevance |
| [External Resource](https://example.com) | ⭐⭐⭐ (if highly relevant) |
```

**格式（English）**：

```markdown
## Related Resources

| Resource | Description |
|----------|-------------|
| [Title 1](link1) | Brief description |
| [Title 2](link2) | Brief description |
```

**表格欄位**：

| 欄位 | 說明 |
|------|------|
| 資源 (Resource) | 超連結 + 簡短標題（例 `[Playbook](./testing-playbook.md)`） |
| 說明 (Description) | 該資源與本文件的關係（一句話）或相關程度（星號 ⭐⭐⭐） |

---

## 3. 內容撰寫指南

### 3.1 語言與風格

- **語言**：繁體中文（Traditional Chinese）為主
- **英文**：技術術語、產品名稱保持英文（例：Prometheus、ConfigMap、PromQL）
- **觀點**：工程語言，避免營銷宣傳詞彙
- **個人代名詞**：避免「我們」（We），改用被動語態或直接描述事實

**不推薦**：「我們提供了一個強大的告警平台...」
**推薦**：「Dynamic Alerting 平台支援多租戶配置驅動告警...」

### 3.2 代碼與配置示例

- 所有代碼、YAML、PromQL 應包含在代碼塊中，並註明語言：

```yaml
# 配置示例
tenants:
  db-a:
    mysql_connection_usage_percent: "85"
```

```bash
# 命令行示例
da-tools diagnose db-a --config-dir conf.d/
```

```promql
# PromQL 示例
rate(requests_total[5m]) > 100
```

- **`yaml` 區塊由它的讀者判**（[#3416](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3416)）：`check_md_yaml_drift`（`md-yaml-fence-check`／`md-yaml-drift-check`／`md-yaml-crd-check`）與 `threshold-unit-sanity-check` 都從同一份清冊（`scripts/tools/_lib_doc_yaml_inventory.py`，詞彙與判定規則寫在它的 docstring）取區塊。沒宣告的區塊用 conf.d 的讀法（`da-guard parse-tree`）讀，讀得了就照結構判定（conf.d 範例、k8s 物件、其他）；讀不了（多份文件、重複鍵、語法錯誤；頂層不是 mapping 除外）就報錯，要你在語言後面用大括號宣告它是什麼：
  - 消費者：```` ```yaml {.k8s} ````（`---` 分隔的 manifest）、`{.confd}`、`{.helm-values}`、`{.prom-rules}`、`{.alertmanager}`、`{.prometheus}`。
  - 故意拒收的反例：`{.confd .expect-refused=<kind>}`（kind 是 parse-tree 回報的那個，例如 `multiple_documents`）。
  - 讀得了但故意寫錯的反例：`{.confd .expect-invalid}`（schema 檢查必須判 fence 裡**每一個**單元都不合法；要並列一份正確的檔，就拆成兩個 fence）。`.expect-invalid` 只能吸收 schema 錯誤，憑證判定一律照報；反例裡的憑證一律寫 Secret 參照（[#3748](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3748)）。它的值照樣會被 `threshold-unit-sanity-check` 判：讀者會照抄。
  - 不給任何閘門判的區塊：`{.illustrative reason="…"}`（reason 必填，每份報告都會列出）。
  - repo 內某個檔案的節錄：`{.quote src="<repo 相對路徑>"}`，見下方「引用來源檔的區塊」。它和 `.illustrative` 一樣不能與其他屬性並用、每份報告都會列出，因為帶省略的節錄不是完整的檔，沒有讀者可以讀它。
  - 憑證判定只看用 YAML 寫成的 conf.d（[#3748](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3748)）：`{.confd}` 與依結構判為 conf.d 的區塊、沒有消費者的 yaml 區塊（routing 區塊在任何深度），以及 `{.helm-values}` 裡依 chart 渲染規則當成 conf.d 的 `thresholdConfig`（`_defaults.yaml` 與每個租戶一份檔）。用 printf、curl、bash heredoc 或另一份檔裡的字串寫出的內容不在判定範圍內；讀者照抄時由產生器的 `credential_plaintext` 拒收，真的密鑰由 CI 的 TruffleHog 抓。所以要示範一份 conf.d 檔，就寫成 ```` ```yaml {.confd} ````，第一行是檔頭註解（`# conf.d/<路徑>.yaml`），並用一句話說存到哪裡。
  - 一個 fence 示範好幾個檔時，在每個檔的第一行寫 column-0 的檔頭註解，**註解以檔名開頭**：`# conf.d/db-a.yaml`、`# conf.d/db-a.yaml (edge tenant)`、`# _defaults.yaml — 模式 A`（檔名之後要嘛換行，要嘛先空一格再寫註）。`# 見 conf.d/_defaults.yaml`、`# 層級 2：conf.d/finance/_defaults.yaml` 這種檔名不在開頭的是散文，不切檔；要當檔頭就改寫成檔名開頭。
  - 重複鍵由宣告的消費者決定：`.confd` 拒收；`.k8s`／`.helm-values` 依 kubectl／Helm 的讀法是後者勝。所以 `.k8s` 區塊的每份文件都必須是帶字串 `apiVersion` 與 `kind` 的物件，否則報錯。
  - 哪些是 yaml 區塊由站台渲染器決定（[#3814](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3814)）：清冊用 mkdocs.yml 的 `markdown_extensions` 渲染每一頁，網站上渲染成程式碼區塊、而且語言名經 pygments 對到 YAML lexer 的，就是要判的區塊——```` ```yaml ````、`~~~yml`、大寫、```` ``` {.yaml} ```` 都算，寫在 blockquote、清單項目或 admonition 裡也算；```` ```yaml+jinja ```` 這類別的 lexer 不算，也不擋。網站上不渲染成程式碼區塊的（例如沒有大括號的 ```` ```yaml confd ````、沒關上的 fence）就不是區塊，沒有閘門會看它。屬性同樣以渲染器的解讀為準：詞彙是封閉的，寫錯就報錯；`title="…"`、`hl_lines`、`linenums` 是 highlight 自己的選項，不算屬性。唯一刻意的差異是 `pymdownx.snippets` 不展開：`docs/includes/` 裡的檔案自己盤點一次。清冊要把渲染出的區塊對回原始檔的行號；同一段內容出現的位置比渲染出來的區塊多（例如 ````` ````markdown ````` 範例裡有一份一模一樣的），或內容相同但 fence 寫法不同，它分不出是哪一行，就直接報錯，請讓兩段內容不同。舊的 `md-yaml-drift` HTML 註解標記已退役，留著也會報錯。

#### 引用來源檔的區塊（`.quote`）

文件裡貼出 repo 內某個檔案的片段時，在 fence 上宣告它是節錄，`check_doc_quotes`（pre-commit 的 `doc-quote-check`，CI 的 Lint job 每個 PR 都跑）會逐行比對它與來源檔（[#1558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1558)）。任何語言的 fence 都可以：

````markdown
```yaml {.quote src="scripts/tools/metric-dictionary.yaml"}
mysql_global_status_threads_connected:
  maps_to: mysql_connection_usage_percent
```
````

- **能整段轉嵌就轉嵌**：要原樣貼出整個檔或一整段，用 `pymdownx.snippets`（`--8<--`），根本沒有副本會漂。`.quote` 處理轉嵌做不到的節錄（要省略中間幾行、要在 GitHub 原始檔檢視裡也看得到內容）。
- **屬性**：`src` 必填，是 repo 根目錄起算、用 `/` 分隔的路徑，必須是 repo 內存在的一般檔；路徑上（檔案本身或任何一層目錄）不能有 symlink，也不能是 Markdown 頁（`.md`、`.markdown`、`.mdx`，不分大小寫）——文件之間要原樣重複用 snippets 轉嵌，引用文件自己或它的翻譯頁什麼都證明不了。守衛不讀版本控制，所以不判斷檔案有沒有被追蹤：CI 只 checkout 追蹤檔，引用未追蹤的檔會在 CI 紅在「不存在」。`anchor` 可選，是一段字面字串，必須在來源檔裡**恰好出現一次**，比對從它所在的行開始往後找。不接受行號與 regex：行號在上游一改就默默指到別處。其他屬性一律報錯。寫錯的宣告也報錯、不會被默默略過：網站渲染出的區塊若屬性裡有大小寫不同的 `quote` class（`{.Quote}`）或以 `src` 開頭的鍵（`{src2=…}`），沒有大括號、網站根本沒渲染成區塊的 fence 開頭行若寫了 `.quote` 或 `src…=`，都會報；`title="quote_utils.py"` 這種 highlight 選項不算。拼成完全不像的字（`{.qoute}`、`{from=…}`）抓不到。HTML 註解裡留著的舊 `.quote` fence 開頭行也會被報出，刪掉即可。
- **比對單位是整行，整個區塊只有一個縮排位移**：區塊的每一段都要**等於**來源檔裡連續的幾行，逐字元比對、只去掉行尾空白。縮排只允許整塊一起平移：存在一個純空白前綴，讓區塊每個非空行加上它就等於對應的來源行（不展開 tab），空行只對空行。所以可以把巢狀的 YAML 整段拉到最左邊，但不能把某一行改掛到別的鍵底下、把 tab 換成空白，或把 Makefile 的 recipe 移到第 0 欄。不做全形半形、標點、Unicode 的正規化——`→` 寫成 `->`、`；` 寫成 `;`、行尾少了半句，都會紅。
- **中間省略要在區塊裡標出位置**：去掉縮排之後**整行恰好是** `# ...`、`// ...`、`-- ...`、`; ...`（或把 `...` 換成 `…`）之一。後面不能接說明（說明是不受檢查的文字，寫在散文裡）；單獨的 `...`／`…` 不是省略標記，因為它在來源裡常是正常的一行（JS 的 spread、Python 的 stub、YAML 的文件結束）。來源檔本身就有一行與省略標記相同時，帶省略的引用一律報錯（分不出是原文還是省略）。區塊在這些行切成段，各段在來源檔裡依序出現、不重疊，兩段之間至少跳過來源的一行。散文寫「節錄」不算標了省略：讀者會把相鄰的兩行當成原文相鄰。開頭與結尾的省略不必標（節錄本來就是來源的一部分），標了就一定要真的省了東西。同一行裡的省略（`foo … bar`）不支援，要省就拆成整行。
- **區塊裡不加原文沒有的東西**：補充說明寫在散文裡；`src` 已經寫明出處，區塊第一行不用再放檔名註解。
- **中英兩份一起標**：`foo.md` 與 `foo.en.md` 的 `.quote` 區塊數與 `src` 必須相同，只改了一邊會紅。
- **守衛的範圍**：只看宣告了 `.quote` 的 fenced block。沒宣告的引用、行內反引號、程式註解、CHANGELOG 都不看；工具的輸出也不行（守衛不執行任何工具，引用的必須是已提交的檔）。改了來源檔，本機的 pre-commit 不會跑這支（它只在動到 `docs/` 時跑），CI 的 Lint job 會紅在引用它的文件上，訊息會點名文件、行號、`src`、第一個對不上的段與來源檔裡最接近的位置；照來源檔更新引用即可。
- **同一段文字不要同時出現在 `.quote` 區塊與別的 fence 裡**：例如在 ````` ````markdown ````` 範例裡貼了一份一模一樣的，清冊對不回是哪一行，會報「at N places」；讓兩段文字不同即可。

### 3.3 表格

使用 Markdown 表格組織信息。表頭必須使用分隔線：

```markdown
| 欄位 | 說明 | 範例 |
|------|------|------|
| Row 1 Col 1 | Row 1 Col 2 | Row 1 Col 3 |
```

### 3.4 列表

使用無序列表（`-` 或 `*`）或有序列表（`1.` `2.`）。避免過度嵌套（超過 3 層）。

### 3.5 強調與引用

- **粗體**：用於關鍵術語（例 `**threshold-exporter**`）
- **斜體**：用於強調概念（例 `*Config-driven architecture*`）
- **代碼引用**：用於代碼名稱、命令、變數（例 `` `user_threshold` ``）
- **引用塊**：用於重要提示或警告

```markdown
> **警告**：此操作無法復原。

> **提示**：使用 `--dry-run` 先預覽效果。
```

### 3.6 交叉引用

使用相對連結指向同目錄或上層目錄的文件：

```markdown
[Architecture Guide](./architecture-and-design.md)
[CLI Reference](../cli-reference.md)
[Playbook](./internal/testing-playbook.md)
```

不使用絕對 URL（除非指向外部資源，例 GitHub、Prometheus 官方文檔）。

---

## 4. 版本管理

### 4.1 版本號同步

文件 frontmatter 的 `version` 欄位應與當前 release 版本同步。

**更新觸發**：

- 平台版本升級時，執行 `make bump-docs` 自動更新所有文件的 `version: v*.*.*`
- 文件內容有重大改動時，在 commit message 中註明（例 `Update docs: add section on X`）

### 4.2 文件生命週期

新增文件時：

1. 從本模板複製 frontmatter
2. 設定 `version: v2.3.0`（當前版本）
3. 設定 `lang: zh` 或 `lang: en`
4. 選擇合適的 `audience` 標籤
5. 填寫內容
6. （建議）加上「相關資源」section
7. 執行 `pre-commit run --all-files` 驗證

過期或廢止的文件應：

- 在首段加入棄用通知
- 設定重定向（在相關文件中手動補充連結提示）
- 逐步移除或封存至 `docs/archive/`

---

## 5. 自動化檢查

frontmatter 是否存在，由 CI 的 `add_frontmatter.py --check`（`.github/workflows/docs-ci.yaml`）把關。它只檢查有沒有 frontmatter，不會逐欄驗證 `title`、`lang`。

**新文件另外要過的三件事**：

- **登錄**：新的 `docs/*.md` 要進 doc-map（`python3 scripts/tools/dx/generate_doc_map.py --generate --lang all`，`doc-map-check` hook 擋漂移）與 `mkdocs.yml` 的 `nav`（含 en locale 的 `nav_translations`）。`docs/internal/**` 不入 doc-map。
- **雙語標題同步**：有 `.en.md` 配對的文件（`docs/internal/**` 以外），改 h2／h3 標題要在同一顆 commit 改 `.en.md`。⚠️ `bilingual-structure-check` 的 `files:` 只配 `.en.md`／`.zh.md`——只 staged 中文那一份時本地 hook 不會跑。
- **frontmatter `version:`** 填**已發布**的平台版本，不是開發中的目標版本。

**「已發布的映像還是舊行為」要加標記**：文件隨 merge 發布，da-tools 映像要等下一個 `tools/v*` tag。描述新行為時若要提醒讀者「vX.Y.Z 映像還是舊行為」，同一段（表格則同一列）要加 `<!-- image-caveat: vX.Y.Z -->`，`vX.Y.Z` 寫該註記所指的那顆映像（例如 `v2.9.0`）；code fence 裡的註記，把標記放在收尾 fence 的下一行（中間不留空行）。`image-caveat-check`（`scripts/tools/lint/check_image_caveats.py`，也在 `make pre-tag`）在 `components/da-tools/app/VERSION` 超過標記版號時失敗，發版時就得把註記拿掉或改寫成歷史敘述。它也會擋「用了 `vX.Y.Z 映像`／`the image you have` 這類句型卻沒有標記」的段落；換別的措辭它看不到，所以標記才是契約。釘版的映像參照（`ghcr.io/vencil/da-tools:vX.Y.Z`）不用標：發版時 `bump_docs.py --tools` 會改寫，`bump_docs.py --check` 擋漏網。

---

## 6. 模板快速複製

新增文件時，使用以下範本：

```markdown
---
title: "Your Document Title Here"
tags: [tag1, tag2]
audience: [platform-engineer]
version: v2.9.0
lang: zh
---

# 文件標題

> **受眾**：描述目標受眾
> **版本**：v2.3.0
>
> **相關文件**：[Related Docs](./related.md)

簡介段落（一到兩句話）。

---

## 主要 Section 1

說明此 section 目的。

### 小 Section 1.1

內容...

---

## 主要 Section 2

內容...

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [Documentation](./doc.md) | Link description |
| [Tool](./tool.md) | Tool description |
```

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [文件導航地圖](./doc-map.md) | 所有 83 個文檔的完整列表與分類 |
| [開發規範](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) | 平台整體開發指引與架構速查 |
| [文件維護 Playbook](./testing-playbook.md#文件敘述風格) | 文件更新與版本管理的實務指南 |
| [Lint 工具使用](../cli-reference.md) | 文件 Lint 工具詳細用法 |
| [GitHub Release Playbook](./github-release-playbook.md) | 文件版本更新與 Release 流程 |
