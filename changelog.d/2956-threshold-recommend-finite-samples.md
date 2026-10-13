---
section: Fixed
topic: threshold-governance
issues: [2956]
created: 2026-10-13T16:33:58+00:00
---
- **`threshold-recommend`／`threshold-govern` 上界 key 的樣本數只算有限值（[#2956](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2956)）**：先前信心等級依「含 NaN／Inf 的點數」判定，百分位卻只從有限值算：999 個 NaN 加 1 個有效點就判 HIGH，全是 NaN 時判 HIGH 並建議把門檻設成 0（-100%），`threshold-govern` 會據此開 PR。現在先濾掉 NaN／Inf：只剩少數有效點時降為 LOW，全部無效時視為「no data points found」。下界 key 本來就有過濾，不受影響。
