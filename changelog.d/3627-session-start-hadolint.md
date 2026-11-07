---
section: Changed
topic: ci
issues: [3627]
created: 2026-11-07T07:00:00+00:00
---
- **remote session 起手式改為安裝釘版的 hadolint（dx；[#3627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3627)）**：`.claude/hooks/session-start.sh` 下載 `hadolint-Linux-x86_64`，驗 sha256 後安裝。版本與 checksum 在執行時從 `scripts/tools/lint/check_iac_vibe_rules.py` 的 `HADOLINT_VERSION`／`HADOLINT_SHA256` 讀取（checksum 新增於此作為 SSOT；ci.yml 安裝步驟那份由 parity 測試綁成同值），腳本裡不另寫一份；讀不到或格式不對時不下載，marker 寫 `RESULT=failed (hadolint: pin unreadable …)`。先前 remote session 沒有 hadolint 也沒有 docker daemon，`iac-sast-check` 一律擋下，`make pr-preflight` 寫不出 marker、無法推送。`test_toolchain_pin_parity.py` 新增 ci.yml checksum 與 wrapper 的對帳，並釘住 session-start 只從 wrapper 讀。
