---
section: Changed
topic: rule-packs
issues: [1177]
created: 2026-11-17T13:30:00+00:00
---
- **⚠️ `ContainerCrashLoop` 也涵蓋重啟迴圈，改為每租戶一條（rule-packs；[#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177)）**：除了目前處於 `CrashLoopBackOff` 的容器，一小時內重啟 ≥3 次、最近 20 分鐘內仍有重啟、且同小時沒有 OOM 終止的容器也算在內。補上兩個缺口：容器跑一陣子才崩潰或被 liveness probe 殺掉時，等待狀態撐不到 30 秒，以前整個迴圈都不發；真實 crash loop 中，容器每次短暫 Running 都會讓告警解除再觸發，現在會持續到最後一次重啟後 20 分鐘內解除。重啟數取 kube-state-metrics 一小時內的實際增量，不外插，多個 KSM 副本不重複計，計數倒退時只計倒退後的次數。告警值是租戶內處於崩潰迴圈的容器數，逐容器見 `tenant:container_crashloop_waiting:by_container` 與 `tenant:container_restart_loop:by_container`。**Breaking**：告警不再帶 `namespace`、`reason` label，每租戶一條；以 `reason="CrashLoopBackOff"` 做的 Alertmanager matcher 請改用 `alertname="ContainerCrashLoop"`。
