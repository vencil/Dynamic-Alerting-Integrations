package handler

// The generator container on /metrics (ADR-036 §5, §6, #2903). The container
// has only a Unix socket, so tenant-api is its scrape path:
//
//   - tenant_api_policy_available — 1 when the generator container's status
//     endpoint says it can judge: the deployment policy file is usable,
//     amtool and da-guard are there and the generator is not older than the
//     policy requires (`ready`); 0 otherwise, and 0 when the status cannot be read
//     (the container is unreachable) — every content-changing write then
//     answers 503. Read live at scrape time, within generator.ScrapeTimeout.
//     The name is kept (an alert reads it); its meaning is ADR-036 §5's, no
//     longer "a usable or last good _domain_policy.yaml".
//   - tenant_api_generator_rejections_total{reason} — writes refused without
//     a verdict (GENERATOR_REJECTED, GENERATOR_BUSY), by reason.
//   - tenant_api_generator_scrape_success — whether the container's own
//     /metrics was read and appended below (its da_generator_* families,
//     da_generator_build_info among them) on this scrape.

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/generator"
)

var activeGenerator atomic.Pointer[generator.Gate]

// SetGeneratorSource installs the gate /metrics reads the generator families
// from. Called once at startup; nil (tests) renders them as "cannot read".
func SetGeneratorSource(g *generator.Gate) { activeGenerator.Store(g) }

func writePolicyAvailabilityMetrics(ctx context.Context, w io.Writer) {
	available := 0
	if g := activeGenerator.Load(); g != nil {
		sctx, cancel := context.WithTimeout(ctx, generator.ScrapeTimeout)
		st, err := g.Client().Status(sctx)
		cancel()
		switch {
		case err != nil:
			slog.Warn("generator status not read for tenant_api_policy_available", "error", err)
		case st.Ready:
			available = 1
		}
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_policy_available Whether the generator container can judge writes (1): its status endpoint reports the deployment policy file usable, amtool and da-guard present and the generator not older than the policy requires. 0 when it cannot, or when its status cannot be read: every content-changing write then answers 503 (POLICY_UNAVAILABLE / VALIDATOR_UNAVAILABLE). Alert on == 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_policy_available gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_policy_available %d\n", available)
}

// writeGeneratorMetrics renders the rejection counter and the scrape
// success gauge, then appends the container's own /metrics as it is. It is
// the LAST block of MetricsHandler: the appended text is the container's
// and is not parsed here.
func writeGeneratorMetrics(ctx context.Context, w io.Writer) {
	g := activeGenerator.Load()
	var counts map[string]int64
	if g != nil {
		counts = g.Rejections()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_generator_rejections_total Writes the generator container gave no verdict on (GENERATOR_REJECTED, GENERATOR_BUSY), by reason; nothing of them was written.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_generator_rejections_total counter\n")
	for _, reason := range generator.RejectionReasons {
		_, _ = fmt.Fprintf(w, "tenant_api_generator_rejections_total{reason=%q} %d\n", reason, counts[reason])
	}
	var body []byte
	if g != nil {
		sctx, cancel := context.WithTimeout(ctx, generator.ScrapeTimeout)
		b, err := g.Client().Metrics(sctx)
		cancel()
		if err != nil {
			slog.Warn("generator /metrics not read", "error", err)
		} else {
			body = b
		}
	}
	ok := 0
	if body != nil {
		ok = 1
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_generator_scrape_success Whether the generator container's /metrics was read on this scrape and appended (its da_generator_* families). 0: not appended.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_generator_scrape_success gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_generator_scrape_success %d\n", ok)
	if body != nil {
		_, _ = w.Write(body)
		if len(body) > 0 && body[len(body)-1] != '\n' {
			_, _ = io.WriteString(w, "\n")
		}
	}
}
