---
section: Fixed
topic: ci
issues: [2107]
created: 2026-09-27T15:53:36+08:00
---
- **`Check Documentation Links` 每支 PR 都跑，不再受 path filter 管（docs-ci；[#2107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2107)）**：只刪除或改名被文件連結的非文件檔（例如 `rule-packs/recipes/*.yaml`、`.coderabbit.yaml`、component 的 QUICKSTART）的 PR，原本讓 check-links 以 skip 過關，斷連結要到 merge 後才紅。check-links 的輸入是所有 Markdown 加上任意位置的連結目標，沒有 filter 表達得出來，所以改成無條件執行，直接回報這個必要檢查的名字（拿掉它的 gate job 與 `docs` filter 裡只為它存在的 `.doclinkignore` 條目）。
