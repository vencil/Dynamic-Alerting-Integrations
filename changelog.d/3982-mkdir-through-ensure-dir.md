---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T00:30:00+00:00
---
- **三支工具的目錄旗標給 `-` 改為結束碼 2（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`baseline -o`、`generate_tenant_fixture.py -o` 與 `assemble_config_dir.py --output` 在解析參數時拒收 `-`，argparse 點名旗標；以前會建出名為 `-` 的目錄並寫入，`baseline --dry-run` 與 `assemble_config_dir.py --check` 以前是結束碼 `0`，現在同樣是 `2`。要那個名字請給 `./-`。這三支連同 `da_assembler --config-dir`、`state-reconcile --manifest-path` 建輸出目錄改經共用的目錄入口，錯誤訊息與結束碼不變；空值（`-o ""`）照舊是結束碼 `2`、什麼都不寫。⚠️ 目前目錄已被刪除時，`generate_tenant_fixture.py` 與 `baseline` 由 traceback 改為一行 `cannot create directory`（結束碼仍是 `2`）。
