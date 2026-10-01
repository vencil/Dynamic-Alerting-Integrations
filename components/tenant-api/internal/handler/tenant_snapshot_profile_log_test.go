package handler

// #2513: loading the tenant snapshot (cold, TTL expiry, or Invalidate after
// a write) runs config.LoadDir with a nil logger; ApplyProfiles' WARN for a
// tenant electing an unknown profile must not reach the process log on each
// reload. The same load handed a logger still writes it (the control below;
// the exporter's own reading is pinned in package main,
// TestLoad_UnknownProfileWarnStillReachesTheProcessLog).
//
// ⛔ NOT t.Parallel(): swaps the process-global log output. The cleanup is
// an idempotent reset to os.Stderr, never a save-then-restore.

import (
	"bytes"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestSnapshotReloadWritesNoProfileLog(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		// Both WARN classes of the profile expansion: tx elects an unknown
		// profile; ty elects one that fills a declared-without-value key.
		"_defaults.yaml": "defaults:\n  m1_x: 1\noptional_overrides: [m2_y]\n",
		"_profiles.yaml": "profiles:\n  real-profile:\n    m1_x: 5\n    m2_y: \"2\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: no-such-profile\n",
		"ty.yaml":        "tenants:\n  ty:\n    _profile: real-profile\n",
	})
	d := &Deps{ConfigDir: configDir, RBAC: newRBACManager(t, ""), SearchCache: NewTenantSnapshotCache()}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })

	list := func() {
		t.Helper()
		w := httptest.NewRecorder()
		ListTenants(d)(w, httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET /api/v1/tenants = %d: %s", w.Code, w.Body.String())
		}
	}
	// Each cell must actually load a new snapshot; a cache hit would pass
	// the log check without exercising LoadDir.
	cached := func() *tenantSnapshot {
		d.SearchCache.mu.Lock()
		defer d.SearchCache.mu.Unlock()
		return d.SearchCache.cached
	}
	var prev *tenantSnapshot
	for _, cell := range []struct {
		name   string
		before func()
	}{
		{"cold load", func() {}},
		{"reload after Invalidate (a write)", d.SearchCache.Invalidate},
		{"reload after TTL expiry", func() {
			d.SearchCache.mu.Lock()
			d.SearchCache.ttl = 0 // every cached snapshot is expired
			d.SearchCache.mu.Unlock()
		}},
	} {
		buf.Reset()
		cell.before()
		list()
		if cur := cached(); cur == nil || cur == prev {
			t.Fatalf("%s: precondition: no new snapshot was loaded", cell.name)
		} else {
			prev = cur
		}
		if buf.Len() != 0 {
			t.Errorf("%s wrote to the process log:\n%s", cell.name, buf.String())
		}
	}
	buf.Reset()

	// Control: the same tree trips the WARN when the load has a logger, so
	// the silence above is the nil logger's, not a tree that trips nothing.
	var ctl bytes.Buffer
	if _, _, err := cfg.LoadDir(configDir, log.New(&ctl, "", 0)); err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(ctl.String(), `WARN: tenant=tx references unknown profile "no-such-profile", ignoring`); n != 1 {
		t.Errorf("control: LoadDir with a logger wrote the unknown-profile WARN %d times, want 1:\n%s", n, ctl.String())
	}
	if n := strings.Count(ctl.String(), `WARN: profile "real-profile" supplies "m2_y", but that key is declared without a platform value`); n != 1 {
		t.Errorf("control: LoadDir with a logger wrote the declared-key WARN %d times, want 1:\n%s", n, ctl.String())
	}
	if buf.Len() != 0 {
		t.Errorf("control: LoadDir with a logger also wrote to the process log:\n%s", buf.String())
	}
}
