---
section: Fixed
topic: dx
issues: [2240]
created: 2026-09-28T01:39:25+00:00
---
- **`check_bat_ascii_purity --full-scan` 與 pytest 的 `.bat` 檢查改為涵蓋 `scripts/ops/` 的子目錄（lint；[#2240](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2240)）**：diff 模式與 pre-commit（CI 走這條）本來就會遞迴，只有 `--full-scan` 與 `tests/dx/test_bat_label_integrity.py` 只看第一層，子目錄裡含非 ASCII 的 `.bat` 會被手動稽核判成沒問題。現在四個入口範圍一致；`scripts/opsx/` 這類相鄰目錄仍不在範圍內。
