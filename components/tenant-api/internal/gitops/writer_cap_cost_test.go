package gitops

// #1722: the ADVERSARIAL half of the caps' justification.
//
// ⛔ WHY THIS FILE EXISTS IN TWO HALVES. The cost claims behind
// TA_MAX_TENANT_DOC_BYTES were originally written into prose — a doc comment, the
// CHANGELOG, a helm values comment — as wall-clock seconds. Four blind-review
// rounds found them stale, mutually inconsistent, or simply mis-transcribed, and
// nothing mechanical could ever have caught that: wall-clock is machine- and
// load-dependent, so it cannot be asserted in CI without becoming flaky.
//
// The split is therefore deliberate:
//
//   - What IS mechanical — key density per byte, which is pure counting — is a
//     TEST, and it is the only form of the "a flow mapping is the expensive
//     shape" claim that survives anywhere.
//   - What is NOT mechanical — how long that costs — is a BENCHMARK, opt-in via
//     `go test -bench`. Nobody has to trust a number in a document: they run it
//     on the machine they care about.
//
// No wall-clock figure appears in shipping prose any more. If you want one, run:
//
//	go test ./internal/gitops/ -run '^$' -bench BenchmarkValidateAtCap -benchtime 1x

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// capShapedBody fills a document to just under the cap in one of the two shapes
// an author can choose. flow packs more keys into the same bytes; the cap is in
// bytes, so that difference is exactly what an adversary optimises.
func capShapedBody(tenantID string, flow bool) string {
	key := func(i int) string {
		const alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
		s, n := "", i
		for {
			s = string(alpha[n%36]) + s
			n /= 36
			if n == 0 {
				break
			}
		}
		return "q" + s
	}
	var b strings.Builder
	if flow {
		fmt.Fprintf(&b, "tenants:\n  %s: {", tenantID)
		for i := 0; ; i++ {
			frag := key(i) + ": 1"
			if i > 0 {
				frag = "," + frag
			}
			if int64(b.Len()+len(frag)+2) > maxTenantDocBytes {
				break
			}
			b.WriteString(frag)
		}
		b.WriteString("}\n")
		return b.String()
	}
	fmt.Fprintf(&b, "tenants:\n  %s:\n", tenantID)
	for i := 0; ; i++ {
		line := "    " + key(i) + ": 1\n"
		if int64(b.Len()+len(line)) > maxTenantDocBytes {
			break
		}
		b.WriteString(line)
	}
	return b.String()
}

// BenchmarkValidateAtCap is the OPT-IN source of truth for "how expensive is a
// document sitting right at the cap". It is a benchmark and not a test on
// purpose: an assertion on wall-clock would be flaky in CI, and a number in a
// document rots. Run it on the machine whose latency you care about.
//
// ⚠️ It measures validate() alone. The batch paths pay this more than once per
// operation and pay mergePatchYAML on top, so a batch is dearer than any figure
// here — see DefaultMaxBatchBodyBytes.
func BenchmarkValidateAtCap(b *testing.B) {
	dir := b.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("defaults:\n  container_cpu: 80\n"), 0o644); err != nil {
		b.Fatal(err)
	}
	path := filepath.Join(dir, "db-a.yaml")
	if err := os.WriteFile(path, []byte("tenants:\n  db-a:\n    container_cpu: 70\n"), 0o644); err != nil {
		b.Fatal(err)
	}
	for _, shape := range []struct {
		name string
		flow bool
	}{{"block", false}, {"flow", true}} {
		body := capShapedBody("db-a", shape.flow)
		b.Run(fmt.Sprintf("%s/%dbytes/%dkeys", shape.name, len(body), strings.Count(body, ": 1")),
			func(b *testing.B) {
				b.ResetTimer() // the fixture above must not be inside the timed region
				for i := 0; i < b.N; i++ {
					if errs, _ := validate(dir, "db-a", path, body); len(errs) == 0 {
						b.Fatal("fixture stopped being rejected — measuring the wrong path")
					}
				}
			})
	}
}
