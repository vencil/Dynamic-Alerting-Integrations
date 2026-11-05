---
section: Fixed
topic: alertmanager-routing
issues: [2519]
created: 2026-11-05T17:40:00+08:00
---
- **`generate-routes`：有 `{{tenant}}` enforced routing 時，你自己 `continue: true` 的路由送過的告警不再多送 root receiver 一次**：沒有租戶路由的租戶，先前靠每租戶一條 `tenant="<t>"` → root receiver 的路由取回 root 投遞；告警若先命中你自己一條 `continue: true` 的路由，之後還會命中它而多送一次。現在合併時在 enforced 路由之後放**一條**沒有 matcher、送往 base root receiver 的路由，你的路由與租戶路由收在它底下——它只在底下都沒命中時才送，與 root 相同。產出的路由樹多一層；沒有 per-tenant enforced routing、或只有單一 `platform-enforced` 路由時不變。
