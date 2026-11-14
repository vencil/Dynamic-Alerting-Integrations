---
section: Changed
topic: da-tools
issues: [3602]
created: 2026-11-13T00:35:42+00:00
---
- **`<&-` 與 `< /dev/null` 一樣讀到空輸入（da-tools；[#3602](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3602)）**：fd 0 在啟動前被關掉時，直譯器把 `sys.stdin` 設為 None，讀 stdin 的工具因此 crash（結束碼 `2`）。現在 `sys.stdin` 綁到補上的唯讀 null device，`da-tools` 分派層同樣處理。⚠️ 結束碼變更：`guard_defaults_scopes --null - <&-` 由 `2` 改為空輸入的 `0`；`offboard --state -`、`verify_diff --stdin` 與互動式提示仍是 `2`，原因改為與空輸入相同的那一行。
