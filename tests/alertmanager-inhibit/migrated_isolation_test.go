package aminhibit

// #4355: a migrated alert (migrate-rule's copy of a legacy rule, labelled
// da_migrated="true") reaches no platform route but its own and mutes no
// alert. Judged by Alertmanager's OWN dispatch.Route.Match and
// inhibit.Inhibitor on the generator's real output
// (testdata/generated_migrated_alertmanager.yaml, written and drift-checked by
// tests/ops/test_generated_migrated_isolation.py). The probes are derived from
// that output — each platform route's equality matchers, each inhibit rule's
// source — so a platform route or base inhibit rule added later is covered
// without editing a list here.

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/alertmanager/config"
	"github.com/prometheus/alertmanager/config/common"
	"github.com/prometheus/alertmanager/dispatch"
	"github.com/prometheus/alertmanager/eventrecorder"
	"github.com/prometheus/alertmanager/inhibit"
	"github.com/prometheus/alertmanager/pkg/labels"
	"github.com/prometheus/alertmanager/provider/mem"
	"github.com/prometheus/alertmanager/types"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/common/model"
)

const (
	generatedMigratedFixture = "tests/alertmanager-inhibit/testdata/generated_migrated_alertmanager.yaml"
	migratedLabel            = "da_migrated" // _grar_routes.MIGRATED_LABEL
	migratedValue            = "true"        // _grar_routes.MIGRATED_VALUE
	migratedShadowReceiver   = "migrated-alerts-shadow"
	shadowTenant             = "ta" // the fixture's tenants (dev-rules #9)
	activeTenant             = "tb"
)

func loadGeneratedMigrated(t *testing.T) *config.Config {
	t.Helper()
	b, err := os.ReadFile(repoRoot(generatedMigratedFixture))
	if err != nil {
		t.Fatalf("read %s: %v", generatedMigratedFixture, err)
	}
	cfg, err := config.Load(string(b))
	if err != nil {
		t.Fatalf("%s: Alertmanager refuses it: %v", generatedMigratedFixture, err)
	}
	return cfg
}

// platformRoutes: the top-level routes up to and including the migrated
// shadow route (the order the generator pins and the Python side asserts).
func platformRoutes(t *testing.T, root *dispatch.Route) []*dispatch.Route {
	t.Helper()
	for i, r := range root.Routes {
		if r.RouteOpts.Receiver == migratedShadowReceiver {
			return root.Routes[:i+1]
		}
	}
	t.Fatalf("%s: no %s route", generatedMigratedFixture, migratedShadowReceiver)
	return nil
}

// probe: a migrated alert shaped like route r — its non-empty equality
// matchers — of *tenant*.
func probe(r *dispatch.Route, tenant string) model.LabelSet {
	ls := model.LabelSet{"alertname": "MigratedProbe", "severity": "critical"}
	for _, m := range r.Matchers {
		if m.Type == labels.MatchEqual && m.Value != "" {
			ls[model.LabelName(m.Name)] = model.LabelValue(m.Value)
		}
	}
	ls[migratedLabel] = migratedValue
	ls["tenant"] = model.LabelValue(tenant)
	return ls
}

func under(r, platform *dispatch.Route) bool {
	return r == platform || strings.HasPrefix(r.Key(), platform.Key()+"/")
}

func TestGeneratedMigratedRouting(t *testing.T) {
	root := dispatch.NewRoute(loadGeneratedMigrated(t).Route, nil)
	platform := platformRoutes(t, root)
	shadow := platform[len(platform)-1]
	for i, p := range platform {
		ls := probe(p, shadowTenant)
		got := root.Match(ls)
		if len(got) != 1 || got[0] != shadow {
			var names []string
			for _, r := range got {
				names = append(names, r.RouteOpts.Receiver+" "+r.Key())
			}
			t.Errorf("platform route %d: shadow tenant's migrated alert %v reaches %v, want only %s",
				i, ls, names, migratedShadowReceiver)
		}
		ls = probe(p, activeTenant)
		for _, r := range root.Match(ls) {
			for j, q := range platform {
				if under(r, q) {
					t.Errorf("platform route %d: cut-over tenant's migrated alert %v is taken by "+
						"platform route %d (%s %s)", i, ls, j, r.RouteOpts.Receiver, r.Key())
				}
			}
		}
		// control: without the mark the probe is that route's own alert
		if p == shadow {
			continue
		}
		plain := probe(p, shadowTenant)
		delete(plain, migratedLabel)
		if got := root.Match(plain); len(got) == 0 || !under(got[0], p) {
			t.Errorf("platform route %d: the control probe %v does not reach it", i, plain)
		}
	}
}

