---
section: Fixed
topic: da-tools
issues: [3174]
created: 2026-10-20T22:00:00+00:00
---
- **從原始碼建的 da-guard 改與出貨版同形（`CGO_ENABLED=0`），generator-service 測試不再偶發記憶體超限（tools；[#3174](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3174)）**：`make da-guard-build`、測試 fixture 與三支 workflow 先前以預設 `go build` 建出 cgo 版；cgo 版每條 OS thread 的 stack（RLIMIT_STACK，通常 8 MiB）計入判定的 RLIMIT_DATA（192 MiB），thread 多時判定在跑完前就被拒。出貨的 da-tools 映像本來就是 `CGO_ENABLED=0`，峰值約 76 MiB、不隨 thread 數變動，判定預算不變。缺 da-guard 時的「從原始碼編譯」提示同步改為 `CGO_ENABLED=0 go build …`。
