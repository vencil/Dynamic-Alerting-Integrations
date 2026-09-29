---
section: Fixed
topic: exporter
issues: [2444]
created: 2026-09-29T14:00:00+00:00
---
- **`da-guard` 的平行測試不再因共用全域 logger 而 `-race` 轉紅（exporter；[#2444](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2444)）**：`run()` 原本把 process-global `log` 指向呼叫者的 `errOut`，平行測試各自傳自己的 buffer，pkg/config 與 collector 的 WARN 就寫進別的測試正在寫的 buffer，main 的 `Go Tests — threshold-exporter` 因此依排程間歇轉紅。改成只在 `main()` 設一次（報告路徑不帶時間戳，served-values 維持原樣），`run()` 不再碰全域；新增 `TestRun_LeavesTheGlobalLoggerAlone` 釘住這件事。binary 的輸出不變。
