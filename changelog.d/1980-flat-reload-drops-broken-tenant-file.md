---
section: Changed
topic: exporter
issues: [1980, 2022]
created: 2026-10-09T19:03:37+00:00
---
- **扁平樹熱重載不再保留壞掉租戶檔的「最後正確值」（exporter；[#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980)、[#2022](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2022)）**：conf.d 根目錄沒有 `_defaults.yaml` 時，租戶檔改成語法錯或租戶 body 型別錯，先前熱重載會繼續以舊值服務該檔的租戶，重啟後才消失；壞檔之後被刪除時租戶仍繼續服務（#2022）。現在與階層式 reload、全量載入、重啟一致：該檔宣告的租戶一律從 `/metrics` 移除。訊號不變：每次掃描印 `WARN: skip unparseable file`、累加 `da_config_parse_failure_total{file_basename}`（告警 `ConfigParseFailure`），`/api/v1/config/identity` 的 `parse_failed` 列出該檔。⚠️ 非原子寫入（編輯器或同步工具先寫出半個檔）時，只要某次掃描讀到半個檔，該檔的租戶就會從 `/metrics` 消失，最長到下一次 reload（`--reload-interval`），告警可能先 resolve 再觸發；請改用寫到暫存檔再 rename 的原子寫入。
