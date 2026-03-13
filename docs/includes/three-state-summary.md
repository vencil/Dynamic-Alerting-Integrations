**三態運營模式：**

| 模式 | 設定 | 效果 |
|------|------|------|
| Normal | （預設） | 正常告警 |
| Silent | `_silent_mode: "1"` | 持續評估但通知靜默 |
| Maintenance | `_state_maintenance: "1"` | 所有告警抑制 |

均支援 `expires` 自動失效。詳見 [架構與設計](../architecture-and-design.md) §2.7。
