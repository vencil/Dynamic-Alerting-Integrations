---
section: Changed
topic: ci
issues: [3258, 3230]
created: 2026-10-22T15:26:12+00:00
---
- **da-tools 映像發版前先實際執行，退役釘舊版映像的 `config-diff.yaml`（ci；[#3258](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3258)）**：`tools/v*` 發版在 Trivy 之後、簽章與 promote 之前，對候選 digest 執行每個子命令的 `--help`，並以映像自己的使用者唯讀掛載兩棵 conf.d 跑 `config-diff`（可讀時須回 1 並報出 `_defaults.yaml` 的變更，讀不到時須回 2）；不過就不發布。`.github/workflows/config-diff.yaml` 刪除：它跑固定版本的已發布映像、平面讀取並跳過 `_` 檔，PR 上的影響報告改看 Config Diff (rendered, advisory)。先前照文件複製那支 workflow 的客戶，改用 `da-tools init --ci github` 產生的 `.github/workflows/dynamic-alerting.yaml`。
