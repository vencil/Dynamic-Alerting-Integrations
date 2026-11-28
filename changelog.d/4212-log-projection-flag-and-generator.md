---
section: Added
topic: tenant-api
issues: [4212]
created: 2026-11-28T12:00:00+00:00
---
- **租戶日誌投影的選擇加入旗標 `_log_projection` 與產生器 `tenant-api log-projection`（ADR-021；[#4212](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4212)）**：conf.d 新增保留鍵 `_log_projection`（`enable`／`disable`，預設 `disable`；租戶檔、根目錄平台檔的 `tenants:` 與租戶引用的 profile 都讀，租戶檔優先；其他寫法不投影）。`tenant-api log-projection render|run` 把帳本有 AccountID **且**旗標開著的租戶，填進 `helm/vector` 產生的樣板，寫成 Vector 的租戶投影設定：帳本與可見度紀錄用 tenant-api 自己的解析函式讀，conf.d 用 exporter 的載入器讀，產生器只填資料、不寫 VRL。`run` 讀不到輸入時保留上一份；寫過的租戶 → AccountID 記在設定檔旁，號碼被改或被別的租戶拿去時整次拒收；寫檔後確認 Vector 的重新載入計數有增加，並輸出 `da_log_projection_*` metrics。本版 `helm/vector` 還沒有使用它，現有的手抄 `tenantProjections` 與投影閘門照舊；改由產生器提供設定是下一步（breaking）。
