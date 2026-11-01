---
section: Changed
topic: da-tools
issues: [3159]
created: 2026-11-01T00:56:42+00:00
---
- **backtest 的 `impact_pct` 一律為 float 或 `null`，新增 `impact_kind`（da-tools；[#3159](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3159)）**：**Breaking**（`--json`／`-o` 的欄位契約）：每筆 `analyzed` 結果的 `impact_pct` 是相對舊門檻觸發點數的百分比（float），舊門檻沒有觸發或沒有門檻時為 `null`；新欄位 `impact_kind` 為 `relative`／`no_firing`／`starts_firing`／`newly_enabled`／`disabled`。先前新啟用且會觸發的列寫出裸 `Infinity`（不合法 JSON）、開始觸發的列寫字串 `"Inf"`、不觸發的列為 `0`，`-100` 同時代表停用與不再觸發；停用且舊門檻沒有觸發的列現在是 `null`。`-o` 與 `--json` 由同一個編碼器寫出，逐位元組相同（檔尾多一個換行）；`--json -o` 通過預檢後才寫不進時（如 `/dev/full`），stdout 改印 `output_unwritable` envelope，不再是完整報告。backtest 結束碼不變。cardinality-forecast 迴歸前也濾掉時間戳非有限的樣本並計入 `non_finite_samples_dropped`（時間戳全非有限的租戶仍列出，`data_points` 為 0）；迴歸本身溢位仍得 NaN 時，`--json` 走 `output_not_serializable`。
