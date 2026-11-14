package main

// served_schedules_differential_test.go — #3808: addSchedules reads each
// tenant alone at its own cuts; the document it gives must be, byte for
// byte, the one the whole-tree reading it replaced gave (every tenant read at
// every tenant's cuts, every piece kept). That reading lives on here, as the
// oracle, reachable from tests only.

import (
	"bytes"
	"fmt"
	"math/rand"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// collidingTree: two tenants whose rows give one series in overlapping
// parts of the day (tenant-a 15:00–17:00, tenant-b 16:00–18:00, so the
// whole tree's Gather names one, both, then the other), each with other
// windows of its own, a tenant that never collides, a profile with a
// schedule, a subtree `_defaults.yaml`, a retired spelling (#1231) and
// time-boxed keys before and past their `expires:`.
var collidingTree = map[string]string{
	"_defaults.yaml": fixtureDims + `defaults:
  mysql_connections: 80
  mysql_threads_running: 30
  redis_memory: 70
  container_cpu: 75
`,
	"_profiles.yaml": `profiles:
  gold:
    container_cpu:
      default: "77"
      overrides:
        - window: "09:00-17:00"
          value: "78"
`,
	"tenant-a.yaml": `tenants:
  tenant-a:
    mysql_connections_critical: 95
    mysql_connections:
      default: "70"
      overrides:
        - window: "15:00-17:00"
          value: "700:critical"
        - window: "01:00-02:00"
          value: "71"
    redis_memory:
      default: "60"
      overrides:
        - window: "16:30-19:00"
          value: disable
      expires: "2026-12-01T00:00:00Z"
`,
	"tenant-b.yaml": `tenants:
  tenant-b:
    _profile: gold
    mysql_connections_critical: 90
    mysql_connections:
      default: "80"
      overrides:
        - window: "16:00-18:00"
          value: "800:critical"
        - window: "22:00-03:00"
          value: "81"
    mysql_cpu:
      default: "40"
      overrides:
        - window: "05:00-06:00"
          value: disable
      expires: "2026-06-01T00:00:00Z"
`,
	"sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n",
	"sub/tenant-c.yaml": `tenants:
  tenant-c:
    _profile: gold
    redis_memory:
      default: "50"
      overrides:
        - window: "16:15-16:45"
          value: "55"
`,
}

// TestServedSchedules_PerTenantMatchesWholeTree (#3808): served-values
// --schedules prints, byte for byte, what the whole-tree reading printed —
// on the fixed trees of the schedule tests and on random trees whose tenants
// collide in their own, overlapping windows — at an --at in every kind of
// part of the day, the colliding ones included (exit 2 for both).
func TestServedSchedules_PerTenantMatchesWholeTree(t *testing.T) {
	t.Parallel()
	trees := map[string]map[string]string{
		"schedule":     scheduleTree,
		"ungatherable": ungatherableTree,
		"colliding":    collidingTree,
	}
	for seed := int64(1); seed <= 40; seed++ {
		trees[fmt.Sprintf("random-%d", seed)] = randomScheduleTree(rand.New(rand.NewSource(seed)))
	}
	ats := []string{scheduleAt, "2026-07-01T15:30:00Z", "2026-07-01T16:20:00Z", "2026-07-01T23:59:00Z", "2026-12-15T05:30:00Z"}
	names := make([]string, 0, len(trees))
	for n := range trees {
		names = append(names, n)
	}
	sort.Strings(names)
	collided := 0
	for _, n := range names {
		dir := filepath.Join(t.TempDir(), "conf.d")
		tree := map[string]string{}
		for k, v := range trees[n] {
			tree["conf.d/"+k] = v
		}
		testutil.WriteTree(t, filepath.Dir(dir), tree)
		for _, at := range ats {
			// #3877: --schedules-only too, whose reading at an --at that
			// cannot be gathered goes on without the served values.
			for _, mode := range []string{"--schedules", "--schedules-only"} {
				args := []string{"--config-dir", dir, "--at", at, mode}
				var newOut, newErr, oldOut, oldErr bytes.Buffer
				newCode := runServedValuesFilled(args, &newOut, &newErr, addSchedules)
				oldCode := runServedValuesFilled(args, &oldOut, &oldErr, addSchedulesWholeTree)
				if newCode != oldCode || !bytes.Equal(newOut.Bytes(), oldOut.Bytes()) {
					t.Fatalf("%s at %s %s: per tenant exit %d, whole tree exit %d; documents differ\nper tenant:\n%s\nwhole tree:\n%s\nstderr:\n%s\n%s",
						n, at, mode, newCode, oldCode, newOut.String(), oldOut.String(), newErr.String(), oldErr.String())
				}
				if newCode != exitOK && newErr.String() != oldErr.String() {
					t.Errorf("%s at %s %s: exit %d, stderr differs:\nper tenant: %s\nwhole tree: %s", n, at, mode, newCode, newErr.String(), oldErr.String())
				}
				if strings.Contains(newOut.String(), "give one series user_threshold{") {
					collided++
				}
			}
		}
	}
	// The comparison must have seen error segments, or it pinned nothing of
	// the one effect that crosses tenants.
	if collided == 0 {
		t.Fatal("no document carried a Gather error segment")
	}
}

// randomScheduleTree is a tree of 2–6 tenants, some under a subtree with its
// own `_defaults.yaml`, some on a profile with a schedule, each writing a few
// keys with random windows — values that collide with the tenant's
// `_critical` key (`N:critical`), `disable`, a retired spelling, and
// `expires:` before or past the --at dates.
func randomScheduleTree(r *rand.Rand) map[string]string {
	windows := []string{"01:00-09:00", "22:00-06:00", "05:00-05:01", "12:30-13:00", "15:00-17:00",
		"16:00-18:00", "16:20-16:40", "00:00-23:59", "23:00-01:00"}
	values := []string{"70", "71", "disable", "700:critical", "60:critical", "95"}
	keys := []string{"mysql_connections", "redis_memory", "mysql_cpu", "mysql_threads_running", "container_cpu"}
	pick := func(xs []string) string { return xs[r.Intn(len(xs))] }
	files := map[string]string{
		"_defaults.yaml": fixtureDims + "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n" +
			"  redis_memory: 70\n  container_cpu: 75\n",
		"_profiles.yaml": "profiles:\n  gold:\n    container_cpu:\n      default: \"77\"\n      overrides:\n" +
			"        - window: \"" + pick(windows) + "\"\n          value: \"" + pick(values) + "\"\n",
		"sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n",
	}
	for i := 0; i < 2+r.Intn(5); i++ {
		tn := fmt.Sprintf("t%d", i)
		var b strings.Builder
		b.WriteString("tenants:\n  " + tn + ":\n")
		if r.Intn(2) == 0 {
			b.WriteString("    _profile: gold\n")
		}
		if r.Intn(2) == 0 {
			b.WriteString("    mysql_connections_critical: 95\n")
		}
		written := map[string]bool{}
		for j := 0; j < 1+r.Intn(3); j++ {
			k := pick(keys)
			if written[k] {
				continue
			}
			written[k] = true
			b.WriteString("    " + k + ":\n      default: \"" + pick(values) + "\"\n      overrides:\n")
			for w := 0; w < 1+r.Intn(2); w++ {
				b.WriteString("        - window: \"" + pick(windows) + "\"\n          value: \"" + pick(values) + "\"\n")
			}
			if r.Intn(3) == 0 {
				b.WriteString("      expires: \"" + pick([]string{"2026-06-01T00:00:00Z", "2026-12-01T00:00:00Z"}) + "\"\n")
			}
		}
		path := tn + ".yaml"
		if r.Intn(2) == 0 {
			path = "sub/" + path
		}
		files[path] = b.String()
	}
	return files
}

// addSchedulesWholeTree is addSchedules as it was before #3808, verbatim but
// for its name: the whole tree read at every cut of the tree.
func addSchedulesWholeTree(cfg *config.ThresholdConfig, at time.Time, tenants map[string]servedTenantValues,
	expiries []config.ResolvedThresholdExpiry, name keyNamer, instantGathered bool,
) error {
	cuts := cfg.ScheduleCuts()
	ends := make([]int, len(cuts))
	for i := range cuts {
		ends[i] = config.MinutesPerDay
		if i+1 < len(cuts) {
			ends[i] = cuts[i+1]
		}
	}
	// The piece that holds `at` is the reading Values came from.
	utc := at.UTC()
	cur := sort.SearchInts(cuts, utc.Hour()*60+utc.Minute()+1) - 1

	passes := make([]map[string]map[string]reading, len(cuts))
	gatherErrs := make([]string, len(cuts))
	for i, m := range cuts {
		r, gerr, err := readingsAt(cfg.AtMinuteOfDay(m), at, name)
		if err == nil && gerr != "" && i == cur && instantGathered {
			err = fmt.Errorf("internal: the schedule reading at --at cannot be gathered, the served values could: %s", gerr)
		}
		if err == nil && gerr == "" && i == cur && !instantGathered {
			err = fmt.Errorf("internal: the served values at --at cannot be gathered, the schedule reading at --at could")
		}
		if err != nil {
			return fmt.Errorf("from %s to %s UTC: %w", hhmm(m), hhmm(ends[i]), err)
		}
		passes[i], gatherErrs[i] = r, gerr
	}

	for tenant, tv := range tenants {
		if !instantGathered {
			break
		}
		if err := sameAsValues(tenant, tv, passes[cur][tenant]); err != nil {
			return err
		}
	}

	expiryOf := map[string]map[string]config.ResolvedThresholdExpiry{}
	for _, te := range expiries {
		canon, _ := config.CanonicalKeyFor(te.MetricKey)
		if expiryOf[te.Tenant] == nil {
			expiryOf[te.Tenant] = map[string]config.ResolvedThresholdExpiry{}
		}
		if prev, dup := expiryOf[te.Tenant][canon]; dup {
			return fmt.Errorf("internal: tenant %s: keys %q and %q both time-box %q", te.Tenant, prev.MetricKey, te.MetricKey, canon)
		}
		expiryOf[te.Tenant][canon] = te
	}

	for tenant, tv := range tenants {
		schedules := map[string]servedSchedule{}
		keys := map[string]bool{}
		for _, p := range passes {
			for k := range p[tenant] {
				keys[k] = true
			}
		}
		for k := range expiryOf[tenant] {
			keys[k] = true
		}
		// #3140: why a segment serves no row, read off the key's written
		// value at the segment's start (nullReason). k is the served,
		// canonical name, so a retired spelling is found under it; when the
		// tenant writes both, the canonical one is read (keys' shadowed).
		writtenOf := map[string]string{}
		for wk := range cfg.Tenants[tenant] {
			canon, isAlias := config.CanonicalKeyFor(wk)
			if _, canonWritten := cfg.Tenants[tenant][canon]; isAlias && canonWritten {
				continue
			}
			writtenOf[name(tenant, canon)] = wk
		}
		recorded := recordedReasons(cfg, tenant, at)
		view := cfg.FamilyView(tenant)
		for k := range keys {
			why := func(minute int) string {
				return nullReason(cfg, tenant, k, writtenOf[k], at, minute, recorded, view)
			}
			sch := servedSchedule{Segments: daySegments(cuts, ends, passes, gatherErrs, tenant, k, why)}
			if te, ok := expiryOf[tenant][k]; ok {
				sv, written := cfg.Tenants[tenant][te.MetricKey]
				if !written || sv.Expiry == nil {
					return fmt.Errorf("internal: tenant %s: the expiry of %q has no `expires:` in the merged config", tenant, te.MetricKey)
				}
				expired := te.Expired
				sch.Expires = sv.Expiry.Expires
				sch.Expired = &expired
			}
			schedules[k] = sch
		}
		tv.Schedules = &schedules
		tenants[tenant] = tv
	}
	return nil
}
