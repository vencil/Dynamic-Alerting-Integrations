---
title: "ADR-019: Profile-as-Directory-Default"
tags: [adr, profile-builder, conf-d, phase-c, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: zh
---

# ADR-019: Profile-as-Directory-Default

> **Language / 語言：** **中文 (Current)** | [English](./019-profile-as-directory-default.en.md)

> Phase .c C-9（v2.8.0 客戶導入管線）。
> 與 [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.md)（目錄分層）+ [ADR-018](018-defaults-yaml-inheritance-dual-hash.md)（繼承語意）為一組。

## 狀態

🟢 **Accepted**（v2.8.0 Phase .c C-9 PR-3 land 時，2026-04-27）

## 背景

C-9 Profile Builder 把客戶的 PromRule corpus 聚成「結構相似」的 cluster。每組 cluster 預期落到 conf.d 樹，問題是**怎麼落**：

1. **每個 tenant 一份完整 tenant.yaml** — N 個結構相似的 rule 變 N 份檔案，繼承關係靠 `_defaults.yaml` 但每個 tenant 還是把所有 key 重複寫一遍。GitOps 反模式。
2. **完全寫進 `_defaults.yaml` 不寫 tenant.yaml** — 失去 per-tenant 微調能力。
3. **共通 default 在 `_defaults.yaml`，只有 tenant 真正不同的值寫 `<id>.yaml` override** — ADR-018 deepMerge 已經支援這種 sparse override，問題是「default 取什麼值？哪些 tenant 算『真正不同』？」沒有明確規則就會被經手人各自詮釋。

C-9 PR-3 同時 ship translator（從 PromRule expr 取 threshold scalar）與 emission（把 cluster 的決策寫成 conf.d 樹）。Translator 的 heuristic 細節是 internal package 範疇（見 `internal/profile/translate.go` package header）；但「emission 形狀是什麼」是跨組件決策，影響 C-10 directory placement、C-12 redundant-override guard、C-11 release packaging — 屬於 ADR 範疇。

## 決策

### Profile-as-Directory-Default

**Cluster 的「共通閾值」放 `_defaults.yaml`；只有「真的不一樣」的 tenant 才寫 `<id>.yaml` override。**

具體規則（emit_translated.go 實作；translator package header 有 metric_key / median 等 heuristic 細節）：

- `_defaults.yaml` 的 `defaults: {<metric_key>: <threshold>}` 用 cluster 的 **median**（不用 mean，避免單一 outlier 拉高/拉低）
- 每個 member 的 threshold 等於 default → **不寫 tenant 檔**（依賴 ADR-018 inheritance）
- 每個 member 的 threshold 不等於 default → 寫 `<id>.yaml`，內容**只含**這個 metric_key 的 override 字串值

範例輸入（3 個 PromRule，閾值 80 / 80 / 1500）：

```yaml
# _defaults.yaml （cluster median = 80）
defaults:
  mysql_connections: 80

# tenant-c.yaml （只有 c 偏離 default）
tenants:
  tenant-c:
    mysql_connections: "1500"
```

tenant-a 和 tenant-b 沒有檔案（runtime deepMerge 從 `_defaults` 拿 80）。

### 為什麼這條 principle 值得 ADR

- **跨組件**：C-9 emission shape / C-10 directory placement / C-11 packaging / C-12 redundant-override guard 全部要遵循這個 default-vs-override 邊界。任一處詮釋不一致就會出現 GitOps 異味（重複的 override / 不該被 default 蓋過的值被蓋過）。
- **客戶可見**：客戶看到的 conf.d/ 形狀直接由這條原則決定。
- **長期穩定**：translator heuristic 可能隨客戶 corpus 演進，但「default vs override 邊界」幾年內不該動。

Translator 內部演算法（metric_key 5-step ladder、majority vote、median 抗 outlier、operator handling、status fallback）屬實作細節，已寫進 `components/threshold-exporter/app/internal/profile/translate.go` package header。本 ADR 不重複，避免雙寫漂移。

## 已知 non-goals（PR-3 不做、跨組件層級）

| Non-goal | 為什麼 | 規劃處 |
|---|---|---|
| C-9 自動推斷 directory placement（哪個 cluster 落 L1 / L2 / L3）| 這是 batch PR pipeline 的職責，需要看整批 corpus 跨 domain/region 分布 | C-10 PR-3 |
| 翻譯 dimensional / regex labels（`{queue=~"q.*"}`）的 emission | 需要表達式重寫 + label expansion，跨組件 | C-10 dimensional support |
| 客戶 PromRule expression 自動改寫成 `> on(tenant) user_threshold{}` 形式 | Rule rewrite 是另一條 toolkit，與 conf.d emission 解耦 | C-10 PR-3 / 客戶手動 |
| Severity-tier 兩階翻譯（`metric_key_critical` 從 cluster 同時推 warning + critical）| Cluster 語意上是同一階；兩階拆開要從「PromRule pair」角度重新 cluster | PR-4 fuzzier matcher / 客戶手動 |

## 互動效應

### 與 ADR-018（deepMerge）

ADR-019 emission 完全依賴 ADR-018 的：

- **null-as-delete**：tenant 想 explicit override 為 0 / null 仍可（PR-3 emission 用顯式數值，不踩 null）
- **map deep-merge**：每個 tenant 檔只列**該 tenant 與 default 不同**的 key，runtime ResolveAt 自動 fallback 到 `_defaults`
- **scalar override**：tenant 字串值（如 `"1500"`）覆蓋 default 數值，runtime 用 strconv 在 ResolveAt 時轉回 float

### 與 ADR-017（目錄分層）

PR-3 emission 的 `<RootPrefix>/<ProposalDir>/` 對應 ADR-017 的 directory level。**Caller**（C-10 batch PR pipeline 是主要使用者）決定 `ProposalDirs[i]` 落在 L1 / L2 / L3 哪一層。**PR-3 不做目錄推斷**；那是 C-10 PR-3 的工作（per planning §C-10）。

### 與 C-12 Dangling Defaults Guard

PR-3 emission 直接吃 ADR-018 deepMerge 形狀後，C-12 guard 自然套用：

- Schema validation：metric_key 必填欄位驗證
- Cardinality guard：predicted-metric-count 包含 PR-3 emission 的所有 metric_key
- Redundant-override warn：tenant override 與 `_defaults` median 相同 → guard 提示移除（其實這條是 Profile-as-Directory-Default 的「事後檢查」防線）

PR-3 ship 後客戶 PR 自動跑 C-12 PR-5 GH Actions wrapper 校驗，閉環。

## 實作位置

| 檔案 | 角色 |
|---|---|
| `internal/profile/translate.go` | Translator + heuristic 細節（metric_key ladder / cluster aggregation / median / operator handling）— package header 有完整 inline doc |
| `internal/profile/emit.go` | `EmissionInput.Translate` 旗標 + dispatch 到 `emitTranslatedProposal`；conf.d-shape 模板實作 |
| `internal/profile/translate_test.go` | 表格測試覆蓋 translator + cluster 聚合（`-race -count=2` 穩定）|

（這些檔案在 `components/threshold-exporter/app/` 下，位於 MkDocs site 範圍外，請從 GitHub 端開啟。）

## 變更紀錄

- v2.8.0 Phase .c C-9 PR-3：本 ADR 與 translator + emit dispatch 一起 ship。
- v2.8.0 Phase .c C-9 PR-3 review：用戶反饋「ADR 必要性？」後，將原本 §2–§6（translator heuristic 細節）移到 `translate.go` package header；本 ADR 收斂為「跨組件 design principle」單一範疇，避免雙寫漂移。
