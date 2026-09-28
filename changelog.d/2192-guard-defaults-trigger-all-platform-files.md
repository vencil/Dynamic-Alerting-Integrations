---
section: Fixed
topic: ci
issues: [2192]
created: 2026-09-28T10:00:28+00:00
---
- **Dangling Defaults Guard 改以所有 `_` 開頭的 YAML 觸發並定位 scope（`guard-defaults-impact.yml`；[#2192](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2192)）**：da-guard 會從 conf.d 根目錄的任何 `_` 平台檔（含 `_platform.yaml`、`_rbac.yaml`）讀 `profiles:` 與 `tenants:` overlay，但 workflow 原本只在 `_defaults` / `_profiles` 變動時觸發，scope 也只看 `_defaults`，因此只改 `_platform.yaml` 的 PR 不會觸發，或驗到錯的樹。現在 `paths:` 改為 `**/_*.yaml` / `.yml`（大小寫不拘），改到的 `_` 檔會歸到它所在的 conf.d 樹；決定平台檔的 `flat_build.go` 與 `merge_tenant.go` 也納入觸發。代價：巢狀、不被讀取的 `_` 檔（如 `examples/_*.yaml`）會多跑一次 guard；conf.d 以外的 `_` 檔列為 NOT CHECKED，且此時仍會對偵測到的樹跑一次整棵驗證。
