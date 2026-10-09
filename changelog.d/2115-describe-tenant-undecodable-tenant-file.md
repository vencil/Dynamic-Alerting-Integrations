---
section: Fixed
topic: confd-reader-consistency
issues: [2115]
created: 2026-10-09T14:36:02+00:00
---
- **`describe_tenant.py` 跳過 exporter 解不出來的租戶檔（tools；[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：樹內租戶檔有租戶的 body 是 list 或純量（`tenants: {tx: [a]}`）時，exporter 整份檔解不出來、檔內租戶都不服務；以前 `describe tx` 回 rc 1 並印 AttributeError traceback，同檔的其他租戶卻照常描述、rc 0。現在與 YAML 解析失敗的租戶檔同一條路：stderr 以 WARNING 點名檔案與租戶、整份跳過，描述其中的租戶回「找不到租戶」rc 2；`--all` 略過該檔、其餘照常。這樣的檔也不再算一個載體：同一租戶另寫在別的檔時，以前報 duplicate（rc 1），現在與 exporter 一樣取另一檔的值（`validate-config` 的 tenant_uniqueness 仍把它算進去）。`tenant-verify` 共用同一個 scanner，行為相同。
