---
section: Changed
topic: rule-packs
issues: [1177]
created: 2026-11-17T13:30:00+00:00
---
- **⚠️ `ContainerCrashLoop` 也涵蓋重啟迴圈，改為每租戶一條（rule-packs；[#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177)）**：除了處於 `CrashLoopBackOff` 的容器，一小時內重啟 ≥3 次、最近 20 分鐘內仍有重啟的容器也算（含 OOM 造成的重啟）。補上兩個缺口：容器跑一陣子才崩潰或被 liveness probe 殺掉時，等待狀態撐不到 30 秒，以前整個迴圈都不發；真實 crash loop 中容器每次短暫 Running 都讓告警解除再觸發。加上 `keep_firing_for: 10m`，迴圈期間持續觸發，最後一次重啟後約 30 分鐘解除；週期約 23–30 分鐘的慢迴圈仍會每個週期通知一次。依設計會定期退出的容器（restartPolicy Always）每小時 ≥3 次也會觸發，可用 maintenance 或關閉 `container_crashloop` 處理。重啟數取 kube-state-metrics 一小時內的實際增量（不外插、KSM 副本不重複計、計數倒退時只計倒退後）。值是處於崩潰迴圈的容器數。**Breaking**：不再帶 `namespace`、`reason` label；以 `reason="CrashLoopBackOff"` 做的 matcher 請改用 `alertname="ContainerCrashLoop"`。
