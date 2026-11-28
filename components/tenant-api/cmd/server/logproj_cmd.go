package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/vencil/tenant-api/internal/federation/logprojection"
	"github.com/vencil/tenant-api/internal/staterestore"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// logProjCmdUsage is printed for `tenant-api log-projection` misuse.
const logProjCmdUsage = `usage: tenant-api log-projection render --config-dir DIR --state-from FILE|DIR|- --template FILE [--out FILE]
       tenant-api log-projection run --template FILE --out FILE [--confd-configmap NAME [--confd-namespace NS] | --config-dir DIR]
                                     [--state-store configmap|dir ...] [--interval 30s] [--metrics-addr :9599]
                                     [--vector-metrics-url URL] [--reload-metric vector_reloaded_total] [--reload-timeout 30s]

Writes Vector's tenant log projection config (ADR-021, #4212): the template
(helm/vector renders it) with the projected tenants filled in. A tenant is
projected when tenant-api's ledger holds an AccountID for it and its conf.d
config sets _log_projection: enable.

render reads a conf.d directory and a state backup (as state show --from
takes: a ConfigMap YAML/JSON file, a state directory, or - for stdin) once,
and writes the config to --out or standard output. Exit 0 written; 1 an
input cannot be used (nothing is written); 2 usage.

run keeps --out equal to what the inputs say, every --interval: conf.d from
the ConfigMap --confd-configmap (read through the Kubernetes API) or the
directory --config-dir, the records from the state store (the server's
--state-* flags). A run that cannot use an input writes nothing and the last
file stays in force; only when no file exists yet and no input was ever usable
is the template written with no tenant in it. With --vector-metrics-url, each
write waits up to --reload-timeout for Vector's reload counter to grow.
Metrics: http://<--metrics-addr>/metrics.`

func logProjSubcommand(argv []string) bool {
	return len(argv) > 1 && argv[1] == "log-projection"
}

// runLogProjCmd runs `tenant-api log-projection ...` and returns the exit code.
func runLogProjCmd(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, logProjCmdUsage)
		return 2
	}
	switch args[0] {
	case "render":
		return runLogProjRender(args[1:], stdin, stdout, stderr)
	case "run":
		return runLogProjRun(args[1:], stderr)
	}
	fmt.Fprintln(stderr, logProjCmdUsage)
	return 2
}

func runLogProjRender(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("tenant-api log-projection render", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprintln(stderr, logProjCmdUsage) }
	confDir := fs.String("config-dir", "", "conf.d directory")
	stateFrom := fs.String("state-from", "", "State backup: a ConfigMap YAML/JSON file, a state directory, or - for stdin")
	tplPath := fs.String("template", "", "The template helm/vector renders")
	out := fs.String("out", "", "Where to write the config (default: standard output)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if !checkTAEnv(os.Environ(), stderr) {
		return 2
	}
	if fs.NArg() > 0 || *confDir == "" || *stateFrom == "" || *tplPath == "" {
		fmt.Fprintln(stderr, logProjCmdUsage)
		return 2
	}
	tpl, err := os.ReadFile(*tplPath)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection render: --template: %v\n", err)
		return 1
	}
	state, err := staterestore.Load(*stateFrom, stdin)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection render: --state-from: %v\n", err)
		return 1
	}
	c, err := loadConfd(*confDir)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection render: --config-dir: %v\n", err)
		return 1
	}
	res, err := logprojection.Compute(state, c)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection render: %v\n", err)
		return 1
	}
	for _, f := range res.Findings {
		fmt.Fprintf(stderr, "log-projection render: tenant %s not projected: %s\n", f.TenantID, f.Reason)
	}
	text, err := logprojection.Render(tpl, res.Projections)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection render: %v\n", err)
		return 1
	}
	if *out == "" {
		if _, err := stdout.Write(text); err != nil {
			fmt.Fprintf(stderr, "log-projection render: write output: %v\n", err)
			return 1
		}
		return 0
	}
	if err := os.WriteFile(*out, text, 0o644); err != nil {
		fmt.Fprintf(stderr, "log-projection render: --out: %v\n", err)
		return 1
	}
	return 0
}

// loadConfd loads a conf.d directory the exporter's way. A file the loader
// could not parse fails the load: a tenant whose file is unreadable would
// otherwise read as "not projected".
func loadConfd(dir string) (*cfg.ThresholdConfig, error) {
	c, parseFailed, err := cfg.LoadDir(dir, nil)
	if err != nil {
		return nil, err
	}
	if len(parseFailed) > 0 {
		return nil, fmt.Errorf("unparseable: %s", strings.Join(parseFailed, ", "))
	}
	return c, nil
}

