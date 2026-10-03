---
section: Fixed
topic: confd-family
issues: [2529]
created: 2026-10-02T14:33:47+00:00
---
- **`da_assembler --render-cr` 收到 SIGTERM／SIGINT 時會停下（da-tools；[#2529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2529)）**：先前只記一行 `Received signal 15, shutting down...` 就繼續等 `da-crdecode`，等它結束或逾時才結束。現在會終止 `da-crdecode`、印一行 `Received signal N, stopped rendering <file>`，並以該訊號結束行程（shell 看到 rc 143／130）。訊號在寫檔前到達就不寫任何檔案；在寫檔途中到達則先寫完再結束，不留半成品。
  - import 本模組不再改動行程的 SIGINT／SIGTERM 處置；handler 改由 `main()` 依模式掛上。controller 模式（`--once` 與 watch）的行為不變。
