---
section: Changed
topic: alertmanager-routing
issues: [2906, 3043]
created: 2026-10-17T07:30:00+00:00
---
- **⚠️ Breaking：路由產生器與經它讀 conf.d 的工具改讀 `da-guard parse-tree`（yaml.v3），需要 da-guard（alertmanager-routing；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`generate-routes`、`validate-config`、`policy-engine`、`explain-route`、`notification-tester`、`check_routing_profiles` 與產生器容器不再用 PyYAML 讀 conf.d；沒有 da-guard（`$DA_GUARD_BINARY` 或 `$PATH`）時結束碼 2。讀法隨之改變：未加引號的 `_routing: off` 從拒收變成停用；`!!binary` 這類標記、null 鍵、多份文件等 `parse-tree` 讀不了的檔整份拒收並帶上 `kind`；過去只有 PyYAML 拒收的寫法（TAB 分隔、`2001-13-45`）照讀。巢狀 mapping（例如 `http_config`、`routes` 條目的 `match`）改依鍵排序輸出，ConfigMap 的 bytes 會改變一次。產生器不再讀用不到的 `_metadata`，這是 [#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043) 的一部分：`load_tenant_tree(...).as_tuple()` 仍是五格，原本放 `_metadata` 的第五格現在固定是 `{}`，之後再拿掉。
