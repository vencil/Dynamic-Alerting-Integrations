---
section: Changed
topic: tools
issues: [3977]
created: 2026-11-18T13:22:39+00:00
---
- **`lint --policy` 的 `file_overrides` 寫錯，載入時即結束碼 `2`，不論有沒有檔案匹配（tools；[#3977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3977)）**：⚠️ breaking。`file_overrides` 須為清單；每筆只能有 `path`（必填；不重複，也不能有兩筆可能匹配同一個檔）、`require_generated_marker`（布林）、`policy`（只放 `denied_functions` 非空字串的清單與 `max_range_duration` 正的時長）。先前 entry 的未知鍵與非 mapping 的 entry 被略過不出聲，`policy` 裡不准放寬的鍵只在有檔案匹配時成 finding，`policy` 寫成清單則崩潰，`max_range_duration` 寫錯會讓該檔的範圍檢查失效。訊息點名第幾筆與原因（原因碼 `policy_invalid`）。
