package logprojection

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// Source reads the inputs of one run. Each read is the whole input as it is
// now; an error means "unusable this time", never "empty".
type Source struct {
	// State is tenant-api's records (the ledger, the visibility selections).
	State func(ctx context.Context) (map[string][]byte, error)
	// Confd is conf.d, loaded by the exporter's loader.
	Confd func(ctx context.Context) (*cfg.ThresholdConfig, error)
}

// The `source` label of a read failure.
const (
	SourceState = "state"
	SourceConfd = "confd"
	// SourceAccounts: the runner's own record of the AccountIDs it wrote
	// (AccountsPath) cannot be read.
	SourceAccounts = "accounts"
)

// The `reason` label of a refused result.
const (
	RefusedAccountChanged = "account_changed"
	RefusedRender         = "render"
)

// Runner keeps Vector's tenant projection config (Out) equal to what the
// inputs say, one run at a time. A run that cannot use its inputs, or whose
// result contradicts an AccountID it wrote before (Conflicts), writes
// nothing: the last result stays in force. Only a Runner that never wrote
// anything, and finds no file, writes the template with no tenant in it (the
// platform copy only).
//
// Every tenant → AccountID it ever wrote is kept in AccountsPath, beside Out,
// so a restarted container (the emptyDir outlives it) still refuses a moved
// id; the record only grows, and is written before Out.
type Runner struct {
	Source   Source
	Template []byte
	Out      string
	// Reloads, when set, reads Vector's reload counter; after a write the
	// runner waits up to ReloadTimeout for it to grow.
	Reloads       func(ctx context.Context) (float64, error)
	ReloadTimeout time.Duration
	Logger        *slog.Logger
	Now           func() time.Time

	mu      sync.Mutex
	seen    map[string]uint32
	haveRun bool
	m       metrics
}

type metrics struct {
	lastSuccess      float64
	projected        int
	findings         map[string]int
	readFailures     map[string]float64
	refused          map[string]float64
	writes           float64
	reloadUnconfirmd float64
	emptyFallback    float64
}

func (r *Runner) logger() *slog.Logger {
	if r.Logger != nil {
		return r.Logger
	}
	return slog.Default()
}

func (r *Runner) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// Once does one run. The error says why nothing was written; the run is
// recorded in the metrics either way.
func (r *Runner) Once(ctx context.Context) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.m.findings == nil {
		r.m.findings = map[string]int{}
		r.m.readFailures = map[string]float64{}
		r.m.refused = map[string]float64{}
	}
	err := r.once(ctx)
	r.haveRun = true
	return err
}

// AccountsPath is where the runner keeps every tenant → AccountID it wrote:
// Out with ".accounts" appended, an extension no config reader takes.
func (r *Runner) AccountsPath() string { return r.Out + ".accounts" }

func (r *Runner) loadSeen() error {
	if r.seen != nil {
		return nil
	}
	data, err := os.ReadFile(r.AccountsPath())
	if errors.Is(err, os.ErrNotExist) {
		r.seen = map[string]uint32{}
		return nil
	}
	if err != nil {
		return err
	}
	seen := map[string]uint32{}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&seen); err != nil {
		return fmt.Errorf("%s: %w", r.AccountsPath(), err)
	}
	r.seen = seen
	return nil
}

func (r *Runner) once(ctx context.Context) error {
	if err := r.loadSeen(); err != nil {
		return r.readFailed(SourceAccounts, err)
	}
	state, err := r.Source.State(ctx)
	if err != nil {
		return r.readFailed(SourceState, err)
	}
	c, err := r.Source.Confd(ctx)
	if err != nil {
		return r.readFailed(SourceConfd, err)
	}
	res, err := Compute(state, c)
	if err != nil {
		return r.readFailed(SourceState, err)
	}
	if bad := Conflicts(r.seen, res.Projections); len(bad) > 0 {
		r.m.refused[RefusedAccountChanged]++
		return fmt.Errorf("refused: %s; the ledger never moves or reuses an id, so this read is kept out",
			strings.Join(bad, "; "))
	}
	out, err := Render(r.Template, res.Projections)
	if err != nil {
		r.m.refused[RefusedRender]++
		return fmt.Errorf("refused: %w", err)
	}
	for _, f := range res.Findings {
		r.logger().Warn("log projection: tenant not projected", "tenant", f.TenantID, "reason", f.Reason)
	}
	r.m.findings = map[string]int{}
	for _, f := range res.Findings {
		r.m.findings[f.Reason]++
	}
	if err := r.remember(res.Projections); err != nil {
		return fmt.Errorf("write %s: %w", r.AccountsPath(), err)
	}
	if cur, rerr := os.ReadFile(r.Out); rerr == nil && bytes.Equal(cur, out) {
		r.success(res.Projections)
		return nil
	}
	if err := r.write(ctx, out); err != nil {
		return err
	}
	r.success(res.Projections)
	return nil
}

