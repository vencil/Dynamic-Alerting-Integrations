---
title: "設計深潛導覽 — 架構 spoke 文件"
tags: [design, architecture, navigation]
audience: [platform-engineer, devops]
version: v2.8.1
lang: zh
---

# 設計深潛導覽

> **Language / 語言：** **中文（當前）** | [English](./README.en.md)

本目錄是 [架構與設計](../architecture-and-design.md)（hub）的**深潛 spoke 文件**。每份聚焦單一設計面向，適合已掌握整體概念、想深入某一塊的讀者。

> **建議讀法：** 先讀 [架構與設計](../architecture-and-design.md) 取得全景，再依興趣深潛以下任一 spoke。想看「**為什麼**這樣設計」的決策脈絡，去 [ADR 索引](../adr/README.md)。

## Spoke 文件

| 文件 | 聚焦 | 何時來這裡 |
|------|------|-----------|
| [Config-Driven 架構設計](config-driven.md) | 三態配置、動態路由、Tenant API、SHA-256 熱重載 | 想了解「YAML 如何驅動全鏈路」 |
| [高可用性 (HA) 設計](high-availability.md) | 副本、PodDisruptionBudget、防雙倍計算 | 規劃生產級 HA 部署 |
| [Rule Packs 與 Projected Volume 架構](rule-packs.md) | 規則包獨立部署、零 PR 衝突、按需評估 | 想了解 15 個 Rule Pack 如何隔離交付 |
| [未來擴展路線](roadmap-future.md) | K8s Operator、Design System、Auto-Discovery | 想了解中長期演進方向 |

## 下一步

- 想看決策理由（trade-off / 替代方案）？→ [ADR 索引](../adr/README.md)
- 想動手部署？→ [整合指南](../integration/README.md) · [Platform Engineer 快速入門](../getting-started/for-platform-engineers.md)
- 想看實測數據？→ [性能基準](../benchmarks.md)
