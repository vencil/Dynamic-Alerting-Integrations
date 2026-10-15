package handler

// #2983: a `_routing`-prefixed tenant key the route generator does not read —
// a misspelling (`_routing_profle`), a platform key (`_routing_defaults`) or
// the flat `_routing_receiver_type` — is refused in a batch patch, as the
// generator's `--validate` refuses it. The `_routing` reserved PREFIX used to
// let all of them through, and the batch patch wrote `_routing_receiver_type`
// into the tenant file, where nothing renders it. (A PUT body carrying one is
// the generator's to refuse, #2903 — except the flat `_routing_receiver_type`,
// a 400 on every write: TestGeneratorGate_FlatReceiverTypeIs400.)

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// A batch patch writing the flat `_routing_receiver_type` (or a misspelled
// routing key) is refused as a body violation — 400 INVALID_BODY naming the
// key, whatever its value (`pagerduty` used to be written). Nothing is
// written.
func TestBatchTenants_UnknownRoutingKeyRefused(t *testing.T) {
	for _, patch := range []string{
		`{"_routing_receiver_type":"slack"}`, `{"_routing_receiver_type":"pagerduty"}`, `{"_routing_profle":"domain-ok"}`,
	} {
		t.Run(patch, func(t *testing.T) {
			configDir := seedGitTree(t, offTree(offDisabledOK))
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
				bytes.NewBufferString(`{"operations":[{"tenant_id":"t-off","patch":`+patch+`}]}`))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
			var bad ErrorResponse
			if err := json.Unmarshal(w.Body.Bytes(), &bad); err != nil {
				t.Fatal(err)
			}
			if w.Code != http.StatusBadRequest || bad.Code != CodeInvalidBody || len(bad.Violations) != 1 ||
				!strings.HasPrefix(bad.Violations[0].Field, `operations[0].patch["_routing`) ||
				!strings.Contains(bad.Violations[0].Reason, "not a key the route generator reads") {
				t.Fatalf("status %d, response %+v: want 400 %s naming the key", w.Code, bad, CodeInvalidBody)
			}
			b, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml"))
			if strings.Contains(string(b), "_routing_receiver_type") || strings.Contains(string(b), "_routing_profle") {
				t.Errorf("refused batch wrote the key:\n%s", b)
			}
		})
	}
}

// A tenant file that already carries the flat key (tenant-api used to write
// it): every other write is refused until it is gone, and a batch `unset`
// removes it — then the write lands.
func TestBatchTenants_LegacyRoutingReceiverTypeOnDisk(t *testing.T) {
	const legacy = offDisabledOK + "    _routing_receiver_type: pagerduty\n"
	const cpuOp = `[{"tenant_id":"t-off","patch":{"cpu_usage_percent":"70"}}]`
	for _, mode := range bothModes {
		t.Run(string(mode), func(t *testing.T) {
			results, _ := runOffBatch(t, mode, legacy, cpuOp)
			if len(results) != 1 || results[0].Status != "error" ||
				!strings.Contains(results[0].Message, "_routing_receiver_type") ||
				!strings.Contains(results[0].Message, "remove it") {
				t.Fatalf("results = %+v, want the write refused naming the key", results)
			}
			results, file := runOffBatch(t, mode, legacy,
				`[{"tenant_id":"t-off","unset":["_routing_receiver_type"]},{"tenant_id":"t-off","patch":{"cpu_usage_percent":"70"}}]`)
			if got := statuses(results); got != okStatus(mode)+","+okStatus(mode) {
				t.Fatalf("statuses = %s: %+v", got, results)
			}
			if file != "" && (strings.Contains(file, "_routing_receiver_type") || !strings.Contains(file, `cpu_usage_percent: "70"`)) {
				t.Errorf("unset + patch must drop the key and land the write:\n%s", file)
			}
		})
	}
}
