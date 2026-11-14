package keyverdict

// schedule_unread (#3834): a reserved key written with a schedule its
// readers ignore is judged as an unread value is — an error in the tenant's
// own file, a notice in a layer the tenant inherits — under its own kind,
// and a write that changes the schedule makes a new block. Tenant ids are
// fixture names (CLAUDE.md #9).

import (
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func scheduleProblem(file, layer, owner, key, reading string) config.LayerValueProblem {
	return config.LayerValueProblem{File: file, Layer: layer, Owner: owner, Key: key,
		Problem: "p", Reading: reading, Schedule: true}
}

func scheduleRows(tr Tree) []valueRow {
	out := []valueRow{}
	for _, f := range tr.Findings {
		if f.Kind == KindScheduleUnread {
			out = append(out, valueRow{f.Severity, deref(f.File), f.Key, f.Tenants, f.Blocks})
		}
	}
	return out
}

func TestScheduleFindings_SeverityByLayer(t *testing.T) {
	t.Parallel()
	in := valueInput()
	in.Schedules = []config.LayerValueProblem{
		scheduleProblem("t1.yaml", config.ValueLayerTenant, "t1", "_severity_dedup", "r"),
		scheduleProblem("_platform.yaml", config.ValueLayerPlatform, "t3", "_silent_mode", "r"),
		scheduleProblem("_defaults.yaml", config.ValueLayerProfile, "gold", "_silent_mode", "r"),
		scheduleProblem("t9.yaml", config.ValueLayerTenant, "t9", "_silent_mode", "r"), // not judged
	}
	tr := Judge(in)
	want := []valueRow{
		{SeverityError, "t1.yaml", "_severity_dedup", []string{"t1"}, true},
		{SeverityNotice, "_defaults.yaml", "_silent_mode", []string{"t1", "t2"}, false},
		{SeverityNotice, "_platform.yaml", "_silent_mode", []string{"t3"}, false},
	}
	if got := scheduleRows(tr); !reflect.DeepEqual(got, want) {
		t.Errorf("findings\n%+v\nwant\n%+v", got, want)
	}
	if got := valueRows(tr); len(got) != 0 {
		t.Errorf("a schedule read as value_unread: %+v", got)
	}
	if !tr.Blocks {
		t.Error("the tenant file's schedule does not block")
	}
}

// The schedule is compared as written (the reading LayerScheduleProblems
// gives): moving a window is a new block, leaving it as it was is not.
func TestScheduleFindings_ChangedScheduleIsNew(t *testing.T) {
	t.Parallel()
	judge := func(reading string) Tree {
		in := valueInput()
		in.Schedules = []config.LayerValueProblem{
			scheduleProblem("t1.yaml", config.ValueLayerTenant, "t1", "_silent_mode", reading)}
		return Judge(in)
	}
	all := func(string) bool { return true }
	if got := NewBlocks(judge("w1"), judge("w1"), all); len(got) != 0 {
		t.Errorf("unchanged schedule: new blocks %+v", got)
	}
	if got := NewBlocks(judge("w1"), judge("w2"), all); len(got) != 1 || got[0].Kind != KindScheduleUnread {
		t.Errorf("moved window: new blocks %+v, want one schedule_unread", got)
	}
}
