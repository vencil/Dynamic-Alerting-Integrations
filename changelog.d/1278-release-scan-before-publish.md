---
section: Security
topic: supply-chain
issues: [1278]
created: 2026-09-28T16:05:59+00:00
---
- **release 改為先掃再發布（exporter、da-tools、da-portal、recipe-preview、tenant-api；[#1278](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1278)）**：映像先只推到候選 tag `:candidate-<run_id>-<run_attempt>`，Trivy 掃 build 輸出的那個 digest，通過後才把 `:v<version>`／`:latest` 指到同一個 digest，之後才簽章、產 SBOM、推 Helm chart、建 GitHub Release。掃到可修的 HIGH/CRITICAL 時，正式 tag、chart、簽章與 Release 都不會出現；過去掃描在推送之後才跑，紅燈擋不住已上架的映像。順序由 `tests/ops/test_release_scan_before_publish.py` 守住。
