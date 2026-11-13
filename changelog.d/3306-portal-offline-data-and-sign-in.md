---
section: Changed
topic: portal
issues: [3306, 1040]
created: 2026-11-07T14:12:01+00:00
---
- **portal：API 回 401 一律導去登入，tenant-manager 的離線與示範資料標示清楚且唯讀（portal；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §5；[#3306](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3306)、[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：portal 各工具對 `/api/` 的任何請求回 401 時，頁面都導去 oauth2-proxy 登入（`/oauth2/start?rd=` 原頁）；畫面上顯示一次性 federation token、或 custom alerts／recipe builder 有未儲存的編輯時不導走（存檔收到 401 時改提示登入已過期、編輯保留在畫面上）。tenant-manager 不再改顯示離線或示範資料。API 不可用時改讀 `platform-data.json` 或示範資料的行為保留，但頁面以橫幅標示是哪一種、不提供任何寫入（原本示範模式預設可寫）；離線資料不再混入示範租戶，API 模式取不到群組時也不再補示範群組。登入回來一分鐘內又收到 401 時不再重導（避免經 IdP 無限循環），改顯示拒絕訊息。
