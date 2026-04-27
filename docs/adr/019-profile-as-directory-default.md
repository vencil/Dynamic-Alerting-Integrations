---
title: "ADR-019: Profile-as-Directory-Default + PromRule→threshold translator"
tags: [adr, profile-builder, translator, conf-d, phase-c, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: zh
---

# ADR-019: Profile-as-Directory-Default + PromRule→threshold translator

> **Language / 語言：** **中文 (Current)** | [English](./019-profile-as-directory-default.en.md)

> Phase .c C-9（v2.8.0 客戶導入管線）。
> 與 [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.md)（目錄分層）+ [ADR-018](018-defaults-yaml-inheritance-dual-hash.md)（繼承語意）為一組。

## 狀態

🟢 **Accepted**（v2.8.0 Phase .c, 2026-04-27, with PR #N C-9 PR-3 land）

## 背景

C-9 Profile Builder 把客戶的 PromRule corpus 聚成「結構相似」的 cluster，目標是把每組 cluster 轉成 conf.d 樹下**一份** `_defaults.yaml`（cluster 共通結構）+ **多份輕量** tenant override（僅變動值），而非 N 份完整 tenant.yaml 拷貝（GitOps 反模式）。

PR-1 ship 了 cluster 引擎，PR-2 ship 了 *intermediate artifact* emission（`shared_expr_template` 等元資料），但這個中間格式不是 threshold-exporter 的 ADR-018 deepMerge 引擎吃得下的形狀：

```yaml
# Intermediate (PR-2 shape) — NOT consumed by exporter runtime
shared_expr_template: 'rate(node_cpu_seconds_total[<NUM>m]) > <NUM>'
dialect: prom
member_count: 5
```

```yaml
# Conf.d-ready (what exporter ResolveAt actually loads)
defaults:
  cpu_rate_5m: 0.85
```

兩者之間的差距是兩件事：

1. 從 PromQL 表達式裡萃取 **scalar threshold**（`> 0.85` → `0.85`）
2. 替每個 cluster 決定一個穩定的 **`metric_key`**（conf.d 用什麼欄位名稱來公開這個值）

PR-3 ship 兩個東西：
- `internal/profile/translate.go` — 純 Go function，做 (1) AST walk + (2) metric_key resolution
- ADR-019（本 doc）— 釘死「Profile-as-Directory-Default」原則 + metric_key 解析順序 + 已知非目標

## 決策

### 1. Profile-as-Directory-Default

**Cluster 的「共通閾值」放 `_defaults.yaml`；只有「真的不一樣」的 tenant 才寫 `<id>.yaml` override。**

具體規則（emit_translated.go 實作）：

- `_defaults.yaml` 的 `defaults: {<metric_key>: <threshold>}` 用 cluster 的 **median**（不用 mean，避免單一 outlier 拉高/拉低）。
- 每個 member 的 threshold 等於 default → 不寫 tenant 檔（依賴 ADR-018 inheritance）
- 每個 member 的 threshold 不等於 default → 寫 `<id>.yaml`，內容只含這個 metric_key 的 override 字串值

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

tenant-a 和 tenant-b 沒有檔案（runtime deepMerge 會從 _defaults 拿 80）。**Conf.d 的「行數節省」就是這個機制具體化**。

### 2. `metric_key` 解析順序（resolveMetricKey）

`metric_key` 是 conf.d 對外的「閾值欄位 key」。由於 PromRule 本身沒有這個概念，translator 必須選一個。**ADR-019 釘死的解析順序如下，由上而下，第一個命中為準**：

| 順位 | 來源 | 行為 | 信心 |
|------|------|------|------|
| 1 | `rule.Labels["metric_key"]` | 直接用 | OK（顯式） |
| 2 | `rule.Alert` snake-cased | 用 alert name 推 | Partial（warn） |
| 3 | `rule.Record` snake-cased | record rule fallback | Partial（warn） |
| 4 | 表達式內第一個 `MetricExpr.__name__` | 最後 fallback | Partial（warn） |
| 5 | 都沒有 | translation status = `skipped` | — |

**設計理由**：

- 顯式 label（順位 1）不發 warning，因為客戶**選擇**了這個 key。沒有不確定性。
- alert/record 名稱（順位 2/3）發 warning，因為「rule 改名 → metric_key 漂移」是真實風險。Reviewer 看到 warning 才會去想要不要改成 explicit label。
- inner metric name（順位 4）最弱，只當作不得已的最後一根稻草。
- 都沒有（順位 5）translator 就停手，**拒絕亂猜**。Skipped status 會 fall back 到 PR-2 的 intermediate artifact 形式，留人介入。

### 3. Cluster-level 聚合規則（TranslateProposal）

當 cluster 有 N 個 member，translator 要決定一組「cluster 級的決策」：

| 軸 | 規則 | 不一致時 |
|---|------|---------|
| `metric_key` | 多數決 | Status = Partial + warning 列出 dissent |
| `operator`（`>` / `>=` / `<` / `<=`）| 多數決 | 同上 |
| `severity` | 多數決 | 同上 |
| `default_threshold` | **median** | 不適用（純數值） |

- **多數決而非錯誤**：因為 cluster 已經被 PR-1 認定為「結構相似」，這些軸 99% 一致；不一致時人類要進來看，但 translator 不該 hard fail（會擋 batch emission），而是降級成 Partial 並把 dissent 寫進 PROPOSAL.md / `_defaults.yaml` header comment。
- **median 而非 mean**：threshold 經常含 outlier（單一 tenant 擅自把 limit 調超高）。Median 對 outlier 免疫；mean 不是。

### 4. Comparison operator 處理

PR-3 識別 4 個 operator：`>`, `>=`, `<`, `<=`。
- `==` / `!=`：**ADR-019 §non-goals**。「等於」對連續閾值很罕見且語意不清，留人介入。
- `0.85 < metric` 倒寫：translator 自動 flip 成 `metric > 0.85` 形式，downstream consumer 看到的永遠是「metric op threshold」單一形狀。
- 多個 comparison（`a > 1 and b > 2`）：translator pre-order 走樹找第一個，記錄為 partial + warning。確切的「哪一個是主 threshold」由人類判定。

### 5. 翻譯狀態與 fallback

translator 對每條 rule 出三種狀態：

| Status | 條件 | Cluster-level 處理 |
|--------|------|-----|
| `ok` | 顯式 metric_key + 單一數值 comparison + severity label | 算入 cluster median |
| `partial` | metric_key 用 fallback 推 / 不一致 / 缺 severity | 算入 cluster median，但 Warnings 寫清楚 |
| `skipped` | parse 錯 / 無 numeric comparison / vector comparison / 等式 / 無 metric_key 來源 | **不算進 cluster median**，PROPOSAL.md 列為待人工處理 |

**Cluster fallback**：cluster 內所有 member 都 skipped → cluster status 也是 skipped → **emit 走 PR-2 intermediate format**（不是 conf.d-ready）。Reviewer 看到 intermediate 形式就知道這 cluster 翻不過來，不會誤以為已經 conf.d 化。

### 6. Emission mode dispatch

`EmissionInput.Translate bool` 是 caller 旗標：

- `false`（PR-2 預設）：emit intermediate format。Backwards-compat 給已經有 PR-2 整合的工具。
- `true`：嘗試 translate；per-proposal 動態 dispatch — TranslationOK / Partial 走 conf.d-shape，TranslationSkipped 走 intermediate-shape。

**為什麼 per-proposal 而不是全 batch all-or-nothing**：客戶 corpus 通常混雜易翻 + 難翻。如果一隻難翻就把整批拉回 intermediate，使用者會覺得 PR-3 沒在做事。Per-proposal 的好處是「能翻多少翻多少，剩下的讓你看」。

## 已知 non-goals（PR-3 不做）

| Non-goal | 為什麼 | 規劃處 |
|---|---|---|
| 自動改寫 PrometheusRule 表達式（`expr > N` → `expr > on(tenant) user_threshold{...}`）| Rule rewrite 是另一個 toolkit，不在 conf.d emission scope | 留 C-10 PR-3 / 客戶手動 |
| `==` / `!=` operator 翻譯 | 對連續 threshold 沒語意 | 留 ADR-020 if 客戶證明用例 |
| Histogram quantile bucketing | 非 scalar comparison | 留 v2.9.0 |
| 翻譯 dimensional / regex labels（如 `{queue=~"q.*"}`）| 需要表達式重寫 + label expansion | 留 C-10 dimensional support |
| Severity-tier 兩階翻譯（從 cluster 同時推 warning + critical）| Cluster 內若 severity 不一致，PR-3 用多數決選一個 | 留 PR-4（fuzzier matcher）|
| 多 comparison 的 root-pick 演算法 | pre-order 走樹拿第一個是「夠用」的策略 | 留客戶 corpus 真的需要時再做 |

## 互動效應

### 與 ADR-018（deepMerge）

ADR-019 emission 完全依賴 ADR-018 的：
- **null-as-delete**：tenant 想 explicit override 為 0 / null 仍可（PR-3 emission 用顯式數值，不踩 null）
- **map deep-merge**：每個 tenant 檔只列**該 tenant 與 default 不同**的 key，runtime ResolveAt 自動把缺漏的 fallback 到 _defaults
- **scalar override**：tenant 字串值（如 `"1500"`）覆蓋 default 數值，runtime 用 strconv 在 ResolveAt 時轉回 float

### 與 ADR-017（目錄分層）

PR-3 emission 的 `<RootPrefix>/<ProposalDir>/` 對應 ADR-017 的 directory level。Caller（C-10 batch PR pipeline 是主要使用者）決定 `ProposalDirs[i]` 落在 L1 / L2 / L3 哪一層。**PR-3 不做目錄推斷**；那是 C-10 PR-3 的工作（per planning §C-10）。

### 與 C-12 Dangling Defaults Guard

PR-3 emission 直接吃 ADR-018 deepMerge 形狀後，C-12 guard 自然套用：
- Schema validation：metric_key 必填欄位驗證
- Cardinality guard：predicted-metric-count 包含 PR-3 emission 的所有 metric_key
- Redundant-override warn：tenant override 與 _defaults median 相同 → guard 提示移除

PR-3 ship 後客戶 PR 會自動跑 C-12 PR-5 GH Actions wrapper 校驗，閉環。

## 實作位置

下列檔案在 `components/threshold-exporter/app/` 之下（位於 MkDocs site 範圍外，請從 GitHub 端開啟）：

| 檔案 | 角色 |
|---|---|
| `internal/profile/translate.go` | `TranslateRule`（單規則）+ `TranslateProposal`（cluster 聚合）|
| `internal/profile/emit.go` | `EmissionInput.Translate` 旗標 + dispatch 到 `emitTranslatedProposal` |
| `internal/profile/translate_test.go` | 表格測試覆蓋 ADR-019 §metric-key-resolution / §cluster-aggregation / §non-goals |

## 驗證

PR-3 land 時必須 `-race -count=2` 全綠：
- `go test ./internal/profile/...`
- `go test ./...`（threshold-exporter 全 module sweep）
- `go test ./...`（tenant-api：驗 EffectiveConfig 沒受 PR-5 那批新欄位影響）

PR-3 不影響的：
- C-12 PR-5 GH Actions wrapper（ADR-019 emission 直接 conf.d-shape，guard 自然套用）
- PR-2 intermediate emission path（fallback 仍可用）
- tenant-api `/effective` JSON 契約（PR-3 不動 EffectiveConfig）

## 變更紀錄

- v2.8.0 Phase .c C-9 PR-3：本 ADR 與 translator + emit dispatch 一起 ship。
