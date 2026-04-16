---
title: "ADR-014: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標"
tags: [adr, metrics, component-health, design-tokens, tier-scoring, v2.7.0]
audience: [frontend-developers, platform-engineers, maintainers]
version: v2.7.0
lang: zh
---

# ADR-014: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標

> 合併了兩個決策：
> - **DEC-08**（Day 1, planning §10）：Tier 分級改用五維加權替代 `appears_in` 單訊號
> - **DEC-M**（Day 4, planning §19）：新增 `token_density` 輔助指標
>
> 兩者皆作用在 `scripts/tools/dx/scan_component_health.py`，合併為單一 ADR 避免碎片化。

## 狀態

✅ **Accepted**（DEC-08 Day 1 + DEC-M Day 4, v2.7.0, 2026-04-16）

## 背景

### 問題 1：Tier 分級單訊號失準（DEC-08）

v2.6.x scanner 用 `appears_in`（工具在幾份文件被引用）作為 Tier 唯一判斷：
- tenant-manager 在 6 份文件被引用 → Tier 1
- 同等複雜但只寫在 1 份文件的 portal 工具 → Tier 3

這導致**獨立運作但高價值的 portal 工具被系統性低估**。Tier 分級直接影響 Phase .a0 migration 排序與 regression budget 分配。

### 問題 2：Group A/B/C 缺乏遷移完成度精度（DEC-M）

Phase .a0 批次 3/4 操作中發現 `token_group`（A/B/C = ≥80 / ≥20 / <20 tokens）只是粗分級：
- 80 tokens + 0 palette → Group A
- 80 tokens + 20 palette → 仍 Group A，但殘留 palette 顯著

## 決策

### Part 1：五維加權 Tier 評分（DEC-08）

```python
score = (
    w_loc * loc_signal       # LOC 0–3 (≥800=3, ≥400=2, ≥150=1, <150=0)
  + w_aud * audience_signal  # Audience 0–2 (multi-persona=2, team-internal=1, single=0)
  + w_jp  * journey_signal   # Journey Phase 0–2 (onboarding=2, operate=1, explore=0)
  + w_wr  * writer_signal    # Writer 能力 0–2 (domain-expert=2, agent=1, unknown=0)
  + w_rec * recency_signal   # Recency -1~+1 (last_touched ≤6mo=+1, ≤12mo=0, >12mo=-1)
)
```

所有 `w_*` 權重目前皆為 **1**（均權）。

**Tier 閾值**：≥7 → Tier 1、4–6 → Tier 2、≤3 → Tier 3

**Deprecation override**：`LOC < 100 AND recency < 0` 或 `writer = 0 AND audience = 0` → 強制標記 deprecation candidate，跳過正常 Tier 分級。

#### 拒絕的替代方案

| 方案 | 拒絕原因 |
|---|---|
| 保留 `appears_in` 單訊號 | 系統性低估獨立 portal 工具 |
| 10+ 維度 | 維護成本過高；校準需要 regression data |
| ML 分類器 | 資料量太少（38 工具）；無 labeled training set |

### Part 2：token_density 輔助指標（DEC-M）

```python
token_density = tokens / (tokens + palette_hits)   # 範圍 [0.0, 1.0]
```

輸出範例：

```json
{
  "threshold-heatmap": {
    "token_count": 12, "palette_count": 87,
    "token_density": 0.121, "token_group": "C"
  }
}
```

#### 使用規範

**✅ 適合**：pre-commit / dashboard 顯示「差臨門一腳」（density ≥ 0.9 且 palette > 0）；跨工具比較遷移完成度。

**⛔ 不適合**：取代 `token_count` 為主指標（density=1.0 的 3-token 工具不成熟）；設硬 gate（懲罰 palette 本來就少的工具）；作為 Group A/B/C 唯一依據。

#### 拒絕的替代方案

| 方案 | 拒絕原因 |
|---|---|
| 以 density 為 Group 分級主軸 | 小工具 (3 tokens, 0 palette) 被錯誤歸為「成熟」 |
| `palette_count` 倒序排序 | 不 scalable；大工具永遠在前 |
| 複合 `migration_score` | 多一個需維護的指標；density 已夠用 |

## 後果

### 正面

- Tier 分級不再系統性低估獨立 portal 工具（修正 tenant-manager 等 Day 1 排序偏差）
- Dashboard 精確顯示「哪些工具差 1-2 個 palette」
- Per-tool JSON output 變更 additive，無 breaking change
- Day 4 batch 4 migration 排序比 Day 1 更合理

### 負面 / 風險

1. **五維權重是 heuristic** — 均權 `w=1` 沒有 empirical 校準。**緩解**：Phase .a 結束後用實際 migration 成果回溯驗證，必要時調整。
2. **Density 誤讀** — 可能把 density=1.0 當「完成」。**緩解**：`scan_component_health.py` docstring 標註「density is a secondary signal」。
3. **Group 閾值 (≥80/≥20/<20) 仍硬編碼** — 來源是 Day 1 經驗估計。**緩解**：Phase .a 收束後以實際分布 recalibrate。

## 相關

- `scripts/tools/dx/scan_component_health.py`（實作）
- `docs/internal/v2.7.0-planning.md` §10 DEC-08 + §19 DEC-M
- `docs/internal/v2.7.0-day1to3-retrospective-review.md` §3.1（DEC-08 retrospective）
