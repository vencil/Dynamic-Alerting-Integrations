---
section: Fixed
topic: dev-workflow
issues: [1920]
created: 2026-09-28T19:00:00+08:00
---
- **pre-push 擋下「沒有 preflight marker」時，印出的指令在沒有 make 的 shell 也能照做（internal、dx；[#1920](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1920)）**：以前橫幅一律叫人跑 `make pr-preflight`，而 Windows host 的 Git Bash 沒有 make。現在橫幅在印出之前先看推送用的那個 shell 有沒有 make，沒有就改印 `python scripts/tools/dx/pr_preflight.py`。`install_prepush_hook.sh` 的說明與 pre-push 看不到 refspec 時的訊息，也補上沒有 make 時的寫法。
