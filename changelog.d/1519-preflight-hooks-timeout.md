---
section: Fixed
topic: dev-workflow
issues: [1519]
created: 2026-09-27T20:10:00+08:00
---
- **完整版 `make pr-preflight` 在較慢的機器上不再必定逾時（internal、dx；[#1519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1519)）**：`Local hooks` 那一項跑 `pre-commit run --all-files` 的上限從 300 秒拉高到 1800 秒。以前在跑完整套 hook 超過 5 分鐘的機器上，這一項一定逾時、判 FAIL，完整版結構上過不了。逾時仍判 FAIL，不會被當成「沒問題」；`make pr-preflight-quick` 不受影響。
