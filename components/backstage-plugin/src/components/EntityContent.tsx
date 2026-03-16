import React, { useEffect, useState } from 'react';
import { useApi } from '@backstage/core-plugin-api';
import {
  InfoCard,
  Table,
  StatusOK,
  StatusWarning,
  StatusError,
  Progress,
} from '@backstage/core-components';
import { Grid, Typography, Box, Chip } from '@material-ui/core';
import { Alert } from '@material-ui/lab';
import { dynamicAlertingApiRef } from '../api/PrometheusClient';
import type { TenantOverview, ThresholdEntry } from '../api/types';

/**
 * Entity content component for Backstage Service Entity pages.
 *
 * Usage in EntityPage.tsx:
 * ```tsx
 * import { DynamicAlertingEntityContent } from '@dynamic-alerting/backstage-plugin';
 *
 * // Inside the service entity page layout:
 * <EntityLayout.Route path="/dynamic-alerting" title="Alerting">
 *   <DynamicAlertingEntityContent tenantId="db-a" />
 * </EntityLayout.Route>
 * ```
 *
 * The tenantId can be derived from the entity's annotations:
 * ```yaml
 * # catalog-info.yaml
 * metadata:
 *   annotations:
 *     dynamic-alerting.io/tenant: "db-a"
 * ```
 */
export function DynamicAlertingEntityContent({
  tenantId,
}: {
  tenantId?: string;
}) {
  const api = useApi(dynamicAlertingApiRef);
  const [overview, setOverview] = useState<TenantOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function fetchData() {
      try {
        const allTenants = await api.getTenantOverviews();
        const match = tenantId
          ? allTenants.find(t => t.tenant === tenantId)
          : allTenants[0];
        if (mounted) {
          setOverview(match || null);
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
    const interval = setInterval(fetchData, 30_000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [api, tenantId]);

  if (loading) return <Progress />;
  if (error) return <Alert severity="error">{error}</Alert>;
  if (!overview) {
    return (
      <Alert severity="info">
        No Dynamic Alerting data found for tenant &quot;{tenantId || 'default'}&quot;.
        Verify the <code>dynamic-alerting.io/tenant</code> annotation
        in your catalog-info.yaml.
      </Alert>
    );
  }

  const statusText = overview.maintenance
    ? 'Maintenance'
    : overview.silentMode !== 'none'
      ? `Silent (${overview.silentMode})`
      : 'Normal';

  const StatusIcon = overview.maintenance
    ? StatusWarning
    : overview.silentMode !== 'none'
      ? StatusWarning
      : StatusOK;

  const columns = [
    { title: 'Metric', field: 'metric' },
    {
      title: 'Value',
      render: (row: ThresholdEntry) => row.value.toFixed(2),
    },
    {
      title: 'Severity',
      render: (row: ThresholdEntry) => (
        <Chip
          label={row.severity}
          color={row.severity === 'critical' ? 'secondary' : 'primary'}
          size="small"
          variant="outlined"
        />
      ),
    },
    { title: 'Component', field: 'component' },
  ];

  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <InfoCard title={`Tenant: ${overview.tenant}`}>
          <Grid container spacing={2}>
            <Grid item xs={3}>
              <Typography variant="subtitle2" color="textSecondary">
                Status
              </Typography>
              <StatusIcon>{statusText}</StatusIcon>
            </Grid>
            <Grid item xs={3}>
              <Typography variant="subtitle2" color="textSecondary">
                Metrics
              </Typography>
              <Typography variant="h6">{overview.metricCount}</Typography>
            </Grid>
            <Grid item xs={3}>
              <Typography variant="subtitle2" color="textSecondary">
                Custom
              </Typography>
              <Typography variant="h6">{overview.customCount}</Typography>
            </Grid>
            <Grid item xs={3}>
              <Typography variant="subtitle2" color="textSecondary">
                Disabled
              </Typography>
              <Typography variant="h6">{overview.disabledCount}</Typography>
            </Grid>
          </Grid>
        </InfoCard>
      </Grid>

      <Grid item xs={12}>
        <Table
          title="Thresholds"
          options={{ paging: true, pageSize: 10, search: true }}
          columns={columns}
          data={overview.thresholds}
        />
      </Grid>
    </Grid>
  );
}
