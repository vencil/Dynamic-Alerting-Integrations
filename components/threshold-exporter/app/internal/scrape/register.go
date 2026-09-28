package scrape

import "github.com/prometheus/client_golang/prometheus"

// Register installs what the exporter's /metrics registry holds: collector,
// then runtime (collectors that read no config — the exporter passes the Go
// runtime collector), then the config metrics, each with MustRegister in that
// order. The exporter's MetricsHandler and da-guard served-values both call
// it, so the two registries cannot drift apart.
func Register(reg prometheus.Registerer, collector prometheus.Collector, m *ConfigMetrics, runtime ...prometheus.Collector) {
	reg.MustRegister(collector)
	for _, r := range runtime {
		reg.MustRegister(r)
	}
	for _, x := range m.Collectors() {
		reg.MustRegister(x)
	}
}
