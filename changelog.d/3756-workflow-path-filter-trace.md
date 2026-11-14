---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-13T17:30:00+00:00
---
- **workflow 的 `on.pull_request.paths` 改由執行期追蹤產生（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：`image-ref-resolve.yaml` 的清單過去漏了經 import 間接載入的檔（例如 `scripts/tools/_lib_input.py`），只改那些檔的 PR 不會觸發它。現在 job 內每個 Python 行程記錄自己執行與開啟的檔、`go_build.sh` 記錄建置的 package；清單中的 `GENERATED path-trace` 區塊由 `make workflow-path-filters` 依已提交的追蹤（`.github/path-traces/`）、Go 來源、字面 import 與具名的邊界集合（非 Python 行程讀的檔）產生，job 最後一步遇到「有載入、不在清單」的檔就失敗並點名；開啟追蹤與斷言的兩個步驟也是生成的。新的 pre-commit hook `workflow-path-filter-check`（`make workflow-path-filters-check`）擋區塊 drift 與接線位置錯誤，並對所有 workflow 的 path filter 擋死條目（刻意保留的要登記原因）與比對器讀不懂的 pattern；順帶修掉 `config-diff-advisory.yml`、`playwright.yml`、`ci.yml`、`docs-ci.yaml` 共五條比對不到任何檔的條目。⚠️ 比對器改與 picomatch 一致：`*.ext` 現在也涵蓋 `.ext` 這個檔名。目前只有 `image-ref-resolve.yaml` 接上追蹤；其餘以 path 設 PR gate 的 workflow（含用 dorny/paths-filter 的 `ci.yml`、`docs-ci.yaml`、`validate.yaml`）列在 `NOT_YET_TRACED`，各附原因。
