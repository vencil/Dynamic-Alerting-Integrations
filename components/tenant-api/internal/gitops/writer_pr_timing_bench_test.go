package gitops

// Benchmarks for the #1339 split: what moving validate()'s stateful half past
// the in-lock fetch actually costs, and where.
//
// These exist because the cost argument for the split was written down as bare
// numbers from throwaway scripts — which by this repo's own standard is "could
// not measure", not "measured and fine". Anything the commit message or
// CHANGELOG claims about the cost of this change has to be re-runnable here:
//
//	go test ./internal/gitops/ -run '^$' -bench 'Validate|WritePR' -benchtime 10x
//
// ⚠️ The absolute numbers are machine- and forge-dependent. The WritePR ones
// below talk to a LOCAL bare remote, so they are a LOWER bound on the git-bound
// costs — a real forge only makes the fetch and push slower. What the
// benchmarks are for is the RATIOS between the three request classes, which is
// what the argument actually rests on.

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// benchConfD writes a conf.d of the requested shape and returns (dir, body).
func benchConfD(tb testing.TB, nDefaults, nTenants, nKeys int) (string, string) {
	tb.Helper()
	dir := tb.TempDir()
	var defaults, tenants strings.Builder
	defaults.WriteString("defaults:\n")
	for i := 0; i < nDefaults; i++ {
		fmt.Fprintf(&defaults, "  metric_%03d: %d\n", i, 50+i%40)
	}
	tenants.WriteString("tenants:\n")
	for tn := 0; tn < nTenants; tn++ {
		fmt.Fprintf(&tenants, "  db-%02d:\n", tn)
		for i := 0; i < nKeys; i++ {
			fmt.Fprintf(&tenants, "    metric_%03d: %d\n", i, 60+i%30)
		}
	}
	body := tenants.String()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults.String()), 0o644); err != nil {
		tb.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "db-00.yaml"), []byte(body), 0o644); err != nil {
		tb.Fatal(err)
	}
	return dir, body
}

// BenchmarkValidate is the work that moved INSIDE w.mu — before this change
// validate() ran entirely outside the lock, so its whole cost is the added
// lock-hold per accepted write. BenchmarkValidateStateless is the part that
// still runs outside it at step 1 (and is therefore also the part step 3c
// redundantly repeats; see the comment there for why that is left alone).
func BenchmarkValidate(b *testing.B) {
	for _, shape := range []struct {
		name                        string
		nDefaults, nTenants, nKeys int
	}{
		{"OneTenantFile", 40, 1, 12},
		{"LargeFlatFile", 200, 50, 40},
	} {
		dir, body := benchConfD(b, shape.nDefaults, shape.nTenants, shape.nKeys)
		fp := filepath.Join(dir, "db-00.yaml")
		b.Run(shape.name+"/Full", func(b *testing.B) {
			b.ReportMetric(float64(len(body)), "bodyB")
			for i := 0; i < b.N; i++ {
				if errs, _ := validate(dir, "db-00", fp, body); len(errs) > 0 {
					b.Fatalf("fixture is invalid: %v", errs)
				}
			}
		})
		b.Run(shape.name+"/StatelessOnly", func(b *testing.B) {
			for i := 0; i < b.N; i++ {
				if _, errs, _ := validateStateless("db-00", body); len(errs) > 0 {
					b.Fatalf("fixture is invalid: %v", errs)
				}
			}
		})
	}
}

// benchPod builds a real repo + real bare remote whose local base is one commit
// behind origin — the shape every request class below is measured against.
// ⛔ Callers MUST b.ResetTimer() after this. Building the fixture is two clones,
// two commits and two pushes; left inside the timed region it is amortised over
// b.N and at a low -benchtime it DOMINATES — the first version of this file
// reported 23.65 ms/op for a body that never touches git at all (the true cost
// is ~25 µs). A benchmark that measures its own setup is the same class of
// mistake as a test that passes for the wrong reason.
func benchPod(b *testing.B) *Writer {
	b.Helper()
	podDir, authorDir := stalePodAndOrigin(b, flatDeclaringOnlyA, "defaults:\n  cpu_usage: 80\n")
	advanceOrigin(b, authorDir, "move origin ahead of the pod",
		map[string]string{"_views.yaml": "views: {}\n"})
	return NewWriter(podDir, podDir)
}

// BenchmarkWritePR_Rejected/Malformed is the class the stateless pre-flight
// still refuses for free. .../StatefulInvalid is the class this change moved
// onto the git path — a body that is well-formed but wrong about conf.d (here:
// a metric key no _defaults.yaml declares), which now costs a fetch, a branch,
// and a rollback. .../Accepted is the cost an authenticated caller can impose
// anyway, and the one the other two should be read against.
func BenchmarkWritePR(b *testing.B) {
	b.Run("Rejected/Malformed", func(b *testing.B) {
		w := benchPod(b)
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			if _, err := w.WritePR(context.Background(), "db-a", "x@example.com",
				"tenants:\n  db-a:\n   : ::\n"); err == nil {
				b.Fatal("expected a validation error")
			}
		}
	})
	b.Run("Rejected/StatefulInvalid", func(b *testing.B) {
		w := benchPod(b)
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			if _, err := w.WritePR(context.Background(), "db-a", "x@example.com",
				"tenants:\n  db-a:\n    definitely_not_a_metric: 5\n"); err == nil {
				b.Fatal("expected a validation error")
			}
		}
	})
	b.Run("Accepted", func(b *testing.B) {
		w := benchPod(b)
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			// ⚠️ A DISTINCT tenant per iteration on purpose: the feature branch
			// name is `tenant-api/<id>/<second>`, so two accepted writes for the
			// same tenant inside one second collide on the branch name and the
			// second one fails. That is a real defect, tracked separately; here
			// it would just make the benchmark measure the failure path.
			id := fmt.Sprintf("t%d", i)
			if _, err := w.WritePR(context.Background(), id, "x@example.com",
				fmt.Sprintf("tenants:\n  %s:\n    cpu_usage: 70\n", id)); err != nil {
				b.Fatalf("accepted path: %v", err)
			}
		}
	})
}
