---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-21T05:30:00+00:00
---
- **advisory workflow 的 path gate 帳本改為分類，三支便宜的驗證 workflow 拿掉 `paths`（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756) 階段 2）**：`scripts/ops/workflow_path_filter.py` 的 `NOT_YET_TRACED`（workflow → 自由文字理由）改名為 `ADVISORY_PATH_GATES`，值是 `(kind, why)`。kind 只有兩種：`diff-driven`（`paths` 定位為 workflow 的主題集合，不做讀檔斷言；清單目前仍是手寫，裁決要的「由工具的 glob 生成」還沒做：`guard-defaults-impact`、`config-diff-advisory`、四支 bench）與 `awaits-read-assertion`（`paths` 應等於 job 讀的檔，等接 atime 讀檔斷言：`component-docker-build`、`backtest`、`forge-e2e-github`，順序寫在 `READ_ASSERTION_ORDER`）。`generate --check` 新增兩種發現：kind 不在這兩種之內（`UNCLASSIFIED`）、等待斷言的條目與順序表不是同一個集合（`UNORDERED`）。`security-audit.yaml`、`required-checks-drift.yml`、`playwright.yml`（E2E spec lint）不再帶 `paths`，每支 PR 都跑，三者仍是 advisory（`required-checks-drift` 因此在 branch protection 與 `.github/required-checks.yaml` 不一致的期間會在每支 PR 上顯示紅燈，不擋合併）；`security-audit.yaml` 與 `playwright.yml` 在 push 上的 `paths` 一併拿掉。
