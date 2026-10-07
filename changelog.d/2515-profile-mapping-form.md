---
section: Fixed
topic: confd-reader-consistency
issues: [2515]
created: 2026-10-07T18:02:00+00:00
---
- **`describe_tenant.py` 讀 mapping 寫法的 `_profile` 與 exporter 一致（tools；[#2515](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2515)）**：`_profile: {default: '010'}`（排程值寫法）在租戶檔或根平台檔 `tenants:` 裡，exporter 會綁定 profile `010`，`describe_tenant.py` 卻不綁、把 `_profile` 原樣留成 mapping，於是生效值與 merged_hash 都和 exporter 不同。現在依 exporter 的讀法取 `default` 的原文（`{default: ~}` 是空字串、不綁定）；經 merge key 帶進來的 `default`（`{<<: {default: x}}`）則與 exporter 一樣取整個 mapping 的 YAML 文字。沒有 `default` 的 mapping 照舊不綁定。`default` 寫成清單或 mapping 時 exporter 會拒收整份檔，`describe_tenant.py` 不鏡像這一點。
