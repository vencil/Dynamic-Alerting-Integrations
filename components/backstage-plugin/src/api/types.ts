/**
 * Configuration for connecting to the Prometheus backend.
 * Set via app-config.yaml under `dynamicAlerting.prometheus`.
 */
export interface PrometheusConfig {
  /** Base URL of the Prometheus instance (e.g., http://prometheus:9090) */
  baseUrl: string;
  /** Optional: Backstage proxy path (e.g., /api/proxy/prometheus) */
  proxyPath?: string;
}

/**
 * Overview data for a single tenant, derived from user_threshold metrics.
 */
export interface TenantOverview {
  /** Tenant identifier (e.g., "db-a") */
  tenant: string;
  /** Total number of threshold metrics exposed */
  metricCount: number;
  /** Number of metrics with custom (non-default) values */
  customCount: number;
  /** Number of disabled metrics */
  disabledCount: number;
  /** Silent mode status: "none" | "warning" | "critical" | "all" */
  silentMode: string;
  /** Maintenance mode active */
  maintenance: boolean;
  /** Thresholds grouped by metric name */
  thresholds: ThresholdEntry[];
}

/**
 * A single resolved threshold metric entry.
 */
export interface ThresholdEntry {
  metric: string;
  value: number;
  severity: string;
  component: string;
  labels: Record<string, string>;
}

/**
 * Alert quality score from da-tools alert-quality output.
 */
export interface AlertQuality {
  tenant: string;
  overallScore: number;
  grade: 'A' | 'B' | 'C' | 'D' | 'F';
  dimensions: {
    coverage: number;
    noise: number;
    actionability: number;
    timeliness: number;
  };
}

/**
 * Recent alert event from Prometheus ALERTS metric.
 */
export interface RecentAlert {
  alertname: string;
  tenant: string;
  severity: string;
  state: 'firing' | 'pending';
  activeAt: string;
  value: number;
  labels: Record<string, string>;
}
