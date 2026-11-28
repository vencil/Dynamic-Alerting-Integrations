package logprojection

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const ledger = "schema_version: v1\nnext_account_id: 1003\nallocations:\n  t1: 1000\n  t2: 1001\n  t3: 1002\n"

func writeTree(t *testing.T, files map[string]string) *cfg.ThresholdConfig {
	t.Helper()
	dir := t.TempDir()
	files["_defaults.yaml"] = "defaults:\n  cpu: 80\n"
	for name, text := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	c, failed, err := cfg.LoadDir(dir, nil)
	if err != nil || len(failed) > 0 {
		t.Fatalf("LoadDir: %v %v", err, failed)
	}
	return c
}

func tenantFile(id, flag string) string {
	s := "tenants:\n  " + id + ":\n    cpu: 70\n"
	if flag != "" {
		s += "    _log_projection: " + flag + "\n"
	}
	return s
}

func ids(ps []Projection) map[string]uint32 {
	out := map[string]uint32{}
	for _, p := range ps {
		out[p.TenantID] = p.AccountID
	}
	return out
}

func TestFlagOf(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		val    string
		on, ok bool
	}{{"enable", true, true}, {"disable", false, true}, {"", false, true},
		{"Enable", false, false}, {"true", false, false}, {"yes", false, false}, {" enable", false, false}} {
		on, ok := FlagOf(cfg.ScheduledValue{Default: tc.val})
		if on != tc.on || ok != tc.ok {
			t.Errorf("FlagOf(%q) = %v,%v; want %v,%v", tc.val, on, ok, tc.on, tc.ok)
		}
	}
}

// TestFlagWordsMatchTheSchema: the schema's enum and FlagOf say the same.
func TestFlagWordsMatchTheSchema(t *testing.T) {
	t.Parallel()
	for _, rel := range []string{"tenant-config.schema.json", "platform-defaults.schema.json"} {
		raw, err := os.ReadFile(filepath.Join(testutil.RepoRoot(t), "docs", "schemas", rel))
		if err != nil {
			t.Fatal(err)
		}
		var enum []any
		var find func(any)
		find = func(v any) {
			switch m := v.(type) {
			case map[string]any:
				if e, ok := m[FlagKey].(map[string]any); ok && enum == nil {
					enum, _ = e["enum"].([]any)
				}
				for _, c := range m {
					find(c)
				}
			case []any:
				for _, c := range m {
					find(c)
				}
			}
		}
		var doc any
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatal(err)
		}
		find(doc)
		want := []any{FlagEnable, FlagDisable, nil}
		if !reflect.DeepEqual(enum, want) {
			t.Errorf("%s: %s enum %v, FlagOf reads %v", rel, FlagKey, enum, want)
		}
	}
}

func TestCompute_ProjectsFlaggedTenantsWithAnAccount(t *testing.T) {
	t.Parallel()
	c := writeTree(t, map[string]string{
		"t1.yaml": tenantFile("t1", "enable"),
		"t2.yaml": tenantFile("t2", "disable"),
		"t3.yaml": tenantFile("t3", ""),
		"t4.yaml": tenantFile("t4", "enable"), // no account
		"t5.yaml": tenantFile("t5", "on"),     // a word FlagOf does not read
	})
	res, err := Compute(map[string][]byte{"_account_registry.yaml": []byte(ledger)}, c)
	if err != nil {
		t.Fatal(err)
	}
	if got := ids(res.Projections); !reflect.DeepEqual(got, map[string]uint32{"t1": 1000}) {
		t.Errorf("projections %v", got)
	}
	want := []Finding{{"t4", ReasonNoAccount}, {"t5", ReasonFlagUnread}}
	if !reflect.DeepEqual(res.Findings, want) {
		t.Errorf("findings %v, want %v", res.Findings, want)
	}
}

// The root platform file's `tenants:` entry is a layer the flag is read from;
// the tenant file wins over it.
func TestCompute_ReadsThePlatformTenantsLayer(t *testing.T) {
	t.Parallel()
	c := writeTree(t, map[string]string{
		"_platform.yaml": "tenants:\n  t1:\n    _log_projection: enable\n  t2:\n    _log_projection: enable\n",
		"t1.yaml":        tenantFile("t1", ""),
		"t2.yaml":        tenantFile("t2", "disable"),
	})
	res, err := Compute(map[string][]byte{"_account_registry.yaml": []byte(ledger)}, c)
	if err != nil {
		t.Fatal(err)
	}
	if got := ids(res.Projections); !reflect.DeepEqual(got, map[string]uint32{"t1": 1000}) {
		t.Errorf("projections %v", got)
	}
}

func TestCompute_NoLedgerProjectsNobody(t *testing.T) {
	t.Parallel()
	c := writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})
	res, err := Compute(map[string][]byte{}, c)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Projections) != 0 || len(res.Findings) != 1 || res.Findings[0].Reason != ReasonNoAccount {
		t.Errorf("%+v", res)
	}
}

