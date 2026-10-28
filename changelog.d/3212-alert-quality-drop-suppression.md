---
section: Removed
topic: da-tools
issues: [3212]
created: 2026-10-24T10:00:00+00:00
---
- **`alert-quality` 移除 Suppression 這一維與 `--alertmanager`（[#3212](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3212)）**：這一維從來沒量對過——Alertmanager v2 的 `filter` 比對 label，查 `alertstate=suppressed` 永遠回空，被抑制數恆為 0；就算查對，分子是當下的告警筆數、分母是 `--period` 期間的樣本點數，不是同一個單位也不是同一段時間。沒有任何資料來源能回答「期間內、分租戶的抑制比例」，所以不報這個數。overall grade 改由 Noise／Stale／Latency 三項決定。⚠️ 行為變更：帶 `--alertmanager` 的呼叫結束碼 `0` → `2`（argparse 未知旗標）；JSON 每筆告警少 `suppressed_count`、`suppression_ratio`、`suppression_grade`，文字與 Markdown 報告少 Suppressed 欄；分數不變（對真的 Alertmanager 這一維恆為 GOOD）。
