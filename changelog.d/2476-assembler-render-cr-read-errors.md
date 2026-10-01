---
section: Fixed
topic: confd-family
issues: [2476, 2481]
created: 2026-09-30T12:10:20+00:00
---
- **`da_assembler --render-cr` 改以 Kubernetes client 的方式解碼 CR，讀不了的 CR 回 rc 2 的錯誤而非 traceback（da-tools；[#2476](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2476)）**：CR 改由新的 `da-crdecode`（`components/threshold-exporter/app/cmd/da-crdecode`）以 kubectl 同一套 YAML→JSON 轉換（`sigs.k8s.io/yaml`）解碼，不再以 PyYAML（YAML 1.1）讀。`--render-cr` 需要這支 binary：放在 `$PATH`，或以 `$DA_CRDECODE_BINARY` 指定；`make assembler-render` 會先建它。
  因此型別、null key、重複鍵與 `<<` merge 都依 Kubernetes 的讀法：沒加引號的 `name: 0o17`、`1e3`、`08`、`y`、`n` 讀成數字或布林，改為 rc 2（先前 rc 0 並寫出 `0o17.yaml` 等檔）；null key 只在 Kubernetes 也拒收時拒收。輸出的租戶 id 與值也是轉換後的樣子：沒加引號的 `010` 寫成 `8`（租戶 id 寫成 `'8'`），`12:30` 與日期時間維持文字，鍵依字母排序。
  非 UTF-8、帶標籤卻建不出值（`!!int team`）、anchor 引用自己、多於一份文件、`.inf`：rc 2，`da-crdecode` 的錯誤原文逐行轉出。巢狀過深超過本工具上限時亦 rc 2，訊息註明是本工具的上限。CRD 宣告的 `spec.profile` 兩條路徑都不會寫出，`--render-cr` 遇到非 null 的 `spec.profile` 改為 rc 2。不重現 API server 的 CRD schema 驗證。
