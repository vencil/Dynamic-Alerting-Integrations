// Package oauth2proxy loads oauth2-proxy structured config files with
// oauth2-proxy's own code, at the version the tenant-api chart deploys
// (ADR-027 §9 item 9).
package oauth2proxy

import (
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"

	"github.com/justinas/alice"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/middleware"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/options"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/sessions"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/app/pagewriter"
	oamw "github.com/oauth2-proxy/oauth2-proxy/v7/pkg/middleware"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/upstream"
)

// repoRoot is the repository root, found from this file's location.
func repoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate the test file")
	}
	return filepath.Join(filepath.Dir(file), "..", "..")
}

// loadAlpha loads path the way oauth2-proxy's main does with --alpha-config
// (loadYamlOptions, then EnsureDefaults): environment references expanded,
// field names matched ignoring case, unknown fields refused.
func loadAlpha(path string) (*options.Options, error) {
	opts := options.NewOptions()
	alpha := options.NewAlphaOptions(opts)
	if err := options.LoadYAML(path, alpha); err != nil {
		return nil, err
	}
	alpha.MergeOptionsWithDefaults(opts)
	opts.EnsureDefaults()
	return opts, nil
}

// received is one request an upstream got.
type received struct {
	Upstream string
	URI      string // path and query as the upstream saw them
	Header   http.Header
}

// recorder collects what every upstream received.
type recorder struct {
	mu  sync.Mutex
	got []received
}

func (r *recorder) handler(id string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		r.mu.Lock()
		r.got = append(r.got, received{Upstream: id, URI: req.URL.RequestURI(), Header: req.Header.Clone()})
		r.mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	})
}

func (r *recorder) take() []received {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.got
	r.got = nil
	return out
}

// liveUpstreams replaces every upstream URI in cfg with a real listener of
// the same scheme (a Unix socket for unix://, an HTTP server for http://)
// whose handler records what it receives under the upstream's id. Paths and
// every other upstream setting stay as the file wrote them.
func liveUpstreams(t *testing.T, cfg *options.UpstreamConfig, rec *recorder) {
	t.Helper()
	// A short directory: a Unix socket path is limited to ~108 bytes.
	dir, err := os.MkdirTemp("", "o2p")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	for i := range cfg.Upstreams {
		up := &cfg.Upstreams[i]
		u, err := url.Parse(up.URI)
		if err != nil {
			t.Fatalf("upstream %q: %v", up.ID, err)
		}
		switch u.Scheme {
		case "unix":
			sock := filepath.Join(dir, up.ID+".sock")
			ln, err := net.Listen("unix", sock)
			if err != nil {
				t.Fatal(err)
			}
			srv := &http.Server{Handler: rec.handler(up.ID)}
			go func() { _ = srv.Serve(ln) }()
			t.Cleanup(func() { _ = srv.Close() })
			up.URI = "unix://" + sock
		case "http":
			srv := httptest.NewServer(rec.handler(up.ID))
			t.Cleanup(srv.Close)
			up.URI = srv.URL
		default:
			t.Fatalf("upstream %q: scheme %q is not one this harness serves", up.ID, u.Scheme)
		}
	}
}

// proxyFor builds oauth2-proxy's request path past authentication for opts:
// the request scope carrying session, the request-header injector (which
// strips the client's copies first) and the upstream router.
func proxyFor(t *testing.T, opts *options.Options, session *sessions.SessionState) http.Handler {
	t.Helper()
	writer, err := pagewriter.NewWriter(pagewriter.Opts{})
	if err != nil {
		t.Fatal(err)
	}
	router, err := upstream.NewProxy(opts.UpstreamServers, nil, writer)
	if err != nil {
		t.Fatalf("upstream.NewProxy: %v", err)
	}
	inject, err := oamw.NewRequestHeaderInjector(opts.InjectRequestHeaders)
	if err != nil {
		t.Fatalf("NewRequestHeaderInjector: %v", err)
	}
	scope := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			next.ServeHTTP(w, middleware.AddRequestScope(req, &middleware.RequestScope{Session: session}))
		})
	}
	return alice.New(scope, inject).Then(router)
}
