---
section: Added
topic: da-tools
issues: [2901, 2858, 2486]
created: 2026-10-14T20:24:32+00:00
---
- **`da-tools generator-service`：產生器容器的服務程式（ADR-036 §3；[#2901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2901)）**：常駐執行，經 Unix socket 上的 HTTP/1.1（權限 0600，只接受與服務同 uid 的對端）回答「這個 base commit 加上這些寫入之後，整棵 conf.d 能否通過 `generate-routes --validate --strict`」。檔案從 git 物件取，每次判定在 fork 出的子程序裡做（自成 process group、`RLIMIT_DATA`、期限到整組結束），一次一筆、其餘排隊；期限、記憶體、輸出不完整、git 讀取失敗、佇列或連線或待處理 body 總量已滿只拒收該筆（HTTP 200，滿時不讀 body），找不到 amtool、產生器比 policy 要求的舊、部署端 policy 檔不可用、服務本身出錯（`VALIDATOR_UNAVAILABLE`／`internal_error`）才回 503。conf.d 檔的解析結果以（內容 SHA-256, 解析模式）跨請求快取，每解析完一份就交回，期限到被結束的判定也不丟已完成的部分；沒命中的檔整批交給 `da-guard parse-tree --inputs`（批次單檔模式，每批只走訪整棵樹一次）解析，冷快取的填滿時間隨檔案數線性成長；啟動時與換 base 後在請求之外預熱，有請求進來即讓出；另有 `/v1/status` 與 `/metrics`；tenant-api 把它的 `/metrics` 附在自己的之後轉出，並有判定記憶體接近上限的告警 `GeneratorJudgementMemoryNearLimit`（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）。
