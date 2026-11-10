---
section: Fixed
topic: docs
issues: [1040]
created: 2026-11-07T13:17:50+00:00
---
- **ADR 小節引用改指到現行小節（docs；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §9；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：程式註解、文件、Helm values、rule-pack 與 portal 裡以 `ADR-NNN §…` 引用設計理由的地方，有些指向 ADR 改寫或重新編號後已不存在的小節（例如 ADR-024 的 `§S5`／`§S6b-2`、ADR-023 的 `§A`–`§C`），讀者會被帶到錯的段落或找不到出處。這些引用改為指向現在承載該決策的小節；行為不變。
