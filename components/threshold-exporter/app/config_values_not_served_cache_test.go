package main

// #2065: valuesNotServedCache recomputes only the tenants whose resolver
// inputs moved. The property: after any sequence of reloads (tenant values,
// subtree defaults, profiles, root platform entries, root defaults and
// optional_overrides, tenants added and removed) and of clock moves across
// `expires:`, the cached verdicts equal a recompute of everything.

import (
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
)

var cacheStart = time.Date(2026, 7, 1, 1, 30, 0, 0, time.UTC)

// cacheTenantValues are the values a tenant key takes: served, unparsed, a
// window the exporter refuses, a valid schedule, and time-boxed bad values.
var cacheTenantValues = []string{
	`"70"`, `"abc"`, `"7O:critical"`, `disable`,
	"{default: \"70\", overrides: [{window: \"05:00-05:00\", value: \"1\"}]}",
	"{default: \"70\", overrides: [{window: \"02:00-03:00\", value: \"abc\"}]}",
	"{default: \"60\", overrides: [{window: \"22:00-06:00\", value: \"90\"}]}",
	"{default: \"abc\", expires: \"2026-07-01T06:00:00Z\", reason: r}",
	"{default: \"abc\", expires: \"2026-07-03T00:00:00Z\", reason: r}",
	"{default: \"55\", expires: \"2026-07-02T00:00:00Z\", reason: r}",
}

type cacheTree struct {
	dir string
	r   *rand.Rand
}

func (c cacheTree) pick(xs []string) string { return xs[c.r.Intn(len(xs))] }

func (c cacheTree) write(t *testing.T, rel, body string) {
	t.Helper()
	p := filepath.Join(c.dir, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	writeTestYAML(t, p, body)
}

func (c cacheTree) remove(t *testing.T, rel string) {
	t.Helper()
	if err := os.Remove(filepath.Join(c.dir, rel)); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
}

func (c cacheTree) tenant(t *testing.T, id string) {
	body := fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: %s\n", id, c.pick(cacheTenantValues))
	if c.r.Intn(2) == 0 {
		body += "    mysql_connections_critical: " + c.pick(cacheTenantValues) + "\n"
	}
	if c.r.Intn(2) == 0 {
		body += "    redis_memory: " + c.pick(cacheTenantValues) + "\n"
	}
	if c.r.Intn(3) == 0 {
		body += "    _profile: gold\n"
	}
	c.write(t, filepath.Join("team", id+".yaml"), body)
}

func (c cacheTree) root(t *testing.T) {
	body := "defaults:\n  mysql_connections: 80\n"
	if c.r.Intn(2) == 0 {
		body += "  mysql_threads_running: 30\n"
	}
	if c.r.Intn(2) == 0 {
		body += "optional_overrides:\n  - redis_memory\n"
	}
	c.write(t, "_defaults.yaml", body)
}

func (c cacheTree) step(t *testing.T, fc *clockwork.FakeClock) string {
	switch c.r.Intn(8) {
	case 0, 1:
		id := c.pick([]string{"tx", "ty", "tz"})
		c.tenant(t, id)
		return "tenant " + id
	case 2:
		switch c.r.Intn(3) {
		case 0:
			c.remove(t, "team/_defaults.yaml")
		case 1:
			c.write(t, "team/_defaults.yaml", "defaults:\n  mysql_threads_running: "+c.pick(cacheTenantValues)+"\n")
		default:
			c.write(t, "team/_defaults.yaml", "defaults:\n  mysql_threads_running:\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n")
		}
		return "subtree defaults"
	case 3:
		c.write(t, "_profiles.yaml", "profiles:\n  gold:\n    mysql_threads_running: "+c.pick(cacheTenantValues)+"\n")
		return "profile"
	case 4:
		c.write(t, "_platform.yaml", "tenants:\n  tx:\n    mysql_threads_running: "+c.pick(cacheTenantValues)+"\n")
		return "platform"
	case 5:
		c.root(t)
		return "root defaults"
	case 6:
		id := c.pick([]string{"tw", "tv"})
		if c.r.Intn(2) == 0 {
			c.remove(t, filepath.Join("team", id+".yaml"))
			return "remove " + id
		}
		c.tenant(t, id)
		return "add " + id
	default:
		d := time.Duration(1+c.r.Intn(30)) * time.Hour
		fc.Advance(d)
		return "clock +" + d.String()
	}
}

func TestValuesNotServedCache_MatchesFullRecomputeOverReloads(t *testing.T) {
	t.Parallel()
	var full, partial int
	for seed := int64(1); seed <= 12; seed++ {
		c := cacheTree{dir: t.TempDir(), r: rand.New(rand.NewSource(seed))}
		c.root(t)
		c.write(t, "_profiles.yaml", "profiles:\n  gold:\n    mysql_threads_running: \"40\"\n")
		for _, id := range []string{"tx", "ty", "tz"} {
			c.tenant(t, id)
		}
		fc := clockwork.NewFakeClockAt(cacheStart)
		m, _, _ := newAuditedManager(t, c.dir)
		m.SetClock(fc)
		if err := m.Load(); err != nil {
			t.Fatalf("seed %d: Load: %v", seed, err)
		}
		for i := 0; i < 40; i++ {
			what := c.step(t, fc)
			m.tickOnce()
			cfg, now := m.GetConfig(), fc.Now()
			got := m.valuesNotServedCache.verdicts(cfg, now)
			want := cfg.ValuesNotServed(now)
			if len(got) == 0 && len(want) == 0 {
				continue
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("seed %d step %d (%s) at %s:\ncached %v\nfull   %v", seed, i, what, now, got, want)
			}
		}
		full += m.valuesNotServedCache.fullPasses
		partial += m.valuesNotServedCache.tenantPasses
	}
	// The property is only worth something if the cache was actually used.
	if partial == 0 || full == 0 {
		t.Errorf("full passes %d, per-tenant passes %d: want both paths taken", full, partial)
	}
}

// TestValuesNotServedCache_RecomputesOnlyTheChangedTenant: an edit to one
// tenant of many re-resolves that tenant alone.
func TestValuesNotServedCache_RecomputesOnlyTheChangedTenant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	for _, id := range []string{"tx", "ty", "tz"} {
		writeTestYAML(t, filepath.Join(dir, id+".yaml"), "tenants:\n  "+id+":\n    mysql_connections: \"70\"\n")
	}
	m, fresh, _ := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	before := m.valuesNotServedCache.tenantPasses
	writeTestYAML(t, filepath.Join(dir, "ty.yaml"), "tenants:\n  ty:\n    mysql_connections: \"abc\"\n")
	m.tickOnce()
	if got := m.valuesNotServedCache.tenantPasses - before; got != 1 {
		t.Errorf("per-tenant recomputes = %d, want 1", got)
	}
	if v := valuesNotServedGauge(t, fresh)["value_unparsed"]; v != 1 {
		t.Errorf("value_unparsed = %v, want 1", v)
	}
}

