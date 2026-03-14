**Three-State Operational Model:**

| Mode | Setting | Effect |
|------|---------|--------|
| Normal | (default) | Normal alerting |
| Silent | `_silent_mode: "1"` | Continues evaluation but silences notifications |
| Maintenance | `_state_maintenance: "1"` | All alerts suppressed |

All modes support `expires` for automatic expiration. See [Architecture & Design] §2.7 for details.
