---
section: Changed
topic: ci
issues: [1446]
created: 2026-10-01T13:35:39+00:00
---
- **A-13（E2E spec 禁 `test.skip()`／`test.fixme()`）改以行為式測試守整條鏈（[#1446](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1446)）**：新增 `tests/lint/test_e2e_spec_lint_behaviour.py`，在拋棄式樹裡原樣複製真實的 `tests/e2e` 設定、注入隨機檔名的 parked spec、跑真的 `e2e_spec_lint.sh` 並要求失敗；乾淨的樹則要求成功。`playwright.yml` 的 `E2E Spec Lint (A-13)` job 在 `npm ci` 之後以 `VIBE_REQUIRE_E2E_ESLINT=1` 執行它（缺 eslint 時為失敗而非 skip）。原本逐項讀設定的列舉式斷言已移除；入口接線、觸發條件與上述執行步驟仍由 `test_e2e_spec_lint.py` 結構式守住。另修正該檔 trigger-key accessor 的豁免可被同名定義遮蔽繞過的缺口：accessor 須為 module 最外層 def，且名稱全檔只能綁定一次。
