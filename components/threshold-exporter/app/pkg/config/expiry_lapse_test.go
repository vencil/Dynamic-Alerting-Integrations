package config

import (
	"fmt"
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
	// place is where the expiring line is written: "tenant" (s/t.yaml) or
	// "platform" (_platform.yaml's `tenants:` entry).
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
		for _, place := range []string{"tenant", "platform"} {
			for _, b := range []string{"", "root", "root-disable", "platform", "profile", "subtree"} {
				if place == "platform" && b == "platform" {
					continue
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
			want := tenantRows(loadTree(t, files), "tx", expiryAt)
			if strings.Join(got, "|") != strings.Join(want, "|") {
				t.Errorf("tenant-api merge serves\n  %v\n/metrics serves\n  %v", got, want)
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

// #4263: a value it gives way to may expire in turn — the chain is read to
// its end, and the cache edge (ValuesNotServedExpiryEdge) is the next
// `expires:` along it, not only the tenant's own.
func TestExpiry_ChainOfExpiries(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  " + k + ": 50\n",
		"_platform.yaml": "tenants:\n  tx:\n    " + k + ":\n      default: \"70\"\n      expires: \"2040-01-01T00:00:00Z\"\n",
		"t.yaml":         "tenants:\n  tx:\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	at := func(y int) []string {
		return offRows(cfg, "tx", "redis", time.Date(y, 1, 1, 0, 0, 0, 0, time.UTC))
	}
	if got := strings.Join(at(2019), "|"); got != k+" warning 5" {
		t.Errorf("2019: %s, want the tenant's 5", got)
	}
	if got := strings.Join(at(2030), "|"); got != k+" warning 70" {
		t.Errorf("2030: %s, want the platform entry's 70", got)
	}
	if got := strings.Join(at(2041), "|"); got != k+" warning 50" {
		t.Errorf("2041: %s, want the root's 50", got)
	}
	edge, ok := ValuesNotServedExpiryEdge(cfg.Tenants["tx"], time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if !ok || !edge.Equal(time.Date(2040, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("expiry edge at 2030 = %v %v, want 2040 (the platform entry's)", edge, ok)
	}
}

// #4263: AtMinuteOfDay reads the schedule of a value given way to at that
// minute too.
func TestExpiry_AtMinuteOfDayReadsTheChain(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  " + k + ": 50\n",
		"_platform.yaml": "tenants:\n  tx:\n    " + k + ":\n      default: \"70\"\n      overrides:\n        - window: \"00:00-12:00\"\n          value: \"80\"\n",
		"t.yaml":         "tenants:\n  tx:\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	// The window's edges are cuts of the day (ScheduleCuts: da-guard's
	// served schedules split the day there).
	if cuts := fmt.Sprint(cfg.ScheduleCuts()); cuts != "[0 720]" {
		t.Errorf("ScheduleCuts = %s, want [0 720] (the platform entry's window, beneath the tenant's)", cuts)
	}
	for minute, want := range map[int]string{60: "80", 13 * 60: "70"} {
		// An instant whose time of day is not the minute: the fixed copy
		// must not read its schedule at the instant.
		got := offRows(cfg.AtMinuteOfDay(minute), "tx", "redis", time.Date(2030, 1, 1, 18, 0, 0, 0, time.UTC))
		if strings.Join(got, "|") != k+" warning "+want {
			t.Errorf("minute %d: %v, want %s", minute, got, want)
		}
	}
}

// #4263: re-running a fill over an already merged config stacks nothing
// twice — the exporter's incremental reload runs ApplyProfiles on the
// previous merge every time, and a chain that grew per reload would grow
// without bound.
func TestExpiry_RefillStacksNothingTwice(t *testing.T) {
	t.Parallel()
	k := "redis_connected_clients"
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  " + k + ": 50\n",
		"_profiles.yaml": "profiles:\n  p:\n    " + k + ":\n      default: \"70\"\n      expires: \"2040-01-01T00:00:00Z\"\n",
		"t.yaml":         "tenants:\n  tx:\n    _profile: p\n    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	links := func() int {
		n := 0
		lapseChain(cfg.Tenants["tx"][k], func(ScheduledValue) { n++ })
		return n
	}
	want := links()
	if want != 2 {
		t.Fatalf("chain has %d links, want 2 (the tenant's value, then the profile's)", want)
	}
	for i := 0; i < 3; i++ {
		cfg.ApplyProfiles()
	}
	if got := links(); got != want {
		t.Errorf("after re-running ApplyProfiles the chain has %d links, want %d", got, want)
	}
}
