package keyverdict

// value_unread (#3591): a reserved value the exporter does not read is an
// error in the tenant's own file and a notice in a layer the tenant
// inherits, and only the judged tenants are named. Tenant ids are fixture
// names (CLAUDE.md #9).

import (
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func valueInput(values ...config.LayerValueProblem) Input {
	return Input{
		Effective: map[string]EffectiveTenant{
			"t1": {Profile: "gold"},
			"t2": {Profile: "gold"},
			"t3": {},
		},
		Values: values,
	}
}

type valueRow struct {
	Severity string
	File     string
	Key      string
	Tenants  []string
	Blocks   bool
}

func valueRows(tr Tree) []valueRow {
	out := []valueRow{}
	for _, f := range tr.Findings {
		if f.Kind == KindValueUnread {
			out = append(out, valueRow{f.Severity, deref(f.File), f.Key, f.Tenants, f.Blocks})
		}
	}
	return out
}

func TestValueFindings_SeverityByLayer(t *testing.T) {
	t.Parallel()
	tr := Judge(valueInput(
		config.LayerValueProblem{File: "t1.yaml", Layer: config.ValueLayerTenant, Owner: "t1", Key: "_silent_mode", Problem: "p"},
		config.LayerValueProblem{File: "_platform.yaml", Layer: config.ValueLayerPlatform, Owner: "t3", Key: "_silent_mode", Problem: "p"},
		config.LayerValueProblem{File: "_defaults.yaml", Layer: config.ValueLayerProfile, Owner: "gold", Key: "_metadata", Problem: "p"},
		// a tenant the verdict does not judge (a --tenant subset, an orphan
		// platform entry): no finding
		config.LayerValueProblem{File: "t9.yaml", Layer: config.ValueLayerTenant, Owner: "t9", Key: "_silent_mode", Problem: "p"},
	))
	want := []valueRow{
		{SeverityError, "t1.yaml", "_silent_mode", []string{"t1"}, true},
		{SeverityNotice, "_defaults.yaml", "_metadata", []string{"t1", "t2"}, false},
		{SeverityNotice, "_platform.yaml", "_silent_mode", []string{"t3"}, false},
	}
	if got := valueRows(tr); !reflect.DeepEqual(got, want) {
		t.Errorf("findings\n%+v\nwant\n%+v", got, want)
	}
	if !tr.Blocks {
		t.Error("the tenant file's unread value does not block")
	}
}

// A profile's or a platform entry's unread value never blocks: the tenants
// cannot fix it in their own files.
func TestValueFindings_InheritedNeverBlocks(t *testing.T) {
	t.Parallel()
	tr := Judge(valueInput(
		config.LayerValueProblem{File: "_platform.yaml", Layer: config.ValueLayerPlatform, Owner: "t1", Key: "_metadata", Problem: "p"},
		config.LayerValueProblem{File: "_defaults.yaml", Layer: config.ValueLayerProfile, Owner: "gold", Key: "_silent_mode", Problem: "p"},
	))
	if tr.Blocks || len(Blocks(tr)) != 0 {
		t.Errorf("blocks %+v", Blocks(tr))
	}
}

// A write that adds an unread value to one tenant of a file another tenant
// of which already has one is a new block: the finding is about the
// tenant's own entry (Own), not the file and key — although effective
// carries no `_metadata` row to say so.
func TestValueFindings_NewOnItsOwnEntry(t *testing.T) {
	t.Parallel()
	p := func(owner string) config.LayerValueProblem {
		return config.LayerValueProblem{File: "team.yaml", Layer: config.ValueLayerTenant, Owner: owner, Key: "_metadata", Problem: "p"}
	}
	before := Judge(valueInput(p("t1")))
	after := Judge(valueInput(p("t1"), p("t2")))
	got := NewBlocks(before, after, func(string) bool { return true })
	if len(got) != 1 || got[0].Tenant != "t2" || !got[0].Own {
		t.Fatalf("new blocks %+v, want t2's own", got)
	}
}
