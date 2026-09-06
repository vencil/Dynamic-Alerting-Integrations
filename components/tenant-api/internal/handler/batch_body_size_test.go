package handler

// #1722: the BATCH endpoints' request-body budget.
//
// Before this, POST /api/v1/tenants/batch read its body with a bare
// json.NewDecoder(r.Body) — d.MaxBody() reaches only the handlers that call
// readLimitedBody, and there is no body-size middleware, so this endpoint had
// no cap at all. It is also the endpoint where an oversize body costs the most:
// WritePRBatch takes the single-writer token BEFORE its pre-flight and
// validates every op twice inside it.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"os"
	"path/filepath"
	"regexp"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
	"gopkg.in/yaml.v3"
)

// batchBodyOfSize builds a syntactically valid batch request of at least n bytes.
func batchBodyOfSize(n int) string {
	var b strings.Builder
	b.WriteString(`{"operations":[`)
	for i := 0; b.Len() < n; i++ {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `{"tenant_id":"db-a","patch":{"k%06d":"1"}}`, i)
	}
	b.WriteString(`]}`)
	return b.String()
}

func TestBatchTenants_RejectsOversizeBody(t *testing.T) {
	t.Parallel()
	body := batchBodyOfSize(int(DefaultMaxBatchBodyBytes) + 1024)
	if int64(len(body)) <= DefaultMaxBatchBodyBytes {
		t.Fatalf("fixture is %d bytes, not over the %d-byte cap", len(body), DefaultMaxBatchBodyBytes)
	}

	configDir := t.TempDir()
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})

	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	// ⛔ 413, not 400: the body is well-formed, it is too big, and the client's
	// remedy differs. Before this change an oversize body was TRUNCATED by
	// io.LimitReader-style reads and surfaced as "invalid JSON: unexpected EOF",
	// which names neither the size nor the limit.
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413; body: %s", w.Code, w.Body.String())
	}
	var resp map[string]any
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	msg, _ := resp["error"].(string)
	if msg == "" {
		msg = fmt.Sprint(resp)
	}
	if strings.Contains(msg, "invalid JSON") {
		t.Errorf("oversize body surfaced as a JSON parse error — the cap "+
			"truncated instead of refusing: %s", msg)
	}
	for _, want := range []string{strconv.FormatInt(DefaultMaxBatchBodyBytes, 10), "TA_MAX_BATCH_BODY_BYTES"} {
		if !strings.Contains(msg, want) {
			t.Errorf("refusal does not name %q, so an operator cannot act on it: %s", want, msg)
		}
	}
}

// TestBatchTenants_UnderTheBudgetReachesPerOpValidation is the
// no-false-rejection guard: a body under the cap must reach the normal per-op
// path.
//
// ⛔ ITS FIRST VERSION NEVER GOT THERE. It built the fixture purely by byte
// count, which produced ~2,900 operations — over the documented 1000-op
// ceiling — so the request died at `must not exceed 1000 items` and the test's
// only real assertion was "not a 413". It would have stayed green with the
// budget check deleted. The fixture is now bounded by BOTH limits, and the
// assertion is on the per-op OUTCOME, which only exists if the request actually
// reached the writer.
func TestBatchTenants_UnderTheBudgetReachesPerOpValidation(t *testing.T) {
	t.Parallel()
	// Small enough that the ops can actually be WRITTEN (direct mode commits per
	// op); the byte-budget side at the full 1000-op ceiling is covered by
	// TestRealisticBulkBatchFitsTheBudget.
	const ops = 20
	body := batchBodyOfNOps(ops)
	if int64(len(body)) > DefaultMaxBatchBodyBytes {
		t.Fatalf("fixture %d exceeds the cap %d", len(body), DefaultMaxBatchBodyBytes)
	}
	configDir := t.TempDir()
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	// ⛔ A read-only RBAC manager makes this vacuous: every op fails at the
	// permission gate, `results` still has one entry per op, and the assertions
	// below hold even with a nil Writer. The point is that the request REACHES
	// the writer, so the caller must actually be allowed to write.
	rbacMgr := permissiveRBACManager(t)
	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})

	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req = setRequestIdentity(req, "a@a.com")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("a %d-byte / %d-op body under both limits did not reach per-op "+
			"handling: status=%d body=%.300s", len(body), ops, w.Code, w.Body.String())
	}
	var resp map[string]any
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	results, _ := resp["results"].([]any)
	if len(results) != ops {
		t.Fatalf("expected %d per-op results, got %d: %v", ops, len(results), resp["summary"])
	}
	// The load-bearing half: at least one op must have been WRITTEN, which is
	// only possible if the request got past every gate to the writer.
	ok := 0
	for _, r := range results {
		if m, isMap := r.(map[string]any); isMap && m["status"] == "ok" {
			ok++
		}
	}
	if ok == 0 {
		t.Fatalf("no op reported status=ok — the request never reached the writer, "+
			"so this test proves nothing: %v", resp["summary"])
	}
}

