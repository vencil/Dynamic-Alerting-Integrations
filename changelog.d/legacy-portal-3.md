---
section: Added
topic: portal
issues: [811, 962, 1075]
created: 2026-09-26T17:00:00+00:00
---
- **Tenant Manager 身分與群組 UX、入門精靈與無障礙改善（portal；[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)、[#811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/811)）**：tenant-manager 頁首新增身分列，顯示目前登入身分與生效篩選，使用者所屬群組在 `_rbac.yaml` 沒有任何權限時提示檢查群組設定；群組側欄接上既有 `/api/v1/groups`，建立、選擇、刪除群組可用，篩選下拉改讀即時資料。Hub 角色篩選修好點了無反應的問題，改為可多選並記住選擇。入門精靈改為先選角色、只顯示該角色的路徑。modal 補上 focus-trap 與關閉後焦點還原，多處把飽和色當文字的 WCAG 對比缺陷改用 AA 色票，並以 design-token lint 防止復發。da-portal 為 pulled image，這些變更隨下次 portal release 生效。
