---
section: Fixed
topic: confd-family
issues: [2396]
created: 2026-09-29T14:15:00+00:00
---
- **`da_assembler --render-cr` 驗證 `metadata.name` 與 `metadata.namespace` 的格式（da-tools；[#2396](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2396)）**：`metadata.name` 直接當輸出檔名，先前不驗：`../escaped` rc 0，檔案寫到 `--config-dir` 之外；name 或 namespace 含換行時，檔頭註解被斷開，輸出多出一個頂層鍵。現在 name 必須是 Kubernetes 物件名稱（RFC 1123 subdomain：小寫英數、`-`、`.`，頭尾為英數，最長 253 字元），namespace 有給時必須是 RFC 1123 label（小寫英數與 `-`，最長 63 字元），不符就印一行錯誤、rc 2、不寫任何檔案。
  ⚠️ 這會縮小接受的輸入：大寫、底線、含 `:` 的日期時間，以及沒加引號、YAML 1.1 讀成數字或布林的 namespace（`8`、`yes`），先前都會照寫，現在一律 rc 2。名稱格式因此與 Kubernetes 的 RFC 1123 規則對齊；YAML 1.1 與 Kubernetes 讀法在型別上的既有差異仍在（例如沒加引號的 `0o17`、`y` 仍會照收）。沒寫 namespace 時行為不變；寫成 `null`、`Null`、`NULL`、`~`、空值或 `""`（直接寫或經 `<<` merge 帶入）時視為未設，輸出與沒寫 namespace 相同（先前檔頭寫成 `None/…`、`/…`），既有的輸出檔會被重寫一次。文件任何位置出現帶 `!!null` 標籤、原文卻不是空值或上述 null 寫法的純量（鍵或值皆同，如 `!!null team`）時整份 rc 2，與 Kubernetes 整份拒收一致。
