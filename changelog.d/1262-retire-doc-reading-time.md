---
section: Removed
topic: dx
issues: [1262]
created: 2026-09-27T05:59:22+00:00
---
- **退役文件閱讀時間檢查（lint；[#1262](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1262)）**：刪除 `scripts/tools/lint/check_doc_reading_time.py` 與 manual-stage hook `check-doc-reading-time`。「文件需要拆分」是無法用字數判定的模糊語義，這個 hook 在現有文件上永遠回 1、也沒有任何閘門在跑它。`hook-vs-skill-coverage.md` 與 `github-release-playbook.md` 的對應引用一併移除；release 的文件簡潔性檢查仍是人工項目。
