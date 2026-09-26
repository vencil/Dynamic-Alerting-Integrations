---
section: Fixed
topic: test-infra
issues: [1158]
created: 2026-09-26T17:00:00+00:00
---
- **測試 flake 與環境假紅清理（internal）**：Hypothesis 改用 repo-wide `deadline=None` profile，消除碰 I/O 的 property 測試間歇假紅（`HYPOTHESIS_PROFILE=strict` 可對未顯式設 deadline 的測試還原預設）；契約測試的 schemathesis 與 hypothesis 版本釘在 `tests/contract/requirements.txt`；Windows host 上整套 pytest 無法收集以及數個 encoding／檔名／wall-clock 假紅已修，host 可跑完整套。
