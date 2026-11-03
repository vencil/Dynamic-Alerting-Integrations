---
section: Changed
topic: ci
issues: [2977, 3180]
created: 2026-10-18T11:47:32+00:00
---
- **Nightly race detector 改帶 `-short`（ci；[#2977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2977)）**：threshold-exporter 的 `cmd/da-guard` 窮舉矩陣測試在 nightly 的 `-race -count=10` 下撞上 30 分鐘的 package 逾時，被誤報成重複執行間的隔離失敗。nightly 現在讓這類矩陣跑縮小版，`-count=10` 仍負責抓重複執行間的問題；完整矩陣由 PR CI 不帶 `-short` 的 threshold-exporter 完整測試 job 跑（[#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180) 起與 race 那條分開）：每支觸發 threshold-exporter Go 測試 job 的 PR（`go_changed` 或 `docs_changed` 的 path gate）跑一次，兩邊都有守衛測試釘住。
