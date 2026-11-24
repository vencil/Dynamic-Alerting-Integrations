---
section: Changed
topic: tools
issues: [4275]
created: 2026-11-24T23:28:34+00:00
---
- **`lint`：租戶規則檔的 owner 與 expiry 改為必填的 annotation（tools；[#4275](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4275)）**：⚠️ breaking。`<掃描目錄>/<租戶>/` 下每條 alert 都要有 `owner` 與 `expiry` 兩個 annotation，`expiry` 是存在的日期 `YYYY-MM-DD`；缺、空值、寫在 labels 都是 ERROR（以前只在缺 label 時出 WARN）。改放 annotation 是因為 label 是告警身分的一部分：續期或換 owner 會讓正在響的告警先 resolve 再重發。lint 不拿 expiry 跟今天比，同一個 commit 哪天跑結果都一樣。rule pack 與不在租戶目錄下的檔不檢查。遷移：把兩個 label 搬到 `annotations:`。
