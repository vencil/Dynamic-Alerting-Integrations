---
section: Fixed
topic: exporter
issues: [2344]
created: 2026-09-28T15:36:02+00:00
---
- **`IncrementalLoad_1000_NoChange` 與 `..._OneFileChanged` 固定在 Reread 模式，不再隨 `-benchtime` 翻模式（bench；[#2344](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2344)）**：fixture 每次新建，前 2s 落在 `TreeScanMtimeGuard` 內全檔重讀，之後才走 mtime fast-path，所以 3s 量到的是兩個模式的混合。現在載入前把 fixture mtime 推到一小時後，兩支恆為 Reread；平面的 Warm 模式由 `..._NoChange_MtimeGuard` 量。⚠️ 與階層的 `DiffAndReload_Hierarchical_*_NoChange`（固定在 Warm，#2048）同名不同模式。PR gate（1s）數字不變；nightly（3s）這兩支會出現一次工作定義階梯，不是退化。
