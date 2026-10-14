package main

// docs/api/README.md and README.en.md open with a 服務規格 / Service
// Specifications table: listen port, the http.Server timeouts, the header
// cap, and the /metrics format. The values live in buildServer, the -listen
// default, and the promhttp options; this test keeps both tables and the
// /metrics media type in docs/api/threshold-exporter-openapi.yaml honest
// against them.
//
// Source side is the running object where one exists:
//   - timeouts and header cap: the fields of buildServer("", nil). Every
//     field it sets other than Addr and Handler must have a row, so a new
//     setting cannot go unlisted;
//   - listen port: the -listen default, read with go/ast (opsSourceFlagsAndEnv);
//   - metric format: the Content-Type /metrics answers to a request that asks
//     for OpenMetrics.
//
// README side: every table row under the heading, `| 項目 | 值 |`.
// A row whose label is not in svcSpecRows, or whose value does not parse,
// fails the test, and so does a label missing from the table. The format
// row's value must hold one backticked media type that the Content-Type
// starts with (promhttp appends charset and escaping parameters).
//
// NOT COVERED: the prose around the table; the LISTEN_ADDR override (README
// §3.2 has it); the openapi file's other schemas and descriptions.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// svcSpecRow ties a table label (zh, en) to what it states.
type svcSpecRow struct {
	zh, en string
	field  string // http.Server field; "" for listen and format
}

var svcSpecRows = []svcSpecRow{
	{"監聽埠", "Listen Port", ""},
	{"讀取逾時", "Read Timeout", "ReadTimeout"},
	{"讀取標頭逾時", "Read Header Timeout", "ReadHeaderTimeout"},
	{"寫入逾時", "Write Timeout", "WriteTimeout"},
	{"空閒逾時", "Idle Timeout", "IdleTimeout"},
	{"最大標頭大小", "Max Header Size", "MaxHeaderBytes"},
	{"指標格式", "Metric Format", ""},
}

var (
	svcPortRe     = regexp.MustCompile(`^([0-9]+)$`)
	svcDurationRe = regexp.MustCompile(`^([0-9]+) (?:秒|seconds?)$`)
	svcBytesRe    = regexp.MustCompile(`^([0-9]+) (?:字節|bytes)$`)
	svcMediaRe    = regexp.MustCompile("`([^`]+)`")
)

// svcSourceSpec returns the expected value of each row, keyed by zh label,
// as the README writes it once parsed: port and bytes as decimal strings,
// durations as time.Duration strings, format as the served Content-Type.
func svcSourceSpec(t *testing.T) map[string]string {
	t.Helper()
	want := map[string]string{}

	s := buildServer("", nil)
	v := reflect.ValueOf(s).Elem()
	covered := map[string]bool{"Addr": true, "Handler": true}
	for _, r := range svcSpecRows {
		if r.field == "" {
			continue
		}
		covered[r.field] = true
		f := v.FieldByName(r.field)
		if !f.IsValid() {
			t.Fatalf("http.Server has no field %s", r.field)
		}
		switch x := f.Interface().(type) {
		case time.Duration:
			want[r.zh] = x.String()
		case int:
			want[r.zh] = strconv.Itoa(x)
		default:
			t.Fatalf("http.Server.%s is %T; extend svcSourceSpec", r.field, x)
		}
	}
	for i := 0; i < v.NumField(); i++ {
		name := v.Type().Field(i).Name
		if !covered[name] && !v.Field(i).IsZero() {
			t.Errorf("buildServer sets http.Server.%s, which the 服務規格 table does not list; add a row to svcSpecRows and both READMEs", name)
		}
	}

	fset, files := opsParsePackage(t)
	flags, _ := opsSourceFlagsAndEnv(t, fset, files)
	listen, ok := flags["listen"]
	if !ok {
		t.Fatal("no -listen flag in source")
	}
	addr, err := strconv.Unquote(listen.def)
	if err != nil {
		addr = listen.def
	}
	port := addr[strings.LastIndex(addr, ":")+1:]
	if port == "" || port == addr {
		t.Fatalf("-listen default %q has no :port", addr)
	}
	want["監聽埠"] = port

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	req.Header.Set("Accept", "application/openmetrics-text; version=1.0.0")
	NewThresholdCollector(newTestManager(&ThresholdConfig{})).MetricsHandler().ServeHTTP(rec, req)
	ct := rec.Header().Get("Content-Type")
	if rec.Code != http.StatusOK || ct == "" {
		t.Fatalf("/metrics answered %d with Content-Type %q", rec.Code, ct)
	}
	want["指標格式"] = ct
	return want
}

