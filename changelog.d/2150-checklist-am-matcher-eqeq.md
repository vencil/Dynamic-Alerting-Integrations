---
section: Fixed
topic: docs
issues: [2150]
created: 2026-09-27T05:59:29+00:00
---
- **troubleshooting-checklist 不再教客戶在 Alertmanager matcher 用 `==`（docs；[#2150](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2150)）**：「shadow alert 漏到 production receiver」一節的 **If not this** 清單，刪掉「AM v0.27 vs v0.32 matcher 語法差異 → 用 `==` 不是 `=~`」這一條。Alertmanager 沒有 `==` 運算子，照做會讓 config 驗證失敗；而 `=~` 是全錨定比對，對 `"shadow"` 這種不含 regex 特殊字元的值與 `=` 行為相同，所以這一條本來就不是這個症狀的排障步驟。同一清單中 `null` receiver 漏配那一條所引的 log 字樣，改為 Alertmanager 實際印出的 `undefined receiver "null" used in route`。中英兩版同步。
