---
section: Changed
topic: exporter
issues: [3832]
created: 2026-11-15T10:18:43+00:00
---
- **⚠️ breaking：平台預設啟用的閾值改由讀取端內建（exporter、da-guard、tenant-api；[#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)）**：threshold registry 標 `arming: default` 的鍵現在是合併鏈最底層，排在根 `_defaults.yaml` 之下。根層沒寫、寫 `null` 或寫了讀不了的值的鍵，一律拿平台值；沒有根層檔的樹也拿到，key verdict 不再回 `undecidable`。升級後每個租戶最多多出這些鍵的列，`merged_hash` 跟著變。根層檔存在但讀不了時不給。整棵樹不要某個鍵：根層 `defaults:` 寫 `<鍵>: disable`（只收這個字，`false`／`off` 報 `root_default_refused` 且照送平台值），每個租戶繼承，整族關閉，給了值的子目錄、profile 或租戶整族回來；根層同時給同族的 `_critical` 或維度值報 `root_disable_family_value`。根層 `optional_overrides:` 列了這種鍵卻沒給數字報 `root_optional_builtin`（結束碼 1）：列名關不掉它。本版內建表摘要 `63343e1d88e9`。
