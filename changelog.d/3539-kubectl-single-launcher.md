---
section: Fixed
topic: tools
issues: [3539]
created: 2026-11-14T04:19:00+00:00
---
- **kubectl 只在一處啟動，寫入類指令也一樣（`_lib_kubectl.kubectl_run`；[#3539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3539)）**：PATH 上的 kubectl 找得到但起不來（沒有執行權限、`Exec format error`、`#!` 直譯器不存在）時，grafana-import 匯入、cutover-tenant、drift-detect `--mode operator`、`make configmap-assemble` 以前是 traceback 加 `unexpected internal error`（直譯器不存在則誤報「not found」），現在一行點名哪個 `kubectl` 指令與原因；generate-alertmanager-routes `--apply` 與 bench 的 synth 租戶腳本改走同一處。結束碼都不變（2）。grafana-import 在 ConfigMap 已套用、`kubectl label` 起不來或逾時時，報表保留已套用的那一列並寫明「its label is NOT confirmed」。⚠️ 訊息文字有變：「kubectl is not on PATH」→「kubectl is not available (install the kubectl binary)」、「no answer within Ns」→「kubectl did not finish within Ns (rc 124)」；cutover-tenant 的「kubectl not found in PATH」「timed out (30s)」與 drift-detect 的「kubectl not found」「kubectl command timeout」同樣改為上述文字。依這些字串判斷的腳本請改看結束碼。