// batchBodyOfNOps builds a batch of exactly n operations, each on its own
// tenant — the shape a real bulk maintenance batch has.
func batchBodyOfNOps(n int) string {
	var b strings.Builder
	b.WriteString(`{"operations":[`)
	for i := 0; i < n; i++ {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `{"tenant_id":"tenant-%04d","patch":{"_silent_mode":"warning"}}`, i)
	}
	b.WriteString(`]}`)
	return b.String()
}

// TestRealisticBulkBatchFitsTheBudget is the MECHANICAL SSOT for the batch
// budget's legitimate-side justification.
//
// ⛔ IT ASSERTS RATHER THAN LOGS, AND IT COVERS MORE THAN ONE SHAPE. The first
// version computed a single-key batch and only logged the wider shapes, while
// prose in three other places quoted the single-key figure as "~4x headroom" —
// a ratio that is 1.4x at nine keys. Every one of those literals has been
// removed; this is where the claim lives now, and a shape that stops fitting
// fails here instead of in production.
func TestRealisticBulkBatchFitsTheBudget(t *testing.T) {
	t.Parallel()
	// ⛔ DERIVED FROM THE STRUCT TAG, NOT COPIED. Writing 1000 here re-created the
	// defect this whole file exists to remove: raising the tag to max=5000 left
	// everything green while a plain single-key batch at the new ceiling (270,016
	// bytes) exceeds the budget.
	maxOps := batchOperationsCeiling(t)
	// A bulk maintenance patch is small; four keys is already generous for one.
	// Beyond this the budget legitimately bites, which the sibling comment on
	// DefaultMaxBatchBodyBytes records as an accepted trade.
	for _, keys := range []int{1, 2, 4} {
		body := bulkBatchBody(maxOps, keys)
		ratio := float64(DefaultMaxBatchBodyBytes) / float64(len(body))
		t.Logf("%d ops x %d patch key(s) = %d bytes (%.2fx inside the %d-byte budget)",
			maxOps, keys, len(body), ratio, DefaultMaxBatchBodyBytes)
		if int64(len(body)) > DefaultMaxBatchBodyBytes {
			t.Errorf("a full %d-op batch with %d patch key(s) is %d bytes, over the "+
				"%d-byte budget — the budget contradicts the documented operations "+
				"ceiling", maxOps, keys, len(body), DefaultMaxBatchBodyBytes)
		}
	}
	// ⚠️ The counter-example is asserted too, so "the headroom is not uniform"
	// stops being a claim someone has to trust. If a future budget change made
	// even this shape fit, the warning in DefaultMaxBatchBodyBytes' doc comment
	// would be stale and this tells us.
	wide := bulkBatchBody(maxOps, 16)
	t.Logf("%d ops x 16 patch keys = %d bytes (over budget: %v)",
		maxOps, len(wide), int64(len(wide)) > DefaultMaxBatchBodyBytes)
	if int64(len(wide)) <= DefaultMaxBatchBodyBytes {
		t.Errorf("a %d-op x 16-key batch (%d bytes) now fits the %d-byte budget — "+
			"the documented warning that a fully-legal batch can still 413 is stale",
			maxOps, len(wide), DefaultMaxBatchBodyBytes)
	}
}

