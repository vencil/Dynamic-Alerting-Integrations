---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T00:30:00+00:00
---
- **三支工具的目錄旗標給 `-` 改為結束碼 2（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`baseline -o`、`generate_tenant_fixture.py -o` 與 `assemble_config_dir.py --output` 在解析參數時拒收 `-`，argparse 點名旗標；以前會建出名為 `-` 的目錄並寫入（`baseline --dry-run` 以前不建目錄、結束碼 `0`，現在同樣是 `2`）。要那個名字請給 `./-`。這三支連同 `da_assembler --config-dir`、`state-reconcile --manifest-path` 建輸出目錄的方式改經共用的目錄入口，錯誤訊息與結束碼不變。