// sourceOf: the alert an inhibit rule's source names — its non-empty
// equality matchers, `.+`-style regexes satisfied with "x", of the shadow tenant.
func sourceOf(r common.InhibitRule) model.LabelSet {
	ls := model.LabelSet{"tenant": shadowTenant}
	for _, m := range r.SourceMatchers {
		if m.Type == labels.MatchEqual && m.Value != "" {
			ls[model.LabelName(m.Name)] = model.LabelValue(m.Value)
		}
	}
	for _, m := range r.SourceMatchers {
		if _, set := ls[model.LabelName(m.Name)]; !set && m.Type == labels.MatchRegexp {
			ls[model.LabelName(m.Name)] = "x"
		}
	}
	return ls
}

// targetOf: an alert the rule's target names, carrying the source's equal labels.
func targetOf(r common.InhibitRule, src model.LabelSet) model.LabelSet {
	ls := model.LabelSet{"alertname": "TenantProbe"}
	for _, m := range r.TargetMatchers {
		if m.Type == labels.MatchEqual && m.Value != "" {
			ls[model.LabelName(m.Name)] = model.LabelValue(m.Value)
		}
	}
	for _, m := range r.TargetMatchers {
		if _, set := ls[model.LabelName(m.Name)]; !set && m.Type == labels.MatchRegexp {
			ls[model.LabelName(m.Name)] = "x"
		}
	}
	for _, e := range r.Equal {
		if v, ok := src[model.LabelName(e)]; ok {
			ls[model.LabelName(e)] = v
		}
	}
	return ls
}

// mutedBy: whether Alertmanager's inhibitor mutes *tgt* while *src* fires,
// with *rules*.
func mutedBy(t *testing.T, rules []common.InhibitRule, src, tgt model.LabelSet) bool {
	t.Helper()
	lg := slog.New(slog.NewTextHandler(io.Discard, nil))
	ap, err := mem.NewAlerts(context.Background(), time.Hour, 0, nil, lg,
		eventrecorder.NopRecorder(), prometheus.NewRegistry(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ap.Close()
	ih := inhibit.NewInhibitor(ap, rules, lg, eventrecorder.NopRecorder())
	go ih.Run()
	defer ih.Stop()
	now := time.Now()
	a := &types.Alert{Alert: model.Alert{Labels: src, StartsAt: now.Add(-time.Minute),
		EndsAt: now.Add(time.Hour)}, UpdatedAt: now}
	if err := ap.Put(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	ih.WaitForLoading()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if ih.Mutes(context.Background(), tgt) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestGeneratedMigratedInhibit(t *testing.T) {
	rules := loadGeneratedMigrated(t).InhibitRules
	if len(rules) == 0 {
		t.Fatalf("%s: no inhibit rules", generatedMigratedFixture)
	}
	for i, r := range rules {
		src := sourceOf(r)
		tgt := targetOf(r, src)
		// control first: the plain source mutes the target, so the probe pair
		// is that rule's own
		if !mutedBy(t, rules[i:i+1], src, tgt) {
			t.Errorf("inhibit_rules[%d]: control: %v does not mute %v", i, src, tgt)
			continue
		}
		marked := src.Clone()
		marked[migratedLabel] = migratedValue
		if mutedBy(t, rules, marked, tgt) {
			t.Errorf("inhibit_rules[%d]: a migrated alert %v mutes %v", i, marked, tgt)
		}
	}
}
