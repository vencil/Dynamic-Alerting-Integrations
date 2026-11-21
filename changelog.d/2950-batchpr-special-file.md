---
section: Fixed
topic: confd-family
issues: [2950]
created: 2026-11-21T12:36:00+00:00
---
- **`da-batchpr` 遇到 FIFO 不再永久卡住（da-batchpr；[#2950](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2950)）**：`apply` 讀 `--emit-dir`、`refresh-source` 讀 patches 目錄時，樹裡若有 FIFO、socket、device，或指向它們的 symlink，先前會直接開檔讀取，FIFO 沒有寫入端時整個指令卡死。現在改用 exporter 與 tenant-api 共用的讀檔函式（`config.ReadConfFile`）：以不阻塞的方式開檔、對開到的同一個 fd 判斷（`IsSpecialFile`），列舉之後才被換成 FIFO 的檔案一樣拒收；點名該路徑並以 exit 2 結束，不會產出漏掉檔案的 PR。
