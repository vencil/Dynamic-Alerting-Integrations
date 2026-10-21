---
section: Fixed
topic: portal
issues: [2833]
created: 2026-10-21T14:44:09+00:00
---
- **tenant-manager 的待審核 PR 連結改讀 API 真的有送的 `web_url`（portal；[#2833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2833)）**：`GET /api/v1/prs` 的 PR 網址欄位是 `web_url`，portal 卻讀 `html_url`，於是頂部「N 個待審核 PR」橫幅與租戶卡片上的 PR 徽章都沒有 `href`，點了不會開 PR。兩處已改讀 `web_url`。原本的單元測試用手寫的 `html_url` fixture，只會跟元件互相印證；portal 測試裡模擬 tenant-api 回應的 fixture 現在改經 `apiFixture`，以 tenant-api 產生的 swagger 定義驗證，且把每個物件視為不得帶未宣告欄位，API 不送的欄位會讓測試直接失敗。
