---
section: Changed
topic: ci
issues: [3180]
created: 2026-11-03T16:10:07+00:00
---
- **threshold-exporter 的 PR CI 拆成 race 與完整兩條（ci；[#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180)）**：原本一條 `-race -count=1` 同時負責抓併發問題與跑完整窮舉矩陣，其中 `cmd/da-guard` 的兩支矩陣是確定性邏輯，在 race detector 下跑完整版只多花時間、不多抓 race。現在 race 那條帶 `-short`；新增 `go-tests-threshold-exporter-full`，整個 module 不帶 `-race`、不帶 `-short`，完整矩陣與覆蓋率報告（artifact 名稱不變）都改由它產出。兩條都是必要 check `Go Tests (1.26)` 的 leg，path gate 相同；守衛測試釘住兩邊的旗標、套件範圍，以及完整那條在 gate 的 `needs:` 裡。讓矩陣本身變便宜的程式設計改由 [#3544](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3544) 追蹤。
