---
section: Fixed
topic: ci
issues: [3871]
created: 2026-11-14T03:00:00+00:00
---
- **Python mutation pilot 不再於第一個 mutant 逾時就整輪中止（ci；[#3871](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3871)）**：pilot 對每個 mutant 起一個新的 pytest session（每個限 120 秒），而 `tests/conftest.py` 在宣告 `VIBE_TEST_GO_BINARIES=build` 的 session 開頭建 da-guard、da-crdecode、da-read。nightly runner 的 Go 快取是冷的，第一個 session 光建置就量到 338 秒；`TimeoutExpired` 沒被接住，整輪在記下任何結果前就失敗，nightly 自 2026-11-04 起一直是紅的。現在 pilot 在第一個 mutant 之前建一次（清單與建置旗標取自 conftest 用的同一份來源：`READER_BUILD_SOURCES`、`go_build_argv`），再以 `VIBE_TEST_<VAR>` 交給每個 session 走 conftest 的重用分支；建置失敗時 pilot 以 rc 2 結束並附上 go 的輸出。單一 session 逾時改記為該 mutant 的 SETUP-FAIL，接著跑下一個。
