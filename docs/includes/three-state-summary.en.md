<!-- Bilingual pair: three-state-summary.md -->

**Three-State Operational Model:**

| Mode | Setting | Effect |
|------|---------|--------|
| Normal | (default) | Normal alerting |
| Silent | `_silent_mode: "warning"` (or `"critical"` / `"all"`) | Continues evaluation but silences notifications |
| Maintenance | `_state_maintenance: "1"` | Alerts carrying the maintenance condition do not fire (not all alerts) |

All modes support `expires` for automatic expiration. See [Architecture & Design](../architecture-and-design.en.md) §2.7 for details.
