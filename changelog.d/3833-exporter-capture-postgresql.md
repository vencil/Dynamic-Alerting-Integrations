---
section: Added
topic: rule-packs
issues: [3833]
created: 2026-11-28T10:55:00+00:00
---
- **PostgreSQL pack 讀的原始指標改由真實 exporter 核對（rule-packs、ci；[#3833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3833)）**：threshold registry 的 pack 可以帶 `exporters:`——exporter 映像（tag＋digest）、啟動旗標、對象資料庫映像與要擷取的狀態。CI 的 `Exporter Capture (PostgreSQL)` 每支 PR 都照這份登記啟動 postgres_exporter 與一組 PostgreSQL primary／standby，抓 `/metrics`，pack 規則讀的每個原始選擇器（含 label matcher）都要選得到至少一條序列，否則失敗並指出規則、選擇器與缺的是什麼。pack 的 `exporter:` 文字由 `exporters:` 推導，檔頭生成區塊列出版本與旗標。其餘九個第三方 exporter 的 pack 還沒有這項核對，逐一列在 `check_exporter_capture.py` 的未驗證清單；新增 pack 時兩者都沒有會被擋下。本次為試行，只做 PostgreSQL。
