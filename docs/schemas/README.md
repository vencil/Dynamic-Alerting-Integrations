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
      expires: "2099-03-13T12:00:00Z"  # ← schema enforces RFC3339 (date-time) format
```

---

## Common Validation Errors

### "Unknown key" warnings

**Error**: `Unknown key "mysql_conections" not in defaults`

**Cause**: usually a typo in the metric name (missing 'c' in connections). ⚠️ But if EVERY tenant key is reported, the cause is on the platform side instead — a `conf.d/` with no `_defaults.yaml`, or one whose `defaults:` block is `{}`, makes every legal override look unknown (a bare `defaults:` key with a null value behaves differently again — on the shipped image it raises `AttributeError` and prints nothing). `validate-config` says which case you are in; **do not delete keys before checking**.

**Resolution**: Check `_defaults.yaml` or run `da-tools diagnose <tenant> --config-dir conf.d/ --show-inheritance` to see valid metric keys — `resolved` lists the keys that already carry a value, `declared` the ones the platform recognises but assigns no value to (⚠️ the shipped v2.9.0 image prints `resolved` but not `declared`) (set one and it takes effect; leave it and the alert stays silent) <!-- image-caveat: v2.9.0 -->

### "Invalid time window format"

**Error**: `Pattern mismatch: "1:00-9:00" does not match "^([01][0-9]|2[0-3])" ...`

**Cause**: Time window missing leading zero (should be `01:00-09:00`)

**Resolution**: Use `HH:MM-HH:MM` format (24-hour, zero-padded)

### "Invalid ISO 8601 timestamp"

**Error**: `"2099-03-13 12:00:00" is not valid under any of the given schemas`

**Cause**: Timestamp missing timezone indicator (T separator, Z suffix)

**Resolution**: Use ISO 8601 format: `2099-03-13T12:00:00Z`

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

- **Silent Mode** (`_silent_mode`): Suppress Alertmanager notifications; alerts still fire (TSDB records exist). The value (or the structured form's `target`) is `warning` / `critical` / `all`, or `disable` (aliases `disabled` / `off` / `false`) to turn it off — exactly the exporter's words, pinned by `TestSilentModeSchemaEnumsMatchReader` (#3227). The schema is stricter than the exporter where an enum cannot follow it: it is case-sensitive, refuses an empty / null value (the exporter reads it as off), and needs `false` quoted — unquoted, yaml.v3 (the schema lint reads conf.d through da-guard parse-tree) reads it as a boolean and the lint says to quote it. An unquoted `off` is a string to yaml.v3; only a YAML 1.1 loader outside the repo reads it as a boolean
- **Maintenance Mode** (`_state_maintenance`): Suppress alerts at PromQL level; no TSDB records; supports recurring schedules
- **Every other state filter** (`_state_<filter>`): one patternProperty, `definitions.stateFilterScalar` — `enable`, or `disable` (aliases `disabled` / `off` / `false`); `_state_maintenance`'s scalar branch is the same definition, pinned to the exporter's reader by `TestStateFilterSchemaMatchesReader` (#3269).
- **No schedule on a reserved key** (#3269): a reserved key takes no `{default, overrides}` time window and no `expires:` / `reason:` in that form — its readers read `default` alone. The schema refuses the form (before #3269 a `_state_<filter>` other than maintenance fell to `additionalProperties` and passed); tenant-api's write gate refuses it too, and the exporter uses `default` and WARNs.

```yaml
tenants:
  my-tenant:
    # Silent mode: notifications suppressed, but alert state tracked
    _silent_mode:
      target: "warning"
      expires: "2099-03-13T12:00:00Z"
      reason: "Known noisy alert during migration"

    # Maintenance mode: alerts suppressed entirely + recurring windows
    _state_maintenance:
      enabled: true
      expires: "2099-03-15T06:00:00Z"
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