// bulkBatchBody builds the JSON a bulk maintenance batch actually sends.
func bulkBatchBody(ops, keysPerPatch int) string {
	var b strings.Builder
	b.WriteString(`{"operations":[`)
	for i := 0; i < ops; i++ {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `{"tenant_id":"tenant-%04d","patch":{`, i)
		for k := 0; k < keysPerPatch; k++ {
			if k > 0 {
				b.WriteString(",")
			}
			fmt.Fprintf(&b, `"_k%d":"warning"`, k)
		}
		b.WriteString(`}}`)
	}
	b.WriteString(`]}`)
	return b.String()
}

func TestMaxBatchBodyBytesFromEnv(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, env     string
		want          int64
		wantMalformed bool
	}{
		{"unset", "", DefaultMaxBatchBodyBytes, false},
		{"valid", "4096", 4096, false},
		{"zero", "0", DefaultMaxBatchBodyBytes, true},
		{"negative", "-1", DefaultMaxBatchBodyBytes, true},
		{"unparseable", "abc", DefaultMaxBatchBodyBytes, true},
		{"numeric prefix", "262144x", DefaultMaxBatchBodyBytes, true},
		{"whitespace trimmed", "  8192  ", 8192, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, malformed := MaxBatchBodyBytesFromEnv(tc.env)
			if got != tc.want || malformed != tc.wantMalformed {
				t.Errorf("got (%d, %v), want (%d, %v)", got, malformed, tc.want, tc.wantMalformed)
			}
		})
	}
}

// TestBatchBudgetIsTighterThanTheGeneralBodyCap pins the RELATIONSHIP rather
// than either number: the batch endpoint's whole reason for a separate knob is
// that its body is parsed repeatedly inside the write lock. If someone raises
// it to the general cap the separate knob has silently stopped meaning anything.
func TestBatchBudgetIsTighterThanTheGeneralBodyCap(t *testing.T) {
	t.Parallel()
	if DefaultMaxBatchBodyBytes >= DefaultMaxBodyBytes {
		t.Errorf("batch budget %d is not tighter than the general body cap %d — "+
			"the separate knob no longer bounds anything",
			DefaultMaxBatchBodyBytes, DefaultMaxBodyBytes)
	}
}

// TestOversizeBatchCarriesAnErrorCode pins the contract errors.go states in its
// own doc comment: every status a WriteJSONError call site uses must map to a
// code, because `code` is REQUIRED by the OpenAPI ErrorResponse schema and an
// unmapped status fails schemathesis response_schema_conformance. 413 was the
// first status introduced without one.
func TestOversizeBatchCarriesAnErrorCode(t *testing.T) {
	t.Parallel()
	if got := codeFromStatus(http.StatusRequestEntityTooLarge); got != CodePayloadTooLarge {
		t.Fatalf("codeFromStatus(413) = %q, want %q — an unmapped status omits "+
			"the required `code` key", got, CodePayloadTooLarge)
	}
	body := batchBodyOfSize(int(DefaultMaxBatchBodyBytes) + 1024)
	configDir := t.TempDir()
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	w := httptest.NewRecorder()
	h(w, req)
	var resp map[string]any
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp["code"] != CodePayloadTooLarge {
		t.Errorf("413 body has code=%v, want %q: %v", resp["code"], CodePayloadTooLarge, resp)
	}
}

// TestReadBodyWithinDoesNotOverflowAtMaxInt64 pins the arithmetic.
//
// ⛔ THE FAILURE MODE WAS SILENT AND INVERTED. limit+1 overflows to a negative
// n at MaxInt64; io.LimitReader then returns EOF immediately, so len(body)==0
// is not greater than limit and the EMPTY read is accepted — every request
// afterwards dies as "invalid JSON: unexpected end of JSON input", the exact
// unactionable message readBodyWithin exists to replace. An operator setting
// MaxInt64 means "no cap", so that is what it must do.
func TestReadBodyWithinDoesNotOverflowAtMaxInt64(t *testing.T) {
	t.Parallel()
	const payload = `{"operations":[]}`
	for _, limit := range []int64{math.MaxInt64, math.MaxInt64 - 1, 1 << 30} {
		req := httptest.NewRequest("POST", "/x", bytes.NewBufferString(payload))
		w := httptest.NewRecorder()
		body, ok := readBodyWithin(w, req, limit, "K")
		if !ok {
			t.Fatalf("limit=%d: refused a %d-byte body", limit, len(payload))
		}
		if len(body) != len(payload) {
			t.Errorf("limit=%d: read %d bytes of a %d-byte body — the body was "+
				"silently truncated and then accepted", limit, len(body), len(payload))
		}
	}
}

