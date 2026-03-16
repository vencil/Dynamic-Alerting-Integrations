import React, { useEffect, useState } from 'react';
import {
  Header,
  Page,
  Content,
  ContentHeader,
  SupportButton,
  Table,
  StatusOK,
  StatusWarning,
  StatusError,
  InfoCard,
  Progress,
} from '@backstage/core-components';
import { useApi } from '@backstage/core-plugin-api';
import { Grid, Chip, Typography, Box } from '@material-ui/core';
import { Alert } from '@material-ui/lab';
import { dynamicAlertingApiRef } from '../api/PrometheusClient';
import type { TenantOverview, RecentAlert } from '../api/types';

/**
 * Status badge component for tenant operational state.
 */
function TenantStatus({
  silentMode,
  maintenance,
}: {
  silentMode: string;
  maintenance: boolean;
}) {
  if (maintenance) {
    return <StatusWarning>Maintenance</StatusWarning>;
  }
  if (silentMode !== 'none') {
    return <StatusWarning>Silent ({silentMode})</StatusWarning>;
  }
  return <StatusOK>Normal</StatusOK>;
}

/**
 * Severity chip with color coding.
 */
function SeverityChip({ severity }: { severity: string }) {
  const colorMap: Record<string, 'default' | 'primary' | 'secondary'> = {
    warning: 'primary',
    critical: 'secondary',
  };
  return (
    <Chip
      label={severity}
      color={colorMap[severity] || 'default'}
      size="small"
      variant="outlined"
    />
  );
}

/**
 * Tenant overview table — lists all tenants with metric counts and status.
 */
function TenantOverviewTable({
  tenants,
}: {
  tenants: TenantOverview[];
}) {
  const columns = [
    { title: 'Tenant', field: 'tenant' },
    { title: 'Metrics', field: 'metricCount', type: 'numeric' as const },
    { title: 'Custom', field: 'customCount', type: 'numeric' as const },
    { title: 'Disabled', field: 'disabledCount', type: 'numeric' as const },
    {
      title: 'Status',
      render: (row: TenantOverview) => (
        <TenantStatus
          silentMode={row.silentMode}
          maintenance={row.maintenance}
        />
      ),
    },
  ];

  return (
    <Table
      title="Tenant Overview"
      options={{ paging: true, pageSize: 10, search: true }}
      columns={columns}
      data={tenants}
    />
  );
}

/**
 * Recent alerts table — shows currently firing alerts.
 */
function RecentAlertsTable({ alerts }: { alerts: RecentAlert[] }) {
  const columns = [
    { title: 'Alert', field: 'alertname' },
    { title: 'Tenant', field: 'tenant' },
    {
      title: 'Severity',
      render: (row: RecentAlert) => <SeverityChip severity={row.severity} />,
    },
    { title: 'State', field: 'state' },
    { title: 'Active Since', field: 'activeAt' },
  ];

  return (
    <Table
      title="Firing Alerts"
      options={{ paging: true, pageSize: 10, search: true }}
      columns={columns}
      data={alerts}
    />
  );
}

/**
 * Summary cards — high-level statistics.
 */
function SummaryCards({ tenants }: { tenants: TenantOverview[] }) {
  const totalMetrics = tenants.reduce((s, t) => s + t.metricCount, 0);
  const maintenanceCount = tenants.filter(t => t.maintenance).length;
  const silentCount = tenants.filter(
    t => t.silentMode !== 'none',
  ).length;

  return (
    <Grid container spacing={3}>
      <Grid item xs={12} sm={3}>
        <InfoCard title="Tenants">
          <Typography variant="h4">{tenants.length}</Typography>
        </InfoCard>
      </Grid>
      <Grid item xs={12} sm={3}>
        <InfoCard title="Total Metrics">
          <Typography variant="h4">{totalMetrics}</Typography>
        </InfoCard>
      </Grid>
      <Grid item xs={12} sm={3}>
        <InfoCard title="In Maintenance">
          <Typography variant="h4">{maintenanceCount}</Typography>
        </InfoCard>
      </Grid>
      <Grid item xs={12} sm={3}>
        <InfoCard title="Silent Mode">
          <Typography variant="h4">{silentCount}</Typography>
        </InfoCard>
      </Grid>
    </Grid>
  );
}

/**
 * Main Dynamic Alerting page component.
 * Fetches data from Prometheus and displays tenant overview + alerts.
 */
export function DynamicAlertingPage() {
  const api = useApi(dynamicAlertingApiRef);
  const [tenants, setTenants] = useState<TenantOverview[]>([]);
  const [alerts, setAlerts] = useState<RecentAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function fetchData() {
      try {
        const [t, a] = await Promise.all([
          api.getTenantOverviews(),
          api.getRecentAlerts(),
        ]);
        if (mounted) {
          setTenants(t);
          setAlerts(a);
          setLoading(false);
        }
      } catch (err) {
        if (mounted) {
          setError(
            err instanceof Error ? err.message : 'Failed to fetch data',
          );
          setLoading(false);
        }
      }
    }

    fetchData();
    // Refresh every 30 seconds
    const interval = setInterval(fetchData, 30_000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [api]);

  return (
    <Page themeId="tool">
      <Header
        title="Dynamic Alerting"
        subtitle="Multi-Tenant Threshold Management & Alert Monitoring"
      />
      <Content>
        <ContentHeader title="">
          <SupportButton>
            Powered by Dynamic Alerting Platform. Displays real-time
            tenant thresholds and alert status from Prometheus.
          </SupportButton>
        </ContentHeader>

        {loading && <Progress />}
        {error && (
          <Box mb={2}>
            <Alert severity="error">{error}</Alert>
          </Box>
        )}

        {!loading && !error && (
          <>
            <Box mb={3}>
              <SummaryCards tenants={tenants} />
            </Box>
            <Box mb={3}>
              <TenantOverviewTable tenants={tenants} />
            </Box>
            <RecentAlertsTable alerts={alerts} />
          </>
        )}
      </Content>
    </Page>
  );
}