receiver 的憑證欄位（webhook 的 `url`／`http_config`、Rocket.Chat 的 `url`、email 的 `auth_password`／`headers`、Slack 的 `api_url`、Teams 的 `webhook_url`、PagerDuty 的 `service_key`／`routing_key`）在 schema 上帶擴充關鍵字 `"x-credential": true`；這類欄位名必須都在 tenant-api 的遮罩名單內（[§3.7](../api/tenant-api-hardening.md)，由 `tests/ops/test_lib_redact.py` 守）。`definitions.secretRef`（`{secret: {name, key}}`，Kubernetes Secret 參照）已定義但**尚未接到任何欄位**（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）。receiver 不收檔案型欄位（[#3257](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3257)）：`definitions.fileReferenceKey` 是一條規則（鍵名以 `_file`、`_ref` 結尾，或為 `files`），`definitions.fileReferenceFree` 把它套在 `definitions.receiver` 的每一層，`http_config` 各定義也不再列出 Alertmanager 的 `*_file`／`*_ref`／`files` 鍵；這類欄位會讓 Alertmanager 讀取它自己容器內的檔案。路由產生器以同一條規則（`_lib_validation.is_file_reference_key`）整棵樹拒收，兩邊由 `tests/shared/test_receiver_file_reference_rule.py` 釘在一起。

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

> Running `validate_config.py` directly needs da-guard: run `make da-guard-build` and set `DA_GUARD_BINARY=$PWD/.build/da-guard` first (the da-tools image ships it); see [validate-config's prerequisite](../cli-reference.md#validate-config).

### IDE Linting

在編輯器之外對整棵 conf.d 跑同一組 schema，用 CI 那支閘門本身——它的選檔規則（兩種副檔名拼法、`_defaults*` 改驗 `platform-defaults.schema.json`、`_routing_profiles.yaml` 改驗 `routing-profiles.schema.json`、其餘 `_*` 略過並列出）就是編輯器綁定要對齊的那一份（編輯器目前沒有綁 `_routing_profiles.yaml`）：

```bash
make da-guard-build   # 或用 da-tools 映像內建的 /usr/local/bin/da-guard
DA_GUARD_BINARY=$PWD/.build/da-guard python3 scripts/tools/lint/check_confd_schema.py --config-dir conf.d
```

這支閘門經 `da-guard parse-tree` 讀 conf.d（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)，與 exporter 同一種 yaml.v3 讀法），所以需要 da-guard：`$DA_GUARD_BINARY`，否則 `$PATH` 上的 `da-guard`；沒有時結束碼 2。schema 判定的是 yaml.v3 讀到的值。

這支閘門（以及 `validate-config` 的 `yaml_quoting` 列）另外檢查一件編輯器看不到的事：schema 標為字串（含 enum）的欄位，yaml.v3 把值讀成布林／數字／時間／null（`channel: 1e3` 是浮點數 1000、`channel: true` 是布林；`channel: yes` 是字串 `"yes"`，不報），就報錯並指出檔案與欄位路徑——解法是加引號。哪些值不是字串由 parse-tree 給的 tag 判定，哪些欄位是字串由這幾份 schema 決定，沒有另外維護的詞表或欄位表（[#2164](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2164)）。

⚠️ `platform-defaults.schema.json` 的 `_routing_defaults`／`_routing_enforced` 以 `$ref` 引用 `tenant-config.schema.json` 的定義，所以用 jsonschema 驗證它時必須同時載入這份檔（`check_confd_schema.schema_registry()`）；只載入 platform schema 會得到 `Unresolvable` 錯誤，而不是靜默放行。外部驗證器單獨載入它時，相對 `$ref` 會依 `$id` 解析成抓不到的 GitHub URL——在 repo 根目錄以 `check-jsonschema --schemafile docs/schemas/platform-defaults.schema.json --base-uri file://$PWD/docs/schemas/platform-defaults.schema.json <file>` 改從本機同目錄解析；編輯器請沿用 `.devcontainer/devcontainer.json` 的 `yaml.schemas` 本機路徑綁定，不要用 URL modeline 綁 platform schema（[#2232](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2232)）。`routing-profiles.schema.json` 的每個 profile 也以 `$ref` 引用 `tenant-config.schema.json#/definitions/routing`（[#2245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2245)），載入規則相同。

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
2. **A top-level `_` key**: add, change or retire it in `components/threshold-exporter/app/pkg/config/reserved_keys.json` (which reader reads it, in which layer; [ADR-038](../adr/038-unread-keys-judged-by-readers.md)), then run `python3 scripts/tools/dx/sync_schema.py --update`: it writes the Python constants (`scripts/tools/_lib_constants.py`), Go's `pkg/config/reserved_keys_gen.go`, the portal's `tools/portal/src/interactive/tools/_common/data/reserved-keys.json` (the `_` keys the portal's config validator accepts in a tenant), and the `_` keys of this schema and `platform-defaults.schema.json` (a new property gets a stub with the registry's description; write its body here). `tests/shared/test_reserved_key_probes.py` then checks that the readers really read it where the registry says, and nowhere else.
3. **Anything else** (struct definitions, receiver types): update the Go code (`pkg/config/types.go`, `pkg/config/resolve.go`) and Python (`scripts/tools/_lib_constants.py` `RECEIVER_TYPES`; re-exported via `_lib_python.py`)
4. **Update schema**: Modify `docs/schemas/tenant-config.schema.json` to match Go/Python definitions
5. **Update docs**: Reflect changes in relevant documentation (getting-started, architecture-and-design, CHANGELOG)
6. **Test**: Validate sample configs against new schema in VS Code or with `validate_config.py`. The reserved `_` keys are generated, not kept in sync by hand: `tests/dx/test_sync_schema.py` regenerates them from the registry in CI and fails on any difference; `sync_schema.py --check` is the same check on demand. Receiver field contracts (required fields and exactly-one groups such as pagerduty `service_key` / `routing_key`) use this schema as the hub: `tests/shared/test_receiver_spec_parity.py` pins Python `RECEIVER_TYPES`. The route generator is the one judge of a receiver (ADR-036 §9); Go keeps only the list of receiver types the exporter looks up (`components/threshold-exporter/app/pkg/receiverspec`, pinned to this schema by `TestKnown_MatchesSchema`). The receiver URL and smarthost formats (#2180) are written once, as the `pattern` of `definitions.receiverHttpUrl` / `receiverSmtpHostPort` (referenced via `allOf`). Optional values follow the schema too (#2295): `send_resolved` / `require_tls` reference `definitions.yamlBool` (boolean, null, or a YAML 1.1 boolean word from its `enum`, the one authored list), `http_config` references `httpConfigOrNull`, and `httpConfig` lists exactly Alertmanager's `http_config` keys, its nested definitions (`httpBasicAuth`, `httpAuthorization`, `httpOAuth2`, `httpTLSConfig`, `httpHeader`) those of the structs Alertmanager decodes them into, all `additionalProperties: false` (`tests/alertmanager-inhibit` pins every set, the reserved header names and the TLS versions to Alertmanager's own code by reflection, #2984; the nested rules are the schema's and Alertmanager's only, see the `python_differs` rows), allows at most one auth method (one `allOf` `not` per pair of auth keys) and carries Alertmanager's proxy rules (`definitions.yamlTrue` is the true half of `yamlBool`). `http_config.proxy_url` carries no pattern: Alertmanager's rule is Go's `net/url.Parse`, which a pattern cannot express, so the route generator leaves it to its amtool gate. The Python route generator reads all of this from this file at run time, so the da-tools image ships it. Keep a pattern to syntax that ECMA-262, Python `re` and Go RE2 read alike. The shared case table `receiver_presence_cases.json` records Alertmanager's own verdict per row (`am`), which `tests/alertmanager-inhibit` checks with `config.Load`.

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
