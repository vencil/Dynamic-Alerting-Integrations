package keyverdict

// #3502: a read key whose schedule has entries /metrics never serves is a
// dead_entry error on the file its value comes from. Tenant ids are fixture
// names (CLAUDE.md #9).

import (
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

var deadWindow = []config.DeadEntry{{Entry: "overrides[1]", Window: "01:00-03:00", Value: "5",
	Reason: config.DeadEntryWindowCovered}}

// deadInput: each tenant reads key `mysql_connection_usage_percent` (served), with des
// as its dead entries, from layer/file.
func deadInput(layer, file string, des map[string][]config.DeadEntry) Input {
	in := Input{Served: map[string]ServedTenant{}, Effective: map[string]EffectiveTenant{}}
	for tenant, d := range des {
		in.Served[tenant] = ServedTenant{Keys: map[string]ServedKey{"mysql_connection_usage_percent": {Exporter: true, DeadEntries: d}},
			Values: map[string]any{"mysql_connection_usage_percent": 70.0}, Rows: map[string]bool{"mysql_connection_usage_percent": true}}
		in.Effective[tenant] = EffectiveTenant{Config: map[string]any{"mysql_connection_usage_percent": map[string]any{}},
			Sources: map[string]config.KeySource{"mysql_connection_usage_percent": {Layer: layer, File: file}}}
	}
	return in
}

func deadFindings(tree Tree) []Finding {
	var out []Finding
	for _, f := range tree.Findings {
		if f.Kind == KindDeadEntry {
			out = append(out, f)
		}
	}
	return out
}

func TestJudge_DeadEntryIsAnErrorOnTheTenantsFile(t *testing.T) {
	t.Parallel()
	tree := Judge(deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": deadWindow}))
	v := one(t, tree, "t1", "mysql_connection_usage_percent")
	if v.Verdict != Read || !reflect.DeepEqual(v.DeadEntries, deadWindow) {
		t.Fatalf("verdict %+v", v)
	}
	fs := deadFindings(tree)
	if len(fs) != 1 {
		t.Fatalf("dead_entry findings %+v", fs)
	}
	f := fs[0]
	if f.Severity != SeverityError || !f.Blocks || deref(f.File) != "a.yaml" ||
		!reflect.DeepEqual(f.Tenants, []string{"t1"}) || deref(f.Reason) != config.DeadEntryWindowCovered ||
		!reflect.DeepEqual(f.DeadEntries, deadWindow) {
		t.Fatalf("finding %+v", f)
	}
	if !tree.Blocks {
		t.Fatal("a dead entry does not block the tree")
	}
	// A key with no dead entry has none, and its row carries [] (a JSON list).
	clean := Judge(deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": nil}))
	if fs := deadFindings(clean); len(fs) != 0 {
		t.Fatalf("no dead entry, findings %+v", fs)
	}
	if v := one(t, clean, "t1", "mysql_connection_usage_percent"); v.DeadEntries == nil || len(v.DeadEntries) != 0 {
		t.Fatalf("no dead entry: dead_entries %#v, want []", v.DeadEntries)
	}
}

// A profile's schedule reaches every tenant that binds it: one finding on
// the profile file, naming them all.
func TestJudge_DeadEntryInAProfileIsOneFindingOnTheProfile(t *testing.T) {
	t.Parallel()
	tree := Judge(deadInput(config.KeyLayerProfile, "_profiles.yaml",
		map[string][]config.DeadEntry{"t1": deadWindow, "t2": deadWindow}))
	fs := deadFindings(tree)
	if len(fs) != 1 || deref(fs[0].File) != "_profiles.yaml" || !reflect.DeepEqual(fs[0].Tenants, []string{"t1", "t2"}) {
		t.Fatalf("findings %+v", fs)
	}
}

// A root platform file's `tenants:` writes one key per tenant with its own
// value: the same file and key with other entries are other findings.
func TestJudge_DeadEntriesOfTwoPlatformEntriesAreTwoFindings(t *testing.T) {
	t.Parallel()
	other := []config.DeadEntry{{Entry: "default", Value: "70", Reason: config.DeadEntryDefaultCovered}}
	tree := Judge(deadInput(config.KeyLayerPlatform, "_platform.yaml",
		map[string][]config.DeadEntry{"t1": deadWindow, "t2": other}))
	fs := deadFindings(tree)
	if len(fs) != 2 {
		t.Fatalf("findings %+v", fs)
	}
	for _, f := range fs {
		if len(f.Tenants) != 1 {
			t.Fatalf("finding names %v", f.Tenants)
		}
		want := deadWindow
		if f.Tenants[0] == "t2" {
			want = other
		}
		if !reflect.DeepEqual(f.DeadEntries, want) {
			t.Errorf("%s: entries %+v, want %+v", f.Tenants[0], f.DeadEntries, want)
		}
	}
}

// An unread key is the unread error; its dead entries add nothing.
func TestJudge_AnUnreadKeyGetsNoDeadEntryFinding(t *testing.T) {
	t.Parallel()
	in := deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": deadWindow})
	in.Served["t1"] = ServedTenant{Keys: map[string]ServedKey{"mysql_connection_usage_percent": {Reason: "no_row", DeadEntries: deadWindow}}}
	if fs := deadFindings(Judge(in)); len(fs) != 0 {
		t.Fatalf("findings %+v", fs)
	}
}

// The ratchet (decision 6): a write that leaves the dead entries as they
// are written adds no block; one that changes them adds one.
func TestNewBlocks_DeadEntryRatchetsOnTheEntriesAsWritten(t *testing.T) {
	t.Parallel()
	written := func(string) bool { return true }
	before := Judge(deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": deadWindow}))
	if bs := NewBlocks(before, before, written); len(bs) != 0 {
		t.Fatalf("unchanged: new blocks %+v", bs)
	}
	changed := []config.DeadEntry{{Entry: "overrides[1]", Window: "01:00-03:00", Value: "6",
		Reason: config.DeadEntryWindowCovered}}
	after := Judge(deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": changed}))
	if bs := NewBlocks(before, after, written); len(bs) != 1 || bs[0].Kind != KindDeadEntry {
		t.Fatalf("changed: new blocks %+v", bs)
	}
	clean := Judge(deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": nil}))
	if bs := NewBlocks(clean, before, written); len(bs) != 1 {
		t.Fatalf("new: new blocks %+v", bs)
	}
}

// Every row carries dead_entries as a list, the profile_not_filled row a
// profile adds included (#3502 review: it read null, and the Python reader
// refused the whole verdict).
func TestJudge_EveryRowCarriesDeadEntriesAsAList(t *testing.T) {
	t.Parallel()
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{}, NotServed: map[string]config.NotServedKey{
			"kafka_lag": {Reason: config.NotServedProfileNotFilled, File: "_profiles.yaml"}}}},
	})
	if len(tree.Keys["t1"]) == 0 {
		t.Fatal("no row")
	}
	for _, v := range tree.Keys["t1"] {
		if v.DeadEntries == nil {
			t.Errorf("%s: dead_entries is nil (JSON null)", v.Key)
		}
	}
}

// A `disable` that switches nothing off at the instant read (disable_noop)
// leaves the rest of the day served: its dead entries are judged, so the
// finding does not depend on --at (#3502 review).
func TestJudge_DeadEntryOnADisableNoopKey(t *testing.T) {
	t.Parallel()
	in := deadInput(config.KeyLayerTenant, "a.yaml", map[string][]config.DeadEntry{"t1": deadWindow})
	in.Served["t1"] = ServedTenant{Keys: map[string]ServedKey{"mysql_connection_usage_percent": {Reason: ReasonDisabled,
		Disable: DisableNoop, DeadEntries: deadWindow}}}
	if fs := deadFindings(Judge(in)); len(fs) != 1 {
		t.Fatalf("findings %+v", fs)
	}
}
