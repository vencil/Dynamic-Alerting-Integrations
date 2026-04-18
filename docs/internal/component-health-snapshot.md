---
title: "Component Health Snapshot (v2.7.0 Phase .a baseline)"
tags: [internal, component-health, design-tokens, playwright]
audience: [maintainer, ui-engineer]
version: v2.7.0
lang: zh
---
# Component Health Snapshot (v2.7.0 Phase .a baseline)

> **Snapshot 時點**：2026-04-17（Phase .a 完成，A-4 同一批次採集）
> **verified-at-version**: v2.7.0-final
> **下一次 refresh**：v2.8.0 Phase .a 或 tier-1 新工具加入時
>
> 本檔原屬 `benchmark-v2.7.0-baseline.md §2`，v2.7.1 doc-hygiene 拆分後獨立（與效能數據分離，後者合併進 [`docs/benchmarks.md`](../benchmarks.md)）。

## Tier 分佈

| Tier | Count | 佔比 |
|:-----|------:|-----:|
| Tier 1 (≥7) | 11 | 29% |
| Tier 2 (4-6) | 24 | 63% |
| Tier 3 (≤3) | 3 | 8% |
| **Total** | **38** | 100% |

## Design Token Migration 狀態

| Token Group | 定義 | Count | 佔比 |
|:------------|:-----|------:|-----:|
| A (mature) | density ≥10/100LOC, 0 palette | 11 | 29% |
| B (partial) | density 5-9.9 或 <5 palette | 4 | 11% |
| C (unmigrated) | density <5 且 ≥5 palette | 23 | 60% |

**Tier 1 Group C（未遷移）**: 0/11 — ✅ Phase .a0 fully closed

**Tier 1 Group A（成熟）**: 8/11 — cicd-setup-wizard、cost-estimator、notification-previewer、operator-setup-wizard、playground、rbac-setup-wizard、tenant-manager、threshold-heatmap

## Playwright E2E 覆蓋

| 層級 | 有 spec | 無 spec | 覆蓋率 |
|:-----|--------:|--------:|-------:|
| Tier 1 | 4 | 7 | 36% |
| Tier 2 | 0 | 24 | 0% |
| Tier 3 | 0 | 3 | 0% |
| **Total** | **4** | **34** | **11%** |

## 品質指標

| 指標 | 數值 | 備註 |
|:-----|-----:|:-----|
| Hardcoded hex colors | 4 tools | 待 Phase .b 清理 |
| Hardcoded px values | 12 tools | 低優先 |
| i18n coverage avg | >90% | Tier 1 平均 97%+ |

## 相關資源

- [`component-health-snapshot.json`](./component-health-snapshot.json)（即時資料 SSOT，由 `component-health.jsx` dashboard 消費）
- [`tool-registry.yaml`](../assets/tool-registry.yaml)（互動工具 SOT）
- [Benchmark Report §10](../benchmarks.md#10-工具鏈效能基線)（效能層面）
