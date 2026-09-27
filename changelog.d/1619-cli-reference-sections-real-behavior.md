---
section: Fixed
topic: cli-docs-accuracy
issues: [1619, 1513, 1818]
created: 2026-09-27T10:05:00+00:00
---
- **CLI 參考的 diagnose／cutover／maintenance-scheduler／rule-pack-split／offboard／lint／analyze-gaps／config-diff 八節改成工具真的行為（docs；[#1619](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1619)、[#1513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1513)、[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：選項表拿掉工具沒有的旗標並補上真的旗標；`cutover` 的 `--readiness-json` 改為必填、步驟與結束碼照程式寫；`maintenance-scheduler` 改寫為透過 `--alertmanager` 直接建立 silence、cron 以 UTC 解讀；`diagnose` 與 `analyze-gaps` 的輸出範例改成實跑結果，結束碼表寫明目前的行為；`rule-pack-split` 的輸出目錄改成實際的 `edge-rules/`／`central-rules/`；`offboard` 註明不備份、不清規則；`lint` 的檢查項目改成內建政策真的檢查的內容；FAQ 的 cutover 步驟改寫。閘門探針不再依賴這些已修掉的實例，改以真的 parser 判合成頁面。
