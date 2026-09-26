package main

// cap_change_test.go — the relaxed-cap notice (#2043 review follow-up).

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// capTrees writes a baseline and a head conf.d with the given root cap lines
// ("" = key absent) and one small tenant, and returns both paths.
func capTrees(t *testing.T, baseCap, headCap string) (base, head string) {
	t.Helper()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"base/conf.d/_defaults.yaml": baseCap + "defaults:\n  m1: 1\n",
		"base/conf.d/tenant-a.yaml":  oneTenant,
		"head/conf.d/_defaults.yaml": headCap + "defaults:\n  m1: 1\n",
		"head/conf.d/tenant-a.yaml":  oneTenant,
	})
	return filepath.Join(tmp, "base", "conf.d"), filepath.Join(tmp, "head", "conf.d")
}

const relaxedMarker = "Per-tenant cardinality cap relaxed by this change"

func TestCapChange_RaisedCapOpensTheReportWithANotice(t *testing.T) {
	t.Parallel()
	base, head := capTrees(t, "", "max_metrics_per_tenant: 2000\n")
	code, stdout, stderr := runOnce(t, "--config-dir", head, "--baseline-config-dir", base)
	if code != exitOK {
		t.Fatalf("exit = %d, want %d — a notice must not change the exit code. stderr=%q", code, exitOK, stderr)
	}
	if !strings.HasPrefix(stdout, "> ⚠️ **"+relaxedMarker+": 500 → 2000**") {
		t.Errorf("report must OPEN with the notice, got: %q", stdout[:min(len(stdout), 200)])
	}
	if !strings.Contains(stderr, "NOTICE: **"+relaxedMarker) {
		t.Errorf("stderr should carry the notice for the CI log: %q", stderr)
	}
}

func TestCapChange_DisablingTheCapIsARelaxation(t *testing.T) {
	t.Parallel()
	base, head := capTrees(t, "max_metrics_per_tenant: 800\n", "max_metrics_per_tenant: -1\n")
	_, stdout, _ := runOnce(t, "--config-dir", head, "--baseline-config-dir", base)
	if !strings.Contains(stdout, relaxedMarker+": 800 → no limit") {
		t.Errorf("a negative cap removes the limit and must be reported: %q", stdout)
	}
}

func TestCapChange_UnchangedOrLoweredCapIsQuiet(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, base, head string }{
		{"unchanged", "max_metrics_per_tenant: 800\n", "max_metrics_per_tenant: 800\n"},
		{"unset both", "", ""},
		{"explicit 500 equals the default", "", "max_metrics_per_tenant: 500\n"},
		{"lowered", "", "max_metrics_per_tenant: 100\n"},
		{"limit added back", "max_metrics_per_tenant: -1\n", "max_metrics_per_tenant: 5000\n"},
	} {
		base, head := capTrees(t, c.base, c.head)
		_, stdout, stderr := runOnce(t, "--config-dir", head, "--baseline-config-dir", base)
		if strings.Contains(stdout, relaxedMarker) || strings.Contains(stderr, "NOTICE") {
			t.Errorf("%s: no notice expected, got stdout=%q stderr=%q", c.name, stdout, stderr)
		}
	}
}

func TestCapChange_NoBaselineMeansNoComparison(t *testing.T) {
	t.Parallel()
	_, head := capTrees(t, "", "max_metrics_per_tenant: 2000\n")
	_, stdout, stderr := runOnce(t, "--config-dir", head)
	if strings.Contains(stdout, relaxedMarker) || strings.Contains(stderr, "NOTICE") {
		t.Errorf("without --baseline-config-dir nothing is compared: %q", stdout)
	}
}

func TestCapChange_UnreadableBaselineIsSaidNotHidden(t *testing.T) {
	t.Parallel()
	base, head := capTrees(t, "max_metrics_per_tenant: lots\n", "max_metrics_per_tenant: 2000\n")
	code, stdout, _ := runOnce(t, "--config-dir", head, "--baseline-config-dir", base)
	if code != exitOK {
		t.Fatalf("exit = %d; an unreadable baseline must not fail the run", code)
	}
	if !strings.Contains(stdout, "Could not compare the per-tenant cardinality cap") {
		t.Errorf("an uncomparable baseline must be stated, not read as 'not raised': %q", stdout)
	}
}

func TestCapChange_JSONCarriesTheNotice(t *testing.T) {
	t.Parallel()
	base, head := capTrees(t, "", "max_metrics_per_tenant: 2000\n")
	_, stdout, stderr := runOnce(t, "--config-dir", head, "--baseline-config-dir", base, "--format", "json")
	var doc struct {
		Notices []string `json:"notices"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("invalid JSON: %v. stderr=%q", err, stderr)
	}
	if len(doc.Notices) != 1 || !strings.Contains(doc.Notices[0], relaxedMarker) {
		t.Errorf("notices = %q, want one relaxed-cap notice", doc.Notices)
	}
}
