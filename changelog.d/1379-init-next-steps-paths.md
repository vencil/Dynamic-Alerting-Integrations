---
section: Fixed
topic: cli-docs-accuracy
issues: [1379]
created: 2026-09-26T22:50:00+00:00
---
- **`init` 結尾「下一步」印的路徑改以執行 init 的目錄為基準（init；[#1379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1379)）**：用 `-o <子目錄>` 安裝時，原本印的 `da-tools validate-config --config-dir conf.d/` 在 repo 根目錄照抄會 `config-dir not found`（rc=2），「編輯 conf.d/<租戶>.yaml」會開出一個新的空檔。現在 Validate 提示、要編輯的檔案、pre-commit 片段檔與還原用的 `git checkout` 路徑都帶上子目錄前綴；不帶 `-o` 時拼法不變。
