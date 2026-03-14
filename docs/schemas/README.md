---
title: "JSON Schema Reference"
tags: [schema, validation, yaml, tooling]
audience: [platform-engineers, tenants]
version: v2.0.0-preview.3
lang: zh
---

# JSON Schema — Tenant Configuration Validation

JSON Schema (draft-07) specifications for validating tenant YAML configurations. These schemas enable IDE autocomplete, validation, and documentation in modern editors.

## Available Schemas

### `tenant-config.schema.json`

**Purpose**: Validates tenant YAML configuration files (`conf.d/*.yaml`) for the Dynamic Alerting platform.

**Coverage**:
- Tenant configuration structure (`tenants:` mapping)
- Metric thresholds (scalar and scheduled values with time-window overrides)
- Alert routing configuration (webhook, email, Slack, Teams, Rocket.Chat, PagerDuty)
- Operational modes (`_silent_mode`, `_state_maintenance`, maintenance windows)
- Metadata injection (`_metadata` with runbook URL, owner, tier)
- Dimensional thresholds (`metric{label="value"}`, `metric{label=~"regex"}`)
- Tenant profile references (v1.12.0+)
- Platform-enforced dual routing (v1.13.0+)

**Version**: v2.0.0-preview

**Schema Format**: JSON Schema Draft 7 ([specification](https://json-schema.org/specification.html))

---

## Integration with VS Code

### Setup Instructions

1. **Install YAML extension** (if not already installed):
   - Go to Extensions → search for "YAML" → install "YAML" by Red Hat

2. **Add schema mapping** to `VS Code settings.json`:
   ```json
   "yaml.schemas": {
     "./docs/schemas/tenant-config.schema.json": "conf.d/*.yaml"
   }
   ```

   **Finding settings.json**:
   - **Windows/Linux**: `Ctrl+Shift+P` → "Preferences: Open Settings (JSON)"
   - **macOS**: `Cmd+Shift+P` → "Preferences: Open Settings (JSON)"

3. **Verify**:
   - Open any `conf.d/*.yaml` file
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
      expires: "2026-03-13T12:00:00Z"  # ← schema enforces ISO 8601 format
```

---

## Common Validation Errors

### "Unknown key" warnings

**Error**: `Unknown key "mysql_conections" not in defaults`

**Cause**: Typo in metric name (missing 'c' in connections)

**Resolution**: Check `_defaults.yaml` or run `diagnose.py --show-inheritance` to see valid metric keys

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

Use the schema with other YAML linters:

```bash
# Example: yamllint with schema support
yamllint -d "{extends: default, rules: {document-start: disable}}" \
  --schema docs/schemas/tenant-config.schema.json \
  conf.d/*.yaml
```

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
2. **Update Go code first**: Modify `components/threshold-exporter/app/config.go` (ValidateTenantKeys, struct definitions)
3. **Sync Python**: Update `scripts/tools/_lib_python.py` (validReservedKeys, RECEIVER_TYPES)
4. **Update schema**: Modify `docs/schemas/tenant-config.schema.json` to match Go/Python definitions
5. **Update docs**: Reflect changes in relevant documentation (getting-started, architecture-and-design, CHANGELOG)
6. **Test**: Validate sample configs against new schema in VS Code or with `validate_config.py`

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
