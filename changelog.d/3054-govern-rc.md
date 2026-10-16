---
section: Changed
topic: tools
issues: [3054]
created: 2026-10-16T08:21:15+00:00
---
- **`threshold-govern --apply` 開出過 PR 也不再直接算成功（tools；[#3054](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3054)）**：結束碼改看失敗（`error` 加上斷路器略過）占「有送出、或本來要送出寫入」的租戶是否 ≥ 90%，達到就回 `1`。⚠️ 結束碼變化：有開出 PR、但其餘 ≥ 90% 失敗的一輪，從 `0` 變成 `1`；其他情況不變。`--max-prs` 額滿而略過的租戶沒有送出，不算失敗也不計入；斷路器略過的租戶在 `--json` 的 `outcomes[].status` 改為 `skipped_breaker`（先前與額滿略過同為 `skipped`），`--json` 的 `summary` 新增 `skipped`（額滿）與 `skipped_breaker` 計數，文字 Summary 也分列兩者。同一份計畫下失敗只會讓結束碼變高；與計畫無關的寫入健康訊號請看 tenant-api 的 `tenant_api_writes_total{result}` 與 `TenantApiWriteErrorsHigh`。
