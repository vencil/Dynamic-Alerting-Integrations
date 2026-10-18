---
section: Changed
topic: ci
issues: [2977, 3180]
created: 2026-10-18T11:47:32+00:00
---
- **Nightly race detector 改帶 `-short`（ci；[#2977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2977)）**：threshold-exporter 的 `cmd/da-guard` 窮舉矩陣測試在 nightly 的 `-race -count=10` 下撞上 30 分鐘的 package 逾時，被誤報成重複執行間的隔離失敗。nightly 現在讓這類矩陣跑縮小版，`-count=10` 仍負責抓重複執行間的問題；完整矩陣照舊由 PR CI（`-race -count=1`，不帶 `-short`）每支 PR 跑一次，兩邊都有守衛測試釘住。讓矩陣本身變便宜另以 [#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180)（`TRK-3180`）追蹤。
