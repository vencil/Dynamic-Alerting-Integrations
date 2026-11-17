---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-17T18:00:00+00:00
---
- **`generate-routes -o` 改為整份取代、權限 `0600`；`-o -` 與非一般檔改為結束碼 2（da-tools；[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：輸出檔以同目錄暫存檔改名蓋上，寫到一半失敗時舊檔完整保留（以前留前半段）。⚠️ 行為變更：(1) `-o -` 在解析參數時 `0` → `2`（以前寫出名為 `-` 的檔；要那個檔請給 `./-`）；(2) `-o /dev/stdout`、`/dev/null` 等裝置與 FIFO 在執行前 `0` → `2`（沒有 reader 的 FIFO 以前卡住）；要印到 stdout 就不帶 `-o`；(3) `-o` 正是本次 stdout 重導的檔時 `0` → `2`，舊檔不動；(4) 非 root、目錄不可寫而檔可寫時 `0` → `2`；(5) 非 root、一般目錄裡別人的可寫檔 `2` → `0`，檔案改屬執行者；sticky 目錄裡仍是 `2`，舊檔不動；(6) hard link 的另一個名字保留舊內容；(7) 既有檔一律換新 inode。不帶 `-o` 仍印到 stdout。
