---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-10-27T13:00:00+00:00
---
- **輸出寫入分兩個入口：operator 輸出交給 kernel 開檔，repo 生成物原子取代（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107) 第一階段）**：`_lib_io` 新增 `write_operator_output`（`-` 即 stdout、一次執行只准一個 stdout 擁有者、kernel 開檔）與 `write_repo_artifact`（原子取代，拒收 `-`、裝置、FIFO、socket）。原子寫入器只留一份，自 `dx/` 移到映像會帶的 `_lib_atomic_write.py`。gar `--findings-json`、fed-key `--jwks-out`、assemble `--manifest` 走 operator 輸出；`state-reconcile --manifest-path` 與 `configmap-assemble --output` 走原子取代。本 euid 擁有、不經 symlink 的一般檔在寫入前設成該輸出的權限（`--findings-json`、兩個生成物為 0600）；生成物永遠以暫存檔加改名寫入，做不到就是錯誤。⚠️ 行為變更（owner 2026-10-21 核可 V1–V14、2026-10-24 核可 V15 與風險 1）：`configmap-assemble --output` 為 `-`、裝置、FIFO 時 `0` → `2`；其餘結束碼變化見 #3107 的 V1–V10。
