package oauth2proxy

// ADR-027 §9 item 9, first half (the library side; CI runs the pinned image's
// --config-test on the same files): the oauth2-proxy config the tenant-api
// chart renders loads under oauth2-proxy's strict loader, every request
// reaches tenant-api, and the identity headers tenant-api receives are the
// session's — a client's own copy of each, however it spells the name, is
// gone (ADR-027 §2).
//
// The chart is rendered with helm. Without helm the test skips, unless
// OAUTH2_PROXY_REQUIRE_HELM=1 (CI) makes that a failure. With
// OAUTH2_PROXY_RENDER_OUT set, each variant's config file and the
// oauth2-proxy container's args and image are written there for the CI step
// that runs the image's --config-test.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/sessions"
	"gopkg.in/yaml.v3"
)

// chartVariant is one set of chart values the test renders.
type chartVariant struct {
	Name string
	Set  []string
	// Claims are the extra claim headers the variant injects.
	Claims map[string]string
}

var chartVariants = []chartVariant{
	// ADR-027 §2, §10: the api upstream is tenant-api's Unix socket, at a
	// path the chart fixes (there is no humanSocket key).
	{Name: "default"},
	{
		Name: "gitlab-claims-socket",
		Set: []string{
			"oauth2Proxy.provider=gitlab",
			"oauth2Proxy.oidcIssuerUrl=https://gitlab.example.com",
			"oauth2Proxy.scope=openid profile email",
			"oauth2Proxy.gitlabGroups={acme/sre}",
			"oauth2Proxy.claimHeaders.org=X-Auth-Request-Org",
			"oauth2Proxy.claimHeaders.region=X-Auth-Request-Region",
		},
		Claims: map[string]string{"org": "X-Auth-Request-Org", "region": "X-Auth-Request-Region"},
	},
}

// rendered is what the test takes out of one chart render.
type rendered struct {
	Config string   // the oauth2-proxy config file
	Image  string   // the oauth2-proxy container image
	Args   []string // the oauth2-proxy container args
	// EnvNames are the names of the oauth2-proxy container's env entries;
	// EnvFrom counts its envFrom sources.
	EnvNames []string
	EnvFrom  int
}

func renderChart(t *testing.T, v chartVariant) rendered {
	t.Helper()
	helm, err := exec.LookPath("helm")
	if err != nil {
		if os.Getenv("OAUTH2_PROXY_REQUIRE_HELM") == "1" {
			t.Fatal("helm is required (OAUTH2_PROXY_REQUIRE_HELM=1) but not on PATH")
		}
		t.Skip("helm not on PATH: the chart's oauth2-proxy config was not checked")
	}
	root := repoRoot(t)
	readChartInputs(t, root)
	args := []string{"template", "t", filepath.Join(root, "helm", "tenant-api"),
		"-f", filepath.Join(root, "tests", "helm", "tenant_api_render_baseline.yaml")}
	for _, s := range v.Set {
		args = append(args, "--set", s)
	}
	var stdout, stderr bytes.Buffer
	cmd := exec.Command(helm, args...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("helm template (%s): %v\n%s", v.Name, err, stderr.String())
	}
	var out rendered
	dec := yaml.NewDecoder(&stdout)
	for {
		var doc struct {
			Kind     string                `yaml:"kind"`
			Metadata struct{ Name string } `yaml:"metadata"`
			Data     map[string]string     `yaml:"data"`
			Spec     struct {
				Template struct {
					Spec struct {
						Containers []struct {
							Name  string   `yaml:"name"`
							Image string   `yaml:"image"`
							Args  []string `yaml:"args"`
							Env   []struct {
								Name string `yaml:"name"`
							} `yaml:"env"`
							EnvFrom []map[string]interface{} `yaml:"envFrom"`
						} `yaml:"containers"`
					} `yaml:"spec"`
				} `yaml:"template"`
			} `yaml:"spec"`
		}
		if err := dec.Decode(&doc); err != nil {
			break
		}
		switch {
		case doc.Kind == "ConfigMap" && strings.Contains(doc.Metadata.Name, "-oauth2-proxy-"):
			out.Config = doc.Data["oauth2-proxy.yaml"]
		case doc.Kind == "Deployment":
			for _, c := range doc.Spec.Template.Spec.Containers {
				if c.Name == "oauth2-proxy" {
					out.Image, out.Args = c.Image, c.Args
					for _, e := range c.Env {
						out.EnvNames = append(out.EnvNames, e.Name)
					}
					out.EnvFrom = len(c.EnvFrom)
				}
			}
		}
	}
	if out.Config == "" || out.Image == "" {
		t.Fatalf("render %s: no oauth2-proxy ConfigMap or container", v.Name)
	}
	if dir := os.Getenv("OAUTH2_PROXY_RENDER_OUT"); dir != "" {
		writeRenderOut(t, filepath.Join(dir, v.Name), out)
	}
	return out
}

