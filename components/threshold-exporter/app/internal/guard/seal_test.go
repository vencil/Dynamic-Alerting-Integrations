package guard

// seal_test.go — whatever kind a finding carries, a report writes only
// registered kinds (#2904; findingkind's package comment).
//
// The kinds are forged with reflect the way review round 6 did (no unsafe
// import): a regression test for the output, not a rule against the trick.

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// forgeKind writes name into a Kind's unexported field through reflect.
func forgeKind(name string) findingkind.Kind {
	var k findingkind.Kind
	f := reflect.ValueOf(&k).Elem().Field(0)
	reflect.NewAt(f.Type(), f.Addr().UnsafePointer()).Elem().SetString(name)
	return k
}

// A kind forged under a registered name is that kind: equal to the var and
// registered. What can go wrong there is a finding given the wrong kind, not
// an unregistered one.
func TestForgedKind_RegisteredNameIsThatKind(t *testing.T) {
	t.Parallel()
	want := findingkind.FindingMissingRequired
	got := forgeKind(want.String())
	if got != want || !got.IsRegistered() {
		t.Errorf("forged %q: equal %v, registered %v", want, got == want, got.IsRegistered())
	}
}

// forgedFindings has one registered finding and two whose kinds are not
// registered (a forged name and the zero Kind), both warnings.
func forgedFindings() []Finding {
	return []Finding{
		{Severity: SeverityError, Kind: findingkind.FindingMissingRequired, TenantID: "b", Field: "cpu", Message: "real"},
		{Severity: SeverityWarn, Kind: forgeKind("not_a_registered_kind"), TenantID: "a", Field: "cpu", Message: "forged"},
		{Severity: SeverityWarn, TenantID: "a", Field: "mem", Message: "no kind"},
	}
}

// forgedReport is forgedFindings as a literal report, not built by
// newGuardReport, with the summary counted as given: what the output's own
// seal is for.
func forgedReport() *GuardReport {
	return &GuardReport{
		Findings: forgedFindings(),
		Summary:  GuardSummary{TotalTenants: 2, Errors: 1, Warnings: 2, PassedTenantCount: 1},
	}
}

const sentinelName = "unregistered_finding_kind"

func TestReportOutput_WritesUnregisteredKindsAsSentinel(t *testing.T) {
	t.Parallel()
	if findingkind.Unregistered().String() != sentinelName {
		t.Fatalf("the sentinel is %q; its name is part of the JSON contract", findingkind.Unregistered())
	}
	wantSummary := GuardSummary{TotalTenants: 2, Errors: 3, Warnings: 0, PassedTenantCount: 0}

	t.Run("json", func(t *testing.T) {
		t.Parallel()
		r := forgedReport()
		b, err := json.Marshal(r)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		var doc struct {
			Findings []struct {
				Severity string `json:"severity"`
				Kind     string `json:"kind"`
			} `json:"findings"`
			Summary GuardSummary `json:"summary"`
		}
		if err := json.Unmarshal(b, &doc); err != nil {
			t.Fatal(err)
		}
		count := map[string]int{}
		for _, f := range doc.Findings {
			if f.Severity != string(SeverityError) {
				t.Errorf("finding %+v is not an error", f)
			}
			count[f.Kind]++
		}
		if count[sentinelName] != 2 || count["missing_required"] != 1 || len(doc.Findings) != 3 {
			t.Errorf("kinds written: %v\n%s", count, b)
		}
		if doc.Summary != wantSummary {
			t.Errorf("summary %+v, want %+v", doc.Summary, wantSummary)
		}
		if r.Findings[1].Kind.IsRegistered() || r.Summary.Warnings != 2 {
			t.Error("marshalling changed the report it was given")
		}
		// A value, not a pointer, goes through the same method.
		if v, err := json.Marshal(*forgedReport()); err != nil || !strings.Contains(string(v), sentinelName) {
			t.Errorf("GuardReport value: %s, %v", v, err)
		}
	})

	t.Run("md", func(t *testing.T) {
		t.Parallel()
		md := forgedReport().Markdown()
		for _, want := range []string{
			"| a | cpu | " + sentinelName + " |",
			"| a | mem | " + sentinelName + " |",
			"| b | cpu | missing_required |",
			"- Errors: **3**",
			"- Warnings: **0**",
		} {
			if !strings.Contains(md, want) {
				t.Errorf("Markdown lacks %q:\n%s", want, md)
			}
		}
		if strings.Contains(md, "not_a_registered_kind") || strings.Contains(md, "### Warnings") {
			t.Errorf("Markdown writes an unregistered kind or keeps it a warning:\n%s", md)
		}
	})

	t.Run("built", func(t *testing.T) {
		t.Parallel()
		in := forgedFindings()
		r := NewGuardReport(in, 2)
		if r.UnregisteredKinds() != 2 || r.Summary != wantSummary {
			t.Errorf("NewGuardReport: %d sealed, summary %+v", r.UnregisteredKinds(), r.Summary)
		}
		if in[1].Kind.IsRegistered() || in[1].Severity != SeverityWarn {
			t.Error("NewGuardReport changed the findings it was given")
		}
		if again := NewGuardReport(r.Findings, 2); again.UnregisteredKinds() != 0 || again.Summary != wantSummary {
			t.Errorf("rebuilding a sealed report sealed %d, summary %+v", again.UnregisteredKinds(), again.Summary)
		}
	})
}
