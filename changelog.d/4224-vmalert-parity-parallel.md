---
section: Changed
topic: ci
issues: [4224]
created: 2026-11-28T10:20:00+00:00
---
- **`Lint Rule Packs` 的 vmalert parity 平行執行（[#4224](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4224)）**：`tests/rulepacks/test_vm_alert_parity.py` 改以 `-n auto --dist worksteal` 執行。每個 fixture 各自是一個 vmalert-tool 行程，彼此只共用各 worker 自己的翻譯副本，因此可以並排跑；worksteal 讓閒下來的 worker 接手忙碌 worker 排隊中的 fixture，最重的兩個 fixture 不會拖住其他。本機 4 核量測：110 個 fixture 序列 689 s、預設分配 391 s、worksteal 301 s。必要檢查的名稱與判準不變。
