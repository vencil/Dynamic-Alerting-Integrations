---
title: "Token Migration Cheat Sheet (archived)"
tags: [internal, design-tokens, migration, archived]
audience: [maintainer]
version: v2.8.1
lang: zh
---
# Token Migration Cheat Sheet — 已歸檔（#444 Phase 1 收尾）

> **狀態：易腐層已移除，耐久層已上抬。**
>
> 本檔原為 #444 Phase 1 token 遷移的工作文件，分兩層：
> - **易腐層**（針對當下 14 檔/70 violations 的逐值 HEX/PX→token 對照表）—— Phase 1
>   遷移已完成、repo-wide 0 violations，這些檔已清乾淨，逐值表留著會誤導未來讀者，**已移除**。
> - **耐久層**（語意優先準則、fontSize/spacing/layout px 處理、CSS fallback 移除、
>   FIXME fallback、豁免語法）—— **已上抬至 [`lint-policy.md`](lint-policy.md) §7「Design-token migration 準則」**。
>
> **未來要遷移 hex/px → `var(--da-*)` token，請直接看 [`lint-policy.md`](lint-policy.md) 的該節。**

## 相關
- Gate：`scripts/tools/lint/check_design_token_usage.py`（(b) class，diff-only fatal）
- Token SSOT：[`design-tokens.css`](../assets/design-tokens.css)
- 領域分類色新設範例：[`design-system-guide.md`](design-system-guide.md) §3.6（#726 dependency-graph）
- Issue：#444（Phase 0 量測修復 #722/#724、Phase 1 遷移 #726/#727/#730）
