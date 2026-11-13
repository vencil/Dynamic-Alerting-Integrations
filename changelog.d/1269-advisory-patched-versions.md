---
section: Added
topic: release
issues: [1269]
created: 2026-11-13T01:40:00+00:00
---
- **`make pre-tag` 檢查 advisory 的 patched 版號真的存在（`advisory-patched-check`；[#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269)）**：draft 與已發布的 security advisory 逐列檢查。package 名稱的括號要標明版號空間：容器映像、OCI chart（查 ghcr.io 的 tag）、git-only chart（查 Chart.yaml 的歷史版號）或平台 release（查 git tag）。patched 有填就必須在該空間存在；沒填而已有比受影響版本都新的版本，視為忘了回填。不受 `ADVISORY_ACK` 影響；查不到（缺 `gh`、連不上 registry、shallow clone）一律中止。帶修正的那次發版本身擋不住（新版本此時還不存在），漏回填會在下一次 pre-tag 被擋。
