---
section: Changed
topic: ci
issues: [3541, 3180]
created: 2026-11-03T16:26:33+00:00
---
- **Nightly race 依上一晚各 package 耗時分片（ci；[#3541](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3541)）**：新的 `plan` job 依上一晚的 `race-timings-<module>` 把每個 module 拆成至多 4 片平行跑（旗標仍是 `-race -count=10 -short -timeout=30m`），`merge` job 把同一 module 的各片合併後照舊只解析一次，既有追蹤 issue 的 fingerprint 不變。⚠️ 原始串流 artifact 改名為 `race-raw-<module>-<shard>`。任一片沒留下完整證據時該 module 判 `no-result`，不以其餘幾片判綠。package 耗時超過 `-timeout` 一半者另開 `slow-package` 追蹤 issue：屬警告，module 仍判綠（一個 package 是一個 test binary，分片拆不開）。module 清單由 `scripts/ops/plan_race_shards.py` 單一維護；race 腿唯讀 PR CI 該 module 的 Go cache（不另存；PR CI 的兩個 Go job 改為各存 `<os>-go-<module>-<hash>`）。nightly 的 `-short` 定為永久：nightly 負責重複壓力、完整覆蓋由 PR CI 的 `-race` 負責。取代 [#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180)。
