---
section: Fixed
topic: exporter
issues: [2122]
created: 2026-09-27T03:58:24+00:00
---
- **兩次 reload 交錯不再讓服務內容倒退回較舊的設定（exporter；[#2122](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2122)）**：階層式 conf.d 的一次 reload 分兩段寫入（先階層資料、再服務用的設定）。前一次 reload 還沒寫完時若檔案又變動，第二次 reload 可能插進這兩段之間，結果 exporter 繼續輸出較舊的閾值，而變更偵測以為已是最新、之後也不會再自動修正，只能重啟 pod 恢復。現在同一個 exporter 的 reload 一次只跑一個，後到的會等前一個完成再以磁碟上的最新內容重跑；scrape 與 `/effective` 不受影響。
