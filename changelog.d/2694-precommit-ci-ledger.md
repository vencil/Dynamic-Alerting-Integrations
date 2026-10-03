---
section: Added
topic: ci
issues: [2694]
created: 2026-10-03T15:04:50+00:00
---
- **新增的 auto stage pre-commit hook 必須宣告 CI 執行點（ci；[#2694](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2694)）**：#2644 對「沒有在任何 workflow 逐名呼叫」的 hook 做的分類，原本只留在 issue 留言裡。現在寫成帳本 `tests/lint/precommit-ci-ledger.yaml`（53 支：獨立 job 擋 15、pytest 擋 35、不需要 3），並由 `tests/lint/test_precommit_ci_ledger.py` 守住：每支 auto stage hook 要嘛在 workflow 的 `run:` 裡以 `pre-commit run <id>` 逐名呼叫，要嘛登記在帳本；帳本裡的 hook 必須仍存在、仍是 auto stage、沒有同時被逐名呼叫，且所列的 workflow job 與 pytest node id 都解析得到。帳本解析失敗或結構不對一律轉紅。
