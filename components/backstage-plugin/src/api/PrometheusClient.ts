import { createApiRef } from '@backstage/core-plugin-api';
import type {
  PrometheusConfig,
  TenantOverview,
  ThresholdEntry,
  RecentAlert,
} from './types';

/**
 * API reference for the Dynamic Alerting Prometheus client.
 * Registered via the plugin's factory and consumed by components.
 */
export const dynamicAlertingApiRef = createApiRef<DynamicAlertingApi>({
  id: 'plugin.dynamic-alerting.api',
});

export interface DynamicAlertingApi {
  /** Fetch all tenant overviews from user_threshold metrics */
  getTenantOverviews(): Promise<TenantOverview[]>;
  /** Fetch recent firing/pending alerts */
  getRecentAlerts(limit?: number): Promise<RecentAlert[]>;
}

/**
 * Default implementation that queries Prometheus via Backstage proxy.
 *
 * Expected app-config.yaml:
 * ```yaml
 * proxy:
 *   endpoints:
 *     '/prometheus':
 *       target: http://prometheus.monitoring.svc:9090
 *       allowedHeaders: ['Accept']
 * dynamicAlerting:
 *   prometheus:
 *     proxyPath: /api/proxy/prometheus
 * ```
 */
export class PrometheusClient implements DynamicAlertingApi {
  private readonly baseUrl: string;

  constructor(config: PrometheusConfig) {
    this.baseUrl = config.proxyPath || config.baseUrl;
  }

  async getTenantOverviews(): Promise<TenantOverview[]> {
    // Query user_threshold metric for all tenants
    const thresholdData = await this.query('user_threshold');
    const silentData = await this.query('user_silent_mode');
    const maintenanceData = await this.query(
      'user_state_filter{filter="maintenance"}',
    );

    // Group by tenant
    const tenantMap = new Map<string, TenantOverview>();

    for (const result of thresholdData) {
      const tenant = result.metric.tenant || 'unknown';
      if (!tenantMap.has(tenant)) {
        tenantMap.set(tenant, {
          tenant,
          metricCount: 0,
          customCount: 0,
          disabledCount: 0,
          silentMode: 'none',
          maintenance: false,
          thresholds: [],
        });
      }
      const overview = tenantMap.get(tenant)!;
      overview.metricCount++;

      const entry: ThresholdEntry = {
        metric: result.metric.metric || result.metric.__name__ || '',
        value: parseFloat(result.value[1] as string),
        severity: result.metric.severity || 'warning',
        component: result.metric.component || '',
        labels: { ...result.metric },
      };
      // Remove standard labels from the labels map
      delete entry.labels.__name__;
      delete entry.labels.tenant;
      delete entry.labels.metric;
      delete entry.labels.severity;
      delete entry.labels.component;

      overview.thresholds.push(entry);
    }

    // Merge silent mode data
    for (const result of silentData) {
      const tenant = result.metric.tenant;
      const overview = tenantMap.get(tenant);
      if (overview) {
        overview.silentMode =
          result.metric.target_severity || 'all';
      }
    }

    // Merge maintenance data
    for (const result of maintenanceData) {
      const tenant = result.metric.tenant;
      const overview = tenantMap.get(tenant);
      if (overview) {
        overview.maintenance = parseFloat(result.value[1] as string) === 1.0;
      }
    }

    return Array.from(tenantMap.values()).sort((a, b) =>
      a.tenant.localeCompare(b.tenant),
    );
  }

  async getRecentAlerts(limit = 50): Promise<RecentAlert[]> {
    const data = await this.query('ALERTS{alertstate="firing"}');
    const alerts: RecentAlert[] = data
      .map(result => ({
        alertname: result.metric.alertname || '',
        tenant: result.metric.tenant || '',
        severity: result.metric.severity || 'warning',
        state: (result.metric.alertstate || 'firing') as 'firing' | 'pending',
        activeAt: result.value[0]
          ? new Date(
              (result.value[0] as number) * 1000,
            ).toISOString()
          : new Date().toISOString(),
        value: parseFloat(result.value[1] as string),
        labels: { ...result.metric },
      }))
      .slice(0, limit);

    return alerts;
  }

  /**
   * Execute a PromQL instant query via the configured proxy.
   */
  private async query(
    expr: string,
  ): Promise<
    Array<{
      metric: Record<string, string>;
      value: [number, string];
    }>
  > {
    const url = `${this.baseUrl}/api/v1/query?query=${encodeURIComponent(expr)}`;
    const response = await fetch(url, {
      headers: { Accept: 'application/json' },
    });

    if (!response.ok) {
      throw new Error(
        `Prometheus query failed: ${response.status} ${response.statusText}`,
      );
    }

    const json = await response.json();
    if (json.status !== 'success') {
      throw new Error(`Prometheus query error: ${json.error || 'unknown'}`);
    }

    return json.data?.result || [];
  }
}
