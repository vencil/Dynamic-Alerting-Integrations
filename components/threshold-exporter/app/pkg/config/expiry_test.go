package config

import (
	"fmt"
	"io"
	"log"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// expiryAt is past every `expires:` these tests write.
var expiryAt = time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)

const expiredAt = "2020-01-01T00:00:00Z"

// tenantRows is every row /metrics serves tenant at at, sorted.
func tenantRows(cfg *ThresholdConfig, tenant string, at time.Time) []string {
	var out []string
	for _, r := range cfg.ResolveAt(at) {
		if r.Tenant == tenant {
			out = append(out, r.Component+"_"+r.Metric+" "+r.Severity+" "+strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	sort.Strings(out)
	return out
}

// lapseCase is one tree: the expiring line (key, written in place), and the
// layer that gives the key a value beneath it.
type lapseCase struct {
	name string
	// key is the line's key as written; base is the threshold key the root,
	// `dimensions:` and the critical row hang on.
	key, base string
	// place is where the expiring line is written: "tenant" (s/t.yaml),
	// "platform" (_platform.yaml's `tenants:` entry) or "subtree"
	// (s/_defaults.yaml).
	place string
	// beneath is the layer under the line that values key: "" (none),
	// "root", "root-disable", "platform", "profile", "subtree".
	beneath string
}

// files is c's tree; with deleted, without the expiring line.
func (c lapseCase) files(deleted bool) map[string]string {
	line := fmt.Sprintf("    %s:\n      default: \"5\"\n      expires: %q\n", c.key, expiredAt)
	beneath := fmt.Sprintf("    %s: \"70\"\n", c.key)
	root := ""
	if strings.Contains(c.key, "{") {
		root += "dimensions:\n  " + c.base + ": [db]\n"
	}
	root += "defaults:\n"
	switch c.beneath {
	case "root":
		root += fmt.Sprintf("  %s: 50\n", c.key)
	case "root-disable":
		root += fmt.Sprintf("  %s: disable\n", c.key)
	}
	tenant := "tenants:\n  tx:\n"
	platform := ""
	if c.beneath == "profile" {
		tenant += "    _profile: p\n"
	}
	if c.place == "tenant" && !deleted {
		tenant += line
	}
	if c.place == "platform" && !deleted {
		platform += line
	}
	if c.beneath == "platform" {
		platform += beneath
	}
	tenant += "    _metadata: {owner: x}\n" // a body even with the line deleted
	files := map[string]string{"_defaults.yaml": root, "s/t.yaml": tenant}
	if platform != "" {
		files["_platform.yaml"] = "tenants:\n  tx:\n" + platform
	}
	if c.beneath == "profile" {
		files["_profiles.yaml"] = "profiles:\n  p:\n" + strings.Replace(beneath, "    ", "    ", 1)
	}
	if c.beneath == "subtree" {
		files["s/_defaults.yaml"] = "defaults:\n" + strings.Replace(beneath, "    ", "  ", 1)
	}
	if c.place == "subtree" {
		files["s/_defaults.yaml"] = "defaults:\n"
		if !deleted {
			files["s/_defaults.yaml"] += strings.ReplaceAll(line, "\n    ", "\n  ")[2:]
		}
	}
	return files
}

// lapseCases is every expiring line × the layer beneath it × key shape. The
// shapes are a key the platform ships no value for (redis_connected_clients:
// nothing beneath is then possible), its `_critical`, a dimensional key and
// a retired spelling (mysql_cpu).
func lapseCases() []lapseCase {
	shapes := []struct{ name, key, base string }{
		{"base", "redis_connected_clients", "redis_connected_clients"},
		{"critical", "redis_connected_clients_critical", "redis_connected_clients"},
		{"dimensional", `redis_connected_clients{db="a"}`, "redis_connected_clients"},
		{"retired", "mysql_cpu", "mysql_cpu"},
	}
	var out []lapseCase
	for _, s := range shapes {
		for _, place := range []string{"tenant", "platform", "subtree"} {
			for _, b := range []string{"", "root", "root-disable", "platform", "profile", "subtree"} {
				if place == "platform" && b == "platform" {
					continue
				}
				if place == "subtree" && b != "" && b != "root" && b != "root-disable" {
					continue // a layer above the subtree is no layer beneath it
				}
				if s.name == "dimensional" && b == "root-disable" {
					continue // a root dimensional value takes no `disable`
				}
				out = append(out, lapseCase{
					name: s.name + "/" + place + "/beneath=" + b, key: s.key, base: s.base, place: place, beneath: b,
				})
			}
		}
	}
	return out
}

// #4263, #4254: an expired line serves exactly what the tree serves with
// the line deleted — whatever layer is beneath it and whatever its shape —
// except when nothing beneath gives the key a value: then it is served past
// its `expires:` (PREVENT #656), never dropped.
func TestExpiry_ExpiredLineServesAsDeleted(t *testing.T) {
	t.Parallel()
	for _, c := range lapseCases() {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got := tenantRows(loadTree(t, c.files(false)), "tx", expiryAt)
			deleted := tenantRows(loadTree(t, c.files(true)), "tx", expiryAt)
			// Nothing beneath: no layer, or a root `disable` the root does
			// not read (a retired spelling: RootDisableText, #3832) — a
			// refused root value, which reads as off.
			nothing := c.beneath == "" || (c.beneath == "root-disable" && !RootDisableText(c.key, "disable"))
			if nothing {
				if strings.Join(got, "|") == strings.Join(deleted, "|") {
					t.Fatalf("nothing beneath, yet the expired line serves what deleting it serves (%v): it lapsed into silence", got)
				}
				return
			}
			if strings.Join(got, "|") != strings.Join(deleted, "|") {
				t.Errorf("expired line serves\n  %v\nthe line deleted serves\n  %v", got, deleted)
			}
		})
	}
}

// #4263: tenant-api's merge of a tenant (MergeTenantWithRootDefaults: the
// tenant's file over the root platform files and profiles, no subtree
// chain) reads an expired line exactly as /metrics does.
func TestExpiry_TenantAPIMergeReadsAsMetrics(t *testing.T) {
	t.Parallel()
	for _, c := range lapseCases() {
		if c.place != "tenant" || c.beneath == "subtree" {
			continue
		}
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			files := c.files(false)
			dir := writeUndeliverableTree(t, files)
			m := MergeTenantWithRootDefaults(dir, "tx", []byte(files["s/t.yaml"]))
			var got []string
			for _, r := range m.ResolveAt(expiryAt) {
				if r.Tenant == "tx" {
					got = append(got, r.Component+"_"+r.Metric+" "+r.Severity+" "+strconv.FormatFloat(r.Value, 'g', -1, 64))
				}
			}
			sort.Strings(got)
			metrics := loadTree(t, files)
			want := tenantRows(metrics, "tx", expiryAt)
			if strings.Join(got, "|") != strings.Join(want, "|") {
				t.Errorf("tenant-api merge serves\n  %v\n/metrics serves\n  %v", got, want)
			}
			// The write gate refuses the `expires:` exactly when /metrics'
			// own check does.
			if a, b := expiresRefused(m.ValidateTenantKeys().Errors), expiresRefused(metrics.ValidateTenantKeys().Errors); a != b {
				t.Errorf("tenant-api refuses the expires: %v, /metrics' check %v", a, b)
			}
		})
	}
}

