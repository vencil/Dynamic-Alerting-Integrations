---
section: Fixed
topic: docs
issues: [1040]
created: 2026-11-07T13:17:50+00:00
---
- **ADR 小節引用改指到現行小節（docs；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §9；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：程式註解、文件、Helm values、rule-pack 與 portal 裡以 `ADR-NNN §…` 引用設計理由的地方，有些指向 ADR 改寫或重新編號後已不存在的小節（例如 ADR-024 的 `§S5`／`§S6b-2`、ADR-023 的 `§A`–`§C`），讀者會被帶到錯的段落或找不到出處。這些引用、以及以 2026-07 草稿編號（`D2-B`、`LD-6 P4b`、`MED-8` 之類）引用 ADR-027 的地方，改為指向現在承載該決策的小節，ADR-027 的舊編號對照表刪除。新的 pre-commit 檢查 `adr-section-refs`（`scripts/tools/lint/check_adr_section_refs.py`）擋下對不到任何小節的 `ADR-NNN §…`，以及 `ADR-027` 後接草稿編號。