func TestCompute_ADamagedRecordIsAnError(t *testing.T) {
	t.Parallel()
	c := writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})
	for name, state := range map[string]map[string][]byte{
		"ledger two tenants one id": {"_account_registry.yaml": []byte(
			"schema_version: v1\nnext_account_id: 1002\nallocations:\n  t1: 1000\n  t2: 1000\n")},
		"ledger newer schema": {"_account_registry.yaml": []byte("schema_version: v2\nnext_account_id: 1001\nallocations: {t1: 1000}\n")},
		"visibility no fields key": {"_account_registry.yaml": []byte(ledger),
			"_log_visibility.t1.yaml": []byte("other: 1\n")},
		"visibility unknown field": {"_account_registry.yaml": []byte(ledger),
			"_log_visibility.t1.yaml": []byte("fields: [upstream]\n")},
	} {
		if _, err := Compute(state, c); !errors.Is(err, ErrInput) {
			t.Errorf("%s: err %v, want ErrInput", name, err)
		}
	}
}

func TestCompute_CarriesTheVisibilitySelection(t *testing.T) {
	t.Parallel()
	c := writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable"), "t2.yaml": tenantFile("t2", "enable")})
	res, err := Compute(map[string][]byte{"_account_registry.yaml": []byte(ledger),
		"_log_visibility.t2.yaml": []byte("fields: [status]\n"),
		"_log_visibility.t1.yaml": []byte("fields: null\n")}, c)
	if err != nil {
		t.Fatal(err)
	}
	if res.Projections[0].VisibleFields != nil {
		t.Errorf("t1 made no selection, got %v", *res.Projections[0].VisibleFields)
	}
	if v := res.Projections[1].VisibleFields; v == nil || !reflect.DeepEqual(*v, []string{"status"}) {
		t.Errorf("t2 selection %v", v)
	}
}

func TestAccountChanges(t *testing.T) {
	t.Parallel()
	prev := []Projection{{TenantID: "t1", AccountID: 1000}, {TenantID: "t2", AccountID: 1001}}
	next := []Projection{{TenantID: "t1", AccountID: 1000}, {TenantID: "t2", AccountID: 1005}, {TenantID: "t3", AccountID: 1002}}
	if got := AccountChanges(prev, next); !reflect.DeepEqual(got, []string{"t2"}) {
		t.Errorf("%v", got)
	}
	if got := AccountChanges(nil, next); got != nil {
		t.Errorf("%v", got)
	}
}

func template(t *testing.T) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "template.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestRender_FillsEveryPlaceholder(t *testing.T) {
	t.Parallel()
	sel := []string{"status", "path"}
	out, err := Render(template(t), []Projection{{TenantID: "t1", AccountID: 1000}, {TenantID: "t2", AccountID: 1001, VisibleFields: &sel}})
	if err != nil {
		t.Fatal(err)
	}
	s := string(out)
	for _, want := range []string{
		`account_map = {"t1":1000,"t2":1001}`,
		`visible_map = {"t2":["status","path"]}`,
		`t_1000: '.account_id == 1000'`, `t_1001: '.account_id == 1001'`,
		"vl_tenant_1000:", "- tenant_route.t_1001", `AccountID: "1001"`,
	} {
		if !strings.Contains(s, want) {
			t.Errorf("output lacks %q:\n%s", want, s)
		}
	}
	if strings.Contains(s, placeholderPrefix) {
		t.Errorf("placeholder left:\n%s", s)
	}
}

// No tenant: the per-tenant entries go, the rest stays (the platform copy only).
func TestRender_NoTenantDropsThePerTenantEntries(t *testing.T) {
	t.Parallel()
	out, err := Render(template(t), nil)
	if err != nil {
		t.Fatal(err)
	}
	s := string(out)
	if strings.Contains(s, "vl_tenant_") || strings.Contains(s, "t_1") || !strings.Contains(s, "account_map = {}") {
		t.Errorf("%s", s)
	}
}

func TestRender_RefusesABrokenTemplate(t *testing.T) {
	t.Parallel()
	for name, tpl := range map[string]string{
		"no account map":          "sinks:\n  vl_tenant___DA_ACCOUNT_ID__: {type: blackhole}\n",
		"no per-tenant entry":     "transforms:\n  x: {source: '__DA_ACCOUNT_MAP__'}\n",
		"id outside an entry":     "a: '__DA_ACCOUNT_MAP__'\nb: '__DA_ACCOUNT_ID__'\nc___DA_ACCOUNT_ID__: 1\n",
		"misspelt placeholder":    "a: '__DA_ACCOUNT_MAP__ __DA_ACOUNT_MAP__'\nc___DA_ACCOUNT_ID__: 1\n",
		"not a mapping":           "- a\n",
		"alias in a tenant entry": "x: &a {k: v}\na: '__DA_ACCOUNT_MAP__'\nc___DA_ACCOUNT_ID__: *a\n",
	} {
		if _, err := Render([]byte(tpl), []Projection{{TenantID: "t1", AccountID: 1000}}); err == nil {
			t.Errorf("%s: rendered", name)
		}
	}
}

// ── Runner ─────────────────────────────────────────────────────────────

