package main

// #2065: valuesNotServedCache recomputes only the tenants whose resolver
// inputs moved. The property: after any sequence of reloads (tenant values,
// subtree defaults, profiles, root platform entries, root defaults and
// optional_overrides, tenants added and removed) and of clock moves across
// `expires:`, the cached verdicts equal a recompute of everything.

import (
	"fmt"
	"io"
	"log"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
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
	// where is each tenant's file directory (relative), "" when absent.
	where map[string]string
}

// cacheTenantDirs are the directories a tenant file moves between: under
// one subtree defaults level, under two, and under none.
var cacheTenantDirs = []string{"team", "team/sub", "other"}

func (c *cacheTree) pick(xs []string) string { return xs[c.r.Intn(len(xs))] }

func (c *cacheTree) write(t *testing.T, rel, body string) {
	t.Helper()
	p := filepath.Join(c.dir, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	writeTestYAML(t, p, body)
}

func (c *cacheTree) remove(t *testing.T, rel string) {
	t.Helper()
	if err := os.Remove(filepath.Join(c.dir, rel)); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
}

func (c *cacheTree) tenantBody(id string) string {
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
	return body
}

// tenant (re)writes id's file, in its current directory or a new one: a
// move removes the old file in the same step, so no id is declared twice.
func (c *cacheTree) tenant(t *testing.T, id string) {
	dir := c.where[id]
	if dir == "" || c.r.Intn(4) == 0 {
		if dir != "" {
			c.remove(t, filepath.Join(dir, id+".yaml"))
		}
		dir = c.pick(cacheTenantDirs)
		c.where[id] = dir
	}
	c.write(t, filepath.Join(dir, id+".yaml"), c.tenantBody(id))
}

func (c *cacheTree) root(t *testing.T) {
	body := "defaults:\n  mysql_connections: 80\n"
	if c.r.Intn(2) == 0 {
		body += "  mysql_threads_running: 30\n"
	}
	if c.r.Intn(2) == 0 {
		body += "optional_overrides:\n  - redis_memory\n"
	}
	c.write(t, "_defaults.yaml", body)
}

func (c *cacheTree) subtree(t *testing.T, rel string) {
	switch c.r.Intn(3) {
	case 0:
		c.remove(t, rel)
	case 1:
		c.write(t, rel, "defaults:\n  mysql_threads_running: "+c.pick(cacheTenantValues)+"\n")
	default:
		c.write(t, rel, "defaults:\n  "+c.pick([]string{"mysql_threads_running", retiredCPUKey})+
			":\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n")
	}
}

// clock moves the fake clock forward or back (a clock set back can
// un-expire a value the cache saw expired).
func (c *cacheTree) clock(fc *clockwork.FakeClock) string {
	d := time.Duration(1+c.r.Intn(30)) * time.Hour
	if c.r.Intn(2) == 0 {
		d = -d
	}
	fc.Advance(d)
	return "clock " + d.String()
}

func (c *cacheTree) step(t *testing.T, fc *clockwork.FakeClock) string {
	switch c.r.Intn(10) {
	case 0, 1:
		id := c.pick([]string{"tx", "ty", "tz"})
		c.tenant(t, id)
		return "tenant " + id + " in " + c.where[id]
	case 2:
		rel := c.pick([]string{"team/_defaults.yaml", "team/sub/_defaults.yaml"})
		c.subtree(t, rel)
		return "subtree defaults " + rel
	case 3:
		c.write(t, "_profiles.yaml", "profiles:\n  gold:\n    mysql_threads_running: "+c.pick(cacheTenantValues)+"\n")
		return "profile"
	case 4:
		// entries for any tenant, the ones no file declares included
		body := "tenants:\n"
		for _, id := range []string{"tx", "ty", "tz", "tw", "tq"} {
			if c.r.Intn(2) == 0 {
				body += "  " + id + ":\n    mysql_threads_running: " + c.pick(cacheTenantValues) + "\n"
			}
		}
		if body == "tenants:\n" {
			c.remove(t, "_platform.yaml")
		} else {
			c.write(t, "_platform.yaml", body)
		}
		return "platform"
	case 5:
		c.root(t)
		return "root defaults"
	case 6:
		id := c.pick([]string{"tw", "tv"})
		if c.where[id] != "" && c.r.Intn(2) == 0 {
			c.remove(t, filepath.Join(c.where[id], id+".yaml"))
			c.where[id] = ""
			return "remove " + id
		}
		c.tenant(t, id)
		return "add " + id
	default:
		return c.clock(fc)
	}
}

func TestValuesNotServedCache_MatchesFullRecomputeOverReloads(t *testing.T) {
	t.Parallel()
	var full, partial int
	for seed := int64(1); seed <= 16; seed++ {
		c := &cacheTree{dir: t.TempDir(), r: rand.New(rand.NewSource(seed)), where: map[string]string{}}
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
		for i := 0; i < 50; i++ {
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

// TestValuesNotServedCache_SingleFileModeMatchesFullRecompute: the same
// property for a single-file config, rewritten and reloaded (Load), with the
// clock moving both ways.
func TestValuesNotServedCache_SingleFileModeMatchesFullRecompute(t *testing.T) {
	t.Parallel()
	var partial int
	for seed := int64(1); seed <= 12; seed++ {
		c := &cacheTree{dir: t.TempDir(), r: rand.New(rand.NewSource(seed)), where: map[string]string{}}
		path := filepath.Join(c.dir, "config.yaml")
		bodies := map[string]string{}
		render := func() string {
			b := "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n"
			if c.r.Intn(3) == 0 {
				b += "optional_overrides:\n  - redis_memory\n"
			}
			b += "profiles:\n  gold:\n    mysql_threads_running: " + c.pick(cacheTenantValues) + "\n"
			b += "tenants:\n"
			for _, id := range []string{"tx", "ty", "tz", "tw"} {
				if body, ok := bodies[id]; ok {
					b += strings.TrimPrefix(body, "tenants:\n")
				}
			}
			return b
		}
		for _, id := range []string{"tx", "ty", "tz"} {
			bodies[id] = c.tenantBody(id)
		}
		writeTestYAML(t, path, render())
		fc := clockwork.NewFakeClockAt(cacheStart)
		fresh, _ := freshMetrics(t)
		m := NewConfigManager(path)
		m.SetMetrics(fresh)
		m.SetLogger(log.New(io.Discard, "", 0))
		m.SetClock(fc)
		if err := m.Load(); err != nil {
			t.Fatalf("seed %d: Load: %v", seed, err)
		}
		for i := 0; i < 40; i++ {
			what := "rewrite"
			switch c.r.Intn(4) {
			case 0:
				what = c.clock(fc)
			case 1:
				id := c.pick([]string{"tw", "tz"})
				if _, ok := bodies[id]; ok {
					delete(bodies, id)
				} else {
					bodies[id] = c.tenantBody(id)
				}
			default:
				id := c.pick([]string{"tx", "ty", "tz"})
				bodies[id] = c.tenantBody(id)
			}
			writeTestYAML(t, path, render())
			if err := m.Load(); err != nil {
				t.Fatalf("seed %d step %d: Load: %v", seed, i, err)
			}
			cfg, now := m.GetConfig(), fc.Now()
			got, want := m.valuesNotServedCache.verdicts(cfg, now), cfg.ValuesNotServed(now)
			if (len(got) != 0 || len(want) != 0) && !reflect.DeepEqual(got, want) {
				t.Fatalf("seed %d step %d (%s) at %s:\ncached %v\nfull   %v", seed, i, what, now, got, want)
			}
		}
		partial += m.valuesNotServedCache.tenantPasses
	}
	if partial == 0 {
		t.Error("no per-tenant pass: the cache was never used")
	}
}

// TestValuesNotServedCache_ClockSetBackUnexpires: a value the cache saw
// expired is recorded again once the clock is set back before its expires.
func TestValuesNotServedCache_ClockSetBackUnexpires(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "tx.yaml"),
		"tenants:\n  tx:\n    mysql_connections: {default: \"abc\", expires: \"2026-07-01T06:00:00Z\", reason: r}\n")
	for _, id := range []string{"ty", "tz"} {
		writeTestYAML(t, filepath.Join(dir, id+".yaml"), "tenants:\n  "+id+":\n    mysql_connections: \"70\"\n")
	}
	m, _, _ := newAuditedManager(t, dir)
	fc := clockwork.NewFakeClockAt(cacheStart.Add(10 * time.Hour))
	m.SetClock(fc)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	if got := m.valuesNotServedCache.verdicts(m.GetConfig(), fc.Now()); len(got) != 0 {
		t.Fatalf("expired value recorded: %v", got)
	}
	want := map[string]map[string]string{"tx": {"mysql_connections": "value_unparsed"}}
	if got := m.valuesNotServedCache.verdicts(m.GetConfig(), cacheStart); !reflect.DeepEqual(got, want) {
		t.Errorf("clock set back before expires: %v, want %v", got, want)
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
