---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-20T14:42:07+00:00
---
- **⚠️ Breaking：`analyze-gaps --tenant-config` 改經 `da-guard parse-tree` 讀那一個檔（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：仍只讀檔案寫了什麼、不繼承，`tenants:` 包裝、以檔名當租戶 id、頂層不是 mapping 當成沒有鍵都不變；值改用 exporter 的 yaml.v3 讀法，未加引號的 `yes` 在 `--json` 的 `current_value` 是 `"yes"`（原本是 `true`）；數字同理（`0o17` 是 15、`1e3` 是 1000.0、`1:30` 是字串），`null` 鍵、`!!binary` 等非白名單 tag、自我參照 anchor 改為結束碼 2。日期值在 `--json`／`-o` 輸出原文，原本是 crash、結束碼 2。從此需要 da-guard（`$DA_GUARD_BINARY`，否則 `$PATH`；映像已內建），沒有或執行失敗時結束碼 2。重複鍵、多份文件、語法錯維持結束碼 2 並指名該檔，原因改用 parse-tree 的說法。文字報告與 `--json` 都依租戶 id、鍵名排序，不再照檔案裡的順序。
