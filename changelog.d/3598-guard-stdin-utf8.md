---
section: Fixed
topic: dx
issues: [3598]
created: 2026-11-13T00:33:20+00:00
---
- **session-guards 一律以 UTF-8 讀 hook 的 stdin（dx；[#3598](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3598)）**：之前以 locale 編碼解碼，cp950 主機（zh-TW Windows）上指令或路徑含中文時解碼失敗——`preflight_bash` 的 `sed -i` 攔截 crash 成不阻擋的 `1`；經 `read_payload` 讀 payload 的 `paths_map`／`skill_usage`／`stop_evidence` 讀成「沒有 payload」而什麼都不做；`session-init` 拿不到 payload 的 `session_id`，marker 退回非 payload 的識別。讀 stdin 的入口只剩 `_guardlib`，靜態閘門擋其他 guard 直接碰 `sys.stdin`。`stop_evidence --message-stdin` 收到非 UTF-8、或沒讀到訊息（stdin 是空的、已關閉或是終端機）時以 `1` 結束並點名（之前非 UTF-8 是 crash、`<&-` 也是 crash）。payload 開頭的 UTF-8 BOM 會被去掉。
