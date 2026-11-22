---
section: Changed
topic: da-tools
issues: [4223]
created: 2026-11-22T15:13:01+00:00
---
- **寫入入口搬到不載入 PyYAML 與 `_lib_constants` 的 `_lib_output`（[#4223](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4223)）**：`write_operator_output`、`write_repo_artifact`、`check_repo_artifact`、`ensure_dir`、`output_write`、`OutputWriteError` 等寫入入口改定義在 `scripts/tools/_lib_output.py`，載入時只依賴標準函式庫、`_lib_exitcodes` 與 `_lib_label`（`write_repo_artifact` 第一次呼叫時另載入同樣不依賴 PyYAML 的 `_lib_atomic_write`）；沒有 PyYAML 的 pre-commit 環境與會重寫 `_lib_constants.py` 的產生器因此也能用入口。`_lib_io` 轉出同名，既有的 `from _lib_io import …` 不必改，工具行為與結束碼不變。⚠️ 給擴充或測試者：`OutputWriteError.__module__` 變成 `_lib_output`；spy 或 monkeypatch 要替換呼叫端實際查找的那個名稱：入口之間經 `_lib_output` 的模組全域互相呼叫，要掛在 `_lib_output`；呼叫端查的是 `_lib_io.<名稱>` 時（含 `_lib_io.write_onboard_hints`）要掛在 `_lib_io`，改 `_lib_output` 不會替換 `_lib_io` 已轉出的綁定。da-tools 與 recipe-preview 映像多帶一個檔。
