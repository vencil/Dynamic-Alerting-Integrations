package customalerts

import (
	"strings"
	"testing"
)

// #3370: a custom alert's `threshold:` is read as the exporter reads it —
// the text the file wrote where YAML misstates it — so the portal shows, and
// a save writes back, what the exporter serves. Before, a bare `017` was
// shown as 15 (the exporter serves 17) and a save rewrote it to 15; a bare
// `0x1F` the exporter refuses was shown as 31 and a save made it served.
func TestExtract_ThresholdIsTheExporterReading(t *testing.T) {
	src := "x-a: &a {recipe: threshold, name: m_high, metric: qd, op: \">\", window: 5m, threshold: 0o17}\n" +
		"tenants:\n  t1:\n    _custom_alerts:\n" +
		"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: 017}\n" +
		"      - {recipe: threshold, name: h_high, metric: qd, op: \">\", window: 5m, threshold: 0x1F}\n" +
		"      - {recipe: threshold, name: n_high, metric: qd, op: \">\", window: 5m, threshold: 70}\n" +
		"      - *a\n"
	got, err := Extract(src, "t1")
	if err != nil {
		t.Fatal(err)
	}
	want := []any{"017", "0x1F", 70, "0o17"}
	if len(got) != len(want) {
		t.Fatalf("Extract = %#v", got)
	}
	for i, w := range want {
		if g := got[i]["threshold"]; g != w {
			t.Errorf("element %d threshold = %#v (%T), want %#v", i, g, g, w)
		}
	}
	out, err := MergeCustomAlerts(src, "t1", got)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Extract(out, "t1")
	if err != nil {
		t.Fatal(err)
	}
	for i, w := range want {
		if g := again[i]["threshold"]; g != w {
			t.Errorf("after a save, element %d threshold = %#v, want %#v\n%s", i, g, w, out)
		}
	}
	if strings.Contains(out, "threshold: 15") || strings.Contains(out, "threshold: 31") {
		t.Errorf("a save rewrote the value to YAML's number:\n%s", out)
	}
}