// #4263: the expiry event and the write check follow the resolver: a line
// that gives way emits threshold_expired and is accepted; one with nothing
// beneath emits nothing and its `expires:` is refused.
func TestExpiry_EventAndWriteCheckFollowTheResolver(t *testing.T) {
	t.Parallel()
	for _, c := range lapseCases() {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			cfg := loadTree(t, c.files(false))
			nothing := c.beneath == "" || (c.beneath == "root-disable" && !RootDisableText(c.key, "disable"))
			emitted := false
			for _, e := range cfg.ResolveThresholdExpiriesAt(expiryAt) {
				if e.Tenant == "tx" && e.Expired {
					emitted = true
				}
			}
			refused := false
			for _, line := range cfg.ValidateTenantKeys().Errors {
				if strings.Contains(line, "tenant=tx") && strings.Contains(line, "`expires:`") {
					refused = true
				}
			}
			if emitted == nothing {
				t.Errorf("expiry event emitted=%v, want %v (nothing beneath: %v)", emitted, !nothing, nothing)
			}
			if refused != nothing {
				t.Errorf("write check refused=%v, want %v (nothing beneath: %v)", refused, nothing, nothing)
			}
		})
	}
}

// loadTreeAt is loadTree answering for at (LoadDirAt).
func loadTreeAt(t *testing.T, files map[string]string, at time.Time) *ThresholdConfig {
	t.Helper()
	cfg, _, err := LoadDirAt(writeUndeliverableTree(t, files), log.New(io.Discard, "", 0), at)
	if err != nil {
		t.Fatalf("LoadDirAt: %v", err)
	}
	return cfg
}