func (r *Runner) readFailed(source string, err error) error {
	r.m.readFailures[source]++
	if !r.haveRun {
		if _, statErr := os.Stat(r.Out); errors.Is(statErr, os.ErrNotExist) {
			// Never written, no file: Vector must still start, with the
			// platform copy only.
			if out, rerr := Render(r.Template, nil); rerr == nil {
				if werr := writeAtomic(r.Out, out); werr == nil {
					r.m.emptyFallback = 1
					r.m.writes++
				}
			}
		}
	}
	return fmt.Errorf("%s: %w", source, err)
}

// remember adds ps to the record of every AccountID written, before Out is
// written: a crash between the two leaves the record ahead, never behind.
func (r *Runner) remember(ps []Projection) error {
	grown := false
	for _, p := range ps {
		if _, ok := r.seen[p.TenantID]; !ok {
			r.seen[p.TenantID] = p.AccountID
			grown = true
		}
	}
	if !grown {
		if _, err := os.Stat(r.AccountsPath()); err == nil || len(r.seen) == 0 {
			return nil
		}
	}
	data, err := json.Marshal(r.seen)
	if err != nil {
		return err
	}
	return writeAtomic(r.AccountsPath(), append(data, '\n'))
}

func (r *Runner) success(ps []Projection) {
	r.m.projected = len(ps)
	r.m.lastSuccess = float64(r.now().UnixNano()) / 1e9
	r.m.emptyFallback = 0
}

