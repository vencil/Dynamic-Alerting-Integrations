---
section: Changed
topic: tenant-api
issues: [2903]
created: 2026-10-15T13:58:02+00:00
---
- **部分更新不得改到沒編輯的內容（ADR-036 §4；[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：批次 patch 與 `PUT .../custom-alerts` 把變更合併進現有租戶檔再整份重新輸出。合併後以 yaml.v3 把原檔與結果都解成值比對，結果必須恰好是原檔套上這次的編輯、定義的 anchor 相同；否則不寫入，回 `TENANT_CONFIG_NOT_LOADABLE`（custom-alerts 409，批次是該筆結果），`config_error` 為 `not_round_trippable`，訊息附第一個差異位置（只寫出本租戶 block 內的路徑）。擋下的例子：block scalar 開頭空行被改、別名共用的租戶 block 連帶被改、`<<` 帶進的值在刪掉明寫鍵後仍在、多份文件。以整檔 `PUT /tenants/{id}` 或在 git 改寫該檔即可。
