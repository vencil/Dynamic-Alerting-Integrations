---
section: Fixed
topic: confd-family
issues: [2476, 2481]
created: 2026-09-30T12:10:20+00:00
---
- **`da_assembler --render-cr` 以 Kubernetes client 的方式解碼 CR，讀不了的 CR 回 rc 2 而非 traceback（da-tools；[#2476](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2476)）**：CR 由新的 `da-crdecode` 以 kubectl 同一套 YAML→JSON 轉換（`sigs.k8s.io/yaml`）解碼；需要這支 binary（`$PATH` 或 `$DA_CRDECODE_BINARY`，`make assembler-render` 會先建）。
  型別與 null key 依該轉換：沒加引號、會被讀成數字或布林的 `name`（`0o17`、`1e3`、`08`、`y`、`n`）改為 rc 2；Kubernetes 也接受的 null key 寫法照常 render。`metadata.name`／`namespace`、`spec.tenants` 底下任何一層的鍵與租戶的 `_profile` 經轉換後若與原文不同（沒加引號的 `010`、`yes`），rc 2 並提示加引號，所以寫出的這些鍵與 `_profile` 一律是原文；兩種讀法結構對不上（如 `!!omap`）時亦 rc 2。同一 mapping 裡轉成 JSON 後撞名的鍵（`010:` 與 `"8":`，含經 `<<` 帶入者）在 Kubernetes 端留下哪一個不固定，現在 rc 2。值依轉換結果寫出（值 `010` 寫成 `8`，`12:30` 維持文字），鍵維持 CR 的順序。
  非 UTF-8、`!!int team`、anchor 引用自己、多於一份文件、`.inf`：rc 2，`da-crdecode` 的錯誤逐行轉出；本工具讀不了原文（如自訂標籤 `!foo`）亦 rc 2；`name: .inf` 不再附舊檔提示。非 null 的 `spec.profile`（不會被寫出）改為 rc 2。不重現 CRD schema 驗證。
