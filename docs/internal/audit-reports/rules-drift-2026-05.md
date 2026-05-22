# Rule-corpus drift audit — 2026-05-22

> 由 `scripts/ops/audit_rules_drift.py`（TRK-307）產生。MANUAL 季度稽核；只標 drift 候選，**不自動修改**。處理 SOP 見 [`quarterly-audit-sop.md`](../quarterly-audit-sop.md)。

## 語料盤點

| 來源 | 數量 |
|---|---|
| dev-rules `### ` 條目 | 34 |
| pre-commit hooks | 67（51 auto / 13 manual / 3 pre-push）|
| 本地 skills | 4 |
| memory feedback 卡 | 17 |

## 1. Count reconciliation（hook 切分 vs CLAUDE.md 宣告）

- ✅ hook 計數一致：CLAUDE.md 宣告 = 實測 = 51 auto + 13 manual + 3 pre-push。

## 2. Hook ↔ dev-rule 覆蓋缺口

- 🕳️ dev-rule 「3. 三態：Custom / Default（省略）/ Disable」 body 未提及任何 hook / lint — 可能 reviewer-only，對照 hook-vs-skill-coverage.md 確認。
- 👁️ dev-rule 「5. SAST：7 條安全 review 準則」 顯式標為 reviewer convention （無機械防線）— 確認 hook-vs-skill-coverage.md §7 漏接已收錄。
- 👁️ dev-rule 「6. 推銷語言不進 repo」 顯式標為 reviewer convention （無機械防線）— 確認 hook-vs-skill-coverage.md §7 漏接已收錄。
- 🕳️ dev-rule 「7. 版號治理：五線 tag」 body 未提及任何 hook / lint — 可能 reviewer-only，對照 hook-vs-skill-coverage.md 確認。
- 🕳️ dev-rule 「8. Sentinel Alert 模式」 body 未提及任何 hook / lint — 可能 reviewer-only，對照 hook-vs-skill-coverage.md 確認。

## 3. 重複候選（相似度 ≥ 0.60）

- ✅ 無相似度 ≥ 0.60 的重複候選。

## 4. Feedback cross-ref / orphan

- ✅ 所有 feedback 卡均在 index 中、無 broken ref。

## 5. Stale feedback（> 120 天未更新）

- ✅ 無超過 120 天未更新的 feedback 卡。

---

_重新產生：`make audit-rules`。本 report 為時間點快照，不代表 live state。_
