---
section: Fixed
topic: cli-docs-accuracy
issues: [2041, 1619, 1379]
created: 2026-09-27T15:11:46+00:00
---
- **速查表與 da-tools README 的旗標欄改成工具真的旗標（docs；[#2041](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2041)）**：命令表格的「常用 Flag」／「最小參數」欄有 34 處列了不存在的旗標，照抄會被 argparse 拒絕（rc=2）。例如 `patch-config` 的 `--namespace`／`--configmap`／`--dry-run` 改為 `--diff`／`--json`／`--exporter-namespace`；`lint` 的 `--strict`／`--json-output` 改為 `--policy`／`--ci`（[#1619](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1619)）；`drift-detect` 的 `--clusters` 改為 `--dirs`；`alert-quality`／`cardinality-forecast` 不存在的 `--all` 拿掉，改寫成「不帶 `--tenant` 即全部租戶」；`config-history` 的 `--config-dir` 移到子命令 `snapshot` 前面；`gitops-check` 改寫成 `local --dir`／`repo --url`／`sidecar` 三個子命令。CLI 契約閘門的帳本因此歸零。