// svcDocTable reads the first table under heading in docs/api/<file>.
func svcDocTable(t *testing.T, file, heading string) ([][]string, []int) {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "api", file))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(b), "\n")
	for i, l := range lines {
		if l != heading {
			continue
		}
		end := i + 1
		for end < len(lines) && !strings.HasPrefix(lines[end], "#") {
			end++
		}
		rows, lineNos := opsTableRows(lines[i+1:end], i+2)
		if len(rows) == 0 {
			t.Fatalf("%s: no table under %q", file, heading)
		}
		return rows, lineNos
	}
	t.Fatalf("%s has no %q section", file, heading)
	return nil, nil
}

func TestServiceSpecReadmeParity(t *testing.T) {
	want := svcSourceSpec(t)
	for _, doc := range []struct {
		file, heading string
		label         func(svcSpecRow) string
	}{
		{"README.md", "## 服務規格", func(r svcSpecRow) string { return r.zh }},
		{"README.en.md", "## Service Specifications", func(r svcSpecRow) string { return r.en }},
	} {
		t.Run(doc.file, func(t *testing.T) {
			byLabel := map[string]svcSpecRow{}
			for _, r := range svcSpecRows {
				byLabel[doc.label(r)] = r
			}
			rows, lineNos := svcDocTable(t, doc.file, doc.heading)
			seen := map[string]bool{}
			for i, cells := range rows {
				ln := lineNos[i]
				if len(cells) != 2 {
					t.Fatalf("%s line %d: expected 項目 | 值, got %d cells", doc.file, ln, len(cells))
				}
				r, ok := byLabel[cells[0]]
				if !ok {
					t.Fatalf("%s line %d: unknown row %q; add it to svcSpecRows", doc.file, ln, cells[0])
				}
				seen[r.zh] = true
				exp := want[r.zh]
				var got string
				switch {
				case r.zh == "監聽埠":
					m := svcPortRe.FindStringSubmatch(cells[1])
					if m == nil {
						t.Fatalf("%s line %d: %q must be a bare port number", doc.file, ln, cells[1])
					}
					got = m[1]
				case r.zh == "指標格式":
					ms := svcMediaRe.FindAllStringSubmatch(cells[1], -1)
					if len(ms) != 1 {
						t.Fatalf("%s line %d: %q must hold exactly one backticked media type", doc.file, ln, cells[1])
					}
					if !strings.HasPrefix(exp, ms[0][1]) {
						t.Errorf("%s line %d: format `%s`, but /metrics answers %q", doc.file, ln, ms[0][1], exp)
					}
					continue
				case r.field == "MaxHeaderBytes":
					m := svcBytesRe.FindStringSubmatch(cells[1])
					if m == nil {
						t.Fatalf("%s line %d: %q must be `<n> 字節` / `<n> bytes`", doc.file, ln, cells[1])
					}
					got = m[1]
				default:
					m := svcDurationRe.FindStringSubmatch(cells[1])
					if m == nil {
						t.Fatalf("%s line %d: %q must be `<n> 秒` / `<n> seconds`", doc.file, ln, cells[1])
					}
					n, _ := strconv.Atoi(m[1])
					got = (time.Duration(n) * time.Second).String()
				}
				if got != exp {
					t.Errorf("%s line %d: %s is %s, source says %s", doc.file, ln, cells[0], got, exp)
				}
			}
			for _, r := range svcSpecRows {
				if !seen[r.zh] {
					t.Errorf("%s: no %q row", doc.file, doc.label(r))
				}
			}
		})
	}
}

// The openapi file declares the /metrics 200 media type a second time.
func TestServiceSpecOpenAPIMetricsMediaType(t *testing.T) {
	want := svcSourceSpec(t)["指標格式"]
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "api", "threshold-exporter-openapi.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var spec struct {
		Paths map[string]map[string]struct {
			Responses map[string]struct {
				Content map[string]any `yaml:"content"`
			} `yaml:"responses"`
		} `yaml:"paths"`
	}
	if err := yaml.Unmarshal(b, &spec); err != nil {
		t.Fatal(err)
	}
	content := spec.Paths["/metrics"]["get"].Responses["200"].Content
	if len(content) == 0 {
		t.Fatal("openapi: /metrics get 200 has no content")
	}
	for mt := range content {
		if !strings.HasPrefix(want, mt) {
			t.Errorf("openapi: /metrics 200 declares %q, but /metrics answers %q", mt, want)
		}
	}
}
