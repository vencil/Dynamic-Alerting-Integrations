---
section: Changed
topic: ci
issues: [3541, 3180]
created: 2026-11-03T16:26:33+00:00
---
- **Nightly race 依上一晚各 package 耗時分片（ci；[#3541](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3541)）**：每個 module 不再擠在單一 runner 上，而是由新的 `plan` job 依上一晚的 `race-timings-<module>` 拆成至多 4 片平行跑（旗標仍是 `-race -count=10 -short -timeout=30m`），再由 `merge` job 把同一 module 的各片合併、照舊只解析一次，fragment 與追蹤 issue 的 fingerprint 不變。⚠️ 原始串流 artifact 改名為 `race-raw-<module>-<shard>`。任一片沒留下完整證據時，該 module 判為 `no-result`，不會以其餘幾片判綠。module 清單改由 `scripts/ops/plan_race_shards.py` 單一維護；race 腿另加 Go build cache。nightly 的 `-short` 定為永久：nightly 負責重複壓力、完整覆蓋由 PR CI 的 `-race` 負責。取代 [#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180)。
