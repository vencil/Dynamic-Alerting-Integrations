---
section: Changed
topic: ci
issues: [3863]
created: 2026-11-22T07:25:52+08:00
---
- **Container SAST 第 1 層的 hadolint WARN 列管表改由工具產生，沒寫理由的新 WARN 會擋（iac-sast-check；[#3863](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3863)）**：原本手寫的列管表已與工具輸出漂移（漏列 `try-local/generator`、3 筆行號過期）。現在每筆 WARN 的 rationale 與退場條件登記在 `scripts/tools/lint/_iac_warn_baseline.py`，以（Dockerfile, 規則碼）為鍵、不含行號；`docs/internal/iac-lint-baseline.md` 的表格由 `check_iac_vibe_rules.py --write-baseline` 產生。⚠️ 政策變化：`--ci`（pre-commit 與 CI `Lint` job）在 WARN 未登記、登記已過期、或表格與輸出不一致時回 1，且 PR body 的 bypass 不適用；已登記的 WARN 照舊不擋 merge。
