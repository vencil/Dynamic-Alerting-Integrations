---
title: "ADR-013: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標"
tags: [adr, metrics, component-health, design-tokens, tier-scoring, v2.7.0]
audience: [frontend-developers, platform-engineers, maintainers]
version: v2.9.0
lang: zh
id: ADR-013
tracking_kind: adr
status: accepted
domain: dx
created_at: 2026-04-18
updated_at: 2026-11-24
---
# ADR-013: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標

> **Language / 語言：** **中文 (Current)** | [English](./013-component-health-token-density-metric.en.md)

## 狀態

✅ **Accepted**（v2.7.0, 2026-04-16）

2026-11-24 修訂：改寫為現行行為。決策不變。

## 決策

`scripts/tools/dx/scan_component_health.py` 掃描 `docs/assets/tool-registry.yaml` 註冊的每個 portal 互動工具（JSX 檔），輸出兩類判斷。兩者作用在同一支工具上，所以收在同一篇 ADR。

1. **Tier 分級改用五項訊號相加，不再只看「被幾份文件引用」**（registry 的 `appears_in`）。`appears_in` 仍照抄進輸出，但不計分。
2. **design token 遷移進度看 token 與殘留 palette 兩邊，不只看 token 數。** 這裡的 design token 指 `var(--da-…)` 這類 CSS 變數，palette 指寫死的 Tailwind 色票 class（例如 `bg-slate-100`）。ADR 原本定義的比例型指標 `token_density` 目前**沒有實作**，詳見下文。

### Tier 分級

分數是五項訊號直接相加（等於每項權重都是 1，程式裡沒有權重參數）：

| 訊號 | 分數 | 判斷方式 |
|---|---|---|
| LOC（行數） | 0–3 | ≥800 → 3、≥400 → 2、≥200 → 1、其餘 0 |
| Audience（受眾） | 0–2 | registry 的 `audience` 同時有 `tenant` 與其他受眾 → 2；有兩種以上受眾但沒有 `tenant` → 1；其餘 0 |
| Phase（使用旅程階段） | 0–2 | registry 的 `journey_phase` 是 `deploy`／`configure` → 2；`monitor`／`troubleshoot` → 1；其餘 0 |
| Writer（會不會寫入） | 0 或 2 | 工具 key 含 wizard、manager、editor、setup、generator、heatmap、playground 其中一個字，或原始碼有送出寫入的痕跡（`onSubmit=`、帶 POST／PUT／PATCH／DELETE 的 `fetch(`、`apiCall`、`window.confirm(`）→ 2；否則 0 |
| Recency（最近修改） | -1～+1 | 檔案最後一次 commit 距今 ≤90 天 → +1；≤180 天 → 0；更久 → -1；查不到日期 → 0 |

分數 ≥7 為 Tier 1、4–6 為 Tier 2、≤3 為 Tier 3。兩種情況會標成 `Tier 3 (deprecation_candidate)`（建議淘汰的候選）：

- LOC < 100 且 Recency 為 -1：**先於**分數判斷，分數再高也一樣。
- 分數 ≤3，且 Writer 與 Audience 都是 0：只在已落到 Tier 3 時才套用。

```python {.quote src="scripts/tools/dx/scan_component_health.py"}
    is_stub_stale = loc < 100 and breakdown["recency"] < 0
    if is_stub_stale:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    if score >= 7:
        return "Tier 1", score, breakdown
    if score >= 4:
        return "Tier 2", score, breakdown
    if breakdown["writer"] == 0 and breakdown["audience"] == 0:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    return "Tier 3", score, breakdown
```

registry 標了 `status: archived` 的工具不評分：輸出 `tier: "Archived"`，並從所有彙總數字排除。下架流程與 `archive_candidates` 自動建議見 [`dev-rules.md`](../internal/dev-rules.md) 的 §T 工具生命週期。

### 範例：沒有任何文件引用，照樣是 Tier 1

registry 中 `threshold-heatmap` 的 `appears_in` 是空的；用舊的單一訊號，它會落在最低一級：

```yaml {.quote src="docs/assets/tool-registry.yaml"}
- key: threshold-heatmap
# ...
  file: interactive/tools/threshold-heatmap.jsx
  audience:
  - platform
  - domain
  - sre
# ...
  journey_phase: monitor
  hub_section: advanced
  appears_in: []
```

執行 `python3 scripts/tools/dx/scan_component_health.py --stdout --today 2026-11-24`，這個工具的輸出（節錄）：

```json
"tier": "Tier 1",
"tier_score": 7,
"tier_breakdown": {"loc": 2, "audience": 1, "phase": 1, "writer": 2, "recency": 1, "_total": 7},
"design_tokens": 99,
"tailwind_palette": 0,
"token_density_per_100_loc": 19.6,
"token_group": "A"
```

504 行 → LOC 2；三種受眾但沒有 `tenant` → Audience 1；`monitor` → Phase 1；key 含 heatmap → Writer 2；檔案在 `--today` 前 90 天內改過 → Recency 1。合計 7，Tier 1。Recency 隨 git 歷史與 `--today` 變動，別的 clone 上跑出來的這一項可能不同。

### token 遷移進度

每個工具輸出這幾個欄位：

