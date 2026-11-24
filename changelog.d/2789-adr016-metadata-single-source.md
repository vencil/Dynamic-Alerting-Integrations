---
section: Changed
topic: confd-family
issues: [2789, 4266]
created: 2026-11-24T14:40:02+00:00
---
- **ADR-016 撤回「由目錄路徑推斷 `_metadata` 預設值」，目錄路徑與 `_metadata` 矛盾時 CI 擋下（confd；[ADR-016](docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md)；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：這項推斷從未實作，現在正式撤回：`_metadata` 是 domain／region／environment 的唯一來源，分層之後仍要手填。這三個值是 tenant-api RBAC 範圍的輸入，不從目錄位置推斷。⚠️ `check_path_metadata_consistency.py --ci` 從只警告改為擋下（結束碼 1）：目錄第一段與 `domain` 矛盾，或某一段是已知環境名而與 `environment` 矛盾，pre-commit 的 `path-metadata-consistency-check` 就失敗；不加 `--ci` 時照舊只報告。ADR-016 另記一條已知限制：`configmap-assemble` 是扁平組裝，子目錄的租戶與 `_defaults.yaml` 到不了線上 exporter（[#4266](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4266)）。