// TestGroupBatchRejectsOversizeBody: the sibling endpoint reaches the SAME
// write path (executeGroupBatchOps → applyPatch → WriteMerged → validate) and
// applies one patch to EVERY member, so an uncapped body there costs more than
// on /tenants/batch, not less. It had no cap at all.
func TestGroupBatchRejectsOversizeBody(t *testing.T) {
	t.Parallel()
	var b strings.Builder
	b.WriteString(`{"patch":{`)
	for i := 0; int64(b.Len()) < DefaultMaxBatchBodyBytes+1024; i++ {
		if i > 0 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `"k%06d":"1"`, i)
	}
	b.WriteString(`}}`)

	configDir := t.TempDir()
	setupGroupsFile(t, configDir, testGroupsYAML)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	h := GroupBatch(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr,
		Groups: groups.NewManager(configDir), WriteMode: WriteModeDirect})
	req := newRequestWithChiParam("POST", "/api/v1/groups/production-dba/batch",
		"id", "production-dba", bytes.NewBufferString(b.String()))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413 for a %d-byte group-batch body: %.300s",
			w.Code, b.Len(), w.Body.String())
	}
}

// TestBatchBodyCapIsInclusiveAndHonoursTheGlobalCap pins the two properties of
// the batch byte cap that a blind mutation round found UNGUARDED: flipping
// `> limit` to `>= limit` in readBodyWithin, and deleting the global-cap clamp
// in Deps.MaxBatchBody, both left the whole handler suite green.
//
// ⛔ The knob name is part of the contract, not decoration: when the global cap
// is the binding one, a 413 telling the operator to raise
// TA_MAX_BATCH_BODY_BYTES points at a knob that cannot move the limit.
func TestBatchBodyCapIsInclusiveAndHonoursTheGlobalCap(t *testing.T) {
	t.Parallel()
	const body = `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`
	n := int64(len(body))

	for _, tc := range []struct {
		name          string
		batch, global int64
		want413       bool
		wantKnob      string
	}{
		{"a body of exactly the batch cap is accepted", n, DefaultMaxBodyBytes, false, ""},
		{"one byte past the batch cap is 413", n - 1, DefaultMaxBodyBytes, true, "TA_MAX_BATCH_BODY_BYTES"},
		{"the global cap still bounds the batch endpoint", DefaultMaxBatchBodyBytes, n - 1, true, "TA_MAX_BODY_BYTES"},
		{"a body of exactly the global cap is accepted", DefaultMaxBatchBodyBytes, n, false, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			configDir := t.TempDir()
			h := BatchTenants(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir,
				RBAC: permissiveRBACManager(t), WriteMode: WriteModeDirect,
				MaxBatchBodyBytes: tc.batch, MaxBodyBytes: tc.global})
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
				bytes.NewBufferString(body))
			req = setRequestIdentity(req, "a@a.com")
			w := httptest.NewRecorder()
			h(w, req)

			got413 := w.Code == http.StatusRequestEntityTooLarge
			if got413 != tc.want413 {
				t.Fatalf("a %d-byte body under batch=%d global=%d: status=%d, want413=%v: %.200s",
					n, tc.batch, tc.global, w.Code, tc.want413, w.Body.String())
			}
			if tc.wantKnob != "" && !strings.Contains(w.Body.String(), tc.wantKnob) {
				t.Fatalf("the 413 does not name %s — it sends the operator to a knob "+
					"that cannot move the limit: %.300s", tc.wantKnob, w.Body.String())
			}
		})
	}
}

