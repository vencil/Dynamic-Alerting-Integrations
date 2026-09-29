---
section: Fixed
topic: dev-workflow
issues: [2210]
created: 2026-09-29T13:43:23+00:00
---
- **mkdocs strict 的 pre-push 守衛不再把每一種建站失敗都說成連結錯誤（internal、dx；[#2210](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2210)）**：以前建站只要回非 0，包括推送途中按 Ctrl-C，`scripts/ops/pre_push_mkdocs_strict.sh` 都會印出「Common fixes」連結修法和 `MKDOCS_STRICT_BYPASS` 的建議。現在這段建議整段拿掉，守衛只說建站沒有通過，原因以 `mkdocs_strict_check.sh` 自己印的 warning 與 `STATUS` 行為準。site-root 路徑語意與 bypass 的說明仍在 `docs/internal/dev-rules.md`。
