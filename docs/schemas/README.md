---
title: "JSON Schema Reference"
tags: [schema, validation, yaml, tooling]
audience: [platform-engineers, tenants]
version: v2.9.0
lang: zh
---

# JSON Schema — Tenant Configuration Validation

JSON Schema (draft-07) specifications for validating tenant YAML configurations. These schemas enable IDE autocomplete, validation, and documentation in modern editors.

## Available Schemas

### `tenant-config.schema.json`

**Purpose**：驗證 `conf.d/` 下的租戶設定檔——扁平或巢狀皆可；副檔名 `.yaml` 或 `.yml`，不分大小寫；檔名開頭不是 `_` 也不是 `.`。`_defaults*` 檔改用 `platform-defaults.schema.json`。

**Coverage**:
- Tenant configuration structure (`tenants:` mapping)
- Metric thresholds (scalar and scheduled values with time-window overrides)
- Alert routing configuration (webhook, email, Slack, Teams, Rocket.Chat, PagerDuty)
- Operational modes (`_silent_mode`, `_state_maintenance`, maintenance windows)
- Metadata injection (`_metadata` with runbook URL, owner, tier)
- Dimensional thresholds (`metric{label="value"}`, `metric{label=~"regex"}`)
- Tenant profile references (v1.12.0+)
- Platform-enforced dual routing (v1.13.0+)

**Version**: v2.9.0

