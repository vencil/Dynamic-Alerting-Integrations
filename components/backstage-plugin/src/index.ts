/**
 * @dynamic-alerting/backstage-plugin
 *
 * Backstage frontend plugin for Dynamic Alerting Platform (§5.13).
 * Displays tenant thresholds, alert quality scores, and recent alert history
 * in a dedicated tab on Backstage Service Entity pages.
 *
 * Data sources:
 * - Prometheus API (threshold metrics via PromQL)
 * - alert_quality.py JSON output (optional, via Backstage proxy)
 */
export { dynamicAlertingPlugin, DynamicAlertingPage } from './plugin';
export { DynamicAlertingEntityContent } from './components/EntityContent';
export type { PrometheusConfig, TenantOverview, AlertQuality } from './api/types';
