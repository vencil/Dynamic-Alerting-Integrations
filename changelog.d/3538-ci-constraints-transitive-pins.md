---
section: Fixed
topic: ci
issues: [3538]
created: 2026-11-04T14:30:00+00:00
---
- **CI 的 Python 傳遞相依改為全部釘版（ci；[#3538](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3538)）**：`requirements/ci-constraints.txt` 先前只釘 `pip install` 直接點名的套件，它們自己帶進來的相依每次都裝當天最新版；`pytest-cov` 帶進的 `coverage` 從 7.17.0 升到 7.17.1 後，被量測的子行程遇到 Ctrl-C 結束碼變成 1，`Python Coverage` 分片因此轉紅。現在 `coverage` 釘在 7.17.0，其餘傳遞相依也依 CI 目前裝到的版本釘上。新守衛 `tests/shared/test_ci_constraints_transitive_pins.py` 從已安裝的 metadata 遞迴走 `Requires-Dist`，走到沒有釘版的套件就失敗，並點名是哪個已釘套件經過哪條路徑帶進來的。
