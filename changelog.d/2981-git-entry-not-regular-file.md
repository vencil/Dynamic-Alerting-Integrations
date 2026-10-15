---
section: Changed
topic: tenant-api
issues: [2981, 2903]
created: 2026-10-15T13:58:02+00:00
---
- **⚠️ breaking：產生器容器從 git 物件取檔時，symlink 與 gitlink 一律判 `not_regular_file`（ADR-036 §3；[#2981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2981)）**：conf.d 裡 mode 不是 100644／100755 的項目（symlink 120000、gitlink 160000 等；路徑有 `.` 開頭的除外）不再照連結文字判，改報會擋下寫入的 `tenant_file_unreadable`，原因寫明 `not_regular_file` 與 git mode。repo 裡把租戶檔做成 symlink 的部署，經 tenant-api 的寫入會被拒（客戶 CI 從檔案系統讀，照舊跟隨 symlink）：把 symlink 換成內容相同的一般檔。tenant-api 寫某個路徑時本來就寫成一般檔，那筆寫入不受影響。
