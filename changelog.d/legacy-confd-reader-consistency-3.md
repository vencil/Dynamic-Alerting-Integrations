---
section: Fixed
topic: confd-reader-consistency
issues: [1339, 1577, 1634, 1652, 1677, 1911, 1957, 1980, 1982, 2049, 2054, 2086]
created: 2026-09-26T17:00:00+00:00
---
- **各平面對租戶的判定一致，重複宣告改為大聲失敗（exporter、tenant-api、da-guard、tools；[#1957](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1957)、[#1677](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1677)、[#1577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1577)）**：`/metrics`、`/effective`、da-guard、tenant-api 改用同一次 conf.d 走訪與完整解析，被拒收檔案裡的租戶在各面都不存在（除非另一個可解析的檔也宣告它；tenant-api 的列表與明細仍列出該檔，帶 `config_error`）；symlink 的 `--config-dir` 可解析，只寫 `t1:` 的空 body 租戶不再 500。⚠️ `--scope` 指到 conf.d 外時 da-guard 由 exit 0 改為 caller error；讀不到的 `_defaults.yaml` 改為略過。`validate-config` 新增 `tenant_uniqueness` 檢查，抓出會讓 exporter 拒載整棵樹的跨檔重複租戶；平面讀取的檢查列遇到階層樹由 PASS 降為 WARN（結束碼仍為 0）。⚠️ `describe_tenant` 遇到重複租戶改為 exit 1（`--all` 亦然，`blast-radius` workflow 會因此失敗），也不再描述隱藏路徑裡的租戶。`backtest_threshold --git-diff` 不再漏掉非 ASCII（合法 UTF-8）檔名的載體，非 UTF-8 檔名改為具名略過。
