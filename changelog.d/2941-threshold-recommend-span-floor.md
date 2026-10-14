---
section: Fixed
topic: threshold-governance
issues: [2941]
created: 2026-10-13T16:50:31+00:00
---
- **`threshold-recommend`／`threshold-govern`：上界 key 資料不足 24 小時一律為 LOW，`--min-samples` 說到做到（[#2941](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2941)）**：先前上界 key 只看樣本數，15 秒評估一次的 recording rule 約 4 小時就到 1000 點、判為 HIGH，`threshold-govern` 會據此開出門檻砍掉九成以上的 PR。現在樣本落在的不同 UTC 小時數少於 24 就降為 LOW，不開 PR（量覆蓋時數而不是跨度，一個孤立的早期樣本撐不開它）。LOW 的建議仍會寫進 `--export-patch`，行尾註明 LOW。`--min-samples` 改為同時作用於 MEDIUM 與 HIGH（先前大於 1000 時擋不住 HIGH），小於 100 的值（先前被靜默忽略）以結束碼 `2` 拒絕。
