---
section: Fixed
topic: confd-family
issues: [1522]
created: 2026-09-30T13:09:50+00:00
---
- **`diagnose` 的 profile 查詢改由繼承鏈的同一次讀取提供（[#1522](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1522)）**：`lookup_tenant_profile` 過去自己再讀一次設定目錄，遇到讀不進去的檔時不印任何訊息就略過；單獨呼叫它的程式因此拿到 profile 名稱，卻不知道有檔案沒被讀到。現在它直接取 `resolve_inheritance_chain` 的 `profile_name`，讀不進去的檔會和繼承鏈一樣在 stderr 印出 `WARN: skip`。`diagnose` 本身只讀一次目錄，同一個壞檔只警告一次，輸出的 `profile` 與 `inheritance_chain` 不變。`lookup_tenant_profile` 已沒有 CLI 呼叫端，保留作為給 import 使用的 API。另外一併修正：`defaults:` 不是 mapping、`optional_overrides:` 不是 list（或含 mapping、list 元素），或某個 profile 的內容不是 mapping 時，`diagnose` 過去會崩潰或靜默照讀；現在與 exporter 相同，整份檔略過，並印出 `WARN: skip` 且記入 `skipped_unusable_files`。