// #4263: a value it gives way to may expire in turn — each instant is the
// merge of what has not expired by then, the config holds until the next
// `expires:` (ValidUntil), and the value now served names its own expiry.
func TestExpiry_ChainOfExpiries(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  " + k + ": 50\n",
		"_platform.yaml": "tenants:\n  tx:\n    " + k + ":\n      default: \"70\"\n      expires: \"2040-01-01T00:00:00Z\"\n",
		"t.yaml":         "tenants:\n  tx:\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	}
	year := func(y int) time.Time { return time.Date(y, 1, 1, 0, 0, 0, 0, time.UTC) }
	for y, want := range map[int]string{2019: "5", 2030: "70", 2041: "50"} {
		cfg := loadTreeAt(t, files, year(y))
		if got := strings.Join(offRows(cfg, "tx", "redis", year(y)), "|"); got != k+" warning "+want {
			t.Errorf("%d: %s, want %s", y, got, want)
		}
	}
	cfg := loadTreeAt(t, files, year(2030))
	if until, ok := cfg.ValidUntil(); !ok || !until.Equal(year(2040)) {
		t.Errorf("ValidUntil at 2030 = %v %v, want 2040 (the platform entry's expires:)", until, ok)
	}
	var named []string
	for _, e := range cfg.ResolveThresholdExpiriesAt(year(2030)) {
		named = append(named, fmt.Sprintf("%s:%v", e.Expires.Format("2006"), e.Expired))
	}
	sort.Strings(named)
	if got := strings.Join(named, ","); got != "2020:true,2040:false" {
		t.Errorf("expiry events at 2030 = %s, want the tenant's (expired) and the platform entry's now served (2020:true,2040:false)", got)
	}
}

// #4263: AtMinuteOfDay and ScheduleCuts read the schedule of the value an
// expired one gave way to — it is the merged value.
func TestExpiry_TheValueGivenWayToIsAnOrdinaryValue(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  " + k + ": 50\n",
		"_platform.yaml": "tenants:\n  tx:\n    " + k + ":\n      default: \"70\"\n      overrides:\n        - window: \"00:00-12:00\"\n          value: \"80\"\n",
		"t.yaml":         "tenants:\n  tx:\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	if cuts := fmt.Sprint(cfg.ScheduleCuts()); cuts != "[0 720]" {
		t.Errorf("ScheduleCuts = %s, want [0 720] (the platform entry's window)", cuts)
	}
	for minute, want := range map[int]string{60: "80", 13 * 60: "70"} {
		got := offRows(cfg.AtMinuteOfDay(minute), "tx", "redis", time.Date(2030, 1, 1, 18, 0, 0, 0, time.UTC))
		if strings.Join(got, "|") != k+" warning "+want {
			t.Errorf("minute %d: %v, want %s", minute, got, want)
		}
	}
}

// #4263: a value beneath that no reader takes (`abc`) is no value: with no
// root value either, the expired line is served past `expires:`, and its
// `expires:` is refused — as a refused root value is none.
func TestExpiry_ARefusedValueBeneathIsNone(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults: {}\n",
		"_profiles.yaml": "profiles:\n  p:\n    " + k + ": abc\n",
		"t.yaml":         "tenants:\n  tx:\n    _profile: p\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	if got := strings.Join(offRows(cfg, "tx", "redis", expiryAt), "|"); got != k+" warning 5" {
		t.Errorf("rows %s, want the tenant's 5 served past expires:", got)
	}
	if errs := strings.Join(cfg.ValidateTenantKeys().Errors, "\n"); !strings.Contains(errs, "`expires:` on \""+k+"\" is refused") {
		t.Errorf("write check: %s, want the expires: refused", errs)
	}
}

// #4263: InheritedRootDisables reads the merged value: an expired tenant
// value over a profile's number does not hand the tenant the root `disable`.
func TestExpiry_InheritedRootDisablesReadsTheMerge(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: disable\n",
		"_profiles.yaml": "profiles:\n  p:\n    pg_connections: \"70\"\n",
		"t.yaml":         "tenants:\n  tx:\n    _profile: p\n    pg_connections:\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	}
	if got := loadTree(t, files).InheritedRootDisables("tx", expiryAt); len(got) != 0 {
		t.Errorf("InheritedRootDisables = %v, want none (the profile's 70 is served)", got)
	}
}