// TestDryRunAndWriteAgreeOnDocumentSize is the write-vs-read symmetry pin.
//
// ⛔ THIS REPO HAS CLOSED THIS ASYMMETRY TWICE (#704, #1718) AND THE #1722 GATE
// RE-OPENED IT: POST /{id}/validate does not call gitops.validate — it
// reassembles the same checks by hand — so a gate added only to the write path
// let the dry-run answer `valid: true` for a body the PUT then refused. Both
// sides now call the same exported gitops.CheckTenantDocSize; this asserts the
// verdicts agree rather than asserting either implementation.
func TestDryRunAndWriteAgreeOnDocumentSize(t *testing.T) {
	t.Parallel()
	var b strings.Builder
	b.WriteString("tenants:\n  db-a:\n")
	for i := 0; int64(b.Len()) <= gitops.MaxTenantDocBytes(); i++ {
		fmt.Fprintf(&b, "    container_cpu{version=\"v%d\"}: 70\n", i)
	}
	oversize := b.String()

	// The write boundary's own answer, taken from the shared gate.
	if errs := gitops.CheckTenantDocSize(oversize); len(errs) == 0 {
		t.Fatalf("fixture (%d bytes) is not over the %d-byte cap",
			len(oversize), gitops.MaxTenantDocBytes())
	}

	configDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(configDir, "_defaults.yaml"),
		[]byte("defaults:\n  container_cpu: 80\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	h := ValidateTenant(&Deps{ConfigDir: configDir})
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/validate",
		"id", "db-a", bytes.NewBufferString(oversize))
	w := httptest.NewRecorder()
	h(w, req)

	var resp ValidateResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Valid {
		t.Fatalf("dry-run says valid:true for a %d-byte body the write path "+
			"refuses — a client that trusts the dry-run gets a 400 on PUT",
			len(oversize))
	}
	if !strings.Contains(strings.Join(resp.Warnings, "; "), "TA_MAX_TENANT_DOC_BYTES") {
		t.Errorf("dry-run refused, but not for the size reason: %v", resp.Warnings)
	}
}

// TestShippedConfigLiteralsMatchTheCode is the MECHANICAL SSOT for the two caps'
// values wherever an operator reads them.
//
// ⛔ THE SAME NUMBER LIVES IN FIVE PLACES and nothing tied them together: the Go
// constants, helm's values.yaml (what actually gets deployed), and TWO README
// tables. Every prose figure in this change set that lacked a check like this
// came back wrong at least once.
//
// ⛔ IT FAILS WHEN ITS INPUTS MOVE, IT DOES NOT SKIP. An earlier version skipped
// when helm's values.yaml could not be read, so an ordinary chart reshuffle
// (`git mv helm/tenant-api helm/charts/tenant-api`) turned it into a no-op and
// the deployed default could then drift 64x from the code with the suite green.
//
// ⛔ IT COMPARES THE COMPILED-IN DEFAULTS, NOT THE RUNTIME VALUES. Reading
// gitops.MaxTenantDocBytes() made the test fail for anyone with
// TA_MAX_TENANT_DOC_BYTES exported — and its message then asserted "the code's
// default is N" about a value that was not the default.
func TestShippedConfigLiteralsMatchTheCode(t *testing.T) {
	t.Parallel()
	root := testutil.RepoRoot(t)

	// helm — the values an operator actually deploys.
	raw, err := os.ReadFile(filepath.Join(root, "helm", "tenant-api", "values.yaml"))
	if err != nil {
		t.Fatalf("cannot read helm values (%v) — this guard exists to stop the "+
			"deployed default drifting from the code, and passing without reading "+
			"it is how that drift becomes invisible", err)
	}
	var vals struct {
		TenantAPI struct {
			Server struct {
				MaxBatchBodyBytes int64 `yaml:"maxBatchBodyBytes"`
				MaxTenantDocBytes int64 `yaml:"maxTenantDocBytes"`
			} `yaml:"server"`
		} `yaml:"tenantApi"`
	}
	if err := yaml.Unmarshal(raw, &vals); err != nil {
		t.Fatalf("parse helm values: %v", err)
	}
	if got := vals.TenantAPI.Server.MaxBatchBodyBytes; got != DefaultMaxBatchBodyBytes {
		t.Errorf("helm values maxBatchBodyBytes=%d but DefaultMaxBatchBodyBytes=%d — "+
			"the deployed default and the code have diverged", got, DefaultMaxBatchBodyBytes)
	}
	if got := vals.TenantAPI.Server.MaxTenantDocBytes; got != gitops.DefaultTenantDocBytes() {
		t.Errorf("helm values maxTenantDocBytes=%d but the code's default is %d — "+
			"the deployed default and the code have diverged", got, gitops.DefaultTenantDocBytes())
	}

	// README — BOTH tables. The limits table quotes KiB, the env-var table quotes
	// bytes; an earlier version's regexp only matched the latter, so the former
	// could sit stale 60 lines above with everything green.
	readmePath := filepath.Join(root, "components", "tenant-api", "README.md")
	readme, err := os.ReadFile(readmePath)
	if err != nil {
		t.Fatalf("cannot read %s (%v) — see above: a guard that skips on a missing "+
			"input guards nothing", readmePath, err)
	}
	for _, tc := range []struct {
		knob string
		want int64
	}{
		{"TA_MAX_BATCH_BODY_BYTES", DefaultMaxBatchBodyBytes},
		{"TA_MAX_TENANT_DOC_BYTES", gitops.DefaultTenantDocBytes()},
	} {
		assertReadmeDocumentsBytes(t, string(readme), tc.knob, tc.want)
	}
}

