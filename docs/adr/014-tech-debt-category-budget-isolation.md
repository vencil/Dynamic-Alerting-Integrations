---
title: "ADR-014: TECH-DEBT 類別與 REG Budget 隔離"
tags: [adr, governance, tech-debt, regressions, budget, v2.7.0]
audience: [platform-engineers, tech-leads]
version: v2.7.0
lang: zh
---

# ADR-014: TECH-DEBT 類別與 REG Budget 隔離

> Originally recorded as **DEC-N** in `docs/internal/v2.7.0-planning.md §19`.
> 此 ADR 補上 **governance guardrails**，避免 TECH-DEBT 成為 REG Budget 的逃生口。

## 狀態

✅ **Accepted**（v2.7.0 Day 4, 2026-04-16）— 已 land 於 `docs/internal/known-regressions.md`，TECH-DEBT-001/002 為首批實例。

## 背景

### REG Budget 機制（既有）

`docs/internal/dev-rules.md` 規範 active REG 總數 ≤ 4% of test count（`make pr-preflight` 會檢查）。超過時觸發「Budget 爆表」，PR 不可 merge，需先處理既有 REG 或延後新需求。

### 新增類別的動機

v2.7.0 Phase .a A-3 batch3 survey 發現：cicd-setup-wizard 與 config-lint 完全無 aria 標記。這些**不是 regression**（從未 work 過），而是**首發就欠缺 a11y 的 tech debt**。

若硬塞進 REG registry：
- 違反 REG 的定義（曾經 work 過現在壞）
- 佔用 REG Budget，擠壓真正 regression 的處理空間
- 污染 regression test history（看似新 regression 實為老債務）

若完全不追蹤：
- 知識在 commit message 裡散失
- 無法在 Dashboard / CHANGELOG 顯示 debt backlog
- Phase .a 的 a11y 覆蓋進度沒有量化觀測

## 決策驅動力

- **可追蹤**（和 REG 一樣有 id + severity + reproduction steps）
- **不佔 Budget**（和 REG 獨立計算）
- **有升級路徑**（必要時可轉 REG；不能永久赦免）
- **有時間上限**（不能跨多個 minor 版本仍 open 而無動靜）

## 決策

### 類別定義

在 `docs/internal/known-regressions.md` 新增 "Tech-Debt" 區塊，規格：

| 屬性 | REG | TECH-DEBT |
|---|---|---|
| `id` 前綴 | `REG-XXX` | `TECH-DEBT-XXX` |
| `first_observed` 判準 | 特定 version 開始壞 | 從未 work 過 |
| 佔用 Budget | ✅ 4% limit | ❌ 不佔用 |
| 需要 regression_test | ✅ 必填 | ❌ optional（但有 fix 時應補） |
| `make pr-preflight` 擋 merge | ✅ | ❌ |

### 治理 Guardrails（governance rules，新增）

為防止 TECH-DEBT 成為逃生口：

1. **升級規則 A — 影響擴散**：若 TECH-DEBT 被用戶回報 3 次以上且影響同一 severity level，**必須**評估是否升級為 REG。評估由 Phase owner + maintainer 共同決定，紀錄在 `docs/internal/dev-rules.md`。

2. **升級規則 B — 時間上限（退火）**：TECH-DEBT 跨越 **1 個 minor 版本** 未動 → 強制 triage（評審會議）；跨越 **2 個 minor 版本** 未動 → **自動升級為 REG** 或標為 `wontfix` 歸檔。由 `make playbook-freshness` 擴充到 `make tech-debt-freshness`（新 Makefile target, v2.7.0 收尾追加）。

3. **反向分類禁止**：已存在的 REG 不可**降級**為 TECH-DEBT 來規避 Budget。若發現錯誤分類，走修正 PR 而非 reclassify。

4. **優先順序**：P1 TECH-DEBT 的修復優先度等同 P2 REG；不低於但也不高於。

### 首批實例

- `TECH-DEBT-001`: cicd-setup-wizard 0 aria（P1, resolved Day 4）
- `TECH-DEBT-002`: config-lint 0 aria + 錯誤區無 role=alert（P2, open, planned v2.7.0）

## 拒絕的替代方案

| 方案 | 拒絕原因 |
|---|---|
| 擴大 REG Budget 上限（4% → 6%）| 治標不治本；類別定義混淆 |
| 用 GitHub Issues label 代替 | 與 repo-internal 治理分離；CI / Makefile 無法讀取 |
| 全部塞進 REG + 標註 `is_original_debt: true` | schema 複雜化；Budget 計算要分流 |
| 另建獨立 `tech-debt.md` 檔 | 與 known-regressions.md 治理邏輯重疊，雙維護成本高 |

## 後果

### 正面

- REG Budget 回到「真正 regression」的本意
- Debt backlog 可 aggregation 計算（e.g. "v2.7.0 close 了 3 個 TECH-DEBT"）
- TECH-DEBT-001 同日 resolved 的 pattern 證明此類別可做「短期發現 → 當日修復」循環

### 負面 / 風險

1. **Budget 逃逸口**：若治理規則鬆動，REG 可能被誤分類為 TECH-DEBT。**緩解**：升級規則 B 的時間上限 + 反向分類禁止規則。
2. **多一層分類學習成本**：新 contributor 要理解兩個 category 的差別。**緩解**：`known-regressions.md` 開頭加 REG vs TECH-DEBT 判準流程圖（Day 5 追加）。
3. **governance rules 還沒自動化**：升級規則 B 目前靠人工 triage；`make tech-debt-freshness` 尚未實作。**緩解**：v2.7.0 release 前完成該 Makefile target；若延後，至少在 `docs/internal/dev-rules.md` 加手動 checklist。

## 相關

- `docs/internal/known-regressions.md`（承載定義 + 實例）
- `docs/internal/dev-rules.md` §12 Branch + PR + 未來 §13 TECH-DEBT treatment
- `docs/internal/v2.7.0-planning.md` §19 DEC-N + §20 Day 5 補丁
