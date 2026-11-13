---
section: Fixed
topic: ci
issues: [3643]
created: 2026-11-13T16:10:09+00:00
---
- **⚠️ Breaking：Container SAST 第 1 層的 hadolint 等級改由登記表決定，`.hadolint.yaml` 移除（`check_iac_vibe_rules`；[#3643](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3643)）**：hadolint 遇到解析不了的設定檔會丟掉整份設定、以預設等級跑完並回 0，原本 BLOCK 的發現因此降成 WARN，工具照樣印 OK。現在等級與受信任 registry 只登記在 `check_iac_vibe_rules.py` 的 `HADOLINT_RULES`／`HADOLINT_TRUSTED_REGISTRIES`，由 wrapper 轉成命令列旗標；每次執行另從 stdin 送一份金絲雀 Dockerfile，登記的規則沒有以登記的等級回來就是沒有結論，exit 2（原本是 0）。`HADOLINT_*` 環境變數與 Dockerfile 裡的 `# hadolint ignore=` 不再有作用，合法例外改走 PR body 的 `bypass-lint: iac-vibe-rules`。直接執行 `hadolint <Dockerfile>` 會套用 hadolint 的預設等級；手動檢查請執行 `python3 scripts/tools/lint/check_iac_vibe_rules.py`。
