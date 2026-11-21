---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-13T17:30:00+00:00
---
- **workflow 的 `on.pull_request.paths` 改由執行期量到的讀檔產生（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：`image-ref-resolve.yaml` 的清單過去漏了間接載入的檔，只改那些檔的 PR 不會觸發它。現在 job 在 checkout 後第一步執行 `scripts/ops/atime_trace.py reset`，最後一個 `run:` 步驟執行 `atime_trace.py finish` 與 `workflow_path_filter.py assert`：期間被量到讀過的追蹤檔（不分行程：make、Go 工具鏈、action、Python）都必須被清單涵蓋，否則點名並失敗；量測本身不成立是結束碼 `2`。清單的 `GENERATED path-trace` 區塊由 `make workflow-path-filters` 依已提交的量測（`.github/path-traces/`）與字面 import 產生，兩個步驟也是生成的。新的 pre-commit hook `workflow-path-filter-check` 擋區塊 drift、接線位置錯誤，以及所有 workflow 的 path filter 裡的死條目與比對器讀不懂的 pattern。⚠️ 比對器改與 picomatch 一致：`*.ext` 也涵蓋 `.ext` 這個檔名。⚠️ `reset` 到 `finish` 之間該 checkout 的 local git config 為 `core.trustctime=false`，接上量測的 job 其他 `run:` 不得提到這個設定。量不到的讀取列在兩支工具的模組 docstring。目前只有 `image-ref-resolve.yaml` 接上；其餘帶 path gate 的 advisory workflow 列在 `ADVISORY_PATH_GATES`。