// expiresRefused reports whether errs refuse an `expires:` of tenant tx.
func expiresRefused(errs []string) bool {
	for _, line := range errs {
		if strings.Contains(line, "tenant=tx") && strings.Contains(line, "`expires:`") {
			return true
		}
	}
	return false
}

// #4263: under two root platform files the tenant-api merge gives way to
// the same file /metrics does — the earlier file's value when the later
// one's has expired too.
func TestExpiry_TenantAPIMergeStacksEveryPlatformFile(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	files := map[string]string{
		"_defaults.yaml": "defaults: {}\n",
		"_a.yaml":        "tenants:\n  tx:\n    " + k + ": \"70\"\n",
		"_b.yaml":        "tenants:\n  tx:\n    " + k + ":\n      default: \"60\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
		"t.yaml":         "tenants:\n  tx:\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	}
	m := MergeTenantWithRootDefaults(writeUndeliverableTree(t, files), "tx", []byte(files["t.yaml"]))
	var api []string
	for _, r := range m.ResolveAt(expiryAt) {
		if r.Component == "redis" {
			api = append(api, r.Component+"_"+r.Metric+" "+r.Severity+" "+strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	metrics := offRows(loadTree(t, files), "tx", "redis", expiryAt)
	if strings.Join(api, "|") != strings.Join(metrics, "|") || strings.Join(metrics, "|") != k+" warning 70" {
		t.Errorf("tenant-api %v, /metrics %v, want both the earlier file's 70", api, metrics)
	}
}

// #4263: a config read past its ValidUntil — between the next `expires:`
// and the rebuild that follows it — keeps answering for the instant it was
// built at: the value it served, not a lapse to the root the merge never
// decided, and no expiry event turning a second time.
func TestExpiry_PastValidUntilTheBuiltInstantStands(t *testing.T) {
	t.Parallel()
	built := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	later := built.Add(2 * time.Hour)
	cfg := loadTreeAt(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  p:\n    mysql_connections:\n      default: \"90\"\n      expires: \"2026-01-01T01:00:00Z\"\n",
		"t.yaml": "tenants:\n  tx:\n    _profile: p\n    mysql_connections:\n      default: \"95\"\n" +
			"      expires: \"2025-12-31T00:00:00Z\"\n",
	}, built)
	if cfg.ValidAt(later) {
		t.Fatal("fixture: the config claims to answer past the profile's expires:")
	}
	if got := strings.Join(offRows(cfg, "tx", "mysql", later), "|"); !strings.Contains(got, "mysql_connections warning 90") {
		t.Errorf("read past ValidUntil: %s, want the profile's 90 the build served (not the root's 80)", got)
	}
	expired := 0
	for _, e := range cfg.ResolveThresholdExpiriesAt(later) {
		if e.Tenant == "tx" && e.Expired {
			expired++
		}
	}
	if expired != 1 {
		t.Errorf("%d expired events for one key past ValidUntil, want 1 (two identical ones fail the whole Gather)", expired)
	}
}

// #4263: DescribedTenants keeps both spellings a tenant writes and serves
// (renamed_shadowed is reported on them), and puts the served spelling in
// place of a written one the merge left out.
func TestExpiry_DescribedTenantsSpellings(t *testing.T) {
	t.Parallel()
	const retired = "mysql_cpu"
	canon, _ := CanonicalKeyFor(retired)
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  " + canon + ": 50\n",
		"_profiles.yaml": "profiles:\n  p:\n    " + canon + ": \"70\"\n",
		"t.yaml": "tenants:\n  tx:\n    " + canon + ": \"9\"\n    " + retired + ": \"8\"\n" +
			"    pg_connections: \"60\"\n  ty:\n    _profile: p\n    " + retired + ":\n      default: \"5\"\n" +
			"      expires: \"2020-01-01T00:00:00Z\"\n    pg_connections: \"61\"\n",
	})
	for i := 0; i < 20; i++ { // map order: the result must not depend on it
		d := cfg.DescribedTenants()
		if _, a := d["tx"][canon]; !a {
			t.Fatalf("tx: %s missing from %v", canon, d["tx"])
		}
		if _, b := d["tx"][retired]; !b {
			t.Fatalf("tx: %s missing from %v (both spellings are written and served)", retired, d["tx"])
		}
		if v, ok := d["ty"][canon]; !ok || v.Default != "70" {
			t.Fatalf("ty: %s = %+v %v, want the profile's 70 served in the expired line's place", canon, v, ok)
		}
		if _, gone := d["ty"][retired]; gone {
			t.Fatalf("ty: the expired %s is described beside the %s it gave way to", retired, canon)
		}
	}
}
