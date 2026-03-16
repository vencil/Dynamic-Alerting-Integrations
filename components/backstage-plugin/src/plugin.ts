import {
  createPlugin,
  createRoutableExtension,
  createApiFactory,
  configApiRef,
} from '@backstage/core-plugin-api';
import {
  dynamicAlertingApiRef,
  PrometheusClient,
} from './api/PrometheusClient';

/**
 * The Dynamic Alerting Backstage plugin (§5.13).
 *
 * Registers the Prometheus API client and provides a routable page
 * that can be mounted in the Backstage app or as an Entity tab.
 */
export const dynamicAlertingPlugin = createPlugin({
  id: 'dynamic-alerting',
  apis: [
    createApiFactory({
      api: dynamicAlertingApiRef,
      deps: { configApi: configApiRef },
      factory: ({ configApi }) => {
        const baseUrl =
          configApi.getOptionalString(
            'dynamicAlerting.prometheus.baseUrl',
          ) || '';
        const proxyPath =
          configApi.getOptionalString(
            'dynamicAlerting.prometheus.proxyPath',
          ) || '/api/proxy/prometheus';

        return new PrometheusClient({ baseUrl, proxyPath });
      },
    }),
  ],
});

/**
 * Routable extension — the main Dynamic Alerting page.
 * Mount in App.tsx: <Route path="/dynamic-alerting" element={<DynamicAlertingPage />} />
 */
export const DynamicAlertingPage = dynamicAlertingPlugin.provide(
  createRoutableExtension({
    name: 'DynamicAlertingPage',
    component: () =>
      import('./components/DynamicAlertingPage').then(
        m => m.DynamicAlertingPage,
      ),
    mountPoint: { id: 'dynamic-alerting' } as any,
  }),
);
