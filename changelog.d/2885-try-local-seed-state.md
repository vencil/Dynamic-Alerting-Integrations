---
section: Changed
topic: tenant-api
issues: [2885]
created: 2026-10-11T12:06:17+00:00
---
- **try-local 的示範 Saved View 改由 state 目錄提供，開機不再印 conf.d 殘檔 WARN（try-local；[#2885](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2885)）**：示範 view 原本放在 `seed/conf.d/_views.yaml`，首次開機匯入 state store 後仍留在 conf.d，之後每次開機都印「conf.d still holds tenant-api state files」。現在放在 `seed/state/`，tenant-api 容器以 `seed/state-seed.sh` 為 entrypoint，啟動前把 state volume 裡還沒有的紀錄複製進去（已存在不覆蓋，改過的 view 會保留）。README 同步更正：Saved View 存在 state store、不產生 git commit，示範 commit 改用調整租戶閾值。
