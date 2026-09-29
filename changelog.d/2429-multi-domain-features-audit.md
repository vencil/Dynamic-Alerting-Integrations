---
section: Fixed
topic: docs
issues: [2429]
created: 2026-09-29T13:30:00+00:00
---
- **multi-domain 的「支援的特性」與「限制與陷阱」對齊現況（docs；[#2429](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2429)）**：`multi-domain-conf-layout`（中英）刪掉三條沒有實作的宣稱——`_defaults.yaml` 的 `{{ env.X }}` 模板與其「逃逸」限制、`.git-blame` 追蹤、`validate-config` 會擋循環繼承；`_defaults.yaml` 保留字那條改寫成實際行為（tenant 由 `tenants:` key 決定，子目錄平台檔的 `tenants:` 會被略過並 WARN），並補上真正會踩的「目錄 symlink 不跟進」。
