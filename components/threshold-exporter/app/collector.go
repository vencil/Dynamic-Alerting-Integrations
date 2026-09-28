package main

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/vencil/threshold-exporter/internal/scrape"
)

// ThresholdCollector implements prometheus.Collector.
// On each scrape, it resolves the current config and exposes:
//   - user_threshold gauge metrics (Scenario A: numeric thresholds + Phase 2B: dimensional)
//   - user_state_filter gauge metrics (Scenario C: state matching flags)
//
// Uses "unchecked collector" mode (empty Describe) to support dynamic label sets
// introduced in Phase 2B. This is the standard Prometheus Go client pattern when
// the label set varies per metric (e.g., custom dimensional labels from config).
//
// The collection itself is internal/scrape.Collector (#2115: da-guard
// served-values gathers the same code); this type wires it to the
// ConfigManager.
type ThresholdCollector struct {
	manager *ConfigManager
	inner   *scrape.Collector
}

func NewThresholdCollector(manager *ConfigManager) *ThresholdCollector {
	return &ThresholdCollector{
		manager: manager,
		inner:   scrape.NewCollector(manager),
	}
}

// Describe sends no descriptors — opts into unchecked collector mode.
// This allows Collect() to produce metrics with dynamic label sets
// (needed for Phase 2B dimensional metrics like {queue="tasks"}).
func (c *ThresholdCollector) Describe(ch chan<- *prometheus.Desc) {
	// Empty: unchecked collector mode for dynamic labels
}

// Collect implements prometheus.Collector: internal/scrape.Collector.
func (c *ThresholdCollector) Collect(ch chan<- prometheus.Metric) {
	c.inner.Collect(ch)
}

// MetricsHandler returns an HTTP handler that serves /metrics
// with both default Go metrics and our custom threshold collector.
func (c *ThresholdCollector) MetricsHandler() http.Handler {
	reg := prometheus.NewRegistry()
	// scrape.Register is the one wiring of this registry, shared with da-guard
	// served-values (#2115): the collector, the Go runtime collector (passed
	// here as the only runtime extra), then the config metrics.
	//
	// v2.7.0 Phase 4: the hierarchical scan / reload metrics are registered on
	// the same registry so /metrics serves them alongside everything else.
	// getConfigMetrics lazily instantiates the singleton so the first
	// metric increment from a scan goroutine does not race with
	// registration (registration itself takes a mutex internally).
	scrape.Register(reg, c, getConfigMetrics().set, collectors.NewGoCollector())

	return promhttp.HandlerFor(reg, promhttp.HandlerOpts{
		EnableOpenMetrics: false,
		// #2032: without ErrorLog a failed Gather (e.g. a duplicate series)
		// returned 500 with nothing in any log. promhttp logs once per failed
		// scrape, undeduplicated, as node_exporter's handler does; a
		// MultiError may span several lines and is left as is.
		ErrorLog: managerErrorLog{c.manager},
		// #2032: Registry makes promhttp register
		// promhttp_metric_handler_errors_total{cause="gathering"|"encoding"}
		// (two series, both pre-initialized to 0) on this exporter's registry.
		// A gathering error fails the whole scrape, so the counter is only
		// visible on the first successful scrape after recovery: it is for
		// after-the-fact tracing, not alerting. The live signal is
		// ThresholdExporterDown (up == 0, since Prometheus treats non-200 as
		// a scrape error). Only this handler sets it: scrape.Register stays
		// untouched so da-guard served-values keeps the same series set.
		Registry: reg,
		// ErrorHandling stays at the default HTTPErrorOnError on purpose
		// (#2031): ContinueOnError would serve a partial body in which the
		// value of a duplicated series silently flips between scrapes.
	})
}

// managerErrorLog adapts the manager's logger seam to promhttp.Logger. It
// looks the logger up on every call rather than at MetricsHandler() time:
// the handler is built once at startup, and a later SetLogger must still
// take effect. The manager is never nil here: Collect dereferences it
// before promhttp can call ErrorLog.
type managerErrorLog struct {
	manager *ConfigManager
}

func (l managerErrorLog) Println(v ...interface{}) {
	l.manager.getLogger().Println(v...)
}
