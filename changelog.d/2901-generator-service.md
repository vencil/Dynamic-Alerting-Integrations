---
section: Added
topic: da-tools
issues: [2901, 2858, 2486]
created: 2026-10-14T20:24:32+00:00
---
- **`da-tools generator-service`：產生器容器的服務程式（ADR-036 §3；[#2901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2901)）**：常駐執行，經 Unix socket 上的 HTTP/1.1 回答「這個 base commit 加上這些寫入之後，整棵 conf.d 能否通過 `generate-routes --validate --strict`」。檔案從 git 物件取，每次判定在 fork 出的子程序裡做（自成 process group、`RLIMIT_DATA`、期限到整組結束），一次一筆、其餘排隊；期限、記憶體、輸出不完整、git 讀取失敗、佇列滿只拒收該筆（HTTP 200），找不到 amtool、產生器比 policy 要求的舊、部署端 policy 檔不可用才回 503。conf.d 檔的解析結果以（git blob id, 解析模式）跨請求快取，每解析完一份就交回，期限到被結束的判定也不丟已完成的部分；啟動時與換 base 後在請求之外預熱，有請求進來即讓出；另有 `/v1/status`、`/metrics` 與 warning 告警 `GeneratorJudgementMemoryNearLimit`（metric 由 chart／tenant-api 接上之前不會有資料）。部署（chart）與 tenant-api 改呼叫它在後續版本。
