---
section: Changed
topic: da-tools
issues: [2900, 2858, 2486]
created: 2026-10-11T16:50:08+00:00
---
- **⚠️ Breaking：`da-tools init` 產生的設定改為固定 da-tools 版本（ADR-036 §6；[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)）**：GitHub workflow、GitLab `variables` 與 `.pre-commit-config.da.yaml` 的 `DA_TOOLS_IMAGE` 預設由 `ghcr.io/vencil/da-tools:latest` 改為執行 `init` 那一版的 `ghcr.io/vencil/da-tools:v<版號>`（tag，不是 digest），讓 CI 判定可重現、可與 tenant-api chart 固定的 da-tools 版本對齊。之後發布的 da-tools 不會再自動進到已產生的專案：只升映像請直接改產生物裡的 `DA_TOOLS_IMAGE`（建議做法）——對已初始化的專案重跑 `init` 會被拒絕，除非帶 `--force`，而 `--force` 會覆寫你改過的檔案；`--da-tools-image` 照舊可覆寫。既有專案不受影響。portal 的 CI/CD 設定精靈預設同步改為版號 tag。
