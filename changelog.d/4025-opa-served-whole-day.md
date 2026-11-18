---
section: Changed
topic: tools
issues: [4025]
created: 2026-11-18T22:20:33+00:00
---
- **⚠️ BREAKING：opa-evaluate 的 `input.served` 改為整個 UTC 日，每個鍵一律是 segment 清單，同一棵樹的政策判決不再隨執行時刻變（tools；[#4025](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4025)）**：先前 `input.served.<租戶>.<鍵>` 是執行那一刻的一個數字，排程停用的時段裡鍵會消失；某時段 `/metrics` 收集不了時只在那段時間 rc 2。現在每個鍵是 `[{from, to, value, severity}]`（值整天不變時一段；沒有列的段 `value: null` 帶 `reason`），rego 要逐段判、先排除 `null`（例：`some seg in input.served[t].k; seg.value != null; seg.value > 90`）。新的 `--at`（預設現在）只用來判斷 `expires:`，尚未到期的限時鍵在 stderr 印 `NOTE`；一天中有收集不了的時段時不論幾點都 rc 2、點名時段。`input.tenants` 的 `_metadata` 改取 served-values 的 facts。
