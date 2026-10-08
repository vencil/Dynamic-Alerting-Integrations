package main

// #2065: the gate refuses a value /metrics does not serve as written, and its
// value_not_served findings are exactly `da-guard effective`'s not_served
// restricted to the four written-value reasons.

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// valueNotServedTree is one tree holding every shape the finding covers,
// beside healthy controls, under tenant fixture names.
func valueNotServedTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n",
		"team/_defaults.yaml": "defaults:\n  mysql_threads_running:\n    default: \"33\"\n" +
			"    overrides: \"01:00-09:00\"\n",
		"team/tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"7O:critical\"\n" +
			"    mysql_connections_critical: abc\n",
		"team/ty.yaml": "tenants:\n  ty:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n" +
			"        - window: \"05:00-05:00\"\n          value: \"1000\"\n    mysql_threads_running: \"31\"\n",
		"ok/tz.yaml": "tenants:\n  tz:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n" +
			"        - window: \"22:00-06:00\"\n          value: \"1000\"\n",
	}
}

func TestGuard_ValueNotServedIsRefused(t *testing.T) {
	t.Parallel()
	code, fs := guardFindingsOf(t, valueNotServedTree())
	if code != exitFindings {
		t.Errorf("exit = %d, want %d", code, exitFindings)
	}
	got := map[string]string{}
	for _, f := range fs {
		if f.Kind != guard.FindingValueNotServed {
			t.Errorf("unexpected finding %+v", f)
			continue
		}
		if f.Severity != guard.SeverityError {
			t.Errorf("severity %s, want error: %+v", f.Severity, f)
		}
		got[f.TenantID+"/"+f.Field] = strings.SplitN(f.Message, ":", 2)[0]
	}
	want := map[string]string{
		"tx/mysql_connections":          "value_unparsed",
		"tx/mysql_connections_critical": "value_unparsed_dropped",
		"tx/mysql_threads_running":      "value_rejected",
		"ty/mysql_connections":          "window_invalid",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("value_not_served = %v, want %v", got, want)
	}
}

// Parity: the gate's findings are `da-guard effective`'s not_served of the
// same tree, restricted to the reasons the finding covers — one record, two
// readers.
func TestGuard_ValueNotServedMatchesEffective(t *testing.T) {
	t.Parallel()
	files := valueNotServedTree()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	code, doc, stderr := runEffectiveOn(t, filepath.Join(tmp, "conf.d"))
	if code != exitOK {
		t.Fatalf("effective: exit %d: %s", code, stderr)
	}
	var want []string
	for id, raw := range doc.Tenants {
		var ec struct {
			NotServed map[string]config.NotServedKey `json:"not_served"`
		}
		if err := json.Unmarshal(raw, &ec); err != nil {
			t.Fatalf("tenant %s: %v", id, err)
		}
		for k, ns := range ec.NotServed {
			if guard.IsValueNotServedReason(k, ns.Reason) {
				want = append(want, id+"/"+k+"/"+ns.Reason)
			}
		}
	}
	_, fs := guardFindingsOf(t, files)
	var got []string
	for _, f := range fs {
		if f.Kind == guard.FindingValueNotServed {
			got = append(got, f.TenantID+"/"+f.Field+"/"+strings.SplitN(f.Message, ":", 2)[0])
		}
	}
	sort.Strings(want)
	sort.Strings(got)
	if len(want) == 0 || !reflect.DeepEqual(got, want) {
		t.Errorf("gate %v, effective %v; want the same non-empty set", got, want)
	}
}

// Controls: a valid schedule, a severity suffix and `disable` are served as
// written; a tenant outside --scope is not reported.
func TestGuard_ValueNotServedControls(t *testing.T) {
	t.Parallel()
	ok := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a/tx.yaml": "tenants:\n  tx:\n    mysql_connections:\n      default: \"70:critical\"\n      overrides:\n" +
			"        - window: \"22:00-06:00\"\n          value: disable\n",
		"b/ty.yaml": "tenants:\n  ty:\n    mysql_connections: abc\n",
	}
	if code, fs := guardFindingsOf(t, ok, "--scope", "a"); code != exitOK || len(fs) != 0 {
		t.Errorf("--scope a: exit %d, findings %+v; want 0 and none", code, fs)
	}
	if code, fs := guardFindingsOf(t, ok); code != exitFindings || len(fs) != 1 || fs[0].TenantID != "ty" {
		t.Errorf("whole tree: exit %d, findings %+v; want 1 and one for ty", code, fs)
	}
}