// TestValuesNotServedCache_ExpiresOnlyEditIsSeen: moving only an `expires:`
// (same value) re-resolves the tenant — an expired unparseable value is not
// recorded, the same value time-boxed into the future is.
func TestValuesNotServedCache_ExpiresOnlyEditIsSeen(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	tx := filepath.Join(dir, "tx.yaml")
	box := func(expires string) string {
		return "tenants:\n  tx:\n    mysql_connections: {default: \"abc\", expires: \"" + expires + "\", reason: r}\n"
	}
	writeTestYAML(t, tx, box("2026-07-01T00:30:00Z"))
	writeTestYAML(t, filepath.Join(dir, "ty.yaml"), "tenants:\n  ty:\n    mysql_connections: \"70\"\n")
	writeTestYAML(t, filepath.Join(dir, "tz.yaml"), "tenants:\n  tz:\n    mysql_connections: \"70\"\n")
	m, _, _ := newAuditedManager(t, dir)
	m.SetClock(clockwork.NewFakeClockAt(cacheStart))
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	if got := m.valuesNotServedCache.verdicts(m.GetConfig(), cacheStart); len(got) != 0 {
		t.Fatalf("expired value recorded: %v", got)
	}
	writeTestYAML(t, tx, box("2026-07-09T00:30:00Z"))
	m.tickOnce()
	if v := m.GetConfig().Tenants["tx"]["mysql_connections"].Expiry; v == nil || v.Expires != "2026-07-09T00:30:00Z" {
		t.Fatalf("precondition: the reload did not commit the new expires (%+v)", v)
	}
	want := map[string]map[string]string{"tx": {"mysql_connections": "value_unparsed"}}
	if got := m.valuesNotServedCache.verdicts(m.GetConfig(), cacheStart); !reflect.DeepEqual(got, want) {
		t.Errorf("after the expires-only edit: %v, want %v", got, want)
	}
}
