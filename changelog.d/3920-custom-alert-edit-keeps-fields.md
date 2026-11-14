---
section: Fixed
topic: portal
issues: [3920]
created: 2026-11-14T16:47:21+00:00
---
- **tenant-manager 編輯既有 custom alert 後，表單沒有欄位可填的設定不再被刪掉（portal recipe builder；[#3920](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3920)）**：先前在「自訂告警」編輯一筆帶 `selectors`、`selectors_re` 或 `group_by` 的 recipe 並儲存，這些欄位會從租戶檔消失，告警比對的範圍因此變寬，畫面上沒有任何提示。現在送出時會把表單不管的欄位照 tenant-api 回給頁面的值帶回；表單管的欄位仍以表單為準，在表單裡清空的不會被補回來。原紀錄沒寫、交給 exporter 預設值的 `op`／`mode`／`for`／`quantile`，沒改動就不再寫回檔案。編輯時換了 recipe 種類，recipe contract 列出的欄位只帶回新種類接受的（例如改成 `absence` 時不帶 `group_by`），contract 沒列的欄位照帶。新增 recipe 的輸出不變。編輯時的「會不會觸發」預覽判的也是實際存檔的那一筆（含這些欄位）；帶 `selectors_re` 的 recipe 預覽會如實回報不支援。⚠️ 在這之前用表單編輯過的 recipe 不會自動復原，請對照 git 歷史把被刪掉的 `selectors`／`selectors_re`／`group_by` 補回租戶檔。
