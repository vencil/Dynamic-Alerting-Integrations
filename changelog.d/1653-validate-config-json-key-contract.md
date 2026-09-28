---
section: Fixed
topic: da-tools
issues: [1653]
created: 2026-09-28T08:05:43+00:00
---
- **`validate-config --json` 的列鍵契約寫成規格並上守衛；`--config-dir` 不存在時也輸出 JSON（da-tools；[#1653](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1653)）**：`cli-reference` 的 `validate-config` 段新增 JSON 輸出表——必有鍵 `check` / `status` / `details` / `caller_error`，可選鍵 `unusable_files` / `skipped_unusable_files` / `skipped_nested_files` / `suggested_action` / `docs_link` 各附出現條件（條件不成立時鍵不存在）。`--config-dir` 不是目錄時，`--json` 原本 stdout 是空的，現在輸出同形狀的單列文件（`check: "config_dir"`、`caller_error: true`），結束碼仍為 `2`。
