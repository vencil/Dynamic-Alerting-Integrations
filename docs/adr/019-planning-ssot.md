---
title: "ADR-019: 計畫追蹤的單一來源 — 各條目自帶 frontmatter，索引自動產生"
tags: [adr, dx, planning, ai-agent, governance]
audience: [platform-engineers, contributors, ai-agents]
version: v2.9.0
lang: zh
id: ADR-019
tracking_kind: adr
status: accepted
domain: docs
created_at: 2026-05-10
updated_at: 2026-11-21
---

# ADR-019: 計畫追蹤的單一來源 — 各條目自帶 frontmatter，索引自動產生

> **這份 ADR 是給誰看的：** 平台的**貢獻者、maintainer 與 AI agent**，主題是專案內部的工作項目怎麼登記與追蹤。部署平台的 Platform Engineer、設定閾值的租戶、評估導入的企業方不需要讀；你要找的多半是 [架構與設計](../architecture-and-design.md)、[ADR 索引](README.md) 或 [快速入門](../getting-started/README.md)。
>
> 依語言政策，本 ADR 只有中文版。

**決策摘要**：每一個追蹤項目（技術債、DX（開發者體驗）待辦、回歸、ADR、sprint 條目）都在它所在的檔案裡帶一段固定格式的 frontmatter，那段 frontmatter 就是這個項目狀態的唯一來源。`docs/internal/planning-index.md` 是由程式掃描全 repo、把這些 frontmatter 彙整出來的索引，不手改。PR 裡的 commit 用 trailer 宣告「解決了某項目」時，CI 檢查該項目的 frontmatter 有沒有同步改成已完成（目前只警告、不擋 merge）。新的追蹤項目一律用 `TRK-<追蹤 issue 號>` 編號；ADR 與 sprint 條目保留各自的編號。

## 狀態

✅ **Accepted**（v2.8.0）。之後的修訂已併入下文對應段落：

