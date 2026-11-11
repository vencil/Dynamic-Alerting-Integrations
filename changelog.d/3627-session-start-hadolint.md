---
section: Changed
topic: ci
issues: [3627, 3611]
created: 2026-11-07T07:00:00+00:00
---
- **remote session 起手式改為安裝釘版的 hadolint、kube-linter、pint、promtool（dx；[#3627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3627)、[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)）**：`.claude/hooks/session-start.sh` 下載各自的 release 檔、驗 sha256 後安裝。版本與 checksum 在執行時從擁有它的檔案讀取——hadolint、kube-linter、pint 是各自 lint wrapper 的 `*_VERSION`／`*_SHA256`（checksum 新增於此作為 SSOT），promtool 是 `.devcontainer/install-promtool.sh`——腳本裡不另寫一份；讀不到或格式不對時不下載，marker 寫 `RESULT=failed (<tool>: pin unreadable …)`。`test_toolchain_pin_parity.py` 對帳 ci.yml 的 checksum 與 wrapper，並釘住 session-start 只從 SSOT 讀。CI 的 `lint` 與 `drift-checks` job 改以 `setup-node` 釘 node 24（devcontainer 的 major），不再用 runner 映像浮動的 node。