// assertReadmeDocumentsBytes checks every README line naming knob: each must
// quote the value either as a byte count or as a KiB figure, and none may quote
// a different one.
//
// ⛔ IT ASSERTS THAT AT LEAST ONE LINE WAS FOUND. A regexp that silently matches
// nothing is the documentation equivalent of a skip, and that is what let one of
// the two tables rot.
func assertReadmeDocumentsBytes(t *testing.T, readme, knob string, want int64) {
	t.Helper()
	wantKiB := want / 1024
	found := 0
	for _, line := range strings.Split(readme, "\n") {
		if !strings.Contains(line, knob) {
			continue
		}
		nums := regexp.MustCompile(`\d+`).FindAllString(line, -1)
		var quoted []string
		ok := false
		for _, n := range nums {
			v, err := strconv.ParseInt(n, 10, 64)
			if err != nil {
				continue
			}
			// A row may legitimately carry other integers (a KiB figure beside the
			// byte count). Accept the line if EITHER representation appears.
			if v == want || (v == wantKiB && strings.Contains(line, "KiB")) {
				ok = true
			}
			quoted = append(quoted, n)
		}
		if !ok {
			t.Errorf("README line documenting %s quotes %v, none of which is the "+
				"code's default %d (or %d KiB): %s", knob, quoted, want, wantKiB,
				strings.TrimSpace(line))
		}
		found++
	}
	if found == 0 {
		t.Errorf("README documents %s nowhere — either the knob is undocumented or "+
			"the tables changed shape and this guard is matching nothing", knob)
	}
}

// batchOperationsCeiling reads the `max=` bound off BatchRequest.Operations'
// validate tag, so the budget check and the API contract cannot disagree.
func batchOperationsCeiling(t *testing.T) int {
	t.Helper()
	f, ok := reflect.TypeOf(BatchRequest{}).FieldByName("Operations")
	if !ok {
		t.Fatal("BatchRequest has no Operations field — this guard reads its " +
			"validate tag and must not pass without it")
	}
	m := regexp.MustCompile(`max=(\d+)`).FindStringSubmatch(f.Tag.Get("validate"))
	if m == nil {
		t.Fatalf("BatchRequest.Operations has no max= in its validate tag (%q) — "+
			"the operations ceiling is unbounded or moved", f.Tag.Get("validate"))
	}
	n, err := strconv.Atoi(m[1])
	if err != nil || n <= 0 {
		t.Fatalf("unparseable operations ceiling %q", m[1])
	}
	return n
}

