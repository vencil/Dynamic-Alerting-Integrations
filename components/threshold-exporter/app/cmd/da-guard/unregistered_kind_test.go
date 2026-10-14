package main

// unregistered_kind_test.go — a finding whose kind is not registered still
// reaches the report (#2904).
//
// findingkind.Kind's MarshalJSON refuses the zero Kind, so before the output
// boundary swapped it, one such finding cost the whole JSON report: exit 2,
// nothing on stdout, nothing written to --output; and Markdown printed an
// empty kind column with the finding's own severity. No real check produces
// one today (TestDaGuard_FixtureFindingsHaveRegisteredKinds), so the check
// is wrapped here to add one.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

const unregisteredKindName = "unregistered_finding_kind"

// withZeroKindFinding runs the real check and adds a warn finding with the
// zero Kind on tenant-a, recomputing nothing: the summary it returns is the
// one the real check computed plus one warning, as a check that forgot to
// pick a kind would leave it.
func withZeroKindFinding(in guard.CheckInput) (*guard.GuardReport, error) {
	r, err := guard.CheckDefaultsImpact(in)
	if err != nil {
		return nil, err
	}
	r.Findings = append(r.Findings, guard.Finding{
		Severity: guard.SeverityWarn,
		TenantID: "tenant-a",
		Field:    "cpu",
		Message:  "a finding whose check chose no kind",
	})
	r.Summary.Warnings++
	return r, nil
}

func TestRun_ZeroKindFinding_IsReportedAsUnregistered(t *testing.T) {
	t.Parallel()
	if findingkind.Unregistered().String() != unregisteredKindName {
		t.Fatalf("the sentinel is %q; it is part of the JSON contract (docs/cli-reference.md)",
			findingkind.Unregistered())
	}
	for _, format := range []string{"json", "md"} {
		for _, toFile := range []bool{false, true} {
			name := format + "/stdout"
			if toFile {
				name = format + "/output"
			}
			t.Run(name, func(t *testing.T) {
				t.Parallel()
				tmp := t.TempDir()
				testutil.WriteTree(t, tmp, map[string]string{
					// tenant-b lacks cpu: one real missing_required error, so
					// the test sees the rest of the report survive.
					"conf.d/_defaults.yaml": "defaults:\n  mem: 70\n",
					"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
					"conf.d/tenant-b.yaml":  "tenants:\n  tenant-b:\n    mem: 75\n",
				})
				args := []string{
					"--config-dir", filepath.Join(tmp, "conf.d"),
					"--required-fields", "cpu",
					"--format", format,
				}
				out := filepath.Join(tmp, "report."+format)
				if toFile {
					args = append(args, "--output", out)
				}
				var stdout, stderr bytes.Buffer
				code := runWith(args, &stdout, &stderr, withZeroKindFinding)
				if code != exitFindings {
					t.Fatalf("exit %d, want %d; stderr=%q", code, exitFindings, stderr.String())
				}
				if !strings.Contains(stderr.String(), `1 finding(s) carried no registered kind; reported as "`+unregisteredKindName+`"`) {
					t.Errorf("stderr does not name the swap: %q", stderr.String())
				}
				body := stdout.String()
				if toFile {
					b, err := os.ReadFile(out)
					if err != nil {
						t.Fatalf("--output not written: %v", err)
					}
					body = string(b)
					if stdout.Len() != 0 {
						t.Errorf("report went to stdout as well as --output: %q", stdout.String())
					}
				}
				switch format {
				case "json":
					checkUnregisteredJSON(t, body)
				case "md":
					checkUnregisteredMarkdown(t, body)
				}
			})
		}
	}
}

func checkUnregisteredJSON(t *testing.T, body string) {
	t.Helper()
	var doc struct {
		Report struct {
			Findings []struct {
				Severity string `json:"severity"`
				Kind     string `json:"kind"`
				TenantID string `json:"tenant_id"`
			} `json:"findings"`
			Summary guard.GuardSummary `json:"summary"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(body), &doc); err != nil {
		t.Fatalf("report is not JSON: %v\n%s", err, body)
	}
	kinds := map[string]string{}
	for _, f := range doc.Report.Findings {
		kinds[f.Kind] = f.Severity
	}
	if len(doc.Report.Findings) != 2 || kinds[unregisteredKindName] != "error" ||
		kinds[findingkind.FindingMissingRequired.String()] != "error" {
		t.Errorf("findings: %+v; want missing_required and %s, both errors", doc.Report.Findings, unregisteredKindName)
	}
	want := guard.GuardSummary{TotalTenants: 2, Errors: 2, Warnings: 0, PassedTenantCount: 0}
	if doc.Report.Summary != want {
		t.Errorf("summary %+v, want %+v (the swapped finding is an error now)", doc.Report.Summary, want)
	}
}

func checkUnregisteredMarkdown(t *testing.T, body string) {
	t.Helper()
	errorsAt := strings.Index(body, "### Errors")
	warnsAt := strings.Index(body, "### Warnings")
	row := strings.Index(body, "| tenant-a | cpu | "+unregisteredKindName+" |")
	if errorsAt < 0 || row < errorsAt {
		t.Errorf("the swapped finding is not a row of the Errors table:\n%s", body)
	}
	if warnsAt >= 0 {
		t.Errorf("a Warnings table is left; the swapped finding should be an error:\n%s", body)
	}
	if !strings.Contains(body, "| tenant-b | cpu | missing_required |") {
		t.Errorf("the real finding is missing:\n%s", body)
	}
	if strings.Contains(body, "|  |") {
		t.Errorf("an empty column is left:\n%s", body)
	}
	if !strings.Contains(body, "- Errors: **2**") || !strings.Contains(body, "- Warnings: **0**") {
		t.Errorf("summary does not count the swapped finding as an error:\n%s", body)
	}
}