func runLogProjRun(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("tenant-api log-projection run", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprintln(stderr, logProjCmdUsage) }
	sf := registerStateFlags(fs)
	tplPath := fs.String("template", "", "The template helm/vector renders")
	out := fs.String("out", "", "The config file Vector watches")
	cmName := fs.String("confd-configmap", "", "ConfigMap holding conf.d (instead of --config-dir)")
	cmNS := fs.String("confd-namespace", "", "Namespace of --confd-configmap (default: the pod's own)")
	interval := fs.Duration("interval", 30*time.Second, "How often to read the inputs")
	metricsAddr := fs.String("metrics-addr", ":9599", "Listen address of /metrics (empty: none)")
	vectorURL := fs.String("vector-metrics-url", "", "Vector's Prometheus page, to confirm each reload (empty: not confirmed)")
	reloadMetric := fs.String("reload-metric", "vector_reloaded_total", "Vector's reload counter on --vector-metrics-url")
	reloadTimeout := fs.Duration("reload-timeout", 30*time.Second, "How long to wait for a reload after a write")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if !checkTAEnv(os.Environ(), stderr) {
		return 2
	}
	if fs.NArg() > 0 || *tplPath == "" || *out == "" || *interval <= 0 {
		fmt.Fprintln(stderr, logProjCmdUsage)
		return 2
	}
	tpl, err := os.ReadFile(*tplPath)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection run: --template: %v\n", err)
		return 1
	}
	confd, err := confdSource(*cmName, *cmNS, sf.ConfigDir)
	if err != nil {
		fmt.Fprintf(stderr, "log-projection run: %v\n", err)
		return 1
	}
	r := &logprojection.Runner{
		Source: logprojection.Source{
			State: func(context.Context) (map[string][]byte, error) { return readStateSnapshot(*sf) },
			Confd: confd,
		},
		Template:      tpl,
		Out:           *out,
		ReloadTimeout: *reloadTimeout,
		Logger:        slog.Default(),
	}
	if *vectorURL != "" {
		r.Reloads = logprojection.ReloadCounter(&http.Client{Timeout: 5 * time.Second}, *vectorURL, *reloadMetric)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *metricsAddr != "" {
		mux := http.NewServeMux()
		mux.Handle("/metrics", r.MetricsHandler())
		srv := &http.Server{Addr: *metricsAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
		go func() {
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				slog.Error("log projection: metrics server stopped", "error", err)
			}
		}()
		defer func() { _ = srv.Close() }()
	}
	r.Run(ctx, *interval)
	return 0
}

// confdSource reads conf.d from the named ConfigMap (each data key one file
// of a flat tree, as `make configmap-assemble` builds it) or from dir.
func confdSource(cmName, cmNS, dir string) (func(context.Context) (*cfg.ThresholdConfig, error), error) {
	if cmName == "" {
		if dir == "" {
			return nil, fmt.Errorf("--confd-configmap or --config-dir is required")
		}
		return func(context.Context) (*cfg.ThresholdConfig, error) { return loadConfd(dir) }, nil
	}
	client, err := buildInClusterClientset()
	if err != nil {
		return nil, fmt.Errorf("--confd-configmap: %w", err)
	}
	ns := cmNS
	if ns == "" {
		if ns, err = inClusterNamespace(); err != nil {
			return nil, fmt.Errorf("--confd-configmap: resolve namespace (or set --confd-namespace): %w", err)
		}
	}
	return func(ctx context.Context) (*cfg.ThresholdConfig, error) {
		cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		cm, err := client.CoreV1().ConfigMaps(ns).Get(cctx, cmName, metav1.GetOptions{})
		if err != nil {
			return nil, fmt.Errorf("get ConfigMap %s/%s: %w", ns, cmName, err)
		}
		return loadConfdFiles(cm.Data)
	}, nil
}

// loadConfdFiles writes files (name → text) into a fresh directory and loads
// it. A name that is not one plain file name is refused: the API server
// already allows only [-._a-zA-Z0-9] in a key, so one is a sign the data is
// not a conf.d ConfigMap.
func loadConfdFiles(files map[string]string) (*cfg.ThresholdConfig, error) {
	dir, err := os.MkdirTemp("", "log-projection-confd-")
	if err != nil {
		return nil, err
	}
	defer func() { _ = os.RemoveAll(dir) }()
	for name, text := range files {
		if name == "" || name == "." || name == ".." || filepath.Base(name) != name {
			return nil, fmt.Errorf("ConfigMap key %q is not a file name", name)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o600); err != nil {
			return nil, err
		}
	}
	return loadConfd(dir)
}
