---
section: Changed
topic: rule-packs
issues: [1320]
created: 2026-11-21T04:00:00+00:00
---
- **DB2 連線數閾值不再標示「待改比值」（rule-packs；[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：`db2_connections_active` 維持要自己填的絕對連線數，可攜性改標為「依部署而定」。原本打算改成佔 `MAXAPPLS` 的比例，但 Db2 的 `MAXAPPLS` 預設是 `AUTOMATIC`，IBM 文件寫明這個設定允許任意數量的連線，實測顯示值 40 的資料庫可以連上 70 條以上——它不是上限，當不了分母。手動固定 `MAXAPPLS` 的部署，把這個 key 填成它的約 85% 即可。告警條件沒有改變；改的是 registry、pack 標頭與 chart 註解的說明，以及 `DB2HighConnectionsCritical` 的 platform_summary 文字（原本寫「超過 MAXAPPLS 新連線會被拒」，在預設設定下不成立）。
