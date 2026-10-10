---
section: Fixed
topic: dev-workflow
issues: [824, 1472, 1476, 1487, 1664, 1689, 1690, 1691, 1756, 1811, 1917, 1924, 1951, 1952, 2617, 2669, 2671, 2687, 2688, 2696, 2701, 2702, 2728, 2734, 2745, 2746, 2765, 2770, 2772]
created: 2026-09-26T17:00:00+00:00
---
- **pre-push 守衛與 preflight 真的在守（internal、dx）**：pre-commit 吃掉 stdin，守衛恆印 `Passed`；改由獨立的 `prepush_dispatch.sh`（`install_prepush_hook.sh` 安裝）以 builtin 讀 stdin、把 refspec 餵給守衛，mkdocs strict 改驗被推的 commit（[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664)、[#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689)）；preflight 只認與安裝器的 shim 逐位元組相同、可執行且不是 symlink 的 `.git/hooks/pre-push`，其餘判未接上；安裝器以 shim 取代 pre-commit 樣板、守衛複本與 git-lfs 的 hook（LFS 由 dispatcher 跑），其他都拒絕；`core.hooksPath` 有設或 hooks 目錄是 symlink 時判量不到、安裝器拒絕；設在別棵 worktree 時 preflight 亦同（[#2669](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2669)、[#2701](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2701)）。⚠️ `pr_preflight` 報 BLOCKED 時 rc 改為 1，`--ci` 旗標移除（[#1472](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1472)）；另修 marker 清除範圍、merge commit 誤擋、fix-push 死鎖與失敗訊息；⚠️ `make pr-preflight-quick` 不再略過「守衛是否接上」的判定，未裝守衛時會紅；Windows 逃生門不再以 `--no-verify` 推送。
