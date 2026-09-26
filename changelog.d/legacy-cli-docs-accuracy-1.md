---
section: Fixed
topic: cli-docs-accuracy
issues: [1321, 1351, 1380, 1381, 1423, 1447, 1448, 1495, 1556, 2090]
created: 2026-09-26T17:00:00+00:00
---
- **文件與 portal 教的 da-tools 指令照抄就能跑（docs、portal）**：清掉一批會被 argparse 以 rc 2 拒絕、或安靜走錯路徑的教學指令——不存在的 `onboard --analyze`、`validate-config --ci`、`operator-generate --split`／`--apply`、`diagnose --tenant`、`lint --strict`（改教 `--ci`）等改為真實旗標，CLI Playground 產生的命令同步修正（[#1381](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1381)、[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）。給客戶的 `docker run` 範例依掛載意圖補 `--user $(id -u):$(id -g)` 或標 `:ro`，含 CI/CD 精靈產出的那條——出貨映像以非 root uid 執行，缺它第一次寫檔就 `PermissionError`（[#1495](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1495)）。工具印給人照跑的建議指令不再指向 repo 內部路徑，並區分需不需要叢集存取；`da-tools profile build` 標明為 library-only、CLI 尚未出貨。