// write replaces Out and, with Reloads set, waits for Vector to load it.
// Vector keeps running its old config when a new one is invalid, and counts
// no failed reload (measured on 0.61.0, #4212), so a reload counter that does
// not grow is the only sign the new file did not take.
func (r *Runner) write(ctx context.Context, out []byte) error {
	var before float64
	var haveBefore bool
	if r.Reloads != nil {
		if v, err := r.Reloads(ctx); err == nil {
			before, haveBefore = v, true
		}
	}
	if err := writeAtomic(r.Out, out); err != nil {
		return fmt.Errorf("write %s: %w", r.Out, err)
	}
	r.m.writes++
	if r.Reloads == nil {
		return nil
	}
	r.m.reloadUnconfirmd = 1
	if !haveBefore {
		return fmt.Errorf("reload not confirmed: Vector's reload counter could not be read before the write")
	}
	deadline := r.now().Add(r.ReloadTimeout)
	for {
		if v, err := r.Reloads(ctx); err == nil && v > before {
			r.m.reloadUnconfirmd = 0
			return nil
		}
		if !r.now().Before(deadline) {
			return fmt.Errorf("reload not confirmed: Vector's reload counter did not grow within %s", r.ReloadTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// writeAtomic replaces path through a synced temp file in its directory and
// a rename: Vector, watching path, never reads a half-written file. The temp
// name has no extension, so a reader of the directory by extension skips it.
func writeAtomic(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".tmp-")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op after the rename
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Run runs Once every interval until ctx ends.
func (r *Runner) Run(ctx context.Context, interval time.Duration) {
	for {
		if err := r.Once(ctx); err != nil {
			r.logger().Error("log projection: run kept the last result", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
		}
	}
}

// metric names, served by WriteMetrics.
const (
	metricPrefix = "da_log_projection_"
)

// WriteMetrics writes the runner's metrics in the Prometheus text format.
func (r *Runner) WriteMetrics(w io.Writer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	b := &strings.Builder{}
	gauge := func(name, help string, v float64) {
		fmt.Fprintf(b, "# HELP %s%s %s\n# TYPE %s%s gauge\n%s%s %s\n",
			metricPrefix, name, help, metricPrefix, name, metricPrefix, name, fmtFloat(v))
	}
	counter := func(name, help, label string, vals map[string]float64, keys []string) {
		fmt.Fprintf(b, "# HELP %s%s %s\n# TYPE %s%s counter\n", metricPrefix, name, help, metricPrefix, name)
		for _, k := range keys {
			fmt.Fprintf(b, "%s%s{%s=%q} %s\n", metricPrefix, name, label, k, fmtFloat(vals[k]))
		}
	}
	gauge("last_success_timestamp_seconds", "Unix time of the last run whose result is in force (0: none yet).", r.m.lastSuccess)
	gauge("projected_tenants", "Tenants in the result in force.", float64(r.m.projected))
	fmt.Fprintf(b, "# HELP %sunprojected_tenants Tenants the last usable run did not project although their flag asks for it, by reason.\n# TYPE %sunprojected_tenants gauge\n", metricPrefix, metricPrefix)
	for _, k := range []string{ReasonFlagUnread, ReasonNoAccount, ReasonTenantIDInvalid} {
		fmt.Fprintf(b, "%sunprojected_tenants{reason=%q} %d\n", metricPrefix, k, r.m.findings[k])
	}
	counter("read_failures_total", "Runs that could not use an input; the last result stayed in force.", "source",
		r.m.readFailures, []string{SourceState, SourceConfd, SourceAccounts})
	counter("refused_total", "Runs whose result was refused; the last result stayed in force.", "reason",
		r.m.refused, []string{RefusedAccountChanged, RefusedRender})
	fmt.Fprintf(b, "# HELP %swrites_total Times the config file was replaced.\n# TYPE %swrites_total counter\n%swrites_total %s\n",
		metricPrefix, metricPrefix, metricPrefix, fmtFloat(r.m.writes))
	gauge("reload_unconfirmed", "1 when Vector's reload counter did not grow after the last write: Vector may still run the config before it.", r.m.reloadUnconfirmd)
	gauge("empty_fallback", "1 while the file in force is the template with no tenant, written because no input was ever usable.", r.m.emptyFallback)
	_, _ = io.WriteString(w, b.String())
}

func fmtFloat(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }

// MetricsHandler serves WriteMetrics.
func (r *Runner) MetricsHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		r.WriteMetrics(w)
	})
}

// ReloadCounter returns a Reloads that sums the samples of metric (all its
// label sets) on the Prometheus text page at url. A page without the metric
// is an error: a counter that is not there cannot confirm a reload.
func ReloadCounter(client *http.Client, url, metric string) func(ctx context.Context) (float64, error) {
	return func(ctx context.Context) (float64, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return 0, err
		}
		resp, err := client.Do(req)
		if err != nil {
			return 0, err
		}
		defer func() { _ = resp.Body.Close() }()
		if resp.StatusCode != http.StatusOK {
			return 0, fmt.Errorf("%s: HTTP %d", url, resp.StatusCode)
		}
		return SumMetric(resp.Body, metric)
	}
}

// SumMetric sums the samples of metric on a Prometheus text page.
func SumMetric(page io.Reader, metric string) (float64, error) {
	sc := bufio.NewScanner(page)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	var sum float64
	found := false
	for sc.Scan() {
		line := sc.Text()
		if line == "" || line[0] == '#' {
			continue
		}
		name, rest := line, ""
		if i := strings.IndexAny(line, "{ "); i >= 0 {
			name, rest = line[:i], line[i:]
		}
		if name != metric {
			continue
		}
		if strings.HasPrefix(rest, "{") {
			j := strings.LastIndex(rest, "}")
			if j < 0 {
				return 0, fmt.Errorf("metric %s: %q: unterminated labels", metric, line)
			}
			rest = rest[j+1:]
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		v, err := strconv.ParseFloat(fields[0], 64)
		if err != nil {
			return 0, fmt.Errorf("metric %s: %q: %w", metric, line, err)
		}
		sum += v
		found = true
	}
	if err := sc.Err(); err != nil {
		return 0, err
	}
	if !found {
		return 0, fmt.Errorf("metric %s not on the page", metric)
	}
	return sum, nil
}
