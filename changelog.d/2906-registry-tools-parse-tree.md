---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-20T12:00:00+00:00
---
- **⚠️ Breaking：`check_account_registry_monotonic` 改經 `da-guard parse-tree` 讀 `_account_registry.yaml`，`check_threshold_registry --regen` 寫兩份 conf.d `_defaults.yaml` 前先經 parse-tree 確認（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：registry 的 `next_account_id` 照 tenant-api 的 yaml.v3 讀法判讀（`0o2000` 是 1024，不再當成讀不了而 fail-closed）；重複鍵這類 yaml.v3 讀不了的檔、`uint32` 範圍外的值 fail-closed（結束碼 1）；兩個 revision 都有該檔而沒有 da-guard 時結束碼 2，檔不存在時照舊略過、不需要 da-guard。`--regen` 改成先算好 registry 文字與所有生成面，conf.d 檔讀回須等於「原檔讀數換上這次生成的 `defaults`／`optional_overrides` 項目」；原檔讀不了、讀回不符或改到 conf.d 而沒有 da-guard 時，registry 與生成面一個檔都不寫、結束碼 2（過去先寫 registry、再逐檔寫生成面，只檢查 PyYAML 能否解析）；檢查模式不變。
