---
section: Fixed
topic: da-guard
issues: [3206]
created: 2026-10-22T18:30:00+00:00
---
- **conf.d 裡有不是合法 UTF-8 的檔名時，da-guard 每個會走訪 conf.d 的子命令一律結束碼 2（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：先前 `parse-tree` 拒收這棵樹，而驗證（`da-tools guard defaults-impact`）、`served-values`、`key-refs` 仍回 0，「da-guard 通過」與「讀 conf.d 的工具全部拒收」說的是同一棵樹。現在這幾個子命令與 `effective` 共用同一道檢查，在讀樹之前拒收，訊息以跳脫形式指名那些位元組（例如 `"bad-\xff.yaml"`），請改名後重跑。
