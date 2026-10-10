package handler

// #1977: GET /tenants/{id}/effective on a 1000-tenant tree (4 domains × 5
// regions × 50 tenants, a `_defaults.yaml` at every level, a root profile
// file), as the server answers it.
//
//   - Cold: Deps without a Writer — a cold walk per request and, before
//     #1977, the exporter's build of the whole tree (every tenant file
//     re-read and re-parsed).
//   - Warm: the production wiring (Deps.Writer). Every file is past the
//     exporter's mtime guard and one request has run before the timer, so
//     each measured request walks on the prior and reads only the tenant's
//     own file, the root `_` files and its chain.

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

func buildEffectiveBenchTree(b *testing.B) string {
	b.Helper()
	root := b.TempDir()
	write := func(p, body string) {
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			b.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			b.Fatal(err)
		}
	}
	keys := []string{"mysql_connections", "mysql_threads_running", "container_cpu", "container_memory",
		"oracle_sessions_active", "redis_memory", "es_index_store_size_bytes", "pg_connections"}
	defaults := func(v int, quoted bool) string {
		var sb strings.Builder
		sb.WriteString("defaults:\n")
		for i, k := range keys {
			if quoted {
				fmt.Fprintf(&sb, "  %s: \"%d\"\n", k, v+i)
			} else {
				fmt.Fprintf(&sb, "  %s: %d\n", k, v+i)
			}
		}
		return sb.String()
	}
	write(filepath.Join(root, "_defaults.yaml"), defaults(80, false))
	write(filepath.Join(root, "_profiles.yaml"), "profiles:\n  small:\n    mysql_connections: \"50\"\n")
	n := 0
	for d := 0; d < 4; d++ {
		dom := filepath.Join(root, fmt.Sprintf("dom%d", d))
		write(filepath.Join(dom, "_defaults.yaml"), defaults(70+d, true))
		for r := 0; r < 5; r++ {
			reg := filepath.Join(dom, fmt.Sprintf("reg%d", r))
			write(filepath.Join(reg, "_defaults.yaml"), defaults(60+r, true))
			for t := 0; t < 50; t++ {
				id := fmt.Sprintf("t-%04d", n)
				write(filepath.Join(reg, id+".yaml"),
					fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"%d\"\n    container_cpu: \"%d\"\n", id, 40+t, 50+t))
				n++
			}
		}
	}
	past := time.Now().Add(-time.Hour)
	if err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		return os.Chtimes(p, past, past)
	}); err != nil {
		b.Fatal(err)
	}
	return root
}

func benchGetTenantEffective(b *testing.B, withWriter bool) {
	root := buildEffectiveBenchTree(b)
	d := &Deps{ConfigDir: root}
	if withWriter {
		d.Writer = gitops.NewWriter(root, "")
	}
	h := GetTenantEffective(d)
	get := func() {
		req := newRequestWithChiParam("GET", "/api/v1/tenants/t-0500/effective", "id", "t-0500", nil)
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusOK {
			b.Fatalf("status %d: %s", w.Code, w.Body.String())
		}
	}
	get() // the Writer's first walk is cold; it becomes the prior
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		get()
	}
}

func BenchmarkGetTenantEffective_Hier1k_Cold(b *testing.B) { benchGetTenantEffective(b, false) }
func BenchmarkGetTenantEffective_Hier1k_Warm(b *testing.B) { benchGetTenantEffective(b, true) }