type fakeInputs struct {
	state    map[string][]byte
	stateErr error
	c        *cfg.ThresholdConfig
}

func (f *fakeInputs) source() Source {
	return Source{
		State: func(context.Context) (map[string][]byte, error) { return f.state, f.stateErr },
		Confd: func(context.Context) (*cfg.ThresholdConfig, error) { return f.c, nil },
	}
}

func newRunner(t *testing.T, in *fakeInputs) *Runner {
	t.Helper()
	return &Runner{Source: in.source(), Template: template(t), Out: filepath.Join(t.TempDir(), "tenants.yaml")}
}

func read(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestRunner_KeepsTheLastResultWhenAnInputFails(t *testing.T) {
	t.Parallel()
	in := &fakeInputs{state: map[string][]byte{"_account_registry.yaml": []byte(ledger)},
		c: writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})}
	r := newRunner(t, in)
	if err := r.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	good := read(t, r.Out)
	in.stateErr = errors.New("api down")
	if err := r.Once(context.Background()); err == nil {
		t.Fatal("a failed read reported success")
	}
	in.stateErr = nil
	in.state = map[string][]byte{"_account_registry.yaml": []byte("schema_version: v1\nnext_account_id: 1001\nallocations: {t1: 1000, t2: 1000}\n")}
	if err := r.Once(context.Background()); err == nil {
		t.Fatal("a damaged ledger reported success")
	}
	if got := read(t, r.Out); got != good {
		t.Errorf("the file moved:\n%s", got)
	}
	var b strings.Builder
	r.WriteMetrics(&b)
	if !strings.Contains(b.String(), `da_log_projection_read_failures_total{source="state"} 2`) {
		t.Errorf("metrics:\n%s", b.String())
	}
}

// Only a runner that never wrote, finding no file, writes the template with
// no tenant; an existing file (the last container's) is never replaced by it.
func TestRunner_EmptyFallback(t *testing.T) {
	t.Parallel()
	in := &fakeInputs{stateErr: errors.New("api down"), c: writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})}
	r := newRunner(t, in)
	_ = r.Once(context.Background())
	if s := read(t, r.Out); !strings.Contains(s, "account_map = {}") {
		t.Errorf("no empty fallback:\n%s", s)
	}
	r2 := newRunner(t, in)
	if err := os.WriteFile(r2.Out, []byte("kept\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_ = r2.Once(context.Background())
	if s := read(t, r2.Out); s != "kept\n" {
		t.Errorf("an existing file was replaced:\n%s", s)
	}
}

func TestRunner_RefusesAMovedAccount(t *testing.T) {
	t.Parallel()
	in := &fakeInputs{state: map[string][]byte{"_account_registry.yaml": []byte(ledger)},
		c: writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})}
	r := newRunner(t, in)
	if err := r.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	in.state = map[string][]byte{"_account_registry.yaml": []byte("schema_version: v1\nnext_account_id: 1010\nallocations: {t1: 1009}\n")}
	if err := r.Once(context.Background()); err == nil || !strings.Contains(err.Error(), "t1") {
		t.Fatalf("err %v", err)
	}
	if !strings.Contains(read(t, r.Out), `"t1":1000`) {
		t.Error("the moved account was written")
	}
}

func TestRunner_ConfirmsTheReload(t *testing.T) {
	t.Parallel()
	in := &fakeInputs{state: map[string][]byte{"_account_registry.yaml": []byte(ledger)},
		c: writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable")})}
	r := newRunner(t, in)
	var count float64 = 3
	grow := true
	r.Reloads = func(context.Context) (float64, error) {
		if grow {
			count++
		}
		return count, nil
	}
	r.ReloadTimeout = 2 * time.Second
	if err := r.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	r.WriteMetrics(&b)
	if !strings.Contains(b.String(), "da_log_projection_reload_unconfirmed 0") {
		t.Errorf("metrics:\n%s", b.String())
	}
	grow = false
	in.c = writeTree(t, map[string]string{"t1.yaml": tenantFile("t1", "enable"), "t2.yaml": tenantFile("t2", "enable")})
	if err := r.Once(context.Background()); err == nil || !strings.Contains(err.Error(), "reload not confirmed") {
		t.Fatalf("err %v", err)
	}
	b.Reset()
	r.WriteMetrics(&b)
	if !strings.Contains(b.String(), "da_log_projection_reload_unconfirmed 1") {
		t.Errorf("metrics:\n%s", b.String())
	}
}

func TestSumMetric(t *testing.T) {
	t.Parallel()
	page := "# HELP vector_reloaded_total x\n# TYPE vector_reloaded_total counter\n" +
		"vector_reloaded_total 2\nvector_reloaded_total{host=\"a\"} 3 1700000000\nvector_reloaded_total_other 9\n"
	v, err := SumMetric(strings.NewReader(page), "vector_reloaded_total")
	if err != nil || v != 5 {
		t.Errorf("%v %v", v, err)
	}
	if _, err := SumMetric(strings.NewReader("other 1\n"), "vector_reloaded_total"); err == nil {
		t.Error("a page without the metric summed")
	}
}
