---
section: Fixed
topic: helm
issues: []
created: 2026-09-27T22:25:00+00:00
---
- **chargeback-aggregator 的 CSV 報表改以 UTF-8 明示寫出（helm；chart 0.2.2）**：內嵌在 `helm/chargeback-aggregator/templates/configmap-script.yaml` 的彙整腳本開 CSV 時沒帶 `encoding`，檔案編碼跟著容器 locale 走；現在固定寫成 UTF-8，租戶名稱含非 ASCII 字元時不再依環境而異。
