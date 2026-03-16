# @dynamic-alerting/backstage-plugin

Backstage frontend plugin for the Dynamic Alerting Platform.
Displays tenant thresholds, alert quality scores, and recent alert history
in your Backstage instance.

## Features

- **Tenant Overview**: metric counts, custom/disabled/default distribution, operational state (Normal/Silent/Maintenance)
- **Threshold Table**: searchable, sortable list of all resolved thresholds per tenant
- **Firing Alerts**: real-time view of currently firing alerts from Prometheus ALERTS metric
- **Entity Integration**: embed as a tab on any Backstage Service Entity page via annotation
- **Auto-refresh**: 30-second polling for near-real-time updates

## Prerequisites

- Backstage instance (v1.20+)
- Prometheus accessible from Backstage backend (direct or via proxy)
- Dynamic Alerting Platform deployed with threshold-exporter exposing `/metrics`

## Installation

```bash
# From Backstage root
yarn --cwd packages/app add @dynamic-alerting/backstage-plugin
```

## Configuration

### app-config.yaml

```yaml
proxy:
  endpoints:
    '/prometheus':
      target: http://prometheus.monitoring.svc:9090
      allowedHeaders: ['Accept']

dynamicAlerting:
  prometheus:
    proxyPath: /api/proxy/prometheus
```

### App.tsx — Standalone Page

```tsx
import { DynamicAlertingPage } from '@dynamic-alerting/backstage-plugin';

// In your App routes:
<Route path="/dynamic-alerting" element={<DynamicAlertingPage />} />
```

### EntityPage.tsx — Service Entity Tab

```tsx
import { DynamicAlertingEntityContent } from '@dynamic-alerting/backstage-plugin';

// In the service entity layout:
<EntityLayout.Route path="/dynamic-alerting" title="Alerting">
  <DynamicAlertingEntityContent />
</EntityLayout.Route>
```

The plugin reads the `dynamic-alerting.io/tenant` annotation from the entity's
`catalog-info.yaml` to determine which tenant to display:

```yaml
# catalog-info.yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: my-database
  annotations:
    dynamic-alerting.io/tenant: "db-a"
```

## Data Sources

| Source | PromQL | Purpose |
|--------|--------|---------|
| `user_threshold` | `user_threshold` | Tenant threshold values |
| `user_silent_mode` | `user_silent_mode` | Silent mode status |
| `user_state_filter` | `user_state_filter{filter="maintenance"}` | Maintenance mode |
| `ALERTS` | `ALERTS{alertstate="firing"}` | Currently firing alerts |

## Development

```bash
cd components/backstage-plugin
yarn install
yarn start   # Starts Backstage dev server with plugin
yarn test    # Run unit tests
yarn build   # Build for production
```
