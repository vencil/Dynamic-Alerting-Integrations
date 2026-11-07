package scrape

// #3502: da_config_values_not_served's HELP lists its reasons from the
// reason table (config.ValueNotServedReasonsHelp), never by hand — the
// hand-written list had missed spelling_duplicate.

import (
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestValuesNotServedHelp_NamesEveryReason(t *testing.T) {
	ch := make(chan *prometheus.Desc, 1)
	NewConfigMetrics().ValuesNotServed.Describe(ch)
	desc := (<-ch).String()
	reasons := config.ValueNotServedReasons()
	if len(reasons) == 0 {
		t.Fatal("no reasons")
	}
	for _, r := range reasons {
		if !strings.Contains(desc, r+" (") {
			t.Errorf("HELP does not explain reason %q: %s", r, desc)
		}
	}
}