// readChartInputs reads every file helm reads for the render. helm is a
// child process, so without this Go's test cache would not see a chart edit
// and `go test` could report a cached pass for a chart it never rendered.
func readChartInputs(t *testing.T, root string) {
	t.Helper()
	inputs := []string{filepath.Join(root, "tests", "helm", "tenant_api_render_baseline.yaml")}
	err := filepath.WalkDir(filepath.Join(root, "helm", "tenant-api"), func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			inputs = append(inputs, p)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range inputs {
		if _, err := os.ReadFile(p); err != nil {
			t.Fatal(err)
		}
	}
}

// writeRenderOut writes a variant for the CI --config-test step: the config
// file, the container args (JSON) and the image.
func writeRenderOut(t *testing.T, dir string, r rendered) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	args, err := json.Marshal(r.Args)
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{
		"oauth2-proxy.yaml": []byte(r.Config),
		"args.json":         args,
		"image":             []byte(r.Image + "\n"),
	} {
		if err := os.WriteFile(filepath.Join(dir, name), data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// adr-check: ADR-027/9.3f ADR-027/9.9b
func TestChartConfig(t *testing.T) {
	t.Parallel()
	for _, v := range chartVariants {
		t.Run(v.Name, func(t *testing.T) {
			t.Parallel()
			r := renderChart(t, v)
			alphaFlag := "--alpha-config="
			var cfgPath string
			for _, a := range r.Args {
				if strings.HasPrefix(a, alphaFlag) {
					cfgPath = strings.TrimPrefix(a, alphaFlag)
				}
			}
			if !strings.HasSuffix(cfgPath, "/oauth2-proxy.yaml") {
				t.Fatalf("oauth2-proxy args %v: no --alpha-config naming the ConfigMap's file", r.Args)
			}

			path := filepath.Join(t.TempDir(), "oauth2-proxy.yaml")
			if err := os.WriteFile(path, []byte(r.Config), 0o600); err != nil {
				t.Fatal(err)
			}
			opts, err := loadAlpha(path)
			if err != nil {
				t.Fatalf("oauth2-proxy refuses the chart's config: %v\n%s", err, r.Config)
			}
			// oauth2-proxy copies a claim into the session only when the
			// provider lists it in additionalClaims
			// (providers/provider_data.go extractAdditionalClaims); a claim
			// header for any other claim is injected empty. The session below
			// is built by hand, so this is what ties it to the provider.
			if len(opts.Providers) != 1 {
				t.Fatalf("providers = %d, want 1", len(opts.Providers))
			}
			listed := map[string]bool{}
			for _, c := range opts.Providers[0].AdditionalClaims {
				listed[c] = true
			}
			for claim := range v.Claims {
				if !listed[claim] {
					t.Errorf("claim %q has a header but the provider's additionalClaims %v do not list it: the header would be empty", claim, opts.Providers[0].AdditionalClaims)
				}
			}
			// ADR-027 §5, §10: /api/ → tenant-api's Unix socket, / → the
			// portal container on the pod's loopback; nothing else.
			ups := opts.UpstreamServers.Upstreams
			if len(ups) != 2 ||
				ups[0].ID != "api" || ups[0].Path != "/api/" || ups[0].URI != "unix:///run/ta-human/human.sock" ||
				ups[1].ID != "portal" || ups[1].Path != "/" || ups[1].URI != "http://127.0.0.1:8081" {
				t.Fatalf("upstreams = %+v, want api /api/ → unix:///run/ta-human/human.sock and portal / → http://127.0.0.1:8081", ups)
			}
			// ADR-027 §9 item 3: /api/ answers 401, not a redirect, without a
			// session — and only /api/: the portal paths still redirect.
			apiRoute := ""
			for _, a := range r.Args {
				if strings.HasPrefix(a, "--api-route=") {
					if apiRoute != "" {
						t.Fatalf("oauth2-proxy args %v: --api-route twice", r.Args)
					}
					apiRoute = strings.TrimPrefix(a, "--api-route=")
				}
				if strings.HasPrefix(a, "--skip-auth-route") || strings.HasPrefix(a, "--trusted-ip") ||
					strings.HasPrefix(a, "--skip-auth-preflight") || strings.HasPrefix(a, "--skip-auth-regex") {
					t.Errorf("oauth2-proxy arg %q: no request may skip login in the production chart (ADR-027 §2)", a)
				}
			}
			apiRE, err := regexp.Compile(apiRoute)
			if apiRoute == "" || err != nil {
				t.Fatalf("oauth2-proxy args %v: want one --api-route regexp (%v)", r.Args, err)
			}

			rec := &recorder{}
			liveUpstreams(t, &opts.UpstreamServers, rec)
			session := &sessions.SessionState{
				Email:            "alice@example.com",
				Groups:           []string{"ops", "dba"},
				AdditionalClaims: map[string]interface{}{"org": "ORG-1", "region": []interface{}{"eu", "us"}},
			}
			h := proxyFor(t, opts, session)

			// The chart routes every path of ADR-027 §10's table where the
			// ADR says, and --api-route covers exactly the api ones.
			_, routes := section10(t)
			for _, rt := range routes {
				w := httptest.NewRecorder()
				h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, rt.Path, nil))
				got := rec.take()
				if len(got) != 1 || got[0].Upstream != rt.Upstream {
					t.Errorf("GET %s reached %+v (status %d), want upstream %s", rt.Path, got, w.Code, rt.Upstream)
				}
				if p := strings.SplitN(rt.Path, "?", 2)[0]; apiRE.MatchString(p) != (rt.Upstream == "api") {
					t.Errorf("--api-route=%s on %s: matches %v, want it to match exactly the api upstream's paths", apiRoute, p, apiRE.MatchString(p))
				}
			}

			req := httptest.NewRequest(http.MethodGet, "/api/v1/me?x=1", nil)
			// A client's own copy of every header the config injects, in
			// every spelling oauth2-proxy treats as the same name. (A header
			// the config does not inject passes through; tenant-api does not
			// read it.)
			forged := []string{"X-Forwarded-Email", "X-Forwarded-Groups"}
			for _, h := range v.Claims {
				forged = append(forged, h)
			}
			for _, name := range forged {
				req.Header.Add(name, "mallory")
				req.Header[strings.ReplaceAll(strings.ToLower(name), "-", "_")] = []string{"mallory"}
			}
			h.ServeHTTP(httptest.NewRecorder(), req)
			got := rec.take()
			if len(got) != 1 || got[0].Upstream != "api" || got[0].URI != "/api/v1/me?x=1" {
				t.Fatalf("upstream requests = %+v, want one to api, path and query unchanged", got)
			}
			hdr := got[0].Header
			want := map[string]string{
				"X-Forwarded-Email":  "alice@example.com",
				"X-Forwarded-Groups": "ops,dba",
			}
			if _, ok := v.Claims["org"]; ok {
				want["X-Auth-Request-Org"] = "ORG-1"
				// A multi-value claim arrives comma-joined on one line.
				want["X-Auth-Request-Region"] = "eu,us"
			}
			for name, value := range want {
				if vals := hdr.Values(name); !reflect.DeepEqual(vals, []string{value}) {
					t.Errorf("upstream %s = %q, want exactly [%q]", name, vals, value)
				}
			}
			for name, vals := range hdr {
				for _, val := range vals {
					if strings.Contains(val, "mallory") {
						t.Errorf("the client's %s reached the upstream: %q", name, vals)
					}
				}
			}
		})
	}
}

// oauth2ProxyEnvAllowed is every environment variable the production chart may
// give the oauth2-proxy container. oauth2-proxy reads any OAUTH2_PROXY_* name
// as a flag, so an extra one could open a route without login
// (OAUTH2_PROXY_SKIP_AUTH_ROUTES, OAUTH2_PROXY_TRUSTED_IPS, ...) where the
// args check in TestChartConfig never looks. A new entry here is a decision.
var oauth2ProxyEnvAllowed = []string{"OAUTH2_PROXY_COOKIE_SECRET"}

// adr-check: ADR-027/9.3f
func TestChartOAuth2ProxyEnvIsAllowlisted(t *testing.T) {
	t.Parallel()
	for _, v := range chartVariants {
		t.Run(v.Name, func(t *testing.T) {
			t.Parallel()
			r := renderChart(t, v)
			if r.EnvFrom != 0 {
				t.Errorf("oauth2-proxy container has %d envFrom source(s): every variable it gets must be named here", r.EnvFrom)
			}
			if !reflect.DeepEqual(r.EnvNames, oauth2ProxyEnvAllowed) {
				t.Errorf("oauth2-proxy container env = %v, want exactly %v (ADR-027 §2: nothing may skip login)", r.EnvNames, oauth2ProxyEnvAllowed)
			}
		})
	}
}

// The image probe (image_config_test.sh) starts only the variants whose
// provider needs no IdP discovery; this is what lets its 401 stand for every
// variant: --api-route does not depend on the provider.
//
// adr-check: ADR-027/9.3b
func TestChartAPIRouteIsTheSameInEveryVariant(t *testing.T) {
	t.Parallel()
	routes := map[string]string{}
	for _, v := range chartVariants {
		var got []string
		for _, a := range renderChart(t, v).Args {
			if strings.HasPrefix(a, "--api-route=") {
				got = append(got, strings.TrimPrefix(a, "--api-route="))
			}
		}
		if len(got) != 1 {
			t.Fatalf("%s: --api-route args %v, want exactly one", v.Name, got)
		}
		routes[v.Name] = got[0]
	}
	first := routes[chartVariants[0].Name]
	for name, r := range routes {
		if r != first {
			t.Errorf("--api-route differs by variant: %v (%s has %q)", routes, name, r)
		}
	}
}
