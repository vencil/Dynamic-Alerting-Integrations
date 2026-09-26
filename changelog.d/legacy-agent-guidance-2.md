---
section: Added
topic: agent-guidance
issues: [824, 1627, 1719]
created: 2026-09-26T17:00:00+00:00
---
- **remote session 的閘門可自我補齊並可觀測，另補兩道治理 gate（internal、dx；[#1719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1719)、[#1627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1627)）**：新增 SessionStart hook，在全新 clone 上補齊 `pre-commit`、`pytest`、tag 與 e2e 依賴並寫下落地痕跡，讓「hook 沒被載入」可被察覺；另加 skill 使用帳本、依路徑注入指引與 Stop 證據檢查三支 session hook。新增 TRK 索引覆蓋守衛，以及公開 repo 的 engagement 去識別化 gate。
