---
section: Fixed
topic: dev-workflow
issues: [1920]
created: 2026-09-28T19:00:00+08:00
---
- **pre-push 擋下「沒有 preflight marker」時，印出的指令在 Windows host 的 Git Bash 也能照做（internal、dx；[#1920](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1920)）**：以前橫幅一律叫人跑 `make pr-preflight`，而 Git Bash 沒有 make，那台機器的 `python3` 也只是 Store stub。現在橫幅直接印 `scripts/tools/dx/pr_preflight.py`，並依 pre-push hook 繼承來的 PATH，挑實際跑得起來的 `python3` 或 `python`；兩個都不行時會明講。橫幅也註明這行要在 bash 裡執行（Windows 上用 Git Bash）。`install_prepush_hook.sh` 的說明，以及 pre-push 看不到 refspec 時的訊息，也補上沒有 make 時的寫法。
