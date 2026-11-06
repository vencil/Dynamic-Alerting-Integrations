---
section: Fixed
topic: ci
issues: [3439]
created: 2026-11-05T14:05:03+00:00
---
- **Container SAST 第 1 層：hadolint 沒跑成時不再回報通過（`check_iac_vibe_rules`；[#3439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3439)）**：hadolint 改以 `--no-fail` 執行，結束碼非 0、stdout 為空，或輸出不是 finding 陣列，一律視為引擎沒跑，exit 2（呼叫端錯誤：沒有結論），並印出 hadolint／docker 的 stderr。先前有 docker CLI、沒有 daemon 的環境（例如雲端 session）印 OK、rc 0，pre-commit 顯示 Passed。
