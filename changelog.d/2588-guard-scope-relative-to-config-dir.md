---
section: Changed
topic: confd-family
issues: [2588]
created: 2026-10-01T23:30:00+00:00
---
- **⚠️ breaking：`da-guard --scope` 的相對路徑改為相對於 `--config-dir`（exporter；[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)）**：過去相對於工作目錄，與 help 的寫法不符；`da-tools guard defaults-impact` 同樣適用。絕對路徑照舊、解析後在 `--config-dir` 外仍 exit 2。舊寫法 `--config-dir conf.d --scope conf.d/db` 現在指向 `conf.d/conf.d/db`，該目錄不存在時 exit 2（`stat scopeDir`），請改寫成 `--scope db`；`guard-defaults-impact.yml` 與 `guard_defaults_scopes.py` 已改用新寫法。