// TestUnboundedMapsAreRejectedBeforeTheWriteLock covers the two element-count
// bounds that the BYTE caps cannot express (#1722).
//
// ⛔ BYTES ARE THE WRONG UNIT FOR THESE TWO. mergePatchYAML is quadratic in the
// patch's key count and runs inside the single-writer token BEFORE the merged
// document reaches TA_MAX_TENANT_DOC_BYTES, and the group registry's transform
// runs inside the lock too — so a body comfortably under TA_MAX_BATCH_BODY_BYTES
// could still buy seconds of in-lock work (measured: 18,721 patch keys ≈ 3.8s of
// merge alone). The bounds live on the struct tags; this proves they fire.
func TestUnboundedMapsAreRejectedBeforeTheWriteLock(t *testing.T) {
	t.Parallel()

	t.Run("batch patch key count", func(t *testing.T) {
		t.Parallel()
		var b strings.Builder
		b.WriteString(`{"operations":[{"tenant_id":"db-a","patch":{`)
		for i := 0; i < 1001; i++ { // one past the tag's max
			if i > 0 {
				b.WriteString(",")
			}
			fmt.Fprintf(&b, `"k%06d":"1"`, i)
		}
		b.WriteString(`}}]}`)
		if int64(b.Len()) > DefaultMaxBatchBodyBytes {
			t.Fatalf("fixture %d bytes exceeds the byte budget — it would be "+
				"rejected for the wrong reason", b.Len())
		}
		configDir := t.TempDir()
		h := BatchTenants(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir,
			RBAC: permissiveRBACManager(t), WriteMode: WriteModeDirect})
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(b.String()))
		req = setRequestIdentity(req, "a@a.com")
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("a 1001-key patch (%d bytes, inside the byte budget) was not "+
				"refused: status=%d body=%.200s", b.Len(), w.Code, w.Body.String())
		}
	})

	// ⛔ This one was MISSING when the other two shipped, and two independent
	// blind reviews found it: GroupBatchRequest.Patch carried no validate tag
	// and GroupBatch called neither ValidateStructTags nor validatePatchMap, so
	// the endpoint where the quadratic merge is paid ONCE PER MEMBER was the one
	// endpoint with no key-count bound at all. A tag alone is not enough here —
	// executeGroupBatchOps builds BatchOperation in Go, so the tenant-batch
	// endpoint's tag never applies to this path.
	t.Run("group batch patch key count", func(t *testing.T) {
		t.Parallel()
		var b strings.Builder
		b.WriteString(`{"patch":{`)
		for i := 0; i < 1001; i++ { // one past the tag's max
			if i > 0 {
				b.WriteString(",")
			}
			fmt.Fprintf(&b, `"k%06d":"1"`, i)
		}
		b.WriteString(`}}`)
		if int64(b.Len()) > DefaultMaxBatchBodyBytes {
			t.Fatalf("fixture %d bytes exceeds the byte budget — it would be "+
				"rejected for the wrong reason", b.Len())
		}
		configDir := t.TempDir()
		setupGroupsFile(t, configDir, testGroupsYAML)
		h := GroupBatch(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir,
			Groups: groups.NewManager(configDir), RBAC: newRBACManager(t, ""),
			WriteMode: WriteModeDirect})
		req := newRequestWithChiParam("POST", "/api/v1/groups/production-dba/batch",
			"id", "production-dba", bytes.NewBufferString(b.String()))
		req = setRequestIdentity(req, "a@a.com")
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("a 1001-key group-batch patch (%d bytes, inside the byte "+
				"budget) was not refused: status=%d body=%.200s",
				b.Len(), w.Code, w.Body.String())
		}
	})

	t.Run("group filters count", func(t *testing.T) {
		t.Parallel()
		var b strings.Builder
		b.WriteString(`{"label":"L","members":[],"filters":{`)
		for i := 0; i < 21; i++ { // one past the tag's max
			if i > 0 {
				b.WriteString(",")
			}
			fmt.Fprintf(&b, `"f%03d":"v"`, i)
		}
		b.WriteString(`}}`)
		configDir := t.TempDir()
		setupGroupsFile(t, configDir, testGroupsYAML)
		h := PutGroup(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir,
			Groups: groups.NewManager(configDir), RBAC: permissiveRBACManager(t)})
		req := newRequestWithChiParam("PUT", "/api/v1/groups/g1", "id", "g1",
			bytes.NewBufferString(b.String()))
		req = setRequestIdentity(req, "a@a.com")
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("a 21-filter group write was not refused: status=%d body=%.200s",
				w.Code, w.Body.String())
		}
	})
}