**Schema Format**: JSON Schema Draft 7 ([specification](https://json-schema.org/specification.html))

---

## Integration with VS Code

### Setup Instructions

1. **Install YAML extension** (if not already installed):
   - Go to Extensions → search for "YAML" → install "YAML" by Red Hat

2. **Add schema mapping** to `VS Code settings.json`：從
   [`.devcontainer/devcontainer.json`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.devcontainer/devcontainer.json)
   複製 `yaml.schemas` 區塊（dev container 內已自動套用）。它同時綁兩種副檔名拼法，並讓 `_*`
   平台檔不吃 tenant schema；單寫 `"conf.d/*.yaml"` 會漏掉 `.yml` 檔，還會把 tenant schema
   套到 `_defaults.yaml` 上。glob 為何長這樣見
   [`editor-schema-validation.md`](../internal/editor-schema-validation.md)。

   **Finding settings.json**:
   - **Windows/Linux**: `Ctrl+Shift+P` → "Preferences: Open Settings (JSON)"
   - **macOS**: `Cmd+Shift+P` → "Preferences: Open Settings (JSON)"

3. **Verify**:
   - 開啟 `conf.d/` 下任一租戶檔（例如 `conf.d/db-a.yaml`）
   - YAML extension should show autocomplete, validation, and inline documentation

### Expected Behavior

Once configured, you'll see:

- **Autocomplete** on reserved keys (`_silent_mode`, `_routing`, `_metadata`, etc.)
- **Validation warnings** for unknown metric keys or malformed receivers
- **Inline documentation** on hover (description, examples)
- **Type hints** for structured values (e.g., time-window overrides)

Example:
```yaml
tenants:
  my-tenant:
    mysql_connections: "70"      # ← autocomplete suggests threshold values
    _silent_mode:                 # ← hover shows documentation
      target: "warning"           # ← schema validates against ["warning", "critical", "all"]
      expires: "2026-03-13T12:00:00Z"  # ← schema enforces RFC3339 (date-time) format
```

---

## Common Validation Errors

### "Unknown key" warnings

**Error**: `Unknown key "mysql_conections" not in defaults`

**Cause**: usually a typo in the metric name (missing 'c' in connections). ⚠️ But if EVERY tenant key is reported, the cause is on the platform side instead — a `conf.d/` with no `_defaults.yaml`, or one whose `defaults:` block is `{}`, makes every legal override look unknown (a bare `defaults:` key with a null value behaves differently again — on the shipped image it raises `AttributeError` and prints nothing). `validate-config` says which case you are in; **do not delete keys before checking**.

**Resolution**: Check `_defaults.yaml` or run `da-tools diagnose <tenant> --config-dir conf.d/ --show-inheritance` to see valid metric keys — `resolved` lists the keys that already carry a value, `declared` the ones the platform recognises but assigns no value to (⚠️ the shipped v2.9.0 image prints `resolved` but not `declared`) (set one and it takes effect; leave it and the alert stays silent)

### "Invalid time window format"

**Error**: `Pattern mismatch: "1:00-9:00" does not match "^([01][0-9]|2[0-3])" ...`

**Cause**: Time window missing leading zero (should be `01:00-09:00`)

**Resolution**: Use `HH:MM-HH:MM` format (24-hour, zero-padded)

### "Invalid ISO 8601 timestamp"

**Error**: `"2026-03-13 12:00:00" is not valid under any of the given schemas`

**Cause**: Timestamp missing timezone indicator (T separator, Z suffix)

**Resolution**: Use ISO 8601 format: `2026-03-13T12:00:00Z`

### Receiver type mismatch

**Error**: `Slack receiver missing required property: "api_url"`

**Cause**: Wrong receiver type or missing required field

**Resolution**: Verify receiver `type` matches the provided fields (see schema definitions)

---

## Schema Highlights

### Dimensional Thresholds

Schema supports dimensional (labeled) thresholds with literal or regex matching:

```yaml
tenants:
  my-tenant:
    # Literal dimension: queue="orders"
    "redis_queue_length{queue=\"orders\"}": "100"

    # Regex dimension: database matching pattern
    "mongodb_dbstats_storage_size{database=~\"prod.*\"}": "53687091200"

    # Multiple dimensions
    "mongodb_collection_avg_obj_size{database=\"orders\",collection=\"transactions\"}": "4096"
```

### Scheduled Values (Time-Window Overrides)

Schema validates scheduled thresholds with time-window overrides (UTC-only, supports cross-midnight):

```yaml
tenants:
  my-tenant:
    # Scalar (backward compatible)
    mysql_connections: "70"

    # Structured (with overrides)
    mysql_connections_backup:
      default: "70"
      overrides:
        - window: "01:00-09:00"     # 1AM–9AM UTC: stricter threshold
          value: "1000"
        - window: "22:00-06:00"     # 10PM–6AM UTC (cross-midnight)
          value: "50"
```

### Operational Modes

Schema distinguishes between:

- **Silent Mode** (`_silent_mode`): Suppress Alertmanager notifications; alerts still fire (TSDB records exist)
- **Maintenance Mode** (`_state_maintenance`): Suppress alerts at PromQL level; no TSDB records; supports recurring schedules

```yaml
tenants:
  my-tenant:
    # Silent mode: notifications suppressed, but alert state tracked
    _silent_mode:
      target: "warning"
      expires: "2026-03-13T12:00:00Z"
      reason: "Known noisy alert during migration"

    # Maintenance mode: alerts suppressed entirely + recurring windows
    _state_maintenance:
      enabled: true
      expires: "2026-03-15T06:00:00Z"
      recurring:
        - cron: "0 2 * * *"           # Daily 2AM UTC
          duration: "4h"              # 4-hour maintenance window
          reason: "Nightly backup"
```

### Alert Routing

Schema supports multiple receiver types with validation:

```yaml
tenants:
  my-tenant:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/services/T/B/xxx"
        channel: "#alerts"
      group_wait: "30s"
      group_interval: "5m"
      repeat_interval: "4h"
      overrides:
        - alertname: "HighConnections"
          receiver:
            type: "pagerduty"
            routing_key: "xxxxx/xxxxx/xxxxx"
```

### Tenant Profiles (v1.12.0+)

Schema validates profile references:

```yaml
tenants:
  my-tenant:
    _profile: "standard-db"      # Must exist in _profiles.yaml
    mysql_connections: "50"      # Override profile value
```

### Metadata Injection

Schema validates tenant metadata fields:

```yaml
tenants:
  my-tenant:
    _metadata:
      runbook_url: "https://wiki.example.com/{{tenant}}"  # {{tenant}} placeholder
      owner: "dba-team"
      tier: "tier-1"
```

---

## Integration with Other Tools

### CI Validation (`validate_config.py`)

The schema is referenced by `validate_config.py` for comprehensive validation:

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

### IDE Linting

在編輯器之外對整棵 conf.d 跑同一組 schema，用 CI 那支閘門本身——它的選檔規則（兩種副檔名拼法、`_defaults*` 改驗 `platform-defaults.schema.json`、其餘 `_*` 略過並列出）就是編輯器綁定要對齊的那一份：

```bash
python3 scripts/tools/lint/check_confd_schema.py --config-dir conf.d
```

（別用 yamllint 做這件事：yamllint 1.38.0 的 CLI 沒有 `--schema` 參數，傳入會以 rc 2 結束。）

---

## Related Documentation

- **[Tenant 快速入門指南](../getting-started/for-tenants.md)**: Getting started guide with practical examples
- **[Architecture and Design](../architecture-and-design.md)**: Deep dive into tenant config structure (§2)
- **[Migration Guide](../migration-guide.md)**: Migrating existing alert rules to tenant config
- **[CLI Reference](../cli-reference.md)**: da-tools CLI commands for config management
- **[Alert Routing Split Scenario](../scenarios/alert-routing-split.md)**: Dual-perspective alert routing example
- **[Glossary](../glossary.md)**: Terminology and definitions

---

## Contributing

To update the schema:

1. **Identify the change**: New reserved key, new receiver type, changed validation rules, etc.
2. **Update Go code first**: Modify `components/threshold-exporter/app/pkg/config/types.go` (`validReservedKeys` / `validReservedPrefixes` / struct definitions) and `pkg/config/resolve.go` (`ValidateTenantKeys`)
3. **Sync Python**: Update `scripts/tools/_lib_constants.py` (`VALID_RESERVED_KEYS`, `RECEIVER_TYPES`; re-exported via `_lib_python.py`)
4. **Update schema**: Modify `docs/schemas/tenant-config.schema.json` to match Go/Python definitions
5. **Update docs**: Reflect changes in relevant documentation (getting-started, architecture-and-design, CHANGELOG)
6. **Test**: Validate sample configs against new schema in VS Code or with `validate_config.py`. Two drift gates run in CI: `tests/shared/test_reserved_key_py_go_parity.py` (Python↔Go reserved keys) and `tests/dx/test_sync_schema.py` (Go↔JSON-schema); `sync_schema.py --check` is the same Go↔schema check on demand.

---

## Version History

| Version | Changes |
|---------|---------|
| v1.13.0 | Platform-enforced routing (`_routing_enforced`), dual-perspective alert routing |
| v1.12.0 | Tenant profiles (`_profile`), four-layer inheritance |
| v1.11.0 | Recurring maintenance schedules (`_state_maintenance.recurring`), scheduled values with time-window overrides |
| v1.8.0  | N:1 tenant mapping (`_namespaces`), regex dimensional thresholds (`=~`) |
| v1.7.0  | Structured silent mode and maintenance mode with expiry |
| v1.4.0  | Routing defaults and customization (`_routing`) |
| v1.0.0  | Initial schema (scalar thresholds, reserved keys) |

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [Tenant 快速入門](../getting-started/for-tenants.md) | ⭐⭐⭐ |
| [CLI Reference](../cli-reference.md) | ⭐⭐ |
| [API Reference](../api/README.md) | ⭐⭐ |