| 日期 | 修訂內容 | 在本文的位置 |
|:--|:--|:--|
| 2026-09 | 新條目的編號改為追蹤 issue 的號碼，手工看表配號停用（[#2106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2106)） | 決策 › 編號政策 |
| 2026-11-21 | 改寫表達，對照現行工具行為更新描述；決策不變 | 全文 |

## 名詞

- **SSOT**（single source of truth，單一事實來源）：同一件事只在一個地方登記，其他地方都從那裡讀或從那裡產生。本文的 SSOT 是各追蹤項目自己的 frontmatter。
- **frontmatter**：寫在檔案開頭、或某個小節標題正下方的一段 YAML 中繼資料（`id:`、`status:` 這類欄位）。
- **TRK**：本專案統一的追蹤編號前綴（tracking），例如 `TRK-2150`。
- **TD / HA / REG**：被 TRK 取代的三種舊前綴，分別是技術債（`TD-NN`，長形為 `TECH-DEBT-NNN`）、DX 強化與自動化項目（`HA-NN`）、產品回歸（`REG-NNN`）。舊編號到 TRK 的對照見 [`planning-id-mapping.md`](../internal/planning-id-mapping.md)。
- **S#**：sprint 規劃條目的編號前綴，例如 `S#74`。
- **自動發現**（discovery）：產生器掃描整個 repo、挑出帶有合格 frontmatter 的條目，而不是讀一份寫死的來源檔清單。
- **trailer**：commit 訊息最後一段的 `Key: value` 行，例如 `Resolves: TRK-2150`。

## 背景

做這個決策時，「未來計畫、已知問題、進行中的工作」分散在許多地方：各份 backlog 文件（例如 `dx-tooling-backlog.md`、`frontend-quality-backlog.md`）、sprint 規劃紀錄、`docs/design/roadmap-future.md`、各 ADR 的未來工作段落、程式碼裡的註解、`flaky-tests.yaml`。編號也有好幾套：`TD-NN`、`HA-NN`、`REG-NNN`、`S#NNN`、`ADR-NNN`。

對 AI agent，這造成四種具體的錯：

1. **不知道來源有哪些**：在一份文件裡規劃某件事，其實另一份 backlog 已經有同樣的條目，於是重複設計或互相衝突。
2. **讀到過期狀態**：工作做完了、只改了程式碼沒回頭關 backlog 條目，下一個讀的人以為還沒做。
3. **交叉引用漂移**：改了 A 處的狀態，引用 A 的 B 處沒跟著改。
4. **答不出「現在有什麼在進行」**：要把所有來源讀一遍才拼得出全貌。

對人類貢獻者則是：新人不知道工作該登記在哪一份 backlog；maintainer 季度回顧要跨好幾個來源收斂；「PR 已經解決了 X、backlog 上 X 還掛著」這類不一致只能定期大掃除。

## 決策

### 三層設計

| 層 | 做什麼 | 實作 |
|:--|:--|:--|
| 第一層：frontmatter 契約 | 每個追蹤項目在自己所在的位置帶固定欄位，這段 frontmatter 是唯一來源 | 本文定義欄位與列舉值 |
| 第二層：自動發現的索引產生器 | 掃描全 repo，把合格條目彙整成 `planning-index.md` | `scripts/dx/generate_planning_index.py` |
| 第三層：CI 狀態同步檢查 | PR 的 commit 宣告解決某項目時，驗證該項目已標為完成、`pr_ref` 指向這支 PR | `scripts/tools/lint/check_planning_status_sync.py` |

來源永遠是各檔案裡的 frontmatter，索引只是衍生出來、隨時可以重新產生的檢視。要改某個項目的狀態就改它的來源，不改索引。

### 第一層：frontmatter 契約

每個追蹤項目帶以下欄位：

```yaml {.illustrative reason="planning item 的 frontmatter 契約（各欄位列出可選值），不是任何程式讀的設定檔"}
---
id: TRK-2150 | S#74 | ADR-019   # 三種編號並存，見「編號政策」
tracking_kind: tech-debt | feature | dx | regression | adr | sprint
status: proposed | accepted | in-progress | done | abandoned | superseded
domain: tenant-api | exporter | portal | docs | ci | rule-packs | ...
supersedes: [TRK-005, TRK-010]  # 選填：取代了哪些舊條目
superseded_by: TRK-3541         # 選填：被誰取代
pr_ref: 2201                    # 完成後填
target_version: v3.0.0          # 選填
created_at: 2026-09-01
updated_at: 2026-09-20
owner: someone                  # 選填：誰負責
---
```

兩支工具實際怎麼用這些欄位：

| 欄位 | 工具的行為 |
|:--|:--|
| `id` | 必填；空值的條目不收錄。CI 同步檢查用它對應 commit trailer 裡的編號。 |
| `tracking_kind` | 必填，值必須在上列六個之中，否則不收錄。這是本專案自訂的列舉，用來把剛好也有 `id:`、`status:` 欄位的無關文件（例如 OpenAPI spec、教學文件）排除在外。 |
| `status` | 必填，值必須在上列六個之中，否則不收錄。索引依它分組；CI 同步檢查要求它是 `done`。 |
| `title` | 選填；索引的標題欄。沒寫就用條目所在的小節標題，再沒有就用 `id`。 |
| `domain` | 索引的 Domain 欄。 |
| `pr_ref` | 索引的 PR 欄；CI 同步檢查要求它等於當前 PR 號碼。 |
| `target_version`、`owner` | 產生器會讀進來，目前索引不顯示。 |
| `supersedes`、`superseded_by`、`created_at`、`updated_at` | 這兩支工具都不讀，屬於給人看的欄位。 |

ADR 與 sprint 條目的 `id:` 維持原本的編號：

```yaml {.illustrative reason="三種 namespace 的 id 寫法並列，同一個 id 鍵寫了三次；不是一份可照抄的 frontmatter"}
# ADR
id: ADR-019
tracking_kind: adr

# Sprint
id: S#74
tracking_kind: sprint

# 統一追蹤項目
id: TRK-2150
tracking_kind: tech-debt | regression | dx
```

frontmatter 可以寫在四種位置，產生器都會找到：

1. **`docs/**/*.md` 的檔案開頭**：整份檔案是一個條目，例如每一份 ADR。
2. **`docs/**/*.md` 裡 H2／H3 標題（`##`／`###`）正下方、語言標記為 `yaml`（或 `yml`）的程式碼區塊**（標題與區塊之間只能有空行，語言標記後面不能接其他內容；用 `---` 包起來的 YAML 不算，會被靜默略過）：一份檔案放多個條目，例如 `dx-tooling-backlog.md`。
3. **`flaky-tests.yaml` 頂層清單裡帶 `tracking_kind:` 的項目**：沒有 `tracking_kind:` 的項目不收錄。
4. **程式碼註解**：`// TECH-DEBT(id=TRK-042, status=in-progress, tracking_kind=tech-debt)` 或 `# TECH-DEBT(...)`，逗號分隔的 `key=value`，而且註解符號必須是該行第一個非空白字元（同一行裡跟在程式碼或文字後面的不算）。掃描是逐行比對，不認得字串邊界：Python 多行字串裡若有一行以 `# TECH-DEBT(` 開頭，同樣會被收錄。掃描範圍是 `components/` 的 `.go`／`.py`、`tools/` 的 `.ts`／`.tsx`／`.js`／`.jsx`、`tests/` 的 `.go`／`.py`／`.ts`／`.tsx`、`scripts/` 的 `.py`／`.sh`，略過 `node_modules/` 與 `archive/` 底下的檔案。

### 編號政策：三種前綴並存

| 前綴 | 用途 | 為什麼不和其他前綴合併 |
|:--|:--|:--|
| **TRK** | 所有「該修還沒修」的追蹤項目：技術債、回歸、DX 待辦。取代舊的 TD／HA／REG 三種前綴。 | TD、HA、REG 本質上是同一類東西，分成三種前綴只是歷史包袱，人和 AI 各記三套沒有價值。 |
| **ADR** | 架構決策紀錄。 | ADR 是永久的設計歷史，不是待辦清單，而且已經被使用者文件、commit 訊息與外部引用大量引用（例如 [README.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/README.md)）；重新編號等於改寫設計史，還要永久維護轉址對照。兩者語意也不同：ADR 回答「這個決策為什麼這樣做」，TRK 回答「這件工作做完了沒」。 |
| **S#** | sprint 規劃條目：屬於某一個 sprint 的工作項目。 | sprint 條目帶時間序與階段的意思，和跨 sprint 持續存在的債務不同，混進 TRK 會稀釋語意。sprint 結束時條目仍未完成才轉成 TRK；已完成的直接歸檔，不改編號。 |

**新條目怎麼編號**：開一張追蹤 issue，條目編號就是那張 issue 的號碼，寫成 `TRK-<issue 號>`，一律四位數以上（issue #2150 → `TRK-2150`）。三位數的號碼全部屬於已凍結的舊表；要追蹤一張號碼小於 1000 的舊 issue，就另開一張新的追蹤 issue。這樣做是因為手工看表取「下一號」不是原子操作：兩個 session 各自取號就會撞號，登記表也永遠落後於開票，表上的空號又會讓下一個人取錯號。改用 issue 號之後，唯一性由 GitHub 保證，也不需要登記（Kubernetes 的 KEP 同樣以追蹤 issue 號當編號）。

`TRK-300`～`TRK-392` 這些手工配出的號碼留在 [`planning-id-mapping.md`](../internal/planning-id-mapping.md)，該表凍結在 TRK-392。`scripts/tools/lint/check_trk_index_coverage.py` 只檢查三位數號碼，有兩道關：

- issue 或 PR 開立、修改標題時（`.github/workflows/trk-index-coverage.yaml`），標題裡出現表上沒有的三位數 TRK，就是有人照舊手工配號，會紅。
- 表上多出凍結時沒有的列（包括 TRK-392 以後的號碼，以及補舊區段空號的列），在 Python Tests 裡會紅。

**舊編號的處理**：

- 舊的 `TECH-DEBT-NNN`／`TD-NN`／`HA-NN`／`REG-NNN` 對應到哪個 TRK，查 [`planning-id-mapping.md`](../internal/planning-id-mapping.md)，其中也說明了三段編號分區（例如 `HA-11` → `TRK-011`、`REG-004` → `TRK-104`、`TD-022` → `TRK-222`）。
- `CHANGELOG-archive.md` 與 `docs/internal/archive/` 裡的舊編號保留原樣，不改寫；讀到時查對照表。
- 本文裡出現的 `TD-NN`／`HA-NN`／`REG-NN` 是在講政策本身，不是在引用某個追蹤項目，所以刻意保留。

### 第二層：自動發現的索引產生器

`scripts/dx/generate_planning_index.py` 掃描上一節列的四種位置，收錄 `id` 非空、`tracking_kind` 與 `status` 都在列舉內的條目，寫進 `docs/internal/planning-index.md` 的 `<!-- PLANNING_INDEX_START -->` 與 `<!-- PLANNING_INDEX_END -->` 之間。

- 依 `status` 分組，順序是 `in-progress`、`accepted`、`proposed`、`done`、`abandoned`、`superseded`；組內依 `tracking_kind`、再依 `id` 排序。三種前綴的條目混在同一組裡，不分開顯示。
- 每列連到來源檔；來源不是 `.md` 時（程式碼、`flaky-tests.yaml`）連結附行號。
- `make planning-index`（等同 `--write`）重新產生索引；`--check` 只比對，索引過期就以結束碼 1 失敗。
- pre-commit 的 `planning-index-check` 會在 `docs/` 底下的 `.md`、`flaky-tests.yaml`、產生器本身或索引變動時執行 `--check`。

用自動發現而不是寫死來源檔清單，是為了讓新增的 backlog 文件不必有人記得去登記：第一個寫上合格 frontmatter 的條目就會出現在索引裡。

#### 範例：一個條目在索引裡長什麼樣子

假設 repo 裡有這兩份檔案（示意內容）。`docs/internal/example-spec.md`：

```markdown
---
title: "範例規格"
id: TRK-2150
tracking_kind: dx
status: done
domain: ci
pr_ref: 2201
created_at: 2026-09-01
updated_at: 2026-09-20
---

# 範例規格
```

`docs/internal/example-wip.md`：

```markdown
---
title: "狀態不在列舉內的規格"
id: TRK-2160
tracking_kind: dx
status: wip
---

# 狀態不在列舉內的規格
```

執行 `python3 scripts/dx/generate_planning_index.py --write` 後，索引的哨點之間會是：

```markdown
### done (1)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `TRK-2150` | dx | 範例規格 | ci | #2201 | [docs/internal/example-spec.md](../internal/example-spec.md) |
```

`TRK-2160` 的 `status: wip` 不在列舉內，整個條目沒有被收錄，產生器也不報錯。

範例刻意用「檔案開頭的 frontmatter」這種寫法：本 ADR 自己也在 `docs/` 底下，若在這裡貼一段「H2／H3 標題＋`yaml` 程式碼區塊」，即使外面再包一層程式碼區塊，產生器也會把它當成真的條目收進索引。

### 第三層：CI 狀態同步檢查

`scripts/tools/lint/check_planning_status_sync.py` 讀 PR 範圍（`<base>..HEAD`）內每一個 commit 的 `Resolves`、`Closes`、`Fixes`、`Fix` trailer，取出其中的 `TRK-<數字>`（可帶一個小寫字母後綴，例如 `TRK-230c`）、`ADR-<數字>`、`S#<數字>`，用第二層同一套自動發現找到對應條目，逐一檢查：

| 檢查 | 不符合時的回報類型 |
|:--|:--|
| 找得到這個編號的條目 | `missing-entry` |
| 條目的 `status` 是 `done` | `status-not-done` |
| 條目的 `pr_ref` 等於當前 PR 號碼（有給 `--pr-number` 時才檢查） | `pr-ref-missing`（沒填）／`pr-ref-mismatch`（填了別的號碼） |

`.github/workflows/planning-status-sync.yaml` 在 PR 開立、推新 commit、重新開啟、編輯時執行，傳入 base 分支與 PR 號碼，**不帶 `--strict`**：有問題時印到 stderr，並在 GitHub Actions 的 PR 檢查頁面加上 warning 註記，但結束碼是 0，不擋 merge。帶 `--strict`（或同義的 `--ci`）時有問題就以結束碼 1 失敗。

本機可以直接跑 `python3 scripts/tools/lint/check_planning_status_sync.py`（base 預設 `origin/main`）。拿上面範例的條目、假設當前 PR 是 2202，兩個 trailer `Resolves: TRK-2150` 與 `Fixes: TRK-2160` 的回報是：

```text
Found 2 planning-status sync issue(s) across 2 trailer reference(s):

  [pr-ref-mismatch] TRK-2150: trailer `Resolves TRK-2150` (commit abc1234) but entry in docs/internal/example-spec.md has pr_ref `2201` ≠ current PR `2202`. Fill the entry's `pr_ref:` field.
  [missing-entry] TRK-2160: trailer `Fixes TRK-2160` (commit abc1234) but no matching planning entry found in repo. Add frontmatter (`id:` + `tracking_kind:` + `status:`) to the backlog file so the entry is discoverable.

Default behaviour is **soft-warn** (exit 0). Run with `--strict` to fail CI on these issues.
```

`TRK-2160` 明明有自己的檔案，回報卻是 `missing-entry`：因為它的 `status` 不合列舉、沒被收錄。

#### 實作注意事項

**trailer 由 git 自己解析，不用正規表示式掃 commit 內文。** 工具呼叫 `git log --format='%h%x09%(trailers:key=<Key>,valueonly=true,unfold=true)'`，因此：

- trailer 的鍵不分大小寫，`resolves:` 與 `Resolves:` 都認。
- 只有 commit 訊息最後一段、`Key: value` 格式的行才是 trailer；寫在內文的「Resolves TRK-2153」不算。
- 編號本身區分大小寫，`trk-2152` 不算。
- 值以空白切開，每一段去掉結尾的 `,`／`;` 後必須**整段**等於一個編號，所以 `TRK-1` 不會誤配到 `TRK-10`，同一行也可以列多個編號。
- 舊前綴 `TD-NN`／`HA-NN`／`REG-NN` 一律忽略，要先換成 TRK 才有意義。

以下是在一個測試 repo 上實際跑出來的結果（每列是一個 commit 訊息的最後一段，第五列是內文）：

| commit 訊息 | 抓到的編號 |
|:--|:--|
| `Resolves: TRK-2150` | `TRK-2150` |
| `resolves: TRK-2151` | `TRK-2151` |
| `Closes: TRK-1, TRK-10` | `TRK-1`、`TRK-10` |
| `Fixes: trk-2152` | 無 |
| 內文寫 `Resolves TRK-2153 in prose`，後面還有一段 | 無 |
| `Resolves: TD-30` | 無 |
| `Fix: S#74`、`Fixes: TRK-230c`、`Closes: ADR-019` | `S#74`、`TRK-230c`、`ADR-019` |

**CI 的 checkout 必須拿到完整歷史。** `actions/checkout` 預設只拉一個 commit，`<base>..HEAD` 的範圍算不出來。`planning-status-sync.yaml` 與 `self-review-pass2.yaml`（同樣讀 PR 範圍內的 trailer）都設了 `fetch-depth: 0`，並另外 `git fetch` base 分支。base ref 找不到時，工具以結束碼 2 結束並提示檢查 `fetch-depth: 0`。

## 後果與已知限制

**得到的**：

- AI agent 與新貢獻者讀一份索引就能看到全部追蹤項目與狀態，不必逐一翻各個來源。
- maintainer 季度回顧從跨多個來源收斂，變成在一份索引裡篩選。
- 追蹤項目的前綴從 TD／HA／REG 三種收斂成 TRK 一種；ADR 與 sprint 條目保留各自的語意，不混進待辦清單。
- 新增的 backlog 文件不需要登記到任何清單就會被索引。

**要承擔的**：

- 每個追蹤項目都要寫 frontmatter，至少 `id`、`tracking_kind`、`status` 三欄，完成時再補 `pr_ref`。
- `planning-id-mapping.md` 要永久保留，歷史檔案裡的舊編號靠它查到對應的 TRK。

**已知限制**：

- CI 同步檢查目前只警告、不擋 merge（workflow 沒有帶 `--strict`）。「PR 解決了某項目、frontmatter 沒改成 `done`」仍然合得進去，只會留下 warning 註記。本決策還要求以下幾件事，目前都沒有實作：
  - 同一個項目連續三支 PR 仍未同步，就改為擋 merge。
  - 同步檢查不通過時在 PR 留言提醒（現在只有 warning 註記）。
  - 本機 pre-push 時先跑一次同步檢查。
  - 每次 commit 時自動重產索引並加進 commit（現在 pre-commit 只做 `--check`，過期就失敗，要自己跑 `make planning-index`）。
  - 每月一次的定期掃描，列出過期條目給 maintainer 季度回顧。
  - 在 CLAUDE.md 的起手式要求 AI agent 先讀 `planning-index.md`（現行 CLAUDE.md 只在路由表指向對照表與本 ADR）。
- `pr_ref` 只能填一個號碼。填成清單時會被串成 `"375, 380"` 這樣的字串，與 PR 號碼比對一定不符，同步檢查回報 `pr-ref-mismatch`。
- 欄位值不在列舉內、或 YAML 解析失敗的條目會被靜默略過，不報錯。它不會出現在索引裡；有 commit 宣告解決它時，CI 回報的是 `missing-entry`，而不是指出欄位寫錯。
- 只認 commit trailer。寫在 PR 標題或 PR 說明裡的「Resolves …」不會被檢查。

**還沒做的延伸**：

- 如果 AI agent 讀索引的習慣還不夠，可以另做一支 skill，在觸發時直接帶入索引內容。
- 依 domain 篩選的檢視，讓某個 domain 的負責人只看自己範圍內進行中的項目。
- ADR 合併時自動掃描本文提到的 TRK，與解決的追蹤項目互相連結。

## 考慮過的替代方案

### 全部合併成單一前綴，ADR 也一起改（不採用）

好處是人和 AI 只需要熟悉一種前綴。不採用的原因：

- ADR 要全部重新編號，這是風險最高的部分。ADR 已經被使用者文件（README、[架構與設計](../architecture-and-design.md)、各元件 README）當成設計依據引用，也出現在 commit 訊息、客戶看的 changelog 與 GitHub issue 裡；重新編號等於改寫設計史，轉址對照要永久維護。
- 所有既有的 TD／HA／REG／S# 引用都要改寫。
- ADR 是設計歷史，待辦追蹤是工作進度，兩者合併會稀釋各自的語意。

與最後採用的做法相比，多出來的成本幾乎都花在 ADR 重新編號上，換到的「ADR 也進 TRK」反而是設計上的退步。

### 保留所有舊前綴，不引入 TRK（不採用）

好處是不必改寫任何既有引用，只要補 frontmatter。不採用的原因：TD、HA、REG 本質同類，長期並存是歷史包袱；AI agent 與新貢獻者要學好幾種看起來都像待辦的前綴；而且沒有解決核心問題——分散不只是「散在不同檔案」，也是「同一個概念有三個名字」。省下的改寫成本很小，換來的是編號分散永久存在。

### 只靠定期掃描找過期條目（不當主要防線，保留為次要安全網）

做法是排程定期掃描，找出「標成完成卻沒有 PR 編號」「標成進行中卻很久沒動」的條目。不當主要防線的原因：這是被動的，漂移已經發生才會發現。PR 宣告解決某項目的那一刻是最主動的檢查點，在 CI 階段驗證成本最低。決策是以 PR 時的檢查為主，每月一次的定期掃描為輔，列出過期條目給 maintainer 季度回顧；這個每月掃描目前沒有實作（見已知限制）。

### 在產生器裡寫死來源檔清單（不採用）

做法是讓產生器只掃一份固定的檔案路徑清單。不採用的原因：之後新增的 backlog 文件（例如一份新的 security backlog）很容易忘記加進清單，新來源就對索引與 AI 隱形。因此採用自動發現：掃描整個 repo，以 `tracking_kind:` 欄位過濾。

### 新 TRK 號碼看登記表手工取下一號（已停用）

這是 TRK 剛推出時的做法。停用的原因見「編號政策」：取號不是原子操作、登記表落後於開票、表上的空號誤導下一個人。

## 相關

- [`planning-index.md`](../internal/planning-index.md)：產生出來的索引。
- [`planning-id-mapping.md`](../internal/planning-id-mapping.md)：舊編號 → TRK 對照表與編號分區。
- [`lint-policy.md`](../internal/lint-policy.md)：lint 的分類與治理；與本 ADR 同時推出。
- [`dev-rules.md`](../internal/dev-rules.md) §4 Doc-as-Code：同樣主張用自動驗證取代人工記憶。
- 實作位置：`scripts/dx/generate_planning_index.py`、`scripts/tools/lint/check_planning_status_sync.py`、`scripts/tools/lint/check_trk_index_coverage.py`、`.github/workflows/planning-status-sync.yaml`、`.github/workflows/trk-index-coverage.yaml`、`.pre-commit-config.yaml` 的 `planning-index-check`。