| 欄位 | 意義 |
|---|---|
| `design_tokens` | `var(--da-…)` 出現次數 |
| `tailwind_palette` | 寫死的 Tailwind 色票 class 出現次數（slate、gray、blue、red 等色系的 `bg-`／`text-`／`border-` 等 50–950 色階） |
| `hex_colors_hardcoded` | 不在註解行（以 `//` 或 `*` 開頭）裡的 `#` 加 3–8 位十六進位色碼次數 |
| `token_density_per_100_loc` | 每 100 行的 token 數 |
| `token_group` | 遷移分組，規則見下 |

分組同時看 token 數與殘留的 palette、hex：

```python {.quote src="scripts/tools/dx/scan_component_health.py"}
    if tokens >= 80 and tailwind_palette <= 5 and hex_hardcoded <= 5:
        return "A"  # mature
    if tokens >= 20:
        return "B"  # in-progress
    if tailwind_palette == 0 and hex_hardcoded == 0 and tokens == 0:
        return "N/A"  # stylistically trivial
    return "C"  # unmigrated
```

所以「80 個 token、還殘留 20 個 palette」落在 B，不是 A。

**沒有實作的部分。** 本 ADR 定義的比例 `token_density = tokens / (tokens + palette_hits)`（0.0–1.0，愈接近 1 表示殘留 palette 愈少），以及 `token_count`、`palette_count`、`token_density` 這三個輸出欄位，掃描器目前都沒有輸出；現有的 `token_density_per_100_loc` 是每百行的 token 數，不是這個比例。使用規範照原決策保留，適用於這個比例將來實作時：

- **適合**：在 dashboard 上找出「差一點就遷完」的工具（比例 ≥0.9 且 palette > 0）、跨工具比較遷移完成度。
- **不適合**：取代 token 數當主指標（只有 3 個 token、0 個 palette 的工具比例是 1.0，但不成熟）、拿來設硬性門檻（會懲罰本來就沒什麼 palette 的工具）、當作分組的唯一依據。

## 背景

**Tier 只看引用數會系統性低估獨立工具。** 早期掃描器只用 `appears_in`（工具在幾份文件被引用）判 Tier：被多份文件引用的工具排進 Tier 1，同樣複雜、只在 1 份文件出現的 portal 工具落到 Tier 3。Tier 決定 token 遷移的先後與 regression 測試預算的分配，排錯了，投入就跟著錯。

**分組只看 token 數，看不出還剩多少沒遷。** 早期的分組只按 token 數切（≥80 → A、≥20 → B、其餘 C），80 個 token 加 0 個 palette 和 80 個 token 加 20 個 palette 都是 A，後者殘留的 palette 卻不少。

## 後果與已知限制

- **權重是經驗值，沒有校準。** 五項直接相加、各級門檻都是估出來的，沒有拿實際遷移結果驗證過；分組門檻（80／20、palette 與 hex 各 ≤5）也一樣寫死在程式裡。
- **Recency 依賴完整的 git 歷史。** 日期取自 `git log`；在 shallow clone（只抓最近幾個 commit 的 clone）上，很多檔案的最後修改日會落在 clone 的邊界 commit 上，Recency 與 `first_commit` 因此失真，也會影響 `archive_candidates`。
- **Writer 只有 0 與 2。** 判斷靠 key 名稱與原始碼字串比對，沒有中間值；名稱碰巧含 heatmap、setup 之類的字也會拿到 2。
- **只給建議，不擋任何東西。** 掃描器純讀取、正常結束碼是 0，沒有接進 pre-commit 或 CI。輸出檔 `docs/internal/component-health-snapshot.json` 列在 `.gitignore`，要在本機重跑產生。
- **portal 的 component-health 頁面不是即時資料。** `tools/portal/src/interactive/tools/component-health.jsx` 內嵌的是一份寫死的快照（頁面標示快照日期 2026-04-17），不會隨掃描器更新。
- **測試只守住一部分。** `tests/dx/test_scan_component_health.py` 守住 archived 工具的排除、`archive_candidates` 的判準，以及一般工具輸出的欄位清單（含 `token_group`、`token_density_per_100_loc`）。Tier 的分數門檻與各訊號的計分規則沒有直接的單元測試。

## 考慮過的替代方案

| 方案 | 沒選的原因 |
|---|---|
| 保留 `appears_in` 單一訊號 | 系統性低估獨立運作的 portal 工具 |
| 10 個以上的訊號 | 維護成本高；要校準需要 regression 資料 |
| 機器學習分類器 | 工具數太少（registry 目前 45 個），也沒有標註好的訓練資料 |
| 以 token 比例作為分組主軸 | 小工具（3 個 token、0 個 palette）會被錯歸為成熟 |
| 按 `palette_count` 由多到少排序 | 大工具永遠排在前面，不反映完成度 |
| 另做一個綜合的 `migration_score` | 多一個要維護的指標；token 比例已經夠用 |

## 相關

- `scripts/tools/dx/scan_component_health.py`（實作）
- `tests/dx/test_scan_component_health.py`（測試）
- [`dev-rules.md`](../internal/dev-rules.md) §T 工具生命週期（archived 與 `archive_candidates`）
- [`component-health-snapshot.md`](../internal/component-health-snapshot.md)
