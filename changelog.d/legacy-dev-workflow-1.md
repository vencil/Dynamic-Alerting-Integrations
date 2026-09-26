---
section: Fixed
topic: dev-workflow
issues: [824, 1472, 1476, 1487, 1664, 1689, 1690, 1691, 1756, 1811, 1917, 1924, 1951, 1952]
created: 2026-09-26T17:00:00+00:00
---
- **pre-push 守衛與 preflight 真的在守（internal、dx）**：pre-commit 會吃掉 pre-push 的 stdin，導致擋直推 main 等守衛看不到 refspec 而恆印 `Passed`；改由獨立的 `prepush_dispatch.sh`（`install_prepush_hook.sh` 安裝）把完整 refspec 餵給每支守衛，mkdocs strict 守衛改驗被推的那顆 commit（[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664)、[#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689)）。⚠️ `pr_preflight` 報 BLOCKED 時 rc 改為 1，`--ci` 旗標移除（[#1472](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1472)）；另修 preflight 的 marker 清除範圍、merge commit 本機誤擋、fix-push 死鎖與失敗訊息；⚠️ `make pr-preflight-quick` 不再略過「pre-push 守衛是否接上」的判定，全新 clone 未安裝守衛時會紅；Windows 逃生門的 push 不再以 `--no-verify` 關掉整條 pre-push。
