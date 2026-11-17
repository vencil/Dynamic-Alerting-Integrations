---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-17T18:00:00+00:00
---
- **`generate-routes -o` 改為整份取代、權限 `0600`；`-o -` 與非一般檔改為結束碼 2（da-tools；[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：同目錄暫存檔改名蓋上，寫入失敗時舊檔完整保留。不帶 `-o` 仍印到 stdout。⚠️ 行為變更：(1) `-o -` 解析時 `0` → `2`（要名為 `-` 的檔請給 `./-`）；(2) `/dev/stdout`、`/dev/null` 等裝置、FIFO、正是本次 stdin／stdout／stderr 重導的檔，執行前 `0` → `2`，舊檔不動；(3) 無法取代的檔 `0` → `2`、舊檔不動：非 root 而目錄不可寫、單檔 bind mount（`docker run -v 檔:檔`、`subPath`，請改掛目錄）、空間不足而原地寫得下；(4) 非 root、一般目錄裡別人的可寫檔 `2` → `0`，改屬執行者；(5) hard link 的另一個名字保留舊內容；既有檔換新 inode，xattr 不保留。
