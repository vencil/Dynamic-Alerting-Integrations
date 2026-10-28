---
section: Removed
topic: da-tools
issues: [3303]
created: 2026-10-24T09:00:00+00:00
---
- **`validate-config` 移除 `policy` 列，`--policy` 只由 routes 列判定（[#3303](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3303)）**：`policy` 列再跑一次路由產生器，再以子字串（`domain`／`allowlist`／`blocked`）從警告裡挑行，任何不阻擋的路由警告只要文字含這三個字之一（例如租戶名稱 `domain-a`、`blocked-a` 的 clamp WARN）就被誤收成 FAIL。混合樹（allowlist 拒收與其他阻擋錯誤並存）routes 列給一般路由建議，allowlist 拒收見 detail 行。現在產生器只跑一次（routes 列），routes 列的阻擋錯誤全是 allowlist 拒收時（以產生器的 `is_policy_refusal` 判 kind）附上 allowlist 的修法與 `--policy` 的文件。⚠️ 行為變更：上述那類樹帶 `--policy` 時結束碼 `1` → `0`；`--json` 不再有 `policy` 列；`--policy` 的參數錯誤只落在 routes 列（結束碼仍是 `2`）。真正的 allowlist 拒收本來就讓 routes 列 FAIL，結束碼不變。
