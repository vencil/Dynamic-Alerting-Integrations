---
section: Removed
topic: tools
issues: [3691]
created: 2026-11-12T23:42:07+00:00
---
- **`scaffold_tenant` 移除 `--generate-profile` 與 `--tier`（da-tools scaffold；[#3691](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3691)）**：⚠️ breaking，帶這兩個旗標會以 rc 2 結束。原因有二：一是 staging tier 把 warning 乘 1.2、critical 不乘，造成 warning 高於 critical、百分比超過 100，值越低越糟的鍵（例如 `db2_bufferpool_hit_ratio`）反而變嚴；二是 profile 會展開成租戶的覆寫值，生成器寫進去的任何數字都會凍結成一份副本，平台之後調整 defaults 或 critical 起始值時，綁定的租戶都收不到。profile 只寫和平台不同的值，請手寫 `_profiles.yaml`；critical 層的起始值仍由 `scaffold_tenant` 與 `init_project` 寫進租戶自己的檔。
