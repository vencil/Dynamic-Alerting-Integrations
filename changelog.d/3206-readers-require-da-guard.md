---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T04:20:00+00:00
---
- **⚠️ Breaking：`config-history snapshot`、`drift-detect`、soak harness（`run_chaos_soak.py`／`make soak-readiness`）與 `migrate-to-operator --source-dir` 改從 da-guard 取檔，需要 da-guard（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：讀哪些檔取自 `da-guard parse-tree` 對該目錄的索引，不再自己列目錄；找不到 da-guard（`$DA_GUARD_BINARY` → `$PATH`；da-tools 映像已內建）結束碼 2。目錄上有 da-guard 驗證會點名的路徑（語法錯、多份文件、懸空或迴圈 symlink、FIFO、列不出內容的目錄、名稱像設定檔的目錄）時，前三者與其他 conf.d 工具同樣在讀寫前拒絕、逐條點名、結束碼 2；exporter 會丟掉一部分的樹也算（例如 `_defaults.yaml` 的 `defaults:` 帶字串值，[#1414](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1414)），以前 `drift-detect` 照樣比對。soak harness 在開跑前就拒絕，且只在開跑時取一次索引。`--source-dir` 不是 conf.d，只取 da-guard 只列檔模式的清單，解析不了的檔照舊 WARNING 並略過；不讀的頂層項目（以 `.` 開頭的隱藏檔、懸空 symlink、FIFO 等，以前會讀隱藏檔）由一行 WARNING 點名。`make soak-readiness` 未設 `DA_GUARD_BINARY` 時先建 `.build/da-guard`。
