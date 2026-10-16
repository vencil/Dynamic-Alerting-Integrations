package main

// docs/api/README.md and README.en.md show what /health, /ready and
// /api/v1/config answer. This test holds those parts against the handlers:
//
//   - §2 and §3: the `| 情況 | 狀態碼 | 內文 |` table. Each section's set of
//     (status, body) rows must equal what the handler answers across the
//     states in apiProbeScenarios, both ways. For /ready that includes a
//     failed reload after a good one, which the README says stays 200.
//   - §4: the yaml blocks (first line `# <file name>`) are written to an empty
//     directory, loaded with NewConfigManager + Load, and the `text` block
//     must be what configViewHandler answers, with ?at= taken from its
//     `Resolve at:` line (and named in the section's curl command). The
//     `Last reload:` value only has to be an RFC 3339 time; inside each
//     blank-line-separated section after the first, line order is not
//     compared (the handler ranges over maps).
//
// A row or block this test cannot read fails it.
//
// NOT COVERED: the prose; which tenant a metric line sits under when the
// example has more than one tenant (lines are compared as a multiset per
// section). Status codes and media types of these routes are also checked by
// openapi_spec_parity_test.go.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

var (
	apiBodyCellRe   = regexp.MustCompile("^`([^`]+)`$")
	apiResolveAtRe  = regexp.MustCompile(`^Resolve at:\s+(\S+) \(overridden\)$`)
	apiLastReloadRe = regexp.MustCompile(`^Last reload:\s+(\S+)$`)
)

// apiDocSection returns the lines from the heading that starts with prefix
// up to the next `## ` heading, and the 1-based line number of the heading.
func apiDocSection(t *testing.T, file, prefix string) ([]string, int) {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "api", file))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(b), "\n")
	for i, l := range lines {
		if !strings.HasPrefix(l, prefix) {
			continue
		}
		end := i + 1
		for end < len(lines) && !strings.HasPrefix(lines[end], "## ") {
			end++
		}
		return lines[i+1 : end], i + 1
	}
	t.Fatalf("%s has no section starting %q", file, prefix)
	return nil, 0
}

// apiServe runs h once and returns "<status> <body without trailing newline>".
func apiServe(h http.Handler, target string) string {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))
	return strconv.Itoa(rec.Code) + " " + strings.TrimSuffix(rec.Body.String(), "\n")
}

