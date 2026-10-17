//go:build unix

package handler

// #3062: planPut's order rows that need a tenant file which is not a regular
// file (a FIFO, syscall.Mkfifo) — see tenant_put_plan_parity_test.go.

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// parityFIFO replaces {id}'s committed file with a FIFO.
func parityFIFO(t *testing.T, dir string) {
	t.Helper()
	if err := os.Remove(filepath.Join(dir, parityIn+".yaml")); err != nil {
		t.Fatal(err)
	}
	mkfifoTenantFile(t, dir, parityIn)
}

func TestPutPlan_OrderRowsOnANonRegularFile(t *testing.T) {
	t.Parallel()
	runParityCases(t, []parityCase{
		// The write scope on {id} as the file holds it (403) before the
		// file that is not regular (409 not_regular_file): with the
		// metadata axis enforced, a caller scoped to an environment may
		// not write a tenant whose metadata cannot be read. /diff gives
		// that caller the masked preview's sequence (#1560), whose next
		// step is the 409.
		{name: "id_write_scope_and_not_regular", file: "tenants:\n" + paritySec(parityIn, "warning"), groups: "prod-ops", metaEnforce: true,
			setup: parityFIFO, body: parityBodyIn, want: http.StatusForbidden, wantText: "ADR-027",
			maskedDiff: true, maskedRefusal: CodeTenantConfigNotLoadable},
		// The file that is not regular (409) before the body's size (413).
		{name: "not_regular_and_body_too_large", file: "tenants:\n" + paritySec(parityIn, "warning"), groups: "in-team",
			setup: parityFIFO, maxBody: 64,
			body: "tenants:\n" + paritySec(parityIn, strings.Repeat("x", 128)), want: http.StatusConflict,
			wantText: "not_regular_file"},
	})
}