// apiProbeScenarios answers, per section heading, every (status, body) the
// handler gives across the states the section describes.
func apiProbeScenarios(t *testing.T) map[string][]string {
	t.Helper()
	notLoaded := NewConfigManager(filepath.Join(t.TempDir(), "missing.yaml"))

	// Loaded once, then a reload that fails to parse.
	file := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(file, []byte("defaults:\n  m: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	reloaded := NewConfigManager(file)
	if err := reloaded.Load(); err != nil {
		t.Fatalf("first load: %v", err)
	}
	if err := os.WriteFile(file, []byte("defaults: [\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := reloaded.Load(); err == nil {
		t.Fatal("reload of an unparseable file succeeded; the failed-reload scenario needs a failing Load")
	}

	return map[string][]string{
		"## 2. GET /health": {apiServe(http.HandlerFunc(healthHandler), "/health")},
		"## 3. GET /ready": {
			apiServe(readyHandler(notLoaded), "/ready"),
			apiServe(readyHandler(reloaded), "/ready"),
		},
	}
}

func TestAPIReadmeProbeTables(t *testing.T) {
	scenarios := apiProbeScenarios(t)
	for _, file := range []string{"README.md", "README.en.md"} {
		for heading, served := range scenarios {
			t.Run(file+"/"+heading, func(t *testing.T) {
				lines, first := apiDocSection(t, file, heading)
				rows, lineNos := opsTableRows(lines, first+1)
				if len(rows) == 0 {
					t.Fatalf("%s: no table under %q", file, heading)
				}
				doc := map[string]bool{}
				for i, cells := range rows {
					if len(cells) != 3 {
						t.Fatalf("%s line %d: expected 情況 | 狀態碼 | 內文, got %d cells", file, lineNos[i], len(cells))
					}
					if _, err := strconv.Atoi(cells[1]); err != nil {
						t.Fatalf("%s line %d: status %q is not a number", file, lineNos[i], cells[1])
					}
					m := apiBodyCellRe.FindStringSubmatch(cells[2])
					if m == nil {
						t.Fatalf("%s line %d: body %q must be one backticked string", file, lineNos[i], cells[2])
					}
					doc[cells[1]+" "+m[1]] = true
				}
				want := map[string]bool{}
				for _, s := range served {
					want[s] = true
					if !doc[s] {
						t.Errorf("%s %s: the handler answers %q, which the table does not list", file, heading, s)
					}
				}
				for s := range doc {
					if !want[s] {
						t.Errorf("%s %s: the table lists %q, which the handler does not answer", file, heading, s)
					}
				}
			})
		}
	}
}

// apiFences returns the fenced blocks in lines as (info string, body).
func apiFences(t *testing.T, file string, lines []string, first int) (langs []string, bodies [][]string) {
	t.Helper()
	open := -1
	for i, l := range lines {
		if !strings.HasPrefix(l, "```") {
			continue
		}
		if open < 0 {
			open = i
			continue
		}
		langs = append(langs, strings.TrimPrefix(lines[open], "```"))
		bodies = append(bodies, lines[open+1:i])
		open = -1
	}
	if open >= 0 {
		t.Fatalf("%s line %d: unclosed code fence", file, first+1+open)
	}
	return langs, bodies
}

// apiSections splits text at blank lines.
func apiSections(text string) [][]string {
	var out [][]string
	for _, block := range strings.Split(strings.TrimRight(text, "\n"), "\n\n") {
		out = append(out, strings.Split(block, "\n"))
	}
	return out
}

func TestAPIReadmeConfigViewExample(t *testing.T) {
	for _, file := range []string{"README.md", "README.en.md"} {
		t.Run(file, func(t *testing.T) {
			lines, first := apiDocSection(t, file, "## 4. GET /api/v1/config")
			langs, bodies := apiFences(t, file, lines, first)

			dir := t.TempDir()
			var expected []string
			nYAML := 0
			for i, lang := range langs {
				switch lang {
				case "yaml":
					body := bodies[i]
					if len(body) == 0 || !strings.HasPrefix(body[0], "# ") {
						t.Fatalf("%s: a yaml block in §4 must start with `# <file name>`", file)
					}
					name := strings.TrimPrefix(body[0], "# ")
					if filepath.Base(name) != name {
						t.Fatalf("%s: yaml block names %q; use a bare file name", file, name)
					}
					content := strings.Join(body[1:], "\n") + "\n"
					if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
						t.Fatal(err)
					}
					nYAML++
				case "text":
					if expected != nil {
						t.Fatalf("%s: §4 has more than one text block", file)
					}
					expected = bodies[i]
				}
			}
			if nYAML == 0 || expected == nil {
				t.Fatalf("%s: §4 needs the yaml input files and one text block of output (got %d yaml, text=%v)", file, nYAML, expected != nil)
			}

			target := "/api/v1/config"
			for _, l := range expected {
				if m := apiResolveAtRe.FindStringSubmatch(l); m != nil {
					target += "?at=" + m[1]
					if !strings.Contains(strings.Join(lines, "\n"), "?at="+m[1]) {
						t.Errorf("%s: the output resolves at %s, but no command in §4 sends ?at=%s", file, m[1], m[1])
					}
				}
			}

			m := NewConfigManager(dir)
			if err := m.Load(); err != nil {
				t.Fatalf("%s: loading the §4 yaml files: %v", file, err)
			}
			rec := httptest.NewRecorder()
			configViewHandler(m).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))

			got := apiSections(rec.Body.String())
			want := apiSections(strings.Join(expected, "\n"))
			if len(got) != len(want) {
				t.Fatalf("%s: §4 output has %d sections, the handler answers %d:\n%s", file, len(want), len(got), rec.Body.String())
			}
			for s := range want {
				g, w := got[s], want[s]
				if s == 0 {
					if len(g) != len(w) {
						t.Fatalf("%s: §4 header has %d lines, the handler answers %d:\n%s", file, len(w), len(g), rec.Body.String())
					}
					for i := range w {
						if wm := apiLastReloadRe.FindStringSubmatch(w[i]); wm != nil {
							if _, err := time.Parse(time.RFC3339, wm[1]); err != nil {
								t.Errorf("%s: §4 `Last reload:` %q is not RFC 3339", file, wm[1])
							}
							if !apiLastReloadRe.MatchString(g[i]) {
								t.Errorf("%s: §4 line %q, the handler answers %q", file, w[i], g[i])
							}
							continue
						}
						if g[i] != w[i] {
							t.Errorf("%s: §4 line %q, the handler answers %q", file, w[i], g[i])
						}
					}
					continue
				}
				if g[0] != w[0] {
					t.Errorf("%s: §4 section %q, the handler answers %q", file, w[0], g[0])
				}
				gs, ws := append([]string(nil), g[1:]...), append([]string(nil), w[1:]...)
				sort.Strings(gs)
				sort.Strings(ws)
				if strings.Join(gs, "\n") != strings.Join(ws, "\n") {
					t.Errorf("%s: §4 section %q lists\n%s\nthe handler answers\n%s", file, w[0], strings.Join(ws, "\n"), strings.Join(gs, "\n"))
				}
			}
		})
	}
}
